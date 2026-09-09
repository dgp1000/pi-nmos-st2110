#!/usr/bin/env python3
"""Atoll TRUE ST 2110-22 JPEG XS sender (AMWA BCP-006-01 / RFC 9134 `video/jxsv`).

The other JPEG XS path (`jxs-send.sh`) muxes the codestream into MPEG-TS -- convenient, but not
ST 2110-22. This is the real thing: the SVT-JPEG-XS codestream (`image/x-jxsc`, one per frame) is
carried directly in RTP with the RFC 9134 payload format, so it is a standards-clean 2110-22
essence that `jxs-nmos.py` advertises with a conformant SDP.

GStreamer has the codec but no RFC 9134 payloader, so -- as with the RFC 8331 ancillary sender --
we hand-build it: an appsink hands each whole codestream to Python, which fragments it into RTP
packets in **codestream packetization mode** (K=0) and multicasts them. Payload header per frame
(RFC 9134 sec 4.2), 32 bits after the RTP header:

    |T|K|L| I |F counter|     SEP counter     |     P counter       |
     0 1 2 3-4  5....9    10..............20    21.............31

  T=1 sequential, K=0 codestream, L=1 on the last packet of the frame, I=00 progressive,
  F = frame number mod 32, SEP = 0 (P never overruns 2048 here), P = packet index mod 2048.
RTP: dynamic PT, marker on the last packet of a frame, 90 kHz timestamp shared across the frame.

Env/conf: JXSV_GRP, JXSV_PORT, ISLAND_PC_IP, ISLAND_IFACE, MCAST_TTL, plus JXS_W/JXS_H/JXS_FPS/
JXS_BPP/JXS_SAMPLING overrides.
"""
import gi, os, sys, socket, struct, subprocess, signal, random
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["JXSV_GRP", "JXSV_PORT", "ISLAND_PC_IP", "ISLAND_IFACE", "MCAST_TTL",
        "JXS_W", "JXS_H", "JXS_FPS", "JXS_BPP", "JXS_SAMPLING", "JXS_PT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
GRP   = CFG.get("JXSV_GRP") or "239.10.10.61"
PORT  = int(CFG.get("JXSV_PORT") or 5032)
LOCAL = CFG.get("ISLAND_PC_IP") or "10.10.10.2"
IFACE = CFG.get("ISLAND_IFACE") or "eth0"
TTL   = int(CFG.get("MCAST_TTL") or 1)

W    = int(os.environ.get("JXS_W", CFG.get("JXS_W") or "1280"))
H    = int(os.environ.get("JXS_H", CFG.get("JXS_H") or "720"))
FPS  = os.environ.get("JXS_FPS", CFG.get("JXS_FPS") or "60")          # gst fraction; e.g. 60/1 or 60000/1001
BPP  = os.environ.get("JXS_BPP", CFG.get("JXS_BPP") or "2")             # JPEG XS bits per pixel (quality vs bitrate)
SAMP = os.environ.get("JXS_SAMPLING", CFG.get("JXS_SAMPLING") or "YCbCr-4:2:2")
GSTFMT = {"YCbCr-4:2:2": "Y42B", "YCbCr-4:4:4": "Y444", "YCbCr-4:2:0": "I420"}.get(SAMP, "Y42B")
PT   = int(os.environ.get("JXS_PT", CFG.get("JXS_PT") or "112"))       # dynamic payload type
MTU  = int(os.environ.get("JXS_MTU", "1400"))     # codestream bytes per RTP packet

fn, fd = (FPS.split("/") + ["1"])[:2]
FPS_N, FPS_D = int(fn), int(fd)
TS_HZ = 90000

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOCAL))

_ssrc = random.getrandbits(32)
_state = {"seq": random.getrandbits(16), "frame": 0}


def send_frame(cs: bytes):
    """Fragment one JPEG XS codestream into RFC 9134 codestream-mode RTP packets and multicast them."""
    frame = _state["frame"]
    ts = round(frame * TS_HZ * FPS_D / FPS_N) & 0xFFFFFFFF
    npkts = max(1, (len(cs) + MTU - 1) // MTU)
    for i in range(npkts):
        chunk = cs[i * MTU:(i + 1) * MTU]
        last = (i == npkts - 1)
        seq = _state["seq"] & 0xFFFF
        b1 = (0x80 if last else 0x00) | (PT & 0x7F)      # marker on the frame's last packet
        rtp = struct.pack("!BBHII", 0x80, b1, seq, ts, _ssrc)
        T, K, L, I = 1, 0, (1 if last else 0), 0         # sequential, codestream, last?, progressive
        F = frame & 0x1F
        P = i & 0x7FF
        word = (T << 31) | (K << 30) | (L << 29) | (I << 27) | (F << 22) | (0 << 11) | P
        ph = struct.pack("!I", word)
        try:
            sock.sendto(rtp + ph + chunk, (GRP, PORT))
        except OSError:
            pass
        _state["seq"] = (_state["seq"] + 1) & 0xFFFF
    _state["frame"] = (frame + 1) & 0xFFFFFFFF


def on_sample(sink):
    sample = sink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK
    buf = sample.get_buffer()
    ok, minfo = buf.map(Gst.MapFlags.READ)
    if ok:
        try:
            send_frame(bytes(minfo.data))
        finally:
            buf.unmap(minfo)
    return Gst.FlowReturn.OK


def main():
    Gst.init(None)
    desc = (
        f"videotestsrc pattern=ball is-live=true "
        f"! video/x-raw,width={W},height={H},framerate={FPS_N}/{FPS_D} "
        f"! textoverlay text='JPEG XS - ST 2110-22 (RFC 9134 video/jxsv)' valignment=top halignment=center "
        f"  font-desc='Sans Bold 22' shaded-background=true "
        f"! clockoverlay valignment=bottom halignment=right time-format='%H:%M:%S' "
        f"  font-desc='Sans Bold 18' shaded-background=true "
        f"! videoconvert ! video/x-raw,format={GSTFMT} "
        f"! svtjpegxsenc bits-per-pixel={BPP} rate-control-mode=cbr-precinct "
        f"! image/x-jxsc "
        f"! appsink name=out emit-signals=true sync=true max-buffers=4 drop=false"
    )
    pipe = Gst.parse_launch(desc)
    pipe.get_by_name("out").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    print(f"jxs-rtp-send: video/jxsv -> {GRP}:{PORT} pt {PT}  {W}x{H}@{FPS} {SAMP} bpp {BPP} (RFC 9134 codestream mode)", flush=True)

    loop = GLib.MainLoop()
    def _stop(*_):
        pipe.set_state(Gst.State.NULL); loop.quit()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    bus = pipe.get_bus(); bus.add_signal_watch()
    def _on_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            err, dbg = m.parse_error(); print(f"jxs-rtp-send: ERROR {err}: {dbg}", flush=True); _stop()
        elif m.type == Gst.MessageType.EOS:
            print("jxs-rtp-send: EOS", flush=True); _stop()
    bus.connect("message", _on_msg)
    try:
        loop.run()
    finally:
        pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
