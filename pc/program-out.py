#!/usr/bin/env python3
"""Atoll "Program Out" -- a software NMOS receiver you can route any island flow to over IS-05.

The rest of the rig renders by pulling a fixed multicast per source. This adds a REAL receiver-side
IS-05 Connection API: a controller (our panel, or any NMOS controller) PATCHes this receiver's
/staged with the transport parameters of the flow it wants -- a multicast address and port -- and
activates. On activation we recognise which island flow that (address, port) is, write the choice to
~/atoll-run/programout, and the `program` layout in output-render.sh renders it fullscreen. So an
IS-05 connection actually drives the picture, which is the point.

Two-way NMOS: this receiver registers in IS-04 (node/device/receiver + heartbeat) so it is
discoverable, and serves the IS-05 v1.1 Connection API for a single receiver on PROGRAMOUT_PORT.
The essence/codec is derived from WHICH known flow the (address, port) belongs to (CATALOG below),
so a plain transport_params PATCH is enough -- no non-standard hints required.
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ATOLL_RUN", "NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "PROGRAMOUT_PORT",
        "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT", "MUSIC_GRP", "MUSIC_PORT",
        "H264_GRP", "H264_PORT", "MJPEG_GRP", "MJPEG_PORT", "VP9_GRP", "VP9_PORT",
        "J2K_GRP", "J2K_PORT", "TSRTP_GRP", "TSRTP_PORT", "PI_RAW_GRP", "PI_RAW_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
RUN = CFG.get("ATOLL_RUN") or "/home/david/atoll-run"
PORT = int(CFG.get("PROGRAMOUT_PORT") or 8092)
KNOB = os.path.join(RUN, "programout")

# Routable island flows: (multicast, port) -> (essence key output-render knows, label, media_type).
# The essence is how the renderer decodes it; deriving it from the wire address means a plain IS-05
# transport_params PATCH (multicast_ip + destination_port) is a complete, standards-clean connection.
def _grp(k):
    return (CFG.get(f"{k}_GRP") or "").strip(), (CFG.get(f"{k}_PORT") or "").strip()
CATALOG = {}   # (ip, port) -> dict
_FLOWS = [
    ("hevc",  "HEVC",  "Live TV",         "video/MP2T"),
    ("jxs",   "HOME",  "Home videos",     "video/MP2T"),
    ("music", "MUSIC", "Music",           "video/MP2T"),
    ("h264",  "H264",  "H.264 RTP",       "video/H264"),
    ("mjpeg", "MJPEG", "MJPEG RTP",       "video/jpeg"),
    ("vp9",   "VP9",   "VP9 RTP",         "video/VP9"),
    ("j2k",   "J2K",   "JPEG 2000",       "video/jpeg2000"),
    ("tsrtp", "TSRTP", "TS over RTP",     "video/MP2T"),
    ("raw",   "PI_RAW","Pi raw 2110-20",  "video/raw"),
]
CATALOG_BY_ESSENCE = {}
for essence, cfgkey, label, mtype in _FLOWS:
    ip, port = _grp(cfgkey)
    if ip and port:
        CATALOG[(ip, port)] = {"essence": essence, "label": label, "media_type": mtype}
        CATALOG_BY_ESSENCE[essence] = {"ip": ip, "port": port, "label": label, "media_type": mtype}

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
NODE_ID = str(uuid.uuid5(NS, "atoll:programout:node"))
DEVICE_ID = str(uuid.uuid5(NS, "atoll:programout:device"))
RX_ID = str(uuid.uuid5(NS, "atoll:programout:receiver"))
ADVERTISE_HOST = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI   # IS-09 System API client
SYS = SystemAPI(REGISTRY)   # IS-09: discover + honour the System API (heartbeat interval, ptp)
REG = f"{REGISTRY}/x-nmos/registration/v1.3"

# ---- IS-05 connection state ----------------------------------------------------------------------
def _blank_params():
    return [{"source_ip": None, "multicast_ip": None, "interface_ip": "auto",
             "destination_port": "auto", "rtp_enabled": True}]
STATE = {
    "active":  {"master_enable": False, "sender_id": None, "activation": {"mode": None, "requested_time": None, "activation_time": None},
                "transport_params": _blank_params()},
    "staged":  {"master_enable": False, "sender_id": None, "activation": {"mode": None, "requested_time": None, "activation_time": None},
                "transport_params": _blank_params()},
    "essence": None, "label": None,   # what the active params resolve to (for the renderer + readout)
}
_lock = threading.Lock()

def _tai(when):
    return f"{int(when) + 37}:{int((when % 1) * 1e9):09d}"

def _resolve_and_write():
    """Derive the essence from the active transport_params and publish the knob the renderer reads."""
    tp = (STATE["active"]["transport_params"] or [{}])[0]
    ip = tp.get("multicast_ip"); port = tp.get("destination_port")
    port = str(port) if port not in (None, "auto") else ""
    hit = CATALOG.get((str(ip), port)) if ip else None
    if STATE["active"]["master_enable"] and hit:
        STATE["essence"], STATE["label"] = hit["essence"], hit["label"]
        line = f"{hit['essence']} {ip} {port} {STATE['active'].get('sender_id') or '-'}\n"
    else:
        STATE["essence"], STATE["label"] = (None, None)
        line = "none - - -\n"
    tmp = KNOB + ".tmp"
    with open(tmp, "w") as f:
        f.write(line)
    os.replace(tmp, KNOB)
    print(f"{time.strftime('%T')} program-out -> {line.strip()}", flush=True)

def _apply_activation(staged):
    """Move staged -> active on an immediate activation, filling activation_time."""
    now = time.time()
    act = dict(staged.get("activation") or {})
    act["activation_time"] = _tai(now)
    STATE["active"] = {
        "master_enable": bool(staged.get("master_enable")),
        "sender_id": staged.get("sender_id"),
        "activation": act,
        "transport_params": staged.get("transport_params") or _blank_params(),
    }
    _resolve_and_write()

# ---- HTTP: IS-05 Connection API (single receiver) ------------------------------------------------
CONN_BASE = "/x-nmos/connection/v1.1"
def _norm_params(params):
    out = []
    for p in (params or []):
        q = dict(_blank_params()[0]); q.update({k: v for k, v in p.items() if v is not None})
        out.append(q)
    return out or _blank_params()

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
            if p == "/x-nmos": return self._send(200, ["connection/"])
            if p == "/x-nmos/connection": return self._send(200, ["v1.1/"])
            if p == CONN_BASE: return self._send(200, ["single/"])
            if p == f"{CONN_BASE}/single": return self._send(200, ["receivers/"])
            if p == f"{CONN_BASE}/single/receivers": return self._send(200, [f"{RX_ID}/"])
            if p == f"{CONN_BASE}/single/receivers/{RX_ID}":
                return self._send(200, ["constraints/", "staged/", "active/", "transporttype/"])
            if p == f"{CONN_BASE}/single/receivers/{RX_ID}/active":
                return self._send(200, STATE["active"])
            if p == f"{CONN_BASE}/single/receivers/{RX_ID}/staged":
                return self._send(200, STATE["staged"])
            if p == f"{CONN_BASE}/single/receivers/{RX_ID}/constraints":
                return self._send(200, [{"source_ip": {}, "multicast_ip": {}, "interface_ip": {},
                                         "destination_port": {}, "rtp_enabled": {}}])
            if p == f"{CONN_BASE}/single/receivers/{RX_ID}/transporttype":
                return self._send(200, "urn:x-nmos:transport:rtp")
            if p == "/programout":   # convenience readout for the panel
                return self._send(200, {"receiver_id": RX_ID, "master_enable": STATE["active"]["master_enable"],
                                        "essence": STATE["essence"], "label": STATE["label"],
                                        "sender_id": STATE["active"]["sender_id"],
                                        "transport_params": STATE["active"]["transport_params"],
                                        "catalog": CATALOG_BY_ESSENCE})
        self._send(404, {"code": 404, "error": "Not Found", "debug": p})

    def do_PATCH(self):
        p = urlparse(self.path).path.rstrip("/")
        if p != f"{CONN_BASE}/single/receivers/{RX_ID}/staged":
            return self._send(404, {"code": 404, "error": "Not Found", "debug": p})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            patch = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"code": 400, "error": "Bad Request", "debug": str(e)})
        with _lock:
            s = STATE["staged"]
            if "master_enable" in patch: s["master_enable"] = bool(patch["master_enable"])
            if "sender_id" in patch: s["sender_id"] = patch["sender_id"]
            if "transport_params" in patch: s["transport_params"] = _norm_params(patch["transport_params"])
            act = patch.get("activation") or {}
            mode = act.get("mode")
            s["activation"] = {"mode": mode, "requested_time": act.get("requested_time"), "activation_time": None}
            if mode == "activate_immediate":
                _apply_activation(s)
                s["activation"]["activation_time"] = STATE["active"]["activation"]["activation_time"]
            return self._send(200, s)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

# ---- IS-04 registration (node / device / receiver + heartbeat), mirrors is07-tally.py ------------
def _ver(): return _tai(time.time())
def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status
def _resources():
    node = {"id": NODE_ID, "version": _ver(), "label": "atoll-program-out",
            "description": "Atoll Program Out -- routable software receiver", "tags": {},
            "href": f"http://{ADVERTISE_HOST}:{PORT}/", "hostname": "atoll-programout",
            "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADVERTISE_HOST, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
    device = {"id": DEVICE_ID, "version": _ver(), "label": "atoll-program-out", "description": "Program Out",
              "tags": {}, "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
              "senders": [], "receivers": [RX_ID],
              "controls": [{"href": f"http://{ADVERTISE_HOST}:{PORT}{CONN_BASE}",
                            "type": "urn:x-nmos:control:sr-ctrl/v1.1", "authorization": False}]}
    receiver = {"id": RX_ID, "version": _ver(), "label": "Program Out",
                "description": "Route any island flow here over IS-05", "tags": {},
                "device_id": DEVICE_ID, "transport": "urn:x-nmos:transport:rtp",
                "interface_bindings": [], "format": "urn:x-nmos:format:video",
                "caps": {"media_types": sorted({f[3] for f in _FLOWS})},
                "subscription": {"sender_id": None, "active": False}}
    return [("node", node), ("device", device), ("receiver", receiver)]
_registered = {"ok": False}
def register_all():
    try:
        for kind, data in _resources(): _post(kind, data)
        _registered["ok"] = True
        print(f"  registered Program Out with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- IS-05 API still serves locally", flush=True); return False
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
    if not os.path.exists(KNOB):
        with open(KNOB, "w") as f: f.write("none - - -\n")
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"Program Out: IS-05 receiver {RX_ID} on http://0.0.0.0:{PORT}{CONN_BASE}/single/receivers/{RX_ID}", flush=True)
    print(f"  routable flows: {', '.join(sorted(CATALOG_BY_ESSENCE))}", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
