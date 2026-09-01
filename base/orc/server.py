"""Orchestrator (plan S-2): roster + events -> MixerAPI state. ~300 lines is
the budget; the WS transport is a thin shell around the same core the tests
drive directly (S-08..S-10), so field and CI exercise identical logic."""
from __future__ import annotations
import asyncio
import json
import time

from common.protocol import make_msg, parse_msg, CONTROL_PORT
from common.roster import Roster
from base.mixer.api import MixerAPI
from base.orc import ladder


class Orchestrator:
    HANGOVER_S = 0.4
    SELF_MOVE_MAX_KMH = 8.0

    def __init__(self, roster: Roster, mixer: MixerAPI):
        self.roster = roster
        self.mixer = mixer
        self.node_status: dict[str, dict] = {}
        self.talking: set[str] = set()
        self.group_speed_kmh = 0.0
        self._hang: dict[str, asyncio.TimerHandle] = {}
        self._ui_clients: set = set()
        self.log: list[dict] = []
        self.populate()

    # -- lifecycle --
    def populate(self) -> None:
        """Idempotent: (re)apply roster to the mixer — also S-10's reattach."""
        for r in self.roster.riders.values():
            out = None if r.role == "music" else (r.ip, r.down_port)
            self.mixer.add_participant(r.id, r.rooms[0], out,
                                       gain=ladder.DEFAULT_GAINS[r.role], role=r.role)
        leads = self.roster.by_role("lead")
        self.mixer.set_broadcast(leads[0].id if leads else None)

    def reattach(self, mixer: MixerAPI) -> None:
        self.mixer = mixer
        self.populate()
        for pid in list(self.talking):
            self._apply_duck(pid)

    def _participants(self) -> dict[str, str]:
        return {r.id: r.role for r in self.roster.riders.values()}

    # -- events (callable directly by tests; WS shell routes here) --
    def on_vad(self, pid: str, is_open: bool) -> None:
        r = self.roster.riders.get(pid)
        if r is None:
            return
        if is_open:
            self.talking.add(pid)
            if (h := self._hang.pop(pid, None)):
                h.cancel()
            self._apply_duck(pid)
        else:
            loop = asyncio.get_event_loop()
            self._hang[pid] = loop.call_later(self.HANGOVER_S, self._end_talk, pid)

    def _apply_duck(self, pid: str) -> None:
        role = self.roster.riders[pid].role
        g = ladder.gains_for(role, self._participants())
        if g:
            g[pid] = 100
            for p, gain in g.items():
                self.mixer.set_gain(p, gain)
            self._event("duck", {"by": pid, "role": role})

    def _end_talk(self, pid: str) -> None:
        self.talking.discard(pid)
        self._hang.pop(pid, None)
        blockers = [p for p in self.talking
                    if ladder.DUCK.get(self.roster.riders[p].role)]
        if blockers:
            self._apply_duck(blockers[0])
        else:
            for p, gain in ladder.default_gains(self._participants()).items():
                self.mixer.set_gain(p, gain)
            self._event("duck", {"by": None})

    def on_move(self, pid: str, room: str, by_ui: str = "chase") -> bool:
        if room not in self.roster.rooms and room != "lead":
            return False
        if by_ui == "self" and self.group_speed_kmh > self.SELF_MOVE_MAX_KMH:
            return False
        self.roster.riders[pid].rooms[0] = room
        self.mixer.move(pid, room)
        self._event("move", {"pid": pid, "room": room})
        return True

    def on_lead_transfer(self, new_lead: str) -> bool:
        if new_lead not in self.roster.riders:
            return False
        for r in self.roster.riders.values():
            if r.role == "lead":
                r.role = "rider"
        self.roster.riders[new_lead].role = "lead"
        self.mixer.set_broadcast(new_lead)
        self._event("lead_transfer", {"lead": new_lead})
        return True

    def on_heartbeat(self, pid: str, data: dict) -> None:
        self.node_status[pid] = {**data, "at": time.time()}

    def on_gps(self, kmh: float) -> None:
        self.group_speed_kmh = kmh

    def snapshot(self) -> dict:
        return {"rooms": self.roster.rooms,
                "riders": {r.id: {"role": r.role, "room": r.rooms[0]}
                           for r in self.roster.riders.values()},
                "talking": sorted(self.talking),
                "speed_kmh": self.group_speed_kmh,
                "nodes": self.node_status,
                "mixer": self.mixer.stats()}

    def _event(self, kind: str, data: dict) -> None:
        self.log.append({"t": time.time(), "kind": kind, **data})

    # -- WebSocket shell --
    async def serve(self, host: str = "0.0.0.0", port: int = CONTROL_PORT):
        import websockets

        async def handler(ws):
            self._ui_clients.add(ws)
            try:
                await ws.send(make_msg("snapshot", "base", self.snapshot()))
                async for raw in ws:
                    try:
                        m = parse_msg(raw)
                    except ValueError:
                        continue
                    t, frm, d = m["t"], m["from"], m.get("data", {})
                    if t == "vad":
                        self.on_vad(frm, bool(d.get("open")))
                    elif t == "heartbeat":
                        self.on_heartbeat(frm, d)
                    elif t == "move":
                        self.on_move(d.get("pid", frm), d.get("room", "main"),
                                     d.get("by", "chase"))
                    elif t == "lead_transfer":
                        self.on_lead_transfer(d.get("lead", ""))
                    elif t == "gps":
                        self.on_gps(float(d.get("kmh", 0)))
                    elif t == "hello":
                        await ws.send(make_msg("snapshot", "base", self.snapshot()))
            finally:
                self._ui_clients.discard(ws)

        return await websockets.serve(handler, host, port)
