#!/usr/bin/env bash
# Slave the PC (WSL) clock to the island PTP grandmaster over the island NIC.
#
# IMPORTANT -- best-effort on WSL: ptp4l reaches the SLAVE state and reports its offset to the
# grandmaster, but the WSL kernel owns the guest clock, so ptp4l's adjustments do NOT actually steer
# it (see docs/ARCHITECTURE.md, "Timing & clocks"). The authoritative island timing is the Pi
# grandmaster (10.10.10.1) + Pi follower (10.10.10.3); this just lets the PC track/display its offset
# and appear as a PTP participant.
#
# The island NIC is a mirrored USB adapter with no PHC, so software timestamping (-S). The
# grandmaster runs domainNumber 0 over UDP/IPv4 -- ptp4l's defaults -- so "-s -S" is all that's needed.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IFACE="eth1"
[ -f "$HERE/atoll.conf" ] && IFACE="$(. "$HERE/atoll.conf" 2>/dev/null; echo "${ISLAND_IFACE:-eth1}")"

# Mirrored networking can bring the island interface up late (or without its address) after a WSL
# restart -- wait until it carries an IPv4 address before starting ptp4l.
for _ in $(seq 1 30); do
    ip -4 addr show "$IFACE" 2>/dev/null | grep -q "inet " && break
    sleep 2
done

exec /usr/sbin/ptp4l -i "$IFACE" -S -s -m
