#!/usr/bin/env python3
"""Atoll music channel -- IS-04 sender registrar (A2 approach).

The music channel's media is produced by music-channel.sh (HEVC video on MUSIC_GRP:MUSIC_PORT,
ST 2110-30 L24 audio on MUSIC_AUDIO_GRP:MUSIC_AUDIO_PORT). This process makes it a discoverable,
routable NMOS *source*: it registers a node/device with two senders (video + audio) in the IS-04
registry and serves an SDP per sender (manifest_href), then heartbeats -- mirroring is07-tally.py /
program-out.py. The senders appear in the panel's IS-04/05 inspector and in Program Out's routable
catalog (which resolves by multicast ip:port).

Transport note: the L24 audio is genuine ST 2110-30 (RTP/L24, rtp.mcast) with a standards-clean SDP.
The video is HEVC in MPEG-TS over plain UDP (the rig's "Home videos"/m0 style); NMOS has no standard
transport URN for raw TS/UDP, so it is advertised as mp2t over UDP in the SDP and rtp.mcast in IS-04
purely for discovery + Program-Out routing (which routes by ip:port, not the SDP). Converting the
video to TS-over-RTP (like tsrtp) would make it fully standards-clean -- a later option.
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "MUSIC_NMOS_PORT", "ISLAND_PC_IP",
        "MUSIC_GRP", "MUSIC_PORT", "MUSIC_AUDIO_GRP", "MUSIC_AUDIO_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)

PORT       = int(CFG.get("MUSIC_NMOS_PORT") or 8093)
ADVERTISE  = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY   = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI   # IS-09 System API client
SYS = SystemAPI(REGISTRY)   # IS-09: discover + honour the System API (heartbeat interval, ptp)
REG        = f"{REGISTRY}/x-nmos/registration/v1.3"
ISLAND_IP  = (CFG.get("ISLAND_PC_IP") or "10.10.10.2").strip()
V_GRP, V_PORT = (CFG.get("MUSIC_GRP") or "").strip(), (CFG.get("MUSIC_PORT") or "").strip()
A_GRP, A_PORT = (CFG.get("MUSIC_AUDIO_GRP") or "").strip(), (CFG.get("MUSIC_AUDIO_PORT") or "").strip()

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID  = u("atoll:music:node")
DEV_ID   = u("atoll:music:device")
VSRC_ID, VFLOW_ID, VSEND_ID = u("atoll:music:src:video"),  u("atoll:music:flow:video"),  u("atoll:music:sender:video")
ASRC_ID, AFLOW_ID, ASEND_ID = u("atoll:music:src:audio"),  u("atoll:music:flow:audio"),  u("atoll:music:sender:audio")

def _tai(when=None):
    t = time.time() if when is None else when
    return f"{int(t)}:{int((t % 1) * 1e9)}"
def _ver():
    return _tai()

# ---- SDPs (served at each sender's manifest_href) ------------------------------------------------
def _sdp_audio():
    v = int(time.time())
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {ISLAND_IP}\r\n"
        "s=Atoll Music - L24 audio (ST 2110-30)\r\n"
        "t=0 0\r\n"
        f"m=audio {A_PORT} RTP/AVP 96\r\n"
        f"c=IN IP4 {A_GRP}/64\r\n"
        f"a=source-filter: incl IN IP4 {A_GRP} {ISLAND_IP}\r\n"
        "a=rtpmap:96 L24/48000/2\r\n"
        "a=ptime:1\r\n"
        "a=mediaclk:direct=0\r\n"
        "a=ts-refclk:ptp=IEEE1588-2008:traceable\r\n")
def _sdp_video():
    v = int(time.time())
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {ISLAND_IP}\r\n"
        "s=Atoll Music - HEVC video (MPEG-TS/UDP)\r\n"
        "t=0 0\r\n"
        f"m=video {V_PORT} udp MP2T\r\n"
        f"c=IN IP4 {V_GRP}/64\r\n"
        f"a=source-filter: incl IN IP4 {V_GRP} {ISLAND_IP}\r\n")

# ---- IS-04 resources ----------------------------------------------------------------------------
def _node():
    return {"id": NODE_ID, "version": _ver(), "label": "atoll-music",
            "description": "Atoll music channel (Now Playing bridge)", "tags": {},
            "href": f"http://{ADVERTISE}:{PORT}/", "hostname": "atoll-music", "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADVERTISE, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
def _device():
    return {"id": DEV_ID, "version": _ver(), "label": "atoll-music", "description": "Atoll music channel",
            "tags": {}, "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
            "senders": [VSEND_ID, ASEND_ID], "receivers": [], "controls": []}
def _src(sid, fmt, label, channels=None):
    d = {"id": sid, "version": _ver(), "label": label, "description": label, "tags": {},
         "caps": {}, "device_id": DEV_ID, "parents": [], "clock_name": None, "format": fmt}
    if channels is not None:                 # source_audio requires a channels array
        d["channels"] = channels
    return d
def _flow_video():
    return {"id": VFLOW_ID, "version": _ver(), "label": "Music HEVC", "description": "Music HEVC 720p",
            "tags": {}, "source_id": VSRC_ID, "device_id": DEV_ID, "parents": [],
            "format": "urn:x-nmos:format:video", "media_type": "video/H265",
            "grain_rate": {"numerator": 30, "denominator": 1},
            "frame_width": 1280, "frame_height": 720,
            "colorspace": "BT709", "interlace_mode": "progressive"}
def _flow_audio():
    return {"id": AFLOW_ID, "version": _ver(), "label": "Music L24", "description": "Music ST 2110-30 L24 stereo",
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
        ("source", _src(VSRC_ID, "urn:x-nmos:format:video", "Music video")),
        ("source", _src(ASRC_ID, "urn:x-nmos:format:audio", "Music audio",
                        channels=[{"label": "Left", "symbol": "L"}, {"label": "Right", "symbol": "R"}])),
        ("flow", _flow_video()), ("flow", _flow_audio()),
        ("sender", _sender(VSEND_ID, VFLOW_ID, "Music (video)", "/sdp/music-video.sdp")),
        ("sender", _sender(ASEND_ID, AFLOW_ID, "Music (L24 audio)", "/sdp/music-audio.sdp")),
    ]

def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{kind} -> {e.code}: {detail}")

_registered = {"ok": False}
def register_all():
    try:
        for kind, data in _resources():
            _post(kind, data)
        _registered["ok"] = True
        print(f"  registered music node + 2 senders (video, L24 audio) with IS-04 at {REG}", flush=True)
        return True
    except Exception as e:
        _registered["ok"] = False
        print(f"  IS-04 registration failed ({e}) -- will retry; SDPs still served locally", flush=True)
        return False
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

# ---- HTTP: serve the two SDPs + a small status page ---------------------------------------------
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sdp/music-audio.sdp": return self._send(200, _sdp_audio(), "application/sdp")
        if p == "/sdp/music-video.sdp": return self._send(200, _sdp_video(), "application/sdp")
        if p in ("/", "/status"):
            return self._send(200, json.dumps({
                "node_id": NODE_ID, "device_id": DEV_ID,
                "senders": {"video": VSEND_ID, "audio": ASEND_ID},
                "video": {"grp": V_GRP, "port": V_PORT, "media": "video/H265 in MPEG-TS/UDP"},
                "audio": {"grp": A_GRP, "port": A_PORT, "media": "audio/L24 (ST 2110-30)"},
                "registered": _registered["ok"], "registry": REG}, indent=2), "application/json")
        return self._send(404, "not found", "text/plain")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"music-nmos: SDPs on http://0.0.0.0:{PORT}/sdp/  senders video={VSEND_ID[:8]} audio={ASEND_ID[:8]}", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
