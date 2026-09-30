# Atoll LTC — PTP-locked timecode box on an Arduino UNO Q

An Arduino UNO Q (Linux MPU + STM32U585 MCU) on the island LAN that follows the Atoll
PTP grandmaster and turns it into physical timecode and sync pulses, with on-air tally on
a Modulino Pixels strip (and take buttons if a Modulino Buttons is added). It is the rig's "LTC generator" — something broadcast people buy —
and none of the commodity ones talk to an NMOS rig.

| Pin / part | Signal |
|---|---|
| **D2** | SMPTE/EBU LTC, 25 fps, 80-bit biphase-mark, 3.3 V square wave |
| **D3** | 1 ms pulse at every frame start |
| **D4** | 100 ms pulse at the top of every second (PPS) |
| Modulino Pixels | LED0 PTP lock · LED1 sync fresh · LED2 frame-0 blink · LED3 phase error · LED5-7 on-air tally (raw / hevc / music) |
| Modulino Buttons (optional, not fitted) | A/B/C = IS-05 takes on the panel (`raw` / `hevc` / `music`); button LEDs mirror the tally. Hot-plug: the sketch re-probes every 2 s |
| LED matrix | frame-of-second sweep + seconds bar |

Into an audio/LTC input use roughly a 10k:1k divider and a DC-blocking capacitor.
Timecode is UTC (the grandmaster serves UTC-labelled time, `ptpTimescale=0`);
set `TZ_OFFSET_S` in `python/main.py` for local time.

## How it works

```
Pi GM (ptp4l -S) ─island─▶ UNO Q hub NIC ─▶ ptp4l (patched, RX-only SW timestamps)
                                            │ CLOCK_REALTIME, ±2 ms
                          python/main.py ◀──┘   Bridge.notify("time_sync", sec, ns, latency_us, locked, tz)  1/s
                                 │                           ▲ host/atoll-ptp-status.py  (lock state, :8765)
                                 ▼                           │ panel /take, /state       (buttons, tally)
                          sketch/sketch.ino  k_timer @100 µs → LTC / frame / PPS; Modulino Pixels + Buttons
```

**Linux side.** `host/atoll-follower.cfg` + `atoll-ptp.service` run a slave-only ptp4l
with software timestamps and `delay_mechanism NONE` (one-way sync). Two hardware facts
force that: the Arduino 8-in-1 hub's Realtek RTL8152 (10/100) exposes *receive-only*
software timestamps, which stock ptp4l refuses (`host/linuxptp-rx-only-timestamps.patch`
downgrades the check to a warning), and the hub's USB path adds a ~4 ms sawtooth of
receive jitter, so the servo gains are very slow. Net: SLAVE state, within about ±2 ms of
the grandmaster — a twentieth of a frame. NTP is disabled on the board (it fights ptp4l).

**MCU side.** Zephyr's kernel tick is 100 µs, which doesn't divide LTC's 250 µs
half-bit, so bit boundaries fall exactly every 500 µs (5 ticks) and the "1" mid-bit
transition at 200 µs — inside every decoder's 75 % threshold. Wall time is modelled as
`tick_count + wall_offset` in 100 µs units. The first sync steps it; afterwards each sync
leaves a residual that is slewed at most 4 ticks per frame (one bit shortened or
lengthened by 100 µs at bits 0/20/40/60), and an integrator learns the MCU oscillator's
rate error (~0.3 % slow on this board) and feeds it forward, so the residual sits at
±1 ms, limited by ptp4l's own wander. One-way Bridge latency (~2 ms) is measured with a
ping round-trip and compensated.

## Files

```
app.yaml, sketch/, python/     the App Lab app  →  ~/ArduinoApps/atoll-ltc on the UNO Q
sketch/sketch.yaml             must list Arduino_Modulino AND its 7 dependency libraries
                               by their library.properties names (two have spaces)
host/install.sh                one-shot Linux-side setup (run on the UNO Q)
host/atoll-follower.cfg        ptp4l follower config
host/atoll-ptp.service         ptp4l as a service (also pins CPU idle states: halves USB jitter spikes)
host/atoll-ptp-status.py/.service   ptp4l state as JSON on :8765 for the app container
host/atoll-ltc-autostart.service    starts the app after boot (app container has no restart policy)
host/linuxptp-rx-only-timestamps.patch
host/raw-offset.py             raw Sync-vs-receive offset series (diagnose NIC jitter)
host/usb-latency-test.sh       what does / doesn't affect USB NIC latency spikes
```

## Bring-up

1. Wire: PoE splitter USB-C → hub PD-in, splitter RJ45 → hub RJ45, hub → UNO Q. The
   island switch has no DHCP; `host/install.sh` gives the hub NIC a static island IP.
2. `host/install.sh` on the board, then copy the app and `arduino-app-cli app start ./atoll-ltc`.
3. Check: `sudo /usr/local/sbin/pmc -u -b 0 'GET PORT_DATA_SET'` → `SLAVE` (~2 min);
   `arduino-app-cli app logs ./atoll-ltc` prints a status line every 5 s:
   `ptp=SLAVE offset=… latency=… panel=up active=raw | mcu 16:30:12:00 locked=1 err=0 rate=29/s pixels=1 buttons=1 i2c=36,3e`
   (`err` in 100 µs ticks; `i2c` lists the Modulino addresses seen — Pixels 36, Buttons 3e).
