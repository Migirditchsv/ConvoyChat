# ConvoyChat

Wi-Fi group intercom for motorcycle convoys. Riders bring their own Bluetooth
headsets (Cardo / Sena / cheap knockoffs); a $25 Pi on each bike masquerades as
the headset's phone (HFP Audio Gateway), gates speech with a VAD, and streams
Opus/RTP over 5 GHz Wi-Fi to a chase-car Linux mixer that runs rooms, a
priority ladder, ducked music, and a phone-browser dashboard that never
touches audio.

**Governing documents** (Claude artifacts, ask Sam for links):
*Convoy Build Plan* — module contracts, invariants INV-1..11, SAFE-1..3,
tests S/H/F. *Convoy Voice Trade Study* — the rationale behind every choice.
Read the plan's §00 change contract before modifying anything; decisions log
to `docs/decisions/`.

## Layout

    common/   protocol, roster, DSP, Opus ctypes binding, earcons
    bridge/   edge node: main (Pi entry point), config (/boot/convoy.toml),
              VAD gate + audio chain, subprocess audio IO, agent, link stats
              + eviction policy
    base/     main (--mode sim|hw|field), mixer (MixerAPI + pymixer),
              orchestrator + ladder, pages (/ /rider /ops), media + TTS
    sim/      fixtures, packet impairment proxy, live virtual riders, demo
    deploy/   roster + convoy.toml examples, systemd units
    tests/    S-01..S-17 Tier-0 suite (no hardware needed)
    docs/     runbook (the three modes), decisions DR-001..010, review notes

## Quickstart (any Linux, no hardware)

    sudo apt install libopus0 espeak-ng   # opus runtime + TTS/fixture voices
    make doctor      # preflight — every dependency checked, with remedies
    make setup
    make up-sim      # START HERE: the whole network on this laptop, forever.
                     #   Prints the LAN URL. Any phone on the Wi-Fi opens it:
                     #   /rider = the rider's page (hold-to-talk, volume, room,
                     #   headset pairing), /ops = chase-car dashboard.
                     #   Six virtual riders run the real bridge engine on wind +
                     #   real speech; the phone page's TALK button drives them.
    make demo        # the 40 s scripted tour instead: writes demo_out/*.wav
    make listen      # play the headline demo recording
    make test        # Tier-0: S-01 .. S-17 (57 tests, ~70 s)

Three modes, one stack — `docs/runbook.md` is the copy-paste guide:

    make up-sim      # sim: router, phones, bridges all spoofed on one machine
    make up          # hardware test: real Pis + headsets, verbose diagnostics
    make up-field    # field: same, quiet; deploy/*.service for boot-time
    make bridge      # on a Pi: the headset bridge from /boot/convoy.toml

Nothing compiles — pure Python over system libopus. Phones never carry
audio (INV-10); the pages are plain HTTP on the convoy Wi-Fi and need no
internet. New to the project? Read `catchup.md`.

## Status

| Milestone | State |
|---|---|
| M0 scaffold + fixtures + chain-in-sim | done — S-01..S-06 green |
| M1 virtual convoy (mixer/orc/UI/media) | done — S-07..S-12 green (31/31) |
| M1.1 real-speech fixtures + operator debug plane | done — DR-008/009 |
| M1.2 LAN-ready: rider phone page, PTT, bridge entry point, three run modes | done — DR-010, S-13..S-17 (57/57) |
| M2 Bluetooth masquerade on metal | next — blocked on hardware arrival |

Safety rules SAFE-1..3 (fail-open mic, audible truth, priority never behind
experiments) are load-bearing: see `docs/decisions/` and the plan.
