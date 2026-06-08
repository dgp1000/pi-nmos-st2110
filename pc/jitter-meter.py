#!/usr/bin/env python3
"""Measure RTP packet inter-arrival jitter on the multicast audio flow.

Joins the multicast group on the island interface. Prefers kernel RX timestamps
(SO_TIMESTAMPNS) for accuracy; falls back to a userspace monotonic clock if the
kernel cmsg isn't delivered (e.g. under WSL). For our 1 ms-ptime ST 2110-30 audio
the ideal inter-arrival is 1000 us; the spread around that is the network jitter.

Usage: jitter-meter.py [num_packets] [group] [iface_ip]
"""
import socket, struct, sys, time, statistics

SO_TIMESTAMPNS = 35   # Linux socket option (not exported by Python's socket module)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
GROUP = sys.argv[2] if len(sys.argv) > 2 else "239.10.10.10"
IFACE_IP = sys.argv[3] if len(sys.argv) > 3 else "10.10.10.2"   # island interface (eth1)
PORT = 5004

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", PORT))
mreq = struct.pack("=4s4s", socket.inet_aton(GROUP), socket.inet_aton(IFACE_IP))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
try:
    s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
except OSError:
    pass
s.settimeout(8)

used_kernel = [False]
def rx_time():
    _, anc, _, _ = s.recvmsg(2048, 256)
    for lvl, typ, data in anc:
        if lvl == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS and len(data) >= 16:
            sec, nsec = struct.unpack("qq", data[:16])
            used_kernel[0] = True
            return sec + nsec / 1e9
    return time.clock_gettime(time.CLOCK_MONOTONIC)   # userspace fallback

print(f"measuring {N} packets on {GROUP}:{PORT} via {IFACE_IP} (ideal 1000 us)...", flush=True)
deltas, last, got = [], None, 0
try:
    for _ in range(10):                 # warm up
        last = rx_time()
    for _ in range(N):
        t = rx_time(); got += 1
        if last is not None:
            deltas.append((t - last) * 1e6)
        last = t
except socket.timeout:
    print(f"  socket timeout after {got} packets — is the Pi multicasting on {GROUP}?")

if not deltas:
    print("  NO samples collected (no packets, or group-join failed).")
    sys.exit(1)

mean = statistics.mean(deltas)
jit = statistics.pstdev(deltas)
lo, hi = min(deltas), max(deltas)
sd = sorted(deltas)
p99 = sd[int(len(sd) * 0.99)]
src = "kernel RX timestamps" if used_kernel[0] else "userspace clock (kernel ts unavailable)"
print(f"\n  source           : {src}")
print(f"  packets measured : {len(deltas)}")
print(f"  mean interval    : {mean:8.1f} us   (ideal 1000.0)")
print(f"  jitter (std-dev) : {jit:8.1f} us")
print(f"  99th pct interval: {p99:8.1f} us")
print(f"  min / max        : {lo:8.1f} / {hi:.1f} us")
print(f"  peak-to-peak     : {hi - lo:8.1f} us")
