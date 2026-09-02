"""pymixer (DR-002): asyncio N-1 mixing server for plain Opus/RTP.

One uplink socket (:MIXER_RTP_PORT); participants identified by SSRC
(DR-005). 60 ms tick: pop each participant's next frame (2-deep reorder,
Opus PLC on short gaps), sum per room with per-talker gains, add the
broadcast source once per foreign room, then per-listener encode of
(room_sum - self + broadcast) and send to the listener's down port.
N-1 is arithmetic here — the reason a mixer beats a forwarder (INV-6).
"""
from __future__ import annotations
import asyncio
import numpy as np

from common.audio import FRAME, FRAME_MS, RTP_TS_STEP
from common.opusbind import Encoder, Decoder
from common.protocol import rtp_pack, rtp_unpack, MIXER_RTP_PORT
from bridge.io_adapters import UdpPort
from base.mixer.api import MixerAPI


class _Part:
    def __init__(self, pid, room, out_addr, gain, role, ssrc):
        self.pid, self.room, self.out_addr = pid, room, out_addr
        self.gain, self.role, self.ssrc = gain, role, ssrc
        self.dec = Decoder()
        self.enc = Encoder() if out_addr else None
        self.buf: dict[int, bytes] = {}
        self.next_seq: int | None = None
        self.seq_out = 0
        self.ts_out = 0
        self.active = False
        self.last_rms = 0.0
        self.pkts = 0
        self.plc = 0
        self.last_rx = 0.0


class PyMixer(MixerAPI):
    def __init__(self, rtp_port: int = MIXER_RTP_PORT):
        self.rtp_port = rtp_port
        self.parts: dict[str, _Part] = {}
        self._by_ssrc: dict[int, _Part] = {}
        self.broadcast_pid: str | None = None
        self.udp = UdpPort()
        self._task = None
        self.ticks = 0

    async def start(self) -> None:
        self.udp.on_packet = self._rx
        await self.udp.bind("127.0.0.1", self.rtp_port)
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.udp.close()

    # -- MixerAPI --
    def add_participant(self, pid, room, out_addr, gain=100, role="rider") -> None:
        from common.protocol import ssrc_of
        p = _Part(pid, room, out_addr, gain, role, ssrc_of(pid))
        self.parts[pid] = p
        self._by_ssrc[p.ssrc] = p

    def remove_participant(self, pid) -> None:
        p = self.parts.pop(pid, None)
        if p:
            self._by_ssrc.pop(p.ssrc, None)

    def move(self, pid, room) -> None:
        self.parts[pid].room = room

    def set_gain(self, pid, gain) -> None:
        self.parts[pid].gain = int(gain)

    def set_broadcast(self, pid) -> None:
        self.broadcast_pid = pid

    def stats(self) -> dict:
        try:
            now = asyncio.get_event_loop().time()
        except RuntimeError:
            now = 0.0
        return {pid: {"room": p.room, "gain": p.gain, "active": p.active,
                      "rms": round(p.last_rms, 1), "pkts": p.pkts, "plc": p.plc,
                      "rx_age_s": round(now - p.last_rx, 1) if p.last_rx else None}
                for pid, p in self.parts.items()}

    # -- internals --
    def _rx(self, data: bytes, addr) -> None:
        try:
            _, seq, _, ssrc, payload = rtp_unpack(data)
        except ValueError:
            return
        p = self._by_ssrc.get(ssrc)
        if p is None:
            return
        p.buf[seq] = payload
        p.pkts += 1
        p.last_rx = asyncio.get_event_loop().time()
        if p.next_seq is None:
            p.next_seq = seq
        while len(p.buf) > 24:   # >= gate pre-roll burst (15) + margin
            p.buf.pop(min(p.buf), None)

    def _pop_frame(self, p: _Part) -> np.ndarray:
        if p.next_seq is not None and p.next_seq in p.buf:
            f = p.dec.decode(p.buf.pop(p.next_seq), FRAME)
            p.next_seq = (p.next_seq + 1) & 0xFFFF
            p.active = True
        elif p.next_seq is not None and p.buf:
            f = p.dec.decode(None, FRAME)                # PLC over a gap
            p.plc += 1
            p.next_seq = (p.next_seq + 1) & 0xFFFF
        else:
            f = np.zeros(FRAME, dtype=np.int16)          # gated / absent = silence
            p.active = False
        p.last_rms = float(np.sqrt(np.mean(f.astype(np.float64) ** 2)))
        return f

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            decoded = {pid: self._pop_frame(p).astype(np.int32) * p.gain // 100
                       for pid, p in self.parts.items()}
            rooms: dict[str, np.ndarray] = {}
            for pid, p in self.parts.items():
                rooms.setdefault(p.room, np.zeros(FRAME, np.int32))
                rooms[p.room] += decoded[pid]
            bcast = None
            bp = self.parts.get(self.broadcast_pid) if self.broadcast_pid else None
            if bp is not None:
                bcast = decoded[self.broadcast_pid]
            for pid, p in self.parts.items():
                if p.out_addr is None or p.enc is None:
                    continue
                mix = rooms[p.room] - decoded[pid]        # N-1 (INV-6)
                if bcast is not None and bp.room != p.room and pid != self.broadcast_pid:
                    mix = mix + bcast
                pcm = np.clip(mix, -32768, 32767).astype(np.int16)
                pkt = rtp_pack(p.seq_out, p.ts_out, p.ssrc, p.enc.encode(pcm))
                p.seq_out = (p.seq_out + 1) & 0xFFFF
                p.ts_out = (p.ts_out + RTP_TS_STEP) & 0xFFFFFFFF
                self.udp.send(pkt, p.out_addr)
            self.ticks += 1
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - loop.time()))
