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
#  Channel changes are MAKE-BEFORE-BREAK (3 Sep 2026): the new channel is opened as a second
#  branch on a second HDHomeRun tuner while the old one stays on air; the selectors cut once the
#  new branch has decoded its first video AND audio frame, and the old branch (and its tuner) is
#  released. No black gap; the fallback is only seen at start-up or if the on-air branch dies.
#
#  Usage: tv-send-inputselect.py [GROUP] [PORT] [STATE_FILE]   (defaults from atoll.conf -> 5010)
# ===========================================================================
import collections, gi, os, sys, subprocess, threading, time
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
# MUXQ: the compressed queues feeding mpegtsmux must hold several seconds. A broadcast transmits
# video ~1 s AHEAD of the matching audio, and since audio and video share one timestamp offset
# (lip sync), the mux has to hold that second of video until its audio arrives. The default 1 s
# queue filled, blocked the encoder, blocked the branch, and the demuxer then stopped delivering
# audio too -- the mux waited on audio, the feed came through in 1-2 s clumps, and the synced
# udpsink turned that into 0.6-3 s holes on 5010 after every channel change (3 Sep 2026).
MUXQ = "queue max-size-time=4000000000 max-size-bytes=0 max-size-buffers=0"
# SINKQ: the queue between the mux and the clock-synced udpsink must hold more than the pinned
# latency. udpsink sends each buffer at its timestamp + FIXED_LATENCY_S, so it holds ~1.5 s of
# stream at any moment; the default 1 s queue was permanently FULL, which blocked the mux, the
# encoder, the selector, the branch and finally souphttpsrc (gdb: every streaming thread parked in
# gst_pad_push behind the sink's clock wait). The whole chain then advanced in lockstep with the
# sink and every hiccup became a 1-3 s source stall. A live source must never be throttled by the
# sink: give the sink queue room for the full latency.
SINKQ = "queue max-size-time=6000000000 max-size-bytes=0 max-size-buffers=0"
pipeline = Gst.parse_launch(
    "input-selector name=vsel sync-streams=false ! queue ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 "
    "preset=p4 tune=low-latency gop-size=30 aud=true ! h265parse config-interval=-1 name=vparse ! " + MUXQ + " ! mux. "
    "input-selector name=asel sync-streams=false ! queue ! audioconvert ! audioresample "
    "! audio/x-raw,channels=6,channel-mask=(bitmask)0x3f ! avenc_aac bitrate=384000 ! aacparse ! " + MUXQ + " ! mux. "
    f"mpegtsmux name=mux alignment=7 ! " + SINKQ + f" ! udpsink host={GRP} port={PORT} multicast-iface={IFACE} auto-multicast=true ttl={TTL} "
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
# "pending" is set at a cut: the next video buffer re-anchors unconditionally (a new branch's PTS
# start near 0, so the 500 ms tolerance alone is not a reliable trigger), and audio is dropped
# until that has happened -- otherwise the first audio frames of the new channel go out with the
# OLD channel's offset (seen as a 12 s PTS excursion on the wire, a discontinuity for receivers).
PTSADJ = {"adj": 0, "vend": None, "pending": False}
def repair_v(pad, info):
    b = info.get_buffer()
    if b is None or b.pts == Gst.CLOCK_TIME_NONE:
        return Gst.PadProbeReturn.OK
    dur = b.duration if b.duration != Gst.CLOCK_TIME_NONE and b.duration > 0 else FRAME_NS
    p = b.pts + PTSADJ["adj"]
    if PTSADJ["vend"] is not None and (PTSADJ["pending"] or p < PTSADJ["vend"] - TOL_NS or p > PTSADJ["vend"] + TOL_NS):
        PTSADJ["adj"] = PTSADJ["vend"] - b.pts
        p = b.pts + PTSADJ["adj"]
    PTSADJ["pending"] = False
    b.pts = p
    b.dts = p                              # no B-frames -> DTS must equal PTS
    PTSADJ["vend"] = p + dur
    return Gst.PadProbeReturn.OK
def repair_a(pad, info):
    b = info.get_buffer()
    if b is None:
        return Gst.PadProbeReturn.OK
    if PTSADJ["pending"]:                   # video has not re-anchored yet: this offset is stale
        return Gst.PadProbeReturn.DROP
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

# ---- source branches: `air` is on the selectors, `standby` is the one being tuned. ---------------
# Make-before-break (3 Sep 2026). A channel change builds the new channel as a SECOND branch, on a
# second HDHomeRun tuner (each /auto/ stream gets its own tuner; the FLEX 4K has four), while the
# old branch stays on air. The selectors cut over on the new branch's first decoded frame, then
# the old branch is torn down and its tuner released. The black/silence fallback (sink_0) is shown
# only when there is nothing on air: start-up, or after the on-air branch itself fails.
def new_branch(ch):
    return {"els": [], "vpad": None, "apad": None, "ch": ch, "live": False, "dead": False,
            "vready": False, "aready": False, "cut_timer": None}
BR = {"air": None, "standby": None}
def air_ch():
    return BR["air"]["ch"] if BR["air"] else None
def requested():
    """The channel the poller should consider satisfied: the one being tuned, else the one on air."""
    return BR["standby"]["ch"] if BR["standby"] else air_ch()

# NOTE: 5010's PTS carries each source's native epoch (live broadcast PCR ~10^5 s, black fallback
# pipeline running-time), so it leaps at a switch; repair_v/repair_a above re-offset onto running-
# time so the encoder sees a continuous timeline, and the receiver normalises the rest.
LASTGOOD = {"ch": DEFCH}   # last channel that actually went live; reverted to if a bad channel errors
# watchdog: change_at = monotonic time of the last (debounced) retune; tries = rebuild attempts since.
# If the standby doesn't reach "live" within STUCK_S we rebuild it; if that fails MAX_REBUILD times
# we drop it and leave the current channel on air (or, with nothing on air, fall back to LASTGOOD).
WATCH = {"change_at": 0.0, "tries": 0}
STUCK_S = 12
MAX_REBUILD = 2
# BUSY names the blocking call the main loop is inside (for the hang guard's log line).
BUSY = {"what": None}
# Element names of the last few torn-down branches: a late ERROR from one of them (decodebin3's
# internals report after the source's own 404) must not be mistaken for the encoder tail failing.
DEAD = collections.deque(maxlen=4)
AUDIO_WAIT_S = 2   # cut on video alone if the standby's audio has not produced a frame by then

def to_fallback():
    vsel.set_property("active-pad", vfb); asel.set_property("active-pad", afb)

def revert_state(ch):
    """Rewrite the channel file so the poller stops asking for a channel we have given up on."""
    try:
        if ch:
            open(STATE, "w").write(ch)
    except Exception:
        pass

def teardown(br):
    # Downstream -> upstream. Each element going to NULL deactivates its own pads, which flushes
    # any push blocked INTO it, so the element above can then stop its task. Source-first could
    # wait forever on souphttpsrc's task while that task sat blocked pushing into a full
    # decodebin3/queue below it -- and that wait ran on the main loop, so the channel poller, the
    # GLib watchdog and the bus handler all froze with it: Live TV black and deaf to channel
    # changes until WSL was restarted (3 Sep 2026). The hang guard below is the backstop.
    if br is None or br["dead"]:
        return
    br["dead"] = True                      # on_pad / cut_to for this branch become no-ops
    els = list(br["els"])
    DEAD.append(frozenset(e.get_name() for e in els))
    for e in reversed(els):
        BUSY["what"] = f"teardown {br['ch']}: {e.get_name()} -> NULL"
        e.set_state(Gst.State.NULL)
    BUSY["what"] = f"teardown {br['ch']}: release selector pads"
    if br["vpad"]: vsel.release_request_pad(br["vpad"])
    if br["apad"]: asel.release_request_pad(br["apad"])
    for e in els:
        pipeline.remove(e)
    BUSY["what"] = None
    br["els"] = []; br["vpad"] = None; br["apad"] = None; br["live"] = False

def ready(br, what):
    """First decoded frame of a kind ('v' / 'a') on a branch. Main loop."""
    if br["dead"]:
        return
    br[what + "ready"] = True
    if BR["air"] is br:                       # already on air, audio trailing the cut: bring it in
        if what == "a":
            asel.set_property("active-pad", br["apad"])
        return
    if BR["standby"] is not br:
        return
    # Cut only once BOTH have flowed: mpegtsmux waits for every pad, so switching audio to a pad
    # that has not produced anything yet starves the whole output (measured 1.2 s hole on 5010).
    if br["vready"] and br["aready"]:
        cut_to(br)
    elif br["vready"] and br["cut_timer"] is None:
        br["cut_timer"] = GLib.timeout_add(AUDIO_WAIT_S * 1000, lambda: (cut_to(br), False)[1])

def cut_to(br):
    """Put a branch on the selectors. Runs on the main loop."""
    if br["dead"] or BR["standby"] is not br:   # superseded by a later change meanwhile
        return
    old = BR["air"]
    PTSADJ["pending"] = True                  # re-anchor on the new branch's first video frame
    if br["vpad"]: vsel.set_property("active-pad", br["vpad"])
    asel.set_property("active-pad", br["apad"] if br["aready"] else afb)   # silence until audio flows
    br["live"] = True
    BR["air"] = br; BR["standby"] = None
    LASTGOOD["ch"] = br["ch"]                 # this channel decoded successfully
    WATCH["change_at"] = 0.0; WATCH["tries"] = 0
    print(f"{time.strftime('%T')} -> live {br['ch']}" + (f", released {old['ch']}" if old else ""), flush=True)
    teardown(old)                             # frees the old tuner

def build(br):
    ch = br["ch"]
    BUSY["what"] = f"build source {ch}"
    s = mk("souphttpsrc", location=f"http://{HDHR}:5004/auto/v{ch}", is_live=True)
    dec = mk("decodebin3")
    pipeline.add(s); pipeline.add(dec); s.link(dec)
    br["els"] = [s, dec]
    # Give this souphttpsrc its OWN libsoup session. Since GStreamer 1.24 all souphttpsrc in a
    # pipeline share one SoupSession (one I/O thread) through the "gst.soup.session" context the
    # pipeline caches from the first source. With the old and new channel on one session thread,
    # the new branch's delivery stalled for 1-5 s right after the cut until the old branch was torn
    # down (both streams from decodebin3 went quiet). Replacing the cached context with an empty
    # one just before this source opens makes it create a session of its own (verified: one
    # "souphttpsrc" thread per source, no stalls).
    pipeline.set_context(Gst.Context.new("gst.soup.session", True))

    def first_vbuf(pad, info):                # first real frames, no guessing: see ready()
        GLib.idle_add(lambda: (ready(br, "v"), False)[1])
        return Gst.PadProbeReturn.REMOVE
    def first_abuf(pad, info):
        GLib.idle_add(lambda: (ready(br, "a"), False)[1])
        return Gst.PadProbeReturn.REMOVE

    def on_pad(_dec, pad):
        if br["dead"]:
            return
        st = pad.query_caps(None).to_string()
        if st.startswith("video/") and br["vpad"] is None:
            chain = [mk("queue"), mk("deinterlace"), mk("videorate"), mk("videoscale"), mk("videoconvert"),
                     mk("capsfilter", caps=VCAPS)]
            for e in chain:
                pipeline.add(e); br["els"].append(e); e.sync_state_with_parent()
            for a, b in zip(chain, chain[1:]): a.link(b)
            pad.link(chain[0].get_static_pad("sink"))
            sp = vsel.request_pad_simple("sink_%u"); chain[-1].get_static_pad("src").link(sp); br["vpad"] = sp
            sp.add_probe(Gst.PadProbeType.BUFFER, first_vbuf)
        elif st.startswith("audio/") and br["apad"] is None:
            chain = [mk("queue"), mk("audioconvert"), mk("audioresample"), mk("capsfilter", caps=ACAPS)]
            for e in chain:
                pipeline.add(e); br["els"].append(e); e.sync_state_with_parent()
            for a, b in zip(chain, chain[1:]): a.link(b)
            pad.link(chain[0].get_static_pad("sink"))
            sp = asel.request_pad_simple("sink_%u"); chain[-1].get_static_pad("src").link(sp); br["apad"] = sp
            sp.add_probe(Gst.PadProbeType.BUFFER, first_abuf)
    dec.connect("pad-added", on_pad)
    for e in [s, dec]: e.sync_state_with_parent()
    BUSY["what"] = None

def change(ch):
    sb = BR["standby"]
    if sb:
        print(f"{time.strftime('%T')} dropping standby {sb['ch']} for {ch}", flush=True)
        teardown(sb); BR["standby"] = None
    on_air = air_ch()
    print(f"{time.strftime('%T')} tuning {ch}" + (f" on a second tuner ({on_air} stays on air)" if on_air
                                                  else " (black until live)"), flush=True)
    br = new_branch(ch); BR["standby"] = br
    build(br)
    WATCH["change_at"] = time.monotonic(); WATCH["tries"] = 0   # arm watchdog for this retune

def owner(el):
    """Which branch a bus message's source belongs to (walks up out of decodebin3's internals)."""
    while el is not None and el is not pipeline:
        name = el.get_name()
        if any(name in names for names in DEAD):
            return "dead"
        for k in ("air", "standby"):
            br = BR[k]
            if br and any(x.get_name() == name for x in br["els"]):
                return k
        el = el.get_parent()
    return None

# Self-heal on pipeline ERROR. A bad channel on the STANDBY (undecodable, NextGen, no free tuner ->
# "Service Unavailable") only costs us that attempt: drop it, keep the current channel on air, put
# its number back in the channel file. An error from the on-air branch or the shared encoder tail
# leaves nothing to show, so revert the file to the last good channel and exit -> systemd restarts.
def on_bus(_bus, msg):
    if msg.type != Gst.MessageType.ERROR:
        return
    e, _dbg = msg.parse_error()
    who = owner(msg.src)
    if who == "dead":
        print(f"{time.strftime('%T')} (late error from a torn-down branch, ignored: {e.message})", flush=True)
        return
    if who == "standby":
        bad = BR["standby"]["ch"]
        print(f"{time.strftime('%T')} ERROR tuning {bad}: {e.message} -> dropped, {air_ch() or 'black'} stays on air", flush=True)
        teardown(BR["standby"]); BR["standby"] = None
        WATCH["change_at"] = 0.0
        revert_state(air_ch())
        return
    bad = air_ch() or requested()
    print(f"{time.strftime('%T')} ERROR on {bad} ({who or 'tail'}): {e.message} -> revert to {LASTGOOD['ch']}, restart", flush=True)
    if LASTGOOD["ch"] and LASTGOOD["ch"] != bad:
        revert_state(LASTGOOD["ch"])
    os._exit(1)
bus = pipeline.get_bus(); bus.add_signal_watch(); bus.connect("message", on_bus)

# Pin the pipeline latency. udpsink is clock-synced (it is what smooths the mux's bursty output
# into an even packet stream), sending each buffer at its timestamp + this latency. Timestamps are
# anchored on the moment the new channel's first video frame ARRIVES, but the mux can only emit a
# video frame once the matching audio has arrived ~1 s later, plus encode time; the queried minimum
# (~0.8 s) is below that, so buffers would reach the sink late and go out in bursts. Fixed at 2.5 s
# the schedule always holds. The cost is a constant ~1.7 s more Live TV delay than before, which
# nothing on the island depends on (the tile's own A/V stays locked inside the TS).
FIXED_LATENCY_S = 2.5
pipeline.set_latency(int(FIXED_LATENCY_S * Gst.SECOND))
pipeline.set_state(Gst.State.PLAYING)
to_fallback()
os.makedirs(os.path.dirname(STATE), exist_ok=True)
if not os.path.exists(STATE): open(STATE, "w").write(DEFCH)
change(open(STATE).read().strip() or DEFCH)
print(f"tv-send-inputselect: watching {STATE} -> {GRP}:{PORT} on {IFACE}", flush=True)

# Debounce: rapid channel-hopping used to tear down + rebuild the source faster than the HDHR tuner
# could settle. We only actually retune once the requested channel has held steady for DEBOUNCE_S,
# so a fast burst collapses to one retune on the final channel. Poll is 0.5s for fine granularity.
PENDING = {"ch": None, "since": 0.0}
DEBOUNCE_S = 1.2
def poll():
    try:
        want = open(STATE).read().strip()
    except Exception:
        return True
    now = time.monotonic()
    if want and want != requested():
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
    sb = BR["standby"]
    if sb and WATCH["change_at"] and (time.monotonic() - WATCH["change_at"]) > STUCK_S:
        WATCH["tries"] += 1
        if WATCH["tries"] <= MAX_REBUILD:
            print(f"{time.strftime('%T')} watchdog: {sb['ch']} not live after {STUCK_S}s "
                  f"({WATCH['tries']}/{MAX_REBUILD}) -> rebuild standby", flush=True)
            teardown(sb); br = new_branch(sb["ch"]); BR["standby"] = br; build(br)
            WATCH["change_at"] = time.monotonic()
        elif air_ch():
            print(f"{time.strftime('%T')} watchdog: {sb['ch']} still not live after {MAX_REBUILD} rebuilds "
                  f"-> giving up, {air_ch()} stays on air", flush=True)
            teardown(sb); BR["standby"] = None; WATCH["change_at"] = 0.0
            revert_state(air_ch())
        elif LASTGOOD["ch"] and LASTGOOD["ch"] != sb["ch"]:
            # nothing on air yet (start-up on a dead channel): go to the last good one instead
            print(f"{time.strftime('%T')} watchdog: {sb['ch']} still not live after {MAX_REBUILD} rebuilds "
                  f"-> nothing on air, tuning {LASTGOOD['ch']}", flush=True)
            revert_state(LASTGOOD["ch"]); change(LASTGOOD["ch"])
        else:
            print(f"{time.strftime('%T')} watchdog: {sb['ch']} still not live after {MAX_REBUILD} rebuilds "
                  f"and nothing else to show -> restart", flush=True)
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
            print(f"{time.strftime('%T')} HANG: main loop silent {stale:.0f}s (channel {requested()}, "
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
