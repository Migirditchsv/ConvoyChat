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
        raw=doc)
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
