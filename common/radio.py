"""Half-duplex radio link discipline (plan fallback layer): PTT, carrier
lockout, hang time, time-out timer, and station identification.

Used by the base-side gateway (base/media/radio.py: room <-> HT) and the
bike-side failover (bridge/radio.py: helmet <-> HT when Wi-Fi is gone).
Everything here is a pure per-frame state machine on 60 ms ticks, so the
legal behaviour (ID every N minutes and at the end of a communication,
never key without a callsign, never key over a busy channel, never key
longer than the time-out) is unit-tested to the frame (S-18).

Services: "ham" (Part 97: ID at least every 10 min and at the end of a
communication; analog voice, no encryption) or "gmrs" (Part 95E: every
15 min and at the end). Licensing is the operator's responsibility; the
software refuses to key without a callsign so an unconfigured bridge can
never transmit by accident.
"""
from __future__ import annotations
from collections import deque
import numpy as np

from common.audio import FS, FRAME, FRAME_MS, dbfs

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.", "/": "-..-.", "-": "-....-",
    " ": " ",
}
ID_INTERVAL_S = {"ham": 600.0, "gmrs": 900.0}


def morse_units(text: str) -> list[tuple[bool, int]]:
    """-> [(on, units), ...] — dot 1, dash 3, intra-char gap 1, char gap 3, word gap 7."""
    out: list[tuple[bool, int]] = []
    words = text.upper().split()
    for wi, word in enumerate(words):
        if wi:
            out.append((False, 7))
        for ci, ch in enumerate(word):
            code = MORSE.get(ch)
            if code is None:
                continue
            if ci:
                out.append((False, 3))
            for ei, el in enumerate(code):
                if ei:
                    out.append((False, 1))
                out.append((True, 3 if el == "-" else 1))
    return out


def morse_pcm(text: str, wpm: int = 20, tone_hz: float = 700.0, level_db: float = -12.0,
              fs: int = FS) -> np.ndarray:
    """Keyed CW audio for `text`. PARIS timing: one unit = 1.2 / wpm seconds.
    5 ms raised-cosine edges keep it click-free through a narrow FM rig."""
    unit = 1.2 / wpm
    amp = 32767 * 10 ** (level_db / 20)
    ramp = int(0.005 * fs)
    segs = []
    for on, units in morse_units(text):
        n = int(round(units * unit * fs))
        if not on:
            segs.append(np.zeros(n))
            continue
        t = np.arange(n) / fs
        x = np.sin(2 * np.pi * tone_hz * t) * amp
        env = np.ones(n)
        r = min(ramp, n // 2)
        env[:r] = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
        env[n - r:] = env[:r][::-1]
        segs.append(x * env)
    if not segs:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(segs).astype(np.int16)


# -- PTT actuators ---------------------------------------------------------

class FakePtt:
    """Test double: records (frame_index, on) transitions."""
    def __init__(self):
        self.on = False
        self.events: list[tuple[int, bool]] = []
        self.frame = 0

    def key(self, on: bool) -> None:
        on = bool(on)
        if on != self.on:
            self.on = on
            self.events.append((self.frame, on))


class GpioPtt:
    """Keys a rig through a GPIO line (optocoupler/transistor to the PTT pin).
    Uses gpiozero when present (RPi OS ships it), else the sysfs fallback so
    a bench Pi without gpiozero still works. `sysfs_root` is injectable for
    tests."""
    def __init__(self, pin: int, active_high: bool = True, sysfs_root: str = "/sys/class/gpio"):
        self.pin, self.active_high = int(pin), bool(active_high)
        self.on = False
        self._dev = None
        self._root = sysfs_root
        try:
            from gpiozero import OutputDevice          # type: ignore
            self._dev = OutputDevice(self.pin, active_high=self.active_high, initial_value=False)
        except Exception:
            self._sysfs_setup()

    def _sysfs_setup(self) -> None:
        import os
        gpio = os.path.join(self._root, f"gpio{self.pin}")
        if not os.path.isdir(gpio):
            with open(os.path.join(self._root, "export"), "w") as f:
                f.write(str(self.pin))
        with open(os.path.join(gpio, "direction"), "w") as f:
            f.write("out")
        self._sysfs_write(False)

    def _sysfs_write(self, on: bool) -> None:
        import os
        level = on if self.active_high else not on
        with open(os.path.join(self._root, f"gpio{self.pin}", "value"), "w") as f:
            f.write("1" if level else "0")

    def key(self, on: bool) -> None:
        self.on = bool(on)
        if self._dev is not None:
            (self._dev.on if self.on else self._dev.off)()
        else:
            self._sysfs_write(self.on)


class SerialPtt:
    """Keys a rig through a USB-serial control line (RTS or DTR driving a
    transistor) — the common x86 base-station arrangement. Pass an open
    pyserial object, or a port name to open one."""
    def __init__(self, port_or_serial, line: str = "rts"):
        if line not in ("rts", "dtr"):
            raise ValueError("line must be rts or dtr")
        self.line = line
        self.on = False
        if isinstance(port_or_serial, str):
            import serial                               # type: ignore
            self._ser = serial.Serial(port_or_serial)
        else:
            self._ser = port_or_serial
        self.key(False)

    def key(self, on: bool) -> None:
        self.on = bool(on)
        setattr(self._ser, self.line, self.on)


def make_ptt(spec: str):
    """'gpio:17' | 'gpio:17:low' | 'serial:/dev/ttyUSB0:rts' | 'none' -> actuator."""
    parts = (spec or "none").split(":")
    if parts[0] == "gpio":
        return GpioPtt(int(parts[1]), active_high=(len(parts) < 3 or parts[2] != "low"))
    if parts[0] == "serial":
        return SerialPtt(parts[1], parts[2] if len(parts) > 2 else "rts")
    if parts[0] in ("none", "fake", ""):
        return FakePtt()
    raise ValueError(f"unknown ptt spec {spec!r}")


# -- the link discipline ---------------------------------------------------

class RadioLink:
    """One half-duplex rig. Call process() once per 60 ms frame.

        out_to_rig, rx_for_ear = link.process(tx_frame, rx_frame)

    tx_frame: PCM we would like to transmit (None/silence = nothing to say).
    rx_frame: PCM from the rig's speaker/line out (None if unavailable).
    out_to_rig: PCM to feed the rig's mic while keyed, else None.
    rx_for_ear: received audio worth hearing (squelched), else None.

    Keying policy, in priority order each frame:
      1. no callsign -> never key (interlock; counted in `blocked`)
      2. cooldown after a time-out trip -> never key
      3. station ID pending -> keyed, ID audio out (speech dropped meanwhile)
      4. channel busy (rx energy above `rx_busy_db`, with hold) -> don't key up
      5. speech present (tx energy above `tx_thresh_db`) -> key, refresh hang
      6. hang time -> stay keyed for `hang_ms` after speech stops
    ID is sent when due at the end of a burst, forced mid-burst if a single
    key-down reaches the interval, and at the end of a communication
    (`end_id_after_s` of silence after any transmission since the last ID).
    """
    def __init__(self, ptt, callsign: str = "", service: str = "ham",
                 hang_ms: int = 600, tot_s: float = 180.0, cooldown_s: float = 5.0,
                 id_interval_s: float | None = None, end_id_after_s: float = 6.0,
                 tx_thresh_db: float = -45.0, rx_busy_db: float = -50.0,
                 rx_hold_ms: int = 500, morse_wpm: int = 20, id_level_db: float = -12.0,
                 id_pcm: np.ndarray | None = None):
        if service not in ID_INTERVAL_S:
            raise ValueError(f"service must be one of {sorted(ID_INTERVAL_S)}")
        self.ptt = ptt
        self.callsign = (callsign or "").strip().upper()
        self.service = service
        self.hang_frames = max(1, int(hang_ms / FRAME_MS))
        self.tot_frames = max(1, int(tot_s * 1000 / FRAME_MS))
        self.cooldown_frames = max(1, int(cooldown_s * 1000 / FRAME_MS))
        self.id_interval_frames = int((id_interval_s or ID_INTERVAL_S[service]) * 1000 / FRAME_MS)
        self.end_id_frames = max(1, int(end_id_after_s * 1000 / FRAME_MS))
        self.tx_thresh_db, self.rx_busy_db = tx_thresh_db, rx_busy_db
        self.rx_hold_frames = max(1, int(rx_hold_ms / FRAME_MS))
        self._id_pcm = id_pcm if id_pcm is not None else (
            morse_pcm(self.callsign, wpm=morse_wpm, level_db=id_level_db) if self.callsign
            else np.zeros(0, np.int16))
        # state
        self.keyed = False
        self.frame = 0
        self._hang = 0
        self._keyed_run = 0
        self._cooldown = 0
        self._rx_hold = 0
        self._idq: deque[np.ndarray] = deque()
        self._last_id_frame = 0
        self._tx_since_id = False
        self._last_tx_frame = -1
        self.rx_busy = False
        # metrics
        self.key_ups = 0
        self.tx_frames = 0
        self.ids_sent = 0
        self.tot_trips = 0
        self.busy_blocks = 0
        self.blocked = 0

    # -- helpers --
    def _queue_id(self) -> None:
        if not len(self._id_pcm):
            return
        pcm = self._id_pcm
        pad = (-len(pcm)) % FRAME
        pcm = np.concatenate([np.zeros(FRAME // 2, np.int16), pcm, np.zeros(pad + FRAME // 2, np.int16)])
        for i in range(0, len(pcm) - FRAME + 1, FRAME):
            self._idq.append(pcm[i:i + FRAME])
        self.ids_sent += 1
        self._last_id_frame = self.frame
        self._tx_since_id = False

    def _set_key(self, on: bool) -> None:
        if on and not self.keyed:
            self.key_ups += 1
            self._keyed_run = 0
        self.keyed = on
        if hasattr(self.ptt, "frame"):
            self.ptt.frame = self.frame
        self.ptt.key(on)

    def id_due(self) -> bool:
        return self._tx_since_id and (self.frame - self._last_id_frame) >= self.id_interval_frames

    # -- the tick --
    def process(self, tx_frame: np.ndarray | None, rx_frame: np.ndarray | None
                ) -> tuple[np.ndarray | None, np.ndarray | None]:
        self.frame += 1
        speech = tx_frame is not None and dbfs(tx_frame) > self.tx_thresh_db
        # carrier detect only means something while we are not keyed; the
        # hold keeps the channel "busy" for exactly rx_hold frames after the
        # last detection so a pause between their words is not our cue
        if not self.keyed and rx_frame is not None and dbfs(rx_frame) > self.rx_busy_db:
            self._rx_hold = self.rx_hold_frames + 1
        self.rx_busy = (not self.keyed) and self._rx_hold > 0
        if self._rx_hold > 0:
            self._rx_hold -= 1
        rx_for_ear = rx_frame if (self.rx_busy and rx_frame is not None) else None

        if not self.callsign:
            if speech:
                self.blocked += 1
            if self.keyed:
                self._set_key(False)
            return None, rx_for_ear

        if self._cooldown > 0:
            self._cooldown -= 1
            if self.keyed:
                self._set_key(False)
            return None, rx_for_ear

        # station ID scheduling
        if speech and not self.keyed and not self.rx_busy and not self._idq:
            pass
        if self.keyed and self._keyed_run >= self.id_interval_frames and not self._idq:
            self._queue_id()                                  # forced mid-burst
        if (not self.keyed and not self._idq and self._tx_since_id
                and self.frame - self._last_tx_frame >= self.end_id_frames):
            self._queue_id()                                  # end of communication
            if self.rx_busy:                                  # wait for a clear channel
                self._idq.clear(); self.ids_sent -= 1; self._tx_since_id = True

        out = None
        if self._idq:
            if not self.keyed and self.rx_busy:
                return None, rx_for_ear                       # hold the ID until clear
            if not self.keyed:
                self._set_key(True)
            out = self._idq.popleft()
            self._hang = self.hang_frames // 2
        elif speech:
            if not self.keyed:
                if self.rx_busy:
                    self.busy_blocks += 1
                    return None, rx_for_ear
                self._set_key(True)
            out = tx_frame
            self._hang = self.hang_frames
            self._tx_since_id = True
            self._last_tx_frame = self.frame
        elif self.keyed:
            if self._hang > 0:
                self._hang -= 1
                out = np.zeros(FRAME, np.int16)
            if self._hang == 0:
                if self.id_due():
                    self._queue_id()                          # end of burst, ID due
                    out = self._idq.popleft()
                else:
                    self._set_key(False)
                    out = None

        if self.keyed:
            self._keyed_run += 1
            self.tx_frames += 1
            if self._keyed_run > self.tot_frames:             # time-out timer
                self._set_key(False)
                self.tot_trips += 1
                self._cooldown = self.cooldown_frames
                self._idq.clear()
                return None, None
        return out, rx_for_ear

    def stats(self) -> dict:
        return {"callsign": self.callsign, "service": self.service, "keyed": self.keyed,
                "rx_busy": self.rx_busy, "key_ups": self.key_ups,
                "tx_s": round(self.tx_frames * FRAME_MS / 1000, 1), "ids_sent": self.ids_sent,
                "tot_trips": self.tot_trips, "busy_blocks": self.busy_blocks,
                "blocked": self.blocked,
                "since_id_s": round((self.frame - self._last_id_frame) * FRAME_MS / 1000, 1)}
