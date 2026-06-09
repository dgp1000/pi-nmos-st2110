# Monitor Audio-Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sound to the iPad monitor — a parallel AAC audio stream that follows the IS-05 source (JPEG-XS clip audio, or the Pi's ST 2110-30 L24 flow), played via an `<audio>` element.

**Architecture:** The JPEG-XS file sender carries the clip's audio as AAC in its MPEG-TS. `monitor-web.py` gains an `/audio` route that mirrors `/stream` (follows `active_src()`) and emits progressive AAC-ADTS; a shared `_serve_subprocess` helper de-duplicates the subprocess streaming/cleanup used by both routes. The page adds a hidden `<audio>` element and a sound on/off toggle (the tap is the iPad autoplay gesture).

**Tech Stack:** Python 3 http.server, FFmpeg (native AAC), GStreamer (`rtpL24depay`), WSL2.

**Run context:** local WSL terminal as `dgper`. Repo: Windows `C:\Users\dgper\pi-nmos-st2110` == WSL `/mnt/c/Users/dgper/pi-nmos-st2110`. Branch `master`, commits approved. NMOS up; JPEG-XS media at `~/jxs-media/bbb-full.mov`; the Pi sends ST 2110-30 audio on `239.10.10.10:5004`. Use Write/Edit on the **Windows** path. Gotchas: outer Git-Bash eats `$VAR` (use `printenv`/literal); wrap `/mnt/c` paths inside `bash -lc '...'`; `pkill` exit 15 is expected.

---

## Task 1: JPEG-XS file sender carries the clip's audio

**Files:**
- Modify: `pc/jxs-stream-send-file.sh`

- [ ] **Step 1: Replace the ffmpeg invocation (drop `-an`, map + encode AAC)**

Find this exact block:
```bash
exec ffmpeg -hide_banner -loglevel warning -re "${LOOP[@]}" -i "$SRC" \
  -vf "scale=${JXS_W}:${JXS_H},fps=${JXS_FPS},format=${JXS_PIXFMT}" \
  -c:v jpegxs -bpp "${JXS_BPP}" -an \
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&ttl=${JXS_TTL}&pkt_size=1316&buffer_size=8388608"
```
Replace with:
```bash
exec ffmpeg -hide_banner -loglevel warning -re "${LOOP[@]}" -i "$SRC" \
  -map 0:v:0 -map 0:a:0? \
  -filter:v "scale=${JXS_W}:${JXS_H},fps=${JXS_FPS},format=${JXS_PIXFMT}" \
  -c:v jpegxs -bpp "${JXS_BPP}" \
  -c:a aac -b:a 160k -ac 2 \
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&ttl=${JXS_TTL}&pkt_size=1316&buffer_size=8388608"
```
(`-map 0:a:0?` makes audio optional so video-only files still work; `-filter:v` applies the geometry to the mapped video.)

- [ ] **Step 2: Verify the JPEG-XS flow now carries AAC audio**

Run (uses a separate test group to avoid disturbing any running sender):
```bash
wsl.exe -d Ubuntu -- bash -lc 'JXS_ADDR=239.10.10.23 JXS_PORT=5009 bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send-file.sh ~/jxs-media/bbb-full.mov >/tmp/atest.log 2>&1 & sleep 4; ffprobe -hide_banner -loglevel error -show_entries stream=codec_type,codec_name,channels -of default=nw=1 "udp://239.10.10.23:5009?localaddr=10.10.10.2&overrun_nonfatal=1&buffer_size=8388608" 2>&1 | head; pkill -f 239.10.10.23; true'
```
Wait — the env-var override only works if the script reads those vars. It does not (the script sources `jxs-stream-env.sh` which hard-sets them). Instead verify against the real group by briefly stopping any running sender:
```bash
wsl.exe -d Ubuntu -- bash -lc 'pkill -f jxs-stream-send-file; sleep 1; bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send-file.sh ~/jxs-media/bbb-full.mov >/tmp/atest.log 2>&1 & sleep 4; ffprobe -hide_banner -loglevel error -show_entries stream=codec_type,codec_name,channels -of default=nw=1 "udp://239.10.10.22:5008?localaddr=10.10.10.2&overrun_nonfatal=1&buffer_size=8388608" 2>&1 | head; pkill -f 239.10.10.22; pkill -f jxs-stream-send-file; true'
```
Expected: streams list includes `codec_type=video / codec_name=jpegxs` AND `codec_type=audio / codec_name=aac / channels=2`.

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/jxs-stream-send-file.sh && git commit -m "feat: carry the clip's audio (AAC) in the JPEG-XS MPEG-TS"
```

---

## Task 2: Monitor `/audio` route + page audio control

**Files:**
- Modify: `pc/monitor-web.py` (full updated content below)

- [ ] **Step 1: Replace the entire file with this content**

Write `pc/monitor-web.py`:

```python
#!/usr/bin/env python3
"""IS-05 source-switchable ST 2110 monitor (video + audio + PTP timecode), iPad-friendly.

Buttons issue real IS-05 takes to switch the active video receiver between the
Pi raw ST 2110-20 flow (NMOS receiver v0) and the PC JPEG-XS island flow (NMOS
receiver m0). /stream follows the active receiver and transcodes video to MJPEG;
/audio follows it and transcodes audio to AAC (the JPEG-XS clip's track, or the
Pi's ST 2110-30 L24 flow). PTP timecode is relayed from the Pi grandmaster.

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
_active = {"src": DEFAULT_SRC, "ts": 0.0}
_ACTIVE_TTL = 2.0  # seconds; avoids hammering the NMOS node on every /stream open

RAW_CAPS = ("application/x-rtp,media=(string)video,clock-rate=(int)90000,"
            "encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,"
            "width=(string)320,height=(string)240,payload=(int)96")
# The Pi's ST 2110-30 L24 audio flow (played when the raw video source is active).
AUDIO_RAW_GROUP, AUDIO_RAW_PORT = "239.10.10.10", 5004
AUDIO_L24_CAPS = ("application/x-rtp,media=audio,clock-rate=48000,"
                  "encoding-name=L24,channels=2,payload=96")

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
    _active["src"] = src
    _active["ts"] = time.monotonic()

def active_src():
    if time.monotonic() - _active["ts"] < _ACTIVE_TTL:
        return _active["src"]
    src = DEFAULT_SRC
    for key, s in SOURCES.items():
        try:
            if is_enabled(s["label"]):
                src = key
                break
        except Exception:
            pass
    _active["src"] = src
    _active["ts"] = time.monotonic()
    return src

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

def audio_cmd(src):
    if src == "jxs":
        s = SOURCES["jxs"]
        url = (f"udp://{s['group']}:{s['port']}?localaddr=10.10.10.2"
               f"&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864")
        return (f"ffmpeg -hide_banner -loglevel error -fflags nobuffer "
                f"-i '{url}' -vn -c:a aac -b:a 160k -ac 2 -f adts -")
    # raw: the Pi's ST 2110-30 L24 flow -> PCM (gst) -> AAC (ffmpeg)
    return ("gst-launch-1.0 -q "
            f"udpsrc address={AUDIO_RAW_GROUP} port={AUDIO_RAW_PORT} multicast-iface={IFACE} "
            f"auto-multicast=true caps='{AUDIO_L24_CAPS}' ! rtpjitterbuffer latency=100 ! "
            f"rtpL24depay ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=S16LE,channels=2,rate=48000 ! fdsink fd=1 | "
            f"ffmpeg -hide_banner -loglevel error -f s16le -ar 48000 -ac 2 -i - "
            f"-c:a aac -b:a 160k -f adts -")

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
 <audio id="aud"></audio>
 <div id="ctrl">
  <button id="bjxs" class="on" onclick="take('jxs',this)">PC JPEG-XS</button>
  <button id="braw" onclick="take('raw',this)">Pi raw 2110-20</button>
  <button id="bsnd" onclick="toggleSound(this)">&#128264; Sound off</button>
 </div>
 <div id="info"></div>
</div>
<script>
const FPS={FPS};
let offset=0, ptp={{}}, audioOn=false;
function reconnectAudio(){{
  const a=document.getElementById('aud');
  a.src='/audio?t='+Date.now(); a.muted=false; a.play().catch(()=>{{}});
}}
async function take(src,btn){{
  try{{await fetch('/take?src='+src,{{cache:'no-store'}});}}catch(e){{}}
  document.getElementById('vid').src='/stream?t='+Date.now();
  if(audioOn) reconnectAudio();
  document.querySelectorAll('#ctrl button:not(#bsnd)').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}}
function toggleSound(btn){{
  const a=document.getElementById('aud');
  audioOn=!audioOn;
  if(audioOn){{ reconnectAudio(); btn.textContent='\\u{{1F50A}} Sound on'; btn.classList.add('on'); }}
  else {{ a.pause(); a.removeAttribute('src'); a.load(); btn.textContent='\\u{{1F507}} Sound off'; btn.classList.remove('on'); }}
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

    def _serve_subprocess(self, cmd, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
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
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/stream":
            self._serve_subprocess(stream_cmd(active_src()),
                                   f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        elif parsed.path == "/audio":
            self._serve_subprocess(audio_cmd(active_src()), "audio/aac")
        elif parsed.path == "/take":
            qs = parse_qs(parsed.query)
            src = qs.get("src", [DEFAULT_SRC])[0]
            if src not in SOURCES:
                src = DEFAULT_SRC
            try:
                take(src); body = json.dumps({"active": src}).encode(); code = 200
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode(); code = 500
            self.send_response(code)
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
    print(f"ST 2110 IS-05 monitor on http://localhost:{PORT}  (video+audio; raw=v0, jxs=m0)")
    print("  Ctrl+C to stop")
    try: s.serve_forever()
    except KeyboardInterrupt: pass
```

- [ ] **Step 2: Syntax check**

```bash
wsl.exe -d Ubuntu -- bash -lc 'python3 -c "import ast; ast.parse(open(\"/mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py\").read()); print(\"syntax ok\")"'
```
Expected: `syntax ok`.

- [ ] **Step 3: Verify /audio serves AAC for the JPEG-XS source**

```bash
wsl.exe -d Ubuntu -- bash -lc 'pkill -f monitor-web.py; pkill -f jxs-stream-send-file; sleep 1; bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send-file.sh ~/jxs-media/bbb-full.mov >/tmp/s.log 2>&1 & setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 3; curl -s -m4 "http://localhost:8096/take?src=jxs" >/dev/null; curl -s -m6 -D - -o /tmp/a.bin "http://localhost:8096/audio" | grep -iE "HTTP|content-type"; echo "bytes:"; wc -c </tmp/a.bin; true'
```
Expected: `Content-Type: audio/aac`, bytes > 20000.

- [ ] **Step 4: Verify /audio serves AAC for the raw (ST 2110-30) source**

(Requires the Pi sending ST 2110-30 audio on 239.10.10.10:5004.)
```bash
wsl.exe -d Ubuntu -- bash -lc 'curl -s -m4 "http://localhost:8096/take?src=raw" >/dev/null; curl -s -m6 -D - -o /tmp/ar.bin "http://localhost:8096/audio" | grep -iE "HTTP|content-type"; echo "bytes:"; wc -c </tmp/ar.bin; pkill -f monitor-web.py; pkill -f jxs-stream-send-file; pkill -f 239.10.10.22; true'
```
Expected: `Content-Type: audio/aac`, bytes > 20000. (If the Pi audio is off, bytes may be ~0 — note it and move on; the jxs path is the primary check.)

- [ ] **Step 5: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/monitor-web.py && git commit -m "feat(monitor): /audio route follows the take (AAC); add sound toggle; DRY stream helper"
```

---

## Task 3: Docs + end-to-end

**Files:**
- Modify: `docs/superpowers/RESUME.md`

- [ ] **Step 1: Update the monitor bullet in RESUME.md**

In `docs/superpowers/RESUME.md`, find the `pc/monitor-web.py` bullet (starts with `- \`pc/monitor-web.py\` — **IS-05 source-switchable monitor**`) and append this sentence to the end of that bullet:
```markdown
 Audio follows the take too: a 🔊 toggle plays the active source's audio (JPEG-XS clip track, or the Pi's ST 2110-30 L24 flow) as AAC via an `<audio>` element (`/audio`), loosely synced. The file sender (`jxs-stream-send-file.sh`) now carries the clip's AAC audio in the MPEG-TS.
```

- [ ] **Step 2: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add docs/superpowers/RESUME.md && git commit -m "docs: monitor now carries audio (follows the IS-05 take)"
```

- [ ] **Step 3: Hand off the iPad check (human)**

The remaining verification is human + iPad: open `http://192.168.4.85:8096`, tap **🔊 Sound off → on**, confirm the JPEG-XS clip's audio plays; tap **Pi raw 2110-20** and confirm the audio follows to the Pi's ST 2110-30 flow; toggle sound off. (Note this is the only step a subagent can't self-verify.)

---

## Self-Review

**Spec coverage:**
- §3.1 sender carries AAC (map + optional audio + aac) → Task 1. ✓
- §3.2 `/audio` route follows `active_src()`, AAC-ADTS, `audio/aac`, process-group cleanup → Task 2 (`audio_cmd`, `_serve_subprocess`, `/audio`). ✓
- §3.2 raw L24 path (gst rtpL24depay → PCM | ffmpeg aac) with caps matching activation-watcher → Task 2 `audio_cmd` raw branch + `AUDIO_L24_CAPS`. ✓
- §3.3 page `<audio>` + sound toggle + take() reconnects audio + start with sound off → Task 2 PAGE/JS. ✓
- §5.1 no-audio source yields silence (optional map / EOF) → Task 1 `0:a:0?`, Task 2 pipeline EOF. ✓
- §5.4 autoplay gesture (sound button) → Task 2 toggleSound. ✓
- §6 testing: ffprobe aac (T1), /audio jxs + raw (T2), iPad (T3). ✓
- §7 deliverables (sender, monitor, RESUME) → Tasks 1–3. ✓

**Placeholder scan:** none — Task 1 exact find/replace; Task 2 full file; commands have expected output. (Task 1 Step 2 notes a non-working env-var variant then gives the working command — that's intentional guidance, not a placeholder.)

**Type/name consistency:** `audio_cmd`/`stream_cmd`/`active_src` consistent; `_serve_subprocess(cmd, content_type)` used by both `/stream` and `/audio`; `SOURCES` keys `raw`/`jxs`; `AUDIO_RAW_GROUP/PORT` + `AUDIO_L24_CAPS` match `activation-watcher.py` (239.10.10.10:5004, L24/48k/2ch/pt96). The earlier code-quality fixes (active cache, 500-on-error, no-cache, process-group reap) are preserved — the reap now lives once in `_serve_subprocess`.
