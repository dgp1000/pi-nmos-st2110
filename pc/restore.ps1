# Restore the PC/WSL side after a reboot.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\pi-nmos-st2110\pc\restore.ps1"

Write-Output "[1/5] Booting WSL + keepalive (so the VM stays up)..."
# Clear any stale keepalives first so re-running this script doesn't stack them.
wsl -d Ubuntu -u root -- bash -lc "pkill -f 'tail -f /dev/null'; true"
Start-Process wsl -ArgumentList '-d','Ubuntu','--','tail','-f','/dev/null' -WindowStyle Hidden
Start-Sleep -Seconds 3

Write-Output "[2/5] GPU: ensure D3D12 OpenGL driver for WSL (vs CPU llvmpipe)..."
wsl -d Ubuntu -u root -- bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/wsl-gpu-setup.sh

Write-Output "[3/5] Docker + NMOS registry/node..."
wsl -d Ubuntu -u root -- bash -lc "systemctl start docker; sleep 3; cd /root/easy-nmos && docker compose -f docker-compose.wsl.yml up -d nmos-registry nmos-virtnode 2>&1 | tail -3"

Write-Output "[4/5] Video + PTP-timecode monitor (background, port 8095)..."
# Stop any prior monitor, then launch it as its own persistent hidden WSL process via
# Start-Process (same pattern as the keepalive above) -- a backgrounded process inside a
# transient `wsl` call does not survive. monitor-run.sh keeps these args space-free.
wsl -d Ubuntu -u root -- bash -lc "pkill -f video-web.py; true"
Start-Process wsl -ArgumentList '-d','Ubuntu','-u','root','--','bash','/mnt/c/Users/dgper/pi-nmos-st2110/pc/monitor-run.sh' -WindowStyle Hidden
Start-Sleep -Seconds 4

Write-Output "[5/5] Checks..."
# Checks live in restore-check.sh (called by path) because PowerShell 5.1 mangles quoted
# bash passed inline to wsl.exe.
wsl -d Ubuntu -- bash /mnt/c/Users/dgper/pi-nmos-st2110/pc/restore-check.sh

Write-Output ""
Write-Output "DONE."
Write-Output "  Monitor (video+timecode):  http://localhost:8095   |  iPad: http://192.168.4.85:8095"
Write-Output "  NMOS audio-take watcher (run in a LOCAL WSL terminal):"
Write-Output "      python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/activation-watcher.py"
Write-Output "  On the Pi (SSH in, then):  sudo bash ~/launch-all.sh"
Write-Output ""
Write-Output "If the island IP above is blank, re-add it in an *admin* PowerShell:"
Write-Output '      New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.10.10.2 -PrefixLength 24'
