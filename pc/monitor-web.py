#!/usr/bin/env python3
"""IS-05 source switch panel + live IS-04/IS-05 inspector + PTP timecode, iPad-friendly.

Control: buttons issue real IS-05 takes to switch the active video receiver
between the Pi raw ST 2110-20 flow (NMOS receiver v0) and the PC JPEG-XS island
flow (NMOS receiver m0). The PC HEVC 4K island flow (239.10.10.65:5010) is not
an NMOS receiver, so selecting it just disables the NMOS receivers.

Inspector: /nmos aggregates IS-04 (registry Query API, :8080) with IS-05 active
connection params (node Connection API, :8090) and the page renders nodes,
receivers (switchable ones first) and senders -- formats, caps, transport
params (multicast addr/port, legs), subscriptions, enables. Served through :8096
so the iPad only needs one reachable port (no CORS / extra firewall holes).

Video/audio are viewed on the native PC window (pc/hevc-stream-view-file.sh,
pc/jxs-stream-view.sh) -- the old in-browser MJPEG/HLS preview was removed (it
tore on smooth motion on iOS; the native window is glitch-free). PTP timecode is
relayed from the Pi grandmaster.

Run in WSL:  python3 monitor-web.py    Open from iPad: http://<pc-wifi-ip>:8096
"""
import http.server, socketserver, urllib.request, json, time
from urllib.parse import urlparse, parse_qs

PORT = 8096
FPS = 60000 / 1001
PI_CLOCK = "http://10.10.10.1:8000/time"
NODE = "http://localhost:8090/x-nmos/node/v1.3"
CONN = "http://localhost:8090/x-nmos/connection/v1.1/single"
QUERY = "http://localhost:8080/x-nmos/query/v1.3"

SOURCES = {
    "jxs":  {"label": "easy-nmos-node/receiver/m0"},   # PC JPEG-XS island flow
    "raw":  {"label": "easy-nmos-node/receiver/v0"},   # Pi raw ST 2110-20
    "hevc": {"label": None},                            # PC HEVC 4K island flow (not NMOS)
}
DEFAULT_SRC = "jxs"
_active = {"src": DEFAULT_SRC, "ts": 0.0}
_ACTIVE_TTL = 2.0  # seconds; avoids hammering the NMOS node on every /state poll
_output = {"layout": "single"}   # native output (monitor 2) layout: single|side|multi

def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)

def _safe_json(url, timeout=3):
    try:
        return http_json(url, timeout=timeout)
    except Exception:
        return None

# ----------------------------- IS-05 control -----------------------------
def receiver_id(label):
    for rx in http_json(f"{NODE}/receivers"):
        if rx.get("label") == label:
            return rx["id"]
    raise RuntimeError(f"receiver {label!r} not found")

def set_enable(label, on):
    rid = receiver_id(label)
    body = {"master_enable": bool(on), "activation": {"mode": "activate_immediate"}}
    req = urllib.request.Request(f"{CONN}/receivers/{rid}/staged",
                                 data=json.dumps(body).encode(), method="PATCH",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status

def is_enabled(label):
    rid = receiver_id(label)
    return bool(http_json(f"{CONN}/receivers/{rid}/active").get("master_enable"))

def take(src):
    for key, s in SOURCES.items():
        if s["label"] is None:        # non-NMOS source (e.g. HEVC) -> nothing to take
            continue
        set_enable(s["label"], key == src)
    _active["src"] = src
    _active["ts"] = time.monotonic()

def active_src():
    """Best-effort current source: trust a recent take, else query the receivers."""
    if time.monotonic() - _active["ts"] < _ACTIVE_TTL:
        return _active["src"]
    if SOURCES.get(_active["src"], {}).get("label") is None:   # non-NMOS -> trust last take
        _active["ts"] = time.monotonic()
        return _active["src"]
    src = DEFAULT_SRC
    for key, s in SOURCES.items():
        if s["label"] is None:
            continue
        try:
            if is_enabled(s["label"]):
                src = key
                break
        except Exception:
            pass
    _active["src"] = src
    _active["ts"] = time.monotonic()
    return src

# ----------------------- IS-04 / IS-05 inspector -------------------------
_nmos_cache = {"t": 0.0, "data": None}
_NMOS_TTL = 3.0

def _grain(gr):
    if isinstance(gr, dict) and gr.get("numerator"):
        d = gr.get("denominator", 1)
        return f"{gr['numerator']}/{d}" if d not in (1, None) else str(gr["numerator"])
    return None

def _is05_active(kind, rid):
    d = _safe_json(f"{CONN}/{kind}/{rid}/active", timeout=2)
    if not isinstance(d, dict):
        return {}
    tp = d.get("transport_params") or []
    leg0 = tp[0] if tp else {}
    return {
        "master_enable": d.get("master_enable"),
        "sender_id": d.get("sender_id"),
        "receiver_id": d.get("receiver_id"),
        "multicast_ip": leg0.get("multicast_ip"),
        "destination_ip": leg0.get("destination_ip"),
        "destination_port": leg0.get("destination_port"),
        "source_ip": leg0.get("source_ip"),
        "interface_ip": leg0.get("interface_ip"),
        "rtp_enabled": leg0.get("rtp_enabled"),
        "legs": len(tp),
        "activation_mode": (d.get("activation") or {}).get("mode"),
        "transport_file_type": (d.get("transport_file") or {}).get("type"),
    }

def _flow_summary(flow):
    if not flow:
        return {}
    out = {"media_type": flow.get("media_type"),
           "format": (flow.get("format") or "").split(":")[-1]}
    if flow.get("frame_width"):
        out["res"] = f"{flow.get('frame_width')}x{flow.get('frame_height')}"
    gr = _grain(flow.get("grain_rate"))
    if gr:
        out["rate"] = gr
    sr = flow.get("sample_rate")
    if isinstance(sr, dict) and sr.get("numerator"):
        out["sample_rate"] = sr["numerator"]
    if flow.get("bit_depth"):
        out["bit_depth"] = flow["bit_depth"]
    return out

def _caps_summary(caps):
    out = {"media_types": caps.get("media_types", [])}
    cs = caps.get("constraint_sets") or [{}]
    c0 = cs[0] if cs else {}
    def enum(k):
        v = c0.get(k)
        return v.get("enum") if isinstance(v, dict) else None
    fw, fh = enum("urn:x-nmos:cap:format:frame_width"), enum("urn:x-nmos:cap:format:frame_height")
    if fw and fh:
        out["res"] = f"{fw[0]}x{fh[0]}"
    gr = enum("urn:x-nmos:cap:format:grain_rate")
    if gr:
        out["rate"] = _grain(gr[0])
    samp = enum("urn:x-nmos:cap:format:color_sampling")
    if samp:
        out["sampling"] = samp[0]
    return out

def _media_rank(media_type):
    m = media_type or ""
    if m.startswith("video") or "2022-6" in m:
        return 0
    if m.startswith("audio"):
        return 1
    return 2

def nmos_overview():
    now = time.monotonic()
    if _nmos_cache["data"] and now - _nmos_cache["t"] < _NMOS_TTL:
        return _nmos_cache["data"]

    nodes = _safe_json(f"{QUERY}/nodes") or []
    devices = _safe_json(f"{QUERY}/devices") or []
    senders = _safe_json(f"{QUERY}/senders") or []
    receivers = _safe_json(f"{QUERY}/receivers") or []
    flows = {f["id"]: f for f in (_safe_json(f"{QUERY}/flows") or [])}

    switch = {v["label"]: k for k, v in SOURCES.items() if v.get("label")}

    def node_view(n):
        return {
            "label": n.get("label"), "id": n.get("id"), "hostname": n.get("hostname"),
            "href": n.get("href"),
            "clocks": [{"name": c.get("name"), "ref_type": c.get("ref_type"),
                        "gmid": c.get("gmid"), "traceable": c.get("traceable"),
                        "locked": c.get("locked")} for c in n.get("clocks", [])],
            "interfaces": [{"name": i.get("name"), "mac": i.get("chassis_id")}
                           for i in n.get("interfaces", [])],
            "api_versions": (n.get("api") or {}).get("versions", []),
        }

    out_senders = []
    for s in senders:
        fl = _flow_summary(flows.get(s.get("flow_id")))
        out_senders.append({
            "id": s.get("id"), "label": s.get("label"),
            "transport": (s.get("transport") or "").split(":")[-1],
            "manifest_href": s.get("manifest_href"),
            "flow": fl,
            "subscription": s.get("subscription", {}),
            "is05": _is05_active("senders", s.get("id")),
        })
    out_senders.sort(key=lambda x: (_media_rank(x["flow"].get("media_type")), x["label"] or ""))

    out_receivers = []
    for r in receivers:
        out_receivers.append({
            "id": r.get("id"), "label": r.get("label"),
            "format": (r.get("format") or "").split(":")[-1],
            "transport": (r.get("transport") or "").split(":")[-1],
            "caps": _caps_summary(r.get("caps", {})),
            "subscription": r.get("subscription", {}),
            "switch": switch.get(r.get("label")),
            "is05": _is05_active("receivers", r.get("id")),
        })
    out_receivers.sort(key=lambda x: (x["switch"] is None, x["label"] or ""))

    data = {
        "nodes": [node_view(n) for n in nodes],
        "devices": [{"label": d.get("label"), "id": d.get("id"),
                     "type": (d.get("type") or "").split(":")[-1]} for d in devices],
        "senders": out_senders,
        "receivers": out_receivers,
        "counts": {"nodes": len(nodes), "devices": len(devices),
                   "senders": len(senders), "receivers": len(receivers), "flows": len(flows)},
    }
    _nmos_cache.update(t=now, data=data)
    return data

def resource_detail(kind, rid):
    """Everything we have on one resource: the full IS-04 object plus (for senders/
    receivers) the IS-05 active/staged/constraints and the sender's SDP transport file.
    The SDP is fetched via localhost:8090 (the node's manifest_href points at a
    docker-internal IP the iPad can't reach)."""
    out = {"is04": _safe_json(f"{QUERY}/{kind}/{rid}")}
    if kind in ("senders", "receivers"):
        out["is05_active"] = _safe_json(f"{CONN}/{kind}/{rid}/active", timeout=2)
        out["is05_staged"] = _safe_json(f"{CONN}/{kind}/{rid}/staged", timeout=2)
        out["is05_constraints"] = _safe_json(f"{CONN}/{kind}/{rid}/constraints", timeout=2)
        href = (out["is04"] or {}).get("manifest_href")
        if kind == "senders" and href:
            try:
                sdp_url = f"http://localhost:8090{urlparse(href).path}"
                with urllib.request.urlopen(sdp_url, timeout=3) as r:
                    out["sdp"] = r.read().decode("utf-8", "replace")
            except Exception:
                out["sdp"] = None
    return out

def pi_time():
    try:
        with urllib.request.urlopen(PI_CLOCK, timeout=2) as r:
            return r.read()
    except Exception:
        return json.dumps({"epoch_ms": time.time() * 1000}).encode()

# --------------------------------- page ----------------------------------
PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>ST 2110 IS-04/05 Switch</title>
<style>
 html,body{margin:0;min-height:100%;background:#000;color:#0f0;
   font-family:'Courier New',monospace;-webkit-user-select:none}
 #top{position:sticky;top:0;background:#000;padding:1.2vh 0 1vh;text-align:center;
   border-bottom:1px solid #131;z-index:5}
 #tc{font-size:min(11vw,12vh);font-weight:bold;line-height:1;text-shadow:0 0 18px #0f0}
 #ctrl{margin-top:1vh;display:flex;flex-wrap:wrap;justify-content:center}
 button{font-family:inherit;font-size:min(3.2vw,3.6vh);margin:.5vh .5vw;padding:.5em 1em;
   background:#111;color:#0f0;border:1px solid #0a0;border-radius:8px}
 button.on{background:#0a0;color:#000;font-weight:bold;box-shadow:0 0 16px #0a0}
 button:active{transform:scale(.96)}
 #info{color:#777;font-size:min(1.9vw,2.2vh);margin-top:1vh;line-height:1.5}
 .gm{color:#fc0;font-weight:bold}
 #lay{margin-top:1vh;display:flex;flex-wrap:wrap;align-items:center;justify-content:center}
 #lay button{font-size:min(2.6vw,3vh);padding:.4em .8em;margin:.4vh .4vw}
 .l2{color:#5a5;font-size:min(1.7vw,2vh);letter-spacing:.12em;margin-right:.6vw}
 #nmos{padding:1.4vh 2vw 4vh}
 h2{color:#0a0;font-size:2.1vh;margin:2.2vh 0 .6vh;border-bottom:1px solid #131;padding-bottom:.3vh;
   letter-spacing:.12em}
 table{width:100%;border-collapse:collapse;font-size:1.75vh}
 th,td{text-align:left;padding:.45vh .6vw;border-bottom:1px solid #0c1c0c;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis;max-width:34vw}
 th{color:#5a5;font-weight:normal;font-size:1.5vh;text-transform:uppercase;letter-spacing:.08em}
 tr.sw td{background:#06140a}
 tr.sw td:first-child{border-left:3px solid #0f0}
 .on-dot{color:#0f0}.off-dot{color:#633}
 .mut{color:#666}.k{color:#3c9}.warn{color:#fc0}
 .pill{display:inline-block;background:#0a0;color:#000;font-weight:bold;border-radius:4px;padding:0 .4em;font-size:1.4vh}
 #meta{color:#555;font-size:1.6vh;margin-top:.6vh}
 tr.clk{cursor:pointer}
 tr.clk:active td{background:#093;color:#000}
 #ov{position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:20;display:none;
   flex-direction:column;padding:2vh 2.5vw}
 #ovbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1vh}
 #ovbar b{color:#3c9;font-size:2vh;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 #ovpre{flex:1;overflow:auto;color:#0f0;font-size:1.7vh;white-space:pre;line-height:1.4;
   border:1px solid #131;padding:1vh;-webkit-overflow-scrolling:touch}
</style></head>
<body>
 <div id="top">
  <div id="tc">--:--:--:--</div>
  <div id="ctrl">
   <button id="bjxs" onclick="take('jxs',this)">Home videos</button>
   <button id="braw" onclick="take('raw',this)">Pi raw 2110-20</button>
   <button id="bhevc" onclick="take('hevc',this)">PC HEVC 4K</button>
  </div>
  <div id="info"></div>
  <div id="lay">
   <span class="l2">OUTPUT &middot; MON 2</span>
   <button id="lsingle" onclick="setLayout('single')">Follow take</button>
   <button id="lside" onclick="setLayout('side')">Side &times; 2</button>
   <button id="lmulti" onclick="setLayout('multi')">Multiview</button>
  </div>
 </div>
 <div id="nmos">loading IS-04/IS-05&hellip;</div>
 <div id="ov"><div id="ovbar"><b id="ovttl">resource</b><button onclick="document.getElementById('ov').style.display='none'">&times; close</button></div><pre id="ovpre"></pre></div>
<script>
const FPS=__FPS__;
let offset=0, ptp={};
const BTN={jxs:'bjxs',raw:'braw',hevc:'bhevc'};
function highlight(src){
  document.querySelectorAll('#ctrl button').forEach(b=>b.classList.remove('on'));
  const b=document.getElementById(BTN[src]); if(b) b.classList.add('on');
}
async function take(src,btn){
  highlight(src);
  try{const r=await fetch('/take?src='+src,{cache:'no-store'});const d=await r.json();
      if(d.error){ btn.textContent+=' !'; } }catch(e){}
  loadNmos();
}
const LAYBTN={single:'lsingle',side:'lside',multi:'lmulti'};
function hlLayout(m){ document.querySelectorAll('#lay button').forEach(b=>b.classList.remove('on')); const b=document.getElementById(LAYBTN[m]); if(b) b.classList.add('on'); }
async function setLayout(m){ hlLayout(m); try{await fetch('/layout?mode='+m,{cache:'no-store'});}catch(e){} }
async function refreshState(){
  try{const r=await fetch('/state',{cache:'no-store'});const d=await r.json();
      if(d.active) highlight(d.active); if(d.layout) hlLayout(d.layout);}catch(e){}
}
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const dot=b=>b?'<span class="on-dot">&#9679;</span>':'<span class="off-dot">&#9675;</span>';
const sid=id=>id?esc(String(id).slice(0,8)):'<span class="mut">none</span>';
function fmtFlow(f){
  if(!f||!f.media_type) return '<span class="mut">&mdash;</span>';
  let t=esc(f.media_type);
  if(f.res) t+=' '+esc(f.res);
  if(f.rate) t+=' @'+esc(f.rate);
  if(f.sample_rate) t+=' '+esc(f.sample_rate)+'Hz';
  if(f.bit_depth) t+='/'+esc(f.bit_depth)+'b';
  return t;
}
function fmtCaps(c){
  if(!c) return '';
  let t=(c.media_types||[]).map(esc).join(',');
  if(c.res) t+=' '+esc(c.res);
  if(c.rate) t+=' @'+esc(c.rate);
  if(c.sampling) t+=' '+esc(c.sampling);
  return t;
}
function mcast(a){
  if(!a) return '<span class="mut">&mdash;</span>';
  const ip=a.multicast_ip||a.destination_ip;
  if(!ip) return '<span class="mut">unset</span>';
  let t='<span class="k">'+esc(ip)+':'+esc(a.destination_port)+'</span>';
  if(a.legs>1) t+=' <span class="mut">x'+a.legs+'</span>';
  if(a.rtp_enabled===false) t+=' <span class="warn">rtp off</span>';
  return t;
}
function renderNmos(d){
  let h='';
  // nodes
  h+='<h2>IS-04 NODES</h2><table><tr><th>node</th><th>hostname</th><th>clock</th><th>interfaces</th><th>api</th></tr>';
  for(const n of d.nodes){
    const clk=(n.clocks||[]).map(c=>esc(c.name)+':'+esc(c.ref_type)+(c.gmid?(' '+esc(c.gmid)):'')+(c.locked!=null?(c.locked?' locked':' unlocked'):'')).join(', ')||'<span class="mut">none</span>';
    const ifs=(n.interfaces||[]).map(i=>esc(i.name)+(i.mac?(' '+esc(i.mac)):'')).join(', ');
    const api=(n.api_versions||[]).slice(-1)[0]||'';
    h+='<tr class="clk" onclick="detail(\\'nodes\\',\\''+esc(n.id)+'\\')"><td><span class="k">'+esc(n.label)+'</span></td><td>'+esc(n.hostname)+'</td><td>'+clk+'</td><td>'+ifs+'</td><td>'+esc(api)+'</td></tr>';
  }
  h+='</table>';
  // receivers
  h+='<h2>IS-04/05 RECEIVERS</h2><table><tr><th>receiver</th><th>format</th><th>caps</th><th>en</th><th>group:port</th><th>from sender</th><th>transport</th></tr>';
  for(const r of d.receivers){
    const a=r.is05||{};
    const cls=r.switch?'clk sw':'clk';
    const lab=esc(r.label)+(r.switch?(' <span class="pill">'+esc(r.switch)+'</span>'):'');
    h+='<tr class="'+cls+'" onclick="detail(\\'receivers\\',\\''+esc(r.id)+'\\')"><td>'+lab+'</td><td>'+esc(r.format)+'</td><td>'+fmtCaps(r.caps)+'</td><td>'+dot(a.master_enable)+'</td><td>'+mcast(a)+'</td><td>'+(a.sender_id?sid(a.sender_id):(r.subscription&&r.subscription.sender_id?sid(r.subscription.sender_id):'<span class="mut">none</span>'))+'</td><td>'+esc(r.transport)+'</td></tr>';
  }
  h+='</table>';
  // senders
  h+='<h2>IS-04/05 SENDERS</h2><table><tr><th>sender</th><th>flow</th><th>en</th><th>group:port</th><th>transport</th><th>sdp</th></tr>';
  for(const s of d.senders){
    const a=s.is05||{};
    h+='<tr class="clk" onclick="detail(\\'senders\\',\\''+esc(s.id)+'\\')"><td>'+esc(s.label)+'</td><td>'+fmtFlow(s.flow)+'</td><td>'+dot(a.master_enable)+'</td><td>'+mcast(a)+'</td><td>'+esc(s.transport)+'</td><td>'+(s.manifest_href?'<span class="k">yes</span>':'<span class="mut">&mdash;</span>')+'</td></tr>';
  }
  h+='</table>';
  const c=d.counts||{};
  h+='<div id="meta">tap any row for full IS-04/05 JSON &middot; '+(c.nodes||0)+' nodes &middot; '+(c.devices||0)+' devices &middot; '+(c.senders||0)+' senders &middot; '+(c.receivers||0)+' receivers &middot; '+(c.flows||0)+' flows</div>';
  document.getElementById('nmos').innerHTML=h;
}
async function loadNmos(){
  try{const r=await fetch('/nmos',{cache:'no-store'});renderNmos(await r.json());}
  catch(e){document.getElementById('nmos').innerHTML='<span class="warn">IS-04/05 unavailable</span>';}
}
async function detail(kind,id){
  const ov=document.getElementById('ov'),pre=document.getElementById('ovpre'),ttl=document.getElementById('ovttl');
  ttl.textContent=kind.replace(/s$/,'')+' '+id; pre.textContent='loading\\u2026'; ov.style.display='flex';
  try{const r=await fetch('/resource?kind='+kind+'&id='+encodeURIComponent(id),{cache:'no-store'});
      pre.textContent=JSON.stringify(await r.json(),null,2);}
  catch(e){pre.textContent='error loading resource';}
}
async function sync(){
  try{const t0=Date.now();const r=await fetch('/time',{cache:'no-store'});const t1=Date.now();
      const d=await r.json();offset=d.epoch_ms-(t1+(t1-t0)/2);ptp=d;}catch(e){}
}
const p=(n,l=2)=>String(n).padStart(l,'0');
function tick(){
  const now=new Date(Date.now()+offset);
  const ff=Math.floor(now.getMilliseconds()/(1000/FPS));
  document.getElementById('tc').textContent=
    p(now.getHours())+':'+p(now.getMinutes())+':'+p(now.getSeconds())+':'+p(ff);
  const role = ptp.state==='MASTER' ? '<span class="gm">GRANDMASTER</span>' : (ptp.state||'\\u2014');
  document.getElementById('info').innerHTML=
    'PTP domain 0 &middot; '+role+' &middot; '+(ptp.gm||'\\u2014')+' &middot; offset '+(ptp.offset||'\\u2014')+' ns';
  requestAnimationFrame(tick);
}
refreshState(); setInterval(refreshState,5000);
loadNmos();     setInterval(loadNmos,6000);
sync(); setInterval(sync,3000); tick();
</script></body></html>"""

PAGE = PAGE_TEMPLATE.replace("__FPS__", repr(FPS))

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send_json(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/take":
            qs = parse_qs(parsed.query)
            src = qs.get("src", [DEFAULT_SRC])[0]
            if src not in SOURCES:
                src = DEFAULT_SRC
            try:
                take(src); self._send_json(json.dumps({"active": src}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/state":
            try:
                self._send_json(json.dumps({"active": active_src(), "layout": _output["layout"]}).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/layout":
            qs = parse_qs(parsed.query)
            mode = qs.get("mode", ["single"])[0]
            if mode not in ("single", "side", "multi"):
                mode = "single"
            _output["layout"] = mode
            self._send_json(json.dumps({"layout": mode}).encode())
        elif parsed.path == "/nmos":
            try:
                self._send_json(json.dumps(nmos_overview()).encode())
            except Exception as e:
                self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/resource":
            qs = parse_qs(parsed.query)
            kind = qs.get("kind", [""])[0]
            rid = qs.get("id", [""])[0]
            if kind not in ("nodes", "devices", "sources", "flows", "senders", "receivers") or not rid:
                self._send_json(json.dumps({"error": "bad kind/id"}).encode(), 400)
            else:
                try:
                    self._send_json(json.dumps(resource_detail(kind, rid)).encode())
                except Exception as e:
                    self._send_json(json.dumps({"error": str(e)}).encode(), 500)
        elif parsed.path == "/time":
            self._send_json(pi_time())
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(("0.0.0.0", PORT), Handler) as s:
    print(f"ST 2110 IS-04/05 switch panel on http://localhost:{PORT}  (control + inspector)")
    print("  Ctrl+C to stop")
    try: s.serve_forever()
    except KeyboardInterrupt: pass
