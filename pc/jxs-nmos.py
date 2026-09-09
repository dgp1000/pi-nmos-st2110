#!/usr/bin/env python3
"""Atoll JPEG XS NMOS -- IS-04 registrar + SDP server for the TRUE ST 2110-22 stream (AMWA
BCP-006-01 / RFC 9134 `video/jxsv`) emitted by `jxs-rtp-send.py`.

Registers a node/device/source/flow/sender in IS-04 and serves a standards-complete manifest so a
controller sees a conformant JPEG XS sender. The SDP is the point: `a=rtpmap:<pt> jxsv/90000` plus
the RFC 9134 `a=fmtp` carrying every BCP-006-01 MUST parameter (packetmode, transmode, profile,
level, sublevel, sampling, depth, width, height, exactframerate, colorimetry, TCS), the ST 2110-22
`b=AS` bandwidth, and the RFC 7273 clock lines (`a=ts-refclk:ptp=...`, `a=mediaclk:direct=0`). The
IS-04 Flow carries `media_type=video/jxsv` with components + profile/level/sublevel + bit_rate, as
BCP-006-01 requires. Geometry/descriptors come from atoll.conf, shared with the sender so the
manifest can never contradict the wire. Mirrors pi-nmos.py (register, serve, heartbeat).
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess, math
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "JXS_NMOS_PORT", "ISLAND_PC_IP", "PTP_GMID",
        "JXSV_GRP", "JXSV_PORT", "JXS_W", "JXS_H", "JXS_FPS", "JXS_BPP", "JXS_SAMPLING", "JXS_DEPTH",
        "JXS_COLORIMETRY", "JXS_TCS", "JXS_PROFILE", "JXS_LEVEL", "JXS_SUBLEVEL", "JXS_PT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)

PORT      = int(CFG.get("JXS_NMOS_PORT") or 8097)
ADVERTISE = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY  = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI      # IS-09 System API client (heartbeat interval)
SYS = SystemAPI(REGISTRY)
REG       = f"{REGISTRY}/x-nmos/registration/v1.3"
PC_IP     = (CFG.get("ISLAND_PC_IP") or "10.10.10.2").strip()
GMID      = (CFG.get("PTP_GMID") or "").strip()
GRP, MPORT = (CFG.get("JXSV_GRP") or "239.10.10.61").strip(), (CFG.get("JXSV_PORT") or "5032").strip()
PT        = int(CFG.get("JXS_PT") or 112)

W, H  = int(CFG.get("JXS_W") or 1280), int(CFG.get("JXS_H") or 720)
BPP   = float(CFG.get("JXS_BPP") or 2)
SAMP  = (CFG.get("JXS_SAMPLING") or "YCbCr-4:2:2").strip()
DEPTH = int(CFG.get("JXS_DEPTH") or 8)
COLOR = (CFG.get("JXS_COLORIMETRY") or "BT709").strip()
TCS   = (CFG.get("JXS_TCS") or "SDR").strip()
PROFILE, LEVEL, SUBLEVEL = (CFG.get("JXS_PROFILE") or "Main422.10").strip(), (CFG.get("JXS_LEVEL") or "2k-1").strip(), (CFG.get("JXS_SUBLEVEL") or "Sublev3bpp").strip()
_fn, _fd = (str(CFG.get("JXS_FPS") or "60").split("/") + ["1"])[:2]
RATE_N, RATE_D = int(_fn), int(_fd)
EXACTFR = str(RATE_N) if RATE_D == 1 else f"{RATE_N}/{RATE_D}"

FPS_VAL = RATE_N / RATE_D
CS_BPS  = BPP * W * H * FPS_VAL                       # JPEG XS codestream bit rate
FLOW_KBPS   = math.ceil(CS_BPS / 1000)               # Flow.bit_rate (codestream, kbit/s)
SENDER_KBPS = math.ceil(CS_BPS * (1400 + 44) / 1400 / 1000)   # + RTP/UDP/IP overhead (44 B / 1400 B payload)

REFCLK = f"a=ts-refclk:ptp=IEEE1588-2008:{GMID}:0\r\n" if GMID else "a=ts-refclk:ptp=IEEE1588-2008:traceable\r\n"

# 4:2:2 -> Cb/Cr at half horizontal resolution; 4:4:4 -> full; 4:2:0 -> half both
_CHROMA = {"YCbCr-4:2:2": (W // 2, H), "YCbCr-4:4:4": (W, H), "YCbCr-4:2:0": (W // 2, H // 2)}.get(SAMP, (W // 2, H))

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID = u("atoll:jxs:node"); DEV_ID = u("atoll:jxs:device")
SRC_ID, FLOW_ID, SEND_ID = u("atoll:jxs:src:video"), u("atoll:jxs:flow:video"), u("atoll:jxs:sender:video")

def _tai(when=None):
    t = time.time() if when is None else when
    return f"{int(t)}:{int((t % 1) * 1e9)}"
def _ver(): return _tai()

def _sdp():
    v = int(time.time())
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {PC_IP}\r\n"
        "s=Atoll - JPEG XS ST 2110-22 (video/jxsv)\r\n"
        "t=0 0\r\n"
        f"m=video {MPORT} RTP/AVP {PT}\r\n"
        f"c=IN IP4 {GRP}/64\r\n"
        f"b=AS:{SENDER_KBPS}\r\n"
        f"a=source-filter: incl IN IP4 {GRP} {PC_IP}\r\n"
        f"a=rtpmap:{PT} jxsv/90000\r\n"
        f"a=fmtp:{PT} packetmode=0; transmode=1; profile={PROFILE}; level={LEVEL}; sublevel={SUBLEVEL}; "
        f"sampling={SAMP}; depth={DEPTH}; width={W}; height={H}; exactframerate={EXACTFR}; "
        f"colorimetry={COLOR}; TCS={TCS}\r\n"
        "a=mediaclk:direct=0\r\n"
        + REFCLK)

def _node():
    return {"id": NODE_ID, "version": _ver(), "label": "atoll-jxs", "description": "Atoll JPEG XS ST 2110-22 source",
            "tags": {}, "href": f"http://{ADVERTISE}:{PORT}/", "hostname": "atoll-jxs", "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADVERTISE, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
def _device():
    return {"id": DEV_ID, "version": _ver(), "label": "atoll-jxs", "description": "JPEG XS 2110-22 sender",
            "tags": {}, "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
            "senders": [SEND_ID], "receivers": [], "controls": []}
def _source():
    return {"id": SRC_ID, "version": _ver(), "label": "JPEG XS video", "description": "Atoll JPEG XS source",
            "tags": {}, "caps": {}, "device_id": DEV_ID, "parents": [], "clock_name": None,
            "format": "urn:x-nmos:format:video"}
def _flow():
    f = {"id": FLOW_ID, "version": _ver(), "label": "JPEG XS", "description": f"JPEG XS {SAMP} {DEPTH}-bit (ST 2110-22)",
         "tags": {}, "source_id": SRC_ID, "device_id": DEV_ID, "parents": [],
         "format": "urn:x-nmos:format:video", "media_type": "video/jxsv",     # BCP-006-01
         "grain_rate": {"numerator": RATE_N, "denominator": RATE_D},
         "frame_width": W, "frame_height": H, "colorspace": COLOR,
         "interlace_mode": "progressive", "transfer_characteristic": TCS,
         "components": [{"name": "Y",  "width": W,           "height": H,           "bit_depth": DEPTH},
                        {"name": "Cb", "width": _CHROMA[0],  "height": _CHROMA[1],  "bit_depth": DEPTH},
                        {"name": "Cr", "width": _CHROMA[0],  "height": _CHROMA[1],  "bit_depth": DEPTH}],
         "profile": PROFILE, "level": LEVEL, "sublevel": SUBLEVEL,             # BCP-006-01 (omit if Unrestricted)
         "bit_rate": FLOW_KBPS}                                               # kbit/s, codestream
    return f
def _sender():
    return {"id": SEND_ID, "version": _ver(), "label": "JPEG XS (ST 2110-22)", "description": "video/jxsv RFC 9134",
            "tags": {}, "flow_id": FLOW_ID, "device_id": DEV_ID, "transport": "urn:x-nmos:transport:rtp.mcast",
            "interface_bindings": [], "subscription": {"receiver_id": None, "active": True},
            "manifest_href": f"http://{ADVERTISE}:{PORT}/sdp/jxs.sdp",
            "bit_rate": SENDER_KBPS, "st2110_21_sender_type": "2110TPW"}      # software sender: Wide, honestly

def _resources():
    return [("node", _node()), ("device", _device()), ("source", _source()),
            ("flow", _flow()), ("sender", _sender())]

def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{kind} -> {e.code}: {e.read().decode(errors='replace')[:300]}")

_registered = {"ok": False}
def register_all():
    try:
        for kind, data in _resources(): _post(kind, data)
        _registered["ok"] = True
        print(f"  registered JPEG XS node + sender with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- will retry; SDP still served locally", flush=True); return False
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

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sdp/jxs.sdp": return self._send(200, _sdp(), "application/sdp")
        if p in ("/", "/status"):
            return self._send(200, json.dumps({
                "node_id": NODE_ID, "device_id": DEV_ID, "sender_id": SEND_ID,
                "media": "video/jxsv (JPEG XS, RFC 9134 / BCP-006-01)",
                "stream": {"grp": GRP, "port": MPORT, "pt": PT},
                "format": {"w": W, "h": H, "sampling": SAMP, "depth": DEPTH, "exactframerate": EXACTFR,
                           "profile": PROFILE, "level": LEVEL, "sublevel": SUBLEVEL,
                           "colorimetry": COLOR, "TCS": TCS},
                "bit_rate_kbps": {"flow": FLOW_KBPS, "sender": SENDER_KBPS},
                "ptp_refclk": REFCLK.strip(), "registered": _registered["ok"], "registry": REG}, indent=2), "application/json")
        self._send(404, json.dumps({"error": "not found"}), "application/json")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"JPEG XS NMOS: node {NODE_ID} + sender on http://0.0.0.0:{PORT}  (SDP /sdp/jxs.sdp)", flush=True)
    Threaded(("0.0.0.0", PORT), Handler).serve_forever()
