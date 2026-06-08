#!/usr/bin/env bash
# Post-restore sanity checks, called by restore.ps1 step [4/4].
# Kept as a file (not an inline PowerShell one-liner) because Windows PowerShell 5.1
# mangles quotes when passing a bash command to native wsl.exe.
echo "WSL island iface:"
ip -4 -br addr show eth1

# The monitor takes a few seconds to bind 8095 after launch; retry before giving up.
code=000
for i in 1 2 3 4 5; do
  code=$(curl -s -m4 -o /dev/null -w '%{http_code}' http://localhost:8095/)
  [ "$code" = "200" ] && break
  sleep 2
done
echo "monitor http $code"
