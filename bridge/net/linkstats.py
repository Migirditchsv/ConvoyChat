"""Link statistics provider for the bridge heartbeat + eviction policy (INV-9).

`iw dev <iface> station dump` is the only thing a stock Nighthawk client can
see about its own link; we parse signal and tx bitrate from it once a second.
RTP loss is measured locally: the downlink is one packet per 60 ms tick while
the mixer runs, so concealed frames / pulled frames over the last window is
the loss the rider actually hears. Pure functions are unit-tested on a
captured dump (S-16); the subprocess wrapper is a thin shell."""
from __future__ import annotations
import asyncio
import re
import shutil
import subprocess

from bridge.net.supervisor import LinkStats

_SIGNAL = re.compile(r"^\s*signal:\s*(-?\d+)", re.M)
_SIGNAL_AVG = re.compile(r"^\s*signal avg:\s*(-?\d+)", re.M)
_TX_RATE = re.compile(r"^\s*tx bitrate:\s*([\d.]+)\s*MBit/s", re.M)
_TX_FAILED = re.compile(r"^\s*tx failed:\s*(\d+)", re.M)
_TX_PKTS = re.compile(r"^\s*tx packets:\s*(\d+)", re.M)


def parse_station_dump(text: str) -> dict:
    """-> {"rssi": dBm, "tx_rate": Mbit/s, "tx_failed": n, "tx_packets": n}
    for the first station (the AP) in an `iw ... station dump`. Missing
    fields are None; an empty dump (not associated) is all None."""
    out = {"rssi": None, "tx_rate": None, "tx_failed": None, "tx_packets": None}
    if not text or "Station" not in text:
        return out
    first = text.split("Station", 2)[1] if text.count("Station") > 1 else text
    m = _SIGNAL_AVG.search(first) or _SIGNAL.search(first)
    if m:
        out["rssi"] = int(m.group(1))
    m = _TX_RATE.search(first)
    if m:
        out["tx_rate"] = float(m.group(1))
    m = _TX_FAILED.search(first)
    if m:
        out["tx_failed"] = int(m.group(1))
    m = _TX_PKTS.search(first)
    if m:
        out["tx_packets"] = int(m.group(1))
    return out


class IwLinkStats:
    """Callable provider for BridgeAgent(link_stats=...) and the eviction tick.
    `engine` supplies downlink loss; `iw` supplies radio numbers. Missing iw
    (a laptop in --sim, a wired dev box) degrades to loss-only, never raises."""
    def __init__(self, iface: str = "wlan0", engine=None, runner=None):
        self.iface = iface
        self.engine = engine
        self.runner = runner or self._run_iw
        self.last: dict = {}
        self._radio: dict = {}
        self._pending = None

    def _run_iw(self) -> str:
        if not shutil.which("iw"):
            return ""
        try:
            return subprocess.run(["iw", "dev", self.iface, "station", "dump"],
                                  capture_output=True, text=True, timeout=1.5).stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    def _refresh_radio(self) -> dict:
        self._radio = parse_station_dump(self.runner())
        return self._radio

    def __call__(self) -> dict:
        """Inside a running loop the `iw` subprocess runs in a worker thread
        (never blocking the 60 ms audio tick behind a hung `iw`); the call
        returns the previous reading. Outside a loop it is synchronous."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._refresh_radio()
        elif self._pending is None or self._pending.done():
            self._pending = loop.run_in_executor(None, self._refresh_radio)
        d = dict(self._radio) if self._radio else parse_station_dump("")
        d["rtp_loss"] = self.engine.downlink_loss_pct() if self.engine is not None else None
        self.last = d
        return d

    def as_link_stats(self) -> LinkStats:
        """For EvictionPolicy.tick_1s: unknown radio numbers are treated as
        healthy (never evict on missing data — an eviction is itself an outage)."""
        d = self.last or self()
        return LinkStats(tx_rate_mbps=d["tx_rate"] if d.get("tx_rate") is not None else 999.0,
                         rtp_loss_pct=d["rtp_loss"] if d.get("rtp_loss") is not None else 0.0)
