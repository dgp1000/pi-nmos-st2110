# Resume / project state

**Last worked:** 2026-06-08. Core project + stretch goals **complete.**

## What's built and working
- **Topology:** Pi 5 (Debian 13 "Trixie", `pi5-nmos`, user `dgperkins`) — WiFi mgmt `192.168.6.232`, wired PoE **media island** `eth0`=10.10.10.1. WSL (mirrored networking) island iface `eth1`=10.10.10.2, WiFi `eth4`. WSL user is `dgper` (uid 1000) — privileged cmds via `wsl -d Ubuntu -u root`.
- ✅ **ST 2110-30 audio** — multicast L24 239.10.10.10:5004, 1ms ptime, heard on PC speakers.
- ✅ **ST 2110-20 video** — multicast RFC 4175 raw (UYVY 320x240@25) 239.10.10.20:**5005**.
- ✅ **PTP** — Pi grandmaster (`ptp4l -i eth0 -m -S`). WSL follower discovers it but can't fully lock (WSL limitation — expected).
- ✅ **NMOS** — registry + node (Docker in WSL, `/root/easy-nmos`, `docker compose -f docker-compose.wsl.yml up -d nmos-registry nmos-virtnode`). IS-05 take drives real audio via `pc/activation-watcher.py` + `pc/take.py`.

## Tools built (in `pc/` and `pi/`)
- `pi/master-clock.py` — terminal PTP studio clock (big digits, threaded pmc poll).
- `pi/master-clock-web.py` — web PTP clock served from the Pi; **iPad** opens `http://pi5-nmos.local:8000`.
- `pc/activation-watcher.py` — NMOS IS-05 take → starts/stops the GStreamer audio receiver (resolves receiver by label).
- `pc/take.py on|off` — trigger the IS-05 take.
- `pc/jitter-meter.py` — RTP inter-arrival jitter (kernel timestamps). Wired island: mean 1000us, std-dev ~600us (bursty = no ST 2110-21 shaping + WSL RX batching).
- `pc/video-web.py` — transcodes the 2110-20 flow to MJPEG and serves it at `http://localhost:8095` (Windows browser). Runs in a background task. (Originally a workaround for WSLg not showing video windows — that limitation is now fixed; see the JPEG-XS demo below.)
- `pc/jxs-stream-send.sh` / `pc/jxs-stream-view.sh` — **PC-native JPEG-XS demo** (1080p59.94, x86 only): FFmpeg encodes a testsrc to JPEG-XS over MPEG-TS/UDP **multicast on the island** (`239.10.10.22:5008` via `eth1`), FFmpeg decodes, GStreamer shows it in a GPU (D3D12) window. Self-receives now; any x86 host on `10.10.10.0/24` can join the same group. One-time setup: `sudo bash pc/jxs-install-libs.sh` (installs the SVT-JPEG-XS lib system-wide + raises UDP buffers). The Pi can't encode/decode JPEG-XS (SVT-JPEG-XS is x86/AVX2 only; the reference libjxs benchmarked ~0.6 fps at 1080p on x86).
- `pc/jxs-stream-send-file.sh <video> [--once]` — stream a real video file (instead of testsrc) through the same JPEG-XS island pipeline; auto-scales/conforms any source to 1080p59.94 and loops. The viewer auto-centers its window on screen 1 (`pc/jxs-center-window.ps1`, move-only — resizing the WSLg window freezes glimagesink). Sample media in `~/jxs-media/` (Big Buck Bunny full movie, CC-BY).
- `pc/monitor-web.py` — **IS-05 source-switchable monitor** (`:8096`, iPad-friendly): on-page buttons issue real IS-05 takes to switch the video receiver between the Pi raw ST 2110-20 (receiver `v0`, `239.10.10.20:5005`) and the PC JPEG-XS island flow (receiver `m0`, `239.10.10.22:5008`); `/stream` follows the active receiver and transcodes to MJPEG, with PTP timecode. One-time: `pc/open-monitor-firewall.ps1` (elevated) opens TCP 8096 for the iPad (`http://192.168.4.85:8096`). Both sources must be streaming to view them. Audio follows the take too: a 🔊 toggle plays the active source's audio (JPEG-XS clip track, or the Pi's ST 2110-30 L24 flow) via an **HLS audio stream** (`/hls/aud.m3u8`, iOS-Safari-native — progressive `/audio` won't play on iOS because media elements need byte-range/HLS, not an unbounded stream; the progressive route is kept for desktop) into an `<audio>` element, loosely synced. The file sender (`jxs-stream-send-file.sh`) carries the clip's AAC audio in the MPEG-TS.
- `pc/hevc-stream-*.sh` — **4K HEVC GPU pipeline** (the GPU counterpart to JPEG-XS); two flavours, both encode+decode on the RTX 2080 Ti (`nvidia-smi` shows NVENC+NVDEC util). Group `239.10.10.65:5010`, env auto-detects the island NIC by IP, window auto-centers.
  - **testsrc demo** (`env`+`send`+`view`): `videotestsrc 4K59.94 → nvh265enc (NVENC ~50 Mbit CBR) → RTP → multicast → nvh265dec (NVDEC) → CPU downscale → glimagesink`. Run `bash pc/hevc-stream-send.sh` + `bash pc/hevc-stream-view.sh`.
  - **real-file A/V** (`send-file`+`view-file`): `filesrc → qtdemux → nvh264dec (NVDEC) → nvh265enc (NVENC) + MP3 audio → mpegtsmux → multicast → tsdemux → nvh265dec + mp3 → glimagesink + autoaudiosink (lip-synced)`. Run `bash pc/hevc-stream-send-file.sh ~/jxs-media/bbb-4k.mp4` + `bash pc/hevc-stream-view-file.sh`. 4K source `~/jxs-media/bbb-4k.mp4` (BBB sunflower 2160p60, CC-BY, archive.org).

## Key gotchas
- **WSLg video display now works** via `export GALLIUM_DRIVER=d3d12` (forces the D3D12 GPU instead of CPU `llvmpipe`; set system-wide in `/etc/profile.d/gpu-d3d12.sh` by `restore.ps1`). Native `glimagesink`/`autovideosink` windows render on the RTX 2080 Ti. (Previously WSLg showed no GStreamer video at all — the MJPEG browser path was the workaround.)
- **WSL renames the island NIC across reboots** — `eth1` one boot, `eth0` the next (the one holding `10.10.10.2`). NEVER hardcode the iface name; detect it by IP. All gst-based scripts now do: Python via shared `pc/island_iface.py`, bash via inline awk (`hevc-stream-env.sh`, `jxs-demo.sh`, `restore-check.sh`). ffmpeg-based `jxs-stream-*` + `jitter-meter.py` use `localaddr=10.10.10.2` (IP) so were never affected.
- **NVIDIA NVDEC/NVENC (CUDA) can't share GPU buffers with the Mesa-D3D12 GL** used by glimagesink — insert `cudadownload` (CUDA→system) first. (Zero-copy `nvh265dec ! glupload` fails `not-negotiated`.) **AND GL upload+scale of a 4K frame on Mesa-D3D12 is only ~3 fps** — do NOT `glupload`/`glcolorscale` 4K; downscale on CPU first (`cudadownload → videoconvert → videoscale → <display size> → glimagesink`), which sustains ~60 fps because the sink only handles the small frame.
- **Streaming HEVC over MPEG-TS needs `h265parse config-interval=-1`** (re-sends VPS/SPS/PPS every IDR); without it a mid-stream joiner can't get the resolution → `unspecified size`, no video (intermittent). RTP's `rtph265pay config-interval=-1` is the equivalent.
- **WSLg can stop mapping windows after heavy gst churn** — plain `glimagesink` gets stuck at PREROLLED (never PLAYING) and no window appears though GL inits. Fix: `wsl --shutdown`, then relaunch (WSLg restarts clean).
- WSLg **audio** works even from detached/`setsid` gst, **as long as `PULSE_SERVER=unix:/mnt/wslg/PulseServer` is exported** (autoaudiosink → pulsesink, `GstPulseSinkClock`). GUI windows also work detached. In a multi-sink pipeline, if one sink (e.g. video) can't preroll, the whole pipeline stalls — so a broken video branch also kills audio.
- nmos-cpp virtnode **regenerates resource UUIDs** — always resolve receivers by **label**.
- IS-05 receiver has **2 transport-param legs** — PATCH only `master_enable`+activation, not a 1-elem `transport_params`.
- Static IP/firewall need **elevated** PowerShell. Firewall rule "ST2110 media inbound (WSL)" opens UDP 5004,5005,319,320. Docker uses host ports 8080/8081/8090/8091/1883.
- Run a **single** `ptp4l` (extras pile up; `sudo pkill ptp4l` then start one).

## Possible next steps (optional)
- ✅ **Video under NMOS IS-05 control** — done: `pc/monitor-web.py` switches the video receiver (v0 raw / m0 JPEG-XS) via IS-05 takes.
- ✅ **Video on the iPad** — done: the switchable monitor at `http://192.168.4.85:8096` (run `pc/open-monitor-firewall.ps1` elevated once).
1. Wired-vs-WiFi **jitter comparison**.
2. Real PTP lock with a **2nd Pi** as follower.
3. Fold `video-web.py` (codec A/B) + `monitor-web.py` into one page; add audio to the AV take.

## Key facts
- Repo: `C:\Users\dgper\pi-nmos-st2110` (Windows git). Audio 239.10.10.10:5004, video 239.10.10.20:5005.
- Inline execution: Claude drives WSL/PowerShell; user runs Pi cmds + local-WSL viewers.
