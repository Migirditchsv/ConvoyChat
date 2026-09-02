Context: The person in the car is a router-node manager, not a computer
person. Meters (rssi, plc, vad mode) do not tell them what to press. A
base restart lost every mute/trim/room; a hung process stayed hung; a
dropped headset waited for a human; a corrupt packet could stop the mixer
tick (S-21 reproduced it: a 10 ms Opus frame yields 160 samples and the
mix arithmetic raised; a code-3 packet with zero frames raised OpusError).
Decision:
1. Issues, not meters: base/orc/doctor.py turns every snapshot into plain
   sentences with one-tap fixes (reconnect headset, unmute, reset trim,
   set volume, release PTT, move to main, reboot) or a physical hint when
   only a human can act (power-cycle, close up). Sorted problems first;
   `health` summary in the header. Pure and tested (S-22).
2. Bike self-check: node_cmd `doctor` (and `bridge.main --doctor`) runs
   read-only probes with a remedy per failure: undervoltage (vcgencmd
   flags), Bluetooth controller present, headset connected, mic/speaker
   commands installed and running, Wi-Fi associated, base link, callsign.
   Rendered in the rider's card on /ops.
3. Settings survive: mute/trim/rooms/lead/hb-tone/volumes persist to
   convoy-state.json beside the roster on every change and are restored
   on start; a rider's last helmet volume is re-pushed when their bridge
   joins. Unknown ids in the file are ignored (the roster is the truth).
4. Self-repair without a human: HeadsetSupervisor reconnects a headset
   after 15 s down, backing off 30 s, at most 6 tries per outage, earcon on
   recovery; systemd watchdog via sd_notify (Type=notify, WatchdogSec) fed
   only while the tick advances, so a wedged loop is restarted; every
   tick (mixer, engine, downlink, sources) survives corrupt packets,
   wrong ptime, throwing sources/sinks and counts the fault (S-21).
Alternatives: a full alerting stack (rejected: no internet, one operator);
auto-reboot on degraded VAD (rejected: 45 s of silence is the operator's
call — it is offered as a button with the cost stated).
Revisit-if: operators still ask "what do I press" — then the phone page
gets the same issues list filtered to the rider.
