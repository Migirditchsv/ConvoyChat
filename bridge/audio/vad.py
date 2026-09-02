"""Speech-presence classifiers with the SAFE-1 supervisor.

SafeVad is the only class the chain uses. It tries Silero (pysilero-vad,
bundled ONNX), falls back to EnergyVad, and on any runtime fault or per-frame
time overrun degrades one rung — ending at OPEN (transmit everything),
because a swallowed hazard call is worse than wind (plan SAFE-1).
"""
from __future__ import annotations
import time
import numpy as np

from common.audio import FS, dbfs


class FloorTracker:
    """Noise-floor estimate in dBFS. Fast-attack warm-up (the first
    WARMUP frames converge at 1 dB/frame) then slow rise / fast fall, so a
    bridge that boots into a -32 dBFS wind bed knows its floor in ~2 s
    instead of ~80 s (measured: the old 0.02 dB/frame tracker kept the
    energy VAD open on wind for its first 80 s — a stuck carrier on RF)."""
    WARMUP = 33            # frames (~2 s)

    def __init__(self, start_db: float = -60.0):
        self.floor_db = start_db
        self.n = 0

    def update(self, level_db: float) -> float:
        self.n += 1
        rise = 1.0 if self.n <= self.WARMUP else 0.05
        if level_db > self.floor_db:
            self.floor_db = min(self.floor_db + rise, level_db)
        else:
            self.floor_db = max(self.floor_db - 0.5, level_db)
        return self.floor_db


def spectral_features(frame: np.ndarray) -> tuple[float, float, float]:
    """(flatness, tilt_db, centroid_hz) over the HFP band 300-3400 Hz.
    Wind through a headset is spectrally sloped (low flatness, negative
    tilt); voiced speech is broader. Fixture-derived; see DR-013."""
    x = frame.astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2 + 1e-6
    freqs = np.fft.rfftfreq(len(x), 1 / FS)
    m = (freqs >= 300) & (freqs <= 3400)
    P = spec[m]
    flat = float(np.exp(np.mean(np.log(P))) / np.mean(P))
    lo = spec[(freqs >= 300) & (freqs < 1000)].sum()
    hi = spec[(freqs >= 1000) & (freqs <= 3400)].sum()
    tilt = float(10 * np.log10(hi / max(lo, 1e-6)))
    cen = float(np.sum(spec * freqs) / max(np.sum(spec), 1e-9))
    return flat, tilt, cen


class SpectralVad:
    """Second rung under Silero (DR-013): logistic model on [snr, flatness,
    tilt, centroid] fitted on the labeled fixtures with tools/fit_vad.py.
    Pure numpy, ~0.3 ms/frame. Weights below are the fitted values; refit
    from real captures when they exist (DR-008 revisit)."""
    name = "spectral"
    # fitted 2026-09-02 by tools/fit_vad.py on rev-5 fixtures (50/90 speech +
    # all wind, wind weighted 3x): wind-open 0.0/0.0/5.7 %, missed 0/0 % at 50/90
    MEAN = np.array([4.6609e+00, 4.3579e-03, -1.8231e+01, 4.7822e-01])
    STD = np.array([4.0593, 0.0126, 2.3993, 0.0556])
    W = np.array([0.326, 4.041, -0.2972, 0.4305])
    B = -2.0830

    def __init__(self):
        self.floor = FloorTracker()

    def features(self, frame: np.ndarray) -> np.ndarray:
        level = dbfs(frame)
        snr = level - self.floor.update(level)
        flat, tilt, cen = spectral_features(frame)
        return np.array([snr, flat, tilt, cen / 1000.0])

    SILENCE_DB = -70.0     # below this there is no signal to classify (digital
                           # silence is perfectly "flat" — it must not read as speech)

    def prob(self, frame: np.ndarray) -> float:
        feats = self.features(frame)
        if dbfs(frame) < self.SILENCE_DB:
            return 0.0
        z = (feats - self.MEAN) / self.STD
        return float(1 / (1 + np.exp(-(z @ self.W + self.B))))


class EnergyVad:
    """Last classifier rung: adaptive-floor energy + crude spectral centroid.
    Wind residue post-HPF is LF-heavy; speech pushes the centroid up."""
    name = "energy"

    def __init__(self):
        self.floor = FloorTracker()

    @property
    def floor_db(self) -> float:
        return self.floor.floor_db

    def prob(self, frame: np.ndarray) -> float:
        level = dbfs(frame)
        snr = level - self.floor.update(level)
        x = frame.astype(np.float64)
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1 / FS)
        centroid = float(np.sum(spec * freqs) / max(np.sum(spec), 1e-9))
        p_snr = 1 / (1 + np.exp(-(snr - 9) / 2.0))
        p_cen = 1 / (1 + np.exp(-(centroid - 500) / 150.0))
        return float(p_snr * p_cen)


class SileroVad:
    name = "silero"
    CHUNK = 512  # silero native chunk @16 kHz (32 ms)

    def __init__(self):
        from pysilero_vad import SileroVoiceActivityDetector
        self._det = SileroVoiceActivityDetector()
        self._buf = np.zeros(0, dtype=np.int16)

    def prob(self, frame: np.ndarray) -> float:
        self._buf = np.concatenate([self._buf, frame])
        p = 0.0
        while len(self._buf) >= self.CHUNK:
            chunk, self._buf = self._buf[:self.CHUNK], self._buf[self.CHUNK:]
            p = max(p, float(self._det(chunk.tobytes())))
        return p


class SafeVad:
    """SAFE-1 supervisor. Modes: silero -> spectral -> energy -> OPEN.

    Demotion policy: exceptions demote immediately; time-budget overruns
    demote only when SUSTAINED (>= OVERRUN_LIMIT consecutive frames). A
    single 55 ms call under a scheduler hiccup is not an emergency in a
    60 ms loop — a stalled classifier is. Frames that overrun still return
    their probability (the work was done by the time we measured it)."""
    BUDGET_S = 0.050
    OVERRUN_LIMIT = 3

    def __init__(self, prefer_silero: bool = True, on_degrade=None):
        self.on_degrade = on_degrade or (lambda mode: None)
        self._chain: list = []
        self._overruns = 0
        if prefer_silero:
            try:
                self._chain.append(SileroVad())
            except Exception:
                pass
        self._chain.append(SpectralVad())
        self._chain.append(EnergyVad())
        self.mode = self._chain[0].name

    def _demote(self) -> None:
        self._chain.pop(0)
        self._overruns = 0
        self.mode = self._chain[0].name if self._chain else "open"
        self.on_degrade(self.mode)

    def prob(self, frame: np.ndarray) -> float:
        while self._chain:
            v = self._chain[0]
            try:
                t0 = time.monotonic()
                p = v.prob(frame)
                if time.monotonic() - t0 > self.BUDGET_S:
                    self._overruns += 1
                    if self._overruns >= self.OVERRUN_LIMIT:
                        self._demote()      # sustained stall: next frame uses fallback
                else:
                    self._overruns = 0
                return p                    # this frame's work is already done
            except Exception:
                self._demote()
        return 1.0  # OPEN: fail-open per SAFE-1
