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
| `meal_data` mealCOB, carbs, lastCarbTime, slopes | logged | `suggested.COB`, carb treatments; deviation slopes default (no carb entries in FCL use) | `ns_source.py` |
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
`glucose` is the value the loop saw (`record['openaps']['suggested']['bg']` when present), `date` the timestamp of that CGM reading. Return `None` if no CGM value within 12 min before `now_ms`.

### ns_iob.IobCalculator(treatments, basal_at, settings)
`basal_at(ms)` -> profile basal U/h (callable, supplied by ns_profile).
`iob_array(now_ms, sensitivity_ratio=1.0)` -> list of 48 dicts (t = now + i*5 min): `{'time': t_ms, 'iob', 'activity', 'basaliob', 'bolussnooze', 'netbasalinsulin', 'hightempinsulin', 'iobWithZeroTemp': {'iob','activity','basaliob'}}` following AAPS `calculateIobArrayForSMB` (bolus IOB + temp-basal IOB net of profile basal * sensitivity_ratio; `iobWithZeroTemp` = same but with the running temp cut at now and a 0 U/h temp for 240 min from now).
`iob_at(t_ms, now_ms, sensitivity_ratio)` -> one such dict. `last_bolus_time(now_ms)` -> ms or 0. `current_temp(now_ms)` -> `{'rate': U/h, 'duration': minutes left (0 = none), 'start': ms}`. `last_temp(now_ms)` -> `{'date': start ms, 'rate', 'duration': full minutes}` of the most recent temp started at or before now (see oref0 `lastTemp`).

### ns_profile.ProfileBuilder(ns_profiles, treatments, settings)
`profile_at(now_ms, console_error=None)` -> the full `profile` dict for `determine_basal.py` (every key it reads; see list in the module). Includes `current_basal`, `sens`, `carb_ratio`, `min_bg`, `max_bg`, `target_bg`, `temptargetSet`, `dia`, `max_daily_basal`, `out_units`, `profile_percentage`, `autoISF_version`, all settings sections copied 1:1, `profile_history` applied by time.
`basal_at(ms)` -> U/h scheduled basal incl. profile switch percentage. `activity_monitor_json(console_error, now_ms)` -> dict `{recentSteps5Minutes ... recentSteps60Minutes, phone_moved, time_since_start}` reproducing the state printed in consoleError.

### ns_source.NightscoutSource(url_or_ns, start_iso, end_iso)
`loops()` yields one dict per original loop run inside the window, in time order:
`record` (raw devicestatus), `suggested`, `stamp` (deliverAt ISO), `now_ms`, `utcOffset` (hours), `glucose_status`, `iob_array`, `currenttemp`, `lastTempAge` (min), `profile`, `activity_monitor`, `autosens_data`, `meal_data`, `microBolusAllowed`, `console_error` (list), `autoISF_version`.
`emulator_core.scanNightscout()` feeds these through the unchanged bookkeeping functions (`apply_glucose_status`, `get_iob_data`, `get_currenttemp`, `get_profile`, `get_autosens_data`, `get_meal_data`, `scanConsoleError`) and then `TreatLoop(json.dumps(record))`, exactly like a logfile loop.

## Validation targets (each module has a `python ns_xxx.py --validate <start> <end>` self-test)
* IOB: `openaps.iob.iob/basaliob/activity` per loop: median |error| < 0.05 U, report worst cases.
* Glucose: `tick` (delta), `dura_ISF ... for Xm` minutes, `acce_ISF adaptation is` (=> bg_acceleration), `Parabolic fit extrapolates a maximum/minimum of X in about Y minutes`.
* Profile: `profile.sens: X, sens: Y`, `Basal unchanged: X`, `CR: X`, `targetBG`, `SMB enabled; current target X has even decimal`, `User setting iobTH=...`.
* End to end: "no change" replay (empty VDF) must reproduce `insulinReq`, `units` (SMB), `rate` and `variable_sens` of the original loop for the large majority of loops; mismatches are listed with reasons.

## Optimization
`emulator_optimize.py` runs a parameter grid (each combination = generated VDF) over the same cached window, without plots, and writes `results/<label>_summary.csv` with per-variant metrics (total SMB/TBR insulin, delta to original, ISF factor statistics, iobTH hits, insulin during rise vs fall phases) - see the module docstring. Every run is reproducible offline from the cache.
