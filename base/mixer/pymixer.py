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
from common.protocol import rtp_pack, rtp_unpack, ssrc_of, MIXER_RTP_PORT
from bridge.io_adapters import UdpPort
from base.mixer.api import MixerAPI

REORDER_DEPTH = 24        # packets held per talker: gate pre-roll burst (15) + margin
RESYNC_AFTER_PLC = 3      # consecutive PLC frames before we jump to the oldest held packet


def seq_ahead(seq: int, ref: int) -> int:
    """How far `seq` is ahead of `ref` in 16-bit serial arithmetic (RFC 1982).
    Wrap-safe: seq_ahead(1, 65535) == 2. Used instead of min()/max() on raw
    sequence numbers, which invert at the 65536 boundary (~65 min of downlink)."""
    return (seq - ref) & 0xFFFF


def oldest_seq(buf: dict[int, bytes], ref: int) -> int:
    """The held packet closest ahead of `ref` — the right one to resume on."""
    return min(buf, key=lambda s: seq_ahead(s, ref))


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
        self.plc_run = 0          # consecutive PLC frames (drives resync)
        self.resyncs = 0
        self.learned_host = False # out_addr host came from the uplink source (symmetric RTP)
        self.exclude: set[str] = set()   # participants this listener never hears


class PyMixer(MixerAPI):
    def __init__(self, rtp_port: int = MIXER_RTP_PORT, bind_host: str = "127.0.0.1",
                 learn_peer: bool = True):
        """bind_host: "0.0.0.0" for a LAN base (real bridges), loopback for sim/tests.
        learn_peer: a participant's downlink is sent to the host its uplink RTP
        arrives from (symmetric RTP) — so the roster never needs bridge IPs."""
        self.rtp_port = rtp_port
        self.bind_host = bind_host
        self.learn_peer = learn_peer
        self.parts: dict[str, _Part] = {}
        self._by_ssrc: dict[int, _Part] = {}
        self.broadcast_pid: str | None = None
        self.udp = UdpPort()
        self._task = None
        self.ticks = 0

    async def start(self) -> None:
        self.udp.on_packet = self._rx
        await self.udp.bind(self.bind_host, self.rtp_port)
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.udp.close()

    # -- MixerAPI --
    def add_participant(self, pid, room, out_addr, gain=100, role="rider") -> None:
        old = self.parts.get(pid)
        p = _Part(pid, room, out_addr, gain, role, ssrc_of(pid))
        if old is not None and old.out_addr and out_addr and self.learn_peer:
            # re-populate (S-10 reattach, roster reload) keeps a learned peer host
            if old.out_addr[0] != out_addr[0] and old.learned_host:
                p.out_addr = (old.out_addr[0], out_addr[1])
                p.learned_host = True
        if old is not None:
            p.exclude = set(old.exclude)
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

    def set_exclude(self, pid, excluded) -> None:
        self.parts[pid].exclude = set(excluded) - {pid}

    def stats(self) -> dict:
        try:
            now = asyncio.get_event_loop().time()
        except RuntimeError:
            now = 0.0
        return {pid: {"room": p.room, "gain": p.gain, "active": p.active,
                      "rms": round(p.last_rms, 1), "pkts": p.pkts, "plc": p.plc,
                      "resyncs": p.resyncs, "exclude": sorted(p.exclude),
                      "peer": p.out_addr[0] if p.out_addr else None,
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
        if self.learn_peer and p.out_addr is not None and addr[0] != p.out_addr[0]:
            p.out_addr = (addr[0], p.out_addr[1])        # symmetric RTP: reply where it came from
            p.learned_host = True
        p.buf[seq] = payload
        p.pkts += 1
        p.last_rx = asyncio.get_event_loop().time()
        if p.next_seq is None:
            p.next_seq = seq
        while len(p.buf) > REORDER_DEPTH:
            p.buf.pop(oldest_seq(p.buf, p.next_seq), None)

    def _pop_frame(self, p: _Part) -> np.ndarray:
        if p.next_seq is not None and p.next_seq in p.buf:
            f = p.dec.decode(p.buf.pop(p.next_seq), FRAME)
            p.next_seq = (p.next_seq + 1) & 0xFFFF
            p.active = True
            p.plc_run = 0
        elif p.next_seq is not None and p.buf:
            p.plc_run += 1
            if p.plc_run <= RESYNC_AFTER_PLC:
                f = p.dec.decode(None, FRAME)            # PLC over a short gap
                p.plc += 1
                p.next_seq = (p.next_seq + 1) & 0xFFFF
            else:
                # the gap outran the buffer (blackout mid-sentence): jump to
                # the oldest held packet instead of concealing forever while
                # next_seq trails the live stream by a constant offset
                p.next_seq = oldest_seq(p.buf, p.next_seq)
                f = p.dec.decode(p.buf.pop(p.next_seq), FRAME)
                p.next_seq = (p.next_seq + 1) & 0xFFFF
                p.active = True
                p.plc_run = 0
                p.resyncs += 1
        else:
            f = np.zeros(FRAME, dtype=np.int16)          # gated / absent = silence
            p.active = False
            p.plc_run = 0
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
                for ex in p.exclude:                       # N-k: listener-specific mutes
                    xp = self.parts.get(ex)
                    if xp is not None and xp.room == p.room:
                        mix = mix - decoded[ex]
                if bcast is not None and bp.room != p.room and pid != self.broadcast_pid:
                    if self.broadcast_pid not in p.exclude:
                        mix = mix + bcast
                pcm = np.clip(mix, -32768, 32767).astype(np.int16)
                pkt = rtp_pack(p.seq_out, p.ts_out, p.ssrc, p.enc.encode(pcm))
                p.seq_out = (p.seq_out + 1) & 0xFFFF
                p.ts_out = (p.ts_out + RTP_TS_STEP) & 0xFFFFFFFF
                self.udp.send(pkt, p.out_addr)
            self.ticks += 1
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - loop.time()))
