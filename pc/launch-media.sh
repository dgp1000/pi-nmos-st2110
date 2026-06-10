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
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/atoll.conf"
SRC="$ATOLL_SRC"
RUN="$ATOLL_RUN"
LOGS="$RUN/logs"
PANEL="http://localhost:$PANEL_PORT"
MACBASE="http://$MAC_MUSIC_HOST:$MAC_MUSIC_PORT"
MUSICROOT="$MUSIC_ROOT"
PLAYLIST="$HOME_PLAYLIST"
BBB="$BBB_FILE"
# REELS_LOOP comes from atoll.conf

mkdir -p "$RUN" "$LOGS"
cp "$SRC"/atoll.conf "$SRC"/atoll_config.py "$SRC"/media-send.sh "$SRC"/music-send.sh "$SRC"/music-placeholder.sh "$SRC"/output-render.sh \
   "$SRC"/move-window-screen.ps1 "$SRC"/reels-nmos.py "$RUN"/

# Build the home-video playlist: symlink every video under the Music tree into one folder (the
# sender globs a single dir, which sidesteps spaces/apostrophes in filenames). -size -200M skips
# the big "making of" doc. Rebuilt each run so newly-added videos get picked up automatically.
mkdir -p "$PLAYLIST"; rm -f "$PLAYLIST"/*
find "$MUSICROOT" -type f -size -200M \( -iname "*.mpg" -o -iname "*.m4v" -o -iname "*.mp4" -o -iname "*.mov" -o -iname "*.avi" -o -iname "*.mkv" \) -exec ln -sf {} "$PLAYLIST"/ \; 2>/dev/null

# Stop any prior pipeline. Each pkill is bracketed and there are NO bare matching literals
# elsewhere on these lines, so pkill can never match (and kill) this script's own process.
pkill -f "[m]edia-send.sh"; pkill -f "[m]usic-send.sh"; pkill -f "[m]usic-placeholder.sh"; pkill -f "[o]utput-render.sh"
sleep 1
pkill -f "[g]st-launch.*udpsink host=239.10.10"; pkill -f "[s]ouphttpsrc location=http://$MAC_MUSIC_HOST"; pkill -f "[w]aylandsink"
sleep 2

# Senders (HEVC video + audio muxed into the TS). Logs go to a user-owned dir (not /tmp, which
# may hold root-owned leftovers that block the redirect).
setsid bash "$RUN"/media-send.sh --hevc "$BBB"     >"$LOGS"/media-hevc.log 2>&1 </dev/null &
setsid bash "$RUN"/media-send.sh --jxs   "$PLAYLIST"  >"$LOGS"/media-jxs.log   2>&1 </dev/null &
setsid bash "$RUN"/media-send.sh --reels "$REELS_LOOP" >"$LOGS"/media-reels.log 2>&1 </dev/null &
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

# Register Test Reels as a discoverable NMOS sender (heartbeats + serves its SDP on :8097).
setsid python3 "$RUN"/reels-nmos.py >"$LOGS"/reels-nmos.log 2>&1 </dev/null &

# Let the encoders warm up + start feeding before the renderer joins their groups.
sleep 14
curl -s "$PANEL/layout?mode=multi" >/dev/null 2>&1 || true
setsid bash "$RUN"/output-render.sh 2 >"$LOGS"/output-render.log 2>&1 </dev/null &
echo "atoll media pipeline up: senders + monitor-2 renderer (from $RUN); logs in $LOGS"
