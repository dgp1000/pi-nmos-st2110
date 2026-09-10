#!/usr/bin/env python3
"""Atoll standards-only NMOS controller -- demonstrates real controller interop.

A controller is any client that DISCOVERS resources via the IS-04 Query API and CONNECTS them via
IS-05, using only the standard interfaces -- no rig-specific glue. This one does exactly that, so it
proves the rig interoperates with an independent controller, and (by connecting an Atoll sender to the
nmos-cpp *reference* node's receiver, through the reference registry) that Atoll interoperates across
implementations, not just with itself.

Usage:  nmos-controller.py <registry-query-url> [sender-substr] [receiver-substr]
  - lists what it discovers, then connects the first matching sender -> receiver over IS-05 and
    verifies the receiver's /active reflects it.
"""
import sys, json, urllib.request, urllib.error

Q = sys.argv[1].rstrip("/")
SND_MATCH = sys.argv[2] if len(sys.argv) > 2 else "H.264"
RCV_MATCH = sys.argv[3] if len(sys.argv) > 3 else "receiver/xv0"

def get(url):
    with urllib.request.urlopen(url, timeout=6) as r:
        return json.loads(r.read())
def get_text(url):
    with urllib.request.urlopen(url, timeout=6) as r:
        return r.read().decode()

senders = get(f"{Q}/senders")
receivers = get(f"{Q}/receivers")
devices = {d["id"]: d for d in get(f"{Q}/devices")}

print(f"=== discovered via IS-04 Query ({Q}) ===")
print(f"  {len(senders)} senders, {len(receivers)} receivers, {len(devices)} devices")

def is05_base(receiver):
    dev = devices.get(receiver["device_id"], {})
    best = None
    for c in dev.get("controls", []):
        if "sr-ctrl" in c["type"]:
            best = c["href"]                       # last one = highest version listed
    return best.rstrip("/") if best else None

snd = next((s for s in senders if SND_MATCH.lower() in (s.get("label") or "").lower()), None)
rcv = next((r for r in receivers if RCV_MATCH.lower() in (r.get("label") or "").lower()), None)
if not snd or not rcv:
    print(f"  no match (sender~{SND_MATCH!r}, receiver~{RCV_MATCH!r})"); sys.exit(2)

base = is05_base(rcv)
print(f"\n=== connect (IS-05) ===")
print(f"  sender   : {snd['label']}  [{snd['id'][:8]}]  manifest {snd.get('manifest_href')}")
print(f"  receiver : {rcv['label']}  [{rcv['id'][:8]}]  on {devices[rcv['device_id']]['label']}")
print(f"  IS-05    : {base}")

sdp = get_text(snd["manifest_href"])
patch = {"sender_id": snd["id"], "master_enable": True,
         "transport_file": {"data": sdp, "type": "application/sdp"},
         "activation": {"mode": "activate_immediate"}}
url = f"{base}/single/receivers/{rcv['id']}/staged"
req = urllib.request.Request(url, data=json.dumps(patch).encode(), method="PATCH",
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        print(f"  PATCH /staged -> HTTP {r.status}")
except urllib.error.HTTPError as e:
    print(f"  PATCH /staged -> HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"); sys.exit(1)

active = get(f"{base}/single/receivers/{rcv['id']}/active")
ok = active.get("master_enable") and active.get("sender_id") == snd["id"]
print(f"\n=== verify (receiver /active) ===")
print(f"  master_enable: {active.get('master_enable')}   sender_id: {(active.get('sender_id') or '')[:8]}")
print(f"\n  RESULT: {'INTEROP OK -- reference receiver is now connected to the Atoll sender' if ok else 'connection did not take'}")
sys.exit(0 if ok else 1)
