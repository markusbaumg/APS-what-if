---
name: hindcast
description: Run a Hindcast what-if study of AAPS autoISF / SMB settings over recorded Nightscout days and report, in decision-level words only, where and how much the loop would have asked for insulin differently. Use when the user says "optimize settings", "what-if study", "which autoISF setting", "/hindcast", asks to compare autoISF or SMB preference values on past days, or asks to check in on, resume, review or propose from a running Hindcast study.
---

# Hindcast: an interactive sitting

Hindcast replays the AAPS loop's decisions (insulinReq, SMB, temp basal, the autoISF factor) on frozen
Nightscout days under other settings and condenses them into decision-level numbers next to a measured
noise floor. It never computes glucose. You run `hc` commands, read two small JSON files, publish one
page, and write in the vocabulary of `references/vocabulary.md`.

Honesty statement (print it at the start of the sitting, at its end, and wherever `hc` prints it):

> Hindcast replays the loop's decisions under other settings over the glucose and insulin history that the phone's own settings produced. It says where and how much the loop would have asked differently; it cannot say what glucose would have done. A variant that boluses earlier is charged for its bolus and for the later ones the phone actually gave, so deltas overstate aggressive settings and understate gentle ones. The base replay disagrees with the phone in about 4 % of loops; differences smaller than that floor are ties. Nothing here changes the phone; settings are for you and your care team to review. This is one person's insulin dosing, not medical advice.

## Setup

- cwd `software/` of the APS-what-if checkout, branch `hindcast`; `hc` means
  `../.venv/bin/python -m hindcast` (the venv is at the repository root).
- Studies live in `software/studies/<name>/`. Give `hc` the study name, or a directory path.
- References, read when the step says so:
  `references/protocol.md` (the steps in detail, stop conditions, exit codes),
  `references/vocabulary.md` (may / may-not wording, card template; generated, do not edit),
  `references/parameters.md` (what may be searched, AAPS preference names, what each key does),
  `references/reading-guide.md` (status.json, round.json and the page: floors, ties, envelope, P),
  `references/github.md` (campaign issue, round comments, proposals, the privacy rule).

## The never-list

1. Never write to the phone, to Nightscout, to `ns_settings.json`, to `aaps_preferences.json` or to a VDF
   under `variants/`. The human edits `ns_settings.json`; Hindcast only reads it.
2. Never state a glucose outcome: no claims about what glucose, lows or highs would have done, no
   outcome statistics of any kind, no statements about the future. Say what the loop would have asked for.
   The full list is `references/vocabulary.md`; `hc lint` and `hc skill checkin` apply it.
3. Never apply a proposal and never set the `applied-on-phone`, `accepted` or `rejected` label. Only the
   human does that, on the phone and on GitHub.
4. Never rank inside the floor: a difference under 2 x floor is a tie and is called a tie.
5. Never vary a locked key (`references/parameters.md`) on your own initiative. Tier C (profile `sens`,
   `carb_ratio`, `current_basal`) and tier S (`max_iob`, `max_basal`) are studied only when the human
   asks for it in this sitting and says why: `hc study new ... --unlock KEY ... --unlock-reason TEXT`.
   Such a study is weak evidence; say so on every card and in the summary. Never delete `trials.jsonl`,
   never run two studies against one study directory.
6. Never read a result CSV, a `.txt` transcript or `trials.jsonl` into context. Read `status.json`,
   `round.json`, the cards, and `hc explain` for the few loops that matter.
7. Never post glucose values, insulin units per day, loop counts per day, prediction arrays or
   transcript text to GitHub (the repository is public).

## The protocol in 12 steps (DESIGN.md section 7.3)

1. **Frame.** Read `ns_settings.json` (current values, `profile_history`, `_assumed`) and list the
   frozen and cached days with `hc freeze --list`. Ask once, in one message, for: days, day tags,
   hold-out days, the parameters (offer tier A of `references/parameters.md`) and the question
   (`explore`, `earlier` or `gentler`). Refuse locked keys unless the human explicitly unlocks them
   (tier C / S, `--unlock` with `--unlock-reason`; weak evidence). Create the study with `hc study new`
   and print the honesty statement (and, for an unlocked study, the weak-evidence caveat it prints).
2. **Warm and freeze.** `hc warm <days>` in the foreground, once (the only network step), then
   `hc freeze <days>`. A day that is not final yet is refused; say so and leave it out.
3. **Floor.** The first phase of every `hc study run` replays base on each day (phase `floor`). When
   `status.json` shows the floor, check `floor.agreement_pct`: below 94 % on a day that is not
   quarantined, stop the run (`hc study stop`) and propose a *finding* issue to the human
   (`references/github.md`); the study would measure the emulator, not the settings.
4. **Run.** Start `hc study run <name> --hours H --workers W` with Bash `run_in_background`. Open or
   refresh the campaign issue with `hc gh campaign <name> --artifact <url>` once the page exists.
5. **Check in** every 20-30 min (`/loop 20m`, or `Monitor` until `STATE.json` `phase` changes). Each
   check-in: `hc skill checkin <name>` (status.json only); apply `references/reading-guide.md`;
   `hc report <name> --single-file --artifact --out <scratchpad>/<name>`; republish the artifact to the
   same URL; post the round comment with `hc gh round <name>` only when the round number moved; one line
   in chat. Do not ping the human.
6. **Resume.** The same `hc study run <name> ...` command continues from `trials.jsonl`. A stale lock
   (dead heartbeat older than 10 min) is taken over; a live one is refused with exit 3: do not start a
   second run.
7. **Stop** when the budget is used, the review order or front is unchanged for 3 rounds, every
   parameter is dead, the fidelity floor is broken on more than 30 % of days, `stale_context` appears on
   a new round (unless `--on-stale reeval` was agreed), a sentinel or null mismatch stops the run (exit
   2), three trials in a row fail, or the disk has less than 2 GB free. Stop with `hc study stop <name>`
   and say which condition applied.
8. **Review.** Run the validate phase on the hold-out days when the study has one; write the cards with
   `hc card <name>`; for each card run `hc explain <name> <iso> --variant <vhash>` on the loops that
   differ most; write the summary in the allowed vocabulary with each candidate's VDF; publish the final
   page and offer to pin it.
9. **Propose** (interactive sittings only, never from a schedule): only a variant whose verdict is
   recommended for review becomes a proposal issue, and only after the human says so
   (`references/github.md`). The human sets the labels; the next sitting reads them back.
10. **Daily routine** (phase 2, schedule skill): warm yesterday once final, `hc freeze --new`, re-run the
    shortlist, demote on a sign flip, one line on the campaign issue. Never a proposal from a routine.
11. **Never**: the never-list above.
12. **Reporting to the human**: every check-in is one line; the sitting ends with the cards, the artifact
    link, the proposal issue links (if any) and the honesty statement.

## Check-in cadence

- One line per check-in, exactly what `hc skill checkin <name>` prints (fields: study, round, phase,
  trials done / planned, the floor, review-order item 1 with its verdict, the dead parameters, the run
  state). For the fixture study `hindcast/tests/fixtures/study_example` it prints:

  ```
  study_example round 1, phase grid: 5/5 trials; floor: base agrees with the phone in 92.4 % of loops (below the 94 % fidelity floor); review order 1: autoISF_max_1.3__dura_ISF_weight_0.5 (tie); not testable on these days: autoISF_max, dura_ISF_weight; finished
  ```

- Republish the artifact to the SAME URL at each check-in (Artifact publish with the same file path, or
  with `url` in a later conversation). Never create a second page for one study.
- At most one campaign comment per round: `hc gh round` is idempotent per round (a hidden marker),
  so call it only when `round` in `status.json` moved.
- Between check-ins say nothing. A guard stop (exit 2) or a broken fidelity floor is reported at once,
  in one line, with the reason `hc` printed.

## Commands (as `hc` has them today)

```
hc freeze --list                                   cache / frozen state per day
hc warm <days> [--allow-partial]                   fill the Nightscout day cache (network)
hc freeze <days> [--new]                           frozen/<host>/<day>.loops.jsonl.gz
hc study new <name> --days 2026-09-01..09-07 [--holdout 2026-09-05] [--tag 2026-09-05=weekend,sport]
             --param "profile autoISF_max" 1.1:1.8:0.05 | --tier A|B|C|S | --space-file F | --from-grid F
             [--unlock "profile sens" ... --unlock-reason TEXT]       tier C / S only on the human's word
             [--constraint "profile a <= profile b"] [--phase oaat:points=5] [--question explore|earlier|gentler]
             [--hours 6] [--workers 4]
hc study run    <name> [--hours H] [--until HH:MM] [--workers N] [--on-stale stop|reeval]
hc study status <name> [--json]                    <= 2 KB
hc study stop   <name>                             writes STOP; the run finishes its trial and exits 0
hc round <name> [--print]                          round.json + status.json from trials.jsonl
hc report <name> [--single-file] [--artifact] [--out DIR] [--days D,...] [--variants top3] [--pred top5]
hc card <name> [--rank N] [--vhash H] [--top N]    linted cards -> studies/<name>/cards/<rank>.md + .vdf
hc explain <name> <iso> [--variant <vhash>|base] [--json]     one loop's drill-down as text
hc gh campaign <name> [--artifact URL] [--dry-run]            open or refresh the campaign issue
hc gh round <name> [--artifact URL] [--dry-run]               at most one comment per round
hc lint --kind card|proposal|issue|line FILE...               the vocabulary and privacy lint
hc skill checkin <name> [--artifact URL]           the one-line check-in
hc skill install [--project] | vocabulary [--check] | lint     install / regenerate / lint this skill
```

Exit codes: 0 done or stopped, 1 command not available (or a GitHub call failed), 2 stopped by a guard or
refused by the lint, 3 refused (locked key, lock held, no such study).

Not built yet, so do not call them; say so if the step needs them: `hc gh propose`, `hc gh sync`,
closing the campaign issue and opening a finding issue (all H-603), `hc export --db-rows` (H-504), a
phase flag on `hc study run`. Until H-603 lands, the human opens and closes those issues; hand them the
text in chat.

`hc skill install` copies this skill to `~/.claude/skills/hindcast/` (user scope); `--project` installs
it into the repository's `.claude/skills/hindcast/` instead, once the maintainers agree.
