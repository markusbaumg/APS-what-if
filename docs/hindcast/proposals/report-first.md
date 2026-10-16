# Hindcast — scenario studio and settings optimizer for the AAPS what-if emulator

*Proposal, design angle: data visualization and product. Written 2026-10-16 against branch `nightscout-source`
(`emulator_core.py` 2026-09-08 04:00, `emulator_optimize.py` 2026-09-08 12:00). No repo file was changed.*

---

## 0. One paragraph

Hindcast is a new layer on top of the existing emulator. It takes a set of AAPS/autoISF settings variants, replays
them over cached Nightscout days through the unchanged `emulator_core.parameters_known()`, condenses each replay into
honest proxy metrics plus a compact per-loop series file, and renders one self-contained HTML page (ECharts, one
pinned script from cdnjs) that works as a Claude Artifact and as a local file: a study overview with the noise floor,
a parameter-space view (parallel coordinates, delta heatmaps, a Pareto scatter), small-multiple scenario timelines
(phone in grey, up to three variants in colour, one shared time axis, no dual axes), and a drill-down to any single
loop with both prediction fans and both decision explanations side by side. A resumable study runner
(`trials.jsonl`, process pool, Optuna TPE/NSGA-II as the third phase) can run unattended for hours against the
offline cache; a Claude skill drives the loop, publishes the page and streams leaderboard rows into the artifact's
database, and ends every round with a candidate card written for a human to review. The product recommends settings
to a person; it never changes the phone, and every number on it is a decision-level counterfactual, not a glucose
outcome.

---

## 1. Names (collision-checked 2026-10-16 with WebSearch)

| Name | Why | Collision check |
|---|---|---|
| **Hindcast** (recommended) | A hindcast is exactly what the emulator does: a model re-run over recorded past inputs to test a parameterisation (weather and ocean modelling vocabulary). Short, pronounceable, memorable, serious. `hindcast` as package name, `hc` as CLI alias. | No diabetes or software product of that name found; the word is a generic technical term, so no brand conflict to worry about. |
| **Replayground** | Replay + playground: the "try settings and look" feeling. Playful; weaker for a tool about insulin. | Nothing of that name; "FS Playground" (flight sim replay add-on) and "Playground" (child-care software) are the nearest. |
| **Loopscope** | A scope to look into the loop's decisions. Reads well next to "Loop", "LoopKit", "LoopLife". | Not found as a product. Risk: the Loop ecosystem owns the "Loop..." prefix; could be read as a Loop (iOS) tool rather than an AAPS tool. |
| **Counterloop** | Counterfactual loop. Precise about what the product is. | Not found ("Counterweight" app is the nearest). Slightly adversarial sound. |
| **Otherwise** | "What the loop would otherwise have done." Elegant, but ungoogleable and generic; listed as the editorial option. | Not usable as a search term; collision check inconclusive by construction. |

The rest of this document uses **Hindcast**. All names are placeholders in paths (`software/hindcast/`, `studies/`).

Sources consulted: [FS Playground replay add-on](https://forums.flightsimulator.com/t/rain-rivets-reflections-replays-fs-playground-replay-app-is-fun/348324),
[Playground child-care software](https://www.tryplayground.com/blog/why-playground-is-the-best-child-care-management-software),
[LoopKit](https://github.com/LoopKit/LoopKit), [LoopLife](https://apps.apple.com/kr/app/id6760711673),
[Counterweight](https://apps.apple.com/app/id6444675809), [AndroidAPS](https://technologickainkubace.org/en/startupy/androidaps/).

---

## 2. What the user needs to see (the product, from the screen inward)

The current output is a matplotlib PDF with one page per loop and a console table. Its failures are instructive:
everything is overlaid on one axis pair (BG, ISF factors, insulin, predictions share a plot), colours are cycled per
series, there is no comparison between scenarios at all (one PDF per VDF), and the decision explanation is a flowchart
rendered as boxes of text on the plot. The replacement is built around four questions, each with its own view, each
view linked to the next by selection.

### 2.1 "Is this study trustworthy?" — Study overview (landing view)

Shown first, above everything, because every other number depends on it.

* **Noise-floor banner.** The `base` variant (phone settings, empty VDF) replayed over the same windows: *"Base replay
  reproduces the phone's SMB in 96.1 % of 1 311 loops (within 0.05 U: 98.5 %). Emulated SMB total is +4.05 U/day above
  the phone (SMB-interval tolerance, see NIGHTSCOUT_SOURCE.md). Differences between variants smaller than ±0.4 U/day or
  fewer than 30 changed loops are within this floor."* The thresholds are computed, not typed (section 4.4).
* **Window strip.** One small card per replayed day: date, loops, mean BG, % > 180, % < 70, carbs entered, temp targets,
  CGM gaps, profile_history changes inside the window. Cards are the day filter for every other view. Character
  chips (`workday`, `sport`, `weekend`, `illness`) are typed by the user in `study.json`, never inferred.
* **Progress and freshness.** Trials done / planned, current phase, last trial time, "data from cache, days final"
  flag. Stat tiles, plain sans, proportional figures.
* **Medical scope line, always visible, not collapsible**: *"Hindcast compares loop decisions against what the phone
  did on recorded days. It cannot simulate how glucose would have responded. Nothing here changes the phone; settings
  are for you and your care team to review."*

### 2.2 "Which settings matter, and where is the good region?" — Parameter space

* **Parallel coordinates** (ECharts `parallel` series): one axis per searched parameter, then the objective proxies
  (`rise_vs_base_U`, `rest_vs_base_U`, `lowside_flags`, `at_cap_pct`, `changed_loops`). Base is a dashed neutral line.
  Brushing an axis range filters the leaderboard table and the scenario picker. Lines are thin (1 px, 55 % alpha);
  the Pareto-front members are drawn last in the accent hue, everything else in the recessive grey ramp. On phone
  width this view is replaced by the leaderboard table (the table-view twin exists anyway).
* **Delta heatmap** for any two parameters (select boxes above the chart, one filter row for the whole view):
  cells = a chosen metric minus its base value, **diverging blue↔red with grey at zero** (zero = "same as the phone's
  settings"), the base cell outlined. Sequential single-hue blue when the metric is a magnitude (e.g. `at_cap_pct`).
  Cells carry a 2 px surface gap; a value label only when the cell is wide enough, otherwise hover and table.
* **Pareto scatter**: x = responsiveness proxy (extra insulin in rising-above-target loops vs base), y = low-side risk
  proxy (extra insulin in falling/below-target loops vs base, plus flagged loops), each dot a variant averaged over
  the selected days, size = number of days it was run on, base as a hollow ring at the origin. Pareto-optimal dots in
  accent, others grey; the three currently selected scenarios carry their entity colours (section 3.3). Hover shows
  the parameter values and the per-day spread as a tiny range bar.
* **Leaderboard table**, sortable, every metric column, with the noise floor painted as a faint band in the delta
  columns. This table *is* the WCAG twin of the charts above, and it is what the Claude skill reads back.

### 2.3 "How would this day have looked?" — Scenario comparison, small multiples

For one day and up to three selected variants plus the phone. Stacked panels, **one shared time axis**, ECharts
`axisPointer.link` so the crosshair runs through all panels, `dataZoom` on the shared axis (drag on the overview strip
at the bottom), hover tooltip listing phone and variants for that minute.

Panels, top to bottom (each with its own single y-axis, hairline grid, 2 px lines):

1. **Glucose** (the recorded curve, identical for all variants, drawn once in primary ink) with the target band
   (orig and emulated target as two faint bands when they differ), carbs as small triangles on the baseline, temp
   targets and profile switches as hairline event rules with labels at the top.
2. **Final ISF factor** (autoISF `final ISF factor`): phone grey, variants coloured; the cap value as a hairline
   with label `autoISF_max 1.3`, so "sitting on the cap" is visible as a flat run touching the line.
3. **insulinReq**: phone grey step line, variants coloured.
4. **SMB**: stems (lollipops, ≥ 8 px head) per loop; phone grey behind, variants in front with a 2 px surface ring.
   Below it a thin **difference strip**: diverging bars of `SMB_variant − SMB_phone` per loop, blue less / red more.
   This strip is the single most useful chart in the product: it shows *when* a variant diverges.
5. **Temp basal in force**: step lines, U/h.
6. **IOB** (recorded) with the emulated `iobTH` and its 130 % tolerance as two hairlines per variant.

Small multiples across *days* instead of across *variants* is a toggle: same panels, one column per day, one variant
vs phone. That is the "does it behave the same on a sport day" view.

Legend: always present (≥ 2 series), colour follows the variant for the life of the page (a variant deselected and
reselected keeps its hue; the fourth variant cannot be added, the picker says why and offers "replace").

### 2.4 "Why did it decide that?" — Loop drill-down

Click any loop in a timeline (hit area = the whole minute column, not the mark) or a row in the table. A side sheet
(full-screen on phone) shows:

* **Header**: time, BG, delta, IOB, COB, profile ISF/CR/basal in force, temp target if any.
* **Decision pair**: two columns, phone and variant — `insulinReq`, `SMB`, `maxBolus`, `TBR rate/duration`, `final
  ISF factor`, `eventualBG`, `minPredBG`, `minGuardBG`, with the cells that differ marked by a 3 px left stripe in the
  variant's colour (shape, not colour alone).
* **Prediction fan**: 48 × 5 min, IOB / ZT / UAM / COB predBGs. Phone in grey (solid IOB, dashed ZT, dotted UAM),
  variant in its colour with the same dash coding; the 3.8 / 5.4 mmol guards as hairlines. Only one chart, one
  axis; the fan is where the "why" lives (e.g. *"minGuardBG 3.6 < 3.8 — SMB disabled"*).
* **ISF factor chain**: a horizontal dumbbell per factor (acce, bg, pp, dura, drift → final), phone dot grey, variant
  dot coloured, connected; shows which sub-factor the setting moved.
* **Explanations**: the original `reason` and consoleError block and the emulated one, as text, with a word-level
  diff highlight (added in variant colour tint, removed struck). The emulator's flowchart boxes (`Flows`) appear as an
  indented list with their JS line references, collapsed by default.
* Keyboard: `←`/`→` move one loop, `[`/`]` move to the previous/next loop where the decision differs.

### 2.5 "What should I change?" — Candidate card

A plain-language card per Pareto candidate, generated by the Python layer (the skill may rewrite it, but the numbers
are the Python's): *"Candidate 7: dura_ISF_weight 0.5 (phone: 0.7), autoISF_max 1.4 (phone: 1.3). On the 6 replayed
days the loop would have asked for 0.9 U/day less, all of it in rising phases, in 41 loops with a smaller SMB and 3
with a larger one; 0 additional low-side flags; the factor sat on the 1.4 cap 4 % of the time (1.3 cap: 9.7 %). Rank
2 on workdays, 5 on the sport day. Not distinguishable from Candidate 3 within the noise floor."* Buttons: *Copy
VDF*, *Show AAPS preference names*, *Open the 10 loops that differ most*. No "apply" button exists.

### 2.6 What is deliberately not on the page

* No glucose-outcome numbers (no TIR, no predicted HbA1c, no "would have avoided X hypos"). The emulator cannot
  produce them; a page that showed them would be lying with a chart.
* No single "score" headline. The leaderboard shows the objective components; ranking is Pareto plus a stated rule
  (section 4.3), and the rule is printed next to the table.
* No dual-axis chart anywhere, and no colour-cycled series. The matplotlib PDF stays available (`pred` option of the
  old front ends) for anyone who wants it; Hindcast does not call `XYplots()` at all.

---

## 3. Rendering

### 3.1 Library choice under the Artifact constraints

Constraints: scripts only from cdnjs / jsdelivr / unpkg with exact pinned versions, stylesheets only from Google
Fonts, everything else inline, ≤ 16 MB page, must work light and dark and at 400 px, must also open as a local file.
Data volume: one day ≈ 1 300 loops; a study ≈ 6 days × 30 variants × ~20 series = ~5 M numbers at full resolution,
far more than one page should embed, so lazy loading of per-variant series is required whatever the library.

| | ECharts 5 | Plotly.js 2 | Vega-Lite 5 | Observable Plot 0.6 |
|---|---|---|---|---|
| CDN / size | cdnjs `echarts/5.5.1/echarts.min.js`, ~1.0 MB, one script, UMD global | cdnjs `plotly.js/2.35.2/plotly.min.js` ~3.6 MB (basic bundle ~1 MB lacks parcoords) | three scripts (vega, vega-lite, vega-embed), ~0.9 MB total | jsdelivr `@observablehq/plot@0.6.16` + `d3@7`, ~0.5 MB |
| Many points (10 k–100 k) | canvas, `large` mode, `sampling: 'lttb'` — smooth | needs `scattergl`; fine | SVG/canvas, slow past ~20 k marks | SVG, slow past ~10 k marks |
| Linked crosshair across stacked panels | built in (`axisPointer.link`, multi-grid in one instance) | `hovermode: 'x unified'` across subplots, works | selection params, needs layered spec | `crosshair` mark per plot, no cross-plot link without custom code |
| Parallel coordinates | built in, with brushing | built in (`parcoords`) | not idiomatic | not available |
| Heatmap with diverging scale | built in (`visualMap`, piecewise or continuous) | built in | built in | `cell` mark, good |
| Small multiples | manual grid array in one instance, or one instance per facet | subplots | native facet (its strongest point) | native facet (its strongest point) |
| Dark mode | theme object built from CSS tokens at init; `setOption` on theme change | template per theme, `relayout` | `config` per theme, re-embed | re-render |
| Interactions beyond hover | `dataZoom`, brush, click events, `connect` between instances | zoom/pan, selection | brush/param | limited |
| Phone width | responsive via `resize()`, touch dataZoom | heavy on phone | ok | ok |

**Decision: ECharts 5.5.1 as the only chart library**, for four reasons: (1) one pinned script, small, on the preferred
CDN; (2) the three chart forms the product needs that are hard elsewhere — multi-panel timelines with one linked
crosshair and a shared dataZoom, parallel coordinates with brushing, heatmaps with a diverging `visualMap` — are all
built in; (3) canvas performance comfortably covers a week of 1-minute loops for four series without downsampling
tricks that would hide single SMB stems; (4) a dark theme can be derived from the page's CSS tokens at init, so the
chart and the page are one system. Vega-Lite and Plot lose on parallel coordinates and linked multi-panel crosshairs;
Plotly wins nothing ECharts lacks and costs 3.6 MB of script and a heavier phone experience.

Everything that is not a chart is plain HTML/CSS: stat tiles, the decision pair, the ISF dumbbells (inline SVG, 6
rows, trivial), the text diff, the flowchart list. This follows the dataviz skill's "components in plain HTML" rule
and keeps the explanation panel readable without any library.

### 3.2 Page shape and data loading

* Authored as `.html` with `<title>` (the study name, e.g. `Hindcast autoISF October`), the token block at the top,
  `<meta name="claude:viewport-fit-sides" content="cover">` with the two `max(16px, env(...))` gutters.
* **Data split into two tiers.**
  * *Tier 1, embedded*: `study.json` (windows, parameters, noise floor, candidate cards) and `trials.json` (one row
    per trial: parameters, day, all metrics) — tens of kB for hundreds of trials. The parameter-space views and the
    leaderboard work from this alone.
  * *Tier 2, lazy*: `series/<day>/<variant>.json` — columnar arrays, values rounded (BG 1 dp, insulin 3 dp, factors
    2 dp, times as offsets from the day start in seconds) — ~60–90 kB per day-variant gzip-equivalent; the phone's
    series (`series/<day>/orig.json`, including recorded BG, IOB, COB, events and the original predBGs for every
    loop) once per day; emulated predBGs per day-variant in `pred/<day>/<variant>.json` (48 × 4 arrays per loop, the
    largest file, ~1 MB per day-variant, loaded only when a loop is opened).
  * The page `fetch()`es tier 2 with relative URLs, which works for files published alongside the artifact via
    `files`. Opened from disk, Chrome blocks `file://` fetches, so the generator has `--single-file`, which inlines
    tier 2 for the selected days/variants as `<script type="application/json" id="...">` blocks and warns when the
    page would exceed 12 MB; local viewing of a whole study instead uses `hindcast serve` (stdlib `http.server`, one
    line).
* Theme: tokens on `:root`, dark under `@media (prefers-color-scheme: dark)` guarded `:root:not([data-theme="light"])`
  and again under `:root[data-theme="dark"]`; the ECharts theme is built once from `getComputedStyle` and rebuilt on a
  `matchMedia` change or a `data-theme` mutation. Body gets an explicit background token.
* State: selected day, variants, zoom and open loop live in the page; a bare `#loop-1788685237` anchor deep-links a
  loop. `localStorage` only remembers the last tab (wrapped in try/catch).
* Live mode (optional, section 6): when `claude.use("db")` resolves, the leaderboard subscribes to
  `studies/<id>/trials` and shows new trials as they arrive, with a "live, 23 trials since you opened this" chip; the
  static tier 1 remains the fallback when `db` is `null`.

### 3.3 Colour and marks (validated, dataviz skill)

* **Form is "emphasis"**: the phone's decision is the context (neutral grey `#898781`, same in both modes), each
  selected variant is an entity in categorical slot 1–3 of the reference palette: blue `#2a78d6`/`#3987e5`, orange
  `#eb6834`/`#d95926`, aqua `#1baf7a`/`#199e70` (light/dark). Validator run on 2026-10-16: all-pairs CVD ΔE 9.2 light /
  9.4 dark, normal-vision 24.0 / 20.9, both PASS; the aqua slot sits at 2.74:1 on the light surface, so direct
  labels at the line ends and the table twin are mandatory (they are in the design anyway). The fourth slot is
  refused by the picker (yellow next to orange fails all-pairs). Colour follows the variant, never its rank.
* **Diverging**: blue (less insulin than the phone) ↔ red (more), grey midpoint `#f0efec`/`#383835`; used for the SMB
  difference strip and the delta heatmap. **Sequential**: the blue ramp 100→700 for magnitudes.
* **Status**: good/warning/serious/critical from the fixed status palette, only for the low-side risk flags, always
  with an icon and a label. Never used for a series.
* Marks: 2 px lines, 1 px for the parallel-coordinate lines, stems with ≥ 8 px heads for SMBs, 2 px surface gaps
  between heatmap cells and a 2 px surface ring on overlapping markers, solid hairline grid one shade off the surface,
  no dashed grid (dashes are reserved for the ZT/UAM prediction coding and explained in the legend).
* Type: one UI sans (system stack) for everything including the stat tiles; `tabular-nums` only in tables and axis
  ticks.
* Table twin for every chart; keyboard focus shows what hover shows; `prefers-reduced-motion` disables the zoom
  animation.

---

## 4. Objective: what Hindcast measures and what it is allowed to claim

### 4.1 Ground rule

Every metric is a **decision-level counterfactual on recorded inputs**. The replay feeds each variant the glucose,
IOB and COB history that was recorded while the phone ran its own settings. So:

* a variant that boluses earlier is charged for its early bolus *and* for the later boluses the phone really gave
  (because IOB did not rise in the replay); deltas overstate aggressive settings and understate gentle ones;
* "risk" can only be a proxy built from the emulated prediction arrays and the recorded glucose, never a count of
  hypos avoided or caused;
* the base variant's disagreement with the phone (~4 % of loops, +4 U/day SMB on 2026-09-06) is the measurement
  noise and is subtracted and displayed, never hidden.

The page, the CLI output and the skill repeat the scope line of section 2.1 verbatim.

### 4.2 Metrics per trial (`hindcast/metrics.py`, versioned `OBJECTIVE_VERSION = 1`)

Reused from `emulator_optimize.metrics_from_csv()` (imported, not copied; the module has a `__main__` guard):
`loops`, `smb_*`, `tbr_*`, `total_*`, `delta_U`, `smb_more/less/equal`, `isf_factor_*`, `at_autoISF_max/min_pct`
(cap per loop via `effective_cap()`), `iob_over_TH_pct`, `rise_*`, `rest_*`, `bg_*`. Added:

| metric | definition | why |
|---|---|---|
| `changed_loops` | loops where SMB, insulinReq (0.05 U) or TBR rate (0.05 U/h) differ from the phone | the raw size of the counterfactual; base's value is the noise count |
| `changed_vs_base` | `changed_loops − changed_loops(base)` on the same day | effect credibility |
| `rise_vs_base_U`, `rest_vs_base_U` | `rise_emul − rise_base`, `rest_emul − rest_base` | the two sides of "when does the extra insulin come" |
| `lowside_flags` | emulated SMB > phone SMB while (`minGuardBG_emul < 3.9 mmol` or recorded BG < 5.0 mmol and falling) | the honest low-side proxy, from the emulated predictions and recorded glucose only |
| `zero_temp_vs_base` | loops with an emulated 0 U/h temp where base had none, minus the reverse | high-side caution proxy |
| `cap_binding_pct` | `at_autoISF_max_pct` and `at_autoISF_min_pct` | a setting at its cap is a setting that was not really tested |
| `max_single_smb_U` | largest emulated SMB | safety reading for a human |
| `agreement_pct` | loops where all four quantities equal the phone's within tolerance | only meaningful for base; shown as the noise floor |

All metrics are computed per *day-variant* and aggregated over the study's days as mean and range; rank stability
across days is `spearman(rank_day_i, rank_day_j)` on `rise_vs_base_U`.

### 4.3 Ranking rule (printed next to the leaderboard, never a hidden scalar)

1. **Feasible** = `lowside_flags ≤ lowside_flags(base) + floor` on every day and `max_single_smb_U ≤ phone's
   maxBolus` and `cap_binding_pct < 50 %`.
2. Among feasible variants, the **Pareto front** of (maximise `rise_vs_base_U` responsiveness, minimise
   `rest_vs_base_U`, minimise `lowside_flags`). For an optimiser that needs a scalar (TPE), the scalarisation is
   `rise_vs_base_U − 2·max(rest_vs_base_U, 0) − 0.3·lowside_flags`, with the weights in `study.json` and shown on
   the page; NSGA-II needs none.
3. Variants whose objective difference is inside the noise floor are reported as a **tie**.

The user may invert the direction (looking for *less* insulin in rising phases, e.g. for someone fighting lows); the
direction is a study setting, visible on the page.

### 4.4 Noise floor (computed per study)

From the base replay over the study days: `floor_U = 2·stdev(delta_U(base) per day)` (minimum 0.3 U) and
`floor_loops = max(changed_loops(base))`. Plus the **sentinel trial**: a variant that sets exactly the phone's
settings as explicit VDF rows must reproduce base to the loop (this already exists as a consistency check in
`emulator_optimize`; here it runs automatically every 25 trials and aborts the study on failure — state leak
detection for a multi-hour run).

### 4.5 What the page may say and may not say

May: "would have asked for", "would have microbolused", "the factor would have sat on the cap", "the loop would have
disabled SMB in N more loops", "indistinguishable within the noise floor".
May not: "better control", "fewer lows", "safer", any glucose or time-in-range figure, any statement about the future.
These phrases are a lint list in `report.py`; the generator refuses to render a candidate card that contains one.

---

## 5. Architecture

### 5.1 Module layout (`software/hindcast/`, PC only, may use `.venv` libraries; `ns_*` untouched)

```
software/hindcast/
  __init__.py
  replay.py       run_trial(window, vdf_lines, outputs) -> TrialResult
                  wraps emulator_core.parameters_known() exactly as emulator_optimize.main() does;
                  afterwards reads the CSV (emulator_optimize.read_result_csv), the .txt transcript,
                  and emulator_core.Pred / emulator_core.FlowChart (module globals, keyed round(ms/1000,1))
                  for emulated predBGs, Fcasts and the flow boxes; writes series/pred files; deletes
                  the bulky .txt/.delta/.log unless --keep-transcripts
  metrics.py      metrics_from_csv (imported from emulator_optimize) + the section 4.2 additions,
                  noise floor, Pareto, ranking rule, OBJECTIVE_VERSION
  study.py        Study dir on disk: study.json, trials.jsonl (append-only), hashes, resume, sentinel
  space.py        parameter space: ranges/choices per '<array> <item>' key, STAIR (time-of-day) params later
  search.py       strategies: oaat, grid, lhs, coordinate, optuna (TPE scalar / NSGA-II multi-objective)
                  optuna is optional; without it oaat/grid/lhs/coordinate run on stdlib
  pool.py         process pool: one emulator_core per worker process (module-global state), cache
                  pre-warm done serially before the pool starts
  export.py       tier-1 and tier-2 JSON, db rows (leaderboard), candidate cards, VDF copy, pref names map
  report.py       HTML page from templates/ (page.html, page.css, page.js, echarts theme builder),
                  --single-file inliner, phrase lint
  cli.py          python -m hindcast ... (also installed as `hc` via a tiny console script in .venv)
  templates/      page.html  page.css  page.js  (the page is hand-written once; no JS build step)
  prefs_map.json  VDF key -> AAPS preference title as shown on the phone (for the candidate card)
software/studies/<study>/      git-ignored, see 5.3
```

### 5.2 Data flow

```
ns_cache (final days)          study.json (space, days, objective, phases)
        │                               │
        ▼                               ▼
   pool.py workers ──► replay.py ──► parameters_known(url, 'Windows/<outputs>/noplot/.', vdf, start, end, {}, msg, '.', epoch)
                          │             │  (unchanged core; writes <day>.<variant>.csv/.txt/.log/.delta next to the VDF)
                          │             ▼
                          ├── metrics.py  ──► trials.jsonl (one line per day-variant, appended atomically)
                          ├── series/<day>/<variant>.json, pred/<day>/<variant>.json
                          └── (first trial of a day) series/<day>/orig.json from the CSV + ns_cache devicestatus predBGs
                                        │
   search.py (ask/tell) ◄───────────────┘  next parameters; optuna storage = studies/<study>/optuna.sqlite3 or journal
                                        │
                                        ▼
   export.py ──► report/index.html (+ series/, pred/ as artifact files)  ──► Artifact publish (agent)
            └──► leaderboard rows (JSON) ───────────────────────────────► ArtifactData batch (agent)
```

`parameters_known()` is called with the identical argument shape `emulator_optimize.main()` uses today
(`set_tty(0, 0, 'batch')` once per worker, `entries = {}` per call, `'.'` decimal, `noplot` in the option string).
The optimizer already proved that sequential calls in one process are clean (identical loop counts, sentinel check);
Hindcast keeps that and adds processes for parallelism.

What is read from `emulator_core` after a run and could break on an upstream re-upload: `Pred`, `FlowChart`
(globals), the CSV header words. Issue #1 in the GitHub plan asks the core owners for a one-function accessor
(`last_run_artifacts()`) so the dependency becomes explicit; until then `replay.py` guards every access and falls
back to "no predictions for this trial" rather than failing the study.

### 5.3 File formats

`studies/<study>/study.json`
```json
{"name": "autoISF_oct", "objective_version": 1, "created": "2026-10-16T19:10:00Z",
 "source": "ns:", "settings_hash": "1a4869fdc61c", "core_versions": {"emulator_core.py": "2026-09-08 04:00", "determine_basal.py": "2026-09-08 04:00"},
 "days": [{"start": "2026-09-06T00:00:00Z", "end": "2026-09-07T00:00:00Z", "tags": ["workday"]}, {"start": "2026-09-05T00:00:00Z", "end": "2026-09-06T00:00:00Z", "tags": ["weekend", "sport"]}],
 "space": {"profile autoISF_max": {"type": "float", "low": 1.1, "high": 1.8, "step": 0.05},
           "profile dura_ISF_weight": {"type": "float", "low": 0.3, "high": 1.0, "step": 0.05},
           "profile enable_pp_ISF": {"type": "bool"}},
 "base_vdf_lines": [],
 "objective": {"direction": "more_in_rise", "scalar_weights": {"rest": 2.0, "lowside": 0.3}, "feasible": {"cap_binding_max_pct": 50}},
 "phases": [{"kind": "oaat", "points": 5}, {"kind": "lhs", "n": 24}, {"kind": "optuna", "sampler": "nsga2", "n": 200}, {"kind": "validate", "top": 8, "days": "holdout"}],
 "budget": {"hours": 6, "workers": 6}}
```

`studies/<study>/trials.jsonl` — one JSON object per line, append-only, written via a temp file + `os.replace` of a
sidecar index to stay valid after a kill:
```json
{"trial": 37, "key": "f3a9…", "phase": "lhs", "variant": "autoISF_max_1.45__dura_ISF_weight_0.55", "params": {"profile autoISF_max": 1.45, "profile dura_ISF_weight": 0.55},
 "day": "2026-09-06", "status": "done", "seconds": 19.4, "metrics": {"loops": 1311, "changed_loops": 97, "rise_vs_base_U": -0.41, "...": 0},
 "files": {"csv": "runs/2026-09-06/…csv", "series": "series/2026-09-06/autoISF_max_1.45__dura_ISF_weight_0.55.json"}}
```
`key = sha1(params | day | settings_hash | core_versions | objective_version)`; a trial whose key exists with
`status: done` is never re-run (resume, and dedupe across phases).

`report/series/<day>/<variant>.json` — columnar:
```json
{"day": "2026-09-06", "variant": "…", "t": [37, 100, 161, …], "smb": [0, 0, 0.05, …], "ins_req": [...], "tbr": [...], "isf_factor": [...], "isf": [...], "iob_th": [...], "tar_lo": [...], "tar_hi": [...], "changed": [0, 0, 1, …]}
```
`report/series/<day>/orig.json` adds `bg`, `iob`, `cob`, `events` (carbs, TT, profile switch, CGM gaps) and the phone's
`smb/ins_req/tbr/isf_factor`. `report/pred/<day>/<variant>.json`: `{"t": [...], "iob": [[48 values], …], "zt": …, "uam": …, "cob": …, "reason": [...], "flows": [...]}`; `pred/<day>/orig.json` holds the phone's predBGs and reasons.

Artifact `db` collections (section 6): `studies/<study>` (one doc: study.json minus the space details),
`studies/<study>/trials/<key>` (the trials.jsonl line without `files`), `studies/<study>/cards/<rank>`.

### 5.4 CLI

```
hc study new autoISF_oct --days 2026-09-01..2026-09-07 --tag 2026-09-05=weekend,sport \
      --param "profile autoISF_max" 1.1:1.8:0.05 --param "profile dura_ISF_weight" 0.3:1.0:0.05 \
      --param "profile enable_pp_ISF" bool --objective more_in_rise
hc study run autoISF_oct --hours 6 --workers 6          # resumable; Ctrl-C or kill at any time
hc study status autoISF_oct                             # phase, trials done, best candidates, noise floor
hc report autoISF_oct [--single-file --days 2026-09-06 --variants top3]   # -> studies/autoISF_oct/report/
hc serve autoISF_oct                                    # local http.server on the report dir
hc explain autoISF_oct 2026-09-06T09:00:37Z --variant <name>   # the drill-down as text (for the skill and for grep)
hc export autoISF_oct --db-rows > rows.json             # what the skill pushes with ArtifactData batch
hc warm 2026-09-01..2026-09-07                          # serial cache pre-fill, refuses non-final days unless --allow-partial
```
The legacy `emulator_optimize.py` keeps working unchanged; `hc study new --from-grid variants/grid_autoISF.json`
imports a grid file as a `grid` phase, so nothing the user has is lost.

### 5.5 Running unattended for hours

* **Offline by construction**: `hc warm` is the only step that talks to Nightscout. It walks the days serially
  through the existing `ns_client` (1.5 s × attempt back-off already there) and refuses days that are not *final*
  (written > 1 h after the UTC day ended) unless asked, because a non-final day file is re-fetched every 5 min by
  `ns_client` and would turn a long study into a polling job. The runner sets `HINDCAST_OFFLINE=1`, and `replay.py`
  checks before each trial that every day file of the window is final; if not, the trial is skipped with status
  `blocked`, not fetched.
* **Workers**: `multiprocessing` with the `spawn` start method, each worker imports `emulator_core` once and calls
  `set_tty`. Trials are day-variant pairs, so a 6-day study parallelises 6-wide even with one variant in flight.
  Measured ~20 s per day-variant with `noplot` (the 2 min 20 s figure is with the PDF); 6 workers ≈ 1 000 day-variants
  per hour on an 8-core laptop, i.e. ~170 variants over 6 days per hour.
* **Disk**: per day-variant the core writes ~1 MB of `.txt/.delta/.log/.csv`; Hindcast keeps the CSV (100 kB) and the
  two JSON files (~1.2 MB with predictions) and deletes the rest unless `--keep-transcripts`; a 6-hour run is ~8 GB
  with predictions, ~1 GB with `--no-pred` (predictions only for the final top-N, re-run at the end).
* **Resume**: `trials.jsonl` is the truth; Optuna's storage is a journal file in the study dir and is rebuilt from
  `trials.jsonl` if it is missing or inconsistent. `hc study run` on an existing study continues the current phase.
* **Guards**: sentinel trial every 25 trials; loop-count equality across variants of a day; wall-clock budget;
  `SYNTAX`/`Z` sentinels from `parameters_known` are recorded as `status: failed` with the `sub_issue` text captured
  from the `set_tty` channel, and the search is told the trial is infeasible rather than crashing.
* **Reporting cadence**: the runner regenerates `report/` (tier 1 only, cheap) every 10 trials and on exit; tier-2
  files are written per trial, so a republish of the artifact at any time shows a consistent snapshot.

---

## 6. The Claude skill and the long loop

`~/.claude/skills/hindcast/SKILL.md` (project-scoped under `.claude/skills/` once the repo owners agree; until then
user-scoped). The skill does not do the search; Python does. The skill decides, publishes and explains.

Procedure the skill encodes:
1. **Frame**: read `ns_settings.json` for the current phone settings and `profile_history`; ask once for the days
   (or pick the last 7 final days from the cache listing) and the parameters to search; refuse parameters outside the
   VDF groups; write `study.json`; print the scope line.
2. **Warm and baseline**: `hc warm`, then the base and sentinel trials on all days; show the noise floor; stop if the
   base agreement is below 90 % on any day (something upstream changed; the study would measure the emulator, not the
   settings).
3. **Run**: start `hc study run --hours N` in the background (Bash `run_in_background`), then check in on a cadence
   (`/loop 20m` or the `schedule` skill for cloud): `hc study status`, publish the report (first time: new Artifact
   with `files` for the tier-2 directories; afterwards republish to the same URL), push new leaderboard rows with
   `ArtifactData batch` so the live page updates without a republish, and post one short status line.
4. **Review**: when the budget is used or the Pareto front has not changed for two phases, run the `validate` phase
   on the hold-out days, regenerate the candidate cards, read the top-10 loops that differ most via `hc explain`, and
   write the summary for the human, in the allowed vocabulary (section 4.5), with the VDF of each candidate attached.
5. **Never**: write to the phone, Nightscout or `ns_settings.json`; claim glucose outcomes; rank inside the noise
   floor; delete `trials.jsonl`.

What the skill may write into the artifact database: `studies/<study>` and `studies/<study>/trials/<key>`
(leaderboard rows) and `studies/<study>/cards/<rank>`; rules `read: view`, `write: owner`; the page declares
`capabilities: {db: {}, user: {}}` and renders fully from the embedded tier 1 when `db` resolves `null`. The database
is a mirror of `trials.jsonl`, never the source of truth, because Python cannot write to it — only the agent can, so
the bridge is explicit and the page says *"live rows via Claude, last push 19:42"*.

Why this split is the right product: a multi-hour optimisation needs a process that survives the agent's context
and the user's laptop lid (Python, resumable), while the parts that need judgement — which days, which parameters are
even sensible to vary, when to stop, how to phrase the result — are the agent's, done at check-in cadence and
visible on one page the user can open from the phone.

---

## 7. Search strategy

Replay cost is ~20 s per day-variant, so the budget in a 6-hour evening is ~6 000 day-variants, or ~1 000 variants
over six days. That is enough for a careful three-phase search, not for brute force over ten parameters.

1. **One-at-a-time sensitivity** (5 points per parameter, all days): finds which parameters move decisions at all on
   these days. The example in NIGHTSCOUT_SOURCE.md shows the trap — `autoISF_max` 1.3 → 1.5 changed +0.01 U on a
   morning where the factor rarely reached 1.3. A parameter whose five points all fall inside the noise floor is
   frozen at the phone's value and reported as "not testable on these days" (that is a result, not a failure).
2. **Latin hypercube** over the sensitive parameters (24–48 points) for an unbiased picture of the space; this is
   what the parallel coordinates and heatmaps show first.
3. **Optuna**: NSGA-II (multi-objective on the three section-4.3 objectives) by default, or TPE on the scalarisation
   when the user wants one number; ask-and-tell interface so the pool stays in control; journal storage in the study
   dir for resume. Each suggested variant runs on all study days before it is told back (the objective is the
   across-day aggregate; per-day values are kept for the stability metric).
4. **Validate**: top-N re-run on hold-out days the search never saw (the skill reserves 1–2 days of different
   character in step 1); the rank stability (Spearman) between search days and hold-out days is on the candidate
   card. Also the predictions-on, full-transcript re-run of the top-N for the drill-down.

Time-of-day parameters (`STAIR_ISF`, `INTERPOL`) are phase-2 of the product: the space type `stair` with 2–4 knots,
rendered as a 24-hour ribbon on the candidate card.

---

## 8. Team and GitHub issues (markusbaumg/APS-what-if)

Milestone **Hindcast MVP** (issues 1–8), milestone **Hindcast search** (9–12), milestone **Hindcast live** (13–15).

1. `core`: expose `Pred`/`FlowChart`/CSV columns through one accessor after `parameters_known()` (small, for the
   repo owners; Hindcast guards the globals until then).
2. `metrics.py`: import `emulator_optimize` metrics, add section 4.2 metrics, noise floor, ranking rule, tests on the
   two CSVs in `variants/` and `results/example_2026-09-06/`.
3. `study.py` + `pool.py`: study dir, `trials.jsonl`, keys, resume, spawn pool, sentinel, offline guard.
4. `replay.py`: wrap `parameters_known()`, write tier-2 JSON, delete transcripts, failure capture.
5. `search.py`: oaat, grid (imports grid JSON), lhs, coordinate; Optuna adapter behind an optional import.
6. `templates/`: the page — overview, parameter space, small multiples, drill-down, candidate card; ECharts 5.5.1;
   tokens and dark theme; table twins; phone layout.
7. `report.py` + `export.py`: tier split, `--single-file`, phrase lint, VDF/pref-name export, `hc serve`.
8. Docs: `software/HINDCAST.md` (contract like NIGHTSCOUT_SOURCE.md), CLAUDE.md section, README paragraph.
9. Optuna NSGA-II/TPE phase, hold-out validation, Spearman stability.
10. STAIR/time-of-day parameter type and ribbon.
11. Logfile source for Hindcast (the other branch's `scanLogfile` path; same `parameters_known` call).
12. Benchmark and profile `parameters_known` for the `noplot` path (if 20 s → 8 s, everything doubles).
13. Skill `hindcast` (SKILL.md, check-in cadence, vocabulary lint, ArtifactData push).
14. Live leaderboard via `db` + `onSnapshot`; `room` presence optional.
15. Candidate card copy review with the user; wording list in `report.py` finalised.

Agents: **viz/report** (6, 7), **runner** (3, 4, 5, 12), **metrics/objective** (2, 9), **skill** (13, 14), **docs**
(8, 15); one integrator owns `study.json` and the `trials.jsonl` schema, both frozen on day one so the five can work in
parallel against fixtures (`results/example_2026-09-06/` is a ready fixture with five CSVs).

---

## 9. MVP vs later

**MVP (first working evening, issues 2–8):** `hc study new/run/report/serve`; oaat + grid + lhs phases; six workers;
metrics with noise floor and ranking rule; the full page with the four views and the candidate card; publish as an
Artifact with tier-2 files; run from a Claude session by hand. Acceptance: re-run the documented example
(`autoISF_max` × `dura_ISF_weight`, 2026-09-06) and show base ≡ sentinel, the +1.37 U bias on the banner, the
`dura_ISF_weight 0.5` effect as 9 red stems in the difference strip in the rising phase, and the 09:00 loop's
`minGuardBG 3.6 < 3.8` explanation in the drill-down.

**Later:** Optuna phases and hold-out validation (9); the skill and the live `db` leaderboard (13, 14); time-of-day
parameters (10); logfile source (11); profiling (12); multi-study comparison (same page, two studies side by side,
e.g. before/after a profile change); export of the candidate as an annotated AAPS preferences diff (never an import).

---

## 10. Risks and honesty, in one place

* **The emulator cannot simulate glucose.** Every output is "what the loop would have asked for"; aggressive variants
  are overstated, gentle ones understated. The page says so on every view; the vocabulary is linted.
* **The noise floor is real and shown.** ~4 % of loops and about +4 U/day of SMB on a sample day separate the base
  replay from the phone (SMB-interval tolerance, rounding). Variants are compared to base, and differences inside
  the floor are ties.
* **Days are samples.** Six days are six days. The page shows per-day spread and hold-out ranks; the skill picks days
  of different character and reserves hold-outs.
* **Safety of the human workflow.** No apply button, no write to the phone or Nightscout, candidate cards state the
  phone's current value next to the proposed one, and the maximum single emulated SMB is always printed.
* **Technical**: the dependency on `emulator_core` globals (issue 1), the CRLF/LF and whole-file re-upload workflow
  upstream (Hindcast lives in its own directory and touches nothing else), concurrency on module-global state (one
  core per process, sentinel checks), disk growth (transcripts deleted, predictions deferred to the top-N), and the
  Artifact page budget (tier split, 12 MB warning).
* **Not medical advice.** Hindcast is a tool for a person with T1D and their care team to look at how a DIY loop would
  have decided differently; it recommends nothing on its own authority and changes nothing.
