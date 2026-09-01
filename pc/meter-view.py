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
RUN = CFG.get("ATOLL_RUN", "")
def grp(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,"
            "sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,"
            "colorimetry=(string)BT601-5,payload=(int)96")
J2K_CAPS = "application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0"
BRAND = ("textoverlay text=ATOLL valignment=top halignment=right ypad=18 xpad=28 "
         "font-desc='Sans Bold 20' color=0x80ffffff shaded-background=false")
# video ends at a named cairooverlay 'ov'; 'usrc' is tapped for bitrate, 'vpre' for source caps.
VTAIL = f"videoconvert ! cairooverlay name=ov ! {BRAND} ! videoconvert ! {SINK} sync=true"
# measure ALL channels at 'level' (so the meters show 5.1), THEN downmix to stereo for playback --
# WSLg's Pulse output is stereo and autoaudiosink won't take a 6-channel stream.
ALEVEL = "audioconvert ! level name=lvl post-messages=true interval=50000000"
# sync=true so the audio sink presents each sample at its stream PTS -- the SAME clock the sync=true
# video uses -- giving true lip-sync from the source timestamps (no hand-tuned delay). GStreamer's
# latency negotiation offsets the two sinks to match. A ~300ms jitter buffer on 'aq' keeps WSLg Pulse
# fed (that underrun was the old sync=true stutter); it does NOT shift sync (PTS presentation is
# unchanged by buffering). autoaudiosink so a dead Pulse falls back silently instead of killing the
# video. Downmix to stereo; level upstream keeps 6-ch meters. Any residual skew is a live TRIM (both
# directions) via a pad offset from ADELAY_FILE below.
APLAY = ("audioconvert ! audio/x-raw,channels=2 ! audioresample "
         "! queue name=aq max-size-buffers=0 max-size-bytes=0 max-size-time=1000000000 min-threshold-time=300000000 "
         "! autoaudiosink sync=true")

def ts_pipeline(g, p):   # HEVC video + MP3 audio in a TS (Live TV / Home / Music / Reels)
    return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d "
            f"d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert name=vpre ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL} "
            f"d. ! audio/mpeg ! queue ! decodebin ! {ALEVEL} ! {APLAY}")   # audio/mpeg pins the audio pad; decodebin = AAC (Live TV) or MP3

def build():
    if SRC in ("hevc", "jxs", "music", "reels"):
        g, p = grp({"hevc": "HEVC", "jxs": "HOME", "music": "MUSIC", "reels": "REELS"}[SRC])
        return ts_pipeline(g, p)
    if SRC == "raw":   # Pi RTP video + the separate ST 2110-30 L24 audio flow
        g, p = grp("PI_RAW"); ag, ap = grp("PI_AUDIO")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true caps=\"{RAW_CAPS}\" "
                f"! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert name=vpre ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL} "
                f"udpsrc address={ag} port={ap} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 "
                f"caps=\"application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96\" "
                f"! rtpjitterbuffer latency=500 ! rtpL24depay ! {ALEVEL} ! {APLAY}")
    if SRC == "j2k":   # video only (no audio) -> meters idle
        g, p = grp("J2K")
        return (f"udpsrc name=usrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 caps=\"{J2K_CAPS}\" "
                f"! rtpj2kdepay ! avdec_jpeg2000 ! videoconvert name=vpre ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL}")
    # jpegxs / unknown -> local test pattern, video only
    return (f"videotestsrc pattern=ball is-live=true ! video/x-raw,width=1920,height=1080,framerate=30/1 "
            f"! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert name=vpre ! videoscale ! video/x-raw,width=1920,height=1080 ! {VTAIL}")

SRCNAME = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "reels": "Test Reels",
           "raw": "Pi raw 2110-20", "j2k": "JPEG 2000 island", "jpegxs": "JPEG XS codec"}
VCODEC = {"hevc": "HEVC / H.265", "jxs": "HEVC / H.265", "music": "HEVC / H.265", "reels": "HEVC / H.265",
          "raw": "Uncompressed RFC 4175", "j2k": "JPEG 2000", "jpegxs": "JPEG XS"}
ACODEC = {"hevc": "AAC 5.1", "jxs": "MPEG audio (MP3)", "music": "MPEG audio (MP3)",
          "reels": "MPEG audio (MP3)", "raw": "L24 PCM (2110-30)"}
TRANSPORT = {"hevc": "MPEG-TS / UDP", "jxs": "MPEG-TS / UDP", "music": "MPEG-TS / UDP",
             "reels": "MPEG-TS / UDP", "raw": "ST 2110-20 RTP", "j2k": "J2K/RTP", "jpegxs": "local"}

pipe = Gst.parse_launch(build())
ov = pipe.get_by_name("ov")
st = {"peak": [], "decay": [], "w": 1920, "h": 1080,
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

LABELS = ["L", "R", "C", "LFE", "Ls", "Rs", "7", "8"]
def on_draw(_ov, ctx, _ts, _dur):
    h = st["h"]
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
