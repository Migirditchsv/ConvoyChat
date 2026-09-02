"""`make demo` — a scripted 40-second ride on the REAL stack, built to be
seen and heard.

While it runs: the live dashboard is at http://localhost:8080 — talking
badges flicker, meters move, and every control (mute, trim, vol, identify,
reboot) works against the real bridges mid-ride.

Afterwards, demo_out/ holds what each rider actually heard (ears/*.wav),
what their mic actually picked up (mouths/*.wav), and a timeline.txt of
every gate opening, duck, move and ack — the listening guide is printed at
the end and written to demo_out/README.txt.

Nothing here is mocked except mouths and ears: real gates, real Opus/RTP,
real mixer, real orchestrator, real agents, real impairment.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import time

import numpy as np

from common.audio import FS, FRAME, FRAME_MS, write_wav, dbfs
from common.dsp import band_db
from common.protocol import CONTROL_PORT
from common.roster import demo_roster
from base.main import _serve_static, UI_PORT
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from base.media.participants import RtpSource
from bridge.agent import BridgeAgent, SimActions
from bridge.engine import BridgeEngine
from bridge.io_adapters import ArraySource, ArraySink
from sim import fixtures
from sim.impair import ImpairProxy

OUT = "demo_out"
DUR_S = 40.0
RTP_PORT = 5100

# (rider, t_start_s, clip_index, line for the narration)
SCRIPT = [
    ("r2_rider", 5.0, 0, "a rider calls out — hear music dip under them"),
    ("r3_rider", 9.0, 3, "second rider answers from the same wind"),
    ("r0_lead", 14.0, 2, "LEAD talks over everyone — riders duck, music nearly vanishes"),
    ("r1_chase", 21.0, 1, "CHASE CAR emergency — everything else goes silent"),
    ("r0_lead", 31.0, 4, "lead again after r3 moved rooms — broadcast reaches BOTH rooms"),
]
MOVE_AT, MOVE_WHO, MOVE_TO = 27.0, "r3_rider", "nav"
WIND_KMH = {"r0_lead": 90, "r1_chase": 50, "r2_rider": 90, "r3_rider": 90, "r4_rider": 120}


def make_music(dur_s: float) -> np.ndarray:
    """A gentle synth arpeggio (A minor add9), so ducking is pleasant to hear."""
    notes = [220.0, 261.63, 329.63, 440.0, 329.63, 261.63]
    step = 0.25
    n = int(dur_s * FS)
    out = np.zeros(n)
    t_note = np.arange(int(step * FS)) / FS
    for k in range(int(dur_s / step)):
        f = notes[k % len(notes)]
        env = np.exp(-t_note * 6.0)
        tone = (np.sin(2 * np.pi * f * t_note)
                + 0.4 * np.sin(2 * np.pi * 2 * f * t_note)) * env
        i = int(k * step * FS)
        out[i:i + len(tone)] += tone[: n - i]
    out = out / np.abs(out).max() * 32767 * 10 ** (-18 / 20)
    return out.astype(np.int16)


def build_mouth(rider: str, clips: list[np.ndarray]) -> np.ndarray:
    """A rider's microphone truth: continuous wind bed for their speed with
    scheduled Harvard utterances on top, through the headset chain."""
    speed = WIND_KMH[rider]
    wind, _ = fixtures._wind_take(DUR_S, speed, seed=hash(rider) % 97)
    wind = fixtures._calibrate(wind, fixtures.WIND_DB[speed]).astype(np.float64)
    for who, t0, ci, _ in SCRIPT:
        if who != rider:
            continue
        sp = fixtures._calibrate(clips[ci], fixtures.SPEECH_DB)
        i = int(t0 * FS)
        wind[i:i + len(sp)] += sp[: len(wind) - i]
    return fixtures.headset_sim(np.clip(wind, -32768, 32767).astype(np.int16))


async def main():
    fixtures.build()
    clips, _ = fixtures._speech_clips()
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(f"{OUT}/ears"); os.makedirs(f"{OUT}/mouths")

    roster = demo_roster(5, base_port=6600, include_music=True)
    mixer = PyMixer(rtp_port=RTP_PORT)

    # r4 rides at the ragged edge: downlink goes through a flapping link
    r4 = roster.riders["r4_rider"]
    real_r4_down = r4.down_port
    flap_down = ImpairProxy(("127.0.0.1", real_r4_down), "flap")
    r4.down_port = await flap_down.start()
    flap_down.p = dict(flap_down.p, blackout=(4.0, 18.0))  # audible mid-ride, 3x

    orc = Orchestrator(roster, mixer)
    await mixer.start()
    ws_server = await orc.serve("0.0.0.0", CONTROL_PORT)
    http = await _serve_static()
    orc.on_gps(90.0)

    events: list[str] = []
    t0 = time.monotonic()

    def note(msg: str):
        line = f"[{time.monotonic()-t0:5.1f}s] {msg}"
        events.append(line)
        print(line, flush=True)

    bridges, agents, sinks, proxies = {}, {}, {}, [flap_down]
    total_frames = int(DUR_S * 1000 / FRAME_MS)
    for rid, r in roster.riders.items():
        if r.role == "music":
            continue
        mouth = build_mouth(rid, clips)
        write_wav(f"{OUT}/mouths/{rid}.wav", mouth)
        src_frames = [(0, mouth)]
        proxy = ImpairProxy(("127.0.0.1", RTP_PORT),
                            "edge" if rid == "r4_rider" else "parkinglot",
                            seed=r.ssrc & 0xFF)
        pport = await proxy.start(); proxies.append(proxy)
        sink = ArraySink(); sinks[rid] = sink
        down = real_r4_down if rid == "r4_rider" else r.down_port
        eng = BridgeEngine(rid, ArraySource(total_frames, src_frames), sink,
                           mixer_addr=("127.0.0.1", pport), down_port=down,
                           prefer_silero=True,
                           on_vad=(lambda o, i=rid: (orc.on_vad(i, o),
                                                     note(f"gate {'OPEN ' if o else 'close'} {i}"))))
        eng.up.set_speed(WIND_KMH[rid])
        bridges[rid] = eng
        link = {"r0_lead": (-58, 86), "r1_chase": (-52, 130), "r2_rider": (-63, 57),
                "r3_rider": (-66, 43), "r4_rider": (-79, 14)}[rid]
        agents[rid] = BridgeAgent(
            rid, eng, SimActions(engine=eng), f"ws://127.0.0.1:{CONTROL_PORT}/",
            link_stats=lambda l=link: {"rssi": l[0] + int(np.random.default_rng().integers(-2, 3)),
                                       "tx_rate": l[1], "rtp_loss": round(float(np.random.default_rng().random()*2), 1)})

    music = RtpSource("music", make_music(8.0), loop_audio=True,
                      mixer_addr=("127.0.0.1", RTP_PORT))

    print("\n" + "=" * 64)
    print("CONVOY DEMO — 40 seconds on the real stack")
    print(f"  watch:  http://localhost:{UI_PORT}   (mute/trim/vol/identify work live)")
    print("  then :  demo_out/ears/*.wav is what each rider heard")
    print("=" * 64 + "\n")

    for eng in bridges.values():
        await eng.start()
    for ag in agents.values():
        await ag.start()
    await music.start()
    note("ride begins — music (synth arpeggio) in room `main`, wind on every mic")
    await orc.send_node_cmd("r2_rider", "set_hb_tone", {"on": True})
    note("hb-tone enabled on r2_rider — soft 880 Hz tick every 5 s in their ear")

    async def director():
        for who, t_at, _, line in SCRIPT:
            await asyncio.sleep(max(0.0, t_at - (time.monotonic() - t0)))
            note(f"SCRIPT: {who} speaks — {line}")
        # remote-debug beat at the end
        await asyncio.sleep(max(0.0, 35.5 - (time.monotonic() - t0)))
        cid = await orc.send_node_cmd("r3_rider", "adjust_volume", {"delta": 20})
        await asyncio.sleep(0.5)
        ack = orc.acks.get(cid, {})
        note(f"remote debug: vol +20 on r3_rider -> ack ok={ack.get('ok')} ({ack.get('detail')})")

    async def mover():
        await asyncio.sleep(max(0.0, MOVE_AT - (time.monotonic() - t0)))
        orc.on_move(MOVE_WHO, MOVE_TO)
        note(f"ROOM MOVE: {MOVE_WHO} -> `{MOVE_TO}` — their music and rider chatter stop;"
             f" only the lead's broadcast will reach them")

    d = asyncio.create_task(director())
    mv = asyncio.create_task(mover())
    await asyncio.gather(*(b.wait() for b in bridges.values()))
    d.cancel(); mv.cancel()
    note("ride ends")

    music.stop()
    for ag in agents.values(): ag.stop()
    for b in bridges.values(): b.stop()
    for p in proxies: p.stop()
    mixer.stop(); ws_server.close(); http.close()
    await asyncio.sleep(0.1)

    for rid, sink in sinks.items():
        write_wav(f"{OUT}/ears/{rid}.wav", sink.audio())
    for line in orc.log:
        events.append(f"[orc] {line}")
    with open(f"{OUT}/timeline.txt", "w") as f:
        f.write("\n".join(events) + "\n")

    guide = f"""CONVOY DEMO — listening guide
=============================

ears/r3_rider.wav   THE ONE TO PLAY FIRST. Music ducks under each speaker,
                    vanishes for the chase car's emergency at ~21 s, then at
                    ~27 s r3 is moved to room `nav`: music and chatter cut
                    out — and at ~31 s the LEAD's broadcast still reaches
                    them. Rooms + priority + heard-once, audible in one file.
ears/r2_rider.wav   Same ride from `main`, plus the soft 880 Hz alive-tick
                    every 5 s (heartbeat tone), and vol/ident earcons if you
                    clicked the dashboard.
ears/r4_rider.wav   The straggler: their downlink flaps 5 s per minute —
                    hear concealment then dropout then recovery.
mouths/r2_rider.wav What r2's mic actually picked up: 90 km/h wind bed with
                    the utterance buried in it. A/B against any ear file =
                    what the gate + mixer removed.
mouths/r4_rider.wav 120 km/h wind, no speech — the gate transmitted none of
                    this (check timeline.txt: no `gate OPEN r4_rider`).
timeline.txt        Every gate opening, duck, room move and command ack with
                    timestamps.

Every ear file is mono 16 kHz — phone-call bandwidth, exactly what a helmet
headset will receive over HFP (INV-2). Rerun with the dashboard open to
drive mute / trim / volume / identify live: `make demo`.
"""
    with open(f"{OUT}/README.txt", "w") as f:
        f.write(guide)
    print("\n" + guide)


if __name__ == "__main__":
    asyncio.run(main())
