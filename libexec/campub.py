#!/usr/bin/env python3
"""campub: pull one IP camera and publish every frame as a fresh POSIX shared-memory
object, one JSON line per frame on stdout for the pane mod to blit.

  campub.py --camera q6358 [--tile 0] [--size 640x360] [--fps 15] [--boxes live|aligned|off]

Lines:  {"tile":0,"shm":"/vc3f9a1c-0-17","w":640,"h":360}
        {"tile":0,"state":"connecting|live|reconnecting|stopped|error","camera":"q6358",...}
        {"tile":0,"stats":{"boxes":"live","objects":3,"box_age_ms":480,"meta_lag_ms":560,"held":0,"unmatched":2}}

Two paths:
  --boxes off      ffmpeg decodes the RTSP stream (v0.2). The password reaches ffmpeg
                   through a 0600 concat list file, never argv.
  --boxes live|aligned
                   this process speaks RTSP itself (TCP interleaved, MD5 digest), depacketizes
                   H.264, decodes with PyAV (libavcodec in-process) so every frame keeps its
                   camera capture time (RTP 0xABAC header extension), and draws the ONVIF
                   analytics boxes whose tt:Frame UtcTime equals that capture time (aligned)
                   or the newest ones (live). Needs `pip3 install av`; without it the tile
                   falls back to the ffmpeg path and says so.
Credentials: cameras.json names a cred key; the password is fetched here by running
$VIGIL_CRED_CMD with the key appended (default `~/.claude/bin/cred get`; e.g. the macOS
keychain: `security find-generic-password -w -s`) and reading its stdout.
Python >= 3.13 (multiprocessing.shared_memory track=False).
"""
import sys

if sys.version_info < (3, 13):
    sys.stdout.write('{"tile":0,"state":"error","error":"campub needs python3 >= 3.13 (found %d.%d)"}\n' % sys.version_info[:2])
    sys.exit(2)   # runs before stdout is made non-blocking below

import argparse
import base64
import calendar
import hashlib
import json
import math
import os
import re
import resource
import secrets
import shlex
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from multiprocessing import shared_memory
from urllib.parse import quote, urljoin, urlsplit

REGISTRY = os.environ.get("VIGIL_CAMERAS_JSON", os.path.expanduser("~/Projects/onvif-mcp/cameras.json"))
CRED_CMD = os.environ.get("VIGIL_CRED_CMD", "~/.claude/bin/cred get")   # parsed in password(), inside the error handler
FFMPEG = os.environ.get("VIGIL_FFMPEG", os.path.expanduser("~/.local/bin/ffmpeg"))
CONNECT_S = 15.0 # first frame: RTSP setup + encoder spin-up measured at 4.5-5.8 s on the Q6358 (2026-10-23)
STALL_S = 5.0    # no frame for this long once live -> restart
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


def password(cam):
    cmd = shlex.split(CRED_CMD)   # ValueError on an unmatched quote: reported by the caller's handler
    if not cmd:
        raise ValueError("VIGIL_CRED_CMD is empty")
    cmd[0] = os.path.expanduser(cmd[0])
    try:
        pw = subprocess.run(cmd + [cam["credKey"]], capture_output=True, text=True, check=True,
                            timeout=CRED_TIMEOUT_S).stdout.rstrip("\r\n")   # keep any spaces the password has
    except subprocess.CalledProcessError as e:   # neither error repeats argv: the command may carry a store token
        raise RuntimeError(f"{cmd[0]} exited {e.returncode} for credKey {cam['credKey']!r}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]} took more than {CRED_TIMEOUT_S} s for credKey {cam['credKey']!r}") from None
    if not pw:
        raise ValueError(f"{cmd[0]} printed no password for credKey {cam['credKey']!r}")
    for s in (pw, quote(pw, safe="")):
        if s not in SECRETS:
            SECRETS.append(s)
    return pw


def rtsp_url(cam, w, h, fps):
    host = urlsplit(cam["base"]).hostname
    pw = password(cam)
    user_q, pw_q = quote(cam["user"], safe=""), quote(pw, safe="")
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


# ---------------------------------------------------------------- ffmpeg path (--boxes off)

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


def run_ffmpeg(a, cam, pub, w, h):
    tmp = tempfile.mkdtemp(prefix="campub-")
    url_file = os.path.join(tmp, "in.txt")
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
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.unlink(url_file)
        except FileNotFoundError:
            pass
        os.rmdir(tmp)


# ---------------------------------------------------------------- boxes path (--boxes live|aligned)

NTP_EPOCH = 2208988800
START = b"\x00\x00\x00\x01"
TT = "{http://www.onvif.org/ver10/schema}"
COLOURS = {"Human": (0, 230, 0), "Vehicle": (255, 160, 0)}   # anything else: white
HOLD_FLOOR_S, HOLD_CAP_S, HOLD_MARGIN_S = 0.6, 1.0, 0.1       # aligned: hold = max lag seen in 30 s + margin, clamped
TCP_CONNECT_S = 3.0                                          # a LAN camera accepts in ms; longer means it is down, and stop must not wait
LIVE_MAX_AGE = 3                                             # live: drop boxes older than this many metadata intervals
STATS_S = 1.0


class RtspError(Exception):
    pass


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class Rtsp:
    """RTSP/1.0 over one TCP socket, RTP/RTCP interleaved on it (RFC 2326 §10.12), MD5 digest.
    One framed reader serves both the responses and the `$`-framed media."""

    HEAD_MAX = 64 * 1024
    BODY_MAX = 1024 * 1024

    def __init__(self, host, user, pw):
        self.user, self.pw = user, pw
        self.sock = socket.create_connection((host, 554), timeout=TCP_CONNECT_S)
        self.sock.settimeout(0.25)   # every blocking read wakes often enough to honour stop (SIGTERM only flips a flag)
        self.buf = b""
        self.cseq = 0
        self.auth = None
        self.session = None
        self.timeout_s = 60
        self.keepalive_at = None
        self.keepalive_method = "GET_PARAMETER"

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv(self, deadline):
        while True:
            if stopping():
                raise RtspError("stopping")
            if time.monotonic() > deadline:
                raise socket.timeout("read deadline")
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                raise RtspError("connection closed by the camera")
            self.buf += data
            return

    def read(self, timeout):
        """Next item within `timeout` s: ("rtp", channel, packet) or ("rtsp", head, body).
        Nothing is consumed until a whole item is in, so a timeout leaves the framing intact."""
        deadline = time.monotonic() + timeout
        while True:
            if self.buf[:1] == b"$":
                if len(self.buf) >= 4:
                    ch, n = self.buf[1], struct.unpack(">H", self.buf[2:4])[0]
                    if len(self.buf) >= 4 + n:
                        pkt, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
                        return "rtp", ch, pkt
                self._recv(deadline)
                continue
            if self.buf and not b"RTSP/1.0".startswith(self.buf[:8]):
                marks = [i for i in (self.buf.find(b"$"), self.buf.find(b"RTSP/1.0")) if i >= 0]
                self.buf = self.buf[min(marks):] if marks else self.buf[-7:]   # out of sync: skip to the next frame or response, keep a split prefix
                if not marks:
                    self._recv(deadline)
                continue
            end = self.buf.find(b"\r\n\r\n")
            if end < 0:
                if len(self.buf) > self.HEAD_MAX:
                    raise RtspError("RTSP header too long")
                self._recv(deadline)
                continue
            head = self.buf[:end].decode("latin-1")
            m = re.search(r"^content-length:\s*(\d+)", head, re.I | re.M)
            n = int(m.group(1)) if m else 0
            if n > self.BODY_MAX:
                raise RtspError("RTSP body too long")
            if len(self.buf) < end + 4 + n:
                self._recv(deadline)
                continue
            body, self.buf = self.buf[end + 4:end + 4 + n], self.buf[end + 4 + n:]
            return "rtsp", head, body.decode("utf-8", "replace")

    def send(self, method, url, extra=()):
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: vigil-cameras", *extra]
        if self.session:
            lines.append(f"Session: {self.session}")
        if self.auth:
            realm, nonce = self.auth
            ha1, ha2 = md5(f"{self.user}:{realm}:{self.pw}"), md5(f"{method}:{url}")
            lines.append(f'Authorization: Digest username="{self.user}", realm="{realm}", nonce="{nonce}", '
                         f'uri="{url}", response="{md5(f"{ha1}:{nonce}:{ha2}")}"')
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    def request(self, method, url, extra=()):
        """Setup-phase request: waits for the response (no media flows yet); answers one 401 with digest."""
        for _ in range(2):
            self.send(method, url, extra)
            while True:
                item = self.read(CONNECT_S)
                if item[0] == "rtsp":
                    break
            _, head, body = item
            status = head.split("\r\n")[0]
            if " 401 " in status and self.auth is None:
                realm, nonce = re.search(r'realm="([^"]*)"', head), re.search(r'nonce="([^"]*)"', head)
                if not realm or not nonce:
                    raise RtspError(f"{method}: 401 without a digest challenge")
                self.auth = (realm.group(1), nonce.group(1))
                continue
            if " 200 " not in status:
                raise RtspError(f"{method}: {status}")
            return head, body
        raise RtspError(f"{method}: digest authentication refused")

    def setup(self, control, channel):
        head, _ = self.request("SETUP", control, [f"Transport: RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"])
        m = re.search(r"^session:\s*([^;\r\n\s]+)(.*)$", head, re.I | re.M)
        if not m:
            raise RtspError("SETUP: no Session header")
        self.session = m.group(1)
        t = re.search(r"timeout\s*=\s*(\d+)", m.group(2), re.I)
        if t:
            self.timeout_s = max(5, int(t.group(1)))

    def play(self, url):
        self.request("PLAY", url, ["Range: npt=now-"])
        self.keepalive_at = time.monotonic() + self.timeout_s / 2

    def keepalive(self, url):
        if self.keepalive_at and time.monotonic() >= self.keepalive_at:
            self.send(self.keepalive_method, url)   # the response comes back through read(); see answered()
            self.keepalive_at = time.monotonic() + self.timeout_s / 2

    def answered(self, head):
        """A response that arrived mid-stream (keepalive). 405/501 → OPTIONS next time; any other failure → reconnect."""
        status = head.split("\r\n")[0]
        if " 200 " in status:
            return
        if (" 405 " in status or " 501 " in status) and self.keepalive_method == "GET_PARAMETER":
            self.keepalive_method = "OPTIONS"
            return
        raise RtspError(f"{self.keepalive_method}: {status}")


def rtp_header(pkt):
    """(marker, seq, payload start, payload end, capture time in ms from the 0xABAC extension or None).
    Raises ValueError on a packet that does not fit its own header."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        raise ValueError("short or non-RTP packet")
    b0, b1 = pkt[0], pkt[1]
    end = len(pkt)
    if b0 & 0x20:                                     # padding: the last byte counts the pad
        end -= pkt[-1]
    p = 12 + 4 * (b0 & 0x0F)
    cap = None
    if (b0 >> 4) & 1:
        if p + 4 > end:
            raise ValueError("extension header beyond the packet")
        eid, el = struct.unpack(">HH", pkt[p:p + 4])
        if p + 4 + 4 * el > end:
            raise ValueError("extension beyond the packet")
        if eid == 0xABAC and el >= 2:
            ns, nf = struct.unpack(">II", pkt[p + 4:p + 12])
            cap = (ns - NTP_EPOCH) * 1000 + (nf * 1000) // 2**32   # floor to the ms; UtcTime is floored the same way
        p += 4 + 4 * el
    if p > end:
        raise ValueError("payload beyond the packet")
    return b1 >> 7, struct.unpack(">H", pkt[2:4])[0], p, end, cap


class H264Depacketizer:
    """RFC 6184 single NAL / STAP-A / FU-A → Annex B access units, closed on the marker bit.
    Starts at the first IDR; any corruption drops the current unit and waits for the next IDR."""

    def __init__(self, sps_pps):
        self.sps_pps = sps_pps
        self.seq = None
        self.wait_idr = True
        self._reset()

    def _reset(self, resync=False):
        self.au = bytearray()
        self.cap = None
        self.has_idr = False
        self.fu = False            # inside a fragmented NAL
        if resync:
            self.wait_idr = True

    def feed(self, pkt):
        m, seq, p, end, cap = rtp_header(pkt)
        if self.seq is not None and seq != (self.seq + 1) & 0xFFFF:
            self._reset(resync=True)
        self.seq = seq
        if self.cap is None:
            self.cap = cap
        d = pkt[p:end]
        if not d:
            return None
        t = d[0] & 0x1F
        if 1 <= t <= 23:
            self.fu = False
            self._nal(d)
        elif t == 24:                        # STAP-A
            self.fu = False
            i = 1
            while i < len(d):
                if i + 2 > len(d):
                    return self._reset(resync=True)
                n = struct.unpack(">H", d[i:i + 2])[0]
                if n == 0 or i + 2 + n > len(d):
                    return self._reset(resync=True)
                self._nal(d[i + 2:i + 2 + n])
                i += 2 + n
        elif t == 28 and len(d) >= 2:        # FU-A
            s, e, ft = d[1] >> 7, (d[1] >> 6) & 1, d[1] & 0x1F
            if s:
                self.fu = True
                self._nal(bytes([(d[0] & 0xE0) | ft]) + d[2:])
            elif self.fu:
                self.au += d[2:]
            else:
                return self._reset(resync=True)   # continuation without a start
            if e:
                self.fu = False
        else:
            return self._reset(resync=True)       # unsupported or truncated packetization
        if not m:
            return None
        au, cap, idr = bytes(self.au), self.cap, self.has_idr
        self._reset()
        if self.wait_idr:
            if not idr:
                return None
            self.wait_idr = False
            au = self.sps_pps + au           # parameter sets from the SDP in front of the first unit
        return au, cap

    def _nal(self, nal):
        if nal and nal[0] & 0x1F == 5:
            self.has_idr = True
        self.au += START + nal


def utc_ms(s):
    """ONVIF UtcTime "2026-11-01T15:14:58.677116Z" (or with a ±hh:mm offset) → ms since the epoch,
    floored to the ms like the 0xABAC conversion. None when it does not parse."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$", s.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss, frac, tz = m.groups()
    try:
        base = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        base -= sign * (int(tz[1:3]) * 3600 + int(tz[-2:]) * 60)
    return base * 1000 + int((frac or "0")[:3].ljust(3, "0"))


def _f(v, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def parse_metadata(doc):
    """Every tt:Frame in a metadata document → [(source, utc_ms, [(l, t, r, b, cls), …])].
    Anything malformed is skipped; nothing here can stop the video."""
    out = []
    try:
        root = ET.fromstring(doc)
    except ET.ParseError:
        return out
    for fr in root.iter(TT + "Frame"):
        utc = utc_ms(fr.get("UtcTime", ""))
        if utc is None:
            continue
        sx = sy = 1.0
        tx = ty = 0.0
        tr = fr.find(TT + "Transformation")
        if tr is not None:
            t = tr.find(TT + "Translate")
            s = tr.find(TT + "Scale")
            if t is not None:
                tx, ty = _f(t.get("x"), 0.0), _f(t.get("y"), 0.0)
            if s is not None:
                sx, sy = _f(s.get("x"), 1.0), _f(s.get("y"), 1.0)
        objs = []
        for ob in fr.iter(TT + "Object"):
            bb = ob.find(f"./{TT}Appearance/{TT}Shape/{TT}BoundingBox")
            if bb is None:
                continue
            cls = ob.find(f"./{TT}Appearance/{TT}Class/{TT}Type")
            l, t_, r, b = (_f(bb.get(k), None) for k in ("left", "top", "right", "bottom"))
            if None in (l, t_, r, b):
                continue
            objs.append((l * sx + tx, t_ * sy + ty, r * sx + tx, b * sy + ty, (cls.text or "").strip() if cls is not None else ""))
        out.append((fr.get("Source", ""), utc, objs))
    return out


def burn(frame, w, h, box, boxes):
    """2 px rectangles into an rgb24 bytearray, clipped to box = (ox, oy, cw, ch), the content
    rectangle inside the letterboxed tile. ONVIF coordinates run −1..1 with y up."""
    ox, oy, cw, ch = box
    xmax, ymax = ox + cw - 1, oy + ch - 1
    for l, t, r, b, cls in boxes:
        colour = bytes(COLOURS.get(cls, (255, 255, 255)))
        xs = sorted((ox + (l + 1) / 2 * cw, ox + (r + 1) / 2 * cw))
        ys = sorted((oy + (1 - t) / 2 * ch, oy + (1 - b) / 2 * ch))
        x0, x1 = (max(ox, min(xmax, int(round(v)))) for v in xs)
        y0, y1 = (max(oy, min(ymax, int(round(v)))) for v in ys)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        row = colour * (x1 - x0 + 1)
        for y in (y0, y0 + 1, y1 - 1, y1):
            o = (y * w + x0) * 3
            frame[o:o + len(row)] = row
        side = colour * 2
        for y in range(y0, y1 + 1):
            o = (y * w) * 3
            frame[o + x0 * 3:o + x0 * 3 + 6] = side
            frame[o + (x1 - 1) * 3:o + (x1 - 1) * 3 + 6] = side


def content_rect(src_w, src_h, w, h):
    """Where a src_w×src_h picture lands inside a w×h tile after scale-to-fit and centre pad."""
    s = min(w / src_w, h / src_h)
    cw, ch = max(1, int(round(src_w * s))), max(1, int(round(src_h * s)))
    return (w - cw) // 2, (h - ch) // 2, cw, ch


class Boxes:
    """Metadata documents keyed (source, utc_ms); joins them to frames by capture time.
    Timestamps match within ±1 ms: the camera writes the same instant into the 0xABAC NTP
    fraction and the UtcTime text, but the two encodings can floor to neighbouring ms."""

    TOL = 1

    def __init__(self, mode):
        self.mode = mode
        self.docs = OrderedDict()          # (source, utc) -> (objs, arrival_mono)
        self.latest = {}                   # source -> (utc, objs, arrival_mono): live mode draws every source still fresh
        self.interval_ms = 100             # between document arrivals, measured
        self.last_arrival = None
        self.frame_arrival = OrderedDict() # cap_ms -> arrival_mono, last ~4 s
        self.lags = deque()                # (mono, lag_s) over the last 30 s
        self.held = 0
        self.unmatched = 0
        self.matched = 0

    def frame_seen(self, cap, mono):
        if cap is None:
            return
        self.frame_arrival[cap] = mono
        while len(self.frame_arrival) > 64:
            self.frame_arrival.popitem(last=False)

    def _near(self, table, key):
        for k in (key, key - self.TOL, key + self.TOL):
            if k in table:
                return k
        return None

    def add(self, doc, mono):
        if self.last_arrival is not None:
            gap = (mono - self.last_arrival) * 1000
            if 0 < gap < 5000:
                self.interval_ms = int(0.8 * self.interval_ms + 0.2 * gap)
        self.last_arrival = mono
        for source, utc, objs in parse_metadata(doc):
            self.docs[(source, utc)] = (objs, mono)
            self.latest[source] = (utc, objs, mono)
            k = self._near(self.frame_arrival, utc)
            if k is not None:
                self.lags.append((mono, mono - self.frame_arrival[k]))
                self.matched += 1
            else:
                self.unmatched += 1
        while len(self.docs) > 128:
            self.docs.popitem(last=False)
        cut = mono - 30
        while self.lags and self.lags[0][0] < cut:
            self.lags.popleft()

    def hold_s(self):
        worst = max((l for _, l in self.lags), default=HOLD_FLOOR_S - HOLD_MARGIN_S)
        return min(HOLD_CAP_S, max(HOLD_FLOOR_S, worst + HOLD_MARGIN_S))

    def for_frame(self, cap, mono):
        """(boxes, age_ms, held) for a frame with capture time `cap` published at `mono`.
        age_ms = how far the boxes trail the frame in camera time (frame capture − document time)."""
        if self.mode == "live":
            fresh = [(utc, objs) for utc, objs, arr in self.latest.values() if (mono - arr) * 1000 <= LIVE_MAX_AGE * self.interval_ms]
            if not fresh:
                return [], None, False
            age = max(cap - utc for utc, _ in fresh) if cap is not None else None
            return [o for _, objs in fresh for o in objs], age, False
        if cap is None:
            return [], None, False
        exact = [v for (src, utc), v in self.docs.items() if abs(utc - cap) <= self.TOL]
        if exact:
            return [o for objs, _ in exact for o in objs], 0, False
        prev = [(utc, v) for (src, utc), v in self.docs.items() if self.TOL < cap - utc <= self.interval_ms + 20]
        if prev:
            self.held += 1
            utc = max(u for u, _ in prev)
            return [o for u, (objs, _) in prev if u == utc for o in objs], cap - utc, True
        return [], None, False


def sdp_tracks(sdp, base):
    """[(media, control_url)] per m= section, controls resolved against the DESCRIBE base (RFC 2326 C.1.1)."""
    out = []
    for section in re.split(r"^(?=m=)", sdp, flags=re.M)[1:]:
        media = re.match(r"m=(\w+) ", section)
        ctrl = re.search(r"^a=control:(\S+)", section, re.M)
        if media and ctrl:
            out.append((media.group(1), urljoin(base, ctrl.group(1))))
    return out


IDENTITY = "1.000000,0.000000,0.000000;0.000000,1.000000,0.000000;0.000000,0.000000,1.000000"
META_MAX = 1024 * 1024


def run_boxes(a, cam, pub, w, h, av):
    host = urlsplit(cam["base"]).hostname
    size = w * h * 3
    attempt = 0
    while not stopping():
        pw = password(cam)
        if stopping():
            break
        emit(tile=a.tile, state="connecting", camera=a.camera, attempt=attempt)
        errors = deque(maxlen=5)
        rtsp = None
        live = False
        try:
            rtsp = Rtsp(host, cam["user"], pw)
            del pw
            url = f"rtsp://{host}/axis-media/media.amp?camera=1&videocodec=h264&resolution={w}x{h}&fps={a.fps}&audio=0&analytics=polygon&onvifreplayext=1"
            head, sdp = rtsp.request("DESCRIBE", url, ["Accept: application/sdp"])
            cb = re.search(r"^content-base:\s*(\S+)", head, re.I | re.M)
            tracks = dict(sdp_tracks(sdp, cb.group(1) if cb else url))
            if "video" not in tracks:
                raise RtspError("no video track in the SDP")
            transform = re.search(r"^a=transform:(\S+)", sdp, re.M)
            boxes_ok = transform is None or transform.group(1) == IDENTITY
            if not boxes_ok:
                errors.append("non-identity a=transform in the SDP; boxes off")
            m = re.search(r"sprop-parameter-sets=([^;\s]+)", sdp)
            try:
                sps_pps = b"".join(START + base64.b64decode(x) for x in m.group(1).split(",")) if m else b""
            except ValueError:
                sps_pps = b""
            vch, mch = 0, 2
            rtsp.setup(tracks["video"], vch)
            if "application" in tracks:
                rtsp.setup(tracks["application"], mch)
            else:
                boxes_ok = False
                errors.append("no metadata track; boxes off")
            rtsp.play(url)

            ctx = av.CodecContext.create("h264", "r")
            ctx.flags |= av.codec.context.Flags.low_delay
            ctx.thread_type = "SLICE"
            depack = H264Depacketizer(sps_pps)
            meta = bytearray()
            boxes = Boxes(a.boxes)
            hold = deque()                      # aligned: (rgb bytearray, cap, arrival_mono)
            rect = None
            last = time.monotonic()
            stats_at = last + STATS_S
            objects = age = 0
            bad = 0

            def publish(rgb, cap, arrival):
                nonlocal objects, age
                objs, age_ms, _ = (boxes.for_frame(cap, time.monotonic()) if boxes_ok else ([], None, False))
                if objs:
                    burn(rgb, w, h, rect[:4], objs)
                objects, age = len(objs), age_ms
                pub.publish(rgb)

            while not stopping():
                try:
                    item = rtsp.read(0.25)
                except socket.timeout:
                    item = None
                now = time.monotonic()
                if item is not None and item[0] == "rtsp":
                    rtsp.answered(item[1])
                elif item is not None:
                    _, ch, pkt = item
                    try:
                        if ch == vch:
                            r = depack.feed(pkt)
                        elif ch == mch:
                            r = None
                            m_, _, p, end, _ = rtp_header(pkt)
                            meta += pkt[p:end]
                            if len(meta) > META_MAX:
                                meta.clear()
                            elif m_:
                                boxes.add(bytes(meta), now)
                                meta.clear()
                        else:
                            r = None               # RTCP
                    except (ValueError, struct.error, IndexError):
                        bad += 1                   # one malformed packet: drop it, keep the stream
                        r = None
                        if ch == vch:
                            depack = H264Depacketizer(sps_pps)
                    if r is not None:
                        au, cap = r
                        boxes.frame_seen(cap, now)
                        pkt_ = av.Packet(au)
                        pkt_.pts = pkt_.dts = cap if cap is not None else 0
                        try:
                            frames = ctx.decode(pkt_)
                        except av.error.InvalidDataError:
                            frames = []
                        for f in frames:
                            if rect is None or (f.width, f.height) != rect[4:]:
                                rect = content_rect(f.width, f.height, w, h) + (f.width, f.height)
                            ox, oy, cw, ch_ = rect[:4]
                            pic = f.reformat(width=cw, height=ch_, format="rgb24")
                            plane = pic.planes[0]
                            if cw == w and ch_ == h and plane.line_size == w * 3:
                                rgb = bytearray(plane)
                            else:
                                rgb = bytearray(size)
                                src = bytes(plane)
                                ls = plane.line_size
                                for y in range(ch_):
                                    o = ((oy + y) * w + ox) * 3
                                    rgb[o:o + cw * 3] = src[y * ls:y * ls + cw * 3]
                            last = now
                            if not live:
                                live = True
                                attempt = 0
                                emit(tile=a.tile, state="live", camera=a.camera)
                            if a.boxes == "aligned" and boxes_ok:
                                hold.append((rgb, f.pts if cap is not None else None, now))
                                while len(hold) > 2 * a.fps:   # a hold past 2 s means the terminal or decoder is behind: drop the oldest
                                    hold.popleft()
                            else:
                                publish(rgb, cap, now)
                while hold and now >= hold[0][2] + boxes.hold_s():
                    rgb, cap, arrival = hold.popleft()
                    publish(rgb, cap, arrival)       # Publisher drops what the terminal has not read
                limit = STALL_S if live else CONNECT_S
                if now - last > limit:
                    errors.append(f"no frame for {limit:.0f}s")
                    break
                rtsp.keepalive(url)
                if now >= stats_at:
                    stats_at = now + STATS_S
                    emit(tile=a.tile, stats={"boxes": a.boxes if boxes_ok else "off", "objects": objects, "box_age_ms": age,
                                             "held": boxes.held, "matched": boxes.matched, "unmatched": boxes.unmatched, "bad_packets": bad,
                                             "meta_lag_ms": int(max(l for _, l in boxes.lags) * 1000) if boxes.lags else None,
                                             "hold_ms": int(boxes.hold_s() * 1000) if a.boxes == "aligned" else 0})
        except Exception as e:   # RTSP, socket, decoder or a bug: the tile reconnects rather than dies
            errors.append(redact(f"{type(e).__name__}: {e}"))
        finally:
            if rtsp is not None:
                rtsp.close()
        if stopping():
            break
        delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
        attempt += 1
        emit(tile=a.tile, state="reconnecting", camera=a.camera, attempt=attempt, delay=delay,
             error=redact("; ".join(errors)))
        pause(delay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", required=True)
    ap.add_argument("--tile", type=int, default=0)
    ap.add_argument("--size", default="640x360")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--boxes", choices=("live", "aligned", "off"), default="off")
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.lower().split("x"))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))   # the password lives in this process: never in a core file
    with open(REGISTRY) as f:
        cam = json.load(f)[a.camera]
    pub = Publisher(a.tile, w, h)
    try:
        av = None
        if a.boxes != "off":
            try:
                import av
            except ImportError:
                emit(tile=a.tile, stats={"boxes": "off", "note": "PyAV not installed (pip3 install av); boxes off"})
        if av is not None:
            run_boxes(a, cam, pub, w, h, av)
        else:
            run_ffmpeg(a, cam, pub, w, h)
    except Exception as e:  # cred failure, bad registry entry, ffmpeg missing: say so, then clean up
        emit(tile=a.tile, state="error", camera=a.camera, error=redact(f"{type(e).__name__}: {e}"))
    finally:
        pub.cleanup()
        emit(tile=a.tile, state="stopped", camera=a.camera)


if __name__ == "__main__":
    main()
