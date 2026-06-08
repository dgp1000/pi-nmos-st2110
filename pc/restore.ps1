# Restore the PC/WSL side after a reboot.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\pi-nmos-st2110\pc\restore.ps1"

Write-Output "[1/4] Booting WSL + keepalive (so the VM stays up)..."
Start-Process wsl -ArgumentList '-d','Ubuntu','--','tail','-f','/dev/null' -WindowStyle Hidden
Start-Sleep -Seconds 3

Write-Output "[2/4] Docker + NMOS registry/node..."
wsl -d Ubuntu -u root -- bash -lc "systemctl start docker; sleep 3; cd /root/easy-nmos && docker compose -f docker-compose.wsl.yml up -d nmos-registry nmos-virtnode 2>&1 | tail -3"

Write-Output "[3/4] Video + PTP-timecode monitor (background, port 8095)..."
wsl -d Ubuntu -u root -- bash -lc "export LD_LIBRARY_PATH=/root/jxs-install/lib; setsid python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/video-web.py >/tmp/videoweb.log 2>&1 </dev/null &"
Start-Sleep -Seconds 2

Write-Output "[4/4] Checks..."
wsl -d Ubuntu -- bash -lc "echo -n 'WSL island IP: '; ip -4 -br addr show eth1 2>/dev/null | awk '{print \$3}'; curl -s -m4 -o /dev/null -w 'monitor http %{http_code}\n' http://localhost:8095/ 2>/dev/null"

Write-Output ""
Write-Output "DONE."
Write-Output "  Monitor (video+timecode):  http://localhost:8095   |  iPad: http://192.168.4.85:8095"
Write-Output "  NMOS audio-take watcher (run in a LOCAL WSL terminal):"
Write-Output "      python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/activation-watcher.py"
Write-Output "  On the Pi (SSH in, then):  sudo bash ~/launch-all.sh"
Write-Output ""
Write-Output "If the island IP above is blank, re-add it in an *admin* PowerShell:"
Write-Output '      New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.10.10.2 -PrefixLength 24'
