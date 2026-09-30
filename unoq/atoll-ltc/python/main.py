"""Atoll LTC timecode generator -- Linux side.

CLOCK_REALTIME on this board is disciplined to the Atoll PTP grandmaster by ptp4l
(service atoll-ptp, see /etc/linuxptp/atoll-follower.cfg). Once a second we push the
wall time to the MCU over the Bridge; the sketch renders LTC from it. PTP lock state
comes from a tiny host-side HTTP service (atoll-ptp-status, port 8765) because the app
runs in a container without access to the ptp4l management socket.
"""
import json
import os
import subprocess
import time
import urllib.request

from arduino.app_utils import App, Bridge

TZ_OFFSET_S = int(os.environ.get("LTC_TZ_OFFSET_S", "0"))   # 0 = UTC timecode

# Modulino Buttons -> Atoll panel IS-05 takes (pc/monitor-web.py source keys), LEDs = tally.
PANEL_URLS = ["http://192.168.4.85:8096", "http://10.10.10.2:8096"]   # Wi-Fi side first, island fallback
BUTTON_TAKES = {"A": "raw", "B": "hevc", "C": "music"}
_panel = None
_active_src = None
STATUS_PORT = 8765
_latency_us = 1500          # one-way Bridge latency estimate, refined from ping RTT
_status_host = None
_tick = 0


def _host_ip():
    """The docker bridge gateway is the host."""
    global _status_host
    if _status_host:
        return _status_host
    try:
        out = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("default"):
                _status_host = line.split()[2]
                break
    except Exception:
        pass
    return _status_host or "172.17.0.1"


def ptp_status():
    try:
        with urllib.request.urlopen(f"http://{_host_ip()}:{STATUS_PORT}/status", timeout=1) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def _panel_get(path, timeout=2):
    """GET path from whichever panel URL answers; remembers the working one."""
    global _panel
    urls = ([_panel] if _panel else []) + [u for u in PANEL_URLS if u != _panel]
    for u in urls:
        try:
            with urllib.request.urlopen(u + path, timeout=timeout) as r:
                _panel = u
                return json.load(r)
        except Exception:
            continue
    _panel = None
    return None


def on_button(name):
    """Bridge handler: a Modulino button was pressed -> IS-05 take on the panel."""
    src = BUTTON_TAKES.get(str(name))
    if not src:
        return False
    d = _panel_get(f"/take?src={src}", timeout=5)
    print(f"button {name} -> take {src}: {d}", flush=True)
    update_tally(d.get("active") if d else None)
    return bool(d and d.get("active") == src)


def update_tally(active):
    global _active_src
    _active_src = active
    leds = [BUTTON_TAKES[b] == active for b in ("A", "B", "C")]
    try:
        Bridge.notify("set_button_leds", *leds)
    except Exception:
        pass


def poll_tally():
    d = _panel_get("/state")
    update_tally(d.get("active") if d else None)


def measure_latency():
    global _latency_us
    rtts = []
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            Bridge.call("ping", timeout=2)
        except Exception:
            return
        rtts.append(time.perf_counter() - t0)
    rtt = min(rtts)
    _latency_us = int(rtt / 2 * 1e6)


def loop():
    global _tick
    _tick += 1
    if _tick == 2 or _tick % 60 == 0:
        measure_latency()

    st = ptp_status()
    locked = st.get("portState") == "SLAVE" and abs(st.get("offset_ns", 10**9)) < 10_000_000

    # Send right after a 100 us grid point so the MCU quantisation is consistent.
    t = time.clock_gettime_ns(time.CLOCK_REALTIME)
    Bridge.notify("time_sync", t // 10**9, t % 10**9, _latency_us, bool(locked), TZ_OFFSET_S)

    if _tick % 2 == 0:
        poll_tally()

    if _tick % 5 == 0:
        try:
            mcu = Bridge.call("get_status", timeout=2)
        except Exception as e:
            mcu = f"(no status: {e})"
        print(f"ptp={st.get('portState','?')} offset={st.get('offset_ns','?')}ns "
              f"latency={_latency_us}us panel={'up' if _panel else 'down'} active={_active_src} | mcu {mcu}", flush=True)

    # sleep until just after the next whole second
    now = time.time()
    time.sleep(max(0.05, 1.0 - (now % 1.0) + 0.002))


Bridge.provide("button", on_button)
print(f"atoll-ltc starting, tz offset {TZ_OFFSET_S}s, status host {_host_ip()}:{STATUS_PORT}", flush=True)
App.run(user_loop=loop)
