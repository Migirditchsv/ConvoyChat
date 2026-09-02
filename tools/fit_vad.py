#!/usr/bin/env python3
"""Fit SpectralVad's logistic weights on the labeled fixtures (DR-013).

    python3 -m tools.fit_vad          # prints MEAN/STD/W/B to paste into bridge/audio/vad.py
                                      # and the gate metrics of the fitted rung

Training set: every frame of mix_{50,90,120} (labels from the fixture
manifest) plus the first 120 s of each wind-only take as negatives (so the
warm-up region is learned as wind, not speech). Plain gradient-descent
logistic regression; no dependency beyond numpy."""
from __future__ import annotations
import numpy as np
from sim import fixtures
from common.audio import FRAME
from bridge.audio.vad import SpectralVad, SileroVad
from bridge.audio.gate import SpeechGate


def dataset(train_speeds=(50, 90)):
    """Positives: labeled speech at train_speeds (120 km/h speech is buried
    at -6 dB and teaching a fallback to chase it makes it open on wind —
    measured: 1323/1333 wind frames open). Negatives: every non-speech
    frame of every mix plus the wind-only takes at all three speeds."""
    X, y, groups = [], [], []
    for speed in (50, 90, 120):
        v = SpectralVad()
        x = fixtures.load(f"mix_{speed}.wav"); lab = fixtures.load_labels(speed)
        n = min(len(x) // FRAME, len(lab))
        for i in range(n):
            f = v.features(x[i * FRAME:(i + 1) * FRAME])
            if lab[i] and speed not in train_speeds:
                continue
            X.append(f); y.append(float(lab[i])); groups.append(speed)
        v = SpectralVad()
        w = fixtures.load(f"windlong_{speed}.wav")
        for i in range(min(len(w) // FRAME, 3000)):
            X.append(v.features(w[i * FRAME:(i + 1) * FRAME])); y.append(0.0); groups.append(-speed)
    return np.array(X), np.array(y), np.array(groups)


def fit(X, y, iters=4000, lr=0.05, l2=1e-3, neg_weight=3.0):
    mean, std = X.mean(0), X.std(0) + 1e-9
    Z = (X - mean) / std
    w = np.zeros(Z.shape[1]); b = 0.0
    pos = y.sum(); neg = len(y) - pos
    # class balance, then wind counts neg_weight x: a fallback must prefer
    # missing speech over keying on wind
    wt = np.where(y > 0, 0.5 * len(y) / pos, neg_weight * 0.5 * len(y) / neg)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = (p - y) * wt
        w -= lr * (Z.T @ g / len(y) + l2 * w)
        b -= lr * g.mean()
    return mean, std, w, b


def gate_metrics(vad_factory, mode):
    out = {}
    for speed in (50, 90, 120):
        x = fixtures.load(f"mix_{speed}.wav"); lab = fixtures.load_labels(speed)
        v = vad_factory(); g = SpeechGate(mode=mode)
        n = min(len(x) // FRAME, len(lab)); sent = np.zeros(n, bool); opens = 0
        for i in range(n):
            f = x[i * FRAME:(i + 1) * FRAME]
            tx, _, just = g.process(f, v.prob(f), speed, mode)
            if tx: sent[max(0, i - len(tx) + 1): i + 1] = True
            opens += just
        missed = np.logical_and(lab[:n], ~sent).sum() / lab[:n].sum()
        w = fixtures.load(f"windlong_{speed}.wav"); v = vad_factory(); g = SpeechGate(mode=mode)
        fo = 0; open_frames = 0; total = 0
        for i in range(len(w) // FRAME):
            f = w[i * FRAME:(i + 1) * FRAME]
            tx, is_open, just = g.process(f, v.prob(f), speed, mode)
            fo += just if i >= 10 else 0
            open_frames += is_open; total += 1
        out[speed] = dict(missed_pct=round(100 * missed, 1), false_opens_300s=fo,
                          wind_open_pct=round(100 * open_frames / max(total, 1), 1))
    return out


if __name__ == "__main__":
    fixtures.build()
    X, y, g = dataset()
    mean, std, w, b = fit(X, y)
    print("    MEAN = np.array(%s)" % np.array2string(mean, precision=4, separator=", "))
    print("    STD = np.array(%s)" % np.array2string(std, precision=4, separator=", "))
    print("    W = np.array(%s)" % np.array2string(w, precision=4, separator=", "))
    print("    B = %.4f" % b)
    SpectralVad.MEAN, SpectralVad.STD, SpectralVad.W, SpectralVad.B = mean, std, w, b
    from bridge.audio.vad import EnergyVad
    print("spectral:", gate_metrics(SpectralVad, "spectral"))
    print("energy  :", gate_metrics(EnergyVad, "energy"))
