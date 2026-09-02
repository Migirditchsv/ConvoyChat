"""S-12b: remote debug plane over real WebSockets — heartbeat/last-contact,
audio_ctl composing with the ladder, node commands with acks, hb-tone."""
import asyncio
import pytest
from common.protocol import make_msg, parse_msg
from common.roster import demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from bridge.agent import BridgeAgent, SimActions
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink

PORT = 8899


class FakeLink:
    def __call__(self):
        return {"rssi": -61, "tx_rate": 72.2, "rtp_loss": 0.4}


@pytest.mark.realtime
def test_debug_plane_end_to_end():
    async def scenario():
        roster = demo_roster(3, base_port=6500)
        mixer = PyMixer(rtp_port=5490)
        orc = Orchestrator(roster, mixer)
        server = await orc.serve("127.0.0.1", PORT)

        # a real engine (idle audio) so volume/hb-tone hooks are the real ones
        eng = BridgeEngine("r2_rider", ArraySource(200, []), ArraySink(),
                           mixer_addr=("127.0.0.1", 5490), down_port=6504,
                           prefer_silero=False)
        await eng.start()
        actions = SimActions(engine=eng)
        agent = BridgeAgent("r2_rider", eng, actions,
                            f"ws://127.0.0.1:{PORT}/", link_stats=FakeLink())
        await agent.start()
        await asyncio.sleep(1.6)   # hello + a heartbeat or two

        # 1) heartbeat -> last contact fresh, link stats present
        snap = orc.snapshot()
        st = snap["nodes"]["r2_rider"]
        assert st["age_s"] <= 1.5 and st["rssi"] == -61

        # 2) audio_ctl composes with the ladder
        orc.on_audio_ctl("r2_rider", trim=50)
        orc.on_vad("r0_lead", True)
        assert mixer.parts["r2_rider"].gain == 12          # 25 * 50%
        orc.on_audio_ctl("r2_rider", mute=True)
        assert mixer.parts["r2_rider"].gain == 0
        orc.on_audio_ctl("r2_rider", mute=False)
        assert mixer.parts["r2_rider"].gain == 12
        orc.on_vad("r0_lead", False)
        await asyncio.sleep(0.6)                            # hangover restore
        assert mixer.parts["r2_rider"].gain == 50           # 100 * 50%

        # 3) node command roundtrip with ack + real engine effect
        cid = await orc.send_node_cmd("r2_rider", "adjust_volume", {"delta": 20})
        await asyncio.sleep(0.4)
        assert orc.acks[cid]["ok"] and "120%" in orc.acks[cid]["detail"]
        assert eng.down.volume_pct == 120
        assert ("adjust_volume", 20) in actions.calls

        # 4) hb tone toggle reaches the engine
        cid = await orc.send_node_cmd("r2_rider", "set_hb_tone", {"on": True})
        await asyncio.sleep(0.3)
        assert orc.acks[cid]["ok"] and eng.hb_tone is True

        # 5) unknown command -> clean failure ack; disconnected target -> instant fail
        cid = await orc.send_node_cmd("r2_rider", "format_disk", {})
        await asyncio.sleep(0.3)
        assert orc.acks[cid]["ok"] is False
        cid = await orc.send_node_cmd("ghost", "reboot", {})
        assert orc.acks[cid]["ok"] is False and "not connected" in orc.acks[cid]["detail"]

        agent.stop(); eng.stop(); mixer.stop()
        server.close(); await server.wait_closed()

    asyncio.run(scenario())


def test_hb_tone_ticks_into_downlink():
    """Engine emits the soft alive-tick into the helmet mix when enabled."""
    async def scenario():
        sink = ArraySink()
        eng = BridgeEngine("t", ArraySource(90, []), sink,
                           mixer_addr=("127.0.0.1", 59999), down_port=0,
                           prefer_silero=False)
        eng.set_hb_tone(True)
        eng._hb_every = 10                       # tick every 0.6 s for the test
        await eng.start(); await eng.wait(); eng.stop()
        import numpy as np
        from common.audio import dbfs
        audio = sink.audio()
        assert dbfs(audio) > -60, "no ticks audible in downlink"
        loud = np.where(np.abs(audio.astype(np.int32)) > 200)[0]
        assert len(loud) > 0 and (loud[-1] - loud[0]) > 16000, "ticks not periodic"
    asyncio.run(scenario())
