"""Roster: the single source of truth for identity, roles, rooms, ports."""
from __future__ import annotations
from dataclasses import dataclass, field
import yaml

from common.protocol import ssrc_of

ROLES = {"lead", "chase", "rider", "music"}
LADDER_PRIORITY = {"chase": 0, "lead": 1, "rider": 2, "music": 3}  # 0 = highest


@dataclass
class Rider:
    id: str
    role: str = "rider"
    rooms: list[str] = field(default_factory=lambda: ["main"])
    bridge_mac: str = ""
    headset: dict = field(default_factory=dict)
    ip: str = "127.0.0.1"
    down_port: int = 0
    ssrc: int = 0


@dataclass
class Roster:
    net: dict
    rooms: list[str]
    riders: dict[str, Rider]

    @property
    def mixer_ip(self) -> str:
        return self.net.get("mixer_ip", "127.0.0.1")

    def by_role(self, role: str) -> list[Rider]:
        return [r for r in self.riders.values() if r.role == role]


def load_roster(path_or_dict) -> Roster:
    if isinstance(path_or_dict, dict):
        doc = path_or_dict
    else:
        with open(path_or_dict) as f:
            doc = yaml.safe_load(f)
    assert doc.get("version") == 1, "roster version must be 1"
    riders: dict[str, Rider] = {}
    ssrcs: dict[int, str] = {}
    for i, rd in enumerate(doc.get("riders", [])):
        r = Rider(id=rd["id"], role=rd.get("role", "rider"),
                  rooms=list(rd.get("rooms", ["main"])),
                  bridge_mac=rd.get("bridge_mac", ""),
                  headset=rd.get("headset", {}),
                  ip=rd.get("ip", "127.0.0.1"))
        assert r.role in ROLES, f"bad role {r.role}"
        r.down_port = rd.get("down_port", 6100 + 2 * i)
        r.ssrc = ssrc_of(r.id)
        if r.ssrc in ssrcs:                      # DR-005 collision check
            raise ValueError(f"SSRC collision: {r.id} vs {ssrcs[r.ssrc]}")
        ssrcs[r.ssrc] = r.id
        riders[r.id] = r
    return Roster(net=doc.get("net", {}), rooms=list(doc.get("rooms", ["main"])),
                  riders=riders)


def demo_roster(n_riders: int = 4, base_port: int = 6100,
                include_music: bool = False) -> Roster:
    """Synthetic roster for sim/tests: 1 lead + 1 chase + (n-2) riders."""
    riders = []
    for i in range(n_riders):
        role = "lead" if i == 0 else ("chase" if i == 1 else "rider")
        riders.append({"id": f"r{i}_{role}", "role": role,
                       "rooms": ["main"], "down_port": base_port + 2 * i})
    if include_music:
        riders.append({"id": "music", "role": "music", "rooms": ["main"],
                       "down_port": base_port + 2 * n_riders})
    return load_roster({"version": 1, "net": {"mixer_ip": "127.0.0.1"},
                        "rooms": ["main", "nav"], "riders": riders})
