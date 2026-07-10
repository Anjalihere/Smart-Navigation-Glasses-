"""
async_pipeline.py

Reference implementation of an async frame-processing pipeline for
Smart Navigation Glasses.

Problem this solves:
    Camera frames arrive faster than a single synchronous Azure API
    round-trip completes. A naive "call Azure, wait, repeat" loop falls
    behind the live feed and, worse, goes silent on the user the moment
    a single API call times out.

This module shows:
    1. A bounded async queue + worker pool (frames don't block on each other)
    2. Retry-with-backoff on transient failures
    3. Fallback to last-known-good result when retries are exhausted
       (an assistive device should degrade gracefully, never go silent)
    4. Frame-hash-based caching to skip near-duplicate consecutive frames
    5. Per-stage latency instrumentation (p50 / p95)

MockAzureClient stands in for real calls to Azure Cognitive Services
(azure-ai-vision, azure-ai-formrecognizer / OCR, azure-cognitiveservices-speech).
Swap it for the real SDK calls when wiring this into the FastAPI backend —
the pipeline/retry/cache/fallback logic around it does not need to change.

Run directly to see a simulated end-to-end pass with logging:
    python async_pipeline.py
"""

import asyncio
from io import BytesIO
import random
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

try:
    from PIL import Image
    import imagehash
except ImportError:  # pragma: no cover - optional dependency in some environments
    Image = None
    imagehash = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# 1. Data model
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    frame_id: int
    captured_at: float
    image_hash: str  # perceptual hash / dedup key
    payload: bytes = b""  # raw image bytes when available


def compute_image_hash(payload: bytes, fallback: Optional[str] = None) -> str:
    """Compute a perceptual hash from encoded image bytes when Pillow/imagehash are available."""
    if payload and Image is not None and imagehash is not None:
        with Image.open(BytesIO(payload)) as image:
            return str(imagehash.phash(image))
    if fallback is not None:
        return fallback
    if payload:
        return str(hash(payload))
    raise ValueError("payload is required to compute an image hash")


@dataclass
class FrameResult:
    frame_id: int
    stage_timings: dict = field(default_factory=dict)
    ocr_text: Optional[str] = None
    vision_tags: Optional[list] = None
    from_cache: bool = False
    failed: bool = False
    used_fallback: bool = False


# ---------------------------------------------------------------------------
# 2. Mock Azure client — swap for real Azure SDK / REST calls
# ---------------------------------------------------------------------------

class MockAzureClient:
    """
    Simulates network latency + occasional timeouts, standing in for real
    Azure Cognitive Services calls (Vision / OCR / Speech).
    """

    def __init__(self, failure_rate: float = 0.15, base_latency=(0.15, 0.5)):
        self.failure_rate = failure_rate
        self.base_latency = base_latency

    async def call_ocr(self, frame: Frame) -> str:
        await asyncio.sleep(random.uniform(*self.base_latency))
        if random.random() < self.failure_rate:
            raise TimeoutError(f"OCR timeout for frame {frame.frame_id}")
        return f"detected-text-frame-{frame.frame_id}"

    async def call_vision(self, frame: Frame) -> list:
        await asyncio.sleep(random.uniform(*self.base_latency))
        if random.random() < self.failure_rate:
            raise TimeoutError(f"Vision timeout for frame {frame.frame_id}")
        return ["door", "stairs"] if frame.frame_id % 3 == 0 else ["hallway"]


# ---------------------------------------------------------------------------
# 3. Retry with exponential backoff
# ---------------------------------------------------------------------------

async def retry_with_backoff(coro_fn, *args, max_attempts=3, base_delay=0.2, **kwargs):
    """
    Generic retry wrapper for a single async call.
    Retries on TimeoutError with exponential backoff + small jitter.
    Re-raises the last exception once max_attempts is exhausted.
    """
    attempt = 0
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except TimeoutError as e:
            attempt += 1
            if attempt >= max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.05)
            log.warning(f"retry {attempt}/{max_attempts} after '{e}' (sleeping {delay:.2f}s)")
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 4. Frame cache — avoid redundant processing of near-identical frames
# ---------------------------------------------------------------------------

class FrameCache:
    """
    Small LRU-ish cache keyed on a perceptual image hash. In production,
    image_hash would come from something like `imagehash.phash(image)` so
    visually-similar consecutive frames (e.g. user standing still) map to
    the same key and skip a redundant Azure call entirely.
    """

    def __init__(self, maxlen=50):
        self._cache: dict[str, FrameResult] = {}
        self._order = deque(maxlen=maxlen)

    def get(self, image_hash: str) -> Optional[FrameResult]:
        return self._cache.get(image_hash)

    def put(self, image_hash: str, result: FrameResult):
        if image_hash not in self._cache and len(self._order) == self._order.maxlen:
            oldest = self._order.popleft()
            self._cache.pop(oldest, None)
        self._cache[image_hash] = result
        self._order.append(image_hash)


# ---------------------------------------------------------------------------
# 5. Latency tracker
# ---------------------------------------------------------------------------

class LatencyTracker:
    def __init__(self):
        self.samples: list[float] = []

    def record(self, elapsed: float):
        self.samples.append(elapsed)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        s = sorted(self.samples)
        idx = min(int(len(s) * p), len(s) - 1)
        return s[idx]

    def summary(self) -> str:
        if not self.samples:
            return "no samples yet"
        return (f"n={len(self.samples)} "
                f"p50={self.percentile(0.5) * 1000:.0f}ms "
                f"p95={self.percentile(0.95) * 1000:.0f}ms "
                f"max={max(self.samples) * 1000:.0f}ms")

    def as_dict(self) -> dict:
        return {
            "count": len(self.samples),
            "p50_ms": round(self.percentile(0.5) * 1000, 2),
            "p95_ms": round(self.percentile(0.95) * 1000, 2),
            "max_ms": round((max(self.samples) if self.samples else 0.0) * 1000, 2),
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# 6. The pipeline
# ---------------------------------------------------------------------------

class FramePipeline:
    """
    Producer -> asyncio.Queue -> N concurrent workers -> results list.

    Each worker, per frame:
      1. Checks the frame cache (skip re-processing near-duplicate frames)
      2. Fires OCR + Vision calls concurrently
      3. Retries transient failures with backoff
      4. Falls back to the last-known-good result if all retries fail
      5. Records per-stage latency
    """

    def __init__(self, azure_client: MockAzureClient, num_workers: int = 4, queue_size: int = 20):
        self.azure_client = azure_client
        self.num_workers = num_workers
        self.frame_queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.results: list[FrameResult] = []
        self.cache = FrameCache()
        self.latency = LatencyTracker()
        self._last_good_result: Optional[FrameResult] = None
        self._state_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.dedupe_hits = 0
        self.fallbacks = 0
        self.queue_depth_samples: list[int] = []

    @staticmethod
    def _clone_result(source: FrameResult, frame_id: int) -> FrameResult:
        return FrameResult(
            frame_id=frame_id,
            stage_timings=dict(source.stage_timings),
            ocr_text=source.ocr_text,
            vision_tags=list(source.vision_tags) if source.vision_tags is not None else None,
            from_cache=source.from_cache,
            failed=source.failed,
            used_fallback=source.used_fallback,
        )

    def _sample_queue_depth(self):
        depth = self.frame_queue.qsize()
        self.queue_depth_samples.append(depth)

    def _snapshot(self) -> dict:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "dedupe_hits": self.dedupe_hits,
            "fallbacks": self.fallbacks,
            "max_queue_depth": max(self.queue_depth_samples) if self.queue_depth_samples else 0,
            "latency": self.latency.as_dict(),
        }

    async def producer(self, frames: list[Frame]):
        for frame in frames:
            await self.frame_queue.put(frame)
            self._sample_queue_depth()
        for _ in range(self.num_workers):
            await self.frame_queue.put(None)  # sentinel to stop each worker
            self._sample_queue_depth()

    async def worker(self, worker_id: int):
        while True:
            frame = await self.frame_queue.get()
            self._sample_queue_depth()
            if frame is None:
                self.frame_queue.task_done()
                break
            await self._process_frame(worker_id, frame)
            self.frame_queue.task_done()

    async def _process_frame(self, worker_id: int, frame: Frame):
        start = time.monotonic()
        result = FrameResult(frame_id=frame.frame_id)

        async with self._state_lock:
            cached = self.cache.get(frame.image_hash)
            if cached:
                self.cache_hits += 1
                result = self._clone_result(cached, frame.frame_id)
                result.from_cache = True
                result.stage_timings["total"] = time.monotonic() - start
                self.latency.record(result.stage_timings["total"])
                self.results.append(result)
                log.info(f"[worker {worker_id}] frame {frame.frame_id} served from cache")
                return

            inflight = self._inflight.get(frame.image_hash)
            if inflight is None:
                inflight = asyncio.get_running_loop().create_future()
                self._inflight[frame.image_hash] = inflight
                owner = True
                self.cache_misses += 1
            else:
                owner = False
                self.dedupe_hits += 1

        if not owner:
            shared = await inflight
            result = self._clone_result(shared, frame.frame_id)
            result.from_cache = True
            result.stage_timings["total"] = time.monotonic() - start
            self.latency.record(result.stage_timings["total"])
            self.results.append(result)
            log.info(f"[worker {worker_id}] frame {frame.frame_id} reused in-flight result")
            return

        try:
            ocr_task = retry_with_backoff(self.azure_client.call_ocr, frame)
            vision_task = retry_with_backoff(self.azure_client.call_vision, frame)
            ocr_text, vision_tags = await asyncio.gather(ocr_task, vision_task)

            result.ocr_text = ocr_text
            result.vision_tags = vision_tags
            self._last_good_result = result
            self.cache.put(frame.image_hash, result)

        except TimeoutError as e:
            log.error(f"[worker {worker_id}] frame {frame.frame_id} failed after retries: {e}")
            if self._last_good_result:
                # Fallback: reuse last known-good guidance instead of going
                # silent. Important for an assistive device — degraded
                # guidance beats no guidance.
                result.ocr_text = self._last_good_result.ocr_text
                result.vision_tags = self._last_good_result.vision_tags
                result.failed = True
                result.used_fallback = True
                self.fallbacks += 1
                log.warning(f"[worker {worker_id}] frame {frame.frame_id} using stale fallback result")
            else:
                result.failed = True

        elapsed = time.monotonic() - start
        result.stage_timings["total"] = elapsed
        self.latency.record(elapsed)
        self.results.append(result)

        async with self._state_lock:
            self.cache.put(frame.image_hash, self._clone_result(result, frame.frame_id))
            if frame.image_hash in self._inflight:
                future = self._inflight.pop(frame.image_hash)
                if not future.done():
                    future.set_result(self._clone_result(result, frame.frame_id))

    def summary(self) -> dict:
        return {
            "frames": len(self.results),
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "dedupe_hits": self.dedupe_hits,
                "hit_rate": round((self.cache_hits + self.dedupe_hits) / max(self.cache_hits + self.cache_misses + self.dedupe_hits, 1), 3),
            },
            "fallbacks": self.fallbacks,
            "max_queue_depth": max(self.queue_depth_samples) if self.queue_depth_samples else 0,
            "latency": self.latency.as_dict(),
        }

    async def run(self, frames: list[Frame]):
        workers = [asyncio.create_task(self.worker(i)) for i in range(self.num_workers)]
        await self.producer(frames)
        await asyncio.gather(*workers)
        return self.results


# ---------------------------------------------------------------------------
# 7. Demo run
# ---------------------------------------------------------------------------

async def main():
    # Create repeatable demo images so the perceptual hash produces real cache hits.
    from PIL import Image as PILImage, ImageDraw

    def build_demo_payload(group: int) -> bytes:
        image = PILImage.new("RGB", (160, 120), color=(40 + group * 20, 60, 90 + group * 10))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20 + group, 18, 120, 92), outline=(255, 255, 255), width=4)
        draw.text((28, 48), f"{group}", fill=(255, 255, 255))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    frames = [
        Frame(
            frame_id=i,
            captured_at=time.time(),
            image_hash=compute_image_hash(build_demo_payload(i % 8), fallback=f"hash-{i % 8}"),
            payload=build_demo_payload(i % 8),
        )
        for i in range(30)
    ]

    client = MockAzureClient(failure_rate=0.2)
    pipeline = FramePipeline(azure_client=client, num_workers=4)

    t0 = time.monotonic()
    results = await pipeline.run(frames)
    total_time = time.monotonic() - t0

    ok = sum(1 for r in results if not r.failed)
    cached = sum(1 for r in results if r.from_cache)
    failed = sum(1 for r in results if r.failed)

    log.info("---- Pipeline summary ----")
    log.info(f"frames processed: {len(results)} in {total_time:.2f}s")
    log.info(f"success: {ok}, served-from-cache: {cached}, failed-and-fell-back: {failed}")
    log.info(f"latency: {pipeline.latency.summary()}")


if __name__ == "__main__":
    asyncio.run(main())
