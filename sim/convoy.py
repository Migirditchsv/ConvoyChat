"""Virtual convoy (plan T-0): N real BridgeEngines on fake IO + real mixer +
real orchestrator. The sim bridge IS the production engine — only the ears
and mouth are arrays."""
from __future__ import annotations
import argparse
import asyncio
import numpy as np

from common.audio import FRAME
from common.roster import demo_roster, Roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink
from sim import fixtures
from sim.impair import ImpairProxy


class SimConvoy:
    def __init__(self, n_riders: int = 4, profile: str = "bench",
                 dur_s: float = 10.0, rtp_port: int = 5100,
                 talk_script: list[tuple[str, float]] | None = None,
                 prefer_silero: bool = False):
        """talk_script: [(node_id, t_start_s), ...] each speaks the phrase clip."""
        self.roster: Roster = demo_roster(n_riders)
        self.mixer = PyMixer(rtp_port=rtp_port)
        self.orc = Orchestrator(self.roster, self.mixer)
        self.profile = profile
        self.dur_s = dur_s
        self.prefer_silero = prefer_silero
        self.rtp_port = rtp_port
        self.bridges: dict[str, BridgeEngine] = {}
        self.sinks: dict[str, ArraySink] = {}
        self.proxies: list[ImpairProxy] = []
        self.talk_script = talk_script or []

    def _source_for(self, node_id: str) -> ArraySource:
        total = int(self.dur_s * 1000 / 60)
        phrase = fixtures.load("phrase.wav")
        clips = [(int(t * 1000 / 60), phrase)
                 for (nid, t) in self.talk_script if nid == node_id]
        return ArraySource(total, clips)

    async def start(self):
        fixtures.build()
        await self.mixer.start()
        for r in self.roster.riders.values():
            proxy = ImpairProxy(("127.0.0.1", self.rtp_port), self.profile,
                                seed=r.ssrc & 0xFF)
            pport = await proxy.start()
            self.proxies.append(proxy)
            sink = ArraySink()
            eng = BridgeEngine(
                r.id, self._source_for(r.id), sink,
                mixer_addr=("127.0.0.1", pport),
                down_port=r.down_port, prefer_silero=self.prefer_silero,
                on_vad=(lambda open_, pid=r.id: self.orc.on_vad(pid, open_)))
            self.sinks[r.id] = sink
            self.bridges[r.id] = eng
            await eng.start()

    async def run(self):
        await self.start()
        await asyncio.gather(*(b.wait() for b in self.bridges.values()))
        self.stop()

    def stop(self):
        for b in self.bridges.values():
            b.stop()
        for p in self.proxies:
            p.stop()
        self.mixer.stop()

    def ear(self, node_id: str) -> np.ndarray:
        return self.sinks[node_id].audio()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--riders", type=int, default=6)
    ap.add_argument("--profile", default="parkinglot")
    ap.add_argument("--dur", type=float, default=15.0)
    args = ap.parse_args()
    script = [("r0_lead", 2.0), ("r2_rider", 6.0)]
    c = SimConvoy(args.riders, args.profile, args.dur, talk_script=script)
    asyncio.run(c.run())
    for nid in c.sinks:
        from common.audio import dbfs
        print(f"{nid:12s} ear level {dbfs(c.ear(nid)):6.1f} dBFS")


if __name__ == "__main__":
    main()
