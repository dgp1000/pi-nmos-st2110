#!/usr/bin/env python3
# ===========================================================================
#  Atoll single-view renderer WITH audio VU meters. Drop-in for output-render.sh's single layout:
#  decodes the active source (video + audio), plays synced audio, and draws one VU bar per audio
#  channel over the picture via cairooverlay + the level element (auto-detects channel count, so a
#  5.1 source shows 6 bars, stereo shows 2). Follows the active source (output-render passes it in).
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
NEED = ["ISLAND_IFACE", "VIDEO_SINK", "ATOLL_PLATFORM", "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT",
        "MUSIC_GRP", "MUSIC_PORT", "REELS_GRP", "REELS_PORT", "PI_RAW_GRP", "PI_RAW_PORT",
        "PI_AUDIO_GRP", "PI_AUDIO_PORT", "J2K_GRP", "J2K_PORT",
        "GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
for k in ("GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
    if CFG.get(k):
        os.environ[k] = CFG[k]
IFACE = CFG["ISLAND_IFACE"] or "eth0"
SINK = CFG["VIDEO_SINK"] or "waylandsink fullscreen=true"
IS_WSL = CFG["ATOLL_PLATFORM"] == "wsl"
def grp(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,"
            "sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,"
            "colorimetry=(string)BT601-5,payload=(int)96")
J2K_CAPS = "application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0"
BRAND = ("textoverlay text=ATOLL valignment=top halignment=right ypad=18 xpad=28 "
         "font-desc='Sans Bold 20' color=0x80ffffff shaded-background=false")
# video ends at a named cairooverlay 'ov' -> BRAND -> sink ; audio runs through a 'level' then plays.
VTAIL = f"videoconvert ! cairooverlay name=ov ! {BRAND} ! videoconvert ! {SINK} sync=true"
ALEVEL = "audioconvert ! level name=lvl post-messages=true interval=50000000 ! audioresample"

def ts_pipeline(g, p):   # HEVC video + MP3 audio in a TS (Live TV / Home / Music / Reels)
    return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d "
            f"d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL} "
            f"d. ! queue ! mpegaudioparse ! mpg123audiodec ! {ALEVEL} ! autoaudiosink sync=true")

def build():
    if SRC in ("hevc", "jxs", "music", "reels"):
        g, p = grp({"hevc": "HEVC", "jxs": "HOME", "music": "MUSIC", "reels": "REELS"}[SRC])
        return ts_pipeline(g, p)
    if SRC == "raw":   # Pi RTP video + the separate ST 2110-30 L24 audio flow
        g, p = grp("PI_RAW"); ag, ap = grp("PI_AUDIO")
        return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true caps=\"{RAW_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL} "
                f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
                f"caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96\" "
                f"! rtpjitterbuffer latency=500 ! rtpL24depay ! {ALEVEL} ! autoaudiosink sync=true")
    if SRC == "j2k":   # video only (no audio) -> meters idle
        g, p = grp("J2K")
        return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{J2K_CAPS}\" "
                f"! rtpj2kdepay ! avdec_jpeg2000 ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL}")
    # jpegxs / unknown -> local test pattern, video only
    return (f"videotestsrc pattern=ball is-live=true ! video/x-raw,width=1920,height=1080,framerate=30/1 "
            f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL}")

pipe = Gst.parse_launch(build())
ov = pipe.get_by_name("ov")
st = {"peak": [], "decay": [], "w": 1920, "h": 1080}

def on_caps(_ov, caps):
    s = caps.get_structure(0); st["w"] = s.get_value("width"); st["h"] = s.get_value("height")
ov.connect("caps-changed", on_caps)

LABELS = ["L", "R", "C", "LFE", "Ls", "Rs", "7", "8"]
def on_draw(_ov, ctx, _ts, _dur):
    peaks = st["decay"] or st["peak"]
    if not peaks:
        return
    h = st["h"]; n = len(peaks)
    bw, gap, x0, maxh = 34, 10, 34, int(h * 0.34)
    baseY = h - 46
    ctx.set_source_rgba(0, 0, 0, 0.35); ctx.rectangle(x0 - 12, baseY - maxh - 14, n * (bw + gap) + 14, maxh + 46); ctx.fill()
    ctx.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD); ctx.set_font_size(16)
    for i, db in enumerate(peaks):
        lvl = max(0.0, min(1.0, (db + 54.0) / 54.0)); bh = int(lvl * maxh); x = x0 + i * (bw + gap)
        ctx.set_source_rgba(1, 1, 1, 0.14); ctx.rectangle(x, baseY - maxh, bw, maxh); ctx.fill()
        r = min(1.0, max(0.0, (lvl - 0.75) / 0.25))
        ctx.set_source_rgba(0.15 + 0.8 * r, 0.8 - 0.45 * r, 0.15, 0.92)
        ctx.rectangle(x, baseY - bh, bw, bh); ctx.fill()
        ctx.set_source_rgba(1, 1, 1, 0.9)
        lbl = LABELS[i] if i < len(LABELS) else str(i + 1)
        ctx.move_to(x + bw / 2 - 6, baseY + 20); ctx.show_text(lbl)
ov.connect("draw", on_draw)

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
