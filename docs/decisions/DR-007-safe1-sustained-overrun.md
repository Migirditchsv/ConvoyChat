Context: SAFE-1's supervisor demoted the classifier on a single over-budget
frame; under CI load one 55 ms Silero call demoted mid-run to energy mode,
flaking S-04 — and on a bridge it would needlessly discard the better
classifier on any scheduler hiccup.
Decision: Exceptions demote immediately; time-budget overruns demote only
when sustained (3 consecutive frames). Overrunning frames still return their
probability. Final fallback remains OPEN (fail-open mic, SAFE-1).
Covered by tests/test_safe1_supervisor.py.
Revisit-if: bench (H-03) shows sustained per-frame times near 50 ms on the
Pi 3A+ — then the budget, not the policy, is the knob.
