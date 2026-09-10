#!/usr/bin/env python3
"""IS-05 source switch panel + live IS-04/IS-05 inspector + PTP timecode, iPad-friendly.

Control: buttons issue real IS-05 takes to switch the active video receiver
between the Pi raw ST 2110-20 flow (NMOS receiver v0) and the PC JPEG-XS island
flow (NMOS receiver m0). The PC HEVC 4K island flow (239.10.10.65:5010) is not
an NMOS receiver, so selecting it just disables the NMOS receivers.

Inspector: /nmos aggregates IS-04 (registry Query API, :8080) with IS-05 active
connection params (node Connection API, :8090) and the page renders nodes,
receivers (switchable ones first) and senders -- formats, caps, transport
params (multicast addr/port, legs), subscriptions, enables. Served through :8096
so the iPad only needs one reachable port (no CORS / extra firewall holes).

Video/audio are viewed on the native PC window (pc/hevc-stream-view-file.sh,
pc/jxs-stream-view.sh) -- the old in-browser MJPEG/HLS preview was removed (it
tore on smooth motion on iOS; the native window is glitch-free). PTP timecode is
relayed from the Pi grandmaster.

Run in WSL:  python3 monitor-web.py    Open from iPad: http://<pc-wifi-ip>:8096
"""
import http.server, socketserver, urllib.request, json, time, os, subprocess, signal, glob
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.error
import atoll_config as cfg

_c = cfg.load()
PORT = int(_c.get("PANEL_PORT", "8096"))
FPS = 60000 / 1001
PI_CLOCK = f"http://{_c.get('ISLAND_PI_IP', '10.10.10.1')}:8000/time"
PI2_STATUS = f"http://{_c.get('ISLAND_PI2_IP', '10.10.10.3')}:8000/status"   # 2nd-Pi PTP follower readout (island-only; proxied for the iPad)
NODE = "http://localhost:8090/x-nmos/node/v1.3"
CONN = "http://localhost:8090/x-nmos/connection/v1.1/single"
PROGRAMOUT = f"http://localhost:{_c.get('PROGRAMOUT_PORT') or '8092'}"   # Program Out software receiver (IS-05)
QUERY = "http://localhost:8080/x-nmos/query/v1.3"
MAC_MUSIC = f"http://{_c.get('MAC_MUSIC_HOST', '192.168.6.159')}:{_c.get('MAC_MUSIC_PORT', '8008')}"   # Mac "Now Playing"; proxied for the iPad
AUDIOMAP = f"http://localhost:{_c.get('AUDIOMAP_NMOS_PORT') or '8094'}"   # IS-08 Channel Mapping API for the music audio

SOURCES = {
    "jxs":  {"label": "easy-nmos-node/receiver/m0"},   # PC JPEG-XS island flow
    "raw":  {"label": "easy-nmos-node/receiver/v0"},   # Pi raw ST 2110-20
    "hevc": {"label": None},                            # PC HEVC 4K island flow (not NMOS)
    "music": {"label": None},                           # Music channel (Mac->island 239.10.10.30:5012, not NMOS)
    "jpegxs": {"label": None},                          # JPEG XS ST 2110-22 codec (local encode->decode)
    "j2k": {"label": None},                             # JPEG 2000 island flow (J2K/RTP RFC 5371)
    "h264": {"label": None},                            # H.264 elementary stream over RTP (RFC 6184), 239.10.10.75:5018
    "mjpeg": {"label": None},                           # Motion JPEG over RTP (RFC 2435), 239.10.10.85:5024
    "vp9": {"label": None},                             # VP9 over RTP (RFC 7741), 239.10.10.90:5026
    "tsrtp": {"label": None},                           # MPEG-TS over RTP (ST 2022-2), 239.10.10.95:5028
    "fec": {"label": None},                             # ST 2022-1 FEC protected TS/RTP, 239.10.10.100:5040
    "sps": {"label": None},                             # ST 2022-7 seamless dual path, 239.10.10.105/106
    "reels": {"label": None},                           # Test Reels (PC->island 239.10.10.31:5014; NMOS m1 later)
}
DEFAULT_SRC = "jxs"
_active = {"src": DEFAULT_SRC, "ts": 0.0}
_ACTIVE_TTL = 2.0  # seconds; avoids hammering the NMOS node on every /state poll
_output = {"layout": "single"}   # native output (monitor 2) layout: single|side|multi|wall
# Multiview tile assignment: which source fills each 2x2 slot (TL, TR, BL, BR). Any source -> any slot.
_slots = ["hevc", "jxs", "music", "tsrtp"]   # raw kept out of the default 4-up (oversubscribes WSLg -> Live TV steps)

def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)

def _safe_json(url, timeout=3):
    try:
        return http_json(url, timeout=timeout)
    except Exception:
        return None

# ----------------------------- IS-08 audio channel mapping ---------------
_AMAP_PRESETS = {
    "stereo": {"0": {"input": "in1", "channel_index": 0}, "1": {"input": "in1", "channel_index": 1}},
    "swap":   {"0": {"input": "in1", "channel_index": 1}, "1": {"input": "in1", "channel_index": 0}},
    "monoL":  {"0": {"input": "in1", "channel_index": 0}, "1": {"input": "in1", "channel_index": 0}},
    "muteR":  {"0": {"input": "in1", "channel_index": 0}, "1": {"input": None, "channel_index": None}},
}
def audiomap_state():
    with urllib.request.urlopen(f"{AUDIOMAP}/audiomap", timeout=3) as r:
        return r.read()
def audiomap_set(preset, secs=0):
    """POST an IS-08 channel-map activation for a named preset (immediate, or scheduled if secs>0)."""
    action = {"out1": _AMAP_PRESETS.get(preset, _AMAP_PRESETS["stereo"])}
    if secs and int(secs) > 0:
        act = {"mode": "activate_scheduled_relative", "requested_time": f"{int(secs)}:0"}
    else:
        act = {"mode": "activate_immediate"}
    body = json.dumps({"activation": act, "action": action}).encode()
    req = urllib.request.Request(f"{AUDIOMAP}/x-nmos/channelmapping/v1.0/map/activations",
                                 data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status

# ----------------------------- IS-05 control -----------------------------
def _programout_state():
    with urllib.request.urlopen(f"{PROGRAMOUT}/programout", timeout=3) as r:
        return json.loads(r.read())

def programout_route(essence, secs=0):
    """PATCH the Program Out receiver's IS-05 /staged to the chosen flow and activate. essence
    "none" (or unknown) disconnects. secs>0 uses activate_scheduled_relative (fires in N seconds)
    instead of activate_immediate. The multicast/port come from the receiver's own catalog."""
    st = _programout_state()
    rid = st["receiver_id"]; cat = st.get("catalog") or {}
    if essence in cat:
        f = cat[essence]
        body = {"master_enable": True,
                "transport_params": [{"multicast_ip": f["ip"], "destination_port": int(f["port"])}]}
    else:
        body = {"master_enable": False}
    if secs and int(secs) > 0:
        body["activation"] = {"mode": "activate_scheduled_relative", "requested_time": f"{int(secs)}:0"}
    else:
        body["activation"] = {"mode": "activate_immediate"}
    _h = {"Content-Type": "application/json"}
    _bt = _auth_bearer()                       # IS-10: carry a token when Program Out enforces auth
    if _bt: _h["Authorization"] = "Bearer " + _bt
    req = urllib.request.Request(f"{PROGRAMOUT}/x-nmos/connection/v1.1/single/receivers/{rid}/staged",
                                 data=json.dumps(body).encode(), method="PATCH", headers=_h)
    with urllib.request.urlopen(req, timeout=4) as r:
        return r.status

def receiver_id(label):
    for rx in http_json(f"{NODE}/receivers"):
        if rx.get("label") == label:
            return rx["id"]
    raise RuntimeError(f"receiver {label!r} not found")

def set_enable(label, on):
    rid = receiver_id(label)
    body = {"master_enable": bool(on), "activation": {"mode": "activate_immediate"}}
    req = urllib.request.Request(f"{CONN}/receivers/{rid}/staged",
                                 data=json.dumps(body).encode(), method="PATCH",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status

def is_enabled(label):
    rid = receiver_id(label)
    return bool(http_json(f"{CONN}/receivers/{rid}/active").get("master_enable"))

def take(src):
    for key, s in SOURCES.items():
        if s["label"] is None:        # non-NMOS source (e.g. HEVC) -> nothing to take
            continue
        set_enable(s["label"], key == src)
    _active["src"] = src
    _active["ts"] = time.monotonic()

def active_src():
    """Best-effort current source: trust a recent take, else query the receivers."""
    if time.monotonic() - _active["ts"] < _ACTIVE_TTL:
        return _active["src"]
    if SOURCES.get(_active["src"], {}).get("label") is None:   # non-NMOS -> trust last take
        _active["ts"] = time.monotonic()
        return _active["src"]
    src = DEFAULT_SRC
    for key, s in SOURCES.items():
        if s["label"] is None:
            continue
        try:
            if is_enabled(s["label"]):
                src = key
                break
        except Exception:
            pass
    _active["src"] = src
    _active["ts"] = time.monotonic()
    return src

# ----------------------- IS-04 / IS-05 inspector -------------------------
_nmos_cache = {"t": 0.0, "data": None}
_NMOS_TTL = 3.0

def _grain(gr):
    if isinstance(gr, dict) and gr.get("numerator"):
        d = gr.get("denominator", 1)
        return f"{gr['numerator']}/{d}" if d not in (1, None) else str(gr["numerator"])
    return None

def _is05_active(kind, rid):
    d = _safe_json(f"{CONN}/{kind}/{rid}/active", timeout=2)
    if not isinstance(d, dict):
        return {}
    tp = d.get("transport_params") or []
    leg0 = tp[0] if tp else {}
    return {
        "master_enable": d.get("master_enable"),
        "sender_id": d.get("sender_id"),
        "receiver_id": d.get("receiver_id"),
        "multicast_ip": leg0.get("multicast_ip"),
        "destination_ip": leg0.get("destination_ip"),
        "destination_port": leg0.get("destination_port"),
        "source_ip": leg0.get("source_ip"),
        "interface_ip": leg0.get("interface_ip"),
        "rtp_enabled": leg0.get("rtp_enabled"),
        "legs": len(tp),
        "activation_mode": (d.get("activation") or {}).get("mode"),
        "transport_file_type": (d.get("transport_file") or {}).get("type"),
    }

def _flow_summary(flow):
    if not flow:
        return {}
    out = {"media_type": flow.get("media_type"),
           "format": (flow.get("format") or "").split(":")[-1]}
    if flow.get("frame_width"):
        out["res"] = f"{flow.get('frame_width')}x{flow.get('frame_height')}"
    gr = _grain(flow.get("grain_rate"))
    if gr:
        out["rate"] = gr
    sr = flow.get("sample_rate")
    if isinstance(sr, dict) and sr.get("numerator"):
        out["sample_rate"] = sr["numerator"]
    if flow.get("bit_depth"):
        out["bit_depth"] = flow["bit_depth"]
    return out

def _caps_summary(caps):
    out = {"media_types": caps.get("media_types", [])}
    cs = caps.get("constraint_sets") or [{}]
    c0 = cs[0] if cs else {}
    def enum(k):
        v = c0.get(k)
        return v.get("enum") if isinstance(v, dict) else None
    fw, fh = enum("urn:x-nmos:cap:format:frame_width"), enum("urn:x-nmos:cap:format:frame_height")
    if fw and fh:
        out["res"] = f"{fw[0]}x{fh[0]}"
    gr = enum("urn:x-nmos:cap:format:grain_rate")
    if gr:
        out["rate"] = _grain(gr[0])
    samp = enum("urn:x-nmos:cap:format:color_sampling")
    if samp:
        out["sampling"] = samp[0]
    return out

def _media_rank(media_type):
    m = media_type or ""
    if m.startswith("video") or "2022-6" in m:
        return 0
    if m.startswith("audio"):
        return 1
    return 2

def nmos_overview():
    now = time.monotonic()
    if _nmos_cache["data"] and now - _nmos_cache["t"] < _NMOS_TTL:
        return _nmos_cache["data"]

    nodes = _safe_json(f"{QUERY}/nodes") or []
    devices = _safe_json(f"{QUERY}/devices") or []
    senders = _safe_json(f"{QUERY}/senders") or []
    receivers = _safe_json(f"{QUERY}/receivers") or []
    flows = {f["id"]: f for f in (_safe_json(f"{QUERY}/flows") or [])}

    switch = {v["label"]: k for k, v in SOURCES.items() if v.get("label")}

    def node_view(n):
        return {
            "label": n.get("label"), "id": n.get("id"), "hostname": n.get("hostname"),
            "href": n.get("href"),
            "clocks": [{"name": c.get("name"), "ref_type": c.get("ref_type"),
                        "gmid": c.get("gmid"), "traceable": c.get("traceable"),
                        "locked": c.get("locked")} for c in n.get("clocks", [])],
            "interfaces": [{"name": i.get("name"), "mac": i.get("chassis_id")}
                           for i in n.get("interfaces", [])],
            "api_versions": (n.get("api") or {}).get("versions", []),
        }

    out_senders = []
    for s in senders:
        fl = _flow_summary(flows.get(s.get("flow_id")))
        out_senders.append({
            "id": s.get("id"), "label": s.get("label"),
            "transport": (s.get("transport") or "").split(":")[-1],
            "manifest_href": s.get("manifest_href"),
            "flow": fl,
            "subscription": s.get("subscription", {}),
            "is05": _is05_active("senders", s.get("id")),
        })
    out_senders.sort(key=lambda x: (_media_rank(x["flow"].get("media_type")), x["label"] or ""))

    out_receivers = []
    for r in receivers:
        out_receivers.append({
            "id": r.get("id"), "label": r.get("label"),
            "format": (r.get("format") or "").split(":")[-1],
            "transport": (r.get("transport") or "").split(":")[-1],
            "caps": _caps_summary(r.get("caps", {})),
            "subscription": r.get("subscription", {}),
            "switch": switch.get(r.get("label")),
            "is05": _is05_active("receivers", r.get("id")),
        })
    out_receivers.sort(key=lambda x: (x["switch"] is None, x["label"] or ""))

    data = {
        "nodes": [node_view(n) for n in nodes],
        "devices": [{"label": d.get("label"), "id": d.get("id"),
                     "type": (d.get("type") or "").split(":")[-1]} for d in devices],
        "senders": out_senders,
        "receivers": out_receivers,
        "counts": {"nodes": len(nodes), "devices": len(devices),
                   "senders": len(senders), "receivers": len(receivers), "flows": len(flows)},
    }
    _nmos_cache.update(t=now, data=data)
    return data

def resource_detail(kind, rid):
    """Everything we have on one resource: the full IS-04 object plus (for senders/
    receivers) the IS-05 active/staged/constraints and the sender's SDP transport file.
    The SDP is fetched via localhost:8090 (the node's manifest_href points at a
    docker-internal IP the iPad can't reach)."""
    out = {"is04": _safe_json(f"{QUERY}/{kind}/{rid}")}
    if kind in ("senders", "receivers"):
        out["is05_active"] = _safe_json(f"{CONN}/{kind}/{rid}/active", timeout=2)
        out["is05_staged"] = _safe_json(f"{CONN}/{kind}/{rid}/staged", timeout=2)
        out["is05_constraints"] = _safe_json(f"{CONN}/{kind}/{rid}/constraints", timeout=2)
        href = (out["is04"] or {}).get("manifest_href")
        if kind == "senders" and href:
            try:
                sdp_url = f"http://localhost:8090{urlparse(href).path}"
                with urllib.request.urlopen(sdp_url, timeout=3) as r:
                    out["sdp"] = r.read().decode("utf-8", "replace")
            except Exception:
                out["sdp"] = None
    return out

def pi_time():
    try:
        with urllib.request.urlopen(PI_CLOCK, timeout=2) as r:
            return r.read()
    except Exception:
        return json.dumps({"epoch_ms": time.time() * 1000}).encode()

def follower_status():
    """Proxy the 2nd-Pi PTP follower's /status (island-only) for the iPad panel."""
    try:
        with urllib.request.urlopen(PI2_STATUS, timeout=2) as r:
            return r.read()
    except Exception:
        return json.dumps({"state": "offline"}).encode()

def follower_resync():
    """Tell the follower to restart ptp4l so it visibly re-converges (demo button)."""
    try:
        with urllib.request.urlopen(PI2_STATUS.replace("/status", "/resync"), timeout=5) as r:
            return r.read()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}).encode()

def music_state():
    """Proxy the Mac music server's now-playing state for the iPad panel."""
    try:
        with urllib.request.urlopen(f"{MAC_MUSIC}/state", timeout=3) as r:
            return r.read()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()

def music_action(action):
    """Proxy a playback control to the Mac music server (POST)."""
    try:
        req = urllib.request.Request(f"{MAC_MUSIC}/{action}", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.dumps({"ok": True, "status": r.status}).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()

# --------------------------------- TV channels ---------------------------
import hdhr as _hdhr
# Find the tuner by DeviceID (HDHR_DEVICE_ID) so a DHCP move self-heals; HDHR_HOST is the fallback.
HDHR_DEVICE_ID = _c.get('HDHR_DEVICE_ID', '').strip()
_HDHR_FALLBACK = _c.get('HDHR_HOST', '192.168.7.88')
HDHR_HOST = _hdhr.resolve(HDHR_DEVICE_ID, _HDHR_FALLBACK) or _HDHR_FALLBACK
def _hdhr_rediscover():
    global HDHR_HOST
    if not HDHR_DEVICE_ID:
        return
    ip = _hdhr.resolve(HDHR_DEVICE_ID, HDHR_HOST)
    if ip and ip != HDHR_HOST:
        print(f"HDHomeRun {HDHR_DEVICE_ID} moved {HDHR_HOST} -> {ip}", flush=True)
        HDHR_HOST = ip
_RUN = _c.get('ATOLL_RUN', '/home/david/atoll-run')
SWITCHER_KNOB = _RUN + "/switcher"
AVSYNC_KNOB = _RUN + "/video-delay-ms"   # A/V sync: +ms holds video back to meet late audio
CC_ENABLE_KNOB = _RUN + "/cc-enable"     # ST 2110-40 captions on/off (anc-recv renders them)
ANC_SCTE_KNOB  = _RUN + "/anc-scte"      # touch -> anc-send emits an SCTE-104 splice (AD break)
ANC_TC_FILE    = _RUN + "/anc-tc"        # anc-recv writes the received ATC timecode here
CC_SOURCE_KNOB = _RUN + "/cc-source"     # "live" (real broadcast CC via cc-relay) or "synthetic"
def _cc_source():
    try: return "live" if open(CC_SOURCE_KNOB).read().strip()=="live" else "synthetic"
    except Exception: return "synthetic"
def _cc_source_set(mode):
    m = "live" if mode=="live" else "synthetic"
    try:
        with open(CC_SOURCE_KNOB,"w") as f: f.write(m)
    except OSError: pass
    return {"source": m}
CAPTION_DELAY_KNOB = _RUN + "/caption-delay-ms"   # shift captions LATER to match the delayed video
def _cc_delay_get():
    try: return int(open(CAPTION_DELAY_KNOB).read().strip())
    except Exception: return 7000
def _cc_delay_set(ms):
    try: ms = max(0, min(15000, int(float(ms))))
    except Exception: return {"ms": _cc_delay_get()}
    try:
        with open(CAPTION_DELAY_KNOB,"w") as f: f.write(str(ms))
    except OSError: pass
    return {"ms": ms}
def _cc_get():
    try: return open(CC_ENABLE_KNOB).read().strip() in ("1","true","on")
    except Exception: return False
def _cc_set(on):
    try:
        with open(CC_ENABLE_KNOB, "w") as f: f.write("1" if on else "0")
    except OSError: pass
    return {"on": _cc_get()}
def _cc_scte():
    try:
        with open(ANC_SCTE_KNOB, "w") as f: f.write("1")
    except OSError: pass
    return {"scte": True}
def _cc_state():
    tc = ""
    try: tc = open(ANC_TC_FILE).read().strip()
    except Exception: pass
    return {"on": _cc_get(), "tc": tc, "source": _cc_source(), "delay": _cc_delay_get()}

# ---- IS-10 authorization (AMWA IS-10 / BCP-003-02) --------------------------------------------
AUTH_ENABLE_KNOB = _RUN + "/auth-enable"        # "1" -> Program Out requires a valid bearer token
AUTH_PORT = _c.get("AUTH_PORT") or "8106"
AUTH_URL  = f"http://localhost:{AUTH_PORT}"       # the IS-10 authorization server
_tok = {"jwt": None, "exp": 0}
def _auth_get():
    try: return open(AUTH_ENABLE_KNOB).read().strip() in ("1", "true", "on")
    except Exception: return False
def _fetch_token(scope="connection"):
    """client_credentials grant from the AS; cached until ~60 s before expiry."""
    now = time.time()
    if _tok["jwt"] and _tok["exp"] - 60 > now:
        return _tok["jwt"]
    data = urlencode({"grant_type": "client_credentials", "client_id": "atoll-panel", "scope": scope}).encode()
    with urllib.request.urlopen(urllib.request.Request(AUTH_URL + "/token", data=data, method="POST"), timeout=4) as r:
        j = json.loads(r.read())
    _tok["jwt"] = j["access_token"]; _tok["exp"] = now + int(j.get("expires_in", 3600))
    return _tok["jwt"]
def _auth_bearer():
    """A token when enforcement is ON (so the panel's own PATCHes pass), else None (stay open)."""
    if not _auth_get(): return None
    try: return _fetch_token()
    except Exception: return None
def _auth_set(on):
    try:
        with open(AUTH_ENABLE_KNOB, "w") as f: f.write("1" if on else "0")
    except OSError: pass
    _tok["jwt"] = None; _tok["exp"] = 0        # drop any cached token on a toggle
    return _auth_state()
def _auth_state():
    up = False; kid = ""; issuer = ""
    try:
        with urllib.request.urlopen(AUTH_URL + "/", timeout=2) as r:
            j = json.loads(r.read()); up = True; kid = j.get("kid", ""); issuer = j.get("issuer", "")
    except Exception: pass
    return {"enabled": _auth_get(), "as_up": up, "kid": kid, "issuer": issuer, "url": AUTH_URL}

# ---- IS-11 stream compatibility (proxy to atoll-is11) ------------------------------------------
import hashlib as _hl
IS11 = f"http://localhost:{_c.get('IS11_PORT') or '8107'}"
IS11_SC = IS11 + "/x-nmos/streamcompatibility/v1.0"
IS11_NODE = IS11 + "/x-nmos/node/v1.3"
_is11 = {"sender": None, "input": None, "flow": None, "receiver": None}
def _is11_get(url, t=3):
    with urllib.request.urlopen(url, timeout=t) as r: return r.status, r.read()
def _is11_discover():
    if _is11["sender"] and _is11["input"]: return
    try:
        _is11["sender"] = json.loads(_is11_get(IS11_SC + "/senders/")[1])[0].rstrip("/")
        _is11["input"] = json.loads(_is11_get(IS11_SC + "/inputs/")[1])[0].rstrip("/")
        _is11["receiver"] = json.loads(_is11_get(IS11_SC + "/receivers/")[1])[0].rstrip("/")
        _is11["flow"] = json.loads(_is11_get(IS11_NODE + "/senders/" + _is11["sender"])[1])["flow_id"]
    except Exception: pass
def _is11_state():
    _is11_discover()
    d = {"up": False, "sender_status": "?", "grain_rate": "?", "base_edid": False,
         "effective_hash": "", "receiver_status": "?"}
    sid, iid, fid, rid = _is11["sender"], _is11["input"], _is11["flow"], _is11["receiver"]
    if not sid: return d
    try:
        d["sender_status"] = json.loads(_is11_get(IS11_SC + f"/senders/{sid}/status/")[1])["state"]; d["up"] = True
    except Exception: return d
    try:
        gr = json.loads(_is11_get(IS11_NODE + f"/flows/{fid}")[1])["grain_rate"]; d["grain_rate"] = f"{gr['numerator']}/{gr['denominator']}"
    except Exception: pass
    try:
        d["base_edid"] = (_is11_get(IS11_SC + f"/inputs/{iid}/edid/base/")[0] == 200)
    except Exception: pass
    try:
        d["effective_hash"] = _hl.sha1(_is11_get(IS11_SC + f"/inputs/{iid}/edid/effective/")[1]).hexdigest()[:8]
    except Exception: pass
    try:
        d["receiver_status"] = json.loads(_is11_get(IS11_SC + f"/receivers/{rid}/status/")[1])["state"]
    except Exception: pass
    return d
def _is11_req(url, method, data=None, ctype=None):
    h = {"Content-Type": ctype} if ctype else {}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=4) as r: return {"ok": r.status in (200, 204), "code": r.status}
    except urllib.error.HTTPError as e: return {"ok": False, "code": e.code}
    except Exception as e: return {"ok": False, "err": str(e)}
def _is11_constrain(num, den):
    _is11_discover()
    body = json.dumps({"constraint_sets": [{"urn:x-nmos:cap:format:grain_rate": {"enum": [{"numerator": int(num), "denominator": int(den)}]}}]}).encode()
    return _is11_req(IS11_SC + f"/senders/{_is11['sender']}/constraints/active/", "PUT", body, "application/json")
def _is11_unconstrain():
    _is11_discover()
    return _is11_req(IS11_SC + f"/senders/{_is11['sender']}/constraints/active/", "DELETE")
def _is11_edid(load):
    _is11_discover()
    url = IS11_SC + f"/inputs/{_is11['input']}/edid/base/"
    if not load:
        return _is11_req(url, "DELETE")
    e = bytearray(128); e[0:8] = bytes([0, 255, 255, 255, 255, 255, 255, 0]); e[8:10] = bytes([0x2D, 0xC9])
    e[18] = 1; e[19] = 4; e[127] = (256 - (sum(e[:127]) % 256)) % 256
    return _is11_req(url, "PUT", bytes(e), "application/octet-stream")

# ---- IS-12 device control (proxy to the atoll-is12 ncp WebSocket) ------------------------------
def _is12_ws_cmd(cmds):
    import socket as _sk, base64 as _b64, struct as _st, os as _o
    s = _sk.socket(); s.settimeout(4); s.connect(("localhost", int(_c.get("IS12_WS_PORT") or 8109)))
    key = _b64.b64encode(_o.urandom(16)).decode()
    s.sendall(("GET /x-nmos/ncp/v1.0 HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: ncp\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(1024)
        if not c: break
        buf += c
    pl = json.dumps({"messageType": 0, "commands": cmds}).encode(); m = _o.urandom(4)
    hdr = bytes([0x81]); n = len(pl)
    hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + _st.pack("!H", n)
    s.sendall(hdr + m + bytes(b ^ m[i % 4] for i, b in enumerate(pl)))
    h = s.recv(2); ln = h[1] & 0x7F
    if ln == 126: ln = _st.unpack("!H", s.recv(2))[0]
    elif ln == 127: ln = _st.unpack("!Q", s.recv(8))[0]
    d = b""
    while len(d) < ln: d += s.recv(ln - len(d))
    s.close()
    return json.loads(d.decode()).get("responses", [])
def _is12_state():
    d = {"up": False, "members": [], "classes": 0, "datatypes": 0, "product": None}
    try:
        r = _is12_ws_cmd([
            {"handle": 1, "oid": 1, "methodId": {"level": 2, "index": 1}, "arguments": {"recurse": False}},
            {"handle": 2, "oid": 3, "methodId": {"level": 1, "index": 1}, "arguments": {"id": {"level": 3, "index": 1}}},
            {"handle": 3, "oid": 3, "methodId": {"level": 1, "index": 1}, "arguments": {"id": {"level": 3, "index": 2}}},
            {"handle": 4, "oid": 2, "methodId": {"level": 1, "index": 1}, "arguments": {"id": {"level": 3, "index": 3}}},
        ])
        d["up"] = True
        d["members"] = [m.get("role") for m in (r[0]["result"].get("value") or [])]
        d["classes"] = len(r[1]["result"].get("value") or [])
        d["datatypes"] = len(r[2]["result"].get("value") or [])
        d["product"] = (r[3]["result"].get("value") or {}).get("name")
    except Exception as e:
        d["err"] = str(e)[:60]
    return d
def _auth_demo():
    """Prove enforcement: stage a no-op PATCH on Program Out WITHOUT a token, then WITH one."""
    st = _programout_state(); rid = st["receiver_id"]
    url = f"{PROGRAMOUT}/x-nmos/connection/v1.1/single/receivers/{rid}/staged"
    body = json.dumps({"master_enable": bool(st.get("master_enable", False))}).encode()   # no-op stage
    def _patch(tok):
        h = {"Content-Type": "application/json"}
        if tok: h["Authorization"] = "Bearer " + tok
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, method="PATCH", headers=h), timeout=4) as r:
                return r.status
        except urllib.error.HTTPError as e: return e.code
        except Exception: return 0
    without = _patch(None)
    try: tok = _fetch_token()
    except Exception: tok = None
    return {"enabled": _auth_get(), "without_token": without, "with_token": _patch(tok),
            "token_preview": (tok[:28] + "…") if tok else None}

# ---- Record & Playback -------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
RECDIR = os.path.join(os.path.dirname((_RUN or "/home/david/atoll-run").rstrip("/")), "atoll-recordings")
_IFACE = _c.get("ISLAND_IFACE", "eth1")
_PC_IP = _c.get("ISLAND_PC_IP", "10.10.10.2")
_TTL = _c.get("MCAST_TTL", "1")
def _grp(k): return _c.get(f"{k}_GRP"), _c.get(f"{k}_PORT")
REC_TS = {"hevc": "HEVC", "jxs": "HOME", "music": "MUSIC"}   # lossless TS-over-UDP dump
REC_RTP = ("raw", "h264", "mjpeg", "vp9", "tsrtp")           # transcoded to HEVC+AAC TS on record
RECORDABLE = set(REC_TS) | set(REC_RTP)
_RAWCAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,"
            "sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,"
            "colorimetry=(string)BT601-5,payload=(int)96")
_H264CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96"
_OPUSCAPS = "application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97"
_MJPEGCAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=96"
_VP9CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=VP9,payload=96"
_TSRTPCAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33"
_L24CAPS = "application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96"
# software HEVC (x265, no NVENC session) at 720p30 I420, muxed AAC -> reels-playable TS
_VENC = ("videoconvert ! videoscale ! videorate ! video/x-raw,format=I420,width=1280,height=720,framerate=30/1 "
         "! x265enc speed-preset=ultrafast tune=zerolatency bitrate=6000 key-int-max=30 ! h265parse config-interval=-1 ! queue ! mux.")
_AENC = "avenc_aac ! aacparse ! queue ! mux."
_SILENCE = f"audiotestsrc wave=silence is-live=true ! audioconvert ! audioresample ! {_AENC}"
def _udp(g, p, buf=8388608, caps=None):
    c = f'caps="{caps}" ' if caps else ""
    return f"udpsrc address={g} port={p} multicast-iface={_IFACE} buffer-size={buf} {c}"
def _record_cmd(src, path):
    if src in REC_TS:                                        # lossless TS dump
        g, p = _grp(REC_TS[src])
        return f'gst-launch-1.0 -q {_udp(g, p)}! filesink location="{path}"'
    mux = f'mpegtsmux name=mux alignment=7 ! queue ! filesink location="{path}"'
    if src == "raw":
        g, p = _grp("PI_RAW"); ag, ap = _grp("PI_AUDIO")
        v = f'{_udp(g, p, caps=_RAWCAPS)}! rtpjitterbuffer latency=100 ! rtpvrawdepay ! {_VENC}'
        a = f'{_udp(ag, ap, 16777216, _L24CAPS)}! rtpjitterbuffer latency=500 ! rtpL24depay ! audioconvert ! audioresample ! {_AENC}'
    elif src == "h264":
        g, p = _grp("H264"); ag, ap = _grp("OPUS")
        v = f'{_udp(g, p, caps=_H264CAPS)}! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! nvh264dec ! cudadownload ! {_VENC}'
        a = f'{_udp(ag, ap, caps=_OPUSCAPS)}! rtpjitterbuffer latency=200 ! rtpopusdepay ! opusdec ! audioconvert ! audioresample ! {_AENC}'
    elif src == "mjpeg":
        g, p = _grp("MJPEG")
        v = f'{_udp(g, p, 16777216, _MJPEGCAPS)}! rtpjitterbuffer latency=100 ! rtpjpegdepay ! nvjpegdec ! {_VENC}'
        a = _SILENCE
    elif src == "vp9":
        g, p = _grp("VP9")
        v = f'{_udp(g, p, caps=_VP9CAPS)}! rtpjitterbuffer latency=100 ! rtpvp9depay ! vp9parse ! nvvp9dec ! {_VENC}'
        a = _SILENCE
    elif src == "tsrtp":
        g, p = _grp("TSRTP")
        v = f'{_udp(g, p, caps=_TSRTPCAPS)}! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d d. ! h264parse ! queue ! nvh264dec ! cudadownload ! {_VENC}'
        a = "d. ! audio/mpeg ! queue ! decodebin ! audioconvert ! audioresample ! " + _AENC
    else:
        return None
    return f'gst-launch-1.0 -q {v} {a} {mux}'
_REELS_G, _REELS_P = _c.get("REELS_GRP"), _c.get("REELS_PORT")   # playback shows on "Test Reels"
_rec = {"proc": None, "src": None, "file": None, "t0": 0}
_play = {"proc": None, "file": None, "loop": False}
def _spawn(cmd):
    return subprocess.Popen("exec " + cmd, shell=True, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def _kill(proc):
    if not proc: return
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try: proc.terminate()
        except Exception: pass
def _alive(d): return bool(d["proc"] and d["proc"].poll() is None)
def _rec_status():
    return {"recording": _alive(_rec), "src": _rec["src"] if _alive(_rec) else None,
            "file": _rec["file"] if _alive(_rec) else None,
            "secs": int(time.time() - _rec["t0"]) if _alive(_rec) else 0,
            "playing": _alive(_play), "play_file": _play["file"] if _alive(_play) else None,
            "loop": _play["loop"] if _alive(_play) else False}
def _rec_start(src):
    if _alive(_rec): return {"error": "already recording"}
    if not src:
        src = active_src()                          # default: record the current source
    if src not in RECORDABLE: return {"error": f"{src} not recordable"}
    os.makedirs(RECDIR, exist_ok=True)
    fn = time.strftime("%Y%m%d-%H%M%S") + f"_{src}.ts"
    cmd = _record_cmd(src, os.path.join(RECDIR, fn))
    if not cmd: return {"error": "no pipeline"}
    _rec.update(proc=_spawn(cmd), src=src, file=fn, t0=time.time())
    return _rec_status()
def _rec_stop():
    _kill(_rec["proc"]); _rec.update(proc=None); return _rec_status()
def _rec_list():
    out = []
    try:
        for f in sorted(glob.glob(os.path.join(RECDIR, "*.ts")), reverse=True):
            stt = os.stat(f); out.append({"name": os.path.basename(f), "mb": round(stt.st_size / 1e6, 1), "mtime": int(stt.st_mtime)})
    except Exception: pass
    return out
def _play_start(fn, loop):
    _kill(_play["proc"])
    path = os.path.join(RECDIR, os.path.basename(fn or ""))
    if not os.path.isfile(path): return {"error": "no such file"}
    cmd = f'python3 "{os.path.join(_HERE, "playback-send.py")}" "{path}" {_REELS_G} {_REELS_P} {_PC_IP} {_TTL} {"loop" if loop else ""}'
    _play.update(proc=_spawn(cmd), file=os.path.basename(fn), loop=bool(loop))
    return _rec_status()
def _play_stop():
    _kill(_play["proc"]); _play.update(proc=None, file=None, loop=False); return _rec_status()
def _rec_delete(fn):
    b = os.path.basename(fn or "")
    if _alive(_play) and _play["file"] == b: return {"error": "playing"}
    try: os.remove(os.path.join(RECDIR, b))
    except Exception as e: return {"error": str(e)}
    return {"deleted": b}
def _avsync_get():
    try: return int(open(AVSYNC_KNOB).read().strip())
    except Exception: return 30
def _avsync_set(ms):
    try: ms = max(-100, min(300, int(float(ms))))
    except Exception: return {"ms": _avsync_get()}
    try:
        with open(AVSYNC_KNOB, "w") as f: f.write(str(ms) + "\n")
    except OSError: pass
    return {"ms": ms}
_SWITCH_SRCS = ["hevc", "jxs", "music", "tsrtp", "h264"]
_SWITCH_LABEL = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "tsrtp": "TS over RTP", "h264": "H.264 RTP"}
_switcher = {"a": "hevc", "b": "music", "trans": "dissolve", "rate": 1.0, "seq": 0}
def _switch_load():   # adopt the current knob on startup so the panel's seq stays in sync with
    try:              # a running switcher across a panel restart (else the first take can no-op)
        a, b, t, r, sq = open(SWITCHER_KNOB).read().split()
        _switcher.update(a=a, b=b, trans=t, rate=float(r), seq=int(sq))
    except Exception:
        pass
_switch_load()
def _switch_write():
    try:
        with open(SWITCHER_KNOB, "w") as f:
            f.write(f"{_switcher['a']} {_switcher['b']} {_switcher['trans']} {_switcher['rate']} {_switcher['seq']}\n")
    except OSError:
        pass
def _switch_pgm(): return _switcher["a"] if _switcher["seq"] % 2 == 0 else _switcher["b"]
def _switch_pvw(): return _switcher["b"] if _switcher["seq"] % 2 == 0 else _switcher["a"]
def _switch_state():
    return {"a": _switcher["a"], "b": _switcher["b"], "trans": _switcher["trans"], "rate": _switcher["rate"],
            "seq": _switcher["seq"], "pgm": _switch_pgm(), "pvw": _switch_pvw(),
            "sources": [{"key": k, "label": _SWITCH_LABEL[k]} for k in _SWITCH_SRCS]}
_switch_write()
TV_STATE = _RUN + '/tv-channel'
TV_FAVS = _RUN + '/tv-favorites'   # favorite channel numbers, one per line, in the order added
FEC_LOSS = _RUN + '/fec-loss'      # ST 2022-1 demo: fraction of media packets to drop
FEC_ENABLE = _RUN + '/fec-enable'  # ST 2022-1 demo: 1 = FEC flows delivered, 0 = gated off
SPS_A = _RUN + '/sps-a'            # ST 2022-7 demo: 1 = path A delivered, 0 = 'cable pulled'
SPS_B = _RUN + '/sps-b'

def _read_favs():
    try:
        return [l.strip() for l in open(TV_FAVS) if l.strip()]
    except Exception:
        return []

def _write_favs(favs):
    with open(TV_FAVS, "w") as f:
        f.write("".join(n + "\n" for n in favs))

def tv_lineup():
    """The HDHomeRun lineup + the currently-tuned channel + favorites, for the panel grid."""
    def _fetch():
        with urllib.request.urlopen(f"http://{HDHR_HOST}/lineup.json", timeout=6) as r:
            return json.load(r)
    try:
        d = _fetch()
    except Exception:
        _hdhr_rediscover()          # tuner may have moved; re-find it by DeviceID and retry once
        try:
            d = _fetch()
        except Exception:
            d = []
    chans = [{"num": c.get("GuideNumber", ""), "name": c.get("GuideName", ""), "hd": bool(c.get("HD"))} for c in d]
    try:
        cur = open(TV_STATE).read().strip()
    except Exception:
        cur = ""
    favs = _read_favs()
    favset = set(favs)
    for c in chans:
        c["fav"] = c["num"] in favset
    byn = {c["num"]: c for c in chans}
    # favorites in saved order, enriched with name/hd from the lineup (blank if no longer listed)
    favorites = [{"num": n, "name": byn.get(n, {}).get("name", ""), "hd": byn.get(n, {}).get("hd", False)} for n in favs]
    return json.dumps({"channels": chans, "current": cur, "favorites": favorites}).encode()

def tv_set(ch):
    """Write the tv-channel state file that tv-send.sh watches -> retunes the Live TV tile."""
    try:
        with open(TV_STATE, "w") as f:
            f.write(ch.strip())
        return json.dumps({"channel": ch}).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()

def sps_state():
    """ST 2022-7 path switches. Both default to up."""
    def rd(path):
        try:
            return int(float(open(path).read().strip()) >= 0.5)
        except Exception:
            return 1
    return json.dumps({"a": rd(SPS_A), "b": rd(SPS_B)}).encode()

def sps_set(path, up):
    f = SPS_A if path == "a" else SPS_B if path == "b" else None
    if f is None:
        return json.dumps({"error": "bad path"}).encode()
    try:
        with open(f, "w") as fh:
            fh.write("1" if str(up) in ("1", "true", "on") else "0")
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    return sps_state()

def fec_state():
    """Current ST 2022-1 demo knobs, for the panel controls."""
    def rd(path, default):
        try:
            return float(open(path).read().strip())
        except Exception:
            return default
    return json.dumps({"loss": rd(FEC_LOSS, 0.0), "enable": int(rd(FEC_ENABLE, 1.0) >= 0.5)}).encode()

def fec_set(loss, enable):
    """Write the knob files meter-view polls (applied live, no restart)."""
    try:
        if loss is not None:
            with open(FEC_LOSS, "w") as f:
                f.write(f"{max(0.0, min(1.0, float(loss))):.4f}")
        if enable is not None:
            with open(FEC_ENABLE, "w") as f:
                f.write("1" if str(enable) in ("1", "true", "on") else "0")
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    return fec_state()

def tv_fav(ch, on):
    """Add (on) or remove (off) a channel from the favorites file; returns the updated list."""
    ch = (ch or "").strip()
    if not ch:
        return json.dumps({"error": "no channel"}).encode()
    favs = _read_favs()
    if on and ch not in favs:
        favs.append(ch)
    elif not on:
        favs = [f for f in favs if f != ch]
    try:
        _write_favs(favs)
        return json.dumps({"favorites": favs}).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()

# --------------------------------- page ----------------------------------
PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Atoll</title>
<style>
 html,body{margin:0;min-height:100%;background:#000;color:#0f0;
   font-family:'Courier New',monospace;-webkit-user-select:none}
 #top{position:sticky;top:0;background:#000;padding:1.2vh 0 1vh;text-align:center;
   border-bottom:1px solid #131;z-index:5}
 #brand{font-weight:bold;letter-spacing:.42em;color:#0f0;font-size:min(3vw,3.4vh);
   line-height:1;text-shadow:0 0 14px #0a0;margin-bottom:.4vh}
 #brand span{display:block;letter-spacing:.16em;color:#5a5;font-weight:normal;
   font-size:min(1.5vw,1.8vh);margin-top:.35vh}
 #tc{font-size:min(11vw,12vh);font-weight:bold;line-height:1;text-shadow:0 0 18px #0f0}
 #ctrl{margin-top:1vh;display:flex;flex-wrap:wrap;justify-content:center}
 button{font-family:inherit;font-size:min(3.2vw,3.6vh);margin:.5vh .5vw;padding:.5em 1em;
   background:#111;color:#0f0;border:1px solid #0a0;border-radius:8px}
 button.on{background:#0a0;color:#000;font-weight:bold;box-shadow:0 0 16px #0a0}
 button:active{transform:scale(.96)}
 #info{color:#777;font-size:min(1.9vw,2.2vh);margin-top:1vh;line-height:1.5}
 .gm{color:#fc0;font-weight:bold}
 #finfo{font-size:min(1.7vw,2vh);margin-top:.4vh;line-height:1.4;color:#777}
 #loud{font-size:min(1.7vw,2vh);margin-top:.4vh;line-height:1.4;color:#777}
 #fresync{margin-top:.6vh} #resyncbtn{font-size:min(1.6vw,1.9vh);padding:.35em .8em;cursor:pointer}
 .flock{color:#3c9;font-weight:bold} .fwarn{color:#fc0;font-weight:bold} .foff{color:#c84;font-weight:bold}
 #lay{margin-top:1vh;display:flex;flex-wrap:wrap;align-items:center;justify-content:center}
 #lay button{font-size:min(2.6vw,3vh);padding:.4em .8em;margin:.4vh .4vw}
 #avsync{display:flex;align-items:center;gap:.7vw;margin-top:.8vh;width:min(60vw,70vh)}
 #anc{display:flex;align-items:center;gap:.7vw;margin-top:.8vh;flex-wrap:wrap;justify-content:center}
 #anc button{font-size:min(1.9vw,2.2vh);padding:.4em .7em;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px}
 #anc button.on{background:#093;color:#000;border-color:#0f0;font-weight:bold}
 #anc #anctc{color:#6cba90;font-variant-numeric:tabular-nums;font-size:min(1.8vw,2.1vh)}
 #recwrap{width:min(70vw,80vh);display:flex;flex-direction:column;gap:.6vh;align-items:center}
 .recrow{display:flex;align-items:center;gap:.6vw;flex-wrap:wrap;justify-content:center}
 .recrow button,#cliplist button,#playrow button{font-size:min(1.7vw,2vh);padding:.35em .7em;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px}
 #recstopb.on{background:#c22;color:#fff;border-color:#f55;font-weight:bold}
 #recstat{color:#9c9;font-variant-numeric:tabular-nums}
 #cliplist{display:flex;flex-direction:column;gap:.35vh;width:100%}
 .clip{display:flex;align-items:center;gap:.5vw;font-size:min(1.5vw,1.8vh);color:#9c9;background:#0a1410;border:1px solid #12352a;border-radius:6px;padding:.3em .6em}
 .clip .nm{flex:1;text-align:left;font-variant-numeric:tabular-nums}
 .clip.playing{border-color:#0f0;color:#cfc}
 #playrow{display:flex;align-items:center;gap:.6vw}
 #playrow.hide{display:none}
 #avsync .avlbl{color:#6cba90;font-size:min(1.5vw,1.8vh);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}
 #avslider{flex:1;height:2.4vh}
 #avsync .avval{color:#9c9;font-size:min(1.7vw,2vh);min-width:5ch;text-align:right;font-variant-numeric:tabular-nums}
 #authwrap{display:flex;flex-direction:column;gap:.7vh}
 #authwrap .recrow{display:flex;align-items:center;gap:.7vw}
 #authbtn.on{background:#1e6b3d;border-color:#2c9c58;color:#eafff0}
 #authres b{color:#9fd;font-variant-numeric:tabular-nums}
 #authkid{font-size:min(1.2vw,1.5vh);color:#789;word-break:break-all;font-family:ui-monospace,monospace}
 #ccdelay{display:flex;align-items:center;gap:.7vw;margin-top:.6vh;width:min(60vw,70vh)}
 #ccdelay .avlbl{color:#6cba90;font-size:min(1.5vw,1.8vh);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}
 #ccdelsl{flex:1;height:2.4vh}
 #ccdelay .avval{color:#9c9;font-size:min(1.7vw,2vh);min-width:5ch;text-align:right;font-variant-numeric:tabular-nums}
 #progwrap{margin-top:.6vh;display:flex;flex-wrap:wrap;align-items:center;justify-content:center}
 #progbtns{display:flex;flex-wrap:wrap;justify-content:center;margin-left:.6vw}
 #progbtns button{font-size:min(2vw,2.3vh);padding:.35em .7em;margin:.3vh .3vw;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px}
 #progbtns button.on{background:#093;color:#000;border-color:#0f0;font-weight:bold}
 #switchwrap{margin-top:.6vh;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:.5vw}
 .sw-pgm{color:#f77;font-weight:700;font-size:min(2vw,2.3vh)}
 .sw-lbl{color:#7c9;font-size:min(1.6vw,1.9vh);letter-spacing:.08em}
 #sw-pvw{display:flex;flex-wrap:wrap}
 #sw-pvw button{font-size:min(1.9vw,2.2vh);padding:.35em .7em;margin:.3vh .3vw;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px}
 #sw-pvw button.on{background:#093;color:#000;border-color:#0f0;font-weight:bold}
 #sw-trans{font-size:min(1.8vw,2.1vh);padding:.35em .7em;background:#141410;border:1px solid #3a3a1a;color:#cc9;border-radius:6px}
 .sw-take{font-size:min(2.2vw,2.6vh);padding:.4em 1.1em;margin-left:.4vw;background:#c22;color:#fff;font-weight:800;border:1px solid #f55;border-radius:6px;letter-spacing:.06em}
 .sw-take:active{background:#f33}
 #amapwrap{margin-top:.6vh;display:flex;flex-wrap:wrap;align-items:center;justify-content:center}
 #amapwrap button{font-size:min(2vw,2.3vh);padding:.35em .7em;margin:.3vh .3vw;background:#0a1014;border:1px solid #1a2a3a;color:#9cc;border-radius:6px}
 #amapwrap button.on{background:#39c;color:#000;border-color:#0cf;font-weight:bold}
 #schedtog{font-size:min(1.8vw,2.1vh);padding:.35em .7em;margin:.3vh .5vw;background:#141410;border:1px solid #3a3a1a;color:#cc9;border-radius:6px}
 #schedtog.on{background:#fc0;color:#000;border-color:#fc0;font-weight:bold}
 #progpend{font-size:min(1.8vw,2.1vh);color:#fc0;margin-left:.5vw;font-weight:bold}
 .l2{color:#5a5;font-size:min(1.7vw,2vh);letter-spacing:.12em;margin-right:.6vw}
 .grp{margin-top:1.3vh;padding-top:.7vh;border-top:1px solid #12352a;display:flex;flex-direction:column;align-items:center;width:100%}
 .scorelink{display:inline-block;margin-top:.7vh;color:#6cf;text-decoration:none;font-size:min(1.6vw,1.9vh);border:1px solid #245;border-radius:6px;padding:.3em .7em;background:#0a1622}
 .scorelink:active{background:#123}
 .grphdr[data-tip]{cursor:help}
 .grphdr[data-tip]::after{content:" \2139";color:#6cf;font-size:.8em;opacity:.75;font-weight:normal}
 #tip{position:fixed;max-width:min(82vw,360px);background:#0b1a24;color:#d3e8f6;border:1px solid #2b5068;border-radius:8px;padding:.55em .75em;font-size:min(1.55vw,1.9vh);line-height:1.42;box-shadow:0 6px 22px rgba(0,0,0,.55);z-index:60;pointer-events:none;opacity:0;display:none}
 #tip.on{opacity:1;display:block}
 .grphdr{color:#6cba90;font-size:min(1.55vw,1.85vh);letter-spacing:.14em;text-transform:uppercase;font-weight:600;margin-bottom:.35vh;opacity:.85}
 .modegrp.modehide{display:none}
 #music{margin-top:.8vh;display:flex;flex-wrap:wrap;align-items:center;justify-content:center}
 #music button{font-size:min(3vw,3.4vh);padding:.3em .7em;margin:.3vh .4vw}
 #mnp{color:#9c9;font-size:min(1.9vw,2.2vh);margin-left:.8vw;max-width:62vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 #slotwrap{margin-top:.8vh;display:flex;flex-direction:column;align-items:center}
 #slots{display:grid;grid-template-columns:repeat(2,1fr);gap:.5vh .6vw;width:min(44vw,50vh);margin-top:.5vh}
 #slots .slot{font-size:min(2vw,2.4vh);padding:.7em .3em;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px}
 #slots .slot.sel{background:#093;color:#000;border-color:#0f0;font-weight:bold}
 #fecwrap{padding:.3vh 0;display:flex;align-items:center;justify-content:center;gap:.6vw;flex-wrap:wrap}
 #fecwrap label{font-size:min(1.6vw,1.9vh);color:#7a7}
 .sub2{font-size:min(1.4vw,1.7vh);color:#575}
 .pathbtn{font-size:min(1.7vw,2vh);padding:.4em 1.1em;border-radius:6px;border:1px solid #1a3a2a;background:#093;color:#000;font-weight:bold;cursor:pointer}
 .pathbtn.down{background:#3a1010;border-color:#803;color:#f88}
 #fecloss{width:min(26vw,30vh);vertical-align:middle}
 #fecval{font-size:min(1.6vw,1.9vh);color:#3c9;min-width:3.2em;display:inline-block;font-variant-numeric:tabular-nums}
 #fectog{font-size:min(1.7vw,2vh);padding:.4em .9em;border-radius:6px;border:1px solid #1a3a2a;background:#0a1410;color:#9c9;cursor:pointer}
 #fectog.on{background:#093;color:#000;border-color:#0f0;font-weight:bold}
 #tvwrap{padding:.3vh 0}
 #tvfav{display:flex;flex-wrap:wrap;gap:.4vh .4vw;justify-content:center;margin-bottom:.5vh}
 #tvfav:empty{display:none}
 .tvfav{font-size:min(1.7vw,2vh);padding:.5em .8em;background:#141006;border:1px solid #3a2e1a;color:#eca85a;border-radius:6px;cursor:pointer;font-weight:bold}
 .tvfav.sel{background:#093;color:#000;border-color:#0f0}
 #tvtoggle{font-size:min(1.9vw,2.2vh);padding:.5em 1em;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px}
 #tvchan{display:none;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.4vh .4vw;margin-top:.5vh;max-height:34vh;overflow-y:auto}
 .tvrow{display:flex;gap:.25vw}
 .tvrow .tvch{flex:1;min-width:0}
 .tvch{font-size:min(1.7vw,2vh);padding:.5em .4em;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:6px;text-align:left;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tvch.sel{background:#093;color:#000;border-color:#0f0;font-weight:bold}
 .star{flex:none;font-size:min(1.7vw,2vh);padding:.5em .55em;background:#0a1410;border:1px solid #1a3a2a;color:#555;border-radius:6px;cursor:pointer}
 .star.on{color:#fc0;border-color:#3a2e1a}
 #slothint{margin-top:.5vh;font-size:min(1.7vw,2vh)}
 #nmos{padding:1.4vh 2vw 4vh}
 h2{color:#0a0;font-size:2.1vh;margin:2.2vh 0 .6vh;border-bottom:1px solid #131;padding-bottom:.3vh;
   letter-spacing:.12em}
 table{width:100%;border-collapse:collapse;font-size:1.75vh}
 th,td{text-align:left;padding:.45vh .6vw;border-bottom:1px solid #0c1c0c;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis;max-width:34vw}
 th{color:#5a5;font-weight:normal;font-size:1.5vh;text-transform:uppercase;letter-spacing:.08em}
 tr.sw td{background:#06140a}
 tr.sw td:first-child{border-left:3px solid #0f0}
 .on-dot{color:#0f0}.off-dot{color:#633}
 .mut{color:#666}.k{color:#3c9}.warn{color:#fc0}
 .pill{display:inline-block;background:#0a0;color:#000;font-weight:bold;border-radius:4px;padding:0 .4em;font-size:1.4vh}
 #meta{color:#555;font-size:1.6vh;margin-top:.6vh}
 tr.clk{cursor:pointer}
 tr.clk:active td{background:#093;color:#000}
 #ov{position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:20;display:none;
   flex-direction:column;padding:2vh 2.5vw}
 #ovbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1vh}
 #ovbar b{color:#3c9;font-size:2vh;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 #ovpre{flex:1;overflow:auto;color:#0f0;font-size:1.7vh;white-space:pre;line-height:1.4;
   border:1px solid #131;padding:1vh;-webkit-overflow-scrolling:touch}
 #demo{margin:.4vh auto;text-align:center}
 #demobtn{font-size:min(2.4vw,2.8vh);padding:.45em 1.1em;background:#20304a;border:1px solid #4a6ea0;color:#cfe;border-radius:8px;font-weight:bold}
 #demobtn.on{background:#a33;border-color:#f66;color:#fff}
 #democap{display:none;position:fixed;left:0;right:0;bottom:0;z-index:50;background:rgba(0,0,0,.85);color:#fff;font-size:min(3vw,3.4vh);line-height:1.35;padding:.7em 1.2em;text-align:center}
</style></head>
<body>
  <div id="democap"></div>
 <div id="top">
  <div id="brand">ATOLL<span>ST&nbsp;2110 &middot; NMOS island monitor &middot; <a href="#" id="anlink" style="color:#3c9;text-decoration:none">analyser &#8599;</a></span></div>
  <div id="tc">--:--:--:--</div>
  <div id="info"></div>
  <div id="finfo"></div>
  <div id="loud"></div>

  <section class="grp"><div class="grphdr" data-tip="Pick a live source and take it to the output - a real AMWA IS-05 connection; the on-air tally follows over IS-07.">Sources &middot; take to output</div>
  <div id="ctrl">
   <button id="bjxs" onclick="take('jxs',this)">Home videos</button>
   <button id="braw" onclick="take('raw',this)">Pi raw 2110-20</button>
   <button id="bhevc" onclick="take('hevc',this)">Live TV</button>
   <button id="bmusic" onclick="take('music',this)">Music</button>
   <button id="breels" onclick="take('reels',this)">Test Reels</button>
   <button id="bjpegxs" onclick="take('jpegxs',this)">JPEG XS</button>
   <button id="bj2k" onclick="take('j2k',this)">JPEG 2000</button>
   <button id="bh264" onclick="take('h264',this)">H.264 RTP</button>
   <button id="bmjpeg" onclick="take('mjpeg',this)">MJPEG RTP</button>
   <button id="bvp9" onclick="take('vp9',this)">VP9 RTP</button>
   <button id="btsrtp" onclick="take('tsrtp',this)">TS over RTP</button>
   <button id="bfec" onclick="take('fec',this)">ST 2022-1 FEC</button>
   <button id="bsps" onclick="take('sps',this)">ST 2022-7 SPS</button>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="How Monitor 2 renders: single, side-by-side, 2x2 multiview, wall, Program Out, the switcher, or the JPEG XS 2110-22 view.">Output &middot; Monitor 2</div>
  <div id="lay">
   <button id="lsingle" onclick="setLayout('single')">Follow take</button>
   <button id="lside" onclick="setLayout('side')">Side &times; 2</button>
   <button id="lwall" onclick="setLayout('wall')">Wall +tally</button>
   <button id="lmulti" onclick="setLayout('multi')">Multiview</button>
   <button id="lprogram" onclick="setLayout('program')">&#127909; Program Out</button>
   <button id="lswitcher" onclick="setLayout('switcher')">&#127899; Switcher</button>
   <button id="ljxs" onclick="setLayout('jxs')">JPEG XS 2110-22</button>
  </div>
  <div id="anc">
   <span class="avlbl">Ancillary &middot; ST 2110-40</span>
   <button id="ccbtn" onclick="ccToggle()">CC: off</button>
   <button id="ccsrcbtn" onclick="ccSource()">src: synthetic</button>
   <button id="scbtn" onclick="ccScte()">Trigger AD break</button>
   <span id="anctc" class="avval">--:--:--:--</span>
  </div>
  <div id="ccdelay">
   <span class="avlbl">Caption delay</span>
   <input type="range" id="ccdelsl" min="0" max="15000" step="250" value="7000" oninput="ccDelayInput(this.value)">
   <span id="ccdelval" class="avval">7.0 s</span>
  </div>
  <div id="avsync">
   <span class="avlbl">A/V sync</span>
   <input type="range" id="avslider" min="-100" max="300" step="5" value="30" oninput="avsyncInput(this.value)">
   <span id="avval" class="avval">30 ms</span>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="Record a source's live multicast to a timestamped file, then replay it PCR-paced as just another source.">Record &amp; Playback</div>
  <div id="recwrap">
   <div class="recrow">
    <button id="recgo" onclick="recStart()">&#9679; Record current source</button>
    <button id="recstopb" onclick="recStop()">&#9632; Stop</button>
    <span id="recstat" class="avval">idle</span>
   </div>
   <div id="cliplist"></div>
   <div id="playrow"><span id="playstat" class="mut">&mdash;</span><button id="playstopb" onclick="playStop()">Stop playback</button></div>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="OAuth2 / JWT security. With enforcement on, Program Out needs a signed bearer token to accept an IS-05 route.">IS-10 Authorization &middot; BCP-003-02</div>
  <div id="authwrap">
   <div class="recrow">
    <button id="authbtn" onclick="authToggle()">Enforcement: off</button>
    <span id="authas" class="avval">AS &mdash;</span>
   </div>
   <div class="recrow">
    <button id="authdemo" onclick="authDemo()">Test token enforcement</button>
    <span id="authres" class="mut">&mdash;</span>
   </div>
   <div id="authkid" class="mut">&mdash;</div>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="IS-11 keeps senders and receivers matched: constrain a sender and its flow retunes; manage EDID, the HDMI-style capability handshake.">Stream Compatibility &middot; IS-11</div>
  <div id="is11wrap">
   <div class="recrow">
    <span class="avlbl">Sender</span> <span id="is11status" class="avval">&mdash;</span>
    <span class="avlbl">Flow rate</span> <span id="is11rate" class="avval">&mdash;</span>
   </div>
   <div class="recrow">
    <button id="is11con" onclick="is11Constrain()">Constrain to 50 fps</button>
    <button id="is11unc" onclick="is11Unconstrain()">Clear constraint</button>
   </div>
   <div class="recrow">
    <span class="avlbl">Base EDID</span> <span id="is11edid" class="avval">&mdash;</span>
    <button id="is11eload" onclick="is11Edid(1)">Load EDID</button>
    <button id="is11eclr" onclick="is11Edid(0)">Clear EDID</button>
   </div>
   <div id="is11eff" class="mut">&mdash;</div>
   <a class="scorelink" href="https://claude.ai/code/artifact/56cef03c-ceed-495e-b1ae-a328afba1dab" target="_blank" rel="noopener">Conformance scorecard &#8599;</a>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="IS-12 is the modern NMOS control plane over WebSocket, exposing the device's MS-05 object model - managers, classes and datatypes.">Device Control &middot; IS-12</div>
  <div id="is12wrap">
   <div class="recrow"><span class="avlbl">Control node</span> <span id="is12up" class="avval">&mdash;</span></div>
   <div id="is12model" class="mut">&mdash;</div>
  </div>
  </section>
  <section class="grp modegrp modehide" data-mode="switcher"><div class="grphdr" data-tip="PROGRAM / PREVIEW with cut or dissolve takes; the audio crossfades in step with the picture.">Production Switcher</div>
  <div id="switchwrap">
   <span id="sw-pgm" class="sw-pgm">PROGRAM &middot; &mdash;</span>
   <span class="sw-lbl">PREVIEW</span>
   <div id="sw-pvw"></div>
   <button id="sw-trans" onclick="toggleSwTrans()">Dissolve</button>
   <button id="sw-take" class="sw-take" onclick="swTake()">TAKE</button>
  </div>
  </section>

  <section class="grp modegrp modehide" data-mode="program"><div class="grphdr" data-tip="Route any flow onto the Program Out software receiver over IS-05 - the picture follows the connection.">Program Out &middot; IS-05 route</div>
  <div id="progwrap">
   <div id="progbtns"></div>
   <button id="schedtog" onclick="toggleSched(this)">&#9201; Schedule +5s: off</button>
   <span id="progpend"></span>
  </div>
  </section>

  <section class="grp modegrp modehide" data-mode="multi wall side"><div class="grphdr" data-tip="Assign any source to each quadrant of the 2x2 multiview, or to the two side-by-side panes.">Multiview / side tiles</div>
  <div id="slotwrap">
   <div id="slots">
    <button class="slot" id="slot0" onclick="selSlotFn(0)">&mdash;</button>
    <button class="slot" id="slot1" onclick="selSlotFn(1)">&mdash;</button>
    <button class="slot" id="slot2" onclick="selSlotFn(2)">&mdash;</button>
    <button class="slot" id="slot3" onclick="selSlotFn(3)">&mdash;</button>
   </div>
   <span id="slothint" class="mut">tap a tile, then a source</span>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="The music channel - HEVC video plus ST 2110-30 L24 audio, with IS-08 channel mapping (swap / mono / mute).">Music</div>
  <div id="music">
   <button onclick="music('prev')" title="previous">&#9198;</button>
   <button id="mpp" onclick="music('playpause')" title="play/pause">&#9208;</button>
   <button onclick="music('next')" title="next">&#9197;</button>
   <button id="mshuf" onclick="music('shuffle')" title="shuffle">&#128256;</button>
   <span id="mnp">&mdash;</span>
  </div>
  <div id="amapwrap">
   <span class="sw-lbl">IS-08 channel map</span>
   <button data-amap="stereo" onclick="setAudiomap('stereo')">Stereo</button>
   <button data-amap="swap" onclick="setAudiomap('swap')">Swap L&harr;R</button>
   <button data-amap="monoL" onclick="setAudiomap('monoL')">Mono (L)</button>
   <button data-amap="muteR" onclick="setAudiomap('muteR')">Mute R</button>
  </div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="Network resilience: ST 2022-1 FEC reconstructs lost packets; ST 2022-7 carries the stream on two paths, hitless.">Resilience &middot; ST 2022</div>
  <div id="fecwrap" style="gap:.6vw"><label>ST&nbsp;2022-7 paths</label><button id="spsa" class="pathbtn" onclick="spsToggle('a')">A</button><button id="spsb" class="pathbtn" onclick="spsToggle('b')">B</button><span class="sub2">pull a path &mdash; the picture must not flinch</span></div>
  <div id="fecwrap"><label>ST&nbsp;2022-1 loss</label><input id="fecloss" type="range" min="0" max="10" step="0.5" value="0" oninput="fecLoss(this.value)"><span id="fecval">0.0%</span><button id="fectog" onclick="fecToggle()">FEC</button></div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="Real broadcast channels from the HDHomeRun tuner; a channel change opens the new channel on a second tuner first, then cuts - no black frame.">Live TV</div>
  <div id="tvwrap"><div id="tvfav"></div><button id="tvtoggle" onclick="toggleTv()">&#128250; TV Channels</button><div id="tvchan"></div></div>
  </section>

  <section class="grp"><div class="grphdr" data-tip="Run the guided, captioned tour of the whole rig, plus other demo helpers.">Demo &middot; tools</div>
  <div id="demo"><button id="demobtn" onclick="runDemo()">&#9654; Guided demo</button></div>
  <div id="fresync"><button id="resyncbtn" onclick="resyncFollower(this)">&#8635; Re-sync follower</button></div>
  </section>
 </div>
 <div id="nmos">loading IS-04/IS-05&hellip;</div>
 <div id="ov"><div id="ovbar"><b id="ovttl">resource</b><button onclick="document.getElementById('ov').style.display='none'">&times; close</button></div><pre id="ovpre"></pre></div>
<script>
const FPS=__FPS__;
let offset=0, ptp={}, synced=false;
const BTN={jxs:'bjxs',raw:'braw',hevc:'bhevc',music:'bmusic',reels:'breels'};
const SRCLABEL={jxs:'Home',raw:'Pi raw',hevc:"Live TV",music:'Music',reels:'Test Reels',jpegxs:'JPEG XS',j2k:'JPEG 2000',h264:'H.264 RTP',mjpeg:'MJPEG RTP',vp9:'VP9 RTP',tsrtp:'TS/RTP',fec:'FEC',sps:'2022-7'};
let selSlot=null, curSlots=['hevc','raw','jxs','music'], curLayout='wall';
function selSlotFn(i){ selSlot=(selSlot===i)?null:i; renderSlots(); }
function renderSlots(){
  const side=(curLayout==='side');
  for(let i=0;i<4;i++){ const b=document.getElementById('slot'+i); if(!b) continue;
    if(side && i>=2){ b.style.display='none'; continue; } b.style.display='';
    const lbl = side ? (i===0?'\u25e7 Left':'Right \u25e8') : ((i+1)+'');
    b.textContent = lbl+': '+(SRCLABEL[curSlots[i]]||curSlots[i]||'\u2014');
    b.classList.toggle('sel',selSlot===i); }
  const h=document.getElementById('slothint');
  if(h) h.textContent = selSlot!=null
    ? ('now tap a source for '+(side?(selSlot===0?'the LEFT pane':'the RIGHT pane'):('tile '+(selSlot+1))))
    : (side?'tap Left or Right, then a source':'tap a tile, then a source');
}
async function assignSlot(pos,src){
  try{const r=await fetch('/slot?pos='+pos+'&src='+src,{cache:'no-store'});const d=await r.json();
      if(d.slots) curSlots=d.slots.split(',');}catch(e){}
  selSlot=null; renderSlots();
}
function highlight(src){
  document.querySelectorAll('#ctrl button').forEach(b=>b.classList.remove('on'));
  const b=document.getElementById(BTN[src]); if(b) b.classList.add('on');
}
async function take(src,btn){
  if(selSlot!=null){ assignSlot(selSlot,src); return; }   // a tile is selected -> place this source there
  highlight(src);
  try{const r=await fetch('/take?src='+src,{cache:'no-store'});const d=await r.json();
      if(d.error){ btn.textContent+=' !'; } }catch(e){}
  loadNmos();
}
async function music(action){
  try{await fetch('/music/'+action,{cache:'no-store'});}catch(e){}
  setTimeout(musicState,350);
}
async function spsRefresh(){try{const d=await(await fetch('/sps/state',{cache:'no-store'})).json();
  for(const k of ['a','b']){const b=document.getElementById('sps'+k);
    const up=!!d[k]; b.classList.toggle('down',!up); b.textContent=k.toUpperCase()+(up?'':' DOWN');}
}catch(e){}}
async function spsToggle(k){const b=document.getElementById('sps'+k);const up=b.classList.contains('down');
  try{await fetch('/sps/set?path='+k+'&up='+(up?1:0),{cache:'no-store'});}catch(e){}spsRefresh();}
let fecOn=true;
async function fecRefresh(){try{const r=await fetch('/fec/state',{cache:'no-store'});const d=await r.json();
  fecOn=!!d.enable; const pct=(d.loss*100);
  const sl=document.getElementById('fecloss'); if(sl&&document.activeElement!==sl) sl.value=pct;
  document.getElementById('fecval').textContent=pct.toFixed(1)+'%';
  const b=document.getElementById('fectog'); b.classList.toggle('on',fecOn); b.textContent=fecOn?'FEC ON':'FEC OFF';
}catch(e){}}
async function fecLoss(v){document.getElementById('fecval').textContent=Number(v).toFixed(1)+'%';
  try{await fetch('/fec/set?loss='+(v/100),{cache:'no-store'});}catch(e){}}
async function fecToggle(){fecOn=!fecOn;
  try{await fetch('/fec/set?enable='+(fecOn?1:0),{cache:'no-store'});}catch(e){}fecRefresh();}
(function(){var a=document.getElementById('anlink');
 if(a) a.href=location.protocol+'//'+location.hostname+':8101/';})();
let tvOpen=false;
async function loadTv(){try{const r=await fetch('/tv/lineup',{cache:'no-store'});const d=await r.json();
const fv=document.getElementById('tvfav');
fv.innerHTML=(d.favorites||[]).map(c=>'<button class="tvfav'+(c.num===d.current?' sel':'')+'" data-ch="'+c.num+'">'+c.num+(c.name?' '+c.name:'')+'</button>').join('');
fv.querySelectorAll('.tvfav').forEach(b=>b.onclick=function(){tvPick(b.getAttribute('data-ch'));});
const g=document.getElementById('tvchan');
g.innerHTML=(d.channels||[]).map(c=>'<div class="tvrow"><button class="tvch'+(c.num===d.current?' sel':'')+'" data-ch="'+c.num+'">'+c.num+' '+c.name+(c.hd?' HD':'')+'</button><button class="star'+(c.fav?' on':'')+'" data-ch="'+c.num+'" data-on="'+(c.fav?'0':'1')+'">'+(c.fav?'&#9733;':'&#9734;')+'</button></div>').join('');
g.querySelectorAll('.tvch').forEach(b=>b.onclick=function(){tvPick(b.getAttribute('data-ch'));});
g.querySelectorAll('.star').forEach(b=>b.onclick=function(){tvFav(b.getAttribute('data-ch'),b.getAttribute('data-on'));});
}catch(e){}}
function toggleTv(){tvOpen=!tvOpen;document.getElementById('tvchan').style.display=tvOpen?'grid':'none';loadTv();}
async function tvPick(ch){try{await fetch('/tv/set?ch='+encodeURIComponent(ch),{cache:'no-store'});}catch(e){}setTimeout(loadTv,400);}
async function tvFav(ch,on){try{await fetch('/tv/fav?ch='+encodeURIComponent(ch)+'&on='+on,{cache:'no-store'});}catch(e){}loadTv();}
async function loadAudiomap(){
  try{
    const d=await(await fetch("/audiomap/state",{cache:"no-store"})).json();
    const cur=d.preset;
    document.querySelectorAll("#amapwrap button").forEach(b=>b.classList.toggle("on", b.getAttribute("data-amap")===cur));
  }catch(e){}
}
async function setAudiomap(preset){
  try{await fetch("/audiomap/set?preset="+encodeURIComponent(preset),{cache:"no-store"});}catch(e){}
  setTimeout(loadAudiomap,300);
}
async function musicState(){
  const np=document.getElementById('mnp');
  try{const r=await fetch('/music/state',{cache:'no-store'});const d=await r.json();
      if(d.error){np.textContent='(offline)';return;}
      np.textContent=(d.title||'\\u2014')+(d.artist?(' \\u2014 '+d.artist):'');
      const pp=document.getElementById('mpp'); if(pp) pp.innerHTML=d.playing?'&#9208;':'&#9654;';
      const sh=document.getElementById('mshuf'); if(sh) sh.classList.toggle('on',!!d.shuffle);
  }catch(e){np.textContent='(offline)';}
}
const LAYBTN={single:'lsingle',side:'lside',multi:'lmulti',wall:'lwall',program:'lprogram',switcher:'lswitcher',jxs:'ljxs'};
async function loadProgramOut(){
  try{
    const d=await(await fetch("/programout/state",{cache:"no-store"})).json();
    const box=document.getElementById("progbtns"); if(!box) return;
    const cat=d.catalog||{}; const cur=d.essence;
    let html="";
    for(const k of Object.keys(cat)){
      html+=`<button data-ess="${k}" class="${(k===cur)?'on':''}">${cat[k].label}</button>`;
    }
    html+=`<button data-ess="none" class="${(!cur)?'on':''}">Clear</button>`;
    box.innerHTML=html;
    box.querySelectorAll("button").forEach(b=>b.onclick=function(){routeProgram(b.getAttribute("data-ess"));});
    const pend=document.getElementById("progpend");
    if(pend) pend.textContent = d.pending ? "\u23F3 scheduled activation pending\u2026" : "";
  }catch(e){}
}
let SCHED=0;
function toggleSched(btn){
  SCHED = SCHED ? 0 : 5;
  btn.textContent = SCHED ? ('\u23F1 Schedule +'+SCHED+'s: ON') : '\u23F1 Schedule +5s: off';
  btn.classList.toggle('on', !!SCHED);
}
async function routeProgram(ess){
  const q = "/programout/route?essence="+encodeURIComponent(ess)+(SCHED?("&secs="+SCHED):"");
  try{await fetch(q,{cache:"no-store"});}catch(e){}
  setTimeout(loadProgramOut,300);
}
function hlLayout(m){ document.querySelectorAll('#lay button').forEach(b=>b.classList.remove('on')); const b=document.getElementById(LAYBTN[m]); if(b) b.classList.add('on'); document.querySelectorAll('.modegrp').forEach(function(g){ var md=(g.getAttribute('data-mode')||'').split(' '); g.classList.toggle('modehide', md.indexOf(m)<0); }); curLayout=m; try{renderSlots();}catch(e){} }
let SW={trans:'dissolve',rate:1.0};
function swLabelOf(d,k){const s=d.sources.find(x=>x.key===k);return s?s.label:k;}
async function loadSwitcher(){
  try{
    const d=await(await fetch('/switcher/state',{cache:'no-store'})).json();
    SW.trans=d.trans; SW.rate=d.rate;
    document.getElementById('sw-pgm').innerHTML='PROGRAM \u00b7 '+esc(swLabelOf(d,d.pgm));
    const box=document.getElementById('sw-pvw');
    box.innerHTML=d.sources.map(function(x){return '<button data-sw="'+x.key+'" class="'+(x.key===d.pvw?'on':'')+'">'+esc(x.label)+'</button>';}).join('');
    box.querySelectorAll('button').forEach(function(b){b.onclick=function(){setPvw(b.getAttribute('data-sw'));};});
    document.getElementById('sw-trans').textContent = d.trans==='cut' ? 'Cut' : ('Dissolve '+Number(d.rate).toFixed(1)+'s');
  }catch(e){}
}
async function setPvw(src){ try{await fetch('/switcher/pvw?src='+encodeURIComponent(src),{cache:'no-store'});}catch(e){} setTimeout(loadSwitcher,200); }
async function swTake(){ try{await fetch('/switcher/take',{cache:'no-store'});}catch(e){} setTimeout(loadSwitcher,200); }
async function toggleSwTrans(){ const t=SW.trans==='cut'?'dissolve':'cut'; try{await fetch('/switcher/trans?type='+t+'&rate='+SW.rate,{cache:'no-store'});}catch(e){} setTimeout(loadSwitcher,200); }
async function setLayout(m){ hlLayout(m); try{await fetch('/layout?mode='+m,{cache:'no-store'});}catch(e){} }
function fmtDur(s){var m=Math.floor(s/60),x=s%60;return m+":"+(x<10?"0":"")+x;}
async function recStart(src){ try{await fetch("/rec/start"+(src?("?src="+src):""),{cache:"no-store"});}catch(e){} recPoll(); }
async function recStop(){ try{await fetch("/rec/stop",{cache:"no-store"});}catch(e){} recPoll(); }
async function playStart(f,loop){ try{await fetch("/play/start?file="+encodeURIComponent(f)+"&loop="+(loop?1:0),{cache:"no-store"});}catch(e){} recPoll(); }
async function playStop(){ try{await fetch("/play/stop",{cache:"no-store"});}catch(e){} recPoll(); }
async function recDelete(f){ try{await fetch("/rec/delete?file="+encodeURIComponent(f),{cache:"no-store"});}catch(e){} recList(); }
let _recPlaying=null;
async function recPoll(){ try{const d=await(await fetch("/rec/status",{cache:"no-store"})).json();
   const b=document.getElementById("recstopb"), st=document.getElementById("recstat");
   if(d.recording){ b.classList.add("on"); st.textContent="\u25cf REC "+(d.src||"")+"  "+fmtDur(d.secs); }
   else { b.classList.remove("on"); st.textContent="idle"; }
   _recPlaying = d.playing ? d.play_file : null;
   const pr=document.getElementById("playrow"), ps=document.getElementById("playstat");
   if(d.playing){ pr.classList.remove("hide"); ps.textContent="\u25b6 Playing "+(d.play_file||"")+(d.loop?" (loop)":"")+" \u2192 Test Reels"; }
   else { pr.classList.add("hide"); }
   recRenderPlaying();
  }catch(e){} }
function recRenderPlaying(){ document.querySelectorAll("#cliplist .clip").forEach(function(c){ c.classList.toggle("playing", c.getAttribute("data-f")===_recPlaying); }); }
async function recList(){ try{const a=await(await fetch("/rec/list",{cache:"no-store"})).json();
   const box=document.getElementById("cliplist");
   box.innerHTML = a.length? a.map(function(c){var n=esc(c.name);return '<div class="clip" data-f="'+n+'"><span class="nm">'+n+'  '+c.mb+' MB</span><button class="rpl" data-f="'+n+'">Play</button><button class="rlp" data-f="'+n+'">Loop</button><button class="rdl" data-f="'+n+'">Del</button></div>';}).join('') : '<span class="mut">no recordings yet</span>';
   box.querySelectorAll(".rpl").forEach(function(b){b.onclick=function(){playStart(b.getAttribute("data-f"),false);};});
   box.querySelectorAll(".rlp").forEach(function(b){b.onclick=function(){playStart(b.getAttribute("data-f"),true);};});
   box.querySelectorAll(".rdl").forEach(function(b){b.onclick=function(){recDelete(b.getAttribute("data-f"));};});
   recRenderPlaying();
  }catch(e){} }
setInterval(recPoll, 1000); setInterval(recList, 4000); recPoll(); recList();
let _ccOn=false;
async function ccToggle(){ _ccOn=!_ccOn; try{await fetch("/cc/set?on="+(_ccOn?1:0),{cache:"no-store"});}catch(e){} ccRender(); }
async function ccScte(){ try{await fetch("/cc/scte",{cache:"no-store"});}catch(e){} }
let _ccSrc="synthetic";
async function ccSource(){ _ccSrc = (_ccSrc==="live")?"synthetic":"live"; try{await fetch("/cc/source?mode="+_ccSrc,{cache:"no-store"});}catch(e){} ccRenderSrc(); }
function ccRenderSrc(){ const b=document.getElementById("ccsrcbtn"); if(b){ b.textContent="src: "+_ccSrc; b.classList.toggle("on",_ccSrc==="live"); } }
let _ccDelT=null;
function ccDelayInput(v){ document.getElementById("ccdelval").textContent=(v/1000).toFixed(1)+" s"; if(_ccDelT)clearTimeout(_ccDelT); _ccDelT=setTimeout(function(){ fetch("/cc/delay?ms="+v,{cache:"no-store"}).catch(function(){}); },150); }
function ccRender(){ const b=document.getElementById("ccbtn"); if(b){ b.textContent="CC: "+(_ccOn?"on":"off"); b.classList.toggle("on",_ccOn);} }
async function ccPoll(){ try{const d=await(await fetch("/cc/state",{cache:"no-store"})).json();
  _ccOn=!!d.on; ccRender(); if(d.source){_ccSrc=d.source; ccRenderSrc();} if(d.delay!==undefined){const sl=document.getElementById("ccdelsl"); if(sl&&document.activeElement!==sl){sl.value=d.delay; document.getElementById("ccdelval").textContent=(d.delay/1000).toFixed(1)+" s";}} const t=document.getElementById("anctc"); if(t&&d.tc) t.textContent=d.tc; }catch(e){} }
let _avT=null;
function avsyncInput(v){
  document.getElementById('avval').textContent = v + ' ms';
  if(_avT) clearTimeout(_avT);
  _avT = setTimeout(function(){ fetch('/avsync/set?ms='+v,{cache:'no-store'}).catch(function(){}); }, 120);
}
async function refreshState(){
  try{const r=await fetch('/state',{cache:'no-store'});const d=await r.json();
      if(d.active) highlight(d.active); if(d.layout) hlLayout(d.layout);
      if(d.slots){ curSlots=d.slots.split(','); renderSlots(); }
      if(d.video_delay!==undefined){ const sl=document.getElementById('avslider');
        if(sl && document.activeElement!==sl){ sl.value=d.video_delay; document.getElementById('avval').textContent=d.video_delay+' ms'; } } }catch(e){}
}
setInterval(ccPoll, 1000); ccPoll();
let _authOn=false;
function authApply(d){
  _authOn=!!d.enabled;
  const b=document.getElementById("authbtn"); if(b){ b.textContent="Enforcement: "+(_authOn?"ON":"off"); b.classList.toggle("on",_authOn); }
  const as=document.getElementById("authas"); if(as) as.textContent = d.as_up?"AS up":"AS down";
  const k=document.getElementById("authkid"); if(k) k.textContent = d.as_up?("issuer "+(d.issuer||"?")+"  \u00b7  kid "+(d.kid||"?")):"authorization server unreachable";
}
async function authToggle(){ try{const d=await(await fetch("/auth/set?on="+(_authOn?0:1),{cache:"no-store"})).json(); authApply(d);}catch(e){} }
async function authDemo(){
  const r=document.getElementById("authres"); if(r) r.textContent="testing\u2026";
  try{const d=await(await fetch("/auth/demo",{cache:"no-store"})).json();
    if(r) r.innerHTML = "no token \u2192 <b>"+d.without_token+"</b> &nbsp; valid token \u2192 <b>"+d.with_token+"</b>"+(d.token_preview?(" &nbsp; <span class='mut'>"+d.token_preview+"</span>"):"");
  }catch(e){ if(r) r.textContent="error"; }
}
async function authPoll(){ try{const d=await(await fetch("/auth/state",{cache:"no-store"})).json(); authApply(d);}catch(e){} }
setInterval(authPoll, 3000); authPoll();
function is11Apply(d){
  const st=document.getElementById("is11status"); if(st){ st.textContent=d.up?d.sender_status:"offline"; st.classList.toggle("on", d.sender_status==="constrained"); }
  const r=document.getElementById("is11rate"); if(r) r.textContent=d.grain_rate;
  const e=document.getElementById("is11edid"); if(e){ e.textContent=d.base_edid?"set":"none"; e.classList.toggle("on", !!d.base_edid); }
  const ef=document.getElementById("is11eff"); if(ef) ef.textContent="effective EDID "+d.effective_hash+"  \u00b7  receiver "+d.receiver_status;
}
async function is11Poll(){ try{const d=await(await fetch("/is11/state",{cache:"no-store"})).json(); is11Apply(d);}catch(e){} }
async function is11Constrain(){ try{await fetch("/is11/constrain?num=50&den=1",{cache:"no-store"});}catch(e){} is11Poll(); }
async function is11Unconstrain(){ try{await fetch("/is11/unconstrain",{cache:"no-store"});}catch(e){} is11Poll(); }
async function is11Edid(load){ try{await fetch("/is11/edid?load="+load,{cache:"no-store"});}catch(e){} is11Poll(); }
setInterval(is11Poll, 3000); is11Poll();
function is12Apply(d){
  const up=document.getElementById("is12up"); if(up){ up.textContent=d.up?"online":"offline"; up.classList.toggle("on",!!d.up); }
  const m=document.getElementById("is12model"); if(m) m.textContent=d.up?("root \u2192 "+(d.members||[]).join(" + ")+"  \u00b7  "+d.classes+" classes / "+d.datatypes+" datatypes  \u00b7  "+(d.product||"")):"control node offline";
}
async function is12Poll(){ try{const d=await(await fetch("/is12/state",{cache:"no-store"})).json(); is12Apply(d);}catch(e){} }
setInterval(is12Poll, 5000); is12Poll();
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
const dot=b=>b?'<span class="on-dot">&#9679;</span>':'<span class="off-dot">&#9675;</span>';
const sid=id=>id?esc(String(id).slice(0,8)):'<span class="mut">none</span>';
function fmtFlow(f){
  if(!f||!f.media_type) return '<span class="mut">&mdash;</span>';
  let t=esc(f.media_type);
  if(f.res) t+=' '+esc(f.res);
  if(f.rate) t+=' @'+esc(f.rate);
  if(f.sample_rate) t+=' '+esc(f.sample_rate)+'Hz';
  if(f.bit_depth) t+='/'+esc(f.bit_depth)+'b';
  return t;
}
function fmtCaps(c){
  if(!c) return '';
  let t=(c.media_types||[]).map(esc).join(',');
  if(c.res) t+=' '+esc(c.res);
  if(c.rate) t+=' @'+esc(c.rate);
  if(c.sampling) t+=' '+esc(c.sampling);
  return t;
}
function mcast(a){
  if(!a) return '<span class="mut">&mdash;</span>';
  const ip=a.multicast_ip||a.destination_ip;
  if(!ip) return '<span class="mut">unset</span>';
  let t='<span class="k">'+esc(ip)+':'+esc(a.destination_port)+'</span>';
  if(a.legs>1) t+=' <span class="mut">x'+a.legs+'</span>';
  if(a.rtp_enabled===false) t+=' <span class="warn">rtp off</span>';
  return t;
}
function renderNmos(d){
  let h='';
  // nodes
  h+='<h2>IS-04 NODES</h2><table><tr><th>node</th><th>hostname</th><th>clock</th><th>interfaces</th><th>api</th></tr>';
  for(const n of d.nodes){
    const clk=(n.clocks||[]).map(c=>esc(c.name)+':'+esc(c.ref_type)+(c.gmid?(' '+esc(c.gmid)):'')+(c.locked!=null?(c.locked?' locked':' unlocked'):'')).join(', ')||'<span class="mut">none</span>';
    const ifs=(n.interfaces||[]).map(i=>esc(i.name)+(i.mac?(' '+esc(i.mac)):'')).join(', ');
    const api=(n.api_versions||[]).slice(-1)[0]||'';
    h+='<tr class="clk" onclick="detail(\\'nodes\\',\\''+esc(n.id)+'\\')"><td><span class="k">'+esc(n.label)+'</span></td><td>'+esc(n.hostname)+'</td><td>'+clk+'</td><td>'+ifs+'</td><td>'+esc(api)+'</td></tr>';
  }
  h+='</table>';
  // receivers
  h+='<h2>IS-04/05 RECEIVERS</h2><table><tr><th>receiver</th><th>format</th><th>caps</th><th>en</th><th>group:port</th><th>from sender</th><th>transport</th></tr>';
  for(const r of d.receivers){
    const a=r.is05||{};
    const cls=r.switch?'clk sw':'clk';
    const lab=esc(r.label)+(r.switch?(' <span class="pill">'+esc(r.switch)+'</span>'):'');
    h+='<tr class="'+cls+'" onclick="detail(\\'receivers\\',\\''+esc(r.id)+'\\')"><td>'+lab+'</td><td>'+esc(r.format)+'</td><td>'+fmtCaps(r.caps)+'</td><td>'+dot(a.master_enable)+'</td><td>'+mcast(a)+'</td><td>'+(a.sender_id?sid(a.sender_id):(r.subscription&&r.subscription.sender_id?sid(r.subscription.sender_id):'<span class="mut">none</span>'))+'</td><td>'+esc(r.transport)+'</td></tr>';
  }
  h+='</table>';
  // senders
  h+='<h2>IS-04/05 SENDERS</h2><table><tr><th>sender</th><th>flow</th><th>en</th><th>group:port</th><th>transport</th><th>sdp</th></tr>';
  for(const s of d.senders){
    const a=s.is05||{};
    h+='<tr class="clk" onclick="detail(\\'senders\\',\\''+esc(s.id)+'\\')"><td>'+esc(s.label)+'</td><td>'+fmtFlow(s.flow)+'</td><td>'+dot(a.master_enable)+'</td><td>'+mcast(a)+'</td><td>'+esc(s.transport)+'</td><td>'+(s.manifest_href?'<span class="k">yes</span>':'<span class="mut">&mdash;</span>')+'</td></tr>';
  }
  h+='</table>';
  const c=d.counts||{};
  h+='<div id="meta">tap any row for full IS-04/05 JSON &middot; '+(c.nodes||0)+' nodes &middot; '+(c.devices||0)+' devices &middot; '+(c.senders||0)+' senders &middot; '+(c.receivers||0)+' receivers &middot; '+(c.flows||0)+' flows</div>';
  document.getElementById('nmos').innerHTML=h;
}
async function loadNmos(){
  try{const r=await fetch('/nmos',{cache:'no-store'});renderNmos(await r.json());}
  catch(e){document.getElementById('nmos').innerHTML='<span class="warn">IS-04/05 unavailable</span>';}
}
async function detail(kind,id){
  const ov=document.getElementById('ov'),pre=document.getElementById('ovpre'),ttl=document.getElementById('ovttl');
  ttl.textContent=kind.replace(/s$/,'')+' '+id; pre.textContent='loading\\u2026'; ov.style.display='flex';
  try{const r=await fetch('/resource?kind='+kind+'&id='+encodeURIComponent(id),{cache:'no-store'});
      pre.textContent=JSON.stringify(await r.json(),null,2);}
  catch(e){pre.textContent='error loading resource';}
}
async function sync(){
  // NTP-ish: estimate (server clock - local clock). Reject jittery round-trips and smooth
  // the rest, so a laggy /time response (PC under load) doesn't make the timecode jump.
  try{const t0=Date.now();const r=await fetch('/time',{cache:'no-store'});const t1=Date.now();
      const d=await r.json();ptp=d;renderInfo();
      const rtt=t1-t0; if(rtt>250) return;          // too jittery to trust this sample
      const est=d.epoch_ms-(t1-rtt/2);              // server time at the client receive-midpoint
      if(!synced){offset=est;synced=true;}          // fast initial lock
      else{offset += Math.max(-40,Math.min(40,est-offset));}  // clamp to 40ms/sync -> never jumps
  }catch(e){}
}
const p=(n,l=2)=>String(n).padStart(l,'0');
const tcEl=document.getElementById('tc'), infoEl=document.getElementById('info');
const finfoEl=document.getElementById('finfo');
function renderInfo(){   // only when ptp changes (called from sync, ~3s) -- NOT per frame
  const role = ptp.state==='MASTER' ? '<span class="gm">GRANDMASTER</span>' : (ptp.state||'\\u2014');
  infoEl.innerHTML='PTP domain 0 &middot; '+role+' &middot; '+(ptp.gm||'\\u2014')+' &middot; offset '+(ptp.offset||'\\u2014')+' ns';
}
function tick(){   // per-frame: ONLY the timecode text (cheap); no innerHTML, no DOM lookups
  const now=new Date(Date.now()+offset);
  const ff=Math.floor(now.getMilliseconds()/(1000/FPS));
  tcEl.textContent=p(now.getHours())+':'+p(now.getMinutes())+':'+p(now.getSeconds())+':'+p(ff);
  requestAnimationFrame(tick);
}
refreshState(); setInterval(refreshState,5000);
loadNmos();     setInterval(loadNmos,6000);
musicState();   setInterval(musicState,4000);
loadAudiomap(); setInterval(loadAudiomap,3000);
loadTv();       // populate the favorites row on load (no interval — avoids hammering the HDHR)
loadProgramOut(); setInterval(loadProgramOut,2000);
async function loadLoud(){
  const el=document.getElementById('loud'); if(!el) return;
  try{
    const d=await(await fetch('http://'+location.hostname+':8104/loudness',{cache:'no-store'})).json();
    if(d.short_term==null){ el.innerHTML='PROGRAM LOUDNESS &middot; <span class="foff">no audio</span>'; return; }
    const cls=d.in_spec?'flock':'fwarn';
    const tp = d.true_peak!=null ? (' &middot; peak '+d.true_peak.toFixed(1)+' dBTP'+(d.tp_over?' \u26a0':'')) : '';
    el.innerHTML='PROGRAM LOUDNESS &middot; <span class="'+cls+'">'+d.short_term.toFixed(1)+' LUFS</span>'
      +' <span class="foff">(R128 '+d.target+' \u00b1'+d.tolerance+', I '+(d.integrated==null?'\u2014':d.integrated.toFixed(1))+', LRA '+d.lra.toFixed(1)+')</span>'+tp;
  }catch(e){ el.innerHTML=''; }
}
loadLoud(); setInterval(loadLoud,1000);
loadSwitcher(); setInterval(loadSwitcher,2000);
fecRefresh();   setInterval(fecRefresh,4000);
spsRefresh();   setInterval(spsRefresh,4000);
async function followerSync(){
  try{const r=await fetch('/follower',{cache:'no-store'});const d=await r.json();
      let cls='foff',label=(d.state||'offline');
      if(d.state==='SLAVE'){const ms=(d.offset||0)/1e6; if(Math.abs(ms)<2){cls='flock';label='LOCKED';}else{cls='fwarn';label='SLAVE';}}
      else if(d.state==='UNCALIBRATED'||d.state==='LISTENING'){cls='fwarn';label=d.state;}
      const off=(d.offset!=null&&d.state!=='offline')?(' &middot; offset '+((d.offset/1e6).toFixed(3))+' ms'):'';
      finfoEl.innerHTML='PTP FOLLOWER (pi2) &middot; <span class="'+cls+'">'+label+'</span>'+off;
  }catch(e){finfoEl.innerHTML='PTP FOLLOWER (pi2) &middot; <span class="foff">unreachable</span>';}
}
async function resyncFollower(btn){
  btn.disabled=true; const old=btn.textContent; btn.textContent='re-syncing...';
  try{await fetch('/follower/resync',{cache:'no-store'});}catch(e){}
  followerSync();
  setTimeout(function(){btn.disabled=false; btn.textContent=old;}, 8000);
}
sync(); setInterval(sync,3000); followerSync(); setInterval(followerSync,3000); tick();

// ---- Guided demo: a scripted tour that drives the existing controls with on-screen captions.
let demoOn=false, demoAbort=false;
function cap(t){ const e=document.getElementById("democap"); if(e){ e.textContent=t||""; e.style.display=t?"block":"none"; } fetch("/demo/caption?text="+encodeURIComponent(t||""),{cache:"no-store"}).catch(function(){}); }
function go(path){ return fetch(path,{cache:"no-store"}).catch(function(){}); }
function nap(ms){ return new Promise(function(r){ setTimeout(r,ms); }); }
async function step(caption, action, dwell){
  if(demoAbort) throw "abort";
  cap(caption);
  if(action) await action();
  await nap(dwell);
  if(demoAbort) throw "abort";
}
async function demoReset(){
  await go("/fec/set?loss=0"); await go("/fec/set?enable=1");
  await go("/sps/set?path=a&up=1"); await go("/sps/set?path=b&up=1");
  await go("/programout/route?essence=none");
  await go("/is11/unconstrain"); await go("/is11/edid?load=0");
}
async function runDemo(){
  const btn=document.getElementById("demobtn");
  if(demoOn){ demoAbort=true; cap("Stopping demo\u2026"); return; }
  demoOn=true; demoAbort=false; if(btn){ btn.textContent="\u25A0 Stop demo"; btn.classList.add("on"); }
  try{
    await demoReset();
    await step("Atoll: a self-contained NMOS ST 2110 broadcast rig. This is the multiviewer \u2014 four live flows at once, each a real NMOS sender.", function(){ return go("/layout?mode=wall"); }, 9000);
    await step("Taking a source is a real IS-05 operation. The red tally border and ON-AIR flag follow it live over IS-07.", function(){ return go("/take?src=hevc"); }, 6000);
    await step("Take another source \u2014 the tally moves with it.", function(){ return go("/take?src=jxs"); }, 6000);
    await step("Program Out: a software NMOS receiver you route any flow to over IS-05. The picture follows the connection \u2014 here, Live TV.", async function(){ await go("/layout?mode=program"); await go("/programout/route?essence=hevc"); }, 8000);
    await step("Route a different flow over the same IS-05 connection \u2014 Home videos now.", function(){ return go("/programout/route?essence=jxs"); }, 7000);
    await step("IS-05 activations can be scheduled, not just immediate \u2014 arming a take for +5s; the connection fires on the clock.", function(){ return go("/programout/route?essence=hevc&secs=5"); }, 9000);
    await step("IS-08 audio channel mapping \u2014 routing the music's stereo channels live. Swapping left and right\u2026", async function(){ await go("/take?src=music"); await go("/layout?mode=single"); await go("/audiomap/set?preset=swap"); }, 8000);
    await step("\u2026and back to straight stereo.", function(){ return go("/audiomap/set?preset=stereo"); }, 6000);
    await step("Live TV: changing channel opens the new channel on a second tuner first, then cuts \u2014 no black frame.", async function(){ await go("/layout?mode=single"); await go("/take?src=hevc"); }, 5000);
    let chans=[];
    try{ const d=await(await fetch("/tv/lineup",{cache:"no-store"})).json(); chans=((d.favorites&&d.favorites.length?d.favorites:d.channels)||[]).map(function(c){return c.num;}); }catch(e){}
    if(chans.length>=2){ await step("Changing channel\u2026", function(){ return go("/tv/set?ch="+encodeURIComponent(chans[0])); }, 6000); await step("\u2026and again \u2014 seamless.", function(){ return go("/tv/set?ch="+encodeURIComponent(chans[1])); }, 6000); }
    await step("ST 2022-1 FEC. Fullscreen the protected feed.", async function(){ await go("/layout?mode=single"); await go("/take?src=fec"); }, 5000);
    await step("Inject 5% packet loss \u2014 FEC reconstructs every lost packet, the picture stays clean.", function(){ return go("/fec/set?loss=0.05"); }, 8000);
    await step("Now switch FEC OFF at the same 5% loss \u2014 watch it tear.", function(){ return go("/fec/set?enable=0"); }, 8000);
    await step("FEC back ON \u2014 clean again. Loss removed.", async function(){ await go("/fec/set?enable=1"); await nap(3000); await go("/fec/set?loss=0"); }, 5000);
    await step("ST 2022-7 seamless protection: the same essence sent on two network paths.", function(){ return go("/take?src=sps"); }, 6000);
    await step("Pull one path \u2014 the other carries it, hitless. The picture does not flinch.", function(){ return go("/sps/set?path=a&up=0"); }, 8000);
    await step("Restore the path. Both live again.", function(){ return go("/sps/set?path=a&up=1"); }, 5000);
    await step("IS-11 stream compatibility \u2014 the layer that keeps senders and receivers matched. Applying a grain-rate constraint retunes the sender\u2019s flow to stay within what a receiver can take (25\u219250 fps).", function(){ return go("/is11/constrain?num=50&den=1"); }, 8000);
    await step("Clear it \u2014 the flow returns to its native rate. IS-11 also carries EDID, the HDMI-style capability handshake, and passes the AMWA IS-11-01 conformance suite.", function(){ return go("/is11/unconstrain"); }, 7000);
    await step("IS-12 device control \u2014 the modern NMOS control plane. A WebSocket carries the MS-05 object model; the rig reads its own device model live \u2014 root block, device + class managers, 6 classes and 58 datatypes.", function(){ return go("/is12/state"); }, 8000);
    cap("Demo complete \u2014 everything you saw runs live and to spec."); await nap(6000);
  }catch(e){}
  await demoReset(); await go("/layout?mode=wall"); cap("");
  demoOn=false; demoAbort=false; if(btn){ btn.textContent="\u25B6 Guided demo"; btn.classList.remove("on"); }
}
(function(){
  var tip=document.createElement("div"); tip.id="tip"; document.body.appendChild(tip); var pinned=null;
  function show(el){ tip.textContent=el.getAttribute("data-tip"); tip.style.display="block"; tip.classList.add("on");
    var r=el.getBoundingClientRect(), t=tip.getBoundingClientRect();
    var x=Math.max(6, Math.min(r.left, window.innerWidth-t.width-6)), y=r.bottom+6;
    if(y+t.height>window.innerHeight-6) y=Math.max(6, r.top-t.height-6);
    tip.style.left=x+"px"; tip.style.top=y+"px"; }
  function hide(){ tip.classList.remove("on"); tip.style.display="none"; }
  document.querySelectorAll("[data-tip]").forEach(function(el){
    el.addEventListener("mouseenter", function(){ if(!pinned) show(el); });
    el.addEventListener("mouseleave", function(){ if(!pinned) hide(); });
    el.addEventListener("click", function(e){ e.stopPropagation(); if(pinned===el){ pinned=null; hide(); } else { pinned=el; show(el); } });
  });
  document.addEventListener("click", function(){ if(pinned){ pinned=null; hide(); } });
})();

</script></body></html>"""

PAGE = PAGE_TEMPLATE.replace("__FPS__", repr(FPS))

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send_json(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/take":
            qs = parse_qs(parsed.query)
            src = qs.get("src", [DEFAULT_SRC])[0]
            if src not in SOURCES:
                src = DEFAULT_SRC
            try:
                take(src); self._send_json(json.dumps({"active": src}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/state":
            try:
                self._send_json(json.dumps({"active": active_src(), "layout": _output["layout"], "slots": ",".join(_slots), "video_delay": _avsync_get()}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/layout":
            qs = parse_qs(parsed.query)
            mode = qs.get("mode", ["single"])[0]
            if mode not in ("single", "side", "multi", "wall", "program", "switcher", "jxs"):
                mode = "single"
            _output["layout"] = mode
            self._send_json(json.dumps({"layout": mode}).encode())
        elif parsed.path == "/switcher/state":
            self._send_json(json.dumps(_switch_state()).encode())
        elif parsed.path == "/switcher/pvw":
            src = parse_qs(parsed.query).get("src", [""])[0]
            if src in _SWITCH_SRCS:
                if _switcher["seq"] % 2 == 0: _switcher["b"] = src
                else: _switcher["a"] = src
                _switch_write()
            self._send_json(json.dumps(_switch_state()).encode())
        elif parsed.path == "/switcher/take":
            _switcher["seq"] += 1; _switch_write()
            self._send_json(json.dumps(_switch_state()).encode())
        elif parsed.path == "/switcher/trans":
            _q = parse_qs(parsed.query)
            _t = _q.get("type", ["dissolve"])[0]
            if _t in ("cut", "dissolve"): _switcher["trans"] = _t
            try:
                _switcher["rate"] = max(0.2, min(3.0, float(_q.get("rate", ["1.0"])[0])))
            except ValueError:
                pass
            _switch_write()
            self._send_json(json.dumps(_switch_state()).encode())
        elif parsed.path == "/programout/state":
            try:
                with urllib.request.urlopen(f"{PROGRAMOUT}/programout", timeout=3) as r:
                    self._send_json(r.read())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 502)
        elif parsed.path == "/audiomap/state":
            try:
                self._send_json(audiomap_state())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 502)
        elif parsed.path == "/audiomap/set":
            _q = parse_qs(parsed.query)
            preset = _q.get("preset", ["stereo"])[0]
            try:
                secs = int(_q.get("secs", ["0"])[0] or 0)
            except ValueError:
                secs = 0
            try:
                audiomap_set(preset, secs)
                self._send_json(json.dumps({"preset": preset, "secs": secs}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 502)
        elif parsed.path == "/programout/route":
            _q = parse_qs(parsed.query)
            ess = _q.get("essence", ["none"])[0]
            try:
                secs = int(_q.get("secs", ["0"])[0] or 0)
            except ValueError:
                secs = 0
            try:
                programout_route(ess, secs)
                self._send_json(json.dumps({"routed": ess, "secs": secs}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 502)
        elif parsed.path == "/demo/caption":
            txt = parse_qs(parsed.query).get("text", [""])[0]
            try:
                with open(_RUN + "/demo-caption", "w") as _f:
                    _f.write(txt)
                self._send_json(json.dumps({"ok": True}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/slot":
            qs = parse_qs(parsed.query)
            try:
                pos = int(qs.get("pos", ["-1"])[0])
            except ValueError:
                pos = -1
            src = qs.get("src", [""])[0]
            if 0 <= pos < 4 and src in SOURCES:
                _slots[pos] = src
                self._send_json(json.dumps({"slots": ",".join(_slots)}).encode())
            else:
                self._send_json(json.dumps({"error": "bad pos/src"}).encode(), 400)
        elif parsed.path == "/nmos":
            try:
                self._send_json(json.dumps(nmos_overview()).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/resource":
            qs = parse_qs(parsed.query)
            kind = qs.get("kind", [""])[0]
            rid = qs.get("id", [""])[0]
            if kind not in ("nodes", "devices", "sources", "flows", "senders", "receivers") or not rid:
                self._send_json(json.dumps({"error": "bad kind/id"}).encode(), 400)
            else:
                try:
                    self._send_json(json.dumps(resource_detail(kind, rid)).encode())
                except Exception as e:
                    self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/time":
            self._send_json(pi_time())
        elif parsed.path == "/follower":
            self._send_json(follower_status())
        elif parsed.path == "/follower/resync":
            self._send_json(follower_resync())
        elif parsed.path.startswith("/music/"):
            action = parsed.path[len("/music/"):]
            if action == "state":
                self._send_json(music_state())
            elif action in ("next", "prev", "playpause", "shuffle"):
                self._send_json(music_action(action))
            else:
                self._send_json(json.dumps({"error": "unknown music action"}).encode(), 404)
        elif parsed.path == "/tv/lineup":
            self._send_json(tv_lineup())
        elif parsed.path == "/tv/set":
            self._send_json(tv_set(parse_qs(parsed.query).get("ch", [""])[0]))
        elif parsed.path == "/sps/state":
            self._send_json(sps_state())
        elif parsed.path == "/sps/set":
            _q = parse_qs(parsed.query)
            self._send_json(sps_set(_q.get("path", [""])[0], _q.get("up", ["1"])[0]))
        elif parsed.path == "/avsync/set":
            _q = parse_qs(parsed.query)
            self._send_json(json.dumps(_avsync_set(_q.get("ms", ["0"])[0])).encode())
        elif parsed.path == "/avsync/state":
            self._send_json(json.dumps({"ms": _avsync_get()}).encode())
        elif parsed.path == "/cc/set":
            _q = parse_qs(parsed.query)
            self._send_json(json.dumps(_cc_set(_q.get("on", ["0"])[0] in ("1","true","on"))).encode())
        elif parsed.path == "/cc/scte":
            self._send_json(json.dumps(_cc_scte()).encode())
        elif parsed.path == "/cc/source":
            self._send_json(json.dumps(_cc_source_set(parse_qs(parsed.query).get("mode",[""])[0])).encode())
        elif parsed.path == "/cc/delay":
            self._send_json(json.dumps(_cc_delay_set(parse_qs(parsed.query).get("ms",["0"])[0])).encode())
        elif parsed.path == "/cc/state":
            self._send_json(json.dumps(_cc_state()).encode())
        elif parsed.path == "/auth/state":
            self._send_json(json.dumps(_auth_state()).encode())
        elif parsed.path == "/auth/set":
            self._send_json(json.dumps(_auth_set(parse_qs(parsed.query).get("on",["0"])[0] in ("1","true","on"))).encode())
        elif parsed.path == "/auth/demo":
            self._send_json(json.dumps(_auth_demo()).encode())
        elif parsed.path == "/is11/state":
            self._send_json(json.dumps(_is11_state()).encode())
        elif parsed.path == "/is11/constrain":
            _q = parse_qs(parsed.query)
            self._send_json(json.dumps(_is11_constrain(_q.get("num",["50"])[0], _q.get("den",["1"])[0])).encode())
        elif parsed.path == "/is11/unconstrain":
            self._send_json(json.dumps(_is11_unconstrain()).encode())
        elif parsed.path == "/is11/edid":
            self._send_json(json.dumps(_is11_edid(parse_qs(parsed.query).get("load",["0"])[0] in ("1","true","on"))).encode())
        elif parsed.path == "/is12/state":
            self._send_json(json.dumps(_is12_state()).encode())
        elif parsed.path == "/rec/start":
            self._send_json(json.dumps(_rec_start(parse_qs(parsed.query).get("src",[""])[0])).encode())
        elif parsed.path == "/rec/stop":
            self._send_json(json.dumps(_rec_stop()).encode())
        elif parsed.path == "/rec/status":
            self._send_json(json.dumps(_rec_status()).encode())
        elif parsed.path == "/rec/list":
            self._send_json(json.dumps(_rec_list()).encode())
        elif parsed.path == "/rec/delete":
            self._send_json(json.dumps(_rec_delete(parse_qs(parsed.query).get("file",[""])[0])).encode())
        elif parsed.path == "/play/start":
            _q = parse_qs(parsed.query)
            self._send_json(json.dumps(_play_start(_q.get("file",[""])[0], _q.get("loop",["0"])[0] in ("1","true","on"))).encode())
        elif parsed.path == "/play/stop":
            self._send_json(json.dumps(_play_stop()).encode())
        elif parsed.path == "/fec/state":
            self._send_json(fec_state())
        elif parsed.path == "/fec/set":
            _q = parse_qs(parsed.query)
            self._send_json(fec_set(_q.get("loss", [None])[0], _q.get("enable", [None])[0]))
        elif parsed.path == "/tv/fav":
            _q = parse_qs(parsed.query)
            self._send_json(tv_fav(_q.get("ch", [""])[0], _q.get("on", ["1"])[0] in ("1", "true", "on")))
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(("0.0.0.0", PORT), Handler) as s:
    print(f"ST 2110 IS-04/05 switch panel on http://localhost:{PORT}  (control + inspector)")
    print("  Ctrl+C to stop")
    try: s.serve_forever()
    except KeyboardInterrupt: pass
