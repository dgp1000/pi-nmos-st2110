#!/usr/bin/env python3
"""Atoll H.264 / H.265 NMOS -- IS-04 registrar + SDP server (AMWA BCP-006-02, NMOS With H.264/H.265).

Makes the rig's H.264 (`h264-send.sh`) and H.265 (`h265-send.sh`) RTP elementary streams standards-
clean: registers each as an IS-04 node/source/flow/sender with a coded-video Flow (`media_type`
video/H264 or video/H265 per BCP-006-02) and serves a conformant SDP -- the RFC 6184 / RFC 7798 media
format with the codec's real parameter sets, the ST 2110-style `b=AS` bandwidth, and the PTP
`ts-refclk` / `mediaclk` clock lines.

The parameter sets are not guessed: at start-up it joins each live stream, depayloads the RTP, and
pulls the actual SPS/PPS (H.264) and VPS/SPS/PPS (H.265) NAL units emitted by NVENC -- deriving
`profile-level-id` + `sprop-parameter-sets` for H.264, and `profile-id` / `tier-flag` / `level-id` +
`sprop-vps/sps/pps` for H.265 -- so the SDP matches the wire exactly.

Env/conf: NMOS_REGISTRY, NMOS_ADVERTISE_HOST, ISLAND_PC_IP, PTP_GMID, H265_NMOS_PORT,
H264_GRP/PORT, H265_GRP/PORT.
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
import socket as _sock, struct as _struct, base64
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "ISLAND_PC_IP", "PTP_GMID", "H265_NMOS_PORT",
        "H264_GRP", "H264_PORT", "H265_GRP", "H265_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
PORT = int(CFG.get("H265_NMOS_PORT") or 8110)
ADV = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI
SYS = SystemAPI(REGISTRY)
REG = f"{REGISTRY}/x-nmos/registration/v1.3"
PC_IP = (CFG.get("ISLAND_PC_IP") or "10.10.10.2").strip()
GMID = (CFG.get("PTP_GMID") or "").strip()
REFCLK = f"a=ts-refclk:ptp=IEEE1588-2008:{GMID}:0\r\n" if GMID else "a=ts-refclk:ptp=IEEE1588-2008:traceable\r\n"

W, H, FPS_N, FPS_D, KBPS = 1280, 720, 30, 1, 4000
H264 = {"key": "h264", "media": "video/H264", "grp": (CFG.get("H264_GRP") or "").strip(),
        "port": int(CFG.get("H264_PORT") or 5018), "label": "H.264 RTP (RFC 6184)"}
H265 = {"key": "h265", "media": "video/H265", "grp": (CFG.get("H265_GRP") or "").strip(),
        "port": int(CFG.get("H265_PORT") or 5019), "label": "H.265 RTP (RFC 7798)"}

# ---- parameter-set capture from the live RTP streams --------------------------------------------
def _nals_h264(payload):
    t = payload[0] & 0x1F
    if 1 <= t <= 23:
        yield t, payload
    elif t == 24:                                   # STAP-A aggregate
        i = 1
        while i + 2 <= len(payload):
            sz = (payload[i] << 8) | payload[i + 1]; i += 2
            nal = payload[i:i + sz]; i += sz
            if nal: yield nal[0] & 0x1F, nal
def _nals_h265(payload):
    if len(payload) < 2: return
    t = (payload[0] >> 1) & 0x3F
    if t < 48:
        yield t, payload
    elif t == 48:                                   # AP aggregate
        i = 2
        while i + 2 <= len(payload):
            sz = (payload[i] << 8) | payload[i + 1]; i += 2
            nal = payload[i:i + sz]; i += sz
            if nal: yield (nal[0] >> 1) & 0x3F, nal

def capture(grp, port, codec, secs=4):
    """Join a stream and collect its parameter-set NAL units -> {nal_type: bytes}."""
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM, _sock.IPPROTO_UDP)
    s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    s.bind(("", port))
    s.setsockopt(_sock.IPPROTO_IP, _sock.IP_ADD_MEMBERSHIP, _struct.pack("4s4s", _sock.inet_aton(grp), _sock.inet_aton(PC_IP)))
    s.settimeout(1.0)
    want = {7, 8} if codec == "h264" else {32, 33, 34}
    sets = {}
    t0 = time.time()
    while time.time() - t0 < secs and not want <= set(sets):
        try:
            pkt, _ = s.recvfrom(2048)
        except _sock.timeout:
            continue
        if len(pkt) < 13:
            continue
        cc = pkt[0] & 0x0F
        off = 12 + cc * 4                            # RTP header (no extension from rtph26xpay)
        payload = pkt[off:]
        gen = _nals_h264(payload) if codec == "h264" else _nals_h265(payload)
        for ntype, nal in gen:
            if ntype in want:
                sets[ntype] = nal
    s.close()
    return sets

def _fmtp_h264(sets):
    sps, pps = sets.get(7), sets.get(8)
    parts = ["packetization-mode=1"]
    if sps and len(sps) >= 4:
        parts.append("profile-level-id=" + sps[1:4].hex())
    if sps and pps:
        parts.append("sprop-parameter-sets=" + base64.b64encode(sps).decode() + "," + base64.b64encode(pps).decode())
    return "; ".join(parts)

def _deemulate(b):
    """Strip H.26x emulation_prevention_three_byte (00 00 03 -> 00 00) so RBSP byte offsets are real."""
    out = bytearray(); i = 0
    while i < len(b):
        if i + 2 < len(b) and b[i] == 0 and b[i + 1] == 0 and b[i + 2] == 3:
            out += b[i:i + 2]; i += 3
        else:
            out.append(b[i]); i += 1
    return bytes(out)

def _fmtp_h265(sets):
    vps, sps, pps = sets.get(32), sets.get(33), sets.get(34)
    parts = []
    rbsp = _deemulate(sps) if sps else b""
    if len(rbsp) >= 15:
        ptl = rbsp[3:]                              # after 2-byte NAL header + 1 byte (vps_id/sublayers/nesting)
        parts.append("profile-space=%d" % (ptl[0] >> 6))
        parts.append("profile-id=%d" % (ptl[0] & 0x1F))
        parts.append("tier-flag=%d" % ((ptl[0] >> 5) & 1))
        parts.append("level-id=%d" % ptl[11])
    if vps: parts.append("sprop-vps=" + base64.b64encode(vps).decode())
    if sps: parts.append("sprop-sps=" + base64.b64encode(sps).decode())
    if pps: parts.append("sprop-pps=" + base64.b64encode(pps).decode())
    parts.append("tx-mode=SRST")
    return "; ".join(parts)

PARAMS = {}                                          # codec key -> fmtp string
def build_params():
    try:
        PARAMS["h264"] = _fmtp_h264(capture(H264["grp"], H264["port"], "h264"))
    except Exception as e:
        PARAMS["h264"] = "packetization-mode=1"; print(f"  h264 param capture failed: {e}", flush=True)
    try:
        PARAMS["h265"] = _fmtp_h265(capture(H265["grp"], H265["port"], "h265"))
    except Exception as e:
        PARAMS["h265"] = "tx-mode=SRST"; print(f"  h265 param capture failed: {e}", flush=True)
    print(f"  H.264 fmtp: {PARAMS['h264'][:70]}...\n  H.265 fmtp: {PARAMS['h265'][:70]}...", flush=True)

# ---- IDs + resources ---------------------------------------------------------------------------
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID = u("atoll:codec:node"); DEV_ID = u("atoll:codec:device")
def ids(k): return u(f"atoll:codec:src:{k}"), u(f"atoll:codec:flow:{k}"), u(f"atoll:codec:sender:{k}")
SENDERS = {"h264": ids("h264"), "h265": ids("h265")}
KBITS = KBPS
SENDER_KBPS = round(KBPS * (1400 + 44) / 1400)

def _tai(t=None):
    t = time.time() if t is None else t
    return f"{int(t)}:{int((t % 1) * 1e9)}"
_V = _tai()

def _sdp(codec):
    C = H264 if codec == "h264" else H265
    enc = "H264" if codec == "h264" else "H265"
    v = int(time.time())
    return ("v=0\r\n"
            f"o=- {v} {v} IN IP4 {PC_IP}\r\n"
            f"s=Atoll - {C['label']}\r\n"
            "t=0 0\r\n"
            f"m=video {C['port']} RTP/AVP 96\r\n"
            f"c=IN IP4 {C['grp']}/64\r\n"
            f"b=AS:{SENDER_KBPS}\r\n"
            f"a=source-filter: incl IN IP4 {C['grp']} {PC_IP}\r\n"
            f"a=rtpmap:96 {enc}/90000\r\n"
            f"a=fmtp:96 {PARAMS.get(codec, '')}\r\n"
            "a=mediaclk:direct=0\r\n" + REFCLK)

def _node():
    return {"id": NODE_ID, "version": _V, "label": "atoll-codec", "description": "Atoll H.264/H.265 coded-video sources",
            "tags": {}, "href": f"http://{ADV}:{PORT}/", "hostname": "atoll-codec", "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADV, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
def _device():
    return {"id": DEV_ID, "version": _V, "label": "atoll-codec", "description": "H.264/H.265 senders", "tags": {},
            "type": "urn:x-nmos:device:generic", "node_id": NODE_ID,
            "senders": [SENDERS[k][2] for k in SENDERS], "receivers": [], "controls": []}
def _source(k):
    sid, _, _ = SENDERS[k]
    return {"id": sid, "version": _V, "label": f"{k} source", "description": f"Atoll {k} source", "tags": {},
            "caps": {}, "device_id": DEV_ID, "parents": [], "clock_name": None, "format": "urn:x-nmos:format:video"}
def _flow(k):
    C = H264 if k == "h264" else H265
    sid, fid, _ = SENDERS[k]
    return {"id": fid, "version": _V, "label": C["label"], "description": f"{C['media']} coded video (BCP-006-02)",
            "tags": {}, "source_id": sid, "device_id": DEV_ID, "parents": [],
            "format": "urn:x-nmos:format:video", "media_type": C["media"],
            "grain_rate": {"numerator": FPS_N, "denominator": FPS_D},
            "frame_width": W, "frame_height": H, "colorspace": "BT709",
            "interlace_mode": "progressive", "transfer_characteristic": "SDR",
            "components": [{"name": "Y",  "width": W,      "height": H, "bit_depth": 8},
                           {"name": "Cb", "width": W // 2, "height": H, "bit_depth": 8},
                           {"name": "Cr", "width": W // 2, "height": H, "bit_depth": 8}],
            "bit_rate": KBITS}
def _sender(k):
    C = H264 if k == "h264" else H265
    _, fid, sndid = SENDERS[k]
    return {"id": sndid, "version": _V, "label": C["label"], "description": f"{C['media']} RTP", "tags": {},
            "flow_id": fid, "device_id": DEV_ID, "transport": "urn:x-nmos:transport:rtp.mcast",
            "interface_bindings": ["eth0"], "subscription": {"receiver_id": None, "active": True},
            "manifest_href": f"http://{ADV}:{PORT}/sdp/{k}.sdp", "bit_rate": SENDER_KBPS,
            "st2110_21_sender_type": "2110TPW"}
def _resources():
    r = [("node", _node()), ("device", _device())]
    for k in SENDERS:
        r += [("source", _source(k)), ("flow", _flow(k)), ("sender", _sender(k))]
    return r

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
        _registered["ok"] = True; print(f"  registered H.264 + H.265 node/flows/senders with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False; print(f"  IS-04 registration failed ({e}) -- will retry", flush=True); return False
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

class H_(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sdp/h264.sdp": return self._send(200, _sdp("h264"), "application/sdp")
        if p == "/sdp/h265.sdp": return self._send(200, _sdp("h265"), "application/sdp")
        if p in ("/", "/status"):
            return self._send(200, json.dumps({"node_id": NODE_ID, "registered": _registered["ok"],
                "h264": {"grp": H264["grp"], "port": H264["port"], "fmtp": PARAMS.get("h264")},
                "h265": {"grp": H265["grp"], "port": H265["port"], "fmtp": PARAMS.get("h265")}}, indent=2), "application/json")
        self._send(404, json.dumps({"error": "not found"}), "application/json")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    build_params()
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"H.264/H.265 NMOS (BCP-006-02): node {NODE_ID} on http://0.0.0.0:{PORT}  (SDPs /sdp/h264.sdp, /sdp/h265.sdp)", flush=True)
    Threaded(("0.0.0.0", PORT), H_).serve_forever()
