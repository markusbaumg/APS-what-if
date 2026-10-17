"""
round.py  -  the verdicts of a Hindcast round: round.json and status.json producers (backlog H-502; DESIGN.md
sections 4.3, 5.3 and 7.2)

    from hindcast import round as rnd
    analysis = rnd.analyze(study, rows, settings)           # the rich, in-memory view (cards.py reads this)
    round_doc = rnd.build_round(study_dir, round_no=3, phase='lhs', settings=settings)      # contracts.Round
    rnd.write_round(study_dir, round_doc)                   # studies/<study>/round.json (compact, atomic rename)
    status = rnd.update_status(study_dir)                   # studies/<study>/status.json (<= 2 KB, via study.build_status)
    round_doc, status = rnd.produce(study_dir, round_no=3, phase='lhs', settings=settings)  # both, in that order

    python -m hindcast.round <study> [--round N] [--phase P] [--studies-dir D] [--settings F] [--print]

What it does
    trials.jsonl rows (contracts.TrialRow; the latest row per key, `done` only) are turned into the trial dicts that
    metrics.rank_variants() reads: the base replay (control 'base' or the empty vhash) is relabelled
    metrics.BASE_VARIANT, the sentinel and null controls are left out (they are checks against state leaks, not
    variants). The ranking of DESIGN.md 5.3 runs on the study's search days; a second ranking on the hold-out days
    gives the hold-out rank; one ranking per day tag (the strata the user typed into study.json) gives the
    per-stratum ranks.

Validate round (H-308). When study.json has a `validate` phase and its validated variants have hold-out rows,
metrics.validate_variants() decides recommended for review by DESIGN.md 5.3 rule 5 with the objective's
`recommend` config (defaults in brackets): one of the top N (phase.top) of the search review order that is not a tie
with base, feasible on every search AND hold-out day, in the top fold_top (3) on at least fold_share (80 %) of the
leave-one-day-out folds over search + hold-out days, the same sign of the primary effect on at least sign_share
(80 %) of the hold-out days, and within the top holdout_top (5) on the hold-out days. Every other validated
variant keeps its candidate / tie verdict with the failed conditions appended to `why` ('direction holds on 8 of
13 hold-out days (needs 80 %)', 'top-3 on 40 % of the 19 leave-one-day-out folds (needs 80 %)', ...). Every
surface states the rule through metrics.recommend_rule_text() from the configured numbers. A variant
recommended for review carries a caveat in `why`: on how many of the hold-out days on which its direction holds the
effect stayed inside 2 x that day's floor. Candidate.validation carries the per-variant numbers
(metrics.VALIDATION_KEYS), Round.validation the study-level ones (top, hold-out days, folds, Kendall tau, how many
are recommended for review). Without a validate phase (or before its rows exist) the older metrics.recommend()
rule (folds over the search days, hold-out top 5) still applies; without hold-out rows it never recommends.

Floors. Every floor and every 'x floor' multiple is computed over the scored days only (search days, hold-out
days for the hold-out ranking): quarantined days are listed, not scored (DESIGN.md 5.4 phase 0), so they neither
widen a floor nor enter a stratum ranking. The multiple a `why` prints for a component is
metrics.floor_multiple() (the mean over days of |effect| / that day's floor), the same function the card calls.
round.json `floors` (and the review order's floors) cover the scored SEARCH days only, one entry per day; status.json
`floor` (study.aggregate_floor) is the worst day over the scored search AND hold-out days. Both leave quarantined
days out; they differ on purpose, and the page labels which day set each number covers.

Verdict per variant (contracts.VERDICTS; the first rule that applies wins)
    base          the base replay itself (the floor)
    pending       some search day has no row yet and none failed: the variant is still being evaluated
    envelope-limited  every hard check (P, maxBolus, cap >= 50 %, loop count) passes on every search day, but the
                  validity envelope E (DEFAULT_OBJECTIVE envelope: 1 U vs base in 3 h, 15 % of the phone's TDD) is
                  left on some - not all - of them. E is a validity envelope, not a safety rule: on those days the
                  replay cannot judge, and for the end goal they are the information (where a larger step would have
                  acted). Never ranked or recommended; round.json lists the days (Candidate.envelope: the signed sum
                  of the largest 3 h window per day, the largest of them, how many days stayed inside), the page and
                  the card ('beyond what the replay can judge on: 2026-09-04 (+1.6 U in 3 h), ...') show them
    infeasible    metrics.feasibility_day() names a hard reason on some day (P, maxBolus, cap >= 50 %, loop count), the
                  envelope E is left on EVERY search day, or the replay failed on a day (the error text is the reason)
    tie           indistinguishable from base within TIE_FACTOR x floor on every component and every day (checked
                  before the recommended verdict: a tie is never recommended for review, DESIGN.md 5.3 rule 4);
                  question earlier / gentler also: a variant that is no step (rise_vs_base_U not beyond the mean
                  per-day floor_rise_U in the question's direction: no more insulin on rises for earlier, no less for
                  gentler) - never a candidate, never on the front, never validated (no_step_why)
    recommended   validate round: metrics.validate_variants() (see above); else metrics.recommend(): feasible on all
                  days, top-3 on >= 80 % of the leave-one-day-out folds, same sign of the primary effect on >= 80 %
                  of days, within the top 5 on the hold-out days
    dead          the variant moves exactly one searched parameter away from the phone's value and that parameter is
                  dead: every one-parameter point of it (at least DEAD_MIN_POINTS) stays inside DEAD_FLOOR_RATIO x
                  floor on every search day ("not testable on these days", H-304's rule, reported in dead_parameters)
    candidate     everything else; `why` names the component, the direction and the size in floors

`why` strings use the vocabulary of DESIGN.md section 3 (every one passes lint.lint_findings(..., 'line'); the
word is 'recommended for review', never 'recommended' alone). round.json keeps the contract words.

round.json (contracts.Round, <= 2 KB per variant): `variants` = one Candidate per variant (base first, then the
review order / ranked order, then envelope-limited, infeasible and pending), `front` = the review order (explore) or
the Pareto front among the steps (earlier / gentler) as vhashes, `dead_parameters`, `floors` per day (floors_from_base, rounded), `notes`.
Candidate.effect carries the EFFECT_KEYS numbers (rounded to 3 dp; base carries the manual-bolus keys only - its
coverage is 0 by construction -, every variant `manual_days`: how many search days carry the manual-bolus keys); Candidate.flags carries the metric flags of
the trial plus the ranks that the contract has no field for, as 'rank:holdout=<n>' and 'rank:<tag>=<n>';
Candidate.tie_with lists up to TIE_WITH_MAX vhashes the variant cannot be told from (computed for the first
TIE_SCAN variants of the order against every other). status.json is then rebuilt by study.build_status(), which
reads round.json for its `top` list, so a round is always written before the status.

Nothing here replays anything or changes the phone; the inputs are trials.jsonl and study.json.
"""
import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import metrics as M                                                           # noqa: E402
from hindcast import space as spc                                                           # noqa: E402
from hindcast import study as st                                                            # noqa: E402
from hindcast.contracts import BASE_VHASH, Candidate, DeadParameter, Round, Study, TrialRow  # noqa: E402

DEAD_FLOOR_RATIO = 1.5                   # H-304: a parameter whose every one-parameter point stays inside 1.5 x floor is dead
DEAD_MIN_POINTS = 2                      # fewer one-parameter points than this: the parameter was not screened, not dead
TIE_SCAN = 50                            # ties between variants are computed for the first TIE_SCAN of the order
TIE_WITH_MAX = 5                         # vhashes listed per variant
WHY_MAX = 300                            # characters of a Candidate.why
EFFECT_KEYS = ('rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U', 'changed_vs_base', 'smb_more_vs_base',
               'smb_less_vs_base', 'lowside_flags', 'prehypo_extra_U', 'env_3h_max_U', 'env_day_pct_tdd',
               'cap_binding_pct', 'max_single_smb_U', 'zero_temp_vs_base', 'floor_multiple', 'review_score',
               'manual_boluses', 'manual_U', 'coverage_U_vs_base', 'boluses_preempted',
               'manual_days')                    # status.json keeps the first 4
MANUAL_EFFECT_KEYS = ('manual_boluses', 'manual_U', 'coverage_U_vs_base', 'boluses_preempted')
ENVELOPE_DAYS_MAX = M.ENVELOPE_DAYS_MAX  # days listed per envelope-limited variant in round.json (size limit)
FLOOR_KEYS = ('floor_rise_U', 'floor_rest_U', 'floor_total_U', 'floor_smb_U', 'floor_tbr_U', 'floor_P_U',
              'floor_loops', 'agreement_pct', 'loops')
HOLDOUT = 'holdout'


def _r3(v):
    return None if v is None else round(float(v), 3)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ----------------------------------------------------------------------------- trial rows -> ranking input

def is_base_row(row):
    """The base replay: control 'base' or the vhash of no parameters."""
    return row.control == 'base' or row.vhash == BASE_VHASH or not row.params


def latest_done_rows(rows):
    """TrialRow list -> the latest row per key, `done` rows only, controls other than base dropped."""
    latest = st.StudyDir.latest_by_key(rows)
    out = []
    for r in latest.values():
        if r.status != 'done' or not r.metrics:
            continue
        if r.control in ('sentinel', 'null'):
            continue
        out.append(r)
    return out


def trial_dicts(rows):
    """TrialRow list -> the dicts metrics.rank_variants() reads: variant (base relabelled), day, metrics, params,
    vhash, status. Only `done` rows (latest per key); sentinel / null controls are not variants."""
    out = []
    for r in latest_done_rows(rows):
        base = is_base_row(r)
        out.append({'variant': M.BASE_VARIANT if base else r.variant, 'vhash': BASE_VHASH if base else r.vhash,
                    'day': r.day, 'metrics': r.metrics, 'params': {} if base else dict(r.params), 'status': 'done'})
    return out


def failed_by_variant(rows):
    """{vhash: {day: error}} of the latest failed rows (no done row for that key)."""
    latest = st.StudyDir.latest_by_key(rows)
    done = {(r.vhash, r.day) for r in latest.values() if r.status == 'done'}
    out: Dict[str, Dict[str, str]] = {}
    for r in latest.values():
        if r.status == 'failed' and not is_base_row(r) and (r.vhash, r.day) not in done:
            out.setdefault(r.vhash, {})[r.day] = (r.error or 'failed')[:160]
    return out


# ----------------------------------------------------------------------------- the analysis

@dataclass
class VariantReport:
    """One variant as cards.py and the page need it: the Candidate fields plus everything the slim round.json
    does not carry (per-day metrics, phone values, strata ranks, the recommend reasons)."""
    vhash: str
    variant: str
    params: Dict[str, Any]
    verdict: str
    why: str
    rank: Optional[int] = None
    days: List[str] = field(default_factory=list)                 # search days with a done row
    holdout_days: List[str] = field(default_factory=list)         # hold-out days with a done row
    feasible: Optional[bool] = None
    infeasible_reasons: Dict[str, List[str]] = field(default_factory=dict)
    failed: Dict[str, str] = field(default_factory=dict)
    tie_with_base: bool = False
    tie_days: Dict[str, bool] = field(default_factory=dict)
    tie_with: List[str] = field(default_factory=list)
    mean: Dict[str, float] = field(default_factory=dict)          # aggregate over the search days
    sign_frac: Dict[str, float] = field(default_factory=dict)
    per_day: Dict[str, dict] = field(default_factory=dict)        # day -> metrics (search and hold-out)
    review_score: float = 0.0
    review_component: str = M.REVIEW_COMPONENTS[0]
    floor_multiples: Dict[str, Optional[float]] = field(default_factory=dict)   # metrics.floor_multiple per component
    pareto: bool = False
    scalar: Optional[float] = None
    strata_ranks: Dict[str, Optional[int]] = field(default_factory=dict)      # tag -> rank (None: not ranked there)
    strata_days: Dict[str, int] = field(default_factory=dict)
    holdout_rank: Optional[int] = None
    holdout_total: int = 0
    recommended: bool = False
    recommend_reasons: List[str] = field(default_factory=list)
    validation: Optional[Dict[str, Any]] = None                   # validate round: metrics.VALIDATION_KEYS
    dead_keys: List[str] = field(default_factory=list)            # the dead parameter(s) a 'dead' variant moves
    phone: Dict[str, Any] = field(default_factory=dict)           # searched key -> the phone's value
    flags: List[str] = field(default_factory=list)
    envelope_limited: bool = False                                # metrics.VariantSummary.envelope_limited
    envelope_days: Dict[str, dict] = field(default_factory=dict)  # search day beyond E -> metrics.envelope_day_record
    step: Optional[bool] = None                                   # earlier / gentler: a step of the question
    step_floor_U: Optional[float] = None                          # the floor on rises the step had to clear

    @property
    def manual_days(self):
        """How many of the search days carry the manual-bolus keys (rows written before the metric existed do not)."""
        return len([d for d in self.days if _num((self.per_day.get(d) or {}).get('manual_boluses'))])

    @property
    def effect(self):
        out = {}
        for k in EFFECT_KEYS:
            v = (self.review_score if k == 'review_score' else
                 (floor_multiple_of(self) if self.floor_multiples else None) if k == 'floor_multiple' else
                 self.manual_days if k == 'manual_days' else self.mean.get(k))
            if k == 'review_score' and self.vhash == BASE_VHASH:
                continue                                          # base: the manual keys only (it is the floor)
            if _num(v):
                out[k] = _r3(v)
        return out

    @property
    def envelope(self):
        """round.json's Candidate.envelope of an envelope-limited variant (None otherwise): the days beyond E (largest
        3 h window first, at most ENVELOPE_DAYS_MAX), their count and the largest 3 h window vs base."""
        if not self.envelope_limited:
            return None
        return M.envelope_summary(self.envelope_days, len(self.days), ENVELOPE_DAYS_MAX)

    def changed(self):
        """The parameters the variant moves away from the phone's value (name -> value)."""
        return {n: v for n, v in self.params.items() if not _same(v, self.phone.get(n))}


@dataclass
class Analysis:
    study: Study
    question: str
    days: List[str]                                   # search days
    holdout_days: List[str]
    ranking: M.Ranking
    holdout_ranking: Optional[M.Ranking]
    folds: Dict[str, List[str]]
    reports: Dict[str, VariantReport]                 # vhash -> report (base included under BASE_VHASH)
    order: List[str]                                  # vhashes, review order / ranked order (feasible only)
    front: List[str]                                  # vhashes: the order (explore) or the Pareto front
    dead: List[DeadParameter]
    floors: Dict[str, dict]                           # day -> floors_from_base()
    strata: Dict[str, List[str]]                      # tag -> days
    trials: int
    warnings: List[str] = field(default_factory=list)
    phone: Dict[str, Any] = field(default_factory=dict)
    validation: Optional[M.Validation] = None         # the validate round (None: no validate phase / no hold-out rows)
    sensitivity: Optional[dict] = None                # metrics.weights_sensitivity (variant names; None for explore)

    @property
    def base(self):
        return self.reports.get(BASE_VHASH)

    def ordered(self, include_base=False):
        """Reports in the order round.json lists them: base, order, envelope-limited, infeasible, pending."""
        rest = [vh for vh in self.reports if vh != BASE_VHASH and vh not in self.order]
        group = {'envelope-limited': 0, 'infeasible': 1}
        rest.sort(key=lambda vh: (group.get(self.reports[vh].verdict, 2), self.reports[vh].variant))
        out = ([BASE_VHASH] if include_base and BASE_VHASH in self.reports else []) + list(self.order) + rest
        return [self.reports[vh] for vh in out]

    def by_rank(self, rank):
        for r in self.reports.values():
            if r.rank == rank and r.vhash != BASE_VHASH:
                return r
        return None


def _same(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if _num(a) and _num(b):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def phone_point(study, settings):
    """searched key -> the phone's current value: study.space[].phone when stored, else space.phone_value()."""
    out = {}
    for name, p in study.space.items():
        v = getattr(p, 'phone', None)
        if v is None and settings:
            v = spc.phone_value(name, settings)
        out[name] = v
    return out


def one_parameter_key(params, phone):
    """The single searched key `params` moves away from the phone's value, or None (zero or several)."""
    moved = [n for n, v in params.items() if not _same(v, phone.get(n))]
    return moved[0] if len(moved) == 1 else None


def dead_parameters(study, ranking, dicts, phone):
    """H-304's rule over the one-parameter variants: a searched key is dead when it has >= DEAD_MIN_POINTS
    one-parameter points and every one of them stays inside DEAD_FLOOR_RATIO x floor on every search day with a
    base. Returns ([DeadParameter], {key: max ratio}, {key: [vhash of its points]})."""
    points: Dict[str, List[str]] = {}
    by_variant: Dict[str, Dict[str, dict]] = {}
    for d in dicts:
        if d['variant'] == M.BASE_VARIANT:
            continue
        by_variant.setdefault(d['vhash'], {})[d['day']] = d['metrics']
        k = one_parameter_key(d['params'], phone)
        if k is not None and d['vhash'] not in points.setdefault(k, []):
            points[k].append(d['vhash'])
    dead, ratios, members = [], {}, {}
    for key in study.space:
        vhs = points.get(key, [])
        if len(vhs) < DEAD_MIN_POINTS:
            continue
        worst = 0.0
        days_seen = set()
        for vh in vhs:
            for day, m in by_variant[vh].items():
                if day not in ranking.floors:
                    continue
                days_seen.add(day)
                r, _ = M.effect_over_floor(m, ranking.floors[day])
                worst = max(worst, r)
        if not days_seen:
            continue
        ratios[key] = worst
        members[key] = vhs
        if worst < DEAD_FLOOR_RATIO:
            dead.append(DeadParameter(key=key, floor_ratio=round(worst, 2),
                                      why=f'not testable on these days: every one of its {len(vhs)} points stays inside '
                                          f'{DEAD_FLOOR_RATIO:g} x floor on all {len(days_seen)} search days'))
    return dead, ratios, members


def _rank_in(order_names, name):
    try:
        return order_names.index(name) + 1
    except ValueError:
        return None


def _component_words(component):
    return 'rising loops' if component == 'rise_vs_base_U' else 'the other loops'


def floor_multiple_of(report, component=None, floors=None):
    """The one 'x floor' multiple of one component of a report, for round.json's `why` and the card alike:
    metrics.floor_multiple() as rank_variants stored it over the scored search days; when nothing was stored and
    `floors` (day -> floors_from_base) is given, metrics.floor_multiple() over the report's search days; else 0
    (never the review score: H-715, one number and one order everywhere)."""
    comp = component or report.review_component
    fm = (report.floor_multiples or {}).get(comp)
    if fm is None and floors is not None:
        fm = M.floor_multiple(report.per_day, floors, comp, report.days)
    return 0.0 if fm is None else fm


def earlier_why(report):
    """The candidate `why` of question earlier (GOAL.md): 'more insulin asked on rises +0.80 U/day (4.0 x floor);
    extra elsewhere 0.10 U/day; low-side flags 0.3/day; on the Pareto front, scalar +0.53, over 3 days'. Only a
    step reaches it (rise above the floor; no_step_why() otherwise), so the amount is never negative. Decision
    level: what the loop would have asked, never a glucose outcome."""
    m = report.mean
    rise = m.get('rise_vs_base_U', 0.0) or 0.0
    rest = m.get('rest_vs_base_U', 0.0) or 0.0
    low = m.get('lowside_flags', 0.0) or 0.0
    n = len(report.days)
    pos = 'on the Pareto front' if report.pareto else 'behind the Pareto front'
    scalar = f', scalar {report.scalar:+.2f}' if _num(report.scalar) else ''
    return (f'more insulin asked on rises {rise:+.2f} U/day '
            f'({floor_multiple_of(report, "rise_vs_base_U"):.1f} x floor); '
            f'extra elsewhere {max(rest, 0.0):.2f} U/day; low-side flags {low:.1f}/day; {pos}{scalar}, over {n} '
            f'day{"s" if n != 1 else ""}')


def gentler_why(report):
    """The candidate `why` of question gentler, the mirror of earlier_why: 'less insulin asked on rises 0.80 U/day
    (4.0 x floor); elsewhere -0.10 U/day; zero temps +0.3/day vs base; on the Pareto front, scalar -0.83, over 3
    days'."""
    m = report.mean
    rise = m.get('rise_vs_base_U', 0.0) or 0.0
    rest = m.get('rest_vs_base_U', 0.0) or 0.0
    zt = max(m.get('zero_temp_vs_base', 0.0) or 0.0, 0.0)
    n = len(report.days)
    pos = 'on the Pareto front' if report.pareto else 'behind the Pareto front'
    scalar = f', scalar {report.scalar:+.2f}' if _num(report.scalar) else ''
    return (f'less insulin asked on rises {max(-rise, 0.0):.2f} U/day '
            f'({floor_multiple_of(report, "rise_vs_base_U"):.1f} x floor); elsewhere {rest:+.2f} U/day; '
            f'zero temps {zt:+.1f}/day vs base; {pos}{scalar}, over {n} day{"s" if n != 1 else ""}')


def no_step_why(report, question):
    """The tie `why` of a variant that is not a step of question earlier / gentler: it asks for no more (earlier) or
    no less (gentler) insulin on rises than the floor lets one see, or it goes the other way."""
    rise = report.mean.get('rise_vs_base_U', 0.0) or 0.0
    fm = floor_multiple_of(report, 'rise_vs_base_U')
    floor = report.step_floor_U if _num(report.step_floor_U) else M.effective_floor(None)
    word, sign = ('earlier', 1) if question == 'earlier' else ('gentler', -1)
    more = 'more' if sign > 0 else 'less'
    n = len(report.days)
    over = f'over {n} day{"s" if n != 1 else ""}'
    if sign * rise > 0:
        return (f'not an {word} step: on rises it asks for {abs(rise):.2f} U/day {more} than base, inside the floor of '
                f'{floor:.2f} U/day ({fm:.1f} x floor), {over}; a tie for this question')
    other = 'less' if sign > 0 else 'more'
    if abs(rise) <= floor:
        return (f'not an {word} step: asks for no {more} insulin on rises than base ({abs(rise):.2f} U/day {other}, '
                f'inside the floor of {floor:.2f} U/day), {over}; a tie for this question')
    return (f'not an {word} step: on rises it asks for {abs(rise):.2f} U/day {other} than base ({fm:.1f} x floor), '
            f'the other direction, {over}; a tie for this question')


def envelope_why(report, question='explore'):
    """The `why` of an envelope-limited variant (metrics.envelope_limited_text over its search days); question
    earlier / gentler: a variant that goes the other way on rises says so (it is no step of the question either)."""
    text = M.envelope_limited_text(report.envelope_days, limit=4)
    if report.step is False and question in M.QUESTION_DIRECTION:
        rise = report.mean.get('rise_vs_base_U', 0.0) or 0.0
        text += (f'; on rises it asks for {abs(rise):.2f} U/day {"more" if rise > 0 else "less"} than base: no '
                 f'{question} step either')
    return text


def candidate_why(report, question='explore'):
    """The candidate verdict's `why`: direction, size and floor multiple (floor_multiple_of) of the primary
    component, days; question earlier: earlier_why(); gentler: gentler_why()."""
    if question == 'earlier':
        return earlier_why(report)
    if question == 'gentler':
        return gentler_why(report)
    comp = report.review_component
    v = report.mean.get(comp, 0.0) or 0.0
    other = 'rest_vs_base_U' if comp == 'rise_vs_base_U' else 'rise_vs_base_U'
    ov = report.mean.get(other)
    n = len(report.days)
    text = (f'would have asked for {abs(v):.2f} U {"more" if v > 0 else "less"} than base per day in '
            f'{_component_words(comp)} ({floor_multiple_of(report, comp):.1f} x floor) over {n} day{"s" if n != 1 else ""}')
    if _num(ov):
        text += f'; {_component_words(other)} {ov:+.2f} U'
    return text


def _infeasible_why(report):
    parts = []
    for day in sorted(set(report.infeasible_reasons) | set(report.failed)):
        if day in report.failed:
            parts.append(f'{day}: the replay failed ({report.failed[day]})')
        else:
            parts.append(f'{day}: {"; ".join(report.infeasible_reasons[day])}')
    return 'infeasible: ' + ' | '.join(parts)


def verdict_of(report, dead_keys, search_days, objective=None, question='explore'):
    """(verdict, why) by the rule of the module docstring; `objective` (the study's) gives the rule-5 numbers of a
    'recommended' why without a validate round (objective.recommend, as metrics.recommend() reads it)."""
    if report.vhash == BASE_VHASH:
        return 'base', 'the phone\'s own settings replayed: its distance from the phone is the floor of every component'
    missing = [d for d in search_days if d not in report.days]
    if report.failed:
        return 'infeasible', _infeasible_why(report)
    if missing and all(set(r) <= {'not replayed on this day'} for r in report.infeasible_reasons.values()):
        return 'pending', f'replayed on {len(report.days)} of {len(search_days)} search days; verdict pending'
    if not report.feasible:
        if report.envelope_limited:                          # E is a validity envelope, not a safety rule
            return 'envelope-limited', envelope_why(report, question)
        return 'infeasible', _infeasible_why(report)
    tail = ''
    if report.validation and report.validation.get('reasons'):
        tail = '; hold-out: ' + '; '.join(report.validation['reasons'])
    dead = None
    if dead_keys:
        key = dead_keys[0]
        dead = ('dead', f'moves only {key.split()[-1]}, which is not testable on these days (every one-parameter point '
                        f'inside {DEAD_FLOOR_RATIO:g} x floor)')
    if report.tie_with_base:                                 # rule 4 before rule 5: a tie is never recommended
        return dead or ('tie', 'indistinguishable from base within the noise floor on every component and every day'
                        + tail)
    if report.step is False and question in M.QUESTION_DIRECTION:   # not an earlier / gentler step: never a candidate
        return dead or ('tie', no_step_why(report, question))
    if report.recommended and report.validation:
        v = report.validation
        share = v.get('fold_share')
        fold_top = v.get('fold_top', M.DEFAULT_OBJECTIVE['recommend']['fold_top'])
        text = (f'search review order {v["search_rank"]}, feasible on every search and hold-out day, in the top '
                f'{fold_top} on {M.share_pct(share if share is not None else 0, 0)} of {v["lodo_folds"]} '
                f'leave-one-day-out folds, direction holds on {v["holds"]}/{v["holdout_days"]} hold-out days, '
                f'hold-out rank {v["holdout_rank"]}: recommended for review')
        cav = M.validation_caveat(v)
        return 'recommended', text + (f'; {cav}' if cav else '')
    if report.recommended:
        return 'recommended', M.recommend_rule_text(objective, validate=False) + ': recommended for review'
    if dead:
        return dead
    return 'candidate', candidate_why(report, question) + tail


def _ties_between(analysis_order, reports, floors, days, scan=TIE_SCAN, limit=TIE_WITH_MAX):
    """tie_with for the first `scan` reports of the order: a tie when every component is inside TIE_FACTOR x floor
    on every search day both were replayed on."""
    names = list(analysis_order)
    for i, vh in enumerate(names[:scan]):
        a = reports[vh]
        for vh2 in names:
            if vh2 == vh or len(a.tie_with) >= limit:
                continue
            b = reports[vh2]
            common = [d for d in days if d in a.per_day and d in b.per_day and d in floors]
            if not common:
                continue
            tie = True
            for d in common:
                t = M.tie_components(a.per_day[d], b.per_day[d], floors[d])
                if not t or not all(t.values()):
                    tie = False
                    break
            if tie:
                a.tie_with.append(vh2)


def validate_phase(study):
    """The study's validate phase (contracts.Phase) or None."""
    return next((p for p in study.phases if p.kind == 'validate'), None)


def validation_of(study, dicts, objective):
    """metrics.validate_variants for the study's validate phase, or None when there is none, it has no days, or no
    validated variant has a hold-out row yet."""
    ph = validate_phase(study)
    if ph is None:
        return None
    from hindcast import search                             # lazy: search imports round lazily too
    try:
        vdays = search.validation_days(study, ph)
    except st.StudyError:
        return None
    if not vdays:
        return None
    val = M.validate_variants(dicts, study.question, objective, study.search_days(), vdays, ph.top)
    if not any(d['day'] in set(vdays) and d['variant'] in set(val.validated) for d in dicts):
        return None
    return val


def analyze(study, rows, settings=None, now=None):
    """The whole verdict step on a study's TrialRow list (or trial dicts): rankings, folds, strata, dead parameters,
    ties, one VariantReport per variant. `rows` are contracts.TrialRow objects (sd.trials()) or already the dicts
    of trial_dicts()."""
    if rows and isinstance(rows[0], TrialRow):
        dicts = trial_dicts(rows)
        failed = failed_by_variant(rows)
    else:
        dicts = list(rows)
        failed = {}
    search_days = study.search_days()
    holdout_days = study.holdout_days()
    objective = study.objective.to_dict()
    phone = phone_point(study, settings)
    ranking = M.rank_variants(dicts, study.question, objective, search_days)
    folds = M.leave_one_day_out(dicts, study.question, objective, search_days)
    holdout_ranking = None
    if holdout_days and any(d['day'] in holdout_days for d in dicts):
        holdout_ranking = M.rank_variants(dicts, study.question, objective, holdout_days)
    M.recommend(ranking, folds, holdout_ranking.order if holdout_ranking else None, objective)
    validation = validation_of(study, dicts, objective)
    if validation is not None:                               # H-308: the validate rule replaces recommend()
        holdout_ranking = validation.holdout_ranking
        for name, v in ranking.variants.items():
            pv = validation.per_variant.get(name)
            v.recommended_for_review = bool(pv and pv['recommended_for_review'])
            v.recommend_reasons = (list(pv['reasons']) if pv else
                                   [f'not in the top {validation.top} of the search review order: not validated'])
    strata: Dict[str, List[str]] = {}
    scored = set(search_days) | set(holdout_days)            # quarantined days are listed, not scored
    for d in study.days:
        if d.day not in scored:
            continue
        for tag in d.tags:
            strata.setdefault(tag, []).append(d.day)
    strata_rankings = {tag: M.rank_variants(dicts, study.question, objective, days) for tag, days in strata.items()
                       if any(x['day'] in days for x in dicts)}
    dead, ratios, members = dead_parameters(study, ranking, dicts, phone)
    dead_keys = {d.key for d in dead}

    by_variant: Dict[str, Dict[str, dict]] = {}
    vhash_of: Dict[str, str] = {}
    params_of: Dict[str, dict] = {}
    for d in dicts:
        by_variant.setdefault(d['variant'], {})[d['day']] = d['metrics']
        vhash_of[d['variant']] = d['vhash']
        params_of[d['variant']] = d['params']
    reports: Dict[str, VariantReport] = {}
    for name, per_day in by_variant.items():
        vh = vhash_of[name]
        rep = VariantReport(vhash=vh, variant=name, params=dict(params_of[name]), verdict='pending', why='',
                            days=[d for d in sorted(per_day) if d in search_days],
                            holdout_days=[d for d in sorted(per_day) if d in holdout_days],
                            per_day=dict(per_day), phone={k: phone.get(k) for k in params_of[name]},
                            failed=dict(failed.get(vh, {})))
        v = ranking.variants.get(name)
        if v is not None:
            rep.feasible, rep.infeasible_reasons = v.feasible, dict(v.infeasible_reasons)
            rep.tie_with_base, rep.tie_days = v.tie_with_base, dict(v.tie_days)
            rep.mean, rep.sign_frac = dict(v.mean), dict(v.sign_frac)
            rep.review_score, rep.review_component = v.review_score, v.review_component
            rep.floor_multiples = dict(v.floor_multiples)
            rep.pareto, rep.scalar, rep.rank = v.pareto, v.scalar, v.rank
            rep.recommended, rep.recommend_reasons = v.recommended_for_review, list(v.recommend_reasons)
            rep.flags = list(v.flags)
            rep.envelope_limited, rep.envelope_days = v.envelope_limited, dict(v.envelope_days)
            rep.step, rep.step_floor_U = v.step, v.step_floor_U
        elif name == M.BASE_VARIANT:                         # base: the manual-bolus keys of its search days (0 coverage)
            agg = M.aggregate_days({d: m for d, m in per_day.items() if d in search_days})['mean']
            rep.mean = {k: agg[k] for k in MANUAL_EFFECT_KEYS if k in agg}
        if holdout_ranking is not None and name != M.BASE_VARIANT:
            rep.holdout_rank = _rank_in(holdout_ranking.order, name)
            rep.holdout_total = len(holdout_ranking.order)
        if validation is not None and name in validation.per_variant:
            rep.validation = dict(validation.per_variant[name])
        for tag, rk in strata_rankings.items():
            rep.strata_ranks[tag] = _rank_in(rk.order, name)
            rep.strata_days[tag] = len([d for d in strata[tag] if d in per_day])
        moved = one_parameter_key(rep.params, phone) if name != M.BASE_VARIANT else None
        rep.dead_keys = [moved] if moved in dead_keys else []
        rep.verdict, rep.why = verdict_of(rep, rep.dead_keys, search_days, objective, study.question)
        rep.why = st.cut_words(rep.why, WHY_MAX)
        reports[vh] = rep
    order = [vhash_of[n] for n in ranking.order]
    front = [vhash_of[n] for n in ranking.front] if study.question != 'explore' else list(order)
    _ties_between(order, reports, ranking.floors, search_days)
    warnings = list(ranking.warnings)
    for key, r in ratios.items():
        if key not in dead_keys and r < 2 * DEAD_FLOOR_RATIO:
            warnings.append(f'{key}: one-parameter points reach {r:.1f} x floor at most')
    return Analysis(study=study, question=study.question, days=search_days, holdout_days=holdout_days,
                    ranking=ranking, holdout_ranking=holdout_ranking, folds=folds, reports=reports, order=order,
                    front=front, dead=dead, floors=ranking.floors, strata=strata, trials=len(dicts),
                    warnings=warnings, phone=phone, validation=validation,
                    sensitivity=M.weights_sensitivity(ranking))


# ----------------------------------------------------------------------------- round.json

def candidate_of(report):
    """contracts.Candidate from a VariantReport (the slim form round.json carries)."""
    flags = list(report.flags)
    if report.holdout_rank is not None:
        flags.append(f'rank:{HOLDOUT}={report.holdout_rank}')
    for tag, rank in sorted(report.strata_ranks.items()):
        if rank is not None:
            flags.append(f'rank:{tag}={rank}')
    return Candidate(vhash=report.vhash, variant=report.variant, verdict=report.verdict, why=st.cut_words(report.why, WHY_MAX),
                     params=dict(report.params), rank=report.rank, days=len(report.days),
                     feasible=report.feasible, effect=report.effect, flags=flags,
                     tie_with=list(report.tie_with[:TIE_WITH_MAX]),
                     validation=dict(report.validation) if report.validation else None,
                     envelope=report.envelope)


def round_sensitivity(analysis):
    """Round.sensitivity: metrics.weights_sensitivity with the variant names mapped to vhashes (round.json names
    variants by vhash, as `front`); None for explore. The report owner renders the panel from it."""
    sens = analysis.sensitivity
    if not sens:
        return None
    vh = {r.variant: r.vhash for r in analysis.reports.values()}
    out = dict(sens, order=[vh.get(n, n) for n in sens['order']], by_weight={})
    for key, per in sens['by_weight'].items():
        out['by_weight'][key] = {f: dict(v, order=[vh.get(n, n) for n in v['order']]) for f, v in per.items()}
    return out


def round_floors(floors):
    return {day: {k: _r3(v) for k, v in f.items() if k in FLOOR_KEYS and _num(v)} for day, f in sorted(floors.items())}


def build_round(study_dir, round_no=None, phase=None, settings=None, now=None, started=None, analysis=None):
    """contracts.Round for the study's current trials.jsonl. `round_no` / `phase` default to STATE.json's values
    (else 0 / the first pending phase). Pass `analysis` to reuse analyze()'s result."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    sd.require()
    study = Study.load(sd.study_json, strict=False)
    if analysis is None:
        analysis = analyze(study, sd.trials(), settings)
    state = sd.read_state()
    if round_no is None:
        round_no = (state.round if state and state.round is not None else 0)
    if phase is None:
        if state is not None:
            phase = state.phase
        else:
            pending = [p.kind for p in study.phases if p.state == 'pending']
            phase = pending[0] if pending else ('done' if study.phases else 'new')
    notes = list(analysis.warnings)
    if analysis.question == 'explore':
        notes.insert(0, 'review order, not a ranking by outcome')
    val = analysis.validation
    if val is not None:
        tau = 'n/a' if val.tau is None else f'{val.tau:+.2f}'
        notes.insert(1 if analysis.question == 'explore' else 0,
                     f'validate: top {len(val.validated)} of the search review order on {len(val.holdout_days)} '
                     f'hold-out day(s), {len(val.folds)} leave-one-day-out folds, Kendall tau search vs hold-out '
                     f'{tau}; {len(val.recommended)} recommended for review (a caveat over '
                     f'{len(val.scored_days)} days, not a statistic)')
    if analysis.question in M.QUESTION_DIRECTION:
        rk = analysis.ranking
        steps = len(rk.order) - len(rk.no_step)
        word = 'more' if analysis.question == 'earlier' else 'less'
        notes.insert(0, f'{analysis.question}: {steps} of {len(rk.order)} feasible variant(s) ask for {word} insulin on '
                        f'rises than the floor and are steps; the Pareto front ({len(rk.front)}) is taken among them, '
                        f'the other {len(rk.no_step)} are ties for this question')
        ph = validate_phase(analysis.study)
        top_note = M.validate_top_note(rk, ph.top) if ph is not None else None
        if top_note:
            notes.insert(1, top_note)
    n_env = len(analysis.ranking.envelope_limited)
    if n_env:
        notes.append(f'{n_env} envelope-limited variant(s): every hard check passes on every day, beyond the validity '
                     'envelope on some days; never ranked, listed with those days')
    doc = Round(round=int(round_no), phase=str(phase), started=started or now or contracts.now_iso(),
                finished=now or contracts.now_iso(), trials=analysis.trials,
                variants=[candidate_of(r) for r in analysis.ordered(include_base=True)],
                dead_parameters=list(analysis.dead), floors=round_floors(analysis.floors),
                front=list(analysis.front), notes=notes,
                validation=analysis.validation.summary() if analysis.validation is not None else None,
                sensitivity=round_sensitivity(analysis))
    for _ in range(4):                                       # fit the per-variant limit: trim notes, then floors
        if not doc.problems():
            break
        if doc.notes:
            doc.notes = doc.notes[:1]
        elif doc.floors:
            doc.floors = {}
            doc.notes = ['floors left out of round.json for size; status.json carries the worst-day floor']
        else:
            for c in doc.variants:
                c.tie_with, c.flags = c.tie_with[:1], c.flags[:3]
    return doc.validate()


def write_round(study_dir, round_doc):
    """studies/<study>/round.json in the compact form the size limit is checked on; returns the path."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    round_doc.validate()
    contracts.write_json_atomic(sd.round_path, round_doc.to_dict(), indent=None)
    return sd.round_path


def update_status(study_dir, now=None):
    """Rebuild and write status.json (study.build_status reads round.json for `top`); returns the Status."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    return sd.write_status(st.build_status(sd, now))


def produce(study_dir, round_no=None, phase=None, settings=None, now=None, started=None, analysis=None):
    """round.json, then status.json; returns (Round, Status). What the orchestrator calls at the end of a round
    (it passes its `analysis`, which it then hands to cards.write_cards: one view for round.json and the cards)."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    doc = build_round(sd, round_no=round_no, phase=phase, settings=settings, now=now, started=started,
                      analysis=analysis)
    write_round(sd, doc)
    return doc, update_status(sd, now)


def round_text(round_doc):
    """A short human rendering of a Round (one line per variant; the JSON is the contract)."""
    lines = [f'round {round_doc.round}  phase {round_doc.phase}  {round_doc.trials} trials  '
             f'{len(round_doc.variants)} variants']
    for n in round_doc.notes:
        lines.append(f'  note: {n}')
    for d in round_doc.dead_parameters:
        lines.append(f'  dead parameter {d.key}: {d.why}')
    for c in round_doc.variants:
        eff = ', '.join(f'{k} {c.effect[k]}' for k in ('rise_vs_base_U', 'rest_vs_base_U') if k in c.effect)
        lines.append(f'  {c.rank if c.rank is not None else "-":>3} {c.variant}  [{st.verdict_label(c.verdict)}] {c.why}'
                     + (f'  ({eff})' if eff else ''))
    lines += ['', contracts.HONESTY_STATEMENT]
    return '\n'.join(lines) + '\n'


# ----------------------------------------------------------------------------- CLI

def register(subparsers):
    """Add the `round` subcommand (cli.py's convention: `func(args) -> int`)."""
    p = subparsers.add_parser('round', help='write round.json and status.json from trials.jsonl (the verdict step)')
    p.add_argument('name', help='study name or directory')
    p.add_argument('--round', type=int, default=None, dest='round_no', help='round number (default: STATE.json)')
    p.add_argument('--phase', default=None, help='phase label (default: STATE.json / the first pending phase)')
    p.add_argument('--print', action='store_true', dest='print_text', help='also print the round as text')
    p.add_argument('--studies-dir', default=st.STUDIES_DIR)
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH)
    p.set_defaults(func=run_round)
    return p


def run_round(args):
    """`hc round <study>`: returns the exit code (0 ok, 3 refused)."""
    try:
        sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
        settings = spc.load_settings(args.settings)
        doc, status = produce(sd, round_no=args.round_no, phase=args.phase, settings=settings)
    except (st.StudyError, contracts.ContractError, OSError) as e:
        sys.stderr.write(f'hc round: refused: {e}\n')
        return st.EXIT_REFUSED
    sys.stdout.write(f'{sd.round_path}: {len(doc.variants)} variants, {len(doc.dead_parameters)} dead parameter(s); '
                     f'{sd.status_path}: {len(status.to_compact_json().encode("utf-8"))} bytes\n')
    if getattr(args, 'print_text', False):
        sys.stdout.write(round_text(doc))
    return st.EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.round', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(['round'] + list(sys.argv[1:] if argv is None else argv))
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
