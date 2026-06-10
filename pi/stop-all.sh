#!/usr/bin/env bash
# Stop all Pi-side generators (counterpart to launch-all.sh): PTP grandmaster, the ST 2110-30
# audio + ST 2110-20 video senders, and the web clock. Leaves the Pi powered + SSH-reachable
# (re-launch with: sudo bash ~/launch-all.sh).
# Run on the Pi as root:   sudo bash ~/stop-all.sh
echo "stopping Pi generators (PTP, 2110 audio/video, web clock)..."
pkill -x ptp4l 2>/dev/null
pkill -x gst-launch-1.0 2>/dev/null
pkill -f master-clock-web.py 2>/dev/null
sleep 1
echo "=== still running (want none) ==="
pgrep -a ptp4l; pgrep -a gst-launch-1.0; pgrep -fa master-clock-web.py | grep -v grep
echo "stopped. (re-launch: sudo bash ~/launch-all.sh)"
