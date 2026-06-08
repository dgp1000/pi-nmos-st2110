#!/usr/bin/env python3
"""Combined ST 2110 monitor: live 2110-20 video + PTP timecode on one page.

A GStreamer subprocess joins the multicast video flow, depays the RFC 4175 raw
video, JPEG-encodes it, and muxes multipart MJPEG to stdout; this server streams
that as multipart/x-mixed-replace. The page also shows a frame-accurate PTP
timecode (HH:MM:SS:FF) by relaying the Pi grandmaster's web-clock time via /time.
(WSLg won't display GStreamer windows, so we view in a browser — PC or iPad.)

Run in WSL:  python3 video-web.py
Open:        http://localhost:8095  (PC)   or  http://<pc-wifi-ip>:8095  (iPad)
"""
import http.server, socketserver, subprocess, urllib.request, json, time

PORT = 8095   # 8080/8081/8090/8091 are used by the NMOS containers
GROUP, VPORT, IFACE = "239.10.10.20", 5005, "eth1"
BOUNDARY = "st2110frame"
FPS = 25
PI_CLOCK = "http://10.10.10.1:8000/time"   # Pi grandmaster web clock, over the island
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

def pi_time():
    """Relay the Pi grandmaster's PTP time + status; fall back to local time."""
    try:
        with urllib.request.urlopen(PI_CLOCK, timeout=2) as r:
            return r.read()
    except Exception:
        return json.dumps({"epoch_ms": time.time() * 1000}).encode()

PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ST 2110 Monitor</title>
<style>
 html,body{{margin:0;height:100%;background:#000;color:#0f0;
   font-family:'Courier New',monospace;overflow:hidden;-webkit-user-select:none}}
 #wrap{{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%}}
 #tc{{font-size:min(12vw,15vh);font-weight:bold;line-height:1;white-space:nowrap;text-shadow:0 0 18px #0f0}}
 #vid{{margin-top:1.5vh;max-width:92vw;max-height:56vh;height:auto;border:2px solid #222;box-shadow:0 0 25px #000}}
 #info{{color:#777;font-size:min(2.2vw,2.6vh);margin-top:1.4vh;text-align:center;line-height:1.6}}
 .gm{{color:#fc0;font-weight:bold}}
</style></head>
<body><div id="wrap">
 <div id="tc">--:--:--:--</div>
 <img id="vid" src="/stream">
 <div id="info"></div>
</div>
<script>
const FPS={FPS};
let offset=0, ptp={{}};
async function sync(){{
  try{{
    const t0=Date.now();
    const r=await fetch('/time',{{cache:'no-store'}});
    const t1=Date.now();
    const d=await r.json();
    offset = d.epoch_ms - (t1 + (t1-t0)/2);
    ptp = d;
  }}catch(e){{}}
}}
const p=(n,l=2)=>String(n).padStart(l,'0');
function tick(){{
  const now=new Date(Date.now()+offset);
  const ff=Math.floor(now.getMilliseconds()/(1000/FPS));
  document.getElementById('tc').textContent=
    p(now.getHours())+':'+p(now.getMinutes())+':'+p(now.getSeconds())+':'+p(ff);
  const role = ptp.state==='MASTER' ? '<span class="gm">GRANDMASTER</span>' : (ptp.state||'—');
  document.getElementById('info').innerHTML=
    'ST 2110-20 &middot; 239.10.10.20:5005 &middot; RFC 4175 &rarr; MJPEG<br>'+
    'PTP domain 0 &middot; '+role+' &middot; '+(ptp.gm||'—')+' &middot; offset '+(ptp.offset||'—')+' ns &middot; '+FPS+'fps TC';
  requestAnimationFrame(tick);
}}
sync(); setInterval(sync,3000); tick();
</script></body></html>"""

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
        elif self.path.startswith("/time"):
            body = pi_time()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
    print(f"ST 2110 monitor (video + PTP timecode) on http://localhost:{PORT}")
    print("  Ctrl+C to stop")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
