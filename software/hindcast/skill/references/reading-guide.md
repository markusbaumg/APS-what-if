# Reading guide: status.json, round.json and the page

Everything here is about the loop's decisions. A number on the page is insulin the loop would have asked
for, or a count of loops whose decision would have differed; it is never a glucose outcome.

## status.json (at most 2 KB; the only file a check-in reads)

| field | reading |
|---|---|
| `phase`, `round`, `trials_done` / `trials_planned`, `trials_failed`, `trials_blocked` | progress; blocked = recorded by the dry evaluator, re-evaluated by a real run |
| `running`, `heartbeat`, `exit_code`, `stop_reason` | run state; a heartbeat older than 10 min with `running` true is a dead run |
| `floor` | base vs phone on the worst day: `floor_rise_U`, `floor_rest_U`, `floor_total_U`, `floor_smb_U`, `floor_tbr_U`, `floor_P_U`, `floor_loops`, `agreement_pct` |
| `days` | day -> role (`search`, `holdout`, quarantine state) |
| `dead_parameters` | parameters "not testable on these days" (frozen at the phone's value) |
| `top` | the first entries of the review order: `variant`, `vhash`, `verdict`, `why`, `rank`, `effect` |
| `stale_context` | the code or settings changed under the study; finish the round and stop |

## round.json (at most 2 KB per variant)

`variants` holds base first, then the review order (explore) or the ranked order, then infeasible and
pending ones. Each entry: `verdict`, `why` (already in the allowed vocabulary; quote it), `params`,
`effect` (`rise_vs_base_U`, `rest_vs_base_U`, `delta_vs_base_U`, `changed_vs_base`, ...), `flags`,
`tie_with` (vhashes it cannot be told from). `front` is the review order or the Pareto front;
`floors` are per day.

Flags worth naming: `lowside_no_pred` (the low-side check used the recorded-BG rule alone),
`tdd_from_window` (the envelope's day share used the window's total, not the phone's TDD),
`zero_temp_rate_proxy`, `tt_smb_off_unknown`, `cap_unknown`, `maxbolus_unknown` (the reading is missing, not
zero).

## The words on the page

- **Floor.** Per day and component, the distance between the base replay (the phone's own settings,
  replayed) and what the phone decided. A difference between two variants smaller than 2 x floor is a
  **tie** and is drawn grey. `agreement_pct` is the share of loops where base reproduces the phone's
  SMB, insulinReq, temp basal and ISF within tolerance.
- **Rising loops / the other loops.** `rise_vs_base_U` is the change in insulin the loop would have
  asked for in loops on a rise, `rest_vs_base_U` in all other loops, both vs base.
- **Envelope E.** The largest 3 h sum and the day share of the change vs base. Beyond the limits
  (default 1 U per 3 h, 15 % of the day's total) the assumption "inputs unchanged" no longer holds:
  the variant is shown as "out of envelope" and not ranked.
- **P, the pre-low constraint.** Extra insulin vs base asked for in the two hours before a recorded low.
  It is positional: it locates the two hours before a low that actually happened. It is a hard limit
  (`P <= P_base + floor`), never a score. Say "asks for more insulin in the two hours before a recorded
  low", never anything about the low itself.
- **Cap binding.** Share of loops where the factor sat on `autoISF_max` or `autoISF_min`. At 50 % or more
  the variant is infeasible: the setting was not really tested.
- **Largest single SMB** next to the phone's maxBolus: a reading for a human to check, always on the card.
- **Review order** (question `explore`): sorted by effect size in floors (the "x floor" column: the mean
  over the days of |effect| / that day's floor on the larger component), "review order, not a ranking by
  outcome". round.json, status.json, the cards, the validate top-N and the page all use this one order. Questions `earlier` / `gentler` show a Pareto front instead.
- **Pareto front** (question `earlier`): the Parameters view opens with a scatter of every variant as the
  mean over the search days vs base, x = insulin in rising loops (more is what the question asks for),
  y = max(rest, 0) (less extra insulin elsewhere), the low-side flags in the tooltip; the phone's
  settings, replayed, sit at the origin. Front members are filled and carry their review order. Next to
  it: the review order under the declared weights (the front first, by the scalar
  `rise - 2.0 x max(rest, 0) - 0.3 x lowside_flags` inside it, then the rest by the scalar) with the
  weights printed, and the **weights-sensitivity** panel: the top 5 under 0.5 x and 1.5 x of each weight
  (round.json `sensitivity`; an older round says it predates the record). A weight that moves the top 5
  is a study setting, not a finding: say so.
- **Would have pre-empted** (leaderboard, when the metrics carry it): how much of the manual boluses the
  loop would already have asked for as SMBs before the bolus was given, and how many boluses. Decision
  level only: a pre-empted bolus changes the glucose that followed, so the later loops still run on the
  history the manual bolus produced.
- **Observed** (day cards and Scenarios badges, with a `baseline.json` next to the studies): the share
  of CGM time between 3.9 and 10 mmol/l, below 3.9, and the manual boluses of the day as it happened on
  the phone. Always labelled observed; never compare variants by them.
- **Cannot be tested with this method**: basal rates, DIA and peak, carb ratio, autosens ratio, the
  smoother. **Not testable on these days**: a searched parameter whose every point stayed inside the
  floor on every day.
- **A low followed here**: a positional mark in the drill-down. Never count these marks, never compare
  variants by them.

## Unlocked studies: weak evidence

When study.json lists `unlocked` keys (tier C: profile `sens`, `carb_ratio`, `current_basal`; tier S: the
caps), every reading above still holds, but the study is weak evidence: the replay keeps the glucose and
insulin history the phone's own profile produced, so a scaled ISF, CR or basal is judged against inputs
that profile shaped. Read the numbers as "how often and by how much the loop would have decided
differently", never as a judgement of the profile. Each card and summary of such a study quotes the
weak-evidence caveat `hc study new` printed, next to the honesty statement (where `hc card` does not
print it yet, put it directly above the cards in the summary; never edit a card by hand); the human
unlocked these keys and reviews every proposal by hand.

## Decision rules at a check-in

| what status.json / round.json shows | what to do |
|---|---|
| a parameter in `dead_parameters` | leave it frozen; report "not testable on these days"; do not widen its range without asking |
| cap binding high, or flags show the factor on the cap | propose varying `autoISF_max` (or `autoISF_min`) before any weight |
| verdict `candidate` | confirm on the remaining strata and the hold-out days before any card |
| `lowside_flags` above 0, or P near its limit | `hc explain <name> <iso> --variant <vhash>` on those loops; name them in the summary |
| verdict `tie` | say "tie" or "indistinguishable within the noise floor"; never pick one of the tied variants |
| verdict `infeasible` | quote the reason (P, envelope, maxBolus, cap, loop count); never rank it |
| verdict `recommended` | write it "recommended for review"; it is a card, and a proposal only on the human's word |
| `agreement_pct` below 94 on a non-quarantined day | stop; finding issue (the study measures the emulator) |
| `stale_context` not empty | finish the round, stop, say so |
