Context: Fixtures moved from espeak synthesis to real Harvard-sentence
utterances (user-supplied public sample audio, sim/ext/speech/). Two findings
followed. (1) Spectral shape decides band-limit loss: one deep voice lost
13 dB through the 300-3400 HFP band, silently turning the -6 dB tier into
-19 dB — so fixture levels are now calibrated POST-band-limit (where a real
headset's AGC acts). (2) Measured separability at 120 km/h: wind showed ZERO
consecutive frames above prob 0.35 in 5 minutes while speech shows runs, so
the high-speed gate profile opens at 0.35 with 2-frame confirmation and holds
at 0.10 (PROFILES table in bridge/audio/gate.py); pre-roll extended to 900 ms.
Decision: Onset guarantee is a <=90 km/h contract (5/5, 0% missed there);
120 km/h pins a measured floor (>=2/5 onsets, <=25% missed) — one utterance
peaks at silero 0.52 for a single frame at -6 dB, below any gate that also
rejects gusts. Per the plan's honest-expectations verdict, the fix for that
gap is mechanical, not more gate tuning.
Revisit-if: F-03 real captures re-baseline the fixtures (assumption register);
re-measure separability and re-derive PROFILES then.
