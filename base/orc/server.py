"""Orchestrator (plan S-2): roster + events -> MixerAPI state. ~300 lines is
the budget; the WS transport is a thin shell around the same core the tests
drive directly (S-08..S-10), so field and CI exercise identical logic.

Snapshots are authoritative (INV-7). They are PUSHED to every UI client on
each state change (debounced) and once a second regardless, so a phone page
never needs to poll and a talking badge is never missed."""
from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid

from common.protocol import make_msg, parse_msg, CONTROL_PORT
from common.roster import Roster
from base.mixer.api import MixerAPI
from base.orc import ladder
from base.orc.doctor import diagnose, summary

log = logging.getLogger("convoy.orc")


class Orchestrator:
    HANGOVER_S = 0.4
    SELF_MOVE_MAX_KMH = 8.0
    PUSH_DEBOUNCE_S = 0.08
    PUSH_PERIOD_S = 1.0
    TEXT_RING = 12

    def __init__(self, roster: Roster, mixer: MixerAPI):
        self.roster = roster
        self.mixer = mixer
        self.node_status: dict[str, dict] = {}
        self.talking: set[str] = set()
        self.group_speed_kmh = 0.0
        self.hb_tone_default = False
        self.texts: list[dict] = []          # recent text/TTS messages (ring)
        self.announce = None                 # QueuedRtpSource, attached by base.main
        self._announcing = False
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
        self._push_handle: asyncio.TimerHandle | None = None
        self._push_task = None
        self.pushes = 0
        self.mode = "hw"                     # sim | hw | field — shown to the pages
        self.radio = None                    # RadioGateway, attached by base.main
        self.state_path: str | None = None   # operator settings survive a base restart
        self.volumes: dict[str, int] = {}    # last known helmet volume per rider (re-pushed on join)
        self.talk_since: dict[str, float] = {}
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
        if self._announcing and self.roster.riders[pid].role == "music":
            base = min(base, 25)             # music ducks under announcements
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
            if pid not in self.talking:
                self.talk_since[pid] = time.time()
            self.talking.add(pid)
            if (h := self._hang.pop(pid, None)):
                h.cancel()
            self._apply_duck(pid)
        else:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._end_talk(pid)
                return
            if pid in self._hang:
                self._hang[pid].cancel()
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
        self.talk_since.pop(pid, None)
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
        if pid not in self.roster.riders:
            return False
        if room not in self.roster.rooms and room != "lead":
            return False
        if by_ui == "self" and self.group_speed_kmh > self.SELF_MOVE_MAX_KMH:
            return False
        self.roster.riders[pid].rooms[0] = room
        self.mixer.move(pid, room)
        self._event("move", {"pid": pid, "room": room, "by": by_ui})
        self.save_state()
        return True

    def on_lead_transfer(self, new_lead: str) -> bool:
        if new_lead not in self.roster.riders:
            return False
        if self.roster.riders[new_lead].role == "music":
            return False
        for r in self.roster.riders.values():
            if r.role == "lead":
                r.role = "rider"
        self.roster.riders[new_lead].role = "lead"
        self.mixer.set_broadcast(new_lead)
        self._event("lead_transfer", {"lead": new_lead})
        self.save_state()
        return True

    def on_heartbeat(self, pid: str, data: dict) -> None:
        if pid not in self.roster.riders or not isinstance(data, dict):
            return
        prev = self.node_status.get(pid, {})
        self.node_status[pid] = {**prev, **data, "at": time.time()}
        v = data.get("volume")
        if isinstance(v, int) and v != self.volumes.get(pid):
            self.volumes[pid] = v
            self.save_state()

    # -- operator audio controls (compose with ladder; survive ducks) --
    def on_audio_ctl(self, pid: str, mute: bool | None = None,
                     trim: int | None = None) -> bool:
        if pid not in self.roster.riders:
            return False
        if mute is not None:
            (self.muted.add if mute else self.muted.discard)(pid)
        if trim is not None:
            try:
                self.trim[pid] = int(max(10, min(150, int(trim))))
            except (TypeError, ValueError):
                return False
        self._push_gains()
        self._event("audio_ctl", {"pid": pid, "mute": mute, "trim": trim})
        self.save_state()
        return True

    # -- text / TTS announcements --
    def attach_announce(self, announce) -> None:
        """Wire the QueuedRtpSource; its on_state must call set_announcing."""
        self.announce = announce

    def set_announcing(self, active: bool) -> None:
        self._announcing = active
        self._push_gains()
        self._event("announce", {"active": bool(active)})

    async def on_text(self, frm: str, msg: str, speak: bool = False) -> None:
        msg = str(msg or "").strip()[:300]
        if not msg:
            return
        entry = {"at": time.time(), "from": str(frm)[:32], "msg": msg,
                 "speak": bool(speak), "spoken": False}
        self.texts = (self.texts + [entry])[-self.TEXT_RING:]
        self._event("text", {"from": frm, "msg": msg, "speak": speak})
        if speak and self.announce is not None:
            try:
                from base.media import tts
                pcm = await tts.render(msg)
                self.announce.enqueue(pcm)
                entry["spoken"] = True
                entry["engine"] = tts.engine_name()
            except Exception as e:
                entry["error"] = str(e)[:120]
            self._schedule_push()

    # -- remote node debug --
    async def send_node_cmd(self, target: str, cmd: str, args: dict | None = None) -> str:
        """Route a command to a connected node. Returns cmd_id; the ack lands
        in self.acks[cmd_id] and node_status[target]['last_ack']."""
        cmd_id = uuid.uuid4().hex[:8]
        ws = self._node_ws.get(target)
        if ws is None:
            self.acks[cmd_id] = {"ok": False, "detail": f"{target} not connected",
                                 "from": target, "at": time.time(), "cmd": cmd}
            self.node_status.setdefault(target, {})["last_ack"] = self.acks[cmd_id]
            self._event("node_cmd", {"target": target, "cmd": cmd, "cmd_id": cmd_id,
                                     "routed": False})
            return cmd_id
        try:
            await ws.send(make_msg("node_cmd", "base",
                                   {"target": target, "cmd": cmd,
                                    "args": args or {}, "cmd_id": cmd_id}))
        except Exception as e:
            self.acks[cmd_id] = {"ok": False, "detail": f"send failed: {e}",
                                 "from": target, "at": time.time(), "cmd": cmd}
            return cmd_id
        self.acks[cmd_id] = {"ok": None, "detail": "pending", "from": target,
                             "at": time.time(), "cmd": cmd}
        self._event("node_cmd", {"target": target, "cmd": cmd, "cmd_id": cmd_id,
                                 "routed": True})
        return cmd_id

    def on_ack(self, frm: str, data: dict) -> None:
        if not isinstance(data, dict):
            return
        cmd_id = data.get("cmd_id")
        prev = self.acks.get(cmd_id, {}) if cmd_id else {}
        rec = {"ok": data.get("ok"), "detail": data.get("detail"), "from": frm,
               "at": time.time(), "cmd": prev.get("cmd")}
        if cmd_id:
            self.acks[cmd_id] = rec
        self.node_status.setdefault(frm, {})["last_ack"] = rec
        self._event("ack", {"from": frm, "cmd_id": cmd_id, "ok": rec["ok"]})

    # -- settings persistence (DR-014): a base restart must not lose the ride --
    def state(self) -> dict:
        lead = next((r.id for r in self.roster.riders.values() if r.role == "lead"), None)
        return {"v": 1, "muted": sorted(self.muted), "trim": dict(self.trim),
                "rooms": {r.id: r.rooms[0] for r in self.roster.riders.values()},
                "lead": lead, "hb_tone_default": self.hb_tone_default,
                "volumes": dict(self.volumes), "saved_at": time.time()}

    def save_state(self) -> None:
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state(), f, indent=1)
            import os
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("could not save state to %s: %s", self.state_path, e)

    def load_state(self, path: str | None = None) -> bool:
        """Apply a saved state to the roster/mixer. Unknown riders are ignored
        (the roster is the truth; the state is a convenience)."""
        path = path or self.state_path
        if not path:
            return False
        try:
            with open(path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return False
        if st.get("v") != 1:
            return False
        for pid in st.get("muted", []):
            if pid in self.roster.riders:
                self.muted.add(pid)
        for pid, t in (st.get("trim") or {}).items():
            if pid in self.roster.riders:
                self.trim[pid] = int(max(10, min(150, int(t))))
        for pid, room in (st.get("rooms") or {}).items():
            if pid in self.roster.riders and (room in self.roster.rooms or room == "lead"):
                self.roster.riders[pid].rooms[0] = room
                self.mixer.move(pid, room)
        lead = st.get("lead")
        if lead in self.roster.riders and self.roster.riders[lead].role != "music":
            for r in self.roster.riders.values():
                if r.role == "lead":
                    r.role = "rider"
            self.roster.riders[lead].role = "lead"
            self.mixer.set_broadcast(lead)
        self.hb_tone_default = bool(st.get("hb_tone_default", False))
        self.volumes = {k: int(v) for k, v in (st.get("volumes") or {}).items() if k in self.roster.riders}
        self._ladder_now = ladder.default_gains(self._participants())
        self._push_gains()
        self._event("state_restored", {"path": path})
        return True

    def on_gps(self, kmh: float) -> None:
        try:
            self.group_speed_kmh = max(0.0, float(kmh))
        except (TypeError, ValueError):
            return

    def snapshot(self) -> dict:
        now = time.time()
        nodes = {}
        for pid, st in self.node_status.items():
            nodes[pid] = {**st, "age_s": round(now - st.get("at", now), 1),
                          "online": (now - st.get("at", 0)) < 5.0}
        from base.media import tts
        snap = {"rooms": self.roster.rooms,
                "riders": {r.id: {"role": r.role, "room": r.rooms[0],
                                  "muted": r.id in self.muted,
                                  "trim": self.trim.get(r.id, 100)}
                           for r in self.roster.riders.values()},
                "talking": sorted(self.talking),
                "speed_kmh": self.group_speed_kmh,
                "self_move_ok": self.group_speed_kmh <= self.SELF_MOVE_MAX_KMH,
                "hb_tone_default": self.hb_tone_default,
                "texts": self.texts,
                "tts_engine": tts.engine_name(),
                "announcing": self._announcing,
                "nodes": nodes,
                "mixer": self.mixer.stats(),
                "mode": self.mode,
                "radio": self.radio.stats() if self.radio is not None else None,
                "talk_since": dict(self.talk_since),
                "at": now}
        snap["issues"] = diagnose(snap, now)
        snap["health"] = summary(snap["issues"])
        return snap

    def _event(self, kind: str, data: dict) -> None:
        self.log.append({"t": time.time(), "kind": kind, **data})
        if len(self.log) > 5000:
            del self.log[:1000]
        self._schedule_push()

    # -- snapshot push --
    def _schedule_push(self) -> None:
        if self._push_handle is not None or not self._ui_clients:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._push_handle = loop.call_later(self.PUSH_DEBOUNCE_S, self._fire_push)

    def _fire_push(self) -> None:
        self._push_handle = None
        asyncio.get_running_loop().create_task(self.broadcast_snapshot())

    async def broadcast_snapshot(self) -> None:
        if not self._ui_clients:
            return
        msg = make_msg("snapshot", "base", self.snapshot())
        self.pushes += 1
        dead = []
        for ws in list(self._ui_clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ui_clients.discard(ws)

    async def _periodic_push(self) -> None:
        while True:
            await asyncio.sleep(self.PUSH_PERIOD_S)
            await self.broadcast_snapshot()

    # -- WebSocket shell --
    async def _dispatch(self, ws, m: dict) -> str | None:
        """Route one parsed message. Returns the node_id if this connection
        just identified itself as a node. Never raises on bad input."""
        t, frm, d = m["t"], str(m.get("from", ""))[:32], m.get("data")
        if not isinstance(d, dict):
            d = {}
        if t == "vad":
            self.on_vad(frm, bool(d.get("open")))
        elif t == "heartbeat":
            self.on_heartbeat(frm, d)
        elif t == "move":
            self.on_move(str(d.get("pid", frm)), str(d.get("room", "main")),
                         str(d.get("by", "chase")))
        elif t == "lead_transfer":
            self.on_lead_transfer(str(d.get("lead", "")))
        elif t == "gps":
            self.on_gps(d.get("kmh", 0))
        elif t == "text":
            await self.on_text(frm, d.get("msg", ""), bool(d.get("speak")))
        elif t == "audio_ctl":
            mute = d.get("mute")
            self.on_audio_ctl(str(d.get("pid", "")),
                              None if mute is None else bool(mute), d.get("trim"))
        elif t == "node_cmd":
            await self.send_node_cmd(str(d.get("target", "")), str(d.get("cmd", "")),
                                     d.get("args") if isinstance(d.get("args"), dict) else {})
        elif t == "ack":
            self.on_ack(frm, d)
        elif t == "hello":
            if d.get("kind") == "node":
                token = self.roster.net.get("node_token")
                if token and d.get("token") != token:
                    log.warning("node hello from %s rejected: bad token", frm)
                    return None
                if frm not in self.roster.riders:
                    log.warning("node hello from unknown rider %r (not in roster)", frm)
                    return None
                self._node_ws[frm] = ws
                self._ui_clients.discard(ws)
                peer = getattr(ws, "remote_address", None)
                self.node_status.setdefault(frm, {})["ip"] = peer[0] if peer else None
                self.node_status[frm].setdefault("at", 0.0)
                self._event("node_join", {"pid": frm, "ip": self.node_status[frm]["ip"]})
                if self.hb_tone_default:
                    await self.send_node_cmd(frm, "set_hb_tone", {"on": True})
                if frm in self.volumes and self.volumes[frm] != 100:
                    await self.send_node_cmd(frm, "set_volume", {"pct": self.volumes[frm]})
                return frm
            await ws.send(make_msg("snapshot", "base", self.snapshot()))
        return None

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
                    except (ValueError, TypeError):
                        continue
                    try:
                        nid = await self._dispatch(ws, m)
                        if nid:
                            node_id = nid
                    except Exception:
                        log.exception("bad %s message from %s", m.get("t"), m.get("from"))
            finally:
                self._ui_clients.discard(ws)
                if node_id and self._node_ws.get(node_id) is ws:
                    del self._node_ws[node_id]
                    self._event("node_leave", {"pid": node_id})

        server = await websockets.serve(handler, host, port)
        if self._push_task is None:
            self._push_task = asyncio.create_task(self._periodic_push())
        _close = server.close

        def close(*a, **k):
            if self._push_task:
                self._push_task.cancel()
                self._push_task = None
            return _close(*a, **k)
        server.close = close
        return server
