"""systemd notify without a dependency: READY=1 at start, WATCHDOG=1 while
the tick is alive. With `Type=notify` + `WatchdogSec=` in the unit, a hung
process (tick stopped, event loop wedged) is restarted by systemd — self
repair for the cases a try/except cannot see. No NOTIFY_SOCKET -> no-op."""
from __future__ import annotations
import os
import socket


class SdNotify:
    def __init__(self, path: str | None = None):
        self.path = path if path is not None else os.environ.get("NOTIFY_SOCKET")
        self.sent: list[str] = []
        self._sock = None
        if self.path:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                addr = self.path
                if addr.startswith("@"):
                    addr = "\0" + addr[1:]
                self._sock.connect(addr)
            except OSError:
                self._sock = None

    @property
    def active(self) -> bool:
        return self._sock is not None

    def notify(self, state: str) -> bool:
        self.sent.append(state)
        if self._sock is None:
            return False
        try:
            self._sock.send(state.encode())
            return True
        except OSError:
            return False

    def ready(self) -> bool:
        return self.notify("READY=1")

    def watchdog(self) -> bool:
        return self.notify("WATCHDOG=1")

    def status(self, text: str) -> bool:
        return self.notify(f"STATUS={text[:120]}")
