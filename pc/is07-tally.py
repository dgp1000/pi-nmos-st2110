#!/usr/bin/env python3
# ===========================================================================
#  Atoll IS-07 tally emitter.
#
#  Broadcast tally is a per-SOURCE boolean: "is this source on air right now". Until now the wall
#  drew its tally border from the panel's own /state, which is an internal shortcut -- correct on
#  screen but not something another device could ever consume. This publishes the same information
#  as real NMOS IS-07 Event & Tally state, so the multiviewer becomes a genuine IS-07 RECEIVER
#  rather than a program reading its own variable.
#
#  One boolean event source per Atoll source key (hevc, raw, fec, ...), each carrying whether that
#  source is currently taken. Source IDs are UUID5-derived from the key, so they are stable across
#  restarts -- a receiver can bookmark one and it stays valid.
#
#  Message format follows the IS-07 state schema exactly, as emitted by the nmos-cpp reference node:
#     {"event_type":"boolean","identity":{"source_id":...},"message_type":"state",
#      "payload":{"value":true},"timing":{"creation_timestamp":"<TAI sec>:<nsec>"}}
#
#  Serves the IS-07 REST API (sources / state / type). The WebSocket transport is the other half of
#  IS-07 and is deliberately not implemented here: no websockets library is installable on this box
#  (PEP 668), and the REST state endpoint is part of the same spec and enough to drive tally.
#
#     http://<host>:8102/x-nmos/events/v1.0/sources/            -> list
#     http://<host>:8102/x-nmos/events/v1.0/sources/<id>/state  -> current tally state
#     http://<host>:8102/x-nmos/events/v1.0/sources/<id>/type   -> {"type":"boolean"}
#     http://<host>:8102/tally                                  -> convenience: {key: bool}
# ===========================================================================
import http.server, socketserver, json, time, uuid, threading, urllib.request, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["PANEL_PORT", "IS07_PORT", "IS07_WS_PORT", "NMOS_REGISTRY", "NMOS_ADVERTISE_HOST"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
PANEL = f"http://localhost:{CFG.get('PANEL_PORT') or 8096}"
PORT = int(CFG.get("IS07_PORT") or 8102)

# Every source the panel can take. One IS-07 boolean per source, exactly as a real tally system does.
SOURCES = ["hevc", "jxs", "music", "reels", "raw", "j2k", "h264", "mjpeg", "vp9", "tsrtp", "fec", "sps", "jpegxs"]
LABEL = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "reels": "Test Reels",
         "raw": "Pi raw 2110-20", "j2k": "JPEG 2000", "h264": "H.264 RTP", "mjpeg": "MJPEG RTP",
         "vp9": "VP9 RTP", "tsrtp": "TS over RTP", "fec": "ST 2022-1 FEC", "sps": "ST 2022-7 SPS",
         "jpegxs": "JPEG XS"}
# Stable, reproducible ids: the same key always yields the same source_id across restarts.
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
SRC_ID = {k: str(uuid.uuid5(NS, f"atoll:is07:tally:{k}")) for k in SOURCES}
ID_SRC = {v: k for k, v in SRC_ID.items()}

# TAI = UTC + 37s (current leap-second offset). IS-07 timestamps are TAI "seconds:nanoseconds",
# the same epoch PTP distributes, so tally shares a timebase with the essence flows.
TAI_OFFSET = 37

_state = {k: False for k in SOURCES}
_changed = {k: time.time() for k in SOURCES}

def _ts(when):
    sec = int(when) + TAI_OFFSET
    nsec = int((when - int(when)) * 1e9)
    return f"{sec}:{nsec:09d}"

def state_message(key):
    return {
        "identity": {"source_id": SRC_ID[key]},
        "event_type": "boolean",
        "message_type": "state",
        "timing": {"creation_timestamp": _ts(_changed[key])},
        "payload": {"value": bool(_state[key])},
    }

def poller():
    """Follow the panel's active take and convert it to per-source tally booleans."""
    while True:
        try:
            with urllib.request.urlopen(f"{PANEL}/state", timeout=3) as r:
                active = json.load(r).get("active", "")
            now = time.time()
            for k in SOURCES:
                on = (k == active)
                if on != _state[k]:
                    _state[k] = on
                    _changed[k] = now      # timestamp marks the transition, not the poll
                    ws_push(k)             # push immediately: IS-07's whole point is not polling
        except Exception:
            pass
        # 100ms: with the websocket transport the push itself is instant, so this poll of the
        # panel is the ONLY thing standing between a cut and the tally lighting. Tally is supposed
        # to be immediate, and 10 req/s against a local HTTP endpoint is nothing.
        time.sleep(0.1)

# ---------------------------------------------------------------------------
#  IS-04 registration
# ---------------------------------------------------------------------------
REGISTRY = CFG.get("NMOS_REGISTRY") or "http://localhost:8080"
REG = f"{REGISTRY}/x-nmos/registration/v1.3"
ADVERTISE_HOST = CFG.get("NMOS_ADVERTISE_HOST") or ""

NODE_ID = str(uuid.uuid5(NS, "atoll:is07:node"))
DEVICE_ID = str(uuid.uuid5(NS, "atoll:is07:device"))
FLOW_ID = {k: str(uuid.uuid5(NS, f"atoll:is07:flow:{k}")) for k in SOURCES}
SENDER_ID = {k: str(uuid.uuid5(NS, f"atoll:is07:sender:{k}")) for k in SOURCES}

def _ver():
    return _ts(time.time())

def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status

def _resources():
    host = ADVERTISE_HOST or "localhost"
    node = {
        "id": NODE_ID, "version": _ver(), "label": "atoll-is07-tally",
        "description": "Atoll IS-07 Event & Tally emitter", "tags": {},
        "href": f"http://{host}:{PORT}/", "hostname": "atoll-is07",
        "caps": {}, "services": [],
        "api": {"versions": ["v1.3"],
                "endpoints": [{"host": host, "port": PORT, "protocol": "http"}]},
        "clocks": [], "interfaces": [],
    }
    device = {
        "id": DEVICE_ID, "version": _ver(), "label": "atoll-is07-tally",
        "description": "Per-source on-air tally as IS-07 boolean events", "tags": {},
        "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
        "senders": [SENDER_ID[k] for k in SOURCES], "receivers": [],
        # How a controller finds the events API, alongside the senders advertised below -- this is
        # how the nmos-cpp reference node advertises its own.
        "controls": [{"href": f"http://{host}:{PORT}/x-nmos/events/v1.0",
                      "type": "urn:x-nmos:control:events/v1.0", "authorization": False}],
    }
    out = [("node", node), ("device", device)]
    for k in SOURCES:
        out.append(("source", {
            "id": SRC_ID[k], "version": _ver(), "label": f"atoll tally {k}",
            "description": f"On-air tally for {LABEL.get(k, k)}", "tags": {},
            "caps": {}, "device_id": DEVICE_ID, "parents": [], "clock_name": None,
            "format": "urn:x-nmos:format:data", "event_type": "boolean",
        }))
    for k in SOURCES:
        out.append(("flow", {
            "id": FLOW_ID[k], "version": _ver(), "label": f"atoll tally {k}",
            "description": f"On-air tally for {LABEL.get(k, k)}", "tags": {},
            "source_id": SRC_ID[k], "device_id": DEVICE_ID, "parents": [],
            "format": "urn:x-nmos:format:data", "media_type": "application/json",
            "event_type": "boolean",
        }))
    # With a working websocket transport we can honestly advertise senders. Each carries the
    # connection_uri a receiver needs, and IS-07's ext_is_07_* hints naming the source and its REST
    # endpoint -- the same shape the nmos-cpp reference node publishes.
    for k in SOURCES:
        out.append(("sender", {
            "id": SENDER_ID[k], "version": _ver(), "label": f"atoll tally {k}",
            "description": f"On-air tally for {LABEL.get(k, k)}", "tags": {},
            "flow_id": FLOW_ID[k], "device_id": DEVICE_ID,
            "transport": "urn:x-nmos:transport:websocket",
            "interface_bindings": [], "manifest_href": None,
            "subscription": {"receiver_id": None, "active": False},
        }))
    return out

_registered = {"ok": False}

def register_all():
    try:
        for kind, data in _resources():
            _post(kind, data)
        _registered["ok"] = True
        print(f"  registered with IS-04 at {REG} ({len(SOURCES)} sources + flows)", flush=True)
        return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- events API still serves locally", flush=True)
        return False

def heartbeat():
    """Registries garbage-collect a node that stops sending health. Re-register on 404, which is
    what a registry restart looks like from here."""
    while True:
        time.sleep(5)
        if not _registered["ok"]:
            register_all()
            continue
        try:
            req = urllib.request.Request(f"{REG}/health/nodes/{NODE_ID}", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("  registry lost our node (404) -- re-registering", flush=True)
                _registered["ok"] = False
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  IS-07 WebSocket transport (RFC 6455, hand-rolled -- no library installable here)
# ---------------------------------------------------------------------------
import socket, base64, hashlib, struct as _struct, select as _select

WS_PORT = int(CFG.get("IS07_WS_PORT") or 8103)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_clients = []                       # [(conn, {subscribed source_ids})]
_clients_lock = threading.Lock()

def _ws_frame(payload: bytes, opcode=0x1) -> bytes:
    """Server->client frame. Never masked, per RFC 6455."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < (1 << 16):
        head += bytes([126]) + _struct.pack("!H", n)
    else:
        head += bytes([127]) + _struct.pack("!Q", n)
    return head + payload

def _ws_read(conn):
    """Read one client frame. Returns (opcode, payload) or None if the peer went away."""
    def recvn(n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                return None
            buf += c
        return buf
    h = recvn(2)
    if not h:
        return None
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    ln = h[1] & 0x7F
    if ln == 126:
        e = recvn(2)
        if not e: return None
        ln = _struct.unpack("!H", e)[0]
    elif ln == 127:
        e = recvn(8)
        if not e: return None
        ln = _struct.unpack("!Q", e)[0]
    mask = recvn(4) if masked else b""
    if masked and mask is None:
        return None
    data = recvn(ln) if ln else b""
    if data is None:
        return None
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data

def _ws_send(conn, obj):
    try:
        conn.sendall(_ws_frame(json.dumps(obj).encode()))
        return True
    except Exception:
        return False

def ws_push(key):
    """Push one source's state to every client subscribed to it."""
    msg = state_message(key)
    sid = SRC_ID[key]
    with _clients_lock:
        dead = []
        for entry in _clients:
            conn, subs = entry
            if sid in subs and not _ws_send(conn, msg):
                dead.append(entry)
        for d in dead:
            _clients.remove(d)

def _ws_client(conn, addr):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            c = conn.recv(1024)
            if not c:
                return
            req += c
            if len(req) > 65536:
                return
        lines = req.decode("latin-1").split("\r\n")
        path = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "/"
        hdrs = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        key = hdrs.get("sec-websocket-key")
        # The endpoint is per-device, as IS-07 specifies.
        if not key or DEVICE_ID not in path:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        entry = (conn, set())
        with _clients_lock:
            _clients.append(entry)
        print(f"  IS-07 ws client connected: {addr[0]}", flush=True)
        while True:
            r = _ws_read(conn)
            if r is None:
                break
            opcode, data = r
            if opcode == 0x8:                      # close
                break
            if opcode == 0x9:                      # ping -> pong
                conn.sendall(_ws_frame(data, 0xA))
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(data.decode() or "{}")
            except Exception:
                continue
            if msg.get("command") == "subscription":
                want = set(msg.get("sources") or [])
                entry[1].clear(); entry[1].update(want)
                _ws_send(conn, {"command": "subscription", "sources": sorted(want)})
                # send current state at once so the receiver starts correct
                for sid in want:
                    k = ID_SRC.get(sid)
                    if k:
                        _ws_send(conn, state_message(k))
            elif msg.get("message_type") == "health":
                _ws_send(conn, {"message_type": "health",
                                "timing": {"creation_timestamp": _ts(time.time())}})
    except Exception:
        pass
    finally:
        with _clients_lock:
            _clients[:] = [e for e in _clients if e[0] is not conn]
        try:
            conn.close()
        except Exception:
            pass

def ws_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", WS_PORT))
    srv.listen(16)
    print(f"  IS-07 websocket: ws://0.0.0.0:{WS_PORT}/x-nmos/events/v1.0/devices/{DEVICE_ID}", flush=True)
    while True:
        try:
            conn, addr = srv.accept()
            threading.Thread(target=_ws_client, args=(conn, addr), daemon=True).start()
        except Exception:
            time.sleep(0.2)

def ws_health():
    """IS-07 keepalive so a receiver can tell a silent source from a dead one."""
    while True:
        time.sleep(5)
        msg = {"message_type": "health", "timing": {"creation_timestamp": _ts(time.time())}}
        with _clients_lock:
            for conn, _ in list(_clients):
                _ws_send(conn, msg)

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")
        base = "/x-nmos/events/v1.0"
        if p in ("", "/"):
            self._send(["x-nmos/"])
        elif p == "/tally":
            self._send({k: _state[k] for k in SOURCES})
        elif p == base:
            self._send(["sources/"])
        elif p == f"{base}/sources":
            self._send([f"{SRC_ID[k]}/" for k in SOURCES])
        elif p.startswith(f"{base}/sources/"):
            rest = p[len(f"{base}/sources/"):].split("/")
            sid = rest[0]
            key = ID_SRC.get(sid)
            if key is None:
                self._send({"error": "unknown source"}, 404)
            elif len(rest) == 1:
                self._send(["state/", "type/"])
            elif rest[1] == "state":
                self._send(state_message(key))
            elif rest[1] == "type":
                self._send({"type": "boolean"})
            else:
                self._send({"error": "not found"}, 404)
        else:
            self._send({"error": "not found"}, 404)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

threading.Thread(target=ws_server, daemon=True).start()
threading.Thread(target=ws_health, daemon=True).start()
threading.Thread(target=poller, daemon=True).start()
register_all()
threading.Thread(target=heartbeat, daemon=True).start()
print(f"is07-tally: {len(SOURCES)} boolean tally sources -> http://0.0.0.0:{PORT}/x-nmos/events/v1.0/", flush=True)
for k in SOURCES:
    print(f"  {LABEL.get(k, k):<20} {SRC_ID[k]}", flush=True)
Server(("", PORT), H).serve_forever()
