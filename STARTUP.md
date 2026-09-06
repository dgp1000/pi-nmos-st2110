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

   | Service | What |
   |---|---|
   | *(docker)* | NMOS stack — registry `:8080`, virtnode `:8090`, AMWA testing `:5000` |
   | `atoll-panel` | `:8096` switcher + inspector + TV remote |
   | `atoll-analyser` | `:8101` island flow analyser (pps / bitrate / RTP loss / IS-07) |
   | `atoll-is07` | `:8102` IS-07 Event & Tally + `:8103` WebSocket transport |
   | `atoll-programout` | `:8092` Program Out software receiver (IS-05 Connection API) |
   | `atoll-tv` | HDHomeRun → HEVC on 5010 (seamless channel changes) |
   | `atoll-tv-web` | `:8098` standalone channel picker (also built into the panel) |
   | `atoll-home` | Home videos, 5008 |
   | `atoll-music` | Mac Now-Playing bridge → HEVC video 5012 + ST 2110-30 L24 audio 5013 (self-healing) |
   | `atoll-music-nmos` | Registers music as an NMOS source (IS-04 senders + SDPs) → :8093 |
   | `atoll-anc` | ST 2110-40 ancillary timecode, 5020 |
   | `atoll-j2k` | JPEG 2000 J2K/RTP, 5016 |
   | `atoll-h264` + `atoll-opus` | H.264 RTP 5018 + its Opus audio 5022 |
   | `atoll-mjpeg` | Motion JPEG RTP, 5024 |
   | `atoll-vp9` | VP9 RTP, 5026 |
   | `atoll-tsrtp` | MPEG-TS over RTP (ST 2022-2), 5028 |
   | `atoll-fec` | ST 2022-1 protected media 5040 + column 5042 + row 5044 |
   | `atoll-sps` | ST 2022-7 dual path — A 5046, B 5048 |
   | `atoll-multiview` | `:8099` browser view |
   | `atoll-jxs-web` | `:8100` JPEG XS |

   Check them all at once:
   ```
   systemctl is-active atoll-panel atoll-analyser atoll-is07 atoll-programout atoll-tv atoll-tv-web atoll-home \
     atoll-music atoll-music-nmos atoll-anc atoll-j2k atoll-h264 atoll-opus atoll-mjpeg atoll-vp9 atoll-tsrtp \
     atoll-fec atoll-sps atoll-multiview atoll-jxs-web
   ```
   (`atoll-hevc`, `atoll-jxs` and `atoll-music-ph` are deliberately **disabled** — superseded.)

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
   (use `0` for the primary monitor if #2 is off-screen). It follows the panel's take/layout. The on-screen present is **glimagesink** (GL/EGL); on every take `output-render.sh` auto-places it full-screen on monitor 2 (`snap-window-screen.ps1`), so no manual window dragging.

5. **View / control.** Panel `192.168.4.85:8096` (iPad) or `localhost:8096` (PC's own browser);
   analyser `:8101`; browser multiview `:8099`; JPEG XS `:8100`. From the PC's own browser use
   **localhost:<port>** (mirrored networking can't hairpin the external IP); from the iPad/Mac use
   **192.168.4.85:<port>**.

## Shut it down

On the PC (WSL):
```
sudo systemctl stop atoll-panel atoll-analyser atoll-is07 atoll-programout atoll-tv atoll-tv-web atoll-home \
  atoll-music atoll-music-nmos atoll-anc atoll-j2k atoll-h264 atoll-opus atoll-mjpeg atoll-vp9 atoll-tsrtp \
  atoll-fec atoll-sps atoll-multiview atoll-jxs-web
cd ~/pi-nmos-st2110/deploy/nmos && sudo docker compose stop
ssh dgperkins@10.10.10.1 sudo systemctl stop atoll-pi
```
Ctrl-C the `output-render.sh`. Then optionally `wsl --shutdown` (PowerShell) to fully stop WSL,
or just shut down Windows. Services are only *stopped*, not disabled — they auto-start on the
next WSL boot.

## Layouts

`output-render.sh` follows the panel's layout button:

| Layout | Renderer | What you get |
|---|---|---|
| `single` / follow-take | `meter-view.py` | fullscreen, per-channel VU meters, live stream-info overlay, lip-synced |
| `program` | `meter-view.py` | Program Out: the flow routed to the software receiver over IS-05 (panel "Program Out" row), fullscreen; idle card when nothing is connected |
| `side` | gst-launch | two sources side by side |
| `multi` | gst-launch | plain 2×2 compositor |
| `wall` | `wall-view.py` | 2×2 **plus** per-tile bitrate, fps, audio meters, IS-07 tally, FEC counters |

The wall's slots default to `hevc,jxs,music,tsrtp` (all compressed) and can be passed as the third
argument: `bash output-render.sh 2 wall hevc,jxs,fec,sps`.

**Guided demo.** The panel's **Guided demo** button runs a hands-off, captioned tour: the wall + IS-07 tally, taking sources, Program Out routing over IS-05, a seamless Live TV channel change, the ST 2022-1 FEC loss A/B, and the ST 2022-7 path-pull. It drives the real controls (so the output on monitor 2 follows) and, on finish or Stop, resets loss/FEC/paths and returns to the wall. Captions show on the panel AND are burned onto the monitor-2 output itself (single/program via meter-view, the wall via wall-view), so it runs standalone. The demo writes ~/atoll-run/demo-caption, which the on-screen renderers read; restart output-render once after updating so the caption-capable views are loaded.

**Keep the `raw` tile out of the busy 4-up.** The Pi `raw` feed (ST 2110-20, UYVY 320x240 59.94)
is depayloaded and upscaled on the CPU. On its own or in a 2-up it is fine, but in a four-tile wall
alongside three GPU-decoded tiles it oversubscribes the WSLg compositor/display, and the Live TV
(`hevc`) tile drops to keyframes only (~1 fps stepping) while audio stays fine (4 Sep 2026). It is
still selectable — put it in `single`/`side`, not the default wall.

## Notes / gotchas

### Networking
- The **island multicast receive on WSL is packet-rate limited**, not bandwidth-limited — HEVC tiles
  (~6 Mbit/s) and the RTP codec feeds fit; uncompressed HD 2110-20 and JPEG XS (~100 Mbit/s CBR) do
  **not**, which is why the Pi stays 320×240 and JPEG XS runs as a local encode→decode. The analyser
  (`:8101`) exists to make this visible — watch the **pps** column, not Mbit/s.
- **High-rate multicast receive needs a large socket buffer.** ST 2110-30 L24 at 1 ms ptime is
  ~1000 pkt/s; the `udpsrc buffer-size=16 MB` request is denied unless `net.core.rmem_max` is
  raised (WSL default 4 MB), so the socket overflows and drops packets before the jitterbuffer —
  heavy audio dropouts. `deploy/sysctl/60-atoll-rmem.conf` raises it to 32 MB (persisted to
  `/etc/sysctl.d`); it covers both the Music and Pi `raw` L24 feeds.
- **WSLg audio sink resync.** The Music L24 monitor runs its sink `sync=false` (free-run on the Windows audio-device clock); a `sync=true` sink slaves with the default skew method and clicks ~every 5 s. Music is a visualizer feed (no lip-sync); other sources keep `sync=true`.
- **`mpegtsmux alignment=7`** is why there is headroom: it bundles 7 TS packets per datagram. Live TV
  went 4,470 → 653 pps and Music 958 → 120 pps for the same bitrate. The analyser highlights any
  flow whose average datagram is under ~400 B, which means someone lost that setting.
- Rapid multicast join/leave can wedge a group's Windows-side membership; if a tile goes dark on a
  specific group, move it to a nearby free port (a port collision with Windows is why J2K is on 5016).
- **Live TV** needs the HDHomeRun reachable on the LAN. It is found by its DeviceID
  (`HDHR_DEVICE_ID` in `atoll.conf`) over discovery, so a DHCP address change self-heals; `HDHR_HOST`
  is just the fallback. Check it with `python3 pc/hdhr.py` (lists DeviceID → IP) or
  `python3 pc/hdhr.py <DeviceID>`. Some ATSC 3.0 channels are DRM and come up black — pick a plain
  HD channel.

### Sync
- **Live TV A/V sync**: single / follow-take view is lip-synced — `meter-view.py` runs the audio sink
  at `sync=true` off the stream's own PTS, not a hand-tuned delay. `~/atoll-run/tv-audio-delay-ms`
  remains as a two-way trim if a source needs nudging.
- The on-screen output presents via **glimagesink** (GL/EGL, vsync-paced, `sync=true`) — single Live TV, the wall, and the inline multi/side layouts. waylandsink's WSLg SHM present (no dmabuf) *stepped* fine scrolling content (the Live TV ticker) even with a clean stream; glimagesink is smooth and lighter on the GPU (3D-engine ~80% → ~60%). glimagesink can't be resized (recreates its window), so the renderer opens it at size: GPU-upscaled to 3840×2160, which WSLg's 1.5× DPI renders as a 2560×1440 window filling monitor 2. `snap-window-screen.ps1` (move-only, by window title) places it; `move-window-screen.ps1` handles the remaining waylandsink windows and skips glimagesink. (`WALL_SYNC` no longer drives the sink.)
- **Source IPs drift (DHCP).** The Mac Now-Playing host (`MAC_MUSIC_HOST` in `pc/atoll.conf`) and the HDHomeRun are on home-WiFi DHCP and move (both moved mid-session). The HDHomeRun self-heals by DeviceID discovery; the Mac music host is a **hardcoded IP** and `.local`/mDNS does not resolve from WSL, so if the Music tile shows "connecting", update `MAC_MUSIC_HOST` and `sudo systemctl restart atoll-music`. A **DHCP reservation** for the Mac mini (and the HDHomeRun) on the router avoids both.
- **Make-before-break channel changes**: the `atoll-tv` service runs `tv-send-inputselect.py` — one
  pipeline where souphttpsrc→decodebin3 feeds input-selectors (video+audio kept TOGETHER) → one
  encoder each → mpegtsmux → 5010. On a change the new channel is opened on a SECOND HDHomeRun tuner
  while the old one stays on air; the selectors cut once the new branch has decoded a video and an
  audio frame, then the old branch and its tuner are released — no black frame (the black/silence
  fallback is on air only at start-up or if the on-air branch dies). Each branch gets its own libsoup
  session, and the pipeline latency is pinned ~2.5 s (mux/sink queues sized above it) so the synced
  udpsink never throttles a live source; the trade is ~1.7 s more Live TV delay. It restamps onto
  running-time so DTS==PTS, which is what stopped the Live TV tile stepping on hardware decode. It also has a debounce (1.2 s), a watchdog (12 s → rebuild,
  twice, then revert to the last good channel) and a hang guard on its own thread (main loop silent
  20 s → exit 1 → systemd restarts it on the requested channel; the journal line names the
  GStreamer call it stuck in). Fallbacks kept in the repo:
  `tv-send-seamless.py` (older inter-bridge design — ~0.7 s A/V skew + green frames) and
  `tv-send.sh` (restart-the-whole-pipeline — perfect single view, rebuilds the multiview on every
  channel change).
- **2nd-Pi PTP follower demo.** `pi2-nmos` (a Pi 2B) at `10.10.10.3` on the island locks its clock to
  the Pi 5 grandmaster. Two boot services: `atoll-follower` (`ptp4l -f ~/follower-ptp.cfg -i eth0`)
  and `atoll-follower-web` (`:8000` readout). **Hybrid E2E** is the key — the island switch does IGMP
  snooping with no querier, so the grandmaster's multicast *receive* membership ages out and it
  ignores multicast `Delay_Req` (follower stuck `UNCALIBRATED`, `rx_Delay_Resp=0`); `hybrid_e2e 1`
  sends `Delay_Req` **unicast** to the master, no grandmaster change needed. NTP is disabled on the
  follower so PTP owns the clock (first lock *steps* it to the master's time). `slaveOnly 1` is
  **production-realistic** — every real ST 2110 endpoint (camera, encoder, receiver) runs slave-only so
  it can never win BMCA and become the facility clock; only dedicated grandmasters are master-capable.
  The per-Pi web readout is the demo bit (real endpoints lock silently; lock health is monitored
  centrally) — the panel's follower status line is the closer-to-production view. Accuracy ~±1–3 ms — the
  Pi 2B USB-NIC software-timestamping ceiling (a Pi 4 native NIC / Pi 5 PHC would be tighter). Headless
  and autonomous: static IP via netplan, both services enabled, re-locks unattended after any reboot.
  **View:** `http://10.10.10.3:8000` from the PC (island-only, not on WiFi). Deploy to a fresh Pi:
  `apt install -y linuxptp`, copy `pi/atoll-pi.conf` + `pi/follow-all.sh` + `pi/follower-ptp.cfg` +
  `pi/follower-clock-web.py`, install the two `pi/atoll-follower*.service` units, `systemctl enable`.
- **Grandmaster clock hardening.** `pi/launch-all.sh` waits up to 60 s for NTP sync before starting
  `ptp4l`, so the Pi 5 never serves a stale boot-time (RTC/fake-hwclock) clock to the whole island.

### The wall
- The cairo overlay is composed on the **GLib main loop**, double-buffered, blitted by region — not
  on the streaming thread. Drawing it inline cost median 25 ms against a 33 ms frame budget and
  judder on every tile; this is median 1.6 ms. Anything you draw must sit **inside a blitted region**
  or it silently never reaches the screen.
- **The FEC tile decodes in software (`avdec_h264`) by design — root cause diagnosed 6 Sep 2026.**
  Under residual loss (what FEC can't fully rebuild — the demo's whole point) the recovered H.264 has
  gaps. The sender carries SPS/PPS inband (`config-interval=-1`); when loss eats the frame carrying
  them, `nvh264dec` loses configuration (`Should configure decoder first` / `Failed to negotiate`) and
  tears. `h264parse config-interval=1` on the receiver re-inserts cached SPS/PPS and kills those fatal
  errors (2/14s→0 at 5% loss) — now in wall-view.py. But NVDEC still has no macroblock error
  concealment, so residual corrupt frames glitch, whereas `avdec_h264` conceals them smoothly — which
  is exactly the graceful degradation the FEC demo shows. So the FEC tile uses `avdec` on purpose
  (~25% of a core for one 720p tile), not as a workaround. `nvh264dec`+`config-interval=1` is a viable
  GPU alternative if you accept glitch-not-conceal. `WALL_SW_DECODE=true` (all tiles → software,
  ~186% CPU) stays only as a blunt diagnostic; it is **not** required and is off in production.
- Native 2560×1440 was tried and reverted — it cost ~2 extra cores (531% vs 333%) and dropped frames.
  `WALL_W`/`WALL_H` stay at 1920×1080.
- Trust **Task Manager** for GPU load on this rig, not WSL's `nvidia-smi` — the latter is blind to
  the WSLg→Windows presentation path and under-reports badly.

### FEC / 2022-7
- The FEC feed is **all-intra** (`key-int-max=1`) on purpose. `rtpst2022-1-fecdec` only emits a
  recovered packet when its matrix completes — measured worst case ~81 packets late. At the original
  ~70 pps that was ~1.16 s, far beyond the jitterbuffer, so packets arrived byte-correct but too
  late. All-intra raises the rate to ~300 pps (~270 ms) and stops the error propagating.
- `fecverify.py` proves recovery is **byte-exact** (35/35 identical). It also found that fecdec emits
  every reconstructed packet with `PTS = CLOCK_TIME_NONE` (36/36), masked by a downstream jitterbuffer.
- `rtpst2022-1-fecenc` rejects a non-zero SSRC — the payloader must be `rtpmp2tpay ssrc=0`.
- Residual ~0.0006% with `reord` in the low thousands is normal and expected; it is reported on the
  tile so recovery is a number, not a claim.

### IS-07
- Tally is **pushed**, not polled: `is07-tally.py` serves a per-device WebSocket endpoint
  (`ws://…:8103/x-nmos/events/v1.0/devices/<device_id>`) and pushes a `state` message the moment a
  source changes, with `health` keepalives every 5 s. Measured cut→tally ≈ 64 ms.
- That remaining ~64 ms is the emitter's own 100 ms poll of the panel, not the transport. Do not
  raise that interval to "save" requests; it is 10 req/s against localhost.
- The wall and the analyser are both real receivers sharing `is07client.py`. Each keeps the panel as
  a fallback but uses it **only while disconnected** — normal operation makes no HTTP calls.
- The sources are registered in **IS-04** (node → device → 13 sources → flows → senders, transport
  `urn:x-nmos:transport:websocket`), with a health heartbeat and re-registration on 404.
- Registry counts include the reference node's own: 21 websocket senders / 15 boolean sources = ours
  (13 + 13) plus easy-nmos-node's (8 + 2). Group by device before suspecting stale duplicates. The
  Query API also paginates at 10 — pass `?paging.limit=100`.
- **Only the `wall` layout and the analyser show IS-07.** Single view and the gst-launch `multi`
  layout have none. To prove tally is really coming from IS-07, stop `atoll-is07`: the "NMOS IS-07"
  line disappears and the analyser header flips to "no events", while tally keeps working via the
  fallback.

### IS-05 (receiver-side)
- **Program Out is a full IS-05 receiver** (`program-out.py`, `:8092`, registered in IS-04). A
  controller PATCHes `/staged` and activates; the `program` layout renders whatever it's connected to.
  Three ways to route it: **transport_params** (multicast_ip+port), **sender_id** (names a discovered
  sender — resolved via the registry Query API + the sender's SDP `manifest_href`), and the three
  **activation modes** (`activate_immediate`, `activate_scheduled_relative`, `activate_scheduled_absolute`;
  scheduled ones fire on a timer). On activate/deactivate it updates the receiver's IS-04 `subscription`
  and re-registers, so controllers/registry see the connection (two-way IS-05). Panel: per-flow route
  buttons + a **"Schedule +5s"** toggle (armed routes fire on the clock, with a pending badge).
- Quick check: `curl -s :8092/programout` shows the active essence, sender_id, transport_params and any
  pending scheduled activation; `curl :8080/x-nmos/query/v1.3/receivers/<id>` shows the live subscription.

### SDPs / ST 2110-21 (sender descriptions)
- **The Pi's real ST 2110 senders are advertised with standards-complete SDPs** by `pi-nmos.py`
  (`atoll-pi-nmos`, `:8095`): ST 2110-20 raw video (`/sdp/pi-video.sdp`) and ST 2110-30 L24 audio
  (`/sdp/pi-audio.sdp`). The video SDP has the full -20 `a=fmtp` (sampling/size/exactframerate/depth/
  colorimetry/PM/SSN) plus the ST 2110-21 pacing type `TP=2110TPW`; both carry `a=mediaclk:direct=0`
  and `a=ts-refclk:ptp=...:<gmid>:0` (grandmaster EUI-64 in `PTP_GMID`, else `traceable`). They
  register as node `atoll-pi` and appear in the panel's IS-04/05 inspector.
- **-21 honesty:** the gst software senders aren't hardware-paced, so they're declared **Wide**
  (`2110TPW`), not Narrow. Change `TP=` in `pi-nmos.py` only if the pacing actually changes.
- Quick check: `curl :8095/sdp/pi-video.sdp` and `curl :8095/sdp/pi-audio.sdp`; `curl :8095/status`.

### IS-08 (audio channel mapping)
- **Music audio has an IS-08 Channel Mapping API** (`audiomap-nmos.py`, `:8094`, IS-04-registered with
  a cm-ctrl control). A controller maps the output's 2 channels to the input's — straight stereo, swap
  L↔R, dual-mono, mute a channel (IS-08 routes channels, it does not mix). It's made audible by an IS-08
  processor in the path: `music-channel.sh` sends decoded L24 to `localhost:MUSIC_AUDIO_PREMAP_PORT` and
  `audiomapper.sh` (`atoll-audiomapper`) applies the routing matrix (`~/atoll-run/audiomap`) and re-sends
  on `MUSIC_AUDIO_GRP`. Only the mapper restarts on a map change → **instant** re-route, music video tile
  untouched (no compositor stall). Activations: immediate or scheduled via `POST /map/activations`.
- Panel: **MUSIC AUDIO · IS-08 CHANNEL MAP** row (Stereo / Swap L↔R / Mono (L) / Mute R). Best shown in
  Music + single view. Quick check: `curl :8094/audiomap` (preset, matrix, pending) or the standard
  `curl :8094/x-nmos/channelmapping/v1.0/map/active`.

### IS-09
- Every Atoll node discovers the **System API** and honours its global config. `atoll_system.py`
  (shared by `is07-tally.py`, `program-out.py`, `music-nmos.py`) browses `_nmos-system._tcp` over
  DNS-SD (`avahi-browse`; lowest advertised `pri` wins), falls back to the registry host (which
  co-hosts the System API on `:8080`) if DNS-SD is empty, fetches `/x-nmos/system/v1.0/global`,
  and drives each node's IS-04 heartbeat from `is04.heartbeat_interval` (was a hard-coded 5 s).
  Confirm with `sudo journalctl -u atoll-is07 | grep IS-09` — expect `System API via DNS-SD …
  heartbeat=5s, ptp.domain=127`.
- **Needs `avahi-utils`** (for `avahi-browse`) on the host — now in `install.sh`'s apt list. If the
  nodes log `configured fallback` instead of `DNS-SD`, avahi-browse is missing or avahi isn't running.
- **Conformance (AMWA `nmos-testing`, `:5000`).** IS-09-01 (System API server) and IS-09-02
  (multicast discovery: test_01/03/04) pass. IS-09-02 advertises a *mock* System API and waits for
  the node to contact it, so run it against the virtnode and **restart `nmos-virtnode` during the
  advert window** to force a fresh discovery: in the UI pick IS-09-02, Node host `172.18.0.2`,
  System version `v1.0`, click Run, then `sudo docker restart nmos-virtnode`. Widen
  `DNS_SD_ADVERT_TIMEOUT` (set `CONFIG.DNS_SD_ADVERT_TIMEOUT` in nmos-testing's UserConfig) for
  comfortable timing. test_02 needs `DNS_SD_MODE=unicast`; test_05 is a manual check.

### Misc
- **multiview-app.py** is an OPTIONAL per-tile renderer (each tile its own pipeline via
  intervideosink; tile sinks MUST be sync=false). It genuinely isolates a tile's decode error, but
  intervideosrc flashes black when a tile is briefly late, so `output-render.sh` stays the default.
  The channel-change problem it targeted was ultimately solved in tv-send instead.
- `output-render.sh` sweeps orphaned renderers on startup (by PID — never `pkill -f`, whose pattern
  matches the sweeping command itself and has killed the SSH session more than once).
