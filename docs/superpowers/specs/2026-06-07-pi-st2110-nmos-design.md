# Design: Raspberry Pi 5 → PC "true" ST 2110 media over NMOS

**Date:** 2026-06-07
**Status:** Approved design — pending implementation plan
**Author:** dgperkins (with Claude)

## 1. Summary

Build a small but genuine SMPTE ST 2110 media setup that flows **real, uncompressed
essence** between two endpoints under **AMWA NMOS** control (IS-04 discovery / IS-05
connection), using hardware already on hand. A Raspberry Pi 5 acts as the sender and
control-plane host; an Ubuntu environment running under WSL on the Windows PC acts as
the receiver. Media is generated (test tone, test pattern) rather than captured, which
is exactly how broadcast test gear behaves.

This continues directly from the earlier `easy-nmos` work (a Dockerised NMOS sandbox
run in WSL), reusing the same `nmos-cpp` registry/node and the IS-04/IS-05 "take"
workflow already learned — but replacing the *simulated* media of the virtual node
with **actual RTP streams** the user can hear and see.

## 2. Goals & non-goals

### Goals
- Flow **true ST 2110-30** (uncompressed L24 PCM audio) end to end — the solid first win.
- Then flow **true ST 2110-20** (uncompressed RFC 4175 video) at low resolution.
- Run a **PTP** timing relationship between the nodes (learning-grade, see constraints).
- Use **multicast RTP on an isolated media network** (separate from the WiFi management/internet
  network) — mirroring broadcast practice and keeping media traffic off the home LAN.
- Keep the **NMOS control plane** central: nodes register with a registry, and an
  **IS-05 "take" actually starts/stops the media**, mirroring real gear.
- Success is **directly perceptible**: audio out the PC speakers, video in a desktop window.

### Non-goals (YAGNI / out of scope for now)
- Broadcast-grade timing accuracy (sub-microsecond PTP). Not achievable on this gear.
- Guaranteed audio/video **lip-sync** across the separate flows (stretch only).
- High-resolution or high-frame-rate video (physically impossible on a single GbE link).
- Capturing real cameras/microphones (generated test signals first; real I/O is a later idea).
- Redundancy (ST 2022-7), security (IS-10), or a controller GUI beyond the registry API.

## 3. Constraints (the physics and the environment)

These drove every major decision and are not negotiable on the current hardware:

1. **Pi 5 has a single gigabit NIC (~0.94 Gbps real).** Uncompressed ST 2110-20 video is
   enormous (1080p59 ≈ 3 Gbps), so video must be **tiny**: target **~320×240–426×240,
   25 fps, 8-bit 4:2:2** (~30–60 Mbps). Audio (-30) is ~2 Mbps and trivially fits.
2. **Both ends are wired into an isolated "media island."** A small PoE switch carries the
   Pi (powered over PoE) and the PC on a dedicated segment with **no uplink to the home
   router**, so media multicast/PTP never floods the house. Each device reaches the internet
   separately over **WiFi** (management/installs). This mirrors real broadcast practice of
   separating the **media** and **management** networks. Because the media path is wired and
   isolated, we use **multicast RTP** (the real ST 2110 transport) and get a **low-jitter PTP**
   relationship — both of which the earlier WiFi assumption had ruled out.
3. **No hardware PTP timestamping** on the Pi 5 onboard NIC or in WSL → **software
   timestamping only**. PTP is used to *learn the timing model and align RTP clocks*; the wired
   island makes it far tighter than WiFi would, but it is still not genlock-grade. Single-flow
   playback does not depend on tight PTP.
4. **Windows is a poor native ST 2110/PTP host.** We avoid native Windows tooling by running
   the Linux toolchain inside **WSL with `networkingMode=mirrored`**, which puts WSL on the
   home LAN (real multicast/LAN participation, unlike the default NAT that broke macvlan earlier).

## 4. Architecture

```
   PI 5  (PoE, the "real" node)                    WINDOWS PC
 ┌──────────────────────────────┐            ┌─────────────────────────────┐
 │ ptp4l  ── PTP leader          │            │  WSL Ubuntu (mirrored net)   │
 │ GStreamer SENDERS:            │            │ ┌─────────────────────────┐ │
 │  • audio: test tone           │            │ │ ptp4l ── PTP follower    │ │
 │    → L24 PCM → ST 2110-30 ──┐ │            │ │ GStreamer RECEIVERS:     │ │
 │  • video: test pattern      │ │            │ │  • audio → PC speakers   │ │
 │    → RFC4175 → ST 2110-20 ──┤ │            │ │    (WSLg audio)          │ │
 │ nmos-cpp NODE (2 senders)   │ │            │ │  • video → window (WSLg) │ │
 │ nmos-cpp REGISTRY           │ │            │ │ nmos-cpp NODE (receivers)│ │
 │ activation-watcher          │ │            │ │ activation-watcher       │ │
 │ eth0 10.10.10.1 ────────────┘ │            │ │ eth 10.10.10.2           │ │
 └──────────┬───────────────────┘            └──────────┬──────────────────┘
            │   MEDIA ISLAND (isolated PoE switch, 10.10.10.0/24)            
            └──────── multicast RTP + PTP ──────[switch]───────┘            
   wlan0 ─► home WiFi (internet/apt)            WiFi ─► home WiFi (internet)  
            IS-05 "take" → activation-watcher starts pipeline → media flows   
```

**Role split**
- **Pi 5** = media sender + registry host + PTP leader (always-on, wired, the clock anchor).
- **WSL on PC** = media receiver; plays audio via WSLg → PC speakers, opens video in a WSLg
  window on the Windows desktop; PTP follower.
- **Registry** brokers discovery; connections are made with IS-05 takes.

## 5. Components

### Pi 5 (sender + registry)
| Component | Role | Source |
|---|---|---|
| `linuxptp` (`ptp4l`) | PTP **leader**, software-timestamped, single chosen domain | apt |
| GStreamer audio sender | `audiotestsrc → L24 PCM 48 kHz, 1 ms ptime → rtpL24pay → udpsink` (RFC 3190 = -30 payload) | gstreamer |
| GStreamer video sender | `videotestsrc → UYVY 4:2:2 320×240@25 → rtpvrawpay → udpsink` (RFC 4175 = -20 payload) | gstreamer |
| `nmos-cpp` node | advertises the two senders (IS-04), exposes IS-05 | nmos-cpp |
| activation-watcher | polls local IS-05 `/active`; starts/stops the sender pipeline on `master_enable` | custom (~Python) |
| `nmos-cpp` registry | discovery broker (same as easy-nmos) | nmos-cpp |

### PC / WSL Ubuntu (receiver)
| Component | Role |
|---|---|
| `.wslconfig` → `networkingMode=mirrored` | puts WSL on the home LAN (one-time; needs `wsl --shutdown`) |
| `linuxptp` (`ptp4l`) | PTP **follower** (software TS; loose over WiFi) |
| GStreamer audio receiver | `udpsrc → rtpL24depay → audioconvert → autoaudiosink` → WSLg → PC speakers |
| GStreamer video receiver | `udpsrc → rtpvrawdepay → videoconvert → autovideosink` → WSLg window |
| `nmos-cpp` node | advertises the two receivers |
| activation-watcher | starts/stops receiver pipeline on IS-05 `master_enable` |

### The key integration component: the activation-watcher
The `easy-nmos` virtual node *fabricates* its own senders; here the nodes must advertise
**our specific** GStreamer streams, and an IS-05 take must actually **start/stop** media.
`nmos-cpp` does not launch GStreamer. The bridge is a small per-side script that polls its
local node's IS-05 `/single/<senders|receivers>/{id}/active` endpoint and starts or stops the
matching GStreamer pipeline when `master_enable` toggles. This mirrors how real NMOS gear
works (control plane decides; media engine reacts) and is where most custom work lives.

**Critical correctness invariant:** the node's advertised **SDP transport file must exactly
match what GStreamer actually sends** — payload type, clock rate, channel count, sampling,
bit depth, and ptime. Mismatch is the classic "connects but won't decode" failure.

## 6. Phased build plan

Each phase proves the hardest physical layer first and ends in an observable result.

| Phase | Build | Done when |
|---|---|---|
| **0 · Connectivity** | Wire media island; static IPs (Pi `eth0` 10.10.10.1 / PC `eth` 10.10.10.2); WSL `mirrored` networking; open Windows Firewall inbound UDP | Pi ↔ PC reach each other on 10.10.10.x and exchange UDP both ways |
| **1 · Real audio** ⭐ | Pi tone → multicast RTP; WSL receiver → speakers (no NMOS/PTP yet) | Tone audible on PC speakers; `tcpdump` shows L24/48 kHz RTP |
| **2 · PTP** | `ptp4l` leader (Pi) + follower (WSL); discipline clock | ptp4l shows lock; offset readable (tens of µs over the wired island); audio still plays |
| **3 · NMOS discovery** | registry on Pi; a node each side advertising the real sender/receiver with matching SDP | registry query lists both nodes; SDP matches the stream |
| **4 · IS-05 drives media** ⭐ | activation-watcher scripts | IS-05 take **starts** audio; disable **stops** it |
| **5 · Low-res video** ⭐ | add -20 sender pipeline + SDP + node sender; receiver opens a window | test pattern appears in a Windows desktop window, started by an IS-05 take |
| **6 · Lip-sync (stretch)** | align A/V via PTP-derived RTP timestamps | best-effort only; tighter now on the wired island, still not promised |

⭐ = the "felt" milestones: hear it → control it → see it.

## 7. Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Mirrored networking unavailable (pre-Win11 22H2) | Check Windows build in Phase 0; fallback = registry+receiver on Pi side, or a bridged Hyper-V Linux VM |
| Windows Firewall blocks inbound UDP to WSL | Add explicit inbound allow rule for the RTP/PTP ports (likely cause of a silent Phase 1) |
| Media island has no DHCP (no router uplink) | Assign **static IPs** on the media interfaces (Pi `eth0` 10.10.10.1, PC `eth` 10.10.10.2 / 24); internet/`apt` come over each device's WiFi |
| Unmanaged switch floods multicast | Island has only 2 ports + no uplink, so flooding is contained; if a managed switch is used later, enable IGMP snooping |
| PC dual-homed (WiFi + wired) routing ambiguity | Media interfaces are a separate subnet (10.10.10.0/24) from the WiFi LAN; bind GStreamer/PTP explicitly to the media interface |
| Residual jitter | GStreamer `rtpjitterbuffer` absorbs it; wired island keeps it low to begin with |
| SDP ↔ pipeline mismatch | Pin PT, clock rate, sampling, depth, ptime in both SDP and GStreamer caps; verify with packet capture |
| No arm64 `nmos-cpp` image | Verify in Phase 3 prep; else run registry on WSL and build the Pi node from source |
| PTP won't lock over WiFi | Accept loose sync (documented non-goal); keep PTP for learning; media plays without it |

## 8. Verification toolkit
- `tcpdump` / Wireshark — RTP headers, payload type, and SDP inspection.
- `ptp4l` logs / `pmc` — timing offset and lock state.
- `nmos-cpp` registry **Query API** (already familiar) — control-plane state.
- The AMWA **NMOS Testing Tool** (already running in easy-nmos) — optional conformance of the
  real nodes' IS-04/IS-05 APIs.
- Ultimately: **ears** (audio) and **eyes** (video window).

## 9. Open questions to resolve during implementation
- Which PTP **domain** to standardise on (AES67 default 0 vs ST 2059 default 127) — pick one
  consistently across both ends.
- Exact low-res video format that balances "looks like video" against the WiFi/GbE budget.
- Whether to host the **registry** on the Pi (preferred, always-on) or in WSL (fallback if no
  arm64 image).
- Direction scope: start **Pi→PC one-way**; bidirectional is a later extension.

## 10. Reference: prior context
- Earlier `easy-nmos` setup lives in WSL at `/root/easy-nmos` with a custom
  `docker-compose.wsl.yml` (bridge networking, localhost port mappings). That work established
  the registry, the IS-04/IS-05 workflow, and the live "take" the user performed. This project
  reuses that knowledge and tooling, swapping simulated media for real RTP essence.
