#!/usr/bin/env python3
"""Atoll Pi ST 2110 -- IS-04 sender registrar + SDP server for the Pi's GENUINE ST 2110 flows.

The Raspberry Pi grandmaster sends two real ST 2110 essences on the island (launch-all.sh):
  * ST 2110-20 uncompressed raw video (RFC 4175) on PI_RAW_GRP:PI_RAW_PORT
  * ST 2110-30 L24 audio on PI_AUDIO_GRP:PI_AUDIO_PORT
...but nothing advertised them in NMOS, so they had no SDPs and were invisible to controllers.
This PC-side process registers them as a node/device with two senders and serves a
standards-complete SDP for each (manifest_href), then heartbeats -- mirroring music-nmos.py.

The SDPs are the point: the video SDP carries the full ST 2110-20 media format AND the ST 2110-21
sender-pacing declaration (PM=2110GPM general packing, TP=... traffic-shaping type, SSN), and both
carry the PTP reference clock (a=ts-refclk:ptp=...:<gmid>:<domain>) and media clock
(a=mediaclk:direct=0). NOTE on -21: these are software gst senders (rtpvrawpay / rtpL24pay) with no
hardware packet shaping, so they are honestly declared TP=2110TPW (Wide), not Narrow -- true 2110TPN
needs a hardware-paced NIC. The SDP tells a receiver/analyser exactly how to read the flow.
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "PI_NMOS_PORT", "ISLAND_PI_IP", "PTP_GMID",
        "PI_RAW_GRP", "PI_RAW_PORT", "PI_AUDIO_GRP", "PI_AUDIO_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)

PORT      = int(CFG.get("PI_NMOS_PORT") or 8095)
ADVERTISE = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY  = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI   # IS-09 System API client
SYS = SystemAPI(REGISTRY)
REG       = f"{REGISTRY}/x-nmos/registration/v1.3"
PI_IP     = (CFG.get("ISLAND_PI_IP") or "10.10.10.1").strip()
GMID      = (CFG.get("PTP_GMID") or "").strip()          # PTP grandmaster EUI-64 (e.g. D8-3A-DD-FF-FE-EA-7B-5E)
V_GRP, V_PORT = (CFG.get("PI_RAW_GRP") or "").strip(), (CFG.get("PI_RAW_PORT") or "").strip()
A_GRP, A_PORT = (CFG.get("PI_AUDIO_GRP") or "").strip(), (CFG.get("PI_AUDIO_PORT") or "").strip()

# ST 2110-20 raw video params (match launch-all.sh: UYVY 320x240 59.94, 4:2:2 8-bit)
V_W, V_H = 320, 240
V_RATE_N, V_RATE_D = 60000, 1001
# reference clock line (RFC 7273): specific grandmaster if we know it, else "traceable"
REFCLK = f"a=ts-refclk:ptp=IEEE1588-2008:{GMID}:0\r\n" if GMID else "a=ts-refclk:ptp=IEEE1588-2008:traceable\r\n"

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID = u("atoll:pi:node"); DEV_ID = u("atoll:pi:device")
VSRC_ID, VFLOW_ID, VSEND_ID = u("atoll:pi:src:video"), u("atoll:pi:flow:video"), u("atoll:pi:sender:video")
ASRC_ID, AFLOW_ID, ASEND_ID = u("atoll:pi:src:audio"), u("atoll:pi:flow:audio"), u("atoll:pi:sender:audio")

def _tai(when=None):
    t = time.time() if when is None else when
    return f"{int(t)}:{int((t % 1) * 1e9)}"
def _ver(): return _tai()

# ---- SDPs (served at each sender's manifest_href) ------------------------------------------------
def _sdp_video():
    v = int(time.time())
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {PI_IP}\r\n"
        "s=Atoll Pi - ST 2110-20 raw video\r\n"
        "t=0 0\r\n"
        f"m=video {V_PORT} RTP/AVP 96\r\n"
        f"c=IN IP4 {V_GRP}/64\r\n"
        f"a=source-filter: incl IN IP4 {V_GRP} {PI_IP}\r\n"
        "a=rtpmap:96 raw/90000\r\n"
        f"a=fmtp:96 sampling=YCbCr-4:2:2; width={V_W}; height={V_H}; "
        f"exactframerate={V_RATE_N}/{V_RATE_D}; depth=8; TCS=SDR; colorimetry=BT601; "
        "PM=2110GPM; SSN=ST2110-20:2017; TP=2110TPW\r\n"
        "a=mediaclk:direct=0\r\n"
        + REFCLK)
def _sdp_audio():
    v = int(time.time())
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {PI_IP}\r\n"
        "s=Atoll Pi - L24 audio (ST 2110-30)\r\n"
        "t=0 0\r\n"
        f"m=audio {A_PORT} RTP/AVP 96\r\n"
        f"c=IN IP4 {A_GRP}/64\r\n"
        f"a=source-filter: incl IN IP4 {A_GRP} {PI_IP}\r\n"
        "a=rtpmap:96 L24/48000/2\r\n"
        "a=ptime:1\r\n"
        "a=mediaclk:direct=0\r\n"
        + REFCLK)

# ---- IS-04 resources ----------------------------------------------------------------------------
def _node():
    return {"id": NODE_ID, "version": _ver(), "label": "atoll-pi", "description": "Atoll Raspberry Pi ST 2110 source",
            "tags": {}, "href": f"http://{ADVERTISE}:{PORT}/", "hostname": "atoll-pi", "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADVERTISE, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
def _device():
    return {"id": DEV_ID, "version": _ver(), "label": "atoll-pi", "description": "Pi ST 2110 senders",
            "tags": {}, "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
            "senders": [VSEND_ID, ASEND_ID], "receivers": [], "controls": []}
def _src(sid, fmt, label, channels=None):
    d = {"id": sid, "version": _ver(), "label": label, "description": label, "tags": {},
         "caps": {}, "device_id": DEV_ID, "parents": [], "clock_name": None, "format": fmt}
    if channels is not None: d["channels"] = channels
    return d
def _flow_video():
    return {"id": VFLOW_ID, "version": _ver(), "label": "Pi raw", "description": "Pi ST 2110-20 raw 4:2:2 8-bit",
            "tags": {}, "source_id": VSRC_ID, "device_id": DEV_ID, "parents": [],
            "format": "urn:x-nmos:format:video", "media_type": "video/raw",
            "grain_rate": {"numerator": V_RATE_N, "denominator": V_RATE_D},
            "frame_width": V_W, "frame_height": V_H, "colorspace": "BT601",
            "interlace_mode": "progressive", "transfer_characteristic": "SDR",
            "components": [{"name": "Y",  "width": V_W,      "height": V_H, "bit_depth": 8},
                           {"name": "Cb", "width": V_W // 2, "height": V_H, "bit_depth": 8},
                           {"name": "Cr", "width": V_W // 2, "height": V_H, "bit_depth": 8}]}
def _flow_audio():
    return {"id": AFLOW_ID, "version": _ver(), "label": "Pi L24", "description": "Pi ST 2110-30 L24 stereo",
            "tags": {}, "source_id": ASRC_ID, "device_id": DEV_ID, "parents": [],
            "format": "urn:x-nmos:format:audio", "media_type": "audio/L24",
            "sample_rate": {"numerator": 48000, "denominator": 1}, "bit_depth": 24}
def _sender(sid, flow_id, label, sdp_path):
    return {"id": sid, "version": _ver(), "label": label, "description": label, "tags": {},
            "flow_id": flow_id, "device_id": DEV_ID, "transport": "urn:x-nmos:transport:rtp.mcast",
            "interface_bindings": [], "subscription": {"receiver_id": None, "active": True},
            "manifest_href": f"http://{ADVERTISE}:{PORT}{sdp_path}"}
def _resources():
    return [
        ("node", _node()), ("device", _device()),
        ("source", _src(VSRC_ID, "urn:x-nmos:format:video", "Pi raw video")),
        ("source", _src(ASRC_ID, "urn:x-nmos:format:audio", "Pi L24 audio",
                        channels=[{"label": "Left", "symbol": "L"}, {"label": "Right", "symbol": "R"}])),
        ("flow", _flow_video()), ("flow", _flow_audio()),
        ("sender", _sender(VSEND_ID, VFLOW_ID, "Pi raw (ST 2110-20)", "/sdp/pi-video.sdp")),
        ("sender", _sender(ASEND_ID, AFLOW_ID, "Pi L24 (ST 2110-30)", "/sdp/pi-audio.sdp")),
    ]
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
        print(f"  registered Pi node + 2 senders (raw video, L24 audio) with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- will retry; SDPs still served locally", flush=True); return False
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

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sdp/pi-video.sdp": return self._send(200, _sdp_video(), "application/sdp")
        if p == "/sdp/pi-audio.sdp": return self._send(200, _sdp_audio(), "application/sdp")
        if p in ("/", "/status"):
            return self._send(200, json.dumps({
                "node_id": NODE_ID, "device_id": DEV_ID,
                "senders": {"video": VSEND_ID, "audio": ASEND_ID},
                "video": {"grp": V_GRP, "port": V_PORT, "media": "video/raw ST 2110-20 (RFC 4175)"},
                "audio": {"grp": A_GRP, "port": A_PORT, "media": "audio/L24 ST 2110-30"},
                "ptp_refclk": REFCLK.strip(), "registered": _registered["ok"], "registry": REG}, indent=2), "application/json")
        self._send(404, json.dumps({"error": "not found"}), "application/json")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"Pi ST 2110 NMOS: node {NODE_ID} + 2 senders on http://0.0.0.0:{PORT}  (SDPs /sdp/pi-video.sdp, /sdp/pi-audio.sdp)", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
