"""Bridge identity + wiring from /boot/convoy.toml (INV-11: identity lives on
the boot partition, the rootfs is read-only and identical on every bike).

Every key has a default so a bare `[node] id = "r3_rider"` boots; env
overrides (CONVOY_NODE_ID, CONVOY_BASE) exist for bench work. tomllib is
stdlib in 3.11 — no dependency."""
from __future__ import annotations
from dataclasses import dataclass, field
import os
import tomllib

DEFAULT_PATH = "/boot/convoy.toml"

# Audio commands: raw s16le / 16 kHz / mono on a pipe. Two documented stacks:
#  PipeWire (RPi OS Bookworm default) with bluez5.roles=[hfp_ag] — target the
#  headset's HFP source/sink node (see docs/runbook.md, hardware mode).
#  bluez-alsa: arecord/aplay -D bluealsa:DEV=<mac>,PROFILE=sco
PW_SOURCE = "pw-record --rate 16000 --channels 1 --format s16 --latency 60ms -"
PW_SINK = "pw-play --rate 16000 --channels 1 --format s16 --latency 60ms -"


@dataclass
class BridgeConfig:
    node_id: str = ""
    base_host: str = "127.0.0.1"
    mixer_port: int = 5100
    control_port: int = 8800
    down_port: int = 0            # 0 = ask the roster (base tells us) — set to the roster value
    bind_host: str = "0.0.0.0"
    source_cmd: str = PW_SOURCE
    sink_cmd: str = PW_SINK
    headset_mac: str = ""
    wifi_iface: str = "wlan0"
    actions_enabled: bool = False
    node_token: str = ""
    prefer_silero: bool = True
    speed_kmh: float = 0.0        # static wind estimate until GPS lands
    # [radio] wired HT fallback (DR-011)
    radio_mode: str = "off"       # off | auto (RF only when the base is unreachable) | always
    radio_callsign: str = ""      # REQUIRED to transmit; empty = interlocked
    radio_service: str = "ham"    # ham | gmrs (ID interval)
    radio_ptt: str = "none"       # gpio:17 | gpio:17:low | serial:/dev/ttyUSB0:rts | none
    radio_rx_cmd: str = ""        # raw s16le/16k/mono from the rig's speaker (arecord/pw-record)
    radio_tx_cmd: str = ""        # raw s16le/16k/mono into the rig's mic (aplay/pw-play)
    radio_hang_ms: int = 600
    radio_tot_s: float = 180.0
    # [failover] hotspot + WireGuard (DR-012)
    failover_enabled: bool = False
    failover_star_ssid: str = "convoy"
    failover_hotspots: list = field(default_factory=list)
    failover_tunnel_base: str = "10.66.0.1"
    failover_wg_iface: str = "convoy"
    failover_fail_s: int = 8
    failover_restore_s: int = 12
    raw: dict = field(default_factory=dict)

    @property
    def base_ws(self) -> str:
        return f"ws://{self.base_host}:{self.control_port}/"

    @property
    def mixer_addr(self) -> tuple[str, int]:
        return (self.base_host, self.mixer_port)


def from_dict(doc: dict) -> BridgeConfig:
    node = doc.get("node", {}) or {}
    audio = doc.get("audio", {}) or {}
    bt = doc.get("bt", {}) or {}
    wifi = doc.get("wifi", {}) or {}
    actions = doc.get("actions", {}) or {}
    net = doc.get("net", {}) or {}
    radio = doc.get("radio", {}) or {}
    fo = doc.get("failover", {}) or {}
    cfg = BridgeConfig(
        node_id=str(node.get("id", "")),
        base_host=str(node.get("base", "127.0.0.1")),
        mixer_port=int(node.get("mixer_port", 5100)),
        control_port=int(node.get("control_port", 8800)),
        down_port=int(node.get("down_port", 0)),
        bind_host=str(audio.get("bind_host", "0.0.0.0")),
        source_cmd=str(audio.get("source_cmd", PW_SOURCE)),
        sink_cmd=str(audio.get("sink_cmd", PW_SINK)),
        headset_mac=str(bt.get("headset_mac", "")),
        wifi_iface=str(wifi.get("iface", "wlan0")),
        actions_enabled=bool(actions.get("enabled", False)),
        node_token=str(net.get("node_token", "")),
        prefer_silero=bool(audio.get("prefer_silero", True)),
        speed_kmh=float(node.get("speed_kmh", 0.0)),
        radio_mode=str(radio.get("mode", "off")),
        radio_callsign=str(radio.get("callsign", "")),
        radio_service=str(radio.get("service", "ham")),
        radio_ptt=str(radio.get("ptt", "none")),
        radio_rx_cmd=str(radio.get("rx_cmd", "")),
        radio_tx_cmd=str(radio.get("tx_cmd", "")),
        radio_hang_ms=int(radio.get("hang_ms", 600)),
        radio_tot_s=float(radio.get("tot_s", 180.0)),
        failover_enabled=bool(fo.get("enabled", False)),
        failover_star_ssid=str(fo.get("star_ssid", "convoy")),
        failover_hotspots=[str(x) for x in (fo.get("hotspots", []) or [])],
        failover_tunnel_base=str(fo.get("tunnel_base", "10.66.0.1")),
        failover_wg_iface=str(fo.get("wg_iface", "convoy")),
        failover_fail_s=int(fo.get("fail_s", 8)),
        failover_restore_s=int(fo.get("restore_s", 12)),
        raw=doc)
    if cfg.radio_mode not in ("off", "auto", "always"):
        raise ValueError("convoy.toml: [radio] mode must be off | auto | always")
    if cfg.radio_mode != "off" and not cfg.radio_callsign:
        raise ValueError("convoy.toml: [radio] callsign is required to enable the radio "
                         "(the link refuses to key without one)")
    cfg.node_id = os.environ.get("CONVOY_NODE_ID", cfg.node_id)
    cfg.base_host = os.environ.get("CONVOY_BASE", cfg.base_host)
    if not cfg.node_id:
        raise ValueError("convoy.toml: [node] id is required (e.g. id = \"r2_rider\")")
    return cfg


def load(path: str | None = None) -> BridgeConfig:
    path = path or os.environ.get("CONVOY_CONFIG", DEFAULT_PATH)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found — copy deploy/convoy.example.toml there and set [node] id")
    with open(path, "rb") as f:
        return from_dict(tomllib.load(f))
