#!/usr/bin/env python3
"""Quick inspector for the Atoll NMOS registry: receivers, senders, the m0 (home) connection,
and any spare video/mux receivers (used while wiring the Test Reels sender)."""
import urllib.request, json
def q(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)
QUERY = "http://localhost:8080/x-nmos/query/v1.3"
CONN  = "http://localhost:8090/x-nmos/connection/v1.1/single"

rxs = q(f"{QUERY}/receivers")
print("=== RECEIVERS ===")
for r in sorted(rxs, key=lambda x: x.get("label", "")):
    print(f"  {r.get('label',''):34} {r.get('format','').split(':')[-1]:6} {r['id']}")
print("=== SENDERS ===")
for s in sorted(q(f"{QUERY}/senders"), key=lambda x: x.get("label", "")):
    print(f"  {s.get('label',''):34} {s.get('transport','').split(':')[-1]:12} flow={str(s.get('flow_id'))[:8]}")

m0 = next((r for r in rxs if r.get("label") == "easy-nmos-node/receiver/m0"), None)
if m0:
    print(f"=== m0 active connection ({m0['id']}) ===")
    try:
        print(json.dumps(q(f"{CONN}/receivers/{m0['id']}/active"), indent=1)[:1100])
    except Exception as e:
        print("  active err:", e)

print("=== spare video/mux receivers (not v0/m0) ===")
spare = [r for r in rxs if r.get("format", "").split(":")[-1] in ("video", "mux")
         and r.get("label") not in ("easy-nmos-node/receiver/v0", "easy-nmos-node/receiver/m0")]
for r in spare:
    print(f"  SPARE: {r.get('label')} {r.get('format','').split(':')[-1]} {r['id']}")
if not spare:
    print("  (none -- only v0 video + m0 mux exist)")

print("=== nodes / devices (for attaching a sender) ===")
for n in q(f"{QUERY}/nodes"):
    print(f"  node {n['id']}  {n.get('label')}")
for d in q(f"{QUERY}/devices"):
    print(f"  device {d['id']}  node={d.get('node_id','')[:8]}")
