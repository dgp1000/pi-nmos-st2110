#!/usr/bin/env python3
"""Serve ptp4l state as JSON on :8765 for the atoll-ltc app container."""
import json, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

PMC = "/usr/local/sbin/pmc"
state = {"portState": "UNKNOWN", "offset_ns": None, "gmPresent": None, "updated": 0}

def poll():
    while True:
        try:
            out = subprocess.run([PMC, "-u", "-b", "0", "GET PORT_DATA_SET", "GET TIME_STATUS_NP"],
                                 capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                k = line.split()
                if len(k) >= 2 and k[0] == "portState": state["portState"] = k[1]
                elif len(k) >= 2 and k[0] == "master_offset": state["offset_ns"] = int(k[1])
                elif len(k) >= 2 and k[0] == "gmPresent": state["gmPresent"] = k[1] == "true"
            state["updated"] = time.time()
        except Exception as e:
            state["error"] = str(e)
        time.sleep(1)

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(state).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

threading.Thread(target=poll, daemon=True).start()
HTTPServer(("0.0.0.0", 8765), H).serve_forever()
