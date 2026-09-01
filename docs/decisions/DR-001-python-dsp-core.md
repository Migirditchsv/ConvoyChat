Context: Plan B-2 named GStreamer as the primary pipeline. Tier-0 must run in
CI containers (and this cloud env) where GStreamer/PipeWire audio is heavy and
flaky, and the DSP core must be unit-testable frame-by-frame.
Decision: The audio chain is a pure-Python/numpy core (frames of 960 samples
@16 kHz) with pluggable IO adapters (arrays/UDP now; pw-cat/ALSA subprocess or
gst appsrc/appsink shell on-device at M2). Interfaces per plan hold.
Alternatives: gst end-to-end (rejected for CI + testability); scipy-mandatory
(kept optional; per-sample fallback exists for the Pi decision later).
Revisit-if: Pi 3A+ CPU shows the Python chain >30% of a core at M2 bench.
