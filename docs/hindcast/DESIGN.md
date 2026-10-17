# Hindcast — final design

Scenario studio, settings optimizer and Claude skill on top of the AAPS what-if emulator
(repo `markusbaumg/APS-what-if`, branch `nightscout-source`). Synthesized 2026-10-16 from the three
proposals (Hindcast / report-first, winner of all three judges; ShadowLoop / optimizer-first; Counterloop /
agent-first), the judges' verdicts, the rendering study (`LIBS.md`) and the autoISF backport review
(`REVIEW.md`). Every statement about the repo below was checked against the code on this date; nothing in
the repo was edited.

---

## 1. Product name

**Hindcast** (package `software/hindcast/`, CLI `hc`, skill `/hindcast`).

Why this one: a *hindcast* is the established term in weather and ocean modelling for re-running a model
over recorded past inputs to test a parameterisation. That is exactly, and only, what the emulator does: it
replays the loop's decision function over the glucose, IOB and COB the phone actually saw, with other
settings. The word carries the honesty statement inside it: it is the opposite of a forecast, it claims
nothing about what *would have followed*. It is one word, pronounceable in English and German, has no
diabetes or AID product collision (checked by the proposal author with WebSearch), does not borrow the
`Loop…` prefix of the iOS ecosystem, and `hc` is a free two-letter command. Package name `hindcast` is
generic enough that nobody will mistake it for a medical device.

Runner-ups, in order:

| name | strength | why not |
|---|---|---|
| **Counterloop** | precise ("counterfactual loop"), good verb ("counterloop a day"), strongest governance proposal | slightly adversarial sound; "counter" reads as "against the loop" to a newcomer |
| **ShadowLoop** | control-engineering "shadow mode" is the right analogy; sounds like a product | Loop-prefix/suffix confusion with the iOS Loop ecosystem; a "shadow loop" suggests it runs live beside the phone, which it does not |
| **Loopwright** | friendly, unique, "a wright mends loops" | Loop-prefix again; cute rather than serious for insulin dosing |
| **Otherwise** | the question in one word ("what the loop would otherwise have done") | ungoogleable, unusable as CLI or package name |

The names of the three proposals survive as internal nicknames for the ideas they contributed (the
"Counterloop ladder", the "ShadowLoop freeze").

---

## 2. Vision

Hindcast turns the emulator from "one VDF, one matplotlib PDF" into a studio: a resumable Python runner
freezes the reconstructed loop inputs of each cached Nightscout day once, replays hundreds to thousands of
settings variants against them through the unchanged `emulator_core.parameters_known()`, condenses each
replay into honest decision-level metrics next to the measured noise floor, and renders one self-contained
ECharts page (a Claude Artifact that also opens from disk) with a trustworthiness banner, a parameter-space
view, small-multiple scenario timelines with a per-loop "where does it diverge" strip, and a drill-down into
any single loop with both prediction fans and a word-diff of the two decision explanations. A Claude skill
drives the long loop for hours (frame, warm, freeze, floor, search, check in, review), publishes the page,
streams rows into the artifact database, tracks the work in GitHub issues, and ends every sitting with
plain-language candidate cards for a human to review — in a vocabulary that the generator lints. Hindcast
recommends settings to a person; it never changes the phone, Nightscout or `ns_settings.json`.

---

## 3. Honesty statement

This paragraph appears verbatim on every page of the dashboard (not collapsible), in `hc` output, in every
candidate card and proposal, and in the skill:

> Hindcast replays the loop's decisions under other settings over the glucose and insulin history that the
> phone's own settings produced. It says where and how much the loop would have asked differently; it
> cannot say what glucose would have done. A variant that boluses earlier is charged for its bolus and for
> the later ones the phone actually gave, so deltas overstate aggressive settings and understate gentle
> ones. The base replay disagrees with the phone in about 4 % of loops; differences smaller than that floor
> are ties. Nothing here changes the phone; settings are for you and your care team to review. This is one
> person's insulin dosing, not medical advice.

**May say**: "would have asked for", "would have microbolused", "would have set a zero temp", "the factor
would have sat on the cap", "the loop would have disabled SMB in N more loops", "asks for more insulin in
the two hours before a recorded low", "indistinguishable within the noise floor", "not testable on these
days", "reacts earlier / later", "review order".

**May not say**: "better", "best", "optimal", "safer", "fewer lows", "fewer highs", "better control", any
time-in-range, HbA1c or hypo count, "would have prevented", any statement about the future, "recommended
setting" without the qualifier "for review".

**Never computed**: virtual glucose, counterfactual IOB, outcome scores built from realised glucose. The
realised glucose after a loop is used in exactly two places and both are *positional*: (a) to locate the
two hours before a recorded low for the pre-hypo constraint P, (b) to shade the BG trace in the drill-down
("a low followed here"). It is never multiplied into a score (the judges' unanimous objection to the
ShadowLoop hindsight label H).

**Cannot be tested with this method** (listed on the page): basal rates, DIA/peak, carb ratio, autosens
ratio, the UKF smoother — they enter through the inputs the replay takes as recorded. **Locked for an
unattended run** (enforced in code, no override flag): the `safety` section (`max_iob`, `max_basal`,
safety multipliers, `lgsThreshold`, `bolus_increment`), `profile sens`, `profile carb_ratio`,
`profile current_basal`, `profile dia`, every `new_parameter` key. These remain available to a human with
the plain emulator; Hindcast refuses them in `study.json`.

---

## 4. Architecture

### 4.1 What exists and is reused unchanged

| existing | role |
|---|---|
| `emulator_core.parameters_known(url, 'Windows/<opts>/noplot/.', vdf, start, end, {}, msg, '.', epoch)` | the one replay call per (variant, day). `set_tty(0, 0, 'batch')` once per process. Sequential calls in one process are clean (the sentinel/null checks prove it every round). |
| `emulator_core.Pred` / `emulator_core.FlowChart` (module globals, keyed `round(ms/1000, 1)`, filled unconditionally in `TreatLoop` l. 1049-1050, also on `noplot`) | emulated predBGs, `Levels` (minGuardBG etc.), autoISF factor chain, flow boxes — read immediately after the call, guarded (`replay.py` degrades to "no predictions" rather than failing) |
| `emulator_optimize.metrics_from_csv / read_result_csv / write_vdf / variant_name / vdf_value / effective_cap` | imported, not copied (the module has a `__main__` guard; importing it only sets `PYTHONUTF8`) |
| `ns_source.NightscoutSource`, `resolve_window`, `window_label`; `ns_client` cache rules; `ns_settings.json` (`profile_history`, `_assumed`) | the only network path (inside `hc warm`/`hc freeze`); window naming; confounders |
| `software/results/example_2026-09-06/` (5 CSVs + summary) and `variants/*.csv` | fixtures for the unit tests and the MVP demo |

`emulator_optimize.py` keeps working unchanged; `hc study new --from-grid variants/grid_autoISF.json`
imports a grid file as a `grid` phase.

### 4.2 Module layout

```
software/hindcast/                     PC only; .venv libraries allowed (numpy optional, optuna optional); ns_* untouched
  __init__.py
  cli.py            python -m hindcast / hc: study new|run|status|stop|report|serve|explain|card|export|check, warm, freeze, gh
  contracts.py      dataclasses + JSON schema for study.json, trials.jsonl rows, status.json, series/pred files (frozen day 1)
  freeze.py         FrozenDay: ns_source.NightscoutSource(...).loops() -> frozen/<host>/<day>.loops.jsonl.gz + <day>.meta.json
                    FrozenSource: drop-in for ns_source.NightscoutSource (same ctor, load() no-op, loops() filtered by window)
  replay.py         run_trial(day, vdf_lines, outputs) -> TrialResult: installs FrozenSource, writes the VDF in a per-worker
                    temp dir, calls parameters_known, reads the CSV + Pred/FlowChart, writes series/pred JSON, deletes transcripts
  pool.py           multiprocessing (spawn), one emulator_core per worker, set_tty once, max_tasks_per_child, nice
  metrics.py        emulator_optimize metrics + section 5 additions, floors, envelope E, constraint P, Pareto, OBJECTIVE_VERSION
  space.py          parameter space ('<array> <item>' keys), ranges/choices/step grid, ordering constraints, locked list, VDF text, vhash
  search.py         phases: oaat, grid, lhs, halving, coordinate, optuna (TPE scalar / NSGA-II), validate (hold-out), cv
  study.py          study dir, trials.jsonl (append-only), STATE.json (atomic rename), lock, STOP, sentinel, null, stale_context
  days.py           cached-day inventory, finality, day character record, confounder badges, strata, hold-out reservation
  export.py         tier-1/tier-2 JSON, db rows, candidate cards, VDF copy, prefs_map lookup
  lint.py           the may/may-not vocabulary; refuses to render a card/proposal/issue body that violates it; privacy lint
  report.py         page from templates/, --single-file inliner (12 MB warning), size check, allowed-host check
  gh.py             GitHub via `gh`: labels, epic/campaign/proposal/finding issues, round comments, label read-back
  templates/        page.html  page.css  page.js  theme.js  (hand-written once; no build step; ECharts 6.1.0 from cdnjs)
  prefs_map.json    VDF key -> AAPS preference title as shown on the phone, and the AAPS unit
  tests/            pytest; fixtures from results/example_2026-09-06 and variants/*.csv
software/studies/<study>/               git-ignored (add to .gitignore): study.json, trials.jsonl, STATE.json, optuna.journal,
                                        runs/, series/, pred/, report/, cards/, proposals/
software/frozen/<host>/                 git-ignored: <day>.loops.jsonl.gz, <day>.meta.json
.claude/skills/hindcast/                SKILL.md + references/ (section 8)
```

### 4.3 Data flow

```
ns.vivikusi.ch ──GET only, ns_client cache, final-day rule──> ns_cache/<host>/...        (hc warm: the only network step)
        │
        ▼  hc freeze  (ns_source.NightscoutSource.loops() once per UTC day; VDF-independent, verified: setVariant runs
        │              after the inputs exist, ns_meal's uci uses the phone's own variable_sens)
frozen/<host>/<day>.loops.jsonl.gz + <day>.meta.json  (loop count, sha1, code_sha of ns_*, settings_sha, final, character, confounders)
        │
        ▼  hc study run   (pool.py workers; search.py asks for the next variants; study.py hands out (variant, day) tasks)
replay.py: FrozenSource installed as ns_source.NightscoutSource -> parameters_known(..., 'noplot', ...) in a per-worker temp dir
        │     -> <start>_<end>.<variant>.csv (+ .txt/.log/.delta, deleted unless --keep-transcripts) ; Pred/FlowChart globals
        ├──> metrics.py  -> one trials.jsonl line per (variant, day), appended atomically
        ├──> series/<day>/<variant>.json (columnar, rounded), pred/<day>/<variant>.json (finalists only, or --pred)
        └──> series/<day>/orig.json + pred/<day>/orig.json once per day (phone's series, recorded BG/IOB/COB, events, predBGs)
        │
        ▼  every N trials and on exit
export.py -> report/index.html (tier 1 embedded) + series/, pred/ (tier 2 files)  ─── Artifact publish (agent, same URL)
          -> status.json (<= 2 KB) and round.json (<= 2 KB per variant)              ─── what the skill reads
          -> db rows (studies/<id>, trials/<key>, cards/<rank>)                      ─── ArtifactData batch (agent)
          -> cards/<n>.md (linted), proposals/<vhash>.md (linted)                 ─── human review; gh.py posts links only
```

### 4.4 How Hindcast uses `parameters_known()`

Identical argument shape to `emulator_optimize.main()`: `url` = `'ns:'` (the Nightscout branch is taken;
with the frozen source installed no socket is opened — a test asserts this), `arg2` =
`'Windows/' + outputs + '/noplot/.'`, `variantFile` = the VDF written into the worker's temp dir (results
land in the VDF's directory: `<start>_<end>.<variant>.csv|.txt|.log|.delta` and `<start>_<end>.orig.txt`),
`start`/`end` resolved once per study with `ns_source.resolve_window` so every trial of a day replays the
same loops and carries the same label, `entries = {}` fresh per call, `msg` = the echo banner (versions
and settings hash go into the `.log`), `'.'` decimal, `datetime(1970, 1, 1)`. `set_tty(0, 0, 'batch')`
once per worker; stdout is redirected per trial into `runs/<key>.out` so the `sub_issue()` text of a
`SYNTAX`/`Z` result is captured and stored with the failed trial. Return tuple element 1 is the status:
`SYNTAX`, `UTF8`, `Z` → `status: failed` (the search is told the point is infeasible, the study continues;
three consecutive failures on different variants stop the run). Default outputs:
`seconds/bg/iob/autosens/acce/bg_ISF/pp/dura/ISFs/insReq/SMB/basal` (the optimizer's) so the 43-column CSV
carries every factor column `metrics.py` reads.

Frozen source injection (no core edit): `scanNightscout()` does `import ns_source` inside the function
and constructs `ns_source.NightscoutSource(fn, t_startLabel, t_stoppLabel, log_msg=log_msg)`, then calls
`.load()` and hands the object to `scanNightscoutLoops(src, fn)` which iterates `src.loops()`. `replay.py`
sets `ns_source.NightscoutSource = FrozenSource` on the real module object (so `resolve_window` and
`window_label`, also imported from `ns_source` inside `parameters_known`, keep working). `FrozenSource`
has the same constructor, a no-op `load()`, and `loops()` yields the day's stored dicts whose `deliverAt`
falls inside the window, in order. The loop dicts are JSON-serialisable by construction (the core
`json.dumps()` them into the collectors), so the frozen format is gzip JSONL, not pickle. A core-side hook
(`loop_source_factory`) is requested from the maintainers as a later issue so the dependency becomes
explicit; until then a test pins the attribute names Hindcast relies on.

### 4.5 File formats

`studies/<study>/study.json` (written by `hc study new`, frozen afterwards except `phases[].state`):

```json
{"name": "autoISF_oct", "objective_version": 1, "created": "2026-10-16T19:10:00Z",
 "source": "ns:", "host": "ns.vivikusi.ch",
 "settings_sha": "1a4869fdc61c", "code_sha": "9f0c…",
 "core_versions": {"emulator_core.py": "2026-09-08 04:00", "determine_basal.py": "2026-09-08 04:00"},
 "days": [{"day": "2026-09-06", "tags": ["workday"], "role": "search"},
          {"day": "2026-09-05", "tags": ["weekend", "sport"], "role": "holdout"}],
 "space": {"profile autoISF_max":     {"type": "float", "low": 1.1, "high": 1.8, "step": 0.05},
           "profile dura_ISF_weight": {"type": "float", "low": 0.3, "high": 1.0, "step": 0.05},
           "profile enable_pp_ISF":   {"type": "bool"}},
 "constraints": [["profile smb_delivery_ratio_min", "<=", "profile smb_delivery_ratio", "<=", "profile smb_delivery_ratio_max"]],
 "base_vdf_lines": [],
 "question": "explore",
 "objective": {"scalar_weights": {"rest": 2.0, "lowside": 0.3}, "envelope": {"max_3h_U": 1.0, "max_day_pct_tdd": 15},
               "feasible": {"cap_binding_max_pct": 50}},
 "phases": [{"kind": "oaat", "points": 5}, {"kind": "lhs", "n": 24}, {"kind": "halving", "rungs": [2, 5, "all"]},
            {"kind": "optuna", "sampler": "nsga2", "n": 200}, {"kind": "validate", "top": 8, "days": "holdout"}],
 "budget": {"hours": 6, "workers": 4}, "sentinel_every": 25}
```

`question` is `explore` (default: nothing is ranked; the leaderboard is in *review order*, labelled so),
`earlier` (the user is looking for more insulin in rising-above-target loops and none elsewhere) or
`gentler` (less insulin in rising loops). The direction is a declared study setting shown on the page,
never a default value judgement (judges' objection to `more_in_rise` as default).

`studies/<study>/trials.jsonl` — one object per line, append-only (`O_APPEND`, one `write()` per line,
lines < 4 KB), never rewritten:

```json
{"trial": 37, "key": "f3a9…", "phase": "lhs", "vhash": "a91f…", "variant": "autoISF_max_1.45__dura_ISF_weight_0.55",
 "params": {"profile autoISF_max": 1.45, "profile dura_ISF_weight": 0.55}, "day": "2026-09-06",
 "status": "done", "seconds": 2.7, "worker": 3, "code_sha": "9f0c…", "settings_sha": "1a48…", "freeze_sha": "77be…",
 "metrics": {"loops": 1311, "changed_loops": 97, "rise_vs_base_U": -0.41, "rest_vs_base_U": 0.02, "lowside_flags": 0,
             "prehypo_extra_U": 0.0, "env_3h_max_U": 0.35, "env_day_pct_tdd": 1.6, "cap_binding_pct": 9.4, "...": 0},
 "feasible": true, "flags": [], "files": {"series": "series/2026-09-06/a91f….json"}}
```

`key = sha1(canonical params | day | settings_sha | code_sha | freeze_sha | objective_version)`. A key with
`status: done` is never re-run (resume and cross-phase dedupe). Status values: `done`, `failed`
(SYNTAX/Z, with `error`), `blocked` (day not final / not frozen), `stale` (code or settings changed after
the trial; re-evaluated lazily, base first). Optuna's journal file is a derived store, rebuilt from
`trials.jsonl` if missing or inconsistent.

`STATE.json` (atomic rename after every trial): `{"phase", "trials_done", "queue_len", "pid",
"heartbeat", "stale_context", "last_key", "budget_left_s", "disk_free_mb"}`. `STOP` file = finish the
current trial, write status, exit 0. Exit codes: 0 finished/stopped, 2 stopped by a guard (sentinel,
null, fidelity, stale_context, three errors, disk), 3 configuration refused (locked key, non-final day,
unknown VDF key).

`frozen/<host>/<day>.meta.json`: `{"day", "loops", "sha1", "ns_code_sha", "settings_sha", "final", "created",
"dropped": {"no_reason": n, "aborted": n, "no_cgm": n, "duplicate": n}, "character": {"tdd_U", "carbs_g",
"bg_mean", "pct_gt180", "pct_lt70", "tt_minutes", "tt_parity_smb_off_pct", "cgm_gap_min", "activity_min"},
"confounders": ["profile_history: autoISF_max 1.3->1.5 at 20:23 local", "cgm gap 71 min 15:30-16:41"]}`.
Day character chips (`workday`, `sport`, …) are typed by the user in `study.json`, never inferred.

`report/series/<day>/<variant>.json` — columnar, rounded (BG 1 dp, insulin 3 dp, factors 2 dp, `t` as
seconds from the UTC day start): `{"day", "vhash", "t": [...], "smb": [...], "ins_req": [...], "tbr": [...],
"isf_factor": [...], "isf": [...], "iob_th": [...], "tar_lo": [...], "tar_hi": [...], "changed": [0|1, ...],
"min_guard": [...]}`. `series/<day>/orig.json` adds `bg`, `iob`, `cob`, `events` (carbs, TT, profile
switch, CGM gaps, lows < 3.9 mmol as positional markers), the phone's `smb/ins_req/tbr/isf_factor`.
`pred/<day>/<variant>.json`: `{"t": [...], "iob": [[48], ...], "zt": ..., "uam": ..., "cob": ..., "levels":
[{...}], "factors": [{"acce", "bg", "pp", "dura", "drift", "final"}], "reason": [...], "console": [...], "flows": [[...]]}`;
`pred/<day>/orig.json` holds the phone's predBGs, reason and consoleError. Tier-2 files are published
through the Artifact `files` map and fetched on demand.

Artifact `db` collections (phase 2): `studies/<id>` (study.json minus the space details, plus status),
`studies/<id>/trials/<key>` (the trials.jsonl line without `files`), `studies/<id>/cards/<rank>`. Rules
`read: view`, `write: owner`. The db is a mirror the agent pushes; `trials.jsonl` is the truth.

### 4.6 CLI

```
hc warm   2026-08-30..2026-09-07                 serial cache fill via ns_client; refuses non-final days unless --allow-partial
hc freeze 2026-08-30..2026-09-07 [--new]         one frozen file per UTC day; --new = only days not yet frozen (at most hourly from run)
hc check  [--day 2026-09-06]                     regression gate: base replay vs stored golden metrics (section 9)
hc study new autoISF_oct --days 2026-09-01..09-07 --holdout 2026-09-05 --tag 2026-09-05=weekend,sport \
      --param "profile autoISF_max" 1.1:1.8:0.05 --param "profile dura_ISF_weight" 0.3:1.0:0.05 --question explore
hc study new autoISF_oct --from-grid variants/grid_autoISF.json --days ...
hc study run    autoISF_oct --hours 6 --workers 4 [--until 23:30] [--on-stale stop|reeval]    resumable; Ctrl-C/kill/STOP any time
hc study status autoISF_oct [--json]            <= 2 KB: phase, trials done/left, floor, top candidates, heartbeat, stale_context
hc study stop   autoISF_oct                     writes STOP
hc report autoISF_oct [--single-file --days 2026-09-06 --variants top3] [--pred top5]   -> studies/autoISF_oct/report/
hc serve  autoISF_oct                           stdlib http.server on report/
hc explain autoISF_oct 2026-09-06T09:00:37Z --variant <vhash>     the drill-down as text (for the skill and for grep)
hc card    autoISF_oct [--rank 1..N]            linted candidate cards -> cards/<n>.md (+ VDF)
hc export  autoISF_oct --db-rows [--since <iso>] > rows.json      what the skill pushes with ArtifactData batch
hc gh      labels|epics|campaign|round|propose|sync ...            GitHub (section 9), every body passes lint.py
```

---

## 5. Objective and metrics

### 5.1 Ground rule and noise floor

Every metric is a decision-level counterfactual on recorded inputs, paired against the **base** replay
(empty VDF = the phone's own settings with `profile_history` applied) so that the replay bias is removed.
Per day and component the **floor** is `floor_X = |X_base − X_phone|` (ShadowLoop's rule; more robust
than a stdev over six days). Measured today (NIGHTSCOUT_SOURCE.md, 2026-09-06, 1311 loops): SMB exact
96.1 %, within 0.05 U 98.5 %; insulinReq within 0.05 U 98.3 %; variable_sens within 1 mg/dl 99.6 %; SMB
total +4.05 U/day (the SMB-interval tolerance — item G of the fidelity epic — is the dominant source).
`floor_loops = changed_loops(base)`. A difference between two variants smaller than `2 × floor` on a
component is a **tie** on that component and is drawn grey.

Two controls run automatically: the **sentinel** (the phone's current values as explicit VDF rows must
reproduce base to the loop; every `sentinel_every` trials and at every phase start) and the **null
variant** (one searched parameter set to the phone's value; once per round). Either mismatch, or unequal
loop counts across variants of a day, is a hard stop (exit 2) — state leaked between in-process replays.

### 5.2 Per-trial metrics (`metrics.py`, `OBJECTIVE_VERSION = 1`)

Imported from `emulator_optimize.metrics_from_csv()`: `loops`, `smb_*`, `tbr_*`, `total_*`, `delta_U`,
`delta_pct`, `smb_more/less/equal`, `smb_loops_*`, `smb_max_emul_U`, `isf_factor_*`, `at_autoISF_max/min_pct`
(cap per loop via `effective_cap()`, profile_history aware), `isf_emul_mean`, `iob_over_TH_pct`,
`iob_over_tol_pct`, `rise_*`, `rest_*`, `bg_*`. Give per loop is `smb + tbr_rate × dt` with `dt` capped at 30
min (the optimizer's convention). Added:

| metric | definition | reading |
|---|---|---|
| `changed_loops` | loops where SMB (0.05 U), insulinReq (0.05 U) or TBR rate (0.05 U/h) differ from the phone | raw size of the counterfactual; base's value is the floor |
| `changed_vs_base` | `changed_loops − changed_loops(base)` | effect credibility |
| `rise_vs_base_U`, `rest_vs_base_U` | `rise_emul − rise_base`, `rest_emul − rest_base` | when the extra/missing insulin comes |
| `lowside_flags` | loops with `smb_emul > smb_phone` while emulated `minGuardBG < 3.9 mmol` (from `Pred[...]['Levels']`) or recorded BG < 5.0 mmol and falling | the honest low-side proxy, from emulated predictions and recorded glucose only |
| `prehypo_extra_U` (**P**) | Σ over loops in the 120 min before a recorded BG < 3.9 mmol of `max(give_emul − give_base, 0)` | hard constraint: `P ≤ P_base + floor`; "any additional insulin before a low that actually happened" |
| `env_3h_max_U`, `env_day_pct_tdd` (**E**) | max over sliding 3 h windows of Σ(give_emul − give_base); `|Σ_day| / TDD_phone × 100` | validity envelope: beyond `max_3h_U` (default 1.0) or `max_day_pct_tdd` (default 15) the "inputs unchanged" assumption is broken; shown greyed "out of envelope", never ranked |
| `zero_temp_vs_base` | loops with an emulated 0 U/h temp where base had none, minus the reverse | high-side caution proxy |
| `cap_binding_pct` (**S**) | `at_autoISF_max_pct` + `at_autoISF_min_pct`; also `iob_over_TH_pct` | a setting at its cap was not really tested; > 50 % = infeasible for ranking, and a reason to vary the cap |
| `max_single_smb_U` | largest emulated SMB; must be ≤ phone's `maxBolus` of that loop | safety reading for a human, always printed on the card |
| `tt_smb_off_pct` | share of loops where the TT parity disabled SMB (phone and variant alike) | SMB parameters are only observable on the rest |
| `agreement_pct` | all four quantities within tolerance (base only) | the noise floor on the banner |

Per day-variant; aggregated over days as mean, range and sign fraction. Hindsight markers ("a low followed
within 2 h", "a high followed") are stored as positional covariates per loop for shading and filtering in
the drill-down; they are **not** a metric.

### 5.3 Ranking rule (printed next to the leaderboard; no hidden scalar)

1. **Feasible** = `P ≤ P_base + floor` on every day, inside the envelope E on every day,
   `max_single_smb_U ≤ phone maxBolus`, `cap_binding_pct < 50 %`, loop count equal to base. Infeasible
   variants are shown greyed with the reason, never ranked.
2. **`question: explore`** (default): no ranking. The table is in *review order* = `|effect| / floor`
   descending where effect = `rise_vs_base_U` + `rest_vs_base_U` on the largest-magnitude component, with
   the header "review order, not a ranking by outcome". Dead parameters (all points inside the floor on
   all days) are reported as "not testable on these days" and frozen at the phone's value.
3. **`question: earlier`**: Pareto front of (maximise `rise_vs_base_U`, minimise `max(rest_vs_base_U, 0)`,
   minimise `lowside_flags`). **`gentler`**: the mirror image (minimise `rise_vs_base_U`, minimise
   `|rest_vs_base_U|`, minimise `zero_temp_vs_base` increase). NSGA-II needs no scalar; for TPE the
   scalarisation `rise_vs_base_U − 2.0·max(rest_vs_base_U, 0) − 0.3·lowside_flags` (sign flipped for
   `gentler`) with the weights in `study.json` and shown on the page, plus a weights-sensitivity panel
   (ranking under ±50 % of each weight) so nobody mistakes a weight choice for a finding.
4. Variants whose component differences are inside `2 × floor` are reported as a **tie**.
5. **Recommended for review** only if: feasible on all days, top-3 on ≥ 80 % of leave-one-day-out folds,
   same sign of the primary effect on ≥ 80 % of days, and rank on the hold-out days within the top 5.

### 5.4 Search strategy and overfitting controls

Cost with the freeze (measured by the ShadowLoop author, verified by a judge: 340-380 loops in 3.8-3.9 s
with `noplot`, 91 % of it VDF-independent reconstruction; `TreatLoop` 1.6 ms/loop): ~2-3 s per day-variant
→ 4 workers ≈ 5 000 day-variants per hour. The binding constraint is the data (9 cached days today, three
`profile_history` changes inside them), not CPU. Days are the unit of everything — never loops.

Phases, each resumable, each a row in `trials.jsonl`:

0. **Freeze and floor** — `hc freeze` (refuses non-final days), base + sentinel on every day, floors,
   day character, confounder badges, quarantine of days under the fidelity floor (`agreement_pct < 94 %` or
   more than 2 h of CGM gap while the phone looped, or a `profile_history` change inside the day for a searched
   key): listed, not hidden, excluded from scoring. Gap time with no CGM and no loop at all (AAPS runs no loop
   without CGM; the replay loses no loop there) is only a 'no CGM and no loop' badge, never a quarantine reason.
   A partial day, on which the phone looped less than 16 h of the 24 (each loop run counted for at most 5 min),
   is quarantined as well, since its TDD and 3 h windows are a fraction of the envelope's.
1. **Screen** — one-at-a-time, 5-7 levels per parameter, on 2-3 days of different strata (Morris-style
   mean |effect| / floor across days). Parameters below 1.5 × floor everywhere are frozen ("not testable").
2. **Latin hypercube** over the live parameters (24-48 points) on the screening days; the first picture
   for parallel coordinates and heatmaps.
3. **Successive halving across days** — rung 1 on the 2 most typical search days (nearest the median
   character), keep the top third (feasibility applied at every rung), rung 2 on 5 days, rung 3 on all
   search days.
4. **Optuna** (optional dependency; `JournalStorage` file backend, no SQLAlchemy; fallback: coordinate
   descent + halving on stdlib) — NSGA-II on the three objectives or TPE on the scalar, ask-and-tell so
   the pool stays in control, each suggestion evaluated on all search days before `tell`. 200-400 trials.
5. **Validate** — top-N on the hold-out days reserved in `study.json` (different character, never seen by
   the search); leave-one-day-out rank stability (Kendall τ, per-variant rank range) shown as a caveat
   with the day count, not as a statistic; predictions-on re-run of the top-N for the drill-down.
6. **Grow** — `hc freeze --new` (at most hourly from inside `run`) adds days that became final; the
   current top-10 is re-evaluated on them first; demotion on sign flip; restart of phase 4 only if the
   front moved beyond the floor. This is the "run more and more" mode (`--until-stable 3`).

Overfitting controls: hold-out days reserved before the search; day-level CV; floor-based ties; envelope
E and constraint P as hard feasibility (they close the replay's blind spot — "dose more on every rise"
cannot win because it leaves the envelope); saturation reported; weights-sensitivity panel; dead
parameters reported as results; the question direction declared, not defaulted; the sentinel and null
controls against state leaks; `code_sha`/`settings_sha`/`freeze_sha` on every row so that a concurrent
core fix marks rows `stale` instead of mixing emulator versions (default `--on-stale stop`: finish the
round, stop, say so; `--on-stale reeval`: re-evaluate base first so the floors update, then the rest
lazily).

Time-of-day parameters (`STAIR_ISF`, `INTERPOL`, 2-4 knots) are phase 2 of the product.

---

## 6. Dashboard

**Library**: Apache ECharts **6.1.0**, the single script
`https://cdnjs.cloudflare.com/ajax/libs/echarts/6.1.0/echarts.min.js` (verified 200 and ≥ 2 weeks old by
the rendering study; the proposal's 5.5.1 is a 404 on cdnjs). Canvas renderer. Everything that is not a
chart is plain HTML/CSS (stat tiles, decision pair, dumbbells as inline SVG, text diff, flow list). Data as
`<script type="application/json">` blocks (`</` escaped, `allow_nan=False`), columnar, rounded. Python
side: stdlib `json` + `string.Template`, template files next to the generator. Before writing
`templates/`, the implementer loads the `artifact-design` and `dataviz` skills; the palette is the dataviz
reference palette as CSS tokens on `:root`, redefined under `@media (prefers-color-scheme: dark)` guarded
by `:root:not([data-theme="light"])` and again under `:root[data-theme="dark"]`, `body` with an explicit
background, 16 px gutters, no horizontal scroll at 400 px. The ECharts theme is built from
`getComputedStyle` at init and rebuilt on a `matchMedia` change or a `data-theme` mutation. The built-in
`dark` theme is not used.

**Colour and marks**: the phone is context in neutral grey (same in both modes); each selected variant is
an entity in categorical slot 1-3 (blue, orange, aqua; validated all-pairs CVD); the fourth slot is refused
by the picker ("replace"). Colour follows the variant for the life of the page, never its rank. Diverging
blue (less insulin than the phone) ↔ red (more) with grey at zero, grey inside the floor. Sequential blue
ramp for magnitudes. Status colours only for feasibility flags, always with icon and label. 2 px lines,
SMB stems with ≥ 8 px heads, 2 px surface gaps in heatmaps, solid hairline grid; dashes reserved for the
ZT/UAM prediction coding. One UI sans, `tabular-nums` in tables and ticks. Every chart has a `<details>`
table twin from the same JSON; keyboard focus shows what hover shows; `prefers-reduced-motion` disables
zoom animation. No dual axis anywhere; no colour-cycled series.

**Pages / panels**:

1. **Study overview** (landing): the honesty paragraph (not collapsible); noise-floor banner with the
   computed numbers ("base reproduces the phone's SMB in 96.1 % of 1 311 loops; emulated SMB total +4.05
   U/day; differences below … are ties"); day strip (one card per day: date, loops, character record,
   confounder badges, quarantine state, role search/hold-out, user chips); progress and freshness tiles
   (trials done/planned, phase, heartbeat, "data from cache, days final", `stale_context`); the "cannot
   be tested with this method" list and the `_assumed` keys from `ns_settings.json`.
2. **Parameter space**: *Parameters tab* (Counterloop): one small panel per searched parameter, x =
   value, y = effect vs base per day as points with the floor band — dead / monotone / saturating at a
   glance; *parallel coordinates* (one axis per parameter, then the components; thin grey lines, Pareto
   members in accent, base dashed neutral; brushing filters the leaderboard; replaced by the table at
   phone width); *delta heatmap* for any two parameters (diverging, grey at zero, base cell outlined) and
   the *days × variants* matrix with the day-character strip (ShadowLoop) so a weekend-only optimum is
   visible; *Pareto scatter* (x = `rise_vs_base_U`, y = `rest_vs_base_U`, size = days, base as hollow
   ring, infeasible greyed with reason); *leaderboard table* with every component, the floor painted as a
   band, the ranking rule and the question printed above it; weights-sensitivity panel when a scalar is
   in use.
3. **Scenario comparison**: for one day and up to three variants plus the phone, stacked panels with one
   shared time axis, `axisPointer.link` crosshair and shared `dataZoom`: glucose (once, with target bands,
   carbs as triangles, TT/profile-switch hairlines, lows shaded positionally), final ISF factor with the
   cap hairline (`autoISF_max 1.5`), insulinReq, SMB stems with the **difference strip** below (variant −
   phone per loop, diverging, grey inside the floor — the single most useful chart), temp basal in force,
   IOB with the emulated `iobTH` and its 130 % tolerance. Toggle: columns per day for one variant ("does
   it behave the same on the sport day"). Confounder badges of the day above the panels.
4. **Loop drill-down** (click a loop column or a table row; side sheet, full screen on phone): header
   (time, BG, delta, IOB, COB, profile in force, TT), decision pair phone/variant (insulinReq, SMB,
   maxBolus, TBR, final ISF factor, eventualBG, minPredBG, minGuardBG; differing cells with a 3 px stripe
   in the variant colour), both prediction fans on one axis (IOB solid, ZT dashed, UAM dotted; 3.8/5.4
   mmol guard hairlines), ISF-factor dumbbells (acce, bg, pp, dura, drift → final), word-level diff of the
   two reason/consoleError texts, the flowchart boxes as a collapsed indented list with JS line refs;
   keys ← → one loop, `[` `]` next differing loop. Loops with a recorded low within 2 h carry a positional
   badge ("a low followed at 09:41"), never a score.
5. **Candidates**: one card per candidate (section 7.4), the proposal state from GitHub (label read-back),
   the round log.

**Updating while the loop runs**: `hc study run` regenerates tier 1 every 10 trials and on exit; tier-2
files are written per trial, so a republish at any time is a consistent snapshot. MVP: the skill
republishes the same artifact URL at each check-in (latency = one check-in). Phase 2: the page declares
`capabilities: {db: {}}`; when `claude.use("db")` resolves, the leaderboard subscribes to
`studies/<id>/trials` and shows rows as the agent pushes them ("live rows via Claude, last push 19:42");
when `db` is `null` the embedded tier 1 is the page. **Size**: tier 1 (study + 1 000 trials) ≈ 0.6 MB;
`orig` series ≈ 100 kB/day; per day-variant series 60-90 kB; `pred` ≈ 1 MB per day-variant (finalists
only, fetched on click). `--single-file` inlines the selected days/variants for viewing from disk
(`file://` blocks `fetch`) and warns above 12 MB; `hc serve` is the alternative for a whole study.
uPlot (jsdelivr) is the documented add-on if a week-long per-loop strip ever has to stay responsive on a
phone.

---

## 7. Claude skill

### 7.1 Layout

```
.claude/skills/hindcast/
  SKILL.md                      trigger phrases, the sitting protocol in 12 steps, the never-list, check-in cadence
  references/protocol.md        frame -> warm -> freeze -> floor -> run -> check in -> review -> propose; stop conditions; exit codes
  references/vocabulary.md      may / may-not list (the same list lint.py enforces) and card template
  references/parameters.md      tiers A/B, locked list, "cannot be tested" list, coupling notes, AAPS preference names
  references/reading-guide.md   how to read status.json / round.json; decision rules (dead -> freeze, saturating -> vary the cap,
                                candidate -> confirm on remaining strata, lowside/P flag -> hc explain the loops, tie -> say tie)
  references/github.md          labels, issue templates, the privacy rule, one comment per round, label read-back
```

Installed from `software/hindcast/skill/` into the project's `.claude/skills/` (project-scoped) once the
maintainers agree; user-scoped until then.

### 7.2 Commands the skill runs

Only `hc` subcommands (section 4.6), `gh` through `hc gh`, Bash `run_in_background` for `hc study run`,
`Monitor` on `STATE.json` or `/loop 20m` for check-ins, `Artifact publish` (same URL) and `ArtifactData
batch` for the page. It never reads a CSV or a `.txt` transcript into context; it reads `status.json`
(≤ 2 KB), `round.json` (≤ 2 KB per variant, with `verdict` dead/candidate/tie/infeasible and a `why`
string) and `hc explain` for the few loops that matter.

### 7.3 Loop and resume protocol

1. **Frame**: read `ns_settings.json` (current values, `profile_history`, `_assumed`); list cached final
   days (`hc freeze --list`); ask once for days, tags, hold-out, parameters and the question; refuse
   locked keys and keys outside the VDF groups; `hc study new`; print the honesty paragraph.
2. **Warm and freeze**: `hc warm` (foreground, once; the only network step), `hc freeze`.
3. **Floor**: `hc study run --phase floor`; read `status.json`; stop and open a *finding* issue if base
   agreement < 94 % on a non-quarantined day (the study would measure the emulator, not the settings).
4. **Run**: `hc study run --hours N --workers W` in the background; `hc gh campaign` opens/updates the
   campaign issue (parameter space, day count, budget, links; no medical data).
5. **Check in** every 20-30 min (`/loop` or `Monitor` until `STATE.json.phase` changes): `hc study
   status --json`; apply the reading-guide rules; `hc report`; republish the artifact; `hc export
   --db-rows --since` → `ArtifactData batch`; one status line in chat; at most one comment per round on
   the campaign issue. Do not ping the human.
6. **Resume**: the same `hc study run` command continues from `trials.jsonl`; a stale pid lock with a
   dead heartbeat (> 10 min) is taken over; a live one is refused.
7. **Stop** when: budget used; the Pareto front / review order unchanged for 3 rounds; all parameters
   dead; fidelity floor broken on > 30 % of days; `stale_context` on a new round (finish, stop, say so,
   unless `--on-stale reeval` was agreed); sentinel/null mismatch (immediate, exit 2); three consecutive
   failed trials; disk < 2 GB.
8. **Review**: `validate` phase on the hold-out days; `hc card`; `hc explain` the 10 loops that differ
   most for each card; write the summary in the allowed vocabulary with the VDF of each candidate; publish
   the final page; offer the pin.
9. **Propose** (interactive sittings only, never from a schedule): `hc gh propose <vhash>` opens a
   proposal issue (title, study id, artifact link, human checklist); the human sets `accepted`,
   `rejected` or `applied-on-phone`; the next sitting reads the labels back (`hc gh sync`) and, on
   `applied-on-phone`, writes a `profile_history` reminder into the proposal file — the human edits
   `ns_settings.json`, never the tool.
10. **Daily routine** (phase 2, `schedule` skill, 03:00 local): `hc warm yesterday` once final, `hc
    freeze --new`, re-run the shortlist on it, demote on sign flip, one line on the campaign issue; no
    proposals.
11. **Never**: write to the phone, Nightscout, `ns_settings.json`, `aaps_preferences.json`, VDFs under
    `variants/`; claim glucose outcomes; rank inside the floor; delete `trials.jsonl`; vary a locked key;
    post glucose, insulin or transcript data to GitHub; run two studies against one study directory.
12. **Reporting to the human**: every check-in is one line (trials, phase, floor, best review-order
    item with its verdict); the sitting ends with the cards, the artifact link, the proposal issue links
    and the honesty paragraph.

---

## 8. GitHub issue workflow (`markusbaumg/APS-what-if`, issues enabled, **repository is public**)

**Privacy rule** (the judges' strongest objection to Counterloop/ShadowLoop automation): what
`ns_settings.json` already publishes — preference names and values — may appear in an issue; what
Nightscout holds — glucose values, insulin units per day, loop counts per day, transcript excerpts,
prediction arrays — may not. Issues carry titles, parameter values, study ids, day counts, phase/status
words and links to the private artifact. `lint.py` enforces this on every body `gh.py` posts (regexes for
`U/day`, `mmol`, `mg/dl`, BG-like numbers next to time stamps, `Microbolusing`). If the user makes the
repo private, the rule relaxes to "no transcripts" by a flag in `study.json`.

**Labels** (created by `hc gh labels`): `hindcast` (all tool work), `epic`, `mvp`, `algorithm-fidelity`,
`blocked-core` (needs a change in `emulator_core.py`/`determine_basal.py`, for the maintainers),
`campaign`, `proposal`, `finding`, `accepted`, `rejected`, `applied-on-phone`, plus the existing
`bug`/`documentation`/`enhancement`.

**Issue kinds**: *epic* (one per section of the backlog, task list of its issues, closed when all are);
*task* (one per backlog block, acceptance criteria, files, dependencies); *campaign* (one per study: the
protocol as a table, one comment per round at most, closed by the agent with the final summary);
*proposal* (one per candidate recommended for review: title `Proposal: dura_ISF_weight 0.7 -> 0.5`,
study id, artifact link, the honesty paragraph, a human checklist — reviewed / discussed / applied on
phone on YYYY-MM-DD / added to profile_history; only the human closes it); *finding* (fidelity drop,
saturating cap, dead parameter worth knowing, hand-off to the core maintainers).

**Team**: one agent per epic in section 10 of the backlog; agents coordinate only through issues and
PRs on an integration branch `hindcast` (off `nightscout-source`), never by editing another agent's
module; the contracts (E0) are frozen on day one so the five can work against fixtures. Every PR that
changes an evaluation pastes `hc check` before/after. Commit attribution per the session's rule.

---

## 9. Test strategy

**Regression gate — the no-change replay** (`hc check`, also a pytest): the base replay of the frozen
2026-09-06 06-09Z window (180 loops) must reproduce the golden metrics stored in
`hindcast/tests/golden/2026-09-06T06-09.base.json` (SMB exact ≥ 97 %, within 0.05 U ≥ 99 %, insulinReq
within 0.05 U = 100 %, variable_sens within 1 mg/dl = 100 %, SMB total 2.65 U ± 0.05) and the frozen
replay must equal the live replay **bit for bit** on the CSV. Golden values are re-baselined only by a PR
that names the fidelity issue that moved them (expected moves: items C, F, G of the fidelity epic raise
the agreement). Every PR pastes `hc check` output.

**Unit tests** (`.venv/bin/python -m pytest software/hindcast/tests`, `pytest` added to a
`requirements-dev.txt`):

- `metrics.py` on the five CSVs of `results/example_2026-09-06/`: reproduces the documented table (base
  +1.37 U, `dura_ISF_weight 0.5` → −0.4 U all in rise, 9 loops with a smaller SMB, @max 9.7 % → 9.4 %),
  base ≡ `autoISF_max_1.3__dura_ISF_weight_0.7`; floors; envelope and P on synthetic series; ties.
- `space.py`: VDF text round-trips through `setVariant()`-style parsing; locked keys refused (exit 3);
  ordering constraints; vhash stability.
- `study.py`: resume after `kill -9` mid-trial loses at most one trial; keys dedupe across phases;
  `STATE.json` atomic; lock takeover rules; STOP; stale detection when `code_sha` changes.
- `freeze.py`: frozen `loops()` equals live `loops()` (dict equality) for a cached day; no socket opened
  during a frozen replay (monkeypatched `socket.socket` raises).
- `pool.py`: two workers, four trials, loop counts equal, sentinel passes.
- `search.py`: oaat/grid/lhs produce the documented counts; halving keeps feasibility; Optuna adapter
  skipped when the import fails.
- `lint.py`: every may-not phrase refused, every may phrase accepted; privacy regexes on sample bodies.
- `report.py`: generated page parses as HTML, every `<script src>` host is cdnjs/jsdelivr/unpkg with an
  exact version, embedded JSON parses, `--single-file` size warning triggers at 12 MB, tokens present
  under all three theme guards.
- `templates/page.js`: a node smoke test (`node --check`) plus a headless check of the data contract
  (the page's expected keys vs `contracts.py`), run by the viz agent with the Chrome tools before each PR.

**Fixtures**: `results/example_2026-09-06/`, `variants/*.csv`, one frozen day committed as a test asset
only if under 1 MB after trimming to the 06-09Z window (otherwise generated in CI-less local runs from
the cache).

---

## 10. MVP cut (demonstrable on the cached 2026-08-30..2026-09-07 days)

Built first, in parallel by five agents against the frozen contracts:

- **E0** contracts + fixtures; **E1** freeze, frozen source, replay wrapper, pool, offline guard;
  **E2** metrics, floors, E/P feasibility, Pareto, ranking rule, lint; **E3** study dir, trials.jsonl,
  resume, lock/STOP, sentinel/null, oaat/grid/lhs phases; **E4** the page: overview, parameters tab +
  leaderboard + heatmap, scenario small multiples with the difference strip, a minimal drill-down
  (decision pair + fans + text diff), candidate card, tier split, `--single-file`, `hc serve`; **E5**
  skill v0 (interactive sitting, republish at check-in); **E6** labels + epics + campaign issue + privacy
  lint; **E9** `hc check` and the pytest scaffold.

**Acceptance demo**: `hc study new demo --from-grid variants/grid_autoISF.json --days 2026-09-06
--question explore`, `hc study run demo`, `hc report demo --single-file`; the page shows base ≡ sentinel,
the +4.05 U/day SMB bias and 96.1 % agreement on the banner, `dura_ISF_weight 0.5` as 9 blue stems in the
difference strip in the rising phase of the morning, `autoISF_max 1.3 → 1.5` as "not testable on this day
(factor on the 1.3 cap 9.7 % of loops, reaches 1.5 in 3.8 %)", and the 09:00 loop's `minGuardBG 3.6 <
3.8 — SMB disabled` explanation in the drill-down; a 1-day × 16-variant study completes in under 2 min
with 4 workers; `kill -9` during the run and a re-run finish the study without re-doing done trials.

**Later phases**: Optuna NSGA-II/TPE, halving, hold-out validation and CV (E3 later issues); live `db`
leaderboard, daily `schedule` routine, proposal issues with label read-back (E5/E6 later); full
drill-down (dumbbells, flow list, keyboard), parallel coordinates, days × variants matrix,
weights-sensitivity panel (E4 later); STAIR/time-of-day parameters; logfile source for the other branch;
multi-study comparison (before/after a profile change); core hooks accepted by the maintainers (E8);
the algorithm-fidelity epic (E7) runs in parallel from day one because it raises the floor every study
depends on.
