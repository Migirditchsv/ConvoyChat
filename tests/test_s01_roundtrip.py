"""S-01: fixture integrity + array IO round trip with the chain bypassed."""
import json, os
import numpy as np
from common.audio import FRAME
from bridge.io_adapters import ArraySource, ArraySink
from sim import fixtures


def test_fixtures_exist_and_labeled():
    man = json.load(open(os.path.join(fixtures.DATA, "manifest.json")))
    for speed in (50, 90, 120):
        mix = fixtures.load(f"mix_{speed}.wav")
        labels = fixtures.load_labels(speed)
        assert len(mix) // FRAME == len(labels)
        assert labels.any() and not labels.all()
    assert man["sets"]["phrase"]["text"]


def test_array_io_bit_exact():
    x = (np.arange(FRAME * 5, dtype=np.int16) % 2000 - 1000).astype(np.int16)
    src = ArraySource(5, [(0, x)])
    sink = ArraySink()
    while (f := src.read()) is not None:
        sink.write(f)
    assert np.array_equal(sink.audio(), x)
