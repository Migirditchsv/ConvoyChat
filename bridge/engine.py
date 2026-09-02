"""Bridge engine: binds chains to IO on a 60 ms tick; used verbatim by the
virtual convoy (a sim bridge IS the real engine on fake IO — plan T-0)."""
from __future__ import annotations
import asyncio

import numpy as np

from common.audio import FRAME_MS
from common.dsp import tone
from common.protocol import MIXER_RTP_PORT
from bridge.audio.chain import UplinkChain, DownlinkChain
from bridge.io_adapters import UdpPort


class BridgeEngine:
    def __init__(self, node_id: str, source, sink, mixer_addr=("127.0.0.1", MIXER_RTP_PORT),
                 down_port: int = 0, prefer_silero: bool = True, on_vad=None):
        self.node_id = node_id
        self.source, self.sink = source, sink
        self.mixer_addr = mixer_addr
        self.down_port = down_port
        self.up = UplinkChain(node_id, prefer_silero=prefer_silero, on_vad=on_vad,
                              on_degrade=self._on_degrade)
        self.down = DownlinkChain()
        self.udp = UdpPort()
        self.stats = {"tx_pkts": 0, "rx_pkts": 0, "vad_open": False}
        self.hb_tone = False           # QoL: periodic soft tick = "link alive"
        self._hb_every = 5000 // FRAME_MS
        self._frame_n = 0
        self._task = None

    def _on_degrade(self, mode: str) -> None:
        self.stats["vad_mode"] = mode

    async def start(self):
        self.udp.on_packet = lambda data, addr: (self.down.push_rtp(data),
                                                 self.stats.__setitem__("rx_pkts", self.stats["rx_pkts"] + 1))
        await self.udp.bind("127.0.0.1", self.down_port)
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            frame = self.source.read()
            if frame is None:
                break
            for pkt in self.up.feed(frame):
                self.udp.send(pkt, self.mixer_addr)
                self.stats["tx_pkts"] += 1
            self.stats["vad_open"] = self.up.gate.is_open
            self._frame_n += 1
            if self.hb_tone and self._frame_n % self._hb_every == 0:
                self.down.queue_earcon(tone(880, 0.03, level_db=-30))
            self.sink.write(self.down.pull())
            next_t += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_t - loop.time()))

    # -- remote-adjustable QoL controls (driven by the agent) --
    def set_volume(self, pct: int) -> None:
        self.down.volume_pct = int(max(10, min(200, pct)))

    def adjust_volume(self, delta_pct: int) -> int:
        self.set_volume(self.down.volume_pct + delta_pct)
        return self.down.volume_pct

    def set_hb_tone(self, on: bool) -> None:
        self.hb_tone = bool(on)

    async def wait(self):
        if self._task:
            await self._task

    def stop(self):
        if self._task:
            self._task.cancel()
        self.udp.close()
