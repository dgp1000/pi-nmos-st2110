# Resume here (next session)

**Last worked:** 2026-06-08.

## Status
- ✅ Design spec + Plan 1 written/committed, revised for wired media-island topology
- ✅ Pi booted — Debian 13 "Trixie", `pi5-nmos`, user `dgperkins`. WiFi mgmt: `192.168.6.232`.
- ✅ Pi software: GStreamer (`rtpL24pay`) + linuxptp 4.2
- ✅ WSL: mirrored networking; GStreamer + linuxptp 4.4; WSLg audio verified
- ✅ Windows firewall rule "ST2110 media inbound (WSL)" — inbound UDP 5004,5005,319,320
- ✅ Early win: live ST 2110-30 audio Pi->PC **over WiFi unicast**
- ✅ 🔌 **Wired media island up:** PoE switch, Pi `eth0`=10.10.10.1, PC Ethernet=10.10.10.2 (WSL mirrors it as `eth1`). Sub-ms latency, 0% loss, isolated from home LAN. PoE power good (`throttled=0x0`).
- ✅ 🎉 **MILESTONE: true ST 2110-30 MULTICAST audio** over the wired island. Pi `udpsink host=239.10.10.10 ... multicast-iface=eth0`; WSL `udpsrc address=239.10.10.10 ... multicast-iface=eth1 -> rtpL24depay -> autoaudiosink`. Heard on PC speakers. **Plan 1 Phase 1 DONE (real multicast).**

## Gotchas learned
- WSLg audio needs `export PULSE_SERVER=unix:/mnt/wslg/PulseServer` in the pipeline shell.
- Run a **single** receiver instance (a detached/background gst receiver reached PLAYING but was silent; a clean foreground one works). Always `pkill gst-launch-1.0` before a new run.
- Static IP / firewall changes need an **elevated** PowerShell (agent shell + `!` prefix are not elevated).
- WSL island interface is **`eth1`** (10.10.10.2); WiFi is `eth4`. Pi island interface is `eth0`.

## Exact next steps
1. **PTP (Phase 2):** ptp4l leader on Pi (`-i eth0`), follower on WSL (`-i eth1`), software timestamping, domain 0. Watch `master offset` converge on the wired link (expect tens of µs). Configs are in Plan 1 Tasks 8-9 (`pi/ptp4l-leader.conf`, `pc/ptp4l-follower.conf`).
2. **Plan 2:** NMOS nodes (IS-04/05) advertising the real audio sender/receiver + an activation-watcher so an IS-05 "take" starts/stops the GStreamer flow. First Plan-2 task: confirm arm64 `nmos-cpp` (Debian 13/arm64) and the node-config schema.
3. Then **low-res ST 2110-20 video** (`videotestsrc -> rtpvrawpay` / `rtpvrawdepay -> autovideosink` in a WSLg window).

## Key facts
- Pi: `dgperkins@pi5-nmos.local` (WiFi 192.168.6.232; island eth0 10.10.10.1). WSL island: eth1 10.10.10.2.
- Audio: multicast group 239.10.10.10:5004, L24/48k/2ch, pt=96, 1ms ptime. Video (Plan 2): 239.10.10.20:5006.
- Repo: `C:\Users\dgper\pi-nmos-st2110`. Inline execution (Claude drives WSL/PowerShell; user runs Pi cmds).
