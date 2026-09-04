#!/usr/bin/env python3
"""Find an HDHomeRun on the LAN by its DeviceID, so a DHCP address change self-heals.

HDHomeRun discovery is a tiny UDP broadcast to :65001; every device answers with its DeviceID, and
we take the reply's own DeviceID tag plus the source IP. `resolve()` returns the IP for a given
DeviceID, or a caller-supplied fallback (the last-known / configured IP) when nothing answers -- so
a single dropped discovery packet, or running this on a box briefly off the tuner's LAN, never blows
away a working address. The tuner's HTTP API then lives at http://<ip>/ and its streams at :5004.

Standalone:  python3 hdhr.py [DEVICE_ID]   -> prints discovered devices (or resolves one).
"""
import socket, struct, binascii, sys, time

DISCOVER_PORT = 65001
_TYPE_DISCOVER_REQ = 0x0002
_TYPE_DISCOVER_RPY = 0x0003
_TAG_DEVICE_TYPE = 0x01
_TAG_DEVICE_ID = 0x02
_DEVTYPE_TUNER = 0x00000001
# Sent to the global broadcast plus the tuner-subnet directed broadcasts (the FLEX sits in the PC's
# mgmt /22, so 192.168.4-7.255 all cover it); harmless where a subnet is absent.
_BROADCASTS = ("255.255.255.255", "192.168.7.255", "192.168.4.255", "192.168.5.255", "192.168.6.255")


def _frame(payload):
    body = struct.pack(">HH", _TYPE_DISCOVER_REQ, len(payload)) + payload
    # libhdhomerun appends the CRC-32 little-endian.
    return body + struct.pack("<I", binascii.crc32(body) & 0xffffffff)


def _request():
    p = (struct.pack(">BBI", _TAG_DEVICE_TYPE, 4, _DEVTYPE_TUNER) +
         struct.pack(">BBI", _TAG_DEVICE_ID, 4, 0xFFFFFFFF))
    return _frame(p)


def _reply_device_id(payload):
    """Pull the 4-byte DeviceID tag out of a discover-reply payload; None if absent."""
    i = 0
    while i + 2 <= len(payload):
        tag = payload[i]; ln = payload[i + 1]; i += 2
        if ln & 0x80:                       # varint length (not used by these short tags, handled anyway)
            if i >= len(payload): break
            ln = (ln & 0x7f) | (payload[i] << 7); i += 1
        val = payload[i:i + ln]; i += ln
        if tag == _TAG_DEVICE_ID and len(val) == 4:
            return "%08X" % struct.unpack(">I", val)[0]
    return None


def discover(timeout=2.0, broadcasts=_BROADCASTS):
    """Return {DEVICE_ID_HEX: ip} for every HDHomeRun that answers within `timeout` seconds."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    req = _request()
    found = {}
    end = time.time() + timeout
    for _ in range(2):                      # two bursts -- discovery is best-effort UDP
        for bc in broadcasts:
            try:
                s.sendto(req, (bc, DISCOVER_PORT))
            except OSError:
                pass
    while time.time() < end:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        if len(data) < 4:
            continue
        typ, ln = struct.unpack(">HH", data[:4])
        if typ != _TYPE_DISCOVER_RPY:
            continue
        dev = _reply_device_id(data[4:4 + ln])
        if dev:
            found[dev] = addr[0]            # the reply's source IP is the device's current address
    s.close()
    return found


def resolve(device_id, fallback=None, timeout=2.0):
    """Current IP of the HDHomeRun with this DeviceID, else `fallback`.

    Never returns None when a fallback is given, so a failed discovery keeps the last-known IP."""
    if not device_id:
        return fallback
    want = device_id.strip().upper()
    try:
        m = discover(timeout=timeout)
    except Exception:
        m = {}
    return m.get(want, fallback)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(resolve(sys.argv[1], None) or "(not found)")
    else:
        for dev, ip in sorted(discover().items()):
            print(dev, ip)
