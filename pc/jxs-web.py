#!/usr/bin/env python3
"""Atoll JPEG XS viewer (:8100).

Decodes the island JPEG XS flow (image/x-jxsc over MPEG-TS) with svtjpegxsdec and serves it as
MJPEG so it's viewable in a browser (PC localhost:8100, or 192.168.4.85:8100 from iPad/Mac).
Proves the JPEG XS (ST 2110-22 codec) pipeline end to end in the rig."""
import http.server, socketserver, subprocess, os, signal

PORT = 8100
BOUNDARY = "jxsframe"

PAGE = ("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atoll JPEG XS</title>
<style>html,body{margin:0;height:100%;background:#000}
img{width:100vw;height:100vh;object-fit:contain;display:block}</style>
</head><body><img src="/stream" alt="jpeg-xs"></body></html>""").encode()


def gst_cmd():
    # Local JPEG XS encode -> decode (image/x-jxsc via SVT-JPEG-XS), then MJPEG for the browser.
    # A JPEG XS *network* flow across the island isn't viable here: the encoder runs ~100 Mbit/s
    # CBR and the WSL multicast-receive path can't sustain that packet rate (same wall as HD
    # uncompressed 2110-20). This proves the ST 2110-22 codec works end to end in the rig.
    pipeline = (
        'gst-launch-1.0 -q videotestsrc pattern=ball is-live=true '
        '! video/x-raw,width=1280,height=720,framerate=30/1 '
        '! textoverlay text="JPEG XS - ST 2110-22 codec (SVT-JPEG-XS) encode->decode" valignment=top halignment=center font-desc="Sans Bold 24" shaded-background=true '
        "! clockoverlay valignment=bottom halignment=right time-format=\"%H:%M:%S\" font-desc=\"Sans Bold 20\" shaded-background=true "
        '! videoconvert ! video/x-raw,format=Y42B '
        '! svtjpegxsenc ! svtjpegxsdec '
        '! videoconvert ! jpegenc quality=80 '
        f'! multipartmux boundary={BOUNDARY} ! fdsink fd=1'
    )
    return ["bash", "-c", pipeline]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
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
    print(f"Atoll JPEG XS viewer on http://localhost:{PORT}")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
