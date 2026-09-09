#!/usr/bin/env python3
"""Atoll ST 2110-22 JPEG XS RECEIVER (RFC 9134 `video/jxsv`, codestream mode).

The companion to `jxs-rtp-send.py`. GStreamer has no RFC 9134 depayloader, so we reassemble in
Python: join the multicast group, collect each RTP packet's codestream fragment, and on the RTP
marker bit (last packet of the frame) push the whole `image/x-jxsc` codestream into an appsrc feeding
`svtjpegxsdec`. Proves the hand-built 2110-22 stream is standards-decodable end to end.

A frame is pushed only if it is whole: it must start with the JPEG XS SOC (0xFF10) AND have suffered
no RTP sequence gap between its packets -- a partial or torn frame is dropped, so the decoder never
sees corrupt data (which otherwise shows as ghost/flashing frames). For display the frames are paced
by a fixed-cadence PTS (clock-aligned, small lead) with `glimagesink sync=true` and a jitter queue,
so bursty decode output plays smoothly; the picture is GL-upscaled to fill the monitor.

JXS_SINK=count (default) decodes headless and reports frame count + resolution; JXS_SINK=display
opens a full-screen window (a real tile). Env/conf: JXSV_GRP, JXSV_PORT, ISLAND_PC_IP, JXS_W/JXS_H/
JXS_FPS/JXS_SAMPLING, JXS_WINW/JXS_WINH (display upscale target).
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
FPS  = os.environ.get("JXS_FPS", CFG.get("JXS_FPS") or "30")
SAMP = os.environ.get("JXS_SAMPLING", CFG.get("JXS_SAMPLING") or "YCbCr-4:2:2")
SINK = os.environ.get("JXS_SINK", "count")
# WSLg vGPU render ceiling by output size: 1080p/720p ~30 fps, 1440p ~24 fps, 4K ~11 fps.
# Cap the upscale at the physical panel (2560x1440) -- above that just wastes GPU and stutters.
WINW = str(min(int(os.environ.get("JXS_WINW", "2560")), 2560))
WINH = str(min(int(os.environ.get("JXS_WINH", "1440")), 1440))
fn, fd = (FPS.split("/") + ["1"])[:2]
FPS_N, FPS_D = int(fn), int(fd)
FRAME_DUR = Gst.SECOND * FPS_D // FPS_N

CAPS = (f"image/x-jxsc,width={W},height={H},sampling={SAMP},"
        f"framerate={FPS_N}/{FPS_D},interlace-mode=progressive,alignment=frame,depth=8")

_dec = {"n": 0, "wh": ""}
_start = time.time()


def main():
    Gst.init(None)
    if SINK == "display":
        tail = (
            "queue max-size-time=500000000 max-size-bytes=0 max-size-buffers=0 leaky=downstream "
            "! svtjpegxsdec ! videoconvert "
            "! textoverlay text='JPEG XS - ST 2110-22 (RFC 9134 video/jxsv)' valignment=top halignment=center "
            "  font-desc='Sans Bold 22' shaded-background=true "
            "! clockoverlay valignment=bottom halignment=right time-format='%H:%M:%S' font-desc='Sans Bold 18' shaded-background=true "
            "! videoconvert ! glupload ! glcolorscale "
            f"! video/x-raw(memory:GLMemory),width={WINW},height={WINH} ! glimagesink sync=true")
    else:
        tail = "svtjpegxsdec ! videoconvert ! video/x-raw ! appsink name=dec emit-signals=true sync=false max-buffers=4 drop=true"
    pipe = Gst.parse_launch(f"appsrc name=src is-live=true do-timestamp=false format=time ! {CAPS} ! {tail}")
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
    print(f"jxs-rtp-recv: video/jxsv <- {GRP}:{PORT}  ({SINK} mode, {W}x{H} {SAMP}"
          f"{', upscale '+WINW+'x'+WINH if SINK=='display' else ''})", flush=True)

    stop = threading.Event()
    _emit = {"n": 0, "base": None}

    def _push(data):
        """Push one complete codestream with a clock-aligned, fixed-cadence PTS (paces the display)."""
        if data[:2] != b"\xff\x10":              # not a whole codestream (SOC missing) -> drop
            return
        if _emit["base"] is None:
            clk = pipe.get_clock()
            rt = (clk.get_time() - pipe.get_base_time()) if clk else 0
            _emit["base"] = (rt if rt and rt > 0 else 0) + 150 * Gst.MSECOND   # small lead for the jitter queue
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, bytes(data))
        buf.pts = _emit["base"] + _emit["n"] * FRAME_DUR
        buf.duration = FRAME_DUR
        src.emit("push-buffer", buf)
        _emit["n"] += 1

    def rx():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(GRP), socket.inet_aton(LOCAL)))
        try: s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError: pass
        s.settimeout(1.0)
        cur = bytearray(); cur_ts = None; corrupt = False; expect = None; ssrc0 = None
        while not stop.is_set():
            try:
                pkt, _ = s.recvfrom(2048)
            except socket.timeout:
                continue
            if len(pkt) < 16:
                continue
            ssrc = struct.unpack("!I", pkt[8:12])[0]
            if ssrc0 is None:
                ssrc0 = ssrc                     # lock onto the first sender we hear
            elif ssrc != ssrc0:
                continue                         # ignore any other stream sharing the group
            marker = (pkt[1] >> 7) & 1
            seq = (pkt[2] << 8) | pkt[3]
            ts = struct.unpack("!I", pkt[4:8])[0]
            gap = (expect is not None and seq != expect)      # any lost/reordered packet
            expect = (seq + 1) & 0xFFFF
            if cur_ts is None:
                cur_ts = ts; corrupt = False
            if ts != cur_ts and cur:              # new frame began before the old marker -> old frame torn
                cur = bytearray(); cur_ts = ts; corrupt = gap
            if gap and cur:                       # loss inside the current frame -> mark it corrupt
                corrupt = True
            cur += pkt[16:]
            if marker:                            # end of frame
                if not corrupt:
                    _push(cur)
                cur = bytearray(); cur_ts = None; corrupt = False
        s.close()

    threading.Thread(target=rx, daemon=True).start()

    loop = GLib.MainLoop()
    def report():
        dt = max(0.001, time.time() - _start)
        if SINK == "display":
            print(f"jxs-rtp-recv: pushed {_emit['n']} frames  ({_emit['n']/dt:.1f} fps to the sink)", flush=True)
        else:
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
