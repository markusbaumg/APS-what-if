# Branch nightscout-source: replay from Nightscout instead of logfiles
This branch adds a second data source: every loop run that AAPS uploaded to **Nightscout** (`devicestatus`,
`entries`, `treatments`, `profile`) is rebuilt into the input set of `determine_basal.py`, so no logfile export is
needed and any window still on the server can be replayed. The first argument of `emulator_batch.py` is the Nightscout
url instead of the logfile pattern; everything else (VDF what-if files, output options, CSV/plots) is unchanged:

```
cd software
PYTHONUTF8=1 python emulator_batch.py https://ns.vivikusi.ch "bg/iob/ISFs/insReq/SMB/basal/noplot/." variants/no_change.vdf 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z
```

New modules (standard library only, they also run under QPython): `ns_client.py` (REST reader with a per-day cache in
`software/ns_cache/`), `ns_glucose.py` (glucose status incl. the autoISF parabola/dura fits), `ns_iob.py` (IOB array
with the AAPS insulin model), `ns_profile.py` (profile, temp targets, profile switches, AAPS preferences from
`ns_settings.json`), `ns_meal.py` (deviation slopes of the autosens worker), `ns_source.py` (the driver) and
`emulator_optimize.py` (grid runner: many VDFs over one window, tabulated with proxy metrics such as the insulin delta against the base variant).
The replay of the unchanged settings reproduces the phone's SMB in 96-97 % of the loops exactly (96.1 % over a full
day, 97.2 % over 06-09Z; 98.5-99.4 % within 0.05 U), the ISF in 99 %, the TBR in 95-96 %; the reasons for the rest are
listed in `software/NIGHTSCOUT_SOURCE.md`, which is
the reference for the data contracts, the commands and how to read an optimization run.

This branch is targeted at AAPS3.4.0.0 
The emulator documentation is not yet updated. Major changes are:
- support for AAPS 3.4.0.0 version
- include the new autoISF3.2.0 capabilities

In case of problems please contact ga-zelle.

See also the change.log

# APS-emulator

I translated the original java-script code of "determineSMB-basal.js" into python and can run it on a PC. 
This allows me to untertake a time tarvel back to any time slot and see how insulin delivery 
would have differed with different APS settings. This offers a safe method to play with settings 
and check their impact before actually adapting them in APS itself.

The historical logfiles contain enough information to rerun the APS loop, but with modified settings like
- changed target
- changed ISF
- SMB on/off
- ...

The main result in tabular and graphical output is the change in insulin required and the related SMB and TBR. 
Related data like SGV, target, Autosens ratio etc. are also shown.

A special output is the flowchart which helps to understand the logic flow through the loop. This is the 
track which statements in "determineSMB-basal" were executed and which not together with the reasoning 
of those decisions taken from the original code.

# Latest major change
There is a new release called QPythonPlus required for Android14 and above which allows running the emulator on phones. 
With older Android versions the previous qpython 3L / 3S still work, even with these updated scripts.

For downloading the Android14+ version go to https://drive.google.com/drive/u/3/folders/1lFqvlmArrV35ikcdW61MdVAx2UUWMcLh 
