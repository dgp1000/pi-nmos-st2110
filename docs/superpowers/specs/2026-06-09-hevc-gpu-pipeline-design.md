# Design: 4K HEVC GPU pipeline on the island

**Date:** 2026-06-09
**Status:** Approved (design); pending implementation plan.
**Builds on:** the JPEG-XS island multicast scripts (same structure, env-file pattern) and `pc/jxs-center-window.ps1` (window centering).

## 1. Goal & scope

A genuine end-to-end **GPU** video pipeline that works the RTX 2080 Ti: encode 4K HEVC with **NVENC**, carry it as RTP over multicast on the island, decode with **NVDEC**, and display in a native GPU window. This is the deliberate counterpoint to the CPU-bound JPEG-XS demo.

### In scope
- All-GStreamer, GPU-resident pipeline: `videotestsrc 4K → nvh265enc → rtph265pay → udpsink (multicast)` and `udpsrc → rtpjitterbuffer → rtph265depay → nvh265dec → glimagesink`.
- 3840×2160 @ 59.94, H.265/HEVC, ~50 Mbit/s, on `239.10.10.65:5010` (mnemonic: H.**265**) via `eth1`.
- Self-receive on this PC; a second x86/GPU host could join the group.

### Out of scope (explicitly)
- ST 2110 compliance (HEVC-over-RTP is standard RTP, not 2110 uncompressed/JPEG-XS).
- The iPad/web monitor (separate; HEVC could feed it later — Future).
- Real-file source (synthetic `videotestsrc` first; a 4K-upscaled file variant is a Future follow-up).
- Multi-stream stress benchmark (the user chose the real-pipeline option, not the throughput stress).

## 2. Why this shape

NVENC/NVDEC are verified working in WSL (4K HEVC test: encoder ~34%, decoder ~8% on the 2080 Ti, CUDA clean). H.265 has proper GStreamer RTP payloaders (`rtph265pay`/`rtph265depay`), so unlike JPEG-XS the whole chain is one toolkit and stays on the GPU — only RTP pay/depay + jitterbuffer touch the CPU. Mirroring the JPEG-XS three-file structure (env + send + view) keeps it consistent with the repo.

## 3. Architecture

```
pc/hevc-stream-send.sh   (NVENC encoder)
  videotestsrc is-live=true
    ! video/x-raw,width=3840,height=2160,framerate=60000/1001
    ! videoconvert
    ! nvh265enc  (CBR ~50 Mbit/s, periodic IDR ~0.5 s, low-latency)
    ! h265parse
    ! rtph265pay config-interval=-1 pt=96
    ! udpsink host=239.10.10.65 port=5010 multicast-iface=eth1 auto-multicast=true ttl=1 buffer-size=8388608

           │  RTP/H.265 multicast on the 10.10.10.0/24 island
           ▼
pc/hevc-stream-view.sh   (NVDEC decoder + GPU display)
  udpsrc address=239.10.10.65 port=5010 multicast-iface=eth1 auto-multicast=true buffer-size=8388608
       caps="application/x-rtp,media=video,encoding-name=H265,clock-rate=90000,payload=96"
    ! rtpjitterbuffer latency=200
    ! rtph265depay ! h265parse ! nvh265dec
    ! queue ! glimagesink sync=false   (D3D12 / RTX 2080 Ti)
```

GPU engines exercised: **NVENC** (encode), **NVDEC** (decode), **D3D12 GL** (render). `nvidia-smi` shows encoder/decoder utilization.

## 4. Parameters

| Parameter | Value |
|---|---|
| Resolution / fps | 3840×2160 @ `60000/1001` (59.94) |
| Codec | H.265/HEVC — `nvh265enc` / `nvh265dec` |
| Bitrate | ~50 Mbit/s, CBR |
| Keyframes | periodic IDR ~every 0.5 s (`gop-size` ≈ 30) + `rtph265pay config-interval=-1` so late joiners get VPS/SPS/PPS |
| Group / port | `239.10.10.65:5010` |
| Interface | `eth1` (island, 10.10.10.2); TTL 1 |
| Display | `glimagesink` (D3D12), window centered on screen 1 |

`nvh265enc` exact property names/enums (e.g. `bitrate`, `rc-mode`/`preset`/`tune`, `gop-size`) will be confirmed with `gst-inspect-1.0 nvh265enc` during implementation; the plan uses the confirmed names.

## 5. Components / files

- `pc/hevc-stream-env.sh` — shared config (geometry, codec, bitrate, group/port, iface, ttl, `GALLIUM_DRIVER=d3d12`), sourced by both scripts so send/receive caps stay in lock-step.
- `pc/hevc-stream-send.sh` — the NVENC sender (synthetic 4K source).
- `pc/hevc-stream-view.sh` — the NVDEC receiver + GPU window; reuses `jxs-center-window.ps1` for centering (window title is the GStreamer "OpenGL Renderer (Ubuntu)" — same as the JPEG-XS viewer).

## 6. Error handling / notes

1. **Late multicast join:** periodic IDR + `config-interval=-1` let a receiver started after the sender sync within ~0.5 s (without it, a late joiner shows nothing until the next random keyframe).
2. **Interface pinning:** `multicast-iface=eth1` on both ends so the flow uses the island NIC, not WiFi.
3. **UDP buffers:** ~50 Mbit/s is modest (vs JPEG-XS 380), but set `buffer-size` on udpsink/udpsrc + `rtpjitterbuffer latency=200` to absorb jitter; the `net.core.rmem_max` ceiling raised by `jxs-install-libs.sh` already covers it.
4. **GPU display:** `GALLIUM_DRIVER=d3d12` for glimagesink (set in the env file); `nvh265dec` outputs CUDA/system memory that glimagesink uploads to GL.
5. **4K window on a 1440p screen:** glimagesink scales it down; centered via the helper.
6. **Persistence:** run via `setsid` like the JPEG-XS demo; killed via the bracket-`pkill` trick (`[h]evc-stream`, `[2]39.10.10.65`).

## 7. Testing

- **Sender:** caps/`ffprobe` (or `gst-inspect` of the running pipeline) confirm H265 4K on the group; `nvidia-smi --query-gpu=utilization.encoder` > 0 while sending.
- **Receiver smoke:** a headless `... ! nvh265dec ! fpsdisplaysink` shows ~59.94 fps rendered and `nvidia-smi utilization.decoder` > 0.
- **GPU:** with sender + viewer running, `nvidia-smi` shows encoder AND decoder utilization simultaneously.
- **Visual (human):** a native 4K HEVC window appears, centered on screen 1, smooth motion.

## 8. Deliverables

- `pc/hevc-stream-env.sh`, `pc/hevc-stream-send.sh`, `pc/hevc-stream-view.sh`.
- `docs/superpowers/RESUME.md` note.

## 9. Future (not now)

- Real-video source (4K-upscaled file, GPU scaler `cudaconvert`/`cudascale`).
- Multi-stream stress mode (N parallel 4K encodes) to push NVENC toward 100%.
- Feed HEVC into the iPad monitor (`monitor-web.py`) as a third selectable source.
- CPU-vs-GPU showdown vs the JPEG-XS pipeline (load/latency/bitrate/quality).
