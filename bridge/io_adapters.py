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
        tr = self.transport
        if tr is None or tr.is_closing():
            return                      # a delayed send after close() is not an error
        try:
            tr.sendto(data, addr)
        except (AttributeError, OSError):
            pass                        # transport torn down mid-shutdown

    def close(self) -> None:
        if self.transport:
            self.transport.close()


class CmdSource:
    """Real microphone: a shell command that writes raw s16le/16 kHz/mono to
    stdout (pw-record, arecord -t raw, gst-launch ... fdsink). A reader thread
    slices it into 60 ms frames. read() NEVER returns None — an underrun or a
    dead process yields silence so the engine tick keeps running and the
    supervisor (bridge/main) can restart the command. This is the DR-001 IO
    seam: the chain never knows whether the frame came from BT or a file."""
    BYTES = FRAME * 2

    def __init__(self, cmd: str, max_queue: int = 8):
        import queue
        import shlex
        self.cmd = cmd
        self.argv = shlex.split(cmd)
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=max_queue)
        self._proc = None
        self._thread = None
        self.underruns = 0
        self.frames = 0
        self.restarts = -1

    def start(self) -> None:
        import subprocess
        import threading
        self._proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        self.restarts += 1
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        import queue
        out = self._proc.stdout
        buf = b""
        while True:
            chunk = out.read(self.BYTES - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) == self.BYTES:
                try:
                    self._q.put_nowait(buf)
                except queue.Full:
                    try:
                        self._q.get_nowait()      # drop oldest: stay live
                    except queue.Empty:
                        pass
                    self._q.put_nowait(buf)
                buf = b""

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def read(self) -> np.ndarray | None:
        import queue
        try:
            raw = self._q.get_nowait()
            self.frames += 1
            return np.frombuffer(raw, dtype=np.int16).copy()
        except queue.Empty:
            self.underruns += 1
            return np.zeros(FRAME, dtype=np.int16)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()


class CmdSink:
    """Real speaker (helmet): a shell command that reads raw s16le/16 kHz/mono
    on stdin (pw-play, aplay -t raw). A broken pipe is recorded, not raised —
    the engine tick must never die because a headset dropped (SAFE-1 spirit)."""
    def __init__(self, cmd: str):
        import shlex
        self.cmd = cmd
        self.argv = shlex.split(cmd)
        self._proc = None
        self.errors = 0
        self.frames = 0
        self.restarts = -1

    def start(self) -> None:
        import subprocess
        self._proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      bufsize=0)
        self.restarts += 1

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def write(self, frame: np.ndarray) -> None:
        if not self.alive:
            self.errors += 1
            return
        try:
            self._proc.stdin.write(np.asarray(frame, dtype=np.int16).tobytes())
            self.frames += 1
        except (BrokenPipeError, OSError, ValueError):
            self.errors += 1

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.kill()
