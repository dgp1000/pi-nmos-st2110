#!/usr/bin/env python3
# ===========================================================================
#  Atoll multiview — per-tile process-isolated renderer (replaces output-render.sh)
#
#  One PERSISTENT compositor pipeline (intervideosrc x4 -> compositor -> sink) plus one
#  INDEPENDENT GstPipeline per visible tile (source -> decode -> intervideosink channel=vN).
#  Because each tile is its own pipeline, a tile's decoder erroring on a channel change (or
#  its source dropping) restarts ONLY that tile -- the compositor and the other tiles never
#  even notice. This is what the single gst-launch pipeline could not do.
#
#  Follows the panel /state {active, layout, slots} exactly like output-render.sh:
#    single -> active source fullscreen        (v0)
#    side   -> Live TV | Pi raw                 (v0,v1)
#    multi  -> 2x2 mosaic of the four slots     (v0..v3)
#
#  Audio: a single follower for the active source, with a LIVE-tunable lip-sync delay --
#  write milliseconds to $ATOLL_RUN/audio-delay-ms and it applies without a restart.
#
#  Run in a LOCAL WSL terminal:  python3 pc/multiview-app.py [SCREEN]   (SCREEN=2 default)
#
#  Verified: all four tiles render, and a live TV channel change blips ONLY the Live TV tile for
#  ~1s (its decoder throws "Internal data stream error" on the retune -> that one pipeline restarts
#  and self-recovers) while the compositor holds ~30fps and the other three tiles never flinch.
#  Key detail: tile intervideosinks MUST be sync=false -- sync=true stalls live-source preroll and
#  was the sole reason tiles came up black earlier (it was never an intervideo connection race).
# ===========================================================================
import gi, os, sys, time, subprocess, threading
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
Gst.init(None)

SCREEN = sys.argv[1] if len(sys.argv) > 1 else "2"
HERE = os.path.dirname(os.path.abspath(__file__))

# --- pull every value we need straight out of atoll.conf (single source of truth) ---
NEED = ["ISLAND_IFACE", "VIDEO_SINK", "PANEL_PORT", "ATOLL_RUN", "ATOLL_PLATFORM",
        "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT", "MUSIC_GRP", "MUSIC_PORT",
        "REELS_GRP", "REELS_PORT", "PI_RAW_GRP", "PI_RAW_PORT", "PI_AUDIO_GRP", "PI_AUDIO_PORT",
        "J2K_GRP", "J2K_PORT",
        # GPU/display/audio env the sinks need (atoll.conf exports these on WSL)
        "GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"]
raw = subprocess.check_output(
    ["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)],
    text=True)
CFG = dict(line.split("=", 1) for line in raw.strip().splitlines() if "=" in line)
# propagate the GPU/display/audio env into our own process so waylandsink/pulsesink find them
for k in ("GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
    if CFG.get(k):
        os.environ[k] = CFG[k]
IFACE = CFG["ISLAND_IFACE"] or "eth0"
SINK = CFG["VIDEO_SINK"] or "waylandsink fullscreen=true"
PANEL = f"http://localhost:{CFG['PANEL_PORT']}"
RUN = CFG["ATOLL_RUN"]
IS_WSL = CFG["ATOLL_PLATFORM"] == "wsl"
DELAY_FILE = os.path.join(RUN, "audio-delay-ms")

def grp(k):  # (group, port) for a source-key prefix
    return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,"
            "sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,"
            "colorimetry=(string)BT601-5,payload=(int)96")
J2K_CAPS = ("application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0")
LBL = "font-desc='Sans Bold 18' shaded-background=true valignment=top halignment=left xpad=14 ypad=10"
TW, TH = 960, 540            # every tile renders at this base size; the compositor scales pads
OUT = "video/x-raw,width=%d,height=%d,format=I420,framerate=30/1" % (TW, TH)

# --- a TS/HEVC source: Live TV, Home videos, Music, Test Reels (all HEVC-in-MPEG-TS) ---
def ts_tile(group, port, label, ch, drain_audio=True):
    a = f" d. ! queue ! mpegaudioparse ! fakesink sync=false" if drain_audio else ""
    return (f"udpsrc address={group} port={port} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 "
            f"! tsdemux name=d d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 "
            f"! videoconvert ! videoscale ! {OUT} ! textoverlay text='{label}' {LBL} ! intervideosink channel={ch} sync=false{a}")

# --- source-key -> a full tile pipeline string ending in intervideosink channel=ch ---
def tile_desc(src, ch):
    if src == "hevc":  g, p = grp("HEVC");  return ts_tile(g, p, "Live TV", ch)
    if src == "jxs":   g, p = grp("HOME");  return ts_tile(g, p, "Home videos", ch)
    if src == "reels": g, p = grp("REELS"); return ts_tile(g, p, "Test Reels", ch)
    if src == "music": g, p = grp("MUSIC"); return ts_tile(g, p, "Music", ch, drain_audio=False)
    if src == "raw":
        g, p = grp("PI_RAW")
        return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true caps=\"{RAW_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! {OUT} "
                f"! textoverlay text='Pi raw 2110-20' {LBL} ! intervideosink channel={ch} sync=false")
    if src == "j2k":
        g, p = grp("J2K")
        return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{J2K_CAPS}\" "
                f"! rtpj2kdepay ! avdec_jpeg2000 ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! {OUT} "
                f"! textoverlay text='JPEG 2000' {LBL} ! intervideosink channel={ch} sync=false")
    if src == "jpegxs":
        return (f"videotestsrc pattern=ball is-live=true ! video/x-raw,width={TW},height={TH},framerate=30/1 "
                f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert ! {OUT} "
                f"! textoverlay text='JPEG XS 2110-22' {LBL} ! intervideosink channel={ch} sync=false")
    # unknown -> treat as Live TV group with the key as label
    g, p = grp("HEVC");  return ts_tile(g, p, src, ch)

# --- audio follower for the active source (the video path adds latency, so we DELAY audio to match) ---
def audio_desc(src):
    tsg = {"hevc": grp("HEVC"), "jxs": grp("HOME"), "music": grp("MUSIC"), "reels": grp("REELS")}
    if src in tsg:
        g, p = tsg[src]
        return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 "
                f"! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false "
                f"a. ! queue max-size-time=1500000000 max-size-buffers=0 max-size-bytes=0 ! mpegaudioparse ! mpg123audiodec "
                f"! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 "
                f"! pulsesink name=asink sync=true buffer-time=200000")
    if src == "raw":
        g, p = grp("PI_AUDIO")
        return (f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
                f"caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96\" "
                f"! rtpjitterbuffer latency=500 ! rtpL24depay ! audioconvert ! audioresample "
                f"! queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 ! pulsesink name=asink sync=true buffer-time=200000")
    return None

# ================= compositor (persistent) =================
BRAND = ("textoverlay text=ATOLL valignment=top halignment=right ypad=18 xpad=28 "
         "font-desc='Sans Bold 20' color=0x80ffffff shaded-background=false")
srcs = "".join(
    f"intervideosrc channel=v{i} timeout=100000000 ! video/x-raw,width={TW},height={TH},framerate=30/1 "
    f"! queue leaky=downstream max-size-buffers=2 ! comp.sink_{i} " for i in range(4))
comp_desc = (
    srcs +
    "compositor name=comp background=black "
    "sink_0::zorder=1 sink_1::zorder=1 sink_2::zorder=1 sink_3::zorder=1 "
    "! video/x-raw,width=1920,height=1080,framerate=30/1 ! videoconvert "
    "! clockoverlay name=clk halignment=center valignment=position ypos=0.03 time-format='%H:%M:%S' "
    "  font-desc='Sans Bold 22' shaded-background=true silent=true "
    "! textoverlay name=gm text='Pi5 PTP GM' halignment=center valignment=position ypos=0.075 "
    "  font-desc='Sans Bold 12' color=0xc8ffffff shaded-background=false silent=true "
    f"! {BRAND} ! {SINK} sync=false")
comp = Gst.parse_launch(comp_desc)
pads = [comp.get_by_name("comp").get_static_pad(f"sink_{i}") for i in range(4)]
clk, gm = comp.get_by_name("clk"), comp.get_by_name("gm")

def set_pad(i, x, y, w, h, alpha):
    p = pads[i]
    p.set_property("xpos", x); p.set_property("ypos", y)
    p.set_property("width", w); p.set_property("height", h); p.set_property("alpha", alpha)

HIDDEN = (0, 0, TW, TH, 0.0)
def layout_plan(layout, active, slots):
    """-> (assign {ch:src|None}, geom {ch:(x,y,w,h,a)}, show_clock)"""
    if layout == "side":
        return ({0: "hevc", 1: "raw", 2: None, 3: None},
                {0: (0, 270, 960, 540, 1.0), 1: (960, 270, 960, 540, 1.0), 2: HIDDEN, 3: HIDDEN}, False)
    if layout == "multi":
        m = (slots.split(",") + ["hevc", "raw", "jxs", "music"])[:4]
        return ({0: m[0], 1: m[1], 2: m[2], 3: m[3]},
                {0: (0, 0, 960, 540, 1.0), 1: (960, 0, 960, 540, 1.0),
                 2: (0, 540, 960, 540, 1.0), 3: (960, 540, 960, 540, 1.0)}, True)
    # single (default)
    return ({0: active, 1: None, 2: None, 3: None},
            {0: (0, 0, 1920, 1080, 1.0), 1: HIDDEN, 2: HIDDEN, 3: HIDDEN}, False)

# ================= per-tile pipeline manager =================
tiles = {}   # ch -> {"src":.., "pipe":GstPipeline}
def stop_tile(ch):
    t = tiles.pop(ch, None)
    if t:
        t["pipe"].set_state(Gst.State.NULL)

def start_tile(ch, src):
    stop_tile(ch)
    pipe = Gst.parse_launch(tile_desc(src, f"v{ch}"))
    bus = pipe.get_bus(); bus.add_signal_watch()
    def on_msg(_b, m, _ch=ch):
        if m.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            if m.type == Gst.MessageType.ERROR:
                e, _ = m.parse_error()
                print(f"{time.strftime('%T')} tile v{_ch} ({src}) ERROR: {e.message} -> restart", flush=True)
            cur = tiles.get(_ch)
            if cur and cur["pipe"] is pipe:      # only if still the live tile for this channel
                GLib.timeout_add(400, lambda: (start_tile(_ch, cur["src"]), False)[1])
    bus.connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)
    tiles[ch] = {"src": src, "pipe": pipe}

def apply_video(layout, active, slots):
    assign, geom, show_clock = layout_plan(layout, active, slots)
    for ch in range(4):
        want = assign[ch]
        have = tiles.get(ch, {}).get("src")
        if want is None:
            stop_tile(ch)
        elif want != have:
            start_tile(ch, want)
        set_pad(ch, *geom[ch])
    clk.set_property("silent", not show_clock)
    gm.set_property("silent", not show_clock)

# ================= audio follower =================
audio = {"src": "__init__", "pipe": None, "delay": None}
def apply_audio(active, layout):
    # single hevc/jxs/music/reels embed nothing here -> we still follow (video is in a separate
    # pipeline now, so there is no embedded audio path); one follower for whatever is active.
    want = active
    if want != audio["src"]:
        if audio["pipe"]:
            audio["pipe"].set_state(Gst.State.NULL); audio["pipe"] = None
        d = audio_desc(want)
        if d:
            p = Gst.parse_launch(d); p.set_state(Gst.State.PLAYING); audio["pipe"] = p
        audio["src"] = want; audio["delay"] = None
    # live lip-sync tuning: milliseconds in $ATOLL_RUN/audio-delay-ms -> pulsesink ts-offset
    if audio["pipe"]:
        try:
            ms = int(open(DELAY_FILE).read().strip())
        except Exception:
            ms = 0
        if ms != audio["delay"]:
            s = audio["pipe"].get_by_name("asink")
            if s: s.set_property("ts-offset", ms * 1_000_000)
            audio["delay"] = ms

# ================= run =================
print(f"multiview-app: following {PANEL}/state -> screen {SCREEN} (Ctrl+C to stop)", flush=True)

def move_window():
    try:
        from shutil import which
        pwsh = which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        mover = subprocess.check_output(["wslpath", "-w", os.path.join(HERE, "move-window-screen.ps1")], text=True).strip()
        subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", mover,
                        "-Screen", SCREEN, "-TimeoutSec", "15"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"(window mover skipped: {e})", flush=True)

cur = {"key": None}
def poll():
    try:
        resp = subprocess.check_output(["curl", "-s", "--max-time", "2", f"{PANEL}/state"], text=True)
    except Exception:
        return True
    def field(name, cls):
        import re
        m = re.search(r'"%s"[: ]*"([%s]*)"' % (name, cls), resp)
        return m.group(1) if m else ""
    active = field("active", "a-z0-9")
    layout = field("layout", "a-z0-9") or "single"
    slots = field("slots", "a-z0-9,") or "hevc,raw,jxs,music"
    if not active:
        return True
    key = f"single:{active}" if layout == "single" else (f"multi:{slots}" if layout == "multi" else layout)
    if key != cur["key"]:
        print(f"{time.strftime('%T')} render -> {key}", flush=True)
        apply_video(layout, active, slots)
        cur["key"] = key
    apply_audio(active, layout)
    return True

# tiles-first: start the tile sinks, THEN bring up the compositor so its intervideosrc latch onto
# already-live channels (reduces the startup connection race), then move the output window.
poll()
comp.set_state(Gst.State.PLAYING)
if IS_WSL and SCREEN != "0":
    threading.Thread(target=move_window, daemon=True).start()
GLib.timeout_add_seconds(1, poll)
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    for ch in list(tiles): stop_tile(ch)
    if audio["pipe"]: audio["pipe"].set_state(Gst.State.NULL)
    comp.set_state(Gst.State.NULL)
