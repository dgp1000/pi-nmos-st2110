#!/usr/bin/env python3
"""Atoll ST 2110-22 JPEG XS RECEIVER (RFC 9134 `video/jxsv`, codestream mode).

The companion to `jxs-rtp-send.py`. GStreamer has no RFC 9134 depayloader, so we reassemble in
Python: join the multicast group, collect each RTP packet's codestream fragment, and on the RTP
marker bit (last packet of the frame) push the whole `image/x-jxsc` codestream into an appsrc feeding
`svtjpegxsdec`. Proves the hand-built 2110-22 stream is standards-decodable end to end.

A frame is pushed only if it is whole: it must start with the JPEG XS SOC (0xFF10), come from the
locked sender SSRC, and have no RTP sequence gap between its packets -- a partial/torn frame is
dropped so the decoder never sees corrupt data (which otherwise shows as ghost/flashing frames).
Frames are paced by a fixed 30 fps cadence (clock-aligned, ~150 ms lead) so bursty arrival plays
evenly.

JXS_SINK=count (default) decodes headless and reports frame count + resolution. JXS_SINK=display
shows it full-screen: gtkglsink (GL, pre-scaled) hosted in a GTK window put fullscreen via GTK's own
fullscreen_on_monitor (the Wayland compositor does the scale -- cheap and smooth on WSLg, where a
glimagesink 4K upscale only manages ~11 fps and a Win32 resize is ignored). Env/conf: JXSV_GRP,
JXSV_PORT, ISLAND_PC_IP, JXS_W/JXS_H/JXS_FPS/JXS_SAMPLING, JXS_MONITOR (fullscreen monitor index).
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
MON  = int(os.environ.get("JXS_MONITOR", "1"))       # Gdk monitor index for fullscreen (1 = second)
fn, fd = (FPS.split("/") + ["1"])[:2]
FPS_N, FPS_D = int(fn), int(fd)
FRAME_DUR = Gst.SECOND * FPS_D // FPS_N

CAPS = (f"image/x-jxsc,width={W},height={H},sampling={SAMP},"
        f"framerate={FPS_N}/{FPS_D},interlace-mode=progressive,alignment=frame,depth=8")

_dec = {"n": 0, "wh": ""}
_start = time.time()


def start_rx(pipe, src, stop):
    """Reassemble RFC 9134 codestream-mode RTP and push whole, in-order frames to `src`, paced."""
    _emit = {"n": 0, "base": None}

    def _push(data):
        if data[:2] != b"\xff\x10":              # not a whole codestream (SOC missing) -> drop
            return
        if _emit["base"] is None:
            clk = pipe.get_clock()
            rt = (clk.get_time() - pipe.get_base_time()) if clk else 0
            _emit["base"] = (rt if rt and rt > 0 else 0) + 150 * Gst.MSECOND   # jitter-buffer lead
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
            gap = (expect is not None and seq != expect)
            expect = (seq + 1) & 0xFFFF
            if cur_ts is None:
                cur_ts = ts; corrupt = False
            if ts != cur_ts and cur:             # new frame began before old marker -> old frame torn
                cur = bytearray(); cur_ts = ts; corrupt = gap
            if gap and cur:
                corrupt = True
            cur += pkt[16:]
            if marker:
                if not corrupt:
                    _push(cur)
                cur = bytearray(); cur_ts = None; corrupt = False
        s.close()

    threading.Thread(target=rx, daemon=True).start()


OVERLAYS = (
    "textoverlay text='JPEG XS - ST 2110-22 (RFC 9134 video/jxsv)' valignment=top halignment=center "
    "font-desc='Sans Bold 22' shaded-background=true "
    "! clockoverlay valignment=bottom halignment=right time-format='%H:%M:%S' font-desc='Sans Bold 18' shaded-background=true")


def run_display():
    """gtkglsink in a GTK window, fullscreen on the target monitor -- smooth (GL) + fills (compositor scale)."""
    gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, Gdk
    pipe = Gst.parse_launch(
        f"appsrc name=src is-live=true do-timestamp=false format=time ! {CAPS} "
        "! queue max-size-time=500000000 max-size-bytes=0 max-size-buffers=0 leaky=downstream "
        f"! svtjpegxsdec ! videoconvert ! {OVERLAYS} "
        "! videoconvert ! glupload ! glcolorscale ! video/x-raw(memory:GLMemory),width=2560,height=1440 ! gtkglsink name=glsink")
    src = pipe.get_by_name("src")
    src.set_property("caps", Gst.Caps.from_string(CAPS)); src.set_property("format", Gst.Format.TIME)
    glsink = pipe.get_by_name("glsink"); glsink.set_property("sync", True)
    widget = glsink.get_property("widget")

    win = Gtk.Window(); win.set_decorated(False); win.connect("destroy", Gtk.main_quit)
    win.set_app_paintable(True)
    win.add(widget); win.show_all()
    disp = Gdk.Display.get_default(); n = disp.get_n_monitors() if disp else 1
    target = MON if 0 <= MON < n else (n - 1)
    try:
        win.fullscreen_on_monitor(win.get_screen(), target)
    except Exception:
        win.fullscreen()
    print(f"jxs-rtp-recv: GTK fullscreen on monitor {target} of {n}  ({W}x{H} {SAMP} <- {GRP}:{PORT})", flush=True)

    stop = threading.Event()
    pipe.set_state(Gst.State.PLAYING)
    start_rx(pipe, src, stop)

    def _quit(*_):
        stop.set(); Gtk.main_quit(); return False
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _quit)
    try:
        Gtk.main()
    finally:
        stop.set(); pipe.set_state(Gst.State.NULL)


def run_count():
    """Headless decode + report -- proves the stream is standards-decodable."""
    pipe = Gst.parse_launch(
        f"appsrc name=src is-live=true do-timestamp=false format=time ! {CAPS} "
        "! svtjpegxsdec ! videoconvert ! video/x-raw ! appsink name=dec emit-signals=true sync=false max-buffers=4 drop=true")
    src = pipe.get_by_name("src")
    src.set_property("caps", Gst.Caps.from_string(CAPS)); src.set_property("format", Gst.Format.TIME)

    def on_dec(sink):
        s = sink.emit("pull-sample")
        if s:
            _dec["n"] += 1
            st = s.get_caps().get_structure(0)
            _dec["wh"] = f"{st.get_value('width')}x{st.get_value('height')} {st.get_value('format')}"
        return Gst.FlowReturn.OK
    pipe.get_by_name("dec").connect("new-sample", on_dec)

    stop = threading.Event()
    pipe.set_state(Gst.State.PLAYING)
    print(f"jxs-rtp-recv: count mode, {W}x{H} {SAMP} <- {GRP}:{PORT}", flush=True)
    start_rx(pipe, src, stop)

    loop = GLib.MainLoop()
    def report():
        dt = max(0.001, time.time() - _start)
        print(f"jxs-rtp-recv: decoded {_dec['n']} frames  ({_dec['n']/dt:.1f} fps)  {_dec['wh']}", flush=True)
        return True
    GLib.timeout_add_seconds(2, report)
    def _stop(*_):
        stop.set(); pipe.set_state(Gst.State.NULL); loop.quit()
    signal.signal(signal.SIGTERM, _stop); signal.signal(signal.SIGINT, _stop)
    try:
        loop.run()
    finally:
        stop.set(); pipe.set_state(Gst.State.NULL)


def main():
    Gst.init(None)
    run_display() if SINK == "display" else run_count()


if __name__ == "__main__":
    main()
