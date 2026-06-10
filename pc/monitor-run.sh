#!/usr/bin/env bash
# Runs the IS-05 switch panel + IS-04/05 inspector (monitor-web.py, :8096) in the FOREGROUND.
# restore.ps1 launches this via `Start-Process wsl ... -WindowStyle Hidden` (as the USER -- the
# earlier *root* launch died right after start) so the hidden window holds it as a persistent
# process; a setsid'd panel in a transient `wsl` call gets torn down when the call returns.
# Stops any prior panel first; this shell's cmdline is `bash monitor-run.sh`, so the pkill
# pattern can't match (and kill) it. exec replaces the shell so there's no extra process.
pkill -f '[m]onitor-web.py' 2>/dev/null; sleep 1
cd /mnt/c/Users/dgper/pi-nmos-st2110/pc || exit 1
exec python3 -u monitor-web.py >/tmp/monitor-web.log 2>&1
