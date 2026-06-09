# Design: JPEG-XS multicast on the island

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan.
**Builds on:** `2026-06-08-jpegxs-pc-native-streaming-design.md` (the PC-native loopback demo).

## 1. Goal & scope

Promote the working PC-native JPEG-XS demo from **loopback** (`127.0.0.1`) to a real **multicast flow on the ST 2110 island** (`10.10.10.0/24`). The x86 PC encodes 1080p59.94 JPEG-XS and multicasts it out the island NIC (`eth1 = 10.10.10.2`); the same PC joins the group and decodes+displays it in the GPU window. Any future x86 host on the island can join the same group unchanged.

### In scope
- Multicast send + self-receive on the island interface.
- Reuse the existing three scripts; multicast becomes the default (loopback retired).

### Out of scope (explicitly)
- **The Raspberry Pi as a JPEG-XS endpoint.** Confirmed dead twice: SVT-JPEG-XS is x86/AVX2-only, and the reference `libjxs` benchmarked at ~0.6 fps for 1080p on this (faster) x86 PC → ~0.2 fps on the Pi. The Pi keeps its raw ST 2110-20 flow.
- A second physical host (none available); the design simply leaves the door open for one.
- True ST 2110-22 / RFC 9134 `jxsv` RTP (no off-the-shelf payloader). Transport stays MPEG-TS/UDP.
- Windows firewall changes (not needed — see §6).

## 2. Why this shape

The loopback demo already proved codec + decode + GPU display. The only thing changing is the **transport endpoint**: a multicast group bound to the island NIC instead of loopback. So the design is a parameterization of the existing, verified scripts — not new components. This keeps one set of scripts, avoids duplication, and makes the receiver reusable for any island host. (Rejected: parallel multicast scripts = duplication; dual-mode auto-detect = needless logic.)

## 3. Architecture — the existing 3 scripts, retargeted

```
pc/jxs-stream-env.sh   (shared config)
   JXS_ADDR=239.10.10.22   JXS_PORT=5008
   JXS_LOCALADDR=10.10.10.2 (island NIC, eth1)   JXS_TTL=1

pc/jxs-stream-send.sh  (encoder)
   ffmpeg testsrc 1920x1080@60000/1001, yuv422p
        -> -c:v jpegxs -bpp 3 -> -f mpegts
        -> udp://239.10.10.22:5008?localaddr=10.10.10.2&ttl=1&pkt_size=1316&buffer_size=8388608

           │  MPEG-TS / UDP multicast on eth1 (10.10.10.0/24 island)
           ▼
pc/jxs-stream-view.sh  (receiver + display, reusable by any island host)
   ffmpeg -i udp://239.10.10.22:5008?localaddr=10.10.10.2&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864
        -> decode jpegxs -> rawvideo yuv422p (stdout)
   | gst-launch-1.0 fdsrc ! rawvideoparse format=Y42B width=1920 height=1080 framerate=60000/1001
        ! queue ! videoconvert ! glimagesink sync=false   (D3D12 / RTX 2080 Ti)
```

The host receives its own multicast via kernel multicast loopback (enabled by default), so self-receive needs no extra machine. A second x86 host joins the same group on its own island NIC with no sender change.

## 4. Parameters (changes from the loopback spec)

| Parameter | Loopback (old) | Island multicast (new) |
|---|---|---|
| `JXS_ADDR` | `127.0.0.1` | `239.10.10.22` (mnemonic: ST 2110-**22**; matches `.10`=audio, `.20`=video) |
| `JXS_PORT` | `5008` | `5008` (unchanged; distinct from 5004/5005) |
| `JXS_LOCALADDR` | (n/a) | `10.10.10.2` — pins send+recv to the island NIC (`eth1`), not WiFi |
| `JXS_TTL` | (n/a) | `1` — keep on the local L2 segment (correct for ST 2110); raise only if routed |

Geometry unchanged: 1920×1080, 59.94 fps (`60000/1001`), `yuv422p` / GStreamer `Y42B`, `bpp 3`. UDP buffering unchanged (sender `buffer_size`, receiver `buffer_size`/`fifo_size`/`overrun_nonfatal`), relying on the `net.core.rmem_max` ceiling set by `jxs-install-libs.sh`.

## 5. Performance

Same encode/decode cost as the loopback demo (encode ~2.6× real-time, decode ~5.7× real-time at 1080p59.94, measured 2026-06-08). Multicast adds no encode cost; ~381 Mbit/s on the wire, well within the 1 GbE island.

## 6. Error handling / notes

1. **Interface pinning:** `localaddr=10.10.10.2` forces both the multicast egress and the group join onto `eth1`. Without it, WSL might pick the WiFi NIC and the Pi/island wouldn't see it.
2. **TTL:** `ttl=1` confines the stream to the island segment (matches ST 2110 local-multicast practice).
3. **Firewall:** none required. Self-receive is internal kernel loopback; a second host *receiving* needs no inbound rule on this PC (this PC only sends). Only an inbound *sender* would need UDP 5008 opened — out of scope.
4. **Packet loss:** prevented by the raised `net.core.rmem_max` + large `buffer_size` (from the loopback work); verified 0 decode errors.
5. **IGMP:** the PoE switch already carries the Pi's `.10`/`.20` multicasts, so group membership for `.22` works the same way.

## 7. Testing

- **Probe:** `ffprobe udp://239.10.10.22:5008?localaddr=10.10.10.2` → `codec_name=jpegxs`, 1920×1080 (passed in feasibility check).
- **Group membership:** `ip maddr show eth1` (or `netstat -g`) lists `239.10.10.22` while the viewer runs.
- **Decode integrity:** receiver decodes ≥180 frames with 0 `HANDLE ERROR`/decode failures.
- **GPU:** viewer reports `GL_RENDERER: D3D12 (NVIDIA GeForce RTX 2080 Ti)`.
- **Visual:** native window shows the moving 1080p test pattern, fed from the multicast group (human-verified).

## 8. Deliverables

- Updated `pc/jxs-stream-env.sh`, `pc/jxs-stream-send.sh`, `pc/jxs-stream-view.sh` (multicast on the island).
- `docs/superpowers/RESUME.md` updated: the JPEG-XS demo now multicasts on the island (`239.10.10.22:5008`).

## 9. Future (not now)

- A second x86 receiver on the island (already supported by the design).
- 10-bit 4:2:2; true ST 2110-22 / RFC 9134.
- iPad viewing would require transcoding (the iPad can't decode JPEG-XS) — e.g., a JPEG-XS→MJPEG/HLS gateway, like the existing `video-web.py` path.
