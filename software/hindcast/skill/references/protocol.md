# Protocol: frame -> warm -> freeze -> floor -> run -> check in -> review -> propose

The detailed form of SKILL.md's 12 steps (DESIGN.md section 7.3). `hc` = `../.venv/bin/python -m hindcast`
from cwd `software/`.

## 1. Frame (one message to the human, then `hc study new`)

Read before asking:

- `ns_settings.json`: the current value of every key you intend to offer, the `profile_history`
  changes (a change inside a day for a searched key quarantines that day), the `_assumed` and `_overrides`
  keys (values fitted to the phone's echoes rather than exported; name them when one is searched).
- `hc freeze --list`: which days are cached, final and frozen.

Ask once, as one list:

1. Days (UTC dates, at least two for any ranking; three or more for leave-one-day-out).
2. Day tags typed by the human (`weekend`, `sport`, `sick`, ...): they become the strata of the cards.
3. Hold-out days (reserved for the validate phase; never seen by the search).
4. Parameters: offer tier A (`references/parameters.md`), or the keys the human names.
5. The question: `explore` (default: no ranking, review order), `earlier` (the loop reacts earlier on
   rises), `gentler` (the mirror image).

Then:

```
hc study new <name> --days <days> [--holdout <days>] [--tag DAY=TAG,TAG ...] --tier A | --param "<group> <key>" low:high:step ... [--question Q] [--hours H] [--workers W]
```

A locked key is refused with exit 3 and a list of reasons; relay the reasons, do not look for a way
around them. Print the honesty statement after the study exists.

Tier C (scales of profile `sens`, `carb_ratio`, `current_basal`) and tier S (`max_iob`, `max_basal`) are
locked by default. Offer them only when the human asks for them, and only with their explicit unlock
and reason, given in this sitting:

```
hc study new <name> --days <days> --tier C --unlock "profile sens" "profile carb_ratio" "profile current_basal" --unlock-reason "<the human's words>"
```

`hc study new` stores the keys and the reason in study.json and prints the weak-evidence caveat: the
replay keeps the glucose and insulin history the phone's own profile produced, so it cannot judge a
different profile, only say how often and by how much the loop would have decided differently. Quote
that caveat in every check-in summary, card and proposal text of such a study. `profile dia` and every
`new_parameter` key cannot be unlocked.

## 2. Warm and freeze

```
hc warm <days>          # once, foreground; the only step that talks to Nightscout
hc freeze <days>        # refuses a day whose cache is not final (written less than an hour after its end)
```

A refused day is left out of the study and named in the next line to the human.

## 3. Floor

The first phase of every run is `floor`: the base replay (empty VDF, the phone's settings with
`profile_history` applied) on each day, plus the sentinel. Read `status.json` once the phase has moved
on:

- `floor.agreement_pct` below 94 on a day that is not quarantined: `hc study stop <name>`; propose a
  *finding* issue to the human (`references/github.md`; there is no `hc gh` command for it yet, H-603:
  hand the human the text). The study would measure the emulator.
- Quarantined days are listed on the page and in the cards, never hidden, and not scored.

## 4. Run

```
hc study run <name> --hours 6 --workers 4          # Bash run_in_background; Ctrl-C, kill or STOP any time
hc gh campaign <name> --artifact <artifact url>    # after the first publish of the page
```

## 5. Check in (every 20-30 min)

1. `hc skill checkin <name>` (reads `status.json` only).
2. Apply the decision rules of `references/reading-guide.md`.
3. `hc report <name> --single-file --artifact --out <scratchpad>/<name>` and republish
   `<scratchpad>/<name>/index.html` to the same artifact URL.
4. When `round` moved since the last check-in: `hc gh round <name> --artifact <url>`.
5. One line in chat: the check-in line, nothing else. Do not ping the human.

`hc export --db-rows` and the ArtifactData push are phase 2 (H-504); until they exist the republish is
the update.

## 6. Resume

The same `hc study run <name> ...` command resumes from `trials.jsonl`; keys already done are skipped. A
lock whose heartbeat is older than 10 min and whose pid is gone is taken over; a live lock is refused
(exit 3). One study directory, one run.

## 7. Stop conditions

| condition | what to do |
|---|---|
| budget used (`--hours`, `--until`) | the run exits 0 by itself; review |
| review order (explore) or front (earlier / gentler) unchanged for 3 rounds | `hc study stop`; review |
| every searched parameter dead | stop; report "not testable on these days" per parameter |
| fidelity floor broken on more than 30 % of days | stop; finding issue |
| `stale_context` on a new round | the run finishes the round and stops (default `--on-stale stop`); say why |
| sentinel or null mismatch, unequal loop counts | the run stops at once with exit 2; report the reason verbatim |
| three consecutive failed trials | stop; report the error text |
| disk below 2 GB | stop |

## 8. Review

1. The validate phase on the hold-out days, when the study reserves any.
2. `hc card <name>` (or `--rank N`): cards are linted when written; a refused card is reported, not
   rewritten by hand.
3. For each card: `hc explain <name> <iso> --variant <vhash>` on the loops that differ most (the page's
   difference strip names them).
4. The summary: per card the verdict word, the changed preference with its AAPS title, the effect next
   to its floor, P and envelope status, ties named as ties, the VDF. Then the honesty statement.
5. Publish the final page; offer the pin once.

## 9. Propose

Interactive sittings only. A proposal needs a variant recommended for review in round.json and the
human's explicit go-ahead. `hc gh propose` is not built yet (H-603):
until it is, give the human the card path and the proposal title in chat and let them open the issue.
Never set `accepted`, `rejected` or `applied-on-phone`; never edit `ns_settings.json` on
`applied-on-phone`: the human writes the `profile_history` entry.

## Exit codes

| code | meaning |
|---|---|
| 0 | done, finished or stopped (STOP, budget, Ctrl-C) |
| 1 | command not available yet, a regression in `hc check`, or a GitHub call failed |
| 2 | stopped by a guard (sentinel / null mismatch, loop counts) or a text refused by the lint |
| 3 | refused: locked key, lock held by a live run, no such study, bad study.json |
