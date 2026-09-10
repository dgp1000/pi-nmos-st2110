#!/usr/bin/env python3
"""Atoll AMWA IS-12 NMOS Control Protocol + MS-05-02 device model.

IS-12 is the modern NMOS control plane: a WebSocket channel (`urn:x-nmos:control:ncp`) carrying the
MS-05-02 object model. This service is a self-contained control node -- it registers a node/device
in IS-04 (self-hosting its Node API too), advertises the `ncp` control, and runs the control
protocol over a hand-rolled RFC 6455 WebSocket (no ws library is installable on this box, so the
frame codec is borrowed from is07-tally).

The device model is the standard MS-05 root: a root **NcBlock** (oid 1) owning an **NcDeviceManager**
(oid 2) and an **NcClassManager** (oid 3). Generic Get/Set/GetSequenceItem/GetSequenceLength work on
every object by property id; NcBlock answers GetMemberDescriptors; the Class Manager answers
GetControlClass / GetDatatype and exposes the controlClasses / datatypes properties -- served from
the canonical MS-05-02 model descriptors in `pc/ms05-models/` (6 classes + 58 datatypes), with class
inheritance merged on request. Commands arrive as IS-12 Command messages (type 0) and are answered
with CommandResponse (type 1); Subscription (3) -> SubscriptionResponse (4); protocol faults -> Error (5).

Env/conf: NMOS_REGISTRY, NMOS_ADVERTISE_HOST, IS12_PORT, IS12_WS_PORT, ISLAND_PC_IP.
"""
import http.server, socketserver, json, time, uuid, threading, urllib.request, urllib.error, os, subprocess
import socket as _sock, base64, hashlib, struct as _struct
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["NMOS_REGISTRY", "NMOS_ADVERTISE_HOST", "IS12_PORT", "IS12_WS_PORT", "ISLAND_PC_IP"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
PORT = int(CFG.get("IS12_PORT") or 8108)
WSPORT = int(CFG.get("IS12_WS_PORT") or 8109)
ADV = (CFG.get("NMOS_ADVERTISE_HOST") or "").strip() or "localhost"
REGISTRY = (CFG.get("NMOS_REGISTRY") or "").strip() or "http://localhost:8080"
from atoll_system import SystemAPI
SYS = SystemAPI(REGISTRY)
REG = f"{REGISTRY}/x-nmos/registration/v1.3"

NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
u = lambda s: str(uuid.uuid5(NS, s))
NODE_ID = u("atoll:is12:node"); DEV_ID = u("atoll:is12:device")
NCP_HREF = f"ws://{ADV}:{WSPORT}/x-nmos/ncp/v1.0"

# ---- MS-05-02 model descriptors -----------------------------------------------------------------
MODELS = os.path.join(HERE, "ms05-models")
CLASSES = {}     # tuple(classId) -> class descriptor
DATATYPES = {}   # name -> datatype descriptor
for fn in os.listdir(os.path.join(MODELS, "classes")):
    d = json.load(open(os.path.join(MODELS, "classes", fn)))
    CLASSES[tuple(d["classId"])] = d
for fn in os.listdir(os.path.join(MODELS, "datatypes")):
    d = json.load(open(os.path.join(MODELS, "datatypes", fn)))
    DATATYPES[d["name"]] = d

def class_with_inheritance(class_id):
    """Merge a class descriptor with its ancestors' properties/methods/events (classId prefixes)."""
    cid = list(class_id)
    own = CLASSES.get(tuple(cid))
    if own is None:
        return None
    props, methods, events = [], [], []
    chain = []
    while cid:
        c = CLASSES.get(tuple(cid))
        if c:
            chain.append(c)
        cid = cid[:-1]
    for c in reversed(chain):        # base -> derived
        props += c.get("properties", [])
        methods += c.get("methods", [])
        events += c.get("events", [])
    merged = dict(own)
    merged["properties"], merged["methods"], merged["events"] = props, methods, events
    return merged

CONTROL_CLASSES = [class_with_inheritance(cid) for cid in sorted(CLASSES.keys())]
DATATYPE_LIST = [DATATYPES[n] for n in sorted(DATATYPES.keys())]

# ---- the device model instances -----------------------------------------------------------------
# NcMethodStatus
OK, BAD_FORMAT, BAD_OID, READONLY, INVALID, PARAM_ERR, DEVICE_ERR, METH_NI, PROP_NI = 200, 400, 404, 405, 406, 407, 500, 501, 502

ROOT, DEVMGR, CLSMGR = 1, 2, 3
OBJS = {
    ROOT:   {"oid": ROOT,  "classId": [1, 1],    "role": "root",          "owner": None, "userLabel": "Root",
             "block": True, "members": [DEVMGR, CLSMGR]},
    DEVMGR: {"oid": DEVMGR, "classId": [1, 3, 1], "role": "DeviceManager", "owner": ROOT, "userLabel": "Device Manager"},
    CLSMGR: {"oid": CLSMGR, "classId": [1, 3, 2], "role": "ClassManager",  "owner": ROOT, "userLabel": "Class Manager"},
}
DEVICE_PROPS = {   # NcDeviceManager level-3 properties
    (3, 1): [1, 0, 0],                                  # ncVersion (NcVersionCode -> [major,minor,patch]? spec uses string) -> set below
    (3, 2): {"name": "Atoll", "website": None, "businessContact": None},   # manufacturer
    (3, 3): {"name": "Atoll IS-12 control node", "key": "atoll-is12", "revisionLevel": "1.0", "brandName": None, "uuid": None, "description": "Demo MS-05 device"},
    (3, 4): "0001",                                     # serialNumber
    (3, 5): None,                                       # userInventoryCode
    (3, 6): "atoll-is12",                               # deviceName
    (3, 7): "control-demo",                             # deviceRole
    (3, 8): {"generic": 1, "detail": None},             # operationalState (NcDeviceOperationalState: generic=NormalOperation=1)
    (3, 9): 0,                                          # resetCause (PowerOn/other)
    (3, 10): None,                                      # message
}
DEVICE_PROPS[(3, 1)] = "1.0.0"                          # ncVersion is a version string

def blk_member_desc(oid):
    o = OBJS[oid]
    return {"description": o.get("userLabel"), "role": o["role"], "oid": o["oid"], "constantOid": True,
            "classId": o["classId"], "userLabel": o.get("userLabel"), "owner": o["owner"]}

def get_prop(obj, pid):
    lvl, idx = pid["level"], pid["index"]
    if lvl == 1:
        return {1: obj["classId"], 2: obj["oid"], 3: True, 4: obj["owner"], 5: obj["role"],
                6: obj.get("userLabel"), 7: None, 8: None}.get(idx, _MISSING)
    if lvl == 2 and obj.get("block"):                  # NcBlock
        if idx == 1: return True                       # enabled
        if idx == 2: return [blk_member_desc(m) for m in obj["members"]]   # members
    if obj["oid"] == CLSMGR and lvl == 3:
        if idx == 1: return CONTROL_CLASSES
        if idx == 2: return DATATYPE_LIST
    if obj["oid"] == DEVMGR and lvl == 3:
        return DEVICE_PROPS.get((lvl, idx), _MISSING)
    return _MISSING
_MISSING = object()

def set_prop(obj, pid, value):
    lvl, idx = pid["level"], pid["index"]
    if lvl == 1 and idx == 6:                           # userLabel is writable
        obj["userLabel"] = value; return OK
    if get_prop(obj, pid) is _MISSING:
        return PROP_NI
    return READONLY

def method_result(status, value=_MISSING):
    r = {"status": status}
    if value is not _MISSING:
        r["value"] = value
    return r

def dispatch(oid, method_id, args):
    obj = OBJS.get(oid)
    if obj is None:
        return method_result(BAD_OID)
    lvl, idx = method_id.get("level"), method_id.get("index")
    args = args or {}
    # ---- NcObject generic ----
    if lvl == 1 and idx == 1:                           # Get
        pid = args.get("id") or {}
        v = get_prop(obj, pid)
        return method_result(PROP_NI) if v is _MISSING else method_result(OK, v)
    if lvl == 1 and idx == 2:                           # Set
        return method_result(set_prop(obj, args.get("id") or {}, args.get("value")))
    if lvl == 1 and idx in (3, 7):                      # GetSequenceItem / GetSequenceLength
        pid = args.get("id") or {}
        seq = get_prop(obj, pid)
        if seq is _MISSING or not isinstance(seq, list):
            return method_result(INVALID if seq is not _MISSING else PROP_NI)
        if idx == 7:
            return method_result(OK, len(seq))
        i = args.get("index", -1)
        return method_result(OK, seq[i]) if 0 <= i < len(seq) else method_result(PARAM_ERR)
    # ---- NcBlock ----
    if obj.get("block") and lvl == 2 and idx == 1:      # GetMemberDescriptors
        recurse = bool(args.get("recurse"))
        out, stack = [], list(obj["members"])
        while stack:
            m = stack.pop(0); out.append(blk_member_desc(m))
            if recurse and OBJS[m].get("block"):
                stack += OBJS[m]["members"]
        return method_result(OK, out)
    # ---- NcClassManager ----
    if oid == CLSMGR and lvl == 3 and idx == 1:         # GetControlClass
        ident = args.get("identity") or args.get("classId")
        inc = args.get("includeInherited", True)
        cid = tuple(ident) if ident else None
        desc = class_with_inheritance(cid) if inc else CLASSES.get(cid)
        return method_result(OK, desc) if desc else method_result(PARAM_ERR)
    if oid == CLSMGR and lvl == 3 and idx == 2:         # GetDatatype
        name = args.get("name")
        dt = DATATYPES.get(name)
        return method_result(OK, dt) if dt else method_result(PARAM_ERR)
    return method_result(METH_NI)

# ================= IS-12 WebSocket control protocol (RFC 6455, hand-rolled) =======================
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _ws_frame(payload, opcode=0x1):
    head = bytes([0x80 | opcode]); n = len(payload)
    if n < 126: head += bytes([n])
    elif n < (1 << 16): head += bytes([126]) + _struct.pack("!H", n)
    else: head += bytes([127]) + _struct.pack("!Q", n)
    return head + payload

def _ws_read(conn):
    def recvn(n):
        b = b""
        while len(b) < n:
            c = conn.recv(n - len(b))
            if not c: return None
            b += c
        return b
    h = recvn(2)
    if not h: return None
    opcode = h[0] & 0x0F; masked = h[1] & 0x80; ln = h[1] & 0x7F
    if ln == 126:
        e = recvn(2);  ln = _struct.unpack("!H", e)[0] if e else None
    elif ln == 127:
        e = recvn(8);  ln = _struct.unpack("!Q", e)[0] if e else None
    if ln is None: return None
    mask = recvn(4) if masked else b""
    data = recvn(ln) if ln else b""
    if data is None or (masked and mask is None): return None
    if masked: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data

def _ws_send(conn, obj):
    try: conn.sendall(_ws_frame(json.dumps(obj).encode())); return True
    except Exception: return False

def handle_message(msg):
    """Map an inbound IS-12 message to its response (or None if no response is due)."""
    mt = msg.get("messageType")
    if mt == 0:                                         # Command -> CommandResponse
        responses = []
        for c in msg.get("commands", []):
            res = dispatch(c.get("oid"), c.get("methodId") or {}, c.get("arguments"))
            responses.append({"handle": c.get("handle"), "result": res})
        return {"messageType": 1, "responses": responses}
    if mt == 3:                                         # Subscription -> SubscriptionResponse
        return {"messageType": 4, "subscriptions": msg.get("subscriptions", [])}
    return {"messageType": 5, "status": BAD_FORMAT, "errorMessage": f"unsupported messageType {mt}"}

def _ws_client(conn, addr):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            c = conn.recv(1024)
            if not c: return
            req += c
            if len(req) > 65536: return
        lines = req.decode("latin-1").split("\r\n")
        hdrs = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1); hdrs[k.strip().lower()] = v.strip()
        key = hdrs.get("sec-websocket-key")
        if not key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"); return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        extra = ""
        if "ncp" in (hdrs.get("sec-websocket-protocol") or ""):
            extra = "Sec-WebSocket-Protocol: ncp\r\n"
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n{extra}\r\n").encode())
        print(f"  IS-12 ncp client connected: {addr[0]}", flush=True)
        while True:
            r = _ws_read(conn)
            if r is None: break
            opcode, data = r
            if opcode == 0x8: break                     # close
            if opcode == 0x9: conn.sendall(_ws_frame(data, 0xA)); continue   # ping->pong
            if opcode != 0x1: continue
            try: msg = json.loads(data.decode() or "{}")
            except Exception:
                _ws_send(conn, {"messageType": 5, "status": BAD_FORMAT, "errorMessage": "invalid JSON"}); continue
            resp = handle_message(msg)
            if resp is not None:
                _ws_send(conn, resp)
    except Exception as e:
        print(f"  ncp client error: {e}", flush=True)
    finally:
        try: conn.close()
        except Exception: pass

def ws_server():
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", WSPORT)); s.listen(8)
    print(f"IS-12 ncp WebSocket on ws://0.0.0.0:{WSPORT}/x-nmos/ncp/v1.0", flush=True)
    while True:
        try:
            conn, addr = s.accept()
            threading.Thread(target=_ws_client, args=(conn, addr), daemon=True).start()
        except Exception:
            pass

# ================= IS-04 (register + self-hosted Node API) ========================================
def _tai(t=None):
    t = time.time() if t is None else t
    return f"{int(t)}:{int((t % 1) * 1e9)}"
_VER = _tai()
def _node():
    return {"id": NODE_ID, "version": _VER, "label": "atoll-is12", "description": "Atoll IS-12 control node",
            "tags": {}, "href": f"http://{ADV}:{PORT}/", "hostname": "atoll-is12", "caps": {}, "services": [],
            "api": {"versions": ["v1.3"], "endpoints": [{"host": ADV, "port": PORT, "protocol": "http"}]},
            "clocks": [], "interfaces": []}
def _device():
    return {"id": DEV_ID, "version": _VER, "label": "atoll-is12", "description": "IS-12 control device", "tags": {},
            "type": "urn:x-nmos:device:generic", "node_id": NODE_ID, "senders": [], "receivers": [],
            "controls": [{"href": NCP_HREF, "type": "urn:x-nmos:control:ncp/v1.0"}]}
def _resources(): return [("node", _node()), ("device", _device())]
def _post(kind, data):
    body = json.dumps({"type": kind, "data": data}).encode()
    req = urllib.request.Request(f"{REG}/resource", data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{kind} -> {e.code}: {e.read().decode(errors='replace')[:200]}")
_registered = {"ok": False}
def register_all():
    try:
        for kind, data in _resources(): _post(kind, data)
        _registered["ok"] = True; print(f"  registered IS-12 node/device with IS-04 at {REG}", flush=True); return True
    except Exception as e:
        _registered["ok"] = False; print(f"  IS-04 registration failed ({e}) -- will retry", flush=True); return False
def heartbeat():
    while True:
        time.sleep(SYS.heartbeat_interval)
        if not _registered["ok"]:
            register_all(); continue
        try:
            urllib.request.urlopen(urllib.request.Request(f"{REG}/health/nodes/{NODE_ID}", data=b"", method="POST"), timeout=5).read()
        except urllib.error.HTTPError as e:
            if e.code == 404: _registered["ok"] = False
        except Exception:
            pass

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        seg = [s for s in urlparse(self.path).path.split("/") if s]
        if seg[:2] == ["x-nmos", "node"]:
            rest = seg[3:] if len(seg) > 2 else []      # seg[2]=v1.3
            if not rest: return self._send(200, ["self/", "devices/", "sources/", "flows/", "senders/", "receivers/"])
            if rest[0] == "self": return self._send(200, _node())
            if rest[0] == "devices":
                return self._send(200, [_device()] if len(rest) == 1 else _device())
            if rest[0] in ("sources", "flows", "senders", "receivers"):
                return self._send(200, [])
            return self._send(404, {"error": "not found"})
        if urlparse(self.path).path in ("/", "/status"):
            return self._send(200, {"node_id": NODE_ID, "device_id": DEV_ID, "ncp": NCP_HREF,
                                    "classes": len(CLASSES), "datatypes": len(DATATYPES),
                                    "registered": _registered["ok"]})
        self._send(404, {"error": "not found"})

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    register_all()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=ws_server, daemon=True).start()
    print(f"IS-12 control node: node {NODE_ID}  HTTP :{PORT}  ncp {NCP_HREF}  ({len(CLASSES)} classes/{len(DATATYPES)} datatypes)", flush=True)
    Threaded(("0.0.0.0", PORT), H).serve_forever()
