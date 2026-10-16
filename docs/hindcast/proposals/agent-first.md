# Counterloop - an agent-driven settings bench on top of the AAPS what-if emulator

Design proposal, angle "agent/skill first". Written 2026-10-16 against branch `nightscout-source`
(emulator_core 2026-09-08 04:00, emulator_optimize 2026-09-08 12:00). Nothing in the repo was edited for this document.

---

## 0. The one-paragraph version

Counterloop is a new layer under `software/counterloop/` that turns the existing emulator into a long-running,
resumable **settings bench**. A Claude Code session (through a `counterloop` skill) runs *campaigns*: it picks a
question ("which autoISF weights would have changed anything in the last two weeks?"), a set of cached Nightscout days
and a search space, then drives the emulator in a loop for hours - replaying every variant on every day, writing one
row per (variant, day) into a SQLite ledger as it goes, reading back a 2 KB `round.json` instead of CSVs, deciding the
next round (drop dead parameters, vary the cap that saturates, re-test a promising value on more days), and keeping two
surfaces current: a GitHub issue per campaign (the task board and audit trail) and a published HTML dashboard (Plotly.js,
static or live via the artifact database). Every number is shown next to the **noise floor** measured on the same days
(the empty VDF reproduces the phone in ~96 % of loops), the objective is a multi-dimensional scorecard rather than a
single "less insulin wins" scalar, and the end product is a **proposal issue** for a human to read: "on these N days
the loop would have asked for X U more, here, for these reasons". Counterloop never writes to the phone or to Nightscout.

---

## 1. Names

All five were checked with a web search for collisions with diabetes / insulin / AAPS software; none turned up a product.

| name | why | note |
|---|---|---|
| **Counterloop** (preferred) | "counterfactual loop": it reruns the loop under a different premise. One word, says exactly what the tool does, pairs with the verb "counterloop a day". CLI `counterloop`, skill `/counterloop`. | no hits; `counterloop` on PyPI must be checked before packaging |
| **Parallax** | the same point seen from a second position shows depth; the phone's decision and the variant's decision are two viewpoints on the same minute. Elegant, but generic (many "Parallax" products outside diabetes). | no diabetes hits |
| **Loopwright** | a wright *makes* and *mends* loops; the tool is a workshop for loop settings. Friendly, searchable, clearly not a medical device. | no hits |
| **Otherwise** | "what would the loop have done otherwise" - the question in one word. Great tagline ("Otherwise for AAPS"), poor as a CLI/search term. | too generic to search |
| **Replay Bench** | descriptive two-word fallback; "bench" signals a test bench, not a pump controller. | no hits |

The rest of this document says Counterloop.

---

## 2. What exists and what the new layer reuses

Reused as-is (no changes needed for the MVP):

| existing | role in Counterloop |
|---|---|
| `emulator_core.parameters_known(url, 'Windows/<opts>/noplot/.', vdf, start, end, {}, msg, '.', epoch)` | the one replay call per (variant, window). Sequential calls in one process are safe (`emulator_optimize.py` already relies on it). `set_tty(0, 0, 'batch')` once per process. |
| `emulator_optimize.metrics_from_csv / read_result_csv / write_vdf / variant_name / vdf_value / effective_cap` | CSV -> metrics dict; VDF generation (base lines + one row per parameter); per-loop cap from `profile_history`. Imported, not copied. (Importing `emulator_optimize` executes `os.chdir(HERE)` and sets `PYTHONUTF8`; Counterloop does the same, so this is harmless.) |
| `ns_client.NightscoutClient` + `ns_cache/` | the only network path. Day files are final one hour after the UTC day ended; non-final files refresh every 5 min. Counterloop's prefetch step is the only phase that touches the network. |
| `ns_source.window_label`, `ns_settings.json` (`profile_history`, `_assumed`) | result-file naming; confounder tracking (settings the phone actually changed inside a window). |
| `results/<...>/<start>_<end>.<variant>.txt` transcripts | per-loop evidence the agent opens for the *few* loops that differ, never wholesale. |

Replaced / superseded:

* `summary.txt` / `summary.csv` per grid run -> one SQLite ledger across all campaigns (`emulator_optimize.py` stays as a
  one-shot CLI; later it can become a thin wrapper that records into the ledger too).
* matplotlib PDF + `LIST` console table as the *reporting* surface -> Plotly.js HTML dashboard. The PDF stays available
  for single-loop forensics (`pred`/`flowchart`); Counterloop does not call it.

Needs a small, coordinated core change later (not MVP, filed as an issue because other agents are editing the core now):

* **`emulator_core.loop_hook`** - an optional callable `loop_hook(loop_index, Fcasts, rT_orig, rT_emul)` invoked at the
  end of `TreatLoop()`. It exposes the emulated prediction arrays (`IOBpredBGs`, `UAMpredBGs`, `ZTpredBGs`, `COBpredBGs`)
  and `Levels` (`minIOBPredBG`, `minZTGuardBG`, ...) which today exist only inside the PDF path. Ten lines, default
  `None`, no behaviour change. Until it lands, the hypo-guard metrics use the *original* `predBGs` from devicestatus.

---

## 3. Module layout

```
software/
  counterloop/                      new package; .venv libraries allowed (not phone code)
    __init__.py
    __main__.py                     python -m counterloop <command>  (argparse, subcommands below)
    campaign.py                     campaign spec, state machine, round planning (strategy plug-ins)
    runner.py                       one (variant, window) replay -> run record; optional process pool
    store.py                        SQLite ledger (results/counterloop/ledger.sqlite), atomic STATE.json
    objective.py                    metrics -> scorecard; noise floor; promotion rules
    days.py                         cached-day inventory, finality, day fingerprints, stratified day sets
    search.py                       strategies: screen (OAT), grid, coordinate_descent, halving
    report.py                       dashboard-data.json + static HTML (Plotly.js), proposal Markdown
    gh.py                           GitHub issues via `gh` CLI: campaign issue, round comments, proposal issues
    agentio.py                      the compact JSON the agent reads (round.json, status.json, show --loops-differ)
    templates/
      dashboard.html                page skeleton (tokens, light/dark, phone width), data injected as JSON
      proposal.md.j2                proposal issue body
  campaigns/                        campaign specs (committed; they are the experiment protocol)
    autoisf_weights_screen.json
    smb_delivery.json
  results/counterloop/              git-ignored (results/ already is)
    ledger.sqlite
    <campaign>/STATE.json, STOP, rounds/round-001.json, ...
    <campaign>/variants/<vhash>/<vhash>.vdf + the emulator's own outputs per window
    dashboard/index.html, dashboard-data.json
.claude/skills/counterloop/
    SKILL.md                        the skill (section 6)
    references/metrics.md           glossary with directions and noise handling
    references/decision-rules.md    the round-planning procedure in prose + the stop conditions
    references/report-template.md   what a round comment / proposal must contain
```

Dependencies added to `requirements.txt` for the bench only: none mandatory for the MVP (sqlite3, json, concurrent.futures
are stdlib; the HTML template is a string). Optional later: `jinja2` for templates, `optuna` for the model-based strategy.

---

## 4. Data flow and file formats

```
 ns.vivikusi.ch ──GET only──> ns_cache/<host>/{entries,treatments,devicestatus}/<day>.json  (prefetch, once per day)
                                            │
   campaigns/x.json ──> campaign.py ──> round plan: [(variant, window)...]  ──> runner.py (1..N worker processes)
                                            │                                     │  parameters_known(... noplot)
                                            │                                     │  -> <start>_<end>.<v>.csv/.txt/.log
                                            │                                     │  metrics_from_csv + extras
                                            ▼                                     ▼
                     store.py: ledger.sqlite (runs, loops_differ, scorecards, events)  <── one INSERT per run, immediately
                                            │
          ┌─────────────────────────────────┼──────────────────────────────────────┐
          ▼                                 ▼                                      ▼
 rounds/round-N.json (agent reads)   report.py -> dashboard-data.json -> index.html   gh.py -> issue comment / proposal
                                                    │ (Artifact publish, or ArtifactData rows)
                                                    ▼
                                            human reviews, decides, changes phone by hand,
                                            records it in ns_settings.json profile_history
```

### 4.1 `campaigns/<name>.json` - the experiment protocol

```json
{
  "name": "autoisf_weights_screen",
  "question": "Which autoISF weights would have changed the loop's decisions on the last 10 workdays?",
  "base_vdf": "variants/no_change.vdf",
  "days": {"select": "cached_final", "from": "2026-08-30", "to": "2026-09-07",
           "strata": ["workday", "weekend"], "min_per_stratum": 2},
  "windows": "day",                                  
  "space": {
    "profile autoISF_max":        {"values": [1.2, 1.3, 1.4, 1.5, 1.6]},
    "profile dura_ISF_weight":    {"values": [0.3, 0.5, 0.7, 0.9]},
    "profile bgAccel_ISF_weight": {"range": [0.0, 0.2], "step": 0.03},
    "profile pp_ISF_weight":      {"values": [0.0, 0.009, 0.018, 0.027]}
  },
  "locked": ["safety *", "profile max_iob", "profile max_basal", "profile dia", "profile sens", "profile carb_ratio",
             "profile current_basal", "new_parameter *"],
  "strategy": {"name": "screen_then_descend", "oat_days": 3, "confirm_days": "all", "max_rounds": 12,
               "promote_if": {"consistent_sign_frac": 0.8, "effect_over_noise": 2.0}},
  "budget": {"wall_hours": 4, "max_runs": 1500},
  "fidelity_floor": {"smb_within_0_05": 0.94, "isf_within_1": 0.97}
}
```

`windows: "day"` = one 24 h UTC window per day (~20 s per variant with `noplot`). `"6h"` splits days for finer time-of-day
analysis at 4x the cost. `locked` is enforced by `campaign.py` and repeated in the skill: a campaign that tries to vary a
locked key is refused with a message, no override flag.

### 4.2 Ledger (`ledger.sqlite`)

```sql
variants  (vhash TEXT PK, params_json TEXT, vdf_text TEXT, base_vdf_sha TEXT, created TEXT)
windows   (wid TEXT PK, start TEXT, end TEXT, day TEXT, final INTEGER, loops INTEGER, fingerprint_json TEXT)
runs      (run_id INTEGER PK, campaign TEXT, round INTEGER, vhash TEXT, wid TEXT, code_sha TEXT,
           settings_sha TEXT, status TEXT, seconds REAL, metrics_json TEXT, csv_path TEXT, started TEXT, finished TEXT,
           UNIQUE(vhash, wid, code_sha, settings_sha))                      -- resume = "skip rows that exist"
loops_differ (run_id, loop_idx, utc, bg, smb_orig, smb_emul, tbr_orig, tbr_emul, isf_orig, isf_emul, reason_tag)
scorecards (campaign, round, vhash, dims_json TEXT, review_rank INTEGER, promoted TEXT)
events    (ts, campaign, kind, payload_json)                                 -- round_start, round_end, stop, error, gh_sync
```

`code_sha` = hash over `determine_basal.py`, `emulator_core.py`, `ns_*.py`; `settings_sha` = hash over `ns_settings.json`.
A run with a different code or settings hash than the campaign started with is still recorded but flagged `stale_context`,
and the round summary says so - the campaign does not silently mix emulator versions (other agents are fixing bugs in the
core right now; this is the realistic case, not a corner case).

`loops_differ` keeps only loops where the emulated and original decision differ by more than the tolerance
(SMB 0.05 U, insulinReq 0.05 U, ISF 1 mg/dl, rate 0.05 U/h). For a day of 1300 loops that is typically 50-300 rows;
it is what the agent and the dashboard drill into.

### 4.3 `rounds/round-NNN.json` - what the agent reads back (target <= 2 KB per variant)

```json
{"campaign": "autoisf_weights_screen", "round": 3, "runs": 42, "seconds": 870, "stale_context": false,
 "noise": {"base_delta_U_per_day": {"mean": 1.21, "sd": 0.35}, "smb_within_0_05": 0.985, "windows_quarantined": []},
 "variants": [
   {"vhash": "a91f", "params": {"profile dura_ISF_weight": 0.5},
    "days": 7, "delta_vs_base_U": {"mean": -0.41, "sd": 0.12, "sign_frac": 1.0},
    "rise_share": 0.93, "at_max_pct": 9.4, "smb_more_less": [0, 61], "guard_flags": 0, "iob_over_th_pct": 0.0,
    "review_rank": 1, "verdict": "candidate", "why": "consistent -0.4 U/day, all in rising phase, 3.4x noise"},
   {"vhash": "0c22", "params": {"profile autoISF_max": 1.5},
    "days": 7, "delta_vs_base_U": {"mean": 0.02, "sd": 0.03, "sign_frac": 0.57},
    "at_max_pct": 3.8, "verdict": "dead", "why": "below noise on every day; factor rarely reaches the cap"}
 ],
 "suggested_next": ["dura_ISF_weight: refine 0.4..0.6 on all strata", "drop autoISF_max above 1.4", "bgAccel: still unscreened"]}
```

`suggested_next` is produced by the strategy code; the agent may follow, amend or override it and must write its own
reasoning into the round comment on GitHub.

### 4.4 `STATE.json` and `STOP`

`STATE.json` (written by atomic rename after every run): `{"campaign","round","phase","queue":[...pending (vhash,wid)],
"done":N,"budget_left":{...},"last_run":{...},"pid":...,"heartbeat":"ISO"}`. A `STOP` file in the campaign directory
finishes the current run, writes the round summary and exits 0; `python -m counterloop run --resume` continues from the
queue; without `--resume` an unfinished campaign refuses to start a second instance (pid + heartbeat check).

---

## 5. Objective: a scorecard, not a scalar

The emulator answers "what would the loop have asked for at each minute of this day under settings S", given the glucose,
IOB and COB history that was recorded **under the phone's settings**. It cannot answer "what would glucose have done".
The objective is built so that this limit is visible in every number.

### 5.1 Noise floor first

For every day in the campaign the base variant (phone settings, empty VDF plus `profile_history`) is run first. Its
`delta_U` (emulated minus original insulin) is the **replay bias** of that day; its agreement columns (SMB within 0.05 U,
insulinReq within 0.05 U, variable_sens within 1 mg/dl, rate within 0.05 U/h) are the **fidelity** of that day. Days
under the `fidelity_floor` are quarantined (CGM gaps, unexplained settings change) and excluded from scoring; they are
listed, not hidden. The noise band shown on every chart is `sd(base delta across days)` plus the per-loop tolerance
mass (`0.05 U x loops_with_SMB`), i.e. the amount by which two honest replays of the same thing differ.

A second control runs in every round: a **null variant** that sets one parameter to the value the phone already has.
It must reproduce the base run bit for bit (equal loop counts, equal metrics); a mismatch means module-global state leaked
between in-process replays and stops the campaign (`emulator_optimize` already warns on unequal loop counts; Counterloop
makes it a hard stop).

### 5.2 Dimensions (each with a declared direction and its own unit)

| dimension | from | reading | direction |
|---|---|---|---|
| `dose_shift` | `delta_vs_base_U` per day (mean, sd, sign fraction) | how much more/less insulin the variant asks for, bias removed | neither; magnitude vs noise is what counts |
| `timing` | `rise_emul - rise_base` vs `rest_emul - rest_base` | extra insulin while rising above target (the loop reacting earlier) vs extra insulin while falling or below target (the proxy for "piling on") | more in `rise`, none in `rest` |
| `saturation` | `at_autoISF_max_pct`, `at_autoISF_min_pct` | share of loops where the final factor sits on the cap; above ~10 % the cap, not the weight, is the active setting | report; a saturated cap is a reason to vary the cap next |
| `smb_shape` | `smb_more/less/equal`, `smb_max_emul_U`, `smb_loops_emul` | whether the change is a few large SMBs or many small ones | report |
| `iob_pressure` | `iob_over_TH_pct`, `iob_max` | original IOB above the variant's effective iobTH (the variant would have been throttled) | lower |
| `guard` | loops with `smb_emul > smb_orig` while original `bg < 90` or original `minGuardBG < 80` (from devicestatus `predBGs`; emulated `Levels` once `loop_hook` exists) | extra dosing near the floor: the one place where "more" is a red flag, not a style choice | zero |
| `stability` | `sd/|mean|` of `delta_vs_base_U` across days and strata | a setting that helps on 3 days and hurts on 2 is not a candidate | lower |
| `fidelity` (base only) | agreement columns | the noise floor | >= floor |

### 5.3 Ranking for review, promotion, and what is never computed

* **Review rank**: Borda rank aggregation over `timing` (share of extra insulin in rising loops), `guard` (ascending),
  `stability` (ascending) and `|dose_shift| / noise` (descending, so that variants that *do nothing* sink). No weighted sum
  of insulin units; no "best" label. The dashboard calls it "review order".
* **Promotion ladder**: `screened` (ran on the OAT day set) -> `candidate` (|effect| > 2x noise on >= 80 % of days with
  the same sign, zero `guard` flags) -> `shortlisted` (confirmed on all strata) -> `proposed` (a proposal issue exists).
  There is no `applied` state the tool can set; the human adds `applied-on-phone` with a date, which Counterloop then
  writes as a `profile_history` reminder into the proposal (the user edits `ns_settings.json` by hand; the tool does not).
* **Never computed or claimed**: time in range, hypo/hyper counts, HbA1c proxies, "glucose would have been lower",
  counterfactual IOB, effects that need the loop's own history to be re-simulated. The dashboard and every proposal carry
  the sentence: *"These are the loop's decisions under other settings, replayed over the glucose and insulin history that
  the phone's settings produced. They say where and how much the loop would have asked differently; they cannot say what
  glucose would have done. A variant that boluses earlier is charged for its bolus and for the later ones the phone
  actually gave, so deltas overstate aggressive settings and understate gentle ones."*

### 5.4 Medical boundary

Counterloop is a decision-support bench for a person with T1D (and whoever they review settings with). It recommends
nothing to the pump: it has no write path to AAPS, uses a read-only Nightscout token (GET only; `ns_client` has no POST),
and its proposals are text for a human to read. The search space excludes the safety section (`max_iob`, `max_basal`,
safety multipliers, `lgsThreshold`), DIA and the profile's basal/ISF/CR; these can be varied with the plain emulator by
hand, deliberately not by an unattended loop.

---

## 6. The skill: how a Claude Code session drives it for hours

`.claude/skills/counterloop/SKILL.md` (summary of its content; `references/` hold the long parts).

### 6.1 Commands the skill knows

```
python -m counterloop days [--from D --to D]                 cached days, finality, fingerprint (bg mean, meals, TT count, CGM gaps)
python -m counterloop prefetch --from D --to D               fill ns_cache for the window; refuses non-final days unless --live
python -m counterloop plan   campaigns/x.json [--dry-run]    validate spec, enforce `locked`, estimate runs x seconds
python -m counterloop run    campaigns/x.json [--resume] [--workers 3] [--until HH:MM]   the long loop; writes STATE.json/round-N.json
python -m counterloop tick   campaigns/x.json                one round, then exit (for /loop, cron or a nervous operator)
python -m counterloop status [campaign]                      <= 20 lines: phase, runs done/left, last heartbeat, stale_context
python -m counterloop show   <run_id|vhash> [--day D] [--loops-differ] [--loop HH:MM]    differing loops / one transcript excerpt
python -m counterloop report campaigns/x.json [--html PATH] [--data-json PATH] [--db-rows PATH]
python -m counterloop propose <vhash> campaigns/x.json       drafts results/counterloop/<c>/proposals/<vhash>.md
python -m counterloop gh     sync|comment|propose ...        GitHub issue operations (section 7)
echo > results/counterloop/<c>/STOP                          graceful stop
```

### 6.2 The agent's loop (one "sitting" of several hours)

1. **Orient** - `days` and `status`; read the open `campaign` issues (`gh issue list --label campaign`); re-read the
   `decision-rules.md` reference. Decide or confirm the question. Write/adjust the campaign JSON and `plan --dry-run`.
   Open or update the campaign issue with the question, day set, space, locked keys, budget.
2. **Prefetch** - `prefetch` for the day set. This is the only network phase; the agent runs it in the foreground, once.
3. **Start the loop** - `run --workers 3` in the background (Bash `run_in_background`). Then **wait on `STATE.json`**
   with `Monitor` (an until-loop on `round` changing or `phase == stopped`), never by polling CSV files.
4. **Per round** (every 10-20 min at 3 workers): read `round-N.json` only. Apply the decision rules:
   * a parameter whose every value is `dead` on the OAT days is dropped from the space (recorded in the round comment);
   * if `at_max_pct` > 10 % for a candidate, vary the cap next and keep the weight fixed;
   * a `candidate` is queued on the remaining strata (`confirm_days`) before anything else;
   * two candidates that touch the same mechanism (e.g. `dura_ISF_weight` and `autoISF_max`) get a small cross grid;
   * any `guard` flag > 0 demotes the variant and triggers `show --loops-differ` for those loops, summarised in one line.
   Write the amended queue (`run` watches `queue/` for appended experiments), post a round comment to the campaign issue
   (table of <= 10 variants, noise band, what was dropped and why, what runs next), regenerate the dashboard and republish
   the artifact (or write the `ArtifactData` rows, section 8). Do not ping the human.
5. **Stop** when one of: budget reached; shortlist unchanged for 3 rounds; all parameters dead; fidelity floor broken
   on > 30 % of days; `stale_context` on a new round (the core changed under the campaign: finish the round, stop, say so);
   null-variant mismatch (immediate). Write the final round comment with the shortlist and the honest-reading paragraph.
6. **Propose** - for each `shortlisted` variant: `propose` -> read the draft -> `gh propose` opens a `proposal` issue
   (section 7). This is the moment the human is pinged (issue mention and one line in the terminal). The session ends with
   `status` and the dashboard link.

### 6.3 Guardrails (quoted in the skill, enforced in code where possible)

* Never edit `ns_settings.json`, `aaps_preferences.json`, VDFs under `variants/`, or anything on the phone/Nightscout.
  The campaign's VDFs live under `results/counterloop/`.
* Never vary a `locked` key; never remove the lock list from a campaign file.
* Never report a delta without the noise band of the same days; never use the words "better", "best", "optimal" for a
  variant - "asks for less/more insulin", "reacts earlier", "saturates the cap" are the allowed verbs.
* Never read a result CSV into context; use `round.json`, `show --loops-differ`, `show --loop`.
* Never run two campaigns against the emulator in the same process; one `run` per campaign directory (pid lock).
* Prefetch only final days unless the human asks for today (`--live`), and then single worker, one window.
* Stop conditions are not negotiable mid-run; a new campaign file is a new campaign.
* Every claim in a proposal links a `run_id` and names the days; the proposal says what it cannot claim (5.3).

### 6.4 Scheduling

* Interactive sitting: the sequence above, the agent stays attached (`Monitor` on `STATE.json`).
* Self-paced: `/loop 20m /counterloop tick campaigns/x.json` - one round per tick; the skill's `tick` path reads the
  previous round, decides, runs one round, reports, exits.
* Daily: a `schedule` routine at 03:00 local: `prefetch` yesterday (now final), rerun the current shortlist on it, append
  a one-line comment to the campaign issue; a shortlisted variant that flips sign on the new day is demoted with a note.
  No proposal is opened from the routine; only interactive sittings open proposals.

### 6.5 Team of agents (the user asked for one)

Each agent owns files and an issue label; they coordinate through issues and PRs, never by editing each other's modules.

| agent | owns | first issues |
|---|---|---|
| **lead / skill author** | `SKILL.md`, `references/`, `campaigns/`, `campaign.py` | #1 epic, #2 campaign spec + decision rules |
| **bench** | `runner.py`, `store.py`, `days.py`, `search.py` | #3 ledger + resume, #4 runner + process pool + null variant, #5 strategies |
| **lens** | `report.py`, `templates/dashboard.html`, dataviz | #6 dashboard-data.json contract, #7 static HTML, #8 live db rows |
| **ledger** | `gh.py`, labels, templates | #9 labels + campaign issue, #10 proposal issue flow |
| **referee** | `objective.py`, `metrics.md`, honesty text, fidelity tests | #11 scorecard + noise, #12 `loop_hook` request to the core maintainers |

Emulator state is module-global, so each agent that needs to *run* the emulator uses its own campaign directory and
process; nobody imports another agent's half-finished module into a running campaign.

---

## 7. GitHub issues as the task board

Labels to create: `campaign`, `round`, `proposal`, `finding`, `bench` (tool work), `blocked-core` (needs a core change),
`accepted`, `rejected`, `applied-on-phone`.

* **Campaign issue** (one per campaign, label `campaign`, title `Campaign: <question>`): body = the campaign JSON rendered
  as a table plus the honesty paragraph; one **comment per round** (`gh issue comment`) with the round table, the noise
  band, dropped parameters, next queue, dashboard link; a task list in the body that the agent ticks (`plan`, `prefetch`,
  rounds 1..N, `report`, `proposals`). Closed by the agent with the final summary.
* **Proposal issue** (label `proposal`, title `Proposal: dura_ISF_weight 0.7 -> 0.5`): the evidence (days, effect vs
  noise, timing split, guard flags, saturation), links to the campaign issue and `run_id`s, the two or three most telling
  loops with transcript excerpts, the "cannot claim" paragraph, and a checklist for the human: *reviewed / discussed /
  applied on phone on YYYY-MM-DD / add to profile_history*. Only the human closes it; the agent reads the label the human
  sets (`accepted`, `rejected`, `applied-on-phone`) in the next sitting and records it in the ledger.
* **Finding issue** (label `finding`): something the campaign surfaced that is not a settings proposal - a fidelity drop
  on a day (likely an emulator bug or a settings change not in `profile_history`), a cap that saturates, a dead parameter
  worth removing from the phone's mental model. These are the hand-offs to the people fixing the core.
* **Bench issues** (label `bench`): the tool's own backlog from section 6.5.

All GitHub traffic goes through `gh` (already authenticated here). The agent posts a comment at most once per round and
never edits a human's comment.

---

## 8. The dashboard and how it stays live

**Rendering**: one self-contained HTML built by `report.py` from `dashboard-data.json`, Plotly.js from
`cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js` (exact pin), colour tokens on `:root` with the dark-mode
redefinitions the Artifact tool requires, a 16 px gutter, cards stack at phone width. It opens from disk
(`results/counterloop/dashboard/index.html`) and publishes unchanged as an Artifact. Size budget: campaign-level data
(scorecards, per-day deltas) is a few hundred KB; per-loop series are embedded only for shortlisted variants and
downsampled to 5 min; everything else is loaded on demand from `files` published next to the page (`runs/<run_id>.json`),
which keeps the page far under 16 MB.

**Pages (tabs)**:
1. *Campaign* - status strip (phase, runs done/left, heartbeat, stale_context), the noise band as a shaded row, the
   review-ordered table of variants (effect vs noise as a dot-with-whisker per day set, timing split as a two-colour bar,
   saturation and guard as small numbers). Explicit header text: "review order, not a ranking by outcome".
2. *Parameters* - small multiples: one panel per parameter, x = value, y = `delta_vs_base_U` per day as points with the
   noise band; the fastest way to see "dead", "monotone", "saturating".
3. *Days* - heatmap days x variants of `delta_vs_base_U`, days annotated with their fingerprint (meals, TTs, CGM gaps,
   `profile_history` changes), quarantined days greyed.
4. *Variant* - for one variant and one day: bg trace with original and emulated SMBs as markers, the autoISF factor
   (orig vs emul) and the cap, iobTH line; the loops that differ are highlighted and clicking one shows the transcript
   excerpt (`show --loop`) that `report.py` pre-extracted for the differing loops.
5. *Decisions* - the proposal list with their GitHub state, and the round log.

**Staying live while the loop runs** - two modes, in order of implementation:

* *MVP - regenerated static page*: after each round the agent runs `report --html` and republishes the same artifact
  (same file path, same URL). Latency = one round (10-20 min). Zero runtime capabilities needed; also works offline from
  disk.
* *Later - artifact database*: the page declares the `db` capability; `report --db-rows` emits the round's rows
  (`runs`, `scorecards`, `status`) and the agent writes them with `ArtifactData` in one `batch` per round; the page
  renders from the db and refreshes on load. The static JSON remains the fallback so the page still works from disk.
  (The `artifact-capabilities` skill is loaded before the first publish; the page never stores anything in browser storage
  except the selected tab.)

The dataviz skill is loaded before `templates/dashboard.html` is written; the palette is tokenised so the page reads as one
system in light and dark.

---

## 9. Search strategy (what "run more and more" means in practice)

Costs: ~20 s per variant-day with `noplot` in one process (the docs' measured figure; the 2 min 20 s the user saw was a
PDF run). One process: ~180 variant-days per hour; 3 workers: ~500. A 10-day campaign therefore screens ~50 variants an hour.

1. **Screen** (round 1-2): one-at-a-time around the phone's current value, 3 days of different strata. Output: dead /
   monotone / saturating per parameter. Most autoISF weights are dead on most days (the example in NIGHTSCOUT_SOURCE.md:
   `autoISF_max` 1.3 -> 1.5 moved +0.01 U); finding this out early is the biggest time saver.
2. **Confirm** (round 3-4): live parameters on all days of the set; `candidate` status needs the same sign on >= 80 % of
   days and |effect| > 2x noise.
3. **Descend** (round 5+): coordinate descent on the live parameters (one step up/down from the current best review rank,
   re-evaluated on all days), with a small 2-parameter cross grid where two candidates share a mechanism; successive
   halving keeps the top half each round. The objective for the descent is the review rank, so the descent moves toward
   "clear, early, consistent, no guard flags", not toward "least insulin".
4. **Widen** (optional, later): Latin-hypercube samples over the live parameters, or `optuna` with the review rank as a
   multi-objective study (Pareto front of `timing` vs `|dose_shift|` with `guard == 0` as a constraint) - shown as a
   front on the dashboard, never collapsed to one point.
5. **Daily re-test** (routine): new final day -> shortlist rerun -> demotion on sign flip.

The strategies live in `search.py` behind one interface `next_round(ledger, campaign, previous_round) -> list[(variant, wid)]`
so the agent can also hand-write a round (`queue/round-override.json`) when it has a hypothesis the code does not.

---

## 10. Unattended-safety checklist (hours alone)

* Network only in `prefetch`; `run` refuses to start if any window's day files are not final (unless `--live`); the
  client's own retry/backoff and 5-min refresh rule stay as they are; one Nightscout request stream at a time.
* Every run inserts its row before the next starts; `STATE.json` by atomic rename; `--resume` skips existing
  `(vhash, wid, code_sha, settings_sha)` rows; crash = at most one lost run.
* `code_sha`/`settings_sha` recorded per run; change -> `stale_context`, finish the round, stop.
* Null variant per round; mismatch -> stop. Loop counts must be equal across variants of a day -> else quarantine day.
* Disk check before each round (the emulator writes ~1 MB of `.txt` per variant-day; a 1500-run campaign is ~1.5 GB;
  `--keep-txt shortlisted` deletes transcripts of dead variants after the round, keeping the CSVs).
* Worker pool with `max_tasks_per_child` to bound memory; stdout of `parameters_known` redirected into the run log.
* `STOP` file, `--until HH:MM`, wall-clock budget; exit codes: 0 finished/stopped, 2 stopped by guard (null mismatch,
  fidelity), 3 configuration refused (locked key, non-final day).
* No writes outside `results/counterloop/` and `ns_cache/`.

---

## 11. MVP and later

**MVP (first sitting of the team, ~1 week of agent work)**
* `store.py` ledger + `STATE.json` + resume; `runner.py` sequential, reusing `emulator_optimize` functions, null variant,
  `loops_differ` extraction; `days.py` inventory and finality; `campaign.py` with `screen` and `grid` strategies;
  `objective.py` scorecard with noise band and promotion ladder (guard from original `predBGs`); `report.py` static
  Plotly HTML with tabs 1-3 and a minimal tab 4; `gh.py` campaign issue + round comments; `SKILL.md` with the loop in 6.2,
  guardrails 6.3, interactive sitting only. Validation: a campaign over the 9 cached days of 2026-08-30..09-07 with the
  existing `grid_autoISF.json` space reproduces the example in NIGHTSCOUT_SOURCE.md row for row from the ledger.

**V2**
* Process pool, coordinate descent and halving, day fingerprints and strata, `tick` + `/loop`, daily `schedule` routine,
  proposal issues and the human label loop, dashboard tab 4/5 complete, live db rows via `ArtifactData`,
  `emulator_optimize.py` becomes a wrapper that records into the ledger.

**V3**
* `loop_hook` in the core (emulated `Levels`/predBGs -> real `guard` dimension), `optuna` multi-objective front, 6 h windows
  with time-of-day analysis, cross-campaign ledger views ("what did we learn about dura_ISF_weight over the year"),
  a `findings` feed into the core maintainers' issues when fidelity drops.

---

## 12. Risks and honest limits

* **The replay is the ceiling.** 96 % agreement is good for decision replay, but 4 % of loops differ for reasons unrelated
  to settings (glucose reconstruction after CGM gaps, autoISF 3.2.1 vs 3.2.0 echo differences, SMB interval tolerance).
  Over a day those add up to the +1.2..1.4 U replay bias. Counterloop subtracts it and shows it; it cannot remove it.
  Effects below ~0.3 U/day are not detectable, and the tool must say "no detectable effect" rather than rank them.
* **Aggressive settings are overcharged.** A variant that boluses earlier is charged for its bolus and for the phone's
  later ones. Timing and consistency are the readable signals; totals are biased by construction.
* **Days are not independent samples.** Ten days of the same week share weather, food, activity and one settings regime
  (`profile_history` has three changes in the cached week). The strata and the daily re-test help; they do not make it a
  trial. Every proposal says so.
* **Module-global emulator state** makes in-process sweeps fragile; the null variant and equal-loop-count checks are the
  defence, and a process pool isolates variants at the cost of re-importing the core per worker.
* **Concurrent core fixes** will change results mid-campaign; `stale_context` makes that visible instead of silent.
* **It is a bench for one person's data.** Settings that look consistent on these days are hypotheses for that person and
  whoever they review settings with - the proposal issue is written for that review, never as an instruction to the pump.
