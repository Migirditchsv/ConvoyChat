"""In-process UDP impairment proxy (DR-003) — CI's tc netem.
Profiles shared with Tier-1 netem configs; numbers from the plan."""
from __future__ import annotations
import asyncio
import random

from bridge.io_adapters import UdpPort

PROFILES = {
    "bench":      dict(loss=0.00, delay=0.002, jitter=0.001, blackout=None),
    "parkinglot": dict(loss=0.01, delay=0.005, jitter=0.005, blackout=None),
    "edge":       dict(loss=0.15, delay=0.020, jitter=0.030, blackout=None),
    "cliff":      dict(loss=0.40, delay=0.060, jitter=0.140, blackout=None),
    "flap":       dict(loss=0.01, delay=0.005, jitter=0.005, blackout=(5.0, 60.0)),
}


class ImpairProxy:
    """Forwards UDP from a listen port to a target with loss/delay/jitter."""
    def __init__(self, target: tuple[str, int], profile: str = "bench", seed: int = 1):
        self.target = target
        self.p = PROFILES[profile]
        self.rng = random.Random(seed)
        self.udp = UdpPort()
        self.port = None
        self._t0 = None

    async def start(self) -> int:
        self.udp.on_packet = self._rx
        self.port = await self.udp.bind()
        loop = asyncio.get_running_loop()
        self._t0 = loop.time()
        return self.port

    def _blackout_now(self, now: float) -> bool:
        b = self.p["blackout"]
        if not b:
            return False
        dur, period = b
        return ((now - self._t0) % period) < dur

    def _rx(self, data: bytes, addr) -> None:
        loop = asyncio.get_event_loop()
        now = loop.time()
        if self._blackout_now(now) or self.rng.random() < self.p["loss"]:
            return
        d = self.p["delay"] + self.rng.random() * self.p["jitter"]
        loop.call_later(d, self.udp.send, data, self.target)

    def stop(self):
        self.udp.close()
