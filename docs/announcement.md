# Atoll — announcement / social copy

Marketing/announcement copy for Atoll, kept in-repo so it stays version-controlled alongside the
features it describes. Shareable image: [`docs/img/scorecard-card.jpg`](img/scorecard-card.jpg)
(all eight AMWA NMOS suites at 100%). The live scorecard it's rendered from is `pc/conformance-run.sh`.

**First comment to add after posting (keeps the link out of the body for reach):**

> Code (Apache-2.0): https://github.com/dgp1000/pi-nmos-st2110 — from-scratch setup in INSTALL.md, design walk in docs/ARCHITECTURE.md.

---

## LinkedIn post — short version (recommended)

🛰️ A while back I open-sourced Atoll — my home SMPTE ST 2110 / NMOS broadcast rig. It kept growing, and it now passes every applicable test across all eight AMWA NMOS suites (IS-04 → IS-12). 🟢

It's an iPad-controlled multiviewer + monitoring island: a GPU PC, a Raspberry Pi PTP grandmaster, and an isolated media LAN.

What's new since launch:
🧭 A full NMOS control plane — IS-10 authorization, IS-11 stream compatibility, and IS-12 device control that actually *drives the rig* (set a property, the program bus cuts).
🔌 Real controller interop — an independent, standards-only controller discovers and routes it over IS-05, including a cross-vendor connect straight into the nmos-cpp reference node.
🧯 ST 2022-7 hitless path protection + ST 2022-1 FEC, demoed live with injected packet loss.
📺 Live TV with make-before-break channel changes, CEA-608 captions, and EBU R128 loudness.
🎬 Record & playback with playlists; JPEG XS + H.264 / H.265 / VP9 / MJPEG / JPEG 2000 carried as conformant NMOS flows.

It started as a way to learn ST 2110 / NMOS end-to-end — and turned into a proper little broadcast island. Apache-2.0; repo in the comments 👇

#ST2110 #NMOS #BroadcastEngineering #IPVideo #GStreamer #SMPTE #PTP #OpenSource #MediaOverIP #AMWA

---

## LinkedIn post — full version

🛰️ A while back I open-sourced my home broadcast-over-IP rig to learn SMPTE ST 2110 / NMOS end-to-end. It kept growing — and Atoll now passes every applicable test across **all eight AMWA NMOS suites** (IS-04, 05, 07, 08, 09, 10, 11, 12), with a live conformance scorecard to prove it. 🟢

Atoll is an iPad-controlled ST 2110 / NMOS multiviewer + monitoring island: a PC with an NVIDIA GPU, a Raspberry Pi PTP grandmaster, and an isolated media LAN.

What's new since launch:

🧭 A full NMOS control plane — IS-10 authorization (RS256 JWT / JWKS), IS-11 stream compatibility (EDID + constraints), and IS-12 device control over WebSocket (the MS-05 object model) that actually *drives the rig* — set a property, the program bus cuts.

🔌 Real controller interop — an independent, standards-only controller discovers the rig through a third-party NMOS registry and routes it over IS-05, including a *cross-vendor* connect straight into the nmos-cpp reference node.

🎬 Record & playback with playlists — capture any source and replay it seamlessly as just another input.

🧯 Resilience you can watch fail and recover — ST 2022-7 hitless path protection and ST 2022-1 FEC, demoed live with injected packet loss.

📺 Live TV over HDHomeRun with make-before-break channel changes, live CEA-608 captions and SCTE-104 ad breaks, plus EBU R128 loudness metering.

🎚️ A pile of real essences — uncompressed 2110-20, 2110-30 audio, JPEG XS (2110-22), and H.264 / H.265 / VP9 / MJPEG / JPEG 2000 carried as conformant NMOS coded-video flows (BCP-006).

🖲️ A production switcher (PROGRAM/PREVIEW), a routable Program Out, IS-07 tally + IS-08 audio channel mapping, and a guided demo that walks the whole thing end to end.

Under the hood: nmos-cpp plus from-scratch IS-07/10/11/12 nodes, GStreamer + NVENC/NVDEC, an easy-nmos Docker stack, linuxptp, and mDNS/DNS-SD discovery — all config-driven, now with from-scratch install docs so anyone with the hardware can clone it and run their own.

It started as a way to learn ST 2110 / NMOS. It's turned into a genuinely capable little broadcast island.

Apache-2.0 on GitHub 👉 https://lnkd.in/emtZZsMu

#ST2110 #NMOS #BroadcastEngineering #IPVideo #GStreamer #SMPTE #PTP #OpenSource #MediaOverIP #AMWA #MS05
