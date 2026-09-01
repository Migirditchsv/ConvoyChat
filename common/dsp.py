"""DSP primitives: RBJ biquads, AGC, Goertzel, chirp latency probe.

scipy is used when present (dev/base); a per-sample fallback keeps the bridge
dependency-light (DR-001). All filters are stateful across 60 ms frames.
"""
from __future__ import annotations
import math
import numpy as np

try:
    from scipy.signal import lfilter as _lfilter  # type: ignore
    _HAVE_SCIPY = True
except Exception:                                  # pragma: no cover
    _HAVE_SCIPY = False


def _rbj(kind: str, fc: float, fs: float, q: float = 0.7071):
    w0 = 2 * math.pi * fc / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "hp":
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
    elif kind == "lp":
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
    else:
        raise ValueError(kind)
    a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    return (np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0]))


class Biquad:
    def __init__(self, kind: str, fc: float, fs: float, q: float = 0.7071):
        self.kind, self.fs, self.q = kind, fs, q
        self.set_fc(fc)
        self._zi = np.zeros(2)

    def set_fc(self, fc: float) -> None:
        self.fc = fc
        self.b, self.a = _rbj(self.kind, fc, self.fs, self.q)

    def process(self, x: np.ndarray) -> np.ndarray:
        xf = x.astype(np.float64)
        if _HAVE_SCIPY:
            y, self._zi = _lfilter(self.b, self.a, xf, zi=self._zi)
        else:
            y = np.empty_like(xf)
            z1, z2 = self._zi
            b0, b1, b2 = self.b
            _, a1, a2 = self.a
            for i, xi in enumerate(xf):
                yi = b0 * xi + z1
                z1 = b1 * xi - a1 * yi + z2
                z2 = b2 * xi - a2 * yi
                y[i] = yi
            self._zi = np.array([z1, z2])
        return np.clip(y, -32768, 32767).astype(np.int16)


class SpeedHPF:
    """Speed-scheduled high-pass (plan B-2): free wind estimate from GPS."""
    TABLE = [(0, 100.0), (40, 180.0), (80, 250.0)]

    def __init__(self, fs: float):
        self.bq = Biquad("hp", 100.0, fs)

    def set_speed(self, kmh: float) -> None:
        fc = self.TABLE[0][1]
        for thr, f in self.TABLE:
            if kmh >= thr:
                fc = f
        if abs(fc - self.bq.fc) > 1:
            self.bq.set_fc(fc)

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.bq.process(x)


class Agc:
    """Slow AGC after the gate (plan B-2): target level, bounded gain, limiter."""
    def __init__(self, target_db: float = -20.0, max_gain_db: float = 18.0,
                 rate_db_per_s: float = 6.0, fs: int = 16000, frame: int = 960):
        self.target, self.max_gain = target_db, max_gain_db
        self.step = rate_db_per_s * frame / fs
        self.gain_db = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        from common.audio import dbfs
        level = dbfs(x)
        if level > -60:
            err = self.target - (level + self.gain_db)
            self.gain_db += float(np.clip(err, -self.step, self.step))
            self.gain_db = float(np.clip(self.gain_db, -6.0, self.max_gain))
        y = x.astype(np.float64) * (10 ** (self.gain_db / 20))
        return np.clip(y, -32768, 32767).astype(np.int16)


def goertzel_power(x: np.ndarray, freq: float, fs: float) -> float:
    """Normalized power of one tone in a frame (for duck/N-1 probes)."""
    n = len(x)
    k = int(0.5 + n * freq / fs)
    w = 2 * math.pi * k / n
    cw = 2 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for xi in x.astype(np.float64) / 32768.0:
        s0 = xi + cw * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - cw * s1 * s2
    return p / (n * n)


def band_db(x: np.ndarray, f_lo: float, f_hi: float, fs: int = 16000) -> float:
    """Power in [f_lo,f_hi] via rFFT, dB re full scale. Probe metric: unlike a
    pure tone through a speech codec (which SILK mangles), band power of
    noise-band probes survives Opus voice mode faithfully."""
    xf = x.astype(np.float64) / 32768.0
    spec = np.abs(np.fft.rfft(xf * np.hanning(len(xf)))) ** 2
    freqs = np.fft.rfftfreq(len(xf), 1 / fs)
    m = (freqs >= f_lo) & (freqs <= f_hi)
    return 10 * np.log10(max(spec[m].sum() / len(xf) ** 2, 1e-14))


def noise_band(f_lo: float, f_hi: float, dur_s: float, fs: int = 16000,
               level_db: float = -20.0, seed: int = 3) -> np.ndarray:
    """Gaussian noise band-limited by FFT mask — the probe source signal."""
    rng = np.random.default_rng(seed)
    n = int(dur_s * fs)
    spec = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / fs)
    spec[(freqs < f_lo) | (freqs > f_hi)] = 0
    x = np.fft.irfft(spec, n)
    x = x / max(np.abs(x).max(), 1e-9) * 32767 * (10 ** (level_db / 20)) * 3
    return np.clip(x, -32768, 32767).astype(np.int16)


def tone(freq: float, dur_s: float, fs: int = 16000, level_db: float = -20.0) -> np.ndarray:
    t = np.arange(int(dur_s * fs)) / fs
    x = np.sin(2 * np.pi * freq * t) * 32767 * (10 ** (level_db / 20))
    return x.astype(np.int16)


def chirp(fs: int = 16000, dur_s: float = 0.5, f0: float = 300, f1: float = 3000) -> np.ndarray:
    t = np.arange(int(dur_s * fs)) / fs
    ph = 2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * dur_s))
    return (np.sin(ph) * 12000).astype(np.int16)


def find_delay_s(recorded: np.ndarray, reference: np.ndarray, fs: int = 16000) -> float:
    """Matched-filter delay estimate (S-05 latency probe)."""
    a = recorded.astype(np.float64)
    b = reference.astype(np.float64)
    n = int(2 ** np.ceil(np.log2(len(a) + len(b))))
    A = np.fft.rfft(a, n)
    B = np.fft.rfft(b, n)
    xc = np.fft.irfft(A * np.conj(B), n)
    lag = int(np.argmax(xc[: len(a)]))
    return lag / fs


def rms_envelope_db(x: np.ndarray, fs: int = 16000, win_ms: float = 50.0) -> np.ndarray:
    w = int(fs * win_ms / 1000)
    n = len(x) // w
    out = np.empty(n)
    xf = x.astype(np.float64)
    for i in range(n):
        seg = xf[i * w:(i + 1) * w]
        out[i] = 20 * np.log10(max(np.sqrt(np.mean(seg ** 2)), 1e-9) / 32768.0)
    return out
