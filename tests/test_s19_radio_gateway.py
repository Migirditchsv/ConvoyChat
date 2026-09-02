"""S-19: the radio fallback end to end on the real stack + a simulated RF
channel. Mixer exclude mask (no music on the air), bike-side failover (helmet
transport swaps to the rig when the base is gone), and the base gateway
(RF speech reaches every helmet through the gate and mixer; the room reaches
the rig, keyed only for speech; half-duplex respected)."""
import asyncio
import numpy as np
import pytest

from common.audio import FRAME, dbfs
from common.dsp import band_db, tone
from common.radio import RadioLink, FakePtt
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from base.media.participants import RtpSource
from base.media.radio import RadioGateway
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink
from bridge.radio import RadioFailover, PREROLL_MAX
from sim.rf import RfChannel, SimRig
from tests.test_s07_mixer import Ear, seg

BAND_A, BAND_B, MUSIC = (500, 900), (1400, 1800), (2200, 2600)
SPEECH = tone(700, 0.06, level_db=-20)


def _noise_frames(f_lo, f_hi, n, seed=1, level_db=-18):
    from common.dsp import noise_band
    x = noise_band(f_lo, f_hi, n * 0.06 + 0.06, level_db=level_db, seed=seed)
    return [x[i * FRAME:(i + 1) * FRAME] for i in range(n)]


# -- mixer exclude ------------------------------------------------------------

@pytest.mark.realtime
def test_exclude_mask_drops_music_for_one_listener():
    PORT = 5720

    async def scenario():
        m = PyMixer(rtp_port=PORT)
        await m.start()
        ear_r, ear_g = Ear(), Ear()
        await ear_r.bind(5721); await ear_g.bind(5722)
        m.add_participant("music", "main", None, gain=60, role="music")
        m.add_participant("a", "main", None)
        m.add_participant("rider", "main", ("127.0.0.1", 5721))
        m.add_participant("radio", "main", ("127.0.0.1", 5722))
        m.set_exclude("radio", {"music", "radio"})          # self is ignored
        assert m.parts["radio"].exclude == {"music"}
        mus = RtpSource.noise("music", *MUSIC, mixer_addr=("127.0.0.1", PORT), seed=21)
        sa = RtpSource.noise("a", *BAND_A, mixer_addr=("127.0.0.1", PORT), seed=22)
        await mus.start(); await sa.start()
        await asyncio.sleep(2.2)
        m.add_participant("radio", "main", ("127.0.0.1", 5722))   # re-populate keeps the mask
        assert m.parts["radio"].exclude == {"music"}
        mus.stop(); sa.stop(); m.stop(); await asyncio.sleep(0.05)
        ear_r.close(); ear_g.close()
        return ear_r.audio(), ear_g.audio(), m.stats()["radio"]["exclude"]

    r, g, ex = asyncio.run(scenario())
    assert ex == ["music"]
    r, g = seg(r, 0.5, 2.0), seg(g, 0.5, 2.0)
    assert band_db(r, *MUSIC) > -40, "rider should hear music"
    assert band_db(g, *BAND_A) > -40, "gateway should hear the talker"
    assert band_db(r, *MUSIC) - band_db(g, *MUSIC) >= 25, "music leaked to the radio listener"


# -- bike-side failover ---------------------------------------------------------

def test_failover_only_keys_when_link_down_and_serialises_preroll():
    rig = SimRig("bike")
    link = RadioLink(rig, "K1ABC", hang_ms=120)
    fo = RadioFailover(link, rig, rig, mode="auto")
    burst = [SPEECH] * 12                                   # a gate opening's pre-roll flush
    assert fo.on_tick(burst, link_up=True) is None and not rig.on and fo._txq == fo._txq.__class__()
    fo.on_tick(burst, link_up=False)
    assert rig.on and rig.frames_tx == 1 and len(fo._txq) == 11   # one frame per tick
    for _ in range(11):
        fo.on_tick([], link_up=False)
    assert rig.frames_tx == 12 and fo.activations == 1
    for _ in range(3):
        fo.on_tick([], link_up=False)                       # hang, then unkey
    assert not rig.on
    fo.on_tick([SPEECH] * (PREROLL_MAX + 5), link_up=False)
    assert fo.dropped == 5 and len(fo._txq) == PREROLL_MAX - 1
    with pytest.raises(ValueError):
        fo.set_mode("loud")
    fo.set_mode("off"); fo.on_tick([SPEECH], link_up=False)
    assert not fo.active
    assert fo.stats()["callsign"] == "K1ABC"


def test_engine_mixes_rf_rx_into_helmet_and_survives_rig_errors():
    async def scenario():
        rig = SimRig("bike")
        rig._deliver(tone(1000, 0.06, level_db=-16)); rig._deliver(tone(1000, 0.06, level_db=-16))
        sink = ArraySink()
        eng = BridgeEngine("t", ArraySource(6, []), sink, prefer_silero=False)
        eng.radio = RadioFailover(RadioLink(rig, "K1ABC"), rig, rig, mode="always")
        eng.link_up = True
        await eng.start(); await eng.wait(); eng.stop()
        audio = sink.audio()
        assert band_db(audio[:FRAME * 2], 900, 1100) > -40      # rig rx audible in the helmet
        # a rig that throws must not stop the tick (SAFE-1 spirit)
        class Bad:
            def read(self): raise OSError("rig unplugged")
            def write(self, f): raise OSError("rig unplugged")
            def key(self, on): pass
        sink2 = ArraySink()
        eng2 = BridgeEngine("t", ArraySource(5, []), sink2, prefer_silero=False)
        eng2.radio = RadioFailover(RadioLink(Bad(), "K1ABC"), Bad(), Bad(), mode="always")
        await eng2.start(); await eng2.wait(); eng2.stop()
        assert len(sink2.frames) == 5
    asyncio.run(scenario())


# -- base gateway over a simulated channel -------------------------------------

@pytest.mark.realtime
def test_gateway_bridges_rf_and_room_half_duplex():
    """Separated rider on an HT (band A) <-RF-> gateway <-> mixer <-> rider in
    a helmet (band B, plus music). Music must never go on the air, RF speech
    must reach the helmet, and the gateway must not key while the RF rider
    is transmitting."""
    PORT = 5730

    async def scenario():
        roster = demo_roster(3, base_port=6800, include_music=True)
        roster.riders["radio"] = type(roster.riders["r2_rider"])(id="radio", role="rider",
                                                                  rooms=["main"], down_port=6810)
        roster.riders["radio"].ssrc = __import__("common.protocol", fromlist=["x"]).ssrc_of("radio")
        mixer = PyMixer(rtp_port=PORT)
        orc = Orchestrator(roster, mixer)
        mixer.set_exclude("radio", {"music"})
        chan = RfChannel(noise_db=-90)
        gw_rig, ht = chan.add(SimRig("gateway")), chan.add(SimRig("ht"))
        gw = RadioGateway("radio", RadioLink(gw_rig, "K1ABC", hang_ms=300), gw_rig, gw_rig,
                          mixer_addr=("127.0.0.1", PORT), down_port=6810, prefer_silero=False,
                          on_vad=lambda o: orc.on_vad("radio", o))
        ear = Ear(); await ear.bind(roster.riders["r2_rider"].down_port)   # helmet rider
        await mixer.start(); await chan.start(); await gw.start()
        music = RtpSource.noise("music", *MUSIC, level_db=-16, mixer_addr=("127.0.0.1", PORT), seed=31)
        helmet = RtpSource.noise("r2_rider", *BAND_B, level_db=-16, mixer_addr=("127.0.0.1", PORT), seed=32)
        await music.start()
        await asyncio.sleep(1.0)                        # music only: gateway must stay unkeyed
        gw_keyed_music = gw.link.key_ups
        # phase 1: the separated rider transmits band A for 2 s
        ht.key(True)
        for f in _noise_frames(*BAND_A, 34, seed=33):
            ht.write(f); await asyncio.sleep(0.06)
        keyed_during_rf = gw.link.key_ups
        ht.key(False)
        await asyncio.sleep(0.6)
        # phase 2: helmet rider talks band B for 2 s -> gateway must key and relay
        await helmet.start()
        heard_on_ht = []
        for _ in range(34):
            heard_on_ht.append(ht.read()); await asyncio.sleep(0.06)
        helmet.stop()
        await asyncio.sleep(1.0)
        keyed_after = gw.link.key_ups
        talking_seen = "radio" in orc.log.__str__()
        music.stop(); gw.stop(); chan.stop(); mixer.stop(); await asyncio.sleep(0.05); ear.close()
        return ear.audio(), np.concatenate(heard_on_ht), gw_keyed_music, keyed_during_rf, keyed_after, gw.stats(), chan.collisions

    helmet_audio, ht_audio, k_music, k_rf, k_after, st, collisions = asyncio.run(scenario())
    assert k_music == 0, "music alone keyed the transmitter"
    assert k_rf == 0, "gateway keyed over the RF rider (half-duplex violated)"
    assert k_after >= 1 and st["tx_s"] >= 1.0, "room speech did not go on the air"
    assert collisions == 0
    # RF rider's band A reached the helmet via gate -> mixer
    rf_in_helmet = seg(helmet_audio, 1.2, 2.8)
    assert band_db(rf_in_helmet, *BAND_A) > -40, f"RF speech not in helmet ({band_db(rf_in_helmet, *BAND_A):.1f})"
    # what went on the air: band B present, music band absent
    assert band_db(ht_audio, *BAND_B) > -40, "helmet speech not on the air"
    assert band_db(ht_audio, *BAND_B) - band_db(ht_audio, *MUSIC) >= 20, "music went on the air"


def test_rf_never_keys_on_a_failed_open_vad_unless_ptt():
    """SAFE-1's final rung is OPEN. Over Wi-Fi that is the right failure; on
    the air it is a stuck transmitter. RF transmits only with a working
    classifier or the rider's thumb."""
    rig = SimRig("bike")
    fo = RadioFailover(RadioLink(rig, "K1ABC"), rig, rig, mode="always")
    fo.on_tick([SPEECH], link_up=False, vad_mode="open", ptt=False)
    assert not rig.on and fo.held_open == 1
    fo.on_tick([SPEECH], link_up=False, vad_mode="open", ptt=True)
    assert rig.on                                          # PTT overrides
    rig2 = SimRig("bike2")
    fo2 = RadioFailover(RadioLink(rig2, "K1ABC"), rig2, rig2, mode="always")
    fo2.on_tick([SPEECH], link_up=False, vad_mode="energy", ptt=False)
    assert rig2.on and fo2.stats()["held_open"] == 0
