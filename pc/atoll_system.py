"""IS-09 System API client for the Atoll NMOS nodes.

Per AMWA IS-09 / BCP-002, an NMOS node should discover the System API and honour its global config
(notably the IS-04 registration heartbeat interval, and the PTP settings for media nodes). The Atoll
Python nodes (program-out, is07-tally, music-nmos) previously hard-coded a 5 s heartbeat and never
looked at the System API -- so IS-09 node-behaviour failed. This shared client fixes that:

  * discovers the System API via DNS-SD  (`_nmos-system._tcp`, using avahi-browse; lowest `pri` wins),
  * falls back to a configured host (the registry co-hosts the System API) if DNS-SD finds nothing,
  * fetches `/x-nmos/system/v1.0/global`, and
  * exposes `heartbeat_interval` (+ the full global config / ptp) for the node to apply.

A daemon thread re-discovers and re-fetches, tracking the global `version` so a live config change
(e.g. the operator raising the heartbeat interval) is picked up without a restart.

Usage:
    from atoll_system import SystemAPI
    SYS = SystemAPI("http://localhost:8080")   # fallback base (registry host)
    ...
    time.sleep(SYS.heartbeat_interval)          # in the node's heartbeat loop
"""
import subprocess, json, urllib.request, threading, time, re

_SYS_TYPE = "_nmos-system._tcp"

class SystemAPI:
    def __init__(self, fallback_base, refresh=30):
        self._fallback = (fallback_base or "http://localhost:8080").rstrip("/")
        self._refresh = refresh
        self._lock = threading.Lock()
        self.href = None                 # active System API base (…/x-nmos/system/vX.Y)
        self.discovered = False          # True when the current href came from DNS-SD
        self.version = None
        self.global_config = {}
        self.ptp = {}
        self.heartbeat_interval = 5      # IS-04 default until the first successful fetch
        threading.Thread(target=self._run, daemon=True).start()

    def _discover(self):
        """Return (base_url, True) from DNS-SD, or None. Lowest advertised `pri` wins (NMOS precedence)."""
        try:
            out = subprocess.check_output(["avahi-browse", "-rtp", _SYS_TYPE],
                                          text=True, timeout=6, stderr=subprocess.DEVNULL)
        except Exception:
            return None
        best = None
        for ln in out.splitlines():
            if not ln.startswith("="):
                continue
            f = ln.split(";")
            if len(f) < 10 or f[2] != "IPv4":
                continue
            addr, port, txt = f[7], f[8], " ".join(f[9:])
            def _txt(k, d):
                m = re.search(rf"{k}=([^\"\s]+)", txt)
                return m.group(1) if m else d
            try:
                pri = int(_txt("pri", "100"))
            except ValueError:
                pri = 100
            ver = _txt("api_ver", "v1.0").split(",")[0]
            proto = _txt("api_proto", "http")
            base = f"{proto}://{addr}:{port}/x-nmos/system/{ver}"
            if best is None or pri < best[0]:
                best = (pri, base)
        return best[1] if best else None

    def _fetch(self, base):
        with urllib.request.urlopen(f"{base}/global", timeout=5) as r:
            return json.load(r)

    def _run(self):
        while True:
            disc = self._discover()
            base = disc or f"{self._fallback}/x-nmos/system/v1.0"
            try:
                g = self._fetch(base)
                changed = (g.get("version") != self.version) or (base != self.href)
                with self._lock:
                    self.href, self.discovered = base, bool(disc)
                    self.global_config, self.version = g, g.get("version")
                    self.ptp = g.get("ptp") or {}
                    hb = (g.get("is04") or {}).get("heartbeat_interval")
                    if isinstance(hb, (int, float)) and hb > 0:
                        self.heartbeat_interval = int(hb)
                if changed:
                    how = "DNS-SD" if disc else "configured fallback"
                    print(f"  IS-09: System API via {how} {base} -- heartbeat={self.heartbeat_interval}s, "
                          f"ptp.domain={self.ptp.get('domain_number')}", flush=True)
            except Exception:
                pass
            time.sleep(self._refresh)
