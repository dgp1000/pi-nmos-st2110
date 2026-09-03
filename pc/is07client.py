#!/usr/bin/env python3
"""Shared IS-07 WebSocket receiver.

Second consumer of the transport (the analyser) would have meant a third hand-rolled copy of the
same RFC 6455 client, so it lives here once. No websockets library is installable on this box (PEP
668 externally-managed environment), hence hand-rolled -- but only the handshake and text frames,
which is all IS-07 uses.

Usage:
    c = Is07Client([source_id("hevc"), ...], port=8103, on_state=cb, on_status=cb2)
    c.start()
Callbacks fire on the client's own thread, so keep them short and do not touch UI toolkits from
them directly.
"""
import base64, hashlib, json, os, socket, struct, threading, time, uuid

# The emitter derives ids the same way, so a receiver can name the source it wants without a
# discovery round-trip. Equivalent to being configured with the source_id to follow.
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def source_id(key):
    return str(uuid.uuid5(NS, f"atoll:is07:tally:{key}"))


def device_id():
    return str(uuid.uuid5(NS, "atoll:is07:device"))


class Is07Client(threading.Thread):
    """Subscribes to a fixed set of IS-07 sources and reports state changes as they are pushed.

    Reconnects by itself: these run inside long-lived processes (a renderer, the analyser) and the
    emitter can restart underneath them.
    """

    def __init__(self, sources, port=8103, host="localhost",
                 on_state=None, on_status=None, read_timeout=20.0):
        super().__init__(daemon=True)
        self.sources = sorted(sources)
        self.host, self.port = host, port
        self.on_state = on_state
        self.on_status = on_status
        # health arrives every 5s, so silence well past that means the link is dead even when the
        # socket has not said so -- the half-open TCP case after a suspend or a killed peer.
        self.read_timeout = read_timeout
        self.connected = False
        self.messages = 0
        self.connected_since = None
        self.last_message = None

    # -- framing ---------------------------------------------------------------------------
    def _send(self, c, obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x81])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < (1 << 16):
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        c.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, c):
        def recvn(n):
            buf = b""
            while len(buf) < n:
                part = c.recv(n - len(buf))
                if not part:
                    return None
                buf += part
            return buf
        h = recvn(2)
        if not h:
            return None
        opcode, masked, ln = h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
        if ln == 126:
            e = recvn(2)
            if not e:
                return None
            ln = struct.unpack("!H", e)[0]
        elif ln == 127:
            e = recvn(8)
            if not e:
                return None
            ln = struct.unpack("!Q", e)[0]
        mask = recvn(4) if masked else b""
        if masked and mask is None:
            return None
        data = recvn(ln) if ln else b""
        if data is None:
            return None
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def _connect(self):
        c = socket.create_connection((self.host, self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        c.sendall((f"GET /x-nmos/events/v1.0/devices/{device_id()} HTTP/1.1\r\n"
                   f"Host: {self.host}:{self.port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = c.recv(1024)
            if not chunk:
                raise IOError("handshake closed")
            resp += chunk
            if len(resp) > 65536:
                raise IOError("handshake too large")
        if b"101" not in resp.split(b"\r\n")[0]:
            raise IOError("handshake refused")
        # Recomputing Accept is what distinguishes a real websocket peer from whatever else could
        # be listening on the port.
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if want.encode() not in resp:
            raise IOError("bad Sec-WebSocket-Accept")
        return c

    def _status(self, up):
        self.connected = up
        self.connected_since = time.time() if up else None
        if self.on_status:
            try:
                self.on_status(up)
            except Exception:
                pass

    # -- loop ------------------------------------------------------------------------------
    def run(self):
        backoff = 1
        while True:
            c = None
            try:
                c = self._connect()
                self._send(c, {"command": "subscription", "sources": self.sources})
                # The emitter answers with every subscribed source's CURRENT state, so a receiver
                # starts correct rather than blank until the next change.
                self._status(True)
                backoff = 1
                c.settimeout(self.read_timeout)
                while True:
                    r = self._read(c)
                    if r is None:
                        break
                    opcode, data = r
                    if opcode == 0x8:                     # close
                        break
                    if opcode == 0x9:                     # ping -> pong
                        m = os.urandom(4)
                        c.sendall(bytes([0x8A, 0x80 | len(data)]) + m
                                  + bytes(b ^ m[i % 4] for i, b in enumerate(data)))
                        continue
                    if opcode != 0x1:
                        continue
                    try:
                        msg = json.loads(data.decode())
                    except Exception:
                        continue
                    self.messages += 1
                    self.last_message = time.time()
                    if msg.get("message_type") == "state" and self.on_state:
                        try:
                            self.on_state((msg.get("identity") or {}).get("source_id"),
                                          bool((msg.get("payload") or {}).get("value")),
                                          (msg.get("timing") or {}).get("creation_timestamp"))
                        except Exception:
                            pass
            except Exception:
                pass
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
            self._status(False)
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)
