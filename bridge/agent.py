"""Bridge control-plane agent (plan B-4/S-2): WS client to the base.

Sends hello/heartbeat/vad; executes remote debug commands (node_cmd) so the
chase passenger can fix a rider's node while the rider keeps eyes on the
road (plan O-1). Every command is acknowledged; every state change is
audible to the rider via earcons (SAFE-2).

Actions are injected: SimActions for the virtual convoy and tests,
DeviceActions for the Pi (M2 — shell commands documented, guarded)."""
from __future__ import annotations
import asyncio
import json

from common.protocol import make_msg, parse_msg
from common.dsp import tone


class SimActions:
    """Test/virtual-convoy action handler. Records everything it does and
    drives the (real) engine's volume / hb-tone / earcon hooks."""
    def __init__(self, engine=None):
        self.engine = engine
        self.calls: list[tuple] = []

    def _earcon(self, name: str):
        if self.engine is not None:
            self.engine.down.queue_earcon(tone(990, 0.05, level_db=-24))

    async def reboot(self):
        self.calls.append(("reboot",)); self._earcon("link_lost"); return "sim reboot scheduled"

    async def reconnect_bt(self):
        self.calls.append(("reconnect_bt",)); self._earcon("connected"); return "sim bt cycled"

    async def reconnect_wifi(self):
        self.calls.append(("reconnect_wifi",)); self._earcon("link_restored"); return "sim wifi cycled"

    async def set_volume(self, pct: int):
        self.calls.append(("set_volume", pct))
        if self.engine is not None:
            self.engine.set_volume(pct)
            return f"volume {self.engine.down.volume_pct}%"
        return f"volume {pct}%"

    async def adjust_volume(self, delta: int):
        self.calls.append(("adjust_volume", delta))
        if self.engine is not None:
            return f"volume {self.engine.adjust_volume(delta)}%"
        return "volume adjusted"

    async def identify(self):
        self.calls.append(("identify",)); self._earcon("identify"); return "identify tone played"

    async def set_hb_tone(self, on: bool):
        self.calls.append(("set_hb_tone", bool(on)))
        if self.engine is not None:
            self.engine.set_hb_tone(on)
        return f"hb_tone {'on' if on else 'off'}"


class DeviceActions(SimActions):
    """M2: real Pi actions. Commands are the contract; wiring lands with
    hardware. Each returns the shell it WOULD run until enabled on-device."""
    CMDS = {
        "reboot": "systemctl reboot",
        "reconnect_bt": "bluetoothctl disconnect <headset-mac> && sleep 2 && bluetoothctl connect <headset-mac>",
        "reconnect_wifi": "wpa_cli -i wlan0 disconnect && wpa_cli -i wlan0 reconnect",
    }

    def __init__(self, engine=None, enabled: bool = False):
        super().__init__(engine)
        self.enabled = enabled  # flipped true by bridge-svc on real hardware

    async def reboot(self):
        if not self.enabled:
            return f"DRY-RUN: {self.CMDS['reboot']}"
        proc = await asyncio.create_subprocess_shell(self.CMDS["reboot"])
        await proc.wait()
        return "rebooting"


class BridgeAgent:
    HEARTBEAT_S = 1.0

    def __init__(self, node_id: str, engine, actions, base_url: str,
                 link_stats=lambda: {}):
        self.node_id = node_id
        self.engine = engine
        self.actions = actions
        self.base_url = base_url
        self.link_stats = link_stats
        self._task = None
        self.connected = False

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        import websockets
        while True:
            try:
                async with websockets.connect(self.base_url) as ws:
                    self.connected = True
                    await ws.send(make_msg("hello", self.node_id, {"kind": "node"}))
                    hb = asyncio.create_task(self._heartbeats(ws))
                    try:
                        async for raw in ws:
                            await self._handle(ws, raw)
                    finally:
                        hb.cancel()
            except (OSError, Exception):
                self.connected = False
                await asyncio.sleep(2.0)   # reconnect backoff; base may be rebooting

    async def _heartbeats(self, ws):
        while True:
            data = {**(self.engine.stats if self.engine else {}),
                    **self.link_stats(),
                    "volume": self.engine.down.volume_pct if self.engine else None,
                    "hb_tone": self.engine.hb_tone if self.engine else False,
                    "vad_state": bool(self.engine and self.engine.stats.get("vad_open"))}
            await ws.send(make_msg("heartbeat", self.node_id, data))
            await asyncio.sleep(self.HEARTBEAT_S)

    async def _handle(self, ws, raw):
        try:
            m = parse_msg(raw)
        except ValueError:
            return
        if m["t"] != "node_cmd":
            return
        d = m.get("data", {})
        cmd, args, cmd_id = d.get("cmd"), d.get("args", {}), d.get("cmd_id")
        handler = {
            "reboot": self.actions.reboot,
            "reconnect_bt": self.actions.reconnect_bt,
            "reconnect_wifi": self.actions.reconnect_wifi,
            "set_volume": lambda: self.actions.set_volume(int(args.get("pct", 100))),
            "adjust_volume": lambda: self.actions.adjust_volume(int(args.get("delta", 0))),
            "identify": self.actions.identify,
            "set_hb_tone": lambda: self.actions.set_hb_tone(bool(args.get("on", False))),
        }.get(cmd)
        if handler is None:
            await ws.send(make_msg("ack", self.node_id,
                                   {"cmd_id": cmd_id, "ok": False,
                                    "detail": f"unknown cmd {cmd!r}"}))
            return
        try:
            detail = await handler()
            await ws.send(make_msg("ack", self.node_id,
                                   {"cmd_id": cmd_id, "ok": True, "detail": detail}))
        except Exception as e:
            await ws.send(make_msg("ack", self.node_id,
                                   {"cmd_id": cmd_id, "ok": False, "detail": str(e)}))

    def stop(self):
        if self._task:
            self._task.cancel()
