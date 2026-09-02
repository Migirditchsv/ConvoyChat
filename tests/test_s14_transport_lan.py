"""S-14: transport robustness for a real LAN — wrap-safe sequence handling,
mixer resync after a gap longer than the reorder buffer (a blackout while
someone is mid-sentence must not silence them for the rest of the spurt),
and symmetric-RTP peer learning so rosters never carry bridge IPs."""
import asyncio
import numpy as np
from common.audio import FRAME
from common.dsp import tone
from common.opusbind import Encoder
from common.protocol import rtp_pack, ssrc_of
from base.mixer.pymixer import PyMixer, seq_ahead, oldest_seq, REORDER_DEPTH
from bridge.audio.chain import DownlinkChain, _oldest


def _talker(pid="a"):
    enc = Encoder(dtx=False)
    frame = tone(700, 0.06, level_db=-16)
    ssrc = ssrc_of(pid)
    return lambda seq: rtp_pack(seq & 0xFFFF, (seq * 2880) & 0xFFFFFFFF, ssrc, enc.encode(frame))


def _loud(f):
    return np.abs(f.astype(np.int32)).max() > 500


def test_serial_arithmetic_at_wrap():
    assert seq_ahead(1, 65535) == 2
    assert seq_ahead(65535, 1) == 65534
    assert oldest_seq({0: b"", 1: b"", 65534: b"", 65535: b""}, 65533) == 65534
    assert _oldest({0: b"", 65535: b""}, 65535) == 65535


def test_mixer_resyncs_after_long_gap():
    """10 good packets, 40 lost mid-sentence, then a live stream: the mixer
    must decode real audio again within a few ticks — not conceal forever."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    m = PyMixer(rtp_port=0)
    m.add_participant("a", "main", None)
    p = m.parts["a"]
    pkt = _talker()
    for s in range(10):
        m._rx(pkt(s), ("127.0.0.1", 1))
    for _ in range(10):
        m._pop_frame(p)
    real = 0
    for s in range(50, 50 + 60):                # one arrival per tick, like a live talker
        m._rx(pkt(s), ("127.0.0.1", 1))
        m._pop_frame(p)
        real += p.plc_run == 0 and p.active
    assert p.resyncs >= 1, "never resynced"
    assert real >= 50, f"only {real}/60 real frames after the gap"


def test_mixer_short_gap_still_plc():
    asyncio.set_event_loop(asyncio.new_event_loop())
    m = PyMixer(rtp_port=0)
    m.add_participant("a", "main", None)
    p = m.parts["a"]
    pkt = _talker()
    for s in range(6):
        if s != 3:
            m._rx(pkt(s), ("127.0.0.1", 1))
    for _ in range(6):
        m._pop_frame(p)
    assert p.plc == 1 and p.resyncs == 0


def test_downlink_survives_seq_wrap():
    """A downlink that has run > 65 min wraps its RTP sequence; the reorder
    buffer must keep decoding straight through 65535 -> 0."""
    down = DownlinkChain()
    pkt = _talker("x")
    seqs = list(range(65530, 65536)) + list(range(0, 6))
    loud = 0
    for s in seqs:
        down.push_rtp(pkt(s))
        loud += _loud(down.pull())
    assert down.concealed == 0 and loud == len(seqs), "glitch at the wrap"


def test_mixer_learns_peer_from_uplink():
    """Roster says 127.0.0.1; the bridge's RTP arrives from 192.168.1.42 —
    the downlink must follow (symmetric RTP), and survive a re-populate."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    m = PyMixer(rtp_port=0)
    m.add_participant("a", "main", ("127.0.0.1", 6100))
    m._rx(_talker()(0), ("192.168.1.42", 51000))
    assert m.parts["a"].out_addr == ("192.168.1.42", 6100)
    assert m.stats()["a"]["peer"] == "192.168.1.42"
    m.add_participant("a", "main", ("127.0.0.1", 6100))    # orchestrator.populate() again
    assert m.parts["a"].out_addr == ("192.168.1.42", 6100)
    m2 = PyMixer(rtp_port=0, learn_peer=False)
    m2.add_participant("a", "main", ("127.0.0.1", 6100))
    m2._rx(_talker()(0), ("192.168.1.42", 51000))
    assert m2.parts["a"].out_addr == ("127.0.0.1", 6100)


def test_reorder_depth_bounds_buffer():
    asyncio.set_event_loop(asyncio.new_event_loop())
    m = PyMixer(rtp_port=0)
    m.add_participant("a", "main", None)
    pkt = _talker()
    for s in range(200):
        m._rx(pkt(s), ("127.0.0.1", 1))
    assert len(m.parts["a"].buf) == REORDER_DEPTH
    assert min(m.parts["a"].buf) == 200 - REORDER_DEPTH   # newest kept, oldest dropped
