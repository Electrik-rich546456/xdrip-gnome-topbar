#!/usr/bin/env python3
"""
xDrip top-bar collector (started and stopped by the GNOME extension "xDrip Top Bar").

Joins the xDrip Cloud Sync group like one more follower phone and keeps two small files in
the data folder chosen in the extension's settings:

  latest.json    newest glucose reading + collector status (read by the top bar)
  history.json   rolling 48 hours of readings, treatments (insulin / carbs) and finger-sticks

PASSIVE by default: it only listens and sends nothing, so it does not appear on the phone.
With --active it sends the battery + roll-call requests a follower phone sends and shows up in
xDrip "System status > Followers" under --name (default gnome_info_collector).

The sync key is read from the first line of standard input (--key-stdin, what the extension
does) or from the environment variable XDRIP_SYNC_KEY.  It is never printed or written to disk.

Needs: python3 and the 'cryptography' package.

  python3 xdrip_gnome_collector.py --data-dir ~/xdrip_topbar --key-stdin < keyfile
  python3 xdrip_gnome_collector.py --selftest
"""

import argparse
try:
    import fcntl                     # Linux / macOS
except ImportError:                  # Windows
    fcntl = None
    import msvcrt
import gzip
import hashlib
import json
import os
import random
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

# ----------------------------------------------------------------------------
# Constants taken from the xDrip source (same as xdrip_cloud_follow_v3)
# ----------------------------------------------------------------------------
SERVER = "jamcm3749021.bluejay.website"
PORT = 25935
SUPPORTED_VERSIONS = "1,1"
WAIT_PING_TIMEOUT_S = 120
IDLE_TIMEOUT_S = 600
ASK_EVERY_S = 3600
MAX_BINARY_SIZE = 4096
TRANSPORT_SYNC_MSG = 2
CMD_HEARTBEAT, CMD_PONG, CMD_PING = "K", "O", "P"
CMD_MESSAGE, CMD_MESSAGE_RECEIVED, CMD_OK, CMD_CEASE = "M", "N", "OK", "CEASE"
STATIC_KEY = "ebe5c0df162a50ba232d2d721ea8e3e1c5423bb0-12bd-48c3-8932-c93883dfcf1f"
TREND_NAMES = {1: "DoubleUp", 2: "SingleUp", 3: "FortyFiveUp", 4: "Flat",
               5: "FortyFiveDown", 6: "SingleDown", 7: "DoubleDown", 0: "NotComputable"}
SECRET_ACTIONS = {"bfb"}
DEFAULT_NAME = "gnome_info_collector"
KEEP_MS = 48 * 3600 * 1000
MGDL_PER_MMOL = 18.0182


def log(text):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), text), flush=True)


# ----------------------------------------------------------------------------
# Keys and crypto
# ----------------------------------------------------------------------------
def derive_keys(sync_key):
    k = sync_key.encode("utf-8")
    topic = hashlib.sha256(k).hexdigest()[:32]
    aes_key = hashlib.md5((STATIC_KEY + hashlib.md5(k).hexdigest()).encode("utf-8")).digest()
    return topic, aes_key


def decrypt(aes_key, blob):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding
    if len(blob) < 32 or (len(blob) - 16) % 16:
        raise ValueError("bad length %d" % len(blob))
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(blob[:16])).decryptor()
    padded = dec.update(blob[16:]) + dec.finalize()
    unp = padding.PKCS7(128).unpadder()
    data = unp.update(padded) + unp.finalize()
    if len(data) > 8 and data[:4] == b"\x1f\x8b\x08\x00":
        data = gzip.decompress(data)
    return data


def encrypt(aes_key, plain, iv=None):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding
    iv = iv or os.urandom(16)
    pad = padding.PKCS7(128).padder()
    ct = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    body = pad.update(plain) + pad.finalize()
    return iv + ct.update(body) + ct.finalize()


# ----------------------------------------------------------------------------
# Minimal protobuf
# ----------------------------------------------------------------------------
def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def pb_fields(buf):
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v = buf[i:i + 8]; i += 8
        elif wt == 2:
            n, i = _varint(buf, i)
            v = buf[i:i + n]; i += n
        elif wt == 5:
            v = buf[i:i + 4]; i += 4
        else:
            raise ValueError("unsupported wire type %d" % wt)
        yield field, wt, v


def zigzag_decode(n):
    return (n >> 1) ^ -(n & 1)


def zigzag_encode(n):
    return (n << 1) ^ (n >> 63)


def pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_field(field, wt, payload):
    return pb_varint(field << 3 | wt) + payload


def pb_bytes(field, data):
    return pb_field(field, 2, pb_varint(len(data)) + data)


def parse_transport(buf):
    t, payload = 0, b""
    for f, wt, v in pb_fields(buf):
        if f == 3 and wt == 0:
            t = v
        elif f == 5 and wt == 2:
            payload = v
    return t, payload


def parse_syncmsg(buf):
    m = {"action": "", "payload": b"", "from": "", "topic": ""}
    for f, wt, v in pb_fields(buf):
        if f == 2 and wt == 2:
            m["action"] = v.decode("utf-8", "replace")
        elif f == 3 and wt == 2:
            m["payload"] = v
        elif f == 4 and wt == 2:
            m["from"] = v.decode("utf-8", "replace")
        elif f == 5 and wt == 2:
            m["topic"] = v.decode("utf-8", "replace")
    return m


def build_transport(action, payload, topic=None, sender=None):
    sync = pb_bytes(2, action.encode()) + pb_bytes(3, payload)
    if sender is not None:
        sync += pb_bytes(4, sender.encode())
    if topic is not None:
        sync += pb_bytes(5, topic.encode())
    return pb_field(1, 0, pb_varint(1)) + pb_field(3, 0, pb_varint(TRANSPORT_SYNC_MSG)) + pb_bytes(5, sync)


def crc32_le(data):
    return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)


def _pb_double(v):
    return struct.unpack("<d", v)[0]


# ----------------------------------------------------------------------------
# xDrip message contents
# ----------------------------------------------------------------------------
def trend_ordinal(slope_per_min, hide):
    """BgReading.getSlopeOrdinal()"""
    if hide:
        return 0
    if slope_per_min <= -3.5: return 7
    if slope_per_min <= -2: return 6
    if slope_per_min <= -1: return 5
    if slope_per_min <= 1: return 4
    if slope_per_min <= 2: return 3
    if slope_per_min <= 3.5: return 2
    return 1


def make_reading(ts_ms, mgdl, slope_per_ms, hide=False):
    sl = 0.0 if slope_per_ms is None else slope_per_ms * 60000.0
    t = trend_ordinal(sl, hide or slope_per_ms is None)
    return {"t": int(ts_ms), "mgdl": round(float(mgdl), 1), "mmol": round(float(mgdl) / MGDL_PER_MMOL, 2),
            "slope": round(sl, 3), "trend": TREND_NAMES[t]}


def readings_from_bgs(text):
    out = []
    for part in text.split("^"):
        part = part.strip()
        if part:
            j = json.loads(part)
            out.append(make_reading(j["timestamp"], j["calculated_value"],
                                    j.get("calculated_value_slope"), bool(j.get("hide_slope", False))))
    return out


def readings_from_bgmm(data):
    if len(data) < 5 or crc32_le(data[:-4]) != data[-4:]:
        raise ValueError("checksum mismatch (wrong key?)")
    out = []
    for f, wt, v in pb_fields(data[:-4]):
        if f != 1 or wt != 2:
            continue
        ts = mg = slope = None
        hide = False
        for f2, wt2, v2 in pb_fields(v):
            if f2 == 1 and wt2 == 0:
                ts = zigzag_decode(v2)
            elif f2 == 7 and wt2 == 1:
                mg = _pb_double(v2)
            elif f2 == 9 and wt2 == 1:
                slope = _pb_double(v2)
            elif f2 == 15 and wt2 == 0:
                hide = bool(v2)
        if ts is not None and mg is not None:
            out.append(make_reading(ts, mg, slope, hide))
    return out


def bloodtests_from_btmm(data):
    if len(data) < 5 or crc32_le(data[:-4]) != data[-4:]:
        raise ValueError("checksum mismatch (wrong key?)")
    out = []
    for f, wt, v in pb_fields(data[:-4]):
        if f != 1 or wt != 2:
            continue
        t = {}
        for f2, wt2, v2 in pb_fields(v):
            if f2 == 1 and wt2 == 0:
                t["t"] = zigzag_decode(v2)
            elif f2 == 2 and wt2 == 1:
                t["mgdl"] = round(_pb_double(v2), 1)
        if "mgdl" in t and "t" in t:
            t["mmol"] = round(t["mgdl"] / MGDL_PER_MMOL, 2)
            out.append(t)
    return out


def decode_message(aes_key, msg):
    action, payload = msg["action"], msg["payload"]
    ev = {"action": action, "readings": [], "bloodtests": [], "text": None, "json": None}
    if not payload:
        return ev
    if len(payload) <= 16:                     # xDrip sends tiny payloads unencrypted
        ev["text"] = payload.decode("utf-8", "replace")
        return ev
    plain = decrypt(aes_key, payload)
    if action == "bgmm":
        ev["readings"] = readings_from_bgmm(plain)
        return ev
    if action == "btmm":
        ev["bloodtests"] = bloodtests_from_btmm(plain)
        return ev
    text = plain.decode("utf-8", "replace")
    ev["text"] = text
    if action == "bgs":
        ev["readings"] = readings_from_bgs(text)
    elif text[:1] in "{[":
        try:
            ev["json"] = json.loads(text)
        except ValueError:
            pass
    return ev


# ----------------------------------------------------------------------------
# The data folder: latest.json + rolling 48 h history.json
# ----------------------------------------------------------------------------
def write_json_atomic(path, doc):
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:          # Windows: the file is open in another program for a moment
                if attempt == 9:
                    raise
                time.sleep(0.1)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    def __init__(self, folder, active, name, now=time.time, source="xdrip-cloud"):
        self.folder = folder
        self.now = now
        self.latest_path = os.path.join(folder, "latest.json")
        self.history_path = os.path.join(folder, "history.json")
        self.readings, self.treatments, self.bloodtests = {}, {}, {}
        self.status = {"connected": False, "source": source,
                       "active": bool(active), "name": name if active else None,
                       "started_ms": self._now_ms(), "last_message_ms": None, "pid": os.getpid(),
                       "problem": None}
        self._load()
        self.save()

    def _now_ms(self):
        return int(self.now() * 1000)

    def _load(self):
        try:
            with open(self.history_path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return
        for r in doc.get("readings", []):
            if isinstance(r, dict) and "t" in r and "mgdl" in r:
                self.readings[int(r["t"])] = r
        for t in doc.get("treatments", []):
            if isinstance(t, dict) and t.get("id"):
                self.treatments[t["id"]] = t
        for b in doc.get("bloodtests", []):
            if isinstance(b, dict) and "t" in b:
                self.bloodtests[int(b["t"])] = b

    def _prune(self):
        cut = self._now_ms() - KEEP_MS
        for d in (self.readings, self.bloodtests):
            for k in [k for k, v in d.items() if v["t"] < cut]:
                del d[k]
        for k in [k for k, v in self.treatments.items() if v["t"] < cut]:
            del self.treatments[k]

    def save(self):
        self._prune()
        readings = sorted(self.readings.values(), key=lambda r: r["t"])
        now = self._now_ms()
        write_json_atomic(self.history_path, {
            "version": 1, "updated_ms": now, "keep_hours": KEEP_MS // 3600000,
            "readings": readings,
            "treatments": sorted(self.treatments.values(), key=lambda t: t["t"]),
            "bloodtests": sorted(self.bloodtests.values(), key=lambda b: b["t"])})
        latest = {"version": 1, "updated_ms": now, "status": self.status,
                  "reading": readings[-1] if readings else None,
                  "previous": readings[-2] if len(readings) > 1 else None}
        write_json_atomic(self.latest_path, latest)

    def replace_treatments(self, treatments):
        """Nightscout: the full list for the last 48 h (so deletions disappear too)."""
        new = {t["id"]: t for t in treatments}
        if new != self.treatments:
            self.treatments = new
            self.save()
            return True
        return False

    def set_status(self, **kw):
        changed = any(self.status.get(k) != v for k, v in kw.items())
        self.status.update(kw)
        if changed:
            self.save()

    def handle(self, ev):
        """Apply one decoded message.  Returns True if anything was stored."""
        a, text = ev["action"], ev["text"]
        self.status["last_message_ms"] = self._now_ms()
        changed = False
        for r in ev["readings"]:
            if self.readings.get(r["t"]) != r:
                self.readings[r["t"]] = r
                changed = True
        for b in ev["bloodtests"]:
            b = {"t": int(b["t"]), "mgdl": b["mgdl"], "mmol": b["mmol"]}
            if self.bloodtests.get(b["t"]) != b:
                self.bloodtests[b["t"]] = b
                changed = True
        if a == "nt" and isinstance(ev["json"], dict):
            j = ev["json"]
            tid = j.get("uuid") or "%s|%s|%s" % (j.get("timestamp"), j.get("insulin"), j.get("carbs"))
            t = {"id": tid, "t": int(j.get("timestamp", 0) or 0),
                 "insulin": float(j.get("insulin", 0) or 0), "carbs": float(j.get("carbs", 0) or 0),
                 "insulin_detail": j.get("insulinJSON") or "",
                 "notes": (j.get("notes") or "").replace("\n", " ")[:200]}
            if t["t"] and self.treatments.get(tid) != t:      # a repeated uuid is an edit
                self.treatments[tid] = t
                changed = True
        elif a == "dt":
            if self.treatments.pop((text or "").strip(), None) is not None:
                changed = True
        elif a == "dat":
            if self.treatments:
                self.treatments.clear()
                changed = True
        # a write at least every few minutes keeps last_message_ms fresh in latest.json
        if changed or a in ("bgs", "bgmm", "ns"):
            self.save()
        return changed


# ----------------------------------------------------------------------------
# Cloud connection (as in xdrip_cloud_follow_v3)
# ----------------------------------------------------------------------------
class LineSocket:
    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def _fill(self):
        chunk = self.s.recv(4096)
        if not chunk:
            raise ConnectionError("server closed the connection")
        self.buf += chunk

    def readline(self):
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_BINARY_SIZE:
                raise ValueError("line too long")
            self._fill()
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace")

    def read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send(self, data):
        self.s.sendall(data.encode() if isinstance(data, str) else data)


def rollcall_json(name):
    serial = hashlib.sha1(name.encode()).hexdigest()[:16]
    return json.dumps({"device_manufactuer": "Python", "device_model": "xdrip_gnome_collector",
                       "device_serial": serial, "device_name": name, "android_version": "0",
                       "xdrip_version": "gnome extension", "role": "Follower", "battery": -1,
                       "bridge_battery": -1, "cloud": 1}, separators=(",", ":"))


class Follower:
    def __init__(self, host, port, token, topic, aes_key, on_event, on_status, passive=True, name=DEFAULT_NAME):
        self.host, self.port, self.token = host, port, token
        self.topic, self.aes_key = topic, aes_key
        self.on_event, self.on_status = on_event, on_status
        self.passive, self.name = passive, name
        self.stop = threading.Event()
        self.sock = None

    def shutdown(self):
        """Stop from another thread: ends run() within a moment instead of at the next timeout."""
        self.stop.set()
        s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self):
        backoff = 5
        while not self.stop.is_set():
            try:
                self._session()
                backoff = 5
            except Exception as e:
                self.on_status(connected=False, problem=str(e)[:120])
                log("connection problem: %s - retrying in %ds" % (e, backoff))
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 180)

    def send_sync(self, ls, action, payload=b""):
        trans = build_transport(action, payload)
        ls.send(("%s %d\n" % (CMD_MESSAGE, len(trans))).encode() + trans)

    def _session(self):
        log("connecting to %s:%d" % (self.host, self.port))
        sock = socket.create_connection((self.host, self.port), timeout=45)
        sock.settimeout(WAIT_PING_TIMEOUT_S)
        self.sock = sock
        ls = LineSocket(sock)
        try:
            ls.send("%s\n%s\n%s\n" % (SUPPORTED_VERSIONS, self.token, self.topic))
            first = ls.readline()
            if first != CMD_OK:
                raise ConnectionError("server did not answer OK (got %r)" % first[:40])
            self.on_status(connected=True, problem=None)
            log("connected (%s)" % ("passive" if self.passive else "active as %s" % self.name))
            last_heard = time.monotonic()
            last_ask = None
            while not self.stop.is_set():
                if not self.passive and (last_ask is None or time.monotonic() - last_ask > ASK_EVERY_S):
                    self.send_sync(ls, "sbr", b"")
                    self.send_sync(ls, "rlcl", encrypt(self.aes_key, rollcall_json(self.name).encode()))
                    last_ask = time.monotonic()
                try:
                    line = ls.readline()
                except socket.timeout:
                    if time.monotonic() - last_heard > IDLE_TIMEOUT_S:
                        raise ConnectionError("server silent for too long")
                    ls.send(CMD_PING + "\n")
                    continue
                last_heard = time.monotonic()
                self._handle(ls, line)
        finally:
            self.sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, ls, line):
        parts = line.split(" ", 2)
        cmd = parts[0]
        size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        param = parts[2] if len(parts) > 2 else ""
        if size > MAX_BINARY_SIZE:
            raise ValueError("oversized packet")
        binary = ls.read_exact(size) if size else b""
        if cmd == CMD_PONG:
            ls.send(CMD_HEARTBEAT + "\n")
        elif cmd == CMD_MESSAGE:
            ttype, payload = parse_transport(binary)
            if ttype == TRANSPORT_SYNC_MSG:
                msg = parse_syncmsg(payload)
                if (not msg["topic"] or msg["topic"] == self.topic) and msg["action"] not in SECRET_ACTIONS:
                    try:
                        ev = decode_message(self.aes_key, msg)
                    except Exception as e:
                        log("could not decode '%s' message: %s (sync key mismatch?)" % (msg["action"], e))
                    else:
                        self.on_event(ev)
            ls.send(CMD_MESSAGE_RECEIVED + (" 0 " + param if param else "") + "\n")
        elif cmd == CMD_CEASE:
            raise ConnectionError("server asked clients to stop (CEASE)")


# ----------------------------------------------------------------------------
# Nightscout (alternative data source: read-only REST API v1)
# ----------------------------------------------------------------------------
NS_TRENDS = {"DoubleUp", "SingleUp", "FortyFiveUp", "Flat", "FortyFiveDown", "SingleDown", "DoubleDown"}
NS_POLL_S = 60                      # new readings are checked every minute
NS_SLOW_S = 300                     # treatments and finger-sticks every 5 minutes


def _ms_from_iso(text):
    import datetime
    t = str(text).strip().replace("Z", "+00:00")
    d = datetime.datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp() * 1000)


def _iso_from_ms(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def ns_reading(e):
    """Nightscout sgv entry -> reading dict, or None."""
    sgv, t = e.get("sgv"), e.get("date")
    try:
        sgv, t = float(sgv), int(t)
    except (TypeError, ValueError):
        return None
    if sgv < 39:                                     # values below 39 are sensor error codes
        return None
    slope = None
    if e.get("delta") is not None:
        slope = _num(e.get("delta")) / 5.0           # mg/dL per 5 min -> per minute
    trend = e.get("direction")
    if trend not in NS_TRENDS:
        trend = TREND_NAMES[trend_ordinal(slope, False)] if slope is not None else "NotComputable"
    return {"t": t, "mgdl": round(sgv, 1), "mmol": round(sgv / MGDL_PER_MMOL, 2),
            "slope": round(slope, 3) if slope is not None else 0.0, "trend": trend}


def ns_treatment(t):
    insulin, carbs = _num(t.get("insulin")), _num(t.get("carbs"))
    if insulin <= 0 and carbs <= 0:
        return None
    when = t.get("mills") or t.get("date")
    try:
        when = int(when) if when else _ms_from_iso(t.get("created_at") or t.get("timestamp"))
    except (TypeError, ValueError):
        return None
    tid = str(t.get("_id") or t.get("identifier") or "%s|%s|%s" % (when, insulin, carbs))
    return {"id": tid, "t": when, "insulin": insulin, "carbs": carbs, "insulin_detail": "",
            "notes": str(t.get("notes") or t.get("eventType") or "").replace("\n", " ")[:200]}


class NightscoutFollower:
    """Polls a Nightscout site.  secret: an access token (recommended, read-only role) or the
    API_SECRET; both are tried automatically.  Leave empty for a site that is readable without one."""

    def __init__(self, url, secret, on_event, on_status, on_treatments, poll_s=NS_POLL_S, slow_s=NS_SLOW_S,
                 now=time.time):
        url = (url or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "https://" + url
        self.base = url
        self.secret = (secret or "").strip()
        self.on_event, self.on_status, self.on_treatments = on_event, on_status, on_treatments
        self.poll_s, self.slow_s, self.now = poll_s, slow_s, now
        self.stop = threading.Event()
        self.mode = None                             # "token" | "secret" | "none", found on first success

    def shutdown(self):
        self.stop.set()

    def _get(self, path, params):
        modes = [self.mode] if self.mode else (["token", "secret"] if self.secret else ["none"])
        refused = None
        for mode in modes:
            q = dict(params)
            headers = {"Accept": "application/json", "User-Agent": "xdrip-topbar-collector"}
            if mode == "token":
                q["token"] = self.secret
            elif mode == "secret":
                headers["api-secret"] = hashlib.sha1(self.secret.encode("utf-8")).hexdigest()
            req = urllib.request.Request(self.base + path + "?" + urllib.parse.urlencode(q), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    refused = PermissionError("Nightscout refused access (HTTP %d) - check the token or API secret"
                                              % e.code)
                    continue
                raise ConnectionError("Nightscout answered HTTP %d" % e.code)
            if self.mode != mode:
                self.mode = mode
                log("Nightscout: connected (%s)" % {"token": "access token", "secret": "API secret",
                                                     "none": "no token"}[mode])
            return data
        self.mode = None
        raise refused or ConnectionError("Nightscout could not be reached")

    def run(self):
        if not self.base:
            self.on_status(connected=False, problem="no Nightscout address set")
            return
        log("Nightscout: polling %s" % self.base)
        since = None
        next_slow = 0
        backoff = self.poll_s
        while not self.stop.is_set():
            now = int(self.now() * 1000)
            start = now - KEEP_MS
            try:
                frm = max(start, since) if since else start
                entries = self._get("/api/v1/entries/sgv.json", {"count": 1000, "find[date][$gte]": frm})
                readings = [r for r in (ns_reading(e) for e in entries if isinstance(e, dict)) if r]
                tests = []
                if time.monotonic() >= next_slow:
                    for e in self._get("/api/v1/entries/mbg.json", {"count": 200, "find[date][$gte]": start}):
                        try:
                            mg = float(e.get("mbg"))
                            tests.append({"t": int(e.get("date")), "mgdl": round(mg, 1),
                                          "mmol": round(mg / MGDL_PER_MMOL, 2)})
                        except (TypeError, ValueError, AttributeError):
                            pass
                    tr = self._get("/api/v1/treatments.json",
                                   {"count": 1000, "find[created_at][$gte]": _iso_from_ms(start)})
                    self.on_treatments([t for t in (ns_treatment(x) for x in tr if isinstance(x, dict)) if t])
                    next_slow = time.monotonic() + self.slow_s
                self.on_status(connected=True, problem=None)
                self.on_event({"action": "ns", "readings": readings, "bloodtests": tests, "text": None, "json": None})
                if readings:
                    since = max(r["t"] for r in readings) - 15 * 60000     # small overlap catches late uploads
                backoff = self.poll_s
            except Exception as e:
                self.on_status(connected=False, problem=str(e)[:120])
                log("Nightscout problem: %s - retrying in %ds" % (e, backoff))
                if self.stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 600)
                continue
            if self.stop.wait(self.poll_s):
                break


# ----------------------------------------------------------------------------
# Running under the extension
# ----------------------------------------------------------------------------
def _is_collector(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return b"xdrip_gnome_collector" in fh.read()
    except OSError:
        return False


def take_lock(folder, takeover=True):
    """Only one collector per data folder.  Returns the lock file descriptor (keep it open), or None.
    With takeover, an older collector still holding the lock (e.g. left behind when GNOME Shell was
    restarted with Alt+F2 r, which keeps the same process id) is asked to stop and replaced."""
    path = os.path.join(folder, ".collector.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)       # no truncation: the holder's pid is in it

    def try_lock():
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    if not try_lock():
        old = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            old = int(os.read(fd, 32).decode().strip() or 0)
        except (OSError, ValueError):
            pass
        if not takeover or not old or old == os.getpid() or not _is_collector(old):
            os.close(fd)
            return None
        log("an older collector (pid %d) is still running - stopping it" % old)
        try:
            os.kill(old, signal.SIGTERM)
        except OSError:
            pass
        for i in range(50):                                  # up to 10 s
            time.sleep(0.2)
            if try_lock():
                break
            if i == 25:
                try:
                    os.kill(old, signal.SIGKILL)
                except OSError:
                    pass
        else:
            os.close(fd)
            return None
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    return fd


def watch_parent(follower):
    """Exit when the process that started us (GNOME Shell) goes away."""
    parent = os.getppid()

    def loop():
        while not follower.stop.wait(5):
            if os.getppid() != parent:
                log("parent process gone - stopping")
                os._exit(0)          # files are always written atomically, so this is safe
    threading.Thread(target=loop, daemon=True).start()


def get_token(folder):
    path = os.path.join(folder, ".collector_token")
    try:
        with open(path) as fh:
            tok = fh.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = "gnome-info-collector-%032x" % random.getrandbits(128)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def main():
    ap = argparse.ArgumentParser(description="xDrip collector for the GNOME top-bar extension")
    ap.add_argument("--data-dir", help="folder for latest.json and history.json")
    ap.add_argument("--key-stdin", action="store_true",
                    help="read the sync key (or the Nightscout token / API secret) from the first line of stdin")
    ap.add_argument("--source", choices=("xdrip-cloud", "nightscout"), default="xdrip-cloud",
                    help="where the data comes from (default: xDrip Cloud Sync)")
    ap.add_argument("--ns-url", help="Nightscout address, e.g. https://mysite.example.com")
    ap.add_argument("--active", action="store_true", help="announce in roll call and ask for batteries")
    ap.add_argument("--name", default=DEFAULT_NAME, help="roll-call name when --active (default %(default)s)")
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-parent-watch", action="store_true", help="keep running if the parent exits")
    ap.add_argument("--selftest", action="store_true", help="offline test, then exit")
    ap.add_argument("--hold-lock", help=argparse.SUPPRESS)          # used by the self test only
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if args.hold_lock:
        if take_lock(args.hold_lock, takeover=False) is None:
            sys.exit(3)
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        time.sleep(60)
        return
    if not args.data_dir:
        sys.exit("--data-dir is required")

    key = sys.stdin.readline().strip() if args.key_stdin else os.environ.get(
        "XDRIP_SYNC_KEY" if args.source == "xdrip-cloud" else "NIGHTSCOUT_SECRET", "").strip()
    if args.source == "xdrip-cloud" and not key:
        sys.exit("no sync key given (set it in the extension settings)")
    if args.source == "nightscout" and not args.ns_url:
        sys.exit("no Nightscout address given (set it in the extension settings)")

    folder = os.path.abspath(os.path.expanduser(args.data_dir))
    os.makedirs(folder, exist_ok=True)
    lock = take_lock(folder)
    if lock is None:
        sys.exit("another collector is already writing to %s" % folder)

    lock_ = threading.Lock()
    store_box = []

    def on_event(ev):
        with lock_:
            try:
                store_box[0].handle(ev)
            except Exception as e:
                log("could not store message: %s" % e)

    def on_status(**kw):
        with lock_:
            try:
                store_box[0].set_status(**kw)
            except Exception as e:
                log("could not write status: %s" % e)

    def on_treatments(items):
        with lock_:
            try:
                store_box[0].replace_treatments(items)
            except Exception as e:
                log("could not store treatments: %s" % e)

    if args.source == "nightscout":
        log("data folder %s  source Nightscout" % folder)
        store = Store(folder, False, None, source="nightscout")
        store_box.append(store)
        f = NightscoutFollower(args.ns_url, key, on_event, on_status, on_treatments)
        del key
    else:
        name = (args.name or DEFAULT_NAME).strip() or DEFAULT_NAME
        topic, aes_key = derive_keys(key)
        del key
        log("sync group id %s...  data folder %s  mode %s" % (topic[:8], folder, "active" if args.active else "passive"))
        store = Store(folder, args.active, name)
        store_box.append(store)
        f = Follower(args.server, args.port, get_token(folder), topic, aes_key, on_event, on_status,
                     passive=not args.active, name=name)

    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    if not args.no_parent_watch:
        watch_parent(f)
    try:
        f.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        f.stop.set()
        try:
            with lock_:
                store.set_status(connected=False, problem="stopped")
        except Exception:
            pass
        log("stopped")


# ----------------------------------------------------------------------------
# Offline self test: fake server, fake messages, check the two files
# ----------------------------------------------------------------------------
def ns_selftest(check):
    """Fake Nightscout that only accepts the API secret (so the token is tried first and refused)."""
    import http.server
    secret = "my-api-secret-123"
    now = int(time.time() * 1000)
    state = {"treatments": [
        {"_id": "t1", "created_at": _iso_from_ms(now - 3600000), "insulin": "2.5", "eventType": "Correction Bolus"},
        {"_id": "t2", "mills": now - 1800000, "carbs": 40, "eventType": "Meal Bolus"},
        {"_id": "t3", "created_at": _iso_from_ms(now - 600000), "eventType": "Note", "notes": "no amounts"}],
        "seen_token": False, "hits": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if "token" in q:
                state["seen_token"] = True
            if self.headers.get("api-secret") != hashlib.sha1(secret.encode()).hexdigest():
                self.send_response(401)
                self.end_headers()
                return
            state["hits"] += 1
            if u.path == "/api/v1/entries/sgv.json":
                frm = int(q.get("find[date][$gte]", ["0"])[0])
                data = [e for e in [
                    {"sgv": 150, "date": now - 900000, "direction": "Flat"},
                    {"sgv": 10, "date": now - 600000, "direction": "NONE"},             # error code -> skipped
                    {"sgv": 162, "date": now - 300000, "direction": "NOT COMPUTABLE", "delta": 12},
                    {"sgv": 171, "date": now - 1000, "direction": "SingleUp"}] if e["date"] >= frm]
            elif u.path == "/api/v1/entries/mbg.json":
                data = [{"mbg": 180, "date": now - 120000}]
            elif u.path == "/api/v1/treatments.json":
                data = state["treatments"]
            else:
                data = []
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    folder = tempfile.mkdtemp(prefix="xdrip-ns-selftest-")
    store = Store(folder, False, None, source="nightscout")
    lk = threading.Lock()

    def ev(e):
        with lk:
            store.handle(e)

    def st(**kw):
        with lk:
            store.set_status(**kw)

    def tr(items):
        with lk:
            store.replace_treatments(items)

    f = NightscoutFollower("http://127.0.0.1:%d/" % srv.server_address[1], secret, ev, st, tr, poll_s=0.2, slow_s=0)
    threading.Thread(target=f.run, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline and not (store.readings and store.treatments):
        time.sleep(0.05)
    with lk:
        mg = [r["mgdl"] for r in sorted(store.readings.values(), key=lambda r: r["t"])]
        trends = [r["trend"] for r in sorted(store.readings.values(), key=lambda r: r["t"])]
        trs = dict(store.treatments)
    check(mg == [150.0, 162.0, 171.0], "Nightscout readings stored (sensor error code skipped)")
    check(trends == ["Flat", "SingleUp", "SingleUp"], "Nightscout trends, missing direction worked out from delta")
    check(state["seen_token"] and f.mode == "secret", "token tried first, then API secret used")
    check(set(trs) == {"t1", "t2"} and trs["t1"]["insulin"] == 2.5 and trs["t2"]["carbs"] == 40.0,
          "Nightscout treatments stored (entries without insulin or carbs ignored)")
    check(store.bloodtests and list(store.bloodtests.values())[0]["mgdl"] == 180.0, "Nightscout finger-stick stored")
    state["treatments"] = state["treatments"][1:]                          # t1 deleted on the site
    time.sleep(0.8)
    with lk:
        check(set(store.treatments) == {"t2"}, "treatment deleted on Nightscout disappears")
        check(store.status.get("connected") and store.status.get("source") == "nightscout", "status says Nightscout, connected")
    f.shutdown()
    srv.shutdown()
    with open(os.path.join(folder, "latest.json")) as fh:
        check(json.load(fh)["reading"]["mgdl"] == 171.0, "latest.json has the newest Nightscout reading")


def selftest():
    sync_key = "this-is-a-test-sync-key-1234567890"
    topic, aes_key = derive_keys(sync_key)
    now_ms = int(time.time() * 1000)
    ok = True

    def check(cond, text):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + text)
        ok = ok and cond

    def rd(ts, mg, sl):
        return pb_bytes(1, pb_field(1, 0, pb_varint(zigzag_encode(ts))) +
                        pb_field(7, 1, struct.pack("<d", mg)) + pb_field(9, 1, struct.pack("<d", sl)))

    old = rd(now_ms - 50 * 3600000, 150.0, 0.0)                       # older than 48 h -> pruned
    multi = old + rd(now_ms - 600000, 180.0, 0.0) + rd(now_ms - 300000, 95.5, -2.5 / 60000)
    one = json.dumps({"timestamp": now_ms, "calculated_value": 88.0, "calculated_value_slope": -1.2 / 60000})
    bt = pb_bytes(1, pb_field(1, 0, pb_varint(zigzag_encode(now_ms - 120000))) + pb_field(2, 1, struct.pack("<d", 108.0)))
    t1 = json.dumps({"uuid": "aaa", "timestamp": now_ms - 900000, "insulin": 4.0, "carbs": 0,
                     "insulinJSON": '[{"units":4.0,"insulin":"Rapid"}]', "notes": "x"})
    t1e = json.dumps({"uuid": "aaa", "timestamp": now_ms - 900000, "insulin": 5.0, "carbs": 0, "notes": "edited"})
    t2 = json.dumps({"uuid": "bbb", "timestamp": now_ms - 800000, "insulin": 0, "carbs": 30})
    t3 = json.dumps({"uuid": "ccc", "timestamp": now_ms - 700000, "insulin": 0, "carbs": 10})
    msgs = [
        ("bgmm", encrypt(aes_key, multi + crc32_le(multi))),
        ("bgs", encrypt(aes_key, one.encode())),
        ("btmm", encrypt(aes_key, bt + crc32_le(bt))),
        ("nt", encrypt(aes_key, t1.encode())),
        ("nt", encrypt(aes_key, t2.encode())),
        ("nt", encrypt(aes_key, t3.encode())),
        ("nt", encrypt(aes_key, t1e.encode())),
        ("dt", b"ccc"),                                                  # short -> unencrypted
        ("bfb", encrypt(aes_key, b"someid^SECRETKEYMATERIAL")),
        ("bgs", encrypt(derive_keys("other key")[1], one.encode())),     # wrong key -> rejected
    ]

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    saw = {"acks": [], "sent": []}

    def server():
        c, _ = srv.accept()
        ls = LineSocket(c)
        saw["hello"] = [ls.readline(), ls.readline(), ls.readline()]
        c.sendall(b"OK\n")
        c.settimeout(0.3)
        try:                                       # a passive client must not send anything here
            saw["sent"].append(c.recv(100))
        except socket.timeout:
            pass
        c.settimeout(5)
        for i, (action, payload) in enumerate(msgs):
            m = build_transport(action, payload, topic, "master")
            c.sendall(("M %d id%d\n" % (len(m), i)).encode() + m)
            saw["acks"].append(ls.readline())
        time.sleep(0.2)
        c.close()
        srv.close()

    threading.Thread(target=server, daemon=True).start()
    folder = tempfile.mkdtemp(prefix="xdrip-selftest-")
    store = Store(folder, False, None)
    lk = threading.Lock()

    def on_event(ev):
        with lk:
            store.handle(ev)

    def on_status(**kw):
        with lk:
            store.set_status(**kw)

    f = Follower("127.0.0.1", port, "selftest-token", topic, aes_key, on_event, on_status, passive=True)
    th = threading.Thread(target=f.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and len(saw["acks"]) < len(msgs):
        time.sleep(0.05)
    time.sleep(0.3)
    f.stop.set()

    with open(os.path.join(folder, "history.json")) as fh:
        h = json.load(fh)
    with open(os.path.join(folder, "latest.json")) as fh:
        latest = json.load(fh)
    raw = open(os.path.join(folder, "history.json")).read() + open(os.path.join(folder, "latest.json")).read()

    check(saw.get("hello", [None])[0] == "1,1" and saw["hello"][2] == topic, "client sends versions and topic")
    check(saw["sent"] == [], "passive client sends nothing to the group")
    check(saw["acks"] == ["N 0 id%d" % i for i in range(len(msgs))], "every message acknowledged")
    check([r["mgdl"] for r in h["readings"]] == [180.0, 95.5, 88.0], "readings stored in order, >48 h pruned")
    check(h["readings"][1]["trend"] == "SingleDown" and h["readings"][2]["trend"] == "FortyFiveDown", "trend names")
    check(latest["reading"]["mgdl"] == 88.0 and latest["previous"]["mgdl"] == 95.5, "latest.json has newest reading")
    tr = {t["id"]: t for t in h["treatments"]}
    check(set(tr) == {"aaa", "bbb"}, "deleted treatment removed")
    check(tr["aaa"]["insulin"] == 5.0 and tr["aaa"]["notes"] == "edited", "edited treatment updated in place")
    check(tr["bbb"]["carbs"] == 30.0, "carbs stored")
    check(h["bloodtests"] and h["bloodtests"][0]["mgdl"] == 108.0, "finger-stick stored")
    check("SECRETKEYMATERIAL" not in raw and sync_key not in raw, "no key material in the files")
    check(latest["status"]["active"] is False and latest["status"]["name"] is None, "status says passive")

    store2 = Store(folder, False, None)
    check(len(store2.readings) == 3 and len(store2.treatments) == 2, "history survives a restart")
    ns_selftest(check)
    if fcntl is None:                                    # Windows: single in-process collector, no takeover
        check(take_lock(folder, takeover=False) is not None, "data folder lock taken")
        rc = json.loads(rollcall_json(DEFAULT_NAME))
        check(rc["device_name"] == "gnome_info_collector", "roll call name gnome_info_collector")
        print("\nSELF TEST", "PASSED" if ok else "FAILED")
        return 0 if ok else 1
    import subprocess
    holder = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--hold-lock", folder])
    time.sleep(0.8)
    check(take_lock(folder, takeover=False) is None, "lock held by a running collector is respected")
    fd = take_lock(folder, takeover=True)
    try:
        holder.wait(timeout=5)
    except subprocess.TimeoutExpired:
        holder.kill()
    check(fd is not None and holder.returncode is not None, "an older collector is stopped and replaced")
    check(take_lock(folder, takeover=False) is None, "new collector now holds the lock")
    rc = json.loads(rollcall_json(DEFAULT_NAME))
    check(rc["device_name"] == "gnome_info_collector" and rc["role"] == "Follower", "roll call name gnome_info_collector")
    print("\nSELF TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
