# Review: autoISF fork `kusi/3.4.2.6-backports` vs the Python emulator port

Read-only review of `/Users/markus/dev/autoisf` (branch `kusi/3.4.2.6-backports`, 24 commits ahead of
`upstream/3.4.2.6+aisf3.2.1`, 68 files / +3330 -201) against
`/Users/markus/dev/APS-what-if/software/determine_basal.py` (port of autoISF 3.2.0, stamp `2026-09-08 04:00`).
Nothing was edited, fetched, checked out or built.

Tag dates for orientation: `upstream/3.4.0.0+aisf3.2.0` tip 2026-02-04, `upstream/3.4.2.1+aisf3.2.0` tip 2026-04-05,
`upstream/3.4.2.6+aisf3.2.1` tip 2026-10-04.

## 1. The 24 backport commits (oldest first)

| # | Commit | One line | Module | Class |
|---|--------|----------|--------|-------|
| 1 | `028e5d39c7` | Kusi adaptations: every BG value in consoleError converted to user units (mmol/L), six division-by-zero guards, `smbmaxminutes`/`uamsmbmaxminutes` max 120 -> 600, Wear bolus tile, Wear carbs input disabled | `plugins/aps/.../DetermineBasalAutoISF.kt`, `core/keys/IntKey.kt`, `wear/` | **ALGORITHM echo text + edge-case math + settings range** |
| 2 | `449c10fee7` | keystore.properties based release signing in `app/build.gradle.kts` | build | infra |
| 3 | `a9b824e44a` | Makefile, release signing for app + wear | build | infra |
| 4 | `346a68550c` | Makefile: wireless watch debugging, dist folder | build | infra |
| 5 | `b512179ee2` | Do not recalculate IOB/COB for a glucose change that carries no new data (`holdsSameData()` filter in CompatDBHelper; backport of nightscout `dbd137fa9c`) | `database/persistence/CompatDBHelper.kt`, `plugins/main/.../AutosensDataStoreObject.kt` | input plumbing (when the calculation runs), no math |
| 6 | `2b5951228f` | Let a running calculation finish before a history change restarts it (`HistoryChangeScheduler`, `awaitCalculationIdle`; backport of `b1951f5acd`) | `plugins/main/.../iobCobCalculator/`, `workflow/CalculationWorkflowImpl.kt` | input plumbing (scheduling) |
| 7 | `18ee33db61` | Remove the per-time basal data cache of `getBasalData` (result depended on the profile passed; backport of `d718bf7ef8`) | `IobCobCalculatorPlugin.kt` | input correctness (TBR/basal lookup), no algorithm change |
| 8 | `d659d59749` | Keep the cached IOB when a new BG arrives; drop only when DIA / bolus snooze divisor / time zone changed (backport of `2e4d7be3c7`, adapted) | `IobCobCalculatorPlugin.kt`, `workflow/LoadBgDataWorker.kt` | input plumbing (IOB cache) |
| 9 | `5aec038714` | IOB cache: read under the lock (#5211, backport of `24cf116a75`) | `IobCobCalculatorPlugin.kt` | infra (thread safety) |
| 10 | `13d5d99561` | Full recalculation now carries the dropped `EventNewBG` timestamp so the loop still runs for that BG (fixes a loop run skipped after a preference change) | `IobCobCalculatorPlugin.kt`, `HistoryChangeScheduler.kt` | input plumbing: loop *timing* (one more loop per affected BG), no math |
| 11 | `5eee5b98c0` | Makefile: adb/apksigner from SDK, verify target | build | infra |
| 12 | `d31e29db8a` | Round-2 fixes: overview basal graph and **TDD** read TBRs/EBs once per range; `LatestOnlyRunner` for cannula usage. Explicitly NOT ported: `AutosensDataStoreObject.clone()` referenceTime fix (would pin the 5-min bucket grid and change deltas/autoISF inputs with a 1-min source) | `implementation/stats/TddCalculatorImpl.kt`, `ProcessedTbrEbDataImpl.kt`, `workflow/PrepareBasalDataWorker.kt`, overview UI | input (TDD) with identical per-step result claimed; display |
| 13 | `6a8dc0c737` | Range reads keep overlapping TBRs/EBs ("active between" query), tie by id; `RunningAt` binary search. Fixes TDD steps that counted profile basal where an older, longer TBR was still running | `core/utils/RunningAt.kt`, `TddCalculatorImpl.kt`, `ProcessedTbrEbDataImpl.kt`, DAOs | input correctness (TDD; only matters with overlapping TBR rows) |
| 14 | `612f674a5c` | Target line: read temporary targets once per graph range | `workflow/PrepareTemporaryTargetDataWorker.kt` | UI |
| 15 | `a2220d1f56` | `LatestOnlyRunner`: a failed run neither crashes the app nor strands the next value | `plugins/main/.../overview/ui/` | infra (crash fix) |
| 16 | `264d6c2ee4` | CompatDBHelper: invalidate from the *oldest* EffectiveProfileSwitch of a batch (keeps IOB/autosens and the profile cache from staying stale after #8) | `CompatDBHelper.kt` | input correctness (profile switch handling for IOB/autosens) |
| 17 | `560442df0f` | NSClientV3: wake lock only around socket work (30 s timeout) instead of app lifetime | `plugins/sync/nsclientV3/services/NSClientV3Service.kt` | infra (battery); affects *upload* timing of devicestatus, not its content |
| 18 | `61ab049b5e` | Logging: `WritableLogFileDefiner` picks `AndroidAPS.log` or `AndroidAPS_1..9.log` when the active file is orphaned in MediaStore | `app/.../logging/`, `logback.xml` | infra (logfile names: the emulator's logfile glob must accept `AndroidAPS_N.log` variants) |
| 19 | `b1223f259b` | Logging: 7 days of hourly logs, max 500 MB | `logback.xml` | infra (shorter logfile retention for the logfile replay) |
| 20 | `94e5736dbe` | KeepAliveWorker daily DB cleanup honours `maintenance_cleanup_days`, min 10 days; never deletes the ProfileSwitch / RunningMode in force now | `app/.../KeepAliveWorker.kt`, `database/AppRepository.kt` | infra / input boundary (history depth for TDD/autosens) |
| 21 | `80d734ad9e` | `PhoneSensorRegistrar`: step counter and accelerometer registered only when `steps_graph_from_smartphone` / `activity_detection` are on; `StepService.resetBaseline()` | `app/MainApp.kt`, `app/utils/PhoneSensorRegistrar.kt`, `plugins/aps/.../openAPSSMB/StepService.kt` | **activity-monitor input** (phone has both on -> behaviour unchanged) |
| 22 | `902f87552b` | SMB interval: `LoopPlugin` (2x) and `CommandSMBBolus` use `SmbInterval.isTooSoon()` = the algorithm's `interval - 6 s` tolerance instead of the full interval | `core/data/aps/SmbInterval.kt`, `plugins/aps/loop/LoopPlugin.kt`, `implementation/queue/commands/CommandSMBBolus.kt` | **LOOP OUTPUT: enacted SMBs** (algorithm text unchanged) |
| 23 | `f502823d78` | Local alert "loop not running" (BG arrives, no decision for N min), prefs `enable_loop_not_running_alert`, `loop_not_running_threshold` | `implementation/alerts/LocalAlertUtilsImpl.kt`, `KeepAliveWorker.kt`, keys, strings | infra (read-only alarm) |
| 24 | `170a0ffeb2` | Round-3 fixes: loop counted inactive while pump not initialised; DB cleanup keeps the EPS in force at the cutoff; step baseline reset before registering, `previousStepCount` volatile | `KeepAliveWorker.kt`, `AppRepository.kt`, `PhoneSensorRegistrar.kt`, `StepService.kt` | infra / activity-monitor input (race only) |

`_docs/WHATS_NEW_3.4.2.6+aisf3.2.1.md` (untracked) describes the *upstream* jump 3.4.0.0 -> 3.4.2.6 and lists the
kusi tweaks as riding along. Its hash placement checks out against the tags: `d1bad3c553` (COB double count),
`3ebb4b17a1` (normal target 99), `0f35b6fc2d` (calibration test), `24f17e2115` (bolus while suspended) are already in
`3.4.2.1+aisf3.2.0`; `517313d5c3` (UKF), `d2a0197681` (get-up trigger), `f3040ea9c9` (STATES export) are 3.4.2.6-only.
One caveat: it calls the "no parabola fit / SMB during Libre calibration" a 3.2.0->3.2.1 change; `0f35b6fc2d` only
extends a unit test, the behaviour itself predates 3.4.0.0 (and the Python port has it since `2025-05-03`).

## 2. Which backports touch the loop algorithm, its inputs or its echo text

**Algorithm file itself (DetermineBasalAutoISF.kt): only `028e5d39c7`.** Three kinds of change inside it:

1. consoleError / reason text in user units. Every `$x` for a BG-type value became `${convert_bg(x)}`
   (`profileUtil.fromMgdlToStringInUnits`), units appended via `bgUnits()` where "mg/dL" was hard-coded.
   Lines (HEAD): 75, 250, 284, 286, 308, 310, 379, 382, 386, 389, 393, 396, 463, 469, 529, 683, 684, 769, 771, 774,
   776, 867, 1109; reason text 910 and 986 (`Min. Delta` now `convert_bg(minDelta)` instead of `toFixed2()`).
   Text only, but it is the text the emulator scrapes (see section 4).
2. Division guards, decision-relevant only in degenerate inputs: `totalCA` (497, csf <= 0), `remainingCIpeak` (506,
   remainingCATime <= 0), `fractionCarbsLeft` (691, carbs == 0 -> 0.0), `blendPct` (733, target_bg <= threshold ->
   0.5), `carbsReq` (865, csf <= 0 -> 0), `roundSMBTo` (1060, bolus_increment <= 0 -> 10.0).
3. `IntKey.kt:52-53`: `smbmaxminutes` / `uamsmbmaxminutes` upper bound 600 (phone runs 240, impossible on stock).

**Decision-relevant outside the algorithm file:**

- `902f87552b` (SMB interval). Before: the algorithm wrote `Microbolusing xU` into `reason` when
  `lastBolusAge > interval - 6 s`, but `LoopPlugin.kt:510/864` and `CommandSMBBolus` dropped the SMB unless the full
  interval had passed. With `smbinterval = 1` and 1-min CGM, SMBs computed 54-59 s after a bolus were suggested but not
  enacted. After: suggested == enacted. The suggested record (what the emulator replays) is unchanged, but the
  insulin history, i.e. `iob_data`, `lastBolusTime` and `lastBolusAge` of the *following* loops, changes.
- `80d734ad9e` / `170a0ffeb2` (StepService). With both step settings on (the phone's state) the registration is as
  before; `resetBaseline()` only avoids a step burst in one 5-min block after toggling. Activity-monitor input, marginal.
- `13d5d99561`, `b512179ee2`, `2b5951228f`: change *when* a loop runs (a BG no longer skipped after a preference
  change; no recalculation for an unchanged BG row). More or fewer devicestatus records, same math.
- `6a8dc0c737`, `d31e29db8a`, `18ee33db61`, `264d6c2ee4`: TDD and basal lookups. autoISF reads TDD only for the
  dynISF-fallback notification (`OpenAPSAutoISFPlugin.kt:211`); `getBasalData` feeds IOB from temp basals. These fix
  stale or wrong *inputs* in rare overlap/race situations, so a few historical loops may differ from what the same
  inputs would give now. Not portable; the emulator reconstructs IOB itself (`ns_iob.py`).
- `94e5736dbe` / `170a0ffeb2` (DB cleanup, min 10 days, EPS at cutoff kept): bounds the history available to the
  phone; the emulator's Nightscout history is independent.

**Pure infrastructure / UI / build:** `449c10fee7`, `a9b824e44a`, `346a68550c`, `5eee5b98c0`, `5aec038714`,
`612f674a5c`, `a2220d1f56`, `560442df0f`, `61ab049b5e`, `b1223f259b`, `f502823d78`.
Two of them matter operationally for the logfile replay: `61ab049b5e` can name the active log `AndroidAPS_1.log` ..
`AndroidAPS_9.log` (the emulator's `AndroidAPS._YYYY-MM-DD_...` glob must tolerate that), and `b1223f259b` keeps only
7 days of hourly zips.

## 3. autoISF 3.2.0 -> 3.2.1 (+ backports): algorithm and echo differences the Python port must mirror

`git diff upstream/3.4.2.1+aisf3.2.0..upstream/3.4.2.6+aisf3.2.1 -- plugins/aps/src/main` touches **one** file,
`OpenAPSAutoISFPlugin.kt`, 3 hunks. `DetermineBasalAutoISF.kt` and `GlucoseStatusCalculatorAutoIsf.kt` are identical
between the two tags. Since the Python port was written against an older 3.2.0 revision, the 3.4.0.0 -> 3.4.2.1 range
(same autoISF version string) is included where it touched the algorithm.

| # | Where (Kotlin, HEAD line) | Difference | Decision or text | Python port today | To mirror |
|---|---------------------------|------------|------------------|-------------------|-----------|
| A | `OpenAPSAutoISFPlugin.kt:142` | `autoIsfVersion = "3.2.1"` (3.4.2.6 only) -> `start AutoISF 3.2.1` echo | text | version compared lexically `>= '3.1.0'`, fine | nothing; `ns_profile._re_version` already picks it up |
| B | `OpenAPSAutoISFPlugin.kt:674` (`d2a0197681`, 3.4.2.6 only) | get-up query: `if (useSleepState && recent5+10+15 == recent30)` sets automation state `query_got_up = query_it`; was `< recent30 && now >= inactivity_idle_end` | **indirect decision**: only via an automation rule that ends `Sleeping` (target/profile change) | not modelled (`state['Sleeping']` is read, never derived, `determine_basal.py:719-720`) | nothing in `determine_basal.py`; the effect arrives through the TT / profile switch the automation performs, which Nightscout carries |
| C | `Constants.kt:14`, `DetermineBasalAutoISF.kt:231`, `OpenAPSAutoISFPlugin.kt:163` (`3ebb4b17a1`, 2026-03-08, in 3.4.2.1) | `normalTarget = NORMAL_TARGET_MGDL = 99` instead of 100 for `exerciseModeActive` (`target_bg > 99`) and `resistanceModeActive` (`target_bg < 99`), and in the ratio formula `c = half - 99`, `c / (c + target_bg - 99)` | **DECISION** | `determine_basal.py:905` `normalTarget = fullBasalTarget` = 100 when no `full_basal_exercise_target` | set 100 -> 99 for `autoISF_version >= '3.2.0'` (or AAPS 3.4.2.1+). **This explains the `_assumed` entry in `ns_settings.json:234`:** TT 5.5 mmol = 99 mg/dL is no longer "below normal", so the phone never printed a resistance ratio although `low_temptarget_lowers_sensitivity` is on in the export. With 99 in the port, the export value `true` can be restored and the override `flags.low_temptarget_lowers_sensitivity: false` in `_overrides` dropped |
| D | `DetermineBasalAutoISF.kt:74` | `enable_smb` high-TT test stays `target_bg > 100` in the autoISF class (DetermineBasalSMB.kt:74 moved to the 99 constant, autoISF did not) | decision (no change) | `determine_basal.py:177/1066` `> 100` | nothing (already matches); do not "fix" it to 99 |
| E | `DetermineBasalAutoISF.kt:257` | `consoleError.add("Sensitivity ratio unchanged: 1.0")` before `Autosens ratio:` in the no-TT/no-steps branch (present since 3.4.0.0+aisf3.2.0, i.e. not a 3.2.1 addition as CLAUDE.md assumes) | text | `determine_basal.py:955-959` prints only `Autosens ratio: ...;` | add the line for transcript parity (it is what `ns_settings._assumed` quotes as evidence) |
| F | `DetermineBasalAutoISF.kt:263` | `iobTH_reduction_ratio = profile_percentage/100 * sensitivityRatio` (sensitivityRatio = TT ratio, or activityRatio, or **autosens ratio**); unchanged since 3.3.3.a+aisf3.1.0 | **DECISION** when `iob_threshold_percent != 100` (phone: 80) and autosens ratio != 1 | `determine_basal.py:963` `profile_percentage/100 * exercise_ratio * activityRatio` -> autosens ratio never enters | pre-existing divergence, not a 3.2.1 change; use `sensitivityRatio` like Kotlin (affects effective iobTH and the Full-loop SMB cap) |
| G | `DetermineBasalAutoISF.kt:1110` | `lastBolusAge > SMBInterval - 6.0` | **DECISION** (SMB vs "Waiting") | `determine_basal.py:2268` `> SMBInterval - 12` ("to be 6s tolerance but released as 12s") | change 12 -> 6. With `smbinterval = 1` the window 54..60 s is exactly where Python says "Microbolusing" and the phone says "Waiting 0m Ns"; before `902f87552b` the phone additionally did not *enact* its own 54-59 s SMBs, afterwards it does, so the suggested/enacted gap closes |
| H | `DetermineBasalAutoISF.kt` 497, 506, 691, 733, 865, 1060 (`028e5d39c7`) | division guards (section 2) | decision in degenerate inputs only | `determine_basal.py:1383` (`totalCA`), `1395` (`remainingCIpeak`), `1622` (`fractionCarbsLeft`, guarded by `if meal_data['carbs']` but leaves the variable undefined on the first loop with carbs == 0 if the `minUAM<999 and minCOB<999` branch is taken), `1701` (`blendPct`), `1888` (`carbsReq`), `2194` (`roundSMBTo`) | mirror the six guards with the same fallbacks (0.0, 0.0, 0.0, 0.5, 0, 10.0) |
| I | `DetermineBasalAutoISF.kt` text lines listed in section 2 (`028e5d39c7`) | BG-type values printed in mmol/L (1 decimal) with `mmol/L` unit text; `Min. Delta` in reason converted | text, but scraped | Python prints mg/dL for `ISF unchanged:` (`:1040`, `short(sens)`), `profile.sens:`, `Carb Impact`, `UAM Impact`, `minPredBG`, `naive_eventualBG`, ...; `convert_bg` already used for targets | for transcript parity convert the same lines with `convert_bg(.., profile)`; **more important: keep the scrapers unit-aware** (section 4) |
| J | `OpenAPSAutoISFPlugin.kt:710` | `consoleLog "Activity monitor detected neutral state"` (ratio suffix commented out, already in 3.4.0.0) | text | `determine_basal.py:763` appends `, sensitivity ratio unchanged: X` | drop the suffix for parity (ns_profile's `am_class()` already strips it) |
| K | `IntKey.kt:52-53` | `smbmaxminutes`/`uamsmbmaxminutes` max 600 | settings range | `profile['maxSMBBasalMinutes']` taken as given (240 in `ns_settings.json:59-60`) | nothing; sweeps may range up to 600 |
| L | `plugins/smoothing/UnscentedKalmanFilterPlugin.kt` (`517313d5c3`, 3.4.2.6, opt-in; **enabled on the phone**, `aaps_preferences.json:158`) | the BG series the loop sees is UKF-smoothed instead of exponential | **input** | `ns_glucose.py` rebuilds glucose_status from `devicestatus.suggested.bg` (the smoothed value the loop used) and raw entries | nothing in the port; note that loops before the upgrade used exponential smoothing, so dura/acce replays across the upgrade date mix two smoothers |
| M | `workflow/iob/IobCobOref1Worker.kt:190` (`d1bad3c553`, 2026-03-11, in 3.4.2.1) | carbs on a 5-min window boundary no longer counted twice (`bgTime - 5min + 1`) | **input (COB)** | COB comes from `suggested.COB`; `ns_meal.py` rebuilds deviation slopes | nothing; pre-March-2026 loops may carry inflated COB |

Not in the ranges but checked: `GlucoseStatusCalculatorAutoIsf.kt`, `StepService` 5-min bucketing, iobTH /
SMB-ratio / parity consoleLog texts (`OpenAPSAutoISFPlugin.kt:1054-1159`) are unchanged 3.4.0.0 -> HEAD.

## 4. Emulator side: what the backports break or explain

1. **`emulator_core.scanConsoleError()` reads the kusi mmol lines as mg/dL.** `emulator_core.py:771-779` sets
   `SMBreason['origISF']` from `ISF unchanged: 6.1` / `ISF from a to b` / `profile.sens: 6.1, sens: 6.1, CSF:` without
   a unit factor. On the 3.3+/Nightscout path `variable_sens` overrides it (`:814`, `:1901`), so the wrong value only
   surfaces when `variable_sens` is missing; `ns_profile.py:723-727` (`--validate`) already multiplies by
   `MMOL_TO_MGDL`. Make `scanConsoleError()` do the same when `out_units == 'mmol/L'`.
2. `ns_glucose.py:736` regex `bg is only ([0-9.]+)m at level` cannot match the Kotlin text
   `dura_ISF by-passed; bg is only $dura05 m at level $avg05` (`OpenAPSAutoISFPlugin.kt:900`, space before `m`), so
   the dura by-pass lines are silently skipped in `--validate`. Unrelated to the backports, found while mapping scrapes.
3. The `lastBolusAge > SMBInterval - 12` port (G) and `normalTarget = 100` (C) are the two decision-level divergences
   that can show up in the "no change" replay of this user: `smbinterval = 1` and TT 5.5 mmol are both in daily use.
4. `ns_iob.py:508` scrapes `last bolus Xm ago` from the `naive_eventualBG ...` line; the kusi text added a space after
   the comma and converted `naive_eventualBG`, the `last bolus` and `maxBolus:` parts are unchanged, so
   `_re_last_bolus` and `re_maxbolus` keep matching.
5. `ns_meal.py:282/317` already accepts `UAM Impact: X (mmol|mg/dL) per 5m`, i.e. the kusi `bgUnits()` text
   (`profileUtil.units.asText` gives `mmol` for mmol users) was anticipated. Check that the printed value is scaled
   back by 18 before being compared with `uci` (it is in mmol now).
6. Logfile replay: accept `AndroidAPS_N.log` basenames (`61ab049b5e`) and expect at most 7 days of zips (`b1223f259b`).

## 5. Bottom line

- Upstream autoISF 3.2.0 -> 3.2.1 changed one algorithm-adjacent line (the sleep get-up query, B) and the version
  string; the emulator needs nothing for it.
- The decision-relevant gaps between the Python port and the phone are older than 3.2.1: `normalTarget` 99 (C, from
  3.4.2.1), the 6 s vs 12 s SMB-interval tolerance (G), and the autosens ratio in `iobTH_reduction_ratio` (F).
- The kusi commit `028e5d39c7` changes echo text, not decisions, but the emulator scrapes that text; the Nightscout
  modules already handle the mmol/L lines, `emulator_core.scanConsoleError()` does not.
- Backport `902f87552b` closes the gap between suggested and enacted SMBs on the phone; it does not change the
  suggested record the emulator replays, but it changes the IOB history after 2026-10-16.
