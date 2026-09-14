#!/usr/bin/env python3
"""Atoll registrar watchdog.

The nmos-cpp registry garbage-collects a node that stops heartbeating (registration_expiry_interval
is 12 s). If an Atoll registrar service starts before the registry is ready, or its heartbeat lapses
after a Windows/WSL blip, its node -- and every sender it advertises -- silently drops out of the
registry, even though the service is still "running". This watchdog polls the IS-04 Query API and, for
any expected Atoll node that has aged out while its service is still active, restarts that service so it
re-registers. It never restarts a node that is intentionally stopped, throttles per service, and holds
off restarting anything when the registry itself is unreachable (instead nudging the registry to start).

Runs as `atoll-watchdog`. Config: NMOS_REGISTRY from atoll.conf.
"""
import subprocess, time, json, urllib.request, os

HERE = os.path.dirname(os.path.abspath(__file__))

def _conf(key, default):
    try:
        v = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf" 2>/dev/null; echo "${{{key}}}"'],
                                    text=True).strip()
        return v or default
    except Exception:
        return default

NMOS = _conf("NMOS_REGISTRY", "http://localhost:8080")
QUERY = f"{NMOS}/x-nmos/query/v1.3/nodes"

# node label (as it appears in IS-04) -> the systemd service that registers it
EXPECTED = {
    "atoll-is11":        "atoll-is11",
    "atoll-is12":        "atoll-is12",
    "atoll-is07-tally":  "atoll-is07",
    "atoll-program-out": "atoll-programout",
    "atoll-music":       "atoll-music-nmos",
    "atoll-audiomap":    "atoll-audiomap",
    "atoll-codec":       "atoll-codec-nmos",
    "atoll-jxs":         "atoll-jxs-nmos",
    "atoll-pi":          "atoll-pi-nmos",
}

POLL = 25               # seconds between checks
GRACE = 60              # startup grace: let the rig boot before acting
MISS_CONFIRM = 2        # a node must be missing this many polls in a row before we act (ride out blips)
RESTART_COOLDOWN = 150  # per-service min seconds between watchdog restarts
REG_RECOVER_COOLDOWN = 300

def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} watchdog: {msg}", flush=True)

def query_labels():
    try:
        with urllib.request.urlopen(QUERY, timeout=6) as r:
            return {n.get("label") for n in json.loads(r.read())}
    except Exception as e:
        log(f"registry query failed ({e})")
        return None

def is_active(svc):
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", svc]).returncode == 0
    except Exception:
        return False

def restart(svc):
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", svc], timeout=30, check=False)
        return True
    except Exception as e:
        log(f"restart {svc} failed ({e})")
        return False

def recover_registry():
    try:
        subprocess.run(["sudo", "-n", "docker", "start", "nmos-registry", "nmos-virtnode"],
                       timeout=30, check=False)
        log("nudged the NMOS docker stack (registry unreachable)")
    except Exception as e:
        log(f"registry recover failed ({e})")

def main():
    log(f"starting; watching {len(EXPECTED)} nodes at {QUERY} (grace {GRACE}s)")
    time.sleep(GRACE)
    miss = {label: 0 for label in EXPECTED}
    last_restart = {}
    reg_fail = 0
    last_reg_recover = 0.0
    while True:
        present = query_labels()
        now = time.time()
        if present is None:                       # registry unreachable -> don't restart registrars
            reg_fail += 1
            if reg_fail >= MISS_CONFIRM and now - last_reg_recover > REG_RECOVER_COOLDOWN:
                recover_registry(); last_reg_recover = now
            time.sleep(POLL); continue
        reg_fail = 0
        for label, svc in EXPECTED.items():
            if label in present:
                miss[label] = 0
                continue
            miss[label] += 1
            if miss[label] < MISS_CONFIRM:
                continue
            if not is_active(svc):                # intentionally stopped -> leave it alone
                continue
            if now - last_restart.get(svc, 0) < RESTART_COOLDOWN:
                continue
            log(f"node '{label}' missing from registry but {svc} is active -> restarting to re-register")
            if restart(svc):
                last_restart[svc] = now
                miss[label] = 0
        time.sleep(POLL)

if __name__ == "__main__":
    main()
