"""S-05 end-to-end latency & loss robustness; S-10 mixer restart resilience."""
import asyncio
import numpy as np
import pytest
from common.audio import FRAME, dbfs
from common.dsp import chirp, find_delay_s
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink
from sim.impair import ImpairProxy


async def _two_node_run(profile: str, port: int, dur_s: float = 6.0,
                        restart_mixer_at: float | None = None):
    roster = demo_roster(2, base_port=port + 200)
    mixer = PyMixer(rtp_port=port)
    orc = Orchestrator(roster, mixer)
    await mixer.start()
    ref = chirp()
    total = int(dur_s * 1000 / 60)
    clips = [(int(1.0 * 1000 / 60), ref), (int(3.5 * 1000 / 60), ref)]
    proxy = ImpairProxy(("127.0.0.1", port), profile)
    pport = await proxy.start()
    talker = BridgeEngine("r0_lead", ArraySource(total, clips), ArraySink(),
                          mixer_addr=("127.0.0.1", pport),
                          down_port=roster.riders["r0_lead"].down_port,
                          prefer_silero=False)
    talker.up.gate._need_open = 0     # force-open: S-05 probes transport, not the gate
    hear_sink = ArraySink()
    hearer = BridgeEngine("r1_chase", ArraySource(total, []), hear_sink,
                          mixer_addr=("127.0.0.1", pport),
                          down_port=roster.riders["r1_chase"].down_port,
                          prefer_silero=False)
    await talker.start(); await hearer.start()

    if restart_mixer_at is not None:
        async def bounce():
            await asyncio.sleep(restart_mixer_at)
            mixer.stop()
            await asyncio.sleep(0.4)
            m2 = PyMixer(rtp_port=port)
            await m2.start()
            orc.reattach(m2)
        asyncio.create_task(bounce())

    await asyncio.gather(talker.wait(), hearer.wait())
    talker.stop(); hearer.stop(); proxy.stop(); orc.mixer.stop()
    return ref, hear_sink.audio()


@pytest.mark.realtime
def test_s05_latency_parkinglot():
    ref, heard = asyncio.run(_two_node_run("parkinglot", 5430))
    assert dbfs(heard) > -50, "no audio arrived"
    d = find_delay_s(heard, np.concatenate([np.zeros(16000, np.int16), ref]))
    # chirp injected at t=1.0 s into the timeline; delay beyond that is the path
    assert d <= 0.35, f"one-way sim path {d*1000:.0f} ms (budget 350)"


@pytest.mark.realtime
def test_s05_survives_edge_profile():
    ref, heard = asyncio.run(_two_node_run("edge", 5440))
    seg = heard[int(0.9 * 16000):int(2.2 * 16000)]
    assert dbfs(seg) > -45, "chirp inaudible under 15% loss — PLC/FEC failing"


@pytest.mark.realtime
def test_s10_mixer_restart_resumes():
    ref, heard = asyncio.run(_two_node_run("bench", 5450, dur_s=7.0,
                                           restart_mixer_at=2.2))
    # second chirp (t=3.5) is after the restart: it must be audible
    seg = heard[int(3.4 * 16000):int(4.6 * 16000)]
    assert dbfs(seg) > -45, "audio did not resume after mixer restart"
