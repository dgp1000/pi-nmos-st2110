# Atoll — Installation Guide

This is the from-scratch setup for standing up your own Atoll instance: an iPad-controlled
SMPTE ST 2110 / NMOS multiviewer and monitoring island. For the design walk see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); for daily bring-up/shutdown and troubleshooting see
[`STARTUP.md`](STARTUP.md).

> **Scope.** Atoll targets a specific hardware class (a GPU PC + a Raspberry Pi PTP grandmaster on an
> isolated media LAN). You don't need every optional piece, but the core rig assumes an NVIDIA GPU,
> two NICs, and one Pi. Steps marked **⚙ manual** are required but not scripted — do them by hand.

---

## 1. Hardware

**Required**
| Item | Notes |
|---|---|
| **PC** | Windows 11 + WSL2 (**mirrored** networking), an **NVIDIA GPU** (NVENC/NVDEC — HEVC encode+decode require it), and **two NICs**: a management NIC on your home LAN and a wired **island NIC**. |
| **Raspberry Pi 5** | The PTP **grandmaster** + ST 2110-20 raw video + ST 2110-30 L24 audio + web clock. The authoritative island clock. |
| **Island L2 switch** | Must do **IGMP snooping**. An isolated `10.10.10.0/24` with no router. |
| **Second monitor** | For the multiview wall (2560×1440 recommended). |
| **iPad / any browser** | The control surface (the panel is just a web page). |

**Optional** (skip cleanly — see §7)
| Item | Enables |
|---|---|
| 2nd Raspberry Pi (Pi 2B) | The PTP **follower** lock demo (`10.10.10.3`). |
| HDHomeRun tuner | The **Live TV** tile (+ live CEA-608 captions, SCTE-104). |
| Mac (or any) "Now Playing" host | The **Music** channel (else a placeholder card is used). |

---

## 2. Windows / WSL prerequisites  ⚙ manual (not scripted)

1. Install WSL2 + Ubuntu, and enable **mirrored networking** — in `%UserProfile%\.wslconfig`:
   ```ini
   [wsl2]
   networkingMode=mirrored
   ```
2. Enable systemd in WSL — in `/etc/wsl.conf`:
   ```ini
   [boot]
   systemd=true
   ```
   Then `wsl --shutdown` and reopen.
3. Give the **island NIC** a static address and make it firewall-friendly (elevated PowerShell):
   ```powershell
   New-NetIPAddress -InterfaceAlias "Ethernet 2" -IPAddress 10.10.10.2 -PrefixLength 24
   Set-NetConnectionProfile -InterfaceAlias "Ethernet 2" -NetworkCategory Private
   New-NetFirewallRule -DisplayName "Atoll island inbound" -Direction Inbound -RemoteAddress 10.10.10.0/24 -Action Allow -Profile Any
   ```
   Also set `secpol.msc → Network List Manager Policies → Unidentified Networks → Private` so the
   island NIC doesn't revert to Public on reboot (see STARTUP.md "Island connectivity / PTP lost").

> WSL is fine for everything except the on-GPU render wall; `install.sh` also supports native Linux.

---

## 3. Install on the PC

```bash
git clone <your-fork-or> https://github.com/dgp1000/pi-nmos-st2110.git
cd pi-nmos-st2110
bash install.sh            # apt deps + GPU/element check + brings up the NMOS docker stack
#   bash install.sh --check   # diagnose only, install nothing
```

`install.sh` installs GStreamer + ffmpeg + docker etc. and runs `docker compose up -d` in `deploy/nmos`
(the nmos-cpp registry `:8080`, a virtual node `:8090`, and the AMWA testing tool `:5000`).

**⚙ manual — add the packages `install.sh` does not:**
```bash
sudo apt-get install -y linuxptp \
     python3-gi python3-gi-cairo python3-numpy python3-cryptography python3-jwt
# JPEG XS viewer only: gstreamer1.0-gtk3 gir1.2-gtk-3.0
```
(Python deps are apt-installed on purpose — pip is blocked under PEP 668, which is why the RFC 6455
WebSocket bits are hand-rolled.)

**System tuning + GPU:**
```bash
sudo cp deploy/sysctl/60-atoll-rmem.conf /etc/sysctl.d/ && sudo sysctl --system   # 32 MB rmem_max
sudo bash pc/wsl-gpu-setup.sh                                                       # WSL: force D3D12 GL driver
```

---

## 4. Configure  (`pc/atoll.conf`)

`atoll.conf` is the single source of truth. Edit at least these host-specific keys:

- **Identity:** `ATOLL_USER` (your WSL username), `PI_USER`, `PI_HOST`.
- **Island:** `ISLAND_PC_IP=10.10.10.2`, `ISLAND_PI_IP=10.10.10.1`, `ISLAND_PI2_IP=10.10.10.3`.
- **Management / advertise:** `PC_WIFI_IP`, and **`NMOS_ADVERTISE_HOST`** = your PC's **LAN** address
  (not the island one — controllers on WiFi can't reach `10.10.10.x`).
- **PTP:** `PTP_GMID` = the Pi 5's grandmaster EUI-64 (blank ⇒ "traceable").
- **Optional sources:** `HDHR_DEVICE_ID` (Live TV; self-heals by DeviceID), `MAC_MUSIC_HOST` (music).
- **Media paths:** `MEDIA_ROOT`, `MUSIC_ROOT`, `REELS_LOOP`, `BBB_FILE`, `HOME_PLAYLIST`.
- Multicast **groups/ports** are pre-set and must stay unique — and must **match** `pi/atoll-pi.conf`
  for the Pi feeds (`PI_AUDIO_GRP`, `PI_RAW_GRP`) or the Pi won't appear in the multiview.

---

## 5. Install the services  ⚙ manual (not scripted)

The `deploy/systemd/` units hardcode user `david` and `/home/david/pi-nmos-st2110`. Rewrite them for
your user/path, install, and enable:

```bash
for u in deploy/systemd/atoll-*.service; do
  sed "s#/home/david/pi-nmos-st2110#$HOME/pi-nmos-st2110#g; s/\bdavid\b/$USER/g" "$u" \
    | sudo tee "/etc/systemd/system/$(basename "$u")" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable atoll-*.service
# Not on the PC / superseded — leave disabled:
sudo systemctl disable atoll-pi.service atoll-hevc.service atoll-jxs.service atoll-music-ph.service
# Optional components you're skipping (see §7), e.g.:
#   sudo systemctl disable atoll-tv.service atoll-tv-web.service atoll-cc-relay.service atoll-follower*.service
```

`STARTUP.md` lists the full service inventory and which are intentionally disabled.

---

## 6. Set up the Pi(s)

**Grandmaster (Pi 5):**
```bash
sudo apt-get install -y linuxptp gstreamer1.0-tools \
     gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad python3
```
Copy `pi/atoll-pi.conf`, `pi/launch-all.sh`, `pi/master-clock-web.py` to the Pi user's home, edit
`atoll-pi.conf` (`PI_USER`, `ISLAND_IFACE`, and the multicast groups — **must match** `pc/atoll.conf`),
give it static `10.10.10.1`, and install/enable an `atoll-pi` unit that runs `launch-all.sh`. On boot it
waits for NTP, starts `ptp4l -i eth0 -S` (grandmaster), then the L24 audio and raw-video senders and the
web clock on `:8000`.

**Follower (optional 2nd Pi):** `sudo apt-get install -y linuxptp`; copy `pi/follow-all.sh`,
`pi/follower-ptp.cfg`, `pi/follower-clock-web.py` + the two `atoll-follower*.service` units; static
`10.10.10.3`. It runs `ptp4l -f follower-ptp.cfg -i eth0 -m` (slave-only, **hybrid E2E**) — hybrid E2E
matters because the island switch snoops IGMP without a querier.

---

## 7. First bring-up & verify

1. Open a WSL terminal → systemd starts the NMOS docker stack and every enabled `atoll-*` service.
   (If the docker stack didn't come up: `cd deploy/nmos && sudo docker compose start`.)
2. Confirm the island NIC still holds `10.10.10.2` and the Pi is reachable:
   `ping 10.10.10.1 && curl -s http://10.10.10.1:8000/time` (expect `state: MASTER`).
3. **The render wall is a manual, local step** (needs the WSLg display — never over SSH):
   ```bash
   cd pc && bash output-render.sh 2      # "2" = second monitor
   ```
4. Open the control panel: **`http://<PC-LAN-IP>:8096`** from the iPad (or `http://localhost:8096` on
   the PC itself — mirrored networking can't hairpin the external IP). Take a source; the wall follows.

---

## 8. Optional components — how to skip

- **2nd Pi follower:** don't deploy the `atoll-follower*` units.
- **Live TV / captions:** leave `atoll-tv`, `atoll-tv-web`, `atoll-cc-relay` disabled.
- **Music:** enable `atoll-music-ph` (placeholder) instead of `atoll-music`/`atoll-music-nmos`.
- **JPEG XS:** skip the `atoll-jxs*` units and the SVT-JPEG-XS/FFmpeg source builds (`svt-ffmpeg-build.sh`,
  `jxs-install-libs.sh`).
- **IS-10 auth:** only enforced when `~/atoll-run/auth-enable` exists — off by default.
- **AMWA testing container:** conformance only; drop that service from `deploy/nmos/docker-compose.yml`
  if you don't want it.

---

## 9. (Optional) Run the conformance suites

Atoll passes every applicable test across all eight AMWA NMOS suites. To reproduce:
```bash
bash pc/conformance-run.sh        # runs the AMWA nmos-testing suites, tallies pass/fail
```
The custom checks `pc/is10-conformance.py` and `pc/is12-conformance.py` cover the two suites whose AMWA
runners aren't available headless here (IS-10 disabled upstream; IS-12 needs the interactive facade).

---

## Troubleshooting

Common issues (mirrored-networking wedges, packet-rate ceiling, PTP-on-WSL being display-only, port
collisions, one-sender-per-group) are documented in **[`STARTUP.md`](STARTUP.md)** under *Notes / gotchas*
and *Island connectivity / PTP lost after a Windows or WSL restart*.
