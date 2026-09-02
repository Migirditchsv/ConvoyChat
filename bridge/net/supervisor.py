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



class HeadsetSupervisor:
    """Self-repair for the helmet link: after `down_s` seconds of the headset
    reporting not-connected, ask for a reconnect; back off between attempts
    so a headset that is simply off is not hammered. Pure; actions injected."""
    def __init__(self, reconnect_action, down_s: int = 15, backoff_s: int = 30,
                 max_attempts: int = 6, earcon_action=lambda name: None):
        self._reconnect = reconnect_action
        self._earcon = earcon_action
        self.down_s, self.backoff_s, self.max_attempts = down_s, backoff_s, max_attempts
        self._down = 0
        self._wait = 0
        self.attempts = 0
        self.repairs = 0
        self._was_connected = True

    def tick_1s(self, connected: bool | None) -> bool:
        """-> True when a reconnect was requested this second."""
        if connected is None:                      # unknown (no headset configured)
            return False
        if connected:
            if not self._was_connected and self.attempts:
                self.repairs += 1
                self._earcon("connected")
            self._was_connected = True
            self._down = 0
            self._wait = 0
            self.attempts = 0
            return False
        self._was_connected = False
        self._down += 1
        if self._wait > 0:
            self._wait -= 1
            return False
        if self._down >= self.down_s and self.attempts < self.max_attempts:
            self.attempts += 1
            self._wait = self.backoff_s
            self._reconnect()
            return True
        return False
