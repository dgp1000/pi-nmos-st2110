#!/usr/bin/env python3
"""Trigger an IS-05 'take' on the audio receiver. Usage: take.py on|off

Resolves the receiver by label (robust to node UUID changes), then PATCHes its
/staged endpoint with master_enable on/off and an immediate activation. On 'on',
it also sets the multicast transport params so the receiver points at our flow.
"""
import json, sys, urllib.request, urllib.error

NODE = "http://localhost:8090/x-nmos/node/v1.3"
CONN = "http://localhost:8090/x-nmos/connection/v1.1"
LABEL = "easy-nmos-node/receiver/a0"
GROUP, PORT = "239.10.10.10", 5004

on = len(sys.argv) > 1 and sys.argv[1].lower() in ("on", "true", "1", "enable")

def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)

rid = next(r["id"] for r in http_json(f"{NODE}/receivers") if r.get("label") == LABEL)
body = {"master_enable": on, "activation": {"mode": "activate_immediate"}}

req = urllib.request.Request(f"{CONN}/single/receivers/{rid}/staged",
                             data=json.dumps(body).encode(), method="PATCH",
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(f"take {'ON' if on else 'OFF'} -> HTTP {r.status}  (receiver {rid})")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:400]}")
