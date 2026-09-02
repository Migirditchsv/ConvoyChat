"""Bridge entry point — what runs on the Pi (and, with --sim, on a laptop):

    python3 -m bridge.main --config /boot/convoy.toml     # hardware
    python3 -m bridge.main --sim --id r2_rider --base 192.168.1.2   # fake mic, real everything else

Wires: CmdSource/CmdSink (or a looping fixture mouth) -> BridgeEngine ->
BridgeAgent(DeviceActions) -> base; IwLinkStats -> heartbeat + EvictionPolicy
(INV-9); an audio-command supervisor that restarts a dead pw-record/pw-play
with earcons (SAFE-2). Verbosity: --verbose prints a status line per second
(hardware testing); field mode logs only state changes (systemd journal)."""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import random
import sys
import time

from common.protocol import CONTROL_PORT, MIXER_RTP_PORT
from bridge import config as cfgmod
from bridge.agent import BridgeAgent, DeviceActions, SimActions
from bridge.engine import BridgeEngine
from bridge.io_adapters import CmdSource, CmdSink, ArraySink
from bridge.net.linkstats import IwLinkStats
from bridge.net.supervisor import EvictionPolicy

log = logging.getLogger("convoy.bridge")


def _down_port_for(node_id: str, roster_path: str | None, fallback: int) -> int:
    """The roster is the base's, but a bridge may carry a copy; else config."""
    if roster_path and os.path.exists(roster_path):
        from common.roster import load_roster
        r = load_roster(roster_path).riders.get(node_id)
        if r:
            return r.down_port
    return fallback


async def run(cfg: cfgmod.BridgeConfig, sim: bool, verbose: bool, roster_path: str | None):
    if sim:
        from sim.live import LoopingMouth, _mouth_material
        from sim import fixtures
        fixtures.build()
        clips_raw, _ = fixtures._speech_clips()
        winds, clips = _mouth_material(clips_raw)
        speed = int(cfg.speed_kmh) if int(cfg.speed_kmh) in winds else 90
        source = LoopingMouth(winds[speed], clips, random.Random(1))
        sink = ArraySink()
        sink.write = lambda f: None            # no ear on a laptop bridge
    else:
        source = CmdSource(cfg.source_cmd); source.start()
        sink = CmdSink(cfg.sink_cmd); sink.start()

    down_port = cfg.down_port or _down_port_for(cfg.node_id, roster_path, 0)
    if not down_port:
        log.warning("down_port unknown: set [node] down_port in convoy.toml or pass "
                    "--roster; using an ephemeral port (the mixer will NOT find it)")
    eng = BridgeEngine(cfg.node_id, source, sink, mixer_addr=cfg.mixer_addr,
                       down_port=down_port, prefer_silero=cfg.prefer_silero,
                       bind_host=cfg.bind_host)
    eng.up.set_speed(cfg.speed_kmh)
    link = IwLinkStats(cfg.wifi_iface, engine=eng)
    if sim:
        actions = SimActions(engine=eng, speak=source.say)
    else:
        actions = DeviceActions(engine=eng, enabled=cfg.actions_enabled,
                                headset_mac=cfg.headset_mac, wifi_iface=cfg.wifi_iface)
    agent = BridgeAgent(cfg.node_id, eng, actions, cfg.base_ws, link_stats=link,
                        log=lambda m: log.info("%s", m), token=cfg.node_token)
    await eng.start()
    await agent.start()

    evict = EvictionPolicy(
        evict_action=lambda: asyncio.get_running_loop().create_task(actions.reconnect_wifi()),
        earcon_action=actions._earcon)

    log.info("bridge %s up: mixer %s:%d  control %s  down_port %d  vad %s  actions %s",
             cfg.node_id, cfg.base_host, cfg.mixer_port, cfg.base_ws, down_port,
             eng.up.vad.mode, "ENABLED" if getattr(actions, "enabled", False) else
             ("sim" if sim else "dry-run"))
    last_alive = (True, True)
    last_conn = None
    tick = 0
    last_restart = 0.0
    RESTART_EVERY_S = 5.0        # a command that dies instantly must not earcon-spam
    while True:
        await asyncio.sleep(1.0)
        tick += 1
        st = link()
        if not sim:
            evict.tick_1s(link.as_link_stats())
            if tick % 10 == 0 and cfg.headset_mac:
                try:
                    await actions.bt_status()          # refresh the heartbeat's headset card
                except Exception as e:
                    log.debug("bt_status: %s", e)
            alive = (source.alive, sink.alive)
            if alive != last_alive:
                log.warning("audio pipe state: mic %s, ear %s",
                            "up" if alive[0] else "DOWN", "up" if alive[1] else "DOWN")
                last_alive = alive
            if (not source.alive or not sink.alive) and time.monotonic() - last_restart >= RESTART_EVERY_S:
                last_restart = time.monotonic()
                if not source.alive:
                    actions._earcon("link_lost"); source.start(); actions._earcon("connected")
                if not sink.alive:
                    sink.start()
        if agent.connected != last_conn:
            log.info("control link %s", "up" if agent.connected else "DOWN")
            last_conn = agent.connected
        if verbose:
            s = eng.stats
            print(f"[{time.strftime('%H:%M:%S')}] {cfg.node_id} ctl={'up' if agent.connected else 'down'} "
                  f"vad={s.get('vad_mode')} open={int(bool(s.get('vad_open')))} ptt={int(bool(s.get('ptt')))} "
                  f"tx={s['tx_pkts']} rx={s['rx_pkts']} loss={st.get('rtp_loss')}% "
                  f"rssi={st.get('rssi')} rate={st.get('tx_rate')} vol={eng.down.volume_pct}% "
                  f"evictions={evict.evictions}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ConvoyChat bridge (edge node)")
    ap.add_argument("--config", default=None, help=f"TOML identity (default {cfgmod.DEFAULT_PATH})")
    ap.add_argument("--sim", action="store_true", help="fake microphone (looping wind + speech); no BT")
    ap.add_argument("--id", default=None, help="node id override (or CONVOY_NODE_ID)")
    ap.add_argument("--base", default=None, help="base host override (or CONVOY_BASE)")
    ap.add_argument("--down-port", type=int, default=None)
    ap.add_argument("--roster", default=None, help="roster.yaml copy to derive down_port")
    ap.add_argument("--verbose", "-v", action="store_true", help="1 Hz status line (hardware testing)")
    ap.add_argument("--quiet", "-q", action="store_true", help="field: warnings only")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    if args.id:
        os.environ["CONVOY_NODE_ID"] = args.id
    if args.base:
        os.environ["CONVOY_BASE"] = args.base
    if args.config or (not args.sim and not args.id):
        cfg = cfgmod.load(args.config)
    else:
        cfg = cfgmod.from_dict({"node": {"id": args.id or os.environ.get("CONVOY_NODE_ID", ""),
                                         "base": args.base or os.environ.get("CONVOY_BASE", "127.0.0.1")}})
    if args.down_port:
        cfg.down_port = args.down_port
    try:
        asyncio.run(run(cfg, args.sim, args.verbose, args.roster))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
