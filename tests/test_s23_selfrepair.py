"""S-23: self-repair plumbing — operator settings survive a base restart and
volumes are re-pushed on join; the systemd watchdog is fed only while the
tick advances; the headset supervisor reconnects with backoff."""
import asyncio
import json
import os
import socket
import pytest

from common.roster import demo_roster
from common.sdnotify import SdNotify
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from bridge.net.supervisor import HeadsetSupervisor


def test_state_round_trip(tmp_path):
    path = str(tmp_path / "state.json")
    o = Orchestrator(demo_roster(4, include_music=True), PyMixer(rtp_port=5760))
    o.state_path = path
    o.on_audio_ctl("r2_rider", mute=True)
    o.on_audio_ctl("r3_rider", trim=40)
    o.on_move("r3_rider", "nav")
    o.on_lead_transfer("r2_rider")
    o.on_heartbeat("r3_rider", {"volume": 130})
    st = json.load(open(path))
    assert st["muted"] == ["r2_rider"] and st["trim"]["r3_rider"] == 40
    assert st["rooms"]["r3_rider"] == "nav" and st["lead"] == "r2_rider" and st["volumes"]["r3_rider"] == 130
    # a fresh base (crash, restart) restores the ride
    o2 = Orchestrator(demo_roster(4, include_music=True), PyMixer(rtp_port=5761))
    o2.state_path = path
    assert o2.load_state()
    assert "r2_rider" in o2.muted and o2.trim["r3_rider"] == 40
    assert o2.roster.riders["r3_rider"].rooms[0] == "nav" and o2.mixer.parts["r3_rider"].room == "nav"
    assert o2.roster.riders["r2_rider"].role == "lead" and o2.roster.riders["r0_lead"].role == "rider"
    assert o2.mixer.broadcast_pid == "r2_rider"
    assert o2.mixer.parts["r2_rider"].gain == 0 and o2.mixer.parts["r3_rider"].gain == 40
    assert o2.volumes == {"r3_rider": 130}
    # junk or missing state is ignored, never fatal
    (tmp_path / "bad.json").write_text("{not json")
    o3 = Orchestrator(demo_roster(3), PyMixer(rtp_port=5762))
    assert o3.load_state(str(tmp_path / "bad.json")) is False
    assert o3.load_state(str(tmp_path / "missing.json")) is False
    json.dump({"v": 1, "muted": ["ghost"], "rooms": {"r2_rider": "not_a_room"}, "lead": "music"},
              open(str(tmp_path / "odd.json"), "w"))
    assert o3.load_state(str(tmp_path / "odd.json"))
    assert o3.muted == set() and o3.roster.riders["r2_rider"].rooms[0] == "main"


@pytest.mark.realtime
def test_volume_repushed_on_join(tmp_path):
    from bridge.agent import BridgeAgent, SimActions
    from bridge.engine import BridgeEngine
    from bridge.io_adapters import ArraySource, ArraySink

    async def scenario():
        roster = demo_roster(3, base_port=6960)
        orc = Orchestrator(roster, PyMixer(rtp_port=5763))
        orc.state_path = str(tmp_path / "s.json")
        orc.volumes["r2_rider"] = 140                      # remembered from a previous ride
        server = await orc.serve("127.0.0.1", 8892)
        eng = BridgeEngine("r2_rider", ArraySource(300, []), ArraySink(),
                           mixer_addr=("127.0.0.1", 5763), down_port=6964, prefer_silero=False)
        await eng.start()
        agent = BridgeAgent("r2_rider", eng, SimActions(engine=eng), "ws://127.0.0.1:8892/")
        await agent.start()
        await asyncio.sleep(1.2)
        assert eng.down.volume_pct == 140, "volume not re-pushed on join"
        agent.stop(); eng.stop(); orc.mixer.stop(); server.close(); await server.wait_closed()
    asyncio.run(scenario())


def test_sdnotify_datagrams(tmp_path):
    path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path); srv.settimeout(1.0)
    sd = SdNotify(path)
    assert sd.active and sd.ready() and sd.watchdog() and sd.status("all good")
    got = [srv.recv(256).decode() for _ in range(3)]
    assert got == ["READY=1", "WATCHDOG=1", "STATUS=all good"]
    srv.close()
    off = SdNotify("")                                  # no NOTIFY_SOCKET: harmless no-op
    assert not off.active and off.watchdog() is False and off.sent == ["WATCHDOG=1"]
    gone = SdNotify(str(tmp_path / "nope.sock"))
    assert not gone.active


def test_headset_supervisor_reconnects_with_backoff():
    calls = []
    ears = []
    hs = HeadsetSupervisor(lambda: calls.append("reconnect"), down_s=3, backoff_s=4,
                           max_attempts=2, earcon_action=ears.append)
    for _ in range(5):
        assert hs.tick_1s(True) is False
    assert hs.tick_1s(None) is False                     # unknown: never act
    assert [hs.tick_1s(False) for _ in range(3)] == [False, False, True]
    assert [hs.tick_1s(False) for _ in range(4)] == [False] * 4      # backoff
    assert hs.tick_1s(False) is True and hs.attempts == 2
    for _ in range(10):
        assert hs.tick_1s(False) is False                # max attempts reached
    assert hs.tick_1s(True) is False and hs.repairs == 1 and ears == ["connected"]
    assert hs.attempts == 0                              # ready for the next outage
