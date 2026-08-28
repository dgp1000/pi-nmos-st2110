#!/usr/bin/env python3
"""Atoll multiview as a browser page (MJPEG over HTTP).

The 2x2 compositor (multiview-mjpeg.sh) writes multipart JPEG frames to stdout; this
server streams them as multipart/x-mixed-replace. Needed because WSLg's native window
path is broken under mirrored networking, so the multiview can't open a local window.
View at http://<pc-wifi-ip>:8099  (works from the PC browser, the iPad, or the Mac).
"""
import http.server, socketserver, subprocess, os, signal

PORT = 8099
BOUNDARY = "atollframe"
HERE = os.path.dirname(os.path.abspath(__file__))

PAGE = b"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atoll Multiview</title>
<style>html,body{margin:0;height:100%;background:#000}
img{width:100vw;height:100vh;object-fit:contain;display:block}</style>
</head><body><img src="/stream" alt="multiview"></body></html>"""

def gst_cmd():
    return ["bash", os.path.join(HERE, "multiview-mjpeg.sh"), BOUNDARY]

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            proc = subprocess.Popen(gst_cmd(), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
            try:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(("0.0.0.0", PORT), Handler) as s:
    print(f"Atoll multiview MJPEG on http://localhost:{PORT}  (open /stream directly for just the image)")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
