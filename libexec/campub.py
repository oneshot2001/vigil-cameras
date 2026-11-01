#!/usr/bin/env python3
"""campub: pull one IP camera with ffmpeg and publish every frame as a fresh POSIX
shared-memory object, one JSON line per frame on stdout for the pane mod to blit.

  campub.py --camera q6358 [--tile 0] [--size 640x360] [--fps 15]

Lines:  {"tile":0,"shm":"/vc3f9a1c-0-17","w":640,"h":360}
        {"tile":0,"state":"connecting|live|reconnecting|stopped|error","camera":"q6358",...}

Credentials: cameras.json names a cred key; the password is fetched here with
~/.claude/bin/cred and reaches ffmpeg through a 0600 concat list file, never argv.
Python >= 3.13 (multiprocessing.shared_memory track=False).
"""
import sys

if sys.version_info < (3, 13):
    sys.stdout.write('{"tile":0,"state":"error","error":"campub needs python3 >= 3.13 (found %d.%d)"}\n' % sys.version_info[:2])
    sys.exit(2)   # runs before stdout is made non-blocking below

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from multiprocessing import shared_memory
from urllib.parse import quote, urlsplit

REGISTRY = os.environ.get("VIGIL_CAMERAS_JSON", os.path.expanduser("~/Projects/onvif-mcp/cameras.json"))
CRED = os.path.expanduser("~/.claude/bin/cred")
FFMPEG = os.environ.get("VIGIL_FFMPEG", os.path.expanduser("~/.local/bin/ffmpeg"))
CONNECT_S = 15.0 # first frame: RTSP setup + encoder spin-up measured at 4.5-5.8 s on the Q6358 (2026-10-23)
STALL_S = 5.0    # no frame for this long once live -> restart ffmpeg
STALE_S = 0.2    # an unread object older than this was never going to be read (blit denied) -> replace it
CRED_TIMEOUT_S = 10
BACKOFF = (1, 2, 4, 8, 10)

stop = threading.Event()   # set from normal code (broken pipe); never from a signal handler
SIGNALLED = False           # Event.set() is not signal-safe (non-reentrant lock): handlers flip a plain flag


def _on_signal(*_):
    global SIGNALLED
    SIGNALLED = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def stopping():
    return SIGNALLED or stop.is_set()


def pause(seconds):
    end = time.monotonic() + seconds
    while not stopping() and time.monotonic() < end:
        time.sleep(0.25)

SECRETS = []  # raw and percent-encoded forms of every credential fetched, for redact()


os.set_blocking(1, False)   # a reader that stopped reading must never pin us past SIGTERM


def emit(**kv):
    """One JSON line on stdout; never blocks. A line cut by a full pipe or by `stop`
    is dropped or left partial, which the mod's JSON parse ignores."""
    data = (json.dumps(kv, separators=(",", ":")) + "\n").encode()
    while data and not stopping():
        try:
            n = os.write(1, data)
        except BlockingIOError:
            return
        except BrokenPipeError:
            stop.set()
            return
        data = data[n:]


def redact(text):
    text = re.sub(r"rtsp://[^@\s/]*@", "rtsp://***@", text)
    alts = sorted((s for s in SECRETS if s), key=len, reverse=True)   # longest first, one pass
    if alts:
        text = re.sub("|".join(re.escape(s) for s in alts), "***", text)
    return text


def rtsp_url(cam, w, h, fps):
    host = urlsplit(cam["base"]).hostname
    pw = subprocess.run([CRED, "get", cam["credKey"]], capture_output=True, text=True, check=True,
                        timeout=CRED_TIMEOUT_S).stdout.strip()
    user_q, pw_q = quote(cam["user"], safe=""), quote(pw, safe="")
    for s in (pw, pw_q):
        if s not in SECRETS:
            SECRETS.append(s)
    return f"rtsp://{user_q}:{pw_q}@{host}/axis-media/media.amp?videocodec=h264&resolution={w}x{h}&fps={fps}"


class Publisher:
    def __init__(self, tile, w, h):
        self.tile, self.w, self.h = tile, w, h
        self.ns = secrets.token_hex(3)   # per-publisher namespace: two sessions never share a name
        self.last = None                 # (name, monotonic time written)
        self.n = 0

    def publish(self, frame):
        if self.last:
            name, t = self.last
            try:
                old = shared_memory.SharedMemory(name=name, track=False)
            except FileNotFoundError:
                old = None
            if old is not None:
                if time.monotonic() - t < STALE_S:
                    old.close()
                    return False            # the terminal has not read it yet: drop this frame
                old.close()
                try:
                    old.unlink()            # never read (pane hidden, blit denied): replace it
                except FileNotFoundError:
                    pass                    # the terminal read it between our open and unlink
        self.n = (self.n + 1) % 100000
        name = f"vc{self.ns}-{self.tile}-{self.n}"   # <= 30 chars with the slash (macOS limit)
        shm = shared_memory.SharedMemory(name=name, create=True, size=len(frame), track=False)
        shm.buf[: len(frame)] = frame
        shm.close()
        self.last = (name, time.monotonic())
        emit(tile=self.tile, shm="/" + name, w=self.w, h=self.h)
        return True

    def cleanup(self):
        if not self.last:
            return
        try:
            s = shared_memory.SharedMemory(name=self.last[0], track=False)
            s.close()
            s.unlink()
        except FileNotFoundError:
            pass
        self.last = None


def spawn_ffmpeg(url_file, w, h):
    argv = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,tcp,udp,rtp",
            "-flags", "low_delay",   # disables frame threading, which held N decoded frames back (~1 s at 15 fps); slice threads stay, codec-required B-frame reordering remains
            "-i", url_file,
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:-1:-1",
            "-fps_mode", "passthrough",   # the camera paces at `fps`; an fps filter added ~1.1 s of start latency and 34 dup frames
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def read_frames(proc, size, latest, got_frame):
    """Reads whole frames from this process's stdout until it closes."""
    while True:
        buf = bytearray(size)
        view = memoryview(buf)
        n = 0
        while n < size:
            k = proc.stdout.readinto(view[n:])
            if not k:
                return
            n += k
        latest[0] = bytes(buf)
        got_frame.set()


def read_errors(proc, errors):
    for line in proc.stderr:
        errors.append(redact(line.decode("utf-8", "replace").rstrip()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", required=True)
    ap.add_argument("--tile", type=int, default=0)
    ap.add_argument("--size", default="640x360")
    ap.add_argument("--fps", type=int, default=15)
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.lower().split("x"))
    with open(REGISTRY) as f:
        cam = json.load(f)[a.camera]

    tmp = tempfile.mkdtemp(prefix="campub-")
    url_file = os.path.join(tmp, "in.txt")
    pub = Publisher(a.tile, w, h)
    size = w * h * 3
    attempt = 0
    proc = None
    try:
        while not stopping():
            url = rtsp_url(cam, w, h, a.fps)
            if stopping():
                break
            fd = os.open(url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"file '{url}'\noption rtsp_transport tcp\noption fflags nobuffer\n")
            del url
            emit(tile=a.tile, state="connecting", camera=a.camera, attempt=attempt)
            if stopping():
                break
            proc = spawn_ffmpeg(url_file, w, h)

            latest = [None]
            got_frame = threading.Event()
            errors = deque(maxlen=5)
            rt = threading.Thread(target=read_frames, args=(proc, size, latest, got_frame), daemon=True)
            et = threading.Thread(target=read_errors, args=(proc, errors), daemon=True)
            rt.start()
            et.start()

            last = time.monotonic()
            live = False
            while not stopping():
                if got_frame.wait(0.25):
                    got_frame.clear()
                    pub.publish(latest[0])
                    last = time.monotonic()
                    if not live:
                        live = True
                        attempt = 0
                        try:
                            os.unlink(url_file)   # ffmpeg has it open; the password leaves the disk
                        except FileNotFoundError:
                            pass
                        emit(tile=a.tile, state="live", camera=a.camera)
                if proc.poll() is not None:
                    break
                limit = STALL_S if live else CONNECT_S
                if time.monotonic() - last > limit:
                    errors.append(f"no frame for {limit:.0f}s")
                    break
            proc.kill()
            proc.wait()
            rt.join(2)        # its pipe closed with the process; never let it read the next one's bytes
            et.join(2)
            if stopping():
                break
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            emit(tile=a.tile, state="reconnecting", camera=a.camera, attempt=attempt, delay=delay,
                 error=redact("; ".join(errors)))
            pause(delay)
    except Exception as e:  # cred failure, bad registry entry, ffmpeg missing: say so, then clean up
        emit(tile=a.tile, state="error", camera=a.camera, error=redact(f"{type(e).__name__}: {e}"))
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        pub.cleanup()
        try:
            os.unlink(url_file)
        except FileNotFoundError:
            pass
        os.rmdir(tmp)
        emit(tile=a.tile, state="stopped", camera=a.camera)


if __name__ == "__main__":
    main()
