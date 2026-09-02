Context: A convoy that outruns the car, or a car that dies, needs a voice
path with no infrastructure. Licensed riders carry ham HTs (or GMRS units);
the plan names them as the independent fallback layer but nothing in the
stack used them.
Decision: A half-duplex link discipline (common/radio.py) shared by two
integrations. (1) Base gateway (base/media/radio.py): a BridgeEngine whose
mouth is the rig's receiver and ear is the rig's mic, so RF speech enters
through the real gate/Opus/mixer as participant `radio` (ducks music, shows
on /ops) and the room's N-1 mix goes on the air keyed only while it carries
speech. Music is excluded by a new MixerAPI.set_exclude (N-k) so nothing
but voice is transmitted. (2) Bike failover (bridge/radio.py): the helmet's
transport moves to the wired HT while the base is unreachable (`auto`), or
always (a licensed relay). The discipline enforces: no callsign -> never
keys; carrier-busy lockout with hold; hang time; time-out timer with
cooldown; Morse station ID at the service interval (ham 10 min, GMRS
15 min), forced mid-burst if a key-down reaches the interval, and at the
end of a communication, delayed until the channel is clear. All of it is
frame-exact and unit-tested (S-18); the end-to-end path runs on a
simulated RF channel (sim/rf.py, S-19) and in `make up-sim --rf`.
SAFE-1 interaction: the classifier's final rung is OPEN, which is right over
Wi-Fi (the mixer absorbs wind) and wrong on a shared channel (a stuck
carrier; measured: the energy fallback keys on a -32 dBFS wind bed for its
first ~80 s while its floor tracker converges). The RF path therefore
transmits only with a working classifier (silero/energy) or the rider's
PTT; a bridge that has failed fully open holds RF silent and the time-out
timer remains the backstop. S-19 pins this.
Alternatives: digital voice modes over the HT (rejected: bandwidth,
hardware, and encryption rules); relaying data (rejected for ham bands,
see feasibility-fallbacks.md).
Revisit-if: a rig needs COS/squelch on a GPIO line instead of energy-based
carrier detect (add a `busy_input` to RadioLink); a jurisdiction's ID rules
differ (ID_INTERVAL_S table).
