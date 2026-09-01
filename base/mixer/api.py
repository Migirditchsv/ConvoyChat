"""MixerAPI (plan S-1): the seam that keeps pymixer/Janus interchangeable.
S-07 is the conformance suite; orchestrator, UI and bridges see only this."""
from __future__ import annotations
from abc import ABC, abstractmethod


class MixerAPI(ABC):
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @abstractmethod
    def add_participant(self, pid: str, room: str, out_addr: tuple[str, int] | None,
                        gain: int = 100, role: str = "rider") -> None: ...
    @abstractmethod
    def remove_participant(self, pid: str) -> None: ...
    @abstractmethod
    def move(self, pid: str, room: str) -> None: ...
    @abstractmethod
    def set_gain(self, pid: str, gain: int) -> None:
        """Input gain, percent (0..200) — a per-talker bus gain, AudioBridge-style."""
    @abstractmethod
    def set_broadcast(self, pid: str | None) -> None:
        """This participant's audio is added ONCE to every room (lead-tee, plan S-2)."""
    @abstractmethod
    def stats(self) -> dict: ...
