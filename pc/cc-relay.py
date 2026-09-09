#!/usr/bin/env python3
"""Atoll REAL closed-caption relay. Captures short chunks of the CURRENT Live-TV channel straight
from the HDHomeRun tuner (which still carries the broadcast's CEA-608, unlike the re-encoded island
feed), extracts the captions with ccextractor, and writes the current caption line to
~/atoll-run/cc-input -- which anc-send carries over ST 2110-40 and anc-recv renders on Program Out.

Chunked because this ccextractor build's live --stream mode is broken; the finite file->SRT path is
reliable. It overlaps capture with playback so captions flow continuously (~a chunk of latency).
Gated by ~/atoll-run/cc-source == "live" (else it idles and clears cc-input so anc-send falls back to
its synthetic samples). Follows ~/atoll-run/tv-channel. Env: ATOLL_RUN, HDHR_HOST.
"""
import os, subprocess, time, signal, sys

RUN = os.environ.get("ATOLL_RUN") or os.path.expanduser("~/atoll-run")
HDHR = os.environ.get("HDHR_HOST", "192.168.4.32")
CC_INPUT = os.path.join(RUN, "cc-input")
CC_SOURCE = os.path.join(RUN, "cc-source")
TV_CHANNEL = os.path.join(RUN, "tv-channel")
CHUNK = 8                      # seconds captured per pass
TS_A, TS_B = "/tmp/cc-relay-a.ts", "/tmp/cc-relay-b.ts"
SRT = "/tmp/cc-relay.srt"

def _read(p, d=""):
    try: return open(p).read().strip()
    except Exception: return d
def _live(): return _read(CC_SOURCE) == "live"
def _write(t):
    try:
        tmp = CC_INPUT + ".tmp"
        with open(tmp, "w") as f: f.write(t)
        os.replace(tmp, CC_INPUT)
    except OSError: pass

def _cleanup(*_):
    _write(""); sys.exit(0)
signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

def capture(ch, path):
    """Start a background curl of CHUNK seconds of the tuner into path. Returns the Popen."""
    return subprocess.Popen(["bash", "-c", f'curl -s --max-time {CHUNK} "http://{HDHR}:5004/auto/v{ch}" -o "{path}"'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def extract(path):
    """ccextractor path -> SRT -> ordered list of caption text lines."""
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
    print(f"cc-relay: real CC extractor (chunked). gate {CC_SOURCE}=live, tuner {HDHR}", flush=True)
    cur, nxt = TS_A, TS_B
    cap = None
    while True:
        if not _live():
            if cap:
                try: cap.kill()
                except Exception: pass
                cap = None
            _write(""); time.sleep(2); continue
        ch = _read(TV_CHANNEL, "24.1")
        if cap is None:                             # prime the first capture
            cap = capture(ch, cur)
        try: cap.wait(timeout=CHUNK + 6)            # finish the in-flight capture into `cur`
        except Exception:
            try: cap.kill()
            except Exception: pass
        texts = extract(cur)
        cap = capture(_read(TV_CHANNEL, ch), nxt)   # start next capture into `nxt` while we play `cur`
        if texts:
            per = max(1.4, CHUNK / len(texts))
            for t in texts:
                if not _live() or _read(TV_CHANNEL, ch) != ch:
                    break
                _write(t); time.sleep(per)
        else:
            time.sleep(0.5)
        cur, nxt = nxt, cur                         # swap; next loop reaps the capture now in `cur`

if __name__ == "__main__":
    main()
