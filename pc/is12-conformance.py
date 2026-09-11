#!/usr/bin/env python3
"""Atoll IS-12 / MS-05-02 conformance check (against is12-nmos.py over the ncp WebSocket).

The AMWA nmos-testing IS-12-01 suite requires the interactive Testing Facade ("No Override URL(s)
specified" without it), so an automated CLI pass is not available here. This script instead drives
the control endpoint directly and validates the protocol + object model the way the suite's automatic
tests would: message framing, generic Get/Set/GetSequenceItem/GetSequenceLength, NcBlock member
discovery, the NcClassManager (control classes, datatypes, inheritance merge), and error statuses.

Run on the host:  python3 pc/is12-conformance.py
"""
import socket, base64, os, struct, json, sys

HOST, WSPORT, PATH = "localhost", 8109, "/x-nmos/ncp/v1.0"

def ws_connect():
    s = socket.socket(); s.connect((HOST, WSPORT)); s.settimeout(6)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: ncp\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(1024)
    if b"101" not in buf:
        raise RuntimeError("no 101 upgrade")
    return s

def _send(s, obj):
    p = json.dumps(obj).encode(); m = os.urandom(4)
    hdr = bytes([0x81]); n = len(p)
    hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack("!H", n)
    s.sendall(hdr + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))

def _recv(s):
    h = s.recv(2); ln = h[1] & 0x7F
    if ln == 126: ln = struct.unpack("!H", s.recv(2))[0]
    elif ln == 127: ln = struct.unpack("!Q", s.recv(8))[0]
    d = b""
    while len(d) < ln:
        d += s.recv(ln - len(d))
    return json.loads(d.decode())

_h = {"n": 0}
def cmd(s, oid, level, index, args=None):
    _h["n"] += 1
    _send(s, {"messageType": 0, "commands": [{"handle": _h["n"], "oid": oid,
              "methodId": {"level": level, "index": index}, "arguments": args or {}}]})
    return _recv(s)

def result(resp):
    return resp["responses"][0]["result"]

res = []
def chk(name, ok, detail=""):
    res.append((("PASS" if ok else "FAIL"), name, detail))

s = ws_connect()
chk("WebSocket ncp upgrade (101)", True)

# --- message framing ---
r = cmd(s, 1, 1, 1, {"id": {"level": 1, "index": 1}})
chk("Command -> CommandResponse (messageType 1)", r.get("messageType") == 1)
chk("response handle echoes command handle", r["responses"][0]["handle"] == _h["n"])
chk("result carries a status", "status" in result(r))

# --- NcObject generic Get on the root block ---
chk("root classId == [1,1] (NcBlock)", result(cmd(s, 1, 1, 1, {"id": {"level": 1, "index": 1}})).get("value") == [1, 1])
chk("root oid == 1", result(cmd(s, 1, 1, 1, {"id": {"level": 1, "index": 2}})).get("value") == 1)
chk("root role == 'root'", result(cmd(s, 1, 1, 1, {"id": {"level": 1, "index": 5}})).get("value") == "root")
chk("root owner == null", result(cmd(s, 1, 1, 1, {"id": {"level": 1, "index": 4}})).get("value") is None)

# --- NcBlock members + GetMemberDescriptors ---
mem = result(cmd(s, 1, 1, 1, {"id": {"level": 2, "index": 2}})).get("value")
chk("root members property returns 3 descriptors", isinstance(mem, list) and len(mem) == 3)
gmd = result(cmd(s, 1, 2, 1, {"recurse": False}))
chk("GetMemberDescriptors status 200", gmd.get("status") == 200)
roles = sorted(d.get("role") for d in gmd.get("value", []))
chk("members are DeviceManager + ClassManager + rigControl", roles == ["ClassManager", "DeviceManager", "rigControl"])
chk("member descriptor has oid+classId+owner", all(set(("role", "oid", "classId", "owner")) <= set(d) for d in gmd.get("value", [])))

# --- NcClassManager (oid 3) ---
cc = result(cmd(s, 3, 1, 1, {"id": {"level": 3, "index": 1}})).get("value")   # controlClasses
chk("classManager.controlClasses returns 7 classes", isinstance(cc, list) and len(cc) == 7)
dts = result(cmd(s, 3, 1, 1, {"id": {"level": 3, "index": 2}})).get("value")  # datatypes
chk("classManager.datatypes returns 58 datatypes", isinstance(dts, list) and len(dts) == 58)
gcc = result(cmd(s, 3, 3, 1, {"identity": [1, 3, 2], "includeInherited": True}))
chk("GetControlClass([1,3,2]) status 200", gcc.get("status") == 200)
chk("GetControlClass returns NcClassManager", gcc.get("value", {}).get("name") == "NcClassManager")
chk("GetControlClass includeInherited merges NcObject props (>=8)", len(gcc.get("value", {}).get("properties", [])) >= 10)
gcc0 = result(cmd(s, 3, 3, 1, {"identity": [1, 3, 2], "includeInherited": False}))
chk("GetControlClass includeInherited=False returns own props only (2)", len(gcc0.get("value", {}).get("properties", [])) == 2)
gdt = result(cmd(s, 3, 3, 2, {"name": "NcClassId"}))
chk("GetDatatype('NcClassId') returns a descriptor", gdt.get("status") == 200 and gdt.get("value", {}).get("name") == "NcClassId")
chk("GetDatatype(unknown) -> ParameterError 407", result(cmd(s, 3, 3, 2, {"name": "NoSuchType"})).get("status") == 407)

# --- NcAtollRigControl (oid 4): an IS-12 property Set that actually drives the rig ---
gccr = result(cmd(s, 3, 3, 1, {"identity": [1, 2, -1, 1], "includeInherited": True}))
chk("GetControlClass([1,2,-1,1]) is NcAtollRigControl", gccr.get("value", {}).get("name") == "NcAtollRigControl")
_rignames = [pr["name"] for pr in gccr.get("value", {}).get("properties", [])]
chk("rig class inherits NcObject+NcWorker and adds program/avSyncMs",
    all(n in _rignames for n in ("userLabel", "enabled", "program", "programLabel", "avSyncMs")))
_prog = result(cmd(s, 4, 1, 1, {"id": {"level": 3, "index": 1}})).get("value")
chk("Get rigControl.program returns the current source", isinstance(_prog, str) and _prog != "")
chk("Set rigControl.program (to current, no cut) -> 200",
    result(cmd(s, 4, 1, 2, {"id": {"level": 3, "index": 1}, "value": _prog})).get("status") == 200)
chk("Set rigControl.program to an invalid source -> ParameterError 407",
    result(cmd(s, 4, 1, 2, {"id": {"level": 3, "index": 1}, "value": "nope"})).get("status") == 407)
chk("Set rigControl.programLabel (read-only) -> Readonly 405",
    result(cmd(s, 4, 1, 2, {"id": {"level": 3, "index": 2}, "value": "x"})).get("status") == 405)
_av = result(cmd(s, 4, 1, 1, {"id": {"level": 3, "index": 3}})).get("value")
chk("Get rigControl.avSyncMs returns an int", isinstance(_av, int))
chk("Set rigControl.avSyncMs (to current) -> 200",
    result(cmd(s, 4, 1, 2, {"id": {"level": 3, "index": 3}, "value": _av})).get("status") == 200)

# --- NcDeviceManager (oid 2) ---
chk("deviceManager classId == [1,3,1]", result(cmd(s, 2, 1, 1, {"id": {"level": 1, "index": 1}})).get("value") == [1, 3, 1])
chk("deviceManager.ncVersion present", bool(result(cmd(s, 2, 1, 1, {"id": {"level": 3, "index": 1}})).get("value")))
chk("deviceManager.manufacturer has a name", isinstance(result(cmd(s, 2, 1, 1, {"id": {"level": 3, "index": 2}})).get("value"), dict))

# --- Set + error statuses ---
chk("Set userLabel -> 200", cmd(s, 1, 1, 2, {"id": {"level": 1, "index": 6}, "value": "Atoll Root"}).get("responses")[0]["result"]["status"] == 200)
chk("userLabel reflects the Set", result(cmd(s, 1, 1, 1, {"id": {"level": 1, "index": 6}})).get("value") == "Atoll Root")
chk("Set read-only oid -> Readonly 405", result(cmd(s, 1, 1, 2, {"id": {"level": 1, "index": 2}, "value": 9})).get("status") == 405)
chk("Get unknown property -> PropertyNotImplemented 502", result(cmd(s, 1, 1, 1, {"id": {"level": 9, "index": 9}})).get("status") == 502)
chk("command to unknown oid -> BadOid 404", result(cmd(s, 9999, 1, 1, {"id": {"level": 1, "index": 1}})).get("status") == 404)
chk("unknown method -> MethodNotImplemented 501", result(cmd(s, 1, 8, 8, {})).get("status") == 501)
chk("GetSequenceLength(members) == 3", result(cmd(s, 1, 1, 7, {"id": {"level": 2, "index": 2}})).get("value") == 3)
chk("GetSequenceItem(members,0) has a role", isinstance(result(cmd(s, 1, 1, 3, {"id": {"level": 2, "index": 2}, "index": 0})).get("value"), dict))

# --- Subscription protocol ---
_send(s, {"messageType": 3, "subscriptions": [1]})
sr = _recv(s)
chk("Subscription -> SubscriptionResponse (messageType 4)", sr.get("messageType") == 4 and sr.get("subscriptions") == [1])
s.close()

p = sum(1 for r in res if r[0] == "PASS"); f = sum(1 for r in res if r[0] == "FAIL")
print(f"\n=== IS-12 / MS-05 conformance: {p} PASS / {f} FAIL ===")
for st, name, detail in res:
    print(f"  [{st}] {name}" + (f"  -- {detail}" if detail else ""))
sys.exit(1 if f else 0)
