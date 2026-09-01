#!/usr/bin/env python3
# ===========================================================================
#  Atoll island flow analyser -- a live, per-flow view of every multicast group on the island:
#  bitrate, PACKET RATE, average datagram size, and for RTP flows the payload type, SSRC and
#  packets actually lost (from sequence-number gaps).
#
#  Packet rate is the metric that matters most here and the one nothing else showed: WSL's mirrored
#  networking limits multicast RECEIVE by packets per second, not bandwidth, which is what caps
#  uncompressed 2110-20 and JPEG XS on this rig. Measuring it ad hoc is how the mpegtsmux
#  alignment=7 fix was found (Live TV was burning 4,470 pps on 188-byte datagrams for 6.7 Mbit/s;
#  bundling 7 TS packets per datagram cut it to 653 pps). This makes that view permanent.
#
#  Deliberately raw sockets, not GStreamer: we only need to count and read RTP headers, so there is
#  no decode cost and the analyser cannot disturb the flows it watches.
#
#  Serves http://<host>:8101/  (HTML dashboard) and /flows (JSON).
# ===========================================================================
import socket, struct, threading, time, json, subprocess, os, select
import http.server, socketserver, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["ISLAND_IFACE", "ISLAND_PC_IP", "ISLAND_PI_IP", "ANALYSER_PORT",
        "HEVC_GRP", "HEVC_PORT", "HOME_GRP", "HOME_PORT", "MUSIC_GRP", "MUSIC_PORT",
        "REELS_GRP", "REELS_PORT", "PI_RAW_GRP", "PI_RAW_PORT", "PI_AUDIO_GRP", "PI_AUDIO_PORT",
        "ANC_GRP", "ANC_PORT", "J2K_GRP", "J2K_PORT", "H264_GRP", "H264_PORT",
        "OPUS_GRP", "OPUS_PORT", "MJPEG_GRP", "MJPEG_PORT", "VP9_GRP", "VP9_PORT",
        "TSRTP_GRP", "TSRTP_PORT", "FEC_GRP", "FEC_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
LOCAL = CFG.get("ISLAND_PC_IP") or "0.0.0.0"
PORT = int(CFG.get("ANALYSER_PORT") or 8101)
PI = CFG.get("ISLAND_PI_IP", "10.10.10.1")

def g(k):
    return CFG.get(f"{k}_GRP", ""), CFG.get(f"{k}_PORT", "")

# label, group, port, standard/description
FLOWS = [
    ("Live TV",        *g("HEVC"),   "HEVC in MPEG-TS / UDP"),
    ("Home videos",    *g("HOME"),   "HEVC in MPEG-TS / UDP"),
    ("Music",          *g("MUSIC"),  "HEVC in MPEG-TS / UDP"),
    ("Test Reels",     *g("REELS"),  "HEVC in MPEG-TS / UDP"),
    ("Pi raw video",   *g("PI_RAW"), "ST 2110-20 uncompressed (RFC 4175)"),
    ("Pi audio",       *g("PI_AUDIO"), "ST 2110-30 L24 PCM (AES67)"),
    ("Ancillary",      *g("ANC"),    "ST 2110-40 ATC timecode (RFC 8331)"),
    ("JPEG 2000",      *g("J2K"),    "J2K over RTP (RFC 5371)"),
    ("H.264",          *g("H264"),   "H.264 over RTP (RFC 6184)"),
    ("Opus",           *g("OPUS"),   "Opus over RTP (RFC 7587)"),
    ("MJPEG",          *g("MJPEG"),  "Motion JPEG over RTP (RFC 2435)"),
    ("VP9",            *g("VP9"),    "VP9 over RTP (RFC 7741)"),
    ("TS over RTP",    *g("TSRTP"),  "MPEG-TS over RTP (ST 2022-2)"),
    ("FEC media",      *g("FEC"),    "ST 2022-1 protected media"),
]
_fg, _fp = g("FEC")
if _fg and _fp:
    FLOWS.append(("FEC column", _fg, str(int(_fp) + 2), "ST 2022-1 column FEC"))
    FLOWS.append(("FEC row",    _fg, str(int(_fp) + 4), "ST 2022-1 row FEC"))
FLOWS = [f for f in FLOWS if f[1] and f[2]]

class Flow:
    __slots__ = ("label", "grp", "port", "std", "sock", "pkts", "bytes", "seq", "lost", "reorder",
                 "pt", "ssrc", "is_rtp", "last", "rate_pps", "rate_bps", "avg", "loss_pct", "total_lost")

    def __init__(self, label, grp, port, std):
        self.label, self.grp, self.port, self.std = label, grp, int(port), std
        self.pkts = self.bytes = 0
        self.seq = None
        self.lost = self.reorder = self.total_lost = 0
        self.pt = self.ssrc = None
        self.is_rtp = False
        self.last = 0.0
        self.rate_pps = self.rate_bps = self.avg = self.loss_pct = 0.0
        self.sock = self._join()

    def _join(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
        # Large receive buffer: at ~12k pps island-wide the analyser must not drop packets itself,
        # or it would report its own overflow as network loss. Verified by raising this and watching
        # the reported loss on the high-rate flows fall to zero.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        except OSError:
            pass
        s.bind(("", self.port))
        mreq = struct.pack("4s4s", socket.inet_aton(self.grp), socket.inet_aton(LOCAL))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setblocking(False)
        return s

    def feed(self, d):
        self.pkts += 1
        self.bytes += len(d)
        self.last = time.time()
        if len(d) < 12 or (d[0] >> 6) != 2:      # not RTP (e.g. bare TS in UDP)
            return
        self.is_rtp = True
        self.pt = d[1] & 0x7F
        self.ssrc = struct.unpack("!I", d[8:12])[0]
        s = struct.unpack("!H", d[2:4])[0]
        if self.seq is not None:
            d16 = (s - self.seq - 1) & 0xFFFF
            if d16 == 0:
                pass                              # in order
            elif d16 < 1000:                      # a real forward gap = that many lost
                self.lost += d16
                self.total_lost += d16
            else:
                self.reorder += 1                 # late/duplicate rather than a huge jump
        self.seq = s

flows = [Flow(*f) for f in FLOWS]
by_fd = {f.sock.fileno(): f for f in flows}

def reader():
    socks = [f.sock for f in flows]
    while True:
        try:
            r, _, _ = select.select(socks, [], [], 0.5)
        except Exception:
            time.sleep(0.2); continue
        for s in r:
            f = by_fd.get(s.fileno())
            while True:                            # drain this socket
                try:
                    d = s.recv(2048)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if f:
                    f.feed(d)

def sampler():
    prev = {f.label: (0, 0, 0) for f in flows}
    t0 = time.time()
    while True:
        time.sleep(1.0)
        now = time.time()
        dt = now - t0 or 1.0
        t0 = now
        for f in flows:
            pp, bb, ll = prev[f.label]
            dp, db, dl = f.pkts - pp, f.bytes - bb, f.total_lost - ll
            prev[f.label] = (f.pkts, f.bytes, f.total_lost)
            f.rate_pps = dp / dt
            f.rate_bps = db * 8 / dt
            f.avg = (db / dp) if dp else 0.0
            f.loss_pct = (100.0 * dl / (dp + dl)) if (dp + dl) else 0.0

def ptp_status():
    try:
        with urllib.request.urlopen(f"http://{PI}:8000/time", timeout=1.5) as r:
            return {"gm": PI, "reachable": True, "time": r.read().decode()[:64]}
    except Exception:
        return {"gm": PI, "reachable": False, "time": ""}

def snapshot():
    now = time.time()
    out = []
    for f in flows:
        alive = (now - f.last) < 3.0 if f.last else False
        out.append({"label": f.label, "grp": f.grp, "port": f.port, "std": f.std,
                    "alive": alive, "pps": round(f.rate_pps), "mbps": round(f.rate_bps / 1e6, 2),
                    "avg": round(f.avg), "rtp": f.is_rtp, "pt": f.pt,
                    "ssrc": (f"{f.ssrc:08x}" if f.ssrc is not None else None),
                    "lost": f.total_lost, "loss_pct": round(f.loss_pct, 3), "reorder": f.reorder})
    tot_p = sum(x["pps"] for x in out)
    return {"flows": out, "total_pps": tot_p,
            "total_mbps": round(sum(x["mbps"] for x in out), 2),
            "ptp": ptp_status(), "ts": time.strftime("%H:%M:%S")}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atoll island analyser</title><style>
 :root{color-scheme:dark}
 body{margin:0;background:#050a06;color:#cfe;font:14px/1.4 ui-monospace,Menlo,Consolas,monospace}
 header{padding:14px 18px;border-bottom:1px solid #143;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
 h1{margin:0;font-size:17px;color:#0f0;letter-spacing:.14em;font-weight:600}
 .sub{color:#5a7;font-size:12px}
 .tot{margin-left:auto;color:#3c9}
 .wrap{overflow-x:auto;padding:0 8px 24px}
 table{width:100%;border-collapse:collapse;min-width:900px}
 th{position:sticky;top:0;background:#071008;text-align:left;color:#5a5;font-weight:400;
   font-size:11px;text-transform:uppercase;letter-spacing:.09em;padding:9px 10px;border-bottom:1px solid #143}
 td{padding:7px 10px;border-bottom:1px solid #0c1a0c;white-space:nowrap;font-variant-numeric:tabular-nums}
 tr:hover td{background:#08150a}
 .n{text-align:right}
 .lbl{color:#dfe;font-weight:600}
 .std{color:#4a6b52;font-size:12px}
 .grp{color:#39a}
 .dead{color:#a55}
 .warn{color:#fc0}
 .bad{color:#f55;font-weight:700}
 .ok{color:#2d2}
 .pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;border:1px solid #1a3a2a;color:#7a9}
 .hi{background:#3a1010;border-color:#803;color:#f88}
 footer{padding:10px 18px;color:#476;font-size:12px;border-top:1px solid #0c1a0c}
</style></head><body>
<header><h1>ATOLL</h1><span class="sub">island flow analyser &middot; 10.10.10.0/24</span>
 <span id="ptp" class="sub"></span><span class="tot" id="tot"></span></header>
<div class="wrap"><table><thead><tr>
 <th>flow</th><th>group : port</th><th class="n">pps</th><th class="n">Mbit/s</th>
 <th class="n">avg pkt</th><th>transport</th><th class="n">lost</th><th class="n">loss %</th><th>standard</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<footer id="ft">&nbsp;</footer>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function load(){
 let d; try{d=await(await fetch('/flows',{cache:'no-store'})).json();}catch(e){return;}
 document.getElementById('tot').textContent=d.total_pps.toLocaleString()+' pps total \\u00b7 '+d.total_mbps+' Mbit/s';
 const p=d.ptp; document.getElementById('ptp').innerHTML='PTP GM '+esc(p.gm)+' '+
   (p.reachable?'<span class="ok">locked</span>':'<span class="bad">unreachable</span>');
 document.getElementById('rows').innerHTML=d.flows.map(f=>{
  const dead=!f.alive;
  const lossCls=f.loss_pct>1?'bad':(f.loss_pct>0?'warn':'ok');
  const tp=f.rtp?('<span class="pill">RTP pt '+f.pt+'</span> <span class="std">ssrc '+esc(f.ssrc)+'</span>')
                :'<span class="pill">raw UDP</span>';
  const hi=(f.avg>0&&f.avg<400)?' class="pill hi" title="one TS packet per datagram - see mpegtsmux alignment"':'class="n"';
  return '<tr>'+
   '<td class="lbl'+(dead?' dead':'')+'">'+esc(f.label)+(dead?' &middot; no signal':'')+'</td>'+
   '<td class="grp">'+esc(f.grp)+' : '+f.port+'</td>'+
   '<td class="n">'+(dead?'&ndash;':f.pps.toLocaleString())+'</td>'+
   '<td class="n">'+(dead?'&ndash;':f.mbps.toFixed(2))+'</td>'+
   '<td '+(dead?'class="n"':hi)+'>'+(dead?'&ndash;':f.avg)+'</td>'+
   '<td>'+(dead?'':tp)+'</td>'+
   '<td class="n">'+(f.rtp?f.lost.toLocaleString():'&ndash;')+'</td>'+
   '<td class="n '+lossCls+'">'+(f.rtp?f.loss_pct.toFixed(2):'&ndash;')+'</td>'+
   '<td class="std">'+esc(f.std)+'</td></tr>';
 }).join('');
 document.getElementById('ft').textContent='updated '+d.ts+
   '  \\u00b7  avg pkt under ~400 B is highlighted: that is one 188-byte TS packet per datagram, which burns packet rate (the island\\u2019s real ceiling) for no bandwidth gain.';
}
load(); setInterval(load,1000);
</script></body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/flows"):
            self._send(json.dumps(snapshot()).encode(), "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

threading.Thread(target=reader, daemon=True).start()
threading.Thread(target=sampler, daemon=True).start()
print(f"analyser: watching {len(flows)} island flows -> http://0.0.0.0:{PORT}/", flush=True)
TCPServer(("", PORT), H).serve_forever()
