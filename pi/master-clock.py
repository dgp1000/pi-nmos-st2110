#!/usr/bin/env python3
"""PTP master clock — broadcast-style time-of-day display.

Run on the Pi (the PTP grandmaster). It reads the PTP domain reference time (the
grandmaster's system clock) and renders a live studio clock. If ptp4l is running
it also shows the grandmaster identity, this port's PTP state, and the offset.

  python3 master-clock.py          # clock only
  sudo python3 master-clock.py     # clock + PTP status (pmc needs the ptp4l socket)

Ctrl+C to quit.
"""
import subprocess, sys, threading, time
from datetime import datetime

G = "\033[92m"; C = "\033[96m"; Y = "\033[93m"; DIM = "\033[2m"; B = "\033[1m"; R = "\033[0m"
CLEAR = "\033[2J"; HOME = "\033[H"; HIDE = "\033[?25l"; SHOW = "\033[?25h"

FONT = {
    "0": ["█████", "█   █", "█   █", "█   █", "█████"],
    "1": ["   █ ", "   █ ", "   █ ", "   █ ", "   █ "],
    "2": ["█████", "    █", "█████", "█    ", "█████"],
    "3": ["█████", "    █", "  ███", "    █", "█████"],
    "4": ["█   █", "█   █", "█████", "    █", "    █"],
    "5": ["█████", "█    ", "█████", "    █", "█████"],
    "6": ["█████", "█    ", "█████", "█   █", "█████"],
    "7": ["█████", "    █", "   █ ", "  █  ", "  █  "],
    "8": ["█████", "█   █", "█████", "█   █", "█████"],
    "9": ["█████", "█   █", "█████", "    █", "█████"],
    ":": ["     ", "  █  ", "     ", "  █  ", "     "],
}

def big(text):
    rows = ["", "", "", "", ""]
    for ch in text:
        glyph = FONT.get(ch, ["     "] * 5)
        for i in range(5):
            rows[i] += glyph[i] + "  "
    return rows

def ptp_status():
    info = {}
    try:
        ts = subprocess.run(["pmc", "-u", "-b", "0", "GET TIME_STATUS_NP"],
                            capture_output=True, text=True, timeout=2).stdout
        for ln in ts.splitlines():
            ln = ln.strip()
            if ln.startswith("master_offset"):
                info["offset"] = ln.split()[1]
            elif ln.startswith("gmIdentity"):
                info["gm"] = ln.split()[1]
        ps = subprocess.run(["pmc", "-u", "-b", "0", "GET PORT_DATA_SET"],
                            capture_output=True, text=True, timeout=2).stdout
        for ln in ps.splitlines():
            ln = ln.strip()
            if ln.startswith("portState"):
                info["state"] = ln.split()[1]
    except Exception:
        pass
    return info

def main():
    sys.stdout.write(HIDE + CLEAR)
    latest = [{}]
    def poller():                      # query pmc off the draw loop so it never hitches
        while True:
            latest[0] = ptp_status()
            time.sleep(1)
    threading.Thread(target=poller, daemon=True).start()
    try:
        while True:
            now = time.time()
            dt = datetime.fromtimestamp(now)
            ms = int((now % 1) * 1000)
            ptp = latest[0]
            sys.stdout.write(HOME)
            out = []
            out.append(f"{B}{C}        P T P   M A S T E R   C L O C K{R}\n")
            for r in big(dt.strftime("%H:%M:%S")):
                out.append(f"{G}{B}  {r}{R}")
            out.append(f"{G}{B}              .{ms:03d}{R}    {DIM}{dt.strftime('%A %d %B %Y')}{R}\n")
            state = ptp.get("state", "—")
            role = f"{Y}{B}GRANDMASTER{R}" if state == "MASTER" else f"{state}"
            out.append(f"  {DIM}PTP domain 0{R}    role: {role}")
            out.append(f"  {DIM}grandmaster :{R} {ptp.get('gm', '—')}")
            out.append(f"  {DIM}offset      :{R} {ptp.get('offset', '—')} ns")
            if not ptp:
                out.append(f"\n  {DIM}(run with sudo, ptp4l active, for live PTP status){R}")
            sys.stdout.write("\n".join(out) + "\033[J")   # [J clears any leftover lines below
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW + R + "\n")

if __name__ == "__main__":
    main()
