#!/usr/bin/env bash
# Bring up the Atoll media pipeline on monitor 2: the three island senders + the multiview
# renderer. Everything runs from a Linux-side copy of the pc/ scripts (~/atoll-run), because
# the /mnt/c 9p cache can hand a stale/half-written script to fast reads -- that silently broke
# the bbb sender mid-session. Idempotent: stops any prior pipeline, then relaunches. Run as the
# user (dgper) for GPU/NVENC. The panel (:8096) should already be up (restore.ps1 step 4).
#
#   bbb  -> 239.10.10.65:5010   ("PC HEVC 4K")
#   home -> 239.10.10.22:5008   ("Home videos")
#   music-> 239.10.10.30:5012   (Mac "Now Playing" bridge, or a placeholder card if Mac is down)
set -uo pipefail
SRC=/mnt/c/Users/dgper/pi-nmos-st2110/pc
RUN=/home/dgper/atoll-run
LOGS="$RUN/logs"
PANEL=http://localhost:8096
MACBASE=http://192.168.6.159:8008
HOMEDIR="/mnt/c/Users/dgper/OneDrive/Music/Home Videos"
BBB=/home/dgper/jxs-media/bbb-4k.mp4

mkdir -p "$RUN" "$LOGS"
cp "$SRC"/media-send.sh "$SRC"/music-send.sh "$SRC"/music-placeholder.sh "$SRC"/output-render.sh \
   "$SRC"/move-window-screen.ps1 "$SRC"/hevc-stream-env.sh "$SRC"/jxs-stream-env.sh "$RUN"/

# Stop any prior pipeline. Each pkill is bracketed and there are NO bare matching literals
# elsewhere on these lines, so pkill can never match (and kill) this script's own process.
pkill -f "[m]edia-send.sh"; pkill -f "[m]usic-send.sh"; pkill -f "[m]usic-placeholder.sh"; pkill -f "[o]utput-render.sh"
sleep 1
pkill -f "[g]st-launch.*udpsink host=239.10.10"; pkill -f "[s]ouphttpsrc location=http://192.168.6.159"; pkill -f "[w]aylandsink"
sleep 2

# Senders (HEVC video + audio muxed into the TS). Logs go to a user-owned dir (not /tmp, which
# may hold root-owned leftovers that block the redirect).
setsid bash "$RUN"/media-send.sh --hevc "$BBB"     >"$LOGS"/media-hevc.log 2>&1 </dev/null &
setsid bash "$RUN"/media-send.sh --jxs  "$HOMEDIR" >"$LOGS"/media-jxs.log  2>&1 </dev/null &
# Music tile: real Mac bridge if the server is reachable (probe /state -- a quick JSON, NOT the
# never-ending .ts), else the "connecting" placeholder so 5012 is always fed (the compositor
# stalls on a tile whose pad never gets caps).
if curl -s --max-time 4 -o /dev/null "$MACBASE/state"; then
  setsid bash "$RUN"/music-send.sh        >"$LOGS"/music-send.log 2>&1 </dev/null &
  echo "music: Mac bridge ($MACBASE)"
else
  setsid bash "$RUN"/music-placeholder.sh >"$LOGS"/music-ph.log   2>&1 </dev/null &
  echo "music: Mac unreachable -> placeholder card"
fi

# Let the encoders warm up + start feeding before the renderer joins their groups.
sleep 14
curl -s "$PANEL/layout?mode=multi" >/dev/null 2>&1 || true
setsid bash "$RUN"/output-render.sh 2 >"$LOGS"/output-render.log 2>&1 </dev/null &
echo "atoll media pipeline up: senders + monitor-2 renderer (from $RUN); logs in $LOGS"
