"""S-18: half-duplex radio link discipline, to the frame. A rig driven by
this code must never key without a callsign, never key over a busy
channel, never exceed the time-out, and must identify at the interval and
at the end of a communication."""
import numpy as np
import pytest

from common.audio import FRAME, FRAME_MS, dbfs
from common.dsp import tone, band_db
from common.radio import (RadioLink, FakePtt, GpioPtt, SerialPtt, make_ptt,
                          morse_units, morse_pcm, ID_INTERVAL_S)

SPEECH = tone(700, 0.06, level_db=-20)
SILENCE = np.zeros(FRAME, np.int16)
NOISE = (np.random.default_rng(3).standard_normal(FRAME) * 3000).astype(np.int16)


def run(link, script):
    """script: iterable of (tx, rx) per frame -> list of (keyed, out, ear)."""
    log = []
    for tx, rx in script:
        out, ear = link.process(tx, rx)
        log.append((link.keyed, out, ear))
    return log


def frames(n, tx=None, rx=None):
    return [(tx, rx)] * n


# -- Morse ------------------------------------------------------------------

def test_morse_units_paris_timing():
    """PARIS is the 50-unit reference word (43 on/off + 7 word gap)."""
    u = morse_units("PARIS")
    assert sum(n for _, n in u) == 43
    assert morse_units("E") == [(True, 1)]
    assert morse_units("EE") == [(True, 1), (False, 3), (True, 1)]
    assert morse_units("E E")[1] == (False, 7)
    assert morse_units("A?B") == morse_units("AB")          # unknown chars dropped


def test_morse_pcm_timing_and_tone():
    pcm = morse_pcm("PARIS PARIS", wpm=20)            # 43 + 7 + 43 = 93 units at 60 ms
    assert abs(len(pcm) / 16000 - 93 * 0.06) < 0.01
    # the tone sits at 700 Hz, nowhere else
    loud = pcm[np.abs(pcm.astype(np.int32)) > 2000]
    assert len(loud) > 16000 * 2
    seg = pcm[: FRAME * 2]
    assert band_db(seg, 600, 800) - band_db(seg, 1200, 3000) > 30
    assert len(morse_pcm("")) == 0


# -- PTT actuators ----------------------------------------------------------

def test_gpio_ptt_sysfs(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "gpiozero", None)   # force the sysfs path
    root = tmp_path
    (root / "export").write_text("")
    (root / "gpio17").mkdir()
    (root / "gpio17" / "direction").write_text("")
    (root / "gpio17" / "value").write_text("")
    p = GpioPtt(17, active_high=True, sysfs_root=str(root))
    assert (root / "gpio17" / "direction").read_text() == "out"
    assert (root / "gpio17" / "value").read_text() == "0"
    p.key(True);  assert (root / "gpio17" / "value").read_text() == "1"
    p.key(False); assert (root / "gpio17" / "value").read_text() == "0"
    low = GpioPtt(17, active_high=False, sysfs_root=str(root))
    low.key(True);  assert (root / "gpio17" / "value").read_text() == "0"


def test_serial_ptt_and_make_ptt():
    class Ser:
        rts = None; dtr = None
    s = Ser()
    p = SerialPtt(s, "rts")
    assert s.rts is False
    p.key(True); assert s.rts is True and s.dtr is None
    with pytest.raises(ValueError):
        SerialPtt(s, "cts")
    assert isinstance(make_ptt("none"), FakePtt)
    with pytest.raises(ValueError):
        make_ptt("smoke:signals")


# -- interlocks -------------------------------------------------------------

def test_no_callsign_never_keys():
    ptt = FakePtt()
    link = RadioLink(ptt, callsign="")
    run(link, frames(20, tx=SPEECH))
    assert ptt.events == [] and link.blocked == 20 and link.tx_frames == 0


def test_bad_service_rejected():
    with pytest.raises(ValueError):
        RadioLink(FakePtt(), "K1ABC", service="cb")


# -- keying, hang, busy -----------------------------------------------------

def test_keys_on_speech_and_hangs():
    ptt = FakePtt()
    link = RadioLink(ptt, "K1ABC", hang_ms=600)
    log = run(link, frames(5, tx=SPEECH) + frames(30, tx=SILENCE))
    assert ptt.events[0] == (1, True)
    keyed = [k for k, _, _ in log]
    assert all(keyed[:5])                                # keyed through speech
    assert all(keyed[5:14]) and not any(keyed[14:])      # 9 silent frames keyed, off on the 10th
    assert log[0][1] is SPEECH                           # speech goes to the rig
    assert not log[6][1].any()                           # hang fills with silence
    assert link.key_ups == 1 and ptt.events[1][1] is False


def test_never_keys_over_a_busy_channel_but_hears_it():
    ptt = FakePtt()
    link = RadioLink(ptt, "K1ABC", rx_hold_ms=180)
    log = run(link, frames(10, tx=SPEECH, rx=NOISE))
    assert ptt.events == [] and link.busy_blocks == 10
    assert all(ear is NOISE for _, _, ear in log)        # squelch open: we hear them
    # channel clears -> hold expires (3 frames) -> we key
    log2 = run(link, frames(6, tx=SPEECH, rx=SILENCE))
    assert [k for k, _, _ in log2] == [False, False, False, True, True, True]


def test_rx_ignored_while_keyed_and_squelched_when_quiet():
    link = RadioLink(FakePtt(), "K1ABC")
    log = run(link, frames(3, tx=SPEECH, rx=NOISE))      # busy first: no key
    assert not link.keyed
    link2 = RadioLink(FakePtt(), "K1ABC")
    log = run(link2, frames(3, tx=SPEECH, rx=SILENCE) + frames(3, tx=SPEECH, rx=NOISE))
    assert link2.keyed and all(ear is None for _, _, ear in log[3:])   # own sidetone ignored
    quiet = RadioLink(FakePtt(), "K1ABC")
    _, ear = quiet.process(None, SILENCE)
    assert ear is None                                     # nothing worth hearing


# -- time-out timer ---------------------------------------------------------

def test_time_out_timer_and_cooldown():
    ptt = FakePtt()
    link = RadioLink(ptt, "K1ABC", tot_s=1.2, cooldown_s=0.6)      # 20 frames, 10 cooldown
    log = run(link, frames(40, tx=SPEECH))
    keyed = [k for k, _, _ in log]
    assert all(keyed[:20]) and not any(keyed[20:30]) and all(keyed[31:])
    assert link.tot_trips == 1 and link.key_ups == 2


# -- station identification -------------------------------------------------

def _id_frames(link):
    return int(np.ceil((len(link._id_pcm) + FRAME) / FRAME))


def test_id_at_end_of_burst_when_due():
    link = RadioLink(FakePtt(), "K1ABC", id_interval_s=6.0, hang_ms=120)  # 100 frames
    run(link, frames(3, tx=SPEECH))                        # a burst, no ID yet
    assert link.ids_sent == 0
    run(link, frames(120, tx=SILENCE))                     # unkeys; end-of-comms ID fires
    assert link.ids_sent == 1
    # next burst 110 frames later: interval elapsed -> ID appended at end of burst
    run(link, frames(100, tx=SILENCE))
    log = run(link, frames(3, tx=SPEECH) + frames(2, tx=SILENCE))
    assert link.ids_sent == 2 and link.keyed             # still keyed, sending ID
    n_id = _id_frames(link)
    log = run(link, frames(n_id + 5, tx=SILENCE))
    assert not link.keyed
    id_audio = np.concatenate([o for _, o, _ in log if o is not None])
    assert band_db(id_audio, 600, 800) > -40              # the CW went to the rig


def test_id_forced_mid_burst_at_interval():
    link = RadioLink(FakePtt(), "K1ABC", id_interval_s=3.0, tot_s=60)   # 50 frames
    log = run(link, frames(80, tx=SPEECH))
    assert link.ids_sent == 1
    outs = [o for _, o, _ in log]
    # frames 0..49 speech, then the ID overrides speech for its duration
    assert outs[10] is SPEECH and outs[52] is not SPEECH and dbfs(outs[52]) > -60


def test_end_of_communication_id_waits_for_clear_channel():
    link = RadioLink(FakePtt(), "K1ABC", end_id_after_s=0.6, hang_ms=60)   # 10 frames
    run(link, frames(2, tx=SPEECH))
    run(link, frames(12, tx=SILENCE, rx=NOISE))            # someone else talking
    assert link.ids_sent == 0 and not link.keyed
    run(link, frames(12, tx=SILENCE, rx=SILENCE))          # clear -> ID goes out
    assert link.ids_sent == 1 and link.keyed
    run(link, frames(_id_frames(link) + 5, tx=SILENCE))
    assert not link.keyed


def test_service_intervals_and_stats():
    ham = RadioLink(FakePtt(), "K1ABC")
    gmrs = RadioLink(FakePtt(), "WRXY123", service="gmrs")
    assert ham.id_interval_frames * FRAME_MS / 1000 == ID_INTERVAL_S["ham"] == 600
    assert gmrs.id_interval_frames * FRAME_MS / 1000 == 900
    s = ham.stats()
    assert s["callsign"] == "K1ABC" and s["keyed"] is False and s["ids_sent"] == 0
