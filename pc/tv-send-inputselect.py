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
import gi, os, sys, subprocess, threading, time
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
    "preset=p4 tune=low-latency gop-size=30 aud=true ! h265parse config-interval=-1 name=vparse ! queue ! mux. "
    "input-selector name=asel sync-streams=false ! queue ! audioconvert ! audioresample "
    "! audio/x-raw,channels=6,channel-mask=(bitmask)0x3f ! avenc_aac bitrate=384000 ! aacparse ! queue ! mux. "
    f"mpegtsmux name=mux alignment=7 ! queue ! udpsink host={GRP} port={PORT} multicast-iface={IFACE} auto-multicast=true ttl={TTL} "
    "videotestsrc pattern=black is-live=true ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 ! vsel. "
    "audiotestsrc wave=silence is-live=true ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=6,channel-mask=(bitmask)0x3f ! asel.")
vsel = pipeline.get_by_name("vsel"); asel = pipeline.get_by_name("asel")
vfb = vsel.get_static_pad("sink_0"); afb = asel.get_static_pad("sink_0")

# Restamp the encoder input onto pipeline running-time. THE bug: a live broadcast's frames carry PCR
# PTS (~10^5 s) while nvh265enc always stamps DTS from the pipeline clock (~10^3 s of uptime). With no
# B-frames DTS must equal PTS, but here they sit ~95,000 s apart; in the 33-bit MPEG-TS clock (wraps
# ~26.5 h) that lands DTS AFTER PTS -- "display before decode" -- so the receiver's nvh265dec stalls
# and drops frames (Live-TV-only jumping, in single AND multi). Fixing DTS after the encoder doesn't
# stick (mpegtsmux re-derives it), so we fix the cause: shift each buffer so PTS rides the running-time
# clock, then DTS==PTS falls out naturally. ONE shared offset for video and audio (video owns it, audio
# follows) keeps the broadcast's A/V relationship intact. The black fallback already runs on running-
# time, so its offset is ~0 and it is unaffected.
FRAME_NS = 33333333
TOL_NS   = 500 * 1000000
PTSADJ = {"adj": 0, "vend": None}
def repair_v(pad, info):
    b = info.get_buffer()
    if b is None or b.pts == Gst.CLOCK_TIME_NONE:
        return Gst.PadProbeReturn.OK
    dur = b.duration if b.duration != Gst.CLOCK_TIME_NONE and b.duration > 0 else FRAME_NS
    p = b.pts + PTSADJ["adj"]
    if PTSADJ["vend"] is not None and (p < PTSADJ["vend"] - TOL_NS or p > PTSADJ["vend"] + TOL_NS):
        PTSADJ["adj"] = PTSADJ["vend"] - b.pts
        p = b.pts + PTSADJ["adj"]
    b.pts = p
    b.dts = p                              # no B-frames -> DTS must equal PTS
    PTSADJ["vend"] = p + dur
    return Gst.PadProbeReturn.OK
def repair_a(pad, info):
    b = info.get_buffer()
    if b is None:
        return Gst.PadProbeReturn.OK
    adj = PTSADJ["adj"]                     # follow video's shared offset -> A/V stays locked
    if b.pts != Gst.CLOCK_TIME_NONE:
        b.pts = b.pts + adj
    if b.dts != Gst.CLOCK_TIME_NONE:
        b.dts = b.dts + adj
    return Gst.PadProbeReturn.OK
vsel.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, repair_v)
asel.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, repair_a)

def mk(factory, **props):
    e = Gst.ElementFactory.make(factory)
    for k, v in props.items():
        e.set_property(k.replace("_", "-"), v)
    return e

src = {"els": [], "vpad": None, "apad": None, "ch": None, "live": False}
# NOTE: 5010's PTS carries each source's native epoch (live broadcast PCR ~10^5 s, black fallback
# pipeline running-time), so it leaps at a switch. That's harmless to a lone decoder AND to a receiver
# that normalises the tile's timeline; the multiview compositor needs that normalisation, which is done
# receiver-side with `identity single-segment=true` on the Live TV tile (output-render.sh). We do NOT
# rewrite PTS here -- an earlier sender-side repair-probe pushed the audio ~0.5 s out of sync, and the
# receiver's tsdemux normalises the epoch anyway.
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

# BUSY names the blocking call the main loop is inside (for the hang guard's log line).
BUSY = {"what": None}

def teardown_source():
    # Downstream -> upstream. Each element going to NULL deactivates its own pads, which flushes
    # any push blocked INTO it, so the element above can then stop its task. The old order (source
    # first) could wait forever on souphttpsrc's task while that task sat blocked pushing into a
    # full decodebin3/queue below it -- and that wait ran on the main loop, so the channel poller,
    # the GLib watchdog and the bus handler all froze with it: Live TV black and deaf to channel
    # changes until WSL was restarted (3 Sep 2026). The hang guard below is the backstop.
    for e in reversed(src["els"]):
        BUSY["what"] = f"teardown {e.get_name()} -> NULL"
        e.set_state(Gst.State.NULL)
    BUSY["what"] = "release selector pads"
    if src["vpad"]: vsel.release_request_pad(src["vpad"])
    if src["apad"]: asel.release_request_pad(src["apad"])
    for e in src["els"]:
        pipeline.remove(e)
    BUSY["what"] = None
    src["els"] = []; src["vpad"] = None; src["apad"] = None; src["live"] = False

def build_source(ch):
    s = mk("souphttpsrc", location=f"http://{HDHR}:5004/auto/v{ch}", is_live=True)
    dec = mk("decodebin3")
    pipeline.add(s); pipeline.add(dec); s.link(dec)
    src["els"] = [s, dec]; src["ch"] = ch
    BUSY["what"] = f"build source {ch}"

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
    BUSY["what"] = None

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

# Main-loop hang guard. The GLib watchdog above cannot fire if the main loop itself is stuck inside
# a GStreamer call that never returns (a state change during teardown, 3 Sep 2026). A plain thread
# checks the loop is still ticking; after HANG_S of silence it logs where the loop stuck and exits 1
# so systemd relaunches us on the requested channel: black for ~HANG_S + RestartSec instead of until
# someone restarts WSL. Live TV is a demo feed; a 25 s outage beats an indefinite one.
HANG_S = 20
TICK = {"t": time.monotonic()}
def tick():
    TICK["t"] = time.monotonic()
    return True
GLib.timeout_add(1000, tick)
def hang_guard():
    while True:
        time.sleep(2)
        stale = time.monotonic() - TICK["t"]
        if stale > HANG_S:
            print(f"{time.strftime('%T')} HANG: main loop silent {stale:.0f}s (channel {src['ch']}, "
                  f"stuck in: {BUSY['what'] or 'unknown'}) -> exit 1, systemd restarts", flush=True)
            os._exit(1)
threading.Thread(target=hang_guard, name="hang-guard", daemon=True).start()

loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipeline.set_state(Gst.State.NULL)
