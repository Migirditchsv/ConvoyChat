# Feasibility: cellular fallback, alternative SBCs, exotic transceivers (2026-09-02)

Companion to `feasibility-mesh.md`. Three questions, three different answers.

## 1. Car drops out, cell coverage is good: phone as backhaul?

The useful resource is the rider's phone's **cellular data**, not Reticulum.

**Don't use Bluetooth for the Pi–phone hop.** The USB dongle is busy holding
the headset's SCO link (INV-1/INV-3); a PAN/tether link to the phone would
share the same 2.4 GHz radio and piconet slots with SCO, exactly the kind of
coexistence risk the plan avoids. Sideband's Bluetooth support is RNode-
over-BLE, not a Pi link. Use the phone's **Wi-Fi hotspot** instead: when the
convoy SSID vanishes, the Pi's idle Wi-Fi joins the rider's hotspot
(wpa_supplicant network priorities; prefer a 5 GHz hotspot). No new
hardware. INV-10 note for Sam: the phone would *route* encrypted audio
packets, never decode or capture audio; the spirit (no phone audio path)
holds, but it is an interpretation to sign off.

**Then a tunnel, not a new stack.** WireGuard from each bike to a
rendezvous the base can reach: the chase tablet if it has its own
cellular, or a $5 VPS that runs `base.main` as a cloud base. The existing
RTP/WS stack is unchanged; the tunnel's inner addresses are stable so
symmetric RTP does not even notice. LTE adds ~50–80 ms each way, so
mouth-to-ear lands around 300–400 ms: acceptable as a fallback. Data:
~7 MB/h per bike (16 kbit/s down continuously, gated up).

**Reticulum is the wrong layer for this.** LXST (its voice transport) is
alpha, Codec2/Opus, with basic mixing but none of the ladder/rooms/N-1
semantics the mixer provides; adopting it means replacing the audio plane
to solve a routing problem WireGuard solves in an afternoon.

**Where Reticulum does fit:** the messaging/position layer, replacing
Meshtastic. Sideband on the phones, RNode LoRa on each Pi (a Heltec/LilyGO
LoRa board flashed with rnodeconf, ~$25–40) on the 3A+'s full PL011 UART,
which INV-3 frees by disabling onboard BT. Text, positions, "stopped",
"need fuel" over kilometres with no infrastructure, delay-tolerant; the
same node bridges over the phone's cellular when available.

## 2. A non-Pi SBC that makes mesh easier?

Mesh capability lives in the Wi-Fi **driver** (mac80211 SoftMAC with mesh
point mode), not in the SBC.

- **BeagleBone Black Wireless:** WL1835 advertises 802.11s, but it is
  2.4 GHz only (INV-4 kills it), the single-core 1 GHz A8 with 512 MB is
  marginal for Silero + Opus (the bridge uses ~1/3 of a modern x86 core),
  and TI's forums show the mesh mode is fiddly. No.
- **OpenWrt router SoC boards** (MediaTek Filogic: Banana Pi R3 Mini,
  GL.iNet): mesh is native and excellent; BT HFP-AG, PipeWire and Python
  DSP on OpenWrt are not. Wrong tool for the bridge role.
- **SBCs with an M.2 E-key** (Pi 5 + $12 PCIe HAT; Radxa Rock 5; Orange
  Pi 5) + an ath10k QCA6174 card: mesh support is listed "partial" with
  reported bring-up issues; MT7921 mesh support is uncertain; the non-Pi
  boards carry the documentation risk already weighted. Pi 5 is the one
  Pi route to u.FL antennas on a mesh radio, at ~2.7 W idle plus the card
  and with heat to manage. Not better than Pi 4B + Alfa AWUS036ACM.

Verdict: nothing makes mesh materially easier than Pi 4B + MT7612U. The
"easier" architecture is role separation: Pi for BT/audio, a dedicated
OpenWrt mesh router only if the fleet outgrows six bikes.

## 3. Reticulum + GPIO "exotic" transceivers

Plumbing is trivial: any byte pipe (serial, KISS, stdio) is a Reticulum
interface, and the PL011 UART is free. Each physical layer decides itself.

| transceiver | verdict | why |
|---|---|---|
| IR blaster (UART-over-IR) | novelty | 9.6–115 kbit/s LOS at metres; sunlight saturates IR receivers; useless at riding distance or in desert daylight |
| near-ultrasonic "shifted" voice | no | consumer transducers carry ~64–160 bit/s at ~1 m (ggwave-class), 5–10× below Codec2's 700 bit/s floor; HFP headsets cannot emit above ~4/8 kHz, so it needs its own transducers; at a stop, phones already have Sideband over Wi-Fi/BLE, or riders walk ten metres |
| wired ham HT, analog voice | yes, for licensed riders | GPIO PTT via optocoupler + I2S codec HAT (the 3A+ has no spare USB) puts the room audio on a simplex frequency or brings a distant licensed rider back in; Part 97: licensed operators, ID every 10 min, unencrypted analog voice is fine |
| wired ham HT, data (Direwolf KISS → Reticulum) | not on ham bands | Reticulum encrypts everything; 97.113's "obscure the meaning" rule makes it a grey area at best, and 1200 baud AFSK cannot carry voice anyway. Encrypted Reticulum belongs on 915 MHz ISM LoRa (RNode) |

## Layered recommendation (cheapest first)
1. Wi-Fi star (done) → 2. phone hotspot + WireGuard cellular fallback
(software only) → 3. RNode LoRa + Reticulum for text/position with no
infrastructure (~$30/bike, replaces Meshtastic) → 4. HT analog voice relay
for licensed riders (I2S HAT + PTT, optional) → 5. IR / ultrasonic: skip.
