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
asked for vs base in rising and in the other loops, each next to its floor (the worst day's |base - phone|) and in
floors; the loops changed by direction (smaller / larger SMB than base, loops differing from the phone vs base);
the low-side flags; the status of the pre-hypo constraint P and of the envelope E; the cap-binding share next to
the phone's; the largest single emulated SMB next to the phone's smallest maxBolus; the rank per stratum (the day
tags of study.json), on the hold-out days and across the leave-one-day-out folds; the ties ("Not distinguishable
from candidate 3 within the noise floor"); the verdict; the honesty paragraph verbatim; the VDF text.

Every card passes lint.lint_text(text, kind='card') (the vocabulary of DESIGN.md section 3 plus the honesty
paragraph) or it is NOT written: write_cards() reports the refusal with the offending phrase and goes on with the
next card; `hc card` then exits 2. Numbers come from round.analyze() (metrics.py), never from this module.

Files: studies/<study>/cards/<rank>.md and <rank>.vdf for ranked variants; cards/<short vhash>.md for a variant
without a rank (infeasible, pending) asked for by --vhash. The honesty rule on names: the card prints "review
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
from hindcast.contracts import BASE_VHASH, HONESTY_STATEMENT                                # noqa: E402

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


def _worst_floor(analysis, key, days):
    vals = [analysis.floors[d].get(key) for d in days if d in analysis.floors and analysis.floors[d].get(key) is not None]
    return M.effective_floor(max(vals), key) if vals else M.effective_floor(None, key)


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


def card_number(report):
    """The number in the title: the rank (review-order position) or None."""
    return report.rank


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
    f_rise = _worst_floor(analysis, 'floor_rise_U', days)
    f_rest = _worst_floor(analysis, 'floor_rest_U', days)
    f_loops = _worst_floor(analysis, 'floor_loops', days)
    out = []
    if total is not None:
        lo, hi = _min(report, 'delta_vs_base_U', days), _max(report, 'delta_vs_base_U', days)
        spread = ''
        if n > 1 and lo is not None and hi is not None:
            spread = (' (the same on every day)' if round(lo, 2) == round(hi, 2)
                      else f' (mean over days; range {_signed_u(lo)} to {_signed_u(hi)})')
        out.append(f'On the {n} replayed search day{"s" if n != 1 else ""} the loop would have asked for '
                   f'{_u(abs(total))} {"more" if total > 0 else "less"} per day than base' + spread + '.')

    def part(v, floor, where):
        if v is None:
            return None
        ratio = abs(v) / floor
        tie = ' (tie: inside 2 x floor)' if M.is_tie(v, floor) else ''
        return f'- {where}: {_signed_u(v)} vs base per day; floor {_u(floor)}; {ratio:.1f} x floor{tie}'
    for v, floor, where in ((rise, f_rise, 'rising loops'), (rest, f_rest, 'the other loops')):
        line = part(v, floor, where)
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
    return out


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
    p_reasons = [r for rs in report.infeasible_reasons.values() for r in rs if 'recorded low' in r]
    if p_reasons:
        out.append('- P (insulin before a recorded low): ' + '; '.join(p_reasons) + '.')
    elif p_worst is not None:
        out.append(f'- P (insulin before a recorded low): within the limit on every day; at most {_u(p_worst)} more than base '
                   f'in the two hours before a recorded low' + (f' (limit {_u(limit_worst)})' if limit_worst is not None else '') + '.')
    else:
        out.append('- P (insulin before a recorded low): no recorded low on these days; not testable here.')
    e_reasons = [r for rs in report.infeasible_reasons.values() for r in rs if 'envelope' in r]
    env3, envd = _max(report, 'env_3h_max_U', days), _max(report, 'env_day_pct_tdd', days)
    if e_reasons:
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


def _rank_lines(report, analysis):
    word = _position_word(analysis.question)
    total = len(analysis.order)
    out = []
    if report.rank is not None:
        out.append(f'- Over the search days: {word} {report.rank} of {total}'
                   + (' (review order, not a ranking by outcome)' if analysis.question == 'explore' else '') + '.')
    else:
        out.append(f'- Over the search days: no {word} (verdict {st.verdict_label(report.verdict)}).')
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
    if analysis.folds:
        cfg = M._objective(analysis.study.objective.to_dict())['recommend']
        share = sum(1 for order in analysis.folds.values() if report.variant in order[:cfg['fold_top']]) / len(analysis.folds)
        out.append(f'- Leave-one-day-out: in the top {cfg["fold_top"]} on {share:.0%} of {len(analysis.folds)} folds.')
    if report.recommended:
        out.append('- Recommended for review: yes (feasible on all days, stable across folds, days and hold-out).')
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
        who = f'candidate {other.rank}' if other.rank is not None else f'variant {contracts.short(vh)}'
        out.append(f'Not distinguishable from {who} ({other.variant}) within the noise floor.')
    return out or ['No tie with another listed variant within the noise floor (ties are checked for the first '
                   f'{rnd.TIE_SCAN} of the order).']


def vdf_text_of(report, analysis):
    """The VDF of the variant: the study's base rows plus one row per searched parameter."""
    study = analysis.study
    return spc.vdf_text(report.params, base_lines=study.base_vdf_lines, names=list(study.space))


def card_text(report, analysis, settings=None, prefs_map=None, number=None):
    """The Markdown card of one VariantReport (not yet linted; see write_card)."""
    prefs_map = load_prefs_map() if prefs_map is None else prefs_map
    number = card_number(report) if number is None else number
    study = analysis.study
    lines = [_title(report, number), '',
             f'Study `{study.name}`, question {study.question}; verdict: **{st.verdict_label(report.verdict)}**. {report.why}', '',
             '## Settings: current -> proposed', ''] + _settings_table(report, analysis, prefs_map) + ['',
             '## Days replayed', ''] + _days_lines(analysis) + ['',
             '## What the loop would have asked for, vs base', ''] + _effect_lines(report, analysis) + ['',
             '## Constraints and safety readings', ''] + _constraint_lines(report, analysis) + ['',
             '## Rank by stratum and hold-out', ''] + _rank_lines(report, analysis) + ['',
             '## Ties', ''] + _tie_lines(report, analysis) + ['',
             '## Honesty', '', HONESTY_STATEMENT, '',
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
    """The reports to write cards for: by rank, by (short) vhash, else the first `top` of the order."""
    out = []
    for r in ranks or []:
        rep = analysis.by_rank(int(r))
        if rep is None:
            raise CardError(f'no variant has {_position_word(analysis.question)} {r} (the order has {len(analysis.order)} entries)')
        out.append(rep)
    for vh in vhashes or []:
        hits = [rep for h, rep in analysis.reports.items() if h == vh or h.startswith(vh)] if vh else []
        if len(hits) != 1:
            raise CardError(f'vhash {vh!r} matches {len(hits)} variants')
        out.append(hits[0])
    if not ranks and not vhashes:
        out = [analysis.reports[vh] for vh in analysis.order[:top]]
    seen, uniq = set(), []
    for rep in out:
        if rep.vhash not in seen and rep.vhash != BASE_VHASH:
            seen.add(rep.vhash)
            uniq.append(rep)
    return uniq


def card_paths(out_dir, report):
    label = str(report.rank) if report.rank is not None else contracts.short(report.vhash)
    return label, os.path.join(out_dir, f'{label}.md'), os.path.join(out_dir, f'{label}.vdf')


def write_cards(study_dir, ranks=None, vhashes=None, top=DEFAULT_TOP, settings=None, out_dir=None, prefs_map=None,
                log=None, analysis=None):
    """Cards for the selected variants into studies/<study>/cards/ (or `out_dir`); returns
    ([(label, md path, vdf path)], [(label, refusal text)]). A card that fails the lint is reported, not written."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    log = log or (lambda text: None)
    if analysis is None:
        analysis = analysis_of(sd, settings)
    out_dir = out_dir or os.path.join(sd.path, CARDS_DIR)
    prefs_map = load_prefs_map() if prefs_map is None else prefs_map
    written, refused = [], []
    for rep in select_reports(analysis, ranks, vhashes, top):
        label, md, vdf = card_paths(out_dir, rep)
        text = card_text(rep, analysis, settings, prefs_map)
        try:
            write_card(md, text, vdf, vdf_text_of(rep, analysis))
        except CardError as e:
            refused.append((label, str(e)))
            log(f'card {label} ({rep.variant}) not written: {e}')
            continue
        written.append((label, md, vdf))
        log(f'card {label}: {md}')
    return written, refused


# ----------------------------------------------------------------------------- CLI

def register(subparsers):
    """Add the `card` subcommand (cli.py's convention: `func(args) -> int`)."""
    p = subparsers.add_parser('card', help='linted candidate cards -> studies/<study>/cards/<rank>.md (+ .vdf)')
    p.add_argument('name', help='study name or directory')
    p.add_argument('--rank', action='append', type=int, default=None, help='the review-order position(s) to write (repeatable)')
    p.add_argument('--vhash', action='append', default=None, help='a variant by (short) vhash, also unranked ones')
    p.add_argument('--top', type=int, default=DEFAULT_TOP, help=f'without --rank/--vhash: the first N of the order (default {DEFAULT_TOP})')
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
