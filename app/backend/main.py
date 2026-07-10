import base64
import json
import logging
import os
import re
import time
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import APIError, AuthenticationError, AzureOpenAI, NotFoundError

try:
    import azure.cognitiveservices.speech as speechsdk
except ImportError:  # pragma: no cover - optional dependency in some environments
    speechsdk = None

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - optional dependency in some environments
    Image = None
    ImageDraw = None

from async_pipeline import Frame, FramePipeline, MockAzureClient, compute_image_hash

# Load .env from project root (so uvicorn works without `source .env`)
_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / ".env")

logger = logging.getLogger(__name__)

app = FastAPI(title="Innorave Multimodal Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"Missing env var: {name}")
    return value


def _normalize_azure_openai_endpoint(raw: str) -> str:
    """Azure OpenAI base URL: https://<resource>.openai.azure.com (no trailing slash)."""
    u = raw.strip().rstrip("/")
    if not u.startswith("http"):
        u = "https://" + u
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if host and ".openai.azure.com" not in host and ".cognitiveservices.azure.com" not in host:
        # Wrong host shape often causes 404
        pass
    return u


def _azure_openai_client() -> AzureOpenAI:
    endpoint = _normalize_azure_openai_endpoint(_env("AZURE_OPENAI_ENDPOINT"))
    return AzureOpenAI(
        api_key=_env("AZURE_OPENAI_API_KEY"),
        azure_endpoint=endpoint,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )


def _openai_404_detail(exc: APIError) -> str:
    body = getattr(exc, "body", None)
    if body is not None:
        return f"Azure returned 404. Response: {body}"
    return str(exc)


def _openai_failure_detail(exc: Exception) -> str:
    return (
        "Azure OpenAI request failed. Check AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, "
        "AZURE_OPENAI_CHAT_DEPLOYMENT, and AZURE_OPENAI_API_VERSION. Details: "
        + str(exc)
    )


def _translator_configured() -> bool:
    return bool(os.getenv("AZURE_TRANSLATOR_KEY", "").strip())


def _bcp47_to_translator_lang(bcp47: str) -> str:
    """Map BCP-47 (e.g. hi-IN) to Azure Translator language code."""
    if not bcp47 or not str(bcp47).strip():
        return "en"
    s = str(bcp47).strip().replace("_", "-")
    parts = [p for p in s.split("-") if p]
    if not parts:
        return "en"
    low_full = s.lower()
    if low_full.startswith("zh-"):
        return "zh-Hant" if "hant" in low_full else "zh-Hans"
    base = parts[0].lower()
    return base if len(base) == 2 else "en"


def _translate_text_rest(text: str, *, to_code: str, from_code: Optional[str] = None) -> str:
    """Azure Translator REST v3.0. If from_code is None, the service auto-detects source language."""
    key = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
    if not key:
        raise RuntimeError("AZURE_TRANSLATOR_KEY not set")
    region = os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
    endpoint = os.getenv("AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com").rstrip("/")
    url = f"{endpoint}/translate"
    params: Dict[str, str] = {"api-version": "3.0", "to": to_code}
    if from_code:
        params["from"] = from_code
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/json",
    }
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    body = [{"text": text}]
    response = requests.post(url, params=params, headers=headers, json=body, timeout=45)
    if response.status_code >= 400:
        raise RuntimeError(response.text or f"HTTP {response.status_code}")
    data = response.json()
    if not data or not isinstance(data, list):
        raise RuntimeError("Unexpected translator response")
    translations = data[0].get("translations") or []
    if not translations:
        raise RuntimeError("No translations in response")
    return str(translations[0].get("text", ""))


def translate_for_user(text: str, target_bcp47: str) -> str:
    """Translate text into the user's selected language (auto-detect source)."""
    if not text or not text.strip():
        return text
    if not _translator_configured():
        return text
    to_code = _bcp47_to_translator_lang(target_bcp47)
    try:
        return _translate_text_rest(text.strip(), to_code=to_code, from_code=None)
    except Exception as exc:
        logger.warning("translate_for_user failed: %s", exc)
        return text


def translate_to_english_for_model(text: str) -> str:
    """Normalize user input to English for the chat/vision model (auto-detect source)."""
    if not text or not text.strip():
        return text
    if not _translator_configured():
        return text
    try:
        return _translate_text_rest(text.strip(), to_code="en", from_code=None)
    except Exception as exc:
        logger.warning("translate_to_english_for_model failed: %s", exc)
        return text


def _document_intelligence_api_version() -> str:
    return os.getenv("AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "2024-11-30").strip() or "2024-11-30"


def _document_intelligence_headers() -> Dict[str, str]:
    key = _env("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    headers: Dict[str, str] = {"Ocp-Apim-Subscription-Key": key}
    region = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_REGION", "").strip()
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    return headers


def _document_intelligence_endpoint_base() -> str:
    raw = _env("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT").strip().rstrip("/")
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw


def _extract_text_from_document_intelligence(analyze_result: Dict[str, Any]) -> str:
    """Plain text from Document Intelligence prebuilt-read analyzeResult."""
    if not isinstance(analyze_result, dict):
        return ""
    content = analyze_result.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    lines_out: list[str] = []
    for page in analyze_result.get("pages") or []:
        for line in page.get("lines") or []:
            t = line.get("content")
            if isinstance(t, str) and t.strip():
                lines_out.append(t.strip())
    return "\n".join(lines_out)


def _poll_document_intelligence(operation_location: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Poll until analyze finishes (Document Intelligence returns 202 + Operation-Location)."""
    deadline = time.time() + 120.0
    while time.time() < deadline:
        try:
            response = requests.get(operation_location, headers=headers, timeout=60)
        except requests.RequestException as exc:
            raise RuntimeError(f"Document Intelligence polling failed: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(response.text or f"HTTP {response.status_code}")
        body = response.json()
        status = str(body.get("status") or "").lower()
        if status == "succeeded":
            return body
        if status in ("partiallysucceeded", "partially_succeeded"):
            return body
        if status == "failed":
            err = body.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(body)
            raise RuntimeError(msg or "Document Intelligence analysis failed")
        retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
        try:
            delay = float(retry_after) if retry_after else 1.0
        except ValueError:
            delay = 1.0
        time.sleep(min(max(delay, 0.2), 5.0))
    raise TimeoutError("Document Intelligence OCR timed out waiting for results")


def _ocr_with_document_intelligence(image_bytes: bytes, content_type: str) -> Dict[str, Any]:
    """Run prebuilt-read on image bytes; returns the poll response body (includes analyzeResult)."""
    base = _document_intelligence_endpoint_base()
    api_ver = _document_intelligence_api_version()
    url = f"{base}/documentintelligence/documentModels/prebuilt-read:analyze?api-version={api_ver}"
    headers = _document_intelligence_headers()
    ct = (content_type or "application/octet-stream").split(";")[0].strip()
    if ct not in (
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/bmp",
        "image/tiff",
        "image/heif",
        "application/octet-stream",
    ):
        ct = "application/octet-stream"
    headers["Content-Type"] = ct

    try:
        response = requests.post(url, headers=headers, data=image_bytes, timeout=60)
    except requests.RequestException as exc:
        raise RuntimeError(f"Document Intelligence request failed: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(response.text or f"HTTP {response.status_code}")

    if response.status_code == 202:
        op_loc = response.headers.get("Operation-Location") or response.headers.get("operation-location")
        if not op_loc:
            raise RuntimeError("Document Intelligence returned 202 without Operation-Location header")
        return _poll_document_intelligence(op_loc, _document_intelligence_headers())

    if response.status_code == 200:
        return response.json()

    raise RuntimeError(f"Unexpected status from Document Intelligence: {response.status_code}")


def _read_upload_as_data_url(file: UploadFile) -> str:
    content = file.file.read()
    b64 = base64.b64encode(content).decode("utf-8")
    media = file.content_type or "image/jpeg"
    return f"data:{media};base64,{b64}"


def _face_configured() -> bool:
    return bool(os.getenv("AZURE_FACE_ENDPOINT", "").strip() and os.getenv("AZURE_FACE_KEY", "").strip())


def _face_endpoint_base() -> str:
    raw = os.getenv("AZURE_FACE_ENDPOINT", "").strip().rstrip("/")
    if not raw:
        raise RuntimeError("AZURE_FACE_ENDPOINT not set")
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw


def _face_headers() -> Dict[str, str]:
    key = os.getenv("AZURE_FACE_KEY", "").strip()
    if not key:
        raise RuntimeError("AZURE_FACE_KEY not set")
    return {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/octet-stream",
    }


def _speech_configured() -> bool:
    return bool(
        speechsdk is not None
        and os.getenv("AZURE_SPEECH_KEY", "").strip()
        and os.getenv("AZURE_SPEECH_REGION", "").strip()
    )


def _speech_config() -> Any:
    if speechsdk is None:
        raise RuntimeError("azure-cognitiveservices-speech is not installed")
    key = _env("AZURE_SPEECH_KEY")
    region = _env("AZURE_SPEECH_REGION")
    cfg = speechsdk.SpeechConfig(subscription=key, region=region)
    voice = os.getenv("AZURE_SPEECH_VOICE_NAME", "").strip()
    if voice:
        cfg.speech_synthesis_voice_name = voice
    return cfg


def _speech_status() -> Dict[str, Any]:
    return {
        "sdk_available": speechsdk is not None,
        "configured": _speech_configured(),
        "key_set": bool(os.getenv("AZURE_SPEECH_KEY", "").strip()),
        "region_set": bool(os.getenv("AZURE_SPEECH_REGION", "").strip()),
        "voice_name": os.getenv("AZURE_SPEECH_VOICE_NAME", "").strip() or None,
        "language": os.getenv("AZURE_SPEECH_LANGUAGE", "en-US").strip() or "en-US",
    }


def _synthesize_speech(text: str) -> Dict[str, Any]:
    if not _speech_configured():
        raise RuntimeError("Azure Speech is not configured. Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION.")
    if not text or not text.strip():
        raise ValueError("text is required")

    cfg = _speech_config()
    cfg.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm)
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
    result = synthesizer.speak_text_async(text.strip()).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        cancellation = result.cancellation_details
        message = getattr(cancellation, "error_details", None) or getattr(cancellation, "reason", None) or "Speech synthesis failed"
        raise RuntimeError(str(message))

    audio_bytes = bytes(result.audio_data or b"")
    return {
        "audio_bytes": audio_bytes,
        "audio_base64": base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else "",
        "content_type": "audio/wav",
    }


def _transcribe_speech_audio(audio_bytes: bytes, *, language: str, content_type: str) -> Dict[str, Any]:
    if not _speech_configured():
        raise RuntimeError("Azure Speech is not configured. Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION.")
    if not audio_bytes:
        raise ValueError("audio is required")
    if speechsdk is None:
        raise RuntimeError("azure-cognitiveservices-speech is not installed")

    suffix = ".wav"
    if content_type and "/" in content_type:
        subtype = content_type.split("/", 1)[1].split(";", 1)[0].strip().lower()
        if subtype in {"wav", "wave", "x-wav"}:
            suffix = ".wav"

    cfg = _speech_config()
    cfg.speech_recognition_language = (language or os.getenv("AZURE_SPEECH_LANGUAGE", "en-US")).strip() or "en-US"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        audio_config = speechsdk.AudioConfig(filename=tmp.name)
        recognizer = speechsdk.SpeechRecognizer(speech_config=cfg, audio_config=audio_config)
        try:
            result = recognizer.recognize_once_async().get()
        except Exception as exc:
            message = str(exc)
            if "INVALID_HEADER" in message or "invalid header" in message.lower():
                raise ValueError("Uploaded audio must be a WAV file or compatible PCM container.") from exc
            raise RuntimeError(message) from exc

    if result.reason == speechsdk.ResultReason.RecognizedSpeech:
        return {"transcript": (result.text or "").strip(), "reason": "recognized"}
    if result.reason == speechsdk.ResultReason.NoMatch:
        return {"transcript": "", "reason": "no-match"}
    if result.reason == speechsdk.ResultReason.Canceled:
        cancellation = result.cancellation_details
        message = getattr(cancellation, "error_details", None) or getattr(cancellation, "reason", None) or "Speech recognition canceled"
        raise RuntimeError(str(message))

    return {"transcript": "", "reason": str(result.reason)}


_PEOPLE_DB: Dict[str, Dict[str, Any]] = {}
_PEOPLE_COUNTER = 0
_PIPELINE_METRICS: Dict[str, Any] = {"runs": 0, "last_run": None}


def _infer_emotion_with_vision(image_bytes: bytes, content_type: str) -> str:
    """Use the existing vision model to estimate emotion as one of: happy, sad, angry, neutral, surprised, unsure."""
    try:
        client = _azure_openai_client()
        deployment = _env("AZURE_OPENAI_CHAT_DEPLOYMENT")
        media = (content_type or "image/jpeg").split(";")[0] or "image/jpeg"
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{media};base64,{b64}"
        result = client.chat.completions.create(
            model=deployment,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You see a single person's face. "
                        "Reply with ONE word only: happy, sad, angry, neutral, surprised, or unsure."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is the person's facial emotion?"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            temperature=0,
            max_tokens=4,
        )
        emo_raw = (result.choices[0].message.content or "").strip().lower()
    except Exception as exc:
        logger.warning("infer emotion via vision failed: %s", exc)
        return "neutral"

    if "happy" in emo_raw:
        return "happy"
    if "sad" in emo_raw:
        return "sad"
    if "angry" in emo_raw or "anger" in emo_raw:
        return "angry"
    if "surpris" in emo_raw:
        return "surprised"
    if "neutral" in emo_raw:
        return "neutral"
    return "unsure"


def _dominant_emotion(attrs: Dict[str, Any]) -> str:
    emo = attrs.get("emotion") or {}
    if not isinstance(emo, dict):
        return "neutral"
    best = "neutral"
    best_v = -1.0
    for k, v in emo.items():
        try:
            score = float(v)
        except (TypeError, ValueError):
            continue
        if score > best_v:
            best_v = score
            best = k
    mapping = {
        "happiness": "happy",
        "sadness": "sad",
        "anger": "angry",
        "neutral": "neutral",
        "surprise": "surprised",
        "fear": "afraid",
        "disgust": "disgusted",
        "contempt": "unsure",
    }
    return mapping.get(best, "neutral")


def _face_status_line(name: Optional[str], status: str, emotion: str) -> str:
    base: str
    if status == "familiar" and name:
        base = f"This is {name}, a familiar person."
    elif status == "new":
        base = "A new person is in front of you."
    else:
        base = "I am not sure who this is."

    emo_part = ""
    if emotion == "happy":
        emo_part = " The person looks happy."
    elif emotion == "sad":
        emo_part = " The person seems sad."
    elif emotion == "angry":
        emo_part = " The person appears angry."
    elif emotion == "neutral":
        emo_part = " The person looks neutral."
    elif emotion == "surprised":
        emo_part = " The person looks surprised."
    elif emotion:
        emo_part = f" The person looks {emotion}."

    line = (base + emo_part).strip()
    return _to_single_line(line)


_VISION_ONE_LINE_SYSTEM_PROMPT = """You are a real-time camera assistant.
An image is attached in this request. Use that image directly.
Reply in exactly one short, clear line.
Do not say you cannot see images.
Do not ask the user to upload or describe the image.
If uncertain, say a brief one-line best effort from the visible scene."""


def _to_single_line(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    clean = re.sub(r"^[-*]\s+", "", clean)
    if len(clean) > 180:
        clean = clean[:177].rstrip() + "..."
    return clean


def _looks_like_no_image_reply(text: str) -> bool:
    t = (text or "").lower()
    patterns = (
        "can't see images",
        "cannot see images",
        "can't view images",
        "cannot view images",
        "i can't see the image",
        "i cannot see the image",
        "please upload the image",
        "describe the image",
    )
    return any(p in t for p in patterns)


_SPATIAL_SYSTEM_PROMPT = """You analyze a single forward-facing camera frame for a blind or low-vision mobility assistant.
There is NO true depth sensor — infer proximity only from visual cues (size in frame, position, perspective, floor lines).
Output ONLY valid JSON (no markdown fences), exactly this shape:
{
  "risk_level": "safe",
  "obstacles": [],
  "guidance": "",
  "direction_hint": "none"
}
Allowed risk_level values: "safe", "caution", "obstacle", "emergency".
- emergency: appears very close, stairs drop-off, fast vehicle, or imminent collision risk.
- obstacle: clear hazard in path (wall, pole, person, vehicle, open door edge, major uneven surface).
- caution: possible hazard or needs attention.
- safe: no immediate hazard; path appears clear.
obstacles: array of objects, each {"type": "wall|person|vehicle|stairs|pole|door|uneven|other", "position": "left|center|right|floor|ahead", "notes": "few words"}.
guidance: ONE short English sentence the user can hear, e.g. "Obstacle ahead, move left." or "Step down detected." or "Wall on the right." Max 120 characters.
direction_hint: one of left, right, back, stop, up, down, center, none."""


def _parse_json_from_model_text(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if m:
            text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _normalize_spatial_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"safe", "caution", "obstacle", "emergency"}
    level = str(data.get("risk_level") or "safe").lower().strip()
    if level not in allowed:
        level = "caution"
    obstacles = data.get("obstacles")
    if not isinstance(obstacles, list):
        obstacles = []
    clean_obs: List[Dict[str, Any]] = []
    for o in obstacles[:12]:
        if not isinstance(o, dict):
            continue
        clean_obs.append(
            {
                "type": str(o.get("type") or "other")[:64],
                "position": str(o.get("position") or "center")[:32],
                "notes": str(o.get("notes") or "")[:200],
            }
        )
    guidance = str(data.get("guidance") or "").strip()[:500]
    if not guidance:
        guidance = "Check your surroundings." if level != "safe" else "Path looks clear."
    dh = str(data.get("direction_hint") or "none").lower().strip()
    if dh not in ("left", "right", "back", "stop", "up", "down", "center", "none"):
        dh = "none"
    return {
        "risk_level": level,
        "obstacles": clean_obs,
        "guidance": guidance,
        "direction_hint": dh,
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/debug/translator-config")
def debug_translator_config() -> Dict[str, Any]:
    return {
        "translator_key_set": _translator_configured(),
        "translator_region_set": bool(os.getenv("AZURE_TRANSLATOR_REGION", "").strip()),
        "endpoint": (os.getenv("AZURE_TRANSLATOR_ENDPOINT") or "https://api.cognitive.microsofttranslator.com").strip()
        or None,
    }


@app.get("/api/debug/document-intelligence-config")
def debug_document_intelligence_config() -> Dict[str, Any]:
    raw = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()
    parsed = urlparse(raw if raw.startswith("http") else f"https://{raw}") if raw else None
    return {
        "endpoint_set": bool(raw),
        "endpoint_host": parsed.hostname if parsed else None,
        "api_version": _document_intelligence_api_version() if raw else None,
        "api_key_set": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()),
        "region_set": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_REGION", "").strip()),
    }


@app.get("/api/debug/openai-config")
def debug_openai_config() -> Dict[str, Any]:
    """Non-secret values so you can confirm .env is loaded (restart server after editing .env)."""
    raw = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    normalized = _normalize_azure_openai_endpoint(raw) if raw else ""
    parsed = urlparse(normalized) if normalized else None
    return {
        "endpoint_raw_set": bool(raw),
        "endpoint_host": parsed.hostname if parsed else None,
        "endpoint_normalized_prefix": f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else None,
        "deployment": os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "").strip() or None,
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "").strip() or None,
        "api_key_set": bool(os.getenv("AZURE_OPENAI_API_KEY", "").strip()),
    }


@app.get("/api/debug/cloud-readiness")
def debug_cloud_readiness() -> Dict[str, Any]:
    return {
        "openai": {
            "endpoint_set": bool(os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()),
            "api_key_set": bool(os.getenv("AZURE_OPENAI_API_KEY", "").strip()),
            "deployment_set": bool(os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "").strip()),
        },
        "document_intelligence": {
            "endpoint_set": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()),
            "api_key_set": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()),
            "region_set": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_REGION", "").strip()),
        },
        "face": {
            "endpoint_set": bool(os.getenv("AZURE_FACE_ENDPOINT", "").strip()),
            "api_key_set": bool(os.getenv("AZURE_FACE_KEY", "").strip()),
        },
        "speech": _speech_status(),
        "translator": {
            "configured": _translator_configured(),
            "region_set": bool(os.getenv("AZURE_TRANSLATOR_REGION", "").strip()),
        },
    }


@app.get("/api/metrics")
def api_metrics() -> Dict[str, Any]:
    return {
        "pipeline": _PIPELINE_METRICS,
        "health": "ok",
    }


def _build_demo_frames(frame_count: int, duplicate_window: int) -> list[Frame]:
    stamp = time.time()
    window = max(1, duplicate_window)
    frames: list[Frame] = []

    for index in range(frame_count):
        group = index % window
        payload = b""
        image_hash = f"demo-{group}"
        if Image is not None and ImageDraw is not None:
            image = Image.new("RGB", (160, 120), color=(40 + group * 20, 60, 90 + group * 10))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20 + group, 18, 120, 92), outline=(255, 255, 255), width=4)
            draw.text((28, 48), f"{group}", fill=(255, 255, 255))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            payload = buffer.getvalue()
            image_hash = compute_image_hash(payload, fallback=image_hash)

        frames.append(
            Frame(
                frame_id=index + 1,
                captured_at=stamp,
                image_hash=image_hash,
                payload=payload,
            )
        )

    return frames


@app.post("/api/pipeline/demo")
async def pipeline_demo(
    frames: int = Query(12, ge=1, le=50),
    workers: int = Query(4, ge=1, le=8),
    duplicate_window: int = Query(4, ge=1, le=25),
    failure_rate: float = Query(0.15, ge=0.0, le=1.0),
) -> Dict[str, Any]:
    demo_frames = _build_demo_frames(frames, duplicate_window)
    pipeline = FramePipeline(
        azure_client=MockAzureClient(failure_rate=failure_rate, base_latency=(0.0, 0.0)),
        num_workers=workers,
        queue_size=max(frames, workers),
    )
    results = await pipeline.run(demo_frames)

    summary = pipeline.summary()
    sample_results = [
        {
            "frame_id": result.frame_id,
            "from_cache": result.from_cache,
            "failed": result.failed,
            "used_fallback": result.used_fallback,
            "ocr_text": result.ocr_text,
            "vision_tags": result.vision_tags,
        }
        for result in results[: min(len(results), 8)]
    ]

    _PIPELINE_METRICS["runs"] = int(_PIPELINE_METRICS.get("runs", 0)) + 1
    _PIPELINE_METRICS["last_run"] = summary

    return {
        "summary": summary,
        "sample_results": sample_results,
    }


@app.post("/chat")
async def chat(question: str = Form(...), target_lang: str = Form("en-US")) -> Dict[str, Any]:
    client = _azure_openai_client()
    deployment = _env("AZURE_OPENAI_CHAT_DEPLOYMENT")
    q_model = translate_to_english_for_model(question) if _translator_configured() else question
    try:
        result = client.chat.completions.create(
            model=deployment,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant. Answer clearly and concisely in English "
                        "(the user may see a translated version)."
                    ),
                },
                {"role": "user", "content": q_model},
            ],
            temperature=0.2,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Azure OpenAI 404 — usually wrong deployment NAME (not model label), wrong endpoint "
                "resource, or bad api-version. Open Foundry → Deployments and copy the Name column "
                "exactly. Try AZURE_OPENAI_API_VERSION=2024-08-01-preview. Details: "
                + _openai_404_detail(exc)
            ),
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=502, detail=_openai_failure_detail(exc)) from exc
    except APIError as exc:
        raise HTTPException(status_code=502, detail=_openai_failure_detail(exc)) from exc
    answer = result.choices[0].message.content or ""
    answer_out = translate_for_user(answer, target_lang) if _translator_configured() else answer
    return {"answer": answer_out}


@app.post("/vision")
async def vision(
    question: str = Form(...), image: UploadFile = File(...), target_lang: str = Form("en-US")
) -> Dict[str, Any]:
    client = _azure_openai_client()
    deployment = _env("AZURE_OPENAI_CHAT_DEPLOYMENT")
    q_model = translate_to_english_for_model(question) if _translator_configured() else question
    image_data_url = _read_upload_as_data_url(image)
    try:
        result = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _VISION_ONE_LINE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": q_model},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            ],
            temperature=0.2,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Azure OpenAI 404 — see /api/debug/openai-config and fix deployment name or api-version. "
                + _openai_404_detail(exc)
            ),
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=502, detail=_openai_failure_detail(exc)) from exc
    except APIError as exc:
        raise HTTPException(status_code=502, detail=_openai_failure_detail(exc)) from exc
    answer = _to_single_line(result.choices[0].message.content or "")
    if _looks_like_no_image_reply(answer):
        retry = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _VISION_ONE_LINE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "The image is attached. Describe only what is visible in one short line. "
                                f"User question: {q_model}"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                },
            ],
            temperature=0.1,
        )
        answer = _to_single_line(retry.choices[0].message.content or "")
    answer_out = translate_for_user(answer, target_lang) if _translator_configured() else answer
    answer_out = _to_single_line(answer_out)
    return {"answer": answer_out}


@app.post("/spatial-safety")
async def spatial_safety(image: UploadFile = File(...), target_lang: str = Form("en-US")) -> Dict[str, Any]:
    """
    Live-frame spatial / obstacle assist: vision model returns structured JSON; guidance may be translated.
    Not a substitute for a cane or trained orientation — estimates only from a 2D image.
    """
    client = _azure_openai_client()
    deployment = _env("AZURE_OPENAI_CHAT_DEPLOYMENT")
    image_data_url = _read_upload_as_data_url(image)
    user_text = "Analyze this frame for obstacles and mobility hazards. Reply with JSON only as specified."
    try:
        result = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _SPATIAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=500,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Azure OpenAI 404 — see /api/debug/openai-config. " + _openai_404_detail(exc)
            ),
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=502, detail=_openai_failure_detail(exc)) from exc
    except APIError as exc:
        raise HTTPException(status_code=502, detail=_openai_failure_detail(exc)) from exc
    raw = result.choices[0].message.content or ""
    parsed = _parse_json_from_model_text(raw)
    normalized = _normalize_spatial_payload(parsed)
    g_en = normalized["guidance"]
    g_out = translate_for_user(g_en, target_lang) if _translator_configured() else g_en
    return {
        "risk_level": normalized["risk_level"],
        "obstacles": normalized["obstacles"],
        "guidance": g_out,
        "guidanceEnglish": g_en,
        "direction_hint": normalized["direction_hint"],
    }


@app.post("/ocr")
async def ocr(image: UploadFile = File(...), target_lang: str = Form("en-US")) -> Dict[str, Any]:
    """
    OCR via Azure Document Intelligence prebuilt-read (not Computer Vision Read).
    Returns `text` (optionally translated), `originalText`, and `analyzeResult` from the service.
    """
    raw_bytes = await image.read()
    content_type = image.content_type or "application/octet-stream"
    try:
        poll_body = _ocr_with_document_intelligence(raw_bytes, content_type)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    ar = poll_body.get("analyzeResult")
    if not isinstance(ar, dict):
        ar = {}
    extracted = _extract_text_from_document_intelligence(ar)

    out: Dict[str, Any] = {
        "status": poll_body.get("status"),
        "modelId": "prebuilt-read",
        "originalText": extracted,
        "analyzeResult": ar,
    }
    if extracted and _translator_configured():
        out["text"] = translate_for_user(extracted, target_lang)
    elif extracted:
        out["text"] = extracted
    else:
        out["text"] = ""
    return out


@app.post("/face-inspect")
async def face_inspect(
    image: UploadFile = File(...),
    target_lang: str = Form("en-US"),
    save_new: bool = Form(True),
) -> Dict[str, Any]:
    """
    Face + emotion helper using Azure Face API.
    - Detects one face.
    - Gets dominant emotion.
    - Very simple in-memory "familiar/new/uncertain" tracking.
    """
    if not _face_configured():
        raise HTTPException(
            status_code=500,
            detail="Azure Face API is not configured. Set AZURE_FACE_ENDPOINT and AZURE_FACE_KEY.",
        )

    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image")

    base = _face_endpoint_base()
    detect_url = f"{base}/face/v1.0/detect"
    params = {
        "returnFaceId": "true",
        "recognitionModel": "recognition_04",
        "detectionModel": "detection_03",
    }
    try:
        resp = requests.post(detect_url, params=params, headers=_face_headers(), data=raw, timeout=20)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Face detect error: {exc}") from exc
    if resp.status_code >= 400:
        body_text = resp.text or ""
        # Fallback when Face API features (identification/verification) are not approved on this subscription.
        if "UnsupportedFeature" in body_text:
            emotion = _infer_emotion_with_vision(raw, image.content_type or "image/jpeg")
            status = "uncertain"
            label = "Person 1"
            summary_en = _face_status_line(None, status, emotion)
            summary_out = translate_for_user(summary_en, target_lang) if _translator_configured() else summary_en
            return {
                "summary": _to_single_line(summary_out),
                "emotion": emotion,
                "status": status,
                "label": label,
                "people": list(_PEOPLE_DB.values()),
            }
        raise HTTPException(status_code=502, detail=f"Face detect failed: {body_text}")
    faces = resp.json()
    if not isinstance(faces, list) or not faces:
        line = "No face detected in front of the camera."
        line_out = translate_for_user(line, target_lang) if _translator_configured() else line
        return {
            "summary": _to_single_line(line_out),
            "people": list(_PEOPLE_DB.values()),
        }

    # Use the first face for now.
    face = faces[0]
    face_id = str(face.get("faceId") or "")
    emotion = _infer_emotion_with_vision(raw, image.content_type or "image/jpeg")

    global _PEOPLE_COUNTER
    status = "uncertain"
    name: Optional[str] = None
    label: str

    # Simple "familiar" tracking: remember faceIds we have seen during this server run.
    for person in _PEOPLE_DB.values():
        if person.get("last_face_id") == face_id:
            status = "familiar"
            name = person.get("name")
            label = person["label"]
            person["times_seen"] += 1
            person["last_seen"] = int(time.time())
            person["last_face_id"] = face_id
            break
    else:
        # New or uncertain person.
        status = "new" if save_new else "uncertain"
        name = None
        label = f"Person {_PEOPLE_COUNTER + 1}"
        if save_new:
            _PEOPLE_COUNTER += 1
            _PEOPLE_DB[label] = {
                "label": label,
                "name": name or label,
                "last_seen": int(time.time()),
                "times_seen": 1,
                "status": "familiar",  # once saved, treat as familiar next time in this run
                "last_face_id": face_id,
            }

    summary_en = _face_status_line(name or label if status == "familiar" else None, status, emotion)
    summary_out = translate_for_user(summary_en, target_lang) if _translator_configured() else summary_en
    return {
        "summary": _to_single_line(summary_out),
        "emotion": emotion,
        "status": status,
        "label": label,
        "people": list(_PEOPLE_DB.values()),
    }


@app.post("/speech-to-text")
async def speech_to_text(audio: UploadFile = File(...), language: str = Form("en-US")) -> Dict[str, Any]:
    raw = await audio.read()
    try:
        result = _transcribe_speech_audio(raw, language=language, content_type=audio.content_type or "")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "transcript": result["transcript"],
        "reason": result["reason"],
        "filename": audio.filename,
        "language": language,
    }


@app.post("/speech")
async def speech(text: str = Form(...), language: str = Form("en-US")) -> Dict[str, Any]:
    try:
        result = _synthesize_speech(text)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "audioUrl": f"data:{result['content_type']};base64,{result['audio_base64']}",
        "text": text,
        "language": language,
        "contentType": result["content_type"],
    }


app.mount("/", StaticFiles(directory="app/frontend", html=True), name="static")


@app.exception_handler(Exception)
async def unhandled(_: Any, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": str(exc)})
