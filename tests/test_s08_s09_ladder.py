"""S-08 duck depth/latency via orchestrator ladder; S-09 heard-once broadcast.
Probes are noise bands (see S-07 header)."""
import asyncio
import numpy as np
import pytest
from common.audio import FRAME
from common.dsp import band_db
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from base.media.participants import RtpSource
from tests.test_s07_mixer import Ear, seg

MUSIC_BAND = (300, 1200)
LEAD_BAND = (600, 1000)


@pytest.mark.realtime
def test_s08_duck_latency_and_recovery():
    PORT = 5410

    async def scenario():
        roster = demo_roster(3, base_port=6200, include_music=True)
        m = PyMixer(rtp_port=PORT)
        orc = Orchestrator(roster, m)
        ear = Ear(); await ear.bind(roster.riders["r2_rider"].down_port)
        await m.start()
        music = RtpSource.noise("music", *MUSIC_BAND, level_db=-16,
                                mixer_addr=("127.0.0.1", PORT))
        await music.start()
        await asyncio.sleep(1.5)
        orc.on_vad("r0_lead", True)
        await asyncio.sleep(1.0)
        orc.on_vad("r0_lead", False)
        await asyncio.sleep(1.6)
        music.stop(); m.stop(); ear.close()
        await asyncio.sleep(0.05)
        return ear.audio()

    audio = asyncio.run(scenario())
    n = len(audio) // FRAME
    env = np.array([band_db(audio[i*FRAME:(i+1)*FRAME], *MUSIC_BAND)
                    for i in range(n)])
    base = np.median(env[8:22])                    # 0.5-1.3 s: steady music
    after = np.where(env[22:] < base - 10)[0]      # search after talk begins
    assert len(after) > 0, "music never ducked >=10 dB"
    t_duck = (22 + after[0]) * 0.06
    assert t_duck <= 1.5 + 0.35, f"duck at {t_duck:.2f}s vs talk ~1.5s (+250ms budget)"
    dur = np.sum(env[22:] < base - 10) * 0.06
    assert dur >= 0.7, f"duck persisted only {dur:.2f}s through a 1s talk spurt"
    assert np.median(env[-8:]) >= base - 3, "music did not recover after hangover"


@pytest.mark.realtime
def test_s09_lead_heard_once_everywhere():
    PORT = 5420

    async def scenario():
        roster = demo_roster(4, base_port=6300)
        roster.riders["r3_rider"].rooms[0] = "nav"
        m = PyMixer(rtp_port=PORT)
        orc = Orchestrator(roster, m)
        orc.on_move("r3_rider", "nav")
        m.move("r0_lead", "lead")                  # lead alone in LEAD room
        ear_main = Ear(); await ear_main.bind(roster.riders["r2_rider"].down_port)
        ear_nav = Ear(); await ear_nav.bind(roster.riders["r3_rider"].down_port)
        await m.start()
        lead = RtpSource.noise("r0_lead", *LEAD_BAND, level_db=-16,
                               mixer_addr=("127.0.0.1", PORT))
        await lead.start()
        await asyncio.sleep(2.4)
        lead.stop(); m.stop(); ear_main.close(); ear_nav.close()
        await asyncio.sleep(0.05)
        return ear_main.audio(), ear_nav.audio()

    a_main, a_nav = asyncio.run(scenario())
    lv_main = band_db(seg(a_main, 0.5, 2.0), *LEAD_BAND)
    lv_nav = band_db(seg(a_nav, 0.5, 2.0), *LEAD_BAND)
    assert lv_main > -30 and lv_nav > -30, f"lead inaudible ({lv_main:.1f}/{lv_nav:.1f})"
    assert abs(lv_main - lv_nav) <= 1.5, \
        f"rooms differ {abs(lv_main-lv_nav):.1f} dB (double-add or missing tee)"
