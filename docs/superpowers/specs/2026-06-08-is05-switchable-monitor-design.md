# Design: IS-05 source-switchable iPad monitor (with PTP)

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan.
**Builds on:** `video-web.py` (MJPEG + PTP monitor), `take.py` / `activation-watcher.py` (IS-05 gate pattern), and the JPEG-XS island multicast (`2026-06-08-jpegxs-island-multicast-design.md`).

## 1. Goal & scope

Add an iPad-viewable web monitor whose **video source is switched via real IS-05 takes** between two sources, with PTP timecode throughout:

- **Pi raw ST 2110-20** — `239.10.10.20:5005` (RFC 4175), NMOS receiver `v0`.
- **PC JPEG-XS (MPEG-TS)** — `239.10.10.22:5008`, NMOS receiver `m0` (mux).

Buttons on the page issue the IS-05 take; the view follows whichever receiver is `master_enable=TRUE`. This is the project's "video under NMOS control" goal, made concrete.

### In scope
- New `pc/monitor-web.py` on port **8096** (leaves `video-web.py` / `:8095` intact).
- Real IS-05 PATCH takes on receivers `v0` and `m0` (gate model, mirroring `take.py`).
- MJPEG transcode of the active source + PTP timecode overlay, reachable from the iPad over WiFi.

### Out of scope (explicitly)
- Full NMOS sender↔receiver connection with transport_params negotiation. Like the existing audio take, IS-05 here is a **gate/selector**: each receiver maps to a fixed flow; the take enables one and disables the other.
- GPU codec / changing the JPEG-XS or raw pipelines.
- Starting the sources. The monitor only consumes; the Pi (`launch-all.sh`) and the PC JPEG-XS sender (`jxs-stream-send*.sh`) must already be streaming.

## 2. Why this shape

`video-web.py` already proves the hard parts: a threaded HTTP server streaming `multipart/x-mixed-replace` MJPEG, a PTP-timecode page driven by the Pi clock (`/time` proxy), and GStreamer transcode of the raw flow. `take.py` proves the IS-05 take (resolve receiver by label → PATCH `/staged` `master_enable` + `activate_immediate`). The new monitor composes these: same server/page/PTP, plus a `/take` endpoint and a `/stream` that follows the active receiver and runs one of two transcode pipelines. A separate file/port keeps the working codec-A/B monitor untouched.

## 3. Architecture — `pc/monitor-web.py`

Threaded HTTP server on `0.0.0.0:8096` with these routes:

- **`/`** — page: big PTP timecode, `<img src="/stream">`, two buttons (**Pi raw 2110-20**, **PC JPEG-XS**), PTP/grandmaster info line.
- **`/take?src=raw|jxs`** — issues IS-05 takes: enable the chosen receiver, disable the other; returns JSON `{active: "raw"|"jxs"}`.
- **`/stream`** — determines the active source (whichever of `v0`/`m0` has `master_enable=TRUE`; if neither, default to last selected), then serves MJPEG from the matching transcode subprocess.
- **`/time`** — Pi-clock proxy (`http://10.10.10.1:8000/time`), unchanged from `video-web.py`.

### Source → receiver → transcode mapping

| Button | NMOS receiver (by label) | Flow | Transcode for `/stream` |
|---|---|---|---|
| Pi raw | `easy-nmos-node/receiver/v0` (video) | `239.10.10.20:5005` RFC 4175 | `gst udpsrc(caps RAW) ! rtpjitterbuffer ! rtpvrawdepay ! videoconvert ! videoscale ! video/x-raw,width=640,height=480 ! jpegenc quality=85 ! multipartmux boundary=... ! fdsink` |
| PC JPEG-XS | `easy-nmos-node/receiver/m0` (mux) | `239.10.10.22:5008` MPEG-TS | `ffmpeg -i udp://239.10.10.22:5008?localaddr=10.10.10.2... -f rawvideo -pix_fmt yuv422p -` piped to `gst fdsrc ! rawvideoparse format=Y42B,1920x1080,60000/1001 ! videoscale ! video/x-raw,width=640,height=480 ! jpegenc quality=85 ! multipartmux boundary=... ! fdsink` |

Both paths emit the same multipart boundary, so the server streams either identically.

### Switching flow

```
iPad page button "PC JPEG-XS"
  -> POST /take?src=jxs
       server: PATCH receiver m0 /staged {master_enable:true, activation:activate_immediate}
               PATCH receiver v0 /staged {master_enable:false, activation:activate_immediate}
  -> page sets img.src = /stream?t=<now>
       server /stream: reads /active for v0,m0 -> m0 enabled -> runs the JPEG-XS transcode
  -> iPad shows JPEG-XS island flow; PTP timecode continues
```

## 4. IS-05 details (verified 2026-06-08)

- Resolve receiver IDs by label each call (virtnode regenerates UUIDs): `GET {NODE}/receivers`, match `label`.
- Take: `PATCH {CONN}/single/receivers/{id}/staged` with `{"master_enable": <bool>, "activation": {"mode": "activate_immediate"}}` → HTTP 200.
- Read active: `GET {CONN}/single/receivers/{id}/active` → `master_enable`.
- Verified: take ON/OFF on `v0` returns 200 and flips `/active master_enable` true/false.
- Endpoints: `NODE=http://localhost:8090/x-nmos/node/v1.3`, `CONN=http://localhost:8090/x-nmos/connection/v1.1`.

## 5. Error handling / notes

1. **Receiver resolution by label** — robust to virtnode UUID regen (same as `take.py`).
2. **Independent multicast receiver** — the JPEG-XS monitor decode joins `239.10.10.22` directly; it does not need the desktop viewer running. Adds ~1 CPU core while JPEG-XS is selected (FFmpeg decode).
3. **iPad reachability** — server binds `0.0.0.0:8096`; the iPad connects over WiFi to the PC's WiFi IP (`192.168.4.85:8096`). Requires a one-time Windows inbound firewall rule for TCP 8096 (elevated PowerShell). PTP still works (fetched server-side from the island, relayed via `/time`).
4. **Black view** — if the selected source isn't streaming, the view is black (expected); the other source/button still works.
5. **`LD_LIBRARY_PATH` not needed** — `libSvtJpegxs` is system-installed (`jxs-install-libs.sh`), so the monitor's FFmpeg finds the `jpegxs` decoder without it.
6. **Defaults** — on first load, `/take?src=jxs` (JPEG-XS) is the default selection.

## 6. Testing

- **IS-05:** `curl /take?src=raw` then `/take?src=jxs`; confirm `/active` for `v0`/`m0` flips accordingly (HTTP 200).
- **Stream raw:** with the Pi raw flow live and `raw` selected, `/stream` yields MJPEG (HTTP 200, multipart) showing the Pi pattern.
- **Stream jxs:** with the JPEG-XS sender live and `jxs` selected, `/stream` yields MJPEG showing the JPEG-XS content.
- **PTP:** the timecode advances and the grandmaster/offset line populates (Pi clock reachable).
- **Switch:** pressing the buttons swaps the live view within ~1–2 s.
- **iPad:** after the firewall rule, `http://192.168.4.85:8096` loads, shows video + timecode, and the buttons switch source. (Human-verified.)

## 7. Deliverables

- `pc/monitor-web.py` — the IS-05 source-switchable monitor (new).
- `pc/open-monitor-firewall.ps1` — one-line helper to add the inbound TCP 8096 rule (run elevated).
- `docs/superpowers/RESUME.md` — document the switchable monitor.

## 8. Future (not now)

- Fold the codec-A/B (`video-web.py`) and this switchable monitor into one page; retire `:8095`.
- Add the audio receiver to the same page (full AV take).
- Real IS-05 sender↔receiver connection (transport_params) instead of the gate model.
