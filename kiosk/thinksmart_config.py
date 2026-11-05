"""Shared env, PipeWire env, mic constants, rotating log, private tmp dir."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

MIC = "alsa_input.platform-c051000.sound-card.HiFi__Microphone__source"
RATE_IN = 48000
RATE_OUT = 16000
BYTES_100MS = RATE_IN * 2 * 4 // 10
TMPDIR = "/tmp/thinksmart-kiosk"
BUSY = "/tmp/kiosk-busy"

# Required environment variables that must be set at startup
REQUIRED_ENV: list[str] = ["XAI_API_KEY", "THINKSMART_MIC"]


def load_env_file(path: str) -> None:
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
    except OSError as exc:
        logging.getLogger("thinksmart-config").warning(f"Failed to load env file {path}: {exc}")


def validate_env() -> None:
    """Validate required environment variables are set. Raises RuntimeError if missing."""
    for key in REQUIRED_ENV:
        val = os.environ.get(key, "")
        if not val or (key == "XAI_API_KEY" and len(val) < 16):
            raise RuntimeError(f"Missing or invalid env var {key}: must be set to a non-empty value")


def load_ha_login(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and "=" not in line.split(":", 1)[0]:
            key, _, val = line.partition(":")
        elif "=" in line:
            key, _, val = line.partition("=")
        else:
            continue
        key = key.strip().upper()
        val = val.strip().strip('"').strip("'")
        if key in ("USER", "USERNAME") and "HA_USERNAME" not in os.environ:
            os.environ["HA_USERNAME"] = val
        elif key == "PASSWORD" and "HA_PASSWORD" not in os.environ:
            os.environ["HA_PASSWORD"] = val


def load_kiosk_env(here: str) -> None:
    load_env_file("/etc/thinksmart-kiosk.env")
    load_env_file(os.path.join(here, "kiosk.env"))
    load_ha_login(os.path.expanduser("~/Documents/ha.env"))


def pipewire_env() -> dict[str, str]:
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    env["PULSE_SERVER"] = "unix:/run/user/1000/pulse/native"
    return env


def ensure_tmpdir() -> str:
    os.makedirs(TMPDIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(TMPDIR, 0o700)
    except OSError:
        pass
    return TMPDIR


def rotating_log(path: str, msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_500_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
