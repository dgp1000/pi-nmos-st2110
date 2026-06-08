#!/usr/bin/env python3
"""View the received ST 2110-20 video in a browser (WSLg won't display GStreamer
windows, so we transcode to MJPEG and serve it over HTTP).

A GStreamer subprocess joins the multicast video flow, depays the RFC 4175 raw
video, JPEG-encodes it, and muxes it as multipart MJPEG to stdout. This server
streams that to the browser as multipart/x-mixed-replace (the classic IP-camera
trick), so any browser shows it as live video.

Run in WSL:  python3 video-web.py
Then open (Windows browser):  http://localhost:8090
"""
import http.server, socketserver, subprocess

PORT = 8095   # 8080/8081/8090/8091 are used by the NMOS containers
GROUP, VPORT, IFACE = "239.10.10.20", 5005, "eth1"
BOUNDARY = "st2110frame"
CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,"
        "encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,"
        "width=(string)320,height=(string)240,payload=(int)96")

def gst_cmd():
    return ["gst-launch-1.0", "-q",
            "udpsrc", f"address={GROUP}", f"port={VPORT}",
            f"multicast-iface={IFACE}", "auto-multicast=true", f"caps={CAPS}",
            "!", "rtpjitterbuffer", "latency=100",
            "!", "rtpvrawdepay", "!", "videoconvert", "!", "videoscale",
            "!", "video/x-raw,width=640,height=480",
            "!", "jpegenc", "quality=85",
            "!", "multipartmux", f"boundary={BOUNDARY}",
            "!", "fdsink", "fd=1"]

PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ST 2110-20 Video</title>
<style>
 body{{margin:0;background:#0b0b0b;color:#0f0;font-family:'Courier New',monospace;text-align:center}}
 h1{{font-size:3.2vw;color:#0cf;letter-spacing:.25em;padding:1.2rem 0 .6rem}}
 img{{max-width:96vw;max-height:80vh;height:auto;border:2px solid #222;box-shadow:0 0 30px #000}}
 .sub{{color:#777;font-size:2.4vw;padding-bottom:1rem}}
</style></head>
<body>
 <h1>ST 2110-20 &nbsp; LIVE</h1>
 <img src="/stream">
 <div class="sub">239.10.10.20:5005 &middot; uncompressed RFC 4175 &middot; transcoded to MJPEG for viewing</div>
</body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            proc = subprocess.Popen(gst_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(("0.0.0.0", PORT), Handler) as s:
    print(f"ST 2110-20 video -> MJPEG serving on http://localhost:{PORT}")
    print(f"  Windows browser: http://localhost:{PORT}")
    print("  Ctrl+C to stop")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
