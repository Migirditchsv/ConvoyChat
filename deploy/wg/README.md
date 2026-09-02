# WireGuard fallback (DR-012)

Topology: hub-and-spoke. Every bike and the base peer with ONE hub that has
a public address. The hub is either the chase tablet when it has its own
cellular with a reachable address (rare behind carrier NAT) or a $5 VPS.
Bikes reach the base at its tunnel address (10.66.0.1) regardless of which
rider's hotspot carried them there.

    keys:   wg genkey | tee X.key | wg pubkey > X.pub     (one pair per device)
    hub:    /etc/wireguard/convoy.conf  <- hub.conf.example  (wg-quick up convoy; enable on boot)
    base:   /etc/wireguard/convoy.conf  <- base.conf.example (always up; the base is 10.66.0.1)
    bike:   /etc/wireguard/convoy.conf  <- bike.conf.example (bridge/main brings it up on failover)

Firewall on the hub: UDP 51820 in. Nothing else. The tunnel carries the
same RTP/WS the star does; no application change.

If the hub IS the base (tablet with a public address), drop the hub file
and put the base's public endpoint in bike.conf; AllowedIPs stays 10.66.0.0/24.
