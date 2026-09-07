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
        self._st_hist = []         # short-term (3 s) loudness samples, ~1 Hz, for LRA (EBU 3342)
        self._since_st = 0
        self._peak = 0.0           # max |sample| seen (sample peak, dBFS)
        self._tpeak = 0.0          # max 4x-oversampled |sample| (true-peak estimate, dBTP)

    def add(self, frames):
        """frames: np.ndarray shape (n, channels) float in [-1,1]."""
        if frames.ndim == 1:
            frames = frames.reshape(-1, 1)
        # peak + 4x-oversampled true-peak estimate (per channel, over this block)
        af = np.abs(frames)
        if af.size:
            self._peak = max(self._peak, float(af.max()))
            for c in range(frames.shape[1]):
                x = frames[:, c]
                if len(x) > 1:
                    xi = np.interp(np.arange(0, len(x) - 1, 0.25), np.arange(len(x)), x)
                    self._tpeak = max(self._tpeak, float(np.abs(xi).max()))
        self.buf = np.concatenate([self.buf, frames.astype(np.float32)])[-3 * self.fs - len(_KFIR):]
        # short-term history for LRA: sample the 3 s loudness ~once/sec
        self._since_st += len(frames)
        if self._since_st >= self.fs:
            self._since_st = 0
            stv = self.short_term()
            if np.isfinite(stv):
                self._st_hist.append(stv)
                if len(self._st_hist) > 7200:
                    self._st_hist = self._st_hist[-7200:]
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

    def peak_dbfs(self):
        return 20.0 * np.log10(self._peak) if self._peak > 0 else -np.inf
    def true_peak_dbtp(self):
        return 20.0 * np.log10(self._tpeak) if self._tpeak > 0 else -np.inf

    def lra(self):
        """EBU Tech 3342 Loudness Range from the short-term distribution (abs -70, rel -20 LU gates)."""
        if len(self._st_hist) < 2:
            return 0.0
        st = np.array(self._st_hist)
        st = st[st > -70.0]                       # absolute gate
        if len(st) < 2:
            return 0.0
        pmean = 10.0 * np.log10(np.mean(10.0 ** (st / 10.0)))   # power mean of the distribution
        st = st[st > pmean - 20.0]                # relative gate, -20 LU
        if len(st) < 2:
            return 0.0
        return float(np.percentile(st, 95) - np.percentile(st, 10))


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
        print(f"  1kHz stereo @ {db:+.0f} dBFS -> M {m.momentary():.2f}  S {m.short_term():.2f}  I {m.integrated():.2f} LUFS  peak {m.peak_dbfs():.2f} dBFS  truepeak {m.true_peak_dbtp():.2f} dBTP  LRA {m.lra():.2f} LU")
    # silence -> -inf
    ms = Loudness(); ms.add(np.zeros((FS * 2, 2), dtype=np.float32))
    print(f"  silence -> momentary {ms.momentary():.1f} LUFS (expect -inf)")
