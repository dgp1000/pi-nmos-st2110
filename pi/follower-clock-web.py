#!/usr/bin/env python3
"""Web PTP FOLLOWER readout — shows this Pi locking to the grandmaster: the live
offset-from-master converging toward zero, the servo state climbing
LISTENING -> UNCALIBRATED -> SLAVE, and the grandmaster's clock identity (proving
it is disciplined by the Pi 5, not its own free-running clock).

Run on the FOLLOWER Pi (with sudo, so pmc can reach the ptp4l socket) while
follow-all.sh's ptp4l is active:

    sudo python3 follower-clock-web.py

Then open  http://<this-pi>:8000  on any browser on the network.
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
                info["offset"] = int(ln.split()[1])
            elif ln.startswith("gmIdentity"):
                info["gm"] = ln.split()[1]
            elif ln.startswith("gmPresent"):
                info["gm_present"] = ln.split()[1]
        cur = subprocess.run(["pmc", "-u", "-b", "0", "GET CURRENT_DATA_SET"],
                             capture_output=True, text=True, timeout=2).stdout
        for ln in cur.splitlines():
            ln = ln.strip()
            if ln.startswith("meanPathDelay"):
                info["path_delay"] = float(ln.split()[1])
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
<title>PTP Follower</title>
<style>
 html,body{margin:0;height:100%;background:#000;color:#0f0;
   font-family:'Courier New',monospace;overflow:hidden;-webkit-user-select:none}
 #wrap{display:flex;flex-direction:column;justify-content:center;align-items:center;height:100%}
 #title{color:#0cf;letter-spacing:.3em;font-size:min(3vw,4.5vh);margin-bottom:1vh}
 #state{font-size:min(6vw,8vh);font-weight:bold;letter-spacing:.15em;margin-bottom:1vh}
 #offlabel{color:#888;font-size:min(2.2vw,3vh);letter-spacing:.2em}
 #offset{font-size:min(15vw,24vh);font-weight:bold;line-height:1;white-space:nowrap;text-shadow:0 0 25px currentColor}
 #offunit{font-size:min(4vw,6vh);color:#888}
 #spark{margin-top:2vh}
 #ptp{color:#777;font-size:min(2.3vw,3.2vh);margin-top:3vh;text-align:center;line-height:1.7}
 .gm{color:#fc0;font-weight:bold}
 .locked{color:#0f0}.warm{color:#fc0}.cold{color:#f44}
</style></head>
<body><div id="wrap">
 <div id="title">PTP FOLLOWER &nbsp;&rarr;&nbsp; GRANDMASTER</div>
 <div id="state" class="cold">STARTING</div>
 <div id="offlabel">OFFSET FROM MASTER</div>
 <div><span id="offset">--</span><span id="offunit"> ns</span></div>
 <canvas id="spark" width="640" height="120"></canvas>
 <div id="ptp"></div>
</div>
<script>
const hist=[];                       // recent |offset| ns, for the convergence sparkline
const cvs=document.getElementById('spark'), cx=cvs.getContext('2d');
function draw(){
  const W=cvs.width,H=cvs.height; cx.clearRect(0,0,W,H);
  cx.strokeStyle='#022'; cx.lineWidth=1;
  for(let i=0;i<=4;i++){const y=H*i/4; cx.beginPath();cx.moveTo(0,y);cx.lineTo(W,y);cx.stroke();}
  if(hist.length<2) return;
  // log scale so both ms-scale (start) and us-scale (locked) are visible at once
  const lg=v=>Math.log10(Math.max(1,Math.abs(v)));
  const top=Math.max(3,...hist.map(lg));               // >= 1000ns headroom
  cx.strokeStyle='#0f0'; cx.lineWidth=2; cx.beginPath();
  hist.forEach((v,i)=>{
    const x=W*i/(hist.length-1), y=H-(lg(v)/top)*H;
    i?cx.lineTo(x,y):cx.moveTo(x,y);
  });
  cx.stroke();
}
async function poll(){
  try{
    const r=await fetch('/status',{cache:'no-store'});
    const d=await r.json();
    const st=d.state||'—';
    const off=(d.offset===undefined)?null:d.offset;
    const se=document.getElementById('state');
    const oe=document.getElementById('offset');
    let cls='cold';
    if(st==='SLAVE'){ cls=(off!==null&&Math.abs(off)<100000)?'locked':'warm'; }
    else if(st==='UNCALIBRATED'){ cls='warm'; }
    se.textContent = st==='SLAVE' ? (cls==='locked'?'LOCKED':'SLAVE') : st;
    se.className=cls;
    oe.textContent = off===null ? '--' : off.toLocaleString();
    oe.parentElement.className=''; oe.style.color=({locked:'#0f0',warm:'#fc0',cold:'#f44'})[cls];
    if(off!==null){ hist.push(off); if(hist.length>240) hist.shift(); draw(); }
    const pd=d.path_delay!==undefined?Math.round(d.path_delay)+' ns':'—';
    document.getElementById('ptp').innerHTML=
      'PTP domain 0 &nbsp; E2E &nbsp; software timestamping'+
      '<br>grandmaster: <span class="gm">'+(d.gm||'—')+'</span>'+
      '<br>mean path delay: '+pd;
  }catch(e){}
}
poll(); setInterval(poll,1000);
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
        if self.path.startswith("/status"):
            self._send(json.dumps(ptp_status()).encode(), "application/json")
        else:
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(("0.0.0.0", PORT), Handler) as httpd:
    print(f"PTP follower readout serving on port {PORT}")
    print(f"  open on any browser on the network:  http://<this-pi>:{PORT}")
    print("  Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
