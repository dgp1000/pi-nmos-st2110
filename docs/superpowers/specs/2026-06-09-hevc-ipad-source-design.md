# Design: HEVC 4K as a third switchable iPad source

**Date:** 2026-06-09
**Status:** Approved (design); pending implementation plan.
**Touches:** `pc/monitor-web.py` only.
**Builds on:** the IS-05 source-switchable monitor (`monitor-web.py`) and the 4K HEVC GPU pipeline (`hevc-stream-*.sh`, multicast `239.10.10.65:5010`, MPEG-TS with HEVC video + optional MP3 audio).

## 1. Goal & scope

Let the iPad monitor switch **to and from the 4K HEVC island stream**, alongside the existing Pi-raw (ST 2110-20) and PC JPEG-XS sources. The HEVC source appears the same way as the others — a small **MJPEG** video preview plus **HLS** audio — selected by a third on-page button.

### In scope
- A third source `"hevc"` (`239.10.10.65:5010`, MPEG-TS) in `monitor-web.py`.
- GPU-decoded MJPEG transcode for `/stream`, AAC-HLS audio for `/hls/aud.m3u8`.
- A third button; exclusive switching among raw / jxs / hevc.

### Out of scope (explicitly)
- Native HLS-video delivery for HEVC (chose consistent MJPEG preview instead).
- Registering HEVC as an NMOS flow/receiver (it stays a plain GStreamer multicast).
- Changing the native fullscreen viewer (`hevc-stream-view*.sh`) — that remains the real 4K view.
- Audio for the *testsrc* HEVC sender (it has none; only the file sender carries MP3).

## 2. The key difference: HEVC is not an NMOS flow

raw (`v0`) and jxs (`m0`) are NMOS receivers switched via real IS-05 takes (PATCH `master_enable`). HEVC has **no NMOS receiver**, so there's nothing to "take". Switching to HEVC instead:
- disables the raw + jxs NMOS receivers (clean exclusive state), and
- points the monitor's transcode at the HEVC multicast.

This requires two small behavioural changes (below) so the no-label source is handled.

## 3. Changes (all in `pc/monitor-web.py`)

1. **`SOURCES`** — add:
   ```python
   "hevc": {"label": None, "group": "239.10.10.65", "port": 5010},
   ```

2. **`take(src)`** — skip the IS-05 call for label-less sources:
   ```python
   for key, s in SOURCES.items():
       if s["label"] is None:
           continue
       set_enable(s["label"], key == src)
   _active["src"] = src; _active["ts"] = time.monotonic()
   ```
   `take("hevc")` → both real receivers disabled, active=hevc. `take("raw"/"jxs")` → unchanged IS-05 behaviour.

3. **`active_src()`** — make a label-less active source sticky (the NMOS re-derive would otherwise drop it after the 2 s TTL):
   ```python
   if time.monotonic() - _active["ts"] < _ACTIVE_TTL:
       return _active["src"]
   if SOURCES.get(_active["src"], {}).get("label") is None:   # non-NMOS -> trust last take
       _active["ts"] = time.monotonic()
       return _active["src"]
   # ... existing NMOS re-derive for raw/jxs ...
   ```

4. **`stream_cmd("hevc")`** — GPU transcode to the shared MJPEG `tail`:
   ```
   gst-launch-1.0 -q udpsrc address=239.10.10.65 port=5010 multicast-iface={IFACE}
     auto-multicast=true ! tsdemux ! h265parse ! nvh265dec ! cudadownload
     ! videoconvert ! videoscale ! video/x-raw,width=640,height=480
     ! jpegenc quality=85 ! multipartmux boundary={BOUNDARY} ! fdsink fd=1
   ```
   (Decode 4K HEVC on NVDEC, downscale on CPU to the 640×480 preview — same `tail` as raw/jxs.)

5. **Audio** — add a `"hevc"` branch to `hls_cmd` (and `audio_cmd` for the legacy `/audio` route) that pulls the MP3 from the TS and encodes AAC:
   ```
   ffmpeg -hide_banner -loglevel error -fflags nobuffer
     -i 'udp://239.10.10.65:5010?localaddr=10.10.10.2&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864'
     -vn <hls flags>      # (or -f adts - for /audio)
   ```
   With the testsrc sender (no audio) ffmpeg finds no audio stream and the HLS simply stays empty → silent, which is correct.

6. **Page** — add one button after the existing two:
   ```html
   <button id="bhevc" onclick="take('hevc',this)">PC HEVC 4K</button>
   ```
   The existing `take(src,btn)` JS already reloads `/stream`, reconnects audio, and moves the `on` highlight — no other page change.

## 4. Data flow

```
iPad tap "PC HEVC 4K"
  -> GET /take?src=hevc  -> disables raw+jxs NMOS receivers, _active=hevc
  -> <img>=/stream       -> stream_cmd(hevc): udpsrc->tsdemux->nvh265dec->MJPEG 640x480
  -> <audio>=/hls/aud.m3u8 -> ensure_hls(hevc): TS MP3 -> AAC HLS
Switch back to raw/jxs -> normal IS-05 take + their existing transcodes.
```

## 5. Error handling / notes

- **HEVC sender must be running** (like the JXS sender). If it isn't, `/stream` produces no frames — same failure mode as the other sources with no flow.
- **Interface**: reuse the module's auto-detected `IFACE` (`island_iface()`), so the `eth0`/`eth1` rename is already handled.
- **Concurrent NVDEC**: the monitor's HEVC decode and the native viewer's decode can run at once (NVDEC handles concurrent sessions); typically only one is used.
- **No audio on testsrc HEVC**: expected; HLS stays silent. No special handling needed.
- **tsdemux audio pad** in `stream_cmd` is left unlinked (video-only transcode) — harmless.

## 6. Testing

- `GET /take?src=hevc` → `{"active":"hevc"}`; raw/jxs receivers report `master_enable=false`.
- `GET /stream` (with HEVC sender running) → multipart JPEG frames flow; `nvidia-smi` shows decoder util > 0.
- `GET /hls/aud.m3u8` with the **file** sender → valid playlist + segments (ffprobe decodes AAC); with the **testsrc** sender → silent (no audio stream).
- `active_src()` stays `hevc` past the 2 s TTL (sticky).
- iPad: tap **PC HEVC 4K** → preview + audio; tap **Pi raw** / **PC JPEG-XS** → switches back (IS-05 take fires).

## 7. Deliverables

- `pc/monitor-web.py` (SOURCES, take, active_src, stream_cmd, hls_cmd/audio_cmd, one page button).
- `docs/superpowers/RESUME.md` note (monitor now has 3 sources incl. HEVC).
