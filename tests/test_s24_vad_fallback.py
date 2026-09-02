"""S-24: the SAFE-1 fallback rungs must not key on wind. The spectral rung
(DR-013) must catch speech at 50/90 km/h like Silero and stay closed on
wind at every speed; the floor tracker must settle in seconds, not 80 s;
the chain order is silero -> spectral -> energy -> OPEN."""
import numpy as np
import pytest

from common.audio import FRAME, dbfs
from bridge.audio.vad import SafeVad, SpectralVad, EnergyVad, FloorTracker
from bridge.audio.gate import SpeechGate
from sim import fixtures
from tests.conftest import has_silero


def run(vad, wav, speed, mode):
    x = fixtures.load(wav)
    g = SpeechGate(mode=mode)
    n = len(x) // FRAME
    sent = np.zeros(n, bool); open_frames = 0
    for i in range(n):
        f = x[i * FRAME:(i + 1) * FRAME]
        tx, is_open, _ = g.process(f, vad.prob(f), speed, mode)
        if tx:
            sent[max(0, i - len(tx) + 1): i + 1] = True
        open_frames += is_open
    return sent, open_frames / n


@pytest.mark.parametrize("speed", [50, 90])
def test_spectral_rung_catches_speech(speed):
    sent, _ = run(SpectralVad(), f"mix_{speed}.wav", speed, "spectral")
    lab = fixtures.load_labels(speed)[: len(sent)]
    missed = np.logical_and(lab, ~sent).sum() / lab.sum()
    assert missed <= 0.10, f"spectral fallback missed {100*missed:.1f}% at {speed}"


@pytest.mark.parametrize("speed", [50, 90, 120])
def test_fallback_rungs_stay_closed_on_wind(speed):
    """The whole point of the rung: a bridge on the energy/spectral fallback
    must not become a wind transmitter (measured before: 80 s open)."""
    _, open_pct = run(SpectralVad(), f"windlong_{speed}.wav", speed, "spectral")
    assert open_pct <= 0.10, f"spectral rung open {100*open_pct:.1f}% of 300 s wind at {speed}"
    _, open_pct = run(EnergyVad(), f"windlong_{speed}.wav", speed, "energy")
    assert open_pct <= 0.10, f"energy rung open {100*open_pct:.1f}% of 300 s wind at {speed}"


def test_first_80s_of_wind_no_longer_keys():
    """Before DR-013 the energy rung was open for ~100 % of the first 80 s on
    the 120 km/h wind bed. Measured now: spectral 0/0/147, energy 29/49/192
    of 1333 frames at 50/90/120. Pin at 20 %."""
    x = fixtures.load("windlong_120.wav")[: 1333 * FRAME]
    for vad, mode in ((SpectralVad(), "spectral"), (EnergyVad(), "energy")):
        g = SpeechGate(mode=mode); opened = 0
        for i in range(1333):
            f = x[i * FRAME:(i + 1) * FRAME]
            _, is_open, _ = g.process(f, vad.prob(f), 120, mode)
            opened += is_open
        assert opened < 0.20 * 1333, f"{mode}: open {opened}/1333 frames in the first 80 s"


def test_floor_tracker_settles_fast():
    ft = FloorTracker(-60.0)
    for _ in range(40):
        ft.update(-32.0)
    assert abs(ft.floor_db + 32.0) < 0.5, "floor did not settle in 40 frames"
    ft.update(-20.0)                                       # a shout: floor barely moves
    assert ft.floor_db < -31.0
    for _ in range(10):
        ft.update(-70.0)                                   # quieter: fast fall
    assert ft.floor_db <= -36.0


def test_chain_order_and_demotion():
    v = SafeVad(prefer_silero=False)
    assert [c.name for c in v._chain] == ["spectral", "energy"]
    v._demote(); assert v.mode == "energy"
    v._demote(); assert v.mode == "open"
    assert v.prob(np.zeros(FRAME, np.int16)) == 1.0
    if has_silero():
        assert [c.name for c in SafeVad()._chain] == ["silero", "spectral", "energy"]


def test_spectral_rung_is_cheap():
    import time
    v = SpectralVad()
    f = (np.random.default_rng(1).standard_normal(FRAME) * 3000).astype(np.int16)
    t0 = time.perf_counter()
    for _ in range(200):
        v.prob(f)
    assert (time.perf_counter() - t0) / 200 < 0.005     # << the 50 ms SAFE-1 budget


def test_spectral_rung_is_silent_on_silence():
    """All-zero frames have maximal flatness; without a level floor the rung
    called them speech and a PTT release never closed the gate (S-15)."""
    v = SpectralVad()
    z = np.zeros(FRAME, np.int16)
    assert all(v.prob(z) == 0.0 for _ in range(20))
    quiet = (np.random.default_rng(2).standard_normal(FRAME) * 3).astype(np.int16)   # ~ -80 dBFS
    assert v.prob(quiet) == 0.0
