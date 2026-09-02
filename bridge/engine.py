"""Bridge engine: binds chains to IO on a 60 ms tick; used verbatim by the
virtual convoy (a sim bridge IS the real engine on fake IO — plan T-0)."""
from __future__ import annotations
import asyncio

import numpy as np

from common.audio import FRAME_MS
from common.dsp import tone
from common import earcons
from common.protocol import MIXER_RTP_PORT
from bridge.audio.chain import UplinkChain, DownlinkChain
from bridge.io_adapters import UdpPort


class BridgeEngine:
    def __init__(self, node_id: str, source, sink, mixer_addr=("127.0.0.1", MIXER_RTP_PORT),
                 down_port: int = 0, prefer_silero: bool = True, on_vad=None,
                 bind_host: str = "127.0.0.1"):
        """bind_host: where the downlink listens — "0.0.0.0" on a real bridge
        (the mixer is another machine), loopback in sim/tests."""
        self.node_id = node_id
        self.source, self.sink = source, sink
        self.mixer_addr = mixer_addr
        self.down_port = down_port
        self.bind_host = bind_host
        self.up = UplinkChain(node_id, prefer_silero=prefer_silero, on_vad=on_vad,
                              on_degrade=self._on_degrade)
        self.vad_listeners: list = []    # extra on_vad(open) hooks (the agent forwards to base)
        self.up.on_vad = self._fan_vad
        self._user_on_vad = on_vad or (lambda open_: None)
        self.down = DownlinkChain()
        self.udp = UdpPort()
        self.stats = {"tx_pkts": 0, "rx_pkts": 0, "vad_open": False, "ptt": False,
                      "vad_mode": self.up.vad.mode}
        self.hb_tone = False           # QoL: periodic soft tick = "link alive"
        self._hb_every = 5000 // FRAME_MS
        self._frame_n = 0
        self._task = None
        self._w_decoded = self._w_concealed = 0
        self._loss_cache: float | None = None
        self._loss_at = 0.0
        self._ptt_frames_left = 0     # dead-man: PTT releases itself unless refreshed
        self.radio = None             # bridge.radio.RadioFailover, attached by bridge/main
        self.link_up = True           # base reachable over Wi-Fi (bridge/main maintains)

    def _on_degrade(self, mode: str) -> None:
        self.stats["vad_mode"] = mode

    def _fan_vad(self, open_: bool) -> None:
        self._user_on_vad(open_)
        for cb in list(self.vad_listeners):
            try:
                cb(open_)
            except Exception:
                pass

    # -- push-to-talk (rider phone page): bypasses the classifier while held.
    # The measured 120 km/h gap (DR-008) is closed by the rider's thumb, not
    # by lowering thresholds into gust territory. --
    PTT_HOLD_S = 6.0      # the phone re-sends `ptt on` every ~2 s while held

    def set_ptt(self, on: bool, hold_s: float | None = None) -> None:
        """PTT is a dead-man switch: `on` arms the gate for hold_s and must be
        refreshed while the thumb is down. If the control link drops with the
        button held, the gate closes itself instead of transmitting wind until
        the link returns (the release command would never arrive)."""
        if on:
            self.up.gate.force_open = True
            self.stats["ptt"] = True
            self._ptt_frames_left = max(1, int((hold_s or self.PTT_HOLD_S) * 1000 / FRAME_MS))
        else:
            self._release_ptt(auto=False)

    def _release_ptt(self, auto: bool) -> None:
        was = self.up.gate.force_open
        self.up.gate.force_open = False
        self.stats["ptt"] = False
        self._ptt_frames_left = 0
        if auto and was:
            self.stats["ptt_autorelease"] = self.stats.get("ptt_autorelease", 0) + 1
            self.down.queue_earcon(earcons.render("ptt_off"))    # SAFE-2: audible

    async def start(self):
        self.udp.on_packet = lambda data, addr: (self.down.push_rtp(data),
                                                 self.stats.__setitem__("rx_pkts", self.stats["rx_pkts"] + 1))
        await self.udp.bind(self.bind_host, self.down_port)
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
            if self.radio is not None:
                try:
                    aux = self.radio.on_tick(self.up.last_tx, self.link_up,
                                             vad_mode=self.up.vad.mode,
                                             ptt=bool(self.stats.get("ptt")))
                except Exception:
                    aux = None                     # SAFE-1 spirit: the rig never stops the tick
                if aux is not None:
                    self.down.push_aux(aux)
            self.stats["vad_open"] = self.up.gate.is_open
            self._frame_n += 1
            if self.hb_tone and self._frame_n % self._hb_every == 0:
                self.down.queue_earcon(tone(880, 0.03, level_db=-30))
            if self._ptt_frames_left > 0:
                self._ptt_frames_left -= 1
                if self._ptt_frames_left == 0:
                    self._release_ptt(auto=True)
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

    def set_mixer_addr(self, addr: tuple[str, int]) -> None:
        """Re-target the uplink at runtime (failover to a tunnelled base)."""
        self.mixer_addr = (str(addr[0]), int(addr[1]))
        self.stats["mixer"] = f"{self.mixer_addr[0]}:{self.mixer_addr[1]}"

    def downlink_loss_pct(self, min_interval_s: float = 1.0) -> float | None:
        """Loss the rider hears over the last window (concealed / arrived+concealed).
        The window is re-cut at most every min_interval_s; callers inside that
        interval (heartbeat AND the eviction tick both ask) get the same answer
        instead of each seeing half a window. None until the mixer has sent."""
        import time
        now = time.monotonic()
        if now - self._loss_at < min_interval_s:
            return self._loss_cache
        d = self.down
        got, plc = d.decoded - self._w_decoded, d.concealed - self._w_concealed
        self._w_decoded, self._w_concealed = d.decoded, d.concealed
        self._loss_at = now
        self._loss_cache = None if got + plc == 0 else round(100.0 * plc / (got + plc), 1)
        return self._loss_cache

    async def wait(self):
        if self._task:
            await self._task

    def stop(self):
        if self._task:
            self._task.cancel()
        self.udp.close()
