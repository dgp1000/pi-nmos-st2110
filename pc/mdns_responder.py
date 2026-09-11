#!/usr/bin/env python3
"""Minimal mDNS / DNS-SD responder -- advertises one _nmos-node._tcp service so an NMOS controller
(and AMWA IS-04-03 test_01, "peer to peer discovery") can find this node without the registry.

Host Python here can't import zeroconf (PEP 668, no venv), so this hand-rolls just enough of
RFC 6762/6763 to answer PTR/SRV/TXT/A for our single service -- the same self-contained approach the
rest of the rig takes with RFC 6455 / RTP. It transmits on every real interface (so the advert
reaches both the WiFi LAN and the docker bridges the tester runs on) while the A record always
carries the node's advertised IP.
"""
import socket, struct, threading, time, subprocess

MCAST_ADDR, MCAST_PORT = "224.0.0.251", 5353
PTR, TXT, SRV, A = 12, 16, 33, 1

def _iface_ips():
    ips = []
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"], text=True)
        for line in out.splitlines():
            p = line.split()
            if len(p) < 4 or p[1] == "lo":
                continue
            ip = p[3].split("/")[0]
            if ip.startswith("127.") or ip == "10.255.255.254":
                continue
            ips.append(ip)
    except Exception:
        pass
    return ips or ["0.0.0.0"]

def _name(n):
    out = b""
    for label in n.rstrip(".").split("."):
        b = label.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"

def _txt(d):
    out = b""
    for k, v in d.items():
        s = f"{k}={v}".encode()
        out += bytes([len(s)]) + s
    return out or b"\x00"

class MdnsResponder(threading.Thread):
    def __init__(self, ip, port, instance="atoll-is11", txt=None, ttl=120):
        super().__init__(daemon=True)
        self.ip, self.port, self.ttl = ip, int(port), ttl
        self.svc = "_nmos-node._tcp.local."
        self.inst = f"{instance}.{self.svc}"
        self.host = f"{instance}.local."
        self.txt = txt or {}
        self.targets = {self.svc.rstrip("."), self.inst.rstrip("."), self.host.rstrip("."),
                        "_services._dns-sd._udp.local"}

    def _packet(self):
        recs = [
            (self.svc, PTR, _name(self.inst)),
            (self.inst, SRV, struct.pack("!HHH", 0, 0, self.port) + _name(self.host)),
            (self.inst, TXT, _txt(self.txt)),
            (self.host, A, socket.inet_aton(self.ip)),
        ]
        body = b""
        for name, rtype, rdata in recs:
            rclass = 1 if rtype == PTR else 0x8001     # cache-flush bit on the unique records
            body += _name(name) + struct.pack("!HHIH", rtype, rclass, self.ttl, len(rdata)) + rdata
        return struct.pack("!HHHHHH", 0, 0x8400, 0, len(recs), 0, 0) + body   # QR+AA response

    def _announce(self):
        pkt = self._packet()
        for ip in _iface_ips():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                s.sendto(pkt, (MCAST_ADDR, MCAST_PORT))
                s.close()
            except Exception:
                pass

    def _wants_us(self, data):
        try:
            _id, flags, qd = struct.unpack("!HHH", data[:6])
            if flags & 0x8000:
                return False
            off, want = 12, False
            for _ in range(qd):
                labels = []
                while True:
                    ln = data[off]
                    if ln == 0:
                        off += 1; break
                    if ln & 0xC0:
                        off += 2; break
                    labels.append(data[off + 1:off + 1 + ln].decode("ascii", "ignore")); off += 1 + ln
                off += 4
                if ".".join(labels).lower() in self.targets:
                    want = True
            return want
        except Exception:
            return False

    def run(self):
        try:
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception: pass
            rx.bind(("", MCAST_PORT))
            for ip in _iface_ips():
                try:
                    rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                  struct.pack("4s4s", socket.inet_aton(MCAST_ADDR), socket.inet_aton(ip)))
                except Exception:
                    pass
            rx.settimeout(1.0)
        except Exception as e:
            print(f"mdns: cannot open socket ({e})", flush=True); return
        for _ in range(3):
            self._announce(); time.sleep(0.25)
        last = time.time()
        while True:
            try:
                data, _addr = rx.recvfrom(4096)
                if self._wants_us(data):
                    self._announce()
            except socket.timeout:
                pass
            except Exception:
                time.sleep(0.5)
            if time.time() - last > 10:
                self._announce(); last = time.time()
