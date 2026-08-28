#!/usr/bin/env python3
"""Atoll ST 2110-40 ancillary-data sender (RFC 8331).

GStreamer has no ancillary-data RTP payloader, so this is a self-contained sender. It emits
one ANC RTP packet per frame carrying an ATC (Ancillary Time Code, SMPTE ST 12M-2, DID 0x60 /
SDID 0x60) data packet — a real, inspectable ST 2110-40 essence on the island. RFC 8331 framing
with SMPTE ST 291 10-bit parity words and checksum. 90 kHz RTP clock, marker bit per frame.

Env: ANC_GRP, ANC_PORT, ISLAND_PC_IP, MCAST_TTL, ANC_FPS, ANC_PT.
"""
import socket, struct, time, os

GRP      = os.environ.get("ANC_GRP", "239.10.10.50")
PORT     = int(os.environ.get("ANC_PORT", "5020"))
LOCALADDR= os.environ.get("ISLAND_PC_IP", "10.10.10.2")
TTL      = int(os.environ.get("MCAST_TTL", "1"))
FPS      = int(os.environ.get("ANC_FPS", "30"))
PT       = int(os.environ.get("ANC_PT", "100"))
SSRC     = 0x2110A17C


def anc_word(v8):
    """8-bit value -> 10-bit ST 291 word: b8 = even parity over b0..b7, b9 = NOT b8."""
    v8 &= 0xFF
    b8 = bin(v8).count("1") & 1           # even parity
    b9 = 0 if b8 else 1
    return (b9 << 9) | (b8 << 8) | v8


class BitWriter:
    """Accumulates bits MSB-first, emits big-endian bytes; 32-bit word alignment for RFC 8331."""
    def __init__(self):
        self.bits = []
    def put(self, value, nbits):
        for i in range(nbits - 1, -1, -1):
            self.bits.append((value >> i) & 1)
    def align32(self):
        while len(self.bits) % 32:
            self.bits.append(0)
    def to_bytes(self):
        while len(self.bits) % 8:
            self.bits.append(0)
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            b = 0
            for j in range(8):
                b = (b << 1) | self.bits[i + j]
            out.append(b)
        return bytes(out)


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def atc_udws(hh, mm, ss, ff):
    """16 ATC user-data words (8-bit payloads) per RP188/ST12M: DBB1, DBB2, 8 timecode BCD bytes,
    then binary-group / flag bytes (zeroed here). Structurally valid ATC_LTC."""
    tc = [
        0x00,            # DBB1 (payload type: LTC)
        0x00,            # DBB2 (flags)
        bcd(ff) & 0x3F,  # frames  (+ flag bits, kept 0)
        0x00,            # binary group 1/2
        bcd(ss) & 0x7F,  # seconds
        0x00,            # binary group 3/4
        bcd(mm) & 0x7F,  # minutes
        0x00,            # binary group 5/6
        bcd(hh) & 0x3F,  # hours
        0x00,            # binary group 7/8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # remaining words -> 16 total
    ]
    return tc[:16]


def build_anc_payload(seq16, hh, mm, ss, ff, field=0):
    """One RFC 8331 payload with a single ATC ANC packet."""
    DID, SDID = 0x60, 0x60
    udw = atc_udws(hh, mm, ss, ff)
    dc = len(udw)                              # Data_Count = 16
    # ST291 checksum: sum of b0..b8 of DID,SDID,DC,UDW words (9-bit), modulo 512; b9 = ~b8
    words9 = [DID, SDID, dc] + udw
    csum = sum(w & 0x1FF for w in words9) & 0x1FF
    cs_b8 = (csum >> 8) & 1
    checksum_word = ((0 if cs_b8 else 1) << 9) | csum

    bw = BitWriter()
    # --- per-ANC-packet fields (bit-packed) ---
    bw.put(0, 1)              # C (0 = luma/HANC C-channel not applicable)
    bw.put(9, 11)             # Line_Number (9 = a plausible VANC line)
    bw.put(0, 12)             # Horizontal_Offset
    bw.put(0, 1)              # S (StreamFlag)
    bw.put(0, 7)              # StreamNum
    bw.put(anc_word(DID), 10)
    bw.put(anc_word(SDID), 10)
    bw.put(anc_word(dc), 10)
    for w in udw:
        bw.put(anc_word(w), 10)
    bw.put(checksum_word, 10)
    bw.align32()             # each ANC packet is 32-bit aligned
    anc_bytes = bw.to_bytes()

    # --- RFC 8331 payload header ---
    hdr = bytearray()
    hdr += struct.pack("!H", seq16)                 # Extended Sequence Number
    hdr += struct.pack("!H", len(anc_bytes))        # Length (bytes of ANC data that follow)
    anc_count = 1
    F = field & 0x3
    hdr.append(anc_count & 0xFF)                     # ANC_Count
    hdr += (F << 22).to_bytes(3, "big")             # F (top 2 bits) + 22 reserved -> 3 bytes
    return bytes(hdr) + anc_bytes


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOCALADDR))
    print(f"anc-send: ST 2110-40 ATC timecode -> {GRP}:{PORT} via {LOCALADDR} @ {FPS}fps")

    seq = 0                     # RTP sequence (16-bit)
    ext = 0                     # extended sequence number
    frame = 0
    tstart = time.time()
    period = 1.0 / FPS
    while True:
        # wall-clock timecode
        el = int(time.time() - tstart)
        hh = (el // 3600) % 24
        mm = (el // 60) % 60
        ss = el % 60
        ff = frame % FPS
        rtp_ts = (int((time.time()) * 90000)) & 0xFFFFFFFF

        payload = build_anc_payload(ext, hh, mm, ss, ff)
        b0 = 0x80                                   # V=2
        b1 = 0x80 | (PT & 0x7F)                     # Marker=1 (last/only ANC pkt of frame) + PT
        rtp = struct.pack("!BBHII", b0, b1, seq & 0xFFFF, rtp_ts, SSRC) + payload
        s.sendto(rtp, (GRP, PORT))

        seq = (seq + 1) & 0xFFFF
        if seq == 0:
            ext = (ext + 1) & 0xFFFF
        else:
            ext = seq
        frame += 1
        # pace to FPS
        target = tstart + frame * period
        dt = target - time.time()
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
