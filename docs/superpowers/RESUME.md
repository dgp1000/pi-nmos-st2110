# Resume here (next session)

**Last worked:** 2026-06-08.

## Status — core project DONE; video is the remaining stretch
- ✅ Pi: Debian 13 "Trixie", `pi5-nmos`, user `dgperkins`. WiFi mgmt 192.168.6.232; island eth0 10.10.10.1.
- ✅ WSL (mirrored networking): GStreamer + linuxptp; WSLg audio. Island iface `eth1` 10.10.10.2; WiFi `eth4`.
  **WSL default user is `dgper` (uid 1000)** — privileged cmds need `wsl -d Ubuntu -u root -- ...`.
- ✅ Windows firewall rule "ST2110 media inbound (WSL)" — inbound UDP 5004,5005,319,320.
- ✅ Wired PoE media island, 10.10.10.0/24, sub-ms, isolated. PoE power good.
- ✅ **ST 2110-30 multicast audio** Pi→PC (group 239.10.10.10:5004, L24/48k/2ch, pt96, 1ms).
- ◐ **PTP:** Pi grandmaster works + WSL follower discovers/selects it; full lock blocked by WSL (event-msg handling). Learning-grade, as the spec predicted. `ptp4l -i eth0 -m -S` (Pi) / `-i eth1 -m -S -s` (WSL, as root).
- ✅ 🏆 **NMOS milestone:** registry + node (Docker in WSL, `rhastie/nmos-cpp`, reusing `/root/easy-nmos` bridge compose). An **IS-05 take starts/stops real audio** via `pc/activation-watcher.py` (re-resolves receiver `a0` by label each poll → starts/stops the GStreamer multicast receiver on master_enable). Trigger with `pc/take.py on|off`.

## How to re-run the NMOS demo
1. Registry+node: `wsl -d Ubuntu -u root -- bash -lc 'cd /root/easy-nmos && docker compose -f docker-compose.wsl.yml up -d nmos-registry nmos-virtnode'`
2. Pi (SSH): run the multicast audio sender (`udpsink host=239.10.10.10 ... multicast-iface=eth0`).
3. **Local WSL terminal** (for WSLg audio — NOT background/SSH): `python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/activation-watcher.py`
4. Take: `wsl -d Ubuntu -- bash -lc 'python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/take.py on'` (audio on) / `off` (audio off).

## Gotchas learned
- WSLg audio needs `PULSE_SERVER=unix:/mnt/wslg/PulseServer`; a **detached/background** gst is silent — run the watcher in a **real local WSL terminal**.
- nmos-cpp virtnode **regenerates resource UUIDs** occasionally (stable in ~16s+ windows) → always resolve receivers **by label**, never a fixed UUID.
- IS-05 receiver has **2 transport-param legs** (2022-7) → don't send a 1-element `transport_params` ("inconsistent array size"); just PATCH `master_enable` + activation (the watcher pins the flow).

## Exact next steps
1. **Low-res ST 2110-20 video** — Pi `videotestsrc -> rtpvrawpay` (e.g. UYVY 320x240@25) multicast on 239.10.10.20:5006; WSL `udpsrc -> rtpvrawdepay -> autovideosink` in a WSLg window. Then add an NMOS video receiver + extend the activation-watcher so an IS-05 take starts the **video** too.
2. (Optional) Real PTP lock with a 2nd Pi as follower.

## Key facts
- Repo: `C:\Users\dgper\pi-nmos-st2110` (Windows git). Scripts in `pc/`: `activation-watcher.py`, `take.py`.
- Audio multicast 239.10.10.10:5004; video (next) 239.10.10.20:5006.
- Inline execution: Claude drives WSL/PowerShell; user runs Pi cmds + the local-WSL watcher.
