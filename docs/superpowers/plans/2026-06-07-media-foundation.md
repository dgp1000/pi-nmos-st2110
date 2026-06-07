# Media Foundation (Pi 5 → PC ST 2110 audio + PTP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flow real, uncompressed ST 2110-30 (L24 PCM) audio from a Raspberry Pi 5 to an Ubuntu/WSL receiver on the Windows PC — audible on the PC speakers — with a PTP timing relationship running alongside.

**Architecture:** Pi (wired) generates a test tone, packetises it as RFC 3190 L24 RTP, and unicasts it to the PC. WSL (in `mirrored` networking mode, on the home LAN) receives it and plays it via WSLg → Windows speakers. `linuxptp` runs leader-on-Pi / follower-on-PC in software-timestamp mode for learning; audio does not depend on tight sync.

**Tech Stack:** GStreamer 1.x (`rtpL24pay`/`rtpL24depay`), linuxptp (`ptp4l`), WSL2 mirrored networking, WSLg audio, Python 3 (test utilities).

**Scope note:** This is Plan 1 of 2. It corresponds to spec Phases 0–2 and is a complete, testable deliverable on its own. Plan 2 (NMOS nodes, IS-05-driven media, low-res video — spec Phases 3–6) is written after this one works and the Pi confirms arm64 nmos-cpp availability.

**Repository layout produced by this plan:**
```
pi-nmos-st2110/
  pi/                 # files copied to / run on the Raspberry Pi
    send-audio.sh
    ptp4l-leader.conf
  pc/                 # files run inside WSL Ubuntu on the PC
    recv-audio.sh
    ptp4l-follower.conf
  tools/
    udp-listen.py     # portable UDP connectivity test (receiver)
    udp-send.py       # portable UDP connectivity test (sender)
  wsl/
    wslconfig.reference  # reference copy of the .wslconfig we apply
  README.md
```

**Conventions used in this plan:**
- `PI_IP` = the Pi's LAN address (get on the Pi with `hostname -I`).
- `PC_IP` = the PC/WSL LAN address (get in WSL after Phase 0 with `hostname -I`).
- Commands prefixed `# on Pi` run in an SSH/terminal session on the Raspberry Pi.
- Commands prefixed `# in WSL` run inside the Ubuntu WSL distro on the PC.
- Commands prefixed `# in PowerShell` run in a Windows PowerShell window.
- The Pi runs Raspberry Pi OS (64-bit) or Ubuntu; both use `apt`. Pi commands use `sudo`. The WSL distro currently logs in as root, so `sudo` is omitted there.

---

## Task 0: Repository scaffolding

> **Git environment for this plan:** the repo lives on the Windows filesystem at
> `C:\Users\dgper\pi-nmos-st2110` (= `/mnt/c/Users/dgper/pi-nmos-st2110` from WSL). All
> `git` commands in this plan are run **in WSL** for consistency with the Linux scripts and
> `chmod`. Two one-time guards (Step 1) make that safe: `safe.directory` (avoids WSL git's
> "dubious ownership" refusal on Windows-owned files) and a `.gitattributes` that forces LF
> line endings so bash scripts do not get CRLF and break on the Pi.

**Files:**
- Create: `.gitattributes`
- Create: `pi/`, `pc/`, `tools/`, `wsl/` directories (via the files below)
- Create: `README.md`

- [ ] **Step 1: Configure git safety + line-ending policy, create directories**

```bash
# in WSL, from the repo root
cd /mnt/c/Users/dgper/pi-nmos-st2110
git config --global --add safe.directory /mnt/c/Users/dgper/pi-nmos-st2110
mkdir -p pi pc tools wsl
```

`.gitattributes`:
```gitattributes
# Force LF everywhere so bash scripts run on Linux (Pi / WSL) without CRLF breakage.
* text=auto eol=lf
*.sh  text eol=lf
*.py  text eol=lf
*.conf text eol=lf
*.md  text eol=lf
```

`README.md`:
```markdown
# pi-nmos-st2110

Real ST 2110 media between a Raspberry Pi 5 and a Windows PC under NMOS control.
See docs/superpowers/specs/ for the design and docs/superpowers/plans/ for the build.

- `pi/`   files run on the Raspberry Pi (sender, PTP leader)
- `pc/`   files run inside WSL Ubuntu on the PC (receiver, PTP follower)
- `tools/` portable connectivity test helpers
- `wsl/`  reference WSL configuration
```

- [ ] **Step 2: Commit**

```bash
# in WSL
git add .gitattributes README.md
git commit -m "chore: scaffold repo layout + LF policy for media foundation"
```

---

## Task 1: Confirm Windows supports mirrored networking

Mirrored networking requires Windows 11 22H2 (build 22621) or newer. The machine reported build 26200 earlier, so this should pass — this task confirms it and records the result.

**Files:** none (verification only)

- [ ] **Step 1: Check the Windows build**

```powershell
# in PowerShell
[System.Environment]::OSVersion.Version
```

Expected: `Build` ≥ 22621 (e.g. `10.0.26200`). If lower, STOP — use the fallback in the spec (registry+receiver on the Pi, or a bridged Hyper-V Linux VM) and do not proceed with mirrored mode.

- [ ] **Step 2: Confirm WSL version supports it**

```powershell
# in PowerShell
wsl --version
```

Expected: a `WSL version:` line of 2.0.0 or newer. If `wsl --version` errors (older Store-less WSL), run `wsl --update` first.

---

## Task 2: Switch WSL to mirrored networking

**Files:**
- Create: `C:\Users\dgper\.wslconfig`
- Create: `wsl/wslconfig.reference` (committed copy)

- [ ] **Step 1: Write the reference config into the repo**

`wsl/wslconfig.reference`:
```ini
[wsl2]
networkingMode=mirrored
# dnsTunneling and firewall default on; mirrored puts WSL directly on the host LAN
```

- [ ] **Step 2: Install it as the real .wslconfig**

```powershell
# in PowerShell
Copy-Item "$env:USERPROFILE\pi-nmos-st2110\wsl\wslconfig.reference" "$env:USERPROFILE\.wslconfig"
Get-Content "$env:USERPROFILE\.wslconfig"
```

Expected: prints the `[wsl2]` / `networkingMode=mirrored` block. (The repo is at `C:\Users\dgper\pi-nmos-st2110` = `$env:USERPROFILE\pi-nmos-st2110`. If it differs, just create `$env:USERPROFILE\.wslconfig` with the same two-line content.)

- [ ] **Step 3: Restart WSL to apply**

```powershell
# in PowerShell
wsl --shutdown
Start-Sleep -Seconds 3
wsl -d Ubuntu -- echo "wsl back up"
```

Expected: prints `wsl back up`.

- [ ] **Step 4: Verify WSL is now on the home LAN (not 172.x NAT)**

```bash
# in WSL
ip -4 addr show | grep -v 127.0.0.1 | grep inet
ip route | grep default
```

Expected: an address on your home subnet (e.g. `192.168.x.y`) that matches the PC's WiFi IP, and a default route via your home router (e.g. `192.168.x.1`). It should NO LONGER be a `172.x` address. Record this as `PC_IP`.

- [ ] **Step 5: Commit**

```bash
# in WSL
git add wsl/wslconfig.reference
git commit -m "feat: enable WSL mirrored networking config"
```

---

## Task 3: Open Windows Firewall for inbound media/PTP UDP

In mirrored mode, inbound LAN traffic to WSL is gated by the Windows host firewall. Open the UDP ports we will use: 5004 (audio RTP), 5006 (reserved for video RTP in Plan 2), 319 + 320 (PTP event/general).

**Files:** none (system config; the command is recorded in README troubleshooting later)

- [ ] **Step 1: Add the inbound rule**

```powershell
# in PowerShell, as Administrator
New-NetFirewallRule -DisplayName "ST2110 RTP/PTP inbound (WSL)" `
  -Direction Inbound -Action Allow -Protocol UDP `
  -LocalPort 5004,5006,319,320
```

Expected: prints the created rule with `Enabled : True`.

- [ ] **Step 2: Verify the rule exists**

```powershell
# in PowerShell
Get-NetFirewallRule -DisplayName "ST2110 RTP/PTP inbound (WSL)" | Select DisplayName,Enabled,Direction,Action
```

Expected: one rule, `Enabled True`, `Direction Inbound`, `Action Allow`.

> Troubleshooting note for later: if Phase-1 audio is silent despite the sender running, and `tcpdump` on the PC shows no packets, the inbound block is the prime suspect. On Windows 11 23H2+ you may additionally need a Hyper-V firewall rule:
> `New-NetFirewallHyperVRule -Name ST2110 -DisplayName "ST2110 WSL" -Direction Inbound -Action Allow -Protocol UDP -LocalPorts 5004,5006,319,320 -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'`

---

## Task 4: Prove bidirectional UDP between Pi and PC

Before any media, prove plain UDP flows both directions over the LAN. This isolates networking from GStreamer.

**Files:**
- Create: `tools/udp-listen.py`
- Create: `tools/udp-send.py`

- [ ] **Step 1: Write the UDP test helpers**

`tools/udp-listen.py`:
```python
#!/usr/bin/env python3
"""Listen for one UDP datagram on a port and print it. Usage: udp-listen.py <port>"""
import socket, sys
port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", port))
print(f"listening on udp/{port} ...", flush=True)
data, addr = s.recvfrom(2048)
print(f"received {data!r} from {addr}")
```

`tools/udp-send.py`:
```python
#!/usr/bin/env python3
"""Send one UDP datagram. Usage: udp-send.py <host> <port> [message]"""
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
msg = (sys.argv[3] if len(sys.argv) > 3 else "hello-st2110").encode()
socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(msg, (host, port))
print(f"sent {msg!r} to {host}:{port}")
```

- [ ] **Step 2: Get this repo onto the Pi**

```bash
# on Pi (once): clone via the GitHub remote if you push it, OR copy tools/ over scp.
# Simplest for now, copy the two helpers via scp from the PC:
# in WSL:
scp tools/udp-listen.py tools/udp-send.py <pi-user>@$PI_IP:~/
```

Expected: two files land in the Pi user's home directory.

- [ ] **Step 3: Test PC → Pi**

```bash
# on Pi
python3 ~/udp-listen.py 9999
```
```bash
# in WSL (second terminal)
python3 tools/udp-send.py $PI_IP 9999 from-pc
```

Expected: the Pi prints `received b'from-pc' from ('<PC_IP>', ...)`.

- [ ] **Step 4: Test Pi → PC**

```bash
# in WSL
python3 tools/udp-listen.py 9999
```
```bash
# on Pi
python3 ~/udp-send.py $PC_IP 9999 from-pi
```

Expected: WSL prints `received b'from-pi' from ('<PI_IP>', ...)`. If this direction fails but the other worked, revisit Task 3 (firewall).

- [ ] **Step 5: Commit**

```bash
# in WSL
git add tools/udp-listen.py tools/udp-send.py
git commit -m "feat: add portable UDP connectivity test helpers"
```

**Phase 0 complete when both UDP directions succeed.**

---

## Task 5: Install GStreamer on both ends

**Files:** none (package install)

- [ ] **Step 1: Install on the Pi**

```bash
# on Pi
sudo apt-get update
sudo apt-get install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-alsa
```

- [ ] **Step 2: Install in WSL**

```bash
# in WSL
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pulseaudio
```

- [ ] **Step 3: Verify the L24 payloader is present on both ends**

```bash
# on Pi AND in WSL
gst-inspect-1.0 rtpL24pay > /dev/null && echo "rtpL24pay OK"
gst-inspect-1.0 rtpL24depay > /dev/null && echo "rtpL24depay OK"
```

Expected: `rtpL24pay OK` and `rtpL24depay OK` on both machines. (Both live in `gstreamer1.0-plugins-good`.)

- [ ] **Step 4: Verify WSL audio output path exists**

```bash
# in WSL
gst-inspect-1.0 autoaudiosink > /dev/null && echo "sink OK"
gst-launch-1.0 audiotestsrc num-buffers=50 ! audioconvert ! autoaudiosink
```

Expected: `sink OK`, then a short beep from the PC speakers (proves WSLg audio works before we involve the network). If silent, ensure WSLg is enabled (`wsl --update`) and the PC volume is up.

---

## Task 6: Audio receiver script (PC/WSL)

Write the receiver first so it is listening when the sender starts.

**Files:**
- Create: `pc/recv-audio.sh`

- [ ] **Step 1: Write the receiver pipeline**

`pc/recv-audio.sh`:
```bash
#!/usr/bin/env bash
# Receive ST 2110-30 (L24 PCM, 48kHz, stereo) RTP and play to PC speakers via WSLg.
set -euo pipefail
PORT="${1:-5004}"
exec gst-launch-1.0 -v \
  udpsrc port="${PORT}" \
    caps="application/x-rtp,media=(string)audio,clock-rate=(int)48000,encoding-name=(string)L24,channels=(int)2,payload=(int)96" \
  ! rtpjitterbuffer latency=50 \
  ! rtpL24depay \
  ! audioconvert \
  ! autoaudiosink sync=false
```

- [ ] **Step 2: Make it executable and commit**

```bash
# in WSL
chmod +x pc/recv-audio.sh
git add pc/recv-audio.sh
git commit -m "feat: add ST 2110-30 audio receiver pipeline"
```

---

## Task 7: Audio sender script (Pi) and first sound

**Files:**
- Create: `pi/send-audio.sh`

- [ ] **Step 1: Write the sender pipeline**

`pi/send-audio.sh`:
```bash
#!/usr/bin/env bash
# Send a 440Hz test tone as ST 2110-30 (L24 PCM, 48kHz, stereo, 1ms ptime) RTP.
# Usage: send-audio.sh <dest-ip> [port]
set -euo pipefail
DEST="${1:?usage: send-audio.sh <dest-ip> [port]}"
PORT="${2:-5004}"
exec gst-launch-1.0 -v \
  audiotestsrc is-live=true wave=sine freq=440 \
  ! audioconvert ! audioresample \
  ! audio/x-raw,format=S24BE,rate=48000,channels=2 \
  ! rtpL24pay pt=96 min-ptime=1000000 max-ptime=1000000 \
  ! udpsink host="${DEST}" port="${PORT}"
```

(`min/max-ptime` are nanoseconds; `1000000` ns = 1 ms packet time, the ST 2110-30 default. `S24BE` = 24-bit big-endian, the L24 wire format.)

- [ ] **Step 2: Copy it to the Pi**

```bash
# in WSL
scp pi/send-audio.sh <pi-user>@$PI_IP:~/
```

- [ ] **Step 3: Start the receiver, then the sender**

```bash
# in WSL (leave running)
./pc/recv-audio.sh 5004
```
```bash
# on Pi
chmod +x ~/send-audio.sh
~/send-audio.sh $PC_IP 5004
```

Expected: **a steady 440 Hz tone from the PC speakers.** The receiver terminal shows pipeline state `PLAYING` and rising buffer stats.

- [ ] **Step 4: Confirm it is real ST 2110-30 traffic on the wire**

```bash
# in WSL (third terminal), while audio is playing
sudo tcpdump -i any -n udp port 5004 -c 5
```

Expected: 5 UDP packets from `PI_IP` to `PC_IP:5004`. (Optional deeper check: `tcpdump ... -X` and confirm the RTP payload-type byte is 96 / `0x60` in the second header byte.)

- [ ] **Step 5: Commit**

```bash
# in WSL
git add pi/send-audio.sh
git commit -m "feat: add ST 2110-30 audio sender pipeline (Pi)"
```

**Phase 1 complete when you hear the tone and tcpdump shows the RTP flow.** This is the first ⭐ milestone.

---

## Task 8: PTP leader config (Pi)

**Files:**
- Create: `pi/ptp4l-leader.conf`

- [ ] **Step 1: Install linuxptp on both ends**

```bash
# on Pi
sudo apt-get install -y linuxptp
# in WSL
apt-get install -y linuxptp
```

- [ ] **Step 2: Write the leader config**

`pi/ptp4l-leader.conf`:
```ini
[global]
# Pi onboard NIC has no hardware PHC -> software timestamping.
time_stamping        software
clock_servo          pi
domainNumber         0
# Announce ourselves as a usable clock so the follower selects us.
priority1            128
logSyncInterval      -3
logAnnounceInterval  1

[eth0]
masterOnly           1
```

- [ ] **Step 3: Run the leader**

```bash
# on Pi (leave running; Ctrl-C to stop)
sudo ptp4l -f ~/ptp4l-leader.conf -i eth0 -m
```

Expected: log lines progressing to `port 1: MASTER` and periodic `master offset` / sync output. (Replace `eth0` if the Pi's wired interface differs — check with `ip -o link`.)

- [ ] **Step 4: Copy the config to the Pi and commit**

```bash
# in WSL
scp pi/ptp4l-leader.conf <pi-user>@$PI_IP:~/
git add pi/ptp4l-leader.conf
git commit -m "feat: add PTP leader config (Pi)"
```

---

## Task 9: PTP follower (PC/WSL) and observe sync

WSL's clock is backed by the host, so the follower is run primarily to **observe** the offset to the Pi's clock — that is the learning goal. Do not be surprised if WSL cannot fully step its own clock; the measured offset is what matters.

**Files:**
- Create: `pc/ptp4l-follower.conf`

- [ ] **Step 1: Write the follower config**

`pc/ptp4l-follower.conf`:
```ini
[global]
time_stamping   software
clock_servo     pi
domainNumber    0
slaveOnly       1
```

- [ ] **Step 2: Identify the WSL interface and run the follower**

```bash
# in WSL
ip -o link | grep -v lo    # note the interface name (e.g. eth0)
ptp4l -f pc/ptp4l-follower.conf -i eth0 -m
```

Expected: the follower selects the Pi as best master (`new foreign master`, then `port 1: UNCALIBRATED` → `SLAVE`) and prints periodic `master offset <N> s2 freq ...` lines. Over a wired path `<N>` settles to tens of microseconds; **over WiFi expect it noisier and larger — that is the documented, accepted limitation.**

- [ ] **Step 3: Record an honest result**

Watch for ~60 seconds. Note the typical `master offset` range. Audio from Task 7 should still play unaffected while PTP runs.

- [ ] **Step 4: Commit**

```bash
# in WSL
git add pc/ptp4l-follower.conf
git commit -m "feat: add PTP follower config (PC/WSL)"
```

**Phase 2 complete when the follower locks to the Pi as master and prints a converging (if loose) offset, with audio still playing.**

---

## Task 10: Capture the foundation result in the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append a "Foundation status" section**

Add to `README.md`:
```markdown
## Foundation status (Plan 1)

- [x] WSL on home LAN via mirrored networking (PC_IP recorded)
- [x] Bidirectional UDP Pi <-> PC verified
- [x] ST 2110-30 L24 audio Pi -> PC, audible on PC speakers
- [x] PTP leader (Pi) / follower (PC) observed; typical offset: <FILL IN observed range>

### Run it
1. PC:  `./pc/recv-audio.sh 5004`
2. Pi:  `~/send-audio.sh <PC_IP> 5004`
3. (optional timing) Pi: `sudo ptp4l -f ~/ptp4l-leader.conf -i eth0 -m`;
   PC: `ptp4l -f pc/ptp4l-follower.conf -i eth0 -m`
```

Replace `<FILL IN observed range>` with the offset you recorded in Task 9.

- [ ] **Step 2: Commit**

```bash
# in WSL
git add README.md
git commit -m "docs: record media-foundation run instructions and status"
```

---

## Done / Next plan

When all tasks above are checked, Plan 1 is complete: **real ST 2110-30 audio flows Pi → PC and you can hear it, with PTP running.**

Then we write **Plan 2 (NMOS + video)** covering spec Phases 3–6:
1. First task of Plan 2 resolves the unknowns this plan deliberately deferred:
   - `docker manifest inspect rhastie/nmos-cpp:latest` (and `sony/nmos-cpp`) for an `arm64` variant; decide registry host (Pi vs WSL) from the result.
   - Dump the nmos-cpp node config schema to pin the exact keys for advertising our specific audio/video senders.
2. Then: registry + nodes (IS-04), the activation-watcher (IS-05 take → start/stop GStreamer), and the low-res RFC 4175 video sender/receiver (`videotestsrc → rtpvrawpay` / `rtpvrawdepay → autovideosink` in a WSLg window).
```
