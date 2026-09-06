#!/usr/bin/env python3
"""Read pc/atoll.conf from Python.

Rather than re-parse the shell file (it has $VAR expansion and the live interface detection),
source it in bash once and capture the resolved values. atoll.conf must sit alongside this module
(it does in both pc/ and ~/atoll-run/)."""
import subprocess, os, functools

_VARS = ("ATOLL_USER PI_USER ISLAND_PC_IP ISLAND_PI_IP ISLAND_PI2_IP ISLAND_IFACE PC_WIFI_IP "
         "MAC_MUSIC_HOST MAC_MUSIC_PORT MAC_MUSIC_TS PANEL_PORT REELS_SDP_PORT "
         "HEVC_GRP HEVC_PORT HOME_GRP HOME_PORT MUSIC_GRP MUSIC_PORT "
         "HDHR_HOST HDHR_DEVICE_ID ATOLL_RUN TV_CHANNEL PROGRAMOUT_PORT AUDIOMAP_NMOS_PORT "
         "REELS_GRP REELS_PORT PI_RAW_GRP PI_RAW_PORT PI_AUDIO_GRP PI_AUDIO_PORT").split()

@functools.lru_cache(maxsize=1)
def load():
    conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atoll.conf")
    script = f'source "{conf}" >/dev/null 2>&1; ' + " ".join(
        f'printf "%s=%s\\n" {v} "${{{v}}}";' for v in _VARS)
    try:
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        out = ""
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k:
            d[k] = v
    return d

def get(key, default=""):
    return load().get(key) or default
