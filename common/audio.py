"""Frame conventions and small audio utilities (stdlib wave IO, no soundfile).

System-wide invariants (plan C-0): internal audio is mono int16 @16 kHz,
FRAME = 960 samples = 60 ms (INV-8). RTP timestamps use the 48 kHz Opus
clock: +2880 per frame (RFC 7587).
"""
from __future__ import annotations
import wave
import numpy as np

FS = 16000
FRAME = 960            # samples per 60 ms frame
FRAME_MS = 60
RTP_TS_STEP = 2880     # 60 ms in the 48 kHz RTP clock


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2, "16-bit PCM only"
        raw = w.readframes(w.getnframes())
        x = np.frombuffer(raw, dtype=np.int16)
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
        return x, w.getframerate()


def write_wav(path: str, x: np.ndarray, fs: int = FS) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs)
        w.writeframes(np.asarray(x, dtype=np.int16).tobytes())


def resample_to(x: np.ndarray, fs_in: int, fs_out: int = FS) -> np.ndarray:
    """Linear-interp resampler. Fixture-grade (DR-001); fine for speech/VAD."""
    if fs_in == fs_out:
        return x.astype(np.int16)
    n_out = int(round(len(x) * fs_out / fs_in))
    t_in = np.arange(len(x), dtype=np.float64)
    t_out = np.linspace(0, len(x) - 1, n_out)
    return np.interp(t_out, t_in, x.astype(np.float64)).astype(np.int16)


def frames(x: np.ndarray, frame: int = FRAME):
    n = (len(x) // frame) * frame
    for i in range(0, n, frame):
        yield x[i:i + frame]


def dbfs(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    r = np.sqrt(np.mean(x.astype(np.float64) ** 2))
    return 20 * np.log10(max(r, 1e-9) / 32768.0)


def normalize_dbfs(x: np.ndarray, target_db: float) -> np.ndarray:
    cur = dbfs(x)
    g = 10 ** ((target_db - cur) / 20)
    return np.clip(x.astype(np.float64) * g, -32768, 32767).astype(np.int16)
