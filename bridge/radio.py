"""Bike-side RF failover: when the base is out of reach, the helmet's
transport becomes the wired HT (plan fallback layer, DR-011).

    engine.radio = RadioFailover(link, rig_source, rig_sink, mode="auto")

Each engine tick hands over the gated PCM the chain would have sent as RTP;
in `auto` mode it goes to the rig only while `link_up` is False, in
`always` mode as well as over Wi-Fi (a licensed rider relaying the room),
in `off` mode never. Received RF audio is returned to the engine, which
mixes it into the helmet before volume. Everything legal (ID, time-out,
busy lockout) lives in common.radio.RadioLink."""
from __future__ import annotations
from collections import deque
import numpy as np

from common.audio import FRAME
from common.radio import RadioLink

MODES = ("auto", "always", "off")
PREROLL_MAX = 15        # frames queued from a gate opening (900 ms pre-roll burst)


class RadioFailover:
    def __init__(self, link: RadioLink, rig_source, rig_sink, mode: str = "auto"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.link = link
        self.rig_source = rig_source
        self.rig_sink = rig_sink
        self.mode = mode
        self._txq: deque[np.ndarray] = deque()
        self.active = False
        self.activations = 0
        self.dropped = 0
        self.held_open = 0      # frames NOT sent because the VAD had failed open (no PTT)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode

    def on_tick(self, tx_frames: list[np.ndarray], link_up: bool,
                vad_mode: str = "silero", ptt: bool = False) -> np.ndarray | None:
        active = self.mode == "always" or (self.mode == "auto" and not link_up)
        if active and not self.active:
            self.activations += 1
        self.active = active
        rx = self.rig_source.read()
        if not active:
            self._txq.clear()
            self.link.process(None, rx)                 # keep carrier state fresh, never key
            return None
        if vad_mode == "open" and not ptt:
            # SAFE-1 fails OPEN over Wi-Fi (the mixer absorbs wind); on a shared
            # channel that would be a stuck carrier. With no classifier left,
            # only the rider's thumb (PTT) may key the rig.
            self.held_open += len(tx_frames)
            tx_frames = []
            self._txq.clear()
        for f in tx_frames:                             # a gate opening flushes a burst:
            if len(self._txq) >= PREROLL_MAX:           # serialise it, one frame per tick
                self._txq.popleft(); self.dropped += 1
            self._txq.append(f)
        tx = self._txq.popleft() if self._txq else None
        out, ear = self.link.process(tx, rx)
        if out is not None:
            self.rig_sink.write(out)
        return ear

    def stats(self) -> dict:
        return {"mode": self.mode, "active": self.active, "activations": self.activations,
                "queued": len(self._txq), "dropped": self.dropped,
                "held_open": self.held_open, **self.link.stats()}
