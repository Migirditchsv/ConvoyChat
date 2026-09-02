"""Priority ladder (plan S-2): Green-GO semantics as one gain table.
chase=Emergency (mutes all) > lead=Announcement (ducks) > rider > music."""
from __future__ import annotations

DEFAULT_GAINS = {"lead": 100, "chase": 100, "rider": 100, "music": 60}
DUCK = {
    "chase": {"lead": 0, "chase": 100, "rider": 0, "music": 0},
    "lead":  {"lead": 100, "chase": 100, "rider": 25, "music": 8},
    # riders never duck people, but music ducks under ANY speech (the
    # original requirement: "music fades when they or others speak")
    "rider": {"lead": 100, "chase": 100, "rider": 100, "music": 25},
    "music": None,
}


def gains_for(speaker_role: str, participants: dict[str, str]) -> dict[str, int] | None:
    """participants: pid -> role. Returns pid -> gain, or None if no duck applies."""
    table = DUCK.get(speaker_role)
    if table is None:
        return None
    return {pid: table[role] for pid, role in participants.items()}


def default_gains(participants: dict[str, str]) -> dict[str, int]:
    return {pid: DEFAULT_GAINS[role] for pid, role in participants.items()}
