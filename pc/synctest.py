#!/usr/bin/env python3
# ===========================================================================
#  Atoll A/V SYNC TEST source. Generates a full-frame WHITE FLASH and a
#  coincident 1 kHz BEEP once per second (a "clapperboard"), encoded HEVC +
#  AAC in MPEG-TS to the REELS group. Select "Test Reels" on the panel, then
#  slide the A/V sync control until the flash and the beep land together.
#
#  The flash and the tick are both keyed to the pipeline running-time (second
#  boundaries), so they leave the encoder coincident -- any gap you see/hear at
#  the output is the rig's presentation offset, which the slider corrects. A
#  frame counter (0..29) shows the phase so you can read the offset in frames
#  (1 frame ~= 33 ms at 30 fps).
#
#  Software HEVC (x265enc) on purpose: the flash pattern is nearly static so it
#  is cheap, and it adds no NVENC session (the live senders already use several).
#  Usage: python3 synctest.py     (Ctrl-C to stop)
# ===========================================================================
import gi, os, math, subprocess
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
import cairo
Gst.init(None)

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "REELS_GRP", "REELS_PORT", "MCAST_TTL"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
IFACE = CFG.get("ISLAND_IFACE") or "eth0"
GRP = CFG.get("REELS_GRP") or "239.10.10.31"
PORT = CFG.get("REELS_PORT") or "5014"
TTL = CFG.get("MCAST_TTL") or "1"
W, H, FPS = 1280, 720, 30
FLASH_FRAMES = 2                      # ~66 ms flash at the second boundary

desc = (
    f"videotestsrc pattern=black is-live=true ! video/x-raw,width={W},height={H},framerate={FPS}/1 "
    f"! videoconvert ! cairooverlay name=ov ! videoconvert ! video/x-raw,format=I420 "
    f"! x265enc speed-preset=ultrafast tune=zerolatency bitrate=4000 key-int-max={FPS} ! h265parse config-interval=-1 ! queue ! mux. "
    # 1 kHz tick once per second, ~50 ms, coincident with the flash
    f"audiotestsrc wave=ticks tick-interval=1000000000 freq=1000 sine-periods-per-tick=50 volume=0.9 is-live=true "
    f"! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! avenc_aac ! aacparse ! queue ! mux. "
    f"mpegtsmux name=mux alignment=7 ! queue ! udpsink host={GRP} port={PORT} multicast-iface={IFACE} auto-multicast=true ttl={TTL}"
)
pipe = Gst.parse_launch(desc)
ov = pipe.get_by_name("ov")
st = {"w": W, "h": H}

def on_caps(_o, caps):
    s = caps.get_structure(0)
    st["w"], st["h"] = s.get_value("width"), s.get_value("height")
ov.connect("caps-changed", on_caps)

def on_draw(_o, ctx, ts, _dur):
    w, h = st["w"], st["h"]
    frame = int((ts / 1e9) * FPS + 0.5)
    fis = frame % FPS                 # frame within the current second (0..29)
    flash = fis < FLASH_FRAMES        # the clap: white frame + filled marker + tick all together
    if flash:
        ctx.set_source_rgb(1, 1, 1); ctx.rectangle(0, 0, w, h); ctx.fill()
    # centre marker: filled red on the clap, faint outline otherwise
    cx, cy, r = w / 2, h / 2, h * 0.16
    if flash:
        ctx.set_source_rgb(0.85, 0.1, 0.12); ctx.arc(cx, cy, r, 0, 2 * math.pi); ctx.fill()
    else:
        ctx.set_source_rgba(1, 1, 1, 0.55); ctx.set_line_width(6); ctx.arc(cx, cy, r, 0, 2 * math.pi); ctx.stroke()
    # title + phase readout (dark on the white flash so it stays legible)
    ctx.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_source_rgb(0, 0, 0) if flash else ctx.set_source_rgb(0.2, 0.9, 0.5)
    ctx.set_font_size(h * 0.055); ctx.move_to(w * 0.05, h * 0.13); ctx.show_text("A / V   S Y N C   T E S T")
    ctx.set_source_rgb(0, 0, 0) if flash else ctx.set_source_rgba(1, 1, 1, 0.9)
    ctx.set_font_size(h * 0.09); ctx.move_to(w * 0.05, h * 0.92); ctx.show_text(f"frame {fis:02d} / {FPS}")
    # a "beep is HERE" cue on the clap frames
    if flash:
        ctx.set_source_rgb(0.85, 0.1, 0.12); ctx.set_font_size(h * 0.08)
        t = "◉ FLASH + BEEP"; ext = ctx.text_extents(t)
        ctx.move_to((w - ext.width) / 2, cy + r + h * 0.11); ctx.show_text(t)
ov.connect("draw", on_draw)

bus = pipe.get_bus(); bus.add_signal_watch()
def on_msg(_b, m):
    if m.type == Gst.MessageType.ERROR:
        e, d = m.parse_error(); print(f"synctest ERROR: {e.message} :: {d}", flush=True)
bus.connect("message", on_msg)

pipe.set_state(Gst.State.PLAYING)
print(f"synctest: flash+beep -> {GRP}:{PORT}  (select 'Test Reels'; Ctrl-C to stop)", flush=True)
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipe.set_state(Gst.State.NULL)
