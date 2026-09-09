#!/usr/bin/env python3
"""Atoll REAL closed-caption relay. Captures short chunks of the CURRENT Live-TV channel straight
from the HDHomeRun tuner (which still carries the broadcast's CEA-608, unlike the re-encoded island
feed), extracts the captions with ccextractor, and writes the current caption line to
~/atoll-run/cc-input -- which anc-send carries over ST 2110-40 and anc-recv renders on Program Out.

Chunked because this ccextractor build's live --stream mode is broken; the finite file->SRT path is
reliable. It overlaps capture with playback so captions flow continuously.

A tunable CAPTION DELAY (~/atoll-run/caption-delay-ms, ms) shifts captions LATER to line them up
with the heavily-buffered Live-TV video path (captions otherwise run ahead of the picture). A
background writer thread releases each paced caption `delay` ms after it is queued, so the delay is
applied without stalling capture. Gated by ~/atoll-run/cc-source == "live"; follows ~/atoll-run/
tv-channel. Env: ATOLL_RUN, HDHR_HOST.
"""
import os, subprocess, time, signal, sys, threading, collections

RUN = os.environ.get("ATOLL_RUN") or os.path.expanduser("~/atoll-run")
HDHR = os.environ.get("HDHR_HOST", "192.168.4.32")
CC_INPUT = os.path.join(RUN, "cc-input")
CC_SOURCE = os.path.join(RUN, "cc-source")
TV_CHANNEL = os.path.join(RUN, "tv-channel")
CAPTION_DELAY = os.path.join(RUN, "caption-delay-ms")
CHUNK = 8                      # seconds captured per pass
TS_A, TS_B = "/tmp/cc-relay-a.ts", "/tmp/cc-relay-b.ts"
SRT = "/tmp/cc-relay.srt"

def _read(p, d=""):
    try: return open(p).read().strip()
    except Exception: return d
def _live(): return _read(CC_SOURCE) == "live"
def _delay():
    try: return max(0.0, min(20.0, float(_read(CAPTION_DELAY, "0")) / 1000.0))
    except Exception: return 0.0
def _write(t):
    try:
        tmp = CC_INPUT + ".tmp"
        with open(tmp, "w") as f: f.write(t)
        os.replace(tmp, CC_INPUT)
    except OSError: pass

# --- delay line: captions are queued (release_time, text); a writer thread emits them on time ---
_q = collections.deque()
_qlock = threading.Lock()
_last = {"txt": None}
def _enqueue(text):
    with _qlock:
        _q.append((time.time() + _delay(), text))
def _flush():
    with _qlock:
        _q.clear()
def _writer():
    while True:
        emit = None
        now = time.time()
        with _qlock:
            while _q and _q[0][0] <= now:      # release all that are due; keep the newest
                emit = _q.popleft()[1]
        if emit is not None and emit != _last["txt"]:
            _write(emit); _last["txt"] = emit
        time.sleep(0.1)

def _cleanup(*_):
    _flush(); _write(""); sys.exit(0)
signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

def capture(ch, path):
    return subprocess.Popen(["bash", "-c", f'curl -s --max-time {CHUNK} "http://{HDHR}:5004/auto/v{ch}" -o "{path}"'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def extract(path):
    try:
        open(SRT, "w").close()
        subprocess.run(["ccextractor", path, "-o", SRT, "--no-fontcolor"],
                       timeout=25, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    texts, block = [], []
    try:
        for line in open(SRT, encoding="utf-8", errors="replace"):
            s = line.rstrip("\n")
            if s.strip() == "":
                if len(block) >= 3:
                    txt = " ".join(x.strip() for x in block[2:]).strip()
                    if txt: texts.append(txt)
                block = []
            else:
                block.append(s)
        if len(block) >= 3:
            txt = " ".join(x.strip() for x in block[2:]).strip()
            if txt: texts.append(txt)
    except Exception:
        pass
    return texts

def main():
    print(f"cc-relay: real CC extractor (chunked, delay-line). gate {CC_SOURCE}=live, tuner {HDHR}", flush=True)
    threading.Thread(target=_writer, daemon=True).start()
    cur, nxt = TS_A, TS_B
    cap = None
    while True:
        if not _live():
            if cap:
                try: cap.kill()
                except Exception: pass
                cap = None
            _flush(); _write(""); _last["txt"] = None; time.sleep(2); continue
        ch = _read(TV_CHANNEL, "24.1")
        if cap is None:
            cap = capture(ch, cur)
        try: cap.wait(timeout=CHUNK + 6)
        except Exception:
            try: cap.kill()
            except Exception: pass
        texts = extract(cur)
        cap = capture(_read(TV_CHANNEL, ch), nxt)       # capture next while we pace out this chunk
        if texts:
            per = max(1.4, CHUNK / len(texts))
            for t in texts:
                if not _live():
                    break
                if _read(TV_CHANNEL, ch) != ch:         # channel changed -> drop queued old-channel CC
                    _flush(); break
                _enqueue(t); time.sleep(per)
        else:
            time.sleep(0.5)
        cur, nxt = nxt, cur

if __name__ == "__main__":
    main()
