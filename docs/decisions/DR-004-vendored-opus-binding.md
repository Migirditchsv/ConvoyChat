Context: pip opuslib (2018, unmaintained) fails to build on py3.11 in this
env; we need DTX/FEC/PLC ctls and a dependency that will exist in 5 years.
Decision: common/opusbind.py — minimal ctypes binding to libopus.so.0
(encoder/decoder, bitrate/DTX/FEC/loss/signal ctls, PLC + FEC decode).
~120 lines, ours, tested by S-02.
Revisit-if: a maintained upstream binding covers the same surface.
