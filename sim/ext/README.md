# External sample audio (drop-in)

`speech/` — real recorded utterances used by the fixture builder in place of
espeak synthesis. Currently: five Harvard-sentence utterances segmented from
user-supplied public sample audio (16 kHz mono, -20 dBFS).

`wind/` — put real wind recordings here (e.g. the RWTH IKS Wind Noise
Database, MIT license: https://www.iks.rwth-aachen.de/en/research/tools-downloads/databases/wind-noise-database/ )
and the fixture builder will use them instead of synthetic wind, level-set by
the speed table. CI stays hermetic without them.

After adding files: `python3 -m sim.fixtures` rebuilds (rev-gated) — delete
`sim/data/manifest.json` or bump FIXTURE_REV to force.
