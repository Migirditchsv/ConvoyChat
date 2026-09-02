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

1. **Tests are the spec.** 115 Tier-0 tests (S-xx), all green. If you change
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
| M1.2 LAN-ready (2026-09-02 review): rider phone page + PTT, bridge entry point, three run modes, transport fixes | done — DR-010, docs/review-2026-09-02.md |
| M1.3 fallback layers in software: ham/GMRS gateway + bike RF failover (DR-011), hotspot + WireGuard failover (DR-012) | done — S-18..S-20; hardware-in-the-loop when rig/HT/sound cards arrive |
| M1.4 operator self-repair: issues+fixes on /ops, bike self-check, persisted settings, watchdogs, headset supervisor, spectral fallback VAD, corrupt-packet hardening | done — DR-013/014, S-21..S-24 |
| **M2 Bluetooth masquerade on real Pi + headsets** | **next — blocked on hardware delivery (~this week)** |

Git: 7 commits on `main`, pushed to `github.com/Migirditchsv/ConvoyChat`.
CI: `.github/workflows/ci.yml` runs `make test` on ubuntu-latest.

## Run it

    make doctor      # preflight with remedies
    make setup       # pip deps (PEP-668-aware; venv fallback printed on refusal)
    make up-sim      # THE entry point: whole network on one laptop, forever.
                     #   Prints LAN URLs. /rider on any phone = the rider's
                     #   page (hold-to-talk, vol, room, pair headset); /ops =
                     #   chase dashboard. Six virtual riders (real engines on
                     #   wind + real speech) chatter on their own.
    make up          # hardware-test base (verbose); make bridge on each Pi
    make up-field    # quiet base; deploy/*.service for boot-time
    make demo        # 40 s scripted ride -> demo_out/ears|mouths/*.wav, timeline
    make listen      # plays the headline recording (ears/r3_rider.wav)
    make test        # 115 tests, ~90 s (Silero over ~25 min of audio dominates)
    make status      # curl /snapshot.json from a running base

`docs/runbook.md` is the copy-paste path for sim / hardware test / field.
Expectations that have confused people: nothing plays through speakers
except `make listen` / `--monitor` — the products are pages, acks and FILES.
All audio is deliberately 16 kHz mono phone-call quality (INV-2): that is
what a helmet headset receives over HFP. Six Silero VADs in one process can
overrun SAFE-1's budget on a slow laptop and demote to energy (red badge on
/ops) — `--energy-vad` avoids the noise; on a Pi each bridge runs alone.

## Architecture in one screen

    [rider's own headset] --BT HFP/SCO (mono 8/16k)--> [bridge/ on Pi 3A+]
        bridge/main:  entry point; /boot/convoy.toml (bridge/config); CmdSource/
                      CmdSink = pw-record/pw-play pipes, supervised
        bridge/audio: HPF(speed) -> SafeVad -> SpeechGate(pre-roll 900ms; PTT
                      = force_open from the phone) -> AGC -> Opus(60ms, DTX,
                      FEC) -> RTP; downlink reorder + PLC + earcons after vol
        bridge/agent: WS control client (hello[+token], heartbeat 1Hz with
                      link/headset state, VAD open/close -> base, acked
                      node_cmds incl. ptt/bt_scan/bt_pair/bt_status/say)
        bridge/net:   iw station-dump link stats -> eviction policy (INV-9)
    --5GHz Wi-Fi, RTP up to :5100 (SSRC=crc32(node_id)), down :6100+2n
      (mixer replies to the uplink's source host: no bridge IPs in the roster)-->
    [base/ on x86 tablet]  base/main --mode sim|hw|field
        base/mixer/pymixer: 60ms tick, per-talker gains, N-1 per listener,
                            lead-broadcast added once per foreign room, PLC,
                            resync after long gaps, wrap-safe seq
        base/orc: ladder + hangover, mute/trim COMPOSING with ladder
                  (effective = muted?0 : ladder*trim/100), node_cmd routing,
                  snapshots PUSHED to pages (debounced + 1 Hz; authoritative)
        base/ui:  /  landing   /rider phone page   /ops dashboard
                  /snapshot.json; plain HTTP :8080, WS :8800 (no TLS by
                  design — pages never touch audio, so no getUserMedia)
        base/media: RtpSource participants (music / TTS / probes)
        sim/live: virtual riders (real engines+agents on looping mouths) in
                  the base process for --mode sim
    [phone browsers] --Wi-Fi, control only--> /rider (each rider) and /ops

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
(transmit) — SafeVad demotes silero->spectral->energy->OPEN, exceptions instantly,
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
DR-009 operator debug plane (node_cmd/ack/audio_ctl, compose rules) ·
DR-010 rider phone page via the base, PTT, symmetric RTP, pushed snapshots,
run modes, bridge entry point · DR-011 radio fallback (half-duplex link
discipline, base gateway, bike failover, mixer exclude mask) · DR-012
hotspot + WireGuard failover state machine · DR-013 spectral fallback VAD
+ fast floor tracker · DR-014 operator self-repair (issues/fixes, doctor,
persisted settings, watchdog, headset supervisor).

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
   undetectable in software). Pairing path exists (bt-agent unit +
   `bt_scan`/`bt_pair` from the rider page, `DeviceActions` shells, dry-run
   until `[actions] enabled`) but is UNVERIFIED against real bluetoothctl
   output. Qualify EVERY headset model: mic-outside-call? codec (btmon:
   CVSD/mSBC)? survives 10 call cycles + 20 power cycles? ownership vs the
   rider's phone (H-07)?
2. Real audio IO on the Pi: `CmdSource`/`CmdSink` exist and are tested with
   fake commands; the pw-record/pw-play `--target` node names for the HFP
   AG source/sink must be found on the device (runbook §2.2) — put them in
   the toml. Measure tick busy % (DR-001 revisit: >30 % of a core).
3. Golden image (RPi OS Lite 64, overlayfs RO root, deploy/*.service,
   /boot/convoy.toml) — units written, image build procedure not.
4. Per-command ack timeouts in the orchestrator (DR-009 revisit; pending
   acks on a dead node currently stay "pending" on the phone).
5. GPS feed: `on_gps` exists; /ops could post browser Geolocation. Until
   then `[node] speed_kmh` in the toml sets the HPF corner statically and
   the ops sim-speed slider drives the self-move rule.
6. base/media: music from files/playlist (RtpSource plumbing exists; file/
   playlist source still a stub). TTS is DONE (S-13). `make base-live`.
7. Nighthawk R-1 config checklist on the actual unit; record model in
   docs/hardware.md. Note stock AX firmware ships OFDMA OFF — enable it.
8. Re-baseline fixtures from real ride captures when the recorder exists.
9. Browser-driven smoke test of /rider (Playwright is a 30-line add; see
   docs/review-2026-09-02.md O4) and the "substantial improvements" list
   there (bridge-local rider page, RTCP-lite expected-loss, roster reload).
10. Hardware-in-the-loop for the fallbacks: real HT on a USB sound card /
    I2S HAT with serial-RTS or GPIO PTT (runbook §4); a rider hotspot + a
    WireGuard hub (runbook §5). Software is tested to the frame/second on
    fakes (S-18..S-20) and in `base.main --mode sim --rf`; the shells
    (arecord/aplay, nmcli, wg-quick) are dry-run until [actions] enabled.

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
    bridge/    main + config (Pi entry point), audio chain (vad/gate/chain/
               engine), agent (Sim/DeviceActions), io (arrays, UDP, Cmd*),
               net (linkstats + eviction policy)
    base/      main (--mode), mixer (api + pymixer), orc (ladder + server),
               ui/static (index/rider/ops), media (participants + tts)
    sim/       fixtures (+ext/ real speech & wind drop-ins), impair, convoy,
               demo (40 s tour), live (forever virtual riders)
    deploy/    roster.example.yaml, convoy.example.toml, systemd units
    tests/     S-01..S-24 + SAFE-1 semantics — `make test` (115)
    tools/     bridgectl (stub) / fieldlog (heartbeats -> JSONL)
    docs/      runbook.md (three modes), review-2026-09-02.md, decisions/,
               hardware.md, ride-checklist.md

Conventions: trunk-based on main; commit messages explain WHY; every commit
ends with the session attribution trailer (see git log); sim/data/ and
demo_out/ are generated, never committed.
