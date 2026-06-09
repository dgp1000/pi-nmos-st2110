#!/usr/bin/env bash
# Post-restore sanity checks, called by restore.ps1 step [4/4].
# Kept as a file (not an inline PowerShell one-liner) because Windows PowerShell 5.1
# mangles quotes when passing a bash command to native wsl.exe.
# Island NIC by IP (WSL renames eth0/eth1 across reboots).
ISL="$(ip -o -4 addr show 2>/dev/null | awk '$4 ~ /^10\.10\.10\.2\// {print $2; exit}')"
echo "WSL island iface: ${ISL:-NOT FOUND (no NIC has 10.10.10.2)}"
[ -n "$ISL" ] && ip -4 -br addr show "$ISL"

# The monitor takes a few seconds to bind 8095 after launch; retry before giving up.
code=000
for i in 1 2 3 4 5; do
  code=$(curl -s -m4 -o /dev/null -w '%{http_code}' http://localhost:8095/)
  [ "$code" = "200" ] && break
  sleep 2
done
echo "monitor http $code"
