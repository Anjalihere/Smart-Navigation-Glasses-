/**
 * Voice UI with optional wake phrase "Ok Glass" (similar idea to "Ok Google").
 * Only one SpeechRecognition can run — we use modes: wake | manual | idle.
 */

const camera = document.getElementById("camera");
const canvas = document.getElementById("snapshot");
const output = document.getElementById("output");
const question = document.getElementById("question");
const micBtn = document.getElementById("micBtn");
const micLabel = document.getElementById("micLabel");
const listeningStatus = document.getElementById("listeningStatus");
const transcriptText = document.getElementById("transcriptText");
const srAnnounce = document.getElementById("sr-announce");
const readAloud = document.getElementById("readAloud");
const repeatAnswer = document.getElementById("repeatAnswer");
const wakeGlass = document.getElementById("wakeGlass");
const wakeStatus = document.getElementById("wakeStatus");
const listenBanner = document.getElementById("listenBanner");
const beepOnListen = document.getElementById("beepOnListen");
const micShell = document.getElementById("micShell");
const voiceLang = document.getElementById("voiceLang");

/** BCP-47 tag from the language chip — drives speech recognition, TTS, and server-side translation. */
function getTargetLang() {
  return (voiceLang && voiceLang.value) || "en-US";
}

function appendTargetLang(form) {
  if (!form) return;
  form.append("target_lang", getTargetLang());
}

let lastAnswerText = "";
let recognition = null;

/** @type {'idle'|'wake'|'manual'} */
let voiceMode = "idle";

/** Manual mode: user tapped mic; tap again to send. */
let sessionActive = false;
let manualStop = false;
let accumulatedFinal = "";
let lastInterim = "";

/** Wake → next spoken phrase is treated as the command (after "What would you like?"). */
let postWakeMode = false;
let postWakeTimer = null;

/** Stopping recognition to switch wake → manual tap-to-talk. */
let switchingToManual = false;

/** Chrome needs ~400–500ms after onend before start() or InvalidStateError is common. */
const RESTART_GAP_MS = 480;
let lastSpeechAt = 0;
/** Wake mode: only show full wake UI once per session (not every recognition restart). */
let wakeUiPrimed = false;
/** Only one scheduled recognition.start() at a time (prevents stacked InvalidStateError). */
let restartTimer = null;
let networkRetryCount = 0;
/** One start beep per user mic session (restarts share the same session until tap stop). */
let listenCuePlayed = false;
let heardSpeechThisSession = false;

const WAKE_RE = /\b(ok|okay)\s+glass\b/i;

const SpeechRecognition =
  typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition);

/** Screen reader only (does not replace visible listening banner). */
function announce(message) {
  srAnnounce.textContent = message;
}

/** Short tone so you know the mic is live (does not use speech recognition). */
function playListenBeep(frequencyHz = 880) {
  if (beepOnListen && !beepOnListen.checked) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = frequencyHz;
    g.gain.value = 0.11;
    osc.connect(g);
    g.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.11);
    window.setTimeout(() => {
      try {
        ctx.close();
      } catch (_) {
        /* ignore */
      }
    }, 200);
  } catch (_) {
    /* ignore */
  }
}

function speak(text) {
  if (!readAloud.checked || !text || !window.speechSynthesis) return;
  try {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = (voiceLang && voiceLang.value) || document.documentElement.lang || "en-US";
    u.rate = 1;
    u.volume = 1;
    window.speechSynthesis.speak(u);
  } catch (_) {
    /* ignore */
  }
}

/** Spoken feedback that is not an AI answer (e.g. “didn’t hear you”). */
function speakFeedback(text) {
  if (!readAloud.checked || !text || !window.speechSynthesis) return;
  try {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = (voiceLang && voiceLang.value) || document.documentElement.lang || "en-US";
    u.rate = 1;
    u.volume = 1;
    window.speechSynthesis.speak(u);
  } catch (_) {
    /* ignore */
  }
}

function setWakeStatus(text) {
  wakeStatus.textContent = text || "";
}

function setMicShellListening(on, kind) {
  if (!micShell) return;
  micShell.classList.toggle("is-listening", on === true);
  micShell.classList.toggle("is-wake", on && kind === "wake");
  micShell.classList.toggle("is-manual", on && kind === "manual");
}

/** @param {boolean} on @param {'wake'|'manual'|undefined} kind */
function setMicUi(on, kind) {
  if (on === false) {
    micBtn.setAttribute("aria-pressed", "false");
    micLabel.textContent = "Tap to speak";
    listeningStatus.textContent = "";
    listeningStatus.className = "listen-hint";
    if (listenBanner) listenBanner.hidden = true;
    micBtn.classList.remove("mic-live");
    setMicShellListening(false);
    return;
  }
  if (kind === "wake") {
    micBtn.setAttribute("aria-pressed", "false");
    micLabel.textContent = "Listening for “OK Glass” — or tap the mic";
    if (listenBanner) listenBanner.hidden = true;
    micBtn.classList.remove("mic-live");
    setMicShellListening(true, "wake");
    announce("Wake listening. Say Ok Glass, then your request. Or tap the microphone to speak without Ok Glass.");
    return;
  }
  if (kind === "manual") {
    micBtn.setAttribute("aria-pressed", "true");
    micLabel.textContent = "Listening… tap again when done";
    if (listenBanner) {
      listenBanner.hidden = false;
      listenBanner.textContent = "Listening — speak now";
    }
    micBtn.classList.add("mic-live");
    listeningStatus.textContent = "Tap the microphone again to send.";
    listeningStatus.className = "listen-hint listen-hint--active";
    setMicShellListening(true, "manual");
    announce("Microphone is on. Speak now. Tap the microphone again when you are done.");
  }
}

function promptWhatWant() {
  announce("What would you like?");
  setWakeStatus("What would you like?");
  if (readAloud.checked) speak("What would you like?");
}

function processWakePhrase(phrase) {
  const raw = (phrase || "").trim();
  if (!raw) return;

  transcriptText.textContent = raw;

  if (postWakeMode) {
    postWakeMode = false;
    if (postWakeTimer) {
      clearTimeout(postWakeTimer);
      postWakeTimer = null;
    }
    setWakeStatus("");
    void handleVoiceText(raw).catch((err) => {
      setAnswer(`Error: ${err.message}`);
    });
    return;
  }

  if (!WAKE_RE.test(raw)) {
    return;
  }

  const after = raw.replace(/^.*?\b(ok|okay)\s+glass\b[,!?.]?\s*/i, "").trim();
  if (after) {
    setWakeStatus("");
    void handleVoiceText(after).catch((err) => {
      setAnswer(`Error: ${err.message}`);
    });
  } else {
    promptWhatWant();
    postWakeMode = true;
    postWakeTimer = window.setTimeout(() => {
      postWakeMode = false;
      postWakeTimer = null;
      setWakeStatus("");
    }, 20000);
  }
}

async function startCamera() {
  const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  if (camera.srcObject) {
    camera.srcObject.getTracks().forEach((t) => t.stop());
  }
  camera.srcObject = stream;
  announce("Camera is on.");
  output.textContent = "Camera is on. Point the camera at what you want help with, then use Describe or ask by voice.";
  syncSpatialSafetyWithCamera();
}

function stopCamera() {
  const stream = camera.srcObject;
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    camera.srcObject = null;
  }
  stopSpatialSafetyLoop();
  resetSafetyUiOff();
  announce("Camera is off.");
  output.textContent = "Camera is off.";
}

function captureBlob() {
  const ctx = canvas.getContext("2d");
  const w = camera.videoWidth || 640;
  const h = camera.videoHeight || 480;
  canvas.width = w;
  canvas.height = h;
  ctx.drawImage(camera, 0, 0, w, h);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
}

function cameraIsActive() {
  return !!(camera.srcObject && camera.videoWidth > 0);
}

async function postForm(url, formData) {
  const res = await fetch(url, { method: "POST", body: formData });
  const data = await res.json();
  if (!res.ok) throw new Error(JSON.stringify(data));
  return data;
}

function setAnswer(text) {
  lastAnswerText = text;
  output.textContent = text;
  speak(text);
  const feed = document.querySelector(".chat-feed");
  if (feed) feed.scrollTop = feed.scrollHeight;
}

function ocrJsonToPlain(data) {
  if (data && typeof data.text === "string" && data.text.trim()) {
    const t = data.text.trim();
    const o = typeof data.originalText === "string" ? data.originalText.trim() : "";
    if (o && o !== t) {
      return `${t}\n\n[Original OCR]\n${o}`;
    }
    return t;
  }
  const ar = data?.analyzeResult;
  if (ar && typeof ar.content === "string" && ar.content.trim()) {
    return ar.content.trim();
  }
  const lineText = (line) => (line && (line.content || line.text)) || "";
  try {
    const read = ar || data?.readResult || data?.analyzeResult?.readResults;
    if (read && typeof read === "object" && !Array.isArray(read)) {
      const lines = [];
      for (const block of read.blocks || []) {
        for (const line of block.lines || []) {
          const t = lineText(line);
          if (t) lines.push(t);
        }
      }
      for (const page of read.pages || []) {
        for (const line of page.lines || []) {
          const t = lineText(line);
          if (t) lines.push(t);
        }
      }
      if (lines.length) return lines.join("\n");
    }
    if (Array.isArray(read)) {
      const lines = [];
      for (const page of read) {
        for (const line of page.lines || []) {
          const t = lineText(line);
          if (t) lines.push(t);
        }
      }
      if (lines.length) return lines.join("\n");
    }
  } catch (_) {
    /* fall through */
  }
  return JSON.stringify(data, null, 2);
}

async function handleVoiceText(raw) {
  const t = (raw || "").trim();
  transcriptText.textContent = t || "—";
  if (!t) return;

  const lower = t.toLowerCase();

  const openCam =
    /\b(open|start|turn on)\s+(the\s+)?camera\b/.test(lower) ||
    /\bcamera\s+on\b/.test(lower) ||
    /\bshow\s+(the\s+)?camera\b/.test(lower);

  const stopCam =
    /\b(close|stop|turn off)\s+(the\s+)?camera\b/.test(lower) ||
    /\bcamera\s+off\b/.test(lower);

  const wantOcr =
    /\b(read\s+(the\s+)?text|ocr|scan\s+(the\s+)?text|read\s+this\s+(page|paper)?)\b/.test(lower);

  const hasSeeVerb = /\b(see|seeing|saw|seen)\b/.test(lower);
  const wantVision =
    /\b(what('s|\s+is)\s+this|what\s+am\s+i\s+seeing|what\s+do\s+you\s+see|look\s+at\s+this)\b/.test(lower) ||
    hasSeeVerb;

  if (stopCam) {
    stopCamera();
    return;
  }

  if (openCam) {
    try {
      await startCamera();
    } catch (err) {
      setAnswer(`Camera error: ${err.message}`);
    }
    return;
  }

  question.value = t;

  if (wantOcr) {
    if (!cameraIsActive()) {
      setAnswer("Turn on the camera first. Say “open camera” or tap Open camera, then try again.");
      return;
    }
    try {
      announce("Reading text from the picture.");
      const blob = await captureBlob();
      const form = new FormData();
      form.append("image", blob, "frame.jpg");
      appendTargetLang(form);
      const data = await postForm("/ocr", form);
      setAnswer(ocrJsonToPlain(data));
    } catch (err) {
      setAnswer(`OCR error: ${err.message}`);
    }
    return;
  }

  if (wantVision) {
    if (!cameraIsActive()) {
      try {
        await startCamera();
      } catch (err) {
        setAnswer(`Camera error: ${err.message}`);
        return;
      }
    }
    try {
      announce("Describing what the camera sees.");
      const blob = await captureBlob();
      const form = new FormData();
      form.append("question", t);
      form.append("image", blob, "frame.jpg");
      appendTargetLang(form);
      const data = await postForm("/vision", form);
      setAnswer(data.answer || JSON.stringify(data, null, 2));
    } catch (err) {
      setAnswer(`Vision error: ${err.message}`);
    }
    return;
  }

  try {
    if (cameraIsActive()) {
      announce("Answering using the camera and your words.");
      const blob = await captureBlob();
      const form = new FormData();
      form.append("question", t);
      form.append("image", blob, "frame.jpg");
      appendTargetLang(form);
      const data = await postForm("/vision", form);
      setAnswer(data.answer || JSON.stringify(data, null, 2));
      return;
    }
    announce("Sending your question.");
    const form = new FormData();
    form.append("question", t);
    appendTargetLang(form);
    const data = await postForm("/chat", form);
    setAnswer(data.answer || JSON.stringify(data, null, 2));
  } catch (err) {
    setAnswer(`Error: ${err.message}`);
  }
}

function clearRestartTimer() {
  if (restartTimer !== null) {
    window.clearTimeout(restartTimer);
    restartTimer = null;
  }
}

/**
 * Schedule a single recognition.start() after a safe gap. Cancels any previous pending start.
 */
function scheduleRecognitionRestart(delayMs = RESTART_GAP_MS) {
  if (!recognition) return;
  clearRestartTimer();
  restartTimer = window.setTimeout(() => {
    restartTimer = null;
    if (!sessionActive) return;
    if (voiceMode === "manual" && manualStop) return;
    try {
      recognition.start();
      networkRetryCount = 0;
    } catch (err) {
      restartTimer = window.setTimeout(() => {
        restartTimer = null;
        try {
          recognition.start();
          networkRetryCount = 0;
        } catch (err2) {
          sessionActive = false;
          voiceMode = "idle";
          wakeUiPrimed = false;
          setMicUi(false);
          setWakeStatus("");
          const hint =
            "Tip: use Chrome or Edge, stay online (speech uses the network), allow microphone, and tap the mic again.";
          announce(`Speech recognition could not restart. ${hint}`);
          output.textContent = `Speech recognition error: ${err2.message || err2}. ${hint}`;
        }
      }, 700);
    }
  }, delayMs);
}

/** While in manual mode, keep recognition running until user taps stop (browser may fire onend often). */
function shouldRestartManualListening() {
  return sessionActive && voiceMode === "manual" && !manualStop;
}

function setupRecognition() {
  if (!SpeechRecognition) {
    micBtn.disabled = true;
    micLabel.textContent = "Microphone not supported in this browser";
    wakeGlass.disabled = true;
    announce("Voice recognition needs Chrome or Edge on desktop.");
    return;
  }

  recognition = new SpeechRecognition();
  recognition.lang = (voiceLang && voiceLang.value) || "en-US";
  recognition.interimResults = true;
  recognition.continuous = true;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    networkRetryCount = 0;
    if (voiceMode === "manual") {
      lastSpeechAt = Date.now();
      if (!listenCuePlayed) {
        listenCuePlayed = true;
        window.setTimeout(() => playListenBeep(880), 350);
      }
    } else if (voiceMode === "wake") {
      if (!wakeUiPrimed) {
        wakeUiPrimed = true;
        setMicUi(true, "wake");
      }
    }
  };

  recognition.onresult = (ev) => {
    if (voiceMode === "wake") {
      let interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) {
          processWakePhrase(r[0].transcript);
        } else {
          interim += r[0].transcript;
        }
      }
      const last = ev.results[ev.results.length - 1];
      const shown = (last[0].transcript || "") + interim;
      transcriptText.textContent = shown.trim() || "—";
      return;
    }

    if (voiceMode !== "manual") return;

    lastSpeechAt = Date.now();
    let interim = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) {
        accumulatedFinal += r[0].transcript + " ";
      } else {
        interim += r[0].transcript;
      }
    }
    lastInterim = interim;
    const combined = (accumulatedFinal + interim).trim();
    transcriptText.textContent = combined || "—";
    if (combined && !heardSpeechThisSession) {
      heardSpeechThisSession = true;
      if (listenBanner) {
        listenBanner.textContent = "Hearing you — keep going";
      }
      window.setTimeout(() => playListenBeep(990), 0);
    }
  };

  recognition.onend = () => {
    if (switchingToManual) {
      switchingToManual = false;
      voiceMode = "manual";
      sessionActive = true;
      accumulatedFinal = "";
      lastInterim = "";
      manualStop = false;
      lastSpeechAt = Date.now();
      postWakeMode = false;
      if (postWakeTimer) {
        clearTimeout(postWakeTimer);
        postWakeTimer = null;
      }
      setWakeStatus("");
      window.speechSynthesis?.cancel();
      listenCuePlayed = false;
      heardSpeechThisSession = false;
      setMicUi(true, "manual");
      scheduleRecognitionRestart(RESTART_GAP_MS);
      return;
    }

    if (manualStop && voiceMode === "manual") {
      sessionActive = false;
      manualStop = false;
      const text = (accumulatedFinal + lastInterim).trim();
      accumulatedFinal = "";
      lastInterim = "";
      listenCuePlayed = false;
      heardSpeechThisSession = false;
      setMicUi(false);

      if (text) {
        void handleVoiceText(text).catch((err) => {
          setAnswer(`Error: ${err.message}`);
        });
      } else {
        output.textContent =
          "No speech was detected. Tap the microphone, speak clearly, then tap again to send.";
        announce("No speech detected.");
        speakFeedback("I didn't hear anything. Tap the microphone, speak, then tap again when you are done.");
      }

      if (wakeGlass && wakeGlass.checked) {
        voiceMode = "wake";
        sessionActive = true;
        wakeUiPrimed = false;
        setWakeStatus("Say Ok Glass, or tap the microphone.");
        scheduleRecognitionRestart(RESTART_GAP_MS);
      } else {
        voiceMode = "idle";
        setWakeStatus("");
      }
      return;
    }

    if (shouldRestartManualListening()) {
      scheduleRecognitionRestart(RESTART_GAP_MS);
      return;
    }

    if (sessionActive && voiceMode === "wake") {
      scheduleRecognitionRestart(RESTART_GAP_MS);
      return;
    }

    setMicUi(false);
    setWakeStatus(wakeGlass && wakeGlass.checked && voiceMode === "wake" ? "Say Ok Glass, or tap the microphone." : "");
  };

  recognition.onerror = (ev) => {
    if (ev.error === "no-speech") {
      return;
    }
    if (ev.error === "aborted") {
      return;
    }
    if (ev.error === "network" && sessionActive && !manualStop) {
      networkRetryCount += 1;
      if (networkRetryCount <= 4) {
        scheduleRecognitionRestart(600 + networkRetryCount * 200);
        return;
      }
    }
    if (ev.error === "audio-capture") {
      sessionActive = false;
      voiceMode = "idle";
      wakeUiPrimed = false;
      setMicUi(false);
      setWakeStatus("");
      const msg = "No microphone found or it is in use by another app.";
      announce(msg);
      output.textContent = msg;
      return;
    }
    sessionActive = false;
    voiceMode = "idle";
    manualStop = false;
    wakeUiPrimed = false;
    setMicUi(false);
    setWakeStatus("");
    const netHint =
      ev.error === "network"
        ? " Check your internet connection — Chrome sends speech to Google for recognition."
        : "";
    const msg =
      ev.error === "not-allowed"
        ? "Microphone permission denied. Allow the microphone in the browser."
        : `Voice error: ${ev.error}.${netHint}`;
    announce(msg);
    output.textContent = msg;
  };
}

micBtn.addEventListener("click", () => {
  if (!recognition) return;

  if (voiceMode === "manual" && sessionActive) {
    manualStop = true;
    try {
      recognition.stop();
    } catch (_) {
      sessionActive = false;
      manualStop = false;
      setMicUi(false);
    }
    return;
  }

  if (voiceMode === "wake" && sessionActive) {
    switchingToManual = true;
    try {
      recognition.stop();
    } catch (_) {
      switchingToManual = false;
      sessionActive = false;
      voiceMode = "idle";
      setMicUi(false);
    }
    return;
  }

  try {
    window.speechSynthesis?.cancel();
    accumulatedFinal = "";
    lastInterim = "";
    manualStop = false;
    lastSpeechAt = Date.now();
    postWakeMode = false;
    if (postWakeTimer) {
      clearTimeout(postWakeTimer);
      postWakeTimer = null;
    }
    voiceMode = "manual";
    sessionActive = true;
    networkRetryCount = 0;
    listenCuePlayed = false;
    heardSpeechThisSession = false;
    clearRestartTimer();
    setMicUi(true, "manual");
    recognition.start();
  } catch (err) {
    sessionActive = false;
    voiceMode = wakeGlass && wakeGlass.checked ? "wake" : "idle";
    setMicUi(false);
    announce(`Could not start microphone: ${err.message}`);
  }
});

wakeGlass.addEventListener("change", () => {
  if (!recognition || !wakeGlass) return;
  if (wakeGlass.checked) {
    if (voiceMode === "idle" && !sessionActive) {
      voiceMode = "wake";
      sessionActive = true;
      wakeUiPrimed = false;
      networkRetryCount = 0;
      clearRestartTimer();
      postWakeMode = false;
      setWakeStatus("Say Ok Glass, or tap the microphone.");
      try {
        recognition.start();
      } catch (err) {
        wakeGlass.checked = false;
        voiceMode = "idle";
        sessionActive = false;
        wakeUiPrimed = false;
        announce(`Could not start wake listening: ${err.message}`);
      }
    }
  } else {
    postWakeMode = false;
    if (postWakeTimer) {
      clearTimeout(postWakeTimer);
      postWakeTimer = null;
    }
    setWakeStatus("");
    if (voiceMode === "wake" && sessionActive) {
      sessionActive = false;
      voiceMode = "idle";
      wakeUiPrimed = false;
      try {
        recognition.stop();
      } catch (_) {
        /* ignore */
      }
    }
  }
});

document.getElementById("openCamBtn").addEventListener("click", async () => {
  try {
    await startCamera();
  } catch (err) {
    setAnswer(`Camera error: ${err.message}`);
  }
});

document.getElementById("askVision").addEventListener("click", async () => {
  if (!cameraIsActive()) {
    setAnswer("Open the camera first.");
    announce("Open the camera first.");
    return;
  }
  try {
    const blob = await captureBlob();
    const form = new FormData();
    form.append("question", question.value || "What am I seeing? Describe what is in the image.");
    form.append("image", blob, "frame.jpg");
    appendTargetLang(form);
    const data = await postForm("/vision", form);
    setAnswer(data.answer || JSON.stringify(data, null, 2));
  } catch (err) {
    setAnswer(`Vision error: ${err.message}`);
  }
});

document.getElementById("readText").addEventListener("click", async () => {
  if (!cameraIsActive()) {
    setAnswer("Open the camera first.");
    announce("Open the camera first.");
    return;
  }
  try {
    const blob = await captureBlob();
    const form = new FormData();
    form.append("image", blob, "frame.jpg");
    appendTargetLang(form);
    const data = await postForm("/ocr", form);
    setAnswer(ocrJsonToPlain(data));
  } catch (err) {
    setAnswer(`OCR error: ${err.message}`);
  }
});

document.getElementById("askChat").addEventListener("click", async () => {
  try {
    await handleVoiceText(question.value || "Hello");
  } catch (err) {
    setAnswer(`Error: ${err.message}`);
  }
});

repeatAnswer.addEventListener("click", () => {
  if (lastAnswerText) speak(lastAnswerText);
  else announce("No answer yet.");
});

if (voiceLang) {
  voiceLang.addEventListener("change", () => {
    const v = voiceLang.value;
    if (recognition) recognition.lang = v;
    document.documentElement.lang = v;
    announce(`Speech language set to ${voiceLang.options[voiceLang.selectedIndex].text}`);
  });
}

/** Vision helper for chip actions with a fixed question. */
async function runVisionQuestion(q) {
  const t = (q || "").trim();
  question.value = t;
  if (!cameraIsActive()) {
    try {
      await startCamera();
    } catch (err) {
      setAnswer(`Camera error: ${err.message}`);
      return;
    }
  }
  try {
    announce("Analyzing the camera view.");
    const blob = await captureBlob();
    const form = new FormData();
    form.append("question", t);
    form.append("image", blob, "frame.jpg");
    appendTargetLang(form);
    const data = await postForm("/vision", form);
    setAnswer(data.answer || JSON.stringify(data, null, 2));
  } catch (err) {
    setAnswer(`Error: ${err.message}`);
  }
}

document.getElementById("chipOpenCamera")?.addEventListener("click", async () => {
  try {
    await startCamera();
  } catch (err) {
    setAnswer(`Camera error: ${err.message}`);
  }
});

document.getElementById("chipAskAnything")?.addEventListener("click", () => {
  question.scrollIntoView({ behavior: "smooth", block: "center" });
  question.focus();
  announce("Ask your question in the box, or tap the microphone to speak.");
});

document.getElementById("chipReadImage")?.addEventListener("click", () => {
  document.getElementById("readText")?.click();
});

document.getElementById("chipDanger")?.addEventListener("click", () => {
  void runVisionQuestion(
    "Detect any danger, hazards, or unsafe conditions in this scene. Briefly describe risks and safety tips."
  );
});

document.getElementById("chipRecognizePerson")?.addEventListener("click", () => {
  void runVisionQuestion(
    "Who is in this image? Describe the person you see — clothing, approximate age range, and context. If you cannot identify a specific individual, say so clearly."
  );
});

/* —— Spatial safety: live frames → /spatial-safety, alarms + TTS —— */
const safetyStrip = document.getElementById("safetyStrip");
const safetyStateText = document.getElementById("safetyStateText");
const safetyGuidanceText = document.getElementById("safetyGuidanceText");
const safetyEmergencyAnnouncer = document.getElementById("safetyEmergencyAnnouncer");
const spatialSafetyOn = document.getElementById("spatialSafetyOn");
const spatialAudioOn = document.getElementById("spatialAudioOn");
const peopleTableBody = document.getElementById("peopleTableBody");

const SPATIAL_INTERVAL_MS = 1300;
const RISK_RANK = { safe: 0, caution: 1, obstacle: 2, emergency: 3 };

let spatialTimer = null;
let spatialInFlight = false;
let lastSpatialRisk = "off";
let lastGuidanceSpoken = "";
let lastGuidanceSpokenAt = 0;
/** @type {Record<string, number>} */
let lastAlarmAt = {};

function resetSafetyUiOff() {
  if (!safetyStrip || !safetyStateText || !safetyGuidanceText) return;
  safetyStrip.className = "safety-strip safety-strip--off";
  safetyStateText.textContent = "Off";
  safetyGuidanceText.textContent = "";
  if (safetyEmergencyAnnouncer) safetyEmergencyAnnouncer.textContent = "";
  lastSpatialRisk = "off";
}

const SAFETY_LABELS = {
  safe: "Safe",
  caution: "Caution",
  obstacle: "Obstacle detected",
  emergency: "Emergency — stop",
  standby: "Standby",
  off: "Off",
};

/**
 * @param {'safe'|'caution'|'obstacle'|'emergency'|'standby'|'off'} level
 * @param {string} guidance
 */
function setSafetyUi(level, guidance) {
  if (!safetyStrip || !safetyStateText || !safetyGuidanceText) return;
  safetyStateText.textContent = SAFETY_LABELS[level] || SAFETY_LABELS.safe;
  safetyGuidanceText.textContent = guidance || "";
  safetyStrip.className = "safety-strip safety-strip--" + level;
}

function stopSpatialSafetyLoop() {
  if (spatialTimer !== null) {
    clearInterval(spatialTimer);
    spatialTimer = null;
  }
  spatialInFlight = false;
}

function syncSpatialSafetyWithCamera() {
  stopSpatialSafetyLoop();
  if (!cameraIsActive()) {
    if (spatialSafetyOn?.checked) {
      setSafetyUi("standby", "Open the camera to start live obstacle detection.");
      lastSpatialRisk = "standby";
    } else {
      resetSafetyUiOff();
    }
    return;
  }
  if (!spatialSafetyOn?.checked) {
    setSafetyUi("standby", "Turn on live obstacle detection to scan for obstacles, walls, people, and steps.");
    lastSpatialRisk = "standby";
    return;
  }
  setSafetyUi("standby", "Scanning…");
  void spatialTick();
  spatialTimer = window.setInterval(() => void spatialTick(), SPATIAL_INTERVAL_MS);
}

async function spatialTick() {
  if (!spatialSafetyOn?.checked || !cameraIsActive()) return;
  if (spatialInFlight) return;
  if (!camera.videoWidth) return;
  spatialInFlight = true;
  try {
    const blob = await captureBlob();
    const form = new FormData();
    form.append("image", blob, "frame.jpg");
    appendTargetLang(form);
    const res = await fetch("/spatial-safety", { method: "POST", body: form });
    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      data = {};
    }
    if (!res.ok) {
      const d = data.detail;
      const msg =
        typeof d === "string"
          ? d
          : Array.isArray(d)
            ? d.map((x) => x.msg || JSON.stringify(x)).join("; ")
            : JSON.stringify(data);
      throw new Error(msg || res.statusText);
    }
    applySpatialResult(data);
  } catch (e) {
    console.warn("spatial-safety", e);
    setSafetyUi("caution", "Could not analyze this frame. Check network and try again.");
  } finally {
    spatialInFlight = false;
  }
}

/**
 * @param {any} data
 */
function applySpatialResult(data) {
  const level = String(data.risk_level || "safe").toLowerCase();
  const guidance = String(data.guidance || "").trim();
  const prev = lastSpatialRisk;
  lastSpatialRisk = level;

  const uiLevel = ["safe", "caution", "obstacle", "emergency"].includes(level) ? level : "safe";
  setSafetyUi(uiLevel, guidance);

  if (safetyEmergencyAnnouncer) {
    if (level === "emergency" && prev !== "emergency") {
      safetyEmergencyAnnouncer.textContent = guidance || "Emergency. Please stop.";
    } else if (level !== "emergency") {
      safetyEmergencyAnnouncer.textContent = "";
    }
  }

  const rank = RISK_RANK[level] ?? 0;
  if (spatialAudioOn?.checked && rank >= RISK_RANK.caution) {
    maybePlaySpatialAlarm(level);
  }
  if (spatialAudioOn?.checked && guidance && rank >= RISK_RANK.caution) {
    maybeSpeakSpatial(guidance, level, prev);
  }
}

/**
 * @param {string} level
 */
function maybePlaySpatialAlarm(level) {
  const now = Date.now();
  const throttle =
    level === "emergency" ? 1400 : level === "obstacle" ? 2200 : 4500;
  const key = level === "emergency" ? "emergency" : level === "obstacle" ? "obstacle" : "caution";
  if (now - (lastAlarmAt[key] || 0) < throttle) return;
  lastAlarmAt[key] = now;
  playSpatialAlarm(level);
}

/**
 * @param {string} level
 */
function playSpatialAlarm(level) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const master = ctx.createGain();
    master.gain.value = 0.22;
    master.connect(ctx.destination);
    const t0 = ctx.currentTime;

    const beep = (start, freq, dur) => {
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = level === "emergency" ? "square" : "sine";
      osc.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, start);
      g.gain.linearRampToValueAtTime(0.35, start + 0.02);
      g.gain.linearRampToValueAtTime(0.0001, start + dur);
      osc.connect(g);
      g.connect(master);
      osc.start(start);
      osc.stop(start + dur + 0.02);
    };

    if (level === "emergency") {
      for (let i = 0; i < 5; i++) {
        beep(t0 + i * 0.11, 920 + (i % 2) * 100, 0.08);
      }
    } else if (level === "obstacle") {
      beep(t0, 440, 0.1);
      beep(t0 + 0.16, 440, 0.1);
      beep(t0 + 0.32, 520, 0.12);
    } else {
      beep(t0, 330, 0.14);
    }

    window.setTimeout(() => {
      try {
        ctx.close();
      } catch (_) {
        /* ignore */
      }
    }, 900);
  } catch (_) {
    /* ignore */
  }
}

/**
 * @param {string} text
 * @param {string} level
 * @param {string} prevLevel
 */
function maybeSpeakSpatial(text, level, prevLevel) {
  const now = Date.now();
  const rank = RISK_RANK[level] ?? 0;
  const prevRank = RISK_RANK[prevLevel] ?? -1;
  const same = text === lastGuidanceSpoken;
  const cooldown = 3200;
  if (same && now - lastGuidanceSpokenAt < cooldown && rank <= prevRank) return;
  lastGuidanceSpoken = text;
  lastGuidanceSpokenAt = now;
  try {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = getTargetLang();
    u.rate = level === "emergency" ? 1.08 : 1;
    u.volume = 1;
    window.speechSynthesis.speak(u);
  } catch (_) {
    /* ignore */
  }
}

spatialSafetyOn?.addEventListener("change", () => {
  syncSpatialSafetyWithCamera();
});

function renderPeopleTable(people) {
  if (!peopleTableBody) return;
  peopleTableBody.innerHTML = "";
  if (!Array.isArray(people) || !people.length) return;
  const fmtTime = (ts) => {
    if (!ts) return "";
    try {
      const d = new Date(ts * 1000);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch {
      return "";
    }
  };
  for (const p of people) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = p.name || "";
    const labelTd = document.createElement("td");
    labelTd.textContent = p.label || "";
    const lastTd = document.createElement("td");
    lastTd.textContent = fmtTime(p.last_seen);
    const timesTd = document.createElement("td");
    timesTd.textContent = String(p.times_seen ?? "");
    const statusTd = document.createElement("td");
    statusTd.textContent = p.status || "";
    tr.appendChild(nameTd);
    tr.appendChild(labelTd);
    tr.appendChild(lastTd);
    tr.appendChild(timesTd);
    tr.appendChild(statusTd);
    peopleTableBody.appendChild(tr);
  }
}

document.getElementById("faceInspect")?.addEventListener("click", async () => {
  if (!cameraIsActive()) {
    setAnswer("Open the camera first.");
    announce("Open the camera first.");
    return;
  }
  try {
    const blob = await captureBlob();
    const form = new FormData();
    form.append("image", blob, "frame.jpg");
    appendTargetLang(form);
    const data = await postForm("/face-inspect", form);
    if (data.summary) {
      setAnswer(data.summary);
    }
    if (Array.isArray(data.people)) {
      renderPeopleTable(data.people);
    }
  } catch (err) {
    setAnswer(`Face error: ${err.message}`);
  }
});

setupRecognition();
