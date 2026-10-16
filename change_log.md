16.Oct.2026
-----------
Review fixes on the Nightscout branch:
- emulator_core.py: the Nightscout error handler guards only the initial fetch (a failure there ends the run with 0
  loops); an exception inside a loop surfaces as a traceback instead of a misleading 'source failed' message with
  half-appended per-loop lists; the emulated maxBolus is read from the console echo on the AAPS 3.3+ / Nightscout path
- determine_basal.py: UAMduration rounded half up like the JS round(x,1), was Python round (half to even)
- ns_client.py: the pre-window Profile Switch / Temporary Target lookup skips records with isValid false and is
  cleared by --refresh together with the treatments day files
- ns_source.py: window bounds given with a UTC offset (+02:00 instead of Z) are normalised to UTC before the cache
  and the loop selection are addressed; the window is resolved once; profile switches and temp targets older than
  the 8 h treatments look-back are fetched separately so a switch running since the previous day is seen
- ns_profile.py: originalProfileName preferred over the display name of a Profile Switch; a running switch keeps its
  own profileJson over profile store uploads made later
- ns_meal.py: UAM duration rounded half up like Kotlin Math.round (was Python round, half to even)
- ns_client.py: the token is redacted from the error text of a failed request
- emulator_optimize.py: generated VDF rows use the VDF separator, a 'Z' sentinel from parameters_known() counts as a
  failed variant, grid and VDF paths are resolved before the chdir into software/
- emulator_core.py: parser_debug off by default
- git: 'upstream' remote (ga-zelle/APS-what-if, fetch only) added as the baseline for whole-file diffs


08.Sep.2026
-----------
Review fixes on the Nightscout branch (no-change replay of 2026-09-06: SMB exact 91.8 -> 96.1 %, TBR 93.8 -> 96.0 %,
emulated SMB total 28.05 -> 24.30 U against 20.25 U on the phone):
- ns_settings.json: smb_delivery_ratio_bg_range 36 mg/dl (fitted to the phone's printed delivery ratios, the export's
  1.0 mmol never fits), smb_max_range_extension 1.8 since 2026-09-07 22:03 (profile_history entry, maxBolus 1.1);
  new '_overrides' section that ns_prefs_import.py never overwrites from an export
- determine_basal.py: determine_varSMBratio gets int(bg) on the AAPS 3.3+/Nightscout path (Kotlin Int parameter)
- ns_source.py: loops selected by their run time deliverAt (window boundaries exact, .delta rows aligned),
  microBolusAllowed False on the 'SMB disabled (!microBolusAllowed)' echo (tempBasalFallback re-runs)
- ns_iob.py: planned temp ends rebuilt on the minute grid of the temp start (currenttemp.duration one minute short),
  basal cache keyed by minute (was ~1.4 million entries and 90 ms per loop per day)
- ns_profile.py: the running profile comes from the newest Profile Switch, a newer profile store upload only supplies
  the edited schedules; bisect instead of rebuilding the candidate list per call; --validate checks maxBolus
- ns_client.py: a day file is final only when written after the day ended (+1 h); truncated/garbled responses are
  retried; CLI accepts 'ns:' and --refresh
- emulator_core.py: 'noplot' no longer part of 'All' (PDF and LIST table were skipped for the GUI presets), Nightscout
  results named <start>_<end>.<variant>.*, transcripts say 'Nightscout <url>' instead of 'logfile', the .log header
  echoes the ns_settings.json stamp/hash/profile_history, a Nightscout fetch failure ends the replay like a
  truncated logfile instead of a traceback (since 16.Oct only the initial fetch is guarded)
- emulator_optimize.py: results/<grid>_<start>_<end>/, refuses to reuse a directory with a summary.csv without
  --overwrite, summary.txt in grid order with base first and a 'vs base' column (no longer 'least insulin first'),
  TBR metric documented as rate in force x interval


07.Sep.2026
-----------
Nightscout as data source (branch nightscout-source): the loop inputs are rebuilt from the Nightscout collections
instead of the AAPS logfiles; emulator_batch.py takes the Nightscout url as first argument.
- ns_client.py: API v1 reader with a per-UTC-day cache (offline replays), profile store served from the cache
- ns_glucose.py, ns_iob.py, ns_profile.py, ns_meal.py: rebuild glucose_status, iob_data/currenttemp, profile
  (incl. ns_settings.json preferences, temp targets, profile switches, activity monitor state) and the deviation
  slopes; each module has a --validate self-test against the phone's own console echoes
- ns_source.py: the replay driver (skips duplicate uploads, 2 h devicestatus lookback for planned temp durations)
- emulator_core.py: scanNightscout(), 'noplot' output option, autoISF echo counted once per loop
- emulator_optimize.py + variants/grid_autoISF.json: grid / one-at-a-time runner writing results/<label>/summary.csv,
  summary.txt (ranking by insulin delta) and summary.json with proxy metrics (SMB/TBR totals, SMB more/less loops,
  autoISF factor statistics, loops at autoISF_max, iobTH hits, insulin in rising vs other phases)
- documentation: software/NIGHTSCOUT_SOURCE.md (data contracts, how to run, measured no-change agreement,
  optimization example and how to read it), README section
Known differences to the phone (see NIGHTSCOUT_SOURCE.md): Python round() vs Kotlin Math.round, SMB interval
tolerance 12 s vs 6 s, zero-temp cap 120 vs 60 min.


28.Jan.2026
-----------
Added compatibility for Debian 13 as proposed by Dries

Added silencing of the spoken **x carbs required in y minutes** alarm by adhering to the related pause activation in AAPS


05.May.2025
-----------
Added handling of planned new AutoISF features
- state automation functionality proposed by faldor20
- gentle transition when Libre calibration introduces a sudden offset in BG values


25.Mar.2025
-----------
Further adaptations for AAPS3.3.2 logfile format
Included AutoISF bug fixes for
- reporting iobTH settings
- enabling resistance mode, i.e. sensitivity ratio from *low TT lowers sensitivity* is now the base and
  AutoISF adaptations are applied on top. This is analogous to the exercise mode, just the opposite direction.

A prototype python version is out for running the emulator on phones with Android-14 or higher. But this only
works for the dedicated AutoISF version, not for standard or dev versions of AAPS3.3


31.Aug.2024
-----------
Adapted to read the new AAPS3.3-dev logfile format. That format may change as long as 3.3 is in dev state.
Documentation
- Libre Office proforma now has macros for importing CSV file and generating the standard plots from the CSV file.


05.Jun.2024
-----------
Activity Monitor sleeping hours need to be shifted from local time to UTC.
The UTC offset is extracted from the logfiles.


02.Jun.2024
-----------
Re-enabled output of DELTA file


May 2024
--------
Adapted for APPS 3.2.0.4
Adapted for autoISF3.0.1
Documentation
- added usage guide for emualtor on phone
- Libre Office proforma for importing CSV file now has color coded cells when 'orig' and 'emul' deviate


25.Nov.2023
-----------
Adapted for autoISF3.0
Adapted for AAPS 3.2.0.2


12.Apr.2023
-----------
Adapted for autoISF 2.2.8.1
New capabilities
 - added commands to define circumdian profiles for lower and higher targets
 - new interface on Android to avoid Android13 problems
 - result files base name is now the start time of the evaluation window if given; this avoids oevrwring results for several time windows on the same day
Documentation
 - new guide for installing the emulator
 - new detailed guide for VDF instructions
 - several How-To guides (in progress)
General improvements
 - faster execution by skipping logfiles older than the emulation time window
 - the "no change" scenario can now be actived automatically by an empty VDF file
 - unify naming to call it "emulator" in line with general references instead of "what-if"
Bug fixes
 - runnnig on the APS phone it will no longer hang up when the active logfile is recycled while trýing to read it


05.Sep.2022
-----------
Adapted for autoISF version 2.2.7


05.May.2022
-----------
Adapted for AAPS version 3.0.0., still backward compatible down to version 2.5 as far as regular AAPS featutres are concerned
Added the autoISF features to its version 2.2.6
 - the various ISF factors are listed separately and replace the varius interim ISF columns
Added echo of script versions used and start parameters defined for accountability
Replaced TAB-file by CSV-File , ready for import into a spreadsheet
 - added option to select decimal symbol "." or ","
Added commands to define circumdian profiles for basal, ISF, carb rartio 
Removed bugs
 - handle loop executions in between the 5 minute cycle
 - European daylight savings time added until starting summer 2022
 - no more time shift between glucose curve and fitted curve
 
 
20.Apr.2021
-----------
Debugging
 - Skip events flaged as  "Error: CGM data is unchanged for the past ~45m. "
 - added more default settings


11.Apr.2021
-----------
Adapted for AAPS version 2.8.2, still backward compatible down to version 2.5 
Major addition
 - checking user formulae in VDF-File for correct python syntax
Added more values to list or plot, namely
 - quadratic regression of recent BG shown in plot as parabola
 - support autoISF by listing and plotting the autoISF factor
Removed bugs
 - in GUI mode the result files are now written to correct folder
 - European daylight savings time added for summer 2021
 

18.Nov.2020
-----------
Adapted for AAPS version 2.7, still compatible with versions 2.5 and 2.6
Major addition
 - support autoISF for BG being consistantly too high for a longer time
 - version selection "2.7" or "<2.7" as a what-if scenario
 - can run on AAPS phone, too,  to give real time what-if hints like in Open Loop
 - on AAPS phone provide spoken alarms for "add'l carbs req" or "extra bolus proposed" from what-if alternative
Added more values to list or plot, namely
 - ISF
 - range for BG being unchanged
 - linear regression of recent BG
 - several min and max values for time window in tab-file, e.g. for autosense or SMB
Limitation
 - the flowchart output for version 2.7 uses outdated row number references


31.May 2020
-----------
Major addition
 - GUI front end to collect inputs and guide users who are less IT literate
 - STAIR to define time dependent variations like basal rate
 - INTERPOL to define inter- and extrapolation in time dependent variations
Minor addition
 - logfiles may also be left in original zip format
Removed bugs
 - one error in determine_basal.py
 - one reporting error in assigning the variations


22.April 2020
-------------
Extended the selection of logfiles to define the evaluation time window by
 - using wildcard characters "*" and "?" to match several logfiles like AndroidAPS._2020-04-02* for a whole day
 - define a start time for the emulation in UTC format like "2020-04-02T10:00:00Z" for noon MESZ time zone
 - define an end time for the emulation in UTC firmat like "2020-04-02T12", i.e. ending at 14:00 in MESZ time zone
Added more values to list or plot, namely
 - IOB, COB, autosense ratio, insulin activity
Added graphic output for
 - predictions as shown on the AAPS home screen
 - flowchart of how the decisons in determine-basal are transversed at each time step
Debugged one error in determine-basal.py


31.March 2020
-------------
Adapted for AAPS verison 2.6.1
Debugged / added DLST handling for summer 2019 through to summer 2020 in ME(S)Z time zone


08.March 2020
-------------
Changes Related to variant definition improve checking the success of variant definition process:
 - debugged reporting of new, additional parameters
 - added reporting of ignored instructions


