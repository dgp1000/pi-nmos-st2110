#!/usr/bin/env python3
"""Atoll music channel -- IS-04 sender registrar + IS-05 sender-side Connection API.

The music channel's media is produced by music-channel.sh (HEVC video on MUSIC_GRP:MUSIC_PORT,
ST 2110-30 L24 audio on MUSIC_AUDIO_GRP:MUSIC_AUDIO_PORT). This process makes it a discoverable,
routable NMOS *source*: it registers a node/device with two senders (video + audio) in the IS-04
registry, serves an SDP per sender (manifest_href), heartbeats, AND serves the IS-05 v1.1
Connection API for those senders -- so a controller (our panel, or any NMOS controller) can
re-point where a sender transmits (multicast destination_ip + destination_port) live.

Sender-side IS-05 (the "other half" of connection management -- Program Out is the receiver half):
  * GET  /x-nmos/connection/v1.1/single/senders                 -> [video/, audio/]
  * GET  .../senders/{id}/{constraints,staged,active,transporttype,transportfile}
  * PATCH .../senders/{id}/staged  -- change transport_params (destination_ip/destination_port),
    master_enable, and activate (activate_immediate / _scheduled_relative / _scheduled_absolute).
On activation we write the sender's transport to a knob the pipeline reads (music-video-transport
for the video udpsink, music-audio-transport for the audiomapper's L24 udpsink) and restart that
one service, so the flow genuinely moves to the new group -- IS-05 drives the packets, which is the
point. The SDP (manifest_href) regenerates from the active transport, and we re-register the sender
(version bump) so the registry/inspector and any receiver see the new destination. Default (no knob)
= the configured groups, so a fresh boot matches the SDP.

Transport note: the L24 audio is genuine ST 2110-30 (RTP/L24, rtp.mcast) with a standards-clean SDP.
The video is HEVC in MPEG-TS over plain UDP (the rig's "Home videos"/m0 style); NMOS has no standard
transport URN for raw TS/UDP, so it is advertised as mp2t over UDP in the SDP and rtp.mcast in IS-04
purely for discovery + Program-Out routing (which routes by ip:port, not the SDP).
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "MUSIC_NMOS_PORT", "ISLAND_PC_IP", "PTP_GMID",
        "MUSIC_GRP", "MUSIC_PORT", "MUSIC_AUDIO_GRP", "MUSIC_AUDIO_PORT", "ATOLL_RUN"]
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
RUN        = (CFG.get("ATOLL_RUN") or "").strip() or os.path.join(os.path.expanduser("~"), "atoll-run")
_GMID = (CFG.get("PTP_GMID") or "").strip()
_REFCLK = (f"a=ts-refclk:ptp=IEEE1588-2008:{_GMID}:0\r\n" if _GMID else "a=ts-refclk:ptp=IEEE1588-2008:traceable\r\n")

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID  = u("atoll:music:node")
DEV_ID   = u("atoll:music:device")
VSRC_ID, VFLOW_ID, VSEND_ID = u("atoll:music:src:video"),  u("atoll:music:flow:video"),  u("atoll:music:sender:video")
ASRC_ID, AFLOW_ID, ASEND_ID = u("atoll:music:src:audio"),  u("atoll:music:flow:audio"),  u("atoll:music:sender:audio")

CONN_BASE = "/x-nmos/connection/v1.1"

def _tai(when=None):
    t = time.time() if when is None else when
    return f"{int(t) + 37}:{int((t % 1) * 1e9):09d}"   # TAI = UTC + 37 leap seconds
def _ver():
    return _tai()
def _dur_to_secs(v):
    try:
        if v is None: return 0.0
        if isinstance(v, (int, float)): return float(v)
        sec, _, nsec = str(v).partition(":")
        return int(sec or 0) + int(nsec or 0) / 1e9
    except Exception:
        return 0.0
def _tai_to_unix(v):
    try:
        sec, _, nsec = str(v).partition(":")
        return (int(sec or 0) - 37) + int(nsec or 0) / 1e9
    except Exception:
        return time.time()

# ---- Sender transport state (the IS-05-editable part) -------------------------------------------
# Each sender's active destination is held here and mirrored to a pipeline knob. The knob is the
# source of truth the shell pipelines read; on startup we adopt any knob already on disk so a
# re-pointed sender survives a music-nmos restart and stays consistent with what is on the wire.
_lock = threading.RLock()
def _read_knob(path, dgrp, dport):
    try:
        with open(path) as f:
            parts = f.read().split()
        if len(parts) >= 2 and parts[0]:
            return parts[0], parts[1]
    except Exception:
        pass
    return dgrp, dport
def _sparams(grp, port):
    try: dport = int(port)
    except Exception: dport = port
    return [{"source_ip": ISLAND_IP, "destination_ip": grp, "destination_port": dport,
             "source_port": "auto", "rtp_enabled": True}]
def _conn_blank():
    return {"master_enable": True, "receiver_id": None,
            "activation": {"mode": None, "requested_time": None, "activation_time": None},
            "transport_params": _sparams("", "")}

SENDERS = {
    VSEND_ID: {"key": "video", "service": "atoll-music",       "knob": os.path.join(RUN, "music-video-transport"),
               "dgrp": V_GRP, "dport": V_PORT, "sdp": "/sdp/music-video.sdp"},
    ASEND_ID: {"key": "audio", "service": "atoll-audiomapper", "knob": os.path.join(RUN, "music-audio-transport"),
               "dgrp": A_GRP, "dport": A_PORT, "sdp": "/sdp/music-audio.sdp"},
}
for sid, S in SENDERS.items():
    grp, port = _read_knob(S["knob"], S["dgrp"], S["dport"])
    S["grp"], S["port"] = grp, port
    S["active"] = {"master_enable": True, "receiver_id": None,
                   "activation": {"mode": "activate_immediate", "requested_time": None, "activation_time": _tai()},
                   "transport_params": _sparams(grp, port)}
    S["staged"] = _conn_blank()
    S["staged"]["transport_params"] = _sparams(grp, port)
    S["pending"] = None   # a threading.Timer for a scheduled activation

def _write_knob(S, grp, port):
    os.makedirs(RUN, exist_ok=True)
    tmp = S["knob"] + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{grp} {port}\n")
    os.replace(tmp, S["knob"])

def _apply_sender(sid, staged, activation_time=None):
    """Move staged->active for a sender: adopt its transport, write the pipeline knob, restart the
    one pipeline service, and re-register the sender so the registry/SDP show the new destination."""
    S = SENDERS[sid]
    tp = (staged.get("transport_params") or _sparams(S["grp"], S["port"]))[0]
    grp = tp.get("destination_ip") or S["grp"]
    port = tp.get("destination_port") or S["port"]
    act = dict(staged.get("activation") or {})
    act["activation_time"] = activation_time or _tai()
    S["active"] = {"master_enable": bool(staged.get("master_enable", True)),
                   "receiver_id": staged.get("receiver_id"),
                   "activation": act, "transport_params": _sparams(grp, str(port))}
    moved = (grp, str(port)) != (S["grp"], str(S["port"]))
    S["grp"], S["port"] = grp, str(port)
    try:
        _write_knob(S, grp, port)
        subprocess.run(["sudo", "-n", "systemctl", "restart", S["service"]],
                       check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        print(f"{time.strftime('%T')} IS-05 sender {S['key']} -> {grp}:{port} "
              f"({'moved, ' if moved else ''}restarted {S['service']})", flush=True)
    except Exception as e:
        print(f"  sender {S['key']} apply/restart failed: {e}", flush=True)
    # re-register the sender (SDP regenerates from S['grp']/S['port'])
    try:
        _post("sender", _sender_resource(sid))
    except Exception as e:
        print(f"  sender {S['key']} re-register failed: {e}", flush=True)

def _cancel_pending(sid):
    S = SENDERS[sid]
    t = S.get("pending")
    if t is not None:
        try: t.cancel()
        except Exception: pass
        S["pending"] = None
def _schedule_sender(sid, fire_at_unix):
    _cancel_pending(sid)
    delay = max(0.0, fire_at_unix - time.time())
    def fire():
        with _lock:
            SENDERS[sid]["pending"] = None
            at = SENDERS[sid]["staged"]["activation"].get("activation_time")
            _apply_sender(sid, SENDERS[sid]["staged"], at)
    t = threading.Timer(delay, fire); t.daemon = True; t.start()
    SENDERS[sid]["pending"] = t

# ---- SDPs (served at each sender's manifest_href / transportfile) -------------------------------
def _sdp_audio():
    v = int(time.time()); S = SENDERS[ASEND_ID]
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {ISLAND_IP}\r\n"
        "s=Atoll Music - L24 audio (ST 2110-30)\r\n"
        "t=0 0\r\n"
        f"m=audio {S['port']} RTP/AVP 96\r\n"
        f"c=IN IP4 {S['grp']}/64\r\n"
        f"a=source-filter: incl IN IP4 {S['grp']} {ISLAND_IP}\r\n"
        "a=rtpmap:96 L24/48000/2\r\n"
        "a=ptime:1\r\n"
        "a=mediaclk:direct=0\r\n"
        + _REFCLK)
def _sdp_video():
    v = int(time.time()); S = SENDERS[VSEND_ID]
    return (
        "v=0\r\n"
        f"o=- {v} {v} IN IP4 {ISLAND_IP}\r\n"
        "s=Atoll Music - HEVC video (MPEG-TS/UDP)\r\n"
        "t=0 0\r\n"
        f"m=video {S['port']} udp MP2T\r\n"
        f"c=IN IP4 {S['grp']}/64\r\n"
        f"a=source-filter: incl IN IP4 {S['grp']} {ISLAND_IP}\r\n")
def _sdp_for(sid):
    return _sdp_video() if sid == VSEND_ID else _sdp_audio()

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
            "senders": [VSEND_ID, ASEND_ID], "receivers": [],
            "controls": [{"href": f"http://{ADVERTISE}:{PORT}{CONN_BASE}",
                          "type": "urn:x-nmos:control:sr-ctrl/v1.1", "authorization": False}]}
def _src(sid, fmt, label, channels=None):
    d = {"id": sid, "version": _ver(), "label": label, "description": label, "tags": {},
         "caps": {}, "device_id": DEV_ID, "parents": [], "clock_name": None, "format": fmt}
    if channels is not None:
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
def _sender_resource(sid):
    S = SENDERS[sid]
    flow_id = VFLOW_ID if sid == VSEND_ID else AFLOW_ID
    label = "Music (video)" if sid == VSEND_ID else "Music (L24 audio)"
    return {"id": sid, "version": _ver(), "label": label, "description": label, "tags": {},
            "flow_id": flow_id, "device_id": DEV_ID, "transport": "urn:x-nmos:transport:rtp.mcast",
            "interface_bindings": [], "subscription": {"receiver_id": S["active"].get("receiver_id"),
                                                       "active": bool(S["active"].get("master_enable", True))},
            "manifest_href": f"http://{ADVERTISE}:{PORT}{S['sdp']}"}

def _resources():
    return [
        ("node", _node()), ("device", _device()),
        ("source", _src(VSRC_ID, "urn:x-nmos:format:video", "Music video")),
        ("source", _src(ASRC_ID, "urn:x-nmos:format:audio", "Music audio",
                        channels=[{"label": "Left", "symbol": "L"}, {"label": "Right", "symbol": "R"}])),
        ("flow", _flow_video()), ("flow", _flow_audio()),
        ("sender", _sender_resource(VSEND_ID)),
        ("sender", _sender_resource(ASEND_ID)),
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

# ---- HTTP: SDPs, status, and the IS-05 sender Connection API ------------------------------------
def _norm_sparams(patch_tp, cur_tp):
    """Merge a PATCH transport_params leg onto the current one (IS-05 partial update)."""
    leg = dict(cur_tp[0]) if cur_tp else {}
    if isinstance(patch_tp, list) and patch_tp and isinstance(patch_tp[0], dict):
        for k, v in patch_tp[0].items():
            leg[k] = v
    return [leg]

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body, indent=2)
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/sdp/music-audio.sdp": return self._send(200, _sdp_audio(), "application/sdp")
        if p == "/sdp/music-video.sdp": return self._send(200, _sdp_video(), "application/sdp")

        # ---- IS-05 sender Connection API ----
        if p == "/x-nmos": return self._send(200, ["connection/"])
        if p == "/x-nmos/connection": return self._send(200, ["v1.1/"])
        if p == f"{CONN_BASE}": return self._send(200, ["single/"])
        if p == f"{CONN_BASE}/single": return self._send(200, ["senders/", "receivers/"])
        if p == f"{CONN_BASE}/single/receivers": return self._send(200, [])
        if p == f"{CONN_BASE}/single/senders":
            return self._send(200, [f"{VSEND_ID}/", f"{ASEND_ID}/"])
        for sid in SENDERS:
            base = f"{CONN_BASE}/single/senders/{sid}"
            if p == base:
                return self._send(200, ["constraints/", "staged/", "active/", "transporttype/", "transportfile/"])
            if p == f"{base}/active":   return self._send(200, SENDERS[sid]["active"])
            if p == f"{base}/staged":   return self._send(200, SENDERS[sid]["staged"])
            if p == f"{base}/constraints":
                return self._send(200, [{"source_ip": {}, "destination_ip": {}, "destination_port": {},
                                         "source_port": {}, "rtp_enabled": {}}])
            if p == f"{base}/transporttype":
                return self._send(200, "urn:x-nmos:transport:rtp.mcast")
            if p == f"{base}/transportfile":
                return self._send(200, _sdp_for(sid), "application/sdp")

        if p in ("/", "/status"):
            return self._send(200, {
                "node_id": NODE_ID, "device_id": DEV_ID,
                "senders": {"video": VSEND_ID, "audio": ASEND_ID},
                "video": {"grp": SENDERS[VSEND_ID]["grp"], "port": SENDERS[VSEND_ID]["port"],
                          "media": "video/H265 in MPEG-TS/UDP"},
                "audio": {"grp": SENDERS[ASEND_ID]["grp"], "port": SENDERS[ASEND_ID]["port"],
                          "media": "audio/L24 (ST 2110-30)"},
                "defaults": {"video": {"grp": V_GRP, "port": V_PORT},
                             "audio": {"grp": A_GRP, "port": A_PORT}},
                "connection_api": f"{CONN_BASE}/single/senders/",
                "registered": _registered["ok"], "registry": REG})
        return self._send(404, {"code": 404, "error": "Not Found", "debug": p})

    def do_PATCH(self):
        p = urlparse(self.path).path
        sid = None
        for s in SENDERS:
            if p == f"{CONN_BASE}/single/senders/{s}/staged":
                sid = s; break
        if sid is None:
            return self._send(404, {"code": 404, "error": "Not Found", "debug": p})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            patch = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"code": 400, "error": f"bad JSON: {e}"})
        with _lock:
            S = SENDERS[sid]
            st = S["staged"]
            if "master_enable" in patch:
                st["master_enable"] = bool(patch["master_enable"])
            if "receiver_id" in patch:
                st["receiver_id"] = patch["receiver_id"]
            if "transport_params" in patch:
                st["transport_params"] = _norm_sparams(patch["transport_params"], st["transport_params"])
            act = patch.get("activation") or {}
            mode = act.get("mode")
            req_t = act.get("requested_time")
            st["activation"] = {"mode": mode, "requested_time": req_t, "activation_time": None}
            _cancel_pending(sid)   # any new PATCH supersedes a pending scheduled activation
            if mode == "activate_immediate":
                _apply_sender(sid, st)
                st["activation"]["activation_time"] = S["active"]["activation"]["activation_time"]
            elif mode == "activate_scheduled_relative":
                fire = time.time() + _dur_to_secs(req_t)
                st["activation"]["activation_time"] = _tai(fire)
                _schedule_sender(sid, fire)
            elif mode == "activate_scheduled_absolute":
                fire = _tai_to_unix(req_t)
                st["activation"]["activation_time"] = req_t
                _schedule_sender(sid, fire)
            # mode None -> stage only
            return self._send(200, st)

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"music-nmos: SDPs + IS-05 on http://0.0.0.0:{PORT}{CONN_BASE}/single/senders/  "
          f"video={VSEND_ID[:8]} audio={ASEND_ID[:8]}", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
