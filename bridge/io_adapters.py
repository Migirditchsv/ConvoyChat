"""IO adapters (DR-001): arrays and UDP now; pw-cat/ALSA shell lands at M2."""
from __future__ import annotations
import asyncio
import numpy as np

from common.audio import FRAME


class ArraySource:
    """Plays a schedule of (start_frame, pcm) over silence — sim rider's mouth."""
    def __init__(self, total_frames: int, clips: list[tuple[int, np.ndarray]] = ()):
        self.total = total_frames
        self._timeline = np.zeros(total_frames * FRAME, dtype=np.int16)
        for start_frame, pcm in clips:
            i0 = start_frame * FRAME
            i1 = min(i0 + len(pcm), len(self._timeline))
            self._timeline[i0:i1] = pcm[: i1 - i0]
        self._pos = 0

    def read(self) -> np.ndarray | None:
        if self._pos >= self.total:
            return None
        f = self._timeline[self._pos * FRAME:(self._pos + 1) * FRAME]
        self._pos += 1
        return f


class ArraySink:
    """Records downlink — sim rider's ear."""
    def __init__(self):
        self.frames: list[np.ndarray] = []

    def write(self, frame: np.ndarray) -> None:
        self.frames.append(frame.copy())

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, np.int16)


class UdpPort:
    """Tiny asyncio datagram wrapper used by bridges, mixer and media."""
    def __init__(self):
        self.transport = None
        self.on_packet = lambda data, addr: None

    class _Proto(asyncio.DatagramProtocol):
        def __init__(self, outer): self.o = outer
        def connection_made(self, tr): self.o.transport = tr
        def datagram_received(self, data, addr): self.o.on_packet(data, addr)

    async def bind(self, host: str = "127.0.0.1", port: int = 0) -> int:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self._Proto(self),
                                            local_addr=(host, port))
        return self.transport.get_extra_info("sockname")[1]

    def send(self, data: bytes, addr) -> None:
        if self.transport:
            self.transport.sendto(data, addr)

    def close(self) -> None:
        if self.transport:
            self.transport.close()
