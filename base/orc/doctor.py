"""Issues for the person in the car (DR-014): turn a snapshot into plain
sentences with one-tap fixes, so a router-node manager who is not a
computer person can keep riders talking without reading meters.

`diagnose(snapshot)` is pure and unit-tested (S-22). Each issue:
    {"pid", "level": "bad"|"warn"|"info", "title", "why",
     "fixes": [{"label", "msg": {"t": ..., "data": {...}}} | {"label", "hint": "..."}]}
A fix with `msg` is a control-plane message the page sends verbatim; a
`hint` is a physical action only a human can take."""
from __future__ import annotations

OFFLINE_S = 5.0
STUCK_TALK_S = 45.0
LOW_TRIM = 30
LOW_VOL = 30
WEAK_RSSI = -80
HIGH_LOSS = 20.0


def _fix_cmd(target: str, cmd: str, label: str, args: dict | None = None) -> dict:
    return {"label": label, "msg": {"t": "node_cmd", "data": {"target": target, "cmd": cmd,
                                                                "args": args or {}}}}


def _fix_audio(pid: str, label: str, **ctl) -> dict:
    return {"label": label, "msg": {"t": "audio_ctl", "data": {"pid": pid, **ctl}}}


def _hint(label: str, text: str) -> dict:
    return {"label": label, "hint": text}


def diagnose(snap: dict, now: float | None = None) -> list[dict]:
    issues: list[dict] = []
    riders = snap.get("riders", {})
    nodes = snap.get("nodes", {})
    talking = set(snap.get("talking", []))
    talk_since = snap.get("talk_since", {})
    now = snap.get("at", 0.0) if now is None else now

    for pid, r in riders.items():
        if r.get("role") == "music":
            continue
        n = nodes.get(pid)
        if n is None:
            issues.append({"pid": pid, "level": "bad", "title": f"{pid}: bridge never connected",
                           "why": "No heartbeat since the base started. Power, Wi-Fi, or the wrong id in convoy.toml.",
                           "fixes": [_hint("Check the bike", "Is the bridge powered (green LED)? Is it on the convoy Wi-Fi? "
                                           "Does [node] id in /boot/convoy.toml match the roster?")]})
            continue
        if not n.get("online", False):
            age = int(n.get("age_s") or 0)
            issues.append({"pid": pid, "level": "bad", "title": f"{pid}: bridge offline for {age}s",
                           "why": "Heartbeats stopped. Usually Wi-Fi range or power.",
                           "fixes": [_hint("Wait / close up", "If the bike is far back, it may reconnect on its own within 10 s of being in range."),
                                     _hint("Power-cycle", "Ask the rider (at a stop) to switch the bridge off and on; it is back in ~45 s.")]})
            continue                                   # nothing else is knowable while offline
        hs = n.get("headset")
        if hs is not None and hs.get("connected") is False:
            issues.append({"pid": pid, "level": "bad", "title": f"{pid}: headset not connected",
                           "why": f"The bridge is up but the helmet headset ({hs.get('name') or 'unknown'}) is not linked. The rider hears nothing.",
                           "fixes": [_fix_cmd(pid, "reconnect_bt", "Reconnect headset"),
                                     _hint("Then", "If it stays off: rider powers the headset off and on near the bike.")]})
        elif hs is None:
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: no headset paired",
                           "why": "Nothing to talk through until a headset is paired.",
                           "fixes": [_fix_cmd(pid, "bt_scan", "Scan for headsets"),
                                     _hint("Or", "The rider can pair from their phone page (scan & pair).")]})
        if r.get("muted"):
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: muted by the operator",
                           "why": "Nobody hears this rider until unmuted.",
                           "fixes": [_fix_audio(pid, "Unmute", mute=False)]})
        trim = int(r.get("trim", 100))
        if trim <= LOW_TRIM:
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: trimmed down to {trim}%",
                           "why": "Others will barely hear this rider.",
                           "fixes": [_fix_audio(pid, "Reset trim to 100%", trim=100)]})
        vol = n.get("volume")
        if vol is not None and int(vol) <= LOW_VOL:
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: helmet volume {vol}%",
                           "why": "The rider may not hear the convoy.",
                           "fixes": [_fix_cmd(pid, "set_volume", "Set volume 100%", {"pct": 100})]})
        vm = n.get("vad_mode")
        if vm == "open":
            issues.append({"pid": pid, "level": "bad", "title": f"{pid}: voice detector failed open",
                           "why": "The classifier crashed; the bridge now transmits everything (wind included) so no call is lost. A reboot restores it.",
                           "fixes": [_fix_cmd(pid, "reboot", "Reboot bridge (45 s of silence)")]})
        elif vm in ("energy", "spectral"):
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: voice detector degraded ({vm})",
                           "why": "Silero was too slow or crashed on this bridge; the fallback lets more wind through.",
                           "fixes": [_hint("Fine for now", "If wind chatter from this rider annoys everyone, reboot the bridge at the next stop."),
                                     _fix_cmd(pid, "reboot", "Reboot bridge")]})
        rssi, loss = n.get("rssi"), n.get("rtp_loss")
        if (rssi is not None and rssi <= WEAK_RSSI) or (loss is not None and loss >= HIGH_LOSS):
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: weak Wi-Fi ({rssi if rssi is not None else '?'} dBm, {loss if loss is not None else '?'}% loss)",
                           "why": "Choppy audio both ways. The bike is at the edge of the car's range.",
                           "fixes": [_hint("Close up", "Have the group tighten up, or slow the car."),
                                     _fix_cmd(pid, "reconnect_wifi", "Reconnect Wi-Fi")]})
        if n.get("ptt") and pid in talking:
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: push-to-talk held",
                           "why": "Their gate is forced open; it releases itself 6 s after the phone stops re-arming it.",
                           "fixes": [_fix_cmd(pid, "ptt", "Release PTT", {"on": False})]})
        since = talk_since.get(pid)
        if pid in talking and since is not None and now - since >= STUCK_TALK_S:
            issues.append({"pid": pid, "level": "warn", "title": f"{pid}: transmitting for {int(now - since)}s",
                           "why": "Continuous transmission: wind beating the gate, or a stuck open mic. Everyone hears it.",
                           "fixes": [_fix_audio(pid, "Mute for now", mute=True),
                                     _fix_cmd(pid, "reboot", "Reboot bridge")]})
        if n.get("link_up") is False:
            issues.append({"pid": pid, "level": "info", "title": f"{pid}: reached us over a fallback path",
                           "why": "The bridge says the convoy Wi-Fi is gone; it is talking through a hotspot tunnel or radio.",
                           "fixes": []})
        rf = n.get("radio")
        if rf and rf.get("active"):
            issues.append({"pid": pid, "level": "info", "title": f"{pid}: on the radio ({rf.get('callsign') or 'no callsign'})",
                           "why": "Helmet audio is going over the HT while the base is unreachable.", "fixes": []})
        if r.get("room") not in (None, "main") and len(riders) > 2:
            issues.append({"pid": pid, "level": "info", "title": f"{pid}: in room `{r.get('room')}`",
                           "why": "They only hear the lead and their room-mates.",
                           "fixes": [{"label": "Move to main", "msg": {"t": "move", "data": {"pid": pid, "room": "main", "by": "chase"}}}]})

    radio = snap.get("radio")
    if radio is not None and not radio.get("callsign"):
        issues.append({"pid": "radio", "level": "bad", "title": "radio gateway has no callsign",
                       "why": "The transmitter will never key without one (roster net.radio.callsign).",
                       "fixes": [_hint("Fix", "Add callsign to roster.yaml under net.radio and restart the base.")]})
    if snap.get("tts_engine") == "none":
        issues.append({"pid": "base", "level": "warn", "title": "announcements unavailable",
                       "why": "No text-to-speech engine on the base machine.",
                       "fixes": [_hint("Install", "sudo apt install espeak-ng, then restart the base.")]})
    order = {"bad": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["level"]], i["pid"]))
    return issues


def summary(issues: list[dict]) -> str:
    bad = sum(i["level"] == "bad" for i in issues)
    warn = sum(i["level"] == "warn" for i in issues)
    if not bad and not warn:
        return "all good"
    parts = []
    if bad:
        parts.append(f"{bad} problem{'s' if bad != 1 else ''}")
    if warn:
        parts.append(f"{warn} warning{'s' if warn != 1 else ''}")
    return ", ".join(parts)
