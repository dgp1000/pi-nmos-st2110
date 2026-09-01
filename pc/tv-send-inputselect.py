#!/usr/bin/env python3
# ===========================================================================
#  Atoll live-TV bridge, SEAMLESS v2 (input-selector, A/V kept together).
#
#  The inter-bridge design (tv-send-seamless.py) split audio and video into separate
#  intervideosrc/interaudiosrc streams -> different latency -> ~0.7s A/V skew, and its timeout
#  emitted green. Here audio+video stay TOGETHER: souphttpsrc -> decodebin3 -> input-selector(v/a)
#  -> ONE encoder each -> mux -> udpsink. PTS relationship is preserved end to end (synced like the
#  original direct encoder), and a black/silence FALLBACK feeds the selectors during a retune so the
#  encoder never stops (5010 stays continuous -> multiview never sees a discontinuity) and the gap
#  shows proper black, not green.
#
#  Usage: tv-send-inputselect.py [GROUP] [PORT] [STATE_FILE]   (defaults from atoll.conf -> 5010)
# ===========================================================================
import gi, os, sys, subprocess, time
gi.require_version("Gst", "1.0")
# Decode the HDHomeRun input on the CPU, never on the GPU. We only DECODE the source here (a cheap
# SD/HD MPEG-2 or H.264 ATSC stream) and re-ENCODE on the GPU (nvh265enc). decodebin3 would otherwise
# auto-plug an NVDEC decoder (nvh264dec for H.264 channels, etc.), and each channel change rebuilds
# that decoder -- leaking NVDEC sessions that starve the multiview RECEIVER's nvh265dec tiles until
# they can only show keyframes (~1 fps "stepping"). Demoting the nvcodec DECODERS to rank NONE (this
# process only, via env before the registry loads) forces avdec_* on the CPU and leaves all of NVDEC
# for the tiles. nvh265enc is an ENCODER and is unaffected. avdec handles ATSC SD/HD at 30fps easily.
os.environ.setdefault("GST_PLUGIN_FEATURE_RANK",
    "nvh264dec:NONE,nvh265dec:NONE,nvmpeg2videodec:NONE,nvmpeg4videodec:NONE,"
    "nvmpegvideodec:NONE,nvvp8dec:NONE,nvvp9dec:NONE,nvjpegdec:NONE")
from gi.repository import Gst, GLib
Gst.init(None)

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "HDHR_HOST", "HEVC_GRP", "HEVC_PORT", "ATOLL_RUN", "MCAST_TTL", "TV_CHANNEL"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
IFACE = CFG["ISLAND_IFACE"] or "eth0"
HDHR = CFG["HDHR_HOST"]
GRP = sys.argv[1] if len(sys.argv) > 1 else CFG["HEVC_GRP"]
PORT = sys.argv[2] if len(sys.argv) > 2 else CFG["HEVC_PORT"]
STATE = sys.argv[3] if len(sys.argv) > 3 else os.path.join(CFG["ATOLL_RUN"], "tv-channel")
TTL = CFG["MCAST_TTL"] or "1"
DEFCH = CFG["TV_CHANNEL"] or "8.1"
VCAPS = Gst.Caps.from_string("video/x-raw,format=NV12,width=1280,height=720,framerate=30/1")
# Always 6-channel (5.1) so the channel count NEVER changes mid-stream (a changing count is a
# discontinuity that breaks the seamless switch). audioconvert upmixes stereo sources into this
# layout; a real 5.1 broadcast passes straight through. Mask 0x3f = FL|FR|FC|LFE|RL|RR (L R C LFE Ls Rs).
ACAPS = Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=6,channel-mask=(bitmask)0x3f,layout=interleaved")

# ---- persistent tail + fallback (one pipeline). vsel/asel sink_0 = the black/silence fallback. ----
pipeline = Gst.parse_launch(
    "input-selector name=vsel sync-streams=false ! queue ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 "
    "preset=p4 tune=low-latency gop-size=30 aud=true ! h265parse config-interval=-1 ! queue ! mux. "
    "input-selector name=asel sync-streams=false ! queue ! audioconvert ! audioresample "
    "! audio/x-raw,channels=6,channel-mask=(bitmask)0x3f ! avenc_aac bitrate=384000 ! aacparse ! queue ! mux. "
    f"mpegtsmux name=mux ! queue ! udpsink host={GRP} port={PORT} multicast-iface={IFACE} auto-multicast=true ttl={TTL} "
    "videotestsrc pattern=black is-live=true ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 ! vsel. "
    "audiotestsrc wave=silence is-live=true ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=6,channel-mask=(bitmask)0x3f ! asel.")
vsel = pipeline.get_by_name("vsel"); asel = pipeline.get_by_name("asel")
vfb = vsel.get_static_pad("sink_0"); afb = asel.get_static_pad("sink_0")

def mk(factory, **props):
    e = Gst.ElementFactory.make(factory)
    for k, v in props.items():
        e.set_property(k.replace("_", "-"), v)
    return e

src = {"els": [], "vpad": None, "apad": None, "ch": None, "live": False}
# Continuous-PTS repair. The black/silence fallback is stamped with pipeline running-time (a few
# thousand seconds of uptime); a live broadcast carries its own PCR-based PTS (tens of thousands of
# seconds). Passing those straight through makes the muxed output PTS leap ~10^5 s at EVERY switch
# (both fallback->live and live->fallback). A lone decoder rides it, but the multiview COMPOSITOR
# can't re-align the Live TV tile across such a jump and drops it to ~1 fps until the stream resets --
# the "video stepping after a channel change" bug. A probe on each encoder-input (selector output)
# rewrites PTS/DTS onto a single continuous timeline: normal frame-to-frame deltas pass through
# untouched (so real timing and A/V relationship are preserved within a segment), and any jump beyond
# TOL is bridged by continuing one step after the last emitted PTS. Video and audio self-correct
# independently; because the fallback keeps them aligned, they resume aligned after a switch.
FRAME_NS = 33333333            # ~1/30 s, the default step when a buffer has no duration
TOL_NS   = 500 * 1000000       # 0.5 s: bigger deltas are treated as a discontinuity to bridge

def make_repair(defdur):
    st = {"adj": 0, "end": None}
    def repair(pad, info):
        b = info.get_buffer()
        if b is None or b.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        dur = b.duration if b.duration != Gst.CLOCK_TIME_NONE and b.duration > 0 else defdur
        p = b.pts + st["adj"]
        if st["end"] is not None and (p < st["end"] - TOL_NS or p > st["end"] + TOL_NS):
            st["adj"] = st["end"] - b.pts     # discontinuity -> continue from where we left off
            p = b.pts + st["adj"]
        b.pts = p
        if b.dts != Gst.CLOCK_TIME_NONE:
            b.dts = b.dts + st["adj"]
        st["end"] = p + dur
        return Gst.PadProbeReturn.OK
    return repair
vsel.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, make_repair(FRAME_NS))
asel.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, make_repair(21333333))  # ~1024/48k audio
LASTGOOD = {"ch": DEFCH}   # last channel that actually went live; reverted to if a bad channel errors
# watchdog: change_at = monotonic time of the last (debounced) retune; tries = rebuild attempts since.
# If the source doesn't reach "live" within STUCK_S of a retune, it stuck on the black fallback
# (0.3 MB/s black HEVC). We rebuild the source in place; if that also stalls, exit -> systemd restart.
WATCH = {"change_at": 0.0, "tries": 0}
STUCK_S = 12
MAX_REBUILD = 2

def to_fallback():
    vsel.set_property("active-pad", vfb); asel.set_property("active-pad", afb)

def to_live():
    if src["vpad"]: vsel.set_property("active-pad", src["vpad"])
    if src["apad"]: asel.set_property("active-pad", src["apad"])
    src["live"] = True
    LASTGOOD["ch"] = src["ch"]   # this channel decoded successfully
    WATCH["change_at"] = 0.0; WATCH["tries"] = 0   # disarm watchdog: source is live

def teardown_source():
    for e in src["els"]:
        e.set_state(Gst.State.NULL)
    if src["vpad"]: vsel.release_request_pad(src["vpad"])
    if src["apad"]: asel.release_request_pad(src["apad"])
    for e in src["els"]:
        pipeline.remove(e)
    src["els"] = []; src["vpad"] = None; src["apad"] = None; src["live"] = False

def build_source(ch):
    s = mk("souphttpsrc", location=f"http://{HDHR}:5004/auto/v{ch}", is_live=True)
    dec = mk("decodebin3")
    pipeline.add(s); pipeline.add(dec); s.link(dec)
    src["els"] = [s, dec]; src["ch"] = ch

    def on_pad(_dec, pad):
        st = pad.query_caps(None).to_string()
        if st.startswith("video/") and src["vpad"] is None:
            chain = [mk("queue"), mk("deinterlace"), mk("videorate"), mk("videoscale"), mk("videoconvert"),
                     mk("capsfilter", caps=VCAPS)]
            for e in chain:
                pipeline.add(e); src["els"].append(e); e.sync_state_with_parent()
            for a, b in zip(chain, chain[1:]): a.link(b)
            pad.link(chain[0].get_static_pad("sink"))
            sp = vsel.get_request_pad("sink_%u"); chain[-1].get_static_pad("src").link(sp); src["vpad"] = sp
            # switch to live on the FIRST real frame through this pad (no guessing)
            sp.add_probe(Gst.PadProbeType.BUFFER, _first_buf)
        elif st.startswith("audio/") and src["apad"] is None:
            chain = [mk("queue"), mk("audioconvert"), mk("audioresample"), mk("capsfilter", caps=ACAPS)]
            for e in chain:
                pipeline.add(e); src["els"].append(e); e.sync_state_with_parent()
            for a, b in zip(chain, chain[1:]): a.link(b)
            pad.link(chain[0].get_static_pad("sink"))
            sp = asel.get_request_pad("sink_%u"); chain[-1].get_static_pad("src").link(sp); src["apad"] = sp
    dec.connect("pad-added", on_pad)
    for e in [s, dec]: e.sync_state_with_parent()

def _first_buf(pad, info):
    if not src["live"]:
        # Capture the epoch offset from this first live frame: off = running-time now - buffer PTS.
        # Applied (same value) to both selector pads in to_live() so the switch is PTS-continuous.
        GLib.idle_add(lambda: (to_live(), print(f"{time.strftime('%T')} -> live {src['ch']}", flush=True), False)[2])
    return Gst.PadProbeReturn.REMOVE

def change(ch):
    print(f"{time.strftime('%T')} tuning {ch} (fallback during retune; encoder stays up)", flush=True)
    to_fallback()
    teardown_source()
    build_source(ch)
    WATCH["change_at"] = time.monotonic(); WATCH["tries"] = 0   # arm watchdog for this retune

# self-heal: a bad channel (undecodable / NextGen) can error the shared pipeline and kill the feed.
# On any pipeline ERROR, revert the channel file to the last good one and exit -> systemd restarts us
# fresh on a channel we know decodes, instead of leaving 5010 dead.
def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        e, _dbg = msg.parse_error()
        bad = src["ch"]
        print(f"{time.strftime('%T')} ERROR on {bad}: {e.message} -> revert to {LASTGOOD['ch']}, restart", flush=True)
        try:
            if LASTGOOD["ch"] and LASTGOOD["ch"] != bad:
                open(STATE, "w").write(LASTGOOD["ch"])
        except Exception:
            pass
        os._exit(1)
bus = pipeline.get_bus(); bus.add_signal_watch(); bus.connect("message", on_bus)

pipeline.set_state(Gst.State.PLAYING)
to_fallback()
os.makedirs(os.path.dirname(STATE), exist_ok=True)
if not os.path.exists(STATE): open(STATE, "w").write(DEFCH)
change(open(STATE).read().strip() or DEFCH)
print(f"tv-send-inputselect: watching {STATE} -> {GRP}:{PORT} on {IFACE}", flush=True)

# Debounce: rapid channel-hopping used to tear down + rebuild the source faster than the HDHR tuner
# could settle, which is what left the sender stuck on the black fallback. We only actually retune
# once the requested channel has held steady for DEBOUNCE_S, so a fast burst collapses to one retune
# on the final channel. Poll is 0.5s for fine granularity.
PENDING = {"ch": None, "since": 0.0}
DEBOUNCE_S = 1.2
def poll():
    try:
        want = open(STATE).read().strip()
    except Exception:
        return True
    now = time.monotonic()
    if want and want != src["ch"]:
        if want != PENDING["ch"]:
            PENDING["ch"] = want; PENDING["since"] = now          # start/restart the settle timer
        elif now - PENDING["since"] >= DEBOUNCE_S:
            PENDING["ch"] = None
            change(want)
    else:
        PENDING["ch"] = None                                       # request matches current; clear
    return True
GLib.timeout_add(500, poll)

def watchdog():
    if not src["live"] and WATCH["change_at"] and (time.monotonic() - WATCH["change_at"]) > STUCK_S:
        WATCH["tries"] += 1
        if WATCH["tries"] <= MAX_REBUILD:
            print(f"{time.strftime('%T')} watchdog: {src['ch']} stuck on fallback "
                  f"({WATCH['tries']}/{MAX_REBUILD}) -> rebuild source", flush=True)
            ch = src["ch"]
            to_fallback(); teardown_source(); build_source(ch)
            WATCH["change_at"] = time.monotonic()
        else:
            # in-place rebuilds didn't recover it -> the switch/tuner state is wedged. Fall back to
            # last known-good channel and exit so systemd relaunches us fresh (what a wsl restart did
            # by hand). Only rewrite STATE if the stuck channel differs, so a genuinely bad channel
            # doesn't ping-pong.
            print(f"{time.strftime('%T')} watchdog: {src['ch']} still stuck after "
                  f"{MAX_REBUILD} rebuilds -> revert to {LASTGOOD['ch']}, restart", flush=True)
            try:
                if LASTGOOD["ch"] and LASTGOOD["ch"] != src["ch"]:
                    open(STATE, "w").write(LASTGOOD["ch"])
            except Exception:
                pass
            os._exit(1)
    return True
GLib.timeout_add_seconds(2, watchdog)
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipeline.set_state(Gst.State.NULL)
