"""Base station entrypoint: mixer + orchestrator WS (:8800) + web pages
(:8080) in one process.

    python3 -m base.main --mode sim            # everything on one laptop (virtual riders)
    python3 -m base.main --mode hw --roster roster.yaml    # real bridges, verbose
    python3 -m base.main --mode field --roster roster.yaml # quiet, for systemd

Pages (plain HTTP on the LAN, INV-10): /  landing   /rider  phone page
/ops  operator dashboard   /snapshot.json  state for curl diagnostics."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time

from common.protocol import CONTROL_PORT, MIXER_RTP_PORT
from common.roster import load_roster, demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from base.media.participants import QueuedRtpSource

STATIC = os.path.join(os.path.dirname(__file__), "ui", "static")
UI_PORT = 8080
ROUTES = {"/": "index.html", "/index.html": "index.html", "/rider": "rider.html",
          "/ops": "ops.html", "/dashboard": "ops.html"}
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
          ".css": "text/css", ".json": "application/json",
          ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
          ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain"}
log = logging.getLogger("convoy.base")


def lan_addresses() -> list[str]:
    """Every non-loopback IPv4 this machine has — the URLs phones can open."""
    addrs: list[str] = []
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=2).stdout.split()
        addrs += [a for a in out if "." in a and not a.startswith("127.")]
    except Exception:
        pass
    try:                                          # default-route address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        a = s.getsockname()[0]
        s.close()
        if a not in addrs and not a.startswith("127."):
            addrs.append(a)
    except Exception:
        pass
    return addrs


def print_urls(port: int = UI_PORT, qr: bool = True) -> None:
    addrs = lan_addresses()
    print("\n  Phones on the convoy Wi-Fi open ONE of these:")
    for a in addrs or ["<this machine's LAN IP>"]:
        print(f"    http://{a}:{port}/          (riders: /rider   operator: /ops)")
    print(f"    http://localhost:{port}/       (this machine)")
    if qr and addrs and shutil.which("qrencode"):
        try:
            print(subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "1",
                                  f"http://{addrs[0]}:{port}/rider"],
                                 capture_output=True, text=True, timeout=3).stdout)
        except Exception:
            pass
    elif qr and addrs:
        print("    (apt install qrencode to print a scannable QR here)")
    print()


def _http_response(status: str, body: bytes, ctype: str = "text/plain") -> bytes:
    return (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode() + body


def route_request(path: str, orc: Orchestrator | None = None) -> tuple[str, bytes, str]:
    """Pure routing (tested in S-17): -> (status, body, content-type)."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path == "/health":
        return "200 OK", b"ok\n", "text/plain"
    if path == "/snapshot.json":
        if orc is None:
            return "503 Service Unavailable", b"{}", "application/json"
        return "200 OK", json.dumps(orc.snapshot(), indent=1).encode(), "application/json"
    fn = ROUTES.get(path) or path.lstrip("/")
    full = os.path.normpath(os.path.join(STATIC, fn))
    try:
        inside = os.path.commonpath([STATIC, full]) == os.path.normpath(STATIC)
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(full):
        return "404 Not Found", b"not found\n", "text/plain"
    with open(full, "rb") as f:
        body = f.read()
    return "200 OK", body, CTYPES.get(os.path.splitext(full)[1], "application/octet-stream")


async def _serve_static(host: str = "0.0.0.0", port: int = UI_PORT, orc: Orchestrator | None = None):
    """Minimal HTTP static server (stdlib-only; the pages are single files)."""
    async def client(reader, writer):
        try:
            req = await asyncio.wait_for(reader.readline(), 5)
            parts = req.split()
            path = parts[1].decode(errors="replace") if len(parts) > 1 else "/"
            while (await asyncio.wait_for(reader.readline(), 5)).strip():
                pass
            status, body, ctype = route_request(path, orc)
            writer.write(_http_response(status, body, ctype))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    return await asyncio.start_server(client, host, port)


async def _monitor(mixer: PyMixer, port: int = 7100):
    """--monitor: the base machine's own speakers join room `main` as a
    listener, so announcements/music/riders are audible with no bridges.
    Uses paplay/aplay raw pipes; falls back to writing monitor.raw."""
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
        if shutil.which(cand[0]):
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


async def _status_lines(orc: Orchestrator, every_s: float = 5.0):
    """Hardware-testing verbosity: one line per interval, only what changed."""
    last = None
    while True:
        await asyncio.sleep(every_s)
        snap = orc.snapshot()
        nodes = snap["nodes"]
        parts = []
        for pid, r in snap["riders"].items():
            if r["role"] == "music":
                continue
            n = nodes.get(pid, {})
            mx = snap["mixer"].get(pid, {})
            v = lambda k, u: f"{n[k]}{u}" if n.get(k) is not None else "-"
            parts.append(f"{pid}:{'UP' if n.get('online') else 'down'}"
                         f"{'*' if pid in snap['talking'] else ''}"
                         f"/{v('rssi', 'dBm')}/{v('rtp_loss', '%')}/plc{mx.get('plc', 0)}")
        line = "  ".join(parts)
        if line != last:
            log.info("nodes: %s | announcing=%s tts=%s", line, snap["announcing"], snap["tts_engine"])
            last = line


async def _radio_gateway(roster, mixer, orc, rf_sim: bool, log):
    """Roster `radio:` section -> a RadioGateway participant (DR-011). In sim
    mode (`--rf`) the rig is a SimRig on an RfChannel with one virtual
    separated rider on an HT, so the fallback is exercised without hardware."""
    from common.radio import RadioLink, make_ptt
    from base.media.radio import RadioGateway
    rcfg = dict(roster.net.get("radio") or {})
    pid = rcfg.get("pid", "radio")
    if not rf_sim and not rcfg:
        return None, None
    callsign = str(rcfg.get("callsign", "") or ("K1SIM" if rf_sim else ""))
    if not callsign:
        log.warning("radio: no callsign in roster net.radio — gateway will never key")
    if pid not in roster.riders:
        from common.roster import Rider
        from common.protocol import ssrc_of
        r = Rider(id=pid, role="rider", rooms=["main"], down_port=6100 + 2 * len(roster.riders))
        r.ssrc = ssrc_of(pid)
        roster.riders[pid] = r
        orc.populate()
    music = {r.id for r in roster.riders.values() if r.role == "music"}
    mixer.set_exclude(pid, music)
    chan = None
    if rf_sim:
        from sim.rf import RfChannel, SimRig
        chan = RfChannel(noise_db=-85)
        rig = chan.add(SimRig("gateway"))
        rig_rx = rig_tx = rig; ptt = rig
        await chan.start()
    else:
        from bridge.io_adapters import CmdSource, CmdSink
        rig_rx = CmdSource(str(rcfg["rx_cmd"])); rig_tx = CmdSink(str(rcfg["tx_cmd"]))
        ptt = make_ptt(str(rcfg.get("ptt", "none")))
    link = RadioLink(ptt, callsign, service=str(rcfg.get("service", "ham")),
                     hang_ms=int(rcfg.get("hang_ms", 600)), tot_s=float(rcfg.get("tot_s", 180)))
    gw = RadioGateway(pid, link, rig_rx, rig_tx, mixer_addr=("127.0.0.1", mixer.rtp_port),
                      down_port=roster.riders[pid].down_port,
                      prefer_silero=bool(rcfg.get("silero", True)),
                      on_vad=lambda o: orc.on_vad(pid, o))
    await gw.start()
    orc.radio = gw
    log.info("radio gateway `%s` up: callsign %s, ptt %s, music excluded %s%s", pid, callsign,
             rcfg.get("ptt", "none"), sorted(music), " (SIM RF channel)" if rf_sim else "")
    return gw, chan


async def _rf_virtual_rider(chan, log, callsign="K1SIM", silero: bool = True):
    """A virtual separated rider on an HT: gated real speech goes on the air
    through a RadioLink; whatever the gateway transmits is discarded (no ear)."""
    from sim.rf import SimRig
    from sim.live import LoopingMouth, _mouth_material
    from sim import fixtures
    from common.radio import RadioLink
    from bridge.radio import RadioFailover
    from bridge.engine import BridgeEngine
    from bridge.io_adapters import ArraySink
    import random
    clips_raw, _ = fixtures._speech_clips()
    winds, clips = _mouth_material(clips_raw)
    mouth = LoopingMouth(winds[50], clips, random.Random(7), chatter_s=(15.0, 35.0))
    rig = chan.add(SimRig("ht_rider"))
    sink = ArraySink(); sink.write = lambda f: None
    # Silero, deliberately: the energy fallback's slow floor tracker keys on
    # wind for its first ~80 s, which on a shared channel is a stuck carrier
    eng = BridgeEngine("rf_rider", mouth, sink, mixer_addr=("127.0.0.1", 1), down_port=0,
                       prefer_silero=silero)
    eng.radio = RadioFailover(RadioLink(rig, callsign + "/M"), rig, rig, mode="always")
    eng.link_up = False
    await eng.start()
    log.info("virtual RF rider up (HT only, no Wi-Fi): talks every 15-35 s on the sim channel")
    return eng


async def main(roster_path: str | None, monitor: bool = False, mode: str = "hw",
               n_riders: int = 6, chatter: bool = True, http_port: int = UI_PORT,
               open_browser: bool = False, silero: bool = True, rf_sim: bool = False):
    roster = load_roster(roster_path) if roster_path else demo_roster(n_riders, include_music=True)
    # RTP listens on every interface unless the roster pins net.mixer_bind
    mixer = PyMixer(bind_host=str(roster.net.get("mixer_bind", "0.0.0.0")))
    orc = Orchestrator(roster, mixer)
    orc.mode = mode

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
    http = await _serve_static(port=http_port, orc=orc)
    from base.media import tts

    gw, chan = await _radio_gateway(roster, mixer, orc, rf_sim and mode == "sim", log)
    rf_rider = await _rf_virtual_rider(chan, log, silero=True) if chan is not None else None

    riders = None
    if mode == "sim":
        from sim.live import VirtualRiders
        riders = VirtualRiders(roster, mixer_port=mixer.rtp_port, chatter=chatter,
                               prefer_silero=silero, log=lambda m: log.info("%s", m),
                               skip={gw.pid} if gw else None)
        await riders.start()
        if not chatter:
            log.info("chatter off: riders speak only via the phone page's TALK / say")

    print(f"\nbase up [{mode}]: mixer rtp:{mixer.rtp_port} on {mixer.bind_host}  "
          f"control ws:{CONTROL_PORT}  http:{http_port}  tts:{tts.engine_name()}"
          f"{'  monitor:ON (room main -> speakers)' if monitor else ''}\n"
          f"roster: {', '.join(roster.riders)}")
    print_urls(http_port, qr=(mode != "field"))
    if mode == "sim":
        print("  sim: virtual riders are talking on their own; open /rider on a phone,\n"
              "       pick a name and hold TALK — the real gate/mixer/ladder react.\n")
    if open_browser and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", f"http://localhost:{http_port}/"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = asyncio.create_task(_status_lines(orc)) if mode == "hw" else None
    try:
        await asyncio.Event().wait()
    finally:
        if status:
            status.cancel()
        if riders:
            riders.stop()
        if rf_rider:
            rf_rider.stop()
        if gw:
            gw.stop()
        if chan:
            chan.stop()
        mixer.stop(); announce.stop(); ws.close(); http.close()
        if mon:
            mon.close()


def cli(argv=None):
    ap = argparse.ArgumentParser(description="ConvoyChat base station")
    ap.add_argument("--mode", choices=["sim", "hw", "field"], default="hw",
                    help="sim: virtual riders on this machine; hw: real bridges, verbose; "
                         "field: real bridges, quiet (systemd)")
    ap.add_argument("--roster", default=None, help="roster.yaml (default: demo roster)")
    ap.add_argument("--riders", type=int, default=6, help="demo roster size (no --roster)")
    ap.add_argument("--no-chatter", action="store_true", help="sim: riders only talk on command")
    ap.add_argument("--rf", action="store_true",
                    help="sim: add the radio gateway on a simulated RF channel with one HT-only rider")
    ap.add_argument("--energy-vad", action="store_true",
                    help="sim: skip Silero (N riders x Silero in one process overruns SAFE-1's "
                         "50 ms budget on slow laptops and demotes them anyway)")
    ap.add_argument("--monitor", action="store_true",
                    help="play room `main` through this machine's speakers")
    ap.add_argument("--http-port", type=int, default=UI_PORT)
    ap.add_argument("--open", action="store_true", help="xdg-open the landing page")
    args = ap.parse_args(argv)
    level = {"sim": logging.INFO, "hw": logging.INFO, "field": logging.WARNING}[args.mode]
    logging.basicConfig(level=level, stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(main(args.roster, args.monitor, args.mode, args.riders,
                         not args.no_chatter, args.http_port, args.open,
                         silero=not args.energy_vad, rf_sim=args.rf))
    except KeyboardInterrupt:
        print("\nbase stopped")


if __name__ == "__main__":
    cli()
