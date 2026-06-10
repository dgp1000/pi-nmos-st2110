# Restore the PC/WSL side after a reboot.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\pi-nmos-st2110\pc\restore.ps1"

# --- config: the repo lives under the Windows user's home; network values mirror pc/atoll.conf ---
$User    = $env:USERNAME
$Distro  = "Ubuntu"
$Pc      = "/mnt/c/Users/$User/pi-nmos-st2110/pc"          # repo pc/ as seen from WSL
$ConfWin = "C:\Users\$User\pi-nmos-st2110\pc\atoll.conf"   # same file, Windows path (for display values)
function Get-Conf($key) { $l = Get-Content $ConfWin | Where-Object { $_ -match "^\s*$key=" } | Select-Object -First 1; if ($l -match "^\s*$key=([^\s#]+)") { $matches[1] } else { '' } }
$WifiIp = Get-Conf 'PC_WIFI_IP'; $PanelPort = Get-Conf 'PANEL_PORT'; $IslandIp = Get-Conf 'ISLAND_PC_IP'

Write-Output "[1/6] Booting WSL + keepalive (so the VM stays up)..."
# Clear any stale keepalives first so re-running this script doesn't stack them.
wsl -d $Distro -u root -- bash -lc "pkill -f 'tail -f /dev/null'; true"
Start-Process wsl -ArgumentList '-d',$Distro,'--','tail','-f','/dev/null' -WindowStyle Hidden
Start-Sleep -Seconds 3

Write-Output "[2/6] GPU: ensure D3D12 OpenGL driver for WSL (vs CPU llvmpipe)..."
wsl -d $Distro -u root -- bash "$Pc/wsl-gpu-setup.sh"

Write-Output "[3/6] Docker + NMOS registry/node + AMWA testing tool..."
wsl -d $Distro -u root -- bash -lc "systemctl start docker; sleep 3; cd /root/easy-nmos && docker compose -f docker-compose.wsl.yml up -d nmos-registry nmos-virtnode nmos-testing 2>&1 | tail -3"

Write-Output "[4/6] IS-05 switch panel + IS-04/05 inspector (background, port $PanelPort)..."
# Persistent hidden WSL window holds the panel (monitor-run.sh exec's it in the foreground).
# Run as the USER (no -u root) -- the earlier root launch died right after start (panel empty).
# monitor-run.sh stops any prior panel itself.
Start-Process wsl -ArgumentList '-d',$Distro,'--','bash',"$Pc/monitor-run.sh" -WindowStyle Hidden
Start-Sleep -Seconds 4

Write-Output "[5/6] Media pipeline: senders (bbb/home/music/reels) + monitor-2 multiview, as the user..."
# Runs senders + renderer from a Linux-side copy of pc/*.sh (dodges the /mnt/c 9p cache race that
# silently broke a sender mid-session). As the user (not root) for GPU/NVENC + WSLg. Idempotent;
# backgrounds its own work (setsid) and exits. Comment out for a control-only boot.
Start-Process wsl -ArgumentList '-d',$Distro,'--','bash',"$Pc/launch-media.sh" -WindowStyle Hidden
Start-Sleep -Seconds 2

Write-Output "[6/6] Checks..."
# Checks live in restore-check.sh (called by path) because PowerShell mangles quoted bash inline.
wsl -d $Distro -- bash "$Pc/restore-check.sh"

Write-Output ""
Write-Output "DONE."
Write-Output "  IS-05 switch panel + IS-04/05 inspector:  http://localhost:$PanelPort   |  iPad: http://${WifiIp}:$PanelPort"
Write-Output "  AMWA NMOS Testing Tool:  http://${WifiIp}:5000   |  Registry browser: http://${WifiIp}:8080"
Write-Output "      (one-time, elevated, to reach 5000/8080 from the iPad:"
Write-Output "       powershell -ExecutionPolicy Bypass -File pc\open-nmos-firewall.ps1)"
Write-Output "  Watch video on the native PC window (glitch-free):  bash pc/hevc-stream-view-file.sh ~/jxs-media/bbb-4k.mp4"
Write-Output "  NMOS audio-take watcher (run in a LOCAL WSL terminal):"
Write-Output "      python3 $Pc/activation-watcher.py"
Write-Output "  On the Pi (SSH in, then):  sudo bash ~/launch-all.sh"
Write-Output ""
Write-Output "If the island IP above is blank, re-add it in an *admin* PowerShell:"
Write-Output "      New-NetIPAddress -InterfaceAlias `"Ethernet`" -IPAddress $IslandIp -PrefixLength 24"
