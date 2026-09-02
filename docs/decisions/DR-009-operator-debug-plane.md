Context: Operator QoL requirements added to v1 (plan S-3/O-1 scope grows):
heartbeat-alive tone, last-contact ages, per-rider mute/trim, network health,
and remote node debug (reboot / reconnect BT / reconnect Wi-Fi / volume /
identify) so the chase passenger fixes nodes while riders keep eyes on the
road.
Decision: Three additive protocol types (node_cmd/ack/audio_ctl). Operator
mute/trim COMPOSE with the ladder (effective = muted?0 : ladder*trim/100) so
a duck never clobbers an operator setting and vice versa. Node commands route
base->node over the node's own control WS with per-command acks surfaced in
the UI; rider-side state changes stay audible via earcons (SAFE-2); volume
is clamped 10-200% and earcons mix AFTER volume so a muted-down rider still
hears alerts. DeviceActions documents the real shell contract for M2 and
dry-runs until enabled on hardware.
Revisit-if: command latency over a lossy link makes acks misleading — then
add per-command timeouts surfaced as failed acks (currently the UI shows the
last ack only).
