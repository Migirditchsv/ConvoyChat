"""Text -> spoken announcement (plan S-4).

Engine selection: Piper (natural voice) when its CLI + a voice model are
installed; espeak-ng otherwise — always present because fixtures need it,
so text->speech works out of the box and upgrades in place. Rendering runs
in a thread executor so the mixer's 60 ms tick never waits on synthesis.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import subprocess
import tempfile

import numpy as np

from common.audio import FS, read_wav, resample_to, normalize_dbfs

ANNOUNCE_DB = -16.0
PIPER_VOICE = os.environ.get("CONVOY_PIPER_VOICE", "")   # path to .onnx voice


def engine_name() -> str:
    if shutil.which("piper") and PIPER_VOICE and os.path.exists(PIPER_VOICE):
        return "piper"
    if shutil.which("espeak-ng"):
        return "espeak"
    return "none"


def _render_blocking(text: str) -> np.ndarray:
    eng = engine_name()
    if eng == "none":
        raise RuntimeError("no TTS engine: install espeak-ng (or piper + voice)")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        if eng == "piper":
            subprocess.run(["piper", "--model", PIPER_VOICE, "--output_file", tmp],
                           input=text.encode(), check=True, capture_output=True)
        else:
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "160", "-w", tmp, text],
                           check=True, capture_output=True)
        x, fs = read_wav(tmp)
        return normalize_dbfs(resample_to(x, fs, FS), ANNOUNCE_DB)
    finally:
        os.unlink(tmp)


async def render(text: str) -> np.ndarray:
    return await asyncio.get_running_loop().run_in_executor(None, _render_blocking, text)
