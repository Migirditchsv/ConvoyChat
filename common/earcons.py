"""Procedural earcons (SAFE-2): generated, not recorded, so the repo carries
no binaries and any agent can regenerate/retune them."""
from __future__ import annotations
import os
import numpy as np
from common.audio import FS, write_wav
from common.dsp import tone

SPECS = {  # name: list of (freq, ms) — rising = good, falling = bad
    "connected":       [(660, 90), (990, 140)],
    "link_lost":       [(880, 90), (440, 180)],
    "link_restored":   [(550, 80), (770, 80), (990, 120)],
    "mixer_unreachable": [(440, 120), (0, 60), (440, 120), (0, 60), (440, 200)],
    "dsp_bypass":      [(1200, 60), (0, 40), (1200, 60)],   # SAFE-1 fired
    "rider_offline":   [(700, 100), (500, 160)],
    "identify":        [(880, 80), (0, 40), (880, 80), (0, 40), (1320, 160)],  # "this one"
    "volume":          [(990, 40)],                                    # level changed
    "ptt_on":          [(660, 40), (880, 60)],                          # gate forced open
    "ptt_off":         [(880, 40), (660, 60)],                          # back to VAD
}


def render(name: str) -> np.ndarray:
    segs = []
    for f, ms in SPECS[name]:
        n = int(FS * ms / 1000)
        if f == 0:
            segs.append(np.zeros(n, dtype=np.int16))
        else:
            x = tone(f, ms / 1000, FS, level_db=-14.0).astype(np.float64)
            ramp = min(64, len(x) // 4)
            env = np.ones(len(x)); env[:ramp] = np.linspace(0, 1, ramp); env[-ramp:] = np.linspace(1, 0, ramp)
            segs.append((x * env).astype(np.int16))
    return np.concatenate(segs)


def main(outdir: str = "common/earcons"):
    os.makedirs(outdir, exist_ok=True)
    for name in SPECS:
        write_wav(os.path.join(outdir, f"{name}.wav"), render(name))
    print(f"earcons -> {outdir}: {', '.join(SPECS)}")


if __name__ == "__main__":
    main()
