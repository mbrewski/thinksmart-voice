(() => {
  const STREAM = "";
  const faceEl = document.getElementById("face");
  const stageEl = document.getElementById("face-stage");
  const hintEl = document.getElementById("hint");
  const timeEl = document.getElementById("clock-time");
  const dateEl = document.getElementById("clock-date");
  const blankEl = document.getElementById("blank");
  const player = document.getElementById("player");

  let clockTz = "UTC";
  let timeFmt = makeTimeFmt(clockTz);
  let dateFmt = makeDateFmt(clockTz);

  function makeTimeFmt(tz) {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function makeDateFmt(tz) {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      weekday: "long",
      month: "long",
      day: "numeric",
    });
  }

  function applyTz(tz) {
    if (!tz || tz === clockTz) return;
    try {
      const tf = makeTimeFmt(tz);
      const df = makeDateFmt(tz);
      clockTz = tz;
      timeFmt = tf;
      dateFmt = df;
    } catch {}
  }

  function tickClock() {
    const now = new Date();
    if (timeEl) timeEl.textContent = timeFmt.format(now);
    if (dateEl) dateEl.textContent = dateFmt.format(now);
  }

  const faces = {
    idle: "/faces/idle.jpg",
    listen: "/faces/listen.jpg",
    speak: "/faces/speak.jpg",
    thinking: "/faces/idle.jpg",
    error: "/faces/error.jpg",
    sleep: "/faces/blink/idle.jpg",
  };
  const blinks = { idle: "/faces/blink/idle.jpg" };

  let rec = false;
  let busy = false;
  let screenBlank = false;
  let startedAt = 0;
  let audioCtx = null;
  let localHold = false;
  let faceMode = "";
  let blinkTimer = 0;
  let streamOk = false;
  let streamErrors = 0;
  let prevUrl = "";
  let polling = false;
  const preloaded = {};

  function preloadUrl(src) {
    if (!src || preloaded[src] || streamOk) return;
    const im = new Image();
    im.src = src;
    preloaded[src] = im;
  }

  function stopAnim() {
    if (blinkTimer) {
      clearTimeout(blinkTimer);
      blinkTimer = 0;
    }
  }

  function startIdleBlink(name) {
    const openSrc = faces[name] || faces.idle;
    const blinkSrc = blinks[name];
    faceEl.src = openSrc;
    if (!blinkSrc) return;
    preloadUrl(blinkSrc);
    const schedule = () => {
      const wait = 3200 + Math.floor(Math.random() * 2800);
      blinkTimer = setTimeout(() => {
        if (faceMode !== name || streamOk) return;
        faceEl.src = blinkSrc;
        blinkTimer = setTimeout(() => {
          if (faceMode === name && !streamOk) faceEl.src = openSrc;
          schedule();
        }, 140);
      }, wait);
    };
    schedule();
  }

  function pushFace(name) {
    fetch("/bot/face?name=" + encodeURIComponent(name)).catch(() => {});
  }

  function startAnim(name) {
    stopAnim();
    if (stageEl) stageEl.className = name;
    pushFace(name);
    if (streamOk) return;
    if (name === "sleep") {
      faceEl.src = faces.sleep;
      return;
    }
    startIdleBlink(name);
  }

  function setFace(name, status, caption, hint) {
    const mode = name === "sleep" || status === "muted" ? "sleep" : status === "thinking" ? "thinking" : name || "idle";
    if (mode !== faceMode) {
      faceMode = mode;
      startAnim(mode);
    }
    if (!hintEl) return;
    if (hint !== undefined) hintEl.textContent = hint;
    else if (status === "ready") hintEl.textContent = "say hey grok, or press to talk";
    else if (status === "listening") hintEl.textContent = "Listening";
    else hintEl.textContent = "";
  }

  function setBlank(on) {
    screenBlank = !!on;
    if (blankEl) blankEl.classList.toggle("on", screenBlank);
  }

  async function unblank() {
    setBlank(false);
    try {
      await fetch("/unblank", { method: "POST" });
    } catch {}
  }

  async function pollJpg() {
    if (polling || screenBlank) return;
    polling = true;
    try {
      const res = await fetch("/bot.jpg?r=" + Date.now(), { cache: "no-store" });
      if (!res.ok) throw new Error("jpg");
      const blob = await res.blob();
      if (blob.size < 200) throw new Error("empty");
      const url = URL.createObjectURL(blob);
      faceEl.src = url;
      if (prevUrl) URL.revokeObjectURL(prevUrl);
      prevUrl = url;
      if (!streamOk) stopAnim();
      streamOk = true;
      streamErrors = 0;
    } catch {
      streamErrors += 1;
      if (streamErrors >= 3 && streamOk) {
        streamOk = false;
        startIdleBlink(faceMode || "idle");
      }
    } finally {
      polling = false;
    }
  }

  function playWavB64(b64) {
    if (!b64) return;
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: "audio/wav" });
    player.src = URL.createObjectURL(blob);
    player.volume = 1;
    player.muted = false;
    const p = player.play();
    if (p && p.catch) p.catch(() => {});
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = audioCtx || new AC();
      if (!player._boost) {
        const src = audioCtx.createMediaElementSource(player);
        const gain = audioCtx.createGain();
        gain.gain.value = 1.6;
        src.connect(gain);
        gain.connect(audioCtx.destination);
        player._boost = gain;
      }
      if (audioCtx.state === "suspended") audioCtx.resume();
    } catch {}
  }

  async function startRec() {
    if (screenBlank || busy || rec || faceMode === "sleep") return;
    rec = true;
    localHold = true;
    startedAt = Date.now();
    try {
      player.pause();
      player.removeAttribute("src");
    } catch {}
    setFace("listen", "listening", "", "Listening");
    try {
      const res = await fetch("/rec/start", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error("record failed");
    } catch (err) {
      rec = false;
      localHold = false;
      setFace("error", "mic error", String(err.message || err));
    }
  }

  async function stopRec() {
    if (!rec || busy) return;
    const held = Date.now() - startedAt;
    if (held < 900) {
      setTimeout(stopRec, 900 - held);
      return;
    }
    rec = false;
    busy = true;
    setFace("idle", "thinking", "");
    try {
      const res = await fetch("/rec/stop", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "turn failed");
      setFace("idle", "ready", "", "say hey grok, or press to talk");
      busy = false;
      localHold = false;
    } catch (err) {
      setFace("error", "error", String(err.message || err));
      busy = false;
      localHold = false;
    }
  }

  document.body.addEventListener("pointerdown", (ev) => {
    if (!ev.isPrimary) return;
    ev.preventDefault();
    if (screenBlank) {
      unblank();
      return;
    }
    try {
      document.body.setPointerCapture(ev.pointerId);
    } catch {}
    startRec();
  });
  document.body.addEventListener("pointerup", (ev) => {
    if (!ev.isPrimary) return;
    ev.preventDefault();
    stopRec();
  });
  document.body.addEventListener("pointercancel", (ev) => {
    if (!ev.isPrimary) return;
    stopRec();
  });
  document.body.addEventListener("contextmenu", (ev) => ev.preventDefault());
  setFace("idle", "ready", "", "say hey grok, or press to talk");
  tickClock();
  setInterval(tickClock, 1000);
  pollJpg();
  setInterval(pollJpg, 120);
  setInterval(async () => {
    if (rec) return;
    try {
      const s = await fetch("/state").then((r) => r.json());
      applyTz(s.tz);
      setBlank(!!s.blank);
      setFace(s.face || "idle", s.status || "", s.caption, s.hint);
      busy = !!s.busy;
    } catch {}
  }, 300);
})();
