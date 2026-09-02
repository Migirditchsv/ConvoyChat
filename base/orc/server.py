"""Orchestrator (plan S-2): roster + events -> MixerAPI state. ~300 lines is
the budget; the WS transport is a thin shell around the same core the tests
drive directly (S-08..S-10), so field and CI exercise identical logic."""
from __future__ import annotations
import asyncio
import json
import time
import uuid

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
        self.hb_tone_default = False
        # operator audio controls, composed with the ladder (never clobbered
        # by duck/restore): effective = 0 if muted else ladder * trim / 100
        self.trim: dict[str, int] = {}
        self.muted: set[str] = set()
        self._ladder_now: dict[str, int] = {}
        self._hang: dict[str, asyncio.TimerHandle] = {}
        self._ui_clients: set = set()
        self._node_ws: dict[str, object] = {}
        self.acks: dict[str, dict] = {}
        self.log: list[dict] = []
        self.populate()

    # -- lifecycle --
    def populate(self) -> None:
        """Idempotent: (re)apply roster to the mixer — also S-10's reattach."""
        self._ladder_now = ladder.default_gains(self._participants())
        for r in self.roster.riders.values():
            out = None if r.role == "music" else (r.ip, r.down_port)
            self.mixer.add_participant(r.id, r.rooms[0], out,
                                       gain=self._effective(r.id), role=r.role)
        leads = self.roster.by_role("lead")
        self.mixer.set_broadcast(leads[0].id if leads else None)

    def _effective(self, pid: str) -> int:
        if pid in self.muted:
            return 0
        base = self._ladder_now.get(pid, 100)
        return base * self.trim.get(pid, 100) // 100

    def _push_gains(self) -> None:
        for pid in self.roster.riders:
            self.mixer.set_gain(pid, self._effective(pid))

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
            self._ladder_now = g
            self._push_gains()
            self._event("duck", {"by": pid, "role": role})

    def _end_talk(self, pid: str) -> None:
        self.talking.discard(pid)
        self._hang.pop(pid, None)
        blockers = [p for p in self.talking
                    if ladder.DUCK.get(self.roster.riders[p].role)]
        if blockers:
            self._apply_duck(blockers[0])
        else:
            self._ladder_now = ladder.default_gains(self._participants())
            self._push_gains()
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

    # -- operator audio controls (compose with ladder; survive ducks) --
    def on_audio_ctl(self, pid: str, mute: bool | None = None,
                     trim: int | None = None) -> bool:
        if pid not in self.roster.riders:
            return False
        if mute is not None:
            (self.muted.add if mute else self.muted.discard)(pid)
        if trim is not None:
            self.trim[pid] = int(max(10, min(150, trim)))
        self._push_gains()
        self._event("audio_ctl", {"pid": pid, "mute": mute, "trim": trim})
        return True

    # -- remote node debug --
    async def send_node_cmd(self, target: str, cmd: str, args: dict | None = None) -> str:
        """Route a debug command to a connected node. Returns cmd_id; the ack
        lands in self.acks[cmd_id] and node_status[target]['last_ack']."""
        cmd_id = uuid.uuid4().hex[:8]
        ws = self._node_ws.get(target)
        if ws is None:
            self.acks[cmd_id] = {"ok": False, "detail": f"{target} not connected", "from": target}
            return cmd_id
        await ws.send(make_msg("node_cmd", "base",
                               {"target": target, "cmd": cmd,
                                "args": args or {}, "cmd_id": cmd_id}))
        self._event("node_cmd", {"target": target, "cmd": cmd, "cmd_id": cmd_id})
        return cmd_id

    def on_ack(self, frm: str, data: dict) -> None:
        cmd_id = data.get("cmd_id")
        rec = {"ok": data.get("ok"), "detail": data.get("detail"), "from": frm,
               "at": time.time()}
        if cmd_id:
            self.acks[cmd_id] = rec
        self.node_status.setdefault(frm, {})["last_ack"] = rec

    def on_gps(self, kmh: float) -> None:
        self.group_speed_kmh = kmh

    def snapshot(self) -> dict:
        now = time.time()
        nodes = {}
        for pid, st in self.node_status.items():
            nodes[pid] = {**st, "age_s": round(now - st.get("at", now), 1)}
        return {"rooms": self.roster.rooms,
                "riders": {r.id: {"role": r.role, "room": r.rooms[0],
                                  "muted": r.id in self.muted,
                                  "trim": self.trim.get(r.id, 100)}
                           for r in self.roster.riders.values()},
                "talking": sorted(self.talking),
                "speed_kmh": self.group_speed_kmh,
                "hb_tone_default": self.hb_tone_default,
                "nodes": nodes,
                "mixer": self.mixer.stats()}

    def _event(self, kind: str, data: dict) -> None:
        self.log.append({"t": time.time(), "kind": kind, **data})

    # -- WebSocket shell --
    async def serve(self, host: str = "0.0.0.0", port: int = CONTROL_PORT):
        import websockets

        async def handler(ws):
            self._ui_clients.add(ws)
            node_id = None
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
                    elif t == "audio_ctl":
                        self.on_audio_ctl(d.get("pid", ""), d.get("mute"),
                                          d.get("trim"))
                    elif t == "node_cmd":
                        await self.send_node_cmd(d.get("target", ""),
                                                 d.get("cmd", ""), d.get("args"))
                    elif t == "ack":
                        self.on_ack(frm, d)
                    elif t == "hello":
                        if d.get("kind") == "node":
                            node_id = frm
                            self._node_ws[frm] = ws
                            if self.hb_tone_default:
                                await self.send_node_cmd(frm, "set_hb_tone",
                                                         {"on": True})
                        await ws.send(make_msg("snapshot", "base", self.snapshot()))
            finally:
                self._ui_clients.discard(ws)
                if node_id and self._node_ws.get(node_id) is ws:
                    del self._node_ws[node_id]

        return await websockets.serve(handler, host, port)
