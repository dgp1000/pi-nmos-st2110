# Resume here (next session)

**Last worked:** 2026-06-08.

## Status
- ✅ Design spec + Plan 1 written, committed, revised for wired media-island topology
- ✅ SD card imaged; **Pi booted** — Debian 13 "Trixie", hostname `pi5-nmos`, user `dgperkins`, on WiFi at `192.168.6.232` (subnet 192.168.4.0/22, gw 192.168.4.1)
- ✅ **Pi software installed:** GStreamer (incl. `rtpL24pay`) + linuxptp 4.2
- ✅ **WSL set up:** mirrored networking on (WSL shares host LAN as `192.168.4.85`/eth4); GStreamer (`rtpL24depay`, `autoaudiosink`) + linuxptp 4.4 installed; WSLg audio verified
- ✅ **Windows firewall** rule "ST2110 media inbound (WSL)" — inbound UDP 5004,5005,319,320
- ✅ 🎉 **MILESTONE: live ST 2110-30 audio Pi -> PC over WiFi (unicast).** Pi `audiotestsrc -> rtpL24pay -> udpsink host=192.168.4.85:5004`; WSL `udpsrc:5004 -> rtpjitterbuffer -> rtpL24depay -> autoaudiosink`. Heard on PC speakers; tcpdump confirmed 300-byte L24 RTP packets.

This was Plan 1 Phase 1 proven **over WiFi unicast** as an early win, ahead of the wired build.

## Gotchas learned
- WSLg audio needs `export PULSE_SERVER=unix:/mnt/wslg/PulseServer` in the pipeline shell.
- Don't run two receivers on port 5004 at once (the live receiver silently lost to a lingering one). Run a single clean instance.
- Firewall rule needs an **elevated** PowerShell (the agent's shell + the `!` prefix are not elevated).

## Exact next steps
1. **Wire the media island:** Pi `eth0` -> PoE switch (drops the USB-C adapter), PC Ethernet -> switch.
2. **Static IPs** (Topology amendment): Pi `eth0` = 10.10.10.1 (`nmcli`, no gateway), Windows Ethernet adapter = 10.10.10.2. Verify from WSL.
3. **Re-run audio as true multicast** on the island: sender `udpsink host=239.10.10.10 ... multicast-iface=eth0`; receiver `udpsrc address=239.10.10.10 ... multicast-iface=<wsl-iface>`.
4. **PTP (Phase 2):** ptp4l leader on Pi / follower on WSL; observe offset on the wired link.
5. Then **Plan 2:** NMOS nodes (IS-04/05) + activation-watcher + low-res ST 2110-20 video. First Plan-2 task: confirm arm64 `nmos-cpp` and node-config schema.

## Key facts
- Pi: `dgperkins@pi5-nmos.local` (WiFi 192.168.6.232). WSL: `192.168.4.85`. Audio port 5004, multicast group 239.10.10.10.
- Repo on Windows: `C:\Users\dgper\pi-nmos-st2110`. Execution style: inline (Claude drives WSL/PowerShell; user runs Pi commands).
