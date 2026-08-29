#!/usr/bin/env python3
# ===========================================================================
#  Atoll live-TV bridge with SEAMLESS channel switching.
#
#  The old tv-send.sh killed the whole gst pipeline on a channel change, so the HEVC stream on
#  5010 stopped and restarted -- that discontinuity threw "Internal data stream error" in the
#  multiview decoder and took down the whole wall. Here the encoder+mux+udpsink run CONTINUOUSLY,
#  fed by intervideosrc/interaudiosrc; only the SOURCE pipeline (HDHR -> decode -> intervideosink)
#  restarts on a channel change. During the ~1-2s retune the inter srcs emit black/silence, so the
#  encoded stream never stops -> the receiver never errors. Only the Live TV picture briefly freezes.
#
#  Usage: tv-send-seamless.py [GROUP] [PORT] [STATE_FILE]   (defaults from atoll.conf -> live 5010)
# ===========================================================================
import gi, os, sys, subprocess, time, threading
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
VCAPS = "video/x-raw,format=NV12,width=1280,height=720,framerate=30/1"

# ---- persistent encoder tail: inter srcs -> HEVC + MP3 -> TS -> island. Never stops. ----
ACAPS = "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"
tail = Gst.parse_launch(
    "intervideosrc channel=tvv timeout=200000000 ! " + VCAPS + " ! queue ! cudaupload "
    "! nvh265enc rc-mode=cbr bitrate=6000 preset=p4 tune=low-latency gop-size=30 aud=true ! h265parse config-interval=-1 ! queue ! mux. "
    "interaudiosrc channel=tva ! " + ACAPS + " ! queue ! audioconvert ! audioresample "
    "! lamemp3enc target=bitrate bitrate=192 ! mpegaudioparse ! queue ! mux. "
    f"mpegtsmux name=mux ! queue ! udpsink host={GRP} port={PORT} multicast-iface={IFACE} auto-multicast=true ttl={TTL}")

# ---- source: HDHR channel -> decode -> inter sinks. Restarts on channel change. ----
# The inter-audio bridge only carries audio when BOTH sides agree on caps, so pin the same
# format on the interaudiosink input as the interaudiosrc output (else only silence flows).
def source_desc(ch):
    return (f"souphttpsrc location=http://{HDHR}:5004/auto/v{ch} is-live=true ! decodebin3 name=dec "
            f"dec. ! queue ! videorate ! videoscale ! videoconvert ! {VCAPS} ! intervideosink channel=tvv sync=false "
            f"dec. ! queue ! audioconvert ! audioresample ! {ACAPS} ! interaudiosink channel=tva sync=false")

src = {"pipe": None, "ch": None}
def start_source(ch):
    if src["pipe"]:
        src["pipe"].set_state(Gst.State.NULL)
    p = Gst.parse_launch(source_desc(ch))
    bus = p.get_bus(); bus.add_signal_watch()
    def on_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            e, _ = m.parse_error()
            print(f"{time.strftime('%T')} source ({src['ch']}) ERROR: {e.message} -> restart in 3s", flush=True)
            GLib.timeout_add(3000, lambda: (start_source(src["ch"]), False)[1])
    bus.connect("message", on_msg)
    p.set_state(Gst.State.PLAYING)
    src["pipe"] = p; src["ch"] = ch
    print(f"{time.strftime('%T')} tuning channel {ch} (tail keeps running -> {GRP}:{PORT})", flush=True)

tail.set_state(Gst.State.PLAYING)
os.makedirs(os.path.dirname(STATE), exist_ok=True)
if not os.path.exists(STATE):
    open(STATE, "w").write(DEFCH)
start_source(open(STATE).read().strip() or DEFCH)
print(f"tv-send-seamless: watching {STATE} -> {GRP}:{PORT} on {IFACE}", flush=True)

def poll():
    try:
        want = open(STATE).read().strip()
    except Exception:
        return True
    if want and want != src["ch"]:
        start_source(want)
    return True
GLib.timeout_add_seconds(1, poll)
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    if src["pipe"]: src["pipe"].set_state(Gst.State.NULL)
    tail.set_state(Gst.State.NULL)
