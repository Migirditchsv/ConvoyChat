"""S-02: Opus/RTP framing — 60 ms ptime, gate-as-DTX, FEC/PLC decode paths."""
import numpy as np
from common.audio import FRAME, RTP_TS_STEP
from common.dsp import tone
from common.opusbind import Decoder
from common.protocol import rtp_unpack
from bridge.audio.chain import UplinkChain, DownlinkChain


def _speech_frames(n):
    x = tone(700, 0.06 * n, level_db=-16)
    return [x[i*FRAME:(i+1)*FRAME] for i in range(n)]


def test_ptime_and_seq():
    up = UplinkChain("t", prefer_silero=False)
    up.gate.force_open = True  # bypass classification for framing check
    pkts = []
    for f in _speech_frames(10):
        pkts += up.feed(f)
    assert len(pkts) >= 9
    metas = [rtp_unpack(p) for p in pkts]
    ts = [m[2] for m in metas]
    assert all((b - a) % (1 << 32) == RTP_TS_STEP for a, b in zip(ts, ts[1:]))
    seqs = [m[1] for m in metas]
    assert all((b - a) % (1 << 16) == 1 for a, b in zip(seqs, seqs[1:]))


def test_gate_is_dtx():
    up = UplinkChain("t", prefer_silero=False)
    silence = np.zeros(FRAME, np.int16)
    pkts = sum((up.feed(silence) for _ in range(50)), [])
    assert len(pkts) <= 2   # ≤~5 pkt/s equivalent: gated silence never transmits


def test_plc_and_decode():
    up = UplinkChain("t", prefer_silero=False)
    up.gate.force_open = True
    down = DownlinkChain()
    pkts = []
    for f in _speech_frames(6):
        pkts += up.feed(f)
    for i, p in enumerate(pkts):
        if i != 3:                      # drop one packet
            down.push_rtp(p)
    out = np.concatenate([down.pull() for _ in range(len(pkts))])
    assert len(out) == len(pkts) * FRAME
    assert np.abs(out.astype(np.int32)).max() > 500   # audio, not silence
