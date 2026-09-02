"""SAFE-1 semantics: exceptions demote instantly; only sustained overruns
demote; final fallback is OPEN (prob=1.0). A gated hazard call must be
impossible to cause by a crashing classifier. Chain without Silero:
spectral -> energy -> OPEN (DR-013)."""
import numpy as np
import bridge.audio.vad as vadmod
from bridge.audio.vad import SafeVad


class Flaky:
    name = "flaky"
    def __init__(self, fail_after=2):
        self.n, self.fail_after = 0, fail_after
    def prob(self, f):
        self.n += 1
        if self.n > self.fail_after:
            raise RuntimeError("classifier crashed")
        return 0.9


def test_exception_demotes_and_ends_open():
    v = SafeVad(prefer_silero=False)
    v._chain = [Flaky(fail_after=1)]        # no fallback below it
    f = np.zeros(960, np.int16)
    assert v.prob(f) == 0.9
    assert v.prob(f) == 1.0                 # crashed -> OPEN, fail-open
    assert v.mode == "open"


def test_single_overrun_does_not_demote(monkeypatch):
    v = SafeVad(prefer_silero=False)        # spectral mode (first rung without Silero)
    times = iter([0.0, 0.2, 1.0, 1.001, 2.0, 2.001, 3.0, 3.001])
    monkeypatch.setattr(vadmod.time, "monotonic", lambda: next(times))
    f = (np.random.default_rng(1).standard_normal(960) * 3000).astype(np.int16)
    v.prob(f)                               # 200 ms call: overrun #1
    assert v.mode == "spectral"             # still there
    v.prob(f); v.prob(f); v.prob(f)         # fast calls reset the counter
    assert v.mode == "spectral"


def test_sustained_overrun_demotes(monkeypatch):
    v = SafeVad(prefer_silero=False)
    seq = []
    t = [0.0]
    def fake_time():
        t[0] += 0.2                          # every call appears to take 200 ms
        return t[0]
    monkeypatch.setattr(vadmod.time, "monotonic", fake_time)
    f = np.zeros(960, np.int16)
    for _ in range(SafeVad.OVERRUN_LIMIT):
        v.prob(f)
    assert v.mode == "energy"                # spectral demoted after sustained stall
    for _ in range(SafeVad.OVERRUN_LIMIT):
        v.prob(f)
    assert v.mode == "open"                  # then energy; the floor is always OPEN
