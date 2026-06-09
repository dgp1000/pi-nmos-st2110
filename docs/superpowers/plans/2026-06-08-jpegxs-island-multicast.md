# JPEG-XS Island Multicast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retarget the working PC-native JPEG-XS demo from loopback to a real multicast flow on the ST 2110 island (`239.10.10.22:5008`, pinned to `eth1`), self-received and decoded into the GPU window.

**Architecture:** Edit the three existing scripts only. The shared config gains a multicast group + island NIC address + TTL; the sender and receiver URLs gain `localaddr` (and the sender `ttl`). Everything else (encode geometry, decode, GPU display, UDP buffer tuning) is unchanged and already verified.

**Tech Stack:** FFmpeg (`libsvtjpegxs`), GStreamer 1.28 (`glimagesink` on Mesa D3D12), WSL2 mirrored networking, MPEG-TS/UDP multicast, bash.

**Run context:** All commands run **inside a local WSL terminal as the default user `dgper`** (has the codec via `/usr/local/lib` and the WSLg display). Repo path in WSL: `/mnt/c/Users/dgper/pi-nmos-st2110`. Git commits run from the repo root. Multicast send + self-receive on `eth1` was already verified feasible during design (`codec_name=jpegxs`, 1920×1080 from the group).

---

## Task 1: Point the shared config at the island multicast group

**Files:**
- Modify: `pc/jxs-stream-env.sh`

- [ ] **Step 1: Replace the transport block**

Find this block at the end of `pc/jxs-stream-env.sh`:

```bash
# --- transport: MPEG-TS over UDP ---
JXS_ADDR=127.0.0.1      # loopback; use 239.10.10.22 for island multicast
JXS_PORT=5008
```

Replace it with:

```bash
# --- transport: MPEG-TS over UDP multicast on the ST 2110 island ---
JXS_ADDR=239.10.10.22       # multicast group (mnemonic: ST 2110-22; matches .10 audio / .20 video)
JXS_PORT=5008
JXS_LOCALADDR=10.10.10.2    # island NIC (eth1); pins send+recv to the island, not WiFi
JXS_TTL=1                   # keep on the local L2 segment (ST 2110 practice; raise only if routed)
```

- [ ] **Step 2: Verify the new vars source correctly**

Run (note: use `set | grep`, not `echo $VAR`, because the outer shell eats `$VAR`):
```bash
wsl.exe -d Ubuntu -- bash -lc 'source /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-env.sh && set | grep -E "^JXS_(ADDR|PORT|LOCALADDR|TTL)="'
```
Expected:
```
JXS_ADDR=239.10.10.22
JXS_LOCALADDR=10.10.10.2
JXS_PORT=5008
JXS_TTL=1
```

- [ ] **Step 3: Commit**

```bash
git add pc/jxs-stream-env.sh
git commit -m "feat: point JPEG-XS demo at island multicast group 239.10.10.22"
```

---

## Task 2: Send to the multicast group on the island NIC

**Files:**
- Modify: `pc/jxs-stream-send.sh`

- [ ] **Step 1: Update the banner line**

Find:
```bash
echo "JPEG-XS sender -> udp://$JXS_ADDR:$JXS_PORT  (${JXS_W}x${JXS_H} @ ${JXS_FPS}, bpp ${JXS_BPP})"
```
Replace with:
```bash
echo "JPEG-XS sender -> udp://$JXS_ADDR:$JXS_PORT via $JXS_LOCALADDR  (${JXS_W}x${JXS_H} @ ${JXS_FPS}, bpp ${JXS_BPP})"
```

- [ ] **Step 2: Update the output URL (add localaddr + ttl; bump send buffer)**

Find:
```bash
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?pkt_size=1316&buffer_size=2097152"
```
Replace with:
```bash
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&ttl=${JXS_TTL}&pkt_size=1316&buffer_size=8388608"
```

- [ ] **Step 3: Verify the sender multicasts jpegxs onto the island**

Run (starts sender in background by full path, probes the group on `eth1`, then stops it):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send.sh >/tmp/jxs-send.log 2>&1 & sleep 4; ffprobe -hide_banner -loglevel error -timeout 6000000 -show_entries stream=codec_name,width,height -of default=nw=1 "udp://239.10.10.22:5008?localaddr=10.10.10.2&overrun_nonfatal=1&buffer_size=67108864"; echo "--- send log ---"; cat /tmp/jxs-send.log; pkill -f "239.10.10.22"; true'
```
Expected: `codec_name=jpegxs`, `width=1920`, `height=1080`; send log shows the banner and no errors. (Exit 15 from `pkill` is expected.)

- [ ] **Step 4: Commit**

```bash
git add pc/jxs-stream-send.sh
git commit -m "feat: JPEG-XS sender multicasts on island (localaddr+ttl)"
```

---

## Task 3: Receive by joining the group on the island NIC

**Files:**
- Modify: `pc/jxs-stream-view.sh`

- [ ] **Step 1: Update the input URL (join the group on eth1)**

Find:
```bash
    -i "udp://${JXS_ADDR}:${JXS_PORT}?overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864" \
```
Replace with:
```bash
    -i "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864" \
```

- [ ] **Step 2: Non-visual decode smoke + group membership over multicast**

Run (sender in background, confirm the group is joined on `eth1`, decode 180 frames cleanly):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send.sh >/tmp/jxs-send.log 2>&1 & sleep 3; echo "=== group membership on eth1 ==="; ip maddr show eth1 | grep 239.10.10.22 || echo "(membership appears when a receiver joins)"; echo "=== decode 180 frames ==="; ffmpeg -hide_banner -loglevel error -i "udp://239.10.10.22:5008?localaddr=10.10.10.2&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864" -frames:v 180 -f null - 2>/tmp/jxs-dec.log; echo "decode rc=$?"; echo "error lines:"; grep -c -iE "HANDLE ERROR|error submitting" /tmp/jxs-dec.log; pkill -f "239.10.10.22"; true'
```
Expected: `decode rc=0` and `error lines: 0`. (The `ip maddr` line may show `239.10.10.22` if checked while a receiver is active; the decode itself joining the group is the real proof.)

- [ ] **Step 3: GPU assertion — viewer renders on D3D12 from the multicast group**

Run:
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send.sh >/tmp/jxs-send.log 2>&1 & sleep 3; GST_DEBUG=glcontext:4 timeout 12 bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-view.sh >/tmp/jxs-view.log 2>&1; grep -m1 GL_RENDERER /tmp/jxs-view.log || echo NO_GL; pkill -f "239.10.10.22"; pkill -f gst-launch; true'
```
Expected: a line containing `GL_RENDERER: D3D12 (NVIDIA GeForce RTX 2080 Ti)`.

- [ ] **Step 4: Commit**

```bash
git add pc/jxs-stream-view.sh
git commit -m "feat: JPEG-XS viewer joins island multicast group on eth1"
```

---

## Task 4: End-to-end visual run + documentation

**Files:**
- Modify: `docs/superpowers/RESUME.md`

- [ ] **Step 1: Full visual run (human-verified)**

Run (sender background, viewer foreground ~20s; watch for the window):
```bash
wsl.exe -d Ubuntu -- bash -lc 'bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-send.sh >/tmp/jxs-send.log 2>&1 & sleep 2; timeout 20 bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/jxs-stream-view.sh >/tmp/jxs-view.log 2>&1; grep -m1 GL_RENDERER /tmp/jxs-view.log; grep -c -iE "HANDLE ERROR|error submitting" /tmp/jxs-view.log; pkill -f "239.10.10.22"; pkill -f gst-launch; true'
```
Expected: native window shows the moving 1080p test pattern (fed from `239.10.10.22`), `GL_RENDERER: D3D12`, and `0` decode errors. **Confirm the window visually.**

- [ ] **Step 2: Update RESUME.md**

In `docs/superpowers/RESUME.md`, find the line:
```markdown
- `pc/jxs-stream-send.sh` / `pc/jxs-stream-view.sh` — **PC-native JPEG-XS demo** (1080p59.94, x86 only): FFmpeg encodes a testsrc to JPEG-XS over MPEG-TS/UDP (`127.0.0.1:5008`), FFmpeg decodes, GStreamer shows it in a GPU (D3D12) window. One-time setup: `sudo bash pc/jxs-install-libs.sh` (installs the SVT-JPEG-XS lib system-wide + raises UDP buffers). The Pi can't encode JPEG-XS (SVT-JPEG-XS is x86/AVX2 only).
```
Replace with:
```markdown
- `pc/jxs-stream-send.sh` / `pc/jxs-stream-view.sh` — **PC-native JPEG-XS demo** (1080p59.94, x86 only): FFmpeg encodes a testsrc to JPEG-XS over MPEG-TS/UDP **multicast on the island** (`239.10.10.22:5008` via `eth1`), FFmpeg decodes, GStreamer shows it in a GPU (D3D12) window. Self-receives now; any x86 host on `10.10.10.0/24` can join the same group. One-time setup: `sudo bash pc/jxs-install-libs.sh` (installs the SVT-JPEG-XS lib system-wide + raises UDP buffers). The Pi can't encode/decode JPEG-XS (SVT-JPEG-XS is x86/AVX2 only; the reference libjxs benchmarked ~0.6 fps at 1080p on x86).
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/RESUME.md
git commit -m "docs: JPEG-XS demo now multicasts on the island"
```

---

## Self-Review

**Spec coverage:**
- §1/§3 retarget to island multicast (group, eth1, self-receive) → Tasks 1–3. ✓
- §4 params (`239.10.10.22:5008`, `localaddr=10.10.10.2`, `ttl=1`; geometry/buffers unchanged) → Task 1 config, used in 2 & 3. ✓
- §6.1 interface pinning (`localaddr`) → Tasks 2 & 3. ✓
- §6.2 TTL=1 → Task 1 + sender URL Task 2. ✓
- §6.3 no firewall change → nothing to do (correct). ✓
- §6.4 packet-loss buffers → unchanged, exercised by Task 3 smoke (0 errors). ✓
- §7 tests: probe (Task 2), group membership + decode integrity (Task 3 Step 2), GPU (Task 3 Step 3), visual (Task 4 Step 1). ✓
- §8 deliverables (3 scripts + RESUME.md) → Tasks 1–4. ✓

**Placeholder scan:** none — every step has exact find/replace text and runnable commands with expected output.

**Type/name consistency:** `JXS_ADDR/PORT/LOCALADDR/TTL` defined in Task 1, used verbatim in Tasks 2–3. Group `239.10.10.22`, port `5008`, `localaddr=10.10.10.2` consistent across all tasks and the verification commands. GStreamer format stays `Y42B` (unchanged). Send buffer bumped 2 MB→8 MB (Task 2); receiver buffer stays 64 MB (already tuned).
