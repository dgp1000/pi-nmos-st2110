#!/usr/bin/env python3
"""Atoll ST 2110-22 JPEG XS RECEIVER (RFC 9134 `video/jxsv`, codestream mode).

The companion to `jxs-rtp-send.py`. GStreamer has no RFC 9134 depayloader, so we reassemble in
Python: join the multicast group, collect each RTP packet's codestream fragment, and on the RTP
marker bit (last packet of the frame) push the whole `image/x-jxsc` codestream into an appsrc feeding
`svtjpegxsdec`. Proves the hand-built 2110-22 stream is standards-decodable end to end.

JXS_SINK=count (default) decodes headless and reports frame count + resolution; JXS_SINK=display
opens a window (a real tile). Env/conf: JXSV_GRP, JXSV_PORT, ISLAND_PC_IP, JXS_W/JXS_H/JXS_FPS/
JXS_SAMPLING.
"""
import gi, os, socket, struct, subprocess, threading, signal, time, sys
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["JXSV_GRP", "JXSV_PORT", "ISLAND_PC_IP", "JXS_W", "JXS_H", "JXS_FPS", "JXS_SAMPLING"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
GRP   = CFG.get("JXSV_GRP") or "239.10.10.61"
PORT  = int(CFG.get("JXSV_PORT") or 5032)
LOCAL = CFG.get("ISLAND_PC_IP") or "10.10.10.2"

W    = int(os.environ.get("JXS_W", CFG.get("JXS_W") or "1280"))
H    = int(os.environ.get("JXS_H", CFG.get("JXS_H") or "720"))
FPS  = os.environ.get("JXS_FPS", CFG.get("JXS_FPS") or "60")
SAMP = os.environ.get("JXS_SAMPLING", CFG.get("JXS_SAMPLING") or "YCbCr-4:2:2")
SINK = os.environ.get("JXS_SINK", "count")
fn, fd = (FPS.split("/") + ["1"])[:2]
FPS_N, FPS_D = int(fn), int(fd)
FRAME_DUR = Gst.SECOND * FPS_D // FPS_N

CAPS = (f"image/x-jxsc,width={W},height={H},sampling={SAMP},"
        f"framerate={FPS_N}/{FPS_D},interlace-mode=progressive,alignment=frame,depth=8")

_dec = {"n": 0, "wh": ""}
_start = time.time()


def main():
    Gst.init(None)
    tail = ("videoconvert ! autovideosink sync=false" if SINK == "display"
            else "videoconvert ! video/x-raw ! appsink name=dec emit-signals=true sync=false max-buffers=4 drop=true")
    pipe = Gst.parse_launch(f"appsrc name=src is-live=true do-timestamp=false format=time ! {CAPS} ! svtjpegxsdec ! {tail}")
    src = pipe.get_by_name("src")
    src.set_property("caps", Gst.Caps.from_string(CAPS))
    src.set_property("format", Gst.Format.TIME)

    if SINK != "display":
        def on_dec(sink):
            s = sink.emit("pull-sample")
            if s:
                _dec["n"] += 1
                st = s.get_caps().get_structure(0)
                _dec["wh"] = f"{st.get_value('width')}x{st.get_value('height')} {st.get_value('format')}"
            return Gst.FlowReturn.OK
        pipe.get_by_name("dec").connect("new-sample", on_dec)

    pipe.set_state(Gst.State.PLAYING)
    print(f"jxs-rtp-recv: video/jxsv <- {GRP}:{PORT}  ({SINK} mode, expect {W}x{H} {SAMP})", flush=True)

    stop = threading.Event()

    def rx():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(GRP), socket.inet_aton(LOCAL)))
        try: s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
        except OSError: pass
        s.settimeout(1.0)
        cur = bytearray(); cur_ts = None; pts = 0
        while not stop.is_set():
            try:
                pkt, _ = s.recvfrom(2048)
            except socket.timeout:
                continue
            if len(pkt) < 16:
                continue
            b1 = pkt[1]; marker = (b1 >> 7) & 1
            ts = struct.unpack("!I", pkt[4:8])[0]
            if cur_ts is None:
                cur_ts = ts
            if ts != cur_ts and cur:          # new frame began but the old marker was lost -> drop the partial
                cur = bytearray(); cur_ts = ts
            cur += pkt[16:]
            if marker:                        # last packet of this frame
                _push(src, cur, pts); pts += FRAME_DUR; cur = bytearray(); cur_ts = None
        s.close()

    def _push(appsrc, data, pts):
        if data[:2] != b"\xff\x10":           # only push complete codestreams (SOC marker)
            return
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, bytes(data))
        buf.pts = pts; buf.duration = FRAME_DUR
        appsrc.emit("push-buffer", buf)

    t = threading.Thread(target=rx, daemon=True); t.start()

    loop = GLib.MainLoop()
    def report():
        if SINK != "display":
            dt = max(0.001, time.time() - _start)
            print(f"jxs-rtp-recv: decoded {_dec['n']} frames  ({_dec['n']/dt:.1f} fps)  {_dec['wh']}", flush=True)
        return True
    GLib.timeout_add_seconds(2, report)

    def _stop(*_):
        stop.set(); pipe.set_state(Gst.State.NULL); loop.quit()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        loop.run()
    finally:
        stop.set(); pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
