#!/usr/bin/env python3
"""Face kiosk: hold-to-talk and Hey Grok; Grok Voice on-device."""
import array
import base64
import fcntl
import http.server
import io
import json
import os
import select
import shutil
import signal
import sys
import http.client
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from thinksmart_audio import audioop, chunk_bytes, ingest, pcm_wav, pw_record_argv, rms, write_private
from thinksmart_config import (
    BUSY,
    RATE_IN,
    RATE_OUT,
    TMPDIR,
    ensure_tmpdir,
    load_kiosk_env,
    pipewire_env,
    rotating_log,
)

load_kiosk_env(HERE)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, WebKit


if not (os.environ.get("XAI_API_KEY") or "").strip():
    raise SystemExit("XAI_API_KEY is unset; set it in /etc/thinksmart-kiosk.env or kiosk.env")

CLOCK_TZ = os.environ.get("CLOCK_TZ", os.environ.get("TZ", "UTC"))
PORT = 1680
URL = f"http://127.0.0.1:{PORT}/thinksmart-kiosk.html"
ensure_tmpdir()
REC_WAV = os.path.join(TMPDIR, "rec.wav")
HINT_IDLE = "say hey grok, or press to talk"
HINT_LISTEN = "Listening"
HINT_MUTE = "mic muted"
SW_MICROPHONE_INSERT = 4
MUTE_DEV = os.environ.get("THINKSMART_MUTE_DEV", "/dev/input/event0")
FACE_STREAM = (os.environ.get("FACE_STREAM") or "").strip()

MIN_CMD = RATE_OUT * 2 * 8 // 10
MAX_CMD = RATE_OUT * 2 * 10
SILENCE_MS = 1600
NO_SPEECH_MS = 5000
FOLLOWUP_NO_SPEECH_MS = 12000
SPEECH_RMS = 1600
FOLLOWUP_SPEECH_RMS = 1600
FOLLOWUP_SPEECH_FRAMES = 2
NEAR_RMS = 3500
FOLLOWUP_NEAR_RMS = 3500
MAX_FOLLOWUPS = 8
try:
    BLANK_SEC = max(5, int(os.environ.get("SCREEN_IDLE_SEC", "60")))
except ValueError:
    BLANK_SEC = 60

_lock = threading.Lock()
_mode = "idle"
_stop = threading.Event()
_result = {}
_followup_depth = 0
_followup_skip = False
_take_hold = False
_mic_muted = False
_last_activity = time.time()
_play_lock = threading.Lock()
_play_proc: subprocess.Popen | None = None
_play_vol_restore: float | None = None
_state = {
    "face": "idle",
    "status": "ready",
    "caption": "",
    "hint": HINT_IDLE,
    "busy": False,
    "tz": CLOCK_TZ,
    "blank": False,
    "mode": "idle",
}

FACE_POSE = {
    "listen": "wide",
    "speak": "notify",
    "error": "alert",
    "thinking": "thinking",
    "idle": "idle",
    "sleep": "sleep",
}


def _env():
    return pipewire_env()


def _log(msg: str) -> None:
    rotating_log("/tmp/kiosk-face.log", msg)


def _push_face(name: str) -> None:
    def _go() -> None:
        try:
            pose = FACE_POSE.get(name, name)
            urllib.request.urlopen(f"{FACE_STREAM}/face?name={pose}", timeout=1).read()
        except OSError:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _read_mic_muted() -> bool:
    """Hardware slider: SW_MICROPHONE_INSERT off means the mics are cut."""
    try:
        fd = os.open(MUTE_DEV, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        buf = array.array("B", [0] * 16)
        req = (2 << 30) | (16 << 16) | (ord("E") << 8) | 0x1B
        fcntl.ioctl(fd, req, buf, True)
        bits = int.from_bytes(buf.tobytes(), "little")
        return not bool(bits & (1 << SW_MICROPHONE_INSERT))
    except OSError:
        return False
    finally:
        os.close(fd)


def _set_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)
        if _mic_muted:
            _state["face"] = "sleep"
            _state["status"] = "muted"
            _state["hint"] = HINT_MUTE
            face = "sleep"
        else:
            face = _state.get("face") or "idle"
            if _state.get("status") == "thinking":
                face = "thinking"
        _state["mode"] = _mode
    _push_face(face)


def _apply_mute(muted: bool) -> None:
    global _mic_muted
    with _lock:
        if muted == _mic_muted:
            return
        _mic_muted = muted
    _log("mic-mute on" if muted else "mic-mute off")
    if muted:
        _stop.set()
        _set_state(face="sleep", status="muted", caption="", hint=HINT_MUTE, busy=False)
    else:
        _set_state(face="idle", status="ready", caption="", hint=HINT_IDLE, busy=False)


def _mute_watch() -> None:
    while True:
        try:
            _apply_mute(_read_mic_muted())
        except Exception as e:
            _log(f"mic-mute watch {e}")
        time.sleep(0.25)


def _poke() -> None:
    """Keep the glass on; first tap or wakeword after idle calls this."""
    global _last_activity
    with _lock:
        _last_activity = time.time()
        was = bool(_state.get("blank"))
        _state["blank"] = False
    if was:
        _log("screen on")
        _panel_on()


def _blank_watch() -> None:
    while True:
        time.sleep(1)
        with _lock:
            if _mode != "idle" or _state.get("busy") or _state.get("blank"):
                continue
            if time.time() - _last_activity < BLANK_SEC:
                continue
            _state["blank"] = True
        _log("screen off")


def _busy_on() -> None:
    try:
        open(BUSY, "w").write("1")
    except OSError:
        pass


def _busy_off() -> None:
    try:
        os.remove(BUSY)
    except FileNotFoundError:
        pass


def _panel_on() -> None:
    env = _env()
    for cmd in (
        ["kscreen-doctor", "--dpms", "on"],
        ["brightnessctl", "set", "100%"],
    ):
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _unmute():
    script = "/usr/local/bin/mic-unmute.sh"
    if os.path.isfile(script):
        subprocess.Popen(["/bin/bash", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wpctl(*args):
    subprocess.run(["wpctl", *args], env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _speaker_on():
    _wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")


def _speaker_mute():
    _wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "1")


def _wav_seconds(data: bytes) -> float:
    try:
        w = wave.open(io.BytesIO(data))
        return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _pcm_wav(pcm: bytes, rate: int = RATE_OUT) -> bytes:
    return pcm_wav(pcm, rate)


def _rms(pcm: bytes) -> int:
    return rms(pcm)


def _sink_vol() -> float:
    try:
        out = subprocess.check_output(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            env=_env(),
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return 1.0
    for tok in out.replace(",", " ").split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 1.0


def _fade_s16_stereo(pcm: bytes, rate: int = RATE_IN, ms: int = 18) -> bytes:
    samples = array.array("h")
    samples.frombytes(pcm)
    nch = 2
    frames = len(samples) // nch
    fade = min(int(rate * ms / 1000), max(1, frames // 4))
    for i in range(fade):
        g_in = i / fade
        g_out = (fade - 1 - i) / fade
        for c in range(nch):
            samples[i * nch + c] = int(samples[i * nch + c] * g_in)
            samples[(frames - 1 - i) * nch + c] = int(samples[(frames - 1 - i) * nch + c] * g_out)
    return samples.tobytes()


def _to_s16_stereo_48k(raw: bytes) -> bytes:
    with wave.open(io.BytesIO(raw), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate() or 24000
        pcm = wf.readframes(wf.getnframes())
    if sw != 2:
        pcm = audioop.lin2lin(pcm, sw, 2)
    if ch == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif ch != 1:
        pcm = audioop.tomono(pcm, 2, 1.0, 0.0)
    if rate != RATE_IN:
        pcm, _st = audioop.ratecv(pcm, 2, 1, rate, RATE_IN, None)
    pcm = audioop.tostereo(pcm, 2, 1.0, 1.0)
    pad = b"\x00\x00\x00\x00" * int(RATE_IN * 0.06)
    pcm = _fade_s16_stereo(pcm)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(RATE_IN)
        wf.writeframes(pad + pcm)
    return buf.getvalue()


def _stop_play() -> None:
    global _play_proc, _play_vol_restore, _followup_skip
    _followup_skip = True
    with _play_lock:
        proc = _play_proc
        _play_proc = None
        restore = _play_vol_restore
        _play_vol_restore = None
    if proc is not None and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        _log("tts interrupted")
    if restore is not None:
        _wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", f"{restore:.2f}")


def _play_file(path: str, dur: float) -> None:
    global _play_proc, _followup_skip, _mode
    _followup_skip = False
    with _lock:
        if _mode in ("idle", "busy"):
            _mode = "speak"
    env = _env()
    proc = subprocess.Popen(
        ["pw-play", "--latency=120ms", path],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with _play_lock:
        _play_proc = proc

    def _wait() -> None:
        global _play_proc, _play_vol_restore, _mode
        try:
            proc.wait(timeout=max(8, int(dur) + 6))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        with _play_lock:
            if _play_proc is proc:
                _play_proc = None
                restore = _play_vol_restore
                _play_vol_restore = None
            else:
                restore = None
        if restore is not None:
            _wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", f"{restore:.2f}")
        rc = proc.returncode
        if rc not in (0, None, -signal.SIGTERM, -signal.SIGKILL) and shutil.which("paplay"):
            subprocess.run(
                ["paplay", path],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(8, int(dur) + 6),
                check=False,
            )
        interrupted = _followup_skip or rc in (-signal.SIGTERM, -signal.SIGKILL)
        _busy_on()
        with _lock:
            still_ours = _mode in ("speak", "busy")
        _log(f"tts done rc={rc} skip={_followup_skip} mode={_mode} ours={still_ours}")
        if interrupted:
            with _lock:
                if _mode in ("speak", "busy"):
                    _mode = "idle"
            _set_state(face="idle", hint=HINT_IDLE, busy=False)
            _busy_off()
            return
        if not still_ours:
            return
        threading.Thread(target=_maybe_followup, daemon=True).start()

    threading.Thread(target=_wait, daemon=True).start()


def _play_b64(b64: str) -> float:
    if not b64:
        return 0.0
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return 0.0
    path = os.path.join(TMPDIR, "tts.wav")
    hot = os.path.join(TMPDIR, "tts-hot.wav")
    try:
        write_private(path, raw)
    except OSError:
        return 0.0
    dur = _wav_seconds(raw)
    try:
        cooked = _to_s16_stereo_48k(raw)
        write_private(hot, cooked)
        play = hot
    except Exception as e:
        _log(f"tts resample fail {e}")
        play = path
    global _play_vol_restore
    prev = _sink_vol()
    play_vol = min(prev, 1.0)
    _speaker_on()
    if play_vol + 0.001 < prev:
        _play_vol_restore = prev
        _wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", f"{play_vol:.2f}")
    _play_file(play, dur)
    return dur


def _post_turn(pcm: bytes) -> dict:
    if len(pcm) < MIN_CMD:
        return {"transcript": "", "reply": "I did not catch that.", "audio_b64": "", "duration": 0}
    wav = _pcm_wav(pcm)
    try:
        write_private(REC_WAV, wav)
    except OSError:
        pass
    try:
        import thinksmart_voice

        _log(f"turn pcm={len(pcm)}")
        data = thinksmart_voice.complete_turn(pcm)
        _log(f"turn tx={(data.get('transcript') or '')[:80]!r} err={data.get('error','')}")
    except Exception as e:
        _speaker_on()
        return {"transcript": "", "reply": "server error", "error": str(e), "audio_b64": "", "duration": 0}
    heard = (data.get("transcript") or "").strip()
    audio = data.get("audio_b64") or ""
    if not heard or not audio:
        data["duration"] = 0
        return data
    _set_state(face="speak", status="speaking", caption=data.get("reply") or "", hint=HINT_IDLE, busy=False)
    dur = _play_b64(audio)
    data["duration"] = dur
    _speaker_on()
    return data


def _record_stream(
    auto: bool,
    no_speech_ms: int = NO_SPEECH_MS,
    speech_rms: int = SPEECH_RMS,
    min_speech_frames: int = 1,
    near_rms: int = NEAR_RMS,
) -> bytes:
    proc = subprocess.Popen(
        pw_record_argv("50ms"),
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    buf = bytearray()
    rv_state = None
    heard = False
    speech_frames = 0
    floor = 400.0
    peak = 0
    t0 = time.time()
    last_voice = t0
    last_busy = t0
    time.sleep(0.25)
    try:
        while proc.poll() is None:
            hold = (not auto) or _take_hold
            if _stop.is_set() and hold:
                break
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if not ready:
                if time.time() - t0 > 25:
                    break
                continue
            raw = proc.stdout.read(chunk_bytes())
            if not raw:
                break
            pcm, rv_state = ingest(raw, rv_state)
            if not pcm:
                continue
            buf.extend(pcm)
            now = time.time()
            if now - last_busy >= 5:
                _busy_on()
                last_busy = now
            if auto and not _take_hold:
                r = _rms(pcm)
                if r > peak:
                    peak = r
                thresh = max(speech_rms, near_rms, int(floor * 3.2))
                if r < thresh:
                    floor = floor * 0.9 + r * 0.1
                    speech_frames = 0
                else:
                    speech_frames += 1
                    if speech_frames >= min_speech_frames:
                        heard = True
                        last_voice = now
                if len(buf) >= MAX_CMD:
                    break
                if heard and (now - last_voice) * 1000 >= SILENCE_MS and len(buf) >= MIN_CMD:
                    break
                if not heard and (now - t0) * 1000 >= no_speech_ms:
                    buf = bytearray()
                    break
            elif len(buf) >= MAX_CMD:
                break
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    _log(f"rec auto={auto} heard={heard} peak={peak} bytes={len(buf)} floor={int(floor)}")
    return bytes(buf)


def _maybe_followup() -> None:
    global _mode
    time.sleep(0.25)
    if _followup_skip or _mic_muted:
        _log(f"followup skip skip={_followup_skip} mute={_mic_muted}")
        _busy_off()
        return
    with _lock:
        if _mode not in ("idle", "speak", "busy"):
            _log(f"followup skip mode={_mode}")
            return
        if _followup_depth >= MAX_FOLLOWUPS:
            _mode = "idle"
            _busy_off()
            return
    _log("followup listen")
    _start_rec(True, followup=True)


def _run_turn(auto: bool, followup: bool = False) -> dict:
    global _mode, _result, _followup_depth
    _busy_on()
    _speaker_mute()
    try:
        _set_state(
            face="listen",
            status="listening",
            caption="",
            hint=HINT_LISTEN,
            busy=False,
        )
        pcm = _record_stream(
            auto,
            FOLLOWUP_NO_SPEECH_MS if followup else NO_SPEECH_MS,
            speech_rms=FOLLOWUP_SPEECH_RMS if followup else SPEECH_RMS,
            min_speech_frames=FOLLOWUP_SPEECH_FRAMES if followup else 2,
            near_rms=FOLLOWUP_NEAR_RMS if followup else NEAR_RMS,
        )
        _speaker_on()
        if _mic_muted:
            _set_state(face="sleep", status="muted", caption="", hint=HINT_MUTE, busy=False)
            with _lock:
                _mode = "idle"
                _result = {}
            _followup_depth = 0
            _busy_off()
            return {}
        if followup and len(pcm) < MIN_CMD:
            _log(f"followup empty pcm={len(pcm)}")
            _set_state(face="idle", status="ready", caption="", hint=HINT_IDLE, busy=False)
            with _lock:
                _mode = "idle"
                _result = {}
            _followup_depth = 0
            _busy_off()
            return {}
        _set_state(face="idle", status="thinking", hint="", busy=True)
        with _lock:
            _mode = "busy"
        data = _post_turn(pcm)
        reply = data.get("reply") or ""
        heard = (data.get("transcript") or "").strip()
        if followup and not heard:
            _set_state(face="idle", status="ready", caption="", hint=HINT_IDLE, busy=False)
            with _lock:
                _mode = "idle"
                _result = data
            _followup_depth = 0
            _busy_off()
            return data
        speaking = bool(data.get("audio_b64"))
        with _lock:
            mode_now = _mode
            _result = data
            if speaking and mode_now == "busy":
                _mode = "speak"
                mode_now = "speak"
            elif not speaking:
                _mode = "idle"
                mode_now = "idle"
        if mode_now in ("listen", "hold"):
            return data
        _set_state(
            face="speak" if speaking else "idle",
            status="speaking" if speaking else (heard or "ready"),
            caption=reply,
            hint=HINT_IDLE,
            busy=False,
        )
        if not speaking:
            _followup_depth = 0
            _busy_off()
        return data
    except Exception:
        with _lock:
            _mode = "idle"
            _result = {}
        _set_state(face="idle", status="ready", caption="", hint=HINT_IDLE, busy=False)
        _busy_off()
        raise
    finally:
        _speaker_on()
        _poke()


def _start_rec(auto: bool, followup: bool = False) -> bool:
    global _mode, _followup_depth, _take_hold
    _poke()
    if not followup:
        _stop_play()
        _followup_depth = 0
    if _mic_muted:
        return False
    _busy_on()
    with _lock:
        if followup:
            if _mode in ("listen", "hold"):
                return True
        elif _mode == "listen" and not auto:
            _take_hold = True
            _mode = "hold"
            return True
        elif _mode in ("listen", "hold", "busy"):
            return True
        _take_hold = False
        _mode = "listen" if auto else "hold"
        if followup:
            _followup_depth += 1
    _stop.clear()
    threading.Thread(target=_run_turn, kwargs={"auto": auto, "followup": followup}, daemon=True).start()
    return True


def _stop_and_turn() -> dict:
    with _lock:
        mode = _mode
        data = dict(_result)
    if mode not in ("hold", "listen"):
        return data or dict(_state)
    _stop.set()
    for _ in range(200):
        with _lock:
            mode = _mode
            data = dict(_result)
        if mode in ("idle", "busy", "speak"):
            return data or dict(_state)
        time.sleep(0.1)
    return dict(_state)


def _http_path(raw: str) -> str:
    return urllib.parse.urlparse(raw).path


class Handler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = _http_path(self.path)
        if path == "/state":
            self._json(200, dict(_state))
            return
        if path == "/bot.jpg":
            if not FACE_STREAM:
                self.send_error(404)
                return
            self._proxy_jpg()
            return
        if path == "/bot.mjpeg":
            self._proxy_mjpeg()
            return
        if path == "/bot/face":
            self._proxy_face()
            return
        self._serve_static(path)

    def do_POST(self):
        path = _http_path(self.path)
        if path == "/rec/start":
            ok = _start_rec(False)
            self._json(200 if ok else 500, {"ok": ok})
            return
        if path == "/rec/stop":
            self._json(200, _stop_and_turn())
            return
        if path == "/wake":
            ok = _start_rec(True)
            self._json(200 if ok else 409, {"ok": ok})
            return
        if path == "/unblank":
            _poke()
            self._json(200, {"ok": True})
            return
        if path == "/quit":
            _exit_to_terminal()
            self._json(200, {"ok": True})
            return
        self.send_error(404)

    def _serve_static(self, path: str) -> None:
        allowed = {
            "/thinksmart-kiosk.html": ("text/html; charset=utf-8", "thinksmart-kiosk.html"),
            "/thinksmart-kiosk.js": ("text/javascript; charset=utf-8", "thinksmart-kiosk.js"),
        }
        rel = None
        ctype = "application/octet-stream"
        if path in allowed:
            ctype, rel = allowed[path]
        elif path.startswith("/faces/") and ".." not in path:
            name = path[len("/faces/") :]
            if name.endswith(".jpg") and "/" not in name.replace("blink/", "", 1):
                rel = os.path.join("faces", name)
                ctype = "image/jpeg"
        if not rel:
            self.send_error(404)
            return
        full = os.path.realpath(os.path.join(HERE, rel))
        root = os.path.realpath(HERE)
        if full != root and not full.startswith(root + os.sep):
            self.send_error(404)
            return
        try:
            data = open(full, "rb").read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_face(self):
        q = ""
        if "?" in self.path:
            q = self.path.split("?", 1)[1]
        url = FACE_STREAM + "/face" + (("?" + q) if q else "")
        try:
            urllib.request.urlopen(url, timeout=1).read()
        except OSError:
            self._json(502, {"ok": False})
            return
        self._json(200, {"ok": True})

    def _proxy_jpg(self):
        conn = None
        try:
            parsed = urllib.parse.urlparse(FACE_STREAM)
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=2)
            conn.request("GET", "/face.jpg")
            upstream = conn.getresponse()
            data = upstream.read()
            if upstream.status != 200 or not data:
                self.send_error(502, "face snapshot down")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            super(http.server.SimpleHTTPRequestHandler, self).end_headers()
            self.wfile.write(data)
        except OSError:
            try:
                self.send_error(502, "face snapshot down")
            except OSError:
                pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _proxy_mjpeg(self):
        conn = None
        try:
            host, port = urllib.parse.urlparse(FACE_STREAM).hostname, urllib.parse.urlparse(FACE_STREAM).port or 80
            conn = http.client.HTTPConnection(host, port, timeout=10)
            conn.request("GET", "/face.mjpeg")
            upstream = conn.getresponse()
            if upstream.status != 200:
                self.send_error(502, "face stream down")
                return
            self.send_response(200)
            ctype = upstream.getheader("Content-Type") or "multipart/x-mixed-replace; boundary=bloubframe"
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            super(http.server.SimpleHTTPRequestHandler, self).end_headers()
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _exit_to_terminal() -> None:
    unit = f"thinksmart-kiosk-exit-{int(time.time())}"
    try:
        subprocess.Popen(
            [
                "sudo",
                "-n",
                "systemd-run",
                "--collect",
                "--quiet",
                f"--unit={unit}",
                "/usr/local/bin/exit-to-terminal.sh",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        _log(f"exit to terminal failed {e}")
        return
    _log("exit to terminal")


def _serve():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.serve_forever()


class Kiosk(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.thinksmart.kiosk")

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Voice")
        win.set_decorated(False)
        win.set_default_size(800, 1280)
        win.fullscreen()
        data_dir = os.path.expanduser("~/.local/share/thinksmart-kiosk")
        cache_dir = os.path.expanduser("~/.cache/thinksmart-kiosk")
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(cache_dir, exist_ok=True)
        try:
            mgr = WebKit.WebsiteDataManager(
                base_data_directory=data_dir,
                base_cache_directory=cache_dir,
            )
            ctx = WebKit.WebContext.new_with_website_data_manager(mgr)
            ctx.set_sandbox_enabled(False)
            webview = WebKit.WebView.new_with_context(ctx)
        except Exception:
            try:
                ctx = WebKit.WebContext.get_default()
                ctx.set_sandbox_enabled(False)
            except Exception:
                pass
            webview = WebKit.WebView()
        settings = webview.get_settings()
        for name, value in (
            ("media-playback-requires-user-gesture", True),
            ("enable-javascript", True),
        ):
            try:
                settings.set_property(name, value)
            except Exception:
                pass
        try:
            settings.set_hardware_acceleration_policy(
                WebKit.HardwareAccelerationPolicy.NEVER
            )
        except Exception as e:
            _log(f"webkit accel {e}")
            try:
                settings.set_property("hardware-acceleration-policy", 2)
            except Exception:
                pass
        webview.set_hexpand(True)
        webview.set_vexpand(True)

        def on_load_fail(_w, _ev, _f, err):
            _log(f"webkit load-failed {err}")
            return False

        def on_load_changed(_w, ev):
            if int(ev) == 3:
                _log(f"webkit load {_w.get_uri()}")

        webview.connect("load-failed", on_load_fail)
        webview.connect("load-changed", on_load_changed)
        webview.load_uri(URL)
        win.set_child(webview)
        win.present()


if __name__ == "__main__":
    os.chdir(HERE)
    _busy_off()
    _unmute()
    _speaker_on()
    _apply_mute(_read_mic_muted())
    threading.Thread(target=_mute_watch, daemon=True).start()
    threading.Thread(target=_blank_watch, daemon=True).start()
    threading.Thread(target=_serve, daemon=True).start()
    try:
        import thinksmart_voice

        thinksmart_voice.reset()
    except Exception:
        pass
    Kiosk().run(None)
