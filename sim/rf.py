"""Simulated half-duplex RF channel for tests and `make up-sim --rf`.

An RfChannel connects SimRigs. Each tick it takes the frame every keyed rig
wrote, sums them (two keyed rigs = doubling, as on air) with a noise floor,
and delivers that to every UNkeyed rig's receiver; keyed rigs receive
nothing (a real HT mutes its receiver while transmitting). Unkeyed rigs
with nobody transmitting receive squelched silence."""
from __future__ import annotations
import asyncio
from collections import deque
import numpy as np

from common.audio import FRAME, FRAME_MS


class SimRig:
    """The three faces of one radio: source (its speaker: what it received),
    sink (its mic: what we feed it while keyed) and PTT. Duck-types
    CmdSource / CmdSink / a Ptt so RadioLink and the engines use it as-is."""
    def __init__(self, name: str = "rig"):
        self.name = name
        self.on = False                     # PTT
        self._rxq: deque[np.ndarray] = deque()
        self._tx: np.ndarray | None = None
        self.key_ups = 0
        self.frames_rx = 0
        self.frames_tx = 0
        self.alive = True

    # Ptt
    def key(self, on: bool) -> None:
        if on and not self.on:
            self.key_ups += 1
        self.on = bool(on)

    # sink (mic in)
    def write(self, frame: np.ndarray) -> None:
        self._tx = np.asarray(frame, dtype=np.int16)
        self.frames_tx += 1

    # source (speaker out)
    def read(self) -> np.ndarray:
        if self._rxq:
            self.frames_rx += 1
            return self._rxq.popleft()
        return np.zeros(FRAME, dtype=np.int16)

    def start(self) -> None: ...
    def stop(self) -> None: ...

    # channel side
    def _take_tx(self) -> np.ndarray | None:
        f, self._tx = self._tx, None
        return f if self.on else None

    def _deliver(self, frame: np.ndarray) -> None:
        if len(self._rxq) > 4:
            self._rxq.popleft()
        self._rxq.append(frame)


class RfChannel:
    def __init__(self, noise_db: float = -80.0, seed: int = 5):
        self.rigs: list[SimRig] = []
        self.noise_db = noise_db
        self.rng = np.random.default_rng(seed)
        self._task = None
        self.ticks = 0
        self.collisions = 0

    def add(self, rig: SimRig) -> SimRig:
        self.rigs.append(rig)
        return rig

    def tick(self) -> None:
        self.ticks += 1
        keyed = [(r, r._take_tx()) for r in self.rigs if r.on]
        if len(keyed) > 1:
            self.collisions += 1
        if not keyed:
            return                                    # squelch closed everywhere
        mix = np.zeros(FRAME, np.int32)
        for _, f in keyed:
            if f is not None:
                mix += f[:FRAME].astype(np.int32)
        noise = self.rng.standard_normal(FRAME) * 32767 * 10 ** (self.noise_db / 20)
        out = np.clip(mix + noise, -32768, 32767).astype(np.int16)
        for r in self.rigs:
            if not r.on:
                r._deliver(out.copy())

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            self.tick()
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - loop.time()))

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
