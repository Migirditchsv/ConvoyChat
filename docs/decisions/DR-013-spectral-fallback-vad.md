Context: SAFE-1 demotes silero -> energy -> OPEN. Measured 2026-09-02: the
energy rung's floor tracker rose 0.02 dB/frame, so a bridge booting into a
-32 dBFS wind bed kept its gate open for ~80 s (1400 frames to converge).
Harmless over Wi-Fi (the mixer absorbs it), a stuck carrier on the radio
fallback (DR-011), and wind chatter for everyone while a bridge is demoted.
A separability probe on the labeled fixtures showed nothing beats Silero
as the primary (AUC 0.99/0.98/0.95 at 50/90/120 km/h vs <0.7 for
harmonicity or level), so the primary is untouched and PROFILES are
untouched (DR-008); the fallback is what changes.
Decision: (1) FloorTracker: fast-attack warm-up (1 dB/frame for the first
33 frames), then 0.05 dB/frame rise, 0.5 dB/frame fall; shared by the
energy rung. (2) A new second rung, SpectralVad: logistic model on
[snr-to-floor, spectral flatness 300-3400 Hz, tilt, centroid], ~0.3 ms per
frame in numpy, fitted by tools/fit_vad.py on 50/90 km/h labeled speech
plus every wind frame at all speeds, wind weighted 3x — a fallback must
prefer missing speech over keying on wind. Chain: silero -> spectral ->
energy -> OPEN. The gate uses firmer thresholds for the spectral mode
(open >= 0.55, keep +0.10). Results on the rev-5 fixtures: spectral missed
0/0 % at 50/90, wind-open 0.0/0.0/5.7 %; energy wind-open now 0.6/1.0/3.8 %
(was ~100 % for the first 80 s). S-24 pins: <= 10 % missed at 50/90, <= 10 %
wind-open at every speed, < 10 % open in the first 80 s, floor settles in
40 frames, chain order.
Honesty: the flatness feature separates *synthetic* red-tilted wind
strongly; real helmet wind is also LF-dominant but the numbers above are
fixture numbers. Refit from real captures (DR-008 revisit) before trusting
the 120 km/h figure.
Revisit-if: real captures move wind-open above 10 % (retune neg_weight and
the spectral gate thresholds, not PROFILES); Silero's own overrun rate on
the 3A+ makes this rung the effective primary (then measure it as such).
