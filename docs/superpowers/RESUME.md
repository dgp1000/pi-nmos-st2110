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
- `pc/jxs-stream-send.sh` / `pc/jxs-stream-view.sh` — **PC-native JPEG-XS demo** (1080p59.94, x86 only): FFmpeg encodes a testsrc to JPEG-XS over MPEG-TS/UDP (`127.0.0.1:5008`), FFmpeg decodes, GStreamer shows it in a GPU (D3D12) window. One-time setup: `sudo bash pc/jxs-install-libs.sh` (installs the SVT-JPEG-XS lib system-wide + raises UDP buffers). The Pi can't encode JPEG-XS (SVT-JPEG-XS is x86/AVX2 only).

## Key gotchas
- **WSLg video display now works** via `export GALLIUM_DRIVER=d3d12` (forces the D3D12 GPU instead of CPU `llvmpipe`; set system-wide in `/etc/profile.d/gpu-d3d12.sh` by `restore.ps1`). Native `glimagesink`/`autovideosink` windows render on the RTX 2080 Ti. (Previously WSLg showed no GStreamer video at all — the MJPEG browser path was the workaround.)
- WSLg **audio**/GUI need a **real local WSL session** (PULSE_SERVER=unix:/mnt/wslg/PulseServer); detached/background gst is silent. Headless data (MJPEG server, take.py) is fine backgrounded.
- nmos-cpp virtnode **regenerates resource UUIDs** — always resolve receivers by **label**.
- IS-05 receiver has **2 transport-param legs** — PATCH only `master_enable`+activation, not a 1-elem `transport_params`.
- Static IP/firewall need **elevated** PowerShell. Firewall rule "ST2110 media inbound (WSL)" opens UDP 5004,5005,319,320. Docker uses host ports 8080/8081/8090/8091/1883.
- Run a **single** `ptp4l` (extras pile up; `sudo pkill ptp4l` then start one).

## Possible next steps (optional)
1. Put **video under NMOS control** too (extend activation-watcher / a video receiver node so an IS-05 take gates the video).
2. View video on the **iPad** (open firewall for 8095, use `http://192.168.4.85:8095`).
3. Wired-vs-WiFi **jitter comparison**.
4. Real PTP lock with a **2nd Pi** as follower.

## Key facts
- Repo: `C:\Users\dgper\pi-nmos-st2110` (Windows git). Audio 239.10.10.10:5004, video 239.10.10.20:5005.
- Inline execution: Claude drives WSL/PowerShell; user runs Pi cmds + local-WSL viewers.
