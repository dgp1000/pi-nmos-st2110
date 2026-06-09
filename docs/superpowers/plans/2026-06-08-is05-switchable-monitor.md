# IS-05 Source-Switchable Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An iPad-viewable web monitor (`:8096`) whose video source switches via real IS-05 takes between the Pi raw ST 2110-20 flow (receiver `v0`) and the PC JPEG-XS island flow (receiver `m0`), with PTP timecode.

**Architecture:** One Python HTTP server (`pc/monitor-web.py`) reusing `video-web.py`'s MJPEG/PTP page and `take.py`'s IS-05 PATCH pattern. `/take?src=` enables one NMOS receiver and disables the other; `/stream` follows the active receiver and transcodes its flow to MJPEG (GStreamer for raw, FFmpeg→GStreamer for JPEG-XS). A firewall helper opens TCP 8096 for the iPad.

**Tech Stack:** Python 3 stdlib http.server, GStreamer 1.28, FFmpeg (`libsvtjpegxs`), nmos-cpp IS-05, WSL2.

**Run context:** Inside a local WSL terminal as `dgper`. Repo in WSL: `/mnt/c/Users/dgper/pi-nmos-st2110`. NMOS containers must be up (`restore.ps1`). IS-05 take on a video receiver was verified (HTTP 200, `master_enable` flips). Git commits from the repo root.

---

## Task 1: The switchable monitor server

**Files:**
- Create: `pc/monitor-web.py`

- [ ] **Step 1: Write the server**

Create `pc/monitor-web.py`:

```python
#!/usr/bin/env python3
"""IS-05 source-switchable ST 2110 monitor (video + PTP timecode), iPad-friendly.

Buttons issue real IS-05 takes to switch the active video receiver between the
Pi raw ST 2110-20 flow (NMOS receiver v0) and the PC JPEG-XS island flow (NMOS
receiver m0). /stream follows whichever receiver is master_enable=TRUE and
transcodes it to MJPEG for the browser. PTP timecode is relayed from the Pi
grandmaster, exactly as video-web.py.

Run in WSL:  python3 monitor-web.py    Open from iPad: http://<pc-wifi-ip>:8096
"""
import http.server, socketserver, subprocess, urllib.request, json, time, os, signal
from urllib.parse import urlparse, parse_qs

PORT = 8096
IFACE = "eth1"
BOUNDARY = "st2110frame"
FPS = 60000 / 1001
PI_CLOCK = "http://10.10.10.1:8000/time"
NODE = "http://localhost:8090/x-nmos/node/v1.3"
CONN = "http://localhost:8090/x-nmos/connection/v1.1"

SOURCES = {
    "raw": {"label": "easy-nmos-node/receiver/v0", "group": "239.10.10.20", "port": 5005},
    "jxs": {"label": "easy-nmos-node/receiver/m0", "group": "239.10.10.22", "port": 5008},
}
DEFAULT_SRC = "jxs"
RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,"
            "encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,"
            "width=(string)320,height=(string)240,payload=(int)96")

def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)

def receiver_id(label):
    for rx in http_json(f"{NODE}/receivers"):
        if rx.get("label") == label:
            return rx["id"]
    raise RuntimeError(f"receiver {label!r} not found")

def set_enable(label, on):
    rid = receiver_id(label)
    body = {"master_enable": bool(on), "activation": {"mode": "activate_immediate"}}
    req = urllib.request.Request(f"{CONN}/single/receivers/{rid}/staged",
                                 data=json.dumps(body).encode(), method="PATCH",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status

def is_enabled(label):
    rid = receiver_id(label)
    return bool(http_json(f"{CONN}/single/receivers/{rid}/active").get("master_enable"))

def take(src):
    for key, s in SOURCES.items():
        set_enable(s["label"], key == src)

def active_src():
    for key, s in SOURCES.items():
        try:
            if is_enabled(s["label"]):
                return key
        except Exception:
            pass
    return DEFAULT_SRC

def stream_cmd(src):
    s = SOURCES[src]
    tail = (f"videoscale ! video/x-raw,width=640,height=480 ! "
            f"jpegenc quality=85 ! multipartmux boundary={BOUNDARY} ! fdsink fd=1")
    if src == "raw":
        return ("gst-launch-1.0 -q "
                f"udpsrc address={s['group']} port={s['port']} multicast-iface={IFACE} "
                f"auto-multicast=true caps='{RAW_CAPS}' ! rtpjitterbuffer latency=100 ! "
                f"rtpvrawdepay ! videoconvert ! {tail}")
    url = (f"udp://{s['group']}:{s['port']}?localaddr=10.10.10.2"
           f"&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864")
    return (f"ffmpeg -hide_banner -loglevel error -fflags nobuffer -flags low_delay "
            f"-i '{url}' -f rawvideo -pix_fmt yuv422p - | "
            f"gst-launch-1.0 -q fdsrc ! "
            f"rawvideoparse format=Y42B width=1920 height=1080 framerate=60000/1001 ! "
            f"videoconvert ! {tail}")

def pi_time():
    try:
        with urllib.request.urlopen(PI_CLOCK, timeout=2) as r:
            return r.read()
    except Exception:
        return json.dumps({"epoch_ms": time.time() * 1000}).encode()

PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ST 2110 IS-05 Monitor</title>
<style>
 html,body{{margin:0;height:100%;background:#000;color:#0f0;
   font-family:'Courier New',monospace;overflow:hidden;-webkit-user-select:none}}
 #wrap{{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%}}
 #tc{{font-size:min(10vw,13vh);font-weight:bold;line-height:1;white-space:nowrap;text-shadow:0 0 18px #0f0}}
 #vid{{margin-top:1vh;max-width:90vw;max-height:52vh;height:auto;border:2px solid #222;box-shadow:0 0 25px #000}}
 #ctrl{{margin-top:1.3vh}}
 button{{font-family:inherit;font-size:min(2.6vw,3vh);margin:0 .5vw;padding:.5em 1em;
   background:#111;color:#0f0;border:1px solid #0a0;border-radius:6px}}
 button.on{{background:#0a0;color:#000;font-weight:bold}}
 #info{{color:#777;font-size:min(2vw,2.4vh);margin-top:1.2vh;text-align:center;line-height:1.6}}
 .gm{{color:#fc0;font-weight:bold}}
</style></head>
<body><div id="wrap">
 <div id="tc">--:--:--:--</div>
 <img id="vid" src="/stream">
 <div id="ctrl">
  <button id="bjxs" class="on" onclick="take('jxs',this)">PC JPEG-XS</button>
  <button id="braw" onclick="take('raw',this)">Pi raw 2110-20</button>
 </div>
 <div id="info"></div>
</div>
<script>
const FPS={FPS};
let offset=0, ptp={{}};
async function take(src,btn){{
  try{{await fetch('/take?src='+src,{{cache:'no-store'}});}}catch(e){{}}
  document.getElementById('vid').src='/stream?t='+Date.now();
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
    'IS-05 switchable source &middot; PTP domain 0 &middot; '+role+' &middot; '+(ptp.gm||'—')+
    ' &middot; offset '+(ptp.offset||'—')+' ns';
  requestAnimationFrame(tick);
}}
sync(); setInterval(sync,3000); tick();
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/stream":
            src = active_src()
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            proc = subprocess.Popen(stream_cmd(src), shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk: break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception: pass
        elif parsed.path == "/take":
            qs = parse_qs(parsed.query)
            src = qs.get("src", [DEFAULT_SRC])[0]
            if src not in SOURCES: src = DEFAULT_SRC
            try:
                take(src); body = json.dumps({"active": src}).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/time":
            body = pi_time()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
    print(f"ST 2110 IS-05 monitor on http://localhost:{PORT}  (sources: raw=v0, jxs=m0)")
    print("  Ctrl+C to stop")
    try: s.serve_forever()
    except KeyboardInterrupt: pass
```

- [ ] **Step 2: Start the server and verify the page + PTP proxy**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 2; echo "--- page ---"; curl -s -m4 http://localhost:8096/ | grep -o "ST 2110 IS-05 Monitor"; echo "--- time ---"; curl -s -m4 http://localhost:8096/time | head -c 120; echo'
```
Expected: prints `ST 2110 IS-05 Monitor` and a JSON time blob (e.g. `{"epoch_ms": ...}` or the Pi clock payload).

- [ ] **Step 3: Verify /take issues real IS-05 takes (flips v0/m0)**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'echo "take jxs:"; curl -s -m5 "http://localhost:8096/take?src=jxs"; echo; echo "take raw:"; curl -s -m5 "http://localhost:8096/take?src=raw"; echo'
```
Expected: `{"active": "jxs"}` then `{"active": "raw"}` (HTTP 200, no error key).

- [ ] **Step 4: Verify /stream serves MJPEG from the JPEG-XS source**

Run (start a JPEG-XS sender, select jxs, check the stream headers + that bytes flow):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send-file.sh ~/jxs-media/bbb-full.mov >/tmp/s.log 2>&1 & sleep 3; curl -s -m5 "http://localhost:8096/take?src=jxs" >/dev/null; echo "--- stream headers ---"; curl -s -m6 -D - -o /tmp/stream.bin "http://localhost:8096/stream"; echo "--- bytes captured ---"; wc -c </tmp/stream.bin; pkill -f "239.10.10.22"; pkill -f jxs-stream-send-file; pkill -f bbb-full; true'
```
Expected: header `Content-Type: multipart/x-mixed-replace; boundary=st2110frame`, and `bytes captured` well over 100000 (MJPEG frames flowing). (Exit 15 from pkill is expected.)

- [ ] **Step 5: Stop the test server and commit**

```bash
wsl.exe -d Ubuntu -- bash -lc 'pkill -f monitor-web.py; true'
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/monitor-web.py && git commit -m "feat: IS-05 source-switchable ST 2110 monitor (raw v0 / JPEG-XS m0)"
```

---

## Task 2: Firewall helper for iPad access

**Files:**
- Create: `pc/open-monitor-firewall.ps1`

- [ ] **Step 1: Write the helper**

Create `pc/open-monitor-firewall.ps1`:

```powershell
# Allow the iPad (and other LAN hosts) to reach the ST 2110 monitor on TCP 8096.
# Run in an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File pc\open-monitor-firewall.ps1
$rule = "ST2110 monitor 8096 (WSL)"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 8096 -Profile Any | Out-Null
  Write-Output "Added firewall rule: $rule (TCP 8096 inbound)"
} else {
  Write-Output "Firewall rule already present: $rule"
}
```

- [ ] **Step 2: Verify it parses (syntax check, non-elevated)**

Run:
```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Command -Syntax New-NetFirewallRule >\$null; [void][System.Management.Automation.Language.Parser]::ParseFile('C:\\Users\\dgper\\pi-nmos-st2110\\pc\\open-monitor-firewall.ps1',[ref]\$null,[ref]\$null); Write-Output 'parse OK'"
```
Expected: `parse OK`. (Actually adding the rule requires elevation — the user runs it once; do not run elevated here.)

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/open-monitor-firewall.ps1 && git commit -m "feat: firewall helper to expose monitor :8096 to the iPad"
```

---

## Task 3: Document + end-to-end verification

**Files:**
- Modify: `docs/superpowers/RESUME.md`

- [ ] **Step 1: Human end-to-end check (PC browser first, then iPad)**

Run (full path: JPEG-XS sender live, monitor up; then open the page):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send-file.sh ~/jxs-media/bbb-full.mov >/tmp/s.log 2>&1 & setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 3; echo "monitor up on :8096"; cat /tmp/mon.log'
```
Then on the **PC browser** open `http://localhost:8096` — confirm: PTP timecode advancing, JPEG-XS video, and the two buttons switch source (JPEG-XS shows BBB; Pi raw shows the Pi flow if `launch-all.sh` is running, else black). Then, after running the firewall helper elevated, open `http://192.168.4.85:8096` on the **iPad** and confirm the same. Stop with `wsl.exe -d Ubuntu -- bash -lc 'pkill -f monitor-web.py; pkill -f 239.10.10.22; pkill -f jxs-stream-send-file; true'`.

- [ ] **Step 2: Update RESUME.md**

In `docs/superpowers/RESUME.md`, under the "Tools built" list, add:

```markdown
- `pc/monitor-web.py` — **IS-05 source-switchable monitor** (`:8096`, iPad-friendly): on-page buttons issue real IS-05 takes to switch the video receiver between the Pi raw ST 2110-20 (receiver `v0`, `239.10.10.20:5005`) and the PC JPEG-XS island flow (receiver `m0`, `239.10.10.22:5008`); `/stream` follows the active receiver and transcodes to MJPEG, with PTP timecode. One-time: `pc/open-monitor-firewall.ps1` (elevated) opens TCP 8096 for the iPad (`http://192.168.4.85:8096`). Both sources must be streaming to view them.
```

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add docs/superpowers/RESUME.md && git commit -m "docs: record IS-05 source-switchable monitor"
```

---

## Self-Review

**Spec coverage:**
- §1/§3 switchable monitor on :8096, raw=v0 / jxs=m0 → Task 1 (`SOURCES`, `/take`, `/stream`). ✓
- §3 routes `/`, `/take`, `/stream`, `/time` → Task 1 Handler. ✓
- §3 transcode mapping (gst raw; ffmpeg→gst jxs) → Task 1 `stream_cmd`. ✓
- §4 IS-05 (resolve by label, PATCH staged master_enable + activate_immediate, read active) → Task 1 `receiver_id`/`set_enable`/`is_enabled`; verified Task 1 Step 3. ✓
- §5.3 firewall for iPad → Task 2 + Task 3 Step 1. ✓
- §5.2 independent multicast receiver, §5.5 no LD_LIBRARY_PATH (system lib) → inherent in `stream_cmd` (no LD path). ✓
- §6 testing: take flip (T1 S3), stream (T1 S4), PTP + switch + iPad (T3 S1). ✓
- §7 deliverables (monitor-web.py, open-monitor-firewall.ps1, RESUME) → Tasks 1–3. ✓

**Placeholder scan:** none — full server code and firewall script included; every run step has exact commands + expected output.

**Type/name consistency:** `SOURCES` keys `raw`/`jxs` used consistently across `take`, `active_src`, `stream_cmd`, `/take`, and the page buttons. Receiver labels `easy-nmos-node/receiver/v0` and `.../m0` match the verified virtnode labels. `BOUNDARY=st2110frame` used in both transcode tails and the Content-Type. GStreamer raw format token `Y42B` matches the JPEG-XS viewer convention. Port `8096` consistent across server, firewall, and docs.
