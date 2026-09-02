# catchup.md — state handover for the local agent

You are picking up **ConvoyChat** (working name `convoy-comms`): a Wi-Fi group
intercom for motorcycle convoys. Riders bring their own Bluetooth helmet
headsets (Cardo / Sena / $10 Coolreach-X7-class knockoffs); a **Raspberry Pi
3 A+ on each bike masquerades as the headset's phone** (Bluetooth HFP
Audio-Gateway role — the same trick Sena "Universal Intercom" and "Cardo
Gateway" ship commercially), gates speech with a VAD so wind never crosses
the air, and streams Opus/RTP over 5 GHz Wi-Fi to a **chase-car Linux tablet**
running an N-1 mixer with rooms, a priority ladder (chase=emergency >
lead=announcement > riders > music), ducked music, and a phone-browser
dashboard that never touches audio. A Netgear Nighthawk (stock firmware) is
the AP. No cellular assumed (desert). GMRS handhelds + Meshtastic are the
independent fallback layer.

Everything that exists is **pure Python 3.11+** (numpy + a vendored ctypes
binding to system libopus). Nothing compiles. `make help` lists every
entry point.

## Where the truth lives

1. **Tests are the spec.** 32 Tier-0 tests (S-xx), all green. If you change
   behavior, change the test in the same commit.
2. `docs/decisions/DR-001..009` — every deviation from the plan, with
   revisit-conditions. Add a DR when you deviate; the format is in DR-001.
3. Two governing Claude artifacts owned by Sam (ask him to share if needed):
   *Convoy Build Plan* (module contracts B-1..B-4 / S-1..S-4 / O-1,
   invariants, test tiers S/H/F, milestones) and *Convoy Voice Trade Study*
   (the physics/platform rationale behind every invariant, with sources).

## Current state (2026-09-02)

| Milestone | Status |
|---|---|
| M0 scaffold, fixtures, gated audio chain in sim | done |
| M1 virtual convoy: mixer, orchestrator, dashboard, media | done |
| M1.1 real-speech fixtures; operator debug plane; `make demo` | done |
| **M2 Bluetooth masquerade on real Pi + headsets** | **next — blocked on hardware delivery (~this week)** |

Git: 5 commits on `main`, pushed to `github.com/Migirditchsv/ConvoyChat`.
CI: `.github/workflows/tier0.yml` runs `make test` on ubuntu-latest.

## Run it

    make doctor      # preflight with remedies
    make setup       # pip deps (PEP-668-aware; venv fallback printed on refusal)
    make demo        # THE entry point: 40 s scripted ride on the real stack.
                     #   Live dashboard: http://localhost:8080 (mute/trim/vol/
                     #   identify/reboot work mid-ride). Writes demo_out/:
                     #   ears/*.wav (what each rider heard), mouths/*.wav
                     #   (what their mic picked up), timeline.txt, README.txt.
    make listen      # plays the headline recording (ears/r3_rider.wav)
    make test        # 32 tests, ~90 s (Silero over ~25 min of audio dominates)
    make base        # base station alone — prints ONE line then serves; not hung
    make convoy      # raw 6-rider sim, no dashboard/wavs

Expectations that have confused people: nothing plays through speakers
except `make listen` — the demo's product is FILES plus the live dashboard.
All audio is deliberately 16 kHz mono phone-call quality (INV-2): that is
what a helmet headset receives over HFP.

## Architecture in one screen

    [rider's own headset] --BT HFP/SCO (mono 8/16k)--> [bridge/ on Pi 3A+]
        bridge/audio: HPF(speed) -> SafeVad -> SpeechGate(pre-roll 900ms)
                      -> AGC -> Opus(60ms, DTX, FEC) -> RTP
        bridge/agent: WS control client (heartbeat 1Hz, acked node_cmds)
        bridge/net:   self-eviction policy (client-side, INV-9)
    --5GHz Wi-Fi, RTP up to :5100 (SSRC=crc32(node_id)), down :6100+2n-->
    [base/ on x86 tablet]
        base/mixer/pymixer: 60ms tick, per-talker gains, N-1 per listener,
                            lead-broadcast added once per foreign room, PLC
        base/orc: ladder + hangover, mute/trim COMPOSING with ladder
                  (effective = muted?0 : ladder*trim/100), node_cmd routing,
                  snapshots (authoritative; deltas are conveniences)
        base/ui:  static dashboard, plain HTTP :8080, WS :8800 (no TLS by
                  design — the page never touches audio, so no getUserMedia)
        base/media: RtpSource participants (music / TTS / probes)
    [phone browsers] --Wi-Fi, control only--> dashboard

Sim = the REAL stack on fake edges: `sim/vheadset` boundary is just
ArraySource/ArraySink; `sim/impair.py` is an in-process netem (profiles:
bench/parkinglot/edge/cliff/flap); `sim/convoy.py` and `sim/demo.py` run
real engines, mixer, orchestrator, agents.

## Invariants (violate none without Sam; full rationale in the artifacts)

- INV-1 mic only via HFP with bridge as Audio Gateway
- INV-2 design for 8 kHz CVSD; 16 kHz mSBC is a detected bonus
- INV-3 SCO through a USB BT dongle (RTL8761B class), never Pi onboard BT
- INV-4 Wi-Fi uplink on 5 GHz (dongle/Wi-Fi have no coex wiring on 2.4)
- INV-5 all noise removal/gating on the bridge, BEFORE Opus
- INV-6 base mixes; one downlink stream per rider; always N-1
- INV-7 media UDP never retransmitted; control TCP/WS with full snapshots
- INV-8 Opus 60 ms + DTX + FEC; VAD gate ahead of it
- INV-9 weak-link eviction is the bridge's job (stock Nighthawk can't)
- INV-10 phones never touch audio; UI is plain HTTP on the LAN
- INV-11 bridges run read-only rootfs; identity in /boot/convoy.toml

Safety (may not be weakened): **SAFE-1** any DSP failure fails OPEN
(transmit) — SafeVad demotes silero->energy->OPEN, exceptions instantly,
time-budget overruns only when sustained (3 frames, DR-007). **SAFE-2** state
changes are audible via bridge-local earcons; earcons mix AFTER volume so a
vol-down rider still hears alerts. **SAFE-3** lead/chase paths never get
experimental DSP first.

## Decision record index

DR-001 pure-Python DSP core, IO adapters (gst deferred to device) ·
DR-002 pymixer first, Janus only if H-03 shows jitter inadequacy ·
DR-003 in-process impair proxy for CI, tc netem on hardware ·
DR-004 vendored ctypes opus binding · DR-005 single uplink port + SSRC id ·
DR-006 probes are noise BANDS (Opus voice mode mangles pure tones ~10 dB) ·
DR-007 SAFE-1 sustained-overrun demotion · DR-008 post-band-limit fixture
calibration + speed-profile gate (see "gate numbers" below) ·
DR-009 operator debug plane (node_cmd/ack/audio_ctl, compose rules).

## Gate numbers you must not "fix" blindly (DR-008)

Fixtures: five real Harvard utterances (sim/ext/speech/, committed) over
synthetic wind at three tiers — post-band-limit-calibrated SNR +12 / 0 /
-6 dB at 50/90/120 km/h. Current measured performance: **50 & 90 km/h: 0%
missed speech, 5/5 onsets transmitted; 120 km/h: ~20% missed, 2/5 onsets —
a pinned floor, not a bug.** One utterance peaks at Silero prob 0.52 for a
single frame at -6 dB; wind shows ZERO consecutive frames >0.35 in 5 min
(hence the >=110 km/h profile: open 0.35, 2-frame confirm, keep 0.10).
The fix for the 120 gap is mechanical (mic placement/sealing) per the trade
study. Re-derive PROFILES only from new measured separability — the method
is in this repo's history and DR-008.

## Known gaps / M2 worklist (in priority order)

1. **B-1 on metal** (the only real feasibility risk): PipeWire
   `bluez5.roles=[hfp_ag]` + mSBC; oFono+phonesim fake call (some headsets
   won't open the mic outside a "call" — three documented categories,
   undetectable in software); `tools/bridgectl pair` (bluetoothctl
   NoInputNoOutput agent + trust) and a reconnect supervisor. Qualify EVERY
   headset model: mic-outside-call? codec (btmon: CVSD/mSBC)? survives 10
   call cycles + 20 power cycles? ownership vs the rider's phone (H-07)?
2. Bridge IO adapter for real audio (pw-cat/ALSA subprocess or gst shell
   around the existing chain — interfaces already match, DR-001).
3. `DeviceActions.enabled=true` wiring (reboot/reconnect shells are
   documented in bridge/agent.py; volume/identify/hb already real).
4. Wire `bridge/net/EvictionPolicy` into a real link-stats provider
   (`iw dev wlan0 station dump` parse) — policy is unit-tested, unwired.
5. bridge/svc: golden image build (RPi OS Lite 64, overlayfs RO root,
   systemd units, watchdog, /boot/convoy.toml) — speced in the plan, unwritten.
6. base/media: music from files/playlist + Piper TTS ("text -> spoken
   announcement") — RtpSource plumbing exists, sources are stubs.
7. Real roster.yaml (schema in common/roster.py + plan §03).
8. Nighthawk R-1 config checklist on the actual unit; record model in
   docs/hardware.md. Note stock AX firmware ships OFDMA OFF — enable it.
9. Re-baseline fixtures from real ride captures when the recorder exists.

## Traps the hardware week will meet (all sourced in the trade study)

- Pi onboard BT cannot carry SCO reliably -> USB dongle mandatory (INV-3);
  verify dongle silicon with `lsusb` (TP-Link UB500 has revision drift;
  EDUP EP-B3536 is the safe RTL8761BU).
- Open kernel bug: BT dongle + other USB audio traffic on one bus can reset
  the dongle after an HFP call (raspberrypi/linux#6690). Keep the dongle as
  the lone USB device; soak-test 10 call cycles.
- mSBC SCO socket has no kernel buffering -> SCHED_FIFO the audio thread.
- The AG masquerade occupies the headset's phone slot: native Cardo/Sena
  mesh will NOT run alongside; rider's phone must not contend (ride-start
  ritual is in the plan's B-1 lifecycle + O-1).

## Repo map

    common/    protocol (RTP + control JSON), roster, dsp, opusbind, earcons
    bridge/    audio chain (vad/gate/chain/engine), agent, net policy, io
    base/      mixer (api + pymixer), orc (ladder + server), ui, media, main
    sim/       fixtures (+ext/ real speech & wind drop-ins), impair, convoy, demo
    tests/     S-01..S-12 + SAFE-1 semantics — `make test`
    tools/     bridgectl / fieldlog stubs (fill at M2)
    docs/      decisions/, hardware.md (fill at build), ride-checklist.md

Conventions: trunk-based on main; commit messages explain WHY; every commit
ends with the session attribution trailer (see git log); sim/data/ and
demo_out/ are generated, never committed.
