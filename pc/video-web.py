#!/usr/bin/env python3
"""Combined ST 2110 monitor: live 2110-20 video + PTP timecode, with a codec A/B.

A GStreamer subprocess joins the multicast video flow, depays the RFC 4175 raw
video, encodes each frame (JPEG at a chosen quality, or lossless PNG), and muxes
multipart frames to stdout; this server streams that as multipart/x-mixed-replace.
The page shows a frame-accurate PTP timecode (relayed from the Pi grandmaster) and
buttons to switch the encoder live -- so you can see lossy JPEG mangle the noise
block while lossless PNG keeps the uncompressed essence pixel-perfect.

Run in WSL:  python3 video-web.py    Open: http://localhost:8095 / http://<wifi-ip>:8095
"""
import http.server, socketserver, subprocess, urllib.request, json, time
from urllib.parse import urlparse, parse_qs

PORT = 8095
GROUP, VPORT, IFACE = "239.10.10.20", 5005, "eth1"
BOUNDARY = "st2110frame"
FPS = 60000 / 1001   # 59.94 fps (US / NTSC)
PI_CLOCK = "http://10.10.10.1:8000/time"
CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,"
        "encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,"
        "width=(string)320,height=(string)240,payload=(int)96")

def gst_cmd(fmt, q):
    base = ["gst-launch-1.0", "-q",
            "udpsrc", f"address={GROUP}", f"port={VPORT}",
            f"multicast-iface={IFACE}", "auto-multicast=true", f"caps={CAPS}",
            "!", "rtpjitterbuffer", "latency=100",
            "!", "rtpvrawdepay", "!", "videoconvert", "!", "videoscale",
            "!", "video/x-raw,width=640,height=480"]
    if fmt == "png":
        enc = ["!", "pngenc", "compression-level=1"]          # lossless
    else:
        enc = ["!", "jpegenc", f"quality={q}"]                # lossy
    return base + enc + ["!", "multipartmux", f"boundary={BOUNDARY}", "!", "fdsink", "fd=1"]

def pi_time():
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
 #tc{{font-size:min(10vw,13vh);font-weight:bold;line-height:1;white-space:nowrap;text-shadow:0 0 18px #0f0}}
 #vid{{margin-top:1vh;max-width:90vw;max-height:52vh;height:auto;border:2px solid #222;box-shadow:0 0 25px #000}}
 #ctrl{{margin-top:1.3vh}}
 button{{font-family:inherit;font-size:min(2.4vw,2.8vh);margin:0 .4vw;padding:.5em 1em;
   background:#111;color:#0f0;border:1px solid #0a0;border-radius:6px}}
 button.on{{background:#0a0;color:#000;font-weight:bold}}
 #info{{color:#777;font-size:min(2vw,2.4vh);margin-top:1.2vh;text-align:center;line-height:1.6}}
 .gm{{color:#fc0;font-weight:bold}}
</style></head>
<body><div id="wrap">
 <div id="tc">--:--:--:--</div>
 <img id="vid" src="/stream?fmt=jpeg&q=85">
 <div id="ctrl">
  <button id="b1" class="on" onclick="setSrc('jpeg',85,this)">JPEG q85 (lossy)</button>
  <button id="b2" onclick="setSrc('jpeg',100,this)">JPEG q100</button>
  <button id="b3" onclick="setSrc('png',0,this)">PNG (lossless)</button>
 </div>
 <div id="info"></div>
</div>
<script>
const FPS={FPS};
let offset=0, ptp={{}};
function setSrc(fmt,q,btn){{
  const url = fmt==='png' ? '/stream?fmt=png' : '/stream?fmt=jpeg&q='+q;
  document.getElementById('vid').src = url + '&t=' + Date.now();
  document.querySelectorAll('#ctrl button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}}
async function sync(){{
  try{{const t0=Date.now();const r=await fetch('/time',{{cache:'no-store'}});const t1=Date.now();
       const d=await r.json();offset=d.epoch_ms-(t1+(t1-t0)/2);ptp=d;}}catch(e){{}}
}}
const p=(n,l=2)=>String(n).padStart(l,'0');
function tick(){{
  const now=new Date(Date.now()+offset);
  const ff=Math.floor(now.getMilliseconds()/(1000/FPS));
  document.getElementById('tc').textContent=
    p(now.getHours())+':'+p(now.getMinutes())+':'+p(now.getSeconds())+':'+p(ff);
  const role = ptp.state==='MASTER' ? '<span class="gm">GRANDMASTER</span>' : (ptp.state||'—');
  document.getElementById('info').innerHTML=
    'ST 2110-20 &middot; RFC 4175 uncompressed &middot; 59.94 fps (US) &middot; encoded for the browser<br>'+
    'PTP domain 0 &middot; '+role+' &middot; '+(ptp.gm||'—')+' &middot; offset '+(ptp.offset||'—')+' ns';
  requestAnimationFrame(tick);
}}
sync(); setInterval(sync,3000); tick();
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/stream":
            qs = parse_qs(parsed.query)
            fmt = qs.get("fmt", ["jpeg"])[0]
            try:
                q = int(qs.get("q", ["85"])[0])
            except ValueError:
                q = 85
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            proc = subprocess.Popen(gst_cmd(fmt, q), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
        elif parsed.path == "/time":
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
    print(f"ST 2110 monitor (video + timecode + codec A/B) on http://localhost:{PORT}")
    print("  Ctrl+C to stop")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
