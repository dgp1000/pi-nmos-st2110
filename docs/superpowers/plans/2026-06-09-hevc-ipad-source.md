# HEVC 4K iPad Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the 4K HEVC island stream (`239.10.10.65:5010`) as a third switchable source on the iPad monitor, shown as an MJPEG preview + HLS audio like the existing Pi-raw and JXS sources.

**Architecture:** One-file change to `pc/monitor-web.py`. HEVC is not an NMOS flow, so it gets `label: None`; `take()`/`active_src()` are taught to handle a label-less source (exclusive switch by disabling the NMOS receivers, then sticky). `stream_cmd`/`hls_cmd` get a `hevc` branch (NVDEC→MJPEG; TS-MP3→AAC-HLS). One page button is added.

**Tech Stack:** Python `http.server`, GStreamer (`tsdemux`/`nvh265dec`/`jpegenc`), ffmpeg (HLS), WSL, RTX 2080 Ti.

**Run context:** local WSL, repo Windows `C:\Users\dgper\pi-nmos-st2110` == WSL `/mnt/c/Users/dgper/pi-nmos-st2110`. Edit on the Windows path. Branch `master`, direct commits. No unit-test framework — verification is integration (curl/nvidia-smi/ffprobe). **Gotchas:** restart the monitor by killing with the bracket trick in a SEPARATE call from the relaunch (`pkill -f '[m]onitor-web.py'`); the relaunch line contains the path so never combine it with the pkill. The monitor's island `IFACE` is auto-detected (`island_iface()`); the island is on `eth0` today. The HEVC sender must be running for HEVC tests.

---

## Task 1: Register HEVC source + non-NMOS switching

**Files:**
- Modify: `pc/monitor-web.py` (`SOURCES`, `take`, `active_src`)

- [ ] **Step 1: Add the HEVC source to `SOURCES`**

Replace:
```python
SOURCES = {
    "raw": {"label": "easy-nmos-node/receiver/v0", "group": "239.10.10.20", "port": 5005},
    "jxs": {"label": "easy-nmos-node/receiver/m0", "group": "239.10.10.22", "port": 5008},
}
```
with:
```python
SOURCES = {
    "raw": {"label": "easy-nmos-node/receiver/v0", "group": "239.10.10.20", "port": 5005},
    "jxs": {"label": "easy-nmos-node/receiver/m0", "group": "239.10.10.22", "port": 5008},
    "hevc": {"label": None, "group": "239.10.10.65", "port": 5010},  # GPU HEVC island flow (not NMOS)
}
```

- [ ] **Step 2: Make `take()` skip the IS-05 call for label-less sources**

Replace:
```python
def take(src):
    for key, s in SOURCES.items():
        set_enable(s["label"], key == src)
    _active["src"] = src
    _active["ts"] = time.monotonic()
```
with:
```python
def take(src):
    for key, s in SOURCES.items():
        if s["label"] is None:        # non-NMOS source (e.g. HEVC) -> nothing to take
            continue
        set_enable(s["label"], key == src)
    _active["src"] = src
    _active["ts"] = time.monotonic()
```
(Effect: `take("hevc")` disables the raw+jxs receivers and sets active=hevc; `take("raw"/"jxs")` is unchanged.)

- [ ] **Step 3: Make `active_src()` sticky for label-less sources**

Replace:
```python
def active_src():
    if time.monotonic() - _active["ts"] < _ACTIVE_TTL:
        return _active["src"]
    src = DEFAULT_SRC
```
with:
```python
def active_src():
    if time.monotonic() - _active["ts"] < _ACTIVE_TTL:
        return _active["src"]
    if SOURCES.get(_active["src"], {}).get("label") is None:   # non-NMOS -> trust last take
        _active["ts"] = time.monotonic()
        return _active["src"]
    src = DEFAULT_SRC
```
(Leaves the existing NMOS re-derive loop below it untouched.)

- [ ] **Step 4: Syntax-check**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'python3 -m py_compile /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py && echo OK'
```
Expected: `OK`

- [ ] **Step 5: Restart the monitor and verify the take works (no NMOS error for the label-less source)**

Kill (separate call, no path):
```bash
wsl.exe -d Ubuntu -- bash -lc "pkill -f '[m]onitor-web.py'; sleep 1; echo killed"
```
Start + test (separate call):
```bash
wsl.exe -d Ubuntu -- bash -lc 'setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 3; echo "take hevc:"; curl -s -m5 "http://localhost:8096/take?src=hevc"; echo; echo "raw enabled? jxs enabled?:"; curl -s -m5 "http://localhost:8096/take?src=hevc" >/dev/null; sleep 1; true'
```
Expected: `{"active": "hevc"}` (HTTP 200, **not** a 500/`receiver None not found`).

- [ ] **Step 6: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/monitor-web.py && git commit -m "feat(monitor): register HEVC as a non-NMOS switchable source"
```

---

## Task 2: HEVC video transcode (`stream_cmd`)

**Files:**
- Modify: `pc/monitor-web.py` (`stream_cmd`)

- [ ] **Step 1: Add the `hevc` branch to `stream_cmd`**

In `stream_cmd(src)`, the function currently starts:
```python
def stream_cmd(src):
    s = SOURCES[src]
    tail = (f"videoscale ! video/x-raw,width=640,height=480 ! "
            f"jpegenc quality=85 ! multipartmux boundary={BOUNDARY} ! fdsink fd=1")
    if src == "raw":
```
Insert a `hevc` branch immediately after the `tail = (...)` line and before `if src == "raw":`:
```python
    if src == "hevc":
        return ("gst-launch-1.0 -q "
                f"udpsrc address={s['group']} port={s['port']} multicast-iface={IFACE} "
                f"auto-multicast=true ! tsdemux ! h265parse ! nvh265dec ! "
                f"cudadownload ! videoconvert ! {tail}")
```
(NVDEC-decodes the HEVC TS, then the shared `tail` downscales to 640×480 and MJPEG-encodes.)

- [ ] **Step 2: Syntax-check**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'python3 -m py_compile /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py && echo OK'
```
Expected: `OK`

- [ ] **Step 3: Start the HEVC sender, restart the monitor, and verify MJPEG frames + NVDEC**

Kill (separate call):
```bash
wsl.exe -d Ubuntu -- bash -lc "pkill -f '[m]onitor-web.py'; sleep 1; echo killed"
```
Start sender + monitor + test (separate call):
```bash
wsl.exe -d Ubuntu -- bash -lc 'setsid bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-send.sh >/tmp/s.log 2>&1 </dev/null & setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 4; curl -s -m5 "http://localhost:8096/take?src=hevc" >/dev/null; echo "=== fetch 3s of /stream ==="; curl -s -m3 "http://localhost:8096/stream" -o /tmp/hevc-stream.bin; ls -l /tmp/hevc-stream.bin; echo "JPEG SOI markers (frames):"; grep -c -a $(printf "\xff\xd8") /tmp/hevc-stream.bin 2>/dev/null || echo "(grep marker)"; echo "boundary present:"; grep -c -a "st2110frame" /tmp/hevc-stream.bin; echo "NVDEC util:"; nvidia-smi --query-gpu=utilization.decoder --format=csv,noheader; pkill gst-launch-1.0; true'
```
Expected: `/tmp/hevc-stream.bin` is non-empty (hundreds of KB), the `st2110frame` boundary count > 0 (multipart MJPEG is flowing), and NVDEC util > 0% (HEVC is GPU-decoded).

- [ ] **Step 4: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/monitor-web.py && git commit -m "feat(monitor): GPU HEVC->MJPEG transcode for the HEVC source"
```

---

## Task 3: HEVC audio (`hls_cmd` + `audio_cmd`)

**Files:**
- Modify: `pc/monitor-web.py` (`audio_cmd`, `hls_cmd`)

- [ ] **Step 1: Add a `hevc` branch to `hls_cmd`**

`hls_cmd(src)` currently builds the `hls` flag string then does `if src == "jxs": ... return ...` else the raw gst path. Insert a `hevc` branch right after the `if src == "jxs":` block's `return` and before the raw `return`:
```python
    if src == "hevc":
        s = SOURCES["hevc"]
        url = (f"udp://{s['group']}:{s['port']}?localaddr=10.10.10.2"
               f"&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864")
        return f"ffmpeg -hide_banner -loglevel error -fflags nobuffer -i '{url}' -vn {hls}"
```
(ffmpeg pulls the MP3 from the TS and re-encodes AAC into the HLS playlist. Same shape as the jxs branch — only the group/port differ.)

- [ ] **Step 2: Add the same `hevc` branch to `audio_cmd` (legacy `/audio` route)**

In `audio_cmd(src)`, after the `if src == "jxs":` block's `return` and before the raw `return`, insert:
```python
    if src == "hevc":
        s = SOURCES["hevc"]
        url = (f"udp://{s['group']}:{s['port']}?localaddr=10.10.10.2"
               f"&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864")
        return (f"ffmpeg -hide_banner -loglevel error -fflags nobuffer "
                f"-i '{url}' -vn -c:a aac -b:a 160k -ac 2 -f adts -")
```

- [ ] **Step 3: Syntax-check**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'python3 -m py_compile /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py && echo OK'
```
Expected: `OK`

- [ ] **Step 4: Verify HLS audio with the FILE sender (which has MP3 audio)**

Kill (separate call):
```bash
wsl.exe -d Ubuntu -- bash -lc "pkill -f '[m]onitor-web.py'; sleep 1; echo killed"
```
Start file sender + monitor + test (separate call):
```bash
wsl.exe -d Ubuntu -- bash -lc 'setsid bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-send-file.sh ~/jxs-media/bbb-4k.mp4 >/tmp/s.log 2>&1 </dev/null & setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 5; curl -s -m5 "http://localhost:8096/take?src=hevc" >/dev/null; echo "=== m3u8 ==="; curl -s -m8 "http://localhost:8096/hls/aud.m3u8"; echo "=== ffprobe the HLS over HTTP (expect aac) ==="; wsl.exe -d Ubuntu -- bash -lc "ffprobe -hide_banner -loglevel error -show_entries stream=codec_name,channels -of default=nw=1 http://localhost:8096/hls/aud.m3u8 2>&1 | head"; pkill gst-launch-1.0; pkill ffmpeg; true'
```
Expected: a valid `#EXTM3U` playlist with a `seg*.ts` entry, and ffprobe reports `codec_name=aac`.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/monitor-web.py && git commit -m "feat(monitor): HEVC source audio (TS MP3 -> AAC HLS)"
```

---

## Task 4: Page button + end-to-end + docs

**Files:**
- Modify: `pc/monitor-web.py` (page button)
- Modify: `docs/superpowers/RESUME.md`

- [ ] **Step 1: Add the HEVC button to the page**

Replace:
```html
  <button id="bjxs" class="on" onclick="take('jxs',this)">PC JPEG-XS</button>
  <button id="braw" onclick="take('raw',this)">Pi raw 2110-20</button>
  <button id="bsnd" onclick="toggleSound(this)">&#128264; Sound off</button>
```
with:
```html
  <button id="bjxs" class="on" onclick="take('jxs',this)">PC JPEG-XS</button>
  <button id="braw" onclick="take('raw',this)">Pi raw 2110-20</button>
  <button id="bhevc" onclick="take('hevc',this)">PC HEVC 4K</button>
  <button id="bsnd" onclick="toggleSound(this)">&#128264; Sound off</button>
```
(The existing `take(src,btn)` JS already reloads `/stream`, reconnects audio, and moves the `on` highlight across `#ctrl button:not(#bsnd)`, so the new button works with no JS change.)

- [ ] **Step 2: Syntax-check + verify the button is served**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'python3 -m py_compile /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py && echo OK'
```
Expected: `OK`

Kill + restart + check the page (separate calls):
```bash
wsl.exe -d Ubuntu -- bash -lc "pkill -f '[m]onitor-web.py'; sleep 1; echo killed"
```
```bash
wsl.exe -d Ubuntu -- bash -lc 'setsid python3 -u /mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-web.py >/tmp/mon.log 2>&1 </dev/null & sleep 3; curl -s -m5 "http://localhost:8096/" | grep -o "PC HEVC 4K\|bhevc" | head'
```
Expected: prints `bhevc` and/or `PC HEVC 4K`.

- [ ] **Step 3: End-to-end on the iPad (human-verified)**

Ensure a HEVC sender is running (`bash pc/hevc-stream-send-file.sh ~/jxs-media/bbb-4k.mp4`) and the monitor is up. On the iPad at `http://192.168.4.85:8096`, fully reload, then:
- Tap **PC HEVC 4K** → the preview shows the HEVC content; turn Sound on → audio plays.
- Tap **Pi raw 2110-20** / **PC JPEG-XS** → switches back (IS-05 take fires; their preview returns).
Expected: all three sources switch cleanly; HEVC preview + audio work. **Confirm on the iPad.**

- [ ] **Step 4: Document in RESUME.md**

In `docs/superpowers/RESUME.md`, update the `monitor-web.py` bullet to mention the third source. Append to that bullet:
```markdown
 A third button **PC HEVC 4K** switches to the GPU HEVC island flow (`239.10.10.65:5010`); it's not an NMOS receiver, so selecting it disables the Pi/JXS receivers and points the monitor's NVDEC→MJPEG transcode at the HEVC multicast (the HEVC sender must be running). Audio is the clip's MP3→AAC HLS (silent for the testsrc HEVC sender).
```

- [ ] **Step 5: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/monitor-web.py docs/superpowers/RESUME.md && git commit -m "feat(monitor): PC HEVC 4K button + docs"
```

---

## Self-Review

**Spec coverage:**
- §3.1 SOURCES hevc → Task 1 Step 1. ✓
- §3.2 take() skip label → Task 1 Step 2. ✓
- §3.3 active_src() sticky → Task 1 Step 3. ✓
- §3.4 stream_cmd hevc (NVDEC→MJPEG) → Task 2. ✓
- §3.5 hls_cmd + audio_cmd hevc → Task 3. ✓
- §3.6 page button → Task 4 Step 1. ✓
- §6 testing (take, stream+NVDEC, HLS aac, e2e) → Task 1 Step 5, Task 2 Step 3, Task 3 Step 4, Task 4 Step 3. ✓
- §7 deliverables (monitor-web.py + RESUME) → all tasks. ✓

**Placeholder scan:** none — every step has exact old→new code and concrete commands with expected output.

**Type/name consistency:** `"hevc"` key, group `239.10.10.65`, port `5010`, `label: None`, button id `bhevc` used consistently. `stream_cmd`/`hls_cmd`/`audio_cmd` branches mirror the existing `jxs` branch shape (only group/port differ) and reuse the module's `IFACE`, `BOUNDARY`, `tail`, and `hls` variables exactly as defined in the current file.
