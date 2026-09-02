# ConvoyChat runbook — copy-paste paths for each mode

Three ways to run the same stack. Nothing changes between them except what
is real at the edges and how loud the logs are.

| mode | what is real | what is faked | logs | command |
|---|---|---|---|---|
| **sim** | mixer, ladder, gates, Opus/RTP, agents, pages, TTS | microphones, radio link, headsets, bridges' IPs | INFO | `make up-sim` |
| **hw** (hardware test) | everything; real Pis and headsets | nothing | INFO + 5 s status lines, 1 Hz on each bridge | `make up` + `make bridge` |
| **field** | everything | nothing | WARNING only; systemd restarts | `deploy/*.service` |
| sim + RF | as sim, plus the radio gateway on a simulated channel with an HT-only rider | the radio | INFO | `python3 -m base.main --mode sim --rf` |

The pages are the same in every mode:

    http://<base-ip>:8080/          landing (pick rider / operator)
    http://<base-ip>:8080/rider     the phone page — one per rider, remembers who you are
    http://<base-ip>:8080/ops       chase-car operator dashboard
    http://<base-ip>:8080/snapshot.json   full state as JSON (curl it)

`make up*` prints the URLs (and a QR code for `/rider` if `qrencode` is
installed). Phones must be on the convoy Wi-Fi; nothing needs the internet.

---

## 0. Once, on any machine that runs the base or a bridge

    sudo apt install libopus0 espeak-ng python3-pip     # + qrencode (optional, prints a QR)
    git clone https://github.com/Migirditchsv/ConvoyChat && cd ConvoyChat
    make setup          # pip deps (prints a venv recipe if pip refuses)
    make doctor         # every line must say OK (silero/scipy are optional)
    make test           # ~90 s, all green — this is the spec

---

## 1. Sim only — one laptop, router/phones/bridges all spoofed

What you get: six virtual riders (lead, chase, riders at 50/90/120 km/h)
running the REAL bridge engine on looping wind + real speech, through an
in-process impaired network, into the real mixer, with the real control
plane and both web pages. Phones on the same Wi-Fi can drive it.

    make up-sim                         # Ctrl-C stops; runs until then
    # slow laptop? six Silero VADs in one process can trip SAFE-1's 50 ms budget
    # (they demote to energy VAD and the ops page shows a red `vad:energy` badge):
    python3 -m base.main --mode sim --energy-vad
    # riders talk on their own every 12-40 s; to make them speak ONLY on command:
    python3 -m base.main --mode sim --no-chatter

Then, on this laptop or any phone on the LAN:

1. Open `http://<printed-ip>:8080/` → **I'm riding** → tap a name (say `r2_rider`).
2. **HOLD TO TALK**: the virtual rider's gate is forced open (PTT); watch the
   ops page duck the others. Release: it closes after the hold time.
3. **make me say a phrase**: drops a real Harvard utterance into that
   rider's wind bed — the actual VAD/gate open on it (no PTT).
4. **vol −/+**, **find my helmet** (identify earcon), room buttons, **scan &
   pair** (fake headsets: Cardo / Sena / X7) — every one of these is a
   `node_cmd` round-trip with an ack, exactly as on hardware.
5. Ops page: the **sim speed** slider is the convoy GPS speed; above 8 km/h
   riders lose self-service room moves (the page tells them why).
6. Type in the ops text bar → spoken by espeak into room `main`; `make
   base-live` variant plays the room through your laptop speakers.

Second machine as a laptop-bridge (still no hardware): on machine B,

    make bridge-sim ID=r3_rider BASE=<machine-A-ip> DOWN=6106
    # DOWN = 6100 + 2*index of that rider in the roster (r3 -> 6106)

and stop the in-process copy of that rider on A by running A with a roster
that omits it, or just watch both fight for the name (last hello wins).

Everything the sim writes is in memory; `make demo` is the 40 s scripted
tour that additionally writes `demo_out/*.wav` you can listen to.

---

## 2. Hardware test — real Pis and headsets, verbose

### 2.1 Chase-car machine (base)

    cp deploy/roster.example.yaml roster.yaml     # edit ids/roles; no IPs needed
    make up                                        # --mode hw: 5 s status lines
    # equivalent: python3 -m base.main --mode hw --roster roster.yaml

Firewall: TCP 8080 (pages), TCP 8800 (control WS), UDP 5100 (uplink RTP),
UDP 6100-6199 (per-bridge downlinks, only if the base is also a bridge).
On the Nighthawk: give the base a DHCP reservation so the URL never
changes; enable OFDMA (stock AX firmware ships it off); 5 GHz only for the
bridges (INV-4). Record the unit in docs/hardware.md.

**If something is wrong, look at the top of `/ops` first.** The Issues
strip says what is wrong in a sentence and offers the fix as a button
("Reconnect headset", "Unmute", "Set volume 100%", "Release PTT", "Move to
main", "Reboot bridge") or as a physical hint ("power-cycle", "close up").
The header badge summarises: `all good`, `1 problem, 2 warnings`. Each
rider card has **check bike**: the bridge runs its own self-check and the
card shows every failed probe with its remedy (undervoltage, dongle,
headset, mic/speaker pipe, Wi-Fi, base link, callsign). On the bike itself
the same check is `python3 -m bridge.main --config /boot/convoy.toml --doctor`.

Operator settings (mute, trim, rooms, lead, heartbeat tone, each rider's
helmet volume) are saved to `convoy-state.json` beside the roster on every
change and restored when the base restarts, so a crash or reboot mid-ride
does not undo the ride. Delete the file to start clean.

Diagnostics while it runs:

    make status                                 # snapshot.json, pretty
    curl -s http://localhost:8080/snapshot.json | python3 -m json.tool | grep -A12 '"r2_rider"'
    tools/fieldlog ws://localhost:8800/ ride.jsonl   # every heartbeat/snapshot to JSONL

Status line fields: `r2_rider:UP*/-62dBm/0.4%/plc5` = online, talking (`*`),
signal, downlink loss the rider hears, concealed frames since start.

### 2.2 Each Pi bridge

Image: Raspberry Pi OS Lite 64-bit (Bookworm). Then:

    sudo apt install libopus0 python3-numpy python3-yaml python3-websockets \
                     pipewire pipewire-audio wireplumber bluez bluez-tools iw
    sudo useradd -m convoy && sudo usermod -aG bluetooth,audio convoy
    sudo git clone https://github.com/Migirditchsv/ConvoyChat /opt/convoychat
    sudo chown -R convoy:convoy /opt/convoychat
    cd /opt/convoychat && sudo -u convoy pip install --break-system-packages -e .[vad]
    sudo cp deploy/convoy.example.toml /boot/convoy.toml
    sudo nano /boot/convoy.toml        # [node] id = this bike's roster id, base = chase-car IP
    sudo cp deploy/bt-agent.service /etc/systemd/system/ && sudo systemctl enable --now bt-agent

Bluetooth HFP audio-gateway (INV-1/3): USB dongle (RTL8761BU class), onboard
BT disabled (`dtoverlay=disable-bt` in /boot/config.txt). PipeWire must
offer the AG role — in `~convoy/.config/wireplumber/bluetooth.lua.d/50-convoy.lua`:

    bluez_monitor.properties = {
      ["bluez5.roles"] = "[ hfp_ag ]",
      ["bluez5.codecs"] = "[ sbc msbc ]",
      ["bluez5.enable-msbc"] = true,
    }

Pair the headset — from the rider page (**scan & pair**) once the bridge is
up, or by hand:

    bluetoothctl scan on            # headset in pairing mode (hold its phone button)
    bluetoothctl pair XX:XX:XX:XX:XX:XX && bluetoothctl trust XX:.. && bluetoothctl connect XX:..
    pw-cli ls Node | grep -i bluez  # find the HFP source/sink node names

Put the node names into `[audio] source_cmd/sink_cmd` if pw-record/pw-play
need `--target`. Then run it verbosely (this is the hardware-test mode):

    make bridge                                    # = python3 -m bridge.main --config /boot/convoy.toml --verbose
    # bench, without the toml:
    python3 -m bridge.main --id r2_rider --base 192.168.1.2 --down-port 6104 --verbose

The 1 Hz line: `ctl=up vad=silero open=0 ptt=0 tx=812 rx=1490 loss=0.3% rssi=-61 rate=173.3 vol=100% evictions=0`.
`ctl=down` → base unreachable; `rx` not growing → the mixer can't reach
this bridge's down_port (check `down_port` matches the roster and no
firewall); `vad=energy` → Silero demoted (SAFE-1; CPU or a crash — check
journal); `loss` climbing → move closer / check 5 GHz; `evictions` > 0 →
the self-eviction policy cycled Wi-Fi (INV-9).

What the rider sees on the phone page: bridge ok/lost, headset connected
or not (refreshed every 10 s from bluetoothctl), link word (good/weak/bad),
and every command's ack as a toast. PTT is a dead-man switch: the page
re-arms it every 2 s while held and the bridge releases it by itself 6 s
after the last arm (with the ptt-off earcon), so a dropped control link
never leaves a gate forced open. `[actions] enabled = true` in the toml
makes reboot/reconnect/pair real; until then every such command acks with
`DRY-RUN: <shell>` so you can read what it would do.

Headset qualification (M2, per model): mic opens outside a call? codec
(`btmon` → CVSD vs mSBC)? survives 10 call cycles + 20 power cycles? Record
in docs/headsets/<model>.md.

---

## 3. Field deployment — critical, quiet, self-restarting

Base (chase-car Linux tablet):

    sudo cp deploy/convoy-base.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now convoy-base
    journalctl -fu convoy-base          # WARNING-level only: node leaves/joins, TTS failures

Bridge (each Pi):

    sudo cp deploy/convoy-bridge.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now convoy-bridge
    # read-only rootfs (INV-11): sudo raspi-config -> Performance -> Overlay FS -> enable
    # identity stays writable on /boot; pairing writes headset_mac there BEFORE you flip RO

Field-mode behaviour: both units are `Type=notify` with a systemd watchdog
(20 s base, 15 s bridge) fed only while the audio tick advances, so a wedged
process is restarted, not just a crashed one; `Restart=always` on both;
a headset that drops is reconnected by the bridge after 15 s (30 s
back-off, six tries, "connected" earcon on recovery); the bridge's audio
commands are supervised (a dead pw-record is restarted with link-lost /
connected earcons so the rider knows); the control link reconnects every
2 s forever; the eviction policy cycles Wi-Fi after 3 s below 12 Mb/s or
above 25 % loss, at most once per 30 s. Everything the operator needs is
on `/ops`; everything the rider needs is on `/rider` (add it to the home
screen — full-screen, keeps the screen awake while open).

Ride start (laminated version in docs/ride-checklist.md):

1. Car: router on, base tablet on → `http://<base>:8080/ops` shows the roster, all cards red.
2. Bikes on. Each card goes green within ~45 s; headset shows *connected*.
3. Radio check per room; lead and chase confirm talk-over ducks everyone.
4. Riders: open `/rider`, pick your name, thumb the TALK button once.
5. Fallback layer (GMRS channel, Meshtastic) confirmed before rolling.

If the base dies mid-ride: bridges keep their last state, earcons announce
link lost; when the base is back every bridge re-hellos and the mixer learns
their addresses again — no action on the bikes.

---

## 4. Ham / GMRS radio fallback (DR-011)

Two places, one link discipline (`common/radio.py`): the base puts the room
on the air through a wired HT; a bike moves its helmet to its own HT when
the base is gone. The software never keys without a callsign, never keys
over a busy channel, never exceeds the time-out, and sends a Morse ID at
the legal interval and at the end of a communication. Licensing is yours.

**Try it with no radio at all:**

    python3 -m base.main --mode sim --rf        # gateway + one HT-only virtual rider on a sim channel
    # /ops shows a `radio` card (what came in over RF) and a header line with keyed / tx seconds / IDs

**Base gateway (chase car):** an HT on a USB sound card (K1/K2 plug cable:
speaker out -> line in, mic in <- line out) and PTT on a USB-serial RTS
line through a transistor. In `roster.yaml`:

    net:
      radio:
        callsign: K1ABC            # REQUIRED
        service: ham               # or gmrs
        ptt: serial:/dev/ttyUSB0:rts
        rx_cmd: "arecord -q -D plughw:1,0 -f S16_LE -r 16000 -c 1 -t raw -"
        tx_cmd: "aplay  -q -D plughw:1,0 -f S16_LE -r 16000 -c 1 -t raw -"

Music never reaches the rig (mixer exclude mask). RF speech enters through
the same gate as a helmet, so it ducks music and shows as `radio` on /ops.
Set the HT to the simplex frequency, squelch normal, VOX OFF (we key PTT).

**Bike failover:** HT wired to the Pi through an I2S codec HAT or USB sound
card (the 3A+ has one USB port: the BT dongle — use the I2S HAT) and PTT on
a GPIO through an optocoupler. `/boot/convoy.toml`:

    [radio]
    mode = "auto"                  # RF only while the base is unreachable
    callsign = "K1ABC"
    ptt = "gpio:17"
    rx_cmd = "arecord -q -D plughw:1,0 -f S16_LE -r 16000 -c 1 -t raw -"
    tx_cmd = "aplay  -q -D plughw:1,0 -f S16_LE -r 16000 -c 1 -t raw -"

`make bridge` then prints `rf=idle` until the base link drops, `rf=ON` /
`rf=ON/KEYED` after. The rider hears the ptt-off earcon after each ID and
sees "RADIO on" on the phone page. `mode = "always"` makes a licensed
rider a permanent relay of the room.

If a bridge's VAD has failed fully open (red `vad:open` on /ops) the RF
path stays silent unless the rider holds TALK; `tot_s = 60` in the toml is
a sensible bike-side time-out (the base gateway keeps 180).

Bench check before a ride: `python3 -m bridge.main --sim --config
/boot/convoy.toml -v` with the rig connected keys the real PTT from the
fake mouth every 12–40 s; watch the rig's TX light and hear the CW ID.

---

## 5. Hotspot + WireGuard fallback (DR-012)

When the convoy Wi-Fi is gone and a phone has coverage, a bridge joins any
listed rider hotspot it can see, tunnels to a hub, and re-points itself at
the base's tunnel address. Riders without a hotspot plan ride on a friend's.

    # once, per device: keys + config from deploy/wg/ (hub = VPS or the tablet)
    wg genkey | tee r2_rider.key | wg pubkey > r2_rider.pub
    sudo cp deploy/wg/bike.conf.example /etc/wireguard/convoy.conf && sudo nano /etc/wireguard/convoy.conf
    # NetworkManager profiles for the star and every hotspot (names = SSIDs):
    sudo nmcli con add type wifi ifname wlan0 con-name "convoy" ssid "convoy" wifi-sec.key-mgmt wpa-psk wifi-sec.psk '<psk>'
    sudo nmcli con add type wifi ifname wlan0 con-name "Sam iPhone" ssid "Sam iPhone" wifi-sec.key-mgmt wpa-psk wifi-sec.psk '<psk>' connection.autoconnect no

`/boot/convoy.toml`:

    [failover]
    enabled = true
    hotspots = ["Sam iPhone", "Kate Pixel"]
    tunnel_base = "10.66.0.1"

Everything dry-runs (logs the nmcli/wg commands it would run) until
`[actions] enabled = true`. `make bridge` prints `link=star|hotspot|tunnel`.
Timing defaults: leave the star after 8 s of base silence, return after
12 s of a strong convoy SSID, give each hotspot 25 s to yield a tunnel.
The base needs nothing new except its own tunnel up (`deploy/wg/base.conf`).

---

## Ports & protocol (for firewalls and curiosity)

    UDP 5100        bridges -> mixer, Opus/RTP, SSRC = crc32(node id)
    UDP 6100+2n     mixer -> bridge n (roster order), one continuous stream
    TCP 8800        control WebSocket (JSON envelopes; snapshots pushed)
    TCP 8080        pages + /snapshot.json + /health

Security model: the convoy Wi-Fi is the trust boundary. Anyone on it can
open the pages and control any rider (deliberate: the chase passenger must
be able to). Set `net.node_token` in the roster and `[net] node_token` in
each toml if you want bridge identities to be unspoofable.
