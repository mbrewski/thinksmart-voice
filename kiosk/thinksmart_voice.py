#!/usr/bin/env python3
"""Grok Voice clip-then-reply on the panel. No companion."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import threading
import time
import wave
from typing import Any, Literal

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_FILE = "/tmp/kiosk-voice.log"  # Renamed for clarity: LOG -> LOG_FILE
SAMPLE_IN = 16000
SAMPLE_OUT = 24000
MIN_PCM_BYTES = SAMPLE_IN * 2 // 4
APPEND_CHUNK = 8 * 1024
TURN_TIMEOUT = 120.0
CONNECT_TIMEOUT = 20.0
MODEL = os.environ.get("GROK_VOICE_MODEL", "grok-voice-latest")
VOICE = os.environ.get("GROK_VOICE_NAME", "eve")

# Module-level logger for consistent logging across the application
logger: logging.Logger = logging.getLogger("thinksmart-voice")


def _log(msg: str) -> None:
    """Log a timestamped message to LOG_FILE. Thread-safe."""
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n" if not line.endswith("\n") else line)
    except OSError as exc:
        logger.warning(f"Failed to write log entry {line!r}: {exc}")


def _api_key() -> str:
    """Get XAI_API_KEY from environment. Raises RuntimeError if missing."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key or len(key) < 16:
        raise RuntimeError(f"Missing or invalid env var {os.path.basename(__file__)}")
    return key


def _instructions() -> str:
    """Load instructions from local files, falling back to default."""
    for path in (
        os.path.join(HERE, "instructions.md"),
        os.path.join(ROOT, "instructions.md"),
    ):
        try:
            text = open(path, encoding="utf-8").read().strip()
            if text:
                return text
        except OSError as exc:
            logger.debug(f"Failed to load instructions from {path}: {exc}")
            continue
    return "You are Grok, a helpful household voice assistant. Speak English in short sentences."


def _tools() -> list[dict[str, Any]]:
    """Build MCP tools configuration based on environment variables."""
    tools: list[dict[str, Any]] = [
        {
            "type": "web_search",
            "location": {
                "country": os.environ.get("SEARCH_COUNTRY", "US"),
                "city": os.environ.get("SEARCH_CITY", "Denver"),
                "timezone": os.environ.get("SEARCH_TZ", "America/Denver"),
            },
        }
    ]
    mcp_url = (os.environ.get("MCP_URL") or "").strip()
    if mcp_url:
        try:
            mcp: dict[str, Any] = {
                "type": "mcp",
                "server_url": mcp_url,
                "server_label": os.environ.get("MCP_LABEL", "home-assistant").strip() or "home-assistant",
                "server_description": (
                    "This home's Home Assistant. Call homeassistant__GetLiveContext "
                    "for which lights are on. Call intent__HassTurnOn, intent__HassTurnOff, "
                    "or light__HassLightSet to control lights."
                ),
            }
            cid = (os.environ.get("CF_ACCESS_CLIENT_ID") or "").strip()
            csec = (os.environ.get("CF_ACCESS_CLIENT_SECRET") or "").strip()
            if cid and csec:
                mcp["headers"] = {
                    "CF-Access-Client-Id": cid,
                    "CF-Access-Client-Secret": csec,
                }
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Failed to configure MCP tools: {exc}")
        else:
            tools.append(mcp)
    return tools


def _session_payload() -> dict[str, Any]:
    """Build the session.update payload for WebSocket connection."""
    try:
        validate_env()  # Ensure required env vars are set before building payload
    except RuntimeError as exc:
        logger.error(f"Environment validation failed during session setup: {exc}")
        raise

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
                    "transcription": {
                        "language_hint": "en",
                        "keyterms": [
                            "Grand Drive",
                            "Centennial",
                            "Aurora",
                            "E-470",
                            "I-225",
                            "Colfax",
                            "home",
                            "work",
                        ],
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_OUT}},
            },
            "tools": _tools(),
        },
    }


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Convert raw PCM audio to WAV format."""
    if not pcm or sample_rate <= 0:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        try:
            wf.writeframes(pcm)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Failed to encode PCM to WAV: {exc}")
    return buf.getvalue()


def _ws_open(ws: Any | None) -> bool:
    """Check if a WebSocket connection is open."""
    if ws is None or not hasattr(ws, "open"):
        return False
    state = getattr(getattr(ws, "_sock", ws), "state", None)
    name = getattr(state, "name", str(state)) if isinstance(state, dict) else (str(state) if state else "")
    return bool(name and name.endswith("OPEN"))


def _event_type(event: dict[str, Any]) -> str | Literal[""]:
    """Extract event type from a WebSocket message."""
    etype = event.get("type") or ""
    # Handle cases where 'type' might be nested differently
    if isinstance(etype, (dict, list)):  # pylint: disable=unsubscriptable-object
        return str(etype)
    return etype


def _error_message(event: dict[str, Any]) -> str:
    """Extract error message from a WebSocket error event."""
    err = event.get("error") if isinstance(event.get("error"), dict) else {} or event
    if not isinstance(err, (dict, list)):  # pylint: disable=unsubscriptable-object
        return "voice error"
    msg = str(err.get("message")) or str(err.get("code", "")) or "voice error"
    return msg


_FILLER = re.compile(r"^(and|uh|um|ah|oh|mm+|hmm+|er|the|a)?[\s.!?]*$", re.I)


def _usable_transcript(text: str | None) -> bool:
    """Check if transcript is non-empty and not just filler words."""
    t = " ".join((text or "").split()).strip()
    if len(t) < 3:
        return False
    if _FILLER.match(t):
        return False
    return True


class GrokVoiceSession:
    """Manages a single voice session with the Grok Voice API."""

    def __init__(self, conversation_id: str | None = None) -> None:
        self._ws: Any | None = None  # WebSocket connection (thread-local via _Runner)
        self._lock = asyncio.Lock()
        self._ready = False
        self.conversation_id = conversation_id or ""

    async def reset(self) -> None:
        """Close the current session and release resources."""
        async with self._lock:
            await self._close()

    async def complete_turn(self, pcm: bytes) -> dict[str, Any]:
        """Process a turn of audio input. Returns transcript/reply or error info."""
        if len(pcm) < MIN_PCM_BYTES:
            return {"transcript": "", "reply": "", "audio_b64": "", "sample_rate": SAMPLE_OUT}

        try:
            async with self._lock:
                result = await asyncio.wait_for(self._turn_locked(pcm), timeout=TURN_TIMEOUT)
                logger.debug(f"Turn completed successfully")
                return result
        except TimeoutError as exc:
            error_msg = f"Voice turn timed out after {TURN_TIMEOUT}s"
            _log(error_msg)
            logger.error(error_msg)
            return {"transcript": "", "reply": "timeout", "error": str(exc), "audio_b64": "", "sample_rate": SAMPLE_OUT}
        except RuntimeError as exc:
            error_msg = f"Voice session error: {exc}"
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")
            logger.error(error_msg)
            return {"transcript": "", "reply": str(exc), "audio_b64": "", "sample_rate": SAMPLE_OUT}

    async def _turn_locked(self, pcm: bytes) -> dict[str, Any]:
        """Execute a voice turn while holding the lock."""
        await self._ensure()
        assert self._ws is not None  # Type guard for internal use only

        try:
            for off in range(0, len(pcm), APPEND_CHUNK):
                chunk = pcm[off : off + APPEND_CHUNK]
                await self._send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                })
            await self._send({"type": "input_audio_buffer.commit"})
        except Exception as exc:  # pylint: disable=broad-except
            error_msg = f"Failed to send audio chunks: {exc}"
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")
            logger.error(error_msg)

        return await self._collect()

    async def _ensure(self) -> None:
        """Ensure WebSocket connection is open and session is ready."""
        if self._ws is not None and self._ready and _ws_open(self._ws):
            return

        # Close any existing broken connections first
        try:
            await self._close()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Failed to close previous connection: {exc}")

        url = f"wss://api.x.ai/v1/realtime?model={MODEL}"
        if self.conversation_id:
            url += f"&conversation_id={self.conversation_id}"

        _log(f"{time.strftime('%H:%M:%S ')}connect {url.split('?')[0]}")

        try:
            headers = {"Authorization": f"Bearer {_api_key()}"}
        except RuntimeError as exc:  # pylint: disable=broad-except
            logger.error(f"Failed to get API key for WebSocket connection: {exc}")
            raise

        kw: dict[str, Any] = {
            "max_size": 16 * 1024 * 1024,
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 10,
            "open_timeout": CONNECT_TIMEOUT,
        }

        # Handle websockets.connect signature variations across versions
        import inspect
        params = inspect.signature(websockets.connect).parameters
        if "additional_headers" in params:
            kw["additional_headers"] = headers
        else:
            kw["extra_headers"] = headers

        try:
            self._ws = await websockets.connect(url, **kw)
        except Exception as exc:  # pylint: disable=broad-except
            error_msg = f"Failed to connect to voice API: {exc}"
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")
            logger.error(error_msg)
            raise

        self._ready = False

        try:
            await self._send(_session_payload())
        except Exception as exc:  # pylint: disable=broad-except
            error_msg = f"Failed to send session.update payload: {exc}"
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")
            logger.error(error_msg)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12.0

        try:
            while loop.time() < deadline:
                event = await self._recv_json(timeout=max(0.1, deadline - loop.time()))
                etype = _event_type(event)
                if etype == "error":
                    raise RuntimeError(_error_message(event))
                if etype == "conversation.created":
                    conv = (event.get("conversation") or {}) if isinstance(event.get("conversation"), dict) else {}
                    self.conversation_id = str(conv.get("id", "")) or self.conversation_id
                elif etype == "session.updated":
                    sess = event.get("session") if isinstance(event.get("session"), dict) else {}
                    tools = (sess.get("tools") or []) if isinstance(sess.get("tools"), list) else []
                    kinds = [str(t.get("type", t.get("server_label", "?"))) for t in tools]
                    _log(f"{time.strftime('%H:%M:%S ')}session.updated tools={kinds}")
                    self._ready = True
                    return
                elif etype:  # Log other events without raising
                    _log(f"{time.strftime('%H:%M:%S ')}session evt {etype}")
        except TimeoutError as exc:
            error_msg = "voice session.update timed out"
            logger.error(error_msg)

        raise RuntimeError("voice session.update timed out")

    async def _collect(self) -> dict[str, Any]:
        """Collect response audio and transcript from the WebSocket."""
        audio_parts: list[bytes] = []
        reply_parts: list[str] = []
        transcript = ""
        created = False
        asked = False
        skip_empty = False
        saw_tool = False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TURN_TIMEOUT

        async def _kick() -> None:
            """Kick off response generation if we have a usable transcript."""
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
            except TimeoutError as exc:  # pylint: disable=broad-except
                logger.debug(f"Timeout waiting for response events")
                if not created and not asked and _usable_transcript(transcript):
                    await _kick()
                    continue
                break

            etype = _event_type(event)

            try:
                if etype == "error":
                    raise RuntimeError(_error_message(event))
                elif etype == "conversation.item.input_audio_transcription.completed":
                    transcript = (" ".join(str(event.get("transcript") or "").split())).strip()
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
                elif "function_call" in etype or "mcp" in etype or etype.startswith("response.web_search"):
                    saw_tool = True
                    _log(f"{time.strftime('%H:%M:%S ')}tool evt {etype}")
                elif etype == "response.done":
                    st = (event.get("response") or {}) if isinstance(event.get("response"), dict) else {}
                    pcm_len = len(audio_parts)
                    saw_tool_str = str(saw_tool).lower()  # Safe conversion for logging
                    _log(f"{time.strftime('%H:%M:%S ')}response.done status={st.get('status')} tools={saw_tool_str} pcm={pcm_len}")

                    if saw_tool and not audio_parts and created:
                        logger.debug("Tool response with no audio; re-kicking")
                        saw_tool = False
                        created = False
                        asked = True
                        await _kick()
                        continue

            except Exception as exc:  # pylint: disable=broad-except
                error_msg = f"Error processing WebSocket event {etype}: {exc}"
                logger.error(error_msg)
                break

        reply = " ".join("".join(reply_parts).split())
        pcm = b"".join(audio_parts)

        if skip_empty and not audio_parts:
            return {"transcript": transcript, "reply": "", "audio_b64": "", "sample_rate": SAMPLE_OUT}

        wav_data = pcm_to_wav(pcm, SAMPLE_OUT) if pcm else b""
        _log(f"{time.strftime('%H:%M:%S ')}turn tx={str(transcript)[:80]!r} reply={str(reply)[:80]!r} pcm={len(pcm)}")

        return {
            "transcript": transcript,
            "reply": reply,
            "audio_b64": base64.b64encode(wav_data).decode("ascii") if wav_data else "",
            "sample_rate": SAMPLE_OUT,
        }

    async def _send(self, payload: dict[str, Any]) -> None:
        """Send a JSON message over the WebSocket."""
        assert self._ws is not None  # Type guard for internal use only
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:  # pylint: disable=broad-except
            error_msg = f"Failed to send payload {payload}: {exc}"
            logger.error(error_msg)

    async def _recv_json(self, timeout: float) -> dict[str, Any]:
        """Receive and parse a JSON message from the WebSocket."""
        assert self._ws is not None  # Type guard for internal use only

        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            except TimeoutError as exc:  # pylint: disable=broad-except
                raise
            except Exception as exc:  # pylint: disable=broad-except
                error_msg = f"WebSocket recv failed: {exc}"
                logger.error(error_msg)
                break

            if isinstance(raw, bytes):
                continue

            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:  # pylint: disable=broad-except
                error_msg = f"Failed to parse JSON from WebSocket: {exc}"
                logger.debug(error_msg)
                continue

            if not isinstance(event, dict):
                continue

            etype = _event_type(event)
            if etype in {"ping", "pong"}:
                continue

            return event  # type: ignore[return-value]

    async def _close(self) -> None:
        """Close the WebSocket connection and reset state."""
        ws = self._ws
        self._ws = None
        self._ready = False

        if ws is None:
            logger.debug("No active WebSocket to close")
            return

        try:
            await ws.close()
        except Exception as exc:  # pylint: disable=broad-except
            error_msg = f"Failed to close WebSocket gracefully: {exc}"
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")


class _Runner:
    """Thread-safe runner that manages a single asyncio event loop and session."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        # Create fresh GrokVoiceSession for each run to avoid global state issues
        self.session = GrokVoiceSession(conversation_id=None)  # Fresh instance per runner
        threading.Thread(target=self.loop.run_forever, daemon=True, name="grok-voice").start()

    def turn(self, pcm: bytes) -> dict[str, Any]:
        """Process a single voice turn. Returns result or error info."""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.session.complete_turn(pcm),  # type: ignore[arg-type]
                self.loop,
            )
            return fut.result(timeout=TURN_TIMEOUT + 5)
        except TimeoutError as exc:  # pylint: disable=broad-except
            error_msg = f"Voice turn timed out after {TURN_TIMEOUT}s in thread pool"
            logger.error(error_msg)
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")
            return {"transcript": "", "reply": "timeout", "audio_b64": "", "sample_rate": SAMPLE_OUT}
        except RuntimeError as exc:  # pylint: disable=broad-except
            error_msg = f"Voice session runtime error in thread pool: {exc}"
            logger.error(error_msg)
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")
            return {"transcript": "", "reply": str(exc), "audio_b64": "", "sample_rate": SAMPLE_OUT}

    def reset(self) -> None:
        """Reset the session and close any active connections."""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.session.reset(),  # type: ignore[arg-type]
                self.loop,
            )
            fut.result(timeout=15)
        except TimeoutError as exc:  # pylint: disable=broad-except
            error_msg = f"Session reset timed out after 15s in thread pool"
            logger.error(error_msg)
            _log(f"{time.strftime('%H:%M:%S ')}{error_msg}")


# Thread-safe singleton runner instance with proper locking
_runner: _Runner | None = None
_runner_lock = threading.Lock()


def _get_runner() -> _Runner:
    """Get or create the global voice session runner. Thread-safe."""
    with _runner_lock:
        if _runner is None:
            try:
                validate_env()  # Validate env vars before creating runner
            except RuntimeError as exc:
                logger.error(f"Environment validation failed for voice module: {exc}")
                raise
            _runner = _Runner()
        return _runner


def complete_turn(pcm: bytes) -> dict[str, Any]:
    """Process a voice turn. Returns transcript/reply or error info."""
    try:
        runner = _get_runner()  # type: ignore[assignment]
        result = runner.turn(pcm)
        return result
    except RuntimeError as exc:  # pylint: disable=broad-except
        if "XAI_API_KEY" in str(exc):
            error_msg = f"{time.strftime('%H:%M:%S ')}Missing XAI_API_KEY environment variable"
            _log(error_msg)
            logger.error(error_msg)
    except Exception as exc:  # pylint: disable=broad-except
        error_msg = f"Voice turn failed with unexpected error: {exc}"
        logger.exception(error_msg)

    return {"transcript": "", "reply": "server error", "audio_b64": "", "sample_rate": SAMPLE_OUT}


def reset() -> None:
    """Reset the voice session. Logs any errors."""
    try:
        runner = _get_runner()  # type: ignore[assignment]
        runner.reset()
    except RuntimeError as exc:  # pylint: disable=broad-except
        if "XAI_API_KEY" in str(exc):
            error_msg = f"{time.strftime('%H:%M:%S ')}Missing XAI_API_KEY environment variable during reset"
            _log(error_msg)
            logger.error(error_msg)
    except Exception as exc:  # pylint: disable=broad-except
        error_msg = f"Session reset failed with unexpected error: {exc}"
        logger.exception(error_msg)
