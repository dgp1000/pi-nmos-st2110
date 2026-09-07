#!/usr/bin/env python3
"""EBU R128 / ITU-R BS.1770-4 loudness measurement -- numpy only (no scipy, no ebur128 element).

The K-weighting (a high-shelf "pre-filter" + an RLB high-pass, both biquads at 48 kHz) is realised
ONCE as a combined FIR (the biquads' impulse response), then applied to each analysis window with
np.convolve -- vectorised and accurate for loudness, and it sidesteps per-sample Python IIR loops.

Loudness of a window (BS.1770):  L = -0.691 + 10*log10( sum_ch G_ch * mean(y_ch^2) )   [LUFS]
with G = 1.0 for L/R (stereo). Momentary = 400 ms, Short-term = 3 s. Integrated is the -70 LUFS
absolute-gated then -10 LU relative-gated mean of 400 ms blocks (75 % overlap), per EBU R128.
"""
import numpy as np

FS = 48000
# BS.1770-4 K-weighting biquad coefficients at 48 kHz (a0 = 1)
_PRE = dict(b=[1.53512485958697, -2.69169618940638, 1.19839281085285],
            a=[1.0, -1.69065929318241, 0.73248077421585])
_RLB = dict(b=[1.0, -2.0, 1.0],
            a=[1.0, -1.99004745483398, 0.99007225036621])

def _biquad(x, c):
    b0, b1, b2 = c["b"]; a1, a2 = c["a"][1], c["a"][2]
    y = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for n in range(len(x)):
        xn = x[n]
        yn = b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[n] = yn; x2 = x1; x1 = xn; y2 = y1; y1 = yn
    return y

def _kweight_fir(ntaps=2048):
    imp = np.zeros(ntaps); imp[0] = 1.0
    return _biquad(_biquad(imp, _PRE), _RLB)   # combined impulse response of the two biquads

_KFIR = _kweight_fir()

def _win_loudness(chans):
    """chans: list of 1-D float arrays (one per channel) for the window. Returns (LUFS, mean_square_sum)."""
    ms_sum = 0.0
    for x in chans:
        if len(x) < len(_KFIR):
            return (-np.inf, 0.0)
        y = np.convolve(x, _KFIR, mode="valid")   # K-weighted
        ms_sum += np.mean(y * y)                   # G = 1.0 for L/R
    if ms_sum <= 0:
        return (-np.inf, 0.0)
    return (-0.691 + 10.0 * np.log10(ms_sum), ms_sum)

class Loudness:
    """Feed interleaved float32 stereo (or mono) frames; read momentary/short-term/integrated LUFS."""
    def __init__(self, fs=FS, channels=2):
        self.fs = fs; self.ch = channels
        self.buf = np.zeros((0, channels), dtype=np.float32)   # rolling raw samples, last ~3 s
        self._blocks = []          # (mean_square_sum) of 400 ms gating blocks, for integrated
        self._since_block = 0      # samples since the last 100 ms gating step (75% overlap of 400ms)

    def add(self, frames):
        """frames: np.ndarray shape (n, channels) float in [-1,1]."""
        if frames.ndim == 1:
            frames = frames.reshape(-1, 1)
        self.buf = np.concatenate([self.buf, frames.astype(np.float32)])[-3 * self.fs - len(_KFIR):]
        # integrated: accumulate 400 ms blocks stepped every 100 ms (75% overlap)
        self._since_block += len(frames)
        step = self.fs // 10
        blk = int(0.4 * self.fs)
        while self._since_block >= step and len(self.buf) >= blk + len(_KFIR):
            self._since_block -= step
            w = self.buf[-(blk + len(_KFIR)):]
            _, ms = _win_loudness([w[:, c] for c in range(self.ch)])
            if ms > 0:
                self._blocks.append(ms)
            if len(self._blocks) > 36000:          # cap ~1h of blocks
                self._blocks = self._blocks[-36000:]

    def _window(self, secs):
        n = int(secs * self.fs) + len(_KFIR)
        if len(self.buf) < n:
            return -np.inf
        w = self.buf[-n:]
        return _win_loudness([w[:, c] for c in range(self.ch)])[0]

    def momentary(self):  return self._window(0.4)
    def short_term(self): return self._window(3.0)

    def integrated(self):
        if not self._blocks:
            return -np.inf
        ms = np.array(self._blocks)
        loud = -0.691 + 10.0 * np.log10(ms)               # per-block loudness
        keep = ms[loud > -70.0]                            # absolute gate
        if len(keep) == 0:
            return -np.inf
        rel = (-0.691 + 10.0 * np.log10(np.mean(keep))) - 10.0   # relative gate threshold
        keep2 = keep[(-0.691 + 10.0 * np.log10(keep)) > rel]
        if len(keep2) == 0:
            return -np.inf
        return -0.691 + 10.0 * np.log10(np.mean(keep2))

if __name__ == "__main__":
    # self-test: a stereo 1 kHz sine at -20 dBFS should read ~ -20 to -21 LUFS; halving amplitude -> -6 LU
    def tone(dbfs, secs=4.0, f=1000.0):
        t = np.arange(int(secs * FS)) / FS
        a = 10 ** (dbfs / 20.0)
        s = (a * np.sin(2 * np.pi * f * t)).astype(np.float32)
        return np.stack([s, s], axis=1)
    for db in (-20.0, -26.0, -23.0):
        m = Loudness()
        sig = tone(db)
        for i in range(0, len(sig), 4800):
            m.add(sig[i:i + 4800])
        print(f"  1kHz stereo @ {db:+.0f} dBFS -> momentary {m.momentary():.2f}  short-term {m.short_term():.2f}  integrated {m.integrated():.2f} LUFS")
    # silence -> -inf
    ms = Loudness(); ms.add(np.zeros((FS * 2, 2), dtype=np.float32))
    print(f"  silence -> momentary {ms.momentary():.1f} LUFS (expect -inf)")
