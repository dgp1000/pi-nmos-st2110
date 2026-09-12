#!/usr/bin/env python3
"""Atoll AMWA IS-11 Stream Compatibility Management API (v1.0).

IS-11 is the "will these actually work together?" layer on top of IS-05 routing: receivers advertise
what they will accept, senders can be constrained to stay compatible and report their status, and
physical inputs/outputs carry EDID (the HDMI-style capability handshake). This service registers a
self-contained NMOS device -- node + device (with the `stream-compat` and `sr-ctrl` controls) + one
source/flow/sender + one receiver + one input + one output -- and serves three APIs on one port:

  * IS-04-visible resources (registered with the registry, heartbeated) so a controller discovers it,
  * a minimal **IS-05** Connection API for the sender + receiver (staged/active/constraints/
    transportfile) -- IS-11 builds on it,
  * the **IS-11** Stream Compatibility API: /senders (status, constraints/active[GET,PUT,DELETE],
    constraints/supported, inputs), /receivers (status, outputs), /inputs ({properties, edid/base
    [GET,PUT,DELETE], edid/effective}), /outputs ({properties, edid}).

The input supports EDID: PUT a Base EDID (validated -- header + 128-byte-block checksum, else 400),
GET it back, DELETE it; the Effective EDID is the Base (or a built-in default) reported to the
upstream. Setting active constraints flips the sender status unconstrained<->constrained. Everything
is in-memory demo state -- the point is a spec-shaped, discoverable, conformance-testable IS-11.

Env/conf: NMOS_REGISTRY, NMOS_ADVERTISE_HOST, ISLAND_PC_IP, PTP_GMID, IS11_PORT.
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "IS11_PORT", "ISLAND_PC_IP", "PTP_GMID"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
PORT = int(CFG.get("IS11_PORT") or 8099)
ADV = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI
SYS = SystemAPI(REGISTRY)
REG = f"{REGISTRY}/x-nmos/registration/v1.3"
PC_IP = (CFG.get("ISLAND_PC_IP") or "10.10.10.2").strip()
GMID = (CFG.get("PTP_GMID") or "").strip()
GRP, MPORT = "239.10.10.90", 5060                       # placeholder transport for the demo sender

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID = u("atoll:is11:node");  DEV_ID = u("atoll:is11:device")
SRC_ID  = u("atoll:is11:src");   FLOW_ID = u("atoll:is11:flow"); SEND_ID = u("atoll:is11:sender")
RECV_ID = u("atoll:is11:receiver"); INPUT_ID = u("atoll:is11:input"); OUTPUT_ID = u("atoll:is11:output")

IS11_BASE = f"http://{ADV}:{PORT}/x-nmos/streamcompatibility/v1.0/"
IS05_BASE = f"http://{ADV}:{PORT}/x-nmos/connection/v1.1/"
REFCLK = f"a=ts-refclk:ptp=IEEE1588-2008:{GMID}:0\r\n" if GMID else "a=ts-refclk:ptp=IEEE1588-2008:traceable\r\n"

def _tai(t=None):
    t = time.time() if t is None else t
    return f"{int(t)}:{int((t % 1) * 1e9)}"
def _ver(): return _tai()
# Stable per-resource versions: a resource's `version` must only change when the resource changes,
# not on every GET -- otherwise controllers/tests that poll until the version settles never finish.
_VERS = {}
def _rv(k):
    if k not in _VERS: _VERS[k] = _tai()
    return _VERS[k]
def _bump(k): _VERS[k] = _tai()

# ---- a minimal but valid 128-byte EDID (header + version 1.4 + fixed checksum) ------------------
def _make_edid():
    e = bytearray(128)
    e[0:8] = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])   # EDID header
    e[8:10] = bytes([0x04, 0x21])          # manufacturer id (arbitrary)
    e[18] = 0x01; e[19] = 0x04             # EDID version 1.4
    e[20] = 0x80; e[21] = 0x50; e[22] = 0x2D            # digital, 80x45 cm
    e[54:72] = bytes([0x01, 0x1D, 0x00, 0x72, 0x51, 0xD0, 0x1E, 0x20, 0x6E, 0x28,
                      0x55, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1E])   # 720p detailed timing-ish
    e[127] = (256 - (sum(e[:127]) % 256)) % 256         # block checksum -> total mod 256 == 0
    return bytes(e)
def _make_edid2():
    e = bytearray(_make_edid())
    e[8:10] = bytes([0x10, 0xAC])           # different manufacturer id -> distinct from DEFAULT
    e[127] = (256 - (sum(e[:127]) % 256)) % 256
    return bytes(e)
DEFAULT_EDID = _make_edid()
CONSTRAINED_EDID = _make_edid2()

def edid_valid(b):
    """A valid EDID: >=128 bytes in 128-byte blocks, correct header, each block checksum sums to 0."""
    if not b or len(b) % 128 != 0:
        return False
    if b[0:8] != bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]):
        return False
    for i in range(0, len(b), 128):
        if sum(b[i:i + 128]) % 256 != 0:
            return False
    return True

# ---- live demo state ---------------------------------------------------------------------------
STATE = {
    "sender_active_constraints": {"constraint_sets": []},
    "input_base_edid": None,                # bytes or None
    "sender_master_enable": False,
    "receiver_master_enable": False,
    "receiver_sender_id": None,
    "flow_grain_rate": {"numerator": 25, "denominator": 1},
}
SUPPORTED_PARAMS = [
    "urn:x-nmos:cap:meta:label", "urn:x-nmos:cap:meta:preference", "urn:x-nmos:cap:meta:enabled",
    "urn:x-nmos:cap:format:media_type", "urn:x-nmos:cap:format:grain_rate",
    "urn:x-nmos:cap:format:frame_width", "urn:x-nmos:cap:format:frame_height",
    "urn:x-nmos:cap:format:interlace_mode", "urn:x-nmos:cap:format:colorspace",
    "urn:x-nmos:cap:format:color_sampling", "urn:x-nmos:cap:format:component_depth",
    "urn:x-nmos:cap:format:transfer_characteristic",
]

def sender_status():
    st = "constrained" if STATE["sender_active_constraints"]["constraint_sets"] else "unconstrained"
    return {"state": st}
def receiver_status():
    return {"state": "unknown"}
def effective_edid():
    if STATE["sender_active_constraints"]["constraint_sets"]:
        return CONSTRAINED_EDID           # Base adjusted to the sender's active caps
    return STATE["input_base_edid"] or DEFAULT_EDID

# ---- SDP / transportfile for the sender --------------------------------------------------------
def _sdp():
    tp = IS05["sender"]["active"]["transport_params"][0]     # SDP must reflect the ACTIVE params
    dst_ip = tp.get("destination_ip") or GRP
    dst_port = tp.get("destination_port") or MPORT
    src_ip = tp.get("source_ip") or PC_IP
    v = int(time.time())
    return ("v=0\r\n"
            f"o=- {v} {v} IN IP4 {PC_IP}\r\n"
            "s=Atoll IS-11 demo sender - ST 2110-20\r\n"
            "t=0 0\r\n"
            f"m=video {dst_port} RTP/AVP 96\r\n"
            f"c=IN IP4 {dst_ip}/64\r\n"
            f"a=source-filter: incl IN IP4 {dst_ip} {src_ip}\r\n"
            "a=rtpmap:96 raw/90000\r\n"
            "a=fmtp:96 sampling=YCbCr-4:2:2; width=1920; height=1080; exactframerate=25; depth=8; interlace; "
            "TCS=SDR; colorimetry=BT709; PM=2110GPM; SSN=ST2110-20:2017; TP=2110TPW\r\n"
            "a=mediaclk:direct=0\r\n" + REFCLK)

# ---- IS-04 resources ---------------------------------------------------------------------------
def _node():
    return {"id": NODE_ID, "version": _rv("node"), "label": "atoll-is11", "description": "Atoll IS-11 stream-compat demo",
            "tags": {}, "href": f"http://{ADV}:{PORT}/", "hostname": "atoll-is11", "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADV, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
def _device():
    return {"id": DEV_ID, "version": _rv("device"), "label": "atoll-is11", "description": "IS-11 device", "tags": {},
            "type": "urn:x-nmos:device:generic", "node_id": NODE_ID, "senders": [SEND_ID], "receivers": [RECV_ID],
            "controls": [{"href": IS05_BASE, "type": "urn:x-nmos:control:sr-ctrl/v1.1"},
                         {"href": IS11_BASE, "type": "urn:x-nmos:control:stream-compat/v1.0"}]}
def _source():
    return {"id": SRC_ID, "version": _rv("source"), "label": "IS-11 source", "description": "IS-11 demo source", "tags": {},
            "caps": {}, "device_id": DEV_ID, "parents": [], "clock_name": None, "format": "urn:x-nmos:format:video"}
def _flow():
    return {"id": FLOW_ID, "version": _rv("flow"), "label": "IS-11 flow", "description": "IS-11 demo flow", "tags": {},
            "source_id": SRC_ID, "device_id": DEV_ID, "parents": [], "format": "urn:x-nmos:format:video",
            "media_type": "video/raw", "grain_rate": STATE["flow_grain_rate"],
            "frame_width": 1920, "frame_height": 1080, "colorspace": "BT709", "interlace_mode": "interlaced_tff",
            "transfer_characteristic": "SDR",
            "components": [{"name": "Y", "width": 1920, "height": 1080, "bit_depth": 8},
                           {"name": "Cb", "width": 960, "height": 1080, "bit_depth": 8},
                           {"name": "Cr", "width": 960, "height": 1080, "bit_depth": 8}]}
def _sender():
    return {"id": SEND_ID, "version": _rv("sender"), "label": "IS-11 sender", "description": "IS-11 demo sender", "tags": {},
            "flow_id": FLOW_ID, "device_id": DEV_ID, "transport": "urn:x-nmos:transport:rtp.mcast",
            "interface_bindings": ["eth0"], "subscription": {"receiver_id": None, "active": STATE["sender_master_enable"]},
            "manifest_href": IS05_BASE + f"single/senders/{SEND_ID}/transportfile"}
def _receiver():
    return {"id": RECV_ID, "version": _rv("receiver"), "label": "IS-11 receiver", "description": "IS-11 demo receiver", "tags": {},
            "device_id": DEV_ID, "transport": "urn:x-nmos:transport:rtp.mcast", "interface_bindings": ["eth0"],
            "format": "urn:x-nmos:format:video",
            "caps": {"media_types": ["video/raw"]},
            "subscription": {"sender_id": STATE["receiver_sender_id"], "active": STATE["receiver_master_enable"]}}

def _resources():
    return [("node", _node()), ("device", _device()), ("source", _source()), ("flow", _flow()),
            ("sender", _sender()), ("receiver", _receiver())]

def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{kind} -> {e.code}: {e.read().decode(errors='replace')[:200]}")
_registered = {"ok": False}
def register_all():
    try:
        for kind, data in _resources(): _post(kind, data)
        _registered["ok"] = True
        print(f"  registered IS-11 node/device/sender/receiver with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- will retry", flush=True); return False
def heartbeat():
    while True:
        time.sleep(SYS.heartbeat_interval)
        if not _registered["ok"]:
            register_all(); continue
        try:
            urllib.request.urlopen(urllib.request.Request(f"{REG}/health/nodes/{NODE_ID}", data=b"", method="POST"), timeout=5).read()
        except urllib.error.HTTPError as e:
            if e.code == 404: _registered["ok"] = False
        except Exception:
            pass

# ---- IS-05 staged/active docs ------------------------------------------------------------------
import copy
def _sender_tp():
    return [{"source_ip": PC_IP, "destination_ip": GRP, "source_port": 5004, "destination_port": MPORT,
             "rtp_enabled": True}]
def _receiver_tp():
    return [{"source_ip": None, "multicast_ip": GRP, "interface_ip": PC_IP, "destination_port": MPORT,
             "rtp_enabled": True}]
def _blank_act(): return {"mode": None, "requested_time": None, "activation_time": None}
IS05 = {
    "sender": {"staged": {"master_enable": False, "receiver_id": None, "activation": _blank_act(),
                          "transport_params": _sender_tp()},
               "active": {"master_enable": False, "receiver_id": None, "activation": _blank_act(),
                          "transport_params": _sender_tp()}},
    "receiver": {"staged": {"master_enable": False, "sender_id": None, "transport_file": {"data": None, "type": None},
                            "activation": _blank_act(), "transport_params": _receiver_tp()},
                 "active": {"master_enable": False, "sender_id": None, "transport_file": {"data": None, "type": None},
                            "activation": _blank_act(), "transport_params": _receiver_tp()}},
}
# permissive constraints -- one object per leg, one entry per transport param
SENDER_CONSTRAINTS = [{"source_ip": {}, "destination_ip": {}, "source_port": {}, "destination_port": {}, "rtp_enabled": {}}]
RECEIVER_CONSTRAINTS = [{"source_ip": {}, "multicast_ip": {}, "interface_ip": {}, "destination_port": {}, "rtp_enabled": {}}]
# resolved values substituted for any "auto" leg value at activation time (IS-05 requires /active concrete)
_AUTO_SENDER = {"source_ip": PC_IP, "destination_ip": GRP, "source_port": 5004, "destination_port": MPORT, "rtp_enabled": True}
_AUTO_RECEIVER = {"source_ip": PC_IP, "multicast_ip": GRP, "interface_ip": PC_IP, "destination_port": MPORT, "rtp_enabled": True}
_STAGE_KEYS = {"sender": {"master_enable", "receiver_id", "activation", "transport_params"},
               "receiver": {"master_enable", "sender_id", "transport_file", "activation", "transport_params"}}
_ACT_MODES = {None, "activate_immediate", "activate_scheduled_relative", "activate_scheduled_absolute"}
def _stage_valid(kind, patch):
    """Reject a malformed staged PATCH (IS-05 wants 400, not a silent 200)."""
    if not isinstance(patch, dict): return False
    if any(k not in _STAGE_KEYS[kind] for k in patch): return False
    if "master_enable" in patch and not isinstance(patch["master_enable"], bool): return False
    if "transport_params" in patch and not isinstance(patch["transport_params"], list): return False
    if "transport_file" in patch and not isinstance(patch["transport_file"], dict): return False
    for idk in ("receiver_id", "sender_id"):
        if idk in patch and patch[idk] is not None and not isinstance(patch[idk], str): return False
    if "activation" in patch:
        a = patch["activation"]
        if not isinstance(a, dict) or a.get("mode") not in _ACT_MODES: return False
    return True
_is05_lock = threading.Lock()

def _dur_secs(t):
    try:
        s, ns = str(t).split(":"); return int(s) + int(ns) / 1e9
    except Exception:
        return 0.0

def _activate(kind):
    """staged -> active for a leg (resolving any "auto"), reset the staged activation, reflect into IS-11/IS-04."""
    st = IS05[kind]["staged"]
    act = copy.deepcopy(st["activation"]); act["activation_time"] = _tai(time.time() + 37)   # real TAI (UTC+37) so active >= requested
    active = copy.deepcopy(st); active["activation"] = act
    resolver = _AUTO_SENDER if kind == "sender" else _AUTO_RECEIVER
    for leg in active["transport_params"]:
        for k, v in list(leg.items()):
            if v == "auto": leg[k] = resolver.get(k)
    IS05[kind]["active"] = active
    IS05[kind]["staged"]["activation"] = _blank_act()      # the pending activation is consumed
    if kind == "sender":
        STATE["sender_master_enable"] = bool(st["master_enable"]); _bump("sender")
    else:
        STATE["receiver_master_enable"] = bool(st["master_enable"]); STATE["receiver_sender_id"] = st.get("sender_id"); _bump("receiver")

def _patch_staged(kind, patch):
    """Merge an IS-05 PATCH into the staged endpoint and honour its activation; returns the staged doc."""
    with _is05_lock:
        s = IS05[kind]["staged"]
        if "master_enable" in patch: s["master_enable"] = bool(patch["master_enable"])
        if kind == "sender" and "receiver_id" in patch: s["receiver_id"] = patch["receiver_id"]
        if kind == "receiver":
            if "sender_id" in patch: s["sender_id"] = patch["sender_id"]
            if "transport_file" in patch: s["transport_file"] = patch["transport_file"]
        if isinstance(patch.get("transport_params"), list):
            for i, leg in enumerate(patch["transport_params"]):
                if i < len(s["transport_params"]) and isinstance(leg, dict):
                    s["transport_params"][i].update(leg)
        act = patch.get("activation") or {}
        mode = act.get("mode")
        s["activation"] = {"mode": mode, "requested_time": act.get("requested_time"), "activation_time": None}
        code = 200; resp = None
        if mode == "activate_immediate":
            _activate(kind)                                  # resets staged.activation, sets active
            resp = copy.deepcopy(IS05[kind]["staged"])       # response: staged params ...
            resp["activation"] = copy.deepcopy(IS05[kind]["active"]["activation"])   # ... reporting what just happened
        elif mode == "activate_scheduled_relative":
            threading.Timer(max(0.0, _dur_secs(act.get("requested_time"))), _activate, args=(kind,)).start()
            s["activation"]["activation_time"] = act.get("requested_time"); code = 202
        elif mode == "activate_scheduled_absolute":
            delay = max(0.0, _dur_secs(act.get("requested_time")) - (time.time() + 37))   # requested_time is absolute TAI (UTC+37)
            threading.Timer(delay, _activate, args=(kind,)).start()
            s["activation"]["activation_time"] = act.get("requested_time"); code = 202
        return (resp if resp is not None else s), code

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(bytes(body))
    def _err(self, code, msg):
        self._send(code, {"code": code, "error": msg, "debug": None})
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_OPTIONS(self):
        self.send_response(200)                         # NMOS CORS preflight expects 200, not 204
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, DELETE, PATCH, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  self.route("GET")
    def do_PUT(self):  self.route("PUT")
    def do_DELETE(self): self.route("DELETE")
    def do_PATCH(self): self.route("PATCH")
    def do_POST(self): self.route("POST")

    def route(self, m):
        p = urlparse(self.path).path
        seg = [s for s in p.split("/") if s != ""]
        try:
            if p in ("/", "/x-nmos", "/x-nmos/"):
                return self._send(200, ["streamcompatibility/", "connection/", "node/"])
            if seg[:2] == ["x-nmos", "streamcompatibility"]:
                return self.is11(m, seg[2:])   # seg[2]=v1.0, seg[3:]=...
            if seg[:2] == ["x-nmos", "connection"]:
                return self.is05(m, seg[2:])
            if seg[:2] == ["x-nmos", "node"]:
                return self.is04(m, seg[2:])
            return self._err(404, "Not Found")
        except (IndexError, ValueError):
            return self._err(404, "Not Found")

    # ---------------- IS-11 ----------------
    def is11(self, m, seg):
        # seg[0] = "v1.0"
        if not seg: return self._send(200, ["v1.0/"])
        rest = seg[1:]
        if not rest:
            return self._send(200, ["inputs/", "outputs/", "senders/", "receivers/"])
        r0 = rest[0]
        if r0 == "senders":
            if len(rest) == 1: return self._send(200, [SEND_ID + "/"])
            if rest[1] != SEND_ID: return self._err(404, "sender not found")
            sub = rest[2:]
            if not sub: return self._send(200, ["constraints/", "inputs/", "status/"])
            if sub == ["status"]: return self._send(200, sender_status())
            if sub == ["inputs"]: return self._send(200, [INPUT_ID])   # uuid-list: bare UUIDs
            if sub[0] == "constraints":
                if len(sub) == 1: return self._send(200, ["active/", "supported/"])
                if sub[1] == "supported": return self._send(200, {"parameter_constraints": SUPPORTED_PARAMS})
                if sub[1] == "active":
                    if m == "GET": return self._send(200, STATE["sender_active_constraints"])
                    if m == "PUT":
                        try: doc = json.loads(self._body() or b"{}")
                        except Exception: return self._err(400, "invalid JSON")
                        if not isinstance(doc.get("constraint_sets"), list):
                            return self._err(400, "constraint_sets (array) required")
                        allowed = set(SUPPORTED_PARAMS)     # reject params not in supportedConstraints
                        for cs in doc["constraint_sets"]:
                            if not isinstance(cs, dict):
                                return self._err(400, "constraint_set must be an object")
                            for kkey in cs:
                                if kkey not in allowed:
                                    return self._err(400, f"unsupported parameter constraint: {kkey}")
                        STATE["sender_active_constraints"] = {"constraint_sets": doc["constraint_sets"]}; _bump("sender"); _bump("input")
                        for cs in doc["constraint_sets"]:      # a grain_rate constraint retunes the flow
                            gr = cs.get("urn:x-nmos:cap:format:grain_rate")
                            if isinstance(gr, dict) and gr.get("enum"):
                                STATE["flow_grain_rate"] = gr["enum"][0]; _bump("flow")
                        return self._send(200, STATE["sender_active_constraints"])
                    if m == "DELETE":
                        STATE["sender_active_constraints"] = {"constraint_sets": []}; _bump("sender"); _bump("input")
                        STATE["flow_grain_rate"] = {"numerator": 25, "denominator": 1}; _bump("flow")
                        return self._send(200, STATE["sender_active_constraints"])
            return self._err(404, "Not Found")
        if r0 == "receivers":
            if len(rest) == 1: return self._send(200, [RECV_ID + "/"])
            if rest[1] != RECV_ID: return self._err(404, "receiver not found")
            sub = rest[2:]
            if not sub: return self._send(200, ["outputs/", "status/"])
            if sub == ["status"]: return self._send(200, receiver_status())
            if sub == ["outputs"]: return self._send(200, [OUTPUT_ID])   # uuid-list: bare UUIDs
            return self._err(404, "Not Found")
        if r0 == "inputs":
            if len(rest) == 1: return self._send(200, [INPUT_ID + "/"])
            if rest[1] != INPUT_ID: return self._err(404, "input not found")
            sub = rest[2:]
            if not sub: return self._send(200, ["edid/", "properties/"])
            if sub == ["properties"]:
                return self._send(200, {"id": INPUT_ID, "version": _rv("input"), "label": "IS-11 input",
                                        "description": "EDID-capable input", "tags": {}, "device_id": DEV_ID,
                                        "edid_support": True, "base_edid_support": True, "adjust_to_caps": True,
                                        "connected": True, "status": {"state": "signal_present"}})
            if sub[0] == "edid":
                if len(sub) == 1: return self._send(200, ["base/", "effective/"])
                if sub[1] == "effective":
                    if m == "GET": return self._send(200, effective_edid(), "application/octet-stream")
                    return self._err(405, "Method Not Allowed")
                if sub[1] == "base":
                    if m == "GET":
                        if STATE["input_base_edid"] is None: return self._send(204, b"", "application/octet-stream")
                        return self._send(200, STATE["input_base_edid"], "application/octet-stream")
                    if m == "PUT":
                        b = self._body()
                        if not edid_valid(b): return self._err(400, "invalid EDID")
                        STATE["input_base_edid"] = b; _bump("input"); _bump("sender")
                        return self._send(204, b"")
                    if m == "DELETE":
                        STATE["input_base_edid"] = None; _bump("input"); _bump("sender")
                        return self._send(204, b"")
            return self._err(404, "Not Found")
        if r0 == "outputs":
            if len(rest) == 1: return self._send(200, [OUTPUT_ID + "/"])
            if rest[1] != OUTPUT_ID: return self._err(404, "output not found")
            sub = rest[2:]
            if not sub: return self._send(200, ["edid/", "properties/"])
            if sub == ["properties"]:
                return self._send(200, {"id": OUTPUT_ID, "version": _rv("output"), "label": "IS-11 output",
                                        "description": "EDID-capable output", "tags": {}, "device_id": DEV_ID,
                                        "edid_support": True, "connected": True, "status": {"state": "signal_present"}})
            if sub[0] == "edid":
                if m == "GET": return self._send(200, DEFAULT_EDID, "application/octet-stream")
                return self._err(405, "Method Not Allowed")
            return self._err(404, "Not Found")
        return self._err(404, "Not Found")

    # ---------------- IS-04 Node API (self-hosted) ----------------
    def is04(self, m, seg):
        if not seg: return self._send(200, ["v1.3/"])
        rest = seg[1:]
        if not rest: return self._send(200, ["self/", "devices/", "sources/", "flows/", "senders/", "receivers/"])
        r0 = rest[0]
        if r0 == "self":
            return self._send(200, _node())
        singles = {"devices": (DEV_ID, _device), "sources": (SRC_ID, _source), "flows": (FLOW_ID, _flow),
                   "senders": (SEND_ID, _sender), "receivers": (RECV_ID, _receiver)}
        if r0 in singles:
            rid, fn = singles[r0]
            if len(rest) == 1: return self._send(200, [fn()])
            if rest[1] == rid: return self._send(200, fn())
            return self._err(404, "Not Found")
        return self._err(404, "Not Found")

    # ---------------- IS-05 Connection API ----------------
    def is05(self, m, seg):
        if not seg: return self._send(200, ["v1.1/"])
        rest = seg[1:]
        if not rest: return self._send(200, ["bulk/", "single/"])
        if rest[0] == "bulk":
            if len(rest) == 1: return self._send(200, ["senders/", "receivers/"])
            if rest[1] in ("senders", "receivers"):
                if m != "POST": return self._err(405, "Method Not Allowed")   # GET on bulk is 405; changes come by POST
                kind = "sender" if rest[1] == "senders" else "receiver"
                rid = SEND_ID if kind == "sender" else RECV_ID
                try: items = json.loads(self._body() or b"[]")
                except Exception: return self._err(400, "invalid JSON")
                if not isinstance(items, list): return self._err(400, "expected an array")
                out = []
                for it in items:
                    iid = (it or {}).get("id"); params = (it or {}).get("params") or {}
                    if iid != rid:
                        out.append({"id": iid, "code": 404, "error": "resource not found", "debug": None})
                    elif not _stage_valid(kind, params):
                        out.append({"id": iid, "code": 400, "error": "invalid transport parameters", "debug": None})
                    else:
                        _staged, code = _patch_staged(kind, params); out.append({"id": iid, "code": code})
                return self._send(200, out)
            return self._err(404, "Not Found")
        if rest[0] == "single" and len(rest) == 1: return self._send(200, ["senders/", "receivers/"])
        if rest[:2] == ["single", "senders"]:
            if len(rest) == 2: return self._send(200, [SEND_ID + "/"])
            if rest[2] != SEND_ID: return self._err(404, "sender not found")
            sub = rest[3:]
            if not sub: return self._send(200, ["constraints/", "staged/", "active/", "transporttype/", "transportfile/"])
            if sub == ["constraints"]: return self._send(200, SENDER_CONSTRAINTS)
            if sub == ["transporttype"]: return self._send(200, "urn:x-nmos:transport:rtp")
            if sub == ["active"]: return self._send(200, IS05["sender"]["active"])
            if sub == ["staged"]:
                if m == "GET": return self._send(200, IS05["sender"]["staged"])
                if m == "PATCH":
                    try: doc = json.loads(self._body() or b"{}")
                    except Exception: return self._err(400, "invalid JSON")
                    if not _stage_valid("sender", doc): return self._err(400, "invalid transport parameters")
                    staged, code = _patch_staged("sender", doc)
                    return self._send(code, staged)
            if sub == ["transportfile"]:
                return self._send(200, _sdp().encode(), "application/sdp")
            return self._err(404, "Not Found")
        if rest[:2] == ["single", "receivers"]:
            if len(rest) == 2: return self._send(200, [RECV_ID + "/"])
            if rest[2] != RECV_ID: return self._err(404, "receiver not found")
            sub = rest[3:]
            if not sub: return self._send(200, ["constraints/", "staged/", "active/", "transporttype/"])
            if sub == ["constraints"]: return self._send(200, RECEIVER_CONSTRAINTS)
            if sub == ["transporttype"]: return self._send(200, "urn:x-nmos:transport:rtp")
            if sub == ["active"]: return self._send(200, IS05["receiver"]["active"])
            if sub == ["staged"]:
                if m == "GET": return self._send(200, IS05["receiver"]["staged"])
                if m == "PATCH":
                    try: doc = json.loads(self._body() or b"{}")
                    except Exception: return self._err(400, "invalid JSON")
                    if not _stage_valid("receiver", doc): return self._err(400, "invalid transport parameters")
                    staged, code = _patch_staged("receiver", doc)
                    return self._send(code, staged)
            return self._err(404, "Not Found")
        return self._err(404, "Not Found")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    try:                                                # advertise over mDNS/DNS-SD (IS-04 peer-to-peer)
        from mdns_responder import MdnsResponder
        MdnsResponder(ADV, PORT, instance="atoll-is11", txt={
            "api_proto": "http", "api_ver": "v1.3", "api_auth": "false",
            "ver_slf": "0", "ver_src": "0", "ver_flw": "0", "ver_dvc": "0", "ver_snd": "0", "ver_rcv": "0",
        }).start()
        print("mdns: advertising _nmos-node._tcp for atoll-is11", flush=True)
    except Exception as e:
        print(f"mdns: disabled ({e})", flush=True)
    print(f"IS-11 stream-compat: node {NODE_ID} on http://0.0.0.0:{PORT}  (IS-11 {IS11_BASE})", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
