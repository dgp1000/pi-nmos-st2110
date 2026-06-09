#!/usr/bin/env python3
"""NMOS IS-05 activation watcher (receiver side).

Resolves an NMOS receiver by LABEL (robust to node restarts that regenerate UUIDs),
then polls its IS-05 /active endpoint. When master_enable goes TRUE it starts a
GStreamer pipeline that joins the multicast audio flow and plays it to the PC
speakers (via WSLg); when master_enable goes FALSE it stops the flow.

This is the control-plane -> real-media bridge: an NMOS "take" actually starts media.
"""
import json, os, subprocess, time, urllib.request
from island_iface import island_iface

NODE = "http://localhost:8090/x-nmos/node/v1.3"
CONN = "http://localhost:8090/x-nmos/connection/v1.1"
RECEIVER_LABEL = "easy-nmos-node/receiver/a0"
IFACE = island_iface()         # auto-detect island NIC (WSL renames eth0/eth1 across reboots)
FALLBACK_GROUP = "239.10.10.10"
FALLBACK_PORT = 5004

def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)

def resolve_receiver_id():
    for rx in http_json(f"{NODE}/receivers"):
        if rx.get("label") == RECEIVER_LABEL:
            return rx["id"]
    raise RuntimeError(f"receiver labelled {RECEIVER_LABEL!r} not found")

def start_pipeline(group, port):
    env = dict(os.environ, PULSE_SERVER="unix:/mnt/wslg/PulseServer")
    caps = ("application/x-rtp,media=audio,clock-rate=48000,"
            "encoding-name=L24,channels=2,payload=96")
    cmd = ["gst-launch-1.0",
           "udpsrc", f"address={group}", f"port={port}",
           f"multicast-iface={IFACE}", "auto-multicast=true", f"caps={caps}",
           "!", "rtpjitterbuffer", "latency=50",
           "!", "rtpL24depay", "!", "audioconvert",
           "!", "autoaudiosink", "sync=false"]
    return subprocess.Popen(cmd, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def stop_pipeline(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

print(f"[watcher] watching {RECEIVER_LABEL} for IS-05 takes ... (Ctrl+C to stop)", flush=True)
proc = None
while True:
    try:
        receiver = resolve_receiver_id()   # re-resolve each poll (robust to UUID changes)
        active = http_json(f"{CONN}/single/receivers/{receiver}/active")
        enabled = bool(active.get("master_enable"))
        group, port = FALLBACK_GROUP, FALLBACK_PORT   # fixed flow for the demo; the take is the gate
        if enabled and proc is None:
            print(f"[watcher] master_enable=TRUE  -> START audio from {group}:{port}", flush=True)
            proc = start_pipeline(group, port)
        elif not enabled and proc is not None:
            print("[watcher] master_enable=FALSE -> STOP audio", flush=True)
            stop_pipeline(proc)
            proc = None
    except Exception as e:
        print(f"[watcher] poll error: {e}", flush=True)
    time.sleep(2)
