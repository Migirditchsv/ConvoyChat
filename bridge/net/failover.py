"""Link failover (DR-012): star -> rider hotspot -> WireGuard tunnel -> star.

When the base stops answering on the convoy Wi-Fi, a bridge joins any
listed rider hotspot it can see (riders without a hotspot plan ride on
someone else's), brings up a WireGuard tunnel to a base the internet can
reach (the chase tablet on its own cellular, or a cloud base), and re-points
the engine and agent at the base's tunnel address. When the convoy SSID is
back and strong for long enough, it tears the tunnel down and returns.

Pure state machine on 1 s ticks with injected actions (S-20). `WifiActions`
is the NetworkManager + wg-quick shell, dry-run until enabled — the same
guard as DeviceActions."""
from __future__ import annotations
from dataclasses import dataclass, field
import math
import re
import shlex
import subprocess

STAR, HOTSPOT, TUNNEL = "star", "hotspot", "tunnel"


@dataclass
class Obs:
    """What the bridge can see this second."""
    base_ok: bool                       # control WS connected AND downlink RTP arriving
    scan: dict[str, int] = field(default_factory=dict)   # ssid -> rssi dBm
    internet_ok: bool = False           # hub/base tunnel endpoint pingable
    tunnel_ok: bool = False             # wg handshake fresh
    current_ssid: str | None = None


@dataclass
class FailoverConfig:
    star_ssid: str = "convoy"
    hotspots: list[str] = field(default_factory=list)   # SSIDs, in preference order
    star_base: str = "192.168.1.2"      # base host on the convoy LAN
    tunnel_base: str = "10.66.0.1"      # base host inside the tunnel
    fail_s: int = 8                     # base silent this long -> leave the star
    restore_s: int = 12                 # star strong this long -> return
    hotspot_timeout_s: int = 25         # no tunnel this long -> next hotspot / back
    min_rssi: int = -78                 # weaker than this does not count as "visible"
    retry_backoff_s: int = 30           # after all hotspots fail, wait before retrying


class LinkFailover:
    def __init__(self, actions, cfg: FailoverConfig | None = None, earcon=lambda name: None):
        self.a = actions
        self.cfg = cfg or FailoverConfig()
        self.earcon = earcon
        self.state = STAR
        self._bad = 0
        self._good = 0
        self._since = 0
        self._hs_idx = 0
        self._backoff = 0
        self._attempts = 0                 # hotspots tried in this episode
        self._tunnel_requested = False
        self.transitions: list[tuple[int, str, str]] = []
        self.tick = 0
        self.current_hotspot: str | None = None

    # -- helpers --
    def _visible(self, ssid: str, scan: dict[str, int]) -> bool:
        r = scan.get(ssid)
        return r is not None and r >= self.cfg.min_rssi

    def _next_hotspot(self, scan: dict[str, int]) -> str | None:
        n = len(self.cfg.hotspots)
        for k in range(n):
            ssid = self.cfg.hotspots[(self._hs_idx + k) % n]
            if self._visible(ssid, scan):
                self._hs_idx = (self._hs_idx + k + 1) % n
                return ssid
        return None

    def _go(self, state: str) -> None:
        self.transitions.append((self.tick, self.state, state))
        self.state = state
        self._since = 0
        self._bad = self._good = 0

    def _return_to_star(self) -> None:
        if self._tunnel_requested:
            self.a.tunnel_down()
            self._tunnel_requested = False
        self.a.join(self.cfg.star_ssid)
        self.a.set_base(self.cfg.star_base)
        self.current_hotspot = None
        self._go(STAR)

    # -- the tick --
    def tick_1s(self, obs: Obs) -> str:
        self.tick += 1
        self._since += 1
        c = self.cfg
        if self.state == STAR:
            if obs.base_ok:
                self._bad = 0
                self._backoff = 0
            else:
                self._bad += 1
            if self._backoff > 0:
                self._backoff -= 1
            elif self._bad >= c.fail_s:
                hs = self._next_hotspot(obs.scan)
                if hs is not None:
                    self.earcon("link_lost")
                    self.a.join(hs)
                    self.current_hotspot = hs
                    self._attempts = 1
                    self._go(HOTSPOT)
        elif self.state == HOTSPOT:
            if obs.internet_ok and not self._tunnel_requested:
                self.a.tunnel_up()
                self._tunnel_requested = True
            if obs.tunnel_ok:
                self.a.set_base(c.tunnel_base)
                self.earcon("link_restored")
                self._go(TUNNEL)
            elif self._since >= c.hotspot_timeout_s:
                if self._tunnel_requested:
                    self.a.tunnel_down()
                    self._tunnel_requested = False
                hs = self._next_hotspot(obs.scan) if self._attempts < len(c.hotspots) else None
                if hs is not None and hs != self.current_hotspot:
                    self.a.join(hs)
                    self.current_hotspot = hs
                    self._attempts += 1
                    self._go(HOTSPOT)
                else:                      # every visible hotspot tried: back off on the star
                    self._backoff = c.retry_backoff_s
                    self._return_to_star()
        elif self.state == TUNNEL:
            if self._visible(c.star_ssid, obs.scan):
                self._good += 1
            else:
                self._good = 0
            if not obs.tunnel_ok:
                self._bad += 1
            else:
                self._bad = 0
            if self._good >= c.restore_s:
                self.earcon("connected")
                self._return_to_star()
            elif self._bad >= c.fail_s:
                self.a.tunnel_down()
                self._tunnel_requested = False
                self._go(HOTSPOT)
        return self.state

    def stats(self) -> dict:
        return {"state": self.state, "hotspot": self.current_hotspot,
                "transitions": len(self.transitions), "since_s": self._since}


# -- scan parsing --------------------------------------------------------------

def parse_nmcli_scan(text: str) -> dict[str, int]:
    """`nmcli -t -f SSID,SIGNAL dev wifi list` -> {ssid: rssi_dBm}. nmcli's
    SIGNAL is 0..100; NetworkManager maps it from dBm as (dbm + 100) * 2
    clipped, so we invert that. Escaped colons in SSIDs are unescaped."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(.*?[^\\]):(\d+)\s*$", line.strip())
        if not m:
            continue
        ssid = m.group(1).replace("\\:", ":")
        if not ssid:
            continue
        sig = int(m.group(2))
        dbm = int(math.floor(sig / 2 - 100))
        out[ssid] = max(dbm, out.get(ssid, -200))         # strongest BSS of that SSID
    return out


def parse_wg_handshake(text: str, now: float, max_age_s: float = 180.0) -> bool:
    """`wg show <iface> latest-handshakes` -> True if any peer shook hands
    within max_age_s (a WireGuard tunnel with a stale handshake is down)."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            ts = int(parts[1])
            if ts > 0 and now - ts <= max_age_s:
                return True
    return False


class WifiActions:
    """NetworkManager + wg-quick shells. Dry-run (records what it would run)
    until `enabled`; the connection profiles for the convoy SSID and each
    hotspot must exist in NetworkManager (runbook §4)."""
    TIMEOUT_S = 20.0

    def __init__(self, iface: str = "wlan0", wg_iface: str = "convoy", enabled: bool = False,
                 hub_host: str = "10.66.0.1"):
        self.iface, self.wg_iface, self.enabled, self.hub_host = iface, wg_iface, enabled, hub_host
        self.log: list[str] = []
        self.joined: str | None = None
        self.base: str | None = None
        self.tunnel = False

    def _sh(self, cmd: str) -> str:
        self.log.append(cmd)
        if not self.enabled:
            return f"DRY-RUN: {cmd}"
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=self.TIMEOUT_S)
            return r.stdout
        except (subprocess.SubprocessError, OSError) as e:
            return f"ERROR: {e}"

    def join(self, ssid: str) -> None:
        self.joined = ssid
        self._sh(f"nmcli con up id {shlex.quote(ssid)}")

    def tunnel_up(self) -> None:
        self.tunnel = True
        self._sh(f"wg-quick up {shlex.quote(self.wg_iface)}")

    def tunnel_down(self) -> None:
        self.tunnel = False
        self._sh(f"wg-quick down {shlex.quote(self.wg_iface)}")

    def set_base(self, host: str) -> None:
        self.base = host                    # bridge/main re-targets engine + agent

    # observations
    def scan(self) -> dict[str, int]:
        if not self.enabled:
            return {}
        return parse_nmcli_scan(self._sh("nmcli -t -f SSID,SIGNAL dev wifi list"))

    def tunnel_ok(self, now: float) -> bool:
        if not self.enabled:
            return False
        return parse_wg_handshake(self._sh(f"wg show {shlex.quote(self.wg_iface)} latest-handshakes"), now)

    def internet_ok(self) -> bool:
        if not self.enabled:
            return False
        r = subprocess.run(["ping", "-c", "1", "-W", "1", self.hub_host], capture_output=True)
        return r.returncode == 0
