"""Base-side radio gateway: the room goes out over a wired HT, and what the
HT receives comes back in through the real gate and mixer (DR-011).

The gateway is a BridgeEngine whose mouth is the rig's receiver and whose
ear is the rig's microphone: received RF speech -> HPF/VAD/gate -> Opus ->
mixer as participant `pid` (so it ducks music like any rider and shows on
the ops page); the mixer's N-1 mix for `pid` -> RadioLink (keyed only while
there is speech, hang, ID, time-out) -> rig mic. Music is excluded from
that mix by MixerAPI.set_exclude — nothing but voice goes on the air."""
from __future__ import annotations
import numpy as np

from common.audio import FRAME
from common.radio import RadioLink
from bridge.engine import BridgeEngine


class _RigRx:
    """Engine source: runs the link discipline once per tick with the mix the
    sink stored last tick, then returns squelched RF audio for the gate."""
    def __init__(self, gw):
        self.gw = gw

    def read(self) -> np.ndarray:
        gw = self.gw
        rx = gw.rig_source.read()
        out, ear = gw.link.process(gw.last_mix, rx)
        gw.last_mix = None
        if out is not None:
            gw.rig_sink.write(out)
        return ear if ear is not None else np.zeros(FRAME, np.int16)


class _RigTx:
    """Engine sink: the mixer's N-1 mix for the gateway, held for next tick."""
    def __init__(self, gw):
        self.gw = gw

    def write(self, frame: np.ndarray) -> None:
        self.gw.last_mix = frame


class RadioGateway:
    def __init__(self, pid: str, link: RadioLink, rig_source, rig_sink,
                 mixer_addr=("127.0.0.1", 5100), down_port: int = 0,
                 prefer_silero: bool = True, on_vad=None):
        self.pid = pid
        self.link = link
        self.rig_source, self.rig_sink = rig_source, rig_sink
        self.last_mix: np.ndarray | None = None
        self.engine = BridgeEngine(pid, _RigRx(self), _RigTx(self), mixer_addr=mixer_addr,
                                   down_port=down_port, prefer_silero=prefer_silero,
                                   on_vad=on_vad)

    async def start(self) -> None:
        for dev in (self.rig_source, self.rig_sink):
            if hasattr(dev, "start"):
                dev.start()
        await self.engine.start()

    def stop(self) -> None:
        self.engine.stop()
        for dev in (self.rig_source, self.rig_sink):
            if hasattr(dev, "stop"):
                dev.stop()

    def stats(self) -> dict:
        return {"pid": self.pid, **self.link.stats(),
                "rx_pkts_to_mixer": self.engine.stats["tx_pkts"],
                "vad_mode": self.engine.up.vad.mode}
