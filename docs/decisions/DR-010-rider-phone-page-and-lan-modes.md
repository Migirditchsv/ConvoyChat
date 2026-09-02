Context: Session 2026-09-02 review found the stack could not run off one
machine: mixer and bridge downlinks bound to loopback, rosters needed
bridge IPs, the agent never sent `vad` (ducking only worked because the demo
wired on_vad in-process), dashboards polled at 2 s, and one malformed UI
message closed a WebSocket. Separately, the Pi bridges have no physical UI,
and the measured 120 km/h gate gap (DR-008) has no rider-side remedy.
Decision:
1. Phones are the bridge's UI, served by the BASE (`/rider`), control-only
   (INV-10 holds): the page routes node_cmds through the existing base->node
   plane, so sim and hardware use identical code and a rider fixes their own
   node without the chase passenger. New node commands: `ptt`, `bt_scan`,
   `bt_pair`, `bt_status`, `say` (sim only). `ptt` forces the gate open
   while held — the rider's thumb closes the -6 dB gap rather than lowering
   thresholds into gust territory (DR-008 stands).
2. Symmetric RTP: the mixer replies to the host a participant's uplink
   arrives from; the roster carries no bridge IPs (DHCP is fine). Node IP is
   also learned from the control WS. Optional `net.node_token` gates node
   identity on the LAN; UI clients stay unauthenticated (LAN trust model,
   documented).
3. Snapshots are pushed (debounced on change + 1 Hz); pages keep a 10 s
   keepalive only. Every WS message path is guarded; bad input is logged
   and dropped, never fatal to the connection.
4. Three run modes in one entry point (`base.main --mode sim|hw|field`):
   sim = virtual riders in-process (the real engines/agents on looping
   mouths), hw = verbose status lines, field = warnings only + systemd units
   in deploy/. The bridge gets its own entry point (`bridge.main`) reading
   /boot/convoy.toml, with subprocess audio adapters (pw-record/pw-play),
   iw link stats feeding the heartbeat and the (now wired) eviction policy.
5. Reorder buffers use 16-bit serial arithmetic and the mixer resyncs after
   a gap longer than its buffer (previously: PLC forever for the rest of the
   talk spurt after a >1.4 s blackout).
Alternatives: bridge-local page on each Pi (rejected for v1: a phone must
find the Pi's IP, no base-side state, duplicate code; kept as a revisit —
the same rider.html can be served by the bridge later with a different WS
target). mDNS/`convoy.local` (deferred: Android mDNS support is uneven;
the base prints URLs + a QR instead).
Revisit-if: riders need to pair a headset with the base down (then serve
the rider page from the bridge too); UI spoofing becomes a real problem on
a shared network (then token the UI as well).
