"""S-15: the control plane as phones and bridges actually use it — over a
real WebSocket. Junk never kills a connection; snapshots are PUSHED; a
bridge's gate state reaches the ladder (ducking off-box); PTT, pairing and
`say` round-trip; node identity can't be spoofed without the token."""
import asyncio
import json
import time
import pytest
import websockets

from common.protocol import make_msg
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from bridge.agent import BridgeAgent, SimActions, COMMANDS
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink

PORT = 8896


def _msg(t, d, frm="ui"):
    return json.dumps({"v": 1, "t": t, "ts": time.time(), "from": frm, "data": d})


async def _recv_snapshot(ws, timeout=2.0):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    m = json.loads(raw)
    assert m["t"] == "snapshot"
    return m["data"]


@pytest.mark.realtime
def test_ui_junk_is_harmless_and_snapshots_push():
    async def scenario():
        orc = Orchestrator(demo_roster(3), PyMixer(rtp_port=5470))
        server = await orc.serve("127.0.0.1", PORT)
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/") as ws:
            await _recv_snapshot(ws)
            await ws.send(_msg("hello", {"kind": "ui"}))
            await _recv_snapshot(ws)
            junk = [_msg("move", {"pid": "ghost", "room": "nav"}),
                    _msg("move", {"pid": "r2_rider", "room": 42}),
                    _msg("audio_ctl", {"pid": "r2_rider", "trim": "loud"}),
                    _msg("audio_ctl", {"pid": ["x"]}),
                    _msg("lead_transfer", {"lead": None}),
                    _msg("gps", {"kmh": "fast"}),
                    _msg("node_cmd", {"target": "nobody", "cmd": "reboot", "args": "x"}),
                    _msg("text", {"msg": 12345}),
                    _msg("vad", "notadict"),
                    _msg("heartbeat", {"rssi": -50}, frm="stranger"),
                    "not json at all",
                    json.dumps({"v": 1, "t": "hello"})]
            for j in junk:
                await ws.send(j)
            await ws.send(_msg("move", {"pid": "r2_rider", "room": "nav", "by": "chase"}))
            # pushed (not polled): the move arrives without another hello
            snap = None
            for _ in range(5):
                snap = await _recv_snapshot(ws)
                if snap["riders"]["r2_rider"]["room"] == "nav":
                    break
            assert snap["riders"]["r2_rider"]["room"] == "nav"
            assert "stranger" not in snap["nodes"]
            assert snap["texts"][-1]["msg"] == "12345"      # coerced, not crashed
            # periodic push keeps arriving with no traffic from us
            t0 = time.time()
            await _recv_snapshot(ws, timeout=2.5)
            assert time.time() - t0 <= 2.5
        server.close(); await server.wait_closed()
    asyncio.run(scenario())


@pytest.mark.realtime
def test_bridge_gate_state_ducks_offbox_and_ptt_roundtrip():
    """The agent forwards gate open/close as `vad` — without this a real
    convoy never ducks (the demo wired on_vad in-process and hid the gap)."""
    async def scenario():
        roster = demo_roster(3, base_port=6520)
        mixer = PyMixer(rtp_port=5471)
        orc = Orchestrator(roster, mixer)
        server = await orc.serve("127.0.0.1", PORT + 1)
        eng = BridgeEngine("r0_lead", ArraySource(400, []), ArraySink(),
                           mixer_addr=("127.0.0.1", 5471), down_port=6520, prefer_silero=False)
        await eng.start()
        said = []
        actions = SimActions(engine=eng, speak=lambda c: said.append(c) or f"clip {c}")
        agent = BridgeAgent("r0_lead", eng, actions, f"ws://127.0.0.1:{PORT + 1}/")
        await agent.start()
        await asyncio.sleep(0.8)
        assert orc.node_status["r0_lead"]["ip"] == "127.0.0.1"
        assert mixer.parts["music"].gain if "music" in mixer.parts else True

        cid = await orc.send_node_cmd("r0_lead", "ptt", {"on": True})
        await asyncio.sleep(0.5)                           # engine tick: gate opens -> vad msg
        assert orc.acks[cid]["ok"] and eng.up.gate.force_open
        assert "r0_lead" in orc.talking, "gate state never reached the base"
        assert mixer.parts["r2_rider"].gain == 25, "lead talking did not duck riders"
        assert orc.node_status["r0_lead"]["ptt"] is True
        cid = await orc.send_node_cmd("r0_lead", "ptt", {"on": False})
        await asyncio.sleep(1.2)                           # close + hangover
        assert "r0_lead" not in orc.talking and mixer.parts["r2_rider"].gain == 100

        cid = await orc.send_node_cmd("r0_lead", "say", {"clip": 3})
        await asyncio.sleep(0.3)
        assert orc.acks[cid]["ok"] and said == [3]
        cid = await orc.send_node_cmd("r0_lead", "bt_scan", {})
        await asyncio.sleep(0.3)
        hs = orc.acks[cid]["detail"]["headsets"]
        assert len(hs) == 3
        cid = await orc.send_node_cmd("r0_lead", "bt_pair", {"mac": hs[1]["mac"]})
        await asyncio.sleep(0.3)
        assert orc.acks[cid]["ok"] and "Sena" in orc.acks[cid]["detail"]
        await asyncio.sleep(1.1)                           # next heartbeat carries it
        assert orc.node_status["r0_lead"]["headset"]["name"] == "Sena 50S"
        cid = await orc.send_node_cmd("r0_lead", "bt_pair", {"mac": "ff:ff:ff:ff:ff:ff"})
        await asyncio.sleep(0.3)
        assert orc.acks[cid]["ok"] is False
        assert set(COMMANDS) >= {"ptt", "bt_scan", "bt_pair", "bt_status", "say"}

        agent.stop(); eng.stop(); mixer.stop()
        server.close(); await server.wait_closed()
    asyncio.run(scenario())


@pytest.mark.realtime
def test_node_token_gates_node_identity():
    async def scenario():
        roster = demo_roster(3)
        roster.net["node_token"] = "s3cret"
        orc = Orchestrator(roster, PyMixer(rtp_port=5472))
        server = await orc.serve("127.0.0.1", PORT + 2)
        async with websockets.connect(f"ws://127.0.0.1:{PORT + 2}/") as ws:
            await _recv_snapshot(ws)
            await ws.send(_msg("hello", {"kind": "node"}, frm="r2_rider"))          # no token
            await ws.send(_msg("hello", {"kind": "node"}, frm="not_in_roster"))
            await asyncio.sleep(0.3)
            assert "r2_rider" not in orc._node_ws and "not_in_roster" not in orc._node_ws
            await ws.send(_msg("hello", {"kind": "node", "token": "s3cret"}, frm="r2_rider"))
            await asyncio.sleep(0.3)
            assert "r2_rider" in orc._node_ws
        await asyncio.sleep(0.2)
        assert "r2_rider" not in orc._node_ws                 # cleaned up on close
        server.close(); await server.wait_closed()
    asyncio.run(scenario())


def test_orchestrator_rejects_bad_inputs_directly():
    o = Orchestrator(demo_roster(3, include_music=True), PyMixer(rtp_port=5473))
    assert not o.on_move("ghost", "nav")
    assert not o.on_lead_transfer("music")
    assert not o.on_audio_ctl("r2_rider", trim="x")
    assert o.on_audio_ctl("r2_rider", trim=999) and o.trim["r2_rider"] == 150
    o.on_heartbeat("ghost", {"rssi": 1}); assert "ghost" not in o.node_status
    o.on_heartbeat("r2_rider", "junk"); assert "r2_rider" not in o.node_status
    o.on_gps("nan-ish"); assert o.group_speed_kmh == 0.0
    snap = o.snapshot()
    assert snap["mode"] == "hw" and snap["self_move_ok"] is True
