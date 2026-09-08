#!/usr/bin/env python3
"""Atoll ST 2110-40 ancillary RECEIVER. Joins the ANC flow, depacketises RFC 8331 (reversing the
10-bit ST 291 word packing), and pulls out the multiplexed data packets:
  * ATC timecode      DID 0x60 / SDID 0x60  -> ~/atoll-run/anc-tc   ("HH:MM:SS:FF")
  * Closed captions   DID 0x61 / SDID 0x01  -> rendered on Program Out (the caption band knob)
  * SCTE-104 splice   DID 0x41 / SDID 0x07  -> shows "AD BREAK" over the caption for its duration

Captions/SCTE are rendered by writing the caption-band knob the renderers already overlay, so a
received 2110-40 caption appears live on the output. Clears the band on exit. Env: ANC_GRP, ANC_PORT,
ISLAND_PC_IP, ATOLL_RUN.
"""
import socket, struct, os, time, signal, sys

GRP   = os.environ.get("ANC_GRP", "239.10.10.50")
PORT  = int(os.environ.get("ANC_PORT", "5020"))
LOCAL = os.environ.get("ISLAND_PC_IP", "10.10.10.2")
RUN   = os.environ.get("ATOLL_RUN") or os.path.expanduser("~/atoll-run")
CC_OUT = os.path.join(RUN, "demo-caption")   # the caption band the renderers overlay
CC_ENABLE = os.path.join(RUN, "cc-enable")   # "1" -> render captions; else the band stays clear
TC_OUT = os.path.join(RUN, "anc-tc")
SCTE_FRAMES = 60                              # keep "AD BREAK" up this many received frames after a splice


class BitReader:
    def __init__(self, b):
        self.b = b; self.pos = 0; self.n = len(b) * 8
    def get(self, nbits):
        v = 0
        for _ in range(nbits):
            if self.pos >= self.n:
                raise EOFError
            byte = self.b[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v
    def align32(self):
        self.pos = (self.pos + 31) & ~31


def parse_payload(payload):
    """RFC 8331 payload -> list of (DID, SDID, [udw8...], line). Returns [] if malformed."""
    if len(payload) < 8:
        return []
    _ext, length, anc_count = struct.unpack("!HHB", payload[:5])
    anc = payload[8:8 + length]
    br = BitReader(anc); out = []
    for _ in range(anc_count):
        try:
            br.get(1)                 # C
            line = br.get(11)         # Line_Number
            br.get(12)                # Horizontal_Offset
            br.get(1)                 # S
            br.get(7)                 # StreamNum
            did = br.get(10) & 0xFF
            sdid = br.get(10) & 0xFF
            dc = br.get(10) & 0xFF
            udw = [br.get(10) & 0xFF for _ in range(dc)]
            br.get(10)                # checksum
            br.align32()
            out.append((did, sdid, udw, line))
        except EOFError:
            break
    return out


def bcd2(x):
    return (x >> 4) * 10 + (x & 0x0F)


def _write(path, text):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack("4s4s", socket.inet_aton(GRP), socket.inet_aton(LOCAL)))
    s.settimeout(2.0)
    print(f"anc-recv: ST 2110-40 <- {GRP}:{PORT}  (captions -> {CC_OUT})", flush=True)

    def _cleanup(*_):
        _write(CC_OUT, "")               # clear the caption band on exit
        sys.exit(0)
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    def cc_on():
        try: return open(CC_ENABLE).read().strip() in ("1", "true", "on")
        except Exception: return False
    last_cc = None; scte_left = 0; last_shown = None; last_log = 0
    tc = "--:--:--:--"
    try:
        while True:
            try:
                pkt, _ = s.recvfrom(2048)
            except socket.timeout:
                _write(CC_OUT, "")       # sender gone -> clear the band
                last_shown = ""
                continue
            if len(pkt) < 12:
                continue
            for did, sdid, udw, _line in parse_payload(pkt[12:]):
                if did == 0x60 and sdid == 0x60 and len(udw) >= 9:      # ATC timecode
                    tc = f"{bcd2(udw[8] & 0x3F):02d}:{bcd2(udw[6] & 0x7F):02d}:{bcd2(udw[4] & 0x7F):02d}:{bcd2(udw[2] & 0x3F):02d}"
                    _write(TC_OUT, tc)
                elif did == 0x61 and sdid == 0x01:                     # CEA-708 caption (text UDW)
                    last_cc = bytes(w & 0x7F for w in udw).decode("ascii", "replace").strip()
                elif did == 0x41 and sdid == 0x07:                    # SCTE-104 splice -> AD BREAK
                    scte_left = SCTE_FRAMES
            if scte_left > 0:
                scte_left -= 1
                shown = (f"\U0001F534 AD BREAK   {last_cc}" if last_cc else "\U0001F534 AD BREAK")
            else:
                shown = last_cc or ""
            if not cc_on():
                shown = ""               # captions off -> keep the band clear
            if shown != last_shown:
                _write(CC_OUT, shown)
                last_shown = shown
            now = time.time()
            if now - last_log >= 5:
                print(f"anc-recv: tc={tc} cc={last_cc!r} scte={'yes' if scte_left else 'no'}", flush=True)
                last_log = now
    finally:
        _write(CC_OUT, "")


if __name__ == "__main__":
    main()
