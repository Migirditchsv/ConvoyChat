Context: Plan S-1 named Janus AudioBridge primary with pymixer as sanctioned
fallback behind MixerAPI. M0/M1 run where Janus buys nothing (no WebRTC
clients exist in this system) and costs a C build.
Decision: pymixer is the M0/M1 backend. Janus is evaluated at M2 on the
tablet ONLY if pymixer's jitter/PLC proves inadequate in H-03/F-01.
S-07 conformance pins MixerAPI so a swap stays invisible to orc/UI/bridges.
Revisit-if: audible artifacts attributable to jitter handling at H-03.
