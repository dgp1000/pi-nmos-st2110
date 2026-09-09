#!/usr/bin/env python3
# ===========================================================================
#  Atoll single-view renderer WITH audio VU meters + a live stream-info overlay.
#
#  Seamless source switching (7 Sep 2026): a PERSISTENT display pipeline
#  (intervideosrc channel=single -> cairooverlay -> glimagesink) reads one
#  intervideo bus; a SEPARATE source pipeline (decode video -> intervideosink,
#  plus decode audio -> level -> autoaudiosink) feeds it. Changing the active
#  source rebuilds ONLY the source pipeline; the window/overlay never respawn.
#  We poll the panel for the active source ourselves, so output-render.sh no
#  longer relaunches us on a source change (its `single` key drops :$active).
#  Single view is one decoder, so a live rebuild has ample GPU headroom on WSLg
#  (unlike the 4-up wall). ATOLL_SINK_TEST=1 swaps glimagesink for fakesink.
#
#  Draws one VU bar per audio channel (auto-detects channel count) and a
#  top-left panel: source, video codec / resolution / framerate / pixel format,
#  audio codec / channels / sample-rate, live bitrate, and the ST 2022-1 FEC /
#  ST 2022-7 SPS demo readouts. Usage: meter-view.py <initial-src> [SCREEN]
# ===========================================================================
import gi, os, sys, subprocess, urllib.request, json, time, threading
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
import cairo
Gst.init(None)

INIT_SRC = sys.argv[1] if len(sys.argv) > 1 else "hevc"
SCREEN = sys.argv[2] if len(sys.argv) > 2 else "2"
HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "VIDEO_SINK", "ATOLL_PLATFORM", "ATOLL_RUN", "PANEL_PORT", "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT",
        "MUSIC_GRP", "MUSIC_PORT", "MUSIC_AUDIO_GRP", "MUSIC_AUDIO_PORT", "REELS_GRP", "REELS_PORT", "PI_RAW_GRP", "PI_RAW_PORT",
        "AUDIO_GAIN_HEVC", "AUDIO_GAIN_JXS", "AUDIO_GAIN_MUSIC",
        "PI_AUDIO_GRP", "PI_AUDIO_PORT", "J2K_GRP", "J2K_PORT", "H264_GRP", "H264_PORT",
        "OPUS_GRP", "OPUS_PORT", "MJPEG_GRP", "MJPEG_PORT", "VP9_GRP", "VP9_PORT",
        "TSRTP_GRP", "TSRTP_PORT", "FEC_GRP", "FEC_PORT", "FEC_COLUMNS", "FEC_ROWS",
        "SPS_A_GRP", "SPS_A_PORT", "SPS_B_GRP", "SPS_B_PORT",
        "GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
# ---- FEC recovery jitterbuffer sizing (2022-1); see meter-view history for the reasoning ----
_FEC_COLS = int(CFG.get("FEC_COLUMNS") or 5)
_FEC_ROWS = int(CFG.get("FEC_ROWS") or 5)
FEC_JB_FLOOR_PPS = 50
FEC_JB_MS = max(500, round(2 * _FEC_COLS * _FEC_ROWS / FEC_JB_FLOOR_PPS * 1000))
FEC_JB = f"rtpjitterbuffer latency={FEC_JB_MS} max-misorder-time={FEC_JB_MS * 5} max-dropout-time={FEC_JB_MS * 5}"

for k in ("GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
    if CFG.get(k):
        os.environ[k] = CFG[k]
IFACE = CFG["ISLAND_IFACE"] or "eth0"
OUT_W = int(os.environ.get("ATOLL_OUT_W", "1280"))     # source scales to this; display GL-upscales
OUT_H = int(os.environ.get("ATOLL_OUT_H", "720"))
_WINW = int(os.environ.get("ATOLL_TV_W", "3840"))      # glimagesink window size (Monitor 2 native)
_WINH = int(os.environ.get("ATOLL_TV_H", "2160"))
IS_WSL = CFG["ATOLL_PLATFORM"] == "wsl"
RUN = CFG.get("ATOLL_RUN", "")
PANEL = f"http://localhost:{CFG.get('PANEL_PORT', '8096')}"
TEST = os.environ.get("ATOLL_SINK_TEST") == "1"
def grp(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,"
            "sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,"
            "colorimetry=(string)BT601-5,payload=(int)96")
J2K_CAPS = "application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0"
H264_CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96"
OPUS_CAPS = "application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97"
MJPEG_CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=96"
VP9_CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=VP9,payload=96"
TSRTP_CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33"
FECSTREAM_CAPS = "application/x-rtp,payload=96,clock-rate=90000"
SPS_CAPS = "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33"
BRAND = ("textoverlay text=ATOLL valignment=top halignment=right ypad=18 xpad=28 "
         "font-desc='Sans Bold 20' color=0x80ffffff shaded-background=false")

# --- each source pipeline's VIDEO branch ends here (feeds the intervideo bus) ---
SRCTAIL = "videoconvert ! intervideosink channel=single sync=false"
# measure ALL channels at 'level' (so meters show 5.1), then downmix to stereo for playback.
ALEVEL = "audioconvert ! level name=lvl post-messages=true interval=50000000"
def aplay(key, sync=True):
    """Audio playback tail with per-source gain (normalize hot sources to Live TV) + a live TRIM
    pad (aq). sync=true PTS-lip-syncs against the shared clock; music L24 uses sync=false."""
    gain = CFG.get(f"AUDIO_GAIN_{key.upper()}") or "1.0"
    vol = f"volume volume={gain} ! " if gain not in ("1.0", "1") else ""
    s = "true" if sync else "false"
    return ("audioconvert ! audio/x-raw,channels=2 ! audioresample "
            f"! {vol}queue name=aq max-size-buffers=0 max-size-bytes=0 max-size-time=1000000000 "
            f"! autoaudiosink sync={s}")

def ts_pipeline(key, g, p):   # HEVC video + MP3/AAC audio in a TS (Live TV / Home / Reels)
    return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d "
            f"d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
            f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {aplay(key)}")

def music_pipeline():   # Music: HEVC video-only TS + ST 2110-30 L24 audio
    g, p = grp("MUSIC"); ag, ap = grp("MUSIC_AUDIO")
    return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d "
            f"d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
            f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
            f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96" '
            f"! rtpjitterbuffer latency=500 ! rtpL24depay ! {ALEVEL} ! {aplay('music', sync=False)}")

def source_desc(key):
    if key == "music":
        return music_pipeline()
    if key in ("hevc", "jxs", "reels"):
        g, p = grp({"hevc": "HEVC", "jxs": "HOME", "reels": "REELS"}[key])
        return ts_pipeline(key, g, p)
    if key == "raw":   # Pi RTP video + the separate ST 2110-30 L24 audio flow
        g, p = grp("PI_RAW"); ag, ap = grp("PI_AUDIO")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true caps=\"{RAW_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
                f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
                f"caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96\" "
                f"! rtpjitterbuffer latency=500 ! rtpL24depay ! {ALEVEL} ! {aplay('raw')}")
    if key == "j2k":   # video only (no audio) -> meters idle
        g, p = grp("J2K")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{J2K_CAPS}\" "
                f"! rtpj2kdepay ! avdec_jpeg2000 ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL}")
    if key == "h264":  # H.264 video (RFC 6184) + its Opus audio (RFC 7587)
        g, p = grp("H264"); ag, ap = grp("OPUS")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{H264_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! nvh264dec ! cudadownload "
                f"! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
                f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true caps=\"{OPUS_CAPS}\" "
                f"! rtpjitterbuffer latency=200 ! rtpopusdepay ! opusdec ! {ALEVEL} ! {aplay('h264')}")
    if key == "mjpeg":  # Motion JPEG over RTP (RFC 2435), video only -> meters idle
        g, p = grp("MJPEG")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 caps=\"{MJPEG_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpjpegdepay ! nvjpegdec "
                f"! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL}")
    if key == "vp9":    # VP9 over RTP (RFC 7741)
        g, p = grp("VP9")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{VP9_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvp9depay ! vp9parse ! nvvp9dec "
                f"! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL}")
    if key == "tsrtp":  # MPEG-TS over RTP (ST 2022-2): full A/V programme inside the TS
        g, p = grp("TSRTP")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{TSRTP_CAPS}\" "
                f"! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d "
                f"d. ! h264parse ! queue ! nvh264dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
                f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {aplay('tsrtp')}")
    if key == "fec":    # ST 2022-1 protected TS/RTP; two live knobs (apply_fec)
        g, p = grp("FEC")
        cp, rp = int(p) + 2, int(p) + 4
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{TSRTP_CAPS}\" "
                f"! identity name=lossy ! rtpst2022-1-fecdec name=fd "
                f"udpsrc address={g} port={cp} multicast-iface={IFACE} auto-multicast=true caps=\"{FECSTREAM_CAPS}\" ! identity name=fecg0 ! queue ! fd.fec_0 "
                f"udpsrc address={g} port={rp} multicast-iface={IFACE} auto-multicast=true caps=\"{FECSTREAM_CAPS}\" ! identity name=fecg1 ! queue ! fd.fec_1 "
                f"fd. ! {FEC_JB} ! rtpmp2tdepay ! tsdemux name=d "
                # 2022-1-recovered H.264 tears on nvh264dec even at 0 loss; avdec is clean (see wall FEC tile).
                f"d. ! h264parse ! queue ! avdec_h264 ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
                f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {aplay('fec')}")
    if key == "sps":    # ST 2022-7: two identical RTP copies merged by sequence number
        ag, ap = CFG["SPS_A_GRP"], CFG["SPS_A_PORT"]
        bg, bp = CFG["SPS_B_GRP"], CFG["SPS_B_PORT"]
        return (f"funnel name=fn ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d "
                f"udpsrc name=ua address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{SPS_CAPS}\" ! identity name=pa ! queue ! fn. "
                f"udpsrc name=ub address={bg} port={bp} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{SPS_CAPS}\" ! identity name=pb ! queue ! fn. "
                f"d. ! h264parse ! queue ! nvh264dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL} "
                f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {aplay('sps')}")
    # jpegxs / unknown -> local test pattern, video only
    return (f"videotestsrc pattern=ball motion=sweep is-live=true ! video/x-raw,width=1920,height=1080,framerate=30/1 "
            f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {SRCTAIL}")

SRCNAME = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "reels": "Test Reels",
           "raw": "Pi raw 2110-20", "j2k": "JPEG 2000 island", "jpegxs": "JPEG XS codec",
           "h264": "H.264 over RTP", "mjpeg": "MJPEG over RTP", "vp9": "VP9 over RTP", "tsrtp": "TS over RTP", "fec": "ST 2022-1 FEC", "sps": "ST 2022-7 seamless"}
VCODEC = {"hevc": "HEVC / H.265", "jxs": "HEVC / H.265", "music": "HEVC / H.265", "reels": "HEVC / H.265",
          "raw": "Uncompressed RFC 4175", "j2k": "JPEG 2000", "jpegxs": "JPEG XS",
          "h264": "H.264 / AVC", "mjpeg": "Motion JPEG", "vp9": "VP9", "tsrtp": "H.264 / AVC", "fec": "H.264 / AVC", "sps": "H.264 / AVC"}
ACODEC = {"hevc": "AAC 5.1", "jxs": "MPEG audio (MP3)", "reels": "MPEG audio (MP3)",
          "music": "L24 PCM (2110-30)", "raw": "L24 PCM (2110-30)", "h264": "Opus", "tsrtp": "AAC", "fec": "AAC", "sps": "AAC"}
TRANSPORT = {"hevc": "MPEG-TS / UDP", "jxs": "MPEG-TS / UDP", "music": "MPEG-TS / UDP",
             "reels": "MPEG-TS / UDP", "raw": "ST 2110-20 RTP", "j2k": "J2K/RTP", "jpegxs": "local",
             "h264": "RTP (RFC 6184) + Opus RTP", "mjpeg": "RTP (RFC 2435)", "vp9": "RTP (RFC 7741)", "tsrtp": "MPEG-TS / RTP (ST 2022-2)", "fec": "TS/RTP + ST 2022-1 FEC", "sps": "TS/RTP x2 (ST 2022-7)"}

# ---- persistent DISPLAY pipeline: reads the single intervideo bus, overlays, presents ----
if TEST:
    _disptail = "videoconvert ! cairooverlay name=ov ! videoconvert ! fakesink name=vsink sync=false"
else:
    _disptail = (f"videoconvert ! cairooverlay name=ov ! {BRAND} ! videoconvert "
                 f"! glupload ! glcolorscale ! video/x-raw(memory:GLMemory),width={_WINW},height={_WINH} ! glimagesink name=vsink sync=true")
disp = Gst.parse_launch(f"intervideosrc channel=single ! video/x-raw,width={OUT_W},height={OUT_H},framerate=30/1 ! {_disptail}")
ov = disp.get_by_name("ov")
_vsink = disp.get_by_name("vsink")
def _apply_avsync():   # hold video back to match late audio; +ms delays video. ~/atoll-run/video-delay-ms
    try: _ms = int(open(os.path.join(RUN, "video-delay-ms")).read().strip())
    except Exception: _ms = 30
    if _vsink: _vsink.set_property("ts-offset", _ms * 1_000_000)
    return True

st = {"src": INIT_SRC, "peak": [], "decay": [], "w": OUT_W, "h": OUT_H, "cap": "",
      "vw": 0, "vh": 0, "vfps": 0.0, "vfmt": "", "mbps": 0.0, "_bytes": 0, "arate": 0, "chan": "",
      "pa_pps": 0, "pb_pps": 0, "_pa": 0, "_pb": 0, "pa_on": True, "pb_on": True,
      "fw_pps": 0, "fa_pps": 0, "fo_pps": 0, "_fw": 0, "_fa": 0, "_fo": 0}

def on_caps(_ov, caps):
    s = caps.get_structure(0); st["w"] = s.get_value("width"); st["h"] = s.get_value("height")
ov.connect("caps-changed", on_caps)

# ---- probe / gate helpers (attached per source pipeline in build_source) ----
def on_bytes(_pad, info):
    b = info.get_buffer()
    if b: st["_bytes"] += b.get_size()
    return Gst.PadProbeReturn.OK
def _count(key):
    def cb(_pad, _info):
        st[key] += 1
        return Gst.PadProbeReturn.OK
    return cb
def caps_probe(p, elem, padname, fn):
    e = p.get_by_name(elem)
    if not e: return
    def cb(_pad, info):
        ev = info.get_event()
        if ev and ev.type == Gst.EventType.CAPS:
            fn(ev.parse_caps().get_structure(0))
        return Gst.PadProbeReturn.OK
    e.get_static_pad(padname).add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, cb)
def set_vcaps(s):
    ok, w = s.get_int("width"); ok2, hh = s.get_int("height")
    if ok: st["vw"] = w
    if ok2: st["vh"] = hh
    okf, num, den = s.get_fraction("framerate")
    if okf and den: st["vfps"] = round(num / den, 2)
    st["vfmt"] = s.get_string("format") or st["vfmt"]
def set_acaps(s):
    okr, r = s.get_int("rate")
    if okr: st["arate"] = r

def _knob(name, default):
    try:
        return float(open(os.path.join(RUN, name)).read().strip())
    except Exception:
        return default

# ---- the current source pipeline + its named elements (refreshed on every rebuild) ----
SP = {"pipe": None, "aqpad": None, "lossy": None, "fecg": [], "ua": None, "ub": None, "pa": None, "pb": None}

def on_msg(_b, msg):
    if msg.type == Gst.MessageType.ELEMENT:
        s = msg.get_structure()
        if s and s.get_name() == "level":
            try:
                st["peak"] = [float(x) for x in s.get_value("peak")]
                st["decay"] = [float(x) for x in s.get_value("decay")]
            except Exception:
                pass
    elif msg.type == Gst.MessageType.ERROR:
        e, dbg = msg.parse_error()
        print(f"meter-view ERROR: {e.message} :: {dbg}", flush=True)

def build_source(key):
    """(Re)build the source pipeline for `key` -> intervideosink channel=single (+ audio -> level ->
    autoaudiosink). Tears down only the source pipeline; the display window/overlay stay up."""
    old = SP["pipe"]
    SP["pipe"] = None
    if old is not None:
        old.set_state(Gst.State.NULL)
    try:
        sp = Gst.parse_launch(source_desc(key))
    except Exception as e:
        print(f"meter-view: source {key} parse error: {e}", flush=True)
        return
    # reset per-source stats + meters so stale numbers don't linger
    st["peak"] = []; st["decay"] = []; st["_bytes"] = 0; st["mbps"] = 0.0
    st["vw"] = 0; st["vh"] = 0; st["vfps"] = 0.0; st["vfmt"] = ""; st["arate"] = 0
    for k in ("_fw", "_fa", "_fo", "_pa", "_pb", "pa_pps", "pb_pps", "fw_pps", "fa_pps", "fo_pps"):
        st[k] = 0
    st["pa_on"] = st["pb_on"] = True
    # bitrate tap
    u = sp.get_by_name("usrc")
    if u: u.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_bytes)
    # source video caps (res/fps/fmt) + audio caps (rate)
    caps_probe(sp, "vpre", "sink", set_vcaps)
    caps_probe(sp, "lvl", "src", set_acaps)
    # FEC recovery counters (usrc/lossy/fd) + SPS per-path counters (ua/ub) -- src pad probes
    for nm, k in (("usrc", "_fw"), ("lossy", "_fa"), ("fd", "_fo"), ("ua", "_pa"), ("ub", "_pb")):
        e = sp.get_by_name(nm)
        if e: e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _count(k))
    # gate / trim element refs for the live knob timers
    aq = sp.get_by_name("aq")
    SP["aqpad"] = aq.get_static_pad("sink") if aq else None
    SP["lossy"] = sp.get_by_name("lossy")
    SP["fecg"] = [sp.get_by_name("fecg0"), sp.get_by_name("fecg1")]
    SP["pa"] = sp.get_by_name("pa"); SP["pb"] = sp.get_by_name("pb")
    b = sp.get_bus(); b.add_signal_watch(); b.connect("message", on_msg)
    SP["pipe"] = sp
    sp.set_state(Gst.State.PLAYING)
    print(f"{time.strftime('%T')} meter-view: source <- {key}", flush=True)

# ---- 1 Hz stats + live knob timers (read from SP, so they follow rebuilds) ----
def tick():
    st["mbps"] = round(st["_bytes"] * 8 / 1e6, 1); st["_bytes"] = 0
    if st["src"] == "hevc" and RUN:
        try: st["chan"] = open(os.path.join(RUN, "tv-channel")).read().strip()
        except Exception: pass
    else:
        st["chan"] = ""
    return True
GLib.timeout_add_seconds(1, tick)

ADELAY_FILE = os.path.join(RUN, "tv-audio-delay-ms")
def apply_adelay():
    try: ms = int(open(ADELAY_FILE).read().strip())
    except Exception: ms = 0
    if SP["aqpad"]: SP["aqpad"].set_offset(ms * 1_000_000)   # +delay / -advance audio
    return True
GLib.timeout_add_seconds(1, apply_adelay)

def apply_fec():   # ST 2022-1 loss injector + FEC gate (source key "fec")
    if SP["lossy"]:
        SP["lossy"].set_property("drop-probability", max(0.0, min(1.0, _knob("fec-loss", 0.0))))
    on = _knob("fec-enable", 1.0) >= 0.5
    for g in SP["fecg"]:
        if g: g.set_property("drop-probability", 0.0 if on else 1.0)
    return True
GLib.timeout_add_seconds(1, apply_fec)

def apply_sps():   # ST 2022-7 per-path stats + live kill switches (source key "sps")
    st["pa_pps"], st["_pa"] = st["_pa"], 0
    st["pb_pps"], st["_pb"] = st["_pb"], 0
    st["fw_pps"], st["fa_pps"], st["fo_pps"] = st["_fw"], st["_fa"], st["_fo"]   # cumulative totals
    for k, e in (("a", SP["pa"]), ("b", SP["pb"])):
        on = _knob(f"sps-{k}", 1.0) >= 0.5
        st[f"p{k}_on"] = on
        if e: e.set_property("drop-probability", 0.0 if on else 1.0)
    return True
GLib.timeout_add_seconds(1, apply_sps)

# ---- overlay ----
LABELS = ["L", "R", "C", "LFE", "Ls", "Rs", "7", "8"]
def _draw_caption(ctx, text, w, h):
    """Standout closed-caption band: a centred rounded near-opaque box with a bright yellow border
    and bold bright-yellow text, lower third. Returns (y0, height) so double-buffered renderers can
    blit exactly that region."""
    if not text:
        return None
    import math
    ctx.save()
    ctx.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    fs = max(26, int(h * 0.044)); ctx.set_font_size(fs)
    maxw = w * 0.86; lines = []; cur = ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if cur and ctx.text_extents(t).width > maxw:
            lines.append(cur); cur = wd
        else:
            cur = t
    if cur:
        lines.append(cur)
    lh = fs * 1.35; padx = fs * 0.95; pady = fs * 0.5
    tw = max((ctx.text_extents(ln).width for ln in lines), default=0)
    bw = min(w - 8, tw + padx * 2); bh = lh * len(lines) + pady * 2
    x0 = (w - bw) / 2.0; y0 = h - bh - int(h * 0.05)
    r = fs * 0.35
    ctx.new_sub_path()
    ctx.arc(x0 + bw - r, y0 + r, r, -math.pi / 2, 0)
    ctx.arc(x0 + bw - r, y0 + bh - r, r, 0, math.pi / 2)
    ctx.arc(x0 + r, y0 + bh - r, r, math.pi / 2, math.pi)
    ctx.arc(x0 + r, y0 + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()
    ctx.set_source_rgba(0, 0, 0, 0.92); ctx.fill_preserve()               # opaque black box
    ctx.set_source_rgba(1.0, 0.83, 0.0, 0.95); ctx.set_line_width(max(2.0, fs * 0.07)); ctx.stroke()  # yellow border
    for i, ln in enumerate(lines):
        lw = ctx.text_extents(ln).width
        bx = (w - lw) / 2.0; by = y0 + pady + lh * (i + 1) - fs * 0.35
        ctx.set_source_rgba(0, 0, 0, 0.85); ctx.move_to(bx + max(1.5, fs * 0.04), by + max(1.5, fs * 0.04)); ctx.show_text(ln)  # drop shadow
        ctx.set_source_rgba(1.0, 0.93, 0.20, 1.0); ctx.move_to(bx, by); ctx.show_text(ln)             # bright yellow text
    ctx.restore()
    return (int(y0) - 2, int(bh) + 4)
def on_draw(_ov, ctx, _ts, _dur):
    src = st["src"]
    h = st["h"]
    _draw_caption(ctx, st.get("cap") or "", st.get("w") or 1920, h)
    lines = [SRCNAME.get(src, src) + (f"     ch {st['chan']}" if st["chan"] else "")]
    res = f"{st['vw']}x{st['vh']}" if st["vw"] else ""
    fps = f"{st['vfps']:g}p" if st["vfps"] else ""
    lines.append("  ".join(x for x in ("Video ", VCODEC.get(src, "?"), res, fps, st["vfmt"]) if x))
    if src in ACODEC:
        ach = len(st["peak"]) if st["peak"] else 0
        arate = f"{st['arate'] // 1000} kHz" if st["arate"] else ""
        lines.append("  ".join(x for x in ("Audio ", ACODEC[src], f"{ach} ch" if ach else "", arate) if x))
    if st["mbps"]:
        lines.append(f"Stream   {st['mbps']} Mbps   {TRANSPORT.get(src, '')}")
    if src == "fec":
        wire, after, out = st["_fw"], st["_fa"], st["_fo"]
        dropped = max(0, wire - after)
        recovered = max(0, min(dropped, out - after))
        resid = (100.0 * max(0, wire - out) / wire) if wire > 0 else 0.0
        lines.append(f"Network  {wire:,} pkts    {dropped:,} dropped")
        lines.append(f"FEC      {recovered:,} recovered    residual {resid:.3f}%")
    if src == "sps":
        a = f"A {st['pa_pps']:>4} pkt/s" + ("" if st["pa_on"] else "  DEAD")
        b = f"B {st['pb_pps']:>4} pkt/s" + ("" if st["pb_on"] else "  DEAD")
        lines.append(f"Paths    {a}    {b}")
    x0, y0, lh = 34, 34, 30
    ctx.set_source_rgba(0, 0, 0, 0.45); ctx.rectangle(x0 - 14, y0 - 8, 640, lh * len(lines) + 20); ctx.fill()
    ctx.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    for i, ln in enumerate(lines):
        ctx.set_font_size(22 if i == 0 else 17)
        ctx.set_source_rgba(0.55, 0.85, 1.0, 0.95) if i == 0 else ctx.set_source_rgba(1, 1, 1, 0.85)
        ctx.move_to(x0, y0 + 22 + i * lh); ctx.show_text(ln)
    # VU meters (bottom-left)
    peaks = st["decay"] or st["peak"]
    if not peaks:
        return
    n = len(peaks); bw, gap, mx, maxh = 34, 10, 34, int(h * 0.34); baseY = h - 46
    ctx.set_source_rgba(0, 0, 0, 0.35); ctx.rectangle(mx - 12, baseY - maxh - 14, n * (bw + gap) + 14, maxh + 46); ctx.fill()
    ctx.set_font_size(16)
    for i, db in enumerate(peaks):
        lvl = max(0.0, min(1.0, (db + 54.0) / 54.0)); bh = int(lvl * maxh); x = mx + i * (bw + gap)
        ctx.set_source_rgba(1, 1, 1, 0.14); ctx.rectangle(x, baseY - maxh, bw, maxh); ctx.fill()
        r = min(1.0, max(0.0, (lvl - 0.75) / 0.25))
        ctx.set_source_rgba(0.15 + 0.8 * r, 0.8 - 0.45 * r, 0.15, 0.92)
        ctx.rectangle(x, baseY - bh, bw, bh); ctx.fill()
        ctx.set_source_rgba(1, 1, 1, 0.9)
        ctx.move_to(x + bw / 2 - 6, baseY + 20); ctx.show_text(LABELS[i] if i < len(LABELS) else str(i + 1))
ov.connect("draw", on_draw)

def _cap_tick():
    try: st["cap"] = open(os.path.join(RUN, "demo-caption")).read().strip()
    except Exception: st["cap"] = ""
    return True
_cap_tick(); GLib.timeout_add(400, _cap_tick)
_apply_avsync(); GLib.timeout_add_seconds(1, _apply_avsync)

# ---- follow the panel's active source; rebuild ONLY the source pipeline on a change ----
def panel_tick():
    # ATOLL_METER_NOPANEL: stay on the source we were launched with (the `program` layout sets this --
    # it relaunches meter-view on each IS-05 route change -- and headless swap tests use it too).
    if os.environ.get("ATOLL_METER_NOPANEL"):
        return True
    try:
        with urllib.request.urlopen(f"{PANEL}/state", timeout=2) as r:
            a = json.load(r).get("active", "")
    except Exception:
        return True
    if a and a != st["src"]:
        st["src"] = a
        build_source(a)
    return True

# ---- bring it up ----
build_source(INIT_SRC)
disp.set_state(Gst.State.PLAYING)
GLib.timeout_add_seconds(1, panel_tick)
_dbus = disp.get_bus(); _dbus.add_signal_watch(); _dbus.connect("message", on_msg)

def _diag():
    print(f"DIAG src={st['src']} {st['mbps']}Mb/s {st['vw']}x{st['vh']} ch={len(st['peak'])}", flush=True)
    return True
GLib.timeout_add_seconds(5, _diag)

_swap = os.environ.get("ATOLL_METER_TEST_SWAP")   # "key@secs" -- headless test only
if _swap:
    try:
        _sk, _secs = _swap.split("@")
        def _do_swap():
            print(f"TEST swap -> {_sk}", flush=True)
            st["src"] = _sk; build_source(_sk); return False
        GLib.timeout_add(int(float(_secs) * 1000), _do_swap)
    except Exception as _e:
        print(f"TEST swap spec bad: {_e}", flush=True)

print(f"meter-view: {INIT_SRC} -> screen {SCREEN} (seamless)", flush=True)
if IS_WSL and SCREEN != "0" and not TEST:
    def mover():
        try:
            from shutil import which
            pwsh = which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
            m = subprocess.check_output(["wslpath", "-w", os.path.join(HERE, "move-window-screen.ps1")], text=True).strip()
            subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", m, "-Screen", SCREEN, "-TimeoutSec", "15"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"(mover skipped: {e})", flush=True)
    GLib.timeout_add_seconds(2, lambda: (threading.Thread(target=mover, daemon=True).start(), False)[1])

loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    if SP["pipe"] is not None:
        SP["pipe"].set_state(Gst.State.NULL)
    disp.set_state(Gst.State.NULL)
