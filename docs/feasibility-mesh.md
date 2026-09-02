# Feasibility: peer-to-peer audio and a mesh network (2026-09-02)

Question: can the Pis serve audio to each other over a mesh, for robustness
and for continued comms when riders are separated from the chase car?

Short answer: **yes for partition tolerance, cheaply, without a mesh** —
by making the base mobile and federating mixers (probe below, zero code).
**Radio-layer mesh on the Pi 3A+ is not feasible** (the onboard radio can't
do it and the single USB port is taken); on a Pi 4B it is possible at
~$47/bike plus 2–4 days of work, and it buys multi-hop range, not
partition tolerance. **Mixerless peer fan-out** is the full answer but
changes INV-6 and is 1–2 weeks of work.

The request is really two goals. They have different answers.

| goal | what breaks today | cheapest thing that fixes it |
|---|---|---|
| G1 partition tolerance: riders keep talking with the car out of range | the mixer is in the car; no car, no audio at all | put a base where the riders are (lead bike) and trunk the two bases |
| G2 range robustness: a stretched column stays connected | every bike must reach the car's AP directly | multi-hop relaying (mesh) — or just a second AP on the lead bike |

## 1. Radio layer: what the hardware can and cannot do

- **The Pi's onboard Wi-Fi cannot be a mesh point.** `brcmfmac` is a
  FullMAC driver; 802.11s is not offered (`iw list` shows no "mesh point").
  This holds for the 3A+, 3B+, 4B and CM4 (same CYW43455).
- **IBSS (ad-hoc, what batman-adv needs) is unreliable on it.** The 2018-era
  batman-adv-on-Pi tutorials all use 2.4 GHz; a 2024 thread reports ad-hoc
  on a Pi 4 with Bookworm failing "time after time". 2.4 GHz is closed to
  us anyway (INV-4: the USB BT dongle and 2.4 GHz Wi-Fi have no coexistence
  wiring).
- **One radio = one mode.** A Pi cannot be a client of the Nighthawk and a
  mesh member at the same time. Mesh means the car becomes a mesh node too
  and the Nighthawk is retired or reduced to the car's LAN.
- **A mesh-capable radio is a USB adapter** (MT7612U, e.g. Alfa
  AWUS036ACM, in-kernel `mt76`, supports IBSS and 802.11s, ~$47). On the
  **3A+ that needs a hub next to the BT dongle**, putting SCO isochronous
  traffic and Wi-Fi bulk traffic on the `dwc_otg` controller whose
  isochronous handling is the reason INV-3 exists. On a **Pi 4B** each gets
  its own xHCI port. So: radio-layer mesh is a Pi 4B decision, ~$47 +
  ~$20 board delta per bike.
- Channel: 5 GHz mesh/IBSS must sit on non-DFS channels (US UNII-3,
  149–161); DFS channels require radar detection that IBSS/mesh cannot do.

## 2. Audio plane: three ways to carry voice once the network exists

### A. Central mixer over a mesh (no audio change)
The mixer speaks plain UDP; batman-adv is layer 2, so the current stack
runs unchanged over a mesh. Gains G2 (multi-hop range) only. Loses
everything when the car partitions off. Literature: batman-adv keeps VoIP
delay acceptable over several hops, but loss and jitter degrade sharply
beyond 2 hops; direct RTT ~5 ms, multi-hop 20–30 ms. Our one-way budget is
350 ms, so 2–3 hops fit.

### B. Federated bases (probe: works today, zero code)
Two mixers trunked through one shared participant: each base adds a
participant `link` pointing its downlink at the other base's uplink port.
Same pid → same SSRC in both directions, and the mixer's N-1 arithmetic
excludes the trunk from what it sends back, so there is no echo by
construction.

    A.add_participant("link", "main", (B_ip, 5100))
    B.add_participant("link", "main", (A_ip, 5100))

Measured in this session (two PyMixers, one rider each, band-noise probes,
S-07 method):

    rider on A hears the other base's rider at -30 dB, own echo at -68 dB
    rider on B hears the other base's rider at -31 dB, own echo at -73 dB

Cost: +60–120 ms latency across the trunk; one trunk per room; the ladder
does not cross the trunk (a lead on base A does not duck riders on base B)
until `vad` events are forwarded base-to-base over the control WS, a
~50-line addition. Symmetric-RTP peer learning already makes the trunk
follow the other base's address.

**Deployment that gives G1 with no new software:** base #1 on the lead
bike (a Pi 4B running `base.main` plus `hostapd` on 5 GHz, or a 12 V
travel router such as a GL.iNet Beryl AX as the AP), base #2 in the chase
car as today, trunk between them. Bikes associate to whichever AP they
can reach (same SSID/PSK; wpa_supplicant roams; symmetric RTP re-learns
the address after a roam). With the car out of range the lead-bike base
keeps the riders talking; when it comes back the trunk reconnects on its
own (every path in the stack already reconnects). The chase car keeps its
dashboard, TTS and music as participants of its own base.

### C. Mixerless peer fan-out (the "real" mesh audio)
Each bridge sends its gated Opus stream to every peer (unicast fan-out,
≤5 copies × 16 kbit/s = 80 kbit/s when talking; gated, so idle is zero)
and mixes what it receives locally: N-1 is automatic (you never decode
yourself), the ladder becomes a per-node gain table applied by role from
a gossiped roster, rooms become a receive filter, the lead broadcast a
receive rule. The base becomes an optional participant that also serves
pages. Nothing central; any subset of bikes that can reach each other
talks.

- CPU on a 3A+: decode only active talkers (gate + DTX ⇒ typically 1–2)
  ≈ +2–6 ms per 60 ms tick on top of today's ~50% of a core. Fits.
- Changes INV-6 ("base mixes, one downlink per rider") and the
  snapshot-authoritative control model (INV-7 becomes "each node is
  authoritative for itself"). Needs Sam's sign-off and a DR.
- Work: per-peer `DownlinkChain`s in the engine, a presence/roster gossip
  (UDP broadcast heartbeats), local ladder, discovery, bridge-served rider
  page, and S-07..S-09 equivalents for the distributed case. 1–2 weeks.
- Over Wi-Fi infrastructure (no mesh) it still delivers G1 among bikes that
  share an AP; over batman-adv it delivers G1+G2.

## 3. What none of this solves
A rider 1 km behind with nobody between them and the group has no Wi-Fi
path, mesh or not (5 GHz bike-to-bike with onboard antennas is ~100–200 m
line of sight; a 6-bike chain covers ~0.5–1 km of column). That case is
the independent fallback layer the plan already names: GMRS and
Meshtastic (LoRa, kilometres). Mesh fixes "the column stretched", not
"the rider is lost".

## 4. Risk × impact

| option | likelihood of trouble | impact | cost | verdict |
|---|---|---|---|---|
| B. federated bases, mobile base on lead bike | low (probe passed; roaming gap ~1–3 s on AP handoff) | G1 solved | 1 Pi 4B + 1 AP (~$120) + ~50 lines for cross-trunk ducking | **do first** |
| A. batman-adv under the current stack | medium on Pi 4B (IBSS/802.11s on MT7612U is well-trodden); not feasible on 3A+ | G2 only | ~$67/bike + 2–4 days | only if a range ride shows the column outrunning two APs |
| C. mixerless fan-out | medium (new distributed state; INV-6/7 change) | G1+G2, no single point of failure | 1–2 weeks | phase 3, after hardware proves the edge |

## 5. Recommendation
1. Now: stay on the Pi 3A+ bridges and the star topology; measure range
   on the first ride (`make status` shows RSSI/loss per bike).
2. If riders are ever out of the car's reach: option B — a second base on
   the lead bike and a `link` trunk. Software work is the cross-trunk
   `vad` forward; everything else is roster configuration. Record as
   DR-011.
3. Mesh proper (A) only with a Pi 4B fleet and MT7612U radios, after B is
   shown insufficient.
4. Mixerless (C) if and when the project wants no base at all.
