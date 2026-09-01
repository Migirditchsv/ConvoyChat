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
    limit = 0.10 if has_silero() else 0.35
    assert missed / speech <= limit, f"missed {missed}/{speech} labeled speech frames"


@pytest.mark.parametrize("speed", [50, 90, 120])
def test_s03_onsets_survive(speed):
    """Pre-roll: the frame at each labeled onset must be transmitted."""
    import json, os
    man = json.load(open(os.path.join(fixtures.DATA, "manifest.json")))
    onsets = man["sets"][str(speed)]["onsets"]
    _, sent = run_gate(f"mix_{speed}.wav", speed)
    tol = 1   # first voiced frame (or its immediate successor) must be SENT
    ok = sum(any(sent[o:o+tol+1]) for o in onsets)
    assert ok == len(onsets), f"clipped onsets: {len(onsets)-ok}/{len(onsets)}"


@pytest.mark.parametrize("speed", [90, 120])
def test_s04_false_openings(speed):
    """300 s wind-only take; first 10 frames skipped (fade-in margin).
    Plan target 6/h; with a 5-minute window we assert <=1 opening (12/h,
    provisional until F-03 real captures re-baseline the fixtures)."""
    opens, _ = run_gate(f"windlong_{speed}.wav", speed, skip_frames=10)
    limit = 1 if has_silero() else 8
    assert opens <= limit, f"{opens} openings in 300 s ({opens*12}/h) at {speed} km/h"
