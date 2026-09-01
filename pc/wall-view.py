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
IS_WSL = CFG["ATOLL_PLATFORM"] == "wsl"
RUN = CFG.get("ATOLL_RUN", "")
PANEL = f"http://localhost:{CFG.get('PANEL_PORT', '8096')}"
def grp(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

W, H = 1920, 1080
TW, TH = W // 2, H // 2              # each quadrant
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
    return f"! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width={TW},height={TH} ! queue leaky=downstream max-size-buffers=2 ! mix.sink_{i} "

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
            head = (udp(g, p, caps=MP2T, name=f"u{i}") + f"! rtpst2022-1-fecdec name=fd{i} " +
                    udp(g, cp, caps=FECSTREAM) + f"! queue ! fd{i}.fec_0 " +
                    udp(g, rp, caps=FECSTREAM) + f"! queue ! fd{i}.fec_1 " +
                    f"fd{i}. ! rtpmp2tdepay ")
        return (head + f"! tsdemux name={d} "
                f"{d}. ! h264parse ! queue ! nvh264dec ! cudadownload " + scale(i) +
                f"{d}. ! audio/mpeg ! queue ! decodebin ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")
    if src == "sps":     # both paths funnelled; the jitterbuffer drops the duplicate copy
        d = f"d{i}"
        return (f"funnel name=fn{i} ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name={d} " +
                udp(CFG["SPS_A_GRP"], CFG["SPS_A_PORT"], caps=MP2T, name=f"u{i}") + f"! queue ! fn{i}. " +
                udp(CFG["SPS_B_GRP"], CFG["SPS_B_PORT"], caps=MP2T) + f"! queue ! fn{i}. " +
                f"{d}. ! h264parse ! queue ! nvh264dec ! cudadownload " + scale(i) +
                f"{d}. ! audio/mpeg ! queue ! decodebin ! audioconvert ! level name=lvl{i} post-messages=true interval=100000000 ! fakesink sync=false ")
    # jpegxs / unknown -> local encode->decode demo pattern
    return (f"videotestsrc pattern=ball motion=sweep is-live=true ! video/x-raw,width={TW},height={TH},framerate=30/1 "
            f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert " + scale(i))

sinkprops = " ".join(f"sink_{i}::xpos={x} sink_{i}::ypos={y}" for i, (x, y) in enumerate(POS))
desc = (f"compositor name=mix ignore-inactive-pads=true background=black {sinkprops} "
        f"! video/x-raw,width={W},height={H} ! videoconvert ! cairooverlay name=ov ! videoconvert ! {SINK} sync=false "
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

def tick():
    for i in range(4):
        st["mbps"][i] = round(st["bytes"][i] * 8 / 1e6, 1)
        st["bytes"][i] = 0
    if RUN:
        try:
            st["chan"] = open(os.path.join(RUN, "tv-channel")).read().strip()
        except Exception:
            pass
    return True
GLib.timeout_add_seconds(1, tick)

def poll_active():                       # tally follows IS-05 takes, no rebuild
    try:
        with urllib.request.urlopen(f"{PANEL}/state", timeout=2) as r:
            st["active"] = json.load(r).get("active", "")
    except Exception:
        pass
    return True
GLib.timeout_add_seconds(1, poll_active)
poll_active()

def on_draw(_ov, ctx, _ts, _dur):
    sx, sy = st["w"] / W, st["h"] / H
    ctx.save(); ctx.scale(sx, sy)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    for i, src in enumerate(SLOTS):
        x, y = POS[i]
        name = LABEL.get(src, src)
        if src == "hevc" and st["chan"]:
            name += f"  ch {st['chan']}"
        # --- tally: red border + ON AIR flag on the tile that is currently taken ---
        if src and src == st["active"]:
            ctx.set_source_rgba(1, 0.1, 0.1, 0.95); ctx.set_line_width(6)
            ctx.rectangle(x + 3, y + 3, TW - 6, TH - 6); ctx.stroke()
            # top-LEFT of the tile: the wall's ATOLL bug sits top-right and would collide there
            ctx.set_source_rgba(0.85, 0.05, 0.05, 0.92)
            ctx.rectangle(x + 10, y + 10, 96, 26); ctx.fill()
            ctx.set_source_rgba(1, 1, 1, 1); ctx.set_font_size(15)
            ctx.move_to(x + 22, y + 29); ctx.show_text("ON AIR")
        # --- UMD label + live bitrate ---
        ctx.set_source_rgba(0, 0, 0, 0.55); ctx.rectangle(x + 8, y + TH - 40, 320, 30); ctx.fill()
        ctx.set_source_rgba(1, 1, 1, 0.95); ctx.set_font_size(17)
        ctx.move_to(x + 16, y + TH - 19); ctx.show_text(name)
        ctx.set_source_rgba(0.35, 0.85, 0.7, 0.95); ctx.set_font_size(14)
        ctx.move_to(x + 240, y + TH - 19); ctx.show_text(f"{st['mbps'][i]:.1f} Mb/s")
        # --- per-tile audio meters (one slim bar per channel, bottom-right of the tile) ---
        pk = st["peak"][i]
        if pk:
            n = min(len(pk), 6); bw, gap, maxh = 7, 4, 54
            mx = x + TW - (n * (bw + gap)) - 14; base = y + TH - 14
            ctx.set_source_rgba(0, 0, 0, 0.4)
            ctx.rectangle(mx - 8, base - maxh - 8, n * (bw + gap) + 12, maxh + 14); ctx.fill()
            for c in range(n):
                db = max(-60.0, min(0.0, pk[c]))
                frac = (db + 60.0) / 60.0
                bh = max(1, int(maxh * frac))
                bx = mx + c * (bw + gap)
                ctx.set_source_rgba(1, 1, 1, 0.14); ctx.rectangle(bx, base - maxh, bw, maxh); ctx.fill()
                r = frac
                ctx.set_source_rgba(0.15 + 0.8 * r, 0.8 - 0.45 * r, 0.15, 0.92)
                ctx.rectangle(bx, base - bh, bw, bh); ctx.fill()
    # --- centre clock (kept from the gst-launch wall) ---
    ctx.set_source_rgba(0, 0, 0, 0.45); ctx.rectangle(W / 2 - 118, H * 0.33, 236, 54); ctx.fill()
    ctx.set_source_rgba(1, 1, 1, 0.96); ctx.set_font_size(34)
    ctx.move_to(W / 2 - 98, H * 0.33 + 38); ctx.show_text(time.strftime("%H:%M:%S"))
    ctx.set_source_rgba(0.8, 0.8, 0.8, 0.8); ctx.set_font_size(13)
    ctx.move_to(W / 2 - 44, H * 0.33 + 54 + 16); ctx.show_text("Pi5 PTP GM")
    # --- ATOLL bug ---
    ctx.set_source_rgba(1, 1, 1, 0.5); ctx.set_font_size(20)
    ctx.move_to(W - 108, 34); ctx.show_text("ATOLL")
    ctx.restore()
ov.connect("draw", on_draw)

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
