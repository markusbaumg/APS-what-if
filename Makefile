# Makefile for the AAPS what-if emulator and Hindcast (software/hindcast)
#
# Run `make` or `make help` to list the targets. Everything runs from software/ with the
# project's .venv (Python >= 3.12; 3.14 is what the author uses). Variables you can override:
#   START/END  UTC ISO window, e.g. make nochange START=2026-10-10T06:00:00Z END=2026-10-10T12:00:00Z
#   VDF        variant file relative to software/ (results land next to it)
#   OPTS       emulator option tokens (see CLAUDE.md), SRC the input ('ns:' or a logfile glob)
#   ARGS       passed through to `make hc ARGS="study status demo"`

VENV   := .venv
PY     := $(abspath $(VENV)/bin/python)
PIP    := $(abspath $(VENV)/bin/pip)
SW     := software
export PYTHONUTF8 := 1

# the golden window of the no-change regression gate (cached under software/ns_cache)
START  ?= 2026-09-06T06:00:00Z
END    ?= 2026-09-06T09:00:00Z
SRC    ?= ns:
VDF    ?= variants/no_change.vdf
OPTS   ?= All/-pred/-flowchart/.
GRID   ?= variants/grid_autoISF.json
LOGS   ?=
ARGS   ?=
BACKUP ?= ../aps-ns-cache-backup

# files whose line endings upstream dictates (CLAUDE.md: preserve whatever a file has)
CRLF_FILES := $(SW)/determine_basal.py $(SW)/emulator_GUI.py change_log.md Demo_Sports_Adaptations.vdf
LF_FILES   := $(SW)/emulator_core.py $(SW)/emulator_batch.py $(wildcard $(SW)/ns_*.py) $(SW)/emulator_optimize.py

.DEFAULT_GOAL := help

.PHONY: help venv install install-dev run run-logs gui validate nochange test lint check \
        fetch refresh optimize hc issues backup-cache push clean

help: ## Show this help
	@echo "Usage: make <target> [START=... END=... VDF=... OPTS=... ARGS=...]"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

venv: $(VENV)/bin/python ## Create the virtual environment (.venv)

install: venv ## Install the runtime dependencies (requirements.txt: matplotlib, numpy)
	$(PIP) install -r requirements.txt

install-dev: install ## Install the development extras too (pytest; see requirements-dev.txt for optuna)
	$(PIP) install -r requirements-dev.txt

run: ## Replay a window with a variant file (SRC='ns:' or a logfile glob, OPTS, VDF, START, END)
	cd $(SW) && $(PY) emulator_batch.py '$(SRC)' '$(OPTS)' $(VDF) $(START) $(END)

run-logs: ## Replay AAPS logfiles: make run-logs LOGS='/path/aapsLogs/AndroidAPS._2026-09-06_*' VDF=...
	@test -n "$(LOGS)" || (echo "Error: give the logfile glob, e.g. make run-logs LOGS='/path/AndroidAPS._2026-09-06_*'" && exit 1)
	cd $(SW) && $(PY) emulator_batch.py '$(LOGS)' '$(OPTS)' $(VDF) $(START) $(END)

gui: ## Open the Tk front end (logfiles only)
	cd $(SW) && $(PY) emulator_GUI.py

validate: ## Compare the ns_* modules against the phone (ns_iob, ns_glucose, ns_profile, ns_meal --validate)
	@for m in ns_iob ns_glucose ns_profile ns_meal; do \
		echo "=== $$m --validate $(START) $(END)"; \
		(cd $(SW) && $(PY) $$m.py --validate $(START) $(END)) | grep -iE 'loops|%|median|mismatch|agree' | head -16; \
	done

nochange: ## The regression gate: empty VDF must reproduce the phone's decisions (agreement table, thresholds)
	cd $(SW) && $(PY) nochange_check.py $(START) $(END)

test: ## Run the Hindcast unit tests (pytest, offline)
	cd $(SW) && $(PY) -m pytest hindcast/tests -q -p no:cacheprovider

lint: ## Compile every script, check line endings (CRLF upstream files, LF ours) and conflict markers
	@! grep -rnE '^(<<<<<<< |>>>>>>> |=======$$)' --include='*.py' --include='*.md' --include='*.json' --include='Makefile' . \
		--exclude-dir=.git --exclude-dir=.venv --exclude-dir=ns_cache --exclude-dir=results --exclude-dir=__pycache__ \
		|| (echo "lint: merge conflict markers found" && exit 1)
	cd $(SW) && $(PY) -m py_compile *.py hindcast/*.py hindcast/tests/*.py
	@cr="$$(printf '\r')"; st=0; \
	for f in $(CRLF_FILES); do [ -f "$$f" ] || continue; \
		if [ "$$(grep -c "$$cr" "$$f")" -lt "$$(wc -l < "$$f")" ]; then echo "lint: $$f must be CRLF (upstream file)"; st=1; fi; done; \
	for f in $(LF_FILES); do [ -f "$$f" ] || continue; \
		if grep -q "$$cr" "$$f"; then echo "lint: $$f must be LF"; st=1; fi; done; \
	for f in $$(find $(SW)/hindcast -name '*.py'); do \
		if grep -q "$$cr" "$$f"; then echo "lint: $$f must be LF"; st=1; fi; done; \
	exit $$st

check: lint test validate nochange ## Everything before a commit: lint, unit tests, validates, no-change gate

fetch: ## Prefetch Nightscout entries/treatments/devicestatus for START..END into software/ns_cache
	cd $(SW) && $(PY) ns_client.py ns: $(START) $(END) entries treatments devicestatus

refresh: ## Re-download the cached days of START..END (and the profile store)
	cd $(SW) && $(PY) ns_client.py ns: $(START) $(END) --refresh

optimize: ## Grid sweep with emulator_optimize.py (GRID, START, END) -> software/results/<label>/
	cd $(SW) && $(PY) emulator_optimize.py --grid $(GRID) --start $(START) --end $(END)

hc: ## Hindcast CLI passthrough: make hc ARGS="study new demo --from-grid variants/grid_autoISF.json --days 2026-09-06"
	cd $(SW) && $(PY) -m hindcast $(ARGS)

issues: ## Open Hindcast issues on GitHub (gh)
	gh issue list -R markusbaumg/APS-what-if --label hindcast --limit 100
	gh issue list -R markusbaumg/APS-what-if --label algorithm-fidelity --limit 50

backup-cache: ## Tar software/ns_cache (the server does not keep loop decisions for ever) into $(BACKUP)
	mkdir -p $(BACKUP)
	tar czf $(BACKUP)/ns_cache_$$(date +%Y-%m-%d).tgz -C $(SW) ns_cache
	ls -lh $(BACKUP)

push: check ## Run check, then push the current branch to origin (the fork)
	git push origin HEAD

clean: ## Remove result files under software/variants, results/, studies/, frozen/ and __pycache__
	rm -f $(SW)/variants/*.csv $(SW)/variants/*.txt $(SW)/variants/*.log $(SW)/variants/*.delta $(SW)/variants/*.pdf
	rm -rf $(SW)/results $(SW)/studies $(SW)/frozen
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
