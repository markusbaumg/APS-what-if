# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python "what-if" emulator for AndroidAPS (AAPS) with autoISF: `software/determine_basal.py` is a line-by-line
translation of the oref1 `determine-basal` (SMB) algorithm plus autoISF 3.2. The emulator replays every historical
loop run with the original inputs and compares the original decision (SMB, temp basal, ISF) with the decision the
algorithm would have taken under changed settings. It cannot simulate the glucose response, only the insulin decisions.

Two data sources feed the replay:
- **AAPS logfiles** (upstream behaviour, branch `A3.4.0.0-ai3.2.0`): parsed by `emulator_core.scanLogfile`.
- **Nightscout** (branch `nightscout-source`): rebuilt from `entries`, `treatments`, `devicestatus`, `profile`
  by the `software/ns_*.py` modules. Contracts and the mapping of every loop input are in `software/NIGHTSCOUT_SOURCE.md`;
  read it before touching any `ns_*` file. User specific AAPS preferences that Nightscout does not carry live in
  `software/ns_settings.json` (committed on purpose, including the Nightscout URL).

There is no build step, no test suite and no linter. Validation is done by replaying real data and comparing
"orig" against "emul" columns; each `ns_*` module has a `--validate <start> <end>` self test against Nightscout ground truth.

## Environment and commands

All commands run from `software/`. Python 3 with `matplotlib`/`numpy` for the plots (`python3 -m venv .venv &&
.venv/bin/pip install -r requirements.txt` at repo root); the `ns_*` modules and `determine_basal.py` are
stdlib-only because they also run on the phone under QPython. `emulator_core` refuses to run unless the
environment variable `PYTHONUTF8=1` is set (`emulator_batch.py` sets it itself).

```bash
# Nightscout replay: url (or "ns:" = url from ns_settings.json), output columns, VDF, UTC window
../.venv/bin/python emulator_batch.py https://ns.vivikusi.ch "seconds/bg/iob/ISFs/insReq/SMB/basal/noplot/." variants/no_change.vdf 2026-09-06T06:00:00Z 2026-09-06T09:00:00Z

# logfile replay (upstream way): glob of AAPS logfiles instead of the url
../.venv/bin/python emulator_batch.py "/path/AndroidAPS._2026-09-06*" "bg/iob/ISFs/SMB/basal/." my_variant.vdf 2026-09-06T06:00:00Z

# pre-fill / inspect the Nightscout cache (software/ns_cache/<host>/<collection>/<day>.json, gitignored)
python3 ns_client.py https://ns.vivikusi.ch 2026-09-01T00:00:00Z 2026-09-07T00:00:00Z

# module self tests against Nightscout ground truth
../.venv/bin/python ns_iob.py     --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z
../.venv/bin/python ns_glucose.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z
../.venv/bin/python ns_profile.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z

# parameter sweep for optimization (grid JSON -> generated VDFs -> results/<label>/summary.csv)
../.venv/bin/python emulator_optimize.py --grid variants/grid_autoISF.json --start 2026-09-06T00:00:00Z --end 2026-09-07T00:00:00Z

# Tk GUI front end (same core)
../.venv/bin/python emulator_GUI.py
```

The second argument is the output feature list, `/`-separated (`bg iob cob autosens acce bg_ISF pp dura ISFs insReq
SMB basal seconds noplot`), and its decimal symbol (`.` or `,`) selects the CSV decimal separator. Result files are
written **next to the VDF file**, named `<start time or logfile>.<variant>.csv|.log|.txt|.delta` plus `.orig.txt`
(console.error transcript of the original loops) and a PDF of plots unless `noplot` is given.
An empty VDF is the "no change" scenario and must reproduce the original decisions; use it to validate any change.

## Architecture (the part that spans files)

`emulator_batch.py` / `emulator_GUI.py` only collect arguments and call `emulator_core.parameters_known()`,
which owns the whole run:

1. `setVariant('1900-01-01T00:00:00')` pre-scans the VDF once, then per loop again (VDF lines are
   `<array> <item> <python expression>`; arrays are `profile`, `glucose_status`, `iob_data`, `meal_data`,
   `autosens_data`, `currenttemp`, `new_parameter`, `state` plus STAIR/INTERPOL/POLYGON helpers). It also
   fills defaults for profile keys missing in older logfiles, so new profile keys need a default there.
2. The scanner (`scanLogfile` or `scanNightscout`) fills **module-level globals** for one loop at a time through
   the same bookkeeping functions (`apply_glucose_status`, `get_iob_data`, `get_currenttemp`, `get_profile`,
   `get_autosens_data`, `get_meal_data`, `scanConsoleError`) and then calls `TreatLoop(json_of_original_result)`.
   `AAPS_Version == '3.3'` means these functions receive JSON strings (AAPS 3.3+ database records and Nightscout);
   older versions receive log-line fragments.
3. `TreatLoop` records the original decision (`orig*` lists), derives the regression/parabola extras
   (`getHistBG`, `getSlopeBG`, `getBestParabolaBG` on the per-loop `bg`/`bgTime` lists), applies the VDF, calls
   `determine_basal.determine_basal(...)` and records the emulated decision (`emul*` lists, `Fcasts` for the
   autoISF factors, `Flows` for the flowchart).
4. After the last loop `parameters_known` writes the CSV/delta files and `XYplots` draws the PDF.

All glucose values inside the pipeline are **mg/dl** (oref internal unit); `profile['out_units'] == 'mmol/L'` only
affects messages. Nightscout profile values in mmol are multiplied by 18 in `ns_profile.py`.
Because the state is global, `parameters_known` must be called sequentially in one process (the optimizer does this;
it re-initialises every list at the start of each call).

`determine_basal.py` keeps the structure of the original JavaScript/Kotlin, including `console_error(...)` echoes
(written to the `<variant>.txt` transcript, compared against the original transcript when hunting mismatches) and
`Flows.append(...)` entries with source line numbers for the flowchart. Additional emulator-only knobs go through
`profile['new_parameter']` rather than new function arguments.

Every module has a `get_version_*` function returning a date string that is echoed into the result logs; bump it
(and keep the previous line commented out, as the existing code does) when changing that module.
