"""S-11-lite: control protocol sanity + orchestrator input hardening."""
import pytest
from common.protocol import make_msg, parse_msg
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator


def test_envelope_roundtrip_and_rejects():
    m = parse_msg(make_msg("vad", "r0", {"open": True}))
    assert m["t"] == "vad" and m["data"]["open"] is True
    with pytest.raises(ValueError):
        parse_msg('{"v":1,"t":"format_disk","from":"x","data":{}}')
    with pytest.raises(ValueError):
        parse_msg('{"v":9,"t":"vad","from":"x","data":{}}')


def test_ladder_rider_ducks_only_music():
    from base.orc import ladder
    parts = {"l": "lead", "c": "chase", "r1": "rider", "r2": "rider", "m": "music"}
    g = ladder.gains_for("rider", parts)
    assert g["m"] == 25 and g["r2"] == 100 and g["l"] == 100, g


def test_orchestrator_hardening():
    r = demo_roster(3)
    o = Orchestrator(r, PyMixer(rtp_port=5460))
    assert not o.on_move("r2_rider", "not_a_room")
    o.on_gps(60.0)
    assert not o.on_move("r2_rider", "nav", by_ui="self")   # moving: self-move denied
    o.on_gps(0.0)
    assert o.on_move("r2_rider", "nav", by_ui="self")
    assert not o.on_lead_transfer("ghost")
    assert o.on_lead_transfer("r2_rider")
    snap = o.snapshot()
    assert snap["riders"]["r2_rider"]["role"] == "lead"
    o.on_vad("ghost", True)   # unknown speaker: ignored, no crash
