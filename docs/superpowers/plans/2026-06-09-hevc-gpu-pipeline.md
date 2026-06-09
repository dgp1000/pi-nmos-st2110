# 4K HEVC GPU Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An all-GStreamer, all-GPU 4K HEVC pipeline on the island — NVENC encode → RTP multicast → NVDEC decode → GPU (GL) display — that genuinely works the RTX 2080 Ti.

**Architecture:** Three scripts (env + send + view) mirroring the JPEG-XS demo. The sender encodes synthetic 4K59.94 to H.265 with `nvh265enc` (CBR ~50 Mbit), packs it as RTP, and multicasts on `239.10.10.65:5010` via `eth1`. The viewer joins the group, decodes with `nvh265dec`, GL-downscales 4K→1080p on the GPU (`glcolorscale`), and renders with `glimagesink`. Window auto-centers via the existing helper.

**Tech Stack:** GStreamer 1.28 `nvcodec` (NVENC/NVDEC) + `opengl` (glcolorscale/glimagesink on Mesa D3D12), RTP H.265, WSL2, RTX 2080 Ti.

**Run context:** local WSL terminal as `dgper`. Repo Windows `C:\Users\dgper\pi-nmos-st2110` == WSL `/mnt/c/Users/dgper/pi-nmos-st2110`. Branch `master`, commits approved. Use Write on the **Windows** path. NVENC/NVDEC verified working in WSL (4K HEVC: enc 34%, dec 8%). Gotchas: outer Git-Bash eats `$VAR` (use `printenv`/`set|grep`); wrap `/mnt/c` paths in quoted `bash -lc`; **cleanup with `pkill gst-launch-1.0`** (comm-based — does NOT self-match the shell, unlike `pkill -f`).

---

## Task 1: Shared config

**Files:**
- Create: `pc/hevc-stream-env.sh`

- [ ] **Step 1: Write the config**

Create `pc/hevc-stream-env.sh`:

```bash
#!/usr/bin/env bash
# Shared config for the 4K HEVC GPU pipeline (NVENC encode -> RTP multicast -> NVDEC decode).
# Sourced by hevc-stream-send.sh and hevc-stream-view.sh so send/receive stay in lock-step.

# GPU for glimagesink (D3D12, not CPU llvmpipe)
export GALLIUM_DRIVER=d3d12

# --- video geometry ---
HEVC_W=3840
HEVC_H=2160
HEVC_FPS=60000/1001        # 59.94
HEVC_BITRATE=50000         # kbit/s, CBR
HEVC_GOP=30                # IDR ~every 0.5s @ 59.94 (fast multicast late-join)
HEVC_DISPLAY_W=1920        # GPU-downscale for the display window; NVDEC still decodes full 4K
HEVC_DISPLAY_H=1080

# --- transport: RTP/H.265 multicast on the island ---
HEVC_ADDR=239.10.10.65     # mnemonic: H.265
HEVC_PORT=5010
HEVC_IFACE=eth1            # island NIC
HEVC_TTL=1
HEVC_CAPS="application/x-rtp,media=video,encoding-name=H265,clock-rate=90000,payload=96"
```

- [ ] **Step 2: Verify it sources (use `set | grep`, not `echo $VAR`)**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'source /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-env.sh && set | grep -E "^HEVC_(W|H|FPS|BITRATE|ADDR|PORT|IFACE)="'
```
Expected:
```
HEVC_ADDR=239.10.10.65
HEVC_BITRATE=50000
HEVC_FPS=60000/1001
HEVC_H=2160
HEVC_IFACE=eth1
HEVC_PORT=5010
HEVC_W=3840
```

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/hevc-stream-env.sh && git commit -m "feat: shared config for 4K HEVC GPU pipeline"
```

---

## Task 2: NVENC sender

**Files:**
- Create: `pc/hevc-stream-send.sh`

- [ ] **Step 1: Write the sender**

Create `pc/hevc-stream-send.sh`:

```bash
#!/usr/bin/env bash
# 4K HEVC NVENC sender: synthetic 4K source -> nvh265enc (GPU) -> RTP -> island multicast.
# Run in WSL:  bash pc/hevc-stream-send.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

echo "HEVC NVENC sender -> udp://$HEVC_ADDR:$HEVC_PORT via $HEVC_IFACE  (${HEVC_W}x${HEVC_H} @ ${HEVC_FPS}, ${HEVC_BITRATE} kbit CBR)"
echo "Ctrl+C to stop."
exec gst-launch-1.0 -q \
  videotestsrc is-live=true ! "video/x-raw,width=${HEVC_W},height=${HEVC_H},framerate=${HEVC_FPS}" \
  ! videoconvert \
  ! nvh265enc rc-mode=cbr bitrate="${HEVC_BITRATE}" preset=p4 tune=low-latency gop-size="${HEVC_GOP}" aud=true \
  ! h265parse ! rtph265pay config-interval=-1 pt=96 \
  ! udpsink host="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true ttl="${HEVC_TTL}" buffer-size=8388608
```

- [ ] **Step 2: Verify NVENC encodes and the RTP stream is decodable**

Run (start sender; check encoder util; headless-decode the group to prove the stream is valid H265; check decoder util):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-send.sh >/tmp/hevc-send.log 2>&1 & sleep 5; echo "=== encoder util ==="; nvidia-smi --query-gpu=utilization.encoder,utilization.gpu,memory.used --format=csv,noheader; echo "=== headless decode (rendered frames climbing = valid stream) ==="; timeout 7 gst-launch-1.0 -q udpsrc address=239.10.10.65 port=5010 multicast-iface=eth1 auto-multicast=true buffer-size=8388608 caps="application/x-rtp,media=video,encoding-name=H265,clock-rate=90000,payload=96" ! rtpjitterbuffer latency=200 ! rtph265depay ! h265parse ! nvh265dec ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false -v 2>&1 | grep -oE "rendered: [0-9]+" | tail -2; echo "=== decoder util ==="; nvidia-smi --query-gpu=utilization.decoder --format=csv,noheader; echo "=== sender errors? ==="; grep -iE "error|not-neg|fail|warn" /tmp/hevc-send.log | head; pkill gst-launch-1.0; true'
```
Expected: encoder util **> 0%**; `rendered:` count increasing (e.g. 100 then 200 — the RTP H265 is valid and NVDEC-decodes); decoder util **> 0%**; no sender errors. (If `nvh265enc` rejects the raw caps, the fallback is to add `format=NV12` to the videotestsrc capsfilter — but `videoconvert` should already bridge.)

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/hevc-stream-send.sh && git commit -m "feat: 4K HEVC NVENC sender (RTP multicast on island)"
```

---

## Task 3: NVDEC viewer + GPU display

**Files:**
- Create: `pc/hevc-stream-view.sh`

- [ ] **Step 1: Write the viewer**

Create `pc/hevc-stream-view.sh`:

```bash
#!/usr/bin/env bash
# 4K HEVC NVDEC viewer: RTP island multicast -> nvh265dec (GPU) -> GL downscale -> GPU window.
# Run in a LOCAL WSL terminal (needs WSLg display):  bash pc/hevc-stream-view.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

echo "HEVC NVDEC viewer <- udp://$HEVC_ADDR:$HEVC_PORT  (GPU window; close window or Ctrl+C to stop)"
# Center the window on screen 1 (Wayland can't self-position) -- reuse the JPEG-XS helper.
if command -v powershell.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  CENTER_PS1="$(wslpath -w "$DIR/jxs-center-window.ps1" 2>/dev/null)"
  [ -n "$CENTER_PS1" ] && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$CENTER_PS1" >/dev/null 2>&1 &
fi

exec gst-launch-1.0 -q \
  udpsrc address="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true buffer-size=8388608 caps="${HEVC_CAPS}" \
  ! rtpjitterbuffer latency=200 ! rtph265depay ! h265parse ! nvh265dec \
  ! glupload ! glcolorscale ! "video/x-raw(memory:GLMemory),width=${HEVC_DISPLAY_W},height=${HEVC_DISPLAY_H}" \
  ! glimagesink sync=false
```

- [ ] **Step 2: GPU assertion — viewer decodes on NVDEC and renders on D3D12**

Run (sender live; run the real viewer with GL debug; confirm renderer + no errors):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-send.sh >/tmp/hevc-send.log 2>&1 & sleep 4; GST_DEBUG=glcontext:4 timeout 12 bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-view.sh >/tmp/hevc-view.log 2>&1; echo "=== GL renderer ==="; grep -m1 GL_RENDERER /tmp/hevc-view.log || echo NO_GL; echo "=== errors? ==="; grep -iE "error|not-neg|fail|could not link" /tmp/hevc-view.log | head; echo "=== enc+dec util during view ==="; nvidia-smi --query-gpu=utilization.encoder,utilization.decoder --format=csv,noheader; pkill gst-launch-1.0; true'
```
Expected: `GL_RENDERER: D3D12 (NVIDIA GeForce RTX 2080 Ti)`; no link/negotiation errors; encoder **and** decoder utilization both > 0% (both engines working at once). (If `glupload`/`glcolorscale` fail to link from `nvh265dec`, fallback: `nvh265dec ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! glimagesink` — but the GL path is preferred since `nvh265dec` advertises `GLMemory`.)

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add pc/hevc-stream-view.sh && git commit -m "feat: 4K HEVC NVDEC viewer + GPU GL display (downscaled window)"
```

---

## Task 4: End-to-end visual + docs

**Files:**
- Modify: `docs/superpowers/RESUME.md`

- [ ] **Step 1: Full visual run (human-verified)**

Run (sender background, viewer ~25 s; watch screen 1 for a smooth 1080p-windowed 4K-HEVC test pattern):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-send.sh >/tmp/hevc-send.log 2>&1 & sleep 3; echo "=== window open ~25s -- WATCH; GPU engines: ==="; nvidia-smi --query-gpu=utilization.encoder,utilization.decoder,utilization.gpu --format=csv,noheader; timeout 25 bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/hevc-stream-view.sh >/tmp/hevc-view.log 2>&1; pkill gst-launch-1.0; true'
```
Expected: a centered window shows the moving test pattern; the printed `nvidia-smi` line shows encoder + decoder utilization. **Confirm the window visually.**

- [ ] **Step 2: Document in RESUME.md**

In `docs/superpowers/RESUME.md`, under the "Tools built" list, add:
```markdown
- `pc/hevc-stream-{env,send,view}.sh` — **4K HEVC GPU pipeline** (the GPU counterpart to JPEG-XS): `videotestsrc 4K59.94 → nvh265enc (NVENC, ~50 Mbit CBR) → RTP → multicast 239.10.10.65:5010 (eth1) → nvh265dec (NVDEC) → GL-downscale to 1080p → glimagesink (D3D12)`. All on the RTX 2080 Ti (encode+decode+render); `nvidia-smi` shows encoder+decoder util. Window auto-centers (`jxs-center-window.ps1`). Run: `bash pc/hevc-stream-send.sh` + `bash pc/hevc-stream-view.sh`.
```

- [ ] **Step 3: Commit**

```bash
cd /c/Users/dgper/pi-nmos-st2110 && git add docs/superpowers/RESUME.md && git commit -m "docs: record 4K HEVC GPU pipeline"
```

---

## Self-Review

**Spec coverage:**
- §1/§3 all-GStreamer GPU pipeline (NVENC→RTP→NVDEC→glimagesink), 4K59.94 HEVC ~50 Mbit, `239.10.10.65:5010`/eth1 → Tasks 1–3. ✓
- §4 params (geometry, bitrate, gop, group/port, iface, ttl, caps) → Task 1 env, used in 2 & 3. Encoder nicks pinned via gst-inspect: `rc-mode=cbr bitrate=50000 preset=p4 tune=low-latency gop-size=30`. ✓
- §6.1 late-join (gop 30 + `config-interval=-1`) → Task 2 sender. ✓
- §6.2 iface pin (`multicast-iface=eth1`) → both scripts. ✓
- §6.3 UDP buffers + jitterbuffer → both scripts. ✓
- §6.4 GPU display (`GALLIUM_DRIVER=d3d12`, GL path) → env + viewer. ✓
- §6.5 4K window scaling → GL downscale to 1920×1080 (Task 3); centered via helper. ✓
- §7 testing: encoder util + decodable stream (Task 2), decoder util + GL renderer (Task 3), visual (Task 4). ✓
- §8 deliverables (3 scripts + RESUME) → Tasks 1–4. ✓

**Placeholder scan:** none — full script content; exact pipelines; commands with expected output. Documented fallbacks (NV12 caps; videoconvert/videoscale path) are explicit alternatives, not placeholders.

**Type/name consistency:** `HEVC_W/H/FPS/BITRATE/GOP/DISPLAY_W/DISPLAY_H/ADDR/PORT/IFACE/TTL/CAPS` defined in Task 1, used verbatim in 2 & 3. Group `239.10.10.65`, port `5010`, `eth1`, payload 96 / encoding-name H265 consistent across sender RTP, receiver caps, and tests. Cleanup uses `pkill gst-launch-1.0` everywhere (no self-kill).
