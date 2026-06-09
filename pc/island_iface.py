"""Detect the WSL interface that holds the ST 2110 island IP.

WSL renames NICs across reboots (eth0 one boot, eth1 the next), so the island
interface name is NOT stable. Always resolve it by IP (the address is stable:
10.10.10.2 on the Windows Ethernet adapter, mirrored into WSL).
"""
import subprocess


def island_iface(ip="10.10.10.2", default="eth0"):
    """Return the interface name currently holding `ip` (e.g. 'eth0'), or `default`."""
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
        for line in out.splitlines():
            parts = line.split()
            # parts: ['2:', 'eth0', 'inet', '10.10.10.2/24', 'brd', ...]
            if len(parts) > 3 and parts[3].split("/")[0] == ip:
                return parts[1]
    except Exception:
        pass
    return default
