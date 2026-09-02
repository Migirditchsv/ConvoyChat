# ConvoyChat — instructions for Claude Code

Read `catchup.md` first — it is the complete state handover (architecture,
invariants INV-1..11, safety rules SAFE-1..3, decision records, M2 worklist,
hardware traps). The two governing design artifacts are owned by Sam; ask
him for links if you need the deep rationale.

Ground rules:
- **Tests are the spec.** `make test` (57 green) before and after your work;
  behavior changes land with their test in the same commit.
- Never weaken an INV-* or SAFE-* without Sam's explicit sign-off; record
  any deviation as `docs/decisions/DR-NNN-*.md` (format in DR-001).
- `make doctor` diagnoses environment problems with remedies.
- Entry points: `make up-sim` (whole network on one laptop), `make up` /
  `make bridge` (hardware), `make demo` (40 s tour). Modes: `docs/runbook.md`.
- Don't commit `sim/data/` or `demo_out/` (generated); `sim/ext/**` wavs ARE
  committed (real sample audio).
- Gate thresholds (bridge/audio/gate.py PROFILES) are measurement-derived —
  re-tune only from new separability data, method in DR-008.
