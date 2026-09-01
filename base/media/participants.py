"""Media participants (plan S-4): anything that speaks Opus/RTP into the
mixer is a participant — music, TTS announcements, test tones. The 'virtual
audio cable' is a socket (INV-6 commentary)."""
from __future__ import annotations
import asyncio
import numpy as np

from common.audio import FRAME, FRAME_MS, RTP_TS_STEP, read_wav, resample_to
from common.dsp import tone, noise_band
from common.opusbind import Encoder
from common.protocol import rtp_pack, ssrc_of, MIXER_RTP_PORT
from bridge.io_adapters import UdpPort


class RtpSource:
    """Streams a PCM buffer (looped or once) to the mixer as participant `pid`."""
    def __init__(self, pid: str, pcm: np.ndarray, loop_audio: bool = True,
                 mixer_addr=("127.0.0.1", MIXER_RTP_PORT)):
        self.pid, self.pcm, self.loop_audio = pid, pcm, loop_audio
        self.mixer_addr = mixer_addr
        self.enc = Encoder(dtx=False)   # media must stream continuously
        self.ssrc = ssrc_of(pid)
        self.udp = UdpPort()
        self._task = None
        self._seq = self._ts = 0

    @classmethod
    def tone(cls, pid: str, freq: float = 440.0, level_db: float = -20.0, **kw):
        return cls(pid, tone(freq, 1.0, level_db=level_db), loop_audio=True, **kw)

    @classmethod
    def noise(cls, pid: str, f_lo: float, f_hi: float, level_db: float = -20.0,
              seed: int = 3, **kw):
        return cls(pid, noise_band(f_lo, f_hi, 2.0, level_db=level_db, seed=seed),
                   loop_audio=True, **kw)

    @classmethod
    def wav(cls, pid: str, path: str, **kw):
        x, fs = read_wav(path)
        return cls(pid, resample_to(x, fs), **kw)

    async def start(self):
        await self.udp.bind()
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        pos = 0
        while True:
            if pos + FRAME > len(self.pcm):
                if not self.loop_audio:
                    break
                pos = 0
            frame = self.pcm[pos:pos + FRAME]
            pos += FRAME
            pkt = rtp_pack(self._seq, self._ts, self.ssrc, self.enc.encode(frame))
            self._seq = (self._seq + 1) & 0xFFFF
            self._ts = (self._ts + RTP_TS_STEP) & 0xFFFFFFFF
            self.udp.send(pkt, self.mixer_addr)
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - loop.time()))

    def stop(self):
        if self._task:
            self._task.cancel()
        self.udp.close()
