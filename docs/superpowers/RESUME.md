# Resume here (next session)

**Paused:** 2026-06-07, end of planning. **Resume:** 2026-06-08.

## Status
- ✅ Design spec approved & committed — `docs/superpowers/specs/2026-06-07-pi-st2110-nmos-design.md`
- ✅ Plan 1 (media foundation, spec phases 0–2) written & committed — `docs/superpowers/plans/2026-06-07-media-foundation.md`
- ✅ Spec + plan revised for the wired media-island topology (multicast, static IPs)
- ✅ SD card imaged (Raspberry Pi OS Lite 64-bit). Settings used:
  - hostname `pi5-nmos`, user `dgperkins`, SSH on (password auth), home WiFi configured, Pi Connect off
- ⏸️ **Not yet started:** booting the Pi and executing Plan 1

## Exact next steps
1. Insert the imaged card into the Pi 5; connect it to the **PoE switch** (power + `eth0`).
2. Power on, wait ~1–2 min for first boot.
3. From the PC: `ping pi5-nmos.local` (answers over WiFi via mDNS).
4. `ssh dgperkins@pi5-nmos.local` — first contact is over **WiFi**.
5. Begin **Plan 1**, inline execution:
   - Task 0–3: repo scaffold, WSL `mirrored` networking, Windows firewall.
   - **Topology amendment** (static IPs): SSH to Pi, `nmcli` give `eth0` `10.10.10.1`; set Windows Ethernet adapter `10.10.10.2`; cable the PC into the switch.
   - Task 4: bidirectional UDP over `10.10.10.x`.
   - Task 5–7: GStreamer install → multicast L24 audio → **hear the tone** on PC speakers.
   - Task 8–9: PTP leader/follower; observe offset.

## Key facts to remember
- Media island: `10.10.10.0/24`, no router uplink; internet via WiFi on each device.
- Audio multicast group `239.10.10.10:5004`; video (Plan 2) `239.10.10.20:5006`.
- Execution style: **inline** (hardware-in-the-loop). Claude drives WSL/PowerShell; user runs Pi commands.
- After Plan 1 works, write **Plan 2** (NMOS nodes + IS-05-driven media + low-res video); its first task confirms arm64 `nmos-cpp` availability and the node-config schema.
- The older `easy-nmos` Docker stack (in WSL at `/root/easy-nmos`) is shut down; this project reuses that nmos-cpp knowledge.
