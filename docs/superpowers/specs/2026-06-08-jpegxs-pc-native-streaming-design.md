# Design: PC-native JPEG-XS streaming demo

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan.

## 1. Goal & scope

Demonstrate a complete **native JPEG-XS (ISO/IEC 21122) video pipeline running entirely on this PC** (WSL2 / x86-64): synthetic 1080p59.94 video → JPEG-XS encode → MPEG-TS over UDP → decode → **GPU-accelerated native window** (Mesa D3D12 on the RTX 2080 Ti, via WSLg).

This proves the codec + transport + native display path on the hardware that can actually run it, now that WSLg GPU display works (`GALLIUM_DRIVER=d3d12`).

### In scope
- PC-only encode → network transport → decode → on-screen display.
- 1080p, 59.94 fps (US broadcast cadence), JPEG-XS 4:2:2.
- A **reusable receiver** decoupled from the sender, so it can later consume any JPEG-XS-over-MPEG-TS source.

### Out of scope (explicitly)
- The Raspberry Pi as a JPEG-XS source. SVT-JPEG-XS is **x86-64/AVX2 only** (no arm64/NEON port), and the Pi has no FFmpeg/SVT. The Pi's existing raw ST 2110-20 flow is **untouched**.
- True ST 2110-22 / RFC 9134 `jxsv` RTP. No off-the-shelf `jxsv` payloader exists; FFmpeg's RTP muxer rejects JPEG-XS (`Unsupported codec jpegxs`). Transport here is MPEG-TS/UDP, not 2110-22.
- NMOS / IS-04 / IS-05 control. Not needed for the demo.

## 2. Why this shape

- **FFmpeg owns JPEG-XS.** The locally built FFmpeg (`--enable-libsvtjpegxs`, binary at `/usr/local/bin/ffmpeg`) is the only tool here that can encode/decode JPEG-XS. GStreamer has no JPEG-XS element in Ubuntu's build.
- **GStreamer owns the GPU window.** `glimagesink` renders on the D3D12 GPU. FFmpeg here was built without `ffplay`/SDL, so it cannot open a window.
- Therefore the pipeline is two processes joined by a pipe: FFmpeg decodes to raw frames on stdout; GStreamer displays them. Each half is independently runnable and testable.

## 3. Architecture — two scripts joined by UDP

```
pc/jxs-stream-send.sh   (encoder)
  ffmpeg: testsrc 1920x1080@60000/1001, yuv422p
        → -c:v jpegxs -bpp 3
        → -f mpegts → udp://127.0.0.1:5008   (continuous / live)

           │  MPEG-TS over UDP
           ▼
pc/jxs-stream-view.sh   (receiver + display, reusable)
  ffmpeg: -i udp://127.0.0.1:5008
        → decode jpegxs → -f rawvideo -pix_fmt yuv422p → stdout
  | gst-launch-1.0 fdsrc
        ! rawvideoparse format=I422 width=1920 height=1080 framerate=60000/1001
        ! videoconvert ! glimagesink        (D3D12 / RTX 2080 Ti)
```

- `jxs-stream-send.sh` simulates the camera/encoder end (synthetic source).
- `jxs-stream-view.sh` is the deliverable receiver: it accepts any JPEG-XS-in-MPEG-TS UDP stream, so a future external sender could feed the same viewer unchanged.

## 4. Parameters (defaults; defined once, shared between scripts)

| Parameter | Default | Notes |
|---|---|---|
| Resolution | 1920×1080 | "at least 1080p" requirement |
| Frame rate | 59.94 (`60000/1001`) | US cadence; matches existing 59.94 monitor work |
| Pixel format | `yuv422p` (8-bit) | JPEG-XS requires 4:2:2. 10-bit (`yuv422p10le` / `I422_10LE`) is a future option |
| Compression | `-bpp 3` | ~visually lossless; ≈381 Mbit/s. Lower bpp = less bandwidth |
| Transport | MPEG-TS / UDP | `udp://127.0.0.1:5008` unicast loopback |
| Port | 5008 | avoids existing 5004 (audio) / 5005 (video) |

The encode geometry and the `rawvideoparse` caps **must stay in lock-step**. Both scripts source a single shared block of variables (`W H FPS PIXFMT GSTFMT BPP ADDR PORT`) so they can never drift.

Multicast (e.g. `239.10.10.22:5008`) is a one-line change to `ADDR` if the stream should be viewable from another host on the island later.

## 5. Performance (measured 2026-06-08 on this PC)

3 s of 1080p59.94 content: encode 1.14 s wall (~2.6× real-time), decode 0.53 s (~5.7× real-time). Real-time with headroom. Bitrate at bpp 3 ≈ 381 Mbit/s (fits 1 GbE; high enough that UDP needs socket-buffer tuning — see below).

## 6. Error handling / known gotchas (all verified this session)

1. **JPEG-XS codec invisible without libs:** export `LD_LIBRARY_PATH=/root/jxs-install/lib` in both scripts, or FFmpeg lists no `jpegxs` codec.
2. **GPU vs CPU display:** export `GALLIUM_DRIVER=d3d12` in the view script so `glimagesink` uses the GPU even from a non-login shell (the system-wide `/etc/profile.d` setting only covers login shells).
3. **Pixel format:** force `format=yuv422p` on encode; `testsrc` defaults to 4:2:0, which SVT-JPEG-XS rejects (`data_yuv[1] is NULL`).
4. **High-rate UDP drops:** set sender `pkt_size` and large socket buffers (e.g. `?pkt_size=1316&buffer_size=...`), and receiver `fifo_size`/`overrun_nonfatal`, plus `-fflags nobuffer -flags low_delay` for low latency. At ~381 Mbit/s untuned UDP loses packets → visible corruption.
5. **Join-any-time:** MPEG-TS is self-describing, so the viewer can attach to a running sender; start order does not matter.
6. **Line endings:** `.sh` files are covered by the repo `.gitattributes` `*.sh eol=lf` rule (CRLF breaks WSL bash).

## 7. Testing

- **Non-visual smoke:** run sender, then `ffmpeg -i udp://127.0.0.1:5008 -frames:v 60 -f null -` — confirms encode→TS→UDP→decode end-to-end with no display.
- **GPU assertion:** `jxs-stream-view.sh` run with `GST_DEBUG=glcontext:4` must report `GL_RENDERER: D3D12 (NVIDIA GeForce RTX 2080 Ti)`.
- **Visual confirmation:** the moving SMPTE-style test pattern appears in a native window at 1080p, smooth at ~59.94 fps (human-verified — not observable headlessly).
- **Bandwidth sanity:** `ffprobe` reports stream bitrate in the expected range for the chosen bpp.

## 8. Deliverables

- `pc/jxs-stream-send.sh` — JPEG-XS MPEG-TS/UDP sender (synthetic source).
- `pc/jxs-stream-view.sh` — reusable JPEG-XS receiver + GPU display.
- Both committed; documented in the project notes (RESUME.md "on-demand" section).

## 9. Future (not now)

- 10-bit 4:2:2 for true broadcast fidelity.
- Multicast on the island; view from the iPad/another host.
- A real JPEG-XS source on an x86 box (the Pi cannot encode JPEG-XS without an arm64 codec port — only the reference `libjxs` could, at uncertain performance).
