#!/usr/bin/env python3
"""Web PTP master clock — serves a full-screen studio clock that shows the
grandmaster's PTP-domain time, viewable from any browser on the network (iPad,
phone, laptop). The page periodically syncs to this server's time (correcting for
round-trip) so it displays the Pi's PTP time, not the client's own clock.

Run on the Pi (the grandmaster). For the live PTP status line, run with sudo
(pmc needs the ptp4l socket) while ptp4l is active:

    sudo python3 master-clock-web.py

Then on the iPad (same WiFi): open  http://pi5-nmos.local:8000
"""
import http.server, json, socketserver, subprocess, time

PORT = 8000

def ptp_status():
    info = {}
    try:
        ts = subprocess.run(["pmc", "-u", "-b", "0", "GET TIME_STATUS_NP"],
                            capture_output=True, text=True, timeout=2).stdout
        for ln in ts.splitlines():
            ln = ln.strip()
            if ln.startswith("master_offset"):
                info["offset"] = ln.split()[1]
            elif ln.startswith("gmIdentity"):
                info["gm"] = ln.split()[1]
        ps = subprocess.run(["pmc", "-u", "-b", "0", "GET PORT_DATA_SET"],
                            capture_output=True, text=True, timeout=2).stdout
        for ln in ps.splitlines():
            ln = ln.strip()
            if ln.startswith("portState"):
                info["state"] = ln.split()[1]
    except Exception:
        pass
    return info

PAGE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>PTP Master Clock</title>
<style>
 html,body{margin:0;height:100%;background:#000;color:#0f0;
   font-family:'Courier New',monospace;overflow:hidden;-webkit-user-select:none}
 #wrap{display:flex;flex-direction:column;justify-content:center;align-items:center;height:100%}
 #title{color:#0cf;letter-spacing:.3em;font-size:min(3vw,4.5vh);margin-bottom:2vh}
 #time{font-size:min(16vw,26vh);font-weight:bold;line-height:1;white-space:nowrap;text-shadow:0 0 25px #0f0}
 #ms{font-size:min(5vw,7vh)}
 #date{color:#888;font-size:min(3.2vw,4.5vh);margin-top:2vh}
 #ptp{color:#777;font-size:min(2.3vw,3.2vh);margin-top:4vh;text-align:center;line-height:1.7}
 .gm{color:#fc0;font-weight:bold}
</style></head>
<body><div id="wrap">
 <div id="title">PTP MASTER CLOCK</div>
 <div id="time">--:--:--</div>
 <div id="ms">.000</div>
 <div id="date"></div>
 <div id="ptp"></div>
</div>
<script>
let offset=0, ptp={};
async function sync(){
  try{
    const t0=Date.now();
    const r=await fetch('/time',{cache:'no-store'});
    const t1=Date.now();
    const d=await r.json();
    offset = d.epoch_ms - (t1 + (t1-t0)/2);   // server time minus local mid-RTT
    ptp = d;
  }catch(e){}
}
const pad=(n,l=2)=>String(n).padStart(l,'0');
const months=['January','February','March','April','May','June','July','August','September','October','November','December'];
const days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function tick(){
  const now=new Date(Date.now()+offset);
  document.getElementById('time').textContent=pad(now.getHours())+':'+pad(now.getMinutes())+':'+pad(now.getSeconds());
  document.getElementById('ms').textContent='.'+pad(now.getMilliseconds(),3);
  document.getElementById('date').textContent=days[now.getDay()]+' '+pad(now.getDate())+' '+months[now.getMonth()]+' '+now.getFullYear();
  const role = ptp.state==='MASTER' ? '<span class="gm">GRANDMASTER</span>' : (ptp.state||'—');
  document.getElementById('ptp').innerHTML='PTP domain 0 &nbsp; role: '+role+
     '<br>grandmaster: '+(ptp.gm||'—')+' &nbsp; offset: '+(ptp.offset||'—')+' ns';
  requestAnimationFrame(tick);
}
sync(); setInterval(sync,3000); tick();
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/time"):
            self._send(json.dumps({"epoch_ms": time.time() * 1000, **ptp_status()}).encode(),
                       "application/json")
        else:
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(("0.0.0.0", PORT), Handler) as httpd:
    print(f"PTP web clock serving on port {PORT}")
    print(f"  open on the iPad (same WiFi):  http://pi5-nmos.local:{PORT}")
    print("  Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
