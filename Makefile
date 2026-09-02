PY ?= python3

help:             ## show this help
	@grep -E '^[a-z-]+:.*##' Makefile | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

doctor:           ## preflight: verify every dependency, with remedies
	@echo "convoy doctor -----------------------------------------------"
	@$(PY) -c "import sys; v=sys.version_info; print(f'  python {v.major}.{v.minor}          ', 'OK' if v>=(3,11) else 'FAIL: need >=3.11')"
	@$(PY) -c "import ctypes.util as u; l=u.find_library('opus'); print('  libopus            ', 'OK ('+l+')' if l else 'FAIL: sudo apt install libopus0')"
	@command -v espeak-ng >/dev/null && echo "  espeak-ng           OK" || echo "  espeak-ng           MISSING (only needed if sim/ext/speech/ is empty): sudo apt install espeak-ng"
	@$(PY) -c "import numpy" 2>/dev/null && echo "  numpy               OK" || echo "  numpy               FAIL: run 'make setup'"
	@$(PY) -c "import yaml" 2>/dev/null && echo "  pyyaml              OK" || echo "  pyyaml              FAIL: run 'make setup'"
	@$(PY) -c "import websockets" 2>/dev/null && echo "  websockets          OK" || echo "  websockets          FAIL: run 'make setup'"
	@$(PY) -c "import pysilero_vad" 2>/dev/null && echo "  silero VAD          OK" || echo "  silero VAD          missing (system still works on energy-VAD fallback): pip install pysilero-vad"
	@$(PY) -c "import scipy" 2>/dev/null && echo "  scipy               OK" || echo "  scipy               missing (slower pure-python filters): pip install scipy"
	@echo "-------------------------------------------------------------"
	@echo "  all OK?  ->  make demo"

setup:            ## install python deps (tries PEP-668 flag first, then plain pip)
	@echo ">> installing python dependencies..."
	$(PY) -m pip install --break-system-packages -e .[dev,vad] 2>/dev/null || \
	$(PY) -m pip install -e .[dev,vad] || \
	( echo ""; echo ">> pip refused. Easiest fix — a venv:"; \
	  echo "     $(PY) -m venv .venv && . .venv/bin/activate && pip install -e .[dev,vad]"; \
	  echo "   then run make with PY=.venv/bin/python3"; exit 1 )
	@$(MAKE) --no-print-directory doctor

_deps:
	@$(PY) -c "import numpy, yaml, websockets" 2>/dev/null || \
	( echo ""; echo ">> python deps missing — run 'make setup' first (details: make doctor)"; echo ""; exit 1 )

fixtures: _deps   ## generate labeled speech+wind test audio (first run ~30-60 s)
	@$(PY) -m sim.fixtures

earcons: _deps    ## generate earcon wavs into common/earcons/
	@$(PY) -m common.earcons

test: fixtures    ## full Tier-0 suite (~90 s, 32 tests; gate metrics dominate)
	@$(PY) -m pytest

test-fast: _deps  ## unit tests only, skips realtime convoy tests (~65 s)
	@$(PY) -m pytest -m "not realtime"

demo: fixtures    ## START HERE: 40 s scripted ride -> dashboard + demo_out/*.wav
	@echo ">> demo starting. Open http://localhost:8080 NOW to watch it live."
	@( command -v xdg-open >/dev/null && sleep 2 && xdg-open http://localhost:8080 >/dev/null 2>&1 & ) || true
	@$(PY) -m sim.demo
	@echo ">> done. Hear it:  make listen   (or open demo_out/ears/*.wav yourself)"

listen:           ## play the headline demo recording (tries paplay/aplay/ffplay/mpv)
	@f=demo_out/ears/r3_rider.wav; test -f $$f || { echo ">> $$f not found — run 'make demo' first"; exit 1; }; \
	( command -v paplay >/dev/null && paplay $$f ) || \
	( command -v aplay  >/dev/null && aplay -q $$f ) || \
	( command -v ffplay >/dev/null && ffplay -nodisp -autoexit -loglevel error $$f ) || \
	( command -v mpv    >/dev/null && mpv --no-video $$f ) || \
	echo ">> no CLI audio player found — open demo_out/ears/r3_rider.wav in any player"

base: _deps       ## run the base station alone (prints one line, then serves until Ctrl-C)
	@echo ">> base station starting — this command stays running (Ctrl-C to stop)."
	@$(PY) -m base.main

base-live: _deps  ## base station + this machine's speakers join room `main` (hear TTS/music live)
	@echo ">> base station + speaker monitor — type in the dashboard text bar to hear TTS."
	@$(PY) -m base.main --monitor

convoy: fixtures  ## 6 simulated riders, raw (no dashboard, no wav output)
	@$(PY) -m sim.convoy --riders 6 --profile parkinglot

.PHONY: help doctor setup _deps fixtures earcons test test-fast demo listen base base-live convoy
.DEFAULT_GOAL := help
