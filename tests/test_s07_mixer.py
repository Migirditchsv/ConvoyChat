"""S-07: MixerAPI conformance — N-1, gains, rooms, move. Backend-agnostic.
Probes are noise BANDS, not tones: Opus voice mode mangles pure sines."""
import asyncio
import numpy as np
import pytest
from common.audio import FRAME
from common.dsp import band_db
from common.opusbind import Decoder
from common.protocol import rtp_unpack
from base.mixer.pymixer import PyMixer
from base.media.participants import RtpSource
from bridge.io_adapters import UdpPort


def make_mixer(port):
    return PyMixer(rtp_port=port)


class Ear:
    def __init__(self):
        self.udp = UdpPort(); self.dec = Decoder(); self.frames = []
    async def bind(self, port):
        self.udp.on_packet = lambda d, a: self._rx(d)
        await self.udp.bind("127.0.0.1", port)
    def _rx(self, d):
        try: _, _, _, _, pl = rtp_unpack(d)
        except ValueError: return
        self.frames.append(self.dec.decode(pl, FRAME))
    def audio(self):
        return np.concatenate(self.frames) if self.frames else np.zeros(0, np.int16)
    def close(self):
        self.udp.close()


BAND_A = (500, 900)
BAND_B = (1400, 1800)


def seg(x, t0, t1):
    return x[int(t0 * 16000):int(t1 * 16000)]


async def _run(seconds, port, setup):
    m = make_mixer(port)
    await m.start()
    ears, sources = await setup(m)
    await asyncio.sleep(seconds)
    for s in sources: s.stop()
    m.stop()
    await asyncio.sleep(0.05)
    for e in ears.values(): e.close()
    await asyncio.sleep(0.05)
    return m, ears


@pytest.mark.realtime
def test_n_minus_one_and_gains():
    PORT = 5310
    async def setup(m):
        ear_a, ear_b = Ear(), Ear()
        await ear_a.bind(5501); await ear_b.bind(5503)
        m.add_participant("a", "main", ("127.0.0.1", 5501))
        m.add_participant("b", "main", ("127.0.0.1", 5503))
        sa = RtpSource.noise("a", *BAND_A, mixer_addr=("127.0.0.1", PORT), seed=11)
        sb = RtpSource.noise("b", *BAND_B, mixer_addr=("127.0.0.1", PORT), seed=12)
        await sa.start(); await sb.start()
        return {"a": ear_a, "b": ear_b}, [sa, sb]

    _, ears = asyncio.run(_run(2.5, PORT, setup))
    a, b = seg(ears["a"].audio(), 0.5, 2.0), seg(ears["b"].audio(), 0.5, 2.0)
    assert band_db(a, *BAND_B) - band_db(a, *BAND_A) >= 25, "self-echo in a's mix"
    assert band_db(b, *BAND_A) - band_db(b, *BAND_B) >= 25, "self-echo in b's mix"


@pytest.mark.realtime
def test_rooms_isolate_and_move():
    PORT = 5312
    async def setup(m):
        ear_b = Ear(); await ear_b.bind(5505)
        m.add_participant("a", "main", None)
        m.add_participant("b", "nav", ("127.0.0.1", 5505))
        sa = RtpSource.noise("a", *BAND_A, mixer_addr=("127.0.0.1", PORT))
        await sa.start()
        asyncio.get_event_loop().call_later(1.4, m.move, "a", "nav")
        return {"b": ear_b}, [sa]

    _, ears = asyncio.run(_run(3.0, PORT, setup))
    x = ears["b"].audio()
    before, after = seg(x, 0.4, 1.2), seg(x, 1.9, 2.7)
    gain = band_db(after, *BAND_A) - band_db(before, *BAND_A)
    assert gain >= 20, f"move() had no effect ({gain:.1f} dB)"


@pytest.mark.realtime
def test_input_gain_applies():
    PORT = 5314
    async def setup(m):
        ear = Ear(); await ear.bind(5507)
        m.add_participant("a", "main", None)
        m.add_participant("l", "main", ("127.0.0.1", 5507))
        sa = RtpSource.noise("a", *BAND_A, mixer_addr=("127.0.0.1", PORT))
        await sa.start()
        asyncio.get_event_loop().call_later(1.4, m.set_gain, "a", 25)
        return {"l": ear}, [sa]

    _, ears = asyncio.run(_run(3.0, PORT, setup))
    x = ears["l"].audio()
    drop = band_db(seg(x, 0.4, 1.2), *BAND_A) - band_db(seg(x, 1.9, 2.7), *BAND_A)
    assert 8 <= drop <= 16, f"gain 100->25 gave {drop:.1f} dB (expect ~12)"
