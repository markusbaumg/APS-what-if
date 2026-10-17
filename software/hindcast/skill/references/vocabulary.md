# Hindcast vocabulary

GENERATED from `software/hindcast/vocabulary.json` by `hc skill vocabulary`
(`hindcast/skill_install.py`); do not edit by hand. `hindcast/lint.py` applies the same file to every
card, proposal, issue body and check-in line, so wording that passes here passes there.

## Honesty statement

Verbatim on every page, card and proposal, and at the end of every sitting:

> Hindcast replays the loop's decisions under other settings over the glucose and insulin history that the phone's own settings produced. It says where and how much the loop would have asked differently; it cannot say what glucose would have done. A variant that boluses earlier is charged for its bolus and for the later ones the phone actually gave, so deltas overstate aggressive settings and understate gentle ones. The base replay disagrees with the phone in about 4 % of loops; differences smaller than that floor are ties. Nothing here changes the phone; settings are for you and your care team to review. This is one person's insulin dosing, not medical advice.

## May say

Decision-level wording: what the loop would have asked for, never a glucose outcome.

- "would have asked for"
- "would have microbolused"
- "would have set a zero temp"
- "the factor would have sat on the cap"
- "the loop would have disabled SMB in N more loops"
- "asks for more insulin in the two hours before a recorded low"
- "indistinguishable within the noise floor"
- "not testable on these days"
- "reacts earlier"
- "reacts later"
- "review order"
- "recommended for review"
- "a low followed here"
- "tie"
- "out of envelope"
- "not distinguishable from candidate 3"

## May not say

Each entry: the refused wording, why, and a sentence the lint refuses. Say what the loop would have
asked for instead.

<!-- lint:off (quoted refused wording) -->
- **better**: an outcome judgement; the replay only knows what the loop would have asked, not what followed. Refused: "This variant is better on the sport day."
- **best**: an outcome judgement; the table is in review order, not a ranking by outcome. Refused: "The best setting is dura_ISF_weight 0.5."
- **optimal**: an outcome judgement; nothing here is optimised for glucose. Refused: "autoISF_max 1.4 is optimal for weekdays."
- **safer**: a safety judgement about glucose outcomes; the replay cannot make one ('safety reading' for a number a human checks is fine). Refused: "The lower cap is safer."
- **fewer lows**: a glucose outcome; virtual glucose is never computed. Refused: "Expect fewer lows overnight."
- **fewer highs**: a glucose outcome; virtual glucose is never computed. Refused: "This gives fewer highs after breakfast."
- **better control**: a glucose outcome; the replay has no glucose response model. Refused: "The result is tighter control in the morning."
- **time in range**: a time-in-range figure is an outcome statistic that cannot follow from a decision replay. Refused: "Time in range rises by 3 %."
- **HbA1c**: an outcome statistic; never computed. Refused: "A lower HbA1c is expected."
- **hypo count**: counting lows or highs is a glucose outcome; a recorded low may only be named positionally ('a low followed here'). Refused: "The hypo count drops from 3 to 1."
- **would have prevented**: a counterfactual glucose outcome; the replay cannot say what glucose would have done. Refused: "It would have prevented the low at 09:41."
- **statements about the future**: a hindcast says nothing about the future; it re-runs recorded days. Refused: "This setting will reduce the morning rise."
- **recommended setting (without 'for review')**: settings are recommended for review by the person and their care team, never as such. Refused: "Recommended setting: autoISF_max 1.4."
- **glucose would have**: a counterfactual glucose statement; the replay only knows the loop's decisions. Refused: "Glucose would have stayed under 10 after lunch."
- **lower glucose**: a glucose outcome claim. Refused: "The variant leads to lower glucose in the afternoon."
- **improves**: a value judgement about outcomes; say what the loop would have asked instead. Refused: "dura_ISF_weight 0.5 improves the morning."
- **worse**: a value judgement about outcomes. Refused: "The weekend looks worse with this cap."
<!-- lint:on -->

## Privacy (GitHub issues and comments; the repository is public)

Preference names and values may appear. Glucose values, insulin units per day, loop counts per day,
transcript excerpts and prediction arrays may not; link to the private artifact instead.

- **U/day**: insulin per day (and a share of the total daily dose) is Nightscout data; it belongs in the private artifact, not in a public issue. Passes: "the day count and the parameter ranges are in the campaign table"
- **mmol**: a glucose value or unit; glucose data never leaves the private artifact. Passes: "autoISF_max 1.3 -> 1.5 (preference values may be named)"
- **mg/dl**: a glucose value or unit. Passes: "smb_delivery_ratio_bg_range stays at its current value"
- **BG-like number next to a time**: a glucose reading with its time stamp is a transcript excerpt (2.0-25.9 read as mmol, 40-399 as mg/dl; a number followed by trials/variants/days/% is a count and passes). Passes: "19:42 round 4, 240 trials done; the 2026-09-06T09:00:37Z loop differs, see the artifact"
- **Microbolusing**: wording of the loop's reason or consoleError text is a transcript excerpt. Passes: "the variant would have microbolused in more loops than base"
- **predBGs**: prediction arrays are Nightscout devicestatus data. Passes: "both prediction fans are in the drill-down of the artifact"
- **insulin units**: an insulin amount in units is dosing data, however small; say the direction and the floor multiple ('asked for less insulin in rising loops, 2.9 x the floor') and leave the amount to the private artifact (only a fixed tolerance under 0.1 U after 'within', such as 'SMB within 0.05 U', passes). Passes: "would have asked for less insulin than base in rising loops (2.9 x the floor) over 1 day; SMB within 0.05 U; UAM and enable_UAM may be named"
- **loop count**: loop counts describe the person's data volume; issues carry day counts, trial counts and decision shares (% of loops) only. Passes: "replayed over 7 days, 240 trials done; the 09:00 loop differs; cap binding 9.7 % of loops"
- **carbs**: carb entries are Nightscout treatment data. Passes: "carbs are shown as triangles on the artifact"
- **glucose statistic**: a share of time above or below a glucose level is Nightscout-derived data. Passes: "cap binding 9.7 % of loops (a decision share, no glucose level)"

## Verdict words

`base`, `dead` (say "not testable on these days"), `candidate`, `tie`, `infeasible` (say why: P,
envelope, maxBolus, cap, loop count), `recommended` (always written "recommended for review"),
`pending`.

## Card template (what `hc card` writes; never rewrite a card by hand)

1. Title: the changed keys with the phone value, "Candidate N: key new (phone: old)".
2. Settings table: AAPS preference title, VDF key, phone (current), candidate (proposed), unit.
3. Days replayed by role (search / hold-out) with the day tags.
4. What the loop would have asked for vs base: rising loops and the other loops, each next to its
   floor, ties named as ties; loops changed by direction; low-side flags; zero temps.
5. Constraints and safety readings: P, envelope E, cap binding, largest single emulated SMB next to
   the phone's maxBolus.
6. Rank by stratum and hold-out, leave-one-day-out share, the reason when it is not recommended for review.
7. Ties, the honesty statement, the VDF text.
8. A study with unlocked keys (tier C: profile sens, carb_ratio, current_basal; tier S: the caps)
   carries the weak-evidence caveat `hc study new` printed, next to the honesty statement: the
   replay keeps the history the phone's own profile produced and cannot judge a different one.
