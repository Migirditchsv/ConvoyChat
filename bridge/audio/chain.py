"""Uplink and downlink audio chains (plan B-2), IO-agnostic (DR-001).

Uplink : frame -> HPF(speed) -> SafeVad+Gate -> AGC -> Opus(DTX,FEC) -> RTP
Downlink: RTP -> reorder buffer -> Opus decode (PLC on gaps) -> + earcons
Gate closed = nothing transmitted (the DTX above DTX); a just-opened gate
flushes the pre-roll as a small burst the mixer's jitter buffer absorbs.
"""
from __future__ import annotations
from collections import deque
import numpy as np

from common.audio import FRAME, RTP_TS_STEP
from common.dsp import SpeedHPF, Agc
from common.opusbind import Encoder, Decoder
from common.protocol import rtp_pack, rtp_unpack, ssrc_of
from bridge.audio.vad import SafeVad
from bridge.audio.gate import SpeechGate


class UplinkChain:
    def __init__(self, node_id: str, fs: int = 16000, prefer_silero: bool = True,
                 on_vad=None, on_degrade=None):
        self.ssrc = ssrc_of(node_id)
        self.hpf = SpeedHPF(fs)
        self.vad = SafeVad(prefer_silero=prefer_silero, on_degrade=on_degrade)
        self.gate = SpeechGate(mode=self.vad.mode)
        self.agc = Agc()
        self.enc = Encoder(fs)
        self.on_vad = on_vad or (lambda open_: None)
        self.speed_kmh = 0.0
        self._seq = 0
        self._ts = 0
        self._was_open = False

    def set_speed(self, kmh: float) -> None:
        self.speed_kmh = kmh
        self.hpf.set_speed(kmh)

    def feed(self, frame: np.ndarray) -> list[bytes]:
        assert len(frame) == FRAME
        y = self.hpf.process(frame)
        p = self.vad.prob(y)
        tx, is_open, _ = self.gate.process(y, p, self.speed_kmh, self.vad.mode)
        if is_open != self._was_open:
            self._was_open = is_open
            self.on_vad(is_open)
        pkts = []
        for f in tx:
            f = self.agc.process(f)
            payload = self.enc.encode(f)
            pkts.append(rtp_pack(self._seq, self._ts, self.ssrc, payload))
            self._seq = (self._seq + 1) & 0xFFFF
            self._ts = (self._ts + RTP_TS_STEP) & 0xFFFFFFFF
        return pkts


class DownlinkChain:
    """Small reorder buffer + PLC. One frame pulled per 60 ms tick."""
    def __init__(self, fs: int = 16000, depth: int = 3):
        self.dec = Decoder(fs)
        self.depth = depth
        self.volume_pct = 100          # rider helmet volume (remote-adjustable)
        self._buf: dict[int, bytes] = {}
        self._next: int | None = None
        self._earcons: deque[np.ndarray] = deque()
        self._misses = 0

    def push_rtp(self, pkt: bytes) -> None:
        try:
            _, seq, _, _, payload = rtp_unpack(pkt)
        except ValueError:
            return
        self._buf[seq] = payload
        if self._next is None:
            self._next = seq
        while len(self._buf) > self.depth * 4:      # runaway guard
            self._buf.pop(min(self._buf), None)

    def queue_earcon(self, pcm: np.ndarray) -> None:
        for i in range(0, len(pcm), FRAME):
            f = pcm[i:i + FRAME]
            if len(f) < FRAME:
                f = np.pad(f, (0, FRAME - len(f)))
            self._earcons.append(f.astype(np.int16))

    def pull(self) -> np.ndarray:
        if self._next is not None and self._next in self._buf:
            out = self.dec.decode(self._buf.pop(self._next), FRAME)
            self._next = (self._next + 1) & 0xFFFF
            self._misses = 0
        elif self._next is not None and self._buf:
            self._misses += 1
            if self._misses <= 2:
                out = self.dec.decode(None, FRAME)          # PLC
                self._next = (self._next + 1) & 0xFFFF
            else:                                           # resync jump
                self._next = min(self._buf)
                out = self.dec.decode(self._buf.pop(self._next), FRAME)
                self._next = (self._next + 1) & 0xFFFF
                self._misses = 0
        else:
            out = np.zeros(FRAME, dtype=np.int16)
        if self.volume_pct != 100:
            out = np.clip(out.astype(np.int32) * self.volume_pct // 100,
                          -32768, 32767).astype(np.int16)
        if self._earcons:                     # earcons AFTER volume: audible even at vol 0
            e = self._earcons.popleft().astype(np.int32)
            out = np.clip(out.astype(np.int32) + e, -32768, 32767).astype(np.int16)
        return out
