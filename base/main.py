"""Base station entrypoint: mixer + orchestrator WS (:8800) + dashboard HTTP
(:8080), one process. `python3 -m base.main --roster roster.yaml` or
`make base` for a demo roster."""
from __future__ import annotations
import argparse
import asyncio
import os

from common.protocol import CONTROL_PORT
from common.roster import load_roster, demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from base.media.participants import QueuedRtpSource

STATIC = os.path.join(os.path.dirname(__file__), "ui", "static")
UI_PORT = 8080


async def _serve_static(host: str = "0.0.0.0", port: int = UI_PORT):
    """Minimal HTTP static server (stdlib-only; the page is one file)."""
    async def client(reader, writer):
        try:
            req = await asyncio.wait_for(reader.readline(), 5)
            path = req.split()[1].decode() if len(req.split()) > 1 else "/"
            while (await asyncio.wait_for(reader.readline(), 5)).strip():
                pass
            fn = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, fn))
            if full.startswith(STATIC) and os.path.isfile(full):
                body = open(full, "rb").read()
                ctype = "text/html" if fn.endswith(".html") else "application/octet-stream"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode()
                             + b"\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\nCache-Control: no-store\r\n\r\n" + body)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    return await asyncio.start_server(client, host, port)


async def _monitor(mixer: PyMixer, port: int = 7100):
    """--monitor: the base machine's own speakers join room `main` as a
    listener, so announcements/music/riders are audible with no bridges.
    Uses paplay/aplay raw pipes; falls back to writing monitor.wav."""
    import shutil as _sh
    import subprocess
    import numpy as np
    from common.audio import FRAME
    from common.opusbind import Decoder
    from common.protocol import rtp_unpack
    from bridge.io_adapters import UdpPort

    mixer.add_participant("monitor", "main", ("127.0.0.1", port), role="rider")
    dec = Decoder()
    player = None
    for cand in (["paplay", "--raw", "--rate=16000", "--format=s16le", "--channels=1"],
                 ["aplay", "-q", "-r", "16000", "-f", "S16_LE", "-c", "1", "-t", "raw"],
                 ["ffplay", "-nodisp", "-loglevel", "quiet", "-f", "s16le",
                  "-ar", "16000", "-ch_layout", "mono", "-i", "pipe:0"]):
        if _sh.which(cand[0]):
            player = subprocess.Popen(cand, stdin=subprocess.PIPE)
            print(f"monitor: playing room `main` via {cand[0]}")
            break
    sink_file = None
    if player is None:
        sink_file = open("monitor.raw", "wb")
        print("monitor: no audio player found — writing monitor.raw "
              "(s16le/16k/mono); install pulseaudio-utils or alsa-utils to hear it")
    udp = UdpPort()

    def rx(data, addr):
        try:
            _, _, _, _, payload = rtp_unpack(data)
        except ValueError:
            return
        pcm = dec.decode(payload, FRAME).tobytes()
        try:
            (player.stdin if player else sink_file).write(pcm)
            (player.stdin if player else sink_file).flush()
        except Exception:
            pass

    udp.on_packet = rx
    await udp.bind("127.0.0.1", port)
    return udp


async def main(roster_path: str | None, monitor: bool = False):
    roster = load_roster(roster_path) if roster_path else demo_roster(6, include_music=True)
    mixer = PyMixer()
    orc = Orchestrator(roster, mixer)

    # announcements: always wired, so the dashboard's "speak" works day one
    announce = QueuedRtpSource("announce",
                               mixer_addr=("127.0.0.1", mixer.rtp_port),
                               on_state=orc.set_announcing)
    mixer.add_participant("announce", "main", None, gain=100, role="rider")
    orc.attach_announce(announce)

    await mixer.start()
    await announce.start()
    mon = await _monitor(mixer) if monitor else None
    ws = await orc.serve("0.0.0.0", CONTROL_PORT)
    http = await _serve_static()
    from base.media import tts
    print(f"base up: mixer rtp:{mixer.rtp_port}  control ws:{CONTROL_PORT}  "
          f"dashboard http:{UI_PORT}  tts:{tts.engine_name()}"
          f"{'  monitor:ON (room main -> speakers)' if monitor else ''}\n"
          f"riders: {', '.join(roster.riders)}\n"
          f"try it: open http://localhost:{UI_PORT}, type in the text bar, "
          f"tick nothing — just press send (speak is on by default).")
    try:
        await asyncio.Event().wait()
    finally:
        mixer.stop(); announce.stop(); ws.close(); http.close()
        if mon: mon.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", default=None)
    ap.add_argument("--monitor", action="store_true",
                    help="play room `main` through this machine's speakers")
    args = ap.parse_args()
    asyncio.run(main(args.roster, args.monitor))
