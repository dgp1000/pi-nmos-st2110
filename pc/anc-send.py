#!/usr/bin/env python3
"""Atoll ST 2110-40 ancillary-data sender (RFC 8331).

GStreamer has no ancillary-data RTP payloader, so this is a self-contained sender. It emits
one ANC RTP packet per frame MULTIPLEXING several ST 291 ancillary data packets — a real,
inspectable ST 2110-40 essence on the island (RFC 8331 framing, 10-bit parity words + checksum,
90 kHz RTP clock, marker bit per frame). Data packets carried:
  * ATC timecode        DID 0x60 / SDID 0x60  (SMPTE ST 12M-2, always on)
  * Closed captions     DID 0x61 / SDID 0x01  (SMPTE 334 / CEA-708 DID; the UDW carry the caption
                        text directly -- a deliberately simplified stand-in for the full CEA-708
                        cc_data bitstream. The 2110-40/RFC 8331 transport is real; only the caption
                        codec payload is simplified.)  Text from ~/atoll-run/cc-input, else a rolling
                        sample set.
  * SCTE-104 splice     DID 0x41 / SDID 0x07  (ad-break marker) -- emitted for a few frames when
                        ~/atoll-run/anc-scte is touched/non-empty.

Env: ANC_GRP, ANC_PORT, ISLAND_PC_IP, MCAST_TTL, ANC_FPS, ANC_PT, ATOLL_RUN.
"""
import socket, struct, time, os

GRP      = os.environ.get("ANC_GRP", "239.10.10.50")
PORT     = int(os.environ.get("ANC_PORT", "5020"))
LOCALADDR= os.environ.get("ISLAND_PC_IP", "10.10.10.2")
TTL      = int(os.environ.get("MCAST_TTL", "1"))
FPS      = int(os.environ.get("ANC_FPS", "30"))
PT       = int(os.environ.get("ANC_PT", "100"))
SSRC     = 0x2110A17C
RUN      = os.environ.get("ATOLL_RUN") or os.path.expanduser("~/atoll-run")
CC_INPUT = os.path.join(RUN, "cc-input")     # live caption text (panel/demo writes it); empty -> samples
SCTE_KNOB= os.path.join(RUN, "anc-scte")     # non-empty -> emit an SCTE-104 splice marker briefly
CC_SAMPLES = [
    "ATOLL NEWS AT SIX -- good evening.",
    "Carried as ST 2110-40 ancillary data.",
    "CEA-708 captions, DID 0x61 / SDID 0x01.",
    "RFC 8331 over RTP, multiplexed with timecode.",
    "Extracted and rendered live on Program Out.",
]


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


def caption_udws(text):
    """Caption text -> UDW payload (one 8-bit word per character, 7-bit ASCII). Simplified stand-in
    for CEA-708 cc_data; keeps the ST 2110-40 transport real and the receiver able to render text."""
    t = (text or "")[:200]
    return [ord(c) & 0x7F for c in t] or [0x20]


def scte104_udws():
    """A minimal SCTE-104 splice_request marker payload (ad break). Not a full multiple_operation
    message -- enough to be recognised and shown as AD BREAK by the receiver."""
    return [0x08, 0x01, 0x00, 0x00, 0x00, 0x01]   # opID splice_request-ish + a splice_event flag


def _anc_packet(bw, DID, SDID, udw, line):
    """Bit-pack one ST 291 ANC data packet into bw (32-bit aligned)."""
    dc = len(udw) & 0xFF
    words9 = [DID, SDID, dc] + udw
    csum = sum(w & 0x1FF for w in words9) & 0x1FF
    checksum_word = ((0 if ((csum >> 8) & 1) else 1) << 9) | csum
    bw.put(0, 1)              # C
    bw.put(line & 0x7FF, 11)  # Line_Number
    bw.put(0, 12)             # Horizontal_Offset
    bw.put(0, 1)              # S
    bw.put(0, 7)              # StreamNum
    bw.put(anc_word(DID), 10)
    bw.put(anc_word(SDID), 10)
    bw.put(anc_word(dc), 10)
    for w in udw:
        bw.put(anc_word(w), 10)
    bw.put(checksum_word, 10)
    bw.align32()


def build_anc_payload(seq16, hh, mm, ss, ff, cap_text=None, scte=False, field=0):
    """One RFC 8331 payload multiplexing ATC timecode + (optional) caption + (optional) SCTE-104."""
    bw = BitWriter()
    n = 0
    _anc_packet(bw, 0x60, 0x60, atc_udws(hh, mm, ss, ff), line=9); n += 1     # ATC timecode
    if cap_text is not None:
        _anc_packet(bw, 0x61, 0x01, caption_udws(cap_text), line=11); n += 1  # CEA-708 captions
    if scte:
        _anc_packet(bw, 0x41, 0x07, scte104_udws(), line=13); n += 1          # SCTE-104 splice
    anc_bytes = bw.to_bytes()

    hdr = bytearray()
    hdr += struct.pack("!H", seq16)                 # Extended Sequence Number
    hdr += struct.pack("!H", len(anc_bytes))        # Length (bytes of ANC data that follow)
    hdr.append(n & 0xFF)                             # ANC_Count
    hdr += ((field & 0x3) << 22).to_bytes(3, "big") # F (top 2 bits) + reserved
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
    scte_left = 0               # frames remaining to emit the SCTE-104 marker
    def read_caption():
        try:
            t = open(CC_INPUT).read().strip()
            if t: return t
        except Exception:
            pass
        return CC_SAMPLES[int((time.time() - tstart) / 4) % len(CC_SAMPLES)]   # rolling sample every 4s
    while True:
        # wall-clock timecode
        el = int(time.time() - tstart)
        hh = (el // 3600) % 24
        mm = (el // 60) % 60
        ss = el % 60
        ff = frame % FPS
        rtp_ts = (int((time.time()) * 90000)) & 0xFFFFFFFF

        cap_text = read_caption()
        if scte_left <= 0:                       # trigger: non-empty knob -> mark, then consume it
            try:
                if os.path.getsize(SCTE_KNOB) > 0:
                    scte_left = FPS               # emit the splice for ~1 s of frames
                    open(SCTE_KNOB, "w").close()  # consume the trigger
            except OSError:
                pass
        scte_now = scte_left > 0
        if scte_now:
            scte_left -= 1
        payload = build_anc_payload(ext, hh, mm, ss, ff, cap_text=cap_text, scte=scte_now)
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
