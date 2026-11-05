#!/usr/bin/env python3
"""Sidecar wake: energy gate + Grok STT, optional sherpa. Isolated from GTK."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from thinksmart_audio import chunk_bytes, ingest, pcm_wav, pw_record_argv, rms, write_private
from thinksmart_config import BUSY, RATE_OUT, TMPDIR, ensure_tmpdir, load_kiosk_env, pipewire_env, rotating_log

load_kiosk_env(HERE)
ensure_tmpdir()

WAKE_URL = "http://127.0.0.1:1680/wake"
XAI_API_KEY = (os.environ.get("XAI_API_KEY") or "").strip()
STT_URL = os.environ.get("STT_URL", "https://api.x.ai/v1/stt")
STT_MODEL = os.environ.get("GROK_STT_MODEL", "grok-voice-transcribe-2.0")
LOG = "/tmp/kiosk-wake.log"
PREROLL = RATE_OUT * 2  # 1.0s
MAX_CLIP = RATE_OUT * 2 * 13 // 10  # 1.3s; wake is short, TV lines are not
MIN_CLIP = RATE_OUT * 2 * 5 // 10  # 0.5s
STT_COOLDOWN = 2.4
TV_COOLDOWN = 12.0
TV_PAUSE_S = 180.0
THRESH_MIN = 2000
FLOOR_MULT = 3.4
SPEECH_FRAMES = 4
NEAR_RMS = 3500
TV_FLOOR = 1600
BUSY_STALE_S = 180.0
WAKE_RE = re.compile(r"\b(hey|ok|okay)\s+(grok|grock)\b", re.I)


def _env():
    return pipewire_env()


def _log(msg: str) -> None:
    rotating_log(LOG, msg)


def _busy() -> bool:
    try:
        st = os.stat(BUSY)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if time.time() - st.st_mtime > BUSY_STALE_S:
        try:
            os.remove(BUSY)
            _log("cleared stale kiosk-busy")
        except OSError:
            pass
        return False
    return True


def _rms(pcm: bytes) -> int:
    return rms(pcm)


def _pcm_wav(pcm: bytes) -> bytes:
    return pcm_wav(pcm)


def _notify(hit: str) -> None:
    try:
        req = urllib.request.Request(
            WAKE_URL, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=3).read()
        _log(f"notified {hit}")
    except Exception as e:
        _log(f"notify failed {e}")


def _transcribe(pcm: bytes) -> str:
    path = os.path.join(TMPDIR, "wake-stt.wav")
    try:
        write_private(path, _pcm_wav(pcm))
    except OSError as e:
        _log(f"stt write {e}")
        return ""
    try:
        if not XAI_API_KEY:
            _log("stt skipped: no XAI_API_KEY")
            return ""
        wav = open(path, "rb").read()
        boundary = "----kiosk" + uuid.uuid4().hex
        parts = []
        def add(name, value, filename=None, ctype=None):
            parts.append(f"--{boundary}\r\n".encode())
            disp = f'Content-Disposition: form-data; name="{name}"'
            if filename:
                disp += f'; filename="{filename}"'
            parts.append((disp + "\r\n").encode())
            if ctype:
                parts.append(f"Content-Type: {ctype}\r\n".encode())
            parts.append(b"\r\n")
            parts.append(value if isinstance(value, bytes) else value.encode())
            parts.append(b"\r\n")
        add("file", wav, "wake.wav", "audio/wav")
        add("model", STT_MODEL)
        add("language", "en")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            STT_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        out = urllib.request.urlopen(req, timeout=22).read()
    except Exception as e:
        _log(f"stt failed {e}")
        return ""
    try:

        payload = json.loads(out.decode())
        text = payload.get("text") or payload.get("transcript") or ""
    except Exception:
        text = out.decode(errors="replace")
    return " ".join(text.split()).strip()


def _is_wake(text: str) -> bool:
    t = re.sub(r"[^a-z ]", " ", (text or "").lower())
    t = " ".join(t.split())
    if not t:
        return False
    words = t.split()
    if len(words) > 4:
        return False
    m = WAKE_RE.search(t)
    if not m:
        return False
    prefix = t[: m.start()].strip().split()
    suffix = t[m.end() :].strip().split()
    return len(prefix) <= 1 and len(suffix) <= 1


class Gate:
    def __init__(self) -> None:
        self.floor = 400.0
        self.speech_n = 0
        self.clip = bytearray()
        self.preroll = bytearray()
        self.active = False
        self.stt_lock = threading.Lock()
        self.last_stt = 0.0
        self.last_log = 0.0
        self.quiet_until = 0.0
        self.misses: list[float] = []
        self.next_stt_wait = STT_COOLDOWN

    def push(self, pcm: bytes) -> None:
        if _busy() or not pcm:
            self.active = False
            self.clip = bytearray()
            self.speech_n = 0
            return
        rms = _rms(pcm)
        now = time.time()
        if now - self.last_log >= 30:
            self.last_log = now
            paused = max(0, int(self.quiet_until - now))
            _log(
                f"rms={rms} floor={int(self.floor)} speech={self.speech_n} "
                f"active={self.active} tv_pause={paused}s"
            )  # no transcript here; STT text is logged only on notify/skip
        thresh = max(THRESH_MIN, int(self.floor * FLOOR_MULT))
        if rms < thresh:
            self.floor = self.floor * 0.97 + rms * 0.03
            self.speech_n = 0
            if self.active:
                self.clip.extend(pcm)
                if len(self.clip) >= MIN_CLIP:
                    self._submit()
                self.active = False
            else:
                self.preroll += pcm
                if len(self.preroll) > PREROLL:
                    self.preroll = self.preroll[-PREROLL:]
            return
        self.speech_n += 1
        if not self.active:
            if self.speech_n < SPEECH_FRAMES:
                self.preroll += pcm
                if len(self.preroll) > PREROLL:
                    self.preroll = self.preroll[-PREROLL:]
                return
            self.active = True
            self.clip = bytearray(self.preroll)
            self.preroll = bytearray()
            _log(f"speech start rms={rms} thresh={thresh}")
        self.clip.extend(pcm)
        if len(self.clip) >= MAX_CLIP:
            self._submit()
            self.active = False

    def _submit(self) -> None:
        pcm = bytes(self.clip)
        self.clip = bytearray()
        self.speech_n = 0
        if len(pcm) < MIN_CLIP:
            return
        now = time.time()
        clip_rms = _rms(pcm)
        near = clip_rms >= max(NEAR_RMS, int(self.floor * 5.0))
        tvish = self.floor >= TV_FLOOR
        if tvish and not near:
            _log(f"skip tv floor clip rms={clip_rms} floor={int(self.floor)}")
            return
        if now < self.quiet_until and not near:
            return
        if now - self.last_stt < self.next_stt_wait:
            return
        if clip_rms < max(THRESH_MIN, int(self.floor * 2.8)):
            _log(f"skip quiet clip rms={clip_rms} floor={int(self.floor)}")
            return
        if not self.stt_lock.acquire(blocking=False):
            return
        self.last_stt = now
        threading.Thread(target=self._stt_thread, args=(pcm,), daemon=True).start()

    def _stt_thread(self, pcm: bytes) -> None:
        try:
            if _busy():
                return
            text = _transcribe(pcm)
            _log(f"stt bytes={len(pcm)} text={text[:160]!r}")
            now = time.time()
            if _is_wake(text):
                self.misses = []
                self.next_stt_wait = STT_COOLDOWN
                self.quiet_until = 0.0
                _notify(text)
                return
            if text:
                self.misses = [t for t in self.misses if now - t < 12.0]
                self.misses.append(now)
                self.next_stt_wait = TV_COOLDOWN
                need = 1 if self.floor >= TV_FLOOR else 2
                if len(self.misses) >= need:
                    self.quiet_until = now + TV_PAUSE_S
                    _log(f"tv-like speech, wake paused {int(TV_PAUSE_S)}s")
            else:
                self.next_stt_wait = STT_COOLDOWN
        finally:
            self.stt_lock.release()


def main() -> None:
    os.chdir(HERE)
    spotter = None
    stream = None
    try:
        import thinksmart_wakeword

        spotter = thinksmart_wakeword.load_spotter()
        stream = spotter.create_stream()
        _log("sidecar kws ready")
    except Exception as e:
        _log(f"kws skipped {e}")
    gate = Gate()
    rv_state = None
    last_hit = 0.0
    while True:
        proc = subprocess.Popen(
            pw_record_argv("80ms"),
            env=_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log(f"sidecar pw-record pid={proc.pid}")
        try:
            while proc.poll() is None:
                if _busy():
                    _log("mic yield to face")
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    while _busy():
                        time.sleep(0.15)
                    break
                raw = proc.stdout.read(chunk_bytes() * 2)
                if not raw:
                    break
                pcm, rv_state = ingest(raw, rv_state)
                if not pcm:
                    continue
                if spotter is not None:
                    hit = thinksmart_wakeword.feed(spotter, stream, pcm)
                    now = time.time()
                    if hit and now - last_hit >= 2.0:
                        last_hit = now
                        _log(f"kws {hit}")
                        _notify(hit)
                        stream = spotter.create_stream()
                gate.push(pcm)
        except Exception as e:
            _log(f"sidecar error {e}")
            if spotter is not None:
                stream = spotter.create_stream()
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("200")
    except OSError:
        pass
    main()
