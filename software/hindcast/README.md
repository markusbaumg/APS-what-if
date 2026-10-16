# Hindcast

Scenario studio and settings optimizer on top of the AAPS what-if emulator in this directory's parent.
It freezes the reconstructed loop inputs of cached Nightscout days, replays many settings variants
against them through the unchanged `emulator_core.parameters_known()`, condenses each replay into
decision-level metrics next to the measured noise floor, and renders one self-contained page plus
candidate cards for a human to review. It is PC-only and lives entirely in this package; the emulator
scripts and the `ns_*` modules are not modified.

The contract is `docs/hindcast/DESIGN.md` (sections 3, 4, 5 and 9 matter most); the issue blocks are in
`docs/hindcast/BACKLOG.md`. Where this file and the code disagree, the code wins.

## Honesty statement

Hindcast replays the loop's decisions under other settings over the glucose and insulin history that the
phone's own settings produced. It says where and how much the loop would have asked differently; it
cannot say what glucose would have done. A variant that boluses earlier is charged for its bolus and for
the later ones the phone actually gave, so deltas overstate aggressive settings and understate gentle
ones. The base replay disagrees with the phone in about 4 % of loops; differences smaller than that floor
are ties. Nothing here changes the phone; settings are for you and your care team to review. This is one
person's insulin dosing, not medical advice.

## Running the tests

```bash
# once, from the repository root
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt     # pytest; optuna is optional

# the one-liner (repository root or software/ as cwd)
.venv/bin/python -m pytest software/hindcast/tests -q

# with the slow replay tests (need the local Nightscout cache)
.venv/bin/python -m pytest software/hindcast/tests -q --run-slow
```

There is no CI; run the one-liner before every PR and paste `hc check` output when an evaluation can
change (DESIGN.md section 9). Tests run offline from fixtures; `tests/conftest.py` lists the paths and the
skip markers (`needs_cache`, `needs_optuna`, `needs_gh`, `slow`). The data fixtures
(`software/results/example_2026-09-06/`, `software/variants/*.csv`, `software/ns_cache/`) are
git-ignored and local; a test that needs one is skipped, not failed, when it is missing, so the suite
passes on a fresh clone.

## GitHub workflow

`python -m hindcast.gh labels|issues|epics --dry-run` (cwd `software/`) mirrors `docs/hindcast/BACKLOG.md`
into labels and issues of `markusbaumg/APS-what-if`, idempotently (existing titles and `H-id`s are
skipped). The repository is public: issue text names preference keys and values, never glucose values,
insulin per day, loop counts per day or transcript excerpts.
