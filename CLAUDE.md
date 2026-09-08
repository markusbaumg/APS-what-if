# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python "what-if" emulator for AndroidAPS (AAPS). `software/determine_basal.py` is a statement-by-statement port of AAPS's `determine-basal.js` (oref1 / SMB) plus the autoISF fork. The emulator replays every historical loop run with its original inputs but modified settings (a `.vdf` "variant definition file") and reports how insulin delivery (insulinReq, SMB, TBR, ISF) would have differed: a semicolon CSV, a multi-page PDF and a flowchart of the decision path.

Fork of ga-zelle's APS-what-if / AAPS-Emulator. Branch `A3.4.0.0-ai3.2.0` targets AAPS 3.4 + autoISF 3.2 and reads AAPS logfiles; branch `nightscout-source` (work in progress) rebuilds the loop inputs from a Nightscout server instead. Upstream changes arrive as whole-file re-uploads through the GitHub web UI, so merging upstream means diffing whole files, not cherry-picking commits.

## Environment and commands

No packaging, no tests, no linter, no CI. The four legacy scripts and the `ns_*` modules live flat in `software/` and import each other as siblings, so run everything from `software/`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # matplotlib is the only third-party dependency
cd software
```

Batch run. Arguments are positional (no argparse; fewer than four raises IndexError):

```bash
PYTHONUTF8=1 ../.venv/bin/python emulator_batch.py '<logfile glob>' '<options>' <variant.vdf> [<start UTC> [<end UTC>]]

# AAPS logfiles (.zip or plain). Quote the glob: the core expands it itself.
PYTHONUTF8=1 ../.venv/bin/python emulator_batch.py '/path/aapsLogs/AndroidAPS._2026-09-06_*' 'All/-pred/-flowchart/.' variants/no_change.vdf 2026-09-06T06:00:00Z 2026-09-06T12:00:00Z

# nightscout-source branch: a URL, or "ns:" (= url from ns_settings.json), replaces the logfile glob
PYTHONUTF8=1 ../.venv/bin/python emulator_batch.py ns: 'bg/iob/ISFs/insReq/SMB/basal/noplot/.' variants/no_change.vdf 2026-09-06T06:00:00Z 2026-09-06T12:00:00Z
```

- `<options>` is a `/`-separated token list. `All` enables everything, `-token` excludes one; `All/-pred/-flowchart` is the documented fast run (`pred` = one PDF page per loop). Valid tokens are exactly the literals tested by `featured()` in `emulator_core.py` (`bg`, `iob`, `cob`, `ISFs`, `insReq`, `SMB`, `basal`, `LIST`, `PDF`, `acce_ISF`, ...); matching is case-sensitive after `_` is turned into a space. A `.` anywhere in the string selects `.` as the CSV decimal symbol, otherwise the CSV uses `,`.
- Start/end labels are UTC ISO strings compared lexically against each loop's `deliverAt`, so a prefix such as `2026-09-06T12` works. The defaults (`2000-…` / `2099-…`) mean "open".
- `PYTHONUTF8` must be exactly `1` or `parameters_known()` aborts with status `UTF8`. On Linux both front ends also append `export PYTHONUTF8=1` to `~/.bashrc` on every start.
- GUI: `python emulator_GUI.py` (no arguments) needs a Tk-enabled Python. The Homebrew build behind `.venv` has no `_tkinter`, so the GUI does not start on this machine.
- Phone: `emulator_batch.py` switches into an interactive QPython mode (keyboard menus, spoken alarms via `androidhelper`, endless loop synchronised to the AAPS loop) solely because it finds the AAPS log folders under `/storage/emulated/0`. The `*.config` files (alarm hours + table columns) are only read there. None of this can be exercised on a PC.
- Nightscout helpers: `python ns_client.py <url> <start> <end> [collection ...]` prefetches into `ns_cache/`; `python ns_source.py <start> <end> [url]` dumps the first reconstructed loop input set. `ns_glucose.py`, `ns_iob.py` and `ns_profile.py` are specified to carry `--validate` self-tests against `devicestatus`; check whether their methods still raise `NotImplementedError` before relying on the Nightscout path end to end.

Results are written next to the VDF file (not next to the logfiles, not in the cwd): `<base>.<variant>.csv|.delta|.log|.txt|.pdf` plus `<base>.orig.txt`. `<base>` is the first logfile's basename when no start time was given, otherwise the start label with `:` replaced by `-` (`nightscout` for an open Nightscout window). CSV and PDF are deleted before rewrite, and the core retries every 10 s while a viewer still holds them. `.gitignore` already covers `results/`, `ns_cache/` and result files under `variants/`.

The only regression check is the **"no change" replay**: an empty VDF (`variants/no_change.vdf`) must reproduce the original insulinReq, SMB, rate and ISF. Compare the `orig` / `emul` column pairs in the CSV.

## Architecture

Call chain, all synchronous in one process:

```
emulator_batch.py / emulator_GUI.py       build the argument strings + version banner, set_tty(channel)
  └─ emulator_core.parameters_known()     reset module globals, pre-scan the VDF, glob logfiles or pick Nightscout
       └─ scanLogfile() | scanNightscout()      one call per logfile / per Nightscout window
            ├─ collectors: get_glucose_status / apply_glucose_status, get_iob_data, get_currenttemp,
            │              get_profile, get_meal_data, get_autosens_data, PrepareSMB / scanConsoleError
            │              -> fill the module-global input dicts (glucose_status, iob_data, profile, ...)
            └─ TreatLoop()   once per original loop result ("Result:" JSON or devicestatus record)
                 ├─ record the ORIGINAL decision      -> orig* lists (SMB, insulinReq, basal, ISF, ...)
                 ├─ setVariant(stmp)                  re-read the VDF, eval() every row onto the input dicts
                 ├─ determine_basal.determine_basal(..., thisTime, Fcasts, Flows, emulAI_ratio)
                 └─ echo_rT() + text-scrape of the returned reason -> emul* lists
       └─ write CSV and .delta, then XYplots() (matplotlib -> PDF) unless Android or `noplot`
```

- **Two front ends, one entry point.** Both call `parameters_known(myseek, arg2, variantFile, startLabel, stoppLabel, entries, msg, my_dec, oldPauseCarbsReqEnds)` and get an 8-tuple back whose second element is the last loop label or a sentinel (`SYNTAX` = VDF error, `UTF8`, `Z` = nothing found). `arg2` is the option string prefixed with `Windows/` (PC) or `Android/` (phone); the core derives `isAndroid` from that prefix, not from the platform. All console output goes through `log_msg()` / `sub_issue()`, which `set_tty()` routes either to `print` or to the Tk text box; calling the core without `set_tty()` first raises NameError.
- **Everything is module-global state** in `emulator_core.py`: the parsed input dicts, `newLoop` (a "Glucose status" record opens a loop, `TreatLoop` closes it), `AAPS_Version` (`'<2.7'`; `'2.7'` covers 2.7 through 3.2; `'3.3'` covers 3.3, 3.4 and Nightscout) and dozens of parallel per-loop lists that must stay aligned with `loop_mills` (each collector appends or overwrites depending on the current length; `XYplots` pops leftovers). Adding a per-loop series means touching the collector, the CSV writer and the plot.
- **Logfile parsing** keys on AAPS's line layout `HH:MM:SS.mmm [thread] [Class.method():line]: message` and on padded message prefixes such as `'IOB data:       '`. AAPS 3.3+ writes all inputs and the result as one `PersistenceLayerImpl ... insertOrUpdateApsResult` line, handled by `TreatLoop33`. Zipped logs are read through `str(bytes)`, which is why several places delete a backslash before apostrophes and why `mmol\/L` keeps its backslash after `json.loads`. Log-line headers are phone local time; every timestamp inside the JSON is UTC.
- **determine_basal.py mirrors the JS.** The original JS lines stay as `#//` comments beside the Python; `typeof()`, `console_error()`, `round_basal()`, `convert_bg()` are shims so the emitted text matches AAPS's consoleError. Emulator-only parameters: `thisTime` (historical epoch ms instead of `new Date()`), `Fcasts` (dict the function fills with per-factor autoISF ratios, prediction arrays and `Levels`), `Flows` (list of `dict(title, indent, adr)` boxes rendered as the flowchart; `adr` is a hand-maintained JS row reference such as `'884+1'`), `emulAI_ratio`. Behaviour switches on the strings `profile['new_parameter']['AAPS_Version']` and `profile['autoISF_version']`, compared lexically.
- **Wording is load-bearing.** `TreatLoop` scrapes the emulated SMB and maxBolus out of the returned reason text (`'Microbolusing ... U'`, `'maxBolus'`) and the original values out of consoleError lines (`'SMB enabled'`, `'ISF unchanged:'`, `'final ISF factor is'`). Changing message text in `determine_basal.py`, or an upstream AAPS wording change, silently breaks the orig-vs-emul comparison.
- **New profile / new_parameter keys need defaults.** `determine_basal.py` indexes `profile[...]` directly and older logs lack newer keys, so every new key needs a fallback in the `if 'key' not in profile:` block of `setVariant()` and, on the Nightscout branch, an entry in `ns_settings.json` / `ns_profile.py`.
- **VDF files are Python.** Each row is `<group> <key> <expression> [### comment]`. Groups: `profile`, `new_parameter`, `glucose_status`, `iob_data`, `meal_data`, `autosens_data`, `currenttemp`, `state`, `temp`, plus `STAIR*`, `INTERPOL`, `POLYGON` for time- or value-dependent settings. Expressions are `eval()`'d inside `setVariant()` with the already-parsed dicts in scope, so `profile['min_bg'] + 10` works. The file is re-read for every loop. There is no comment-line syntax: a blank line or a `#rofile ...` row is logged as "not actioned". Reference: "Documentation in English/Guide to VDF Files for the AAPS Emulator.pdf"; example: `Demo_Sports_Adaptations.vdf`.
- **Nightscout branch.** `software/NIGHTSCOUT_SOURCE.md` is the binding contract between the modules. `ns_client.py` (stdlib urllib, per-UTC-day JSON cache in `ns_cache/<host>/`) feeds `ns_glucose.py` / `ns_iob.py` / `ns_profile.py`, which rebuild `glucose_status`, `iob_data` + `currenttemp` and `profile`; `ns_source.NightscoutSource.loops()` composes them with what `devicestatus` carries directly (original result, autosens ratio, COB, consoleError); `emulator_core.scanNightscout()` pushes each loop through the unchanged collectors and `TreatLoop`. `ns_settings.json` holds the AAPS preferences Nightscout does not carry (copied 1:1 into `profile`; VDF rows still override) and the personal Nightscout URL. Treat it as private configuration.

## Conventions and traps

- **Mixed line endings, per file:** `determine_basal.py`, `emulator_GUI.py`, the `*.config` files, `change_log.md` and the demo VDF are CRLF (upstream's format); `emulator_core.py`, `emulator_batch.py` and all `ns_*` files are LF. Preserve whatever a file already has, or the next upstream diff is pure noise.
- **Version stamps instead of tags.** Each module has `get_version_<name>()` returning a `'YYYY-MM-DD HH:MM'` string with a trailing comment naming the change. Bump it when you change the module and keep the previous line commented as `#cho_msg[...]`. The stamps are echoed into every result `.log`. `change_log.md` is newest-first: date heading, then bullets.
- **Code is disabled by deleting the first character** (`#cho_msg`, `#rofile`, `#lif`, `#f len(...)`) rather than prefixing `#`. Grep for the truncated keyword when hunting dead code.
- **No `__main__` guards** in the four legacy scripts: importing `emulator_GUI.py` opens a Tk window, importing `emulator_batch.py` runs Android detection and argv parsing. Only the `ns_*.py` modules have guards.
- **German identifiers and comments** in `emulator_core.py` (`hole(sLine, Ab, Auf, Zu)`, `zeile`, `wo`, `wdhl`) and German TTS strings in `emulator_batch.py` are load-bearing; do not rename or translate.
- `parser_debug = True` at the top of `emulator_core.py` appends every parsed log line to `/tmp/emulator_parser_debug.log`. Turn it off before long runs.
- Batch exit status is 0 even after a `SYNTAX` or `UTF8` abort; check the printed status, not `$?`.
- The PDFs under "Documentation in English" mostly predate the 2023 rename and still call the scripts `vary_settings_*.py`; the Installation Guide (2025) is the only document with current names, and the root README states the docs are not yet updated for this branch.
