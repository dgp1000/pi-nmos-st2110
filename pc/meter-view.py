#!/usr/bin/env python3
# ===========================================================================
#  Atoll single-view renderer WITH audio VU meters + a live stream-info overlay. Drop-in for
#  output-render.sh's single layout: decodes the active source (video + synced audio), draws one VU
#  bar per audio channel (auto-detects channel count) and a top-left panel showing source, video
#  codec / resolution / framerate / pixel format, audio codec / channels / sample-rate, and the live
#  measured stream bitrate. Follows the active source (output-render passes it in).
#
#  Usage: meter-view.py <source-key> [SCREEN]   (source-key: hevc|jxs|music|reels|raw|jpegxs|j2k)
# ===========================================================================
import gi, os, sys, subprocess, threading, time
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
import cairo
Gst.init(None)

SRC = sys.argv[1] if len(sys.argv) > 1 else "hevc"
SCREEN = sys.argv[2] if len(sys.argv) > 2 else "2"
HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "VIDEO_SINK", "ATOLL_PLATFORM", "ATOLL_RUN", "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT",
        "MUSIC_GRP", "MUSIC_PORT", "MUSIC_AUDIO_GRP", "MUSIC_AUDIO_PORT", "REELS_GRP", "REELS_PORT", "PI_RAW_GRP", "PI_RAW_PORT",
        "AUDIO_GAIN_HEVC", "AUDIO_GAIN_JXS", "AUDIO_GAIN_MUSIC",
        "PI_AUDIO_GRP", "PI_AUDIO_PORT", "J2K_GRP", "J2K_PORT", "H264_GRP", "H264_PORT",
        "OPUS_GRP", "OPUS_PORT", "MJPEG_GRP", "MJPEG_PORT", "VP9_GRP", "VP9_PORT",
        "TSRTP_GRP", "TSRTP_PORT", "FEC_GRP", "FEC_PORT", "FEC_COLUMNS", "FEC_ROWS",
        "SPS_A_GRP", "SPS_A_PORT", "SPS_B_GRP", "SPS_B_PORT",
        "GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
# ---- FEC recovery jitterbuffer sizing (2022-1) ----------------------------------------------------
# ST 2022-1 column FEC can only reconstruct a lost packet once its whole L x D matrix has arrived, so
# a recovered packet is emitted up to ~2 x L x D packet-times behind the live edge (the matrix plus
# the column-FEC row that follows it). Size the jitterbuffer to hold that span even at a slow feed, so
# recovery stays robust to GOP / bitrate changes and does NOT depend on the all-intra sender hack
# keeping the packet rate high enough for a fixed 500 ms buffer (the old, fragile arrangement -- if
# the feed reverted to a normal GOP at ~70 pps the matrix fell ~1.1 s behind and tore). FEC_JB_FLOOR_PPS
# is a deliberately conservative packet-rate floor (normal-GOP 3 Mbit/s TS ~70-150 pps; the current
# all-intra feed is ~300). At the 5x5 matrix this yields 1000 ms; it scales if FEC_COLUMNS/ROWS change.
_FEC_COLS = int(CFG.get("FEC_COLUMNS") or 5)
_FEC_ROWS = int(CFG.get("FEC_ROWS") or 5)
FEC_JB_FLOOR_PPS = 50
FEC_JB_MS = max(500, round(2 * _FEC_COLS * _FEC_ROWS / FEC_JB_FLOOR_PPS * 1000))
FEC_JB = f"rtpjitterbuffer latency={FEC_JB_MS} max-misorder-time={FEC_JB_MS * 5} max-dropout-time={FEC_JB_MS * 5}"

for k in ("GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
    if CFG.get(k):
        os.environ[k] = CFG[k]
IFACE = CFG["ISLAND_IFACE"] or "eth0"
SINK = CFG["VIDEO_SINK"] or "waylandsink fullscreen=true"
_AGAIN = (CFG.get(f"AUDIO_GAIN_{SRC.upper()}") or "1.0")   # per-source audio gain (normalize hot sources to Live TV)
_VOL = f"volume volume={_AGAIN} ! " if _AGAIN not in ("1.0", "1") else ""   # no element at unity gain
_SINK = "glimagesink" if SRC == "hevc" else SINK   # Live TV present path: glimagesink (GL/EGL, vsync-paced) -- waylandsink SHM present steps the ticker under WSLg
# Present resolution for the on-screen sink. WSLg has no dmabuf, so waylandsink takes CPU (SHM)
# frames that Weston must upload + composite every vsync -- cost scales with pixels. 720p roughly
# halves the 3D/present load vs 1080p and upscales fine to a fullscreen monitor. Override with
# ATOLL_OUT_W/ATOLL_OUT_H (e.g. 1920x1080) when there is present headroom.
OUT_W = int(os.environ.get("ATOLL_OUT_W", "1280"))
OUT_H = int(os.environ.get("ATOLL_OUT_H", "720"))
_WINW = int(os.environ.get("ATOLL_TV_W", "3840"))   # Live TV glimagesink window size (Monitor 2 native)
_WINH = int(os.environ.get("ATOLL_TV_H", "2160"))
_WINSCALE = f"glupload ! glcolorscale ! video/x-raw(memory:GLMemory),width={_WINW},height={_WINH} ! " if SRC == "hevc" else ""
IS_WSL = CFG["ATOLL_PLATFORM"] == "wsl"
RUN = CFG.get("ATOLL_RUN", "")
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
# video ends at a named cairooverlay 'ov'; 'usrc' is tapped for bitrate, 'vpre' for source caps.
VTAIL = f"videoconvert ! cairooverlay name=ov ! {BRAND} ! videoconvert ! {_WINSCALE}{_SINK} sync=true"
# measure ALL channels at 'level' (so the meters show 5.1), THEN downmix to stereo for playback --
# WSLg's Pulse output is stereo and autoaudiosink won't take a 6-channel stream.
ALEVEL = "audioconvert ! level name=lvl post-messages=true interval=50000000"
# sync=true gives true PTS lip-sync (both sinks on the shared clock). Use a PLAIN queue here: a
# min-threshold-time hold in front of a sync=true sink releases audio in bursts -> periodic pops, so
# just let the sink pull a steady flow and schedule by PTS. autoaudiosink so a dead Pulse falls back
# silently instead of killing the video. Downmix to stereo; level upstream keeps 6-ch meters. Any
# residual skew is a live TRIM (both directions) via a pad offset from ADELAY_FILE below.
APLAY = ("audioconvert ! audio/x-raw,channels=2 ! audioresample "
         f"! {_VOL}queue name=aq max-size-buffers=0 max-size-bytes=0 max-size-time=1000000000 "
         "! autoaudiosink sync=true")

def ts_pipeline(g, p):   # HEVC video + MP3 audio in a TS (Live TV / Home / Music / Reels)
    return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d "
            f"d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
            f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {APLAY}")   # audio/mpeg pins the audio pad; decodebin = AAC (Live TV) or MP3

def music_pipeline():   # Music: HEVC video (video-only TS) + ST 2110-30 L24 audio (audio-follows-source)
    g, p = grp("MUSIC"); ag, ap = grp("MUSIC_AUDIO")
    return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d "
            f"d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
            f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
            f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96" '
            f"! rtpjitterbuffer latency=500 ! rtpL24depay ! {ALEVEL} ! {APLAY.replace('sync=true', 'sync=false')}")

def build():
    if SRC == "music":
        return music_pipeline()
    if SRC in ("hevc", "jxs", "reels"):
        g, p = grp({"hevc": "HEVC", "jxs": "HOME", "reels": "REELS"}[SRC])
        return ts_pipeline(g, p)
    if SRC == "raw":   # Pi RTP video + the separate ST 2110-30 L24 audio flow
        g, p = grp("PI_RAW"); ag, ap = grp("PI_AUDIO")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true caps=\"{RAW_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
                f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
                f"caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96\" "
                f"! rtpjitterbuffer latency=500 ! rtpL24depay ! {ALEVEL} ! {APLAY}")
    if SRC == "j2k":   # video only (no audio) -> meters idle
        g, p = grp("J2K")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{J2K_CAPS}\" "
                f"! rtpj2kdepay ! avdec_jpeg2000 ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL}")
    if SRC == "h264":  # H.264 video (RFC 6184) + its Opus audio (RFC 7587) -- two separate RTP essence
        g, p = grp("H264"); ag, ap = grp("OPUS")   # flows for one programme, like 2110-20 + 2110-30
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{H264_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! nvh264dec ! cudadownload "
                f"! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
                f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true caps=\"{OPUS_CAPS}\" "
                f"! rtpjitterbuffer latency=200 ! rtpopusdepay ! opusdec ! {ALEVEL} ! {APLAY}")
    if SRC == "mjpeg":  # Motion JPEG over RTP (RFC 2435), all-intra, video only -> meters idle
        g, p = grp("MJPEG")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 caps=\"{MJPEG_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpjpegdepay ! nvjpegdec "
                f"! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL}")
    if SRC == "vp9":    # VP9 over RTP (RFC 7741); vp9parse supplies the caps nvvp9dec requires
        g, p = grp("VP9")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{VP9_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvp9depay ! vp9parse ! nvvp9dec "
                f"! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL}")
    if SRC == "tsrtp":  # MPEG-TS over RTP (ST 2022-2): a full A/V programme inside the TS.
        g, p = grp("TSRTP")   # the queue on the audio branch is required or the demuxer stalls
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{TSRTP_CAPS}\" "
                f"! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d "
                f"d. ! h264parse ! queue ! nvh264dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
                f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {APLAY}")
    if SRC == "fec":    # ST 2022-1 protected TS/RTP. Two LIVE knobs (see apply_fec below):
        g, p = grp("FEC")   # 'lossy' injects packet loss on the media flow; 'fecg0/fecg1' gate the
        cp, rp = int(p) + 2, int(p) + 4   # FEC flows off, which is how you show the damage FEC hides.
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{TSRTP_CAPS}\" "
                f"! identity name=lossy ! rtpst2022-1-fecdec name=fd "
                f"udpsrc address={g} port={cp} multicast-iface={IFACE} auto-multicast=true caps=\"{FECSTREAM_CAPS}\" ! identity name=fecg0 ! queue ! fd.fec_0 "
                f"udpsrc address={g} port={rp} multicast-iface={IFACE} auto-multicast=true caps=\"{FECSTREAM_CAPS}\" ! identity name=fecg1 ! queue ! fd.fec_1 "
                f"fd. ! {FEC_JB} ! rtpmp2tdepay ! tsdemux name=d "
                # The 2022-1-recovered H.264 tears on nvh264dec (hardware) even at 0 loss; avdec is
                # clean. Same fix as the wall FEC tile (commit e4a2d04) -- this fullscreen path was
                # missed. One 720p 3 Mbps tile in software is cheap, and it is the only fec view.
                f"d. ! h264parse ! queue ! avdec_h264 ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
                f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {APLAY}")
    if SRC == "sps":    # ST 2022-7: two identical RTP copies merged by sequence number.
        ag, ap = CFG["SPS_A_GRP"], CFG["SPS_A_PORT"]   # funnel interleaves both paths and
        bg, bp = CFG["SPS_B_GRP"], CFG["SPS_B_PORT"]   # rtpjitterbuffer drops the duplicate copy,
        # which IS the 2022-7 merge -- first copy of each seq wins, so losing a whole path costs
        # nothing. 'pa'/'pb' are the live kill switches; 'ua'/'ub' are tapped for per-path rates.
        return (f"funnel name=fn ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d "
                f"udpsrc name=ua address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{SPS_CAPS}\" ! identity name=pa ! queue ! fn. "
                f"udpsrc name=ub address={bg} port={bp} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{SPS_CAPS}\" ! identity name=pb ! queue ! fn. "
                f"d. ! h264parse ! queue ! nvh264dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL} "
                f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {APLAY}")
    # jpegxs / unknown -> local test pattern, video only
    return (f"videotestsrc pattern=ball motion=sweep is-live=true ! video/x-raw,width=1920,height=1080,framerate=30/1 "
            f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert name=vpre ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H} ! {VTAIL}")

SRCNAME = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "reels": "Test Reels",
           "raw": "Pi raw 2110-20", "j2k": "JPEG 2000 island", "jpegxs": "JPEG XS codec",
           "h264": "H.264 over RTP", "mjpeg": "MJPEG over RTP", "vp9": "VP9 over RTP", "tsrtp": "TS over RTP", "fec": "ST 2022-1 FEC", "sps": "ST 2022-7 seamless"}
VCODEC = {"hevc": "HEVC / H.265", "jxs": "HEVC / H.265", "music": "HEVC / H.265", "reels": "HEVC / H.265",
          "raw": "Uncompressed RFC 4175", "j2k": "JPEG 2000", "jpegxs": "JPEG XS",
          "h264": "H.264 / AVC", "mjpeg": "Motion JPEG", "vp9": "VP9", "tsrtp": "H.264 / AVC", "fec": "H.264 / AVC", "sps": "H.264 / AVC"}
ACODEC = {"hevc": "AAC 5.1", "jxs": "MPEG audio (MP3)", "music": "MPEG audio (MP3)",
          "reels": "MPEG audio (MP3)", "music": "L24 PCM (2110-30)", "raw": "L24 PCM (2110-30)", "h264": "Opus", "tsrtp": "AAC", "fec": "AAC", "sps": "AAC"}
TRANSPORT = {"hevc": "MPEG-TS / UDP", "jxs": "MPEG-TS / UDP", "music": "MPEG-TS / UDP",
             "reels": "MPEG-TS / UDP", "raw": "ST 2110-20 RTP", "j2k": "J2K/RTP", "jpegxs": "local",
             "h264": "RTP (RFC 6184) + Opus RTP", "mjpeg": "RTP (RFC 2435)", "vp9": "RTP (RFC 7741)", "tsrtp": "MPEG-TS / RTP (ST 2022-2)", "fec": "TS/RTP + ST 2022-1 FEC", "sps": "TS/RTP x2 (ST 2022-7)"}

pipe = Gst.parse_launch(build())
ov = pipe.get_by_name("ov")
st = {"peak": [], "decay": [], "w": 1920, "h": 1080, "cap": "",
      "vw": 0, "vh": 0, "vfps": 0.0, "vfmt": "", "mbps": 0.0, "_bytes": 0, "arate": 0, "chan": ""}

def on_caps(_ov, caps):
    s = caps.get_structure(0); st["w"] = s.get_value("width"); st["h"] = s.get_value("height")
ov.connect("caps-changed", on_caps)

# --- live bitrate tap on the incoming stream ---
usrc = pipe.get_by_name("usrc")
if usrc:
    def on_bytes(_pad, info):
        b = info.get_buffer()
        if b: st["_bytes"] += b.get_size()
        return Gst.PadProbeReturn.OK
    usrc.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_bytes)
    def tick():
        st["mbps"] = round(st["_bytes"] * 8 / 1e6, 1); st["_bytes"] = 0
        if SRC == "hevc" and RUN:
            try: st["chan"] = open(os.path.join(RUN, "tv-channel")).read().strip()
            except Exception: pass
        return True
    GLib.timeout_add_seconds(1, tick)

# --- source video caps (resolution / framerate / pixel format, before our rescale) ---
def caps_probe(elem, name, fn):
    e = pipe.get_by_name(elem)
    if not e: return
    def cb(_pad, info):
        ev = info.get_event()
        if ev and ev.type == Gst.EventType.CAPS:
            fn(ev.parse_caps().get_structure(0))
        return Gst.PadProbeReturn.OK
    e.get_static_pad(name).add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, cb)
def set_vcaps(s):
    ok, w = s.get_int("width");  ok2, hh = s.get_int("height")
    if ok: st["vw"] = w
    if ok2: st["vh"] = hh
    okf, num, den = s.get_fraction("framerate")
    if okf and den: st["vfps"] = round(num / den, 2)
    st["vfmt"] = s.get_string("format") or st["vfmt"]
def set_acaps(s):
    okr, r = s.get_int("rate")
    if okr: st["arate"] = r
caps_probe("vpre", "sink", set_vcaps)
caps_probe("lvl", "src", set_acaps)

# --- live A/V TRIM (sync=true already lip-syncs from the source PTS; this only corrects any residual
# skew, e.g. if WSLg Pulse mis-reports its latency). echo <ms> > ~/atoll-run/tv-audio-delay-ms:
# POSITIVE delays audio, NEGATIVE advances it. 0 = pure PTS sync. Applied as a pad offset within ~1s.
aq = pipe.get_by_name("aq")
_aqpad = aq.get_static_pad("sink") if aq else None
ADELAY_FILE = os.path.join(RUN, "tv-audio-delay-ms")
def apply_adelay():
    try:
        ms = int(open(ADELAY_FILE).read().strip())
    except Exception:
        ms = 0
    if _aqpad:
        _aqpad.set_offset(ms * 1_000_000)   # +delay / -advance audio, both directions
    return True
if _aqpad:
    GLib.timeout_add_seconds(1, apply_adelay)
    apply_adelay()

# --- ST 2022-1 FEC demo knobs (source key "fec"), applied live, no restart ---
#   echo 0.03 > ~/atoll-run/fec-loss    # fraction of MEDIA packets to drop (0 = clean)
#   echo 0    > ~/atoll-run/fec-enable  # 0 gates the FEC flows off -> the damage becomes visible
# Gating the FEC streams (rather than rebuilding without the decoder) makes the comparison live: the
# same pipeline shows protected vs unprotected while the loss rate stays fixed.
_lossy = pipe.get_by_name("lossy")
_fecg = [pipe.get_by_name("fecg0"), pipe.get_by_name("fecg1")]
def _knob(name, default):
    try:
        return float(open(os.path.join(RUN, name)).read().strip())
    except Exception:
        return default
def apply_fec():
    if _lossy:
        _lossy.set_property("drop-probability", max(0.0, min(1.0, _knob("fec-loss", 0.0))))
    on = _knob("fec-enable", 1.0) >= 0.5
    for g in _fecg:
        if g:
            g.set_property("drop-probability", 0.0 if on else 1.0)
    return True
if _lossy:
    GLib.timeout_add_seconds(1, apply_fec)
    apply_fec()

# --- ST 2022-7 per-path stats + live kill switches (source key "sps") ---
#   echo 0 > ~/atoll-run/sps-a    # "pull the cable" on path A; the picture must not flinch
#   echo 0 > ~/atoll-run/sps-b
# Counters come from a byte/packet probe on each path's udpsrc, so the overlay can show which path
# is actually carrying -- the whole point being that the output is unaffected either way.
st["pa_pps"] = st["pb_pps"] = 0
st["_pa"] = st["_pb"] = 0
st["pa_on"] = st["pb_on"] = True
def _count(key):
    def cb(_pad, _info):
        st[key] += 1
        return Gst.PadProbeReturn.OK
    return cb
for _nm, _k in (("ua", "_pa"), ("ub", "_pb")):
    _e = pipe.get_by_name(_nm)
    if _e:
        _e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _count(_k))
_pgate = {"a": pipe.get_by_name("pa"), "b": pipe.get_by_name("pb")}
def apply_sps():
    st["pa_pps"], st["_pa"] = st["_pa"], 0
    st["pb_pps"], st["_pb"] = st["_pb"], 0
    for k in ("a", "b"):
        on = _knob(f"sps-{k}", 1.0) >= 0.5
        st[f"p{k}_on"] = on
        g = _pgate[k]
        if g:
            g.set_property("drop-probability", 0.0 if on else 1.0)
    return True
if _pgate["a"]:
    GLib.timeout_add_seconds(1, apply_sps)
    apply_sps()

# --- ST 2022-1 recovery counters (source key "fec"). Three taps tell the whole story:
#   usrc  = packets on the wire         (what the sender emitted)
#   lossy = packets after the injector  (what "the network" delivered)
#   fd    = packets out of the decoder  (what FEC handed downstream)
# recovered = fd - lossy, residual = wire - fd. This is the honest measure of what FEC is doing:
# unlike a picture it cannot be confounded by decoder concealment, display timing or GOP length,
# which is exactly why the visual version of this demo proved so unreliable.
st["fw_pps"] = st["fa_pps"] = st["fo_pps"] = 0
st["_fw"] = st["_fa"] = st["_fo"] = 0
for _nm, _k in (("usrc", "_fw"), ("lossy", "_fa"), ("fd", "_fo")):
    _e = pipe.get_by_name(_nm)
    if _e:
        _e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _count(_k))
# NB: counters are CUMULATIVE, never reset. The taps sit either side of a 4s buffer, so per-second
# differencing compares output against input from 4s earlier and yields nonsense (negative residual).
# Over a run the fixed offset becomes negligible and the totals read true.

LABELS = ["L", "R", "C", "LFE", "Ls", "Rs", "7", "8"]
def _draw_caption(ctx, text, w, h):
    """Guided-demo narration, burned onto the output as a bottom band (word-wrapped, centred)."""
    if not text:
        return
    ctx.save()
    ctx.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    fs = max(22, int(h * 0.032)); ctx.set_font_size(fs)
    maxw = w * 0.9; lines = []; cur = ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if cur and ctx.text_extents(t).width > maxw:
            lines.append(cur); cur = wd
        else:
            cur = t
    if cur:
        lines.append(cur)
    lh = fs * 1.35; pad = fs * 0.6; bh = lh * len(lines) + pad * 2
    y0 = h - bh - int(h * 0.03)
    ctx.set_source_rgba(0, 0, 0, 0.75); ctx.rectangle(0, y0, w, bh); ctx.fill()
    ctx.set_source_rgba(1, 1, 1, 0.98)
    for i, ln in enumerate(lines):
        tw = ctx.text_extents(ln).width
        ctx.move_to((w - tw) / 2, y0 + pad + lh * (i + 1) - fs * 0.35); ctx.show_text(ln)
    ctx.restore()

def on_draw(_ov, ctx, _ts, _dur):
    h = st["h"]
    _draw_caption(ctx, st.get("cap") or "", st.get("w") or 1920, h)
    # --- stream-info panel (top-left) ---
    lines = [SRCNAME.get(SRC, SRC) + (f"     ch {st['chan']}" if st["chan"] else "")]
    res = f"{st['vw']}x{st['vh']}" if st["vw"] else ""
    fps = f"{st['vfps']:g}p" if st["vfps"] else ""
    lines.append("  ".join(x for x in ("Video ", VCODEC.get(SRC, "?"), res, fps, st["vfmt"]) if x))
    if SRC in ACODEC:
        ach = len(st["peak"]) if st["peak"] else 0
        arate = f"{st['arate'] // 1000} kHz" if st["arate"] else ""
        lines.append("  ".join(x for x in ("Audio ", ACODEC[SRC], f"{ach} ch" if ach else "", arate) if x))
    if st["mbps"]:
        lines.append(f"Stream   {st['mbps']} Mbps   {TRANSPORT.get(SRC, '')}")
    if SRC == "fec":   # numeric proof of recovery, independent of how the picture happens to look
        wire, after, out = st["_fw"], st["_fa"], st["_fo"]
        dropped = max(0, wire - after)
        recovered = max(0, min(dropped, out - after))
        resid = (100.0 * max(0, wire - out) / wire) if wire > 0 else 0.0
        lines.append(f"Network  {wire:,} pkts    {dropped:,} dropped")
        lines.append(f"FEC      {recovered:,} recovered    residual {resid:.3f}%")
    if SRC == "sps":   # show which path is carrying; the picture is unaffected either way
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
    # --- VU meters (bottom-left) ---
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
    try:
        st["cap"] = open(os.path.join(RUN, "demo-caption")).read().strip()
    except Exception:
        st["cap"] = ""
    return True
_cap_tick(); GLib.timeout_add(400, _cap_tick)

bus = pipe.get_bus(); bus.add_signal_watch()
def on_msg(_b, msg):
    if msg.type == Gst.MessageType.ELEMENT:
        s = msg.get_structure()
        if s and s.get_name() == "level":
            try:
                st["peak"] = [float(x) for x in s.get_value("peak")]
                st["decay"] = [float(x) for x in s.get_value("decay")]
            except Exception:
                pass
bus.connect("message", on_msg)

pipe.set_state(Gst.State.PLAYING)
print(f"meter-view: {SRC} -> screen {SCREEN}", flush=True)
if IS_WSL and SCREEN != "0":
    def mover():
        try:
            from shutil import which
            pwsh = which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
            m = subprocess.check_output(["wslpath", "-w", os.path.join(HERE, "move-window-screen.ps1")], text=True).strip()
            subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", m, "-Screen", SCREEN, "-TimeoutSec", "15"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"(mover skipped: {e})", flush=True)
    threading.Thread(target=mover, daemon=True).start()
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipe.set_state(Gst.State.NULL)
