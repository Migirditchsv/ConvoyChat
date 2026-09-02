Context: When the convoy Wi-Fi is gone but cellular coverage is good,
riders' phones are a backhaul. Not every rider has a hotspot plan.
Bluetooth to the phone is ruled out (the dongle is holding SCO, INV-3).
Decision: bridge/net/failover.py — a 1 s state machine star -> hotspot ->
tunnel -> star with injected actions. On `fail_s` seconds of base silence
(control down or downlink RTP stopped) the bridge joins the strongest
listed rider hotspot it can see (any rider's; riders without a plan ride
on another's), brings up a WireGuard tunnel to a hub the internet can reach
(deploy/wg/), and re-points the engine's RTP and the agent's control WS at
the base's tunnel address at runtime. When the convoy SSID is back above
`min_rssi` for `restore_s`, it tears down and returns. Hotspots that give
no internet are rotated once each, then the bridge backs off on the star.
Shells (nmcli, wg-quick, wg show, ping) live in WifiActions and dry-run
until [actions] enabled, like DeviceActions. The audio and control planes
are unchanged: the tunnel carries the same RTP/WS; symmetric RTP on the
base learns the tunnel address. Tested to the second in S-20 with recorded
actions, parsers on captured output, and a live engine/agent re-target
between two bases.
INV-10 interpretation for Sam: the phone routes encrypted packets and
never decodes audio; the page never touches audio.
Alternatives: Reticulum/LXST (rejected for voice: alpha, no ladder/rooms,
replaces the audio plane); Bluetooth PAN (rejected: SCO coexistence).
Revisit-if: the base has no reachable endpoint and no VPS is acceptable —
then a cloud base trunked to the car base (feasibility-mesh.md §2B).
