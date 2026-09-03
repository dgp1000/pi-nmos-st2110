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
   | `atoll-tv` | HDHomeRun → HEVC on 5010 (seamless channel changes) |
   | `atoll-tv-web` | `:8098` standalone channel picker (also built into the panel) |
   | `atoll-home` | Home videos, 5008 |
   | `atoll-music` | Mac Now-Playing bridge → 5012 (self-healing) |
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
   systemctl is-active atoll-panel atoll-analyser atoll-is07 atoll-tv atoll-tv-web atoll-home \
     atoll-music atoll-anc atoll-j2k atoll-h264 atoll-opus atoll-mjpeg atoll-vp9 atoll-tsrtp \
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
   (use `0` for the primary monitor if #2 is off-screen). It follows the panel's take/layout.

5. **View / control.** Panel `192.168.4.85:8096` (iPad) or `localhost:8096` (PC's own browser);
   analyser `:8101`; browser multiview `:8099`; JPEG XS `:8100`. From the PC's own browser use
   **localhost:<port>** (mirrored networking can't hairpin the external IP); from the iPad/Mac use
   **192.168.4.85:<port>**.

## Shut it down

On the PC (WSL):
```
sudo systemctl stop atoll-panel atoll-analyser atoll-is07 atoll-tv atoll-tv-web atoll-home \
  atoll-music atoll-anc atoll-j2k atoll-h264 atoll-opus atoll-mjpeg atoll-vp9 atoll-tsrtp \
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
| `side` | gst-launch | two sources side by side |
| `multi` | gst-launch | plain 2×2 compositor |
| `wall` | `wall-view.py` | 2×2 **plus** per-tile bitrate, fps, audio meters, IS-07 tally, FEC counters |

The wall's slots default to `hevc,raw,jxs,music` and can be passed as the third argument:
`bash output-render.sh 2 wall hevc,raw,jxs,fec`.

## Notes / gotchas

### Networking
- The **island multicast receive on WSL is packet-rate limited**, not bandwidth-limited — HEVC tiles
  (~6 Mbit/s) and the RTP codec feeds fit; uncompressed HD 2110-20 and JPEG XS (~100 Mbit/s CBR) do
  **not**, which is why the Pi stays 320×240 and JPEG XS runs as a local encode→decode. The analyser
  (`:8101`) exists to make this visible — watch the **pps** column, not Mbit/s.
- **`mpegtsmux alignment=7`** is why there is headroom: it bundles 7 TS packets per datagram. Live TV
  went 4,470 → 653 pps and Music 958 → 120 pps for the same bitrate. The analyser highlights any
  flow whose average datagram is under ~400 B, which means someone lost that setting.
- Rapid multicast join/leave can wedge a group's Windows-side membership; if a tile goes dark on a
  specific group, move it to a nearby free port (a port collision with Windows is why J2K is on 5016).
- **Live TV** needs the HDHomeRun reachable at `192.168.7.88`; some ATSC 3.0 channels are DRM and
  come up black — pick a plain HD channel.

### Sync
- **Live TV A/V sync**: single / follow-take view is lip-synced — `meter-view.py` runs the audio sink
  at `sync=true` off the stream's own PTS, not a hand-tuned delay. `~/atoll-run/tv-audio-delay-ms`
  remains as a two-way trim if a source needs nudging.
- The wall paces its sinks from PTS (`WALL_SYNC=true`). Without it playback bursts then pauses.
- **Seamless channel changes**: the `atoll-tv` service runs `tv-send-inputselect.py` — one pipeline
  where souphttpsrc→decodebin3 feeds input-selectors (video+audio kept TOGETHER) → one encoder each →
  mpegtsmux → 5010, with a black/silence fallback the selectors switch to during a retune so the
  encoder never stops. It restamps onto running-time so DTS==PTS, which is what stopped the Live TV
  tile stepping on hardware decode. It also has a debounce (1.2 s), a watchdog (12 s → rebuild,
  twice, then revert to the last good channel) and a hang guard on its own thread (main loop silent
  20 s → exit 1 → systemd restarts it on the requested channel; the journal line names the
  GStreamer call it stuck in). Fallbacks kept in the repo:
  `tv-send-seamless.py` (older inter-bridge design — ~0.7 s A/V skew + green frames) and
  `tv-send.sh` (restart-the-whole-pipeline — perfect single view, rebuilds the multiview on every
  channel change).

### The wall
- The cairo overlay is composed on the **GLib main loop**, double-buffered, blitted by region — not
  on the streaming thread. Drawing it inline cost median 25 ms against a 33 ms frame budget and
  judder on every tile; this is median 1.6 ms. Anything you draw must sit **inside a blitted region**
  or it silently never reaches the screen.
- `WALL_SW_DECODE=true` is **required**, not optional: `nvh264dec` mishandles the FEC stream and
  breaks the picture up. Software decode costs ~186% CPU. Root cause still undiagnosed.
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

### Misc
- **multiview-app.py** is an OPTIONAL per-tile renderer (each tile its own pipeline via
  intervideosink; tile sinks MUST be sync=false). It genuinely isolates a tile's decode error, but
  intervideosrc flashes black when a tile is briefly late, so `output-render.sh` stays the default.
  The channel-change problem it targeted was ultimately solved in tv-send instead.
- `output-render.sh` sweeps orphaned renderers on startup (by PID — never `pkill -f`, whose pattern
  matches the sweeping command itself and has killed the SSH session more than once).
