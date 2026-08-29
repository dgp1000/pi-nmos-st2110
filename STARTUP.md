# Atoll — bring-up / shutdown runbook

The rig is the **PC (Windows + WSL2/Ubuntu)** plus the **Raspberry Pi 5** on the isolated
ST 2110 "island" LAN (10.10.10.x). Everything on the PC runs as **systemd services** (enabled,
so they auto-start when WSL boots); the one manual step is the monitor-2 multiview.

Access: from a Mac `ssh atoll-pc` (192.168.4.85:2222, key auth). Pi from PC or Mac:
`ssh dgperkins@10.10.10.1` (key auth + passwordless sudo). Repo lives at `~/pi-nmos-st2110`.

## Bring it up (in order)

1. **Power on the PC and launch WSL.** WSL does *not* auto-start on Windows boot — open an
   Ubuntu/WSL terminal (or `wsl` in PowerShell). That boots WSL + systemd, and all the enabled
   services auto-start:
   - NMOS docker stack — registry `:8080`, virtnode `:8090`, AMWA testing `:5000`
   - `atoll-panel` (:8096 switcher + inspector), `atoll-tv` (HDHomeRun → HEVC on 5010),
     `atoll-tv-web` (:8098 standalone channel picker — now also built into the panel),
     `atoll-home` (5008), `atoll-music` (Mac Now-Playing bridge → 5012, self-healing),
     `atoll-anc` (ST 2110-40 ancillary timecode on 5020), `atoll-multiview` (:8099 browser view),
     `atoll-jxs-web` (:8100 JPEG XS), `atoll-j2k` (JPEG 2000 J2K/RTP island tile on 5016).

   Check: `systemctl is-active atoll-panel atoll-tv atoll-tv-web atoll-home atoll-music atoll-anc atoll-multiview atoll-jxs-web atoll-j2k`
   If the NMOS stack didn't come up (a `stop` overrides `restart:unless-stopped`):
   `cd ~/pi-nmos-st2110/deploy/nmos && sudo docker compose start` (or `up -d`).

2. **Island NIC IP.** Confirm the Windows **"Ethernet 2"** adapter still has static
   **10.10.10.2**. If not, in an **admin PowerShell**:
   `netsh interface ip set address "Ethernet 2" static 10.10.10.2 255.255.255.0`

3. **Pi.** It auto-starts `atoll-pi` on Pi boot (PTP grandmaster + ST 2110-20 raw video on
   239.10.10.21:5006 + ST 2110-30 L24 audio on 239.10.10.10:5004). If it was only stopped:
   `ssh dgperkins@10.10.10.1 sudo systemctl start atoll-pi`

4. **Multiview on monitor 2 — MANUAL** (needs the WSLg display, so run it in a *local* PC WSL
   terminal, never over SSH): `cd ~/pi-nmos-st2110/pc && bash output-render.sh 2`
   (use `0` for the primary monitor if #2 is off-screen). It follows the panel's take/layout.

5. **View / control.** Panel `192.168.4.85:8096` (iPad) or `localhost:8096` (PC's own browser);
   browser multiview `:8099`; JPEG XS `:8100`. From the PC's own browser use **localhost:<port>**
   (mirrored networking can't hairpin the external IP); from the iPad/Mac use **192.168.4.85:<port>**.

## Shut it down

On the PC (WSL):
```
sudo systemctl stop atoll-panel atoll-tv atoll-tv-web atoll-home atoll-music atoll-anc atoll-multiview atoll-jxs-web atoll-j2k
cd ~/pi-nmos-st2110/deploy/nmos && sudo docker compose stop
ssh dgperkins@10.10.10.1 sudo systemctl stop atoll-pi
```
Ctrl-C the `output-render.sh`. Then optionally `wsl --shutdown` (PowerShell) to fully stop WSL,
or just shut down Windows. Services are only *stopped*, not disabled — they auto-start on the
next WSL boot.

## Notes / gotchas
- The **island multicast receive on WSL is packet-rate limited** — HEVC tiles (~6 Mbit/s) and
  JPEG 2000 (J2K/RTP, sub-Mbit/s) fit; uncompressed HD 2110-20 and JPEG XS (~100 Mbit/s CBR) do
  **not**, which is why the Pi stays 320×240 and JPEG XS runs as a local encode→decode.
- **Live TV** needs the HDHomeRun reachable at `192.168.7.88`; some ATSC 3.0 channels are DRM and
  come up black — pick a plain HD channel.
- Rapid multicast join/leave can wedge a group's Windows-side membership; if a tile goes dark on a
  specific group, move it to a nearby free port (a port collision with Windows is why J2K is on 5016).
- **Live TV A/V sync**: single / follow-take view is lip-synced (audio+video share one pipeline).
  In multi/side the audio is a SEPARATE follower with no shared clock, so its offset vs the video is
  non-deterministic (each restart lands at a different phase) and can't be reliably tuned. A knob
  exists — `~/atoll-run/audio-delay-ms` (queue min-threshold-time delay, default 0 = smooth, audio
  slightly early) — but don't expect it to lock. For synced Live TV audio, watch in single view.
- **Seamless channel changes**: the `atoll-tv` service runs `tv-send-seamless.py` — a continuous
  HEVC encoder tail (fed by intervideosrc/interaudiosrc) whose SOURCE pipeline restarts on a channel
  change. 5010 never stops, so the multiview never sees a discontinuity and no longer crashes on a
  channel change (only the Live TV tile briefly freezes during the HDHR retune). `tv-send.sh` is the
  older restart-the-whole-pipeline sender, kept as a fallback.
- **multiview-app.py** is an OPTIONAL per-tile renderer (each tile its own pipeline via
  intervideosink; tile sinks MUST be sync=false). It genuinely isolates a tile's decode error, but
  intervideosrc flashes black when a tile is briefly late, so `output-render.sh` stays the default.
  The channel-change problem it targeted was ultimately solved in tv-send instead.
