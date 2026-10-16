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
    gives the hold-out rank; leave-one-day-out folds and metrics.recommend() give the recommended-for-review flag;
    one ranking per day tag (the strata the user typed into study.json) gives the per-stratum ranks.

Verdict per variant (contracts.VERDICTS; the first rule that applies wins)
    base          the base replay itself (the floor)
    pending       some search day has no row yet and none failed: the variant is still being evaluated
    infeasible    metrics.feasibility_day() names a reason on some day (P, envelope E, maxBolus, cap >= 50 %, loop
                  count), or the replay failed on a day (the error text is the reason)
    recommended   metrics.recommend(): feasible on all days, top-3 on >= 80 % of the leave-one-day-out folds, same
                  sign of the primary effect on >= 80 % of days, within the top 5 on the hold-out days
    dead          the variant moves exactly one searched parameter away from the phone's value and that parameter is
                  dead: every one-parameter point of it (at least DEAD_MIN_POINTS) stays inside DEAD_FLOOR_RATIO x
                  floor on every search day ("not testable on these days", H-304's rule, reported in dead_parameters)
    tie           indistinguishable from base within TIE_FACTOR x floor on every component and every day
    candidate     everything else; `why` names the component, the direction and the size in floors

`why` strings use the vocabulary of DESIGN.md section 3 (every one passes lint.lint_findings(..., 'line'); the
word is 'recommended for review', never 'recommended' alone). round.json keeps the contract words.

round.json (contracts.Round, <= 2 KB per variant): `variants` = one Candidate per variant (base first, then the
review order / ranked order, then infeasible and pending), `front` = the review order (explore) or the Pareto
front (earlier / gentler) as vhashes, `dead_parameters`, `floors` per day (floors_from_base, rounded), `notes`.
Candidate.effect carries the EFFECT_KEYS numbers (rounded to 3 dp); Candidate.flags carries the metric flags of
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
               'cap_binding_pct', 'max_single_smb_U', 'zero_temp_vs_base', 'review_score')
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
    pareto: bool = False
    scalar: Optional[float] = None
    strata_ranks: Dict[str, Optional[int]] = field(default_factory=dict)      # tag -> rank (None: not ranked there)
    strata_days: Dict[str, int] = field(default_factory=dict)
    holdout_rank: Optional[int] = None
    holdout_total: int = 0
    recommended: bool = False
    recommend_reasons: List[str] = field(default_factory=list)
    dead_keys: List[str] = field(default_factory=list)            # the dead parameter(s) a 'dead' variant moves
    phone: Dict[str, Any] = field(default_factory=dict)           # searched key -> the phone's value
    flags: List[str] = field(default_factory=list)

    @property
    def effect(self):
        out = {}
        for k in EFFECT_KEYS:
            v = self.review_score if k == 'review_score' else self.mean.get(k)
            if _num(v):
                out[k] = _r3(v)
        return out

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

    @property
    def base(self):
        return self.reports.get(BASE_VHASH)

    def ordered(self, include_base=False):
        """Reports in the order round.json lists them: base, order, infeasible, pending."""
        rest = [vh for vh in self.reports if vh != BASE_VHASH and vh not in self.order]
        rest.sort(key=lambda vh: (self.reports[vh].verdict != 'infeasible', self.reports[vh].variant))
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


def candidate_why(report):
    """The candidate verdict's `why`: direction, size and floors of the primary component, days."""
    comp = report.review_component
    v = report.mean.get(comp, 0.0) or 0.0
    other = 'rest_vs_base_U' if comp == 'rise_vs_base_U' else 'rise_vs_base_U'
    ov = report.mean.get(other)
    n = len(report.days)
    text = (f'would have asked for {abs(v):.2f} U {"more" if v > 0 else "less"} than base per day in '
            f'{_component_words(comp)} ({report.review_score:.1f} x floor) over {n} day{"s" if n != 1 else ""}')
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


def verdict_of(report, dead_keys, search_days):
    """(verdict, why) by the rule of the module docstring."""
    if report.vhash == BASE_VHASH:
        return 'base', 'the phone\'s own settings replayed: its distance from the phone is the floor of every component'
    missing = [d for d in search_days if d not in report.days]
    if report.failed:
        return 'infeasible', _infeasible_why(report)
    if missing and all(set(r) <= {'not replayed on this day'} for r in report.infeasible_reasons.values()):
        return 'pending', f'replayed on {len(report.days)} of {len(search_days)} search days; verdict pending'
    if not report.feasible:
        return 'infeasible', _infeasible_why(report)
    if report.recommended:
        return 'recommended', ('feasible on all days, in the top 3 on at least 80 % of the leave-one-day-out folds, '
                               'same sign of the primary effect on at least 80 % of days, within the top 5 on the '
                               'hold-out days: recommended for review')
    if dead_keys:
        key = dead_keys[0]
        return 'dead', (f'moves only {key.split()[-1]}, which is not testable on these days (every one-parameter point '
                        f'inside {DEAD_FLOOR_RATIO:g} x floor)')
    if report.tie_with_base:
        return 'tie', 'indistinguishable from base within the noise floor on every component and every day'
    return 'candidate', candidate_why(report)


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
    strata: Dict[str, List[str]] = {}
    for d in study.days:
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
            rep.pareto, rep.scalar, rep.rank = v.pareto, v.scalar, v.rank
            rep.recommended, rep.recommend_reasons = v.recommended_for_review, list(v.recommend_reasons)
            rep.flags = list(v.flags)
        if holdout_ranking is not None and name != M.BASE_VARIANT:
            rep.holdout_rank = _rank_in(holdout_ranking.order, name)
            rep.holdout_total = len(holdout_ranking.order)
        for tag, rk in strata_rankings.items():
            rep.strata_ranks[tag] = _rank_in(rk.order, name)
            rep.strata_days[tag] = len([d for d in strata[tag] if d in per_day])
        moved = one_parameter_key(rep.params, phone) if name != M.BASE_VARIANT else None
        rep.dead_keys = [moved] if moved in dead_keys else []
        rep.verdict, rep.why = verdict_of(rep, rep.dead_keys, search_days)
        rep.why = rep.why[:WHY_MAX]
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
                    warnings=warnings, phone=phone)


# ----------------------------------------------------------------------------- round.json

def candidate_of(report):
    """contracts.Candidate from a VariantReport (the slim form round.json carries)."""
    flags = list(report.flags)
    if report.holdout_rank is not None:
        flags.append(f'rank:{HOLDOUT}={report.holdout_rank}')
    for tag, rank in sorted(report.strata_ranks.items()):
        if rank is not None:
            flags.append(f'rank:{tag}={rank}')
    return Candidate(vhash=report.vhash, variant=report.variant, verdict=report.verdict, why=report.why[:WHY_MAX],
                     params=dict(report.params), rank=report.rank, days=len(report.days),
                     feasible=report.feasible, effect=report.effect, flags=flags,
                     tie_with=list(report.tie_with[:TIE_WITH_MAX]))


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
    doc = Round(round=int(round_no), phase=str(phase), started=started or now or contracts.now_iso(),
                finished=now or contracts.now_iso(), trials=analysis.trials,
                variants=[candidate_of(r) for r in analysis.ordered(include_base=True)],
                dead_parameters=list(analysis.dead), floors=round_floors(analysis.floors),
                front=list(analysis.front), notes=notes)
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


def produce(study_dir, round_no=None, phase=None, settings=None, now=None, started=None):
    """round.json, then status.json; returns (Round, Status). What the orchestrator calls at the end of a round."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
    doc = build_round(sd, round_no=round_no, phase=phase, settings=settings, now=now, started=started)
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
