Context: S-07/08/09 level probes used pure sine tones; Opus voice mode (SILK)
is pathological on tones — ~10 dB loss with +/-6 dB wobble — so probes
measured codec artifacts, not mixer behavior.
Decision: All level probes use band-limited noise (common.dsp.noise_band)
measured by band power (band_db). Media participants encode with dtx=False.
Revisit-if: never; tones remain fine for earcons (heard by humans, not
asserted by tests).
