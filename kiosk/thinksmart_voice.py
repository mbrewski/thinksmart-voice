#!/usr/bin/env python3
"""Grok Voice clip-then-reply on the panel. No companion."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import threading
import time
import wave
from typing import Any

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = "/tmp/kiosk-voice.log"
SAMPLE_IN = 16000
SAMPLE_OUT = 24000
MIN_PCM_BYTES = SAMPLE_IN * 2 // 4
APPEND_CHUNK = 8 * 1024
TURN_TIMEOUT = 120.0
CONNECT_TIMEOUT = 20.0
MODEL = os.environ.get("GROK_VOICE_MODEL", "grok-voice-latest")
VOICE = os.environ.get("GROK_VOICE_NAME", "eve")

log = logging.getLogger("thinksmart-voice")


def _log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _api_key() -> str:
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("XAI_API_KEY is not set")
    return key


def _instructions() -> str:
    for path in (
        os.path.join(HERE, "instructions.md"),
        os.path.join(ROOT, "instructions.md"),
    ):
        try:
            text = open(path, encoding="utf-8").read().strip()
            if text:
                return text
        except OSError:
            continue
    return "You are Grok, a helpful household voice assistant. Speak English in short sentences."


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "web_search",
            "location": {
                "country": os.environ.get("SEARCH_COUNTRY", "US"),
                "city": os.environ.get("SEARCH_CITY", "Denver"),
                "timezone": os.environ.get("SEARCH_TZ", "America/Denver"),
            },
        }
    ]


def _session_payload() -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "voice": VOICE,
            "instructions": _instructions(),
            "turn_detection": None,
            "reasoning": {"effort": os.environ.get("GROK_REASONING", "none")},
            "resumption": {"enabled": True},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_IN},
                    "transcription": {"language_hint": "en"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_OUT}},
            },
            "tools": _tools(),
        },
    }


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _ws_open(ws: Any) -> bool:
    if ws is None:
        return False
    if hasattr(ws, "open"):
        return bool(ws.open)
    state = getattr(ws, "state", None)
    name = getattr(state, "name", str(state or ""))
    return name.endswith("OPEN")


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or "")


def _error_message(event: dict[str, Any]) -> str:
    err = event.get("error") if isinstance(event.get("error"), dict) else event
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "voice error")
    return "voice error"


_FILLER = __import__("re").compile(r"^(and|uh|um|ah|oh|mm+|hmm+|er|the|a)?[\s.!?]*$", __import__("re").I)


def _usable_transcript(text: str) -> bool:
    t = " ".join((text or "").split()).strip()
    if len(t) < 3:
        return False
    if _FILLER.match(t):
        return False
    return True


class GrokVoiceSession:
    def __init__(self) -> None:
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._ready = False
        self.conversation_id = ""

    async def reset(self) -> None:
        async with self._lock:
            await self._close()

    async def complete_turn(self, pcm: bytes) -> dict[str, Any]:
        if len(pcm) < MIN_PCM_BYTES:
            return {"transcript": "", "reply": "", "audio_b64": "", "sample_rate": SAMPLE_OUT}
        async with self._lock:
            return await asyncio.wait_for(self._turn_locked(pcm), timeout=TURN_TIMEOUT)

    async def _turn_locked(self, pcm: bytes) -> dict[str, Any]:
        await self._ensure()
        assert self._ws is not None
        for off in range(0, len(pcm), APPEND_CHUNK):
            chunk = pcm[off : off + APPEND_CHUNK]
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        await self._send({"type": "input_audio_buffer.commit"})
        return await self._collect()

    async def _ensure(self) -> None:
        if self._ws is not None and self._ready and _ws_open(self._ws):
            return
        await self._close()
        url = f"wss://api.x.ai/v1/realtime?model={MODEL}"
        if self.conversation_id:
            url += f"&conversation_id={self.conversation_id}"
        _log(f"connect {url.split('?')[0]}")
        headers = {"Authorization": f"Bearer {_api_key()}"}
        kw: dict[str, Any] = {
            "max_size": 16 * 1024 * 1024,
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 10,
            "open_timeout": CONNECT_TIMEOUT,
        }
        import inspect

        params = inspect.signature(websockets.connect).parameters
        if "additional_headers" in params:
            kw["additional_headers"] = headers
        else:
            kw["extra_headers"] = headers
        self._ws = await websockets.connect(url, **kw)
        self._ready = False
        await self._send(_session_payload())
        deadline = asyncio.get_event_loop().time() + 12.0
        while asyncio.get_event_loop().time() < deadline:
            event = await self._recv_json(timeout=max(0.1, deadline - asyncio.get_event_loop().time()))
            etype = _event_type(event)
            if etype == "error":
                raise RuntimeError(_error_message(event))
            if etype == "conversation.created":
                conv = event.get("conversation") if isinstance(event.get("conversation"), dict) else {}
                self.conversation_id = str(conv.get("id") or self.conversation_id)
            if etype == "session.updated":
                self._ready = True
                return
        raise RuntimeError("voice session.update timed out")

    async def _collect(self) -> dict[str, Any]:
        audio_parts: list[bytes] = []
        reply_parts: list[str] = []
        transcript = ""
        created = False
        asked = False
        skip_empty = False
        saw_tool = False
        loop = asyncio.get_event_loop()
        deadline = loop.time() + TURN_TIMEOUT

        async def _kick() -> None:
            nonlocal asked
            asked = True
            await self._send({"type": "response.create"})

        while True:
            now = loop.time()
            if now >= deadline:
                break
            wait = min(8.0, max(0.2, deadline - now))
            try:
                event = await self._recv_json(timeout=wait)
            except TimeoutError:
                if not created and not asked and _usable_transcript(transcript):
                    await _kick()
                    continue
                break
            etype = _event_type(event)
            if etype == "error":
                raise RuntimeError(_error_message(event))
            if etype == "conversation.item.input_audio_transcription.completed":
                transcript = " ".join(str(event.get("transcript") or "").split()).strip()
                if not _usable_transcript(transcript):
                    skip_empty = True
                    break
                if not created and not asked:
                    await _kick()
            elif etype == "response.created":
                created = True
            elif etype in {"response.output_audio.delta", "response.audio.delta"}:
                blob = event.get("delta") or event.get("audio") or ""
                if blob:
                    audio_parts.append(base64.b64decode(blob))
            elif etype in {
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
            }:
                piece = event.get("delta") or ""
                if piece:
                    reply_parts.append(str(piece))
            elif etype in {
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            }:
                done_text = event.get("transcript")
                if done_text:
                    reply_parts = [str(done_text)]
            elif "function_call" in etype or "mcp_call" in etype or etype.startswith("response.web_search"):
                saw_tool = True
            elif etype == "response.done":
                pcm = b"".join(audio_parts)
                reply = " ".join("".join(reply_parts).split())
                if saw_tool and not pcm and created:
                    saw_tool = False
                    created = False
                    asked = True
                    await _kick()
                    continue
                break
        reply = " ".join("".join(reply_parts).split())
        pcm = b"".join(audio_parts)
        if skip_empty and not pcm:
            return {"transcript": transcript, "reply": "", "audio_b64": "", "sample_rate": SAMPLE_OUT}
        wav = pcm_to_wav(pcm, SAMPLE_OUT) if pcm else b""
        _log(f"turn tx={transcript[:80]!r} reply={reply[:80]!r} pcm={len(pcm)}")
        return {
            "transcript": transcript,
            "reply": reply,
            "audio_b64": base64.b64encode(wav).decode("ascii") if wav else "",
            "sample_rate": SAMPLE_OUT,
        }

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))

    async def _recv_json(self, timeout: float) -> dict[str, Any]:
        assert self._ws is not None
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            if isinstance(raw, bytes):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = _event_type(event)
            if etype in {"ping", "pong"}:
                continue
            return event

    async def _close(self) -> None:
        ws = self._ws
        self._ws = None
        self._ready = False
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass


class _Runner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.session = GrokVoiceSession()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="grok-voice").start()

    def turn(self, pcm: bytes) -> dict[str, Any]:
        fut = asyncio.run_coroutine_threadsafe(self.session.complete_turn(pcm), self.loop)
        return fut.result(timeout=TURN_TIMEOUT + 5)

    def reset(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.session.reset(), self.loop)
        fut.result(timeout=15)


_runner: _Runner | None = None
_runner_lock = threading.Lock()


def _get() -> _Runner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = _Runner()
        return _runner


def complete_turn(pcm: bytes) -> dict[str, Any]:
    try:
        return _get().turn(pcm)
    except Exception as e:
        _log(f"turn failed {e}")
        return {"transcript": "", "reply": "server error", "error": str(e), "audio_b64": "", "sample_rate": SAMPLE_OUT}


def reset() -> None:
    try:
        _get().reset()
    except Exception as e:
        _log(f"reset failed {e}")
