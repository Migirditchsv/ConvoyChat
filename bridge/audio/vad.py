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


class EnergyVad:
    """Stdlib fallback: adaptive-floor energy + crude spectral centroid.
    Wind residue post-HPF is LF-heavy; speech pushes the centroid up."""
    name = "energy"

    def __init__(self):
        self.floor_db = -60.0

    def prob(self, frame: np.ndarray) -> float:
        level = dbfs(frame)
        # slow-rising / fast-falling noise floor tracker
        self.floor_db = min(self.floor_db + 0.02, level) if level > self.floor_db \
            else max(self.floor_db - 0.5, level)
        snr = level - self.floor_db
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
    """SAFE-1 supervisor. Modes: silero -> energy -> OPEN.

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
