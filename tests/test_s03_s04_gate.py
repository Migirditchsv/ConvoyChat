"""S-03 missed speech / S-04 false openings, on labeled post-chain fixtures.
Silero targets are the plan's; energy-fallback runs looser (flagged)."""
import numpy as np
import pytest
from common.audio import FRAME
from bridge.audio.vad import SafeVad
from bridge.audio.gate import SpeechGate
from sim import fixtures
from tests.conftest import has_silero


def run_gate(wav: str, speed: int, skip_frames: int = 0):
    """Returns (openings, sent): sent[i] True iff frame i's AUDIO was
    transmitted — a just-opened gate flushes its pre-roll, so frames before
    the opening are sent too. This is what 'missed speech' means (plan S-03)."""
    x = fixtures.load(wav)
    vad = SafeVad(prefer_silero=has_silero())
    gate = SpeechGate(mode=vad.mode)
    opens, sent = 0, np.zeros(len(x) // FRAME, bool)
    for i in range(len(sent)):
        f = x[i*FRAME:(i+1)*FRAME]
        tx, _, just = gate.process(f, vad.prob(f), speed, vad.mode)
        if tx:
            sent[max(0, i - len(tx) + 1): i + 1] = True
        opens += just if i >= skip_frames else 0
    return opens, sent


@pytest.mark.parametrize("speed", [50, 90, 120])
def test_s03_missed_speech(speed):
    _, sent = run_gate(f"mix_{speed}.wav", speed)
    labels = fixtures.load_labels(speed)
    n = min(len(labels), len(sent))
    labels, sent = labels[:n], sent[:n]
    speech = labels.sum()
    missed = np.logical_and(labels, ~sent).sum()
    # 120 km/h tier is -6 dB SNR through the headset chain: one of the five
    # real utterances peaks at silero 0.52 for a single frame — below any
    # gate that also rejects gusts. 25% is the measured honest floor there
    # (DR-008); the answer at that SNR is the mechanical work, per the plan.
    limits = {50: 0.05, 90: 0.05, 120: 0.25}   # 50/90 measure 0.0% today
    limit = limits[speed] if has_silero() else 0.45
    assert missed / speech <= limit, f"missed {missed}/{speech} labeled speech frames"


@pytest.mark.parametrize("speed", [50, 90])
def test_s03_onsets_survive(speed):
    """The onset CONTRACT: at <=90 km/h every utterance's first voiced frame
    (or its immediate successor) is transmitted — pre-roll working."""
    import json, os
    man = json.load(open(os.path.join(fixtures.DATA, "manifest.json")))
    onsets = man["sets"][str(speed)]["onsets"]
    _, sent = run_gate(f"mix_{speed}.wav", speed)
    ok = sum(any(sent[o:o+2]) for o in onsets)
    assert ok == len(onsets), f"clipped onsets: {len(onsets)-ok}/{len(onsets)}"


def test_s03_onset_floor_120():
    """At 120 km/h (-6 dB SNR through the headset chain) three of the five
    real utterances open too late for any pre-roll — their intros sit below
    silero's separable range for this input (measured, DR-008). This test
    pins the FLOOR so regressions below current behavior still fail; the
    fix for the gap is mechanical (mic placement, sealing), per the plan's
    honest-expectations verdict, not more gate tuning."""
    import json, os
    man = json.load(open(os.path.join(fixtures.DATA, "manifest.json")))
    onsets = man["sets"]["120"]["onsets"]
    _, sent = run_gate("mix_120.wav", 120)
    ok = sum(any(sent[o:o+2]) for o in onsets)
    assert ok >= 2, f"onset floor regressed: {ok}/{len(onsets)} sent"


@pytest.mark.parametrize("speed", [90, 120])
def test_s04_false_openings(speed):
    """300 s wind-only take; first 10 frames skipped (fade-in margin).
    Plan target 6/h; with a 5-minute window we assert <=1 opening (12/h,
    provisional until F-03 real captures re-baseline the fixtures)."""
    opens, _ = run_gate(f"windlong_{speed}.wav", speed, skip_frames=10)
    limit = 1 if has_silero() else 8
    assert opens <= limit, f"{opens} openings in 300 s ({opens*12}/h) at {speed} km/h"
