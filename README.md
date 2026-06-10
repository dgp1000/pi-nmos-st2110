# Atoll

**An iPad-controlled ST 2110 / NMOS broadcast-over-IP multiviewer and monitoring rig.**

Atoll turns a PC with an NVIDIA GPU into a small broadcast-IP monitoring island: a layout-switchable
**multiviewer** on a dedicated monitor, an **IS-05 source switch + IS-04/05 inspector** you drive from
an iPad, real **PTP timecode** from a Raspberry Pi grandmaster, and a handful of live **channels** —
SMPTE ST 2110 video/audio from the Pi, HEVC playout, home videos, a "Now Playing" music feed bridged
from a Mac, and a looping reels channel — every one discoverable over NMOS.

It started as a home lab for learning ST 2110 / NMOS end to end. Everything is config-driven, so with
the right hardware you can re-home it to your own network by editing two files.

---

## What it does

- **2×2 multiviewer** on a second monitor — **any source in any tile**, reassigned live from the iPad
  ("pick a tile, then a source"). Also single-source "follow take" and side-by-side layouts.
- **IS-05 switch panel** — tap a source to issue a real IS-05 take; the native output follows.
- **IS-04/05 inspector** — browse nodes / senders / receivers, their flows, caps, transport params and
  subscriptions, live from the registry Query API and the node Connection API.
- **PTP timecode** overlaid on the wall, relayed from the Pi grandmaster.
- **Audio that follows the selected source**, lip-synced where the source carries its own audio.
- **A discoverable NMOS sender** registered for the reels channel (source → flow → sender + SDP).

### Channels

| Channel        | Transport                         | Group : port        | Source                          |
|----------------|-----------------------------------|---------------------|---------------------------------|
| Pi raw video   | SMPTE **ST 2110-20** (RTP)        | `239.10.10.20:5005` | Raspberry Pi 5                  |
| Pi audio       | SMPTE **ST 2110-30** L24 (RTP)    | `239.10.10.10:5004` | Raspberry Pi 5                  |
| PC HEVC 4K     | HEVC over MPEG-TS                  | `239.10.10.65:5010` | PC playout (Big Buck Bunny)     |
| Home videos    | HEVC over MPEG-TS                  | `239.10.10.22:5008` | PC playlist                     |
| Music          | HEVC over MPEG-TS                  | `239.10.10.30:5012` | Mac "Now Playing", bridged      |
| Test Reels     | HEVC over MPEG-TS                  | `239.10.10.31:5014` | PC reels loop (NMOS-registered) |

---

## Architecture

```
        iPad / browser                         Monitor 2 (the wall)
       (control + inspect)                     2×2 multiview · side · single
              │  http :8096                              ▲  waylandsink
              ▼                                          │
   ┌──────────────────────────────────────────────────────────────┐
   │  PC + WSL2  (NVIDIA GPU: NVENC / NVDEC)                        │
   │    monitor-web.py     panel + IS-04/05 inspector  :8096        │
   │    output-render.sh   multiview / side / single renderer       │
   │    media-send.sh ×N   HEVC playout senders                     │
   │    music-send.sh      bridges the Mac feed onto the island     │
   │    reels-nmos.py      registers the Test Reels NMOS sender      │
   │    docker:  nmos-registry :8080 · virtnode :8090 · testing :5000│
   └───────────────────────────────┬──────────────────────────────┘
                                    │  wired ST 2110 "island"
                                    │  (managed switch, IGMP snooping)
                   ┌────────────────┴───────────────┐
                   ▼                                 ▼
        Raspberry Pi 5  (PTP grandmaster)      Mac  (optional)
        ST 2110-20 / -30 generators           "Now Playing" .ts over WiFi
        PTP web clock :8000
```

The PC and Pi sit on a dedicated wired **island** (e.g. `10.10.10.0/24`) carrying the multicast media;
the iPad and the optional Mac reach the PC over the regular management/WiFi network.

---

## Hardware you need

**Required**
- A PC with an **NVIDIA GPU** (NVENC/NVDEC — GeForce RTX class or better). Everything is HEVC.
- A **managed Ethernet switch with IGMP snooping**, plus a wired NIC on the PC for the island.
- A browser device for control (the panel is tuned for an iPad, but any browser works).

**Operating system** — Windows 10/11 with **WSL2** (the reference setup), or a **Debian/Ubuntu**
Linux box (the scripts are Linux; the WSL-specific bits are noted below).

**Optional**
- A **Raspberry Pi 5** as the ST 2110 source + PTP grandmaster (`pi/`).
- A **Mac** running a "Now Playing" `.ts` server for the music channel.

---

## Quick start

```bash
git clone <this-repo> pi-nmos-st2110
cd pi-nmos-st2110

# 1. Install deps + bring up the NMOS stack (Debian/Ubuntu, WSL2 or native).
bash install.sh                 # or:  bash install.sh --check   (checks only, no changes)

# 2. Configure for your environment.
$EDITOR pc/atoll.conf           # user, island subnet, the 6 multicast groups, media paths

# 3. Give the wired NIC the island IP (matches ISLAND_PC_IP), e.g.:
sudo ip addr add 10.10.10.2/24 dev eth0      # Linux
#   New-NetIPAddress -InterfaceAlias Ethernet -IPAddress 10.10.10.2 -PrefixLength 24   (Windows admin)

# 4. Start it.
bash pc/monitor-run.sh          # the panel (:8096)
bash pc/launch-media.sh         # the senders + the monitor-2 multiview

# On Windows/WSL you can do steps 1-4 in one shot after a reboot:
#   powershell -ExecutionPolicy Bypass -File pc/restore.ps1
```

Open the panel at `http://<this-host>:8096` (from the iPad, the PC's WiFi IP). On the Pi:
deploy `pi/` and run `sudo bash ~/launch-all.sh` (with `pi/atoll-pi.conf` alongside).

---

## Configuration

All environment-specific values live in **two files** — no IPs, groups, paths, or usernames are
hardcoded anywhere else:

- **`pc/atoll.conf`** — the PC/host config. Identity (`ATOLL_USER`), the island IP +
  auto-detected interface, the six multicast groups, media paths, ports, and the WSL GPU setting.
  Sourced by the bash scripts; read by Python via `pc/atoll_config.py` (which sources it in bash once
  and caches); read by `restore.ps1` (which parses it natively on Windows).
- **`pi/atoll-pi.conf`** — the Pi config. Its ST 2110-20/-30 groups **must match** `atoll.conf`'s
  `PI_*` values. Deploy it alongside `pi/launch-all.sh`.

Re-homing Atoll to your network is: edit those two files. See `pc/atoll.conf` for the full surface.

---

## Using the panel

- **Sources** — tap **Home / Pi raw / PC HEVC / Music / Test Reels** to take that source (the output
  follows, and so does the audio).
- **Output · Mon 2** — **Follow take** (single fullscreen), **Side × 2**, or **Multiview** (2×2).
- **Multiview tiles** — tap a tile in the 2×2 grid, then tap a source to drop it into that quadrant.
  Any source in any tile, live.
- **Music** — transport controls (⏮ ⏯ ⏭ 🔀) + live title/artist, proxied to the Mac's control API.
- **Inspector** — tap any IS-04/05 row to drill into its full JSON (flows, caps, transport, subs).

---

## Repository layout

```
install.sh            one-shot host bootstrap (deps, GPU/plugin checks, NMOS stack, guidance)
pc/
  atoll.conf          ← the PC config (edit this)
  atoll_config.py     Python reader for atoll.conf
  monitor-web.py      iPad panel: IS-05 switch + IS-04/05 inspector + PTP clock (:8096)
  output-render.sh    the monitor-2 renderer (multiview / side / single)
  media-send.sh       HEVC playout sender (--hevc | --jxs | --reels)
  music-send.sh       bridges the Mac "Now Playing" feed onto the island
  reels-nmos.py       registers the Test Reels NMOS sender (+ serves its SDP)
  launch-media.sh     brings up all senders + the renderer
  restore.ps1         Windows/WSL one-shot boot
  build-reels-loop.ps1  concatenate reels clips into one continuous loop
  hevc-/jxs-stream-*  standalone HEVC and JPEG-XS streaming demos
pi/
  atoll-pi.conf       ← the Pi config (edit this)
  launch-all.sh       Pi ST 2110-20/-30 generators + PTP grandmaster + web clock
  master-clock-web.py PTP web clock (:8000)
deploy/nmos/          the NMOS stack: docker-compose + registry.json + node.json
docs/                 design notes / project state
```

---

## Platform notes

The reference rig runs on **Windows + WSL2** (mirrored networking, WSLg for the GPU display, the
`d3d12` Mesa driver). Those specifics — the `restore.ps1` boot, the `/mnt/c` 9p handling, the GPU
driver export — are isolated and noted in the code. A **Linux-native** deployment (a box on the island
with an NVIDIA GPU) removes them entirely and is the natural target for an appliance; that port is the
main remaining work toward a turnkey release.

---

## Credits & licence

Built on excellent open infrastructure:
- **[nmos-cpp](https://github.com/sony/nmos-cpp)** / **[easy-nmos](https://github.com/rhastie/easy-nmos)** — the IS-04/05 registry + virtual node.
- **[AMWA NMOS Testing Tool](https://github.com/AMWA-TV/nmos-testing)** — conformance testing.
- **[GStreamer](https://gstreamer.freedesktop.org/)** + **[FFmpeg](https://ffmpeg.org/)** — the media pipeline.
- **Big Buck Bunny** (© Blender Foundation, CC-BY 3.0) — the HEVC demo clip.

Add a `LICENSE` of your choice before publishing. NMOS is an [AMWA](https://www.amwa.tv/) family of specs;
SMPTE ST 2110 is a SMPTE standard.
