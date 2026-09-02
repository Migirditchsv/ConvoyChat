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

# Speed-indexed gate profiles, tuned on labeled fixtures (S-03/S-04) and
# re-derived from measured separability: at 120 km/h (-6 dB SNR) wind shows
# ZERO consecutive frames above 0.35 in 5 min while speech shows runs of
# them — so high speed opens LOWER but requires 2-frame confirmation, and
# holds at a lower keep threshold to bridge soft mid-utterance stretches.
# (speed_kmh, open_thr, keep_thr, consecutive_frames_to_open)
PROFILES = [(0, 0.45, 0.15, 1), (80, 0.55, 0.15, 1), (110, 0.35, 0.10, 2)]


class SpeechGate:
    def __init__(self, preroll_ms: int = 900, hold_ms: int = 600,
                 mode: str = "silero"):
        self._pre = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._hold_frames = max(1, hold_ms // FRAME_MS)
        self._mode = mode
        self._above = 0
        self._hold = 0
        self.is_open = False
        self.force_open = False   # test/PTT hook: bypass classification

    def profile(self, speed_kmh: float, mode: str | None = None):
        """-> (open_thr, keep_thr, need_consecutive) for this speed."""
        open_t, keep_t, need = PROFILES[0][1:]
        for sp, o, k, n in PROFILES:
            if speed_kmh >= sp:
                open_t, keep_t, need = o, k, n
        if (mode or self._mode) == "energy":
            open_t, keep_t = open_t + 0.10, keep_t + 0.20
        elif (mode or self._mode) == "spectral":
            open_t, keep_t = max(open_t, 0.55), keep_t + 0.10   # fitted model: firmer open
        return open_t, keep_t, need

    def process(self, frame: np.ndarray, prob: float, speed_kmh: float = 0.0,
                mode: str | None = None) -> tuple[list[np.ndarray], bool, bool]:
        """-> (frames_to_transmit, is_open, just_opened)."""
        open_t, keep_t, need = self.profile(speed_kmh, mode)
        if self.force_open:
            prob, open_t = 1.0, 0.0
        if self.is_open and prob >= keep_t:
            self._hold = self._hold_frames          # refresh hold while speech continues
        just_opened = False
        if prob >= open_t:
            self._above += 1
            if not self.is_open and self._above >= need:
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
