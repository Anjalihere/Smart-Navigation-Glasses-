# Smart Navigation Glasses
A real-time assistive navigation system that turns a camera feed into spoken
guidance for visually impaired users. Frames are processed concurrently
through Azure Cognitive Services (OpenAI, Vision, OCR, Translator, Speech) behind a fault-tolerant async
pipeline designed to keep functioning when individual cloud calls fail.

## Demo video
[![Watch the video](<img width="1021" height="680" alt="Screenshot 2026-05-15 at 12 18 18 AM" src="https://github.com/user-attachments/assets/392e57ea-39e4-4307-9270-741edbab92c1" />
)](https://www.youtube.com/watch?v=3keTI96f-Wo)

## Status
| Component | Status |
|---|---|
| Frame capture | Implemented |
| Object/scene vision tagging (`/vision`) | Implemented, requires valid Azure OpenAI config |
| Text detection (`/ocr`) | Implemented, requires valid Azure Document Intelligence config |
| Spatial safety heuristics (`/spatial-safety`) | Implemented, requires valid Azure OpenAI config |
| Query/chat interface (`/chat`) | Implemented via Azure OpenAI, requires valid config |
| Multilingual translation | Implemented via Azure Translator; language set through a UI selector |
| Text-to-speech output | Implemented; frontend uses browser `speechSynthesis`, backend also has Azure Speech SDK routes |
| Speech-to-text input | Implemented in backend via Azure Speech SDK; live mic currently uses browser Web Speech API instead |

The code paths are in place, but the cloud-backed routes depend on
working Azure credentials, endpoints, and network access in your environment.
When those settings are wrong, the API fails clearly instead of pretending
the feature is available.

## Architecture
```
Camera feed
    |
    v
Frame Queue (bounded, backpressure-aware)
    |
    v
Worker Pool (N concurrent async workers)
    |
    +--> frame cache check (skip near-duplicate consecutive frames)
    |
    +--> OCR call    --+
    +--> Vision call  --+-- concurrent, retried with exponential backoff
    |
    v
Result (falls back to last-known-good result if retries are exhausted)
    |
    v
Voice output (implemented via Azure Speech SDK)
    |
    v
Translation layer (Azure Translator, optional per-user language selection)
```

## Why this design
- **Bounded queue + worker pool instead of one blocking call per frame.**
  Camera frames arrive faster than a single Azure round-trip completes; a
  naive synchronous loop falls behind the live feed.
- **Retry with exponential backoff on transient failures.** Cloud API calls
  occasionally time out. A single dropped call shouldn't be treated as fatal.
- **Fallback to the last-known-good result when retries are exhausted,**
  rather than surfacing an error to the user. This is an assistive device —
  degraded guidance beats no guidance.
- **Frame-hash-based caching** to skip near-identical consecutive frames
  (e.g. the user standing still), cutting redundant Azure calls without
  hurting responsiveness.
- **Per-stage latency instrumentation** (p50 / p95 logged), added after
  finding that response time mattered more than raw feature count for an
  interactive, real-time system.

## Features

### Conversational query interface (`/chat`)
Supports a full voice loop, not just typed queries: a wake word ("OK Glass")
triggers listening via the browser's speech recognition, the query (plus
latest vision/OCR context) is sent to Azure OpenAI, and the answer is spoken
back via the browser's `speechSynthesis`. Backend Azure Speech SDK routes for
STT/TTS also exist, separate from this browser-based live path.

### Multilingual support (Azure Translator)
Language is set via a UI selector (`voiceLang`), not detected from speech.
The selector sets the browser's recognition language and sends a
`target_lang` to the backend, which translates `/chat` and OCR read-aloud
output into that language via Azure Translator (source language auto-detected
by Translator, not by STT).

See [`async_pipeline.py`](./async_pipeline.py) for the pipeline reference
implementation (queue, retry/backoff, cache, fallback, latency tracking),
and [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) for how the backend
exposes the demo pipeline endpoint.

The pipeline is exercised through [`/api/pipeline/demo`](./app/backend/main.py),
while the main cloud routes (`/vision`, `/ocr`, `/spatial-safety`, `/chat`)
still call Azure services directly.

## Performance
Local demo benchmark from the async pipeline simulator:
- p50 / p95 end-to-end frame latency: `0.13 ms / 0.21 ms`
- Cache hit rate in a 12-frame demo with a 4-frame duplicate window: `66.7%`
- Failure/fallback rate with `failure_rate=0`: `0%`

## Tech stack
Python, FastAPI, asyncio, Azure Cognitive Services (Vision, Document Intelligence/OCR, Face, Speech, Translator, OpenAI)

## Setup
```bash
git clone https://github.com/Anjalihere/Smart-Navigation-Glasses-.git
cd Smart-Navigation-Glasses-
python -m venv venv
source venv/bin/activate  # venv/ is gitignored, not committed
pip install -r requirements.txt
cp .env.example .env  # add your Azure keys
bash run.sh
```
