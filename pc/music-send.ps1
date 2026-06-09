# Atoll "Now Playing" music channel (Windows side).
# Captures the Mac music server's now-playing page (a Windows Chrome window) -> 720p HEVC
# (NVENC) -> MPEG-TS -> island multicast 239.10.10.30:5012, which WSL/output-render decodes
# as the bottom-right multiview tile.
#
# gdigrab captures the SCREEN REGION at the window's rect, so the "Now Playing" window must
# stay ON-SCREEN and UN-OCCLUDED (Chrome also blacks out off-screen windows). Audio is muted
# here (visual-only, phase 1); the visualiser still animates from the Web Audio graph.
#
# Run hidden/detached:
#   powershell -NoProfile -Command "Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\Users\dgper\pi-nmos-st2110\pc\music-send.ps1' -WindowStyle Hidden"
$ErrorActionPreference = 'SilentlyContinue'
$url    = 'http://192.168.6.159:8008/nowplaying'
$group  = '239.10.10.30'; $port = '5012'; $island = '10.10.10.2'
$udd    = 'C:\Users\dgper\.atoll-music-chrome'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'

# (re)launch the capture Chrome
Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
  Where-Object { $_.CommandLine -like '*atoll-music-chrome*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
Start-Process $chrome -ArgumentList @(
  "--app=$url",'--mute-audio',
  '--disable-gpu','--disable-gpu-compositing','--disable-direct-composition',
  '--disable-backgrounding-occluded-windows','--disable-features=CalculateNativeWinOcclusion',
  '--autoplay-policy=no-user-gesture-required',
  '--window-size=1280,760','--window-position=60,60',
  "--user-data-dir=$udd",'--no-first-run','--no-default-browser-check'
)
Start-Sleep 6

# capture -> 720p HEVC -> island multicast; loop so it self-restarts if it drops
$dest = "udp://${group}:${port}?pkt_size=1316&localaddr=${island}&ttl=1"
while ($true) {
  & ffmpeg -hide_banner -loglevel warning -f gdigrab -framerate 30 -i 'title=Now Playing' `
    -vf 'scale=1280:720,format=yuv420p' `
    -c:v hevc_nvenc -preset p4 -tune ll -b:v 6M -g 30 -forced-idr 1 -an `
    -f mpegts $dest
  Start-Sleep 2
}
