Context: Plan C-0 sketched per-node uplink ports (5100+2n).
Decision: All bridges send uplink RTP to ONE mixer port (5100); participants
are identified by RTP SSRC = crc32(node_id) (roster-derived, collision-checked
at load). Downlink remains per-node ports (6100+2n). Fewer sockets, standard
RTP practice, simpler NAT-less LAN config.
Revisit-if: SSRC collision or a middlebox that can't cope (none on a flat LAN).
