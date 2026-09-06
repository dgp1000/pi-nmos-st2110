# Atoll — system architecture

A module-by-module walk through the code: what each program is, what it does, and how it
connects to the others. The diagrams are Mermaid, so GitHub renders them inline. For *operating*
the rig see [STARTUP.md](../STARTUP.md); for *what* it is see the [README](../README.md).

Every value quoted here (group, port, timer) is read from the code as of this writing. Where a
number lives in config it says so; the config files are `pc/atoll.conf` and `pi/atoll-pi.conf`.

---

## 1. The whole system on one page

Atoll is five layers. Everything in the code sits in exactly one of them.

```mermaid
flowchart TB
  subgraph UI["CONTROL SURFACES (browser)"]
    ipad["iPad / any browser<br/>panel :8096 · analyser :8101<br/>tv picker :8098 · browser multiview :8099"]
  end

  subgraph CTRL["CONTROL PLANE (PC / WSL)"]
    panel["monitor-web.py<br/>IS-05 panel · IS-04/05 inspector · knobs<br/>:8096"]
    statefiles[("~/atoll-run/*<br/>tv-channel · fec-loss · fec-enable<br/>sps-a · sps-b · audio-delay-ms · tv-audio-delay-ms · programout")]
    is07["is07-tally.py<br/>IS-07 Event and Tally<br/>REST :8102 · WebSocket :8103"]
    nmos["docker: nmos-cpp<br/>registry :8080 · virtnode :8090<br/>AMWA testing :5000"]
    analyser["analyser.py<br/>flow analyser :8101"]
    progout["program-out.py<br/>Program Out receiver<br/>IS-05 Connection API :8092"]
  end

  subgraph SEND["SENDERS (systemd, PC / WSL)"]
    tv["tv-send-inputselect.py<br/>HDHomeRun → HEVC/TS"]
    media["media-send.sh<br/>playlist → HEVC/TS"]
    music["music-channel.sh + music-nmos.py<br/>Mac Now-Playing → HEVC video + L24 audio<br/>NMOS source"]
    rtp["h264 · opus · mjpeg · vp9 · j2k<br/>essence over RTP"]
    tsrtp["tsrtp · fec · sps<br/>TS over RTP + 2022-1 / 2022-7"]
    anc["anc-send.py<br/>ST 2110-40 ATC"]
  end

  subgraph ISLAND["THE ISLAND — 10.10.10.0/24 multicast"]
    groups[("239.10.10.x groups<br/>16 flows, ~12k pps")]
  end

  subgraph PI["RASPBERRY PIs — the island clock"]
    pi["Pi 5 · launch-all.sh<br/>ptp4l grandmaster<br/>ST 2110-30 L24 · ST 2110-20 raw<br/>master-clock-web.py :8000"]
    pifollow["Pi 2 · atoll-follower<br/>ptp4l -s hybrid E2E<br/>web readout :8000"]
    pifollow -. PTP lock .-> pi
  end

  subgraph RX["RECEIVERS / RENDERERS (PC / WSL)"]
    render["output-render.sh<br/>the loop that follows the panel"]
    wall["wall-view.py<br/>2x2 + meters + IS-07 tally"]
    meter["meter-view.py<br/>single + VU + info"]
    gstmulti["gst-launch multi / side<br/>built inline by output-render"]
    mjpeg["multiview-web.py + multiview-mjpeg.sh<br/>browser 2x2 :8099"]
  end

  monitor["Monitor 2 (WSLg glimagesink, GL/EGL)"]

  ipad -- HTTP --> panel
  ipad -- HTTP --> analyser
  panel -- "IS-05 PATCH /staged" --> nmos
  panel -- "IS-04 Query · IS-05 active" --> nmos
  panel -- writes --> statefiles
  panel -- "IS-05 PATCH /staged (route)" --> progout
  progout -- "IS-04 register" --> nmos
  progout -- writes --> statefiles
  panel -. "/state polled" .-> is07
  panel -. "/state polled 1 s" .-> render
  is07 -- "IS-04 registration + heartbeat" --> nmos
  is07 -- "ws push" --> wall
  is07 -- "ws push" --> analyser
  statefiles -. "polled 0.5-1 s" .-> tv
  statefiles -. "polled 1 s" .-> wall
  statefiles -. "polled 1 s" .-> meter
  statefiles -. "polled 1 s" .-> render

  tv --> groups
  media --> groups
  music --> groups
  rtp --> groups
  tsrtp --> groups
  anc --> groups
  pi --> groups

  groups --> wall
  groups --> meter
  groups --> gstmulti
  groups --> mjpeg
  groups --> analyser

  render -- "spawns one of" --> wall
  render -- "spawns one of" --> meter
  render -- "spawns one of" --> gstmulti
  wall --> monitor
  meter --> monitor
  gstmulti --> monitor
  mjpeg --> ipad
```

**How to read it.** Solid arrows carry media or make a request. Dotted arrows are *polls*: Atoll
deliberately couples its processes through the panel's `/state` endpoint and a handful of plain
text files, not through sockets between programs, so any one process can be restarted without the
others noticing. The two exceptions are IS-05 (the panel really does PATCH the NMOS node) and
IS-07 (tally really is pushed over WebSocket), because those are the standards the rig exists to
demonstrate.

---

## 2. Physical and network topology

```mermaid
flowchart LR
  subgraph HOME["Home network (WiFi / management)"]
    ipad2["iPad<br/>192.168.4.x"]
    mac["Mac<br/>DHCP :8008 (MAC_MUSIC_HOST)<br/>Now-Playing server"]
    hdhr["HDHomeRun FLEX 4K<br/>DHCP; found by DeviceID<br/>:5004"]
  end

  subgraph PC["PC — Windows 11 + WSL2 (mirrored networking), RTX 2080 Ti"]
    wifi["mgmt NIC<br/>192.168.4.85"]
    eth2["island NIC 'Ethernet 2'<br/>10.10.10.2 static"]
    wsl["WSL2 Ubuntu: systemd services · docker · WSLg display + Pulse"]
  end

  subgraph ISL["Island L2 — IGMP-snooping switch"]
    sw(("10.10.10.0/24<br/>multicast 239.10.10.x"))
  end

  subgraph RPI["Raspberry Pi 5 (pi5-nmos)"]
    pieth["eth0 10.10.10.1<br/>PTP grandmaster"]
  end

  subgraph RPI2["Raspberry Pi 2 (pi2-nmos · follower)"]
    pi2eth["eth0 10.10.10.3<br/>PTP follower (hybrid E2E)"]
  end

  mon2["Monitor 2 2560x1440<br/>composite 1920x1080 → GPU upscale 3840x2160<br/>→ glimagesink 2560x1440 (WSLg 1.5x DPI)"]

  ipad2 <--> wifi
  mac --> wifi
  hdhr --> wifi
  wifi --- wsl
  wsl --- eth2
  eth2 <--> sw
  sw <--> pieth
  sw <--> pi2eth
  wsl --> mon2
```

Two networks, one bridge. The PC is the only device on both: it *pulls* the Mac and HDHomeRun
streams over WiFi and *re-encodes* them onto the island. Nothing on the island is routable from
the home network, which is why `NMOS_ADVERTISE_HOST` in `atoll.conf` is the WiFi address, not the
island address (a controller on WiFi could never reach `10.10.10.2`).

The island's ceiling is **packet rate**, not bandwidth: WSL mirrored networking limits multicast
*receive* to roughly 12–15k packets/s across all groups. That single fact explains most of the
design choices below (720p everywhere, `mpegtsmux alignment=7` on every TS sender, the Pi staying
at 320x240, JPEG XS running as a local encode→decode rather than a network flow).

---

## 3. Process inventory

Every long-running program, who starts it, and where it lives. Enabled systemd units start when
WSL boots (WSL itself does not start with Windows). `~/atoll-run` is `ATOLL_RUN` in `atoll.conf`.

### Control plane

| Process | File | Owner | Listens | Role |
|---|---|---|---|---|
| Panel | `pc/monitor-web.py` | `atoll-panel` | `:8096` | The iPad page and the single source of truth for *active source*, *layout* and *tile slots*. Issues IS-05 takes and routes Program Out over IS-05. Writes the knob files. Proxies the Mac music API and the Pi clock. A **Guided demo** button runs a scripted, captioned tour of the whole rig. |
| IS-07 emitter | `pc/is07-tally.py` | `atoll-is07` | `:8102` REST, `:8103` ws | One boolean event source per Atoll source key. Registers node/device/13 sources/flows/senders in IS-04. Pushes state on transition. |
| Program Out | `pc/program-out.py` | `atoll-programout` | `:8092` | Software NMOS receiver: serves the IS-05 v1.1 Connection API and registers its node/device/receiver in IS-04. On activation it maps the connection's multicast/port to an island flow and writes `~/atoll-run/programout` for the renderer. |
| Music NMOS source | `pc/music-nmos.py` | `atoll-music-nmos` | `:8093` | Registers the music channel as an IS-04 node with two senders — video (HEVC) and ST 2110-30 L24 audio — and serves an SDP per sender, so music is discoverable in the inspector and routable via Program Out. Heartbeats like the other registrars. |
| Flow analyser | `pc/analyser.py` | `atoll-analyser` | `:8101` | Raw-socket join of every group: pps, bitrate, average datagram, RTP pt/SSRC/loss. IS-07 receiver (tally column + event log). |
| NMOS registry | `deploy/nmos/docker-compose.yml` → `nmos-registry` | docker | `:8080` HTTP, `:8081` ws, `:1883` MQTT | nmos-cpp IS-04 Registration + Query API, and the IS-09 System API (`/x-nmos/system/v1.0/global`) the Atoll nodes discover. |
| NMOS virtual node | `nmos-virtnode` | docker | `:8090` HTTP, `:8091` ws | nmos-cpp example node: the receivers `v0`/`m0` the panel switches, plus its own IS-07 sources. |
| AMWA testing tool | `nmos-testing` | docker | `:5000` | Conformance tester. IS-04/05/07 pass; **IS-09-01** (System API server) and **IS-09-02** (multicast discovery — test_01/03/04) now pass against Atoll's stack (see §8 for the results + method). |
| TV picker (standalone) | `pc/tv-web.py` | `atoll-tv-web` | `:8098` | Channel grid that writes `tv-channel`. Superseded by the panel's built-in remote; kept running. |

### Senders

| Source key | Service | File | Encode | Transport | Group : port |
|---|---|---|---|---|---|
| `hevc` "Live TV" | `atoll-tv` | `pc/tv-send-inputselect.py` | NVENC HEVC 6 Mb/s + AAC 5.1 384 kb/s | MPEG-TS / UDP | `239.10.10.65:5010` |
| `jxs` "Home videos" | `atoll-home` | `pc/media-send.sh --jxs ~/atoll-playlist` | NVENC HEVC 12 Mb/s + MP3 192 | MPEG-TS / UDP | `239.10.10.22:5008` |
| `music` | `atoll-music` | `pc/music-channel.sh` | NVENC HEVC 6 Mb/s, video-only (or a 4 Mb/s placeholder card) | MPEG-TS / UDP | `239.10.10.30:5012` |
| *(audio for `music`)* | `atoll-music` | `pc/music-channel.sh` | L24 48 kHz stereo, 1 ms ptime (pt 96) | ST 2110-30 RTP | `239.10.10.32:5013` |
| `reels` "Test Reels" | *(none — only `launch-media.sh`)* | `pc/media-send.sh --reels` | NVENC HEVC + MP3 | MPEG-TS / UDP | `239.10.10.31:5014` |
| `raw` "Pi raw 2110-20" | `atoll-pi` (on the Pi) | `pi/launch-all.sh` | none — UYVY 320x240 59.94 | ST 2110-20 RTP (RFC 4175) | `239.10.10.21:5006` |
| *(audio for `raw`)* | `atoll-pi` | `pi/launch-all.sh` | none — L24 48 kHz stereo, 1 ms ptime | ST 2110-30 RTP | `239.10.10.10:5004` |
| *(ancillary)* | `atoll-anc` | `pc/anc-send.py` | ATC timecode, ST 291 words | ST 2110-40 RTP (RFC 8331), pt 100 | `239.10.10.50:5020` |
| `j2k` | `atoll-j2k` | `pc/j2k-send.sh` | `avenc_jpeg2000` | J2K RTP (RFC 5371) | `239.10.10.70:5016` |
| `h264` | `atoll-h264` | `pc/h264-send.sh` | NVENC H.264 4 Mb/s | RTP (RFC 6184), pt 96 | `239.10.10.75:5018` |
| *(audio for `h264`)* | `atoll-opus` | `pc/opus-send.sh` | Opus 96 kb/s | RTP (RFC 7587), pt 97 | `239.10.10.80:5022` |
| `mjpeg` | `atoll-mjpeg` | `pc/mjpeg-send.sh` | `jpegenc quality=60` | RTP (RFC 2435) | `239.10.10.85:5024` |
| `vp9` | `atoll-vp9` | `pc/vp9-send.sh` | `vp9enc` CPU realtime | RTP (RFC 7741) | `239.10.10.90:5026` |
| `tsrtp` | `atoll-tsrtp` | `pc/tsrtp-send.sh` | x264 CPU + AAC | MPEG-TS over RTP (ST 2022-2), pt 33 | `239.10.10.95:5028` |
| `fec` | `atoll-fec` | `pc/fec-send.sh` | x264 all-intra + AAC | TS/RTP + ST 2022-1 column/row FEC | `239.10.10.100:5040 / 5042 / 5044` |
| `sps` | `atoll-sps` | `pc/sps-send.sh` | x264 + AAC, one encoder | TS/RTP duplicated after payloader (ST 2022-7) | A `239.10.10.105:5046`, B `239.10.10.106:5048` |
| `jpegxs` | `atoll-jxs-web` (viewer only) | `pc/jxs-web.py` | SVT JPEG XS local enc→dec | none — MJPEG to browser `:8100` | *(island sender `atoll-jxs` is disabled)* |

Disabled on purpose, still in the repo: `atoll-hevc` (Big Buck Bunny clip on 5010, replaced by
Live TV), `atoll-jxs` (JPEG XS over TS at ~100 Mb/s, exceeds the WSL ceiling), `atoll-music-ph`
(placeholder card, now owned by `music-channel.sh`).

### Renderers

| Process | File | Owner | Output | Role |
|---|---|---|---|---|
| Renderer loop | `pc/output-render.sh` | **manual, local WSL terminal** | spawns the three below | Polls the panel once a second, builds a pipeline key from layout+source+slots, relaunches a child renderer only when the key changes. Also runs the audio follower. |
| Wall | `pc/wall-view.py` | child of output-render | WSLg window, monitor 2 | The instrumented 2x2: cairo overlay with tally border, per-tile bitrate/fps/audio meters, FEC counters. IS-07 receiver. Presents via **glimagesink** (GL/EGL, vsync-paced), GPU-upscaled to fill monitor 2. |
| Single view | `pc/meter-view.py` | child of output-render | WSLg window | Fullscreen active source with VU meters and a stream-info panel. Hosts the live FEC/2022-7 knobs for single view. Live TV (`hevc`) presents via **glimagesink** (GL/EGL); other sources via `waylandsink`. |
| gst-launch multi / side | inline strings in `output-render.sh` | child of output-render | WSLg window | The original 2x2 and side-by-side, kept for comparison with the wall. Both present via **glimagesink** (GL/EGL), GPU-upscaled to fill monitor 2. |
| Browser multiview | `pc/multiview-web.py` + `pc/multiview-mjpeg.sh` | `atoll-multiview` | MJPEG `:8099` | Same compositor topology, JPEG frames over HTTP instead of a window. Built when WSLg could not show a window. |
| JPEG XS viewer | `pc/jxs-web.py` | `atoll-jxs-web` | MJPEG `:8100` | Local svtjpegxsenc→svtjpegxsdec, proof that the ST 2110-22 codec works here. |

### Pi

| Process | File | Owner | Role |
|---|---|---|---|
| `ptp4l -i eth0 -S` | `pi/launch-all.sh` | `atoll-pi` | PTP grandmaster for the island. |
| gst L24 sender | `pi/launch-all.sh` | `atoll-pi` | 440 Hz sine → `rtpL24pay` 1 ms ptime → `239.10.10.10:5004`. |
| gst raw sender | `pi/launch-all.sh` | `atoll-pi` | `videotestsrc` UYVY 320x240 59.94 → `rtpvrawpay` → `239.10.10.21:5006`. |
| Web clock | `pi/master-clock-web.py` | `atoll-pi` | `:8000` page + `/time` JSON, PTP status via `pmc`. The panel proxies `/time` for its timecode. |
| `ptp4l -s` follower (hybrid E2E) | `pi/follower-ptp.cfg` · `atoll-follower.service` | `atoll-pi` (Pi 2 · `10.10.10.3`) | 2nd-Pi **PTP follower** — locks its clock to the grandmaster. Hybrid E2E (unicast `Delay_Req`); software timestamping; NTP off so PTP owns the clock. |
| Follower web readout | `pi/follower-clock-web.py` · `atoll-follower-web.service` | `atoll-pi` (Pi 2) | `:8000` — live offset-from-master, servo state (`LISTENING`→`UNCALIBRATED`→`SLAVE`), GM identity, convergence sparkline. |

**PTP follower demo (2nd Pi).** A second Pi (a Pi 2B here — any Pi works) joins the island at
`10.10.10.3` and locks its clock to the Pi 5 grandmaster, demonstrating PTP between two nodes. The
one wrinkle: the island switch does **IGMP snooping with no querier**, so the grandmaster's *receive*
membership ages out and it silently ignores multicast `Delay_Req` — the follower reaches
`UNCALIBRATED` with `rx_Delay_Resp = 0` and never measures path delay. **Hybrid E2E** (unicast
`Delay_Req` straight to the master) sidesteps this with no grandmaster change. The Pi 2B has no PTP
hardware clock and a USB-attached NIC, so software-timestamping convergence sits around **±1–3 ms**
(a Pi 4's native NIC would be far tighter; a Pi 5's PHC tighter still). Both `atoll-follower*`
services are enabled for boot and the static IP persists via netplan, so the follower is fully
**autonomous** — verified re-locking unattended across both its own and the grandmaster's reboots.
`pi/launch-all.sh` now waits for NTP sync before starting `ptp4l`, so the grandmaster never anchors
to a stale boot-time clock and serves the wrong time to the rig.

---

## 4. Configuration: one file, three readers

```mermaid
flowchart LR
  conf["pc/atoll.conf<br/>bash, KEY=value + a little logic<br/>platform detect · island NIC by IP · groups · ports · paths"]
  piconf["pi/atoll-pi.conf<br/>PI_AUDIO_* / PI_RAW_* MUST match"]

  conf -- "source" --> bash["every *.sh sender + output-render.sh"]
  conf -- "atoll_config.py<br/>(sources once in bash, caches)" --> py1["monitor-web.py · reels-nmos.py"]
  conf -- "NEED list: bash sources it, echoes each key" --> py2["wall-view · meter-view · analyser<br/>is07-tally · tv-send-inputselect · fecverify · program-out"]
  conf -- "regex parse on Windows" --> ps["restore.ps1"]
  piconf -- source --> pi["pi/launch-all.sh"]
```

`atoll.conf` is the only place a group, port, IP, username or path is written. Three details in
it shape everything else:

- **Platform branch.** `ATOLL_PLATFORM` is set from `/proc/version`. On WSL it exports
  `GALLIUM_DRIVER=d3d12`, `PULSE_SERVER`, `XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir` and
  `WAYLAND_DISPLAY` so GStreamer sinks find WSLg even under systemd (whose default runtime dir is
  wrong for WSLg).
- **Island NIC by IP.** WSL renames `eth0`/`eth1` across boots, so `ISLAND_IFACE` is resolved by
  finding whichever interface holds `ISLAND_PC_IP`. `pc/island_iface.py` does the same for the two
  legacy Python tools that predate the config file.
- **Python does not re-parse shell.** The Python programs shell out once, `source` the file, and
  echo the keys they need. That is why a new config key has to be added to a program's `NEED`
  list (or `atoll_config._VARS`) before it can read it.
- **The HDHomeRun is found by DeviceID, not IP.** It is on DHCP and its address drifts (a reboot
  moved it `192.168.7.88` → `192.168.4.32`, 4 Sep 2026). `pc/hdhr.py` resolves `HDHR_DEVICE_ID`
  to the current IP over the HDHomeRun UDP discovery broadcast; the sender and the panel call it at
  startup and re-call it when a tune or lineup fetch fails, so a DHCP move self-heals. `HDHR_HOST`
  is only the fallback used when discovery gets no answer.

---

## 5. The control plane in detail

### 5.1 `monitor-web.py` — the panel

One Python process, one thread per request, no framework. It holds three pieces of in-memory
state and exposes them as JSON:

| State | Set by | Read by |
|---|---|---|
| `_active["src"]` | `GET /take?src=` | `GET /state` → `output-render.sh`, `is07-tally.py` |
| `_output["layout"]` (`single`/`side`/`multi`/`wall`/`program`) | `GET /layout?mode=` | `GET /state` |
| `_slots[4]` (source key per quadrant) | `GET /slot?pos=&src=` | `GET /state` |

`take(src)` is the IS-05 part. `SOURCES` maps two keys to real NMOS receivers on the virtual node
(`raw` → `easy-nmos-node/receiver/v0`, `jxs` → `receiver/m0`); every other key has `label: None`.
A take resolves each labelled receiver by label (so node restarts that regenerate UUIDs do not
matter), then PATCHes `/x-nmos/connection/v1.1/single/receivers/<id>/staged` with
`master_enable` true for the taken one and false for the rest, `activate_immediate`. Taking a
non-NMOS source simply disables both. `active_src()` trusts the last take for 2 s
(`_ACTIVE_TTL`), then re-derives it from the receivers' `/active` state, so an external IS-05
controller changing `v0`/`m0` shows up on the panel too.

The rest of the routes are thin:

- `/nmos` builds the inspector view: IS-04 Query API (`:8080`) for nodes/devices/senders/
  receivers/flows, joined with each sender's and receiver's IS-05 `/active` from the node
  (`:8090`), cached 3 s. `/resource?kind=&id=` returns one object in full, plus IS-05
  active/staged/constraints and, for senders, the SDP fetched via `localhost:8090` because the
  node's own `manifest_href` points at a docker-internal address the iPad cannot reach.
- `/time` proxies the Pi's `:8000/time`; falls back to local time so the clock never blanks.
- `/music/state|next|prev|playpause|shuffle` proxy the Mac's Now-Playing API so the iPad needs
  one origin and no CORS.
- `/tv/lineup`, `/tv/set`, `/tv/fav` read the HDHomeRun `lineup.json` and write the
  `tv-channel` / `tv-favorites` files.
- `/fec/state`, `/fec/set`, `/sps/state`, `/sps/set` read and write the four demo knob files.

The page itself (the `PAGE_TEMPLATE` string) polls `/state` every 5 s, `/nmos` every 6 s, music
and the FEC/2022-7 knobs every 4 s, and `/time` every 3 s. It is a triple-quoted Python string, so
a `\'` inside its JavaScript collapses to `'` and breaks the whole script; use `data-*`
attributes and JS-assigned handlers when adding controls.

Program Out is the real receiver-side connection. `program-out.py` is a software NMOS receiver
(node + device + receiver, registered in IS-04, heartbeat every 5 s) that serves the IS-05 v1.1
Connection API on `:8092`. The panel's Program Out row PATCHes its `/staged` with the chosen
flow's `transport_params` (multicast IP + port) and `activate_immediate`; on activation the
receiver looks the (address, port) up in its catalogue of island flows and writes
`essence addr port sender_id` to `~/atoll-run/programout`. The `program` output layout follows
that knob, so an IS-05 connection actually drives the picture — unlike the `v0`/`m0` gate, and
discoverable, so any NMOS controller can route it too.

### 5.2 The knob files in `~/atoll-run`

These are the rig's "GPIO": the panel writes them, long-running pipelines poll them and apply the
value to a live element property, so nothing rebuilds.

| File | Written by | Read by | Effect |
|---|---|---|---|
| `tv-channel` | panel `/tv/set`, `tv-web.py`, the TV sender itself on error | `tv-send-inputselect.py` (0.5 s), `wall-view`/`meter-view` (label) | Retune Live TV. |
| `tv-favorites` | panel `/tv/fav` | panel | Favourite channel list. |
| `programout` | `program-out.py` on IS-05 activation | `output-render.sh` (`program` layout) | The flow routed to Program Out: essence + multicast + port. |
| `fec-loss` | panel `/fec/set` | `meter-view`, `wall-view` (1 s) | `identity drop-probability` on the FEC media flow: the loss injector. |
| `fec-enable` | panel `/fec/set` | `meter-view`, `wall-view` | Gates the column/row FEC flows (drop-probability 0 or 1) so protected vs unprotected is a live A/B at constant loss. |
| `sps-a`, `sps-b` | panel `/sps/set` | `meter-view`, `wall-view` | "Pull the cable" on a 2022-7 path. |
| `audio-delay-ms` | hand-edited | `output-render.sh` (1 s) | Trim on the standalone audio follower (side/multi/wall). |
| `tv-audio-delay-ms` | hand-edited | `meter-view.py` (1 s) | Pad offset on single view's audio queue; positive delays audio. |

### 5.3 A take, end to end

```mermaid
sequenceDiagram
  autonumber
  participant B as iPad browser
  participant P as monitor-web.py
  participant N as nmos-virtnode
  participant T as is07-tally.py
  participant W as wall-view.py
  participant R as output-render.sh
  participant M as meter-view.py

  B->>P: GET /take?src=raw
  P->>N: PATCH .../receivers/v0/staged master_enable=true
  P->>N: PATCH .../receivers/m0/staged master_enable=false
  P-->>B: {"active":"raw"}
  Note over P: _active = raw, ts = now

  loop every 100 ms
    T->>P: GET /state
  end
  Note over T: jxs false, raw true — stamp TAI of the transition
  T-->>W: ws state {source_id(jxs), false}
  T-->>W: ws state {source_id(raw), true}
  Note over W: tally border moves, no rebuild

  loop every 1 s
    R->>P: GET /state
  end
  alt layout = single
    Note over R: key single:raw differs → kill child, spawn
    R->>M: python3 meter-view.py raw 2
  else layout = wall / multi / side
    Note over R: key unchanged → nothing rebuilt
    Note over R: audio follower relaunched for the new source
  end
```

Two independent consumers, two latencies. Tally lights within about one poll of the emitter (the
push itself is instant). The renderer takes up to a second to notice, and only rebuilds when the
*visible* pipeline must change: a take in `wall` or `multi` moves the red border but leaves the
video untouched.

### 5.4 A Live TV channel change

```mermaid
sequenceDiagram
  autonumber
  participant B as iPad browser
  participant P as monitor-web.py
  participant F as ~/atoll-run/tv-channel
  participant S as tv-send-inputselect.py
  participant H as HDHomeRun
  participant G as island 5010
  participant V as any receiver of 5010

  B->>P: GET /tv/set?ch=19.1
  P->>F: write "19.1"
  loop every 500 ms
    S->>F: read
  end
  Note over S: value differs from current → start 1.2 s debounce
  Note over S: still 19.1 after 1.2 s → change(): the OLD channel stays on air
  S->>H: GET /auto/v19.1 — a second HTTP stream, so the HDHomeRun opens a second tuner
  H-->>S: MPEG-2 / AC-3 transport stream (standby branch: souphttpsrc + decodebin3 + scaler chain, its own libsoup session)
  Note over S: standby's first video AND audio frame decoded → cut_to(): selectors switch old → new, LASTGOOD = 19.1
  S->>S: tear down the old branch (downstream → upstream) → its tuner is released
  Note over G: encoder + mux never stopped: 5010 is continuous
  V-->>V: old channel until the cut, then the new one — no black frame
  Note over S: bad standby (503 no free tuner, undecodable, not live after 12 s ×3) → dropped, old channel stays on air, channel file reverted
  Note over S: error on the on-air branch or the encoder tail → revert to LASTGOOD and exit 1 → systemd restarts
  Note over S: hang guard (own thread): main loop silent 20 s → log the element → exit 1 → systemd restarts on the requested channel
```

The whole point of `tv-send-inputselect.py` is that the encoder tail never stops. Channel changes
are make-before-break: the sender keeps an *on-air* branch and, during a change, a *standby* branch
(each `souphttpsrc → decodebin3 → scaler chain → selector request pad`), so two of the HDHomeRun's
four tuners are busy for the second or two the change takes. Three things make the cut clean rather
than a hole on 5010: the standby is cut in only once it has decoded both a video and an audio frame
(the mux stalls the whole output if it is switched to a pad that has not produced yet); each source
gets its own libsoup session (a `gst.soup.session` context per branch — otherwise both channels
share one I/O thread and the new one is starved until the old is released); and the pipeline
latency is pinned (~2.5 s) with the mux and sink queues sized above it, so the clock-synced udpsink
is never throttling a live source. The cost is ~1.7 s more end-to-end Live TV delay than the old
build, which nothing on the island depends on; a sub-second residual gap at the instant of the cut
is absorbed by the receivers' jitter buffers. The black / silence fallback on `sink_0` is only ever
on air at start-up or if the on-air branch itself dies. Its pipeline is built once:

```mermaid
flowchart LR
  subgraph persistent["persistent tail — built once at startup"]
    vsel["input-selector vsel"] --> venc["cudaupload → nvh265enc CBR 6 Mb/s<br/>→ h265parse config-interval=-1"]
    asel["input-selector asel"] --> aenc["audioconvert → 6 ch 0x3f → avenc_aac 384 kb/s → aacparse"]
    venc --> mux["mpegtsmux alignment=7"]
    aenc --> mux
    mux --> udp["udpsink 239.10.10.65:5010"]
    black["videotestsrc black 720p59.94"] --> vsel
    silence["audiotestsrc silence 48k 6ch"] --> asel
  end
  subgraph source["source branch — rebuilt on every channel change"]
    soup["souphttpsrc HDHR /auto/vCH"] --> dec["decodebin3<br/>(nv*dec ranked NONE → CPU avdec)"]
    dec -- video --> vchain["queue → deinterlace → videorate → videoscale → videoconvert → NV12 720p59.94"] --> vsel
    dec -- audio --> achain["queue → audioconvert → audioresample → S16LE 48k 6ch"] --> asel
  end
```

Three details that are not obvious from the diagram:

- **PTS restamp probes** on the selector source pads rewrite every buffer's PTS (and DTS) onto
  pipeline running-time with one shared offset for video and audio. Broadcast PCR time is
  ~10^5 s while `nvh265enc` stamps DTS from running-time; the resulting DTS-after-PTS in the
  33-bit TS clock made hardware decoders drop frames. Fixing it *before* the encoder is the only
  place it sticks.
- **Decode on CPU, encode on GPU.** `GST_PLUGIN_FEATURE_RANK` demotes every `nv*dec` before
  `Gst.init` so a channel change never leaks an NVDEC session that the receiving tiles need.
- **Always 6-channel AAC.** A changing channel count is a discontinuity; stereo channels are
  upmixed into the 5.1 layout so the audio stream shape never changes mid-stream.

### 5.5 A demo knob

```mermaid
sequenceDiagram
  participant B as iPad
  participant P as monitor-web.py
  participant F as ~/atoll-run/fec-loss
  participant M as meter-view.py or wall-view.py
  B->>P: GET /fec/set?loss=0.05
  P->>F: write 0.0500
  loop every 1 s
    M->>F: read
    M->>M: identity name=lossy drop-probability = 0.05
  end
  Note over M: counters at wire / after injector / after fecdec / after jitterbuffer update the overlay
```

The same mechanism drives `fec-enable` (gates the two parity flows), `sps-a`/`sps-b` (gates a
2022-7 path) and the two audio trims. Because the change is a property write on a running element
the picture never blinks, which is what makes the "watch it fall apart and recover" demo work.

---

## 6. The senders

All senders are the same shape: a source, a conform to 720p30, an encoder, a packager, a
`udpsink` onto one multicast group with `ttl=1`, wrapped in `while true` so a crash restarts the
pipeline. They differ in the packager, and that is what puts them in three families.

```mermaid
flowchart LR
  subgraph A["Family 1 — MPEG-TS in bare UDP"]
    a1["Live TV · Home · Music · Reels"] --> a2["mpegtsmux alignment=7"] --> a3["udpsink"]
  end
  subgraph B["Family 2 — one essence per RTP flow"]
    b1["H.264 · VP9 · MJPEG · J2K · Opus · Pi raw · Pi L24 · Music L24 · ANC"] --> b2["rtpXpay"] --> b3["udpsink"]
  end
  subgraph C["Family 3 — MPEG-TS inside RTP (ST 2022-2)"]
    c1["tsrtp · fec · sps"] --> c2["mpegtsmux alignment=7 → rtpmp2tpay pt 33"]
    c2 --> c3["udpsink"]
    c2 --> c4["rtpst2022-1-fecenc ssrc=0<br/>→ media P · column P+2 · row P+4"]
    c2 --> c5["tee after payloader<br/>→ path A · path B"]
  end
```

**Family 1** is the original rig. Bare TS has no sequence numbers, so a receiver cannot tell
loss from lateness, but it is what every decoder understands. Every one of these sets
`alignment=7` (seven 188-byte TS packets per datagram); the analyser highlights any flow whose
average datagram is under 400 B because that is the signature of forgetting it, and it cost
~4,650 pps island-wide before it was found.

**Family 2** is what ST 2110 does: separate flows per essence. The H.264 and Opus feeds are a
pair (bars and tone), and the receivers join both groups in one pipeline. `config-interval=-1`
on both `h264parse` and `rtph264pay` repeats SPS/PPS with every IDR so a late joiner can decode.
`anc-send.py` is hand-built because GStreamer has no ancillary payloader: it emits one RFC 8331
packet per frame carrying an ATC LTC timecode packet (DID 0x60 / SDID 0x60) with ST 291 parity
words and checksum, marker bit set, 90 kHz clock.

**Family 3** is the demonstrator layer. Wrapping the TS in RTP is what makes ST 2022-1 FEC and
ST 2022-7 possible:

- `fec-send.sh` feeds the payloaded stream through `rtpst2022-1-fecenc` with a
  `FEC_COLUMNS x FEC_ROWS` matrix (5x5 from config, 40 % overhead) and sends three flows. The
  encoder requires `ssrc=0` on `rtpmp2tpay`. The video is all-intra (`key-int-max=1`) so the
  packet rate is high enough that a recovered packet, which can trail the live edge by a whole
  matrix, still lands inside the receiver's jitterbuffer window.
- `sps-send.sh` runs **one** encoder and `tee`s **after** `rtpmp2tpay ssrc=2022`, so both paths
  are bit-identical at the RTP layer. Two encoders would produce two streams no receiver could
  merge. The two paths are two groups on one L2, so fault isolation is simulated from the panel.

**The Live TV, Home and Music senders** need a word each. `media-send.sh` is a playlist: given a
directory it globs every video file, decodes with `decodebin3`, letterboxes to 720p30 NV12 and
loops forever; `atoll-home` points it at `~/atoll-playlist`, a folder of symlinks that
`launch-media.sh` builds from `MUSIC_ROOT`. `music-channel.sh` probes the Mac's `/state` every
cycle and runs either the live bridge (pull `nowplaying.ts` over WiFi, NVDEC → NVENC HEVC
video-only on 5012, plus AAC → ST 2110-30 **L24** (`rtpL24pay`, 1 ms ptime) on 5013) or a
"connecting" card for 15 s, so 5012 is never empty; an empty tile stalls the compositor.
`music-nmos.py` (`atoll-music-nmos`, `:8093`) then registers the channel as an NMOS source —
two senders (video + L24 audio) with an SDP each — so it is discoverable in the inspector and
routable via Program Out. `tv-send-inputselect.py` is section 5.4. The Mac Now-Playing host is reached at `MAC_MUSIC_HOST:8008` (`pc/atoll.conf`), a **hardcoded IP that drifts** with DHCP (it moved 192.168.6.159 → 192.168.4.51 mid-run, the same drift that moves the HDHomeRun); `.local`/mDNS does not resolve from WSL, so a **DHCP reservation** for the Mac mini (and the HDHomeRun) on the router is the durable fix — otherwise, when the Music tile shows "connecting", update `MAC_MUSIC_HOST` and restart `atoll-music`.

---

## 7. The renderers

### 7.1 `output-render.sh` — the loop

```mermaid
flowchart TD
  start(["bash output-render.sh 2"]) --> sweep["kill orphaned wall-view / meter-view by PID"]
  sweep --> poll["curl panel /state → active, layout, slots"]
  poll --> dead{"child pid dead?"}
  dead -- yes --> force["cur_key = force rebuild"]
  dead -- no --> key
  force --> key["key = single:active · side · multi:slots · wall:slots"]
  key --> changed{"key != cur_key?"}
  changed -- yes --> kill["kill child process group"] --> build["build_pipeline(layout, active, slots)"]
  build --> spawn["setsid bash -c cmd &<br/>+ move-window + snap-window-screen.ps1 on WSL"]
  changed -- no --> audio
  spawn --> audio["audio follower: akey = active unless single"]
  audio --> achanged{"akey or audio-delay-ms changed,<br/>or follower died?"}
  achanged -- yes --> aspawn["kill + spawn audio_cmd(active)"]
  achanged -- no --> sleep
  aspawn --> sleep["sleep 1"] --> poll
```

`build_pipeline` is a five-way case:

| layout | What runs | Rebuild key |
|---|---|---|
| `single` | `python3 meter-view.py <active> <screen>` | `single:<active>` — every take rebuilds |
| `program` | `python3 meter-view.py <routed> <screen>` (idle card if nothing connected) | `program:<routed>` — follows the Program Out IS-05 route, not the take |
| `side` | inline `gst-launch-1.0` compositor: Live TV left, Pi raw right | `side` — never rebuilds on take |
| `multi` | inline `gst-launch-1.0` compositor, four `tile_full()` fragments | `multi:<slots>` — slot changes rebuild, takes do not |
| `wall` | `python3 wall-view.py <slots> <screen>` | `wall:<slots>` — same |

`tile_full(src, idx, w, h)` is the one function to read if you want to know how a source key
becomes a decodable tile: a `case` over every key that emits the udpsrc → depay/demux → decode →
scale → `textoverlay` label → `queue leaky=downstream` → `mix.sink_<idx>` fragment, plus the audio
pad drain that TS sources need so an unlinked pad does not error the pipeline. The parser that
extracts `active`/`layout`/`slots` from the JSON is `sed` with `[a-z0-9,]*`, so source keys must
stay lowercase alphanumerics.

**Audio follows the source.** In `single` the child renderer plays its own lip-synced audio. In
every other layout the video pipeline drops audio, and `audio_cmd(active)` runs a separate
gst-launch that joins the *active* source's audio (the TS audio pad through `decodebin`, the Pi's
L24 flow, or the Opus flow for `h264`) and plays it with `autoaudiosink sync=true`, optionally
behind a `queue min-threshold-time` trim from `audio-delay-ms`. Switching sources swaps the
follower without touching video.

**Why it is manual.** The renderers draw to WSLg's Wayland surface, which exists only in a local
WSL terminal session. Launching over SSH gives audio and no window. Kill it by PID
(`/tmp/output-render.pid` is not written by this script; use `pgrep -af "[o]utput-render.sh"`),
never `pkill -f`, whose pattern matches the SSH command that launched it.

### 7.2 `wall-view.py` — the instrumented 2x2

```mermaid
flowchart LR
  subgraph pipe["ONE GstPipeline (Gst.parse_launch)"]
    t0["tile 0: udpsrc u0 → depay/demux → decode → scale → identity tap0 → queue 700ms leaky"] --> mix
    t1["tile 1 …"] --> mix
    t2["tile 2 …"] --> mix
    t3["tile 3 …"] --> mix["compositor mix<br/>4 quadrants"]
    mix --> ov["cairooverlay ov"] --> gl["glupload → glcolorscale 3840x2160"] --> sink["glimagesink sync=true"]
    a0["per tile: audio → decodebin → level lvli → fakesink"]
  end
  subgraph probes["pad probes (streaming threads, count only)"]
    p1["ui src → bytes → Mb/s"]
    p2["tapi src → frames → fps"]
    p3["FEC tile: u / flossy / fd / fjb → wire, after loss, after fecdec, after jitterbuffer, reorder"]
  end
  subgraph main["GLib main loop"]
    tick["tick() 1 s: Mb/s, fps, totals, tv-channel label"]
    knobs["apply_knobs() 1 s: fec-loss, fec-enable, sps-a, sps-b → identity drop-probability"]
    render["_render_overlay() 10 Hz: draw into the spare cairo surface, publish"]
    draw["on_draw(): blit only the overlay regions of the published surface"]
  end
  subgraph is07["IS-07 receiver thread (is07client)"]
    ws["ws://localhost:8103/.../devices/device_id<br/>subscribe source_id(key) per tile"] --> tally["st.tally[i] = value"]
    fb["poll_panel() 1 s — ONLY while disconnected"]
  end
  bus["bus: level messages → st.peak[i]"]
```

The wall exists because a gst-launch string cannot change after it starts. It keeps the
*identical* one-pipeline topology as `multi` (proven stable here), then adds everything a real
multiviewer draws live: a red tally border and "ON AIR / NMOS IS-07" flag on the taken tile, a
UMD label with Mb/s and fps (fps under 28 turns orange), per-channel audio meters from a `level`
element per tile, and for the FEC tile the counters `dropped / recovered / resid / reord`.

Four engineering choices to know about:

- **The present path is glimagesink, not waylandsink.** waylandsink presents through WSLg's SHM path (no dmabuf) — a per-frame CPU→GPU upload with uneven pacing that visibly *steps* fine scrolling content (a broadcast news ticker), even though the stream is clean (even PTS, 16.7 ms = 59.94 fps; the HDHomeRun app is smooth). glimagesink presents via OpenGL/EGL, vsync-paced — smooth, and it dropped the GPU 3D-engine load ~80% → ~60%. glimagesink cannot be resized under WSLg (it recreates its window), so the tail upscales to 3840×2160 on the GPU (`glupload ! glcolorscale`) and WSLg's 1.5× DPI makes that a 2560×1440 window filling monitor 2; the per-tile queues are 700 ms leaky (was 2-buffer) so the bursty Live TV delivery does not starve/step its tile.

- **The overlay is double-buffered and drawn off the streaming thread.** `on_draw` only blits
  regions (tally border, flag, bottom strip, ATOLL bug) from a surface the main loop finished
  10 Hz earlier. Painting a full ARGB surface per frame cost ~20 ms on its own.
- **Tally is a real IS-07 receiver.** Each tile subscribes to `source_id(key)` (UUID5 of the key,
  derived identically in the emitter). The panel is polled *only* while the WebSocket is down, and
  the flag then drops its "NMOS IS-07" line so the fallback is visible.
- **Only the FEC tile is software-decoded.** `nvh264dec` tears the ST 2022-1-recovered FEC stream on
  the live wall (torn bands from the recovered / reordered H.264). It is clean in an isolated decode
  and clean on `avdec_h264`, so it is a hardware-decode fault under real wall load, not a bad stream.
  The FEC tile alone therefore uses `avdec_h264` (~25 % of a core for one 720p 3 Mbps tile); every
  other H.264 tile (tsrtp, 2022-7, h264) stays on `nvh264dec`. The old blunt workaround
  `WALL_SW_DECODE=true` forced *all* H.264 tiles to software (~186 % CPU) and is off in production:
  software-decoding every tile oversubscribes the WSLg display and steps the GPU-decoded Live TV tile
  to keyframes. It stays as a diagnostic for isolating decode artifacts. (Same reason the default
  4-up is compressed tiles only -- the CPU-decoded Pi `raw` tile oversubscribes the wall too; see
  STARTUP.)

### 7.3 `meter-view.py` — single view

Same construction as the wall for one source: `build()` returns a pipeline string per source key
ending in `videoconvert → cairooverlay ov → ATOLL bug → sink sync=true` (Live TV/`hevc` upscales on the GPU — `glupload → glcolorscale 3840×2160 → glimagesink` — for a smooth, full-screen monitor-2 window; other sources use `waylandsink`), with the audio branch
`decodebin → level → downmix to stereo → queue aq → autoaudiosink sync=true`. Level measures
all channels before the downmix, so Live TV shows six bars (L R C LFE Ls Rs) while WSLg's stereo
Pulse still gets something it accepts. Probes on `usrc` (bitrate), `vpre` (source caps) and
`lvl` (sample rate) feed the top-left info panel. For `fec` and `sps` it carries the same knob
polling and counters as the wall, plus per-path packet rates for 2022-7 with a "DEAD" marker.

### 7.4 The browser paths

`multiview-web.py` (`:8099`) forks `multiview-mjpeg.sh` per HTTP client, which is the `multi`
compositor with `jpegenc → multipartmux → fdsink` in place of the window, streamed as
`multipart/x-mixed-replace`. `jxs-web.py` (`:8100`) does the same for a local
`svtjpegxsenc → svtjpegxsdec` loop. Neither follows the panel's slot assignment live in the way
`output-render` does; they were built as a remote view when WSLg could not open a window and are
kept as the iPad/Mac way to *see* the wall.

---

## 8. The NMOS plane

```mermaid
flowchart TB
  subgraph docker["docker compose — deploy/nmos"]
    reg["nmos-registry (nmos-cpp)<br/>Registration + Query API :8080<br/>+ IS-09 System API /x-nmos/system<br/>expiry 12 s"]
    vnode["nmos-virtnode (nmos-cpp example node)<br/>Node + Connection API :8090<br/>receivers v0 v1 m0 m1 … · own IS-07 sources · events ws :8091"]
    test["amwa/nmos-testing :5000"]
    vnode -- "registers + heartbeats" --> reg
  end

  panel["monitor-web.py"] -- "IS-05 PATCH staged<br/>master_enable on v0 / m0" --> vnode
  panel -- "IS-04 Query (inspector)" --> reg
  panel -- "IS-05 /active /staged /constraints, SDP (inspector)" --> vnode

  is07["is07-tally.py<br/>node atoll-is07-tally · device · 13 sources · 13 flows · 13 senders<br/>transport urn:x-nmos:transport:websocket"] -- "POST /resource · POST /health/nodes/id 5 s · re-register on 404" --> reg
  is07 -- "REST :8102 /x-nmos/events/v1.0/sources/id/state" --> anyctl["any IS-07 controller"]
  is07 -- "ws :8103 /x-nmos/events/v1.0/devices/device_id" --> wall["wall-view.py"]
  is07 -- ws --> an["analyser.py"]

  reels["reels-nmos.py (launch-media only)<br/>source → flow → sender for Test Reels<br/>templated on sender/m1, re-POST 5 s<br/>SDP served :8097"] --> reg

  legacy["activation-watcher.py + take.py (legacy demo)<br/>poll receiver a0 /active → start/stop L24 audio"] -. "IS-05" .-> vnode

  progout["program-out.py<br/>node atoll-program-out · device · receiver 'Program Out'<br/>IS-05 Connection API :8092"] -- "POST /resource · health 5 s" --> reg
  musicnmos["music-nmos.py<br/>node atoll-music · device · 2 senders (video + L24 audio)<br/>SDP per sender :8093"] -- "POST /resource · health 5 s" --> reg
  is07 -. "IS-09: discover System API via DNS-SD (_nmos-system._tcp)<br/>heartbeat from global · also program-out + music-nmos" .-> reg
  panel -- "IS-05 PATCH staged (route any island flow)" --> progout
  progout -- "writes ~/atoll-run/programout" --> ro["output-render.sh<br/>program layout"]
```

Five things Atoll adds to the stock nmos-cpp stack:

1. **A control surface that issues real IS-05.** The panel's switch between the Pi raw flow and
   the Home videos flow is an actual `master_enable` toggle on the node's receivers `v0` and `m0`.
   The renderer does not read that state; it pulls the multicast directly and follows the panel.
   That makes IS-05 a *gate* in this rig, not a transport reconfiguration, which is why no
   `transport_params` are sent.
2. **An IS-07 emitter with a transport.** `is07-tally.py` publishes per-source booleans in the
   exact message shape the nmos-cpp node uses (`identity.source_id`, `event_type`,
   `message_type`, `timing.creation_timestamp` in TAI = UTC + 37, `payload.value`). Timestamps
   mark the *transition*, not the poll. The WebSocket is hand-rolled RFC 6455 (no library is
   installable, PEP 668): a client sends `{"command":"subscription","sources":[…]}`, gets the
   echo plus the current state of each source immediately, then `state` on every change and
   `health` every 5 s. With a served transport the 13 senders are honestly advertised in IS-04.
3. **A discoverable custom sender.** `reels-nmos.py` shows the pattern for registering your own
   source/flow/sender against the registry directly: copy the schema from an existing node
   resource, attach to the virtnode's node/device, re-POST under the expiry, serve an SDP.
   `music-nmos.py` uses the same pattern to publish the music channel as its own node with two
   senders (video + ST 2110-30 L24 audio) and an SDP each — the A2 approach: a standalone
   registrar, not an extension of the virtnode. The L24 SDP is standards-clean 2110-30; the
   video is HEVC-in-MPEG-TS/UDP advertised for discovery + Program-Out routing (which matches
   by multicast ip:port, not the SDP, since NMOS has no raw-TS/UDP transport URN).
4. **A routable software receiver.** `program-out.py` registers its own node/device/receiver and
   serves the IS-05 v1.1 Connection API on `:8092`. PATCHing its receiver with the transport
   parameters of any island flow and activating writes `~/atoll-run/programout`, which the
   renderer's `program` layout follows — a real transport-params connection that drives the
   output, and discoverable, so an external NMOS controller can route it too. This is the
   receiver-side counterpart to the gate in point 1.
5. **An IS-09 System API client.** Every Atoll node now honours the System API. `atoll_system.py`
   (shared by `is07-tally.py`, `program-out.py` and `music-nmos.py`) discovers it via DNS-SD
   (`_nmos-system._tcp`, using `avahi-browse`; lowest advertised `pri` wins), falls back to the
   configured registry host — the nmos-cpp registry co-hosts the System API on `:8080` — if
   DNS-SD finds nothing, fetches `/x-nmos/system/v1.0/global`, and drives each node's IS-04
   registration **heartbeat interval** from `is04.heartbeat_interval` there instead of a
   hard-coded 5 s. A daemon thread re-discovers and re-fetches, tracking the global `version`, so
   a live change is picked up without a restart; it also reads the `ptp` block (domain_number,
   announce_receipt_timeout) for reference. This closes the IS-09 node-behaviour gap — the nodes
   previously ignored the System API. Needs `avahi-utils` (for `avahi-browse`) on the host.

   **Verified with AMWA `nmos-testing`.** IS-09-01 (System API server) passes — 5/5 applicable,
   0 fail. IS-09-02 (node System-API discovery, multicast) passes the runnable tests against the
   virtnode: test_01 (discover via multicast DNS), test_03 (correct versioned path) and test_04
   (selects by advertised priority); test_02/02_01 (unicast DNS) stay disabled unless
   `DNS_SD_MODE=unicast`, and test_05 is the manual check that the config takes effect — which our
   client does by applying `is04.heartbeat_interval`. IS-09-02's discovery tests advertise a *mock*
   System API and wait for the node to contact it, so the node under test is **restarted during the
   advertisement window** (widen `DNS_SD_ADVERT_TIMEOUT` in nmos-testing's UserConfig to make the
   timing comfortable) to trigger a fresh DNS-SD discovery.

`is07client.py` is the shared receiver (`Is07Client(sources, port, on_state, on_status)`,
`source_id(key)`, `device_id()`): reconnects with capped backoff, treats 20 s of silence as a
dead link, fires callbacks on its own thread.

---

## 9. The analyser

`analyser.py` opens one raw UDP socket per flow in `FLOWS` (built from `atoll.conf`, 18 rows
counting the two FEC parity flows), joins the group with a 16 MB receive buffer, and a single
`select` loop counts packets and bytes. If the first byte says RTP v2 it also tracks payload
type, SSRC and sequence gaps (a forward gap under 1000 is loss, anything else is reorder). A
sampler thread turns the counters into pps / Mbit/s / average datagram / loss % every second.
`/flows` returns the JSON the page polls; the page marks average datagrams under 400 B, colours
loss, shows the IS-07 tally per row via an explicit `IS07_KEY` map (two 2022-7 rows share `sps`,
three FEC rows share `fec`, Pi audio/ANC/Opus show a dash), and lists the IS-07 event stream with
TAI timestamps. A **PGM** column (and a `PGM â <flow>` header readout) flags whichever flow is
currently routed to Program Out over IS-05 â polled from `program-out.py` and matched by multicast
address, so it is exact. The header also shows the panelâs current **Take** and an **FEC** summary with a live
recovery counter. The analyser runs one small GStreamer ST 2022-1 decoder (its only use of gst) that
joins the media + parity flows, injects the SAME `fec-loss`/`fec-enable` knobs the renderers use, and
counts packets at the wire, after the loss injector, and after recovery â so it reports matrix, parity
overhead, packets **recovered / dropped**, and lifetime residual, and it tracks the demo. (A wire-only
count would read zero: the demoâs loss is injected receiver-side, not on the wire.) An **output**
column shows where each flow currently sits on monitor 2 â wall/multi quadrant (TL/TR/BL/BR),
side L/R, single, or program â from the panel layout + slots. It is deliberately not GStreamer so it cannot disturb what it measures, and
its own buffers are sized so it does not report its own overflow as network loss.

---

## 10. Launch paths, then and now

```mermaid
flowchart LR
  subgraph now["Current — systemd (what STARTUP.md describes)"]
    wslboot["open a WSL terminal"] --> sysd["systemd starts every enabled atoll-* unit + docker"]
    sysd --> manual["you run: bash pc/output-render.sh 2 (local terminal)"]
  end
  subgraph then["Original — one-shot launchers (still in repo)"]
    restore["pc/restore.ps1 (Windows)<br/>keepalive · wsl-gpu-setup.sh · docker · monitor-run.sh · launch-media.sh · restore-check.sh"]
    up["pc/atoll-up.sh (Linux-native)<br/>docker · monitor-run.sh · launch-media.sh"]
    lm["pc/launch-media.sh<br/>copy pc/*.sh → ~/atoll-run · build ~/atoll-playlist<br/>setsid media-send x3 · music bridge or placeholder<br/>reels-nmos.py · output-render.sh"]
    restore --> lm
    up --> lm
  end
  install["install.sh<br/>apt · GPU/element check · docker compose up · config + media + NIC guidance"]
```

The systemd units in `deploy/systemd/` are the live path: every sender and control process is a
unit with `Restart=on-failure`, running as the WSL user from the repo's `pc/` directory. The
one-shot launchers predate that and are kept for a Linux-native box and for the pieces systemd
does not cover (`~/atoll-playlist`, the Test Reels sender, `reels-nmos.py`). Some of them carry
paths from the previous PC build (`monitor-run.sh` hardcodes `/mnt/c/Users/dgper/...`,
`restore-check.sh` still probes `:8095`), so treat them as reference rather than run them blind.

`move-window-screen.ps1` and `snap-window-screen.ps1` are the WSL-only placement helpers `output-render.sh` runs after each renderer maps (Wayland clients cannot position themselves). `move-window-screen.ps1` catches the newly-mapped window and snaps it to the chosen monitor's bounds for `TimeoutSec` — for `waylandsink` windows, which tolerate the resize; it now **skips** `OpenGL Renderer` (glimagesink) windows. `snap-window-screen.ps1` places the glimagesink window (matched by its `OpenGL Renderer (Ubuntu)` title) **move-only** — never resizing, because glimagesink recreates its window on any resize — so it relies on the renderer opening at the right size (the 3840×2160 → 2560×1440 upscale above). Both run via a resolved full PowerShell path (`PWSH`) so placement works even when `output-render.sh` is started from an environment whose PATH lacks the Windows interop dirs.

---

## 11. Legacy, experimental and diagnostic code

Kept in `pc/` because each proved something or is the fallback for something.

| File | Status | What it is |
|---|---|---|
| `tv-send.sh` | fallback | Original Live TV bridge: kills and restarts gst on a channel change (stereo MP3). |
| `tv-send-seamless.py` | fallback | Second attempt: continuous encoder fed by intervideosrc/interaudiosrc. Worked, but the split A/V bridges drifted ~0.7 s and its timeout emitted green. Lesson: `intervideosink` on a live source must be `sync=false`. |
| `multiview-app.py` | experimental | Per-tile GstPipelines feeding one compositor over intervideo channels so a tile error restarts only that tile. Isolation proven; startup races and preroll stalls kept it from replacing `output-render.sh`. Made redundant when the *sender* became seamless. |
| `music-send.sh`, `music-placeholder.sh` | superseded | The two halves `music-channel.sh` now wraps. |
| `hevc-stream-*.sh`, `hevc-stream-env.sh` | demo | The original 4K HEVC NVENC→NVDEC RTP pipeline that proved GPU encode/decode under WSL. |
| `jxs-*.sh`, `jxs-install-libs.sh`, `svt-ffmpeg-build.sh` | demo | The JPEG XS history: SVT-JPEG-XS via custom FFmpeg over MPEG-TS at 1080p59.94 (~381 Mb/s), retired because GStreamer had no decoder then and no RFC 9134 payloader exists. |
| `video-web.py` | legacy | First viewer: 2110-20 raw → MJPEG/PNG to a browser at `:8095`, with a codec A/B and PTP timecode. Hardcodes the old Pi group `239.10.10.20:5005`. |
| `activation-watcher.py`, `take.py` | legacy demo | The first IS-05 → media bridge: watch receiver `a0`'s `master_enable`, start/stop an L24 audio receiver. |
| `reels-nmos.py`, `nmos-inspect.py` | utility | Register the Test Reels sender; dump the registry. |
| `fecverify.py` | diagnostic | Tees one udpsrc into a reference branch and a loss→fecdec branch and compares dropped packets byte-for-byte. Proved recovery is 100 % byte-correct and that the visible damage was *ordering*, not content. |
| `jitter-meter.py` | diagnostic | RTP inter-arrival jitter on the Pi audio flow with kernel receive timestamps. |
| `grab-screen.ps1` | diagnostic | Windows-side screen grab. Cannot see the WSLg surface; documented so nobody trusts it again. |
| `build-reels-loop.ps1` | utility | Concatenates the vertical phone clips into one 720p loop for the Test Reels channel. |
| `open-*-firewall.ps1`, `wsl-gpu-setup.sh` | setup | Windows firewall holes for the iPad; force Mesa's d3d12 driver under WSLg. |

---

## 12. Cross-cutting rules the code relies on

These are the constraints that recur across modules. Each one was learned by breaking it.

- **One sender per multicast group.** Two senders on a group produce a corrupt stream and doubled
  pps. Never start a sender ad hoc over SSH with `setsid … &`; use the unit, and count real
  senders with `pgrep -fc "[u]dpsink host=<grp> port=<port>"`.
- **High-rate multicast receive needs a large socket buffer.** L24 audio at 1 ms ptime is ~1000
  pkt/s; a `udpsrc buffer-size=16 MB` request is *denied* unless `net.core.rmem_max` is raised
  (WSL default is 4 MB), and the socket then overflows and drops packets before the jitterbuffer
  sees them — heavy audio dropouts. `deploy/sysctl/60-atoll-rmem.conf` sets it to 32 MB (persisted
  to `/etc/sysctl.d`). Fixes both the Music and Pi `raw` L24 feeds.
- **The WSLg audio sink resyncs on a slaved clock.** The pulse sink plays on the Windows audio-device clock, not the pipeline clock; a `sync=true` audio sink slaves to it with the default *skew* method and hard-corrects roughly every 5 s — an audible click. The Music monitor runs its L24 sink **`sync=false`** (free-run on the device clock): music is a visualizer feed, so there is no lip-sync to lose. Applied in `meter-view.py` (single/program) and the `output-render.sh` audio follower (wall/multi/side); other sources keep `sync=true`.
- **`mpegtsmux alignment=7` on every TS sender.** Packet rate is the ceiling; 188-byte datagrams
  burn it for nothing.
- **`config-interval=-1`** on `h264parse`/`h265parse` and on `rtph264pay`, so joiners get headers.
- **Every TS tile in a compositor must drain its audio pad**, and on the RTP path that drain needs
  a `queue` before `fakesink` or the demuxer stalls. The music tile is video-only because the
  placeholder card has no audio pad to drain.
- **A compositor stalls if any input never gets caps.** Every tile's group must be fed before the
  multi/wall layouts start; that is why the placeholder card exists.
- **Keep A/V together through one encoder** when re-timing them; do not split across
  intervideo/interaudio bridges (independent latency = skew) and do not delay video into an
  `intervideosink` (starves it → green).
- **Restamp before the encoder, not after.** `mpegtsmux` re-derives DTS.
- **`rtpst2022-1-fecdec` output needs a jitterbuffer after it** with a wide misorder window
  (`latency=500 max-misorder-time=5000 max-dropout-time=5000`): recovered packets carry their
  original, already-past timestamps and no buffer PTS.
- **Source keys are lowercase alphanumerics.** The renderer's `sed` parser and the IS-07 UUID5
  derivation both assume it; the analyser maps labels to keys explicitly rather than by name.
- **Python reads config through `NEED` lists.** Add the key there before using it.
- **The panel's JavaScript lives in a Python string.** No `\'` inside it.
- **Instrument the running pipeline, not a standalone harness**, when a symptom is visible on the
  wall. Three harnesses in a row measured the wrong property; the wall showed ~4000 reorders
  where each harness showed zero.
- **Tear a dynamic branch down downstream → upstream** (`teardown_source` in the TV sender). An
  element going to NULL flushes the pushes blocked *into* it, which frees the element above to stop
  its task. Source-first waited forever on `souphttpsrc`'s task, blocked into a full queue below it,
  and that wait ran on the main loop, so the watchdog that should have caught it was frozen too
  (Live TV black, deaf to channel changes, 3 Sep 2026). The thread-based hang guard is the backstop.
