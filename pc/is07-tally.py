#!/usr/bin/env python3
# ===========================================================================
#  Atoll IS-07 tally emitter.
#
#  Broadcast tally is a per-SOURCE boolean: "is this source on air right now". Until now the wall
#  drew its tally border from the panel's own /state, which is an internal shortcut -- correct on
#  screen but not something another device could ever consume. This publishes the same information
#  as real NMOS IS-07 Event & Tally state, so the multiviewer becomes a genuine IS-07 RECEIVER
#  rather than a program reading its own variable.
#
#  One boolean event source per Atoll source key (hevc, raw, fec, ...), each carrying whether that
#  source is currently taken. Source IDs are UUID5-derived from the key, so they are stable across
#  restarts -- a receiver can bookmark one and it stays valid.
#
#  Message format follows the IS-07 state schema exactly, as emitted by the nmos-cpp reference node:
#     {"event_type":"boolean","identity":{"source_id":...},"message_type":"state",
#      "payload":{"value":true},"timing":{"creation_timestamp":"<TAI sec>:<nsec>"}}
#
#  Serves the IS-07 REST API (sources / state / type). The WebSocket transport is the other half of
#  IS-07 and is deliberately not implemented here: no websockets library is installable on this box
#  (PEP 668), and the REST state endpoint is part of the same spec and enough to drive tally.
#
#     http://<host>:8102/x-nmos/events/v1.0/sources/            -> list
#     http://<host>:8102/x-nmos/events/v1.0/sources/<id>/state  -> current tally state
#     http://<host>:8102/x-nmos/events/v1.0/sources/<id>/type   -> {"type":"boolean"}
#     http://<host>:8102/tally                                  -> convenience: {key: bool}
# ===========================================================================
import http.server, socketserver, json, time, uuid, threading, urllib.request, os, subprocess
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = ["PANEL_PORT", "IS07_PORT"]
raw = subprocess.check_output(["bash", "-c", f'source "{HERE}/atoll.conf"; ' + "".join(f'echo "{k}=${{{k}}}";' for k in NEED)], text=True)
CFG = dict(l.split("=", 1) for l in raw.strip().splitlines() if "=" in l)
PANEL = f"http://localhost:{CFG.get('PANEL_PORT') or 8096}"
PORT = int(CFG.get("IS07_PORT") or 8102)

# Every source the panel can take. One IS-07 boolean per source, exactly as a real tally system does.
SOURCES = ["hevc", "jxs", "music", "reels", "raw", "j2k", "h264", "mjpeg", "vp9", "tsrtp", "fec", "sps", "jpegxs"]
LABEL = {"hevc": "Live TV", "jxs": "Home videos", "music": "Music", "reels": "Test Reels",
         "raw": "Pi raw 2110-20", "j2k": "JPEG 2000", "h264": "H.264 RTP", "mjpeg": "MJPEG RTP",
         "vp9": "VP9 RTP", "tsrtp": "TS over RTP", "fec": "ST 2022-1 FEC", "sps": "ST 2022-7 SPS",
         "jpegxs": "JPEG XS"}
# Stable, reproducible ids: the same key always yields the same source_id across restarts.
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
SRC_ID = {k: str(uuid.uuid5(NS, f"atoll:is07:tally:{k}")) for k in SOURCES}
ID_SRC = {v: k for k, v in SRC_ID.items()}

# TAI = UTC + 37s (current leap-second offset). IS-07 timestamps are TAI "seconds:nanoseconds",
# the same epoch PTP distributes, so tally shares a timebase with the essence flows.
TAI_OFFSET = 37

_state = {k: False for k in SOURCES}
_changed = {k: time.time() for k in SOURCES}

def _ts(when):
    sec = int(when) + TAI_OFFSET
    nsec = int((when - int(when)) * 1e9)
    return f"{sec}:{nsec:09d}"

def state_message(key):
    return {
        "identity": {"source_id": SRC_ID[key]},
        "event_type": "boolean",
        "message_type": "state",
        "timing": {"creation_timestamp": _ts(_changed[key])},
        "payload": {"value": bool(_state[key])},
    }

def poller():
    """Follow the panel's active take and convert it to per-source tally booleans."""
    while True:
        try:
            with urllib.request.urlopen(f"{PANEL}/state", timeout=3) as r:
                active = json.load(r).get("active", "")
            now = time.time()
            for k in SOURCES:
                on = (k == active)
                if on != _state[k]:
                    _state[k] = on
                    _changed[k] = now      # timestamp marks the transition, not the poll
        except Exception:
            pass
        time.sleep(0.5)

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")
        base = "/x-nmos/events/v1.0"
        if p in ("", "/"):
            self._send(["x-nmos/"])
        elif p == "/tally":
            self._send({k: _state[k] for k in SOURCES})
        elif p == base:
            self._send(["sources/"])
        elif p == f"{base}/sources":
            self._send([f"{SRC_ID[k]}/" for k in SOURCES])
        elif p.startswith(f"{base}/sources/"):
            rest = p[len(f"{base}/sources/"):].split("/")
            sid = rest[0]
            key = ID_SRC.get(sid)
            if key is None:
                self._send({"error": "unknown source"}, 404)
            elif len(rest) == 1:
                self._send(["state/", "type/"])
            elif rest[1] == "state":
                self._send(state_message(key))
            elif rest[1] == "type":
                self._send({"type": "boolean"})
            else:
                self._send({"error": "not found"}, 404)
        else:
            self._send({"error": "not found"}, 404)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

threading.Thread(target=poller, daemon=True).start()
print(f"is07-tally: {len(SOURCES)} boolean tally sources -> http://0.0.0.0:{PORT}/x-nmos/events/v1.0/", flush=True)
for k in SOURCES:
    print(f"  {LABEL.get(k, k):<20} {SRC_ID[k]}", flush=True)
Server(("", PORT), H).serve_forever()
