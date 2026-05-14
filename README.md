# Innorave Smart (Starter)

This is a starter scaffold for your Azure multimodal project:

- `/chat` for text Q&A (Azure OpenAI)
- `/vision` for image + question (Azure OpenAI Vision)
- `/spatial-safety` for assistive obstacle / path estimation from a camera frame (structured JSON + optional translation); used by **Live obstacle detection** in the UI (not a replacement for a cane or professional training)
- `/ocr` for camera image OCR via **Azure Document Intelligence** (`prebuilt-read`), with optional Azure Translator for the selected UI language
- Azure Translator (optional): user-chosen language in the app is sent as `target_lang`; answers and OCR text are translated when `AZURE_TRANSLATOR_KEY` is set
- `/speech-to-text` and `/speech` placeholders (next step: Azure Speech SDK)

## 1) Install local dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Configure environment

```bash
cp .env.example .env
```

Fill in Azure keys in `.env`. The app loads `.env` automatically on startup (you can still `source .env` if you want variables in your shell).

**Document Intelligence (OCR):** set `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` and `AZURE_DOCUMENT_INTELLIGENCE_KEY` from your Document Intelligence resource in Azure Portal. Use the same endpoint style as other Cognitive Services (`https://<resource>.cognitiveservices.azure.com`). If the portal shows a region for your key, set `AZURE_DOCUMENT_INTELLIGENCE_REGION`. Confirm with `http://127.0.0.1:8000/api/debug/document-intelligence-config` (restart after changing `.env`).

**Azure Translator (recommended for multilingual voice):** set `AZURE_TRANSLATOR_KEY` and `AZURE_TRANSLATOR_REGION` from your Translator resource (same region as the key). The frontend sends `target_lang` (e.g. `hi-IN`, `te-IN`) with `/chat`, `/vision`, and `/ocr`. The backend translates user input to English for the model when needed, then translates replies (and OCR output) into `target_lang`. If Translator is not configured, behavior is unchanged (English model I/O).

Visit `http://127.0.0.1:8000/api/debug/translator-config` to confirm the server sees your Translator key (not the secret value).

## 3) Run locally

```bash
python -m uvicorn app.backend.main:app --reload
```

Open: `http://127.0.0.1:8000`

### If chat returns `404` from Azure OpenAI

1. Open Foundry → your resource → **Deployments**. Copy the **Name** column (e.g. `gpt-4.1-mini`), not the long “model (version …)” label if it differs.
2. Set `AZURE_OPENAI_CHAT_DEPLOYMENT` to that **Name** exactly (case-sensitive).
3. In Azure Portal → OpenAI resource → **Keys and Endpoint**, copy **Endpoint** into `AZURE_OPENAI_ENDPOINT` (must be `https://<resource>.openai.azure.com` for that same resource as the key).
4. Try `AZURE_OPENAI_API_VERSION=2024-08-01-preview` in `.env`, restart the server.
5. Visit `http://127.0.0.1:8000/api/debug/openai-config` to confirm the server sees your deployment name and host.

## 4) Next recommended steps

1. Replace speech placeholders with Azure Speech SDK:
   - `/speech-to-text`: transcribe microphone audio
   - `/speech`: synthesize answer audio
2. Add Document Intelligence endpoint for PDF/Office OCR route
3. Add a simple intent router:
   - "read this" -> `/ocr`
   - default -> `/vision` or `/chat`
4. Once working locally, switch base to Microsoft `openai-chat-vision-quickstart` template and move these routes into that backend for `azd up` deployment.

## 5) Azure CLI/azd notes

You still need these tools installed to do Azure provisioning/deployment:

- Azure CLI (`az`)
- Azure Developer CLI (`azd`)

Then deploy from the official template flow:

```bash
azd init -t openai-chat-vision-quickstart
azd auth login
azd up
```
