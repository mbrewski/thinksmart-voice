#!/usr/bin/env python3
"""Face kiosk: hold-to-talk and Hey Grok; Grok Voice on-device."""
import array
import audioop
import base64
import fcntl
import http.server
import io
import json
import os
import signal
import http.client
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import wave

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, WebKit

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass
    except OSError:
        pass


_load_env_file("/etc/thinksmart-kiosk.env")
_load_env_file(os.path.join(HERE, "kiosk.env"))


if not (os.environ.get("XAI_API_KEY") or "").strip():
    raise SystemExit("XAI_API_KEY is unset; set it in /etc/thinksmart-kiosk.env or kiosk.env")

CLOCK_TZ = os.environ.get("CLOCK_TZ", os.environ.get("TZ", "UTC"))
PORT = 1680
URL = f"http://127.0.0.1:{PORT}/thinksmart-kiosk.html?v=39"
REC_WAV = "/tmp/kiosk-rec.wav"
BUSY = "/tmp/kiosk-busy"
MIC = "alsa_input.platform-c051000.sound-card.HiFi__Microphone__source"
HINT_IDLE = "say hey grok, or press to talk"
HINT_LISTEN = "Listening"
HINT_MUTE = "mic muted"
SW_MICROPHONE_INSERT = 4
MUTE_DEV = "/dev/input/event0"
FACE_STREAM = (os.environ.get("FACE_STREAM") or "").strip()

RATE_IN = 48000
RATE_OUT = 16000
BYTES_100MS = RATE_IN * 2 * 4 // 10
MIN_CMD = RATE_OUT * 2 * 8 // 10
MAX_CMD = RATE_OUT * 2 * 10
SILENCE_MS = 1600
NO_SPEECH_MS = 5000
FOLLOWUP_NO_SPEECH_MS = 12000
SPEECH_RMS = 1600
FOLLOWUP_SPEECH_RMS = 2800
FOLLOWUP_SPEECH_FRAMES = 4
NEAR_RMS = 3500
FOLLOWUP_NEAR_RMS = 4500
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
}


def _env():
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    env["PULSE_SERVER"] = "unix:/run/user/1000/pulse/native"
    return env


def _log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with open("/tmp/kiosk-wake.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _push_face(name: str) -> None:
    def _go() -> None:
        try:
            subprocess.run(
                ["curl", "-sS", "-m", "1", f"{FACE_STREAM}/face?name={name}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
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
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _rms(pcm: bytes) -> int:
    n = len(pcm) // 2
    if n <= 0:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    acc = 0
    for x in samples:
        acc += x * x
    return int((acc / n) ** 0.5)


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


def _fade_s16_stereo(pcm: bytes, rate: int = 48000, ms: int = 18) -> bytes:
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
    if rate != 48000:
        pcm, _st = audioop.ratecv(pcm, 2, 1, rate, 48000, None)
    pcm = audioop.tostereo(pcm, 2, 1.0, 1.0)
    pad = b"\x00\x00\x00\x00" * int(48000 * 0.06)
    pcm = _fade_s16_stereo(pcm)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
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
    global _play_proc, _followup_skip
    _followup_skip = False
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
        global _play_proc, _play_vol_restore
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
        if rc not in (0, None, -signal.SIGTERM, -signal.SIGKILL):
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
        with _lock:
            still_speak = _mode == "speak"
            if still_speak:
                _mode = "idle"
        if not still_speak:
            if interrupted:
                _busy_off()
            return
        _set_state(face="idle", hint=HINT_IDLE, busy=False)
        if interrupted:
            _busy_off()
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
    path = "/tmp/kiosk-tts.wav"
    hot = "/tmp/kiosk-tts-hot.wav"
    try:
        open(path, "wb").write(raw)
    except OSError:
        return 0.0
    dur = _wav_seconds(raw)
    try:
        cooked = _to_s16_stereo_48k(raw)
        open(hot, "wb").write(cooked)
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
        open(REC_WAV, "wb").write(wav)
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
        [
            "/usr/bin/pw-record",
            "--rate=48000",
            "--channels=2",
            "--format=s32",
            "--latency=50ms",
            "--target",
            MIC,
            "-",
        ],
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
    t0 = time.time()
    last_voice = t0
    time.sleep(0.25)
    try:
        while proc.poll() is None:
            if _stop.is_set() and not auto:
                break
            raw = proc.stdout.read(BYTES_100MS)
            if not raw:
                break
            s16 = audioop.lin2lin(raw, 4, 2)
            mono = audioop.tomono(s16, 2, 0.5, 0.5)
            pcm, rv_state = audioop.ratecv(mono, 2, 1, RATE_IN, RATE_OUT, rv_state)
            if not pcm:
                continue
            buf.extend(pcm)
            now = time.time()
            if auto:
                r = _rms(pcm)
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
    return bytes(buf)


def _maybe_followup() -> None:
    time.sleep(0.4)
    if _followup_skip or _mic_muted:
        _busy_off()
        return
    with _lock:
        if _mode not in ("idle", "speak"):
            return
        if _followup_depth >= MAX_FOLLOWUPS:
            _mode = "idle"
            _busy_off()
            return
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
        _set_state(
            face="speak" if speaking else "idle",
            status="speaking" if speaking else (heard or "ready"),
            caption=reply,
            hint=HINT_IDLE,
            busy=False,
        )
        with _lock:
            _mode = "speak" if speaking else "idle"
            _result = data
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
    global _mode, _followup_depth
    _poke()
    if not followup:
        _stop_play()
        _followup_depth = 0
    if _mic_muted:
        return False
    _busy_on()
    with _lock:
        if _mode in ("listen", "hold", "busy"):
            return True
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
    if mode != "hold":
        return data or dict(_state)
    _stop.set()
    for _ in range(1000):
        with _lock:
            mode = _mode
            data = dict(_result)
        if mode == "idle":
            return data or dict(_state)
        time.sleep(0.1)
    return dict(_state)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.startswith("/state"):
            self._json(200, dict(_state))
            return
        if self.path.startswith("/bot.jpg"):
            if not FACE_STREAM:
                self.send_error(404)
                return
            self._proxy_jpg()
            return
        if self.path.startswith("/bot.mjpeg"):
            self._proxy_mjpeg()
            return
        if self.path.startswith("/bot/face"):
            self._proxy_face()
            return
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/rec/start"):
            ok = _start_rec(False)
            self._json(200 if ok else 500, {"ok": ok})
            return
        if self.path.startswith("/rec/stop"):
            self._json(200, _stop_and_turn())
            return
        if self.path.startswith("/wake"):
            ok = _start_rec(True)
            self._json(200 if ok else 409, {"ok": ok})
            return
        if self.path.startswith("/unblank"):
            _poke()
            self._json(200, {"ok": True})
            return
        self.send_error(404)

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
        win.fullscreen()
        try:
            ctx = WebKit.WebContext.get_default()
            ctx.set_sandbox_enabled(False)
        except Exception:
            pass
        webview = WebKit.WebView()
        settings = webview.get_settings()
        for name, value in (
            ("media-playback-requires-user-gesture", False),
            ("enable-javascript", True),
        ):
            try:
                settings.set_property(name, value)
            except Exception:
                pass
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
