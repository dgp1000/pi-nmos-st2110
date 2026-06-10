#!/usr/bin/env python3
"""Register the Atoll 'Test Reels' channel as a first-class, discoverable NMOS sender.

The easy-nmos virtnode auto-generates only generic example resources, so this registers a real
source -> flow -> sender for Test Reels (HEVC over MPEG-TS on 239.10.10.31:5014) directly with
the IS-04 Registration API. The resources are attached to the virtnode's existing node/device,
so the virtnode's heartbeat keeps them alive (registration_expiry_interval=12s); we also re-POST
periodically (a bumped version) as a safety net. An SDP for the sender is served on :8097.

Run in WSL:  python3 pc/reels-nmos.py     (Ctrl+C to deregister + stop)
"""
import urllib.request, urllib.error, json, uuid, time, http.server, threading, signal, sys

REGBASE = "http://localhost:8080/x-nmos/registration/v1.3"
QUERY   = "http://localhost:8080/x-nmos/query/v1.3"
ISLAND_IP = "10.10.10.2"; SDP_PORT = 8097
GRP = "239.10.10.31"; PORT = 5014

def q(path):
    with urllib.request.urlopen(f"{QUERY}/{path}", timeout=5) as r:
        return json.load(r)

def post(typ, data):
    body = json.dumps({"type": typ, "data": data}).encode()
    req = urllib.request.Request(f"{REGBASE}/resource", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"

def ver():
    t = time.time(); return f"{int(t)}:{int((t % 1) * 1e9)}"

# ---- templates from the virtnode (so the schemas are exactly what the registry accepts) ----
node    = next(n for n in q("nodes") if n.get("label") == "easy-nmos-node")
device  = next(d for d in q("devices") if d.get("node_id") == node["id"])
send_t  = next(s for s in q("senders") if s.get("label") == "easy-nmos-node/sender/m1")
flow_t  = q(f"flows/{send_t['flow_id']}")
src_t   = q(f"sources/{flow_t['source_id']}")
print(f"virtnode node={node['id'][:8]} device={device['id'][:8]}  (templating on sender/m1)")

SRC, FLOW, SEND = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
SDP_HREF = f"http://{ISLAND_IP}:{SDP_PORT}/reels.sdp"

def build():
    src = dict(src_t);  src.update(id=SRC, label="Atoll Test Reels", description="Test Reels (snapplings)", version=ver())
    flow = dict(flow_t); flow.update(id=FLOW, source_id=SRC, label="Test Reels HEVC", description="HEVC 720p over MPEG-TS", version=ver())
    snd = dict(send_t); snd.update(id=SEND, flow_id=FLOW, label="Test Reels", description=f"Atoll Test Reels -> {GRP}:{PORT}", version=ver(), manifest_href=SDP_HREF)
    return src, flow, snd

SDP = ("v=0\r\n"
       f"o=- {int(time.time())} {int(time.time())} IN IP4 {ISLAND_IP}\r\n"
       "s=Atoll Test Reels (HEVC over MPEG-TS)\r\n"
       "t=0 0\r\n"
       f"m=video {PORT} udp MP2T\r\n"
       f"c=IN IP4 {GRP}/32\r\n")

class SDPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/sdp")
        self.send_header("Content-Length", str(len(SDP))); self.end_headers()
        self.wfile.write(SDP.encode())
    def log_message(self, *a): pass

def deregister(*_):
    for typ, rid in [("sender", SEND), ("flow", FLOW), ("source", SRC)]:
        try:
            req = urllib.request.Request(f"{REGBASE}/resource/{typ}s/{rid}", method="DELETE")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    print("\nderegistered Test Reels sender."); sys.exit(0)

signal.signal(signal.SIGINT, deregister); signal.signal(signal.SIGTERM, deregister)
threading.Thread(target=lambda: http.server.HTTPServer(("0.0.0.0", SDP_PORT), SDPHandler).serve_forever(), daemon=True).start()
print(f"SDP on {SDP_HREF}")

# register, then re-POST (bumped version) every 5s as a safety net under the 12s expiry
while True:
    src, flow, snd = build()
    for typ, data in (("source", src), ("flow", flow), ("sender", snd)):
        st = post(typ, data)
        if str(st) not in ("200", "201"):
            print(f"  {typ}: {st}")
    time.sleep(5)
