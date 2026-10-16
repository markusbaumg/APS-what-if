# Hindcast — backlog for `markusbaumg/APS-what-if`

Companion to `DESIGN.md`. One block per issue, ready for `gh issue create -R markusbaumg/APS-what-if
--title ... --label ... --body-file ...`. IDs `H-nnn` are stable references for the dependency lines
(replace with the GitHub numbers when created; the epic bodies carry task lists of their issues).

Conventions for every issue body (not repeated below):
- Branch: integration branch `hindcast` off `nightscout-source`; one PR per issue; never edit another
  agent's module; new code only under `software/hindcast/`, `.claude/skills/hindcast/`,
  `software/hindcast/tests/`, unless the issue says otherwise (`blocked-core` issues touch the core and
  are for the maintainers).
- Every PR that can change an evaluation pastes `hc check` before/after (H-902) and the no-change
  agreement table.
- **Privacy** (repo is public): issue text may name preference keys and values; never glucose values,
  insulin units per day, loop counts per day, transcript excerpts or prediction arrays. Link to the
  private artifact instead.
- The honesty paragraph of DESIGN.md §3 is quoted in every user-facing surface an issue produces.
- Commit messages end with the session's attribution line.

Labels to create first (`hc gh labels` once H-601 exists; by hand before that):
`hindcast`, `epic`, `mvp`, `algorithm-fidelity`, `blocked-core`, `campaign`, `proposal`, `finding`,
`accepted`, `rejected`, `applied-on-phone`.

Team start (five agents, day one, no dependencies between them):
**contracts/integrator** H-001 H-002 H-003 → then E3; **replay** H-101 H-102 (after H-001) H-104;
**metrics** H-201 H-202 H-205 (fixtures exist); **viz** H-401 H-402 (against the fixture JSON of H-003);
**fidelity** H-701 H-702 H-703 H-704 (in the core, coordinated with the maintainers' concurrent fixes:
check `git log -p software/determine_basal.py` first, a concurrent fix may already have landed);
**skill/gh** H-601 H-602 H-501 (text work, no code dependency).

---

## E0 — Contracts and fixtures (epic `H-000`) · labels: hindcast, epic, mvp

Freeze the data contracts on day one so five agents can work against fixtures.

### H-001 · contracts.py: study.json, trials.jsonl row, STATE.json, status.json, round.json · **MVP**
labels: hindcast, mvp
Body:
- Dataclasses + `to_json`/`from_json` + a `validate()` for the five documents of DESIGN.md §4.5;
  JSON Schema files under `software/hindcast/schemas/` generated from the dataclasses.
- `key()` = sha1 over canonical params (sorted, `repr` via `emulator_optimize.vdf_value`), day,
  settings_sha, code_sha, freeze_sha, objective_version; `vhash()` = sha1 over canonical params only.
- `code_sha` = sha1 over `determine_basal.py`, `emulator_core.py`, `ns_*.py` contents; `settings_sha` over
  `ns_settings.json` with `nightscout.token`/`api_secret_sha1` blanked.
Acceptance: round-trip tests; a `study.json` with a locked key fails `validate()` with the key named;
`round.json` for 10 variants serialises under 20 KB. Files: `software/hindcast/contracts.py`,
`schemas/*.json`, `tests/test_contracts.py`. Depends on: nothing.

### H-002 · Series / pred / orig JSON contract and the tier-1/tier-2 split · **MVP**
labels: hindcast, mvp
Body: the columnar formats of DESIGN.md §4.5 (`series/<day>/<variant>.json`, `series/<day>/orig.json`,
`pred/<day>/<variant>.json`, `pred/<day>/orig.json`), rounding rules (BG 1 dp, insulin 3 dp, factors
2 dp, `t` = seconds since UTC day start), `allow_nan=False` with `None` for missing, `</` escaping for
inline embedding, and the mapping table CSV column → series key (from the 43-column header, see
`emulator_optimize.read_result_csv`). Acceptance: a writer and a reader; a generated fixture set for
2026-09-06 06-09Z from `variants/2026-09-06T06-00-00Z_2026-09-06T09-00-00Z.no_change.csv`; size
measured and recorded in the docstring (expect ≤ 90 kB per day-variant, ≤ 1.2 MB pred). Files:
`contracts.py`, `export.py` (writer part), `tests/fixtures/report/`. Depends on: H-001.

### H-003 · Fixture study directory from `results/example_2026-09-06/` · **MVP**
labels: hindcast, mvp
Body: build `tests/fixtures/study_example/` (study.json, trials.jsonl with the five CSV-derived rows,
series/, a status.json and round.json) by a script `tests/make_fixtures.py` that only reads the existing
result CSVs (no replay). This is what the viz agent (E4) and the skill agent (E5) develop against.
Acceptance: `hc report tests/fixtures/study_example` renders once H-401 exists; the trials rows reproduce
the numbers of the documented table (base +1.37 U etc.). Depends on: H-001, H-002, H-201.

### H-004 · prefs_map.json: VDF key → AAPS preference title and unit
labels: hindcast
Body: map every key of the `autoisf`, `smb`, `activity_monitor`, `flags` sections of `ns_settings.json`
and the common `profile` keys to the title shown in the AAPS preferences screen (autoISF 3.2 build) and
its unit (mmol vs mg/dl, %, min). Source: `aaps_preferences.json` keys and the AAPS strings; mark
unknowns `null` rather than guessing. Used by the candidate card ("Show AAPS preference names").
Acceptance: every key in `ns_settings.json` sections resolves or is listed as unmapped in a test.
Files: `software/hindcast/prefs_map.json`, `tests/test_prefs_map.py`. Depends on: nothing.

---

## E1 — Replay engine (epic `H-100`) · labels: hindcast, epic, mvp

### H-101 · freeze.py: frozen day files and FrozenSource · **MVP**
labels: hindcast, mvp
Body:
- `hc freeze <from>..<to> [--new] [--allow-partial] [--list]`: for each UTC day run
  `ns_source.NightscoutSource('ns:', day_start, day_end).load()` then `.loops()` and write
  `frozen/<host>/<day>.loops.jsonl.gz` + `<day>.meta.json` (loop count, sha1, `ns_code_sha`,
  `settings_sha`, final flag from `ns_client`'s rule, dropped-loop counters, day character record,
  confounders from `profile_history`, CGM gaps, TT parity SMB-off share, duplicate uploads). Refuse
  non-final days unless `--allow-partial`; `--new` freezes only missing days and is what `run` calls at
  most hourly.
- `FrozenSource(fn, start_iso, end_iso, log_msg=None)`: `load()` no-op; `loops()` yields stored dicts with
  `deliverAt` inside the window in order; exposes the same end-of-run counters `ns_source` prints.
- A frozen file whose `ns_code_sha` or `settings_sha` no longer matches is reported `stale` by `--list`
  and refused by `run` unless `--refreeze`.
Acceptance: frozen `loops()` == live `loops()` (dict equality) for 2026-09-06 06-09Z; a day freezes in
< 60 s; gz size recorded (expect ~5 MB/day). Files: `freeze.py`, `days.py` (character/confounders),
`tests/test_freeze.py`. Depends on: H-001.

### H-102 · replay.py: run_trial() around parameters_known() with the frozen source · **MVP**
labels: hindcast, mvp
Body: install `FrozenSource` as `ns_source.NightscoutSource` on the real module object; write the VDF
(`emulator_optimize.write_vdf` with the base lines) into a per-worker temp dir; call
`parameters_known('ns:', 'Windows/'+outputs+'/noplot/.', vdf, start, end, {}, msg, '.',
datetime(1970,1,1))` with stdout redirected to `runs/<key>.out`; map return status `SYNTAX`/`UTF8`/`Z` →
`failed` with the captured `sub_issue` text; read the CSV via `emulator_optimize.read_result_csv`; read
`emulator_core.Pred` and `FlowChart` immediately (guarded: missing → `flags: ["no_pred"]`); write series
(always) and pred (when asked); delete `.txt/.delta/.log/.orig.txt` unless `--keep-transcripts`.
Acceptance: metrics of a frozen replay equal the live replay's CSV bit for bit on 2026-09-06 06-09Z;
no socket is opened during a frozen replay (monkeypatched `socket.socket` raises → test passes);
a VDF with a syntax error yields `status: failed` and the study continues. Files: `replay.py`,
`tests/test_replay.py`. Depends on: H-001, H-101.

### H-103 · pool.py: spawn worker pool, one emulator_core per process · **MVP**
labels: hindcast, mvp
Body: `multiprocessing` with the `spawn` start method; worker init sets `PYTHONUTF8=1`, imports
`emulator_core`, calls `set_tty(0, 0, 'batch')`, installs the frozen source; `max_tasks_per_child`
(default 200); `os.nice(5)`; `--workers` capped at `cpu_count()-1`; tasks are (variant, day) pairs;
results stream back as they finish; a worker exception is one `failed` row, not a crash.
Acceptance: 2 workers × 4 trials give equal loop counts and identical metrics to a serial run; memory of
a worker stays flat over 100 trials (RSS logged). Files: `pool.py`, `tests/test_pool.py`.
Depends on: H-102.

### H-104 · hc warm: serial cache fill, final-day guard, offline mode · **MVP**
labels: hindcast, mvp
Body: `hc warm <from>..<to> [--allow-partial]` walks the days serially through
`ns_client.NightscoutClient` (its 1.5 s back-off stays), refuses days not yet final (written > 1 h
after the UTC day ended) unless asked; sets `HINDCAST_OFFLINE=1` for everything else; `replay.py`
checks before each trial that the day is frozen and final, else `status: blocked`. Document the
rate: one request stream, never two studies warming at once. Acceptance: a second `hc warm` of the
same days performs zero network requests (count via a wrapped `urlopen`). Files: `cli.py`,
`days.py`. Depends on: H-001.

### H-105 · Benchmark: frozen vs live replay cost, recorded in HINDCAST.md
labels: hindcast
Body: measure per-day-variant seconds for live and frozen replay with 1 and 4 workers on 2026-09-06;
record in `software/HINDCAST.md` (H-901) and in `hc study status` ETA. Expect ~2-3 s frozen. Also
profile the frozen path once (`cProfile`) and list the top 5 — the file-open/close of the transcripts
and the CSV write are the candidates for the `quiet` core request (H-802). Depends on: H-103.

---

## E2 — Metrics, objective, lint (epic `H-200`) · labels: hindcast, epic, mvp

### H-201 · metrics.py: optimizer metrics + Hindcast additions, OBJECTIVE_VERSION 1 · **MVP**
labels: hindcast, mvp
Body: import `emulator_optimize.metrics_from_csv`; add `changed_loops`, `changed_vs_base`,
`rise_vs_base_U`, `rest_vs_base_U`, `lowside_flags` (emulated `minGuardBG` from pred/Levels when present,
else recorded-BG rule only, flagged), `prehypo_extra_U` (P), `env_3h_max_U`, `env_day_pct_tdd` (E),
`zero_temp_vs_base`, `cap_binding_pct`, `max_single_smb_U` vs the phone's `maxBolus`, `tt_smb_off_pct`,
`agreement_pct`; per-loop hindsight covariates (low within 2 h, high within 3 h) stored positionally in
the series, never scored. Acceptance: the five fixture CSVs reproduce the documented table exactly;
synthetic series tests for P and E (a +0.5 U SMB 90 min before a 3.5 mmol reading → P = 0.5; a day-long
+0.02 U/loop drift → `env_3h_max_U` = 3.6 ...). Files: `metrics.py`, `tests/test_metrics.py`.
Depends on: nothing (fixtures exist).

### H-202 · Noise floor, ties, feasibility, Pareto, ranking rule, review order · **MVP**
labels: hindcast, mvp
Body: `floor_X = |X_base − X_phone|` per day and component, `floor_loops = changed_loops(base)`;
tie = `< 2×floor`; feasibility (P, E, maxBolus, cap < 50 %, equal loop counts); Pareto front for
`earlier`/`gentler`; review order for `explore`; the scalarisation with weights from `study.json`;
`recommended_for_review` rule (feasible all days, top-3 on ≥ 80 % folds, sign ≥ 80 % days, hold-out top 5);
the printed ranking-rule text. Acceptance: unit tests on constructed trial sets (a variant inside the
floor on every component is a tie with base; an out-of-envelope variant is never in the front). Files:
`metrics.py` (ranking part), `tests/test_ranking.py`. Depends on: H-201.

### H-203 · Weights-sensitivity computation
labels: hindcast
Body: for a study with a scalar in use, recompute the ranking under ±50 % of each weight and report the
rank range per variant for the page panel. Depends on: H-202.

### H-204 · Day quarantine and fidelity floor
labels: hindcast, mvp
Body: in `days.py`: a day is quarantined when base `agreement_pct < 94 %`, or a CGM gap > 2 h, or a
`profile_history` change inside the day for a searched key, or loop counts differ across variants;
quarantined days are listed with the reason on the page and excluded from scoring; thresholds in
`study.json`. Depends on: H-201, H-101.

### H-205 · lint.py: vocabulary and privacy lint · **MVP**
labels: hindcast, mvp
Body: the may/may-not list of DESIGN.md §3 as data (`vocabulary.json`, shared with the skill's
`references/vocabulary.md` by generation); `lint_text(kind='card'|'proposal'|'issue')` raises with the
offending phrase; the privacy regexes for `kind='issue'` (`U/day`, `mmol`, `mg/dl`, BG-like numbers next
to a time, `Microbolusing`, `predBGs`). Acceptance: tests for every phrase in both lists; a sample card
passes; the same card with "safer" fails. Files: `lint.py`, `vocabulary.json`, `tests/test_lint.py`.
Depends on: nothing.

### H-206 · Candidate card generator (text + VDF + preference names) · **MVP**
labels: hindcast, mvp
Body: `hc card`: one Markdown card per candidate (DESIGN.md §6 page 5): current → proposed values with
AAPS preference names (H-004), days replayed, `rise_vs_base_U`/`rest_vs_base_U` with the floor, loops
changed by direction, P and E status, cap-binding share, max single emulated SMB, per-stratum rank,
hold-out rank, ties ("not distinguishable from candidate 3"), the honesty paragraph, the VDF text; passes
`lint.py` or is not written. Acceptance: fixture study yields a card for `dura_ISF_weight 0.5` whose
wording matches the documented example. Files: `export.py`, `tests/test_card.py`. Depends on: H-202,
H-205, H-004.

---

## E3 — Study runner and search (epic `H-300`) · labels: hindcast, epic, mvp

### H-301 · study.py: study dir, trials.jsonl append, STATE.json, lock, STOP, resume · **MVP**
labels: hindcast, mvp
Body: `hc study new` (writes study.json, refuses locked keys with exit 3, imports `--from-grid`);
`trials.jsonl` append with `O_APPEND` single write; `STATE.json` by atomic rename after every trial;
pid + heartbeat lock (takeover if heartbeat > 10 min old); `STOP` file; `hc study stop`; resume = skip
keys with `status: done`; exit codes 0/2/3; `hc study status [--json]` ≤ 2 KB. Acceptance: `kill -9`
mid-trial then re-run loses at most one trial (test with a 20-trial grid); two concurrent `run`s on one
study: the second exits 3. Files: `study.py`, `cli.py`, `tests/test_study.py`. Depends on: H-001.

### H-302 · Sentinel, null variant, equal-loop-count guards, stale_context · **MVP**
labels: hindcast, mvp
Body: sentinel trial (phone values as explicit rows from `ns_settings.json` with `profile_history` at
each day) every `sentinel_every` trials and at each phase start; null variant once per round; both must
equal base to the loop → else exit 2 with the differing loop listed in `status.json`; `code_sha` /
`settings_sha` / `freeze_sha` recorded per row, mismatch vs `study.json` → `stale_context`; `--on-stale
stop` (default: finish the round, write status, exit 2) or `reeval` (mark rows stale, re-run base first,
then lazily). Acceptance: tests that mutate a frozen file's meta or `ns_settings.json` between rounds.
Files: `study.py`, `tests/test_guards.py`. Depends on: H-301, H-102.

### H-303 · space.py: parameter space, step grid, constraints, locked list, VDF text · **MVP**
labels: hindcast, mvp
Body: types float (low/high/step), int, bool, choice; ordering constraints (`min ≤ ratio ≤ max` sampled
as ratio + half-widths); the locked list (DESIGN.md §3) enforced with no override; VDF text via
`emulator_optimize.write_vdf`; `variant_name` compatible with the optimizer's. Tiers A/B of ShadowLoop
§4 as a documented default space file `software/hindcast/spaces/autoisf_tierA.json` with the phone's
current values from `ns_settings.json` as the centre. Depends on: H-001.

### H-304 · search.py phases: oaat, grid, lhs; dead-parameter freezing · **MVP**
labels: hindcast, mvp
Body: `next_batch(study, trials) -> list[(params, day)]` interface; oaat (5-7 levels around the phone's
value), grid (cartesian, `--from-grid`), lhs (n points, seeded); after oaat: parameters with all points
inside 1.5 × floor on all screening days are frozen and reported "not testable on these days" in
`round.json`. Acceptance: counts match the optimizer's `combinations()` for grid and oaat; lhs is
reproducible by seed. Files: `search.py`, `tests/test_search.py`. Depends on: H-303, H-202.

### H-305 · hc study run: the orchestrator (phases, pool, budget, reporting cadence) · **MVP**
labels: hindcast, mvp
Body: runs phases in order, hands tasks to the pool, appends rows, applies guards, honours `--hours`,
`--until HH:MM`, `--workers`, disk check (< 2 GB stops, exit 2), three consecutive failures stop,
regenerates tier 1 (`export.py`) every 10 trials and on exit, writes `round.json` per round and
`status.json` every trial; calls `hc freeze --new` at most hourly when `grow` is on. Acceptance: the
MVP demo of DESIGN.md §10 runs end to end in < 2 min with 4 workers on 2026-09-06. Depends on: H-301..304,
H-103, H-401.

### H-306 · Successive halving across days
labels: hindcast
Body: rungs from `study.json` (`[2, 5, "all"]`), typical days = nearest the median character record,
keep top third per rung, feasibility at every rung, rows carry `rung`. Depends on: H-305, H-204.

### H-307 · Optuna adapter (NSGA-II / TPE), journal storage, rebuild from trials.jsonl
labels: hindcast
Body: optional import (`pip install optuna` into `.venv`; verify it runs on Python 3.14, else document
the fallback); `JournalStorage` file backend in the study dir; ask-and-tell with each suggestion run on
all search days before `tell`; step-grid sampling; journal rebuilt from `trials.jsonl` when missing or
inconsistent; `coordinate` descent as the stdlib fallback phase. Depends on: H-305.

### H-308 · Validate phase: hold-out days, leave-one-day-out, rank stability
labels: hindcast
Body: `role: holdout` days never enter search phases; `validate` re-runs the top-N on them; Kendall τ and
per-variant rank range over LODO folds; `recommended_for_review` computed; results into `status.json`
and the cards with the day count as the caveat. Depends on: H-305, H-202.

### H-309 · Grow mode: --until-stable, new final days, shortlist re-test, demotion
labels: hindcast
Body: `hc freeze --new` hourly, top-10 re-evaluated on new days first, demote on sign flip, restart the
Optuna phase only when the front moved beyond the floor; `--until-stable N` rounds. Depends on: H-307,
H-308.

---

## E4 — Dashboard (epic `H-400`) · labels: hindcast, epic, mvp

Before any template work: load the `artifact-design` and `dataviz` skills; ECharts **6.1.0** from cdnjs
is the only external script; palette = dataviz reference palette as tokens under the three theme
guards; table twin for every chart; no dual axis; at most three variant colours.

### H-401 · Page skeleton, tokens, theme builder, data loading, report.py generator · **MVP**
labels: hindcast, mvp
Body: `templates/page.html|css|js|theme.js`; `<title>` = study name; tokens on `:root` with dark under
`@media (prefers-color-scheme: dark)` guarded `:root:not([data-theme="light"])` and `:root[data-theme="dark"]`;
`body` background; 16 px gutters, no horizontal scroll at 400 px; ECharts theme from `getComputedStyle`,
rebuilt on `matchMedia` change; tier-1 JSON embedded in `<script type="application/json">`; tier-2
`fetch()` relative with a "not available from disk" fallback; `report.py` with `string.Template`,
`--single-file` inliner (12 MB warning), allowed-host and exact-version check on every `<script src>`;
`hc serve`. Acceptance: renders the fixture study (H-003) light and dark at 400 and 1200 px (screenshots
attached to the PR via the Chrome tools); size printed. Depends on: H-002 (fixture JSON); develops
against H-003.

### H-402 · Study overview view · **MVP**
labels: hindcast, mvp
Body: honesty paragraph (non-collapsible), noise-floor banner with computed numbers, day strip cards
(character record, confounder badges, quarantine reason, role, user chips), progress/freshness tiles
(`stale_context` shown), "cannot be tested" list, `_assumed` keys. Stat tiles in plain HTML. Depends on:
H-401.

### H-403 · Parameter space: parameters tab, leaderboard, delta heatmap · **MVP**
labels: hindcast, mvp
Body: parameters tab (one small panel per parameter, x = value, y = effect vs base per day, floor band);
leaderboard table (every component, floor band in delta columns, feasibility reason, ranking rule and
question printed above, review-order header for `explore`); delta heatmap for two chosen parameters
(diverging, grey at zero, base cell outlined, 2 px gaps, labels only when wide). Table twin for each.
Depends on: H-401.

### H-404 · Scenario comparison: small multiples with linked crosshair and the SMB difference strip · **MVP**
labels: hindcast, mvp
Body: panels glucose (target bands, carbs, TT/profile-switch hairlines, lows shaded positionally), final
ISF factor with cap hairline, insulinReq, SMB stems + difference strip (diverging, grey inside the floor),
TBR in force, IOB with iobTH and tolerance; one shared axis, `axisPointer.link`, shared `dataZoom`;
picker with three entity slots and refusal of a fourth; day toggle (columns per day for one variant);
legend always; direct labels at line ends. Depends on: H-401, H-002.

### H-405 · Loop drill-down, minimal: decision pair, prediction fans, text diff · **MVP**
labels: hindcast, mvp
Body: side sheet (full screen on phone) on click of a loop column or table row; header; decision pair with
3 px stripes on differing cells; both prediction fans on one axis with 3.8/5.4 mmol hairlines; word-level
diff of reason/consoleError; positional "a low followed at HH:MM" badge; lazy load of `pred/`. Depends on:
H-404.

### H-406 · Drill-down, full: ISF-factor dumbbells, flow list, keyboard navigation
labels: hindcast
Body: inline-SVG dumbbells (acce, bg, pp, dura, drift → final), the `Flows` boxes as a collapsed indented
list with JS refs, `←/→` and `[`/`]` keys, `#loop-<epoch>` deep link, `localStorage` for the last tab only
(try/catch). Depends on: H-405.

### H-407 · Parallel coordinates with brush, Pareto scatter, days × variants matrix
labels: hindcast
Body: `parallel` series (grey 1 px lines, front members in accent, base dashed), brushing filters the
leaderboard; Pareto scatter (base as hollow ring, infeasible greyed with reason, size = days); days ×
variants heatmap with day-character strip and quarantined days greyed; parcoords replaced by the table at
phone width. Depends on: H-403.

### H-408 · Candidates view and weights-sensitivity panel
labels: hindcast
Body: candidate cards (from H-206) with Copy VDF and preference names toggle, no apply button; GitHub
proposal state from `status.json`; round log; weights-sensitivity rank ranges (H-203). Depends on: H-206,
H-203, H-401.

### H-409 · Live mode: `db` capability, leaderboard subscription, fallback
labels: hindcast
Body: load the `artifact-capabilities` skill first; page declares `capabilities: {db: {}}`; when
`claude.use("db")` resolves, subscribe to `studies/<id>/trials` and merge rows into the leaderboard with a
"live rows via Claude, last push HH:MM" chip; `db === null` → embedded tier 1 only; rules `read: view`,
`write: owner`. Depends on: H-403, H-504.

---

## E5 — Claude skill and live artifact (epic `H-500`) · labels: hindcast, epic, mvp

### H-501 · SKILL.md v0 and references (interactive sitting) · **MVP**
labels: hindcast, mvp
Body: `.claude/skills/hindcast/SKILL.md` with triggers ("optimize settings", "what-if study", "which
autoISF setting", "/hindcast"), the 12-step protocol of DESIGN.md §7.3, the never-list, check-in cadence;
`references/protocol.md`, `vocabulary.md` (generated from `vocabulary.json`), `parameters.md`,
`reading-guide.md`, `github.md`. Source lives in `software/hindcast/skill/`, installed by `hc skill
install` (copy). Acceptance: a dry run of the skill against the fixture study produces the expected
check-in line and card without reading any CSV. Depends on: H-205 (vocabulary); text otherwise
independent.

### H-502 · status.json / round.json producers and `hc explain`
labels: hindcast, mvp
Body: `status.json` ≤ 2 KB and `round.json` ≤ 2 KB per variant with `verdict`
(dead/candidate/tie/infeasible/recommended) and `why`; `hc explain <study> <iso> --variant <vhash>` prints
the drill-down as text (decision pair, factor chain, diff of reasons) from the series/pred files.
Depends on: H-301, H-202, H-002.

### H-503 · Check-in automation: Monitor/loop recipe, republish, one-line status
labels: hindcast
Body: the skill's check-in step as a reference recipe: `Monitor` until `STATE.json.phase` changes or
`/loop 20m`; `hc report`; Artifact republish to the same URL; `hc export --db-rows --since`; the one-line
chat format; the "at most one campaign comment per round" rule. Depends on: H-501, H-305.

### H-504 · `hc export --db-rows` and the ArtifactData batch push
labels: hindcast
Body: rows for `studies/<id>`, `studies/<id>/trials/<key>` (no `files`), `studies/<id>/cards/<rank>`;
`--since <iso>` for incremental pushes; the skill pushes with one `ArtifactData batch` per check-in.
Depends on: H-301, H-206.

### H-505 · Daily routine (schedule skill): warm yesterday, freeze --new, re-test shortlist
labels: hindcast
Body: a `schedule` routine definition at 03:00 local: `hc warm` yesterday once final, `hc freeze --new`,
re-run the shortlist, demote on sign flip, one line on the campaign issue, never a proposal. Depends on:
H-309, H-603.

---

## E6 — GitHub workflow (epic `H-600`) · labels: hindcast, epic, mvp

### H-601 · hc gh labels|epics: create labels and the epic issues from this backlog · **MVP**
labels: hindcast, mvp
Body: `gh.py` via the `gh` CLI; idempotent label creation; epic issues with task lists referencing the
created task issues; `hc gh issues --from BACKLOG.md` creates every block below once (dedupe by title).
Depends on: nothing.

### H-602 · Campaign issue and round comments with privacy lint · **MVP**
labels: hindcast, mvp
Body: `hc gh campaign <study>` opens/updates one issue per study (protocol table: parameters and ranges,
day count, question, budget, artifact link; no medical data); `hc gh round <study>` posts at most one
comment per round (phase, trials done, dropped parameters, verdict words, link); every body passes
`lint.py` kind `issue`. Depends on: H-205, H-301.

### H-603 · Proposal and finding issues; label read-back into the study
labels: hindcast
Body: `hc gh propose <study> <vhash>` (title `Proposal: <key> <from> -> <to>`, study id, artifact link,
honesty paragraph, human checklist; opened only interactively); `hc gh finding` (fidelity drop,
saturating cap, dead parameter); `hc gh sync` reads `accepted`/`rejected`/`applied-on-phone` back into
`status.json` and, on `applied-on-phone`, writes a `profile_history` reminder into `proposals/<vhash>.md`
(never into `ns_settings.json`). Depends on: H-602, H-206.

---

## E7 — Algorithm fidelity (epic `H-700`) · labels: hindcast, epic, algorithm-fidelity

From `REVIEW.md` (autoISF fork `kusi/3.4.2.6-backports` vs `determine_basal.py`) and the mismatch list in
`NIGHTSCOUT_SOURCE.md`. These touch `software/determine_basal.py`, `emulator_core.py`, `ns_*.py` — the
files other agents are fixing concurrently: **before starting, check `git log -p` and the working tree
for a fix that already landed**, and coordinate on the issue. Every PR: bump the version stamp (keep
the old line as `#cho_msg`), preserve CRLF in `determine_basal.py`, paste the before/after no-change
agreement table for 2026-09-06 (full day) and `hc check`. Expected effect: items 701-703 remove the
three decision-level divergences and should raise SMB agreement above 97 %.

### H-701 · SMB interval tolerance 12 s → 6 s (REVIEW item G) · **MVP** (raises the floor)
labels: algorithm-fidelity, blocked-core
Body: `determine_basal.py` ~l. 2268 `lastBolusAge > SMBInterval - 12` → `- 6.0` (Kotlin
`DetermineBasalAutoISF.kt:1110`). With `smbinterval = 1` and 1-min loops this is the dominant SMB-total
bias (35 loops, +4 U/day on 2026-09-06). Acceptance: SMB exact agreement on 2026-09-06 rises; the
"Waiting 0m Ns" loops now match; golden re-baselined in the same PR naming this issue.

### H-702 · normalTarget 100 → 99 for autoISF ≥ 3.2.0 / AAPS 3.4.2.1+ (REVIEW item C)
labels: algorithm-fidelity, blocked-core
Body: `determine_basal.py` ~l. 905 `normalTarget = fullBasalTarget` (100) → 99 (`Constants.NORMAL_TARGET_MGDL`),
in the exercise/resistance mode tests and in the ratio formula `c = half − 99`. Then restore
`flags.low_temptarget_lowers_sensitivity` to the export value `true` and drop the `_overrides` entry in
`ns_settings.json` (the `_assumed` note explains: TT 5.5 mmol = 99 is no longer "below normal"), verify
with `ns_profile.py --validate` over the cached week (779 TT-5.5 loops must still print no resistance
ratio). Do **not** change the `enable_smb` high-TT test `> 100` (item D).

### H-703 · iobTH_reduction_ratio uses sensitivityRatio incl. autosens (REVIEW item F)
labels: algorithm-fidelity, blocked-core
Body: `determine_basal.py` ~l. 963 `profile_percentage/100 * exercise_ratio * activityRatio` → the Kotlin
`profile_percentage/100 * sensitivityRatio` (TT ratio, or activity ratio, or autosens ratio). Affects the
effective iobTH and the Full-loop SMB cap when `iob_threshold_percent != 100` (phone: 80) and autosens
≠ 1. Acceptance: `eff. iobTH emul` column matches the phone's printed iobTH line on days with autosens ≠ 1.

### H-704 · Six division-by-zero guards (REVIEW item H)
labels: algorithm-fidelity, blocked-core
Body: mirror Kotlin fallbacks: `totalCA` (csf ≤ 0 → 0.0, ~l. 1383), `remainingCIpeak` (remainingCATime
≤ 0 → 0.0, ~l. 1395), `fractionCarbsLeft` (carbs == 0 → 0.0, ~l. 1622; today undefined on the first
carb-free loop in the `minUAM<999 and minCOB<999` branch), `blendPct` (target ≤ threshold → 0.5,
~l. 1701), `carbsReq` (csf ≤ 0 → 0, ~l. 1888), `roundSMBTo` (bolus_increment ≤ 0 → 10.0, ~l. 2194).
Acceptance: a synthetic loop with carbs = 0 in that branch no longer raises NameError.

### H-705 · Echo parity: "Sensitivity ratio unchanged: 1.0" and the activity-monitor suffix (items E, J)
labels: algorithm-fidelity, blocked-core
Body: add `console_error('Sensitivity ratio unchanged: 1.0')` before `Autosens ratio:` in the no-TT/no-steps
branch (~l. 955-959); drop the `, sensitivity ratio unchanged: X` suffix on "Activity monitor detected
neutral state" (~l. 763). Text only; the word-diff in the drill-down becomes clean. Acceptance: transcript
diff of a quiet night loop shows no echo-only differences.

### H-706 · Unit-aware scrapers: scanConsoleError() reads kusi mmol lines (REVIEW §4.1), echo in user units (item I)
labels: algorithm-fidelity, blocked-core
Body: `emulator_core.scanConsoleError()` ~l. 771-779 multiplies `ISF unchanged:` / `ISF from a to b` /
`profile.sens: ..., sens: ..., CSF:` by `MMOL_TO_MGDL` when `out_units == 'mmol/L'` (as `ns_profile.py:723-727`
already does); `determine_basal.py` prints the same BG-type values with `convert_bg()` for parity. Note
the `mmol\/L` vs `mmol/L` trap from zipped logs (CLAUDE.md). Acceptance: a kusi-build consoleError block
yields the right `origISF` when `variable_sens` is absent.

### H-707 · ns_glucose dura by-pass regex: `bg is only X m at level`
labels: algorithm-fidelity
Body: `ns_glucose.py:736` regex cannot match `dura_ISF by-passed; bg is only $dura05 m at level $avg05`
(space before `m`); fix and re-run `ns_glucose.py --validate`; record the new agreement in
`NIGHTSCOUT_SOURCE.md`.

### H-708 · Logfile scanner accepts `AndroidAPS_N.log` basenames; 7-day retention note
labels: algorithm-fidelity
Body: backport `61ab049b5e` names the active log `AndroidAPS_1..9.log`; the glob/basename logic in
`scanLogfile()`/`parameters_known()` must accept it; document that the phone keeps 7 days of hourly zips
(`b1223f259b`) in "The AAPS Logfile System" notes. Logfile path only (other branch too).

### H-709 · Verify ns_meal UAM Impact scaling for mmol text (REVIEW §4.5)
labels: algorithm-fidelity
Body: `ns_meal.py:282/317` accepts `UAM Impact: X (mmol|mg/dL) per 5m`; confirm the printed mmol value is
scaled by 18 before comparison with `uci`; add a test line pair; `ns_meal --validate` numbers unchanged
or better.

### H-710 · Zero-temp cap in the SMB branch: 120 min → 60 min (NIGHTSCOUT_SOURCE mismatch list)
labels: algorithm-fidelity, blocked-core
Body: 162 of 276 mismatching loops on 2026-09-06 are the zero-temp duration (fork caps at 60 min in the
SMB branch, `determine_basal.py` at 120; rate equal). Verify against the fork source line first, then
mirror. Acceptance: TBR duration agreement rises; rate agreement unchanged.

### H-711 · Kotlin Math.round (half up) vs Python round (half even) at the scraped/rounded points
labels: algorithm-fidelity, blocked-core
Body: 201 rounding-only mismatches (e.g. minZTUAMPredBG 104.5 → 105 vs 104 → insulinReq 0.10 vs 0.09).
Introduce a `kround()` helper used where the JS/Kotlin rounds (`Math.round`), keeping `round_basal()`
semantics; measure the agreement change; the `#//` JS comments mark the places. Risk: wide diff in a
CRLF upstream file — coordinate with the maintainers before starting.

### H-712 · Note the UKF smoother and 3.2.1 upgrade date as a confounder badge
labels: algorithm-fidelity, hindcast
Body: loops before the phone's upgrade to 3.4.2.6 used exponential smoothing, after it UKF (item L);
`days.py` adds a confounder badge "smoother changed" when a study spans the upgrade date (from
`autoISF_version`/`start AutoISF 3.2.1` echo in devicestatus). No core change.

---

## E8 — Core requests for the maintainers (epic `H-800`) · labels: hindcast, epic, blocked-core

Hindcast runs without these (it reads globals and swaps a module attribute); they make the dependency
explicit and are offered as small PRs for the owners of `emulator_core.py`.

### H-801 · `emulator_core.last_run_artifacts()` accessor for Pred, FlowChart and the per-loop lists
labels: blocked-core, hindcast
Body: one function returning a dict of the module-global per-loop lists (`loop_mills`, `orig*`,
`emul*`, `Pred`, `FlowChart`) after `parameters_known()`; no behaviour change; Hindcast's `replay.py`
prefers it when present and falls back to the globals. Bump the version stamp.

### H-802 · `quiet` option token: skip `.txt`/`.delta`/`.orig.txt` writes
labels: blocked-core, hindcast
Body: a `featured('quiet')` test around the transcript writers (CSV and `.log` still written). Saves
~3.5 MB of churn per day-variant and the file open/close time H-105 measures. Must not be enabled by
`All` (like `noplot`). Bump the stamp; update CLAUDE.md's option list.

### H-803 · `loop_source_factory` hook in scanNightscout()
labels: blocked-core, hindcast
Body: module-level `loop_source_factory = None`; when set, `scanNightscout()` calls it instead of
`ns_source.NightscoutSource(...)`. ~8 lines, default behaviour unchanged; replaces Hindcast's attribute
swap. Depends on: H-101 (to retire the swap).

---

## E9 — Docs, regression gate, tests (epic `H-900`) · labels: hindcast, epic, mvp

### H-901 · software/HINDCAST.md (contract like NIGHTSCOUT_SOURCE.md), CLAUDE.md section, README paragraph
labels: hindcast, documentation
Body: module contracts, file formats, CLI, the honesty paragraph, the measured costs (H-105), the
"where this and the code disagree, the code wins" rule; a CLAUDE.md section (commands, traps: module
attribute swap, globals read, locked list, privacy rule); README paragraph; `.gitignore` additions
(`software/studies/`, `software/frozen/`). Depends on: H-305, H-401 (to document what exists).

### H-902 · hc check: the no-change replay as regression gate with golden metrics · **MVP**
labels: hindcast, mvp
Body: `hc check [--day]` replays base on the frozen 2026-09-06 06-09Z window, compares with
`tests/golden/2026-09-06T06-09.base.json` (thresholds of DESIGN.md §9), asserts frozen == live CSV bit
for bit, prints a one-screen table for PR descriptions; `pytest` wrapper; re-baseline only via
`--write-golden` in a PR that names the fidelity issue. Depends on: H-102.

### H-903 · pytest scaffold, requirements-dev.txt, fixture policy
labels: hindcast, mvp
Body: `software/hindcast/tests/conftest.py` (paths, fixture study, skip-if-no-cache markers),
`requirements-dev.txt` (`pytest`; `optuna` optional), a `make test`-style one-liner in HINDCAST.md; CI is
not available, so document the local command. Depends on: nothing.

### H-904 · Resumability test: kill -9 during a 4-worker run
labels: hindcast
Body: integration test that starts `hc study run` as a subprocess, kills it after N trials, re-runs,
asserts no duplicate keys, at most one lost trial, identical final metrics to an uninterrupted run.
Depends on: H-305.

### H-905 · Page checks with the Chrome tools before each E4 PR
labels: hindcast, documentation
Body: a checklist and script for the viz agent: open `hc serve`, screenshot light/dark at 400/1200 px,
read console for errors, verify the table twins exist, verify no `<script src>` outside the allowed hosts.
Depends on: H-401.

---

## Later (not scheduled; one issue each when picked up)

- **H-L1** STAIR/INTERPOL time-of-day parameter type (2-4 knots) with a 24 h ribbon on the card.
- **H-L2** Logfile source for Hindcast (branch `A3.4.0.0-ai3.2.0`'s `scanLogfile` path; same
  `parameters_known` call; freeze from parsed logs).
- **H-L3** Multi-study comparison on one page (before/after a profile change).
- **H-L4** uPlot week-long per-loop strip for phones (jsdelivr add-on).
- **H-L5** Export of a candidate as an annotated AAPS preferences *diff* (never an import).
- **H-L6** Day-type clustering from the character record as a suggestion for user chips (never applied
  automatically).

---

## Dependency order for the first sitting

```
day 1   H-001 H-201 H-205 H-004 H-601 H-903 H-701 H-702 H-703 H-704        (no dependencies)
        H-101 H-104 H-303 H-301 H-002                                      (need H-001 only)
day 2   H-102 H-202 H-003 H-401 H-304 H-602 H-902
day 3   H-103 H-302 H-206 H-402 H-403 H-404 H-502 H-204
day 4   H-305 H-405 H-501 H-105 H-901                      -> MVP demo (DESIGN.md §10)
then    H-306 H-307 H-308 H-309 H-406 H-407 H-408 H-409 H-503 H-504 H-505 H-603 H-705..H-712 H-801..H-803 H-904 H-905
```
