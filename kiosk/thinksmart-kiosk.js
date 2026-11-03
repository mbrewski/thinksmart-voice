(() => {
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
    error: "/faces/idle.jpg",
    sleep: "/faces/idle.jpg",
  };

  let rec = false;
  let busy = false;
  let screenBlank = false;
  let startedAt = 0;
  let localHold = false;
  let faceMode = "";
  const preloaded = {};

  function preloadUrl(src) {
    if (!src || preloaded[src]) return;
    const im = new Image();
    im.src = src;
    preloaded[src] = im;
  }

  Object.values(faces).forEach(preloadUrl);

  function startAnim(name) {
    if (stageEl) stageEl.className = name;
    faceEl.src = faces[name] || faces.idle;
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

  async function startRec() {
    if (screenBlank || rec || faceMode === "sleep") return;
    if (busy && faceMode !== "speak") return;
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
