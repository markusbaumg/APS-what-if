# Nightscout as emulator data source (branch `nightscout-source`)

The emulator replays every historical loop run of AAPS with `determine_basal.py`
and compares the original decision with the emulated one under changed settings.
On this branch the loop inputs are rebuilt from **Nightscout** instead of the
AAPS logfiles, so no logfile export is needed and any time window that is still
on the Nightscout server can be replayed (ns.vivikusi.ch keeps devicestatus for
weeks, ~1 record per minute).

```
python emulator_batch.py  https://ns.vivikusi.ch  "bg/iob/ISFs/insReq/SMB/basal/."  variants/no_change.vdf  2026-09-06T06:00:00Z 2026-09-06T12:00:00Z
                          ^ NS url instead of logfile pattern (or "ns:" = url from ns_settings.json)
```

## What Nightscout provides and what it does not

| loop input (determine_basal argument) | AAPS logfile (old branch) | Nightscout (this branch) | module |
|---|---|---|---|
| `glucose_status` glucose, delta, short/long avgdelta, dura_ISF_*, parabola fit | logged 1:1 | rebuilt from `entries` (raw 1-min Libre3 values) and the smoothed `devicestatus.openaps.suggested.bg` the loop actually used; validated against `tick`, `dura_ISF ... did not do it for Xm`, `acce_ISF adaptation is`, `Parabolic fit extrapolates` in consoleError | `ns_glucose.py` |
| `iob_data` iob, activity, basaliob, iobArray[48] with iobWithZeroTemp, lastBolusTime, lastTemp | logged 1:1 | rebuilt from `treatments` (boluses, SMBs, temp basals) with the AAPS exponential insulin model; validated against `devicestatus.openaps.iob` | `ns_iob.py` |
| `currenttemp` rate, duration(left) | logged | from the last `Temp Basal` treatment | `ns_iob.py` |
| `profile` (OapsProfileAutoIsf: ~90 keys) | logged 1:1 | NS profile store (basal, ISF, CR, target, DIA, timezone) + `Temporary Target` treatments + `Profile Switch` treatments + **`ns_settings.json`** for all AAPS preferences | `ns_profile.py` |
| `autosens_data.ratio` | logged | consoleError `Autosens ratio: X` (carried forward when a loop does not print it) | `ns_source.py` |
| `meal_data` mealCOB, carbs, lastCarbTime, slopeFromMaxDeviation / slopeFromMinDeviation | logged | `suggested.COB`, carb treatments; the deviation slopes are rebuilt with the AAPS autosens worker algorithm over the smoothed 5-min series and pinned to the `UAM Duration` the phone printed (with the MealData defaults 0 / 999 the UAM prediction collapses and most SMBs are disabled) | `ns_source.py`, `ns_meal.py` |
| activity monitor step counts | logged (`Activity Monitor json`) | **not in NS** -> the detected state is read from consoleError (`Activity monitor detected ...`) and turned into synthetic step counts that reproduce it, so the scale factors stay tunable | `ns_profile.py` |
| original result (reason, insulinReq, SMB, TBR, predBGs, consoleError) | logged | `devicestatus.openaps.suggested` 1:1 | core `TreatLoop` |

All glucose values inside the pipeline are **mg/dl** (oref internal unit); `profile['out_units']='mmol/L'` only formats messages. NS profile values in mmol are multiplied by 18.

## Data contracts (fixed - every module keeps to these)

Times: unix **milliseconds UTC** everywhere (`now_ms`). NS ISO strings are parsed with `ns_client.parse_iso`.

### ns_client.NightscoutClient(url, cache_dir=None, token=None)
`entries(start, end)`, `treatments(start, end)`, `devicestatus(start, end)` -> ascending lists of raw NS records in `[start, end)`; `profiles()` -> profile store documents newest first. Per-UTC-day JSON cache in `software/ns_cache/<host>/`.

### ns_glucose.GlucoseHistory(entries, devicestatus, settings)
`status_at(now_ms, record)` -> dict with **exactly** these keys (mg/dl, mg/dl per 5 min):
`glucose, date, delta, short_avgdelta, long_avgdelta, noise, dura_ISF_minutes, dura_ISF_average, parabola_fit_minutes, parabola_fit_correlation, parabola_fit_a0, parabola_fit_a1, parabola_fit_a2, parabola_fit_last_delta, parabola_fit_next_delta, bg_acceleration`
`glucose` is the value the loop saw (`record['openaps']['suggested']['bg']` when present), `date` the timestamp of the CGM reading the loop ran on. Return `None` if no CGM value within 12 min before `now_ms`.
Loop-to-reading pin (with `record`): a sluggish phone sometimes runs determine-basal on a reading 1-5 readings (60-320 s) older than the newest one already on the server (2026-10-15: 38 of 345 loops, 10-14: 28/668, 10-13: 18/1113, 10-10 06-12Z: 23/259, 09-06 06-09Z: 0). `status_at` takes as cutoff the newest of the last `PIN_READINGS` (6) readings whose rebuilt smoothed bg equals `suggested.bg` within `PIN_TOL` (0.05 mg/dl), and rebuilds `delta`, short/long avgdelta, `dura_ISF_*` and `date` from the buckets of that reading; the Libre raw 1-minute parabola fit stays on the newest reading (`settings.glucose.pin_fit` `'newest'`, the default; `'pinned'` fits on the pinned reading too, `pin_readings` 1 switches the pin off). No candidate matches -> newest reading. Counters `gh.pinned` ({readings back: loops}) and `gh.pin_unmatched`; `NightscoutSource.loops()` prints both at the end of a run and `--validate` reports them. Probing older cutoffs is undone in the Kalman state (learned R), only the chosen cutoff's smoothing is kept. Why the split: the fork computes bg/deltas/dura from the autosens bucket store (computed at the older reading) but reads the raw readings for the fit from the database. Measured per-day agreement (`metrics.agreement`, ns_iob as of b8055aa) before / whole-state pin / split (kept): 10-13 97.21 / 97.57 / 97.48 %, 10-14 94.46 / 95.66 / 95.66 %, 10-15 90.43 / 92.46 / 92.75 %; the whole-state pin lowered the 10-13 ISF count (1094 -> 1093, split 1096) and has one ISF loop fewer than the split on 09-06 09-15Z and 10-10 06-12Z; pinning dura_ISF to the newest reading as well was worse (10-14 95.21 %).

### ns_iob.IobCalculator(treatments, basal_at, settings, dia_at=None, devicestatus=None)
`devicestatus` (optional, the records with a 2 h lookback) lets the calculator recover the *planned* duration of the running temp basal from `openaps.enacted` / `pump.extended`; Nightscout's `Temp Basal` treatment only carries the cut remainder.
`basal_at(ms)` -> profile basal U/h and `dia_at(ms)` -> DIA hours (callables, supplied by ns_profile; DIA falls back to `settings.insulin.dia_hours` or 6).
`iob_array(now_ms, sensitivity_ratio=1.0)` -> list of 48 dicts (t = now + i*5 min): `{'time': t_ms, 'iob', 'activity', 'basaliob', 'bolussnooze', 'netbasalinsulin', 'hightempinsulin', 'iobWithZeroTemp': {'iob','activity','basaliob'}}` following AAPS `calculateIobArrayForSMB` (bolus IOB + temp-basal IOB net of profile basal * sensitivity_ratio; `iobWithZeroTemp` = same but with the running temp cut at now and a 0 U/h temp for 240 min from now).
Temp basals keep the duration Nightscout recorded (`durationInMilliseconds`): when AAPS replaces a running temp it ends that record, so ordinary records never overlap and need no cutting; records that do overlap (2026-10-15 21:46-22:30Z after the AAPS reinstall: a 0 U/h temp recorded 43.7 min while three later temps started) are summed as recorded, exactly like AAPS's IOB (phone IOB reproduced to 0.000 U from 22:02 on; IOB loops off by > 0.05 U that day 76 -> 6, the 6 being the reinstall minutes 21:54-21:59 when the phone's fresh DB had no history). Only a cancel marker (0 ms, no `endmills`) ends the previous temp. `current_temp` returns the newest record covering now, as AAPS's DB query does (an older overlapping record again once the newer one ended).
`iob_at(t_ms, now_ms, sensitivity_ratio)` -> one such dict. `last_bolus_time(now_ms)` -> ms or 0. `current_temp(now_ms)` -> `{'rate': U/h, 'duration': minutes left (0 = none), 'start': ms}`. `last_temp(now_ms)` -> `{'date': start ms, 'rate', 'duration': full minutes}` of the most recent temp started at or before now (see oref0 `lastTemp`).

### ns_profile.ProfileBuilder(ns_profiles, treatments, settings)
`profile_at(now_ms, console_error=None)` -> the full `profile` dict for `determine_basal.py` (every key it reads; see list in the module). Includes `current_basal`, `sens`, `carb_ratio`, `min_bg`, `max_bg`, `target_bg`, `temptargetSet`, `dia`, `max_daily_basal`, `out_units`, `profile_percentage`, `autoISF_version`, all settings sections copied 1:1, `profile_history` applied by time.
`basal_at(ms)` -> U/h scheduled basal incl. profile switch percentage. `dia_hours(ms)` -> DIA of the active profile. `activity_monitor_json(console_error, now_ms)` -> dict `{recentSteps5Minutes ... recentSteps60Minutes, phone_moved, time_since_start}` reproducing the state printed in consoleError.

### ns_meal.DeviationHistory(glucose_history, iob_calc, devicestatus, isf_fallback)
`slopes_at(now_ms, uci=None, printed_duration=None)` -> `(slopeFromMaxDeviation, slopeFromMinDeviation)`; port of the
IobCobOref1Worker slope calculation (autosens table keyed by roundUpTime, bgi from the ns_iob activity and
`suggested.isfMgdlForCarbs`). `uci` is the carb impact exactly as determine_basal computes it, `printed_duration` the
`UAM Duration` parsed from consoleError (`printed_uam(console_error)`); when both are given the pair is calibrated so
that the replay reproduces the printed duration. `isf_fallback(ms)` -> ISF mg/dl/U when a record has no isfMgdlForCarbs.

### ns_source.NightscoutSource(url_or_ns, start_iso, end_iso)
devicestatus is loaded with a 2 h lookback (planned temp durations, deviation history of the first loops) and 2 min
beyond the end (upload lag); a loop belongs to the window when its run time `suggested.deliverAt` lies in
`[start, end)` - not its upload time `created_at` - so adjacent windows replay every loop exactly once. Duplicate
uploads (same `deliverAt`) are skipped and counted; the profile store is fetched with `newer_than=window end`.
`microBolusAllowed` is False for loops whose consoleError carries `SMB disabled (!microBolusAllowed)` (AAPS re-runs the
loop with `tempBasalFallback` after a failed SMB delivery; counted as `SMB fallback loop`) or whose pump status says
open loop / suspended. `window_label(start, end)` -> `<start>_<end>` (':' -> '-'), the base name of the result files.
`loops()` yields one dict per original loop run inside the window, in time order:
`record` (raw devicestatus), `suggested`, `stamp` (deliverAt ISO), `now_ms`, `utcOffset` (hours), `glucose_status`, `iob_array`, `currenttemp`, `lastTempAge` (min), `profile`, `activity_monitor`, `autosens_data`, `meal_data`, `microBolusAllowed`, `console_error` (list), `autoISF_version`.
`emulator_core.scanNightscout()` feeds these through the unchanged bookkeeping functions (`apply_glucose_status`, `get_iob_data`, `get_currenttemp`, `get_profile`, `get_autosens_data`, `get_meal_data`, `scanConsoleError`) and then `TreatLoop(json.dumps(record))`, exactly like a logfile loop.

## Validation targets (each module has a `python ns_xxx.py --validate <start> <end>` self-test)
* IOB: `openaps.iob.iob/basaliob/activity` per loop: median |error| < 0.05 U, report worst cases.
* Glucose: smoothed bg vs `suggested.bg`, `tick` (delta), `dura_ISF ... for Xm` minutes, `acce_ISF adaptation is` (=> bg_acceleration), `Parabolic fit extrapolates a maximum/minimum of X in about Y minutes`, plus the pin counts. 2026-10-15 before / after the pin: smoothed bg exact 86.7 -> 96.5 % (within 1 mg/dl 91.6 -> 98.3 %), tick exact 92.2 -> 98.3 %, dura minutes exact 97.8 -> 98.9 %, acce_ISF factor unchanged 96.2 % within 0.02; 2026-09-06 06-09Z 100 % before and after.
* Profile: `profile.sens: X, sens: Y`, `Basal unchanged: X`, `CR: X`, `targetBG`, `SMB enabled; current target X has even decimal`, `User setting iobTH=...`.
* Meal: `UAM Duration: X hours` printed by the phone reproduced by the rebuilt slopes (53.1 % raw, 99.2 % after pinning, cached week).
* End to end: "no change" replay (empty VDF) must reproduce `insulinReq`, `units` (SMB), `rate` and `variable_sens` of the original loop for the large majority of loops; mismatches are listed with reasons.

## How to run

All commands from `software/` with `PYTHONUTF8=1` set (emulator_core insists on it); the Python needs numpy and
matplotlib only for the plots - the `ns_*` modules are standard library and also run under QPython.

```
cd software
# driver alone: dumps the first loop input set and counts the loops of the window
python ns_source.py 2026-09-06T06:00:00Z 2026-09-06T06:10:00Z

# no-change replay, 3 h, no plots (results next to the VDF: variants/2026-09-06T06-00-00Z_2026-09-06T09-00-00Z.no_change.{csv,txt,log,delta} + .orig.txt)
PYTHONUTF8=1 python emulator_batch.py https://ns.vivikusi.ch "seconds/bg/iob/autosens/acce/bg_ISF/pp/dura/ISFs/insReq/SMB/basal/noplot/." variants/no_change.vdf 2026-09-06T06:00:00Z 2026-09-06T09:00:00Z

# full day
PYTHONUTF8=1 python emulator_batch.py https://ns.vivikusi.ch "seconds/bg/iob/autosens/acce/bg_ISF/pp/dura/ISFs/insReq/SMB/basal/noplot/." variants/no_change.vdf 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z

# with plots (PDF next to the VDF; MPLBACKEND=Agg when there is no display)
PYTHONUTF8=1 MPLBACKEND=Agg python emulator_batch.py https://ns.vivikusi.ch "seconds/bg/iob/autosens/acce/bg_ISF/pp/dura/ISFs/insReq/SMB/basal/." variants/no_change.vdf 2026-09-06T06:00:00Z 2026-09-06T09:00:00Z

# a what-if variant: any VDF (syntax '<array> <item> <value>', e.g. 'profile autoISF_max 1.3')
PYTHONUTF8=1 python emulator_batch.py https://ns.vivikusi.ch "bg/iob/ISFs/insReq/SMB/basal/noplot/." variants/autoISF_max_1.3.vdf 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z

# module self-tests against the phone's own echoes
python ns_iob.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z
python ns_glucose.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z
python ns_profile.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z
python ns_meal.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z

# AAPS preferences export (encrypted; needs 'cryptography') -> scrubbed aaps_preferences.json + the mapped keys of
# ns_settings.json; keys under '_overrides' are kept, --no-settings writes only the scrubbed copy
python ns_prefs_import.py <AAPS export.json> [--password <pw>] [--no-settings]
```
`ns:` instead of the url takes the url from `ns_settings.json`. The first run of a window downloads the four collections
into `ns_cache/<host>/<collection>/<YYYY-MM-DD>.json` (one UTC day per file; a day file is final once it was written
more than an hour after the UTC day ended - a file fetched while the day was running is partial and is refetched -, the
profile store is served from the cache when it was written an hour after the window end), every later run of that
window is offline; `python ns_client.py ns: <start> <end> --refresh` deletes the day files of `[start, end)` to force a
refetch (backfilled treatments). The replay reads treatments and entries 8 h and devicestatus 2 h before `start`
(`LOOKBACK_MS`, `DS_LOOKBACK_MS` in `ns_source.py`), and `--refresh` does not touch those look-back days, so a backfill
that falls into the look-back is only picked up when the refreshed window starts correspondingly earlier (or the day
file is deleted by hand). `emulator_batch.py` with a logfile pattern instead of a url still works as before
(AAPS logfile branch). Output files land next to the VDF, named `<start>_<end>.<variant>.*` (UTC, ':' -> '-'; open
bounds spelled out), so runs with different windows do not overwrite each other; the `.log` header echoes the
`ns_settings.json` modification time, a hash of its settings sections, the import stamp, `profile_history` and
`_overrides`. On Android (QPython) the same modules are used.

Two things that are easy to get wrong:
* a loop is replayed only if Nightscout holds a devicestatus with `openaps.suggested.predBGs` for it; during a CGM gap
  AAPS uploads none, so those minutes are simply absent in the results (the 71 min gap 2026-09-06 15:30-16:41 shows as
  a jump in the CSV, not as zero rows).
* AAPS uploads ~15 % of the devicestatus records twice (new `_id`, same `deliverAt`); `ns_source` skips the copies and
  reports `N x duplicate upload` at the end - the count is not an error.

### Measured agreement of the no-change replay (2026-09-06 .. 09-07 cache, AAPS 3.4.2 / autoISF 3.2.1)
'exact' = identical printed value; SMB / TBR to 0.001 U, insulinReq to 0.005 U, ISF to 0.05 mg/dl.
(after the review fixes of 2026-09-08: `smb_delivery_ratio_bg_range` 36, `determine_varSMBratio(int(bg))`, planned temp
ends on the minute grid, fallback loops without SMB; the 2026-09-03 row is from before them)

| window | loops | SMB exact / within 0.05 U | insulinReq exact / within 0.05 U | variable_sens exact / within 1 mg/dl | TBR rate exact / within 0.05 U/h | final autoISF factor exact | SMB total orig / emul | TBR insulin orig / emul |
|---|---|---|---|---|---|---|---|---|
| 2026-09-06 06-09Z | 180 | 97.2 % / 99.4 % | 85.0 % / 100 % | 99.4 % / 100 % | 95.0 % / 98.9 % | 100 % | 2.55 / 2.65 U | 0.18 / 0.20 U |
| 2026-09-06 full day | 1311 | 96.1 % / 98.5 % | 82.2 % / 98.3 % | 98.7 % / 99.6 % | 96.0 % / 98.6 % | 99.2 % (99.7 % within 0.01) | 20.25 / 24.30 U | 1.67 / 1.66 U |
| 2026-09-03 (37 TTs, CGM gap; before the fixes) | 1220 | 93.8 % / 98.0 % | 87.7 % / 99.5 % | 98.3 % / 99.2 % | 96.7 % / 98.0 % | 98.9 % (99.4 %) | 15.35 / 20.95 U | 0.77 / 0.59 U |

Prediction arrays (determine_basal wrapped, 2026-09-06): IOB predBGs differ in 102 of 1311 loops, ZT in 87, UAM in 652,
eventualBG in 299 - all by 1-3 mg/dl. Deviation slopes (ns_meal --validate, whole cached week, 9440 loops): the rebuilt
slopes reproduce the printed `UAM Duration` in 53.1 % of the loops, 99.2 % after pinning them to the printed duration.

Where the remaining mismatches come from (per loop; a loop may carry several, 276 of 1311 loops on 2026-09-06):
* 201 rounding only (all four quantities within 0.05): Kotlin `Math.round` (half up) vs Python `round()` (half to even) in
  determine_basal.py, e.g. minZTUAMPredBG 104.5 -> 105 on the phone, 104 here -> insulinReq 0.10 vs 0.09 -> SMB 0.05 vs 0.
* 162 zero-temp cap: the fork caps the zero temp in the SMB branch at 60 min, determine_basal.py at 120 min (rate equal).
* 35 SMB interval tolerance: fork `lastBolusAge > SMBInterval - 6.0` (seconds/60), determine_basal.py `- 12` -> with
  1-minute loops the emulator microboluses where the phone printed `Waiting 0m 6s to microbolus again`. This is the
  dominant remaining SMB-total difference (20.25 U phone vs 24.30 U emulated on 2026-09-06); **the emulated totals are
  biased upwards by it in every variant, the base variant of an optimization run shows the size of the bias**.
* 7 loops: a 1 mg/dl difference in a prediction array moves minPredBG by 1 -> different insulinReq; 17 unclassified.
* fixed on 2026-09-08 (review): `smb_delivery_ratio_bg_range` is 36 mg/dl in `ns_settings.json` (the phone's printed
  ratios fit only with 36, 3025/3025 lines over 2026-09-01..07, never with the 18 = 1.0 mmol of the export; kept by
  `_overrides` against re-imports) and `determine_varSMBratio` gets `int(bg)` like the Kotlin `Int` parameter - together
  they were 122 + ~20 SMB mismatches per day. `smb_max_range_extension` 1.15 -> 1.8 on 2026-09-07 22:03 local
  (`maxBolus: 1.1` printed since, `profile_history` entry; `ns_profile --validate` now checks `maxBolus`).
* ~1 %: glucose reconstruction transients after CGM gaps (acce/dura/pp factor differs in 8-14 loops per day).
* cosmetic echo differences (`Sensitivity ratio unchanged`, `ISF unchanged: 91.8` vs `5.1`, `3 hours` vs `3.0 hours`).

## Optimization
`emulator_optimize.py` runs a set of what-if settings over one cached window and ranks the variants. Every combination
becomes a generated VDF (base VDF lines + one line per parameter) in `results/<label>/`, is replayed in-process through
`emulator_core.parameters_known` with the `noplot` option, and its CSV is condensed into one row of metrics.

```
cd software
# the grid file: 5 x 3 combinations of autoISF_max and dura_ISF_weight (+ base) over a full day
python emulator_optimize.py --grid variants/grid_autoISF.json --start 2026-09-06T00:00:00Z --end 2026-09-07T00:00:00Z

# each value alone instead of all combinations; own label; only write the VDFs
python emulator_optimize.py --grid variants/grid_autoISF.json --mode one_at_a_time --label oaat_0906 --start 2026-09-06T00:00:00Z --end 2026-09-07T00:00:00Z --dry-run

# parameters from the command line (override / add to the grid file; any VDF array/item works)
python emulator_optimize.py --grid variants/grid_autoISF.json --param "profile autoISF_max" 1.3 1.5 --param "profile dura_ISF_weight" 0.5 0.7 --start 2026-09-06T06:00:00Z --end 2026-09-06T12:00:00Z --label example_2026-09-06
```
Options: `--base-vdf` (default `variants/no_change.vdf`; its lines are copied into every generated VDF, so a
`profile autoISF_max 1.3` there fixes the value for all variants that do not vary it), `--url` (default from
`ns_settings.json`), `--outputs` (emulator output options, `noplot` is added), `--label` (default `<grid name>_<start>_<end>`),
`--overwrite` (needed to reuse a `results/<label>/` that already holds a `summary.csv`; otherwise the run refuses).
Grid JSON: `{"parameters": {"profile autoISF_max": [1.2, 1.3, 1.4, 1.5, 1.6], "profile dura_ISF_weight": [0.5, 0.7, 0.9]}, "mode": "grid"}`
- keys are `<array> <item>` exactly as in a VDF line, `mode` is `grid` (cartesian product) or `one_at_a_time`.
The base VDF is always run as variant `base`. Run time: ~5 s per variant and 6 h window, ~20 s per full day
(the first variant fills the Nightscout cache, all others are offline; a finished run is reproducible offline).

Files in `results/<label>/`: `<variant>.vdf`, the normal emulator outputs `<start>_<end>.<variant>.csv/.txt/.log/.delta`
and `<start>_<end>.orig.txt`, `summary.csv` (one row per variant: parameter values + all metrics, `;` separated),
`summary.json` (the same plus paths) and `summary.txt` (the table below, grid order with `base` first - deliberately
not a ranking: sorting by delta would reward whatever delivers less). `results/` is git-ignored.

Metrics per variant (all from the CSV, columns found by header text): `loops`; `smb_orig_U` / `smb_emul_U` (sum of SMBs
the phone gave / the emulator would have given); `tbr_orig_U` / `tbr_emul_U` (temp basal rate **in force** x time to
the next loop, capped at 30 min per loop - `TBR orig` is the rate the phone set in that loop or, when it set none, the
running temp, see `emulator_core.basalFromReason`); `total_*_U` = SMB + TBR; `delta_U` = emul - orig and `delta_pct`;
`delta_vs_base_U` = `delta_U` minus the `delta_U` of `base` (the replay bias removed); `smb_more` / `smb_less` /
`smb_equal` (loops with emulated SMB > / < / == original); `smb_loops_orig/emul`, `smb_max_emul_U`;
`isf_factor_emul_mean/max/min` (final autoISF factor), `at_autoISF_max_pct` / `at_autoISF_min_pct` (loops sitting on
the cap; the cap is evaluated per loop - a grid or base VDF value applies to every loop, otherwise the value of
`profile_history` in `ns_settings.json` at that loop's time, so a cap that changes inside the window is honoured on both
sides of the change); `isf_emul_mean`;
`iob_over_TH_pct` / `iob_over_tol_pct` (loops where the **original** IOB exceeds the emulated effective iobTH / its 130 %
tolerance - the replay has no emulated IOB, see below); `rise_orig_U` / `rise_emul_U` (insulin given while bg is above the
emulated target and rising, delta = bg minus the bg 5 min earlier) versus `rest_orig_U` / `rest_emul_U` (below target
or falling); `bg_mean`, `bg_gt180_pct`, `bg_lt70_pct` (character of the window, identical for all variants).

### Example: 2026-09-06 06:00-12:00Z, autoISF_max 1.3/1.5 x dura_ISF_weight 0.5/0.7
```
emulator_optimize  example_2026-09-06   https://ns.vivikusi.ch   2026-09-06T06:00:00Z .. 2026-09-06T12:00:00Z   mode grid
ranked by delta insulin (emulated - original, SMB + TBR) - least extra insulin first     <- run of 2026-09-07, before the
                                                                                             fixes; today the table is in
                                                                                             grid order with a 'vs base' column

 # variant                                   autoISF_max  dura_ISF_weight loops     SMB o/e U     TBR o/e U  delta U     d%   SMB >/</=  ISFf mean/max  @max% iob>TH%    rise o/e U    rest o/e U
 1 autoISF_max_1.3__dura_ISF_weight_0.5              1.3              0.5   340        3/4.05   0.563/0.481    +0.97  +27.2    24/9/307       1.07/1.3    9.4     0.0     3.48/4.51     0.08/0.02
 2 autoISF_max_1.5__dura_ISF_weight_0.5              1.5              0.5   340        3/4.05   0.563/0.486    +0.97  +27.3    24/9/307       1.09/1.5    3.8     0.0     3.48/4.51     0.08/0.03
 3 base                                                                     340        3/4.45   0.563/0.488    +1.37  +38.6    24/2/314       1.09/1.3    9.7     0.0     3.48/4.86     0.08/0.07
 4 autoISF_max_1.3__dura_ISF_weight_0.7              1.3              0.7   340        3/4.45   0.563/0.488    +1.37  +38.6    24/2/314       1.09/1.3    9.7     0.0     3.48/4.86     0.08/0.07
 5 autoISF_max_1.5__dura_ISF_weight_0.7              1.5              0.7   340        3/4.45   0.563/0.493    +1.38  +38.7    24/2/314        1.1/1.5    3.8     0.0     3.48/4.87     0.08/0.08

replay bias of the base VDF (same settings as the phone): delta +1.37 U - subtract it mentally from every other row
```
How to read it: `base` and `autoISF_max_1.3__dura_ISF_weight_0.7` are identical because 1.3 / 0.7 were the phone's
settings that morning (`profile_history`) - this is the consistency check that no state leaks between the in-process runs
(loop counts must be equal too; the runner warns otherwise). The +1.37 U of `base` is the replay bias (SMB interval
tolerance and delivery ratio range, see above), not an effect of any setting. Relative to `base`, dura_ISF_weight 0.5
would have asked for 0.4 U less, all of it in the rising phase (`rise` 4.86 -> 4.51 U), in 9 loops with a smaller SMB;
raising autoISF_max from 1.3 to 1.5 changes almost nothing on this morning (+0.01 U, the factor sat on the 1.3 cap in
9.7 % of the loops and reaches 1.5 in 3.8 %). No loop had IOB above the emulated iobTH.

### The metrics are proxies - read them as such
The replay hands every variant the glucose, IOB and COB history that was recorded **with the original settings**. It
answers "what would the loop have asked for at each minute of this day", not "what would glucose have done". In
particular:
* an SMB the emulator gives 'extra' would have lowered the next hours' glucose and IOB on the phone; the replay does not
  see that, so a variant that boluses earlier is charged for the early bolus AND the later ones the phone actually gave.
  Deltas therefore overstate the effect of more aggressive settings and understate the effect of gentler ones.
* `iob_over_TH_pct` uses the original IOB against the emulated threshold, `rise`/`rest` use the original glucose curve.
* the numbers of a single window are dominated by that day's meals and activity. Run several days of different character
  (workday / weekend / sport / illness) and compare the ranking, not the absolute deltas; note the `profile_history`
  changes inside the window (ISF 4.9 -> 5.1 on 2026-09-04 21:20, autoISF_max 1.3 -> 1.5 on 2026-09-07 20:23,
  smb_max_range_extension 1.15 -> 1.8 on 2026-09-07 22:03) - the replay applies them to the base and to every variant
  that does not override the parameter.
* look at WHEN the extra insulin comes (`rise` vs `rest`, `SMB >/</=`, `@max%`) rather than at the delta alone, then
  open the `<start>_<end>.<variant>.txt` transcript of the loops that differ before changing anything on the phone.
* the replay-vs-phone agreement (section above) sets the noise floor: differences of a few 0.1 U per day between variants
  are within it.
