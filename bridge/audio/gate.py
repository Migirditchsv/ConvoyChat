"""Speech gate: pre-roll + hysteresis + speed-indexed thresholds (plan B-2).

The single highest-value stage: wind never crosses the air (INV-5/8), and
onsets are protected by a pre-roll ring so the first word of a hazard call
survives the gate opening. Metrics S-03 (missed speech) / S-04 (false
openings) pin its behavior.
"""
from __future__ import annotations
from collections import deque
import numpy as np

from common.audio import FRAME_MS

# speed (km/h) -> silero-prob threshold; energy mode uses +0.10
THRESHOLDS = [(0, 0.45), (80, 0.55), (110, 0.62)]


class SpeechGate:
    def __init__(self, preroll_ms: int = 600, hold_ms: int = 600,
                 open_frames: int = 1, mode: str = "silero"):
        self._pre = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._hold_frames = max(1, hold_ms // FRAME_MS)
        self._need_open = open_frames
        self._mode = mode
        self._above = 0
        self._hold = 0
        self.is_open = False

    def threshold(self, speed_kmh: float, mode: str | None = None) -> float:
        thr = THRESHOLDS[0][1]
        for s, t in THRESHOLDS:
            if speed_kmh >= s:
                thr = t
        return thr + (0.10 if (mode or self._mode) == "energy" else 0.0)

    def keep_threshold(self, mode: str | None = None) -> float:
        """Dual-threshold hangover: once open, stay while prob clears a low
        bar. Wind p99 sits near 0.11 on fixtures, so 0.15 keeps phrases
        contiguous at -6 dB SNR without holding the gate for gusts."""
        return 0.15 if (mode or self._mode) != "energy" else 0.35

    def process(self, frame: np.ndarray, prob: float, speed_kmh: float = 0.0,
                mode: str | None = None) -> tuple[list[np.ndarray], bool, bool]:
        """-> (frames_to_transmit, is_open, just_opened)."""
        thr = self.threshold(speed_kmh, mode)
        if self.is_open and prob >= self.keep_threshold(mode):
            self._hold = self._hold_frames          # refresh hold while speech continues
        just_opened = False
        if prob >= thr:
            self._above += 1
            if not self.is_open and self._above >= self._need_open:
                self.is_open = True
                just_opened = True
            self._hold = self._hold_frames
        else:
            self._above = 0
            if self.is_open:
                self._hold -= 1
                if self._hold <= 0:
                    self.is_open = False
        if self.is_open:
            out = list(self._pre) + [frame] if just_opened else [frame]
            self._pre.clear()
            return out, True, just_opened
        self._pre.append(frame)
        return [], False, False
