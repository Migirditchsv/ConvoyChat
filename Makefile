PY ?= python3

setup:            ## install runtime + dev deps
	$(PY) -m pip install --break-system-packages -e .[dev] 2>/dev/null || $(PY) -m pip install -e .[dev]
	-$(PY) -m pip install --break-system-packages -e .[vad] 2>/dev/null || $(PY) -m pip install -e .[vad]

fixtures:         ## generate labeled speech+wind fixtures (needs espeak-ng)
	$(PY) -m sim.fixtures

earcons:          ## generate earcon wavs into common/earcons/
	$(PY) -m common.earcons

test: fixtures    ## full Tier-0 suite
	$(PY) -m pytest

test-fast:        ## unit tests only (no realtime convoy tests)
	$(PY) -m pytest -m "not realtime"

convoy:           ## run a 6-rider simulated convoy interactively
	$(PY) -m sim.convoy --riders 6 --profile parkinglot

base:             ## run the base station (mixer + control WS + dashboard) on a demo roster
	$(PY) -m base.main

.PHONY: setup fixtures earcons test test-fast convoy base
