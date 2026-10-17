"""
cards.py  -  candidate cards: one linted Markdown card per candidate, with the VDF (backlog H-206; DESIGN.md
sections 3, 6 page 5 and 7.3 step 8)

    hc card <study> [--rank N ...] [--vhash H ...] [--top N] [--out DIR] [--studies-dir D] [--settings F]
    python -m hindcast.cards <study> ...

    from hindcast import cards
    analysis = cards.analysis_of(study_dir, settings)                     # round.analyze() over trials.jsonl
    text = cards.card_text(report, analysis, settings=settings)           # the Markdown of one variant
    written = cards.write_cards(study_dir, ranks=[1, 2], settings=settings)   # [(label, md path, vdf path)]

What a card says (the backlog's list, in this order): the title "Candidate <rank>: <key> <value> (phone: <value>),
..."; current -> proposed values with the AAPS preference names from prefs_map.json (a key without a verified title
shows its VDF key - never a guessed name); the days replayed with their roles and tags; what the loop would have
asked for vs base in rising and in the other loops, each next to its floor (|base - phone| per scored day, shown as
the range over the days when it differs) and in floors (round.floor_multiple_of: the same number round.json's `why`
prints; quarantined days are listed, never scored); the loops changed by direction (smaller / larger SMB than base, loops differing from the phone vs base);
the low-side flags; the status of the pre-hypo constraint P and of the envelope E; the cap-binding share next to
the phone's; the largest single emulated SMB next to the phone's smallest maxBolus; the rank per stratum (the day
tags of study.json), on the hold-out days and across the leave-one-day-out folds (after the validate phase, H-308:
'hold-out: rank r of N, direction holds on k/n days, LODO range a-b' and Kendall tau); the ties ("Not distinguishable
from candidate 3 within the noise floor"); the verdict; the honesty paragraph verbatim and the end goal of
docs/hindcast/GOAL.md in the words of the page (contracts.END_GOAL) (for a study with unlocked keys, tier C / S, followed by
Study.unlock_caveat() and contracts.NO_GLUCOSE_RESPONSE read for the card); the VDF text.

Every card passes lint.lint_text(text, kind='card') (the vocabulary of DESIGN.md section 3 plus the honesty
paragraph) or it is NOT written: write_cards() reports the refusal with the offending phrase and goes on with the
next card; `hc card` then exits 2. Numbers come from round.analyze() (metrics.py), never from this module.

Numbering (H-716, card_order): the variants recommended for review first, then the candidates, then the ties, each
group in the review order (metrics.review_order_key: the floor multiple on the review component, descending, the
same order round.json, status.json and the page use); the card's own text still names the review-order position.
`hc study run` writes the cards at the end of every finished round through the same write_cards() as `hc card`.

Files: studies/<study>/cards/<n>.md and <n>.vdf (n = card number) for ranked variants; cards/<short vhash>.md for a
variant without a number (infeasible, pending) asked for by --vhash. A default write also writes cards/<short
vhash>.md for up to ENVELOPE_CARDS envelope-limited variants (never ranked; their E line lists the days beyond the
validity envelope, 'beyond what the replay can judge on: 2026-09-04 (+1.6 U in 3 h), ...'). A full rewrite (no --rank / --vhash / --out)
removes numbered cards left from an earlier numbering. The honesty rule on names: the card prints "review
order" for question explore and "rank" only for earlier / gentler; the verdict word for the contract value
'recommended' is "recommended for review".
"""
import argparse
import json
import os
import sys
from typing import Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import lint                                                                   # noqa: E402
from hindcast import metrics as M                                                           # noqa: E402
from hindcast import round as rnd                                                           # noqa: E402
from hindcast import space as spc                                                           # noqa: E402
from hindcast import study as st                                                            # noqa: E402
from hindcast.contracts import BASE_VHASH, END_GOAL, HONESTY_STATEMENT                      # noqa: E402

PREFS_MAP_PATH = os.path.join(HINDCAST_DIR, 'prefs_map.json')
CARDS_DIR = 'cards'
DEFAULT_TOP = 10
MGDL_PER_MMOL = M.MMOL_TO_MGDL


class CardError(Exception):
    """A card that must not be written (lint refusal) or cannot be built (unknown rank / vhash)."""


# ----------------------------------------------------------------------------- preference names

def load_prefs_map(path=PREFS_MAP_PATH):
    """prefs_map.json as a dict ({} when missing: every key then prints as its VDF key)."""
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def pref_entry(name, prefs_map):
    """The lookup rule of prefs_map.json: 'profile <key>' (or a bare key) -> entry or None; other groups never."""
    if ' ' in name:
        group, key = name.split(None, 1)
        if group != 'profile':
            return None
    else:
        key = name
    return (prefs_map.get('keys') or {}).get(key)


def pref_label(name, prefs_map):
    """(label, unit, verified): the AAPS preference title when prefs_map.json carries a verified one, else the bare
    VDF key; the unit text or ''."""
    entry = pref_entry(name, prefs_map) or {}
    key = name.split(None, 1)[1] if ' ' in name else name
    title = entry.get('title')
    return (title or key, entry.get('unit') or '', bool(title))


def fmt_value(v):
    if v is None:
        return 'not in ns_settings.json'
    if isinstance(v, bool):
        return 'on' if v else 'off'
    if isinstance(v, float):
        text = f'{v:.4f}'.rstrip('0').rstrip('.')
        return text if text not in ('', '-0') else '0'
    return str(v)


def _u(v, digits=2):
    return f'{v:.{digits}f} U'


def _signed_u(v, digits=2):
    return f'{v:+.{digits}f} U'


# ----------------------------------------------------------------------------- the analysis of a study

def analysis_of(study_dir, settings=None):
    """round.analyze() over the study's trials.jsonl (the same view round.json is built from)."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    sd.require()
    study = contracts.Study.load(sd.study_json, strict=False)
    return rnd.analyze(study, sd.trials(), settings)


def _floor_band(analysis, component, days):
    """(smallest, largest) effective floor of `component` over the scored `days` (metrics.floor_band)."""
    band = M.floor_band(analysis.floors, component, [d for d in days if d in analysis.floors])
    if band is None:
        fl = M.effective_floor(None, M.FLOOR_COMPONENTS[component])
        band = (fl, fl)
    return band


def _floor_text(band, unit=True):
    lo, hi = band
    if round(lo, 2) == round(hi, 2):
        return _u(hi) if unit else f'{hi:.0f}'
    return (f'{lo:.2f}-{hi:.2f} U by day' if unit else f'{lo:.0f}-{hi:.0f} by day')


def _sum(report, key, days):
    vals = [report.per_day[d].get(key) for d in days if d in report.per_day]
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(vals) if vals else None


def _max(report, key, days):
    vals = [report.per_day[d].get(key) for d in days if d in report.per_day]
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(vals) if vals else None


def _min(report, key, days):
    vals = [report.per_day[d].get(key) for d in days if d in report.per_day]
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return min(vals) if vals else None


def _position_word(question):
    return 'review order' if question == 'explore' else 'rank'


CARD_GROUPS = {'recommended': 0, 'candidate': 1, 'tie': 2}   # H-716: the card numbering; every other verdict after


def card_order(analysis):
    """The reports that get a card number, in card-number order (H-716): the variants recommended for review first,
    then the candidates, then the ties (then any other ranked verdict, e.g. dead), each group in the review order
    (analysis.order = metrics.review_order_key for explore, the ranked order for earlier / gentler). Infeasible and
    pending variants have no number."""
    ranked = [analysis.reports[vh] for vh in analysis.order if vh in analysis.reports and vh != BASE_VHASH]
    pos = {r.vhash: i for i, r in enumerate(ranked)}
    return sorted(ranked, key=lambda r: (CARD_GROUPS.get(r.verdict, len(CARD_GROUPS)), pos[r.vhash]))


ENVELOPE_CARDS = 5                       # envelope-limited variants that get a card (by short vhash) in a default write


def envelope_reports(analysis):
    """The envelope-limited variants (no card number, never ranked), the most of the question's direction on rises
    first (explore: the largest |rise|): their cards say on which days a larger step would have acted."""
    reps = [r for r in analysis.reports.values() if r.verdict == 'envelope-limited']
    sign = M.QUESTION_DIRECTION.get(analysis.question)
    def key(r):
        rise = r.mean.get('rise_vs_base_U') or 0.0
        return (-(sign * rise if sign else abs(rise)), r.variant)
    return sorted(reps, key=key)


def card_numbers(analysis):
    """{vhash: card number} (1-based position in card_order)."""
    return {r.vhash: i for i, r in enumerate(card_order(analysis), 1)}


def card_number(report, analysis=None):
    """The number in the title and the file name: the position in card_order(analysis); without an analysis the
    rank (review-order position); None for a variant without one."""
    if analysis is None:
        return report.rank
    return card_numbers(analysis).get(report.vhash)


def _title(report, number):
    parts = []
    for name, value in report.changed().items() or report.params.items():
        key = name.split(None, 1)[-1]
        parts.append(f'{key} {fmt_value(value)} (phone: {fmt_value(report.phone.get(name))})')
    head = f'Candidate {number}' if number is not None else f'Variant {contracts.short(report.vhash)}'
    return f'# {head}: {", ".join(parts) if parts else report.variant}'


def _settings_table(report, analysis, prefs_map):
    lines = ['| AAPS preference | VDF key | phone (current) | candidate (proposed) | unit |', '|---|---|---|---|---|']
    unverified = []
    names = [n for n in analysis.study.space if n in report.params] + [n for n in report.params if n not in analysis.study.space]
    for name in names:
        label, unit, verified = pref_label(name, prefs_map)
        if not verified:
            unverified.append(name.split(None, 1)[-1])
        cur, new = fmt_value(report.phone.get(name)), fmt_value(report.params[name])
        mark = '' if cur == new else ' <-'
        lines.append(f'| {label} | `{name}` | {cur} | {new}{mark} | {unit} |')
    if unverified:
        lines.append('')
        lines.append('No verified preference title in prefs_map.json for: ' + ', '.join(unverified)
                     + ' (the VDF key is shown instead of a guessed name).')
    return lines


def _days_lines(analysis):
    study = analysis.study
    out = []
    for role, label in (('search', 'search day'), ('holdout', 'hold-out day'), ('quarantined', 'quarantined day')):
        days = [d for d in study.days if d.role == role]
        if not days:
            continue
        label += 's' if len(days) != 1 else ''
        items = []
        for d in days:
            extra = ', '.join(d.tags)
            if role == 'quarantined' and d.quarantine_reason:
                extra = (extra + '; ' if extra else '') + d.quarantine_reason
            items.append(f'{d.day}' + (f' ({extra})' if extra else ''))
        out.append(f'- {len(days)} {label}: ' + ', '.join(items))
    return out


def _effect_lines(report, analysis):
    days = report.days
    n = len(days)
    if not n or not report.mean:
        return ['Not replayed on any search day yet.']
    rise = report.mean.get('rise_vs_base_U')
    rest = report.mean.get('rest_vs_base_U')
    total = report.mean.get('delta_vs_base_U')
    f_loops = _floor_band(analysis, 'changed_vs_base', days)[1]
    out = []
    if total is not None:
        lo, hi = _min(report, 'delta_vs_base_U', days), _max(report, 'delta_vs_base_U', days)
        spread = ''
        if n > 1 and lo is not None and hi is not None:
            spread = (' (the same on every day)' if round(lo, 2) == round(hi, 2)
                      else f' (mean over days; range {_signed_u(lo)} to {_signed_u(hi)})')
        out.append(f'On the {n} replayed search day{"s" if n != 1 else ""} the loop would have asked for '
                   f'{_u(abs(total))} {"more" if total > 0 else "less"} per day than base' + spread + '.')

    def part(v, comp, where):
        if v is None:
            return None
        band = _floor_band(analysis, comp, days)
        ratio = rnd.floor_multiple_of(report, comp, analysis.floors)       # the number round.json's why prints
        varies = round(band[0], 2) != round(band[1], 2)
        notes = ['mean of the per-day multiples'] if varies else []
        if M.tie_every_day(report.per_day, analysis.floors, comp, days):
            notes.append(f'tie: inside {M.TIE_FACTOR:g} x floor' + (' on every day' if varies else ''))
        return (f'- {where}: {_signed_u(v)} vs base per day; floor {_floor_text(band)}; {ratio:.1f} x floor'
                + (f' ({"; ".join(notes)})' if notes else ''))
    for v, comp, where in ((rise, 'rise_vs_base_U', 'rising loops'), (rest, 'rest_vs_base_U', 'the other loops')):
        line = part(v, comp, where)
        if line:
            out.append(line)
    more = _sum(report, 'smb_more_vs_base', days)
    less = _sum(report, 'smb_less_vs_base', days)
    if more is not None and less is not None:
        out.append(f'- Loops changed by direction: {int(less)} loop{"s" if less != 1 else ""} with a smaller SMB than base '
                   f'and {int(more)} with a larger one (over all {n} day{"s" if n != 1 else ""}).')
    changed = report.mean.get('changed_vs_base')
    if changed is not None:
        out.append(f'- Loops differing from the phone: {changed:+.0f} per day vs base (floor: {f_loops:.0f} loops the base '
                   f'replay itself changes).')
    lowside = _sum(report, 'lowside_flags', days)
    if lowside is not None:
        out.append(f'- Low-side flags: {int(lowside)} loop{"s" if lowside != 1 else ""} with a larger SMB than the phone\'s while '
                   'the emulated minGuardBG was below 3.9 mmol/l or the recorded BG below 5.0 mmol/l and falling.')
    zt = _sum(report, 'zero_temp_vs_base', days)
    if zt is not None:
        out.append(f'- Zero temps: {int(zt):+d} loop{"s" if abs(zt) != 1 else ""} with an emulated zero temp vs base.')
    line = manual_bolus_line(report, days)
    if line:
        out.append(line)
    return out


MANUAL_LIMIT = ('This is a direction, not a prediction: a bolus the loop pre-empts changes the glucose that follows, '
                'and with it the later decisions and the bolus itself.')


def manual_bolus_line(report, days):
    """The manual-bolus coverage line (docs/hindcast/GOAL.md, decision level) over the report's search days, or None
    when the rows carry no coverage keys (rows written before the metric existed)."""
    n = _sum(report, 'manual_boluses', days)
    if n is None:
        return None
    n = int(n)
    known = [d for d in days if isinstance((report.per_day.get(d) or {}).get('manual_boluses'), (int, float))]
    note = ''
    if len(known) < len(days):
        note = (f' (known on {len(known)} of {len(days)} search days: the others are rows written before the '
                f'manual-bolus metric existed)')
    if not n:
        return (f'- Manual boluses: none on the {len(known)} replayed search day{"s" if len(known) != 1 else ""}'
                + note + '.')
    units = _sum(report, 'manual_U', days) or 0.0
    pre = _sum(report, 'manual_presmb_U', days) or 0.0
    pre_b = _sum(report, 'manual_presmb_base_U', days) or 0.0
    cov = _sum(report, 'coverage_U_vs_base', days) or 0.0
    pre_n = int(_sum(report, 'boluses_preempted', days) or 0)
    return (f'- Manual boluses: on these days you gave {n} bolus{"es" if n != 1 else ""}, {_u(units)}; under this variant '
            f'the loop would have delivered {_u(pre)} of SMB in the hour before them (base: {_u(pre_b)}), '
            f'{_signed_u(cov)} vs base; {pre_n} of {n} with at least {M.PREEMPT_SHARE * 100:.0f} % of the bolus asked '
            f'for earlier than base' + note + '. ' + MANUAL_LIMIT)


def _constraint_lines(report, analysis):
    days = report.days
    cfg = M._objective(analysis.study.objective.to_dict())
    out = []
    p_worst, limit_worst = None, None
    for d in days:                                   # the limit of feasibility_day(): base's own P (0 vs itself) + floor_P
        p = (report.per_day.get(d) or {}).get('prehypo_extra_U')
        if p is None or d not in analysis.floors:
            continue
        if p_worst is None or p > p_worst:
            p_worst, limit_worst = p, M.effective_floor(analysis.floors[d].get('floor_P_U'))
    base = analysis.base
    counts = M.prehypo_counts(base.per_day, days) if base is not None else None
    head = '- ' + M.prehypo_label(counts, analysis.prehypo_hours) + ': '
    p_reasons = [r for rs in report.infeasible_reasons.values() for r in rs if 'recorded low' in r]
    if p_reasons:
        out.append(head + '; '.join(p_reasons) + '.')
    elif p_worst is not None:
        out.append(head + f'within the limit on every day; at most {_u(p_worst)} more than base '
                   f'in the two hours before a counted low' + (f' (limit {_u(limit_worst)})' if limit_worst is not None else '') + '.')
    elif counts is not None and counts['lows'] and counts['manual'] == counts['lows'] and not counts['unknown']:
        out.append(head + 'every recorded low on these days follows a manual bolus; not testable here.')
    elif counts is not None and counts['lows']:
        out.append(head + 'no counted low with a before-window on these days; not testable here.')
    else:
        out.append(head + 'no recorded low on these days; not testable here.')
    e_reasons = [r for rs in report.infeasible_reasons.values() for r in rs if 'envelope' in r]
    env3, envd = _max(report, 'env_3h_max_U', days), _max(report, 'env_day_pct_tdd', days)
    if getattr(report, 'envelope_limited', False):
        out.append(envelope_line(report, cfg))
    elif e_reasons:
        out.append('- E (validity envelope): out of envelope: ' + '; '.join(e_reasons) + '.')
    elif env3 is not None or envd is not None:
        out.append(f'- E (validity envelope): inside on every day; largest 3 h difference vs base {_u(env3 or 0.0)} '
                   f'(limit {cfg["envelope"]["max_3h_U"]:g} U), largest day {envd or 0.0:.1f} % of the phone\'s TDD '
                   f'(limit {cfg["envelope"]["max_day_pct_tdd"]:g} %).')
    cap = report.mean.get('cap_binding_pct')
    base = analysis.base
    base_cap = base.mean.get('cap_binding_pct') if base else None
    caps = {k.split()[-1]: v for k, v in report.params.items() if k.split()[-1] in ('autoISF_max', 'autoISF_min')}
    phone_caps = {k.split()[-1]: v for k, v in report.phone.items() if k.split()[-1] in ('autoISF_max', 'autoISF_min')}
    if cap is not None:
        text = f'- Cap binding: the factor sat on a cap in {cap:.1f} % of loops'
        if caps:
            text += ' (' + ', '.join(f'{k} {fmt_value(v)}' for k, v in caps.items()) + ')'
        if base_cap is not None:
            text += f'; the phone\'s own cap' + (' (' + ', '.join(f'{k} {fmt_value(v)}' for k, v in phone_caps.items()) + ')' if phone_caps else '') \
                    + f': {base_cap:.1f} %'
        if cap >= cfg['feasible']['cap_binding_max_pct']:
            text += f'. At or above {cfg["feasible"]["cap_binding_max_pct"]:g} % the setting was not really tested'
        out.append(text + '.')
    else:
        out.append('- Cap binding: unknown (no autoISF cap in the replay).')
    mx = _max(report, 'max_single_smb_U', days)
    mb = _min(report, 'maxbolus_phone_min_U', days)
    over = _sum(report, 'smb_over_maxbolus_loops', days)
    if mx is not None:
        text = f'- Largest single emulated SMB: {_u(mx)}'
        if mb is not None:
            text += f'; the phone\'s maxBolus on these days was at least {_u(mb)}'
            if over:
                text += f'; the emulated SMB was above the phone\'s maxBolus in {int(over)} loops'
        else:
            text += '; the phone echoed no maxBolus on these days'
        out.append(text + '.')
    return out


ENVELOPE_CARD_NOTE = ('E is a validity limit, not a safety rule: on those days the history the replay runs on would '
                      'have been different, so the replay cannot judge them; they show where a step this large would '
                      'have acted.')


def envelope_line(report, cfg):
    """The E line of an envelope-limited variant: 'beyond what the replay can judge on: 2026-09-04 (+1.6 U in 3 h),
    ...' with the limits and how many search days stayed inside."""
    ed = report.envelope_days or {}
    inside = len(report.days) - len(ed)
    return (f'- E (validity envelope): beyond what the replay can judge on: {M.envelope_days_text(ed)} (limits '
            f'{cfg["envelope"]["max_3h_U"]:g} U vs base in 3 h, {cfg["envelope"]["max_day_pct_tdd"]:g} % of the '
            f'phone\'s TDD); inside on the other {inside} search day{"s" if inside != 1 else ""}. ' + ENVELOPE_CARD_NOTE)


def pareto_line(report, analysis):
    """earlier / gentler: where the variant sits relative to the Pareto front of the feasible variants, with its three
    objectives and its scalar position; None for explore or an unranked variant."""
    if analysis.question == 'explore' or report.rank is None:
        return None
    rk = analysis.ranking
    front = list(rk.front)
    feas = list(rk.order)
    by_scalar = sorted(feas, key=lambda n: (-(rk.variants[n].scalar or 0.0), n))
    spos = by_scalar.index(report.variant) + 1 if report.variant in by_scalar else None
    m = report.mean
    rise, rest = m.get('rise_vs_base_U', 0.0) or 0.0, m.get('rest_vs_base_U', 0.0) or 0.0
    if analysis.question == 'earlier':
        objectives = (f'more insulin asked on rises {rise:+.2f} U/day, extra elsewhere {max(rest, 0.0):.2f} U/day, '
                      f'low-side flags {m.get("lowside_flags", 0.0) or 0.0:.1f}/day')
    else:
        objectives = (f'rises {rise:+.2f} U/day, elsewhere {rest:+.2f} U/day, zero temps '
                      f'{max(m.get("zero_temp_vs_base", 0.0) or 0.0, 0.0):+.1f}/day vs base')
    where = (f'on the Pareto front ({len(front)} of {len(feas)} feasible variants)' if report.pareto
             else f'behind the Pareto front (dominated; the front has {len(front)} of {len(feas)} feasible variants)')
    scalar = (f'; scalar {report.scalar:+.2f}, position {spos} of {len(feas)} by the scalar'
              if isinstance(report.scalar, (int, float)) and spos else '')
    return f'- Pareto position: {where}; {objectives}{scalar}.'


def weights_line(report, analysis):
    """earlier / gentler: the scalar weights (a study setting, not a finding) and how often the variant stays in the
    scalar top N under 0.5x / 1.5x of each weight (metrics.weights_sensitivity); None for explore."""
    sens = getattr(analysis, 'sensitivity', None)
    if not sens:
        return None
    w = ', '.join(f'{k} {v:g}' for k, v in sens['weights'].items())
    if analysis.question == 'earlier':
        formula = (f'rise - {sens["weights"]["rest"]:g} x max(rest, 0) - {sens["weights"]["lowside"]:g} x '
                   'lowside_flags')
    else:
        formula = (f'-rise - {sens["weights"]["rest"]:g} x |rest| - {sens["weights"]["zero_temp"]:g} x zero-temp '
                   'increase')
    settings = [v for per in sens['by_weight'].values() for v in per.values()]
    top = sens['top']
    inside = sum(1 for v in settings if report.variant in v['order'])
    same = sum(1 for v in settings if v['same_top'])
    stated = 'in' if report.variant in sens['order'] else 'not in'
    factors = ' and '.join(f'{f:g}x' for f in sens['factors'])
    return (f'- Weights (a study setting shown here, not a finding): {w}; scalar = {formula}. At these weights the '
            f'variant is {stated} the scalar top {top}; under {factors} of each weight it is in the scalar top {top} in '
            f'{inside} of {len(settings)} settings (the top {top} itself is unchanged in {same} of {len(settings)}).')


def _rank_lines(report, analysis):
    word = _position_word(analysis.question)
    total = len(analysis.order)
    out = []
    if report.rank is not None:
        out.append(f'- Over the search days: {word} {report.rank} of {total}'
                   + (' (review order, not a ranking by outcome)' if analysis.question == 'explore' else
                      ' (the Pareto front first, then the scalar)') + '.')
    else:
        out.append(f'- Over the search days: no {word} (verdict {st.verdict_label(report.verdict)}).')
    for line in (pareto_line(report, analysis), weights_line(report, analysis)):
        if line:
            out.append(line)
    if report.strata_ranks:
        out.append('')
        out.append(f'| stratum | days replayed | {word} |')
        out.append('|---|---|---|')
        for tag, rank in sorted(report.strata_ranks.items()):
            out.append(f'| {tag} | {report.strata_days.get(tag, 0)} | {rank if rank is not None else "-"} |')
        out.append('')
    if analysis.holdout_days:
        if report.holdout_rank is not None:
            out.append(f'- Hold-out days ({", ".join(analysis.holdout_days)}): {word} {report.holdout_rank} of {report.holdout_total}.')
        elif report.holdout_days:
            out.append(f'- Hold-out days: replayed but not ranked there (infeasible or tie).')
        else:
            out.append('- Hold-out days: not replayed yet.')
    else:
        out.append('- Hold-out days: none reserved in study.json.')
    val = getattr(analysis, 'validation', None)
    if val is not None:
        if report.validation:
            v = report.validation
            out.append(f'- Validate phase ({len(val.holdout_days)} hold-out days, {len(val.folds)} leave-one-day-out '
                       f'folds over {len(val.scored_days)} days; a caveat with the day count, not a statistic): '
                       f'{M.validation_line(v)}.')
            if v.get('flip_days'):
                out.append(f'- Direction ({v["component"]}) flips on: {", ".join(v["flip_days"])}.')
        else:
            out.append(f'- Validate phase: not in the top {val.top} of the search review order, not replayed on the '
                       f'hold-out days.')
        if val.tau is not None:
            out.append(f'- Kendall tau between the search and the hold-out order of the {len(val.validated)} '
                       f'validated variants: {val.tau:+.2f}.')
    elif analysis.folds:
        cfg = M.recommend_config(analysis.study.objective.to_dict())
        share = sum(1 for order in analysis.folds.values() if report.variant in order[:cfg['fold_top']]) / len(analysis.folds)
        out.append(f'- Leave-one-day-out: in the top {cfg["fold_top"]} on {M.share_pct(share, 0)} of '
                   f'{len(analysis.folds)} folds.')
    if report.recommended and report.validation:
        v = report.validation
        rule = M.recommend_rule_text(analysis.study.objective.to_dict(), validate=True)
        out.append(f'- Recommended for review: yes (not a tie with base, {rule}).')
        cav = M.validation_caveat(v)
        if cav:
            out.append(f'- {cav[0].upper()}{cav[1:]}.')
    elif report.recommended:
        rule = M.recommend_rule_text(analysis.study.objective.to_dict(), validate=False)
        out.append(f'- Recommended for review: yes ({rule}).')
    elif report.recommend_reasons:
        out.append('- Not recommended for review: ' + '; '.join(report.recommend_reasons) + '.')
    return out


def _tie_lines(report, analysis):
    out = []
    if report.tie_with_base:
        out.append('Indistinguishable from base within the noise floor on every component and every day.')
    for vh in report.tie_with:
        other = analysis.reports.get(vh)
        if other is None:
            continue
        n = card_number(other, analysis)
        who = f'candidate {n}' if n is not None else f'variant {contracts.short(vh)}'
        out.append(f'Not distinguishable from {who} ({other.variant}) within the noise floor.')
    return out or ['No tie with another listed variant within the noise floor (ties are checked for the first '
                   f'{rnd.TIE_SCAN} of the order).']


def vdf_text_of(report, analysis):
    """The VDF of the variant: the study's base rows plus one row per searched parameter."""
    study = analysis.study
    return spc.vdf_text(report.params, base_lines=study.base_vdf_lines, names=list(study.space))


def unlock_lines(study):
    """The weak-evidence paragraph of a card whose study varies keys locked by default (tier C / S, Study.unlocked):
    Study.unlock_caveat() first, then contracts.NO_GLUCOSE_RESPONSE with 'this page' read as 'this card' - the
    same two sentences report.unlock_caveat() puts next to the page's honesty paragraph; [] otherwise."""
    fn = getattr(study, 'unlock_caveat', None)
    text = fn() if callable(fn) else None
    if not text:
        return []
    text = text[0].upper() + text[1:]
    return [text + ' ' + contracts.NO_GLUCOSE_RESPONSE.replace('this page', 'this card'), '']


def card_text(report, analysis, settings=None, prefs_map=None, number=None):
    """The Markdown card of one VariantReport (not yet linted; see write_card)."""
    prefs_map = load_prefs_map() if prefs_map is None else prefs_map
    number = card_number(report, analysis) if number is None else number
    study = analysis.study
    lines = [_title(report, number), '',
             f'Study `{study.name}`, question {study.question}; verdict: **{st.verdict_label(report.verdict)}**. {report.why}', '',
             '## Settings: current -> proposed', ''] + _settings_table(report, analysis, prefs_map) + ['',
             '## Days replayed', ''] + _days_lines(analysis) + ['',
             '## What the loop would have asked for, vs base', ''] + _effect_lines(report, analysis) + ['',
             '## Constraints and safety readings', ''] + _constraint_lines(report, analysis) + ['',
             '## Rank by stratum and hold-out', ''] + _rank_lines(report, analysis) + ['',
             '## Ties', ''] + _tie_lines(report, analysis) + ['',
             '## Honesty', '', HONESTY_STATEMENT, '', END_GOAL, ''] + unlock_lines(study) + [
             '## VDF', '', '```', vdf_text_of(report, analysis).rstrip('\n'), '```', '']
    return '\n'.join(lines)


def lint_card(text):
    """Raise CardError (wrapping lint.LintError) when the card breaks the vocabulary or lacks the honesty paragraph."""
    try:
        lint.lint_text(text, 'card')
    except lint.LintError as e:
        raise CardError(str(e)) from e


def write_card(path, text, vdf_path=None, vdf_text=None):
    """Lint, then write the card (and the VDF next to it); returns the path. A refused card writes nothing."""
    lint_card(text)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(tmp, path)
    if vdf_path and vdf_text is not None:
        with open(vdf_path, 'w', encoding='utf-8') as fh:
            fh.write(vdf_text)
    return path


def select_reports(analysis, ranks=None, vhashes=None, top=DEFAULT_TOP):
    """The reports to write cards for: by card number (`ranks`), by (short) vhash, else the first `top` of
    card_order (recommended for review first, then candidates, then ties, each in the review order)."""
    out = []
    ordered = card_order(analysis)
    for r in ranks or []:
        if not 1 <= int(r) <= len(ordered):
            raise CardError(f'no variant has card number {r} (the order has {len(ordered)} entries)')
        out.append(ordered[int(r) - 1])
    for vh in vhashes or []:
        hits = [rep for h, rep in analysis.reports.items() if h == vh or h.startswith(vh)] if vh else []
        if len(hits) != 1:
            raise CardError(f'vhash {vh!r} matches {len(hits)} variants')
        out.append(hits[0])
    if not ranks and not vhashes:
        out = ordered[:top] + envelope_reports(analysis)[:ENVELOPE_CARDS]
    seen, uniq = set(), []
    for rep in out:
        if rep.vhash not in seen and rep.vhash != BASE_VHASH:
            seen.add(rep.vhash)
            uniq.append(rep)
    return uniq


def card_paths(out_dir, report, number=None):
    label = str(number) if number is not None else (str(report.rank) if report.rank is not None
                                                     else contracts.short(report.vhash))
    return label, os.path.join(out_dir, f'{label}.md'), os.path.join(out_dir, f'{label}.vdf')


def _prune_numbered(out_dir, keep):
    """Remove the numbered cards (<n>.md / <n>.vdf) of `out_dir` whose label is not in `keep`: after a full
    rewrite a card number left over from an earlier numbering would name another variant. Returns the paths."""
    removed = []
    try:
        names = os.listdir(out_dir)
    except OSError:
        return removed
    for name in sorted(names):
        stem, ext = os.path.splitext(name)
        if ext in ('.md', '.vdf') and stem.isdigit() and stem not in keep:
            path = os.path.join(out_dir, name)
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    return removed


def write_cards(study_dir, ranks=None, vhashes=None, top=DEFAULT_TOP, settings=None, out_dir=None, prefs_map=None,
                log=None, analysis=None, prune=None):
    """THE card writer of `hc card` and of `hc study run` (the orchestrator calls it at the end of every finished
    round with the round's analysis, H-716). Cards for the selected variants into studies/<study>/cards/ (or
    `out_dir`), numbered by card_order; returns ([(label, md path, vdf path)], [(label, refusal text)]). A card
    that fails the lint is reported, not written. `prune` (default: True for the default selection - no ranks,
    no vhashes - into the study's own cards/ directory) removes numbered cards left from an earlier numbering."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    log = log or (lambda text: None)
    if analysis is None:
        analysis = analysis_of(sd, settings)
    if prune is None:
        prune = not ranks and not vhashes and out_dir is None
    out_dir = out_dir or os.path.join(sd.path, CARDS_DIR)
    prefs_map = load_prefs_map() if prefs_map is None else prefs_map
    numbers = card_numbers(analysis)
    written, refused = [], []
    for rep in select_reports(analysis, ranks, vhashes, top):
        number = numbers.get(rep.vhash)
        label, md, vdf = card_paths(out_dir, rep, number)
        text = card_text(rep, analysis, settings, prefs_map, number=number)
        try:
            write_card(md, text, vdf, vdf_text_of(rep, analysis))
        except CardError as e:
            refused.append((label, str(e)))
            log(f'card {label} ({rep.variant}) not written: {e}')
            continue
        written.append((label, md, vdf))
        log(f'card {label}: {md}')
    if prune:
        _prune_numbered(out_dir, {label for label, _md, _vdf in written})
    return written, refused


# ----------------------------------------------------------------------------- CLI

def register(subparsers):
    """Add the `card` subcommand (cli.py's convention: `func(args) -> int`)."""
    p = subparsers.add_parser('card', help='linted candidate cards -> studies/<study>/cards/<n>.md (+ .vdf)')
    p.add_argument('name', help='study name or directory')
    p.add_argument('--rank', action='append', type=int, default=None,
                   help='the card number(s) to write (repeatable): recommended for review first, then candidates, then ties, each in the review order')
    p.add_argument('--vhash', action='append', default=None, help='a variant by (short) vhash, also unranked ones')
    p.add_argument('--top', type=int, default=DEFAULT_TOP, help=f'without --rank/--vhash: the first N card numbers (default {DEFAULT_TOP})')
    p.add_argument('--out', default=None, help='directory for the cards (default studies/<study>/cards/)')
    p.add_argument('--studies-dir', default=st.STUDIES_DIR)
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH)
    p.set_defaults(func=run_card)
    return p


def run_card(args):
    """`hc card <study> ...`: 0 every card written, 2 a card was refused by the lint, 3 study / selection refused."""
    try:
        sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
        settings = spc.load_settings(args.settings)
        written, refused = write_cards(sd, ranks=args.rank, vhashes=args.vhash, top=args.top, settings=settings,
                                       out_dir=args.out, log=lambda t: sys.stdout.write(t + '\n'))
    except (st.StudyError, contracts.ContractError, CardError, OSError) as e:
        sys.stderr.write(f'hc card: refused: {e}\n')
        return st.EXIT_REFUSED
    if not written and not refused:
        sys.stdout.write('no variant to write a card for (no ranked variant yet; run the study or give --vhash)\n')
    sys.stdout.write('\n' + HONESTY_STATEMENT + '\n')
    return st.EXIT_GUARD if refused else st.EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.cards', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(['card'] + list(sys.argv[1:] if argv is None else argv))
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
