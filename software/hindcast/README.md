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

## Commands

`python -m hindcast <command>` from cwd `software/` (alias `hc`); `hc <command> --help` lists the options.
Exit codes: 0 done, 1 regression (`check`) or command not available, 2 stopped by a guard or a card the
lint refused, 3 refused or not checked.

| Command | Module | What it does |
|---|---|---|
| `hc study new\|run\|status\|stop` | `study.py`, `space.py` | create a study, run or resume its queue (dry evaluator until the orchestrator, H-305, exists), the status summary, the STOP file |
| `hc warm` | `freeze.py` | fill the Nightscout day cache (the only network step) |
| `hc freeze` | `freeze.py` | freeze cached UTC days into `frozen/<host>/<day>.loops.jsonl.gz` |
| `hc replay <day>` | `replay.py` | replay one variant on one frozen day and print its metrics (hand check) |
| `hc check` | `check.py` | regression gate: base replay of 2026-09-06 06-09Z, frozen vs live-day, against `tests/golden/` |
| `hc search <name>` | `search.py` | the search plan: phases, pending tasks, parameters frozen as not testable |
| `hc round <name>` | `round.py` | the verdict step: `round.json` and `status.json` from `trials.jsonl` |
| `hc report <name>` / `hc serve <name>` | `report.py` | render `studies/<name>/report/index.html` (normal, `--single-file`, `--artifact`); serve it on localhost. The page names its source "Nightscout" only: the Nightscout URL and hostname are scrubbed from the embedded JSON and refused in the rendered HTML |
| `hc explain <name> <iso>` | `explain.py` | the drill-down of one loop as text (decision pair, factor chain, reason diff) |
| `hc card <name>` | `cards.py` | linted candidate cards `cards/<n>.md` plus `.vdf` (n: recommended for review first, then candidates, then ties, each in the review order); `hc study run` writes them after every round through the same function |
| `hc gh labels\|issues\|epics\|campaign\|round` | `gh.py` | GitHub labels, backlog issues, the campaign issue and one comment per round |
| `hc lint` | `lint.py` | the may / may-not vocabulary and privacy lint |
| `hc skill install\|vocabulary\|lint\|checkin` | `skill_install.py` | the Claude skill: install it to `~/.claude/skills/hindcast/` (user scope; `--project` for the repository's `.claude/skills/`), regenerate `skill/references/vocabulary.md`, lint the skill text, the one-line check-in |
| `hc export` | - | not built yet (H-504) |

The worker pool (`pool.py`) and the day quarantine (`days.py`) have no command of their own; the
orchestrator (H-305) drives them.

## Unlocking locked keys

The DESIGN.md section 3 lock (the `safety` section, `profile sens`, `carb_ratio`, `current_basal`, `dia`, every
`new_parameter` key) stays the default: `hc study new` refuses such a key with exit code 3. A study may lift
it only explicitly and per study: `study.json` lists the keys in `unlocked` and says why in `unlock_reason`
(required and non-empty; `Study.validate()` refuses an unlock without one, and an unlock without a reason
unlocks nothing). On the command line: `hc study new <name> ... --tier C --unlock "profile sens" "profile
carb_ratio" "profile current_basal" --unlock-reason TEXT` (`--unlock` is repeatable, the union is used);
`--tier C` or `--tier S` without the unlock exits 3 naming the locked keys, and a created study prints the
space file's evidence line and the weak-evidence caveat. `new_parameter` keys, non-profile groups and `profile dia` (it enters through the recorded
IOB) can never be unlocked. Two space files need an unlock: `spaces/profile_tierC.json` searches ISF, CR, basal
(and the profile percentage, which is not locked) as type `scale`, a multiplier of the value each loop
recorded, with phone value 1.0 = no VDF row (`profile sens profile['sens']*1.1`; current_basal re-rounded to the
0.05 pump step); `spaces/safety_tierS.json` searches the caps `max_iob` and `max_basal`, where a study only
reports how often the cap binds. Both carry `"_evidence": "weak: decision-level replay, no glucose
response"`: the replay keeps the glucose the phone's own profile produced, so it cannot judge a different
profile or cap, only how the decisions would have moved. Every surface of an unlocked study quotes
`Study.unlock_caveat()`; every proposal is reviewed by hand.

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
