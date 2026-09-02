"""S-16: the bridge as it runs on a Pi — config from TOML, subprocess audio
adapters (a dead mic never stops the tick), iw link parsing, DeviceActions
dry-run safety (nothing executes until enabled) and headset command guards."""
import asyncio
import os
import sys
import numpy as np
import pytest

from common.audio import FRAME
from bridge import config as cfgmod
from bridge.io_adapters import CmdSource, CmdSink
from bridge.net.linkstats import parse_station_dump, IwLinkStats
from bridge.net.supervisor import EvictionPolicy
from bridge.agent import DeviceActions, SimActions
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink

IW_DUMP = """Station 3c:37:86:1a:2b:3c (on wlan0)
\tinactive time:\t10 ms
\trx bytes:\t123456
\trx packets:\t900
\ttx bytes:\t654321
\ttx packets:\t1200
\ttx retries:\t7
\ttx failed:\t3
\tsignal:  \t-58 [-60, -62] dBm
\tsignal avg:\t-57 dBm
\ttx bitrate:\t173.3 MBit/s VHT-MCS 8 40MHz short GI VHT-NSS 1
\trx bitrate:\t86.7 MBit/s VHT-MCS 4 40MHz short GI VHT-NSS 1
\tauthorized:\tyes
"""


def test_parse_iw_station_dump():
    d = parse_station_dump(IW_DUMP)
    assert d == {"rssi": -57, "tx_rate": 173.3, "tx_failed": 3, "tx_packets": 1200}
    assert parse_station_dump("")["rssi"] is None
    assert parse_station_dump("garbage")["tx_rate"] is None


def test_linkstats_provider_feeds_eviction_without_iw():
    """No `iw` (laptop): radio numbers None -> treated healthy; loss comes
    from the engine; missing data never triggers an eviction."""
    link = IwLinkStats("wlan9", engine=None, runner=lambda: "")
    d = link()
    assert d["rssi"] is None and d["rtp_loss"] is None
    ls = link.as_link_stats()
    assert ls.tx_rate_mbps > 12 and ls.rtp_loss_pct == 0.0
    n = {"e": 0}
    pol = EvictionPolicy(lambda: n.__setitem__("e", 1))
    for _ in range(10):
        pol.tick_1s(ls)
    assert n["e"] == 0
    weak = IwLinkStats("wlan0", runner=lambda: IW_DUMP.replace("173.3", "6.0"))
    assert weak.as_link_stats().tx_rate_mbps == 6.0


def test_engine_downlink_loss_window():
    from bridge.audio.chain import DownlinkChain
    from tests.test_s14_transport_lan import _talker
    eng = BridgeEngine("t", ArraySource(1, []), ArraySink(), prefer_silero=False)
    assert eng.downlink_loss_pct() is None
    pkt = _talker("x")
    for s in range(10):
        if s not in (4, 5):
            eng.down.push_rtp(pkt(s))
        eng.down.pull()
    # 0-3 decoded, 4-5 concealed once 6 arrives, 6-7 decoded, 8-9 still queued
    assert eng.downlink_loss_pct() == 25.0
    assert eng.downlink_loss_pct() is None      # window reset


def test_config_defaults_and_env(tmp_path, monkeypatch):
    p = tmp_path / "convoy.toml"
    p.write_text('[node]\nid = "r3_rider"\nbase = "192.168.1.2"\n[actions]\nenabled = true\n')
    cfg = cfgmod.load(str(p))
    assert cfg.node_id == "r3_rider" and cfg.base_ws == "ws://192.168.1.2:8800/"
    assert cfg.mixer_addr == ("192.168.1.2", 5100) and cfg.actions_enabled
    assert cfg.source_cmd.startswith("pw-record")
    monkeypatch.delenv("CONVOY_NODE_ID", raising=False)
    with pytest.raises(ValueError):
        cfgmod.from_dict({"node": {}})
    with pytest.raises(FileNotFoundError):
        cfgmod.load(str(tmp_path / "missing.toml"))
    ex = os.path.join(os.path.dirname(__file__), "..", "deploy", "convoy.example.toml")
    assert cfgmod.load(ex).node_id == "r2_rider"
    monkeypatch.setenv("CONVOY_NODE_ID", "r9_rider")          # env wins over the file
    assert cfgmod.load(str(p)).node_id == "r9_rider"


def test_cmd_source_frames_and_underrun_never_none():
    """A generator process that writes 5 frames then exits: we get 5 real
    frames, then SILENCE (never None — the engine tick must not end)."""
    gen = (f"{sys.executable} -c \"import sys,struct; "
           f"[sys.stdout.buffer.write(struct.pack('<{FRAME}h', *([1000+i]*{FRAME}))) for i in range(5)]\"")
    src = CmdSource(gen)
    src.start()
    got = []
    for _ in range(60):
        f = src.read()
        assert f is not None and len(f) == FRAME
        if f[0] != 0:
            got.append(int(f[0]))
        if len(got) == 5:
            break
        import time; time.sleep(0.01)
    assert got == [1000, 1001, 1002, 1003, 1004]
    import time; time.sleep(0.2)
    f = src.read()
    assert not src.alive and f is not None and not f.any()
    assert src.underruns >= 1


def test_cmd_sink_broken_pipe_is_counted_not_raised(tmp_path):
    out = tmp_path / "ear.raw"
    sink = CmdSink(f"{sys.executable} -c \"import sys,shutil; shutil.copyfileobj(sys.stdin.buffer, open('{out}','wb'))\"")
    sink.start()
    frame = (np.arange(FRAME) % 100).astype(np.int16)
    for _ in range(3):
        sink.write(frame)
    sink.stop()
    import time; time.sleep(0.3)
    assert sink.frames == 3 and not sink.alive
    sink.write(frame)                                 # dead: counted
    assert sink.errors == 1
    dead = CmdSink("true"); dead.start(); time.sleep(0.2)
    dead.write(frame)
    assert dead.errors >= 1


def test_device_actions_dry_run_until_enabled():
    async def scenario():
        eng = BridgeEngine("t", ArraySource(1, []), ArraySink(), prefer_silero=False)
        a = DeviceActions(engine=eng, enabled=False, headset_mac="AA:BB:CC:DD:EE:FF")
        r = await a.reboot()
        assert r.startswith("DRY-RUN: systemctl reboot")
        assert "bluetoothctl disconnect AA:BB:CC:DD:EE:FF" in await a.reconnect_bt()
        assert "wpa_cli -i wlan0" in await a.reconnect_wifi()
        assert (await a.bt_scan())["headsets"] == []
        assert "bluetoothctl pair" in await a.bt_pair("11:22:33:44:55:66")
        with pytest.raises(ValueError):
            await a.bt_pair("not-a-mac; rm -rf /")
        st = await a.bt_status()
        assert st["headset"] is None and "bluetoothctl info" in st["dry_run"]
        with pytest.raises(RuntimeError):
            await a.say(0)
        assert len(eng.down._earcons) >= 2                 # SAFE-2: state changes audible
        # enabled + a harmless shell: the runner really executes and captures
        b = DeviceActions(engine=None, enabled=True, headset_mac="AA:BB:CC:DD:EE:FF")
        b.cmds = lambda: {"bt_status": "printf 'Name: Cardo\\nPaired: yes\\nConnected: no\\n'",
                          "reboot": "false"}
        st = await b.bt_status()
        assert st["headset"] == {"mac": "AA:BB:CC:DD:EE:FF", "name": "Cardo",
                                 "paired": True, "connected": False}
        with pytest.raises(RuntimeError):
            await b._sh("reboot")
    asyncio.run(scenario())


def test_sim_actions_headset_flow_and_earcons():
    async def scenario():
        eng = BridgeEngine("t", ArraySource(1, []), ArraySink(), prefer_silero=False)
        a = SimActions(engine=eng)
        assert (await a.bt_status())["headset"]["name"] == "Cardo PACKTALK"
        await a.bt_pair("00:11:22:aa:bb:03")               # case-insensitive
        assert a.last_headset["name"] == "X7 headset"
        assert sum(h["connected"] for h in a.headsets) == 1
        await a.ptt(True); assert eng.up.gate.force_open and eng.stats["ptt"]
        await a.ptt(False); assert not eng.up.gate.force_open
        await a.identify()
        n = len(eng.down._earcons)
        assert n >= 4
        # earcons mix AFTER volume (SAFE-2): audible even at minimum volume
        eng.set_volume(10)
        out = np.concatenate([eng.down.pull() for _ in range(n)])
        assert np.abs(out.astype(np.int32)).max() > 1000
    asyncio.run(scenario())
