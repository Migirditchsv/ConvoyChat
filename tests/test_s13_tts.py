"""S-13: text -> TTS -> announcement, end to end: the dashboard's text
message renders speech, streams it into the room as the announce
participant, ducks music while speaking, and restores after."""
import asyncio
import numpy as np
import pytest
from common.audio import dbfs
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from base.media.participants import QueuedRtpSource, RtpSource
from base.media import tts
from tests.test_s07_mixer import Ear, seg


def test_tts_render_engine_present():
    pcm = asyncio.run(tts.render("gravel on the left"))
    assert len(pcm) > 8000 and dbfs(pcm) > -30
    assert tts.engine_name() in ("espeak", "piper")


@pytest.mark.realtime
def test_announcement_audible_and_ducks_music():
    PORT = 5620

    async def scenario():
        roster = demo_roster(3, base_port=6700, include_music=True)
        mixer = PyMixer(rtp_port=PORT)
        orc = Orchestrator(roster, mixer)
        announce = QueuedRtpSource("announce", mixer_addr=("127.0.0.1", PORT),
                                   on_state=orc.set_announcing)
        mixer.add_participant("announce", "main", None, gain=100, role="rider")
        orc.attach_announce(announce)
        ear = Ear(); await ear.bind(roster.riders["r2_rider"].down_port)
        await mixer.start(); await announce.start()
        music = RtpSource.noise("music", 300, 1200, level_db=-16,
                                mixer_addr=("127.0.0.1", PORT))
        await music.start()
        await asyncio.sleep(1.2)
        await orc.on_text("ui", "radio check convoy", speak=True)
        gain_during = None
        for _ in range(50):                      # sample music gain mid-speech
            await asyncio.sleep(0.1)
            if orc._announcing:
                gain_during = mixer.parts["music"].gain
                break
        while announce.playing or not announce._q.empty():
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.8)
        gain_after = mixer.parts["music"].gain
        texts = orc.snapshot()["texts"]
        await asyncio.sleep(0.6)
        music.stop(); announce.stop(); mixer.stop(); ear.close()
        await asyncio.sleep(0.05)
        return ear.audio(), gain_during, gain_after, texts

    audio, g_during, g_after, texts = asyncio.run(scenario())
    assert g_during is not None and g_during <= 25, f"music not ducked ({g_during})"
    assert g_after == 60, f"music gain not restored ({g_after})"
    assert texts and texts[-1]["spoken"] and texts[-1]["msg"] == "radio check convoy"
    # the announcement itself must be audible in a rider's ear: compare the
    # window right after on_text against pre-text music-only level shape
    pre = seg(audio, 0.4, 1.1)
    talk = seg(audio, 1.6, 2.6)
    assert dbfs(talk) > dbfs(pre) - 3 and dbfs(talk) > -30, \
        f"announcement inaudible (pre {dbfs(pre):.1f}, talk {dbfs(talk):.1f})"
