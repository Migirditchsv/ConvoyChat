Context: Plan T-0 specified tc netem in per-node network namespaces; CI
containers lack CAP_NET_ADMIN.
Decision: Packet impairment for Tier-0 is an in-process UDP proxy
(sim/impair.py) implementing the same named profiles. tc netem remains the
tool for Tier-1/2 on real hardware; profile numbers are shared.
Revisit-if: never (both exist; profiles are the contract).
