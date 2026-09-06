#!/usr/bin/env python3
"""Atoll IS-08 Audio Channel Mapping API for the music audio -- route the music channel's L24
output channels over NMOS. A controller (our panel, or any NMOS controller) maps the output's
channels to the input's channels; on activation we translate the map to a gst audiomixmatrix and
restart the tiny audiomapper hop, so the routing is audible on the music you're already hearing.

IS-08 is channel ROUTING (each output channel takes exactly one input channel, or silence), not
mixing -- so the expressible presets are straight stereo, swap L<->R, dual-mono (both = L), and
mute a channel. One input `in1` (the decoded music stereo, 2 ch) and one output `out1` (the L24
sender, 2 ch). Serves the IS-08 v1.0 Channel Mapping API and registers node/device in IS-04 with a
cm-ctrl control so it is discoverable. Activations support immediate + scheduled (relative/absolute).
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ATOLL_RUN", "NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "AUDIOMAP_NMOS_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
RUN = CFG.get("ATOLL_RUN") or "/home/david/atoll-run"
PORT = int(CFG.get("AUDIOMAP_NMOS_PORT") or 8094)
MAPFILE = os.path.join(RUN, "audiomap")
MAPPER_SERVICE = "atoll-audiomapper"

ADVERTISE_HOST = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI
SYS = SystemAPI(REGISTRY)
REG = f"{REGISTRY}/x-nmos/registration/v1.3"

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
NODE_ID = str(uuid.uuid5(NS, "atoll:audiomap:node"))
DEVICE_ID = str(uuid.uuid5(NS, "atoll:audiomap:device"))

IN_ID, OUT_ID = "in1", "out1"
CM_BASE = "/x-nmos/channelmapping/v1.0"

# ---- IS-08 state ---------------------------------------------------------------------------------
def _stereo():
    return {OUT_ID: {"0": {"input": IN_ID, "channel_index": 0},
                     "1": {"input": IN_ID, "channel_index": 1}}}
STATE = {
    "active_map": _stereo(),
    "activation": {"mode": None, "requested_time": None, "activation_time": None},
}
_activations = {}      # id -> {"activation": {...}, "action": {...}, "_timer": Timer}
_lock = threading.RLock()

def _tai(when): return f"{int(when) + 37}:{int((when % 1) * 1e9):09d}"
def _dur_to_secs(v):
    try:
        if v is None: return 0.0
        if isinstance(v, (int, float)): return float(v)
        sec, _, nsec = str(v).partition(":"); return int(sec or 0) + int(nsec or 0) / 1e9
    except Exception: return 0.0
def _tai_to_unix(v):
    try:
        sec, _, nsec = str(v).partition(":"); return (int(sec or 0) - 37) + int(nsec or 0) / 1e9
    except Exception: return time.time()

IO = {
    "inputs": {IN_ID: {
        "properties": {"name": "Music source", "description": "Decoded music stereo (pre-map)"},
        "parent": {"type": None, "id": None},
        "channels": [{"label": "Left"}, {"label": "Right"}],
        "caps": {"reordering": True, "block_size": 1}}},
    "outputs": {OUT_ID: {
        "properties": {"name": "Music audio (ST 2110-30)", "description": "L24 sender on the music audio group"},
        "source_id": None,
        "channels": [{"label": "Out 1"}, {"label": "Out 2"}],
        "caps": {"routable_inputs": [IN_ID, None]}}},
}

def _matrix_from_map(m):
    """IS-08 map (out channel -> input channel, or null) -> gst audiomixmatrix out x in routing matrix."""
    out = m.get(OUT_ID, {})
    rows = []
    for i in ("0", "1"):
        e = out.get(i) or {}
        row = [0.0, 0.0]
        if e.get("input") == IN_ID and e.get("channel_index") in (0, 1):
            row[e["channel_index"]] = 1.0
        rows.append(row)
    return "<<%.1f,%.1f>,<%.1f,%.1f>>" % (rows[0][0], rows[0][1], rows[1][0], rows[1][1])

def _apply_map(m, activation_time=None):
    STATE["active_map"] = m
    STATE["activation"] = {"mode": "activate_immediate", "requested_time": None,
                           "activation_time": activation_time or _tai(time.time())}
    matrix = _matrix_from_map(m)
    tmp = MAPFILE + ".tmp"
    with open(tmp, "w") as f: f.write(matrix + "\n")
    os.replace(tmp, MAPFILE)
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", MAPPER_SERVICE],
                       timeout=10, check=False)
    except Exception as e:
        print(f"  mapper restart failed: {e}", flush=True)
    print(f"{time.strftime('%T')} IS-08 map applied -> matrix {matrix}", flush=True)

def _schedule(act_id, action, fire_at_unix, activation):
    def _fire():
        with _lock:
            a = _activations.pop(act_id, None)
            if a is None: return
            activation["activation_time"] = _tai(time.time())
            _apply_map(action, activation["activation_time"])
    t = threading.Timer(max(0.0, fire_at_unix - time.time()), _fire)
    t.daemon = True
    _activations[act_id] = {"activation": activation, "action": action, "_timer": t}
    t.start()

def _pub(a):   # activation dict without the private timer, for responses
    return {"activation": a["activation"], "action": a["action"]}

# ---- HTTP: IS-08 Channel Mapping API -------------------------------------------------------------
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")
        with _lock:
            if p == "/x-nmos": return self._send(200, ["channelmapping/"])
            if p == "/x-nmos/channelmapping": return self._send(200, ["v1.0/"])
            if p == CM_BASE: return self._send(200, ["inputs/", "outputs/", "map/", "io/"])
            if p == f"{CM_BASE}/io": return self._send(200, IO)
            if p == f"{CM_BASE}/inputs": return self._send(200, [f"{IN_ID}/"])
            if p == f"{CM_BASE}/inputs/{IN_ID}":
                return self._send(200, ["properties/", "parent/", "channels/", "caps/"])
            if p == f"{CM_BASE}/inputs/{IN_ID}/properties": return self._send(200, IO["inputs"][IN_ID]["properties"])
            if p == f"{CM_BASE}/inputs/{IN_ID}/parent": return self._send(200, IO["inputs"][IN_ID]["parent"])
            if p == f"{CM_BASE}/inputs/{IN_ID}/channels": return self._send(200, IO["inputs"][IN_ID]["channels"])
            if p == f"{CM_BASE}/inputs/{IN_ID}/caps": return self._send(200, IO["inputs"][IN_ID]["caps"])
            if p == f"{CM_BASE}/outputs": return self._send(200, [f"{OUT_ID}/"])
            if p == f"{CM_BASE}/outputs/{OUT_ID}":
                return self._send(200, ["properties/", "sourceid/", "channels/", "caps/"])
            if p == f"{CM_BASE}/outputs/{OUT_ID}/properties": return self._send(200, IO["outputs"][OUT_ID]["properties"])
            if p == f"{CM_BASE}/outputs/{OUT_ID}/sourceid": return self._send(200, IO["outputs"][OUT_ID]["source_id"])
            if p == f"{CM_BASE}/outputs/{OUT_ID}/channels": return self._send(200, IO["outputs"][OUT_ID]["channels"])
            if p == f"{CM_BASE}/outputs/{OUT_ID}/caps": return self._send(200, IO["outputs"][OUT_ID]["caps"])
            if p == f"{CM_BASE}/map": return self._send(200, ["active/", "activations/"])
            if p == f"{CM_BASE}/map/active":
                return self._send(200, {"activation": STATE["activation"], "map": STATE["active_map"]})
            if p == f"{CM_BASE}/map/active/{OUT_ID}":
                return self._send(200, STATE["active_map"].get(OUT_ID, {}))
            if p == f"{CM_BASE}/map/activations":
                return self._send(200, {k: _pub(v) for k, v in _activations.items()})
            if p.startswith(f"{CM_BASE}/map/activations/"):
                aid = p.rsplit("/", 1)[-1]
                a = _activations.get(aid)
                return self._send(200, _pub(a)) if a else self._send(404, {"code": 404, "error": "Not Found"})
            # convenience readout for the panel
            if p == "/audiomap":
                return self._send(200, {"active_map": STATE["active_map"], "matrix": _matrix_from_map(STATE["active_map"]),
                                        "preset": _preset_name(STATE["active_map"]),
                                        "pending": [ _pub(v) for v in _activations.values() ], "io": IO})
        self._send(404, {"code": 404, "error": "Not Found", "debug": p})

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        if p != f"{CM_BASE}/map/activations":
            return self._send(404, {"code": 404, "error": "Not Found", "debug": p})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"code": 400, "error": "Bad Request", "debug": str(e)})
        action = body.get("action") or {}
        act = dict(body.get("activation") or {})
        mode = act.get("mode"); req_t = act.get("requested_time")
        aid = str(uuid.uuid4())
        with _lock:
            if mode == "activate_immediate":
                act["activation_time"] = _tai(time.time())
                _apply_map(action, act["activation_time"])
                return self._send(200, {aid: {"activation": act, "action": action}})
            elif mode == "activate_scheduled_relative":
                fire = time.time() + _dur_to_secs(req_t)
                act["activation_time"] = _tai(fire)
                _schedule(aid, action, fire, act)
                return self._send(202, {aid: {"activation": act, "action": action}})
            elif mode == "activate_scheduled_absolute":
                fire = _tai_to_unix(req_t)
                act["activation_time"] = req_t
                _schedule(aid, action, fire, act)
                return self._send(202, {aid: {"activation": act, "action": action}})
            return self._send(400, {"code": 400, "error": "Bad Request", "debug": "unknown activation mode"})

    def do_DELETE(self):
        p = urlparse(self.path).path.rstrip("/")
        if p.startswith(f"{CM_BASE}/map/activations/"):
            aid = p.rsplit("/", 1)[-1]
            with _lock:
                a = _activations.pop(aid, None)
                if a and a.get("_timer"): a["_timer"].cancel()
            return self._send(200 if a else 404, {"deleted": aid} if a else {"code": 404, "error": "Not Found"})
        self._send(404, {"code": 404, "error": "Not Found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

# named presets for the panel readout
_PRESETS = {
    "stereo":  {"0": {"input": IN_ID, "channel_index": 0}, "1": {"input": IN_ID, "channel_index": 1}},
    "swap":    {"0": {"input": IN_ID, "channel_index": 1}, "1": {"input": IN_ID, "channel_index": 0}},
    "monoL":   {"0": {"input": IN_ID, "channel_index": 0}, "1": {"input": IN_ID, "channel_index": 0}},
    "muteR":   {"0": {"input": IN_ID, "channel_index": 0}, "1": {"input": None, "channel_index": None}},
}
def _preset_name(m):
    out = m.get(OUT_ID, {})
    for name, spec in _PRESETS.items():
        if out == spec: return name
    return "custom"

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

# ---- IS-04 registration (node / device with a cm-ctrl control) -----------------------------------
def _ver(): return _tai(time.time())
def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r: return r.status
def _resources():
    node = {"id": NODE_ID, "version": _ver(), "label": "atoll-audiomap",
            "description": "Atoll IS-08 audio channel mapping (music audio)", "tags": {},
            "href": f"http://{ADVERTISE_HOST}:{PORT}/", "hostname": "atoll-audiomap",
            "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADVERTISE_HOST, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
    device = {"id": DEVICE_ID, "version": _ver(), "label": "atoll-audiomap", "description": "Music audio channel mapping",
              "tags": {}, "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
              "senders": [], "receivers": [],
              "controls": [{"href": f"http://{ADVERTISE_HOST}:{PORT}{CM_BASE}",
                            "type": "urn:x-nmos:control:cm-ctrl/v1.0", "authorization": False}]}
    return [("node", node), ("device", device)]
_registered = {"ok": False}
def register_all():
    try:
        for kind, data in _resources(): _post(kind, data)
        _registered["ok"] = True
        print(f"  registered audiomap with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- IS-08 API still serves locally", flush=True); return False
def heartbeat():
    while True:
        time.sleep(SYS.heartbeat_interval)
        if not _registered["ok"]:
            register_all(); continue
        try:
            req = urllib.request.Request(f"{REG}/health/nodes/{NODE_ID}", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except urllib.error.HTTPError as e:
            if e.code == 404: _registered["ok"] = False
        except Exception:
            pass

if __name__ == "__main__":
    os.makedirs(RUN, exist_ok=True)
    if not os.path.exists(MAPFILE):
        with open(MAPFILE, "w") as f: f.write(_matrix_from_map(STATE["active_map"]) + "\n")
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"IS-08 Channel Mapping API on http://0.0.0.0:{PORT}{CM_BASE}  (output {OUT_ID}, input {IN_ID})", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
