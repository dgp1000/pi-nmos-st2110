# JPEG-XS PC-Native Streaming Demo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream a 1080p59.94 JPEG-XS video over MPEG-TS/UDP and display it in a GPU-accelerated native window, entirely on this PC (WSL2/x86).

**Architecture:** Two shell scripts joined by UDP. A sender uses FFmpeg (`testsrc → jpegxs → mpegts → udp`). A reusable receiver uses FFmpeg to decode (`udp mpegts jpegxs → raw`) and pipes raw frames into a GStreamer GPU window (`fdsrc ! rawvideoparse ! videoconvert ! glimagesink`). A one-time setup step makes the locally-built SVT-JPEG-XS library usable by the default (display-capable) user.

**Tech Stack:** Locally-built FFmpeg (`--enable-libsvtjpegxs`, `/usr/local/bin/ffmpeg`), SVT-JPEG-XS (`libSvtJpegxs.so`), GStreamer 1.28 (`glimagesink` on Mesa D3D12), WSLg, bash.

**Run context:** All scripts run **inside a local WSL terminal as the default user `dgper`** (which has WSLg display). Repo path in WSL is `/mnt/c/Users/dgper/pi-nmos-st2110`. Only Task 1 needs root (via `sudo`). Git commits are run from the repo root.

---

## File Structure

- `pc/jxs-install-libs.sh` (new) — one-time: copy `libSvtJpegxs.so*` into `/usr/local/lib` + `ldconfig` so the default user's ffmpeg finds the `jpegxs` codec.
- `pc/jxs-stream-env.sh` (new) — shared config (geometry, transport, `GALLIUM_DRIVER`) sourced by both scripts so encode caps and parse caps cannot drift.
- `pc/jxs-stream-send.sh` (new) — JPEG-XS MPEG-TS/UDP sender (synthetic `testsrc`).
- `pc/jxs-stream-view.sh` (new) — reusable JPEG-XS receiver + GPU display.
- `docs/superpowers/RESUME.md` (modify) — add the demo to the "on-demand" section.

All `.sh` files are kept LF by the repo's `.gitattributes` (`*.sh eol=lf`).

---

## Task 1: Make FFmpeg+JPEG-XS usable by the default user

**Files:**
- Create: `pc/jxs-install-libs.sh`

**Why:** `libSvtJpegxs.so*` lives in `/root/jxs-install/lib`; `/root` is mode `700`, so `dgper` can't traverse to it and `dgper`'s ffmpeg shows no `jpegxs` codec. Copying into `/usr/local/lib` + `ldconfig` fixes this for all users with no `LD_LIBRARY_PATH`.

- [ ] **Step 1: Create the installer script**

Create `pc/jxs-install-libs.sh`:

```bash
#!/usr/bin/env bash
# One-time: make the locally-built SVT-JPEG-XS shared library usable by ANY user.
# The build left it under /root (mode 700), so the default user can't reach it
# and ffmpeg shows no jpegxs codec. Copy into /usr/local/lib and refresh the
# loader cache. Run once:  sudo bash pc/jxs-install-libs.sh
set -euo pipefail
SRC=/root/jxs-install/lib
if [ ! -e "$SRC/libSvtJpegxs.so" ]; then
  echo "ERROR: $SRC/libSvtJpegxs.so not found (is SVT-JPEG-XS built?)" >&2
  exit 1
fi
cp -Pv "$SRC"/libSvtJpegxs.so* /usr/local/lib/
ldconfig
echo "Registered:"
ldconfig -p | grep -i jpegxs
```

- [ ] **Step 2: Run the installer (as root)**

Run (in WSL):
```bash
cd /mnt/c/Users/dgper/pi-nmos-st2110
sudo bash pc/jxs-install-libs.sh
```
Expected: `cp` prints the three `libSvtJpegxs.so*` files copied, then `Registered:` lists `libSvtJpegxs.so.0 => /usr/local/lib/libSvtJpegxs.so.0`.

- [ ] **Step 3: Verify the default user now sees the codec**

Run (as `dgper`, no lib path):
```bash
ffmpeg -hide_banner -codecs 2>/dev/null | grep -i jpegxs
```
Expected: a line containing `jpegxs ... (decoders: libsvtjpegxs) (encoders: libsvtjpegxs)`.

- [ ] **Step 4: Commit**

```bash
git add pc/jxs-install-libs.sh
git commit -m "feat: installer to expose SVT-JPEG-XS lib to all users"
```

---

## Task 2: Shared config file

**Files:**
- Create: `pc/jxs-stream-env.sh`

- [ ] **Step 1: Create the config**

Create `pc/jxs-stream-env.sh`:

```bash
#!/usr/bin/env bash
# Shared config for the PC-native JPEG-XS streaming demo. Sourced by
# jxs-stream-send.sh and jxs-stream-view.sh so the encode geometry and the
# decode/parse caps can never drift apart.

# Use the D3D12 GPU (not CPU llvmpipe) for GStreamer GL display. Set explicitly
# so the viewer works even from a non-login shell.
export GALLIUM_DRIVER=d3d12

# --- video geometry: encode caps and rawvideoparse caps MUST match these ---
JXS_W=1920
JXS_H=1080
JXS_FPS=60000/1001      # 59.94 fps (US cadence)
JXS_PIXFMT=yuv422p      # ffmpeg pixel format; JPEG-XS is 4:2:2
JXS_GSTFMT=Y42B         # GStreamer format token for planar yuv422p 8-bit
JXS_BPP=3               # JPEG-XS bits/pixel (quality vs bandwidth)

# --- transport: MPEG-TS over UDP ---
JXS_ADDR=127.0.0.1      # loopback; use 239.10.10.22 for island multicast
JXS_PORT=5008
```

- [ ] **Step 2: Verify it sources and exports correctly**

Run:
```bash
cd /mnt/c/Users/dgper/pi-nmos-st2110
bash -c 'source pc/jxs-stream-env.sh; echo "$JXS_W $JXS_H $JXS_FPS $JXS_PIXFMT $JXS_GSTFMT $JXS_BPP $JXS_ADDR:$JXS_PORT; GALLIUM=$GALLIUM_DRIVER"'
```
Expected: `1920 1080 60000/1001 yuv422p Y42B 3 127.0.0.1:5008; GALLIUM=d3d12`

- [ ] **Step 3: Commit**

```bash
git add pc/jxs-stream-env.sh
git commit -m "feat: shared config for JPEG-XS streaming demo"
```

---

## Task 3: Sender script

**Files:**
- Create: `pc/jxs-stream-send.sh`

- [ ] **Step 1: Create the sender**

Create `pc/jxs-stream-send.sh`:

```bash
#!/usr/bin/env bash
# JPEG-XS streaming SENDER (synthetic source): generates a 1080p59.94 test
# pattern, encodes it to JPEG-XS, muxes to MPEG-TS, and streams over UDP.
# Run in a WSL terminal:  bash pc/jxs-stream-send.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/jxs-stream-env.sh"

echo "JPEG-XS sender -> udp://$JXS_ADDR:$JXS_PORT  (${JXS_W}x${JXS_H} @ ${JXS_FPS}, bpp ${JXS_BPP})"
echo "Ctrl+C to stop."
exec ffmpeg -hide_banner -loglevel warning \
  -f lavfi -i "testsrc=size=${JXS_W}x${JXS_H}:rate=${JXS_FPS}" \
  -vf "format=${JXS_PIXFMT}" \
  -c:v jpegxs -bpp "${JXS_BPP}" \
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?pkt_size=1316&buffer_size=2097152"
```

- [ ] **Step 2: Start the sender in the background**

Run:
```bash
cd /mnt/c/Users/dgper/pi-nmos-st2110
bash pc/jxs-stream-send.sh &
SENDER_PID=$!
sleep 3
```
Expected: prints the `JPEG-XS sender -> udp://127.0.0.1:5008 ...` banner and keeps running (no error exit).

- [ ] **Step 3: Verify the stream carries jpegxs (probe the UDP)**

Run:
```bash
ffprobe -hide_banner -loglevel error -timeout 3000000 \
  -show_entries stream=codec_name,width,height -of default=nw=1 \
  udp://127.0.0.1:5008
```
Expected: `codec_name=jpegxs`, `width=1920`, `height=1080`.

- [ ] **Step 4: Stop the test sender**

Run:
```bash
kill "$SENDER_PID" 2>/dev/null; wait "$SENDER_PID" 2>/dev/null; true
```

- [ ] **Step 5: Commit**

```bash
git add pc/jxs-stream-send.sh
git commit -m "feat: JPEG-XS MPEG-TS/UDP sender (1080p59.94)"
```

---

## Task 4: Receiver + GPU display script

**Files:**
- Create: `pc/jxs-stream-view.sh`

- [ ] **Step 1: Create the viewer**

Create `pc/jxs-stream-view.sh`:

```bash
#!/usr/bin/env bash
# JPEG-XS streaming RECEIVER + native GPU display. Receives JPEG-XS-in-MPEG-TS
# over UDP, decodes with FFmpeg, and shows it in a GPU-accelerated GStreamer
# window (Mesa D3D12). Reusable for any jpegxs/mpegts/udp source.
# Run in a LOCAL WSL terminal (needs WSLg display):  bash pc/jxs-stream-view.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/jxs-stream-env.sh"

echo "JPEG-XS viewer <- udp://$JXS_ADDR:$JXS_PORT  (GPU window; close window or Ctrl+C to stop)"
ffmpeg -hide_banner -loglevel warning \
    -fflags nobuffer -flags low_delay \
    -i "udp://${JXS_ADDR}:${JXS_PORT}?fifo_size=1000000&overrun_nonfatal=1" \
    -f rawvideo -pix_fmt "${JXS_PIXFMT}" - \
  | gst-launch-1.0 -q \
      fdsrc ! rawvideoparse format="${JXS_GSTFMT}" width="${JXS_W}" height="${JXS_H}" framerate="${JXS_FPS}" \
      ! queue ! videoconvert ! glimagesink sync=false
```

- [ ] **Step 2: Start a sender for testing**

Run:
```bash
cd /mnt/c/Users/dgper/pi-nmos-st2110
bash pc/jxs-stream-send.sh &
SENDER_PID=$!
sleep 2
```
Expected: sender banner; running.

- [ ] **Step 3: Non-visual smoke — decode 60 frames from UDP**

Run (the receiver's decode half only):
```bash
ffmpeg -hide_banner -loglevel error \
  -i "udp://127.0.0.1:5008?fifo_size=1000000&overrun_nonfatal=1" \
  -frames:v 60 -f null - && echo "DECODE_OK"
```
Expected: `DECODE_OK` (no decode errors). Confirms encode→TS→UDP→decode works end-to-end without a display.

- [ ] **Step 4: GPU assertion — confirm the display uses D3D12**

Run:
```bash
GST_DEBUG=glcontext:4 timeout 8 bash pc/jxs-stream-view.sh 2>&1 | grep -m1 GL_RENDERER
```
Expected: a line `GL_RENDERER: D3D12 (NVIDIA GeForce RTX 2080 Ti)`.

- [ ] **Step 5: Stop the test sender**

Run:
```bash
kill "$SENDER_PID" 2>/dev/null; wait "$SENDER_PID" 2>/dev/null; true
```

- [ ] **Step 6: Commit**

```bash
git add pc/jxs-stream-view.sh
git commit -m "feat: JPEG-XS receiver + GPU display (FFmpeg decode -> GStreamer)"
```

---

## Task 5: End-to-end visual run + documentation

**Files:**
- Modify: `docs/superpowers/RESUME.md` (the "On-demand (not auto-started)" list in `RESTORE.md` is the equivalent index; add to `RESUME.md` tools list too)

- [ ] **Step 1: Full visual run (human-verified)**

Run in **two local WSL terminals** (default user):
```bash
# Terminal A (sender):
cd /mnt/c/Users/dgper/pi-nmos-st2110 && bash pc/jxs-stream-send.sh
# Terminal B (viewer):
cd /mnt/c/Users/dgper/pi-nmos-st2110 && bash pc/jxs-stream-view.sh
```
Expected: a native window opens showing the moving SMPTE-style `testsrc` pattern at 1080p, smooth at ~59.94 fps. Stop with Ctrl+C in each.

- [ ] **Step 2: Document the demo in RESUME.md**

In `docs/superpowers/RESUME.md`, under the "Tools built (in `pc/` and `pi/`)" list, add:

```markdown
- `pc/jxs-stream-send.sh` / `pc/jxs-stream-view.sh` — **PC-native JPEG-XS demo** (x86 only): FFmpeg encodes 1080p59.94 testsrc to JPEG-XS over MPEG-TS/UDP (`127.0.0.1:5008`), FFmpeg decodes, GStreamer shows it in a GPU (D3D12) window. One-time setup: `sudo bash pc/jxs-install-libs.sh`. The Pi can't encode JPEG-XS (SVT-JPEG-XS is x86/AVX2 only).
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/RESUME.md
git commit -m "docs: record PC-native JPEG-XS streaming demo"
```

---

## Self-Review

**Spec coverage:**
- §1/§3 PC-only encode→transport→decode→display → Tasks 3, 4, 5. ✓
- §4 params (1080p, 59.94, yuv422p/Y42B, bpp3, MPEG-TS/UDP 5008) → Task 2 config, used in 3 & 4. ✓
- §6.1 codec libs reachable → Task 1 (improved over spec's `LD_LIBRARY_PATH=/root/...` by installing to `/usr/local/lib`; same requirement, cleaner + works for the display-capable user). ✓
- §6.2 `GALLIUM_DRIVER=d3d12` in viewer → Task 2 env (exported, sourced by viewer). ✓
- §6.3 force `yuv422p` → Task 3 `-vf format=yuv422p`. ✓
- §6.4 UDP buffer tuning + low latency → Task 3 `buffer_size`, Task 4 `fifo_size`/`overrun_nonfatal`/`nobuffer`/`low_delay`. ✓
- §6.5 join-any-time (MPEG-TS) → inherent; verified by viewer attaching to running sender in Task 4. ✓
- §7 tests: non-visual smoke (Task 4 Step 3), GPU assertion (Task 4 Step 4), visual (Task 5 Step 1), bitrate sanity (covered by ffprobe in Task 3). ✓
- §8 deliverables (two scripts + docs) → Tasks 3, 4, 5; plus Task 1 installer. ✓

**Placeholder scan:** none — every script is complete; every run step has exact commands and expected output.

**Type/name consistency:** `JXS_W/H/FPS/PIXFMT/GSTFMT/BPP/ADDR/PORT` defined in Task 2 and used verbatim in Tasks 3–4. GStreamer format `Y42B` matches ffmpeg `yuv422p` (confirmed against existing `jxs-stress.sh`). Port `5008` consistent throughout.
