#!/usr/bin/env python3
"""Atoll loudness meter -- EBU R128 / ITU-R BS.1770 on the PROGRAM audio (:8104).

Follows the panel's active source, decodes its audio to F32 stereo 48 kHz via an appsink, and runs
the numpy BS.1770 meter (bs1770.py): Momentary (400 ms), Short-term (3 s) and gated Integrated LUFS,
plus a max short-term and an EBU R128 in-spec check (target -23 LUFS, +/-1 LU). Serves /loudness JSON
and a broadcast-style web readout. A real "legal for air?" QC tool, decoupled from the display.
"""
import gi, os, sys, subprocess, threading, time, json, http.server, socketserver, urllib.request
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bs1770
Gst.init(None)

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "PANEL_PORT", "LOUDNESS_PORT",
        "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT", "MUSIC_GRP", "MUSIC_PORT",
        "MUSIC_AUDIO_GRP", "MUSIC_AUDIO_PORT", "REELS_GRP", "REELS_PORT",
        "PI_AUDIO_GRP", "PI_AUDIO_PORT", "TSRTP_GRP", "TSRTP_PORT", "OPUS_GRP", "OPUS_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
IFACE = CFG["ISLAND_IFACE"] or "eth0"
PORT = int(CFG.get("LOUDNESS_PORT") or 8104)
PANEL = f"http://localhost:{CFG.get('PANEL_PORT','8096')}"
def _g(k): return CFG[f"{k}_GRP"], CFG[f"{k}_PORT"]

TARGET, TOL = -23.0, 1.0   # EBU R128 target and tolerance (LUFS / LU)
APP = "audioconvert ! audioresample ! audio/x-raw,format=F32LE,channels=2,rate=48000 ! appsink name=sink emit-signals=true max-buffers=8 drop=false sync=false"

def audio_desc(src):
    if src in ("hevc", "jxs", "reels", "tsrtp"):
        if src == "tsrtp":
            g, p = _g("TSRTP")
            head = (f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 '
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=d')
        else:
            g, p = _g("HEVC" if src == "hevc" else ("HOME" if src == "jxs" else "REELS"))
            head = f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=8388608 ! tsdemux name=d'
        return f'{head} d. ! audio/mpeg ! queue ! decodebin ! {APP}'
    if src in ("music", "raw"):
        g, p = _g("MUSIC_AUDIO" if src == "music" else "PI_AUDIO")
        return (f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true buffer-size=16777216 '
                f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96" '
                f'! rtpjitterbuffer latency=500 ! rtpL24depay ! {APP}')
    if src == "h264":
        g, p = _g("OPUS")
        return (f'udpsrc address={g} port={p} multicast-iface={IFACE} auto-multicast=true '
                f'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97" '
                f'! rtpjitterbuffer latency=200 ! rtpopusdepay ! opusdec ! {APP}')
    return None

class Meter:
    def __init__(self):
        self.src = None; self.pipe = None
        self.m = bs1770.Loudness()
        self.max_st = -np.inf
        self._lock = threading.Lock()
        GLib.timeout_add_seconds(1, self._poll)

    def _on_sample(self, sink):
        s = sink.emit("pull-sample")
        if not s: return Gst.FlowReturn.OK
        buf = s.get_buffer(); ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            a = np.frombuffer(mi.data, dtype=np.float32)
            buf.unmap(mi)
            if a.size >= 2:
                frames = a.reshape(-1, 2)
                with self._lock:
                    self.m.add(frames)
                    st = self.m.short_term()
                    if np.isfinite(st) and st > self.max_st:
                        self.max_st = st
        return Gst.FlowReturn.OK

    def _build(self, src):
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL); self.pipe = None
        with self._lock:
            self.m = bs1770.Loudness(); self.max_st = -np.inf
        desc = audio_desc(src)
        if not desc:
            self.src = src; return
        self.pipe = Gst.parse_launch(desc)
        self.pipe.get_by_name("sink").connect("new-sample", self._on_sample)
        self.pipe.set_state(Gst.State.PLAYING)
        self.src = src
        print(f"{time.strftime('%T')} loudness: measuring '{src}'", flush=True)

    def _poll(self):
        try:
            with urllib.request.urlopen(f"{PANEL}/state", timeout=2) as r:
                st = json.load(r)
            src = st.get("active")
            # program layout: follow the Program Out route instead
            if st.get("layout") == "program":
                try:
                    with urllib.request.urlopen(f"http://localhost:8092/programout", timeout=2) as r2:
                        src = json.load(r2).get("essence") or src
                except Exception: pass
            if src and src != self.src:
                self._build(src)
        except Exception:
            pass
        return True

    def snapshot(self):
        with self._lock:
            mo, st, it = self.m.momentary(), self.m.short_term(), self.m.integrated()
        def f(x): return round(float(x), 1) if np.isfinite(x) else None
        st_v = f(st)
        return {"source": self.src, "momentary": f(mo), "short_term": st_v, "integrated": f(it),
                "max_short_term": f(self.max_st), "target": TARGET, "tolerance": TOL,
                "in_spec": (st_v is not None and abs(st_v - TARGET) <= TOL),
                "ts": time.strftime("%H:%M:%S")}

meter = Meter()

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atoll Loudness</title>
<style>
 html,body{margin:0;height:100%;background:#0b0f0d;color:#dfe;font-family:'Segoe UI',system-ui,sans-serif}
 #wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:2vh}
 #src{color:#6b9;letter-spacing:.15em;text-transform:uppercase;font-size:min(3vw,3.5vh)}
 #big{font-size:min(20vw,26vh);font-weight:800;line-height:.9;font-variant-numeric:tabular-nums}
 #big .u{font-size:.3em;color:#789;font-weight:600}
 #lab{color:#789;font-size:min(2.4vw,3vh);letter-spacing:.1em}
 #row{display:flex;gap:4vw;margin-top:1vh}
 .kv{text-align:center}.kv .k{color:#789;font-size:min(1.8vw,2.2vh);letter-spacing:.08em}
 .kv .v{font-size:min(4vw,5vh);font-weight:700;font-variant-numeric:tabular-nums}
 #spec{margin-top:2vh;font-size:min(2.6vw,3vh);font-weight:700;padding:.4em 1.2em;border-radius:8px}
 .ok{background:#093;color:#000}.hi{background:#c22;color:#fff}.lo{background:#c72;color:#fff}.na{background:#333;color:#999}
</style></head><body><div id="wrap">
 <div id="src">program &middot; <span id="srcn">&mdash;</span></div>
 <div id="big"><span id="stv">--</span><span class="u"> LUFS</span></div>
 <div id="lab">SHORT-TERM (3s)</div>
 <div id="row">
  <div class="kv"><div class="k">MOMENTARY</div><div class="v" id="mo">--</div></div>
  <div class="kv"><div class="k">INTEGRATED</div><div class="v" id="it">--</div></div>
  <div class="kv"><div class="k">MAX S-T</div><div class="v" id="mx">--</div></div>
 </div>
 <div id="spec" class="na">&mdash;</div>
</div>
<script>
async function tick(){
 let d; try{d=await(await fetch('/loudness',{cache:'no-store'})).json();}catch(e){return;}
 const g=id=>document.getElementById(id);
 g('srcn').textContent=d.source||'\\u2014';
 const fmt=v=>v==null?'--':v.toFixed(1);
 g('stv').textContent=fmt(d.short_term); g('mo').textContent=fmt(d.momentary);
 g('it').textContent=fmt(d.integrated); g('mx').textContent=fmt(d.max_short_term);
 const sp=g('spec');
 if(d.short_term==null){sp.className='na';sp.textContent='\\u2014 no audio';}
 else if(d.in_spec){sp.className='ok';sp.textContent='\\u2713 EBU R128 in spec ('+d.target+' \\u00b1'+d.tolerance+' LU)';}
 else if(d.short_term>d.target){sp.className='hi';sp.textContent='TOO LOUD ('+(d.short_term-d.target).toFixed(1)+' LU over '+d.target+')';}
 else {sp.className='lo';sp.textContent='TOO QUIET ('+(d.target-d.short_term).toFixed(1)+' LU under '+d.target+')';}
}
tick(); setInterval(tick,500);
</script></body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/loudness"):
            b = json.dumps(meter.snapshot()).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","*"); self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b)
        else:
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)

class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True

def _http():
    Srv(("0.0.0.0", PORT), H).serve_forever()
threading.Thread(target=_http, daemon=True).start()
print(f"loudness meter (EBU R128 / BS.1770) on http://0.0.0.0:{PORT}/  -- follows the program audio", flush=True)
GLib.MainLoop().run()
