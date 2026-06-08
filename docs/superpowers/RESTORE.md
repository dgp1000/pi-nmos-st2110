# Restore after a Windows reboot

**What survives the reboot (no action needed):** WSL filesystem (FFmpeg+JPEG XS build, SVT, easy-nmos, all repo scripts), `.wslconfig` (mirrored networking), the Windows `Ethernet` static IP `10.10.10.2`, Windows firewall rules, the Pi's `eth0` 10.10.10.1 (and the Pi itself — it's PoE-powered and does not reboot). Docker is enabled on boot; NMOS containers have `restart: unless-stopped`.

**What needs relaunching:** the running generators/servers (they die when their SSH sessions / WSL stop).

## Before you reboot
Copy the Pi relaunch script onto the Pi (one time):
```powershell
scp "C:\Users\dgper\pi-nmos-st2110\pi\launch-all.sh" dgperkins@pi5-nmos.local:~/
```

## After the reboot — 2 steps

### 1) PC / WSL  (PowerShell)
```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\pi-nmos-st2110\pc\restore.ps1"
```
Boots WSL (+ keepalive), starts Docker + the NMOS registry/node, and launches the video+PTP-timecode monitor on http://localhost:8095. It prints checks at the end.

### 2) Pi  (SSH)
```bash
ssh dgperkins@pi5-nmos.local
sudo bash ~/launch-all.sh
```
Relaunches the PTP grandmaster, ST 2110-30 audio sender, ST 2110-20 (59.94) video sender, and the PTP web clock (http://pi5-nmos.local:8000).

## Then, to verify the reboot fixed WSLg windows
In a **local WSL terminal**: `DISPLAY=:0 xeyes`  — if a window now paints, native `ffplay`/GStreamer windows will work too.

## On-demand (not auto-started)
- **NMOS audio "take" watcher** (local WSL terminal, for IS-05-driven audio):
  `python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/activation-watcher.py`  then `... take.py on|off`
- **Terminal studio clock** (Pi): `sudo python3 ~/master-clock.py`
- **Jitter meter** (WSL): `python3 /mnt/c/Users/dgper/pi-nmos-st2110/pc/jitter-meter.py`
- **JPEG XS streaming** (WSL): the FFmpeg sender/probe from `pc/` (FFmpeg at /usr/local/bin, libs registered via ldconfig).

## Key addresses
Pi WiFi mgmt 192.168.6.232 · Pi island eth0 10.10.10.1 · PC island 10.10.10.2 (WSL eth1) ·
audio 239.10.10.10:5004 · video 239.10.10.20:5005 · monitor :8095 · web clock :8000 · NMOS registry :8080 / node :8090
