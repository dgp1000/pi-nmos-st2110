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
ACAPS = Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved")

# ---- persistent tail + fallback (one pipeline). vsel/asel sink_0 = the black/silence fallback. ----
pipeline = Gst.parse_launch(
    "input-selector name=vsel sync-streams=false ! queue ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 "
    "preset=p4 tune=low-latency gop-size=30 aud=true ! h265parse config-interval=-1 ! queue ! mux. "
    "input-selector name=asel sync-streams=false ! queue ! audioconvert ! audioresample ! lamemp3enc "
    "target=bitrate bitrate=192 ! mpegaudioparse ! queue ! mux. "
    f"mpegtsmux name=mux ! queue ! udpsink host={GRP} port={PORT} multicast-iface={IFACE} auto-multicast=true ttl={TTL} "
    "videotestsrc pattern=black is-live=true ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 ! vsel. "
    "audiotestsrc wave=silence is-live=true ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! asel.")
vsel = pipeline.get_by_name("vsel"); asel = pipeline.get_by_name("asel")
vfb = vsel.get_static_pad("sink_0"); afb = asel.get_static_pad("sink_0")

def mk(factory, **props):
    e = Gst.ElementFactory.make(factory)
    for k, v in props.items():
        e.set_property(k.replace("_", "-"), v)
    return e

src = {"els": [], "vpad": None, "apad": None, "ch": None, "live": False}

def to_fallback():
    vsel.set_property("active-pad", vfb); asel.set_property("active-pad", afb)

def to_live():
    if src["vpad"]: vsel.set_property("active-pad", src["vpad"])
    if src["apad"]: asel.set_property("active-pad", src["apad"])
    src["live"] = True

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
            chain = [mk("queue"), mk("videorate"), mk("videoscale"), mk("videoconvert"),
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
        GLib.idle_add(lambda: (to_live(), print(f"{time.strftime('%T')} -> live {src['ch']}", flush=True), False)[2])
    return Gst.PadProbeReturn.REMOVE

def change(ch):
    print(f"{time.strftime('%T')} tuning {ch} (fallback during retune; encoder stays up)", flush=True)
    to_fallback()
    teardown_source()
    build_source(ch)

pipeline.set_state(Gst.State.PLAYING)
to_fallback()
os.makedirs(os.path.dirname(STATE), exist_ok=True)
if not os.path.exists(STATE): open(STATE, "w").write(DEFCH)
change(open(STATE).read().strip() or DEFCH)
print(f"tv-send-inputselect: watching {STATE} -> {GRP}:{PORT} on {IFACE}", flush=True)

def poll():
    try:
        want = open(STATE).read().strip()
    except Exception:
        return True
    if want and want != src["ch"]:
        change(want)
    return True
GLib.timeout_add_seconds(1, poll)
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipeline.set_state(Gst.State.NULL)
