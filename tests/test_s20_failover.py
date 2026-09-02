"""S-20: star -> hotspot -> tunnel -> star, one second at a time, with every
shell replaced by a recorder. Plus the parsers the real actions rely on and
the runtime re-targeting of engine and agent."""
import asyncio
import time
import pytest

from bridge.net.failover import (LinkFailover, FailoverConfig, Obs, STAR, HOTSPOT, TUNNEL,
                                 WifiActions, parse_nmcli_scan, parse_wg_handshake)
from bridge import config as cfgmod


class Rec:
    def __init__(self):
        self.calls = []
    def join(self, ssid): self.calls.append(("join", ssid))
    def tunnel_up(self): self.calls.append(("tunnel_up",))
    def tunnel_down(self): self.calls.append(("tunnel_down",))
    def set_base(self, host): self.calls.append(("set_base", host))


def cfg(**kw):
    base = dict(star_ssid="convoy", hotspots=["Sam iPhone", "Kate Pixel"], star_base="192.168.1.2",
                tunnel_base="10.66.0.1", fail_s=3, restore_s=4, hotspot_timeout_s=6,
                min_rssi=-78, retry_backoff_s=5)
    base.update(kw)
    return FailoverConfig(**base)


def test_happy_path_star_to_tunnel_and_back():
    a, ears = Rec(), []
    fo = LinkFailover(a, cfg(), earcon=ears.append)
    ok = Obs(base_ok=True, scan={"convoy": -60})
    for _ in range(5):
        assert fo.tick_1s(ok) == STAR
    lost = Obs(base_ok=False, scan={"Sam iPhone": -65})
    assert fo.tick_1s(lost) == STAR and fo.tick_1s(lost) == STAR      # 2 s bad: not yet
    assert fo.tick_1s(lost) == HOTSPOT                                   # 3rd second: leave
    assert a.calls == [("join", "Sam iPhone")] and ears == ["link_lost"]
    hs = Obs(base_ok=False, scan={"Sam iPhone": -65}, internet_ok=True)
    fo.tick_1s(hs)
    assert ("tunnel_up",) in a.calls
    fo.tick_1s(Obs(base_ok=False, scan={"Sam iPhone": -65}, internet_ok=True, tunnel_ok=True))
    assert fo.state == TUNNEL and a.calls[-1] == ("set_base", "10.66.0.1")
    # convoy SSID back and strong for restore_s -> tear down, rejoin, re-point
    back = Obs(base_ok=False, scan={"convoy": -62, "Sam iPhone": -65}, tunnel_ok=True)
    for _ in range(3):
        assert fo.tick_1s(back) == TUNNEL
    assert fo.tick_1s(back) == STAR
    assert a.calls[-3:] == [("tunnel_down",), ("join", "convoy"), ("set_base", "192.168.1.2")]
    assert ears[-1] == "connected" and len(fo.transitions) == 3


def test_weak_star_does_not_trigger_return():
    a = Rec(); fo = LinkFailover(a, cfg())
    for _ in range(3):
        fo.tick_1s(Obs(base_ok=False, scan={"Kate Pixel": -50}))
    fo.tick_1s(Obs(base_ok=False, scan={}, internet_ok=True, tunnel_ok=True))
    assert fo.state == TUNNEL and fo.current_hotspot == "Kate Pixel"
    for _ in range(10):
        fo.tick_1s(Obs(base_ok=False, scan={"convoy": -85}, tunnel_ok=True))   # too weak
    assert fo.state == TUNNEL


def test_hotspot_without_internet_rotates_then_backs_off():
    a = Rec(); fo = LinkFailover(a, cfg())
    both = {"Sam iPhone": -60, "Kate Pixel": -62}
    for _ in range(3):
        fo.tick_1s(Obs(base_ok=False, scan=both))
    assert fo.current_hotspot == "Sam iPhone"
    for _ in range(6):
        fo.tick_1s(Obs(base_ok=False, scan=both))            # no internet on Sam's
    assert fo.current_hotspot == "Kate Pixel" and fo.state == HOTSPOT
    for _ in range(6):
        fo.tick_1s(Obs(base_ok=False, scan=both))            # none on Kate's either
    assert fo.state == STAR and a.calls[-2:] == [("join", "convoy"), ("set_base", "192.168.1.2")]
    # backoff: keeps trying the star for retry_backoff_s before another attempt
    for _ in range(4):
        assert fo.tick_1s(Obs(base_ok=False, scan=both)) == STAR
    for _ in range(5):
        fo.tick_1s(Obs(base_ok=False, scan=both))
    assert fo.state == HOTSPOT


def test_tunnel_loss_falls_back_to_hotspot_state():
    a = Rec(); fo = LinkFailover(a, cfg())
    for _ in range(3):
        fo.tick_1s(Obs(base_ok=False, scan={"Sam iPhone": -60}))
    fo.tick_1s(Obs(base_ok=False, internet_ok=True, tunnel_ok=True))
    assert fo.state == TUNNEL
    for _ in range(3):
        fo.tick_1s(Obs(base_ok=False, tunnel_ok=False))
    assert fo.state == HOTSPOT and a.calls[-1] == ("tunnel_down",)
    fo.tick_1s(Obs(base_ok=False, internet_ok=True))
    assert a.calls[-1] == ("tunnel_up",)                     # re-dials


def test_no_hotspot_visible_stays_on_star():
    a = Rec(); fo = LinkFailover(a, cfg())
    for _ in range(20):
        assert fo.tick_1s(Obs(base_ok=False, scan={"Neighbour": -40})) == STAR
    assert a.calls == []
    assert fo.stats()["state"] == STAR


# -- parsers and shells ----------------------------------------------------------

def test_parse_nmcli_scan():
    txt = "convoy:80\nSam iPhone:45\nweird\\:name:60\n:30\nconvoy:20\nbroken line\n"
    d = parse_nmcli_scan(txt)
    assert d == {"convoy": -60, "Sam iPhone": -78, "weird:name": -70}


def test_parse_wg_handshake():
    now = 1_700_000_000
    txt = f"abc=\t{now - 30}\ndef=\t0\n"
    assert parse_wg_handshake(txt, now) is True
    assert parse_wg_handshake(f"abc=\t{now - 400}\n", now) is False
    assert parse_wg_handshake("", now) is False


def test_wifi_actions_dry_run():
    w = WifiActions(enabled=False)
    w.join("Sam iPhone"); w.tunnel_up(); w.set_base("10.66.0.1"); w.tunnel_down()
    assert w.log == ["nmcli con up id 'Sam iPhone'", "wg-quick up convoy", "wg-quick down convoy"]
    assert w.joined == "Sam iPhone" and w.base == "10.66.0.1" and w.tunnel is False
    assert w.scan() == {} and w.tunnel_ok(time.time()) is False and w.internet_ok() is False


def test_config_radio_and_failover_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[node]\nid="r2_rider"\n[radio]\nmode="auto"\ncallsign="k1abc"\nptt="gpio:17:low"\n'
                 '[failover]\nenabled=true\nhotspots=["Sam iPhone"]\n')
    c = cfgmod.load(str(p))
    assert c.radio_mode == "auto" and c.radio_callsign == "k1abc" and c.radio_ptt == "gpio:17:low"
    assert c.failover_enabled and c.failover_hotspots == ["Sam iPhone"] and c.failover_fail_s == 8
    p.write_text('[node]\nid="r2_rider"\n[radio]\nmode="auto"\n')
    with pytest.raises(ValueError):
        cfgmod.load(str(p))                                   # radio without callsign
    p.write_text('[node]\nid="r2_rider"\n[radio]\nmode="loud"\ncallsign="K1ABC"\n')
    with pytest.raises(ValueError):
        cfgmod.load(str(p))
    ex = cfgmod.load("deploy/convoy.example.toml")
    assert ex.radio_mode == "off" and ex.failover_enabled is False


# -- runtime re-targeting ---------------------------------------------------------

@pytest.mark.realtime
def test_engine_and_agent_retarget_at_runtime():
    from common.roster import demo_roster
    from base.mixer.pymixer import PyMixer
    from base.orc.server import Orchestrator
    from bridge.agent import BridgeAgent, SimActions
    from bridge.engine import BridgeEngine
    from bridge.io_adapters import ArraySource, ArraySink

    async def scenario():
        roster = demo_roster(3, base_port=6900)
        orc_a = Orchestrator(roster, PyMixer(rtp_port=5740))
        orc_b = Orchestrator(demo_roster(3, base_port=6950), PyMixer(rtp_port=5741))
        sa = await orc_a.serve("127.0.0.1", 8893)
        sb = await orc_b.serve("127.0.0.1", 8894)
        eng = BridgeEngine("r2_rider", ArraySource(400, []), ArraySink(),
                           mixer_addr=("127.0.0.1", 5740), down_port=6904, prefer_silero=False)
        agent = BridgeAgent("r2_rider", eng, SimActions(engine=eng), "ws://127.0.0.1:8893/")
        await eng.start(); await agent.start()
        await asyncio.sleep(0.8)
        assert "r2_rider" in orc_a._node_ws and "r2_rider" not in orc_b._node_ws
        eng.set_mixer_addr(("127.0.0.1", 5741))
        agent.set_base_url("ws://127.0.0.1:8894/")
        await asyncio.sleep(2.5)                              # close + reconnect backoff
        assert "r2_rider" in orc_b._node_ws and "r2_rider" not in orc_a._node_ws
        assert eng.stats["mixer"] == "127.0.0.1:5741"
        agent.stop(); eng.stop(); orc_a.mixer.stop(); orc_b.mixer.stop()
        sa.close(); sb.close(); await sa.wait_closed(); await sb.wait_closed()
    asyncio.run(scenario())
