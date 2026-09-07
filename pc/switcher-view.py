#!/usr/bin/env python3
"""Atoll production switcher -- a PROGRAM / PREVIEW vision-mixer view on monitor 2.

Two selected sources are decoded and each tee'd to a FULLSCREEN compositor pad and a small INSET
pad, so the on-air PROGRAM (fullscreen) and the cued PREVIEW (inset) are just which pads are shown.
A TAKE swaps the buses; it is a pure alpha/zorder animation on the one compositor -- CUT is instant,
DISSOLVE crossfades the fullscreen over `rate` seconds -- so it never rebuilds the pipeline and the
picture never drops. Only CHANGING which two sources are loaded rebuilds (like changing a wall tile).

Driven by ~/atoll-run/switcher:  "<pgm_src> <pvw_src> <transition> <rate> <take_seq>"
  e.g.  "hevc music dissolve 1.0 7"
The panel writes it: picking PVW changes a source (rebuild); TAKE bumps take_seq (animate) and swaps
pgm/pvw. PROGRAM audio follows the on-air source via a small subprocess, restarted on take.
"""
import gi, os, sys, subprocess, time
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
import cairo
Gst.init(None)

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "ATOLL_RUN", "AUDIO_GAIN_HEVC", "AUDIO_GAIN_JXS", "AUDIO_GAIN_MUSIC",
        "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT", "MUSIC_GRP", "MUSIC_PORT",
        "MUSIC_AUDIO_GRP", "MUSIC_AUDIO_PORT", "TSRTP_GRP", "TSRTP_PORT", "H264_GRP", "H264_PORT",
        "OPUS_GRP", "OPUS_PORT", "GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
for k in ("GALLIUM_DRIVER", "PULSE_SERVER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
    if CFG.get(k):
        os.environ[k] = CFG[k]
IFACE = CFG["ISLAND_IFACE"] or "eth0"
RUN = CFG.get("ATOLL_RUN") or os.path.expanduser("~/atoll-run")
KNOB = os.path.join(RUN, "switcher")
SCREEN = sys.argv[1] if len(sys.argv) > 1 else "2"

W, H = 1920, 1080
WINW = int(os.environ.get("ATOLL_TV_W", "3840"))
WINH = int(os.environ.get("ATOLL_TV_H", "2160"))
SINK = ("fakesink sync=true" if os.environ.get("ATOLL_SINK_TEST")
        else f"glupload ! glcolorscale ! video/x-raw(memory:GLMemory),width={WINW},height={WINH} ! glimagesink sync=true")
# PREVIEW inset: bottom-right quarter-ish, with a margin
IW, IH = 600, 338
IX, IY = W - IW - 48, H - IH - 48
LABEL = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "tsrtp": "TS over RTP", "h264": "H.264 RTP"}

def _g(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

# Video decode fragment for a source key, ending at a named tee (video/x-raw, W x H, 30fps).
def vsrc(key, tee):
    if key == "hevc":
        g, p = _g("HEVC"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d_{tee} d_{tee}. ! h265parse ! queue ! nvh265dec ! cudadownload"
    elif key == "jxs":
        g, p = _g("HOME"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d_{tee} d_{tee}. ! h265parse ! queue ! nvh265dec ! cudadownload"
    elif key == "music":
        g, p = _g("MUSIC"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d_{tee} d_{tee}. ! h265parse ! queue ! nvh265dec ! cudadownload"
    elif key == "tsrtp":
        g, p = _g("TSRTP"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33\" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d_{tee} d_{tee}. ! h264parse ! queue ! nvh264dec ! cudadownload"
    elif key == "h264":
        g, p = _g("H264"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96\" ! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! queue ! nvh264dec ! cudadownload"
    else:
        # unknown -> a flat colour so the compositor pad always has data
        dec = "videotestsrc pattern=black is-live=true"
    return (f"{dec} ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale "
            f"! video/x-raw,width={W},height={H} ! queue leaky=downstream max-size-time=700000000 "
            f"max-size-buffers=0 max-size-bytes=0 ! tee name={tee}")

# PROGRAM-follow audio pipeline for a source (subprocess; restarted on take).
def audio_cmd(key):
    gain_h = CFG.get("AUDIO_GAIN_HEVC") or "1.0"; gain_j = CFG.get("AUDIO_GAIN_JXS") or "1.0"; gain_m = CFG.get("AUDIO_GAIN_MUSIC") or "1.0"
    if key in ("hevc", "jxs", "tsrtp"):
        g, p = _g("HEVC" if key == "hevc" else ("HOME" if key == "jxs" else "TSRTP"))
        if key == "tsrtp":
            head = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=a'
        else:
            head = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=a'
        gain = gain_h if key == "hevc" else (gain_j if key == "jxs" else "1.0")
        vdrain = "a. ! queue ! h265parse ! fakesink sync=false" if key != "tsrtp" else "a. ! queue ! h264parse ! fakesink sync=false"
        return (f'gst-launch-1.0 -q {head} {vdrain} a. ! audio/mpeg ! queue max-size-time=1500000000 ! decodebin ! audioconvert '
                f'! audio/x-raw,channels=2 ! audioresample ! queue max-size-time=2000000000 ! volume volume={gain} ! autoaudiosink sync=true')
    if key == "music":
        g, p = _g("MUSIC_AUDIO")
        return (f'gst-launch-1.0 -q udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 '
                f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96" ! rtpjitterbuffer latency=500 '
                f'! rtpL24depay ! audioconvert ! audioresample ! queue max-size-time=2000000000 ! volume volume={gain_m} ! autoaudiosink sync=false')
    if key == "h264":
        g, p = _g("OPUS")
        return (f'gst-launch-1.0 -q udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true '
                f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97" ! rtpjitterbuffer latency=200 '
                f'! rtpopusdepay ! opusdec ! audioconvert ! audioresample ! queue max-size-time=2000000000 ! autoaudiosink sync=true')
    return None

# ---- switcher state -----------------------------------------------------------------------------
def read_knob():
    try:
        parts = open(KNOB).read().split()
    except OSError:
        parts = []
    parts += ["hevc", "music", "cut", "1.0", "0"][len(parts):]
    return parts[0], parts[1], parts[2], float(parts[3] or 1.0), int(parts[4] or 0)

class Switcher:
    def __init__(self):
        self.pipe = None
        self.mix = None
        self.pads = {}          # name -> compositor pad
        self.audio_proc = None
        self.src_set = None      # (a, b) currently loaded
        self.pgm = "A"          # which fullscreen is on air
        self.take_seq = -1
        self._anim = None
        self.build()
        GLib.timeout_add(300, self.poll)

    def build(self):
        a, b, trans, rate, seq = read_knob()
        self.src_set = (a, b); self.take_seq = seq; self.pgm = "A" if seq % 2 == 0 else "B"
        desc = (
            f"compositor name=mix background=black "
            f"! video/x-raw,width={W},height={H} ! videoconvert ! cairooverlay name=ov ! videoconvert ! {SINK} "
            f"{vsrc(a,'ta')} ta. ! queue ! mix. ta. ! queue ! mix. "
            f"{vsrc(b,'tb')} tb. ! queue ! mix. tb. ! queue ! mix. "
        )
        self.pipe = Gst.parse_launch(desc)
        self.mix = self.pipe.get_by_name("mix")
        # parse-launch created 4 request sink pads (link order ta,ta,tb,tb -> sink_0..sink_3).
        # Fetch them by iterating and map by name.
        byname = {}
        it = self.mix.iterate_sink_pads()
        while True:
            res, pad = it.next()
            if res == Gst.IteratorResult.OK:
                byname[pad.get_name()] = pad
            elif res == Gst.IteratorResult.RESYNC:
                it.resync()
            else:
                break
        self.pads = {"Afull": byname["sink_0"], "Ains": byname["sink_1"],
                     "Bfull": byname["sink_2"], "Bins": byname["sink_3"]}
        for nm, (x, y, w, h, z) in {
            "Afull": (0, 0, W, H, 5), "Bfull": (0, 0, W, H, 6),
            "Ains": (IX, IY, IW, IH, 20), "Bins": (IX, IY, IW, IH, 21)}.items():
            p = self.pads[nm]
            p.set_property("xpos", x); p.set_property("ypos", y)
            p.set_property("width", w); p.set_property("height", h); p.set_property("zorder", z)
        self.apply_bus(instant=True)
        ov = self.pipe.get_by_name("ov")
        ov.connect("draw", self.on_draw)
        self.pipe.get_bus().add_signal_watch(); self.pipe.get_bus().connect("message", self.on_msg)
        self.pipe.set_state(Gst.State.PLAYING)
        self.start_audio()
        print(f"switcher: PGM={a} PVW={b}", flush=True)

    def _pgm_key(self):
        a, b = self.src_set
        return a if self.pgm == "A" else b
    def _pvw_key(self):
        a, b = self.src_set
        return b if self.pgm == "A" else a

    def apply_bus(self, instant=True):
        # fullscreen: pgm alpha 1 on top, other alpha 0; inset: pvw visible, pgm hidden
        onair, off = ("Afull", "Bfull") if self.pgm == "A" else ("Bfull", "Afull")
        pvwins, pgmins = ("Bins", "Ains") if self.pgm == "A" else ("Ains", "Bins")
        self.pads[onair].set_property("zorder", 6); self.pads[onair].set_property("alpha", 1.0)
        self.pads[off].set_property("zorder", 5); self.pads[off].set_property("alpha", 0.0)
        self.pads[pvwins].set_property("alpha", 1.0)
        self.pads[pgmins].set_property("alpha", 0.0)

    def take(self, newpgm, trans, rate):
        # move to bus `newpgm` ("A"/"B"); CUT = instant, DISSOLVE = fade the incoming fullscreen up
        inc = "Bfull" if newpgm == "B" else "Afull"
        if trans == "dissolve" and rate > 0.05:
            self.pads[inc].set_property("zorder", 7)      # incoming rides on top during the mix
            self.pads[inc].set_property("alpha", 0.0)
            steps = max(2, int(rate / 0.033))
            state = {"i": 0}
            def step():
                state["i"] += 1
                fr = state["i"] / steps
                self.pads[inc].set_property("alpha", min(1.0, fr))
                if state["i"] >= steps:
                    self.pgm = newpgm; self.apply_bus(instant=True); self._anim = None
                    return False
                return True
            self._anim = GLib.timeout_add(33, step)
        else:
            self.pgm = newpgm; self.apply_bus(instant=True)
        self.start_audio()

    def start_audio(self):
        cmd = audio_cmd(self._pgm_key())
        if self.audio_proc:
            self.audio_proc.terminate()
            try: self.audio_proc.wait(timeout=2)
            except Exception: self.audio_proc.kill()
            self.audio_proc = None
        if cmd:
            self.audio_proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rebuild(self):
        if self._anim: GLib.source_remove(self._anim); self._anim = None
        if self.pipe: self.pipe.set_state(Gst.State.NULL)
        if self.audio_proc:
            self.audio_proc.terminate()
            try: self.audio_proc.wait(timeout=2)
            except Exception: self.audio_proc.kill()
            self.audio_proc = None
        self.build()

    def poll(self):
        a, b, trans, rate, seq = read_knob()
        if (a, b) != self.src_set:
            print(f"switcher: source set {self.src_set} -> {(a,b)}; rebuilding", flush=True)
            self.rebuild(); return True
        if seq != self.take_seq:
            self.take_seq = seq
            target = "A" if seq % 2 == 0 else "B"
            if target != self.pgm:
                print(f"switcher: TAKE ({trans} {rate}s) PGM {self._pgm_key()} -> {self._pvw_key()}", flush=True)
                self.take(target, trans, rate)
        return True

    # cairo overlay: PROGRAM (red) border + labels, PREVIEW (green) inset border + label
    def on_draw(self, _ov, ctx, _ts, _dur):
        S = WINW / 1920.0
        pgm = LABEL.get(self._pgm_key(), self._pgm_key())
        pvw = LABEL.get(self._pvw_key(), self._pvw_key())
        # PROGRAM border (full frame)
        ctx.set_source_rgb(0.84, 0.13, 0.16); ctx.set_line_width(6 * S)
        ctx.rectangle(3 * S, 3 * S, WINW - 6 * S, WINH - 6 * S); ctx.stroke()
        def tag(x, y, text, rgb, big=True):
            ctx.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            fs = (34 if big else 26) * S; ctx.set_font_size(fs)
            ext = ctx.text_extents(text)
            ctx.set_source_rgba(0, 0, 0, 0.6); ctx.rectangle(x, y, ext.width + 20 * S, fs + 14 * S); ctx.fill()
            ctx.set_source_rgb(*rgb); ctx.move_to(x + 10 * S, y + fs + 2 * S); ctx.show_text(text)
        tag(20 * S, 20 * S, "PROGRAM  ·  " + pgm, (1, 0.3, 0.32))
        # PREVIEW inset border + label
        ix, iy, iw, ih = IX * S, IY * S, IW * S, IH * S
        ctx.set_source_rgb(0.15, 0.8, 0.4); ctx.set_line_width(5 * S)
        ctx.rectangle(ix, iy, iw, ih); ctx.stroke()
        tag(ix + 6 * S, iy - 40 * S if iy > 44 * S else iy + 6 * S, "PREVIEW  ·  " + pvw, (0.4, 1, 0.6), big=False)

    def on_msg(self, _b, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error(); print(f"switcher ERROR: {err} :: {dbg}", flush=True)

if __name__ == "__main__":
    os.makedirs(RUN, exist_ok=True)
    if not os.path.exists(KNOB):
        open(KNOB, "w").write("hevc music cut 1.0 0\n")
    sw = Switcher()
    # move the GL window to monitor 2 (WSL), same helper the other renderers use
    def mover():
        ps = os.path.join(HERE, "snap-window-screen.ps1")
        pw = subprocess.run(["bash", "-c", "command -v powershell.exe || echo /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"], capture_output=True, text=True).stdout.strip()
        try:
            win = subprocess.check_output(["wslpath", "-w", ps], text=True).strip()
            subprocess.Popen([pw, "-ExecutionPolicy", "Bypass", "-File", win, SCREEN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("snap failed:", e, flush=True)
        return False
    if CFG.get("WAYLAND_DISPLAY"):
        GLib.timeout_add_seconds(2, mover)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        sw.pipe.set_state(Gst.State.NULL)
        if sw.audio_proc: sw.audio_proc.terminate()
