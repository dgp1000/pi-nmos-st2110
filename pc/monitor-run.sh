#!/usr/bin/env bash
# Runs the video+timecode monitor in the foreground. restore.ps1 launches this via
# `Start-Process wsl ... -WindowStyle Hidden` so it lives as its own persistent WSL
# process (nohup inside a transient `wsl` call does not survive). exec replaces the
# shell so there's no extra process hanging around.
export LD_LIBRARY_PATH=/root/jxs-install/lib
cd /mnt/c/Users/dgper/pi-nmos-st2110/pc || exit 1
exec python3 -u video-web.py >/tmp/videoweb.log 2>&1
