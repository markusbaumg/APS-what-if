# Hindcast end goal

Stated by Markus on 2026-10-17 (morning after the first campaign):

> The loop is more aggressive and quicker so we have a fully closed loop without any manual bolusing and
> with time in range over 90 %.

This is the end goal every Hindcast study, proposal and page serves from now on. It replaces the
`explore` default of the first campaign as the direction; the honesty rule of DESIGN.md section 3 is
unchanged.

## What the goal means in measurable terms

| Target | Observable where | Hindcast can |
|---|---|---|
| Time in range 3.9–10 mmol/l above 90 % | only on the phone / Nightscout CGM, after a setting ran for real | report the observed TIR per day as the baseline and track it as settings change; it can **never** compute a counterfactual TIR |
| No manual bolusing | Nightscout treatments (boluses the user entered vs SMBs) | report the baseline; for each manual bolus, say how much of it the loop under a variant would already have delivered as SMBs before the user acted (decision-level coverage) |
| More aggressive and quicker on rises | the replay | rank variants by how much earlier and how much more insulin the loop would have asked for in rising loops, under the pre-hypo constraint (no extra insulin before a recorded low) and the validity envelope |

## How the method reaches it: staged steps

A replay only holds inside the validity envelope E (at most 1 U difference from the phone in any 3 h
window and at most 15 % of the day's TDD): beyond it the glucose history would have been different and
the counterfactual is meaningless. The first campaign showed that most single moves toward "more" leave
that envelope on at least one day. So the way to the goal is a loop of steps, each small enough to be
judged:

1. **Search** (`question: earlier`): the Pareto front of more insulin on rises, least extra insulin
   elsewhere, fewest low-side flags, among the variants that stay inside the envelope on every day.
   Validated on hold-out days exactly as before (rule 5).
2. **Propose one step** from the front, as a card: current → proposed, what it would have changed, where.
3. **Markus applies it** on the phone when he decides to; the time goes into `profile_history`.
4. **Observe** a week: the observed TIR, lows, manual boluses per day are the real feedback.
5. **Grow**: freeze the new days, replay them with the previous setting as the variant (the only
   counterfactual with a glucose response that was produced by the new setting), re-run the search from
   the new base, repeat.

Stop rules: any step that raises time below 3.9 mmol/l, or the number of low-side flags, is rolled back
and reported; no step is proposed that fails the pre-hypo constraint on any day.

## Baseline (observed, from Nightscout; filled by `hc baseline`)

See `docs/hindcast/BASELINE.md` once it is generated: TIR, time below range, manual boluses and units per
day, carb entries per day, SMB units per day, for the frozen days.

## Non-goals

Hindcast does not change the phone, does not post anywhere by itself, and never claims a glucose outcome
for a setting that has not run on the phone. "More aggressive" never means lifting the safety section
(max_iob, max_basal, maxSMB minutes are searched only as caps that bind, and raised only by Markus).
