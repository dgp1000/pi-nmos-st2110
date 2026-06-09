# Design: audio-follows-the-take on the iPad monitor

**Date:** 2026-06-08
**Status:** Approved (design); pending implementation plan.
**Builds on:** `2026-06-08-is05-switchable-monitor-design.md` (`monitor-web.py`), `pc/jxs-stream-send-file.sh`, and the existing ST 2110-30 audio flow (`pc/activation-watcher.py` caps).

## 1. Goal & scope

Add sound to the iPad monitor. The MJPEG video path is unchanged; a **parallel AAC audio stream** is added that the page plays via an `<audio>` element and that **follows the IS-05 source selection**:

- **JPEG-XS** selected → the clip's audio (carried in the JPEG-XS MPEG-TS).
- **Pi raw** selected → the Pi's ST 2110-30 L24 audio flow (`239.10.10.10:5004`).

A/V are loosely synced (two independent streams) — accepted for a monitoring use-case.

### In scope
- `pc/jxs-stream-send-file.sh`: carry the source file's audio as AAC in the MPEG-TS (currently dropped with `-an`).
- `pc/monitor-web.py`: a new `/audio` endpoint (follows `active_src()`), an `<audio>` element, and a mute/unmute control.

### Out of scope (explicitly)
- Tight lip-sync / single muxed stream (would require the HLS rebuild the user declined).
- Audio for the synthetic `jxs-stream-send.sh` testsrc sender (no source audio; stays silent).
- Changing the video path, IS-05 logic, or PTP timecode.

## 2. Why this shape

The switchable monitor already follows the active source for video via `/stream` + `active_src()`. Audio mirrors that exactly with a second endpoint `/audio`, so the page becomes full AV-follow with minimal new surface. Keeping audio a separate stream preserves the low-latency MJPEG video and avoids HLS. Verified feasibility: ffmpeg here has the native `aac` encoder (no mp3), and a JPEG-XS MPEG-TS carries AAC stereo audio cleanly (probed: `jpegxs` + `aac` 2ch; audio extracted to ADTS fine).

## 3. Architecture

### 3.1 Sender change — `pc/jxs-stream-send-file.sh`
Map and encode the file's audio into the MPEG-TS alongside JPEG-XS video:
- Replace `-an` with explicit maps + AAC: `-map 0:v:0 -map 0:a:0? … -c:a aac -b:a 160k -ac 2`.
- `0:a:0?` (optional audio map) so files without an audio track still work (video-only).
- Everything else (scale/fps/format, `-c:v jpegxs -bpp`, mpegts/udp multicast params) unchanged.

### 3.2 Monitor — `pc/monitor-web.py`
New route **`/audio`** that follows `active_src()` and streams **AAC-ADTS over HTTP** (`Content-Type: audio/aac`, `Cache-Control: no-cache`), via an `audio_cmd(src)` helper:

| Active source | `/audio` pipeline |
|---|---|
| `jxs` | `ffmpeg -hide_banner -loglevel error -fflags nobuffer -i 'udp://239.10.10.22:5008?localaddr=10.10.10.2&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864' -vn -c:a aac -b:a 160k -f adts -` |
| `raw` | `gst-launch-1.0 -q udpsrc address=239.10.10.10 port=5004 multicast-iface=eth1 auto-multicast=true caps='application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96' ! rtpjitterbuffer latency=100 ! rtpL24depay ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,channels=2,rate=48000 ! fdsink fd=1` piped to `ffmpeg -hide_banner -loglevel error -f s16le -ar 48000 -ac 2 -i - -c:a aac -b:a 160k -f adts -` |

- Same subprocess lifecycle as `/stream`: `shell=True`, `start_new_session=True`, process-group SIGTERM/SIGKILL + `wait()` on disconnect.
- The L24 caps match `activation-watcher.py` exactly.

### 3.3 Page
- Add `<audio id="aud" autoplay>` (no `controls`; hidden) and a small 🔊/🔇 toggle button in `#ctrl`.
- The existing `take(src,…)` handler, after switching video, also reconnects audio: `document.getElementById('aud').src = '/audio?t='+Date.now()`. The button tap is a user gesture, satisfying iPad autoplay-with-sound.
- Mute toggle flips `aud.muted` and the button label.
- On initial load, audio starts muted (iPad blocks unsolicited sound); the first source-button tap or the unmute toggle starts it.

## 4. Data flow

```
IS-05 take (button) -> /take -> enable v0|m0
page reloads /stream (video, MJPEG) AND /audio (AAC)
  /audio reads active_src():
    jxs -> ffmpeg(udp mpegts) -vn -> AAC ADTS -> <audio>
    raw -> gst(L24 depay -> PCM) | ffmpeg -> AAC ADTS -> <audio>
```

## 5. Error handling / notes

1. **No audio on the active source** (testsrc sender, or Pi audio off): the `/audio` pipeline yields EOF/silence; the page plays nothing and the video is unaffected.
2. **Optional audio map** (`0:a:0?`) keeps the sender working on video-only files.
3. **Safari transport:** progressive AAC-ADTS via `<audio src>`. If a future iPad test shows Safari won't play a continuous ADTS stream, the fallback is audio-only HLS from the same `/audio` source (documented, not built now).
4. **Autoplay:** iPad requires a user gesture for sound; the source buttons / unmute toggle provide it. Start muted.
5. **Loose sync:** independent video/audio streams; lip-sync may drift — accepted.
6. **CPU:** `/audio` adds a light decode/encode (AAC is cheap); the jxs video decode is the heavy part and is unchanged.

## 6. Testing

- **Sender:** with audio added, `ffprobe` the JPEG-XS group shows `jpegxs` + `aac` streams.
- **/audio jxs:** with the file sender (audio) live and jxs selected, `curl /audio` returns `Content-Type: audio/aac` with bytes flowing.
- **/audio raw:** with the Pi ST 2110-30 audio live and raw selected, `curl /audio` returns `audio/aac` with bytes flowing.
- **Switch:** selecting jxs vs raw swaps which audio `/audio` serves.
- **iPad (human):** after a button tap, sound plays and follows the take; mute toggle works; video still smooth.

## 7. Deliverables

- `pc/jxs-stream-send-file.sh` (modified — carry AAC audio).
- `pc/monitor-web.py` (modified — `/audio` route, `audio_cmd`, `<audio>` element + mute toggle).
- `docs/superpowers/RESUME.md` (note the monitor now carries audio).

## 8. Future (not now)

- Audio-only HLS fallback if Safari rejects progressive ADTS.
- Tight lip-sync via a single muxed HLS/WebRTC AV stream.
- A tone on the synthetic testsrc sender so jxs always has audio even without a file.
