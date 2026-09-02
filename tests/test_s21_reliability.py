"""S-21: nothing on the wire may stop a tick. A corrupt Opus payload with a
valid RTP header, a source that throws, a sink that throws: the mixer, the
bridge engine and the downlink keep running and count the fault."""
import asyncio
import numpy as np
import pytest

from common.audio import FRAME
from common.dsp import tone, band_db
from common.protocol import rtp_pack, ssrc_of
from base.mixer.pymixer import PyMixer
from base.media.participants import RtpSource
from bridge.audio.chain import DownlinkChain
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink
from tests.test_s07_mixer import Ear, seg

CORRUPT = b"\xff\x00" + b"\x00" * 40      # code-3 packet with zero frames: libopus rejects it
SHORT = bytes(range(256)) * 4                # decodes as a 10 ms SILK frame (160 samples), wrong ptime
JUNK = CORRUPT


def test_mixer_survives_corrupt_payloads():
    asyncio.set_event_loop(asyncio.new_event_loop())
    m = PyMixer(rtp_port=0)
    m.add_participant("a", "main", None)
    p = m.parts["a"]
    for s in range(5):
        m._rx(rtp_pack(s, s * 2880, ssrc_of("a"), CORRUPT if s % 2 else SHORT), ("127.0.0.1", 1))
    for _ in range(5):
        f = m._pop_frame(p)
        assert f.shape == (FRAME,)                # a 10 ms packet must not change the shape
    assert p.bad == 5 and m.stats()["a"]["bad"] == 5


def test_downlink_survives_corrupt_payloads():
    d = DownlinkChain()
    for s in range(4):
        d.push_rtp(rtp_pack(s, s * 2880, ssrc_of("x"), CORRUPT if s % 2 else SHORT))
    for _ in range(4):
        assert d.pull().shape == (FRAME,)
    assert d.bad == 4


@pytest.mark.realtime
def test_mixer_keeps_mixing_others_while_one_talker_sends_garbage():
    PORT = 5750

    async def scenario():
        m = PyMixer(rtp_port=PORT); await m.start()
        ear = Ear(); await ear.bind(5751)
        m.add_participant("good", "main", None)
        m.add_participant("bad", "main", None)
        m.add_participant("l", "main", ("127.0.0.1", 5751))
        src = RtpSource.noise("good", 500, 900, mixer_addr=("127.0.0.1", PORT), seed=41)
        await src.start()
        bad_ssrc = ssrc_of("bad")
        from bridge.io_adapters import UdpPort
        u = UdpPort(); await u.bind()
        for s in range(40):
            u.send(rtp_pack(s, s * 2880, bad_ssrc, JUNK), ("127.0.0.1", PORT))
            await asyncio.sleep(0.05)
        ticks_before = m.ticks
        await asyncio.sleep(0.5)
        assert m.ticks > ticks_before + 5, "mixer loop stopped"
        src.stop(); u.close(); m.stop(); await asyncio.sleep(0.05); ear.close()
        return ear.audio(), m.parts["bad"].bad, m.tick_errors

    audio, bad, errs = asyncio.run(scenario())
    assert bad >= 30 and errs == 0
    assert band_db(seg(audio, 1.0, 2.4), 500, 900) > -40, "good talker inaudible"


def test_engine_survives_source_sink_and_chain_faults():
    async def scenario():
        class FlakySource:
            def __init__(self): self.n = 0
            def read(self):
                self.n += 1
                if self.n == 3: raise OSError("mic hiccup")
                if self.n > 8: return None
                return tone(700, 0.06, level_db=-20)
        class FlakySink(ArraySink):
            def write(self, f):
                if len(self.frames) == 4: 
                    self.frames.append(f.copy()); raise OSError("ear hiccup")
                super().write(f)
        sink = FlakySink()
        eng = BridgeEngine("t", FlakySource(), sink, prefer_silero=False)
        eng.up.gate.force_open = True
        await eng.start(); await eng.wait(); eng.stop()
        assert eng.stats["tick_errors"] >= 2
        assert len(sink.frames) >= 6, "tick stopped after a fault"
        assert eng.stats["tx_pkts"] >= 5
    asyncio.run(scenario())
