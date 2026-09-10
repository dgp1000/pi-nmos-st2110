#!/usr/bin/env python3
"""Atoll recording PLAYBACK sender. Streams one or more recorded MPEG-TS files to a multicast group in
proper 1316-byte (7 x 188) datagrams, paced to each stream's own PCR clock so it plays at real time
(the tsparse->udpsink path fails: it aggregates into >64 KB buffers). Multiple files play back-to-back
from the same socket with no teardown between them, so a playlist streams as one continuous output
(the receiver rides the PCR discontinuity at each boundary and resumes at the next keyframe). Optional
looping restarts the whole list. Used by the panel's Record/Playback + Playlist controls; replays to
the Test Reels group, so you select "Test Reels" to watch.

Usage: playback-send.py <grp> <port> <iface-ip> <ttl> <loop:0|1> <file.ts> [file2.ts ...]
"""
import socket, sys, time

GRP  = sys.argv[1]
PORT = int(sys.argv[2])
IFIP = sys.argv[3]
TTL  = int(sys.argv[4])
LOOP = sys.argv[5] in ("1", "loop", "true")
FILES = sys.argv[6:]

TS = 188
DGRAM = TS * 7            # 1316-byte datagrams, as the senders emit
PCR_HZ = 27_000_000.0


def pcr_map(buf):
    """Scan 188-byte TS packets for PCRs -> sorted list of (byte_offset, pcr_seconds)."""
    pts = []
    n = len(buf)
    off = 0
    while off + TS <= n:
        if buf[off] != 0x47:                       # resync to a TS sync byte
            nxt = buf.find(b"\x47", off + 1)
            if nxt < 0:
                break
            off = nxt
            continue
        afc = (buf[off + 3] >> 4) & 0x3
        if afc in (2, 3):                          # has adaptation field
            aflen = buf[off + 4]
            if aflen >= 7 and (buf[off + 5] & 0x10):   # PCR present
                b = buf[off + 6:off + 12]
                base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
                ext = ((b[4] & 0x01) << 8) | b[5]
                pcr = (base * 300 + ext) / PCR_HZ
                pts.append((off, pcr))
        off += TS
    return pts


def sched_time(offset, pts):
    """Interpolate the scheduled play-time (s, relative to first PCR) for a byte offset."""
    if not pts:
        return None
    if offset <= pts[0][0]:
        return 0.0
    for i in range(1, len(pts)):
        o1, t1 = pts[i]
        if offset <= o1:
            o0, t0 = pts[i - 1]
            frac = (offset - o0) / (o1 - o0) if o1 > o0 else 0
            return (t0 + frac * (t1 - t0)) - pts[0][1]
    # past the last PCR: extrapolate at the last observed rate
    o0, t0 = pts[-2] if len(pts) >= 2 else pts[0]
    o1, t1 = pts[-1]
    rate = (t1 - t0) / (o1 - o0) if o1 > o0 else 0
    return (t1 + (offset - o1) * rate) - pts[0][1]


def load(fn):
    buf = open(fn, "rb").read()
    pts = pcr_map(buf)
    if len(pts) < 2:
        # no usable PCRs -> fall back to a nominal 8 Mbps constant pace
        dur = max(0.5, len(buf) * 8 / 8_000_000)
        pts = [(0, 0.0), (len(buf), dur)]
    span = sched_time(len(buf), pts) or 0.0
    return buf, pts, span


def main():
    if not FILES:
        print("playback: no files", flush=True); return
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(IFIP))
    print(f"playback: {len(FILES)} file(s) -> {GRP}:{PORT}{' (loop)' if LOOP else ''}", flush=True)
    while True:
        base = time.time()
        elapsed = 0.0                              # continuous timeline across the whole list
        for fn in FILES:
            try:
                buf, pts, span = load(fn)
            except OSError:
                print(f"playback: skip missing {fn}", flush=True); continue
            off = 0
            while off < len(buf):
                chunk = buf[off:off + DGRAM]
                due = sched_time(off, pts) or 0.0
                dt = base + elapsed + due - time.time()
                if dt > 0:
                    time.sleep(dt)
                try:
                    s.sendto(chunk, (GRP, PORT))
                except OSError:
                    pass
                off += DGRAM
            elapsed += span
        if not LOOP:
            break
    print("playback: done", flush=True)


if __name__ == "__main__":
    main()
