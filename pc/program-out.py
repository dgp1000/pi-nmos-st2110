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

Receiver-side IS-05, three ways a controller can drive it:
  * **transport_params** -- PATCH a multicast_ip + destination_port directly.
  * **sender_id** -- PATCH the id of a discovered NMOS sender; we look it up in the registry Query
    API, fetch its SDP (manifest_href), parse the multicast/port, and route to it. Canonical NMOS
    "connect this receiver to that sender".
  * **scheduled activation** -- activate_immediate, activate_scheduled_relative ("in N seconds") and
    activate_scheduled_absolute (at a TAI time); a timer fires the staged->active move on schedule.
On activation we also update the receiver's IS-04 `subscription` (sender_id + active) and re-register,
so an external controller/registry sees the connection -- genuinely two-way. The essence/codec is
derived from WHICH known flow the (address, port) belongs to (CATALOG below).
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
QUERY = f"{REGISTRY}/x-nmos/query/v1.3"

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
_lock = threading.RLock()
_pending = {"timer": None}      # a scheduled activation waiting to fire
_SUB = (False, None)           # last (active, sender_id) pushed to IS-04, so we only re-register on change

def _tai(when):
    return f"{int(when) + 37}:{int((when % 1) * 1e9):09d}"

def _dur_to_secs(v):
    """IS-05 'sec:nsec' duration (activate_scheduled_relative requested_time) -> float seconds."""
    try:
        if v is None: return 0.0
        if isinstance(v, (int, float)): return float(v)
        sec, _, nsec = str(v).partition(":")
        return int(sec or 0) + int(nsec or 0) / 1e9
    except Exception:
        return 0.0

def _tai_to_unix(v):
    """IS-05 TAI 'sec:nsec' absolute time -> unix seconds (TAI is UTC + 37 leap seconds)."""
    try:
        sec, _, nsec = str(v).partition(":")
        return (int(sec or 0) - 37) + int(nsec or 0) / 1e9
    except Exception:
        return time.time()

def _resolve_sender(sender_id):
    """IS-05 sender_id routing: look the sender up in the registry Query API, fetch its SDP
    (manifest_href), and return (multicast_ip, port). Canonical 'connect receiver to sender'."""
    try:
        with urllib.request.urlopen(f"{QUERY}/senders/{sender_id}", timeout=4) as r:
            snd = json.load(r)
        mh = snd.get("manifest_href")
        if not mh:
            print(f"  sender {sender_id[:8]} has no manifest_href -- cannot resolve transport", flush=True)
            return None
        with urllib.request.urlopen(mh, timeout=4) as r:
            sdp = r.read().decode("utf-8", "replace")
        ip = port = None
        for ln in sdp.splitlines():
            ln = ln.strip()
            if ln.startswith("c=IN IP4"):
                ip = ln.split()[2].split("/")[0]
            elif ln.startswith("m="):
                parts = ln.split()
                if len(parts) >= 2:
                    port = parts[1]
        if ip and port:
            print(f"  resolved sender {sender_id[:8]} -> {ip}:{port} (via SDP)", flush=True)
            return (ip, port)
    except Exception as e:
        print(f"  sender_id resolve failed for {sender_id}: {e}", flush=True)
    return None

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

def _update_subscription():
    """Reflect the active connection into the IS-04 receiver's `subscription` and re-register (bump
    version) so a controller/registry sees this receiver is connected -- the two-way half of IS-05."""
    global _SUB
    active = bool(STATE["active"]["master_enable"])
    sid = STATE["active"]["sender_id"] if active else None
    if (active, sid) == _SUB:
        return
    _SUB = (active, sid)
    try:
        _post("receiver", _receiver_resource())
        print(f"  IS-04 subscription -> active={active} sender={ (sid[:8] if sid else None) }", flush=True)
    except Exception as e:
        print(f"  subscription re-register failed: {e}", flush=True)

def _apply_activation(staged, activation_time=None):
    """Move staged -> active, fill activation_time, resolve essence, push the IS-04 subscription."""
    act = dict(staged.get("activation") or {})
    act["activation_time"] = activation_time or _tai(time.time())
    STATE["active"] = {
        "master_enable": bool(staged.get("master_enable")),
        "sender_id": staged.get("sender_id"),
        "activation": act,
        "transport_params": staged.get("transport_params") or _blank_params(),
    }
    _resolve_and_write()
    _update_subscription()

def _cancel_pending():
    t = _pending.get("timer")
    if t is not None:
        t.cancel()
        _pending["timer"] = None

def _schedule_activation(fire_at_unix):
    """Arm a timer to fire the staged->active move at fire_at_unix (a scheduled IS-05 activation)."""
    _cancel_pending()
    delay = max(0.0, fire_at_unix - time.time())
    def _fire():
        with _lock:
            _pending["timer"] = None
            at = STATE["staged"]["activation"].get("activation_time")
            _apply_activation(STATE["staged"], at)
    t = threading.Timer(delay, _fire)
    t.daemon = True
    _pending["timer"] = t
    t.start()

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
                pend = STATE["staged"]["activation"] if _pending.get("timer") is not None else None
                return self._send(200, {"receiver_id": RX_ID, "master_enable": STATE["active"]["master_enable"],
                                        "essence": STATE["essence"], "label": STATE["label"],
                                        "sender_id": STATE["active"]["sender_id"],
                                        "transport_params": STATE["active"]["transport_params"],
                                        "pending": pend, "catalog": CATALOG_BY_ESSENCE})
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
            patch_has_mcast = False
            if "transport_params" in patch:
                s["transport_params"] = _norm_params(patch["transport_params"])
                patch_has_mcast = bool(s["transport_params"][0].get("multicast_ip"))
                # routing by raw transport without naming a sender clears any prior sender association
                if patch_has_mcast and "sender_id" not in patch:
                    s["sender_id"] = None
            # IS-05 sender_id routing: a sender is named in THIS patch and no multicast is given in
            # THIS patch -> resolve the sender's SDP for its transport (ignoring any stale staged tp).
            if patch.get("sender_id") and not patch_has_mcast:
                res = _resolve_sender(patch["sender_id"])
                if res:
                    s["transport_params"] = _blank_params()
                    s["transport_params"][0]["multicast_ip"] = res[0]
                    s["transport_params"][0]["destination_port"] = res[1]
                else:
                    return self._send(400, {"code": 400, "error": "Bad Request",
                                            "debug": f"could not resolve sender_id {patch['sender_id']}"})
            act = patch.get("activation") or {}
            mode = act.get("mode")
            req_t = act.get("requested_time")
            s["activation"] = {"mode": mode, "requested_time": req_t, "activation_time": None}
            _cancel_pending()   # any new PATCH supersedes a pending scheduled activation
            if mode == "activate_immediate":
                _apply_activation(s)
                s["activation"]["activation_time"] = STATE["active"]["activation"]["activation_time"]
            elif mode == "activate_scheduled_relative":
                fire = time.time() + _dur_to_secs(req_t)
                s["activation"]["activation_time"] = _tai(fire)
                _schedule_activation(fire)
            elif mode == "activate_scheduled_absolute":
                fire = _tai_to_unix(req_t)
                s["activation"]["activation_time"] = req_t
                _schedule_activation(fire)
            # mode None -> stage only (and any pending scheduled activation was cancelled above)
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
def _receiver_resource():
    return {"id": RX_ID, "version": _ver(), "label": "Program Out",
            "description": "Route any island flow here over IS-05", "tags": {},
            "device_id": DEVICE_ID, "transport": "urn:x-nmos:transport:rtp",
            "interface_bindings": [], "format": "urn:x-nmos:format:video",
            "caps": {"media_types": sorted({f[3] for f in _FLOWS})},
            "subscription": {"sender_id": STATE["active"]["sender_id"] if STATE["active"]["master_enable"] else None,
                             "active": bool(STATE["active"]["master_enable"])}}
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
    return [("node", node), ("device", device), ("receiver", _receiver_resource())]
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
