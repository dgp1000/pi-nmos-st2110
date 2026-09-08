#!/usr/bin/env python3
"""Atoll production switcher -- a PROGRAM / PREVIEW vision-mixer view on monitor 2.

DECOUPLED design (a real switcher must never interrupt PROGRAM to cue a preview):
 * a PERSISTENT display pipeline owns the compositor + overlay + the glimagesink window and never
   restarts. It pulls the two buses over intervideosrc (channels busA / busB), tees each to a
   FULLSCREEN and an INSET compositor pad -- so PROGRAM (fullscreen) and PREVIEW (inset) are just
   which pads are shown, and a TAKE is a pure alpha/zorder animation (CUT instant, DISSOLVE fades
   the incoming fullscreen up over `rate` s).
 * two INDEPENDENT source pipelines (decode -> intervideosink channel=busA/busB) feed the buses.
   Changing a source restarts ONLY that source pipeline; the display (and the on-air PROGRAM) keep
   running, and intervideosrc shows black for that bus only until the new source arrives.

Driven by ~/atoll-run/switcher:  "<srcA> <srcB> <transition> <rate> <take_seq>". take_seq PARITY
picks PGM (even=A, odd=B) so a TAKE just bumps the seq (animate); the source identities stay put.
PROGRAM audio is mixed IN-PROCESS: each bus decodes audio -> interaudiosink (abusA/abusB); a
persistent audio pipeline runs both through `volume` elements into an audiomixer, and a TAKE
animates those volumes -- so a DISSOLVE crossfades the sound with the picture and a CUT switches
it instantly. No audio subprocess.
"""
import gi, os, sys, subprocess, time, signal
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
IW, IH = 600, 338                       # PREVIEW inset size
IX, IY = W - IW - 48, H - IH - 48       # bottom-right, with a margin
RAWCAPS = f"video/x-raw,format=I420,width={W},height={H},framerate=30/1"
LABEL = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "tsrtp": "TS over RTP", "h264": "H.264 RTP"}

def _g(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

# Decode a source key to raw I420 W x H 30 fps (a source pipeline appends `! intervideosink channel=..`).
def decode(key):
    if key == "hevc":
        g, p = _g("HEVC"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d d. ! h265parse ! queue ! nvh265dec ! cudadownload"
    elif key == "jxs":
        g, p = _g("HOME"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d d. ! h265parse ! queue ! nvh265dec ! cudadownload"
    elif key == "music":
        g, p = _g("MUSIC"); dec = f"udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d d. ! h265parse ! queue ! nvh265dec ! cudadownload"
    elif key == "tsrtp":
        g, p = _g("TSRTP"); dec = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d d. ! h264parse ! queue ! nvh264dec ! cudadownload'
    elif key == "h264":
        g, p = _g("H264"); dec = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" ! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! queue ! nvh264dec ! cudadownload'
    else:
        dec = "videotestsrc pattern=black is-live=true"
    return (f"{dec} ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! {RAWCAPS} "
            f"! queue leaky=downstream max-size-time=700000000 max-size-buffers=0 max-size-bytes=0")

# PROGRAM-follow audio (subprocess; switched only when the PGM source changes).
def audio_decode(key, chan):
    """Decode a source's audio to F32LE 48k stereo (normalised by AUDIO_GAIN_*) and hand it to the
    audio bus `chan` (interaudiosink). The persistent audio mixer picks it up on interaudiosrc and
    crossfades it via the per-bus volume. Sources without audio feed silence so the mixer always has
    both inputs. Appended to that bus's source pipeline, so it rebuilds with the source."""
    gain_h = CFG.get("AUDIO_GAIN_HEVC") or "1.0"; gain_j = CFG.get("AUDIO_GAIN_JXS") or "1.0"; gain_m = CFG.get("AUDIO_GAIN_MUSIC") or "1.0"
    tail = f"audioconvert ! audioresample ! audio/x-raw,format=F32LE,rate=48000,channels=2 ! interaudiosink channel={chan} sync=false"
    if key in ("hevc", "jxs", "tsrtp"):
        g, p = _g("HEVC" if key == "hevc" else ("HOME" if key == "jxs" else "TSRTP"))
        if key == "tsrtp":
            head = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=ad'
            vdrain = "ad. ! queue ! h264parse ! fakesink sync=false"
        else:
            head = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=ad'
            vdrain = "ad. ! queue ! h265parse ! fakesink sync=false"
        gain = gain_h if key == "hevc" else (gain_j if key == "jxs" else "1.0")
        return (f'{head} {vdrain} ad. ! audio/mpeg ! queue max-size-time=1500000000 ! decodebin ! audioconvert '
                f'! audio/x-raw,channels=2 ! audioresample ! volume volume={gain} ! {tail}')
    if key == "music":
        g, p = _g("MUSIC_AUDIO")
        return (f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 '
                f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96" ! rtpjitterbuffer latency=500 '
                f'! rtpL24depay ! audioconvert ! audioresample ! volume volume={gain_m} ! {tail}')
    if key == "h264":
        g, p = _g("OPUS")
        return (f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true '
                f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97" ! rtpjitterbuffer latency=200 '
                f'! rtpopusdepay ! opusdec ! audioconvert ! audioresample ! {tail}')
    return f"audiotestsrc wave=silence is-live=true ! {tail}"
def read_knob():
    try:
        parts = open(KNOB).read().split()
    except OSError:
        parts = []
    parts += ["hevc", "music", "cut", "1.0", "0"][len(parts):]
    return parts[0], parts[1], parts[2], float(parts[3] or 1.0), int(parts[4] or 0)

class Switcher:
    def __init__(self):
        self.disp = None; self.mix = None; self.pads = {}
        self.srcpipe = {"A": None, "B": None}
        self.srckey = {"A": None, "B": None}
        self.aud = None; self.volA = None; self.volB = None
        self.pgm = "A"; self.take_seq = -1; self._anim = None; self._frames = 0
        a, b, trans, rate, seq = read_knob()
        self.take_seq = seq; self.pgm = "A" if seq % 2 == 0 else "B"
        self.set_source("A", a); self.set_source("B", b)   # source pipelines up first
        self.build_display()                                # persistent display consumes busA/busB
        self.build_audio()                                  # persistent audio mixer consumes abusA/abusB
        self.apply_bus()                                    # set initial PGM/PVW volumes
        GLib.timeout_add(300, self.poll)
        GLib.timeout_add_seconds(5, self._diag)
        if CFG.get("WAYLAND_DISPLAY"):
            GLib.timeout_add_seconds(2, self._snap)
        print(f"switcher: PGM(bus {self.pgm})={self._pgm_key()} PVW={self._pvw_key()}", flush=True)

    # --- independent source pipelines (one per bus) -----------------------------------------------
    def set_source(self, bus, key):
        ch = "busA" if bus == "A" else "busB"
        old = self.srcpipe.get(bus)
        if old:
            old.set_state(Gst.State.NULL)
        ach = "abusA" if bus == "A" else "abusB"
        pipe = Gst.parse_launch(decode(key) + f" ! intervideosink channel={ch} sync=false " + audio_decode(key, ach))
        b = pipe.get_bus(); b.add_signal_watch()
        b.connect("message", lambda _b, m, bs=bus: self._src_msg(m, bs))
        pipe.set_state(Gst.State.PLAYING)
        self.srcpipe[bus] = pipe; self.srckey[bus] = key
        print(f"{time.strftime('%T')} switcher: bus {bus} <- {key} (channel {ch})", flush=True)

    def _src_msg(self, msg, bus):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"switcher SRC {bus} ERROR: {err} :: {dbg}", flush=True)

    # --- persistent display pipeline (never restarts) ---------------------------------------------
    def build_display(self):
        desc = (
            f"compositor name=mix background=black ! video/x-raw,width={W},height={H} "
            f"! videoconvert ! cairooverlay name=ov ! videoconvert ! {SINK} "
            f"intervideosrc channel=busA ! {RAWCAPS} ! tee name=ta ta. ! queue ! mix. ta. ! queue ! mix. "
            f"intervideosrc channel=busB ! {RAWCAPS} ! tee name=tb tb. ! queue ! mix. tb. ! queue ! mix. "
        )
        self.disp = Gst.parse_launch(desc)
        self.mix = self.disp.get_by_name("mix")
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
        self.apply_bus()
        self.disp.get_by_name("ov").connect("draw", self.on_draw)
        self.disp.get_bus().add_signal_watch(); self.disp.get_bus().connect("message", self.on_msg)
        self.disp.set_state(Gst.State.PLAYING)
        ovpad = self.disp.get_by_name("ov").get_static_pad("src")
        if ovpad:
            def _count(_pad, _info):
                self._frames += 1
                return Gst.PadProbeReturn.OK
            ovpad.add_probe(Gst.PadProbeType.BUFFER, _count)

    # --- persistent PROGRAM audio: mix both buses through per-bus volumes (the TAKE animates them) --
    def build_audio(self):
        asink = "fakesink sync=false" if os.environ.get("ATOLL_SINK_TEST") else "autoaudiosink sync=false"
        desc = ("interaudiosrc channel=abusA ! audio/x-raw,rate=48000,channels=2 ! volume name=volA ! "
                "audiomixer name=amix ! audioconvert ! audioresample ! " + asink + " "
                "interaudiosrc channel=abusB ! audio/x-raw,rate=48000,channels=2 ! volume name=volB ! amix. ")
        self.aud = Gst.parse_launch(desc)
        self.volA = self.aud.get_by_name("volA"); self.volB = self.aud.get_by_name("volB")
        b = self.aud.get_bus(); b.add_signal_watch(); b.connect("message", self.on_msg)
        self.aud.set_state(Gst.State.PLAYING)

    def _snap(self):
        try:
            ps = os.path.join(HERE, "snap-window-screen.ps1")
            pw = subprocess.run(["bash", "-c", "command -v powershell.exe || echo /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"], capture_output=True, text=True).stdout.strip()
            win = subprocess.check_output(["wslpath", "-w", ps], text=True).strip()
            subprocess.Popen([pw, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win, "-Screen", SCREEN, "-TimeoutSec", "10"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("snap failed:", e, flush=True)
        return False

    def _diag(self):
        al = {k: round(self.pads[k].get_property("alpha"), 2) for k in ("Afull", "Bfull", "Ains", "Bins")}
        vol = (round(self.volA.get_property("volume"), 2) if self.volA else None,
               round(self.volB.get_property("volume"), 2) if self.volB else None)
        print(f"switcher DIAG: pgm={self._pgm_key()} pvw={self._pvw_key()} frames/5s={self._frames} alphas={al} vol(A,B)={vol}", flush=True)
        self._frames = 0
        return True

    def _pgm_key(self): return self.srckey["A"] if self.pgm == "A" else self.srckey["B"]
    def _pvw_key(self): return self.srckey["B"] if self.pgm == "A" else self.srckey["A"]

    def apply_bus(self):
        onair, off = ("Afull", "Bfull") if self.pgm == "A" else ("Bfull", "Afull")
        pvwins, pgmins = ("Bins", "Ains") if self.pgm == "A" else ("Ains", "Bins")
        self.pads[onair].set_property("zorder", 6); self.pads[onair].set_property("alpha", 1.0)
        self.pads[off].set_property("zorder", 5); self.pads[off].set_property("alpha", 0.0)
        self.pads[pvwins].set_property("alpha", 1.0)
        self.pads[pgmins].set_property("alpha", 0.0)
        if self.volA and self.volB:                       # PGM audible, PVW muted (at rest)
            self.volA.set_property("volume", 1.0 if self.pgm == "A" else 0.0)
            self.volB.set_property("volume", 1.0 if self.pgm == "B" else 0.0)

    def take(self, newpgm, trans, rate):
        if self._anim:
            GLib.source_remove(self._anim); self._anim = None
        inc = "Bfull" if newpgm == "B" else "Afull"
        vol_in = self.volB if newpgm == "B" else self.volA     # incoming PGM audio fades up
        vol_out = self.volA if newpgm == "B" else self.volB    # outgoing PGM audio fades down
        if trans == "dissolve" and rate > 0.05:
            self.pads[inc].set_property("zorder", 7)      # incoming rides on top during the mix
            self.pads[inc].set_property("alpha", 0.0)
            steps = max(2, int(rate / 0.033)); state = {"i": 0}
            def step():
                state["i"] += 1
                frac = min(1.0, state["i"] / steps)
                self.pads[inc].set_property("alpha", frac)      # video crossfade
                if vol_in:  vol_in.set_property("volume", frac)         # audio crossfade, in step
                if vol_out: vol_out.set_property("volume", 1.0 - frac)
                if state["i"] >= steps:
                    self.pgm = newpgm; self.apply_bus(); self._anim = None
                    return False
                return True
            self._anim = GLib.timeout_add(33, step)
        else:
            self.pgm = newpgm; self.apply_bus()             # CUT: apply_bus swaps volumes instantly

    # --- PROGRAM audio (switched only when the PGM source changes) --------------------------------
    def poll(self):
        a, b, trans, rate, seq = read_knob()
        if a != self.srckey["A"]:
            self.set_source("A", a)      # restart ONLY source A; display + PROGRAM keep running
        if b != self.srckey["B"]:
            self.set_source("B", b)      # restart ONLY source B
        if seq != self.take_seq:
            self.take_seq = seq
            target = "A" if seq % 2 == 0 else "B"
            if target != self.pgm:
                print(f"switcher: TAKE ({trans} {rate}s) PGM {self._pgm_key()} -> {self._pvw_key()}", flush=True)
                self.take(target, trans, rate)
        return True

    def on_draw(self, _ov, ctx, _ts, _dur):
        # cairooverlay sits on the 1920x1080 compositor output (before the GL upscale), so W x H space.
        pgm = LABEL.get(self._pgm_key(), self._pgm_key() or "—")
        pvw = LABEL.get(self._pvw_key(), self._pvw_key() or "—")
        ctx.set_source_rgb(0.84, 0.13, 0.16); ctx.set_line_width(6)
        ctx.rectangle(3, 3, W - 6, H - 6); ctx.stroke()
        def tag(x, y, text, rgb, big=True):
            ctx.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            fs = 40 if big else 30; ctx.set_font_size(fs)
            ext = ctx.text_extents(text)
            ctx.set_source_rgba(0, 0, 0, 0.6); ctx.rectangle(x, y, ext.width + 24, fs + 16); ctx.fill()
            ctx.set_source_rgb(*rgb); ctx.move_to(x + 12, y + fs + 2); ctx.show_text(text)
        tag(24, 24, "PROGRAM  ·  " + pgm, (1, 0.3, 0.32))
        ctx.set_source_rgb(0.15, 0.8, 0.4); ctx.set_line_width(5)
        ctx.rectangle(IX, IY, IW, IH); ctx.stroke()
        tag(IX, IY - 46 if IY > 50 else IY + 6, "PREVIEW  ·  " + pvw, (0.4, 1, 0.6), big=False)

    def on_msg(self, _b, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error(); print(f"switcher DISPLAY ERROR: {err} :: {dbg}", flush=True)

    def stop(self):
        if self._anim:
            GLib.source_remove(self._anim); self._anim = None
        for pipe in (self.aud, self.disp, self.srcpipe.get("A"), self.srcpipe.get("B")):
            try:
                if pipe: pipe.set_state(Gst.State.NULL)
            except Exception:
                pass

if __name__ == "__main__":
    os.makedirs(RUN, exist_ok=True)
    if not os.path.exists(KNOB):
        open(KNOB, "w").write("hevc music cut 1.0 0\n")
    sw = Switcher()
    loop = GLib.MainLoop()
    def _shutdown(*_):
        sw.stop(); loop.quit(); return False
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _shutdown)
    try:
        loop.run()
    finally:
        sw.stop()
