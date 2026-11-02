#!/usr/bin/env python3
"""ThinkSmart volume keys → PipeWire sink (cage has no media-key handler).

EVIOCGRAB on event0/event1 steals KEY_VOLUME* from cage. The face still opens
event0 without grab for EVIOCGSW (mute slider). Grab does not block that ioctl
on a second fd.
"""
import fcntl
import os
import re
import select
import struct
import subprocess
import time

EV_KEY = 1
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
EVENT = struct.Struct("llHHi")
EVIOCGRAB = 0x40044590
DEVICES = ("/dev/input/event0", "/dev/input/event1")
LOG = "/tmp/thinksmart-volume.log"
VOL_MIN = 0.15
VOL_MAX = 1.00
VOL_STEP = 0.05
AMP = 50
CLICK = "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga"


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def env():
    os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    os.environ.setdefault("PULSE_SERVER", "unix:/run/user/1000/pulse/native")


def wpctl(*args) -> str:
    env()
    p = subprocess.run(
        ["wpctl", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return (p.stdout or "") + (p.stderr or "")


def set_amp(value: int) -> None:
    subprocess.run(
        [
            "amixer",
            "-c",
            "0",
            "cset",
            "name=Speaker Amp Master Playback Volume",
            f"{value},{value}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def current_vol() -> float:
    out = wpctl("get-volume", "@DEFAULT_AUDIO_SINK@")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", out)
    return float(m.group(1)) if m else 1.0


def click() -> None:
    if os.path.isfile(CLICK):
        subprocess.Popen(
            ["paplay", CLICK],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def set_vol(vol: float) -> float:
    vol = max(VOL_MIN, min(VOL_MAX, round(vol, 2)))
    wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")
    wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", f"{vol:.2f}")
    click()
    log(f"vol={vol:.2f} amp={AMP} get={wpctl('get-volume', '@DEFAULT_AUDIO_SINK@').strip()}")
    return vol


def apply_key(code: int, value: int):
    if value not in (1, 2):
        return
    log(f"key code={code} value={value}")
    if code == KEY_VOLUMEUP:
        set_vol(current_vol() + VOL_STEP)
    elif code == KEY_VOLUMEDOWN:
        set_vol(current_vol() - VOL_STEP)


def open_devices():
    fds = {}
    for path in DEVICES:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                log(f"grab {path}")
            except OSError as e:
                log(f"grab fail {path} {e}")
            fds[fd] = path
            log(f"open {path}")
        except OSError as e:
            log(f"open fail {path} {e}")
    return fds


def main():
    env()
    set_amp(AMP)
    set_vol(min(1.00, current_vol() if current_vol() > 0 else 0.80))
    log("start")
    while True:
        fds = open_devices()
        if not fds:
            time.sleep(1)
            continue
        try:
            while True:
                ready, _, _ = select.select(list(fds), [], [], 2.0)
                for fd in ready:
                    data = os.read(fd, EVENT.size * 32)
                    for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                        _sec, _usec, typ, code, value = EVENT.unpack_from(data, off)
                        if typ == EV_KEY:
                            apply_key(code, value)
        except OSError as e:
            log(f"read {e}")
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            time.sleep(0.5)


if __name__ == "__main__":
    main()
