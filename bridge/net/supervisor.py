"""Self-eviction policy (INV-9): the AP can't evict, so the client does.

Pure state machine; stats arrive from whatever provider exists (iw parsing on
the Pi at M2, synthetic streams in S-06). Actions are injected so tests can
observe them.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class LinkStats:
    tx_rate_mbps: float
    rtp_loss_pct: float


class EvictionPolicy:
    MIN_RATE = 12.0
    MAX_LOSS = 25.0
    SUSTAIN_S = 3
    COOLDOWN_S = 30

    def __init__(self, evict_action, earcon_action=lambda name: None):
        self._evict = evict_action
        self._earcon = earcon_action
        self._bad_s = 0
        self._cooldown = 0
        self.evictions = 0

    def tick_1s(self, s: LinkStats) -> None:
        if self._cooldown > 0:
            self._cooldown -= 1
        bad = s.tx_rate_mbps < self.MIN_RATE or s.rtp_loss_pct > self.MAX_LOSS
        self._bad_s = self._bad_s + 1 if bad else 0
        if self._bad_s >= self.SUSTAIN_S and self._cooldown == 0:
            self._earcon("link_lost")
            self._evict()
            self.evictions += 1
            self._bad_s = 0
            self._cooldown = self.COOLDOWN_S
