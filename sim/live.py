"""Virtual riders that never stop: the sim edge for `make up-sim`.

Each rider is the REAL BridgeEngine + BridgeAgent on a synthetic mouth:
a looping wind bed for their speed with Harvard utterances dropped in at
random (chatter) or on demand — the rider phone page's TALK button sends
node_cmd `say`, which is how a laptop-only tester exercises the whole
gate -> RTP -> mixer -> ladder -> dashboard path with nothing spoofed
except the microphone."""
from __future__ import annotations
import asyncio
import random

import numpy as np

from common.audio import FS, FRAME, FRAME_MS
from common.protocol import CONTROL_PORT
from bridge.agent import BridgeAgent, SimActions
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySink
from sim import fixtures
from sim.impair import ImpairProxy

SPEEDS = [90, 50, 90, 90, 120, 50, 90]
LINKS = [(-58, 86), (-52, 130), (-63, 57), (-66, 43), (-79, 14), (-60, 72), (-70, 29)]


class LoopingMouth:
    """Endless ArraySource: wind loop + scheduled speech overlays."""
    def __init__(self, wind: np.ndarray, clips: list[np.ndarray], rng: random.Random,
                 chatter_s: tuple[float, float] | None = (12.0, 40.0)):
        self.wind = wind
        self.clips = clips
        self.rng = rng
        self.chatter_s = chatter_s
        self.pos = 0
        self._active: list[tuple[int, np.ndarray]] = []   # (offset_into_clip, clip)
        self._next_chatter = self._draw()
        self.said = 0

    def _draw(self) -> int:
        if not self.chatter_s:
            return 1 << 60
        lo, hi = self.chatter_s
        return int(self.rng.uniform(lo, hi) * 1000 / FRAME_MS)

    def say(self, clip: int) -> str:
        c = self.clips[clip % len(self.clips)]
        self._active.append((0, c))
        self.said += 1
        return f"saying clip {clip % len(self.clips)} ({len(c)/FS:.1f}s)"

    def read(self) -> np.ndarray:
        n = len(self.wind)
        i = (self.pos * FRAME) % n
        f = self.wind[i:i + FRAME].astype(np.int32)
        if len(f) < FRAME:                       # wrap
            f = np.concatenate([f, self.wind[:FRAME - len(f)].astype(np.int32)])
        keep = []
        for off, c in self._active:
            seg = c[off:off + FRAME]
            f[:len(seg)] += seg
            if off + FRAME < len(c):
                keep.append((off + FRAME, c))
        self._active = keep
        self.pos += 1
        self._next_chatter -= 1
        if self._next_chatter <= 0:
            self.say(self.rng.randrange(len(self.clips)))
            self._next_chatter = self._draw()
        return np.clip(f, -32768, 32767).astype(np.int16)


class SinkRing(ArraySink):
    """Keeps only the last few seconds (a forever-running sim must not grow)."""
    def __init__(self, keep_s: float = 10.0):
        super().__init__()
        self.keep = int(keep_s * 1000 / FRAME_MS)

    def write(self, frame: np.ndarray) -> None:
        self.frames.append(frame.copy())
        if len(self.frames) > self.keep:
            del self.frames[: len(self.frames) - self.keep]


def _mouth_material(clips_raw: list[np.ndarray]) -> tuple[dict[int, np.ndarray], list[np.ndarray]]:
    winds = {}
    for speed in fixtures.WIND_DB:
        w, _ = fixtures._wind_take(30.0, speed, seed=11)
        w = fixtures._calibrate(w, fixtures.WIND_DB[speed]).astype(np.float64)
        nramp = int(0.3 * FS)
        w[:nramp] *= np.linspace(0, 1, nramp); w[-nramp:] *= np.linspace(1, 0, nramp)
        winds[speed] = fixtures.headset_sim(w.astype(np.int16))
    clips = [fixtures.headset_sim(fixtures._calibrate(c, fixtures.SPEECH_DB)) for c in clips_raw]
    return winds, clips


class VirtualRiders:
    """Attach N virtual riders (every non-music rider in `roster`) to a running
    base: RTP to `mixer_port`, control to `control_url`."""
    def __init__(self, roster, mixer_port: int = 5100, control_url: str | None = None,
                 chatter: bool = True, prefer_silero: bool = True, seed: int = 1,
                 log=lambda msg: None, skip: set[str] | None = None):
        """skip: roster ids already served by something else (the radio gateway)."""
        self.roster = roster
        self.mixer_port = mixer_port
        self.control_url = control_url or f"ws://127.0.0.1:{CONTROL_PORT}/"
        self.chatter = chatter
        self.prefer_silero = prefer_silero
        self.rng = random.Random(seed)
        self.log = log
        self.engines: dict[str, BridgeEngine] = {}
        self.agents: dict[str, BridgeAgent] = {}
        self.mouths: dict[str, LoopingMouth] = {}
        self.sinks: dict[str, SinkRing] = {}
        self.proxies: list[ImpairProxy] = []
        self.speeds: dict[str, int] = {}
        self.skip = set(skip or ())

    async def start(self) -> None:
        fixtures.build()
        clips_raw, _ = fixtures._speech_clips()
        winds, clips = _mouth_material(clips_raw)
        riders = [r for r in self.roster.riders.values()
                  if r.role != "music" and r.id not in self.skip]
        for k, r in enumerate(riders):
            speed = SPEEDS[k % len(SPEEDS)]
            self.speeds[r.id] = speed
            mouth = LoopingMouth(winds[speed], clips, random.Random(self.rng.random()),
                                 chatter_s=(12.0, 40.0) if self.chatter else None)
            proxy = ImpairProxy(("127.0.0.1", self.mixer_port),
                                "edge" if speed == 120 else "parkinglot", seed=r.ssrc & 0xFF)
            pport = await proxy.start()
            self.proxies.append(proxy)
            sink = SinkRing()
            eng = BridgeEngine(r.id, mouth, sink, mixer_addr=("127.0.0.1", pport),
                               down_port=r.down_port, prefer_silero=self.prefer_silero)
            eng.up.set_speed(speed)
            link = LINKS[k % len(LINKS)]
            rng = np.random.default_rng(k)
            agent = BridgeAgent(
                r.id, eng, SimActions(engine=eng, speak=mouth.say), self.control_url,
                link_stats=lambda l=link, rng=rng, eng=eng: {
                    "rssi": l[0] + int(rng.integers(-2, 3)), "tx_rate": l[1],
                    "rtp_loss": eng.downlink_loss_pct()},
                log=lambda m, rid=r.id: self.log(f"{rid}: {m}"))
            self.engines[r.id], self.agents[r.id] = eng, agent
            self.mouths[r.id], self.sinks[r.id] = mouth, sink
            await eng.start()
            await agent.start()
        self.log(f"virtual riders up: {', '.join(self.engines)} "
                 f"(speeds {', '.join(f'{k}@{v}' for k, v in self.speeds.items())} km/h)")

    def stop(self) -> None:
        for a in self.agents.values():
            a.stop()
        for e in self.engines.values():
            e.stop()
        for p in self.proxies:
            p.stop()
