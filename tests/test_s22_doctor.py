"""S-22: the operator's issue list, from snapshots to sentences with fixes."""
import asyncio
import pytest
from base.orc.doctor import diagnose, summary
from bridge.agent import parse_throttled, DeviceActions, SimActions


def snap(**over):
    base = {"at": 1000.0, "rooms": ["main", "nav"], "talking": [], "talk_since": {},
            "riders": {"r0_lead": {"role": "lead", "room": "main", "muted": False, "trim": 100},
                       "r2_rider": {"role": "rider", "room": "main", "muted": False, "trim": 100},
                       "music": {"role": "music", "room": "main", "muted": False, "trim": 100}},
            "nodes": {"r0_lead": {"online": True, "age_s": 0.5, "volume": 100, "vad_mode": "silero",
                                  "rssi": -60, "rtp_loss": 0.0, "headset": {"name": "Cardo", "connected": True}},
                      "r2_rider": {"online": True, "age_s": 0.8, "volume": 100, "vad_mode": "silero",
                                   "rssi": -62, "rtp_loss": 0.4, "headset": {"name": "Sena", "connected": True}}},
            "tts_engine": "espeak", "radio": None}
    base.update(over)
    return base


def titles(issues):
    return [i["title"] for i in issues]


def test_all_good():
    assert diagnose(snap()) == [] and summary([]) == "all good"


def test_never_seen_and_offline_short_circuit_other_checks():
    s = snap(); s["nodes"].pop("r2_rider")
    s["nodes"]["r0_lead"] = {"online": False, "age_s": 40, "headset": {"connected": False}, "volume": 10}
    iss = diagnose(s)
    assert titles(iss) == ["r0_lead: bridge offline for 40s", "r2_rider: bridge never connected"]
    assert all(i["level"] == "bad" for i in iss) and all("hint" in f for i in iss for f in i["fixes"])
    assert summary(iss) == "2 problems"


def test_headset_mute_trim_volume_fixes_are_sendable_messages():
    s = snap()
    s["nodes"]["r2_rider"]["headset"] = {"name": "Sena", "connected": False}
    s["riders"]["r2_rider"].update(muted=True, trim=20)
    s["nodes"]["r2_rider"]["volume"] = 10
    iss = diagnose(s)
    by = {i["title"]: i for i in iss}
    hs = by["r2_rider: headset not connected"]
    assert hs["level"] == "bad" and hs["fixes"][0]["msg"] == {"t": "node_cmd", "data": {"target": "r2_rider", "cmd": "reconnect_bt", "args": {}}}
    assert by["r2_rider: muted by the operator"]["fixes"][0]["msg"] == {"t": "audio_ctl", "data": {"pid": "r2_rider", "mute": False}}
    assert by["r2_rider: trimmed down to 20%"]["fixes"][0]["msg"]["data"]["trim"] == 100
    assert by["r2_rider: helmet volume 10%"]["fixes"][0]["msg"]["data"]["args"] == {"pct": 100}
    assert iss[0]["level"] == "bad"                          # sorted: problems first
    assert summary(iss) == "1 problem, 3 warnings"


def test_vad_degraded_weak_link_stuck_talk_and_rooms():
    s = snap(talking=["r2_rider"], talk_since={"r2_rider": 900.0})
    s["nodes"]["r2_rider"].update(vad_mode="open", rssi=-85, rtp_loss=30.0, ptt=True)
    s["riders"]["r0_lead"]["room"] = "nav"
    iss = diagnose(s)
    t = titles(iss)
    assert "r2_rider: voice detector failed open" in t
    assert any(x.startswith("r2_rider: weak Wi-Fi (-85 dBm, 30.0% loss)") for x in t)
    assert "r2_rider: push-to-talk held" in t
    assert "r2_rider: transmitting for 100s" in t
    assert "r0_lead: in room `nav`" in t
    move = next(i for i in iss if i["title"] == "r0_lead: in room `nav`")
    assert move["fixes"][0]["msg"] == {"t": "move", "data": {"pid": "r0_lead", "room": "main", "by": "chase"}}
    s["nodes"]["r2_rider"]["vad_mode"] = "spectral"
    assert "r2_rider: voice detector degraded (spectral)" in titles(diagnose(s))


def test_base_level_issues():
    s = snap(tts_engine="none", radio={"callsign": "", "keyed": False})
    t = titles(diagnose(s))
    assert "announcements unavailable" in t and "radio gateway has no callsign" in t
    s = snap(); s["nodes"]["r2_rider"].update(link_up=False, radio={"active": True, "callsign": "K1ABC"})
    t = titles(diagnose(s))
    assert "r2_rider: reached us over a fallback path" in t and "r2_rider: on the radio (K1ABC)" in t


def test_parse_throttled():
    assert parse_throttled("throttled=0x50005") == {"undervoltage_now": True, "undervoltage_past": True,
                                                    "throttled_now": True, "throttled_past": True}
    assert parse_throttled("throttled=0x0")["undervoltage_now"] is False
    assert parse_throttled("garbage")["undervoltage_past"] is False


def test_device_doctor_reports_remedies():
    async def scenario():
        a = DeviceActions(engine=None, enabled=False, headset_mac="AA:BB:CC:DD:EE:FF")
        outputs = {"vcgencmd": "throttled=0x50000", "bluetoothctl list": "", "iw dev": ""}
        a._run = lambda cmd: next((v for k, v in outputs.items() if k in cmd), "")
        class Cfg:
            source_cmd = "definitely-not-a-binary --x"; sink_cmd = "aplay -q"; wifi_iface = "wlan0"
            radio_mode = "auto"; radio_callsign = ""
        r = await a.doctor(cfg=Cfg(), agent_connected=False, source_alive=False, sink_alive=True)
        by = {c["name"]: c for c in r["checks"]}
        assert r["ok"] is False
        assert by["power"]["ok"] and "earlier" in by["power"]["detail"]
        assert not by["bluetooth dongle"]["ok"] and "dongle" in by["bluetooth dongle"]["remedy"]
        assert not by["headset"]["ok"]
        assert not by["mic command"]["ok"] and "definitely-not-a-binary" in by["mic command"]["remedy"]
        assert not by["mic pipe"]["ok"] and by["speaker pipe"]["ok"]
        assert not by["wifi"]["ok"] and not by["base link"]["ok"]
        assert not by["callsign"]["ok"]
        sim = SimActions()
        assert (await sim.doctor())["checks"][0]["ok"]
    asyncio.run(scenario())
