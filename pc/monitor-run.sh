#!/usr/bin/env bash
# Runs the IS-05 switch panel + IS-04/05 inspector (monitor-web.py, :8096) in the
# foreground. restore.ps1 launches this via `Start-Process wsl ... -WindowStyle Hidden`
# so it lives as its own persistent WSL process (a backgrounded process inside a
# transient `wsl` call does not survive). exec replaces the shell so there's no extra
# process hanging around. Control-only now (no transcode/GPU) -> no special env needed.
cd /mnt/c/Users/dgper/pi-nmos-st2110/pc || exit 1
exec python3 -u monitor-web.py >/tmp/monitor-web.log 2>&1
