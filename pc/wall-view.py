#!/usr/bin/env python3
# ===========================================================================
#  Atoll WALL renderer -- the 2x2 multiview with the things a real multiviewer has that the
#  gst-launch version cannot do: a live TALLY border on the on-air tile, per-tile AUDIO METERS,
#  and a per-tile BITRATE readout.
#
#  Why Python: tally has to follow IS-05 takes *without* rebuilding the wall. A gst-launch string
#  cannot change a property after it starts, so the old multi layout could only have shown tally by
#  tearing the whole mosaic down on every take. Here the pipeline is built once and a cairooverlay
#  redraws tally/meters/bitrate every frame from state we update live.
#
#  Topology is deliberately identical to output-render.sh's multi (ONE pipeline, one compositor, no
#  intervideo bridges) -- that is the arrangement already proven stable on this rig.
#
#  Usage: wall-view.py <slots> [SCREEN]     slots = "tl,tr,bl,br" e.g. "hevc,raw,jxs,music"
#  Slot changes are handled by output-render.sh rebuilding us; we handle ACTIVE changes live.
# ===========================================================================
import gi, os, sys, subprocess, urllib.request, json, time
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
import cairo
Gst.init(None)

SLOTS = (sys.argv[1] if len(sys.argv) > 1 else "hevc,raw,jxs,music").split(",")
SLOTS = (SLOTS + ["hevc"] * 4)[:4]
SCREEN = sys.argv[2] if len(sys.argv) > 2 else "2"
HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "VIDEO_SINK", "ATOLL_PLATFORM", "ATOLL_RUN", "PANEL_PORT",
        "WALL_W", "WALL_H", "WALL_SYNC", "WALL_SW_DECODE", "IS07_PORT",
        "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT", "MUSIC_GRP", "MUSIC_PORT",
        "REELS_GRP", "REELS_PORT", "PI_RAW_GRP", "PI_RAW_PORT", "PI_AUDIO_GRP", "PI_AUDIO_PORT",
        "J2K_GRP", "J2K_PORT", "H264_GRP", "H264_PORT", "OPUS_GRP", "OPUS_PORT",
        "MJPEG_GRP", "MJPEG_PORT", "VP9_GRP", "VP9_PORT", "TSRTP_GRP", "TSRTP_PORT",
        "FEC_GRP", "FEC_PORT", "SPS_A_GRP", "SPS_A_PORT", "SPS_B_GRP", "SPS_B_PORT",
        "GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
for k in ("GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
    if CFG.get(k):
        os.environ[k] = CFG[k]
IFACE = CFG["ISLAND_IFACE"] or "eth0"
SINK = CFG["VIDEO_SINK"] or "waylandsink fullscreen=true"
# sync=false presents each frame the moment it arrives -- unpaced and unaligned to the display
# refresh, which tears moving objects (a horizontal split showing halves of two different frames).
# That is a DISPLAY artifact, nothing to do with the stream: it appears even when the transport is
# byte-perfect. sync=true paces presentation from each frame's PTS instead. WALL_SYNC=false restores
# the old behaviour if clock-syncing ever stalls on this WSLg path.
SYNC = "true" if (CFG.get("WALL_SYNC", "").lower() == "true") else "false"
# H.264 decode path for the tiles: GPU by default, software when WALL_SW_DECODE=true (diagnostic --
# isolates whether partial/black frame regions come from nvh264dec/cudadownload rather than the feed)
if CFG.get("WALL_SW_DECODE", "").lower() == "true":
    H264DEC = "avdec_h264 ! videoconvert"
else:
    H264DEC = "nvh264dec ! cudadownload"
IS_WSL = CFG["ATOLL_PLATFORM"] == "wsl"
RUN = CFG.get("ATOLL_RUN", "")
PANEL = f"http://localhost:{CFG.get('PANEL_PORT', '8096')}"
def grp(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

W = int(CFG.get("WALL_W") or 1920)
H = int(CFG.get("WALL_H") or 1080)
TW, TH = W // 2, H // 2              # each quadrant
S = W / 1920.0                       # overlay scale, so the UI keeps its proportions at any size
POS = [(0, 0), (TW, 0), (0, TH), (TW, TH)]
LABEL = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "reels": "Test Reels",
         "raw": "Pi raw 2110-20", "j2k": "JPEG 2000", "h264": "H.264 RTP", "mjpeg": "MJPEG RTP",
         "vp9": "VP9 RTP", "tsrtp": "TS over RTP", "fec": "ST 2022-1 FEC", "sps": "ST 2022-7 SPS", "jpegxs": "JPEG XS"}
RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,"
            "sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,"
            "colorimetry=(string)BT601-5,payload=(int)96")
FECSTREAM = "application/x-rtp,payload=96,clock-rate=90000"
MP2T = "application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33"

def udp(g, p, buf=8388608, caps=None, name=None):
    n = f"name={name} " if name else ""
    c = f'caps="{caps}" ' if caps else ""
    return (f"udpsrc {n}address={g} port={p} multicast-iface={IFACE} auto-multicast=true "
            f"buffer-size={buf} {c}")

def scale(i):
    return f"! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width={TW},height={TH} ! identity name=tap{i} ! queue leaky=downstream max-size-buffers=2 ! mix.sink_{i} "

def ts_tile(i, g, p, vparse, vdec):
    """MPEG-TS over plain UDP: video decoded, audio decoded only to feed this tile's level meter."""
    d = f"d{i}"
    return (udp(g, p, name=f"u{i}") + f"! tsdemux name={d} "
            f"{d}. ! {vparse} ! queue ! {vdec} ! cudadownload " + scale(i) +
            f"{d}. ! audio/mpeg ! queue ! decodebin ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")

def tile(i, src):
    """One quadrant. Returns a pipeline fragment ending at mix.sink_<i> (+ an audio meter branch)."""
    if src in ("hevc", "jxs", "music", "reels"):
        g, p = grp({"hevc": "HEVC", "jxs": "HOME", "music": "MUSIC", "reels": "REELS"}[src])
        if src == "music":   # video-only placeholder: no audio pad to demux (would hang preroll)
            d = f"d{i}"
            return (udp(g, p, name=f"u{i}") + f"! tsdemux name={d} {d}. ! h265parse ! queue ! nvh265dec ! cudadownload " + scale(i))
        return ts_tile(i, g, p, "h265parse", "nvh265dec")
    if src == "raw":         # ST 2110-20 video + the separate 2110-30 L24 audio flow for the meter
        g, p = grp("PI_RAW"); ag, ap = grp("PI_AUDIO")
        return (udp(g, p, buf=8388608, caps=RAW_CAPS, name=f"u{i}") +
                "! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert " + scale(i) +
                udp(ag, ap, buf=16777216, caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96") +
                f"! rtpjitterbuffer latency=500 ! rtpL24depay ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")
    if src == "j2k":
        g, p = grp("J2K")
        return (udp(g, p, caps="application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0", name=f"u{i}") +
                "! rtpj2kdepay ! avdec_jpeg2000 " + scale(i))
    if src == "h264":        # video RTP + its separate Opus RTP audio essence -> meter
        g, p = grp("H264"); ag, ap = grp("OPUS")
        return (udp(g, p, caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96", name=f"u{i}") +
                "! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! nvh264dec ! cudadownload " + scale(i) +
                udp(ag, ap, caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97") +
                f"! rtpjitterbuffer latency=200 ! rtpopusdepay ! opusdec ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")
    if src == "mjpeg":
        g, p = grp("MJPEG")
        return (udp(g, p, buf=16777216, caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=96", name=f"u{i}") +
                "! rtpjitterbuffer latency=100 ! rtpjpegdepay ! nvjpegdec " + scale(i))
    if src == "vp9":
        g, p = grp("VP9")
        return (udp(g, p, caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=VP9,payload=96", name=f"u{i}") +
                "! rtpjitterbuffer latency=100 ! rtpvp9depay ! vp9parse ! nvvp9dec " + scale(i))
    if src in ("tsrtp", "fec"):   # TS inside RTP; fec additionally recombines the 2022-1 flows
        d = f"d{i}"
        if src == "tsrtp":
            g, p = grp("TSRTP")
            head = udp(g, p, caps=MP2T, name=f"u{i}") + "! rtpjitterbuffer latency=200 ! rtpmp2tdepay "
        else:
            g, p = grp("FEC"); cp, rp = int(p) + 2, int(p) + 4
            head = (udp(g, p, caps=MP2T, name=f"u{i}") + f"! identity name=flossy{i} ! rtpst2022-1-fecdec name=fd{i} " +
                    udp(g, cp, caps=FECSTREAM) + f"! identity name=fg0_{i} ! queue ! fd{i}.fec_0 " +
                    udp(g, rp, caps=FECSTREAM) + f"! identity name=fg1_{i} ! queue ! fd{i}.fec_1 " +
                    f"fd{i}. ! rtpjitterbuffer latency=500 max-misorder-time=5000 max-dropout-time=5000 ! identity name=fjb{i} ! rtpmp2tdepay ")
        return (head + f"! tsdemux name={d} "
                f"{d}. ! h264parse ! queue ! {H264DEC} " + scale(i) +
                f"{d}. ! audio/mpeg ! queue ! decodebin ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")
    if src == "sps":     # both paths funnelled; the jitterbuffer drops the duplicate copy
        d = f"d{i}"
        return (f"funnel name=fn{i} ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name={d} " +
                udp(CFG["SPS_A_GRP"], CFG["SPS_A_PORT"], caps=MP2T, name=f"u{i}") + f"! identity name=spa{i} ! queue ! fn{i}. " +
                udp(CFG["SPS_B_GRP"], CFG["SPS_B_PORT"], caps=MP2T) + f"! identity name=spb{i} ! queue ! fn{i}. " +
                f"{d}. ! h264parse ! queue ! {H264DEC} " + scale(i) +
                f"{d}. ! audio/mpeg ! queue ! decodebin ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")
    # jpegxs / unknown -> local encode->decode demo pattern
    return (f"videotestsrc pattern=ball motion=sweep is-live=true ! video/x-raw,width={TW},height={TH},framerate=30/1 "
            f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert " + scale(i))

sinkprops = " ".join(f"sink_{i}::xpos={x} sink_{i}::ypos={y}" for i, (x, y) in enumerate(POS))
desc = (f"compositor name=mix ignore-inactive-pads=true background=black {sinkprops} "
        # NOTE: do NOT ask the compositor for BGRA to "save" the pre-overlay conversion. Measured, that
        # is WORSE (296% vs 238% CPU at 2560x1440): compositing in 4-byte BGRA moves 2.67x more
        # data per pixel than YUV, which costs more than the conversion it avoids.
        f"! video/x-raw,width={W},height={H} ! videoconvert ! cairooverlay name=ov ! videoconvert ! {SINK} sync={SYNC} "
        + "".join(tile(i, s) for i, s in enumerate(SLOTS)))
pipe = Gst.parse_launch(desc)
ov = pipe.get_by_name("ov")

# ---- live state the overlay draws from ----
st = {"active": "", "peak": [[] for _ in range(4)], "bytes": [0] * 4, "mbps": [0.0] * 4,
      "w": W, "h": H, "chan": ""}

def on_caps(_ov, caps):
    s = caps.get_structure(0)
    st["w"], st["h"] = s.get_value("width"), s.get_value("height")
ov.connect("caps-changed", on_caps)

for i in range(4):                       # per-tile bitrate tap
    u = pipe.get_by_name(f"u{i}")
    if u:
        def mk(idx):
            def cb(_pad, info):
                b = info.get_buffer()
                if b:
                    st["bytes"][idx] += b.get_size()
                return Gst.PadProbeReturn.OK
            return cb
        u.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, mk(i))

# --- ST 2022-1 recovery counters for whichever tile holds the fec source. Same three taps as the
# single view: wire -> after the loss injector -> out of the decoder. Showing recovery as NUMBERS
# is the reliable demo; the picture alone cannot distinguish "FEC repaired it" from "the decoder
# concealed it", which is why the visual version was so unconvincing.
for _k in ("fw", "fa", "fo", "fj"):
    st[_k] = [0] * 4
    st["_" + _k] = [0] * 4
st["fps"] = [0.0] * 4
st["_fc"] = [0] * 4
def _fpsn(idx):
    def cb(_pad, _info):
        st["_fc"][idx] += 1
        return Gst.PadProbeReturn.OK
    return cb
for _i in range(4):
    _e = pipe.get_by_name(f"tap{_i}")
    if _e:
        _e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _fpsn(_i))

st["reord"] = [0] * 4
st["_hi"] = [None] * 4
def _tapn(key, idx):
    def cb(_pad, _info):
        st[key][idx] += 1
        return Gst.PadProbeReturn.OK
    return cb

def _ordertap(idx):
    """Count out-of-order RTP arrivals at the depayloader input for this tile."""
    import struct as _st
    def cb(_pad, info):
        b = info.get_buffer()
        ok, mi = b.map(Gst.MapFlags.READ)
        if ok:
            try:
                d = mi.data
                if len(d) >= 4:
                    seq = _st.unpack("!H", d[2:4])[0]
                    hi = st["_hi"][idx]
                    if hi is not None:
                        back = (hi - seq) & 0xFFFF
                        if 0 < back < 4000:
                            st["reord"][idx] += 1
                    if hi is None or ((seq - hi) & 0xFFFF) < 4000:
                        st["_hi"][idx] = seq
            finally:
                b.unmap(mi)
        return Gst.PadProbeReturn.OK
    return cb
for _i in range(4):
    for _nm, _key in ((f"u{_i}", "_fw"), (f"flossy{_i}", "_fa"), (f"fd{_i}", "_fo"), (f"fjb{_i}", "_fj")):
        _e = pipe.get_by_name(_nm)
        if _e:
            _e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _tapn(_key, _i))
            if _nm.startswith("fjb"):
                _e.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _ordertap(_i))

def tick():
    for i in range(4):
        st["mbps"][i] = round(st["bytes"][i] * 8 / 1e6, 1)
        st["bytes"][i] = 0
        st["fps"][i] = st["_fc"][i]; st["_fc"][i] = 0
        for k in ("fw", "fa", "fo", "fj"):          # CUMULATIVE totals: the taps straddle the fecdec
            st[k][i] = st["_" + k][i]        # buffer, so per-second differencing is meaningless
    if RUN:
        try:
            st["chan"] = open(os.path.join(RUN, "tv-channel")).read().strip()
        except Exception:
            pass
    return True
GLib.timeout_add_seconds(1, tick)

def _knob(name, default):
    try:
        return float(open(os.path.join(RUN, name)).read().strip())
    except Exception:
        return default

# The panel's FEC loss / FEC on-off / 2022-7 path buttons write these knob files; meter-view honours
# them in single view and, without this, the WALL ignored them -- so pulling a path did nothing to
# the tile you were actually watching. Same files, same live behaviour, applied per tile.
_gates = {"floss": [], "fgate": [], "spa": [], "spb": []}
for _i in range(4):
    for _nm, _key in ((f"flossy{_i}", "floss"), (f"spa{_i}", "spa"), (f"spb{_i}", "spb")):
        _e = pipe.get_by_name(_nm)
        if _e:
            _gates[_key].append(_e)
    for _nm in (f"fg0_{_i}", f"fg1_{_i}"):
        _e = pipe.get_by_name(_nm)
        if _e:
            _gates["fgate"].append(_e)

def apply_knobs():
    loss = max(0.0, min(1.0, _knob("fec-loss", 0.0)))
    for e in _gates["floss"]:
        e.set_property("drop-probability", loss)
    fec_on = _knob("fec-enable", 1.0) >= 0.5
    for e in _gates["fgate"]:
        e.set_property("drop-probability", 0.0 if fec_on else 1.0)
    for key, knob in (("spa", "sps-a"), ("spb", "sps-b")):
        up = _knob(knob, 1.0) >= 0.5
        for e in _gates[key]:
            e.set_property("drop-probability", 0.0 if up else 1.0)
    return True
if any(_gates.values()):
    GLib.timeout_add_seconds(1, apply_knobs)
    apply_knobs()

# --- NMOS IS-07 tally receiver -------------------------------------------------------------
# Tally used to be read from the panel's own /state, which is an internal shortcut: correct on
# screen, but nothing another device could consume. Each tile now follows the IS-07 boolean event
# source for ITS source key, so the wall is a real IS-07 receiver rather than a program reading its
# own variable. Source ids are UUID5-derived from the key exactly as the emitter derives them --
# equivalent to a receiver being configured with the source_id it should follow.
import uuid as _uuid
IS07 = f"http://localhost:{CFG.get('IS07_PORT') or 8102}"
_NS = _uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
def _src_id(key):
    return str(_uuid.uuid5(_NS, f"atoll:is07:tally:{key}"))
st["tally"] = [False] * 4
st["is07"] = False                       # whether the emitter is answering

def poll_active():                       # tally per tile, straight from IS-07 state
    ok = False
    for i, key in enumerate(SLOTS):
        try:
            with urllib.request.urlopen(f"{IS07}/x-nmos/events/v1.0/sources/{_src_id(key)}/state", timeout=2) as r:
                msg = json.load(r)
            st["tally"][i] = bool(msg.get("payload", {}).get("value"))
            ok = True
        except Exception:
            pass
    st["is07"] = ok
    if not ok:                           # emitter down -> fall back to the panel so tally still works
        try:
            with urllib.request.urlopen(f"{PANEL}/state", timeout=2) as r:
                a = json.load(r).get("active", "")
            for i, key in enumerate(SLOTS):
                st["tally"][i] = (key == a)
        except Exception:
            pass
    return True
GLib.timeout_add_seconds(1, poll_active)
poll_active()

# Double-buffered overlay. The DRAWING (~30-60ms of Python+cairo text) happens on the GLib main
# loop, never on the streaming thread: on_draw only ever blits an already-rendered surface, so the
# frame budget is not hostage to how long the overlay takes to compose. Two surfaces are alternated
# so a blit can never read the one being drawn into.
_ovbufs = [None, None]
_ovidx = 0
_ovsurf = None
_ovsize = (0, 0)

def _render_overlay():
    """Compose the overlay on the MAIN LOOP into the spare buffer, then publish it."""
    global _ovidx, _ovsurf, _ovsize, _ovbufs
    w, h = int(st["w"]), int(st["h"])
    if w <= 0 or h <= 0:
        return True
    if _ovsize != (w, h):
        _ovbufs = [cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h) for _ in range(2)]
        _ovsize = (w, h)
    spare = 1 - _ovidx
    surf = _ovbufs[spare]
    c = cairo.Context(surf)
    c.set_operator(cairo.OPERATOR_CLEAR); c.paint()
    c.set_operator(cairo.OPERATOR_OVER)
    _draw_overlay(c)
    surf.flush()
    _ovidx = spare
    _ovsurf = surf          # publish; rebinding a name is atomic under the GIL
    return True

def on_draw(_ov, ctx, _ts, _dur):
    surf = _ovsurf
    if surf is None:
        return
    # Blit ONLY the regions that carry overlay. Painting a full-screen ARGB surface every frame was
    # itself ~20ms (8MB of alpha compositing in cairo's software renderer).
    sx, sy = st["w"] / W, st["h"] / H
    ctx.set_source_surface(surf, 0, 0)
    b = 8 * S
    for i in range(4):
        x, y = POS[i]
        ctx.rectangle(x * sx, (y + TH - 95 * S) * sy, TW * sx, 95 * S * sy)   # UMD / fps / FEC / meters
        ctx.rectangle(x * sx, y * sy, 130 * S * sx, 46 * S * sy)              # ON AIR flag
        ctx.rectangle(x * sx, y * sy, TW * sx, b * sy)                        # tally border: top
        ctx.rectangle(x * sx, (y + TH - b) * sy, TW * sx, b * sy)             #               bottom
        ctx.rectangle(x * sx, y * sy, b * sx, TH * sy)                        #               left
        ctx.rectangle((x + TW - b) * sx, y * sy, b * sx, TH * sy)             #               right
    ctx.rectangle((W - 130 * S) * sx, 0, 130 * S * sx, 46 * S * sy)           # ATOLL bug
    ctx.fill()

def _draw_overlay(ctx):
    sx, sy = st["w"] / W, st["h"] / H
    ctx.save(); ctx.scale(sx, sy)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    for i, src in enumerate(SLOTS):
        x, y = POS[i]
        name = LABEL.get(src, src)
        if src == "hevc" and st["chan"]:
            name += f"  ch {st['chan']}"
        # --- tally: red border + ON AIR flag on the tile that is currently taken ---
        if st["tally"][i]:
            ctx.set_source_rgba(1, 0.1, 0.1, 0.95); ctx.set_line_width(6 * S)
            ctx.rectangle(x + 3 * S, y + 3 * S, TW - 6 * S, TH - 6 * S); ctx.stroke()
            # top-LEFT of the tile: the wall's ATOLL bug sits top-right and would collide there
            ctx.set_source_rgba(0.85, 0.05, 0.05, 0.92)
            ctx.rectangle(x + 10 * S, y + 10 * S, 96 * S, 26 * S); ctx.fill()
            ctx.set_source_rgba(1, 1, 1, 1); ctx.set_font_size(15 * S)
            ctx.move_to(x + 22 * S, y + 29 * S); ctx.show_text("ON AIR")
        # --- UMD label + live bitrate ---
        ctx.set_source_rgba(0, 0, 0, 0.55); ctx.rectangle(x + 8 * S, y + TH - 40 * S, 320 * S, 30 * S); ctx.fill()
        ctx.set_source_rgba(1, 1, 1, 0.95); ctx.set_font_size(17 * S)
        ctx.move_to(x + 16 * S, y + TH - 19 * S); ctx.show_text(name)
        ctx.set_source_rgba(0.35, 0.85, 0.7, 0.95); ctx.set_font_size(14 * S)
        ctx.move_to(x + 240 * S, y + TH - 19 * S); ctx.show_text(f"{st['mbps'][i]:.1f} Mb/s")
        f = st["fps"][i]
        if f:                                  # a tile short of 30fps is dropping frames
            ctx.set_source_rgba(1, 0.45, 0.35, 0.98) if f < 28 else ctx.set_source_rgba(0.35, 0.85, 0.7, 0.95)
            ctx.move_to(x + 360 * S, y + TH - 19 * S); ctx.show_text(f"{f:.0f} fps")
        if src == "fec" and st["fw"][i]:      # numeric proof of ST 2022-1 recovery
            wire, after, out = st["fw"][i], st["fa"][i], st["fo"][i]
            deliv = st["fj"][i] or out          # after the jitterbuffer = what the decoder really got
            dropped = max(0, wire - after); recovered = max(0, min(dropped, deliv - after))
            resid = (100.0 * max(0, wire - deliv) / wire) if wire > 0 else 0.0
            ctx.set_source_rgba(0, 0, 0, 0.55); ctx.rectangle(x + 8 * S, y + TH - 74 * S, 330 * S, 30 * S); ctx.fill()
            ctx.set_source_rgba(1, 0.75, 0.4, 0.95) if (resid > 0.01 or st["reord"][i]) else ctx.set_source_rgba(0.45, 0.95, 0.55, 0.95)
            ctx.set_font_size(14 * S)
            ctx.move_to(x + 16 * S, y + TH - 54 * S)
            ctx.show_text(f"dropped {dropped:,}  recovered {recovered:,}  resid {resid:.3f}%  reord {st['reord'][i]:,}")
        # --- per-tile audio meters (one slim bar per channel, bottom-right of the tile) ---
        pk = st["peak"][i]
        if pk:
            n = min(len(pk), 6); bw, gap, maxh = 7 * S, 4 * S, 54 * S
            mx = x + TW - (n * (bw + gap)) - 14 * S; base = y + TH - 14 * S
            ctx.set_source_rgba(0, 0, 0, 0.4)
            ctx.rectangle(mx - 8 * S, base - maxh - 8 * S, n * (bw + gap) + 12 * S, maxh + 14 * S); ctx.fill()
            for c in range(n):
                db = max(-60.0, min(0.0, pk[c]))
                frac = (db + 60.0) / 60.0
                bh = max(1, int(maxh * frac))
                bx = mx + c * (bw + gap)
                ctx.set_source_rgba(1, 1, 1, 0.14); ctx.rectangle(bx, base - maxh, bw, maxh); ctx.fill()
                r = frac
                ctx.set_source_rgba(0.15 + 0.8 * r, 0.8 - 0.45 * r, 0.15, 0.92)
                ctx.rectangle(bx, base - bh, bw, bh); ctx.fill()
    # --- ATOLL bug ---
    ctx.set_source_rgba(1, 1, 1, 0.5); ctx.set_font_size(20 * S)
    ctx.move_to(W - 108 * S, 34 * S); ctx.show_text("ATOLL")
    if st.get("is07"):
        ctx.set_source_rgba(0.45, 0.95, 0.55, 0.75); ctx.set_font_size(12 * S)
        ctx.move_to(W - 190 * S, 50 * S); ctx.show_text("IS-07 tally")
    ctx.restore()
ov.connect("draw", on_draw)
_render_overlay()                        # compose once up front so the first frames have an overlay
GLib.timeout_add(100, _render_overlay)   # thereafter 10Hz, on the main loop, off the streaming thread

bus = pipe.get_bus(); bus.add_signal_watch()
def on_msg(_b, msg):
    if msg.type == Gst.MessageType.ELEMENT:
        s = msg.get_structure()
        if s and s.get_name() == "level":
            nm = msg.src.get_name()          # lvl<i> -> that tile's meters
            if nm.startswith("lvl"):
                try:
                    st["peak"][int(nm[3:])] = [float(v) for v in s.get_value("peak")]
                except Exception:
                    pass
    elif msg.type == Gst.MessageType.ERROR:
        e, dbg = msg.parse_error()
        print(f"wall-view ERROR: {e.message} :: {dbg}", flush=True)
bus.connect("message", on_msg)

pipe.set_state(Gst.State.PLAYING)
print(f"wall-view: {','.join(SLOTS)} -> screen {SCREEN}", flush=True)
if IS_WSL and SCREEN != "0":
    def mover():
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", subprocess.check_output(["wslpath", "-w", os.path.join(HERE, "move-window-screen.ps1")], text=True).strip(),
                              "-Screen", SCREEN, "-TimeoutSec", "12"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return False
    GLib.timeout_add_seconds(2, mover)
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipe.set_state(Gst.State.NULL)
