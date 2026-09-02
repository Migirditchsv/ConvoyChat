"""Labeled fixtures (plan T-0): espeak hazard phrases + synthetic wind,
passed through a headset simulator (band-limit + expander gate) so the gate
is tuned on post-chain audio, not studio audio. Real captures from the F-03
recorder replace these later (assumption register)."""
from __future__ import annotations
import json
import hashlib
import os
import subprocess
import numpy as np

from common.audio import FS, FRAME, read_wav, write_wav, resample_to, normalize_dbfs, dbfs
from common.dsp import Biquad

DATA = os.path.join(os.path.dirname(__file__), "data")
EXT_SPEECH = os.path.join(os.path.dirname(__file__), "ext", "speech")
EXT_WIND = os.path.join(os.path.dirname(__file__), "ext", "wind")
FIXTURE_REV = 5   # bump to force regeneration when synthesis changes
PHRASES = ["gravel on the left", "car pulling out ahead", "slowing hard now",
           "fuel stop in ten miles", "pothole right side", "all clear come through"]
# wind level vs speed (dBFS), speech at -20 dBFS -> SNR +12 / 0 / -6
WIND_DB = {50: -32.0, 90: -20.0, 120: -14.0}
SPEECH_DB = -20.0


def _espeak(text: str) -> np.ndarray:
    tmp = os.path.join(DATA, "_tmp.wav")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", tmp, text],
                   check=True, capture_output=True)
    x, fs = read_wav(tmp)
    os.remove(tmp)
    return normalize_dbfs(resample_to(x, fs), SPEECH_DB)


def _speech_clips() -> tuple[list[np.ndarray], str]:
    """Real recorded speech from sim/ext/speech/ when present (committed
    Harvard-sentence utterances, user-supplied public sample audio); espeak
    synthesis as the hermetic fallback."""
    if os.path.isdir(EXT_SPEECH):
        wavs = sorted(f for f in os.listdir(EXT_SPEECH) if f.endswith(".wav"))
        if wavs:
            clips = []
            for f in wavs:
                x, fs = read_wav(os.path.join(EXT_SPEECH, f))
                clips.append(normalize_dbfs(resample_to(x, fs), SPEECH_DB))
            return clips, f"ext:{len(clips)}"
    return [_espeak(p) for p in PHRASES], "espeak"


def _wind_take(dur_s: float, speed_kmh: int, seed: int = 7) -> tuple[np.ndarray, str]:
    """Real wind recordings from sim/ext/wind/ when present (drop in e.g. the
    RWTH IKS wind-noise database, MIT-licensed — see README); synthetic
    red-tilted gusty noise as the hermetic fallback. Real takes are tiled/cut
    to duration and level-set by the speed table."""
    if os.path.isdir(EXT_WIND):
        wavs = sorted(f for f in os.listdir(EXT_WIND) if f.endswith(".wav"))
        if wavs:
            rng = np.random.default_rng(seed + speed_kmh)
            x, fs = read_wav(os.path.join(EXT_WIND, wavs[rng.integers(len(wavs))]))
            x = resample_to(x, fs)
            n = int(dur_s * FS)
            reps = int(np.ceil(n / len(x)))
            x = np.tile(x, reps)[:n]
            return normalize_dbfs(x, WIND_DB[speed_kmh]), "ext"
    return synth_wind(dur_s, speed_kmh, seed), "synth"


def synth_wind(dur_s: float, speed_kmh: int, seed: int = 7) -> np.ndarray:
    """Red-tilted noise + gust amplitude modulation (primer §05 character)."""
    rng = np.random.default_rng(seed + speed_kmh)
    n = int(dur_s * FS)
    x = rng.standard_normal(n)
    lp1 = Biquad("lp", 400, FS); lp2 = Biquad("lp", 800, FS)
    x = lp2.process(lp1.process((x * 8000).astype(np.int16))).astype(np.float64)
    g = rng.standard_normal(n // 800 + 2)
    g = np.interp(np.arange(n) / 800, np.arange(len(g)), g)
    gust = 1.0 + 0.6 * np.tanh(np.convolve(g, np.ones(50) / 50, "same"))
    return normalize_dbfs((x * gust).astype(np.int16), WIND_DB[speed_kmh])


def headset_sim(x: np.ndarray) -> np.ndarray:
    """Vendor-chain caricature: HFP band-limit 300-3400 + downward expander."""
    hp1, hp2 = Biquad("hp", 300, FS), Biquad("hp", 300, FS)
    lp1, lp2 = Biquad("lp", 3400, FS), Biquad("lp", 3400, FS)
    y = lp2.process(lp1.process(hp2.process(hp1.process(x)))).astype(np.float64)
    out = np.empty_like(y)
    gain, hop = 1.0, 480
    for i in range(0, len(y), hop):
        seg = y[i:i + hop]
        r = 20 * np.log10(max(np.sqrt(np.mean(seg ** 2)), 1e-9) / 32768)
        target = 1.0 if r > -45 else 0.15
        gain += (target - gain) * 0.3
        out[i:i + hop] = seg * gain
    return np.clip(out, -32768, 32767).astype(np.int16)


def _post_sim_level(x: np.ndarray) -> float:
    """Level after the headset chain. Spectral shape decides how much a
    signal loses to the 300-3400 band-limit (a deep voice loses >13 dB, our
    synthetic wind ~5 dB), so SNR tiers are only meaningful if levels are
    calibrated POST-band-limit — which is also where a real headset's AGC
    acts. Measured per clip, applied as pre-mix makeup gain."""
    return dbfs(headset_sim(x))


def _calibrate(x: np.ndarray, target_post_db: float) -> np.ndarray:
    g = target_post_db - _post_sim_level(x)
    y = x.astype(np.float64) * (10 ** (g / 20))
    return np.clip(y, -32768, 32767).astype(np.int16)


def build(force: bool = False) -> dict:
    os.makedirs(DATA, exist_ok=True)
    manifest_path = os.path.join(DATA, "manifest.json")
    if os.path.exists(manifest_path) and not force:
        with open(manifest_path) as f:
            m = json.load(f)
        if m.get("rev") == FIXTURE_REV:
            return m
    print("building fixtures (first run takes ~30-60 s)...", flush=True)
    speech, speech_src = _speech_clips()
    print(f"  speech source: {speech_src} ({len(speech)} clips)", flush=True)
    speech = [_calibrate(sp, SPEECH_DB) for sp in speech]
    sets = {}
    wind_src = "synth"
    for speed, _ in WIND_DB.items():
        print(f"  wind tier {speed} km/h: 60 s labeled mix + 300 s wind-only take...", flush=True)
        dur = 60.0
        wind, wind_src = _wind_take(dur, speed)
        wind = _calibrate(wind, WIND_DB[speed])
        # long wind-only take for S-04 rate statistics, faded in to kill the
        # filter/expander warmup transient (a click Silero reads as an onset)
        wl, _ = _wind_take(300.0, speed, seed=99)
        wl = _calibrate(wl, WIND_DB[speed]).astype(np.float64)
        nramp = int(0.5 * FS); wl[:nramp] *= np.linspace(0, 1, nramp)
        write_wav(os.path.join(DATA, f"windlong_{speed}.wav"),
                  headset_sim(wl.astype(np.int16)))
        mix = wind.astype(np.float64).copy()
        labels = np.zeros(int(dur * FS) // FRAME, dtype=bool)
        onsets = []
        t = int(3.0 * FS)
        for sp in speech:
            if t + len(sp) >= len(mix):
                break
            mix[t:t + len(sp)] += sp.astype(np.float64)
            # label only VOICED frames of the clean clip (not silent lead-in/
            # tail-out), so gate metrics measure speech, not clip padding
            first = None
            for k in range(len(sp) // FRAME):
                fr = sp[k * FRAME:(k + 1) * FRAME]
                if dbfs(fr) > -45.0:
                    labels[t // FRAME + k] = True
                    if first is None:
                        first = t // FRAME + k
            if first is not None:
                onsets.append(first)
            t += len(sp) + int(4.0 * FS)
        clean_mix = np.clip(mix, -32768, 32767).astype(np.int16)
        post = headset_sim(clean_mix)
        wind_only_post = headset_sim(wind)
        write_wav(os.path.join(DATA, f"mix_{speed}.wav"), post)
        write_wav(os.path.join(DATA, f"wind_{speed}.wav"), wind_only_post)
        np.save(os.path.join(DATA, f"labels_{speed}.npy"), labels)
        sets[str(speed)] = {"mix": f"mix_{speed}.wav", "wind": f"wind_{speed}.wav",
                            "labels": f"labels_{speed}.npy", "onsets": onsets,
                            "windlong": f"windlong_{speed}.wav",
                            "n_frames": int(len(labels))}
    # one clean speech clip for convoy scripts + probes
    clip = headset_sim(speech[0])
    write_wav(os.path.join(DATA, "phrase.wav"), clip)
    sets["phrase"] = {"wav": "phrase.wav",
                      "text": PHRASES[0] if speech_src == "espeak" else "harvard_00"}
    h = hashlib.sha256(json.dumps(sets, sort_keys=True).encode()).hexdigest()[:12]
    manifest = {"rev": FIXTURE_REV, "hash": h, "fs": FS, "sets": sets,
                "speech_source": speech_src, "wind_source": wind_src}
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"fixtures built -> {DATA} ({h})")
    return manifest


def load(name: str) -> np.ndarray:
    x, fs = read_wav(os.path.join(DATA, name))
    assert fs == FS
    return x


def load_labels(speed: int) -> np.ndarray:
    return np.load(os.path.join(DATA, f"labels_{speed}.npy"))


if __name__ == "__main__":
    build()
