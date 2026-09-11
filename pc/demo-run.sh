#!/usr/bin/env bash
# Mirrors monitor-web.py runDemo() server-side so the guided tour plays on Monitor 2.
P=http://localhost:8096
cap(){ curl -s -G --data-urlencode "text=$1" "$P/demo/caption" >/dev/null 2>&1; }
go(){ curl -s "$P$1" >/dev/null 2>&1; }
reset(){ go "/fec/set?loss=0"; go "/fec/set?enable=1"; go "/sps/set?path=a&up=1"; go "/sps/set?path=b&up=1"; go "/programout/route?essence=none"; go "/is11/unconstrain"; go "/is11/edid?load=0"; }
echo "$(date +%T) demo start"; reset
cap "Atoll: a self-contained NMOS ST 2110 broadcast rig. This is the multiviewer - four live flows at once, each a real NMOS sender."; go "/layout?mode=wall"; sleep 9
cap "Taking a source is a real IS-05 operation. The red tally border and ON-AIR flag follow it live over IS-07."; go "/take?src=hevc"; sleep 6
cap "Take another source - the tally moves with it."; go "/take?src=jxs"; sleep 6
cap "Program Out: a software NMOS receiver you route any flow to over IS-05."; go "/layout?mode=program"; go "/programout/route?essence=hevc"; sleep 8
cap "Route a different flow over the same IS-05 connection - Home videos now."; go "/programout/route?essence=jxs"; sleep 7
cap "IS-05 activations can be scheduled - arming a take for +5s; the connection fires on the clock."; go "/programout/route?essence=hevc&secs=5"; sleep 9
cap "IS-08 audio channel mapping - swapping the music's left and right channels live."; go "/take?src=music"; go "/layout?mode=single"; go "/audiomap/set?preset=swap"; sleep 8
cap "...and back to straight stereo."; go "/audiomap/set?preset=stereo"; sleep 6
cap "Live TV: changing channel opens the new channel on a second tuner first, then cuts - no black frame."; go "/layout?mode=single"; go "/take?src=hevc"; sleep 5
CH=$(curl -s "$P/tv/lineup" | python3 -c 'import sys,json;d=json.load(sys.stdin);f=(d.get("favorites") or d.get("channels") or []);print(" ".join(str(c["num"]) for c in f[:2]))' 2>/dev/null)
set -- $CH
if [ -n "$1" ]; then cap "Changing channel..."; go "/tv/set?ch=$1"; sleep 6; fi
if [ -n "$2" ]; then cap "...and again - seamless."; go "/tv/set?ch=$2"; sleep 6; fi
cap "ST 2022-1 FEC. Fullscreen the protected feed."; go "/layout?mode=single"; go "/take?src=fec"; sleep 5
cap "Inject 5% packet loss - FEC reconstructs every lost packet, the picture stays clean."; go "/fec/set?loss=0.05"; sleep 8
cap "Now switch FEC OFF at the same 5% loss - watch it tear."; go "/fec/set?enable=0"; sleep 8
cap "FEC back ON - clean again. Loss removed."; go "/fec/set?enable=1"; sleep 3; go "/fec/set?loss=0"; sleep 5
cap "ST 2022-7 seamless protection: the same essence sent on two network paths."; go "/take?src=sps"; sleep 6
cap "Pull one path - the other carries it, hitless."; go "/sps/set?path=a&up=0"; sleep 8
cap "Restore the path. Both live again."; go "/sps/set?path=a&up=1"; sleep 5
cap "IS-11 stream compatibility - constraining a sender retunes its flow to stay compatible (25->50 fps)."; go "/is11/constrain?num=50&den=1"; sleep 8
cap "Clear it - the flow returns to native rate. IS-11 also carries EDID and passes the AMWA IS-11-01 suite."; go "/is11/unconstrain"; sleep 7
cap "IS-12 device control - a WebSocket carries the MS-05 object model; reading the device model live (root block, device + class managers, 58 datatypes)."; go "/is12/state"; sleep 8
cap "And IS-12 drives the rig: setting rigControl.program over the control WebSocket cuts the program bus."; go "/layout?mode=single"; go "/is12/drive?src=jxs"; sleep 7
cap "Demo complete - everything you saw runs live and to spec."; sleep 6
reset; go "/layout?mode=wall"; cap ""
echo "$(date +%T) demo complete"
