# ShadowLoop — a settings-optimization studio on top of the AAPS what-if emulator

Proposal, optimizer-first angle. Repo `/Users/markus/dev/APS-what-if`, branch `nightscout-source`.
Author: design subagent (data science / optimization). Date 2026-10-16.

Everything below was checked against the code and against two measurement runs made for this proposal
(numbers in section 5). No repo file was touched; measurement outputs are under
`/private/tmp/claude-501/-Users-markus-dev-APS-what-if/446128db-e088-4708-985b-76ba5a068a15/scratchpad/design/optimizer-first/{timing,profile}/`.

---

## 0. One paragraph

ShadowLoop turns the emulator from "one VDF, one PDF" into a resumable optimization studio: it freezes the
reconstructed loop inputs of each Nightscout day once, replays thousands of settings variants against them
(decision replay costs ~1.4 ms per loop, the reconstruction it reuses costs ~24 ms per loop), scores every
variant with a paired, hindsight-labelled, hypo-asymmetric objective that is computed per loop against the
phone's own decision and the glucose that actually followed, cross-validates the ranking across days, and
streams every evaluation into a SQLite store from which a self-contained Plotly HTML dashboard (and, when run
from Claude Code, a live Artifact) is generated. A Claude skill drives the staged loop (freeze → noise floor →
sensitivity screen → coordinate/TPE search → confirmation → review page) for hours unattended. The product
ends with a recommendation page for a human to read; it never changes the phone, and it says on every page
what the replay can and cannot claim.

---

## 1. Names (collision check done with WebSearch on 2026-10-16)

| name | why | collision check |
|---|---|---|
| **ShadowLoop** (recommended) | a shadow loop that runs beside the real one with other settings; "shadow mode" is how control engineers describe exactly this kind of offline replay | no diabetes/AID product or app of that name found; search only returned Tidepool Loop |
| **Hindsight** ("Hindsight for AAPS") | names the scoring idea: every decision is judged with the glucose that actually followed | no diabetes software of that name found; generic English word, so not ownable, but no confusion |
| **Loopwright** | the craftsman who tunes a loop; memorable, unique | nothing found |
| **Otherwise** | "what the loop would have done otherwise"; works as a CLI verb (`otherwise run ...`) | nothing in the diabetes space; very generic word, hard to search for |
| **Replay Studio** | descriptive fallback | descriptive, not ownable |

Package/CLI name used below: `shadowloop` (`python -m shadowloop <command>`). Swap the string if another name wins.

---

## 2. What the product is, and what it is not (the contract on every page)

1. **It compares decisions, not outcomes.** For every historical loop run it asks "with settings V, what
   would the algorithm have asked for at this minute, given the glucose, IOB and COB the phone actually had".
   It cannot simulate how glucose would have reacted to a different dose.
2. **Every number is paired and relative.** A variant is scored loop by loop against (a) the phone's actual
   decision and (b) the base replay (same settings as the phone, same emulator). The base replay's
   disagreement with the phone (today ~4 % of loops, SMB total biased +20 % by the known interval-tolerance
   difference) is the noise floor; variant differences below it are reported as "indistinguishable".
3. **Hindsight labels are a one-step proxy.** Judging the decision at minute t with the glucose of t+15..t+180
   assumes everything except that one decision stays as it was. That is approximately true for one loop and
   increasingly false for a variant that would have dosed very differently for hours. ShadowLoop measures that
   divergence (section 3.4) and refuses to rank variants that leave the "validity envelope".
4. **It recommends; a human decides.** Output is a review page naming the parameters, the proposed values,
   the evidence (per-day tables, the loops that drive the difference, transcripts), the caveats and the
   confounders (profile changes inside the windows, TT automations, CGM gaps). Nothing is ever written to the
   phone, to Nightscout or to `ns_settings.json` by the optimizer.
5. **Not a medical device, not medical advice.** T1D insulin dosing; the user and their care team own the
   decision. The page says so in plain words, not in a footer.

---

## 3. Objective function

### 3.1 Per-loop quantities (all available today)

For loop i (run time t_i) and variant V the replay yields the decision tuple

    D_V(i) = (insulinReq, SMB_U, TBR_rate, TBR_duration, ISF_final, autoISF_factor, eventualBG, minPredBG,
              minGuardBG, predBGs{IOB,ZT,UAM,COB}, flow path)

(`emulInsReq`, `emulSMB`, `emulBasal`, `emulISF`, `emulAI_ratio`, `Pred[t]` = `Fcasts` incl. `Levels`, `FlowChart[t]` in
`emulator_core`; the phone's tuple is the `orig*` lists plus `suggested.predBGs`). The record also gives the
inputs (bg, iob, cob, target, TT parity, microBolusAllowed) and, because the whole day is cached, the **realized
glucose after the loop**: `bg(t_i + k)` for k up to hours, from `entries`/devicestatus.

Insulin "in force" per loop: `give(i) = SMB_U(i) + TBR_rate(i) * dt_i` with `dt_i = min(t_{i+1} - t_i, 30 min)`
(the convention of `emulator_optimize.metrics_from_csv`, kept). Paired delta vs phone: `Δ_V(i) = give_V(i) - give_phone(i)`.
Paired delta vs base: `δ_V(i) = give_V(i) - give_base(i)` (removes the replay bias; this is the quantity the search uses).

### 3.2 Hindsight label per loop (variant-independent, computed once per window)

From the realized trajectory after t_i, with ISF_phone(i) to convert glucose to units and with T_low = 70,
T_warn = 80, T_high = 180 mg/dl (user-configurable, mmol shown on the page):

    low_next  = min bg in (t_i, t_i + 120 min]
    high_next = max bg in (t_i + 30, t_i + 180 min]
    label(i)  = NEEDED_LESS  if low_next < T_low, or (low_next < T_warn and bg falling at t_i)
              = NEEDED_MORE  if high_next > T_high and bg(t_i) above target, and no low within 180 min
              = FINE         otherwise
    mag(i)    = (T_warn - low_next)/ISF  for NEEDED_LESS  (units "too many")
              = (high_next - target)/ISF  for NEEDED_MORE (units "missing"), each capped at 1.0 U

Covariates stored with the label: `carbs_within_2h` (carb treatment within ±2 h → meal loop), `tt_parity_smb_off`
(TT with odd decimal → SMB off for phone and variant alike), `cgm_gap_near`, `profile_change_inside_window`,
`activity_state`.

### 3.3 Score components per variant and window (all relative to base)

| component | definition | direction | why it is honest |
|---|---|---|---|
| **H**, hindsight agreement | Σ_i w(i) · sign-match(δ_V(i), label(i)) · min(|δ_V(i)|, mag(i)); w = 2.5 for NEEDED_LESS loops (hypo side), 1 for NEEDED_MORE, FINE loops contribute −|δ| · 0.5 (extra or missing insulin when nothing was wrong is neutral-to-bad) | maximise | uses only one-step counterfactuals and realized glucose; asymmetric toward hypo avoidance |
| **P**, pre-hypo insulin | Σ over NEEDED_LESS loops of max(δ_V(i), 0) in U/day | hard constraint: P ≤ P_base + floor_P | a variant that gives *any* additional insulin in the two hours before a realized low is rejected before ranking |
| **R**, rise response | Σ over NEEDED_MORE loops of δ_V(i) | report; part of H | extra insulin where hindsight wanted it |
| **S**, saturation | % loops with autoISF factor on `autoISF_max`/`autoISF_min`; % loops with iob > emulated iobTH | report; penalty above 25 % | a weight whose factor sits on the cap is not doing what the user thinks |
| **E**, divergence (validity envelope) | max over sliding 3 h windows of Σ δ_V, and |Σ_day δ_V| / TDD | hard constraint: 3 h ≤ 1.0 U and day ≤ 15 % | beyond this the replay's "inputs unchanged" assumption is false; the variant is marked *out of envelope*, shown, not ranked |
| **F**, forecast skill (diagnostic) | median |eventualBG_V(i) − bg(t_i + 30 min)| vs the same for base | report | the only glucose-level metric that is counterfactual-free at short horizon; biased by later dosing, hence diagnostic only |
| **A**, agreement with phone | % loops with SMB/rate/ISF within tolerance (the no-change test) | base only | the noise floor; each component gets `floor_X` = |X_base − X_phone| |

Scalar objective for the search: `J_V = Σ_days H_V,d / n_days − λ · std_days(H_V,d)` with λ = 0.5 (robustness), subject
to P and E constraints per day. Multi-objective view (Pareto of H vs P vs |E|) is kept in the store and shown on
the dashboard; the scalar is only the search driver. Weights (2.5, 0.5, λ) are study parameters, written into
the study record and shown on the page; a sensitivity-of-the-ranking-to-weights panel is part of the
confirmation stage so that nobody mistakes a weight choice for a finding.

### 3.4 Validity envelope, noise floor, confounders

* **Envelope**: computed per variant and window (E above). Default limits are deliberately tight; the user can
  loosen them, the page then says "ranking valid only under the assumption that ±X U over 3 h would not have
  changed the glucose trace".
* **Noise floor**: measured once per window from the base replay against the phone (today: SMB exact 96 %,
  insulinReq within 0.05 U 98 %, variable_sens within 1 mg/dl 99.6 %, SMB total +20 % bias). Each component
  gets a floor; a difference between two variants smaller than 2 × floor is drawn in grey and called
  "indistinguishable". Other agents are currently fixing the known bias sources; the floor is re-measured on every
  `freeze`, so improvements flow in automatically.
* **Confounders** flagged per window and shown as badges: `profile_history` changes inside the window (ISF 4.9→5.1
  on 09-04, autoISF_max 1.3→1.5 and smb_max_range_extension 1.15→1.8 on 09-07), CGM gaps (the 71 min gap on 09-06),
  TT automations that switch SMB by target parity, open-loop/suspended periods, duplicate uploads (harmless, counted).

---

## 4. Parameter space

The replay can only test what `determine_basal.py` reads and what a VDF row can set. Settings that enter
through the *inputs* (basal rates, DIA/peak → IOB, CR → COB, autosens ratio) are **not tunable** here; the page
lists them under "cannot be tested with this method".

Tier A — high leverage, worth searching first (autoISF 3.2 as on the phone; current value from `ns_settings.json`):

| VDF key | current | search range | step/scale | coupling |
|---|---|---|---|---|
| `profile autoISF_max` | 1.5 | 1.0 … 2.0 | 0.05 | caps every weight; if S > 25 % the weights below are unobservable |
| `profile autoISF_min` | 0.3 | 0.3 … 1.0 | 0.05 | floor of the ISF factor (hypo side) |
| `profile dura_ISF_weight` | 0.7 | 0.0 … 1.5 | 0.1 | plateau strength; interacts with `enable_dura_ISF_with_COB` |
| `profile bgAccel_ISF_weight` | 0.09 | 0.0 … 0.30 | 0.01 | with `bgBrake_ISF_weight` and `parabola_fit_source` |
| `profile bgBrake_ISF_weight` | 0.03 | 0.0 … 0.30 | 0.01 | hypo side of acce |
| `profile smb_delivery_ratio` | 0.2 | 0.1 … 0.7 | 0.05 | with `_min`/`_max`/`_bg_range` (the interpolation) |
| `profile smb_delivery_ratio_min` / `_max` | 0.1 / 0.5 | 0.05 … 0.4 / 0.3 … 0.9 | 0.05 | ordering constraint min ≤ ratio ≤ max |
| `profile smb_delivery_ratio_bg_range` | 36 | 18 … 90 mg/dl | 9 | the fitted 36 is itself `_assumed` |
| `profile iob_threshold_percent` | 80 | 50 … 100 | 5 | gates SMB entirely above the threshold |

Tier B — second pass:

`profile pp_ISF_weight` 0 … 0.05 (0.002), `profile pp_ISF_hours` {0,1,2,3,4,6}, `profile enable_pp_ISF_always`
{T,F}, `profile higher_ISFrange_weight` 0 … 1.0 (0.05), `profile lower_ISFrange_weight` 0 … 1.0, `profile
delta_ISFrange_weight` 0 … 1.0, `profile smb_max_range_extension` 1.0 … 3.0 (0.1), `profile SMBInterval` {1,2,3,5},
`profile maxSMBBasalMinutes` / `maxUAMSMBBasalMinutes` {30,60,90,120,180,240}, `profile activity_scale_factor`
1.0 … 1.5, `profile inactivity_scale_factor` 0 … 1.0 (only days with the activity echo), `profile
enable_dura_ISF_with_COB` {T,F}, `profile parabola_fit_source` {3,5}.

Tier C — profile-level, only with the envelope at its tightest and stated separately on the page:
`profile sens` ±20 % (multiplicative row `profile sens profile['sens']*1.1`), `profile min_bg`/`max_bg`/`target_bg`
±18 mg/dl, `profile half_basal_exercise_target`. These shift *every* loop, so E is hit quickly; they are
diagnostics ("the hindsight labels say the fixed ISF is too strong overnight") rather than search dimensions.

Encoding rules for the optimizer: continuous parameters are sampled on the step grid above (the phone's UI
cannot set finer values anyway); booleans and enumerations are categorical; dependent ranges (`min ≤ ratio ≤ max`)
are enforced by sampling `ratio` and the two half-widths. Every sampled point is written as a VDF so that it is
reproducible with `emulator_batch.py` by hand.

---

## 5. Run-time budget (measured 2026-10-16 on this Mac, `.venv` Python 3.14, cache warm)

| measurement | result |
|---|---|
| `parameters_known`, 6 h window 06–12Z 2026-09-06, `noplot`, empty VDF | 380 loops in **3.9 s** (3.8 s on the repeat) → ~100 loops/s |
| cProfile of the 3 h window (180 replayed loops, 4.81 s total) | `ns_source.loops()` 4.38 s (91 %): `ns_meal.slopes_at` 3.08 s, `ns_glucose` smoothing 0.93 s, `ns_iob.iob_array` 0.79 s; **`TreatLoop` 0.28 s = 1.6 ms/loop, of which `determine_basal` 1.3 ms**; file open/close for the transcripts 0.14 s |
| the task's "2 min 20 s for 320 loops with PDF" | consistent with the above: the PDF (`XYplots`) is ~97 % of that time, not the replay |

Consequences:

* **Freeze once, replay many.** The reconstruction does not depend on the VDF (`setVariant` modifies the profile
  copy after the inputs exist), so a frozen loop set per window (pickle of `ns_source.loops()` output, ~1.3 k
  loops/day, ~40 MB/day uncompressed, ~6 MB with zlib) turns the per-variant cost into ~1.6 ms/loop +
  bookkeeping. A full day ≈ 1300 loops ≈ **2–3 s per variant**, a 7-day set ≈ **15–20 s**, 30 days ≈ 80 s.
* **Budget for "several hours"**: 4 h = 14 400 s ≈ 700 full-week evaluations in one process; with 4 worker
  processes (module globals force one `emulator_core` per process) ≈ 2 500. TPE converges on 4–6 parameters in
  150–300 trials; successive halving spends most trials on 1–2 days and only finalists on the full set. The
  budget is generous; the binding constraint is the data (how many days of distinct character exist), not CPU.
* **Without the freeze** (today's `emulator_optimize.py`): 25–30 s per variant-day → 4 h ≈ 70 week-evaluations.
  Still usable for the screen stage, so the MVP works before the freeze hook lands, but the freeze is the first
  engineering ticket.
* **Transcripts**: `TreatLoop` writes a `.txt` transcript per loop (≈ 3.5 MB per variant-day) and the CSV; for
  thousands of variants this is GBs of churn. The optimizer runs with a new `quiet` output token (no `.txt`/`.delta`,
  CSV still written to a temp dir and deleted after metric extraction) and re-runs finalists with full outputs.

---

## 6. Search strategy (stages; each stage is resumable and its result is a row in the store)

**Stage 0 — freeze and noise floor.** `shadowloop freeze --days 2026-09-01..2026-09-07` fetches/caches the days
(refusing to freeze a day whose cache file is not yet *final* per `ns_client`'s rule unless `--allow-partial`),
runs `ns_source.loops()` once per UTC day, stores the frozen loop set, computes the hindsight labels and the
day-character record (TDD, carbs, % >180, % <70, TT minutes, CGM gaps, activity minutes, profile changes), then
replays base and stores agreement and floors.

**Stage 1 — sensitivity screen.** One-at-a-time over Tier A (+B on request), 5–7 levels each, on all frozen days
(Morris elementary effects: mean |ΔJ| and spread across days per parameter). Output: ranked parameter list with
"effect / floor" ratios; parameters below 1.5 × floor are frozen at their current value for the next stages.
Cost on 7 days frozen: 15 params × 6 levels × 15 s ≈ 25 min.

**Stage 2 — coarse search with successive halving.** Coordinate descent (or a 3–4 parameter coarse grid, ≤ 200
points) over the surviving parameters. Rung 1 evaluates all points on the 2 most "typical" days (nearest to the
median day character), keeps the top third, rung 2 adds 3 days, rung 3 runs the rest on all days. Constraints P
and E are applied at every rung (a violation on any day eliminates).

**Stage 3 — Bayesian refinement.** Optuna TPE (multivariate, with the step grid) over the surviving parameters
within ±1 step-grid cell of the Stage 2 optimum's neighbourhood and the full ranges; objective J on all days;
`JournalStorage` with the file backend (pure Python, resumable, no DB driver); pruning via the rung logic above
(MedianPruner on per-day H). Budget: 200–400 trials (≈ 1–2 h on 7 days with 4 workers).

**Stage 4 — cross-validation and confirmation.** Leave-one-day-out: re-rank the top 10 on each 6-day subset,
report rank stability (Kendall τ, per-variant rank range) and the held-out day's H; also "worst day" ranking. A
candidate is *recommended* only if it is top-3 on ≥ 80 % of folds and never violates P/E. Finalists (top 3 + base +
phone) are re-run with full outputs (`.txt`, `.csv`, `pred`/`flowchart` PDF pages for the 20 most differing
loops), and the review page is built.

**Stage 5 — grow the data, repeat.** When new final days appear in Nightscout (`shadowloop freeze --new`), the
study re-evaluates the current top 10 on them first (cheap), updates CV, and only if the ranking moved beyond
noise restarts Stage 3 with the new days included. This is the "run it for hours and keep going" mode:
`shadowloop run --study s1 --budget 4h --until-stable` loops Stage 3→4→5 until the budget is used or the top-3
set has not changed for N rounds.

Overfitting guards: days are the CV unit (never loops: adjacent loops are not independent); day-character
stratification so that a weekend-only optimum is visible; λ·std penalty; the floor-based "indistinguishable" rule;
weights sensitivity panel; and E, which keeps the search from exploiting the replay's blind spot (variants that
dose much more look good on NEEDED_MORE loops only because the phone's later insulin is still in the trace).

---

## 7. Architecture

### 7.1 Module layout (new package under `software/`, may use `.venv` libraries; `ns_*` stay stdlib-only and untouched)

```
software/
  shadowloop/
    __init__.py
    __main__.py          python -m shadowloop <command>   (argparse; commands below)
    freeze.py            frozen loop sets: NightscoutSource -> list(loops) -> zlib-pickle per UTC day + labels.json
    hindsight.py         per-loop hindsight labels, covariates, day-character record (stdlib + numpy)
    replay.py            evaluate(variant, window) -> per-loop arrays + metrics; drives emulator_core.parameters_known
                         with the frozen source injected; quiet mode; worker-process pool
    objective.py         components H,P,R,S,E,F,A; floors; scalar J; envelope checks; weights as a dataclass
    space.py             parameter space (tiers, ranges, steps, constraints), VDF text generation, variant hash
    search.py            stages: screen (OAT/Morris), halving, coordinate descent, optuna TPE, CV; resumable
    store.py             SQLite schema + append-only writers; export to dashboard JSON
    report.py            static HTML (Plotly.js pinned) from the store; review page; finalist deep-dive pages
    daemon.py            `run --budget` loop: picks the next stage, checkpoints, rate-limits Nightscout, writes heartbeat
    skill/               SKILL.md + references for the Claude skill (copied to .claude/skills/shadowloop on install)
  results/shadowloop/    store, frozen sets, exports (git-ignored like results/)
```

Two small changes in existing files (one PR, reviewed against the no-change replay):

1. `emulator_core.py`: `scanNightscout()` takes its source from a module-level hook,
   `loop_source_factory(fn, start, end, log_msg)` defaulting to `ns_source.NightscoutSource`; the frozen source
   implements `.load()` (no-op) and `.loops()` (yields the stored dicts). `scanNightscoutLoops(src, fn)` already
   takes the source object, so this is ~8 lines. Plus the `quiet` option token (skip `.txt`/`.delta` writes) and a
   `last_run_series()` accessor returning the per-loop global lists as a dict (so the optimizer does not parse the
   CSV; CSV parsing stays as fallback via `emulator_optimize.read_result_csv`).
2. `emulator_optimize.py`: becomes a thin wrapper (`shadowloop screen --grid ...` with the same CLI) or is left
   as is and documented as the single-window predecessor; its `metrics_from_csv` metrics are kept as the
   "classic metrics" block in the store so old numbers remain comparable.

Why not bypass `parameters_known` entirely? `TreatLoop`/`setVariant` are tied to ~60 module globals; reimplementing
them would fork the emulator. Driving `parameters_known` per variant keeps one code path and the "no change"
regression check valid for the optimizer too. The cost is the global state → one `emulator_core` per worker process
(`multiprocessing` with `spawn`), which is also what makes parallelism safe.

### 7.2 Data flow

```
Nightscout ──ns_client (cache, final-day rule)──> ns_source.loops()  ──freeze──> frozen/<host>/<day>.loops.pkl.z
                                                                                 frozen/<host>/<day>.labels.json  (hindsight, covariates, day character)
space.py: variant params ──> VDF text (hash = sha1 of normalised rows) ──> results/shadowloop/vdf/<hash>.vdf
replay.py: for (variant, day): parameters_known(frozen source, quiet) ──> per-loop arrays (npz, finalists only) + classic metrics
objective.py: arrays + labels ──> components, floors, J ──> store.evaluations
search.py / daemon.py: read store ──> next batch of variants ──> replay ──> store (append) ──> export dashboard.json
report.py: store ──> dashboard.html (+ review.html, loop pages) ; Claude skill publishes dashboard.html as an Artifact
```

### 7.3 Result store (SQLite, `results/shadowloop/store.sqlite`, WAL mode, append-only semantics)

```
windows      (id, host, day_utc, start_ms, end_ms, loops, frozen_path, frozen_sha1, cache_final bool,
              emulator_stamps json, character json, floors json, confounders json, created)
variants     (id, hash, params json, vdf_text, tier, origin {'phone','base','oat','grid','tpe','manual'}, created)
evaluations  (id, variant_id, window_id, study_id, status {'ok','syntax','envelope','error'}, loops,
              classic json, components json, J real, seconds, worker, arrays_path nullable, created)
studies      (id, name, space json, weights json, limits json, stage_state json, budget_s, started, heartbeat, finished)
trials       (study_id, optuna_trial json)                  -- mirror of optuna's journal for the dashboard
cv_results   (study_id, variant_id, fold json, rank_stats json, recommended bool)
```

Uniqueness `(variant_id, window_id, emulator_stamps)`: an evaluation is never repeated for the same variant,
day and emulator version; when another agent's bug fix bumps a version stamp, affected rows are marked stale
and re-evaluated lazily (base first, so the floors update). Per-loop arrays (`npz`: t, bg, give_phone, give_base,
give_V, ISF triple, factor, flow-path id, label) are kept for base, phone and finalists only; everything else is
reproducible from the VDF + frozen set within seconds.

Exports: `results/shadowloop/<study>/dashboard.json` (≤ 2 MB: leaderboard, per-day matrix, parameter marginals,
Pareto points, finalist timelines downsampled to 5 min), `summary.csv` (one row per variant like today's
`emulator_optimize`), `recommendation.md` (human text, what/why/caveats).

### 7.4 CLI

```
python -m shadowloop freeze   --days 2026-09-01..2026-09-07 [--new] [--allow-partial] [--refresh]
python -m shadowloop floor    --days ...                       # base replay, agreement, floors (part of freeze by default)
python -m shadowloop screen   --study s1 --tier A [--levels 6] [--days ...]
python -m shadowloop search   --study s1 --stage halving|tpe --trials 300 --workers 4
python -m shadowloop cv       --study s1 --top 10
python -m shadowloop confirm  --study s1 --top 3                # full outputs, loop pages
python -m shadowloop report   --study s1 [--open]               # dashboard.html, review.html
python -m shadowloop run      --study s1 --budget 4h [--until-stable 3] [--workers 4]   # the unattended loop
python -m shadowloop status   --study s1                        # heartbeat, stage, best so far, ETA
python -m shadowloop eval     --vdf variants/x.vdf --days ...   # one variant, prints components; the emulator_batch of the new world
```

Every command is idempotent and re-entrant: it reads the store, does only what is missing, appends, exports.
`run` writes `heartbeat` every evaluation and a `checkpoint` per stage; a killed process resumes from the store
(optuna's journal file is the search state). A lock file prevents two daemons on the same study.

### 7.5 Unattended safety

* Nightscout: only `freeze` talks to the server, through `ns_client` (cache, retries, final-day rule); the daemon
  calls `freeze --new` at most once per hour, never refetches final days, respects a configurable minimum
  interval and backs off on HTTP 429/5xx. All search stages are offline by construction.
* Disk: quiet mode; per-evaluation files deleted after extraction; arrays only for finalists; store size shown
  in `status`.
* CPU: `--workers` capped at `os.cpu_count()-1`; `nice` the workers.
* Failure isolation: a `SYNTAX`/exception in one variant is one `status='error'` row, the daemon continues; three
  consecutive errors on different variants stop the daemon with a message (likely an emulator change).
* Reproducibility: every row carries the version stamps (`get_version_core/determine_basal`), the frozen set's
  sha1, the `ns_settings.json` hash that the `.log` header already echoes, and the weights/limits of the study.

---

## 8. Visualization (secondary, but the user's first complaint)

Rendering: Python generates a self-contained `dashboard.html` with the data embedded as JSON and Plotly.js from
cdnjs with an exact pinned version (`plotly.js/2.35.2/plotly.min.js`; verify the pin exists at implementation time,
any 2.3x works). One file, < 3 MB, opens locally in a browser and publishes unchanged as a Claude Artifact (scripts
only from the allowed CDN, light/dark via CSS tokens + `prefers-color-scheme`, two Plotly templates switched at
load, phone width with stacked panels, 16 px gutters). Vega-Lite is the alternative if the team prefers
declarative specs; ECharts if the per-loop timelines get heavy (50 k points). Plotly is proposed because
parallel-coordinates, heatmaps and linked hover come for free.

Pages:

1. **Study overview** — honesty box (section 2 in five lines), study parameters, data badges (days, loops, floor,
   confounders), progress (trials done / budget / ETA).
2. **Leaderboard** — top N variants: J with per-day spread as error bars, components H/P/R/S/E as compact columns,
   "indistinguishable from" grouping in grey, envelope status, CV stability. Click → variant page.
3. **Parameter landscape** — parallel coordinates (params → J), per-parameter marginal effect plots with the floor
   band, interaction heatmap for the two strongest parameters.
4. **Day matrix** — variants × days heatmap of H (and of P violations), day-character strip on top, so a
   weekend-only optimum is visible at a glance.
5. **Variant page** — one day at a time: BG trace with hindsight labels shaded (NEEDED_LESS red, NEEDED_MORE
   amber), phone vs variant SMB/TBR markers, ISF factor ribbons (phone / base / variant), IOB with the emulated
   iobTH; below, the table of the 20 loops with the largest |δ| linking to the transcript and the flowchart
   panel (the existing `Flows` list rendered as a compact text/SVG box list; the matplotlib PDF stays available
   through `confirm`).
6. **Recommendation** — the human-readable result: parameter, current → proposed, effect in U/day on rising
   loops, pre-hypo insulin change, saturation, days where it helped/hurt, caveats, "what to watch for after
   changing it on the phone", and the paragraph that it is a decision aid, not advice.

Live mode: `run` re-exports `dashboard.json` after every evaluation batch; `report --watch` rebuilds the HTML.
In Claude Code the skill publishes the page once and then streams rows into the Artifact's shared database
(`ArtifactData` batch writes, one document per evaluation: variant hash, day, components, J) so the published
page updates without republishing; the static export remains the source of truth and works without Claude.

---

## 9. Claude skill, agent team, GitHub issues

**Skill `shadowloop`** (`.claude/skills/shadowloop/SKILL.md`, installed from `software/shadowloop/skill/`):
triggers on "optimize settings", "what-if study", "run the shadow loop", "which autoISF setting". It encodes the
staged protocol and the stop rules: (1) `freeze` the requested period, refuse non-final days unless told; (2) run
`floor`, read the agreement numbers, abort if agreement < 90 % (emulator regression) and open an issue; (3)
`screen`, then `run --budget <h>` in the background, check `status` every 15–30 min (via the `loop` skill), post
one-line progress; (4) when the budget is used or `--until-stable` fires, `cv`, `confirm`, `report`, publish the
dashboard as an Artifact, summarise the recommendation in chat with the caveats verbatim; (5) never edit
`ns_settings.json`, the phone or Nightscout; a request to "apply" the result is answered with the manual steps
and the "watch for" list. The skill's reference file carries the parameter table (section 4) and the reading guide.

**Agent team** (parallel work packages, each a GitHub issue; one integration branch `shadowloop`):

| role | package | depends on |
|---|---|---|
| core-hooks | `emulator_core` hook + `quiet` + `last_run_series()`; no-change regression before/after | — |
| freeze | `freeze.py`, frozen format, `hindsight.py`, day character | core-hooks (for base replay) |
| objective | `objective.py` incl. floors/envelope, unit tests on synthetic series | freeze |
| search | `space.py`, `search.py` (OAT, halving, optuna), `daemon.py`, resumability tests (kill −9 mid-run) | replay, objective |
| store+report | `store.py`, exports, `report.py` pages, dark/light, phone width, Artifact publish | objective |
| skill+docs | SKILL.md, README section, NIGHTSCOUT_SOURCE.md "Optimization" rewrite, CLAUDE.md lines | all |
| reviewer | the honesty contract on every page; checks every metric's definition against section 3; red-team the envelope | continuous |

**GitHub issues** (repo `markusbaumg/APS-what-if`): one epic per package above with labels `shadowloop`,
`stage:mvp|later`, `needs-review`; a pinned "contract" issue holding sections 2–3 of this proposal as the
acceptance criteria; every evaluation-changing PR must paste the before/after no-change agreement and the
base-variant floors.

---

## 10. Risks and honesty

* **The hindsight score is a proxy, and an exploitable one.** Without the envelope constraint E, "dose more" wins
  every NEEDED_MORE loop because the phone's later insulin is still in the glucose trace. E and P are therefore
  hard constraints, not weights, and the page shows out-of-envelope variants greyed with the reason.
* **Replay bias** (SMB interval tolerance, zero-temp cap, rounding) currently inflates emulated SMB totals by ~20 %.
  Pairing against base removes the variant-independent part; the variant-dependent part (interval tolerance
  matters more for variants that SMB more often) remains and is one reason for the floor rule. Other agents are
  fixing these now; the version-stamp staleness logic re-evaluates automatically.
* **Few days, strong day effects.** 9 cached days (08-30 … 09-07), two with profile changes inside. The first
  real study should freeze 3–4 weeks; the day-character badges and the CV table exist to make "this optimum is a
  weekend artefact" visible, not to hide it.
* **Inputs that are themselves assumptions**: `smb_delivery_ratio_bg_range` 36 and `low_temptarget_lowers_sensitivity`
  false are fitted (`_assumed`); synthetic step counts reproduce the activity state. A variant that changes
  `activity_scale_factor` is tested against reconstructed, not measured, activity. The page lists `_assumed` keys.
* **TT automations** switch SMB by target parity; a variant cannot change that, so SMB parameters are evaluated
  only on the loops where SMB was allowed — the page shows that share (today roughly a third of the night is SMB-off).
* **Optuna on Python 3.14**: use `JournalStorage` with the file backend (no SQLAlchemy); if optuna itself lags
  3.14, the fallback is the built-in coordinate descent + halving (sufficient for ≤ 5 parameters) — the search
  module has the sampler behind one interface for this reason.
* **Scope creep**: the emulator stays the single source of truth for decisions; ShadowLoop never reimplements
  `determine_basal` logic, never computes "virtual glucose", and labels every glucose-level number as realized
  (phone) data.
* **Medical**: this is one person's insulin dosing. The recommendation page is a decision aid for the user and
  their care team; the tool changes nothing on the phone and says so where it cannot be missed.

---

## 11. MVP vs later

**MVP (first week, works end to end on the 9 cached days):**
1. `core-hooks` PR: source hook, `quiet`, `last_run_series()`; no-change replay unchanged.
2. `freeze` + `hindsight` (labels, covariates, day character) + `floor`.
3. `objective` with H, P, E, S, A and the floor rule; `space` Tier A; `replay` with 1–4 workers.
4. `screen` (OAT) and `search --stage halving` (coarse grid, successive halving); SQLite store; `summary.csv`
   compatible with `emulator_optimize`.
5. `report`: overview, leaderboard, day matrix, variant page (BG + markers + ISF ribbons), recommendation text;
   static HTML, light/dark, phone width; skill v0 that runs the stages and publishes the page.

**Later:**
6. Optuna TPE stage, `run --budget --until-stable`, daemon heartbeat/lock, live `ArtifactData` streaming.
7. Leave-one-day-out CV, weights-sensitivity panel, Pareto view, parallel coordinates.
8. Finalist deep-dive: 20-loop drill-down with flowchart boxes, transcript links, PDF via `confirm`.
9. Tier B/C spaces, multi-week freezes, day-type clustering, forecast-skill component F.
10. GitHub automation (issue per study with the recommendation attached), `eval` as the daily "what-if" CLI.

**Explicitly not planned:** any glucose simulation, any write to the phone/Nightscout/settings, ranking without
the envelope and floor rules, per-loop (instead of per-day) cross-validation.

---

## Appendix A — files and places referenced

* `/Users/markus/dev/APS-what-if/software/emulator_optimize.py` — current grid runner; its `metrics_from_csv`
  metrics become the "classic" block.
* `/Users/markus/dev/APS-what-if/software/emulator_core.py` — `parameters_known` (l. 2709), `scanNightscout`
  (l. 1828), `scanNightscoutLoops(src, fn)` (l. 1877, the hook point), `TreatLoop` (l. 819; `Pred[t] = Fcasts`,
  `FlowChart[t] = Flows` at l. 1049–1050).
* `/Users/markus/dev/APS-what-if/software/ns_source.py` — `NightscoutSource.load()/loops()`, `window_label`.
* `/Users/markus/dev/APS-what-if/software/ns_client.py` — cache final-day rule (`SETTLE_MS`, `refresh_minutes`).
* `/Users/markus/dev/APS-what-if/software/NIGHTSCOUT_SOURCE.md` — contract and measured agreement table.
* CSV layout seen: 43 `;`-columns, 3 header rows (`UNIX time`, `bg accel`, `target low/high orig/emul`, `cob`,
  `iob`, `eff./tol. iobTH emul`, `final ISF orig/emul`, `ISF orig/prof/emul`, `Ins.Req. orig/emul`, `max bolus
  orig/emul`, `SMB orig/emul`, `TBR orig/emul`, autoISF factor columns), footer rows Minimum/Maximum/Totals.
* Measurements: `.../scratchpad/design/optimizer-first/timing/` (two 6 h runs, 3.9 s / 3.8 s) and
  `.../scratchpad/design/optimizer-first/profile/` (cProfile of the 3 h window).
