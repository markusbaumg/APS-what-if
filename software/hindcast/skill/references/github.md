# GitHub: campaign issue, round comments, proposals

Repository `markusbaumg/APS-what-if`, issues enabled, **public**. All GitHub traffic goes through
`hc gh`; never call `gh issue create` or `gh issue comment` directly for study content. Every body passes
`hindcast.lint` kind `issue` (vocabulary plus privacy) before it is posted; a refused body is not posted.

## Privacy rule

May appear: preference names and values (what `ns_settings.json` already publishes), study ids, day
counts, trial counts, phase and status words, verdict words, links to the private artifact.

May not appear: glucose values or units, insulin units per day, loop counts per day, transcript excerpts
(reason or consoleError wording), prediction arrays, carb amounts, glucose statistics. Link to the
artifact instead.

## Campaign issue (one per study)

```
hc gh campaign <name> --artifact <artifact url> [--dry-run]
```

Title `Campaign: <study>`, labels `hindcast` + `campaign`. The body is the protocol: searched parameters
with their AAPS titles and ranges, constraints, day count by role, question, phases, budget, the code and
settings stamps, the artifact link and the honesty statement. Re-running it rewrites the body only when
it changed. Closing it at the end of the study is not built yet (H-603): there is no `hc gh` command
for it, so the agent hands the human the final summary (decision-level words, links, no data) in chat
and the human closes the issue.

## One comment per round

```
hc gh round <name> --artifact <artifact url> [--dry-run]
```

At most one comment per round: a hidden marker `<!-- hindcast-round: <study> round <n> -->` makes a
second call for the same round post nothing. Call it at a check-in only when `round` in `status.json`
moved. Never post between rounds, never post the check-in line as a comment.

## Labels

`hindcast`, `epic`, `mvp`, `algorithm-fidelity`, `blocked-core`, `campaign`, `proposal`, `finding`,
`accepted`, `rejected`, `applied-on-phone` (created by `hc gh labels`).

`hc gh campaign` sets `hindcast` + `campaign` on the issue it opens; that is the only issue the agent
opens today. Proposal and finding issues are opened by the human until `hc gh propose` and a finding
command exist (not built yet, H-603). The agent never sets `accepted`, `rejected` or `applied-on-phone`:
those are the human's answer, read back at the next sitting.

## Proposal issues

One per candidate recommended for review, and only after the human says so in an interactive sitting
(never from a schedule). Title `Proposal: <key> <phone value> -> <proposed value>`, body: study id,
artifact link, the card's settings table, the honesty statement, and a checklist only the human ticks:

- [ ] reviewed
- [ ] discussed with the care team
- [ ] applied on the phone on YYYY-MM-DD
- [ ] added to `profile_history` in `ns_settings.json`

`hc gh propose` and the label read-back `hc gh sync` are not built yet (H-603). Until they are, hand the
human the card path and the proposal title in chat; they open the issue. Only the human closes a
proposal. On `applied-on-phone` the human edits `ns_settings.json` (`profile_history`); the agent only
reminds.

## Finding issues

For things worth knowing that are not proposals: the fidelity floor broken on a day, a cap that hides
every weight, a dead parameter, a hand-off to the core maintainers (`blocked-core`,
`algorithm-fidelity`). Opening a finding issue through `hc gh` is not built yet (H-603): write the issue
text, check it with `hc lint --kind issue FILE`, and hand it to the human, who opens the issue. It
follows the same privacy rule.

## Unlocked studies (tier C / S)

A campaign of a study with unlocked keys (profile `sens`, `carb_ratio`, `current_basal`, or the caps)
carries the weak-evidence caveat `hc study new` printed; so does every proposal text drawn from it.
