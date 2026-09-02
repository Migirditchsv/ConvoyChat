"""Bridge control-plane agent (plan B-4/S-2): WS client to the base.

Sends hello/heartbeat/vad; executes remote commands (node_cmd) so the chase
passenger — or the rider, from the phone page — can fix a node while the
rider keeps eyes on the road (plan O-1). Every command is acknowledged;
every state change is audible to the rider via earcons (SAFE-2).

Actions are injected: SimActions for the virtual convoy and tests,
DeviceActions for the Pi (real shells, dry-run until `enabled`)."""
from __future__ import annotations
import asyncio
import re
import shlex

from common.protocol import make_msg, parse_msg
from common.dsp import tone
from common import earcons

# Every command the base may route to a node. The rider page and the ops
# dashboard only ever send these; anything else is acked ok=False.
COMMANDS = ("reboot", "reconnect_bt", "reconnect_wifi", "set_volume",
            "adjust_volume", "identify", "set_hb_tone", "ptt",
            "bt_scan", "bt_pair", "bt_status", "say", "doctor")


class SimActions:
    """Test/virtual-convoy action handler. Records everything it does and
    drives the (real) engine's volume / hb-tone / earcon / PTT hooks.
    `speak(clip)` is the virtual rider's mouth (sim/live.py) for `say`."""
    FAKE_HEADSETS = [
        {"mac": "00:11:22:AA:BB:01", "name": "Cardo PACKTALK", "paired": True, "connected": True},
        {"mac": "00:11:22:AA:BB:02", "name": "Sena 50S", "paired": False, "connected": False},
        {"mac": "00:11:22:AA:BB:03", "name": "X7 headset", "paired": False, "connected": False},
    ]

    def __init__(self, engine=None, speak=None):
        self.engine = engine
        self.speak = speak
        self.calls: list[tuple] = []
        self.headsets = [dict(h) for h in self.FAKE_HEADSETS]
        self.last_headset: dict | None = dict(self.FAKE_HEADSETS[0])   # shown in heartbeats

    def _earcon(self, name: str):
        if self.engine is None:
            return
        try:
            pcm = earcons.render(name)
        except KeyError:
            pcm = tone(990, 0.05, level_db=-24)
        self.engine.down.queue_earcon(pcm)

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
            self._earcon("volume")
            return f"volume {self.engine.down.volume_pct}%"
        return f"volume {pct}%"

    async def adjust_volume(self, delta: int):
        self.calls.append(("adjust_volume", delta))
        if self.engine is not None:
            v = self.engine.adjust_volume(delta)
            self._earcon("volume")
            return f"volume {v}%"
        return "volume adjusted"

    async def identify(self):
        self.calls.append(("identify",)); self._earcon("identify"); return "identify tone played"

    async def set_hb_tone(self, on: bool):
        self.calls.append(("set_hb_tone", bool(on)))
        if self.engine is not None:
            self.engine.set_hb_tone(on)
        return f"hb_tone {'on' if on else 'off'}"

    async def ptt(self, on: bool):
        """Push-to-talk from the rider's phone: force the gate open while held."""
        self.calls.append(("ptt", bool(on)))
        if self.engine is not None:
            self.engine.set_ptt(on)
            self._earcon("ptt_on" if on else "ptt_off")
        return f"ptt {'on' if on else 'off'}"

    async def bt_scan(self):
        self.calls.append(("bt_scan",))
        return {"headsets": [dict(h) for h in self.headsets]}

    async def bt_pair(self, mac: str):
        self.calls.append(("bt_pair", mac))
        for h in self.headsets:
            if h["mac"].lower() == mac.lower():
                for o in self.headsets:
                    o["connected"] = False
                h["paired"] = h["connected"] = True
                self.last_headset = dict(h)
                self._earcon("connected")
                return f"paired {h['name']}"
        raise ValueError(f"no headset {mac} in range")

    async def bt_status(self):
        self.calls.append(("bt_status",))
        cur = next((h for h in self.headsets if h["connected"]), None)
        self.last_headset = dict(cur) if cur else None
        return {"headset": cur, "codec": "mSBC" if cur else None}

    async def doctor(self):
        """Self-check the operator can run from the ops page. Sim: all green."""
        self.calls.append(("doctor",))
        return {"checks": [
            {"name": "config", "ok": True, "detail": "sim node", "remedy": ""},
            {"name": "bluetooth dongle", "ok": True, "detail": "simulated", "remedy": ""},
            {"name": "headset", "ok": bool(self.last_headset and self.last_headset.get("connected")),
             "detail": (self.last_headset or {}).get("name", "none"), "remedy": "pair from the phone page"},
            {"name": "audio pipes", "ok": True, "detail": "array IO", "remedy": ""},
            {"name": "power", "ok": True, "detail": "no undervoltage", "remedy": ""},
        ]}

    async def say(self, clip: int = 0):
        """Sim only: the virtual rider says something (drives the real gate)."""
        self.calls.append(("say", int(clip)))
        if self.speak is None:
            raise RuntimeError("say is only available on virtual riders")
        return self.speak(int(clip))


class DeviceActions(SimActions):
    """Real Pi actions. Every shell is the documented contract; nothing runs
    until `enabled` (bridge/main flips it from convoy.toml [actions]
    enabled=true). Disabled -> each returns "DRY-RUN: <shell>" so the ack
    shows exactly what would have happened."""
    TIMEOUT_S = 20.0

    def __init__(self, engine=None, enabled: bool = False, headset_mac: str = "",
                 wifi_iface: str = "wlan0"):
        super().__init__(engine)
        self.enabled = enabled
        self.headset_mac = headset_mac
        self.wifi_iface = wifi_iface
        self.last_headset = {"mac": headset_mac, "name": "?", "paired": None,
                             "connected": None} if headset_mac else None

    def cmds(self) -> dict[str, str]:
        mac = self.headset_mac or "<headset-mac>"
        return {
            "reboot": "systemctl reboot",
            "reconnect_bt": f"bluetoothctl disconnect {mac} && sleep 2 && bluetoothctl connect {mac}",
            "reconnect_wifi": (f"wpa_cli -i {self.wifi_iface} disconnect && "
                               f"wpa_cli -i {self.wifi_iface} reconnect"),
            "bt_scan": "bluetoothctl --timeout 8 scan on >/dev/null; bluetoothctl devices",
            "bt_pair": "bluetoothctl pair {mac} && bluetoothctl trust {mac} && bluetoothctl connect {mac}",
            "bt_status": f"bluetoothctl info {mac}",
        }

    async def _sh(self, name: str, **fmt) -> str:
        shell = self.cmds()[name].format(**fmt)
        if not self.enabled:
            return f"DRY-RUN: {shell}"
        proc = await asyncio.create_subprocess_shell(
            shell, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"{name}: timed out after {self.TIMEOUT_S:.0f}s")
        text = out.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"{name}: exit {proc.returncode}: {text[-200:]}")
        return text

    async def reboot(self):
        self._earcon("link_lost")
        if not self.enabled:
            return await self._sh("reboot")
        # fire and forget: the shell reboots the box, so never await its exit
        await asyncio.create_subprocess_shell(self.cmds()["reboot"])
        return "rebooting"

    async def reconnect_bt(self):
        out = await self._sh("reconnect_bt")
        self._earcon("connected")
        return out[-160:] if self.enabled else out

    async def reconnect_wifi(self):
        out = await self._sh("reconnect_wifi")
        self._earcon("link_restored")
        return out[-160:] if self.enabled else out

    async def bt_scan(self):
        out = await self._sh("bt_scan")
        if not self.enabled:
            return {"headsets": [], "dry_run": out}
        found = []
        for line in out.splitlines():
            m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.+)", line.strip())
            if m:
                found.append({"mac": m.group(1), "name": m.group(2).strip(),
                              "paired": False, "connected": False})
        return {"headsets": found}

    async def bt_pair(self, mac: str):
        if not re.fullmatch(r"[0-9A-Fa-f:]{17}", mac or ""):
            raise ValueError(f"bad MAC {mac!r}")
        out = await self._sh("bt_pair", mac=shlex.quote(mac))
        if self.enabled:
            self.headset_mac = mac
            self._earcon("connected")
        return out if not self.enabled else f"paired+trusted {mac}"

    async def bt_status(self):
        if not self.headset_mac:
            return {"headset": None, "detail": "no headset_mac configured — pair first"}
        out = await self._sh("bt_status")
        if not self.enabled:
            return {"headset": None, "dry_run": out}
        info = dict(re.findall(r"^\s*(\w[\w ]*?):\s*(.+)$", out, re.M))
        self.last_headset = {"mac": self.headset_mac, "name": info.get("Name", "?"),
                             "paired": info.get("Paired") == "yes",
                             "connected": info.get("Connected") == "yes"}
        return {"headset": self.last_headset}

    async def say(self, clip: int = 0):
        raise RuntimeError("say is a virtual-rider command; on hardware use PTT")

    # -- device self-check --------------------------------------------------
    def _run(self, cmd: str) -> str:
        """Synchronous helper for doctor probes (short, read-only commands)."""
        import subprocess
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    async def doctor(self, cfg=None, agent_connected: bool | None = None,
                     source_alive: bool | None = None, sink_alive: bool | None = None):
        """Read-only checks with a remedy for each failure. Never changes state."""
        import shutil
        checks = []
        def add(name, ok, detail, remedy=""):
            checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:160],
                           "remedy": remedy if not ok else ""})
        # power: vcgencmd get_throttled bit 0 = undervoltage now, bit 16 = has occurred
        thr = self._run("vcgencmd get_throttled 2>/dev/null").strip()
        flags = parse_throttled(thr)
        add("power", not flags["undervoltage_now"],
            "undervoltage NOW" if flags["undervoltage_now"] else
            ("undervoltage happened earlier" if flags["undervoltage_past"] else (thr or "vcgencmd unavailable")),
            "cable too thin or supply too weak: use a 5.1 V/2.5 A supply and a short thick cable")
        # bluetooth controller (the USB dongle)
        ctl = self._run("bluetoothctl list 2>/dev/null").strip()
        add("bluetooth dongle", bool(ctl), ctl or "no controller",
            "plug the USB Bluetooth dongle in (INV-3: onboard BT is disabled); check `lsusb`")
        hs = self.last_headset
        add("headset", bool(hs and hs.get("connected")),
            f"{hs.get('name', '?')} {'connected' if hs and hs.get('connected') else 'NOT connected'}" if hs
            else "none paired", "pair from the phone page, or `bluetoothctl connect <mac>`")
        for label, cmd in (("mic command", getattr(cfg, "source_cmd", "")),
                           ("speaker command", getattr(cfg, "sink_cmd", ""))):
            exe = (cmd.split() or [""])[0]
            add(label, bool(exe) and shutil.which(exe) is not None, cmd or "not set",
                f"install `{exe}` (pipewire-audio-client-libraries or alsa-utils)")
        if source_alive is not None:
            add("mic pipe", source_alive, "running" if source_alive else "DOWN",
                "the mic command exited: check the PipeWire/bluealsa device name in convoy.toml")
        if sink_alive is not None:
            add("speaker pipe", sink_alive, "running" if sink_alive else "DOWN",
                "the speaker command exited: check the device name in convoy.toml")
        iface = getattr(cfg, "wifi_iface", self.wifi_iface)
        wl = self._run(f"iw dev {iface} link 2>/dev/null").strip()
        add("wifi", wl.startswith("Connected"), wl.splitlines()[0] if wl else "not associated",
            "join the convoy Wi-Fi: `nmcli con up id convoy`; is the car's AP on?")
        if agent_connected is not None:
            add("base link", agent_connected, "control WebSocket up" if agent_connected else "base unreachable",
                "check [node] base in convoy.toml and that the base is running (`make up`)")
        add("callsign", not (getattr(cfg, "radio_mode", "off") != "off" and not getattr(cfg, "radio_callsign", "")),
            getattr(cfg, "radio_callsign", "") or "none (radio off)", "set [radio] callsign or the rig never keys")
        return {"checks": checks, "ok": all(c["ok"] for c in checks)}


def parse_throttled(text: str) -> dict:
    """vcgencmd get_throttled=0x50005 -> flags. Bit 0 undervoltage now,
    bit 16 undervoltage has occurred, bit 2 throttled now, bit 18 occurred."""
    try:
        v = int(text.strip().split("=")[-1], 16)
    except ValueError:
        v = 0
    return {"undervoltage_now": bool(v & 0x1), "undervoltage_past": bool(v & 0x10000),
            "throttled_now": bool(v & 0x4), "throttled_past": bool(v & 0x40000)}


class BridgeAgent:
    HEARTBEAT_S = 1.0
    RECONNECT_S = 2.0

    def __init__(self, node_id: str, engine, actions, base_url: str,
                 link_stats=lambda: {}, log=lambda msg: None, token: str = ""):
        self.node_id = node_id
        self.engine = engine
        self.actions = actions
        self.base_url = base_url
        self.link_stats = link_stats
        self.log = log
        self.token = token
        self.doctor_kwargs = lambda: {}     # bridge/main injects cfg + pipe state for real checks
        self._task = None
        self._ws = None
        self.connected = False
        self.vad_events = 0
        if engine is not None and hasattr(engine, "vad_listeners"):
            engine.vad_listeners.append(self._on_vad)

    async def start(self):
        self._task = asyncio.create_task(self._run())

    # -- gate state -> base (the orchestrator's ladder ducks on this; INV-6/7) --
    def _on_vad(self, open_: bool) -> None:
        ws = self._ws
        if ws is None:
            return
        self.vad_events += 1
        asyncio.get_running_loop().create_task(self._send(ws, "vad", {"open": bool(open_)}))

    async def _send(self, ws, t: str, data: dict) -> None:
        try:
            await ws.send(make_msg(t, self.node_id, data))
        except Exception:
            pass

    async def _run(self):
        import websockets
        while True:
            try:
                async with websockets.connect(self.base_url) as ws:
                    self._ws = ws
                    self.connected = True
                    self.log(f"control: connected to {self.base_url}")
                    hello = {"kind": "node"}
                    if self.token:
                        hello["token"] = self.token
                    await ws.send(make_msg("hello", self.node_id, hello))
                    if self.engine is not None and self.engine.stats.get("vad_open"):
                        await ws.send(make_msg("vad", self.node_id, {"open": True}))
                    hb = asyncio.create_task(self._heartbeats(ws))
                    try:
                        async for raw in ws:
                            await self._handle(ws, raw)
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.connected or self._ws is None:
                    self.log(f"control: base unreachable ({type(e).__name__}); retrying")
                self.connected = False
                self._ws = None
                await asyncio.sleep(self.RECONNECT_S)   # base may be rebooting

    def status(self) -> dict:
        e = self.engine
        return {**(e.stats if e else {}),
                **self.link_stats(),
                "volume": e.down.volume_pct if e else None,
                "hb_tone": e.hb_tone if e else False,
                "vad_state": bool(e and e.stats.get("vad_open")),
                "ptt": bool(e and e.stats.get("ptt")),
                "headset": getattr(self.actions, "last_headset", None),
                "radio": e.radio.stats() if (e is not None and getattr(e, "radio", None)) else None,
                "link_up": bool(e.link_up) if e is not None else None}

    async def _heartbeats(self, ws):
        while True:
            try:
                await ws.send(make_msg("heartbeat", self.node_id, self.status()))
            except Exception:
                return
            await asyncio.sleep(self.HEARTBEAT_S)

    async def _handle(self, ws, raw):
        try:
            m = parse_msg(raw)
        except ValueError:
            return
        if m["t"] != "node_cmd":
            return
        d = m.get("data") or {}
        cmd, args, cmd_id = d.get("cmd"), d.get("args") or {}, d.get("cmd_id")
        if not isinstance(args, dict):
            args = {}
        a = self.actions
        handler = {
            "reboot": a.reboot,
            "reconnect_bt": a.reconnect_bt,
            "reconnect_wifi": a.reconnect_wifi,
            "set_volume": lambda: a.set_volume(int(args.get("pct", 100))),
            "adjust_volume": lambda: a.adjust_volume(int(args.get("delta", 0))),
            "identify": a.identify,
            "set_hb_tone": lambda: a.set_hb_tone(bool(args.get("on", False))),
            "ptt": lambda: a.ptt(bool(args.get("on", False))),
            "bt_scan": a.bt_scan,
            "bt_pair": lambda: a.bt_pair(str(args.get("mac", ""))),
            "bt_status": a.bt_status,
            "say": lambda: a.say(int(args.get("clip", 0))),
            "doctor": lambda: a.doctor(**self.doctor_kwargs()) if hasattr(a, "doctor") else None,
        }.get(cmd)
        if handler is None:
            await self._send(ws, "ack", {"cmd_id": cmd_id, "ok": False,
                                         "detail": f"unknown cmd {cmd!r}"})
            return
        try:
            detail = await handler()
            await self._send(ws, "ack", {"cmd_id": cmd_id, "ok": True, "detail": detail})
        except Exception as e:
            await self._send(ws, "ack", {"cmd_id": cmd_id, "ok": False, "detail": str(e)})

    def set_base_url(self, url: str) -> None:
        """Re-target the control link at runtime; the open socket is closed so
        the reconnect loop dials the new base."""
        if url == self.base_url:
            return
        self.base_url = url
        ws = self._ws
        if ws is not None:
            asyncio.get_running_loop().create_task(ws.close())

    def stop(self):
        if self._task:
            self._task.cancel()
