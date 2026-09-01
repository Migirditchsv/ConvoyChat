# convoy-comms

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
    bridge/   edge node: VAD gate + audio chain, eviction policy, agent
    base/     mixer (MixerAPI + pymixer), orchestrator + ladder, web UI, media
    sim/      fixtures, packet impairment proxy, virtual convoy
    tests/    S-xx Tier-0 suite (no hardware needed)
    tools/    bridgectl / fieldlog stubs (device-side, filled at M2)

## Quickstart (any Linux, no hardware)

    sudo apt install libopus0 espeak-ng   # opus runtime + fixture voices
    make setup
    make test                             # Tier-0: S-01 .. S-10

## Status

| Milestone | State |
|---|---|
| M0 scaffold + fixtures + chain-in-sim | done — S-01..S-06 green |
| M1 virtual convoy (mixer/orc/UI/media) | done — S-07..S-11 green (29/29) |
| M2 Bluetooth masquerade on metal | next — blocked on hardware arrival |

Safety rules SAFE-1..3 (fail-open mic, audible truth, priority never behind
experiments) are load-bearing: see `docs/decisions/` and the plan.
