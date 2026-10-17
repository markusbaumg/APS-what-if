"""
search.py  -  the search phases of a Hindcast study: oaat, grid, lhs, and the dead-parameter freeze (backlog H-304;
DESIGN.md sections 5.3 rule 2 and 5.4)

    from hindcast import search
    batch = search.next_batch(study, trials, settings=settings)       # -> [study.Task, ...]: the (params, day) pairs to
                                                                      #    replay next, base replays first
    p     = search.plan(study, trials, settings=settings)             # every phase: total / done / pending, frozen keys
    rnd   = search.round_for_phase(study, trials, p.current.index, round_no=1, settings=settings)   # contracts.Round
    search.write_round(study_dir, rnd)                                # studies/<study>/round.json
    hc search <study> [--json] [--batch N] [--write-round]            # the plan as text (register / run_search)

A task is `study.Task(params, day, phase, control, round)`; `(task.params, task.day)` is the pair the backlog names.
Nothing here replays, appends to trials.jsonl or opens a socket: the module reads `study.json` and the trial rows
and says what to evaluate next; `study.Runner` evaluates, `replay.py` (H-102) replays. Points come from
`space.ParamSpace` (H-303) only, so a Hindcast point carries the optimizer's variant name and vhash.

Phases (contracts.Phase; the state in study.json is mirrored by the orchestrator, completeness is read from the
trial rows so a resumed run continues where it stopped):

    floor   the base replay (empty VDF = the phone's own settings with profile_history) on every search and
            hold-out day. Every other phase prepends the base tasks of its days when they have no done row yet:
            the floors (metrics.floors_from_base) need them.
    oaat    one-at-a-time screen: `points` levels per parameter (default DEFAULT_LEVELS = 5; DESIGN.md says 5-7)
            spread over the parameter's grid around the phone's value (space.ParamSpace.oaat_points: the centre
            itself excluded, bool/choice parameters contribute every other choice). A phase imported from an
            optimizer grid file with mode one_at_a_time (`phase.grid` set) uses exactly those values, the phone's
            value included, so the count equals emulator_optimize.combinations(params, 'one_at_a_time') (the
            optimizer's base entry = the floor task).
    grid    cartesian product over `phase.grid` (--from-grid import; count = combinations(params, 'grid') minus
            the base entry) or, without one, over the full step grid of the live parameters; chain constraints
            filter the product.
    lhs     Latin hypercube with `n` points (default DEFAULT_LHS_N = 24) from random.Random(`seed`, default
            DEFAULT_SEED = 1): one stratum per point and parameter, decoded through ParamSpace.decode (chains kept
            by construction), snapped to the grid, duplicates dropped, so the same seed gives the same points on
            every machine and a different seed another design. Fewer than n points come back when the grid is
            smaller than n or a frozen chain member cuts points; the shortfall is in the notes.
    validate
            (H-308) the top `top` (default metrics.DEFAULT_VALIDATE_TOP = 8) variants of the search review order
            (metrics.rank_variants over the search days on the done rows, feasible only; the same rows round.analyze
            reads) replayed on the validation days: phase.days 'holdout' (or empty) = the study's `role: holdout`
            days (a quarantined day has left that role), or a comma list of hold-out days. The base of every
            validation day comes first like in every phase. The phase is complete when every (variant, day) task is
            settled (done, or failed like in every other phase - a failed hold-out replay is reported as not
            feasible on that day, it is not handed out forever). The points depend on the rows, so phase_tasks needs
            `trials` for it; while the search phases are still running the list is provisional (the plan only
            counts it, next_batch reaches it once every earlier phase is settled). The verdicts (hold-out rank,
            direction per hold-out day, leave-one-day-out rank range, Kendall tau, recommended for review) are
            round.py's, through metrics.validate_variants.
    halving, coordinate, optuna, cv, grow
            not built here (H-306, H-307, H-309; cv: the leave-one-day-out folds are computed by the validate round,
            no replays of their own): the plan marks them `skipped` with the issue number, next_batch skips them.

Days. A phase runs on the study's search days (`role: search`; hold-out and quarantined days never enter a search
phase; only `floor` and `validate` touch hold-out days). `phase.days` narrows them: 'screening' = `screening_days(study)` (up to SCREENING_DAYS = 3 days covering as
many distinct user-typed tag sets as possible, in study order), or a comma list of days. Tasks are point-major
(every day of one point, then the next point) so that a stopped run leaves whole variants behind.

Dead parameters (DESIGN.md 5.3 rule 2, 5.4 phase 1). Once an oaat phase is complete, each screened parameter is
judged on its screening days: for every one of its levels the effect vs base on every FLOOR_COMPONENT
(rise_vs_base_U, rest_vs_base_U, delta_vs_base_U, smb_vs_base_U, tbr_vs_base_U, changed_vs_base) is divided by the
day's effective floor (metrics.effective_floor over metrics.floors_from_base of the day's base row). A parameter
whose largest ratio over all levels, components and days is below DEAD_FACTOR = 1.5 is DEAD: it is reported in
round.json as contracts.DeadParameter with `why` "not testable on these days ..." and frozen for every later
phase. Frozen means not varied: the parameter is left out of later points, which replays it at the phone's own
value of each day (profile_history applied) exactly as the base does; a frozen member of a chain constraint is
carried explicitly at the phone's current value instead, so the chain the sampler keeps stays whole. The verdict
is conservative: a parameter is judged only when the base row and every level are settled (done or failed) on
every screening day and at least one level is done per day; levels without the *_vs_base metrics or an
incomplete screen leave the parameter live, with a note saying so. Dead parameters are a result, not a failure,
and the report says "not testable on these days", never "no effect".

Phase-runner interface (what the orchestrator, H-305, calls; everything here is pure over (study, trials)):

    runner = study.Runner(sd, study_obj, settings=settings, freeze_sha_of=...)
    round_no = 0
    previous = None
    while True:
        trials = sd.trials()
        p = plan(study_obj, trials, settings=settings, key_of=runner.key_of)
        for ph in p.phases:                                  # mirror completeness into study.json
            if ph.skipped and study_obj.phases[ph.index].state != 'skipped':
                sd.update_phase_state(ph.index, 'skipped')
            elif ph.complete and study_obj.phases[ph.index].state != 'done':
                sd.update_phase_state(ph.index, 'done')
        if p.current is None:
            break                                            # every phase done or skipped
        round_no += 1
        started = contracts.now_iso()
        batch = next_batch(study_obj, trials, settings=settings, key_of=runner.key_of, round_no=round_no)
        keys = [runner.key_of(t) for t in batch]
        if not batch or keys == previous:                    # no progress: a blocked day never froze, a point keeps
            raise study.GuardStop('search makes no progress')  # failing - the guard, not an endless loop
        previous = keys
        result = runner.run(batch, evaluate, phase=p.current.kind)
        rnd = round_for_phase(study_obj, sd.trials(), p.current.index, round_no, started, settings=settings,
                              key_of=runner.key_of)
        write_round(sd, rnd)
        if result.stop_reason != 'finished':
            break                                            # STOP, budget, guard: the Runner said why

    `key_of` makes the done-ness test identical to Runner.pending (by trial key: params, day, settings_sha,
    code_sha, freeze_sha, objective_version). Without it a (vhash, day) pair counts as settled when its latest row
    carries the study's code_sha and settings_sha. `hc search` passes the key_of of a Runner built the way
    `hc study run` builds it (runner_for: the live tree's code_sha / settings_sha), so the plan it prints is the
    one the next run works through. A task that has no settled row in that context but a row of another context
    (an older code or settings stamp), or whose latest row is `stale`, is counted as `stale`, not `pending`; both
    are handed out by next_batch. A row with status `done` or `failed` is settled; `blocked` and
    `stale` rows are handed out again (the day may have been frozen, the context re-evaluated); `retry=()` turns
    that off. `batch_size` cuts the batch for a shorter reporting cadence; the next call continues. The sentinel
    and null controls (H-302) are the orchestrator's, not phase tasks.

    The base of a day comes first (base_state). Its failed row is not settled: the base is handed out again until
    it has failed BASE_RETRY_LIMIT (3) times. While the latest base row of a day is failed or blocked, next_batch
    hands out the base alone and holds the day's variants (they would be compared with no base, or an old one);
    the variant rows that came back blocked because the base failed are re-planned once the base is done. A day in
    `unfrozen` ({day: reason}: freeze.blocked_reason at the start of the orchestrator's round) gets its base handed
    out once - one `blocked` row that says why - and nothing more until the freeze exists; `held` ({}) receives
    {day: why} for every day whose tasks were held, so a no-progress stop can name the cause.

Vocabulary: every string that can reach a human (notes, `why`, the plan text) passes lint.lint_findings(..., 'line');
test_search.py checks it. Nothing here changes the phone or ns_settings.json.
"""
import argparse
import json
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import metrics as mt                                                          # noqa: E402
from hindcast import space as spc                                                           # noqa: E402
from hindcast import study as st                                                            # noqa: E402
from hindcast.contracts import (BASE_VHASH, HONESTY_STATEMENT, Candidate, DeadParameter, Phase, Round, Study,  # noqa: E402
                                TrialRow, now_iso)
from hindcast.study import EXIT_OK, StudyDir, StudyError, Task                              # noqa: E402

DEAD_FACTOR = 1.5                                      # all levels inside DEAD_FACTOR x floor on all screening days = dead
DEAD_COMPONENTS = tuple(mt.FLOOR_COMPONENTS)           # the components the dead test looks at (metric key -> floor key)
DEFAULT_LEVELS = 5                                     # oaat levels per parameter (DESIGN.md: 5-7)
DEFAULT_LHS_N = 24                                     # lhs points (DESIGN.md: 24-48)
DEFAULT_SEED = 1                                       # lhs seed when the phase names none (reproducible by default)
SCREENING_DAYS = 3                                     # phase.days == 'screening': at most this many search days
SETTLED = ('done', 'failed')                           # a row with one of these statuses is not handed out again
BASE_RETRY_LIMIT = 3                                   # a failed base replay is handed out again until it failed this often
RETRY = ('blocked', 'stale')                           # these are handed out again (frozen since, context re-evaluated)
BUILT_KINDS = ('floor', 'oaat', 'grid', 'lhs', 'validate')
SEARCH_KINDS = ('oaat', 'grid', 'lhs', 'halving', 'coordinate', 'optuna')   # phases that search: search days only
VALIDATE_PHASE = 'validate'
LATER_ISSUE = {'halving': 'H-306', 'coordinate': 'H-307', 'optuna': 'H-307',
               'cv': 'H-308 (the leave-one-day-out folds are computed by the validate round)', 'grow': 'H-309'}
FLOOR_PHASE = 'floor'
NOT_TESTABLE = 'not testable on these days'


class SearchError(StudyError):
    """The study's phases cannot be planned (unknown day in phase.days, point off the space...): exit 3."""


# ----------------------------------------------------------------------------- rows

def _rows(trials) -> List[TrialRow]:
    """trials.jsonl rows as TrialRow (dicts accepted), in trial order."""
    out = []
    for r in trials or []:
        if isinstance(r, TrialRow):
            out.append(r)
        elif isinstance(r, dict):
            out.append(TrialRow.from_dict(r, strict=False))
        else:
            raise TypeError(f'trial rows must be TrialRow or dict, not {type(r).__name__}')
    return sorted(out, key=lambda r: r.trial)


def _phone(space, settings):
    """{name: the phone's value} for the parameters that have one (settings sections, else SpaceParam.phone)."""
    return space.phone_point(settings or {})


def _unit(param, value):
    """the unit-cube coordinate that ParamSpace.decode / space.pick map back onto `value` (the bin centre)"""
    values = param.values()
    i = values.index(value) if value in values else values.index(spc.snap(param, value))
    return (i + 0.5) / len(values)


class RowLookup:
    """The latest row per task. With `key_of` (Runner.key_of) the lookup is by trial key, exactly Runner.pending's
    rule; without it by (vhash, day) over the rows that carry the study's code_sha and settings_sha."""

    def __init__(self, study, trials, space, key_of=None):
        self.study = study
        self.space = space
        self.key_of = key_of
        rows = _rows(trials)
        self.by_key: Dict[str, TrialRow] = {}
        for r in rows:
            self.by_key[r.key] = r
        self.by_pair: Dict[Tuple[str, str], TrialRow] = {}
        for r in sorted(self.by_key.values(), key=lambda r: r.trial):
            if r.code_sha == study.code_sha and r.settings_sha == study.settings_sha:
                self.by_pair[(r.vhash, r.day)] = r
        # the latest row per (vhash, day) over every context: tells a stale task (rows exist, but of another code /
        # settings / freeze context) from a pending one (never evaluated). The scheduled controls are not tasks.
        self.any_pair: Dict[Tuple[str, str], TrialRow] = {}
        for r in sorted(self.by_key.values(), key=lambda r: r.trial):
            if r.control in (None, 'base'):
                self.any_pair[(r.vhash, r.day)] = r
        self.failures: Dict[Any, int] = {}             # failed rows per key (or per (vhash, day) without key_of)
        for r in rows:
            if r.status == 'failed':
                ident = r.key if key_of is not None else (r.vhash, r.day)
                self.failures[ident] = self.failures.get(ident, 0) + 1
        self.base_rows: Dict[str, TrialRow] = {}
        for r in sorted(self.by_key.values(), key=lambda r: r.trial):
            if r.status == 'done' and (r.vhash == BASE_VHASH or r.control == 'base') and r.metrics:
                if key_of is None and not (r.code_sha == study.code_sha and r.settings_sha == study.settings_sha):
                    continue
                self.base_rows[r.day] = r

    def vhash(self, params):
        return BASE_VHASH if not params else contracts.vhash(self.space.normalize(params))

    def row(self, task) -> Optional[TrialRow]:
        if self.key_of is not None:
            return self.by_key.get(self.key_of(task))
        return self.by_pair.get((self.vhash(task.params), task.day))

    def status(self, task) -> Optional[str]:
        r = self.row(task)
        return r.status if r else None

    def base(self, day) -> Optional[TrialRow]:
        if self.key_of is not None:
            r = self.row(Task(params={}, day=day, phase=FLOOR_PHASE, control='base'))
            return r if r is not None and r.status == 'done' and r.metrics else None
        return self.base_rows.get(day)

    def is_stale(self, task) -> bool:
        """True when the task's latest row in this context is `stale`, or it has none but a row of the same
        (vhash, day) exists under another context."""
        s = self.status(task)
        if s == 'stale':
            return True
        return s is None and (self.vhash(task.params), task.day) in self.any_pair

    def failed_count(self, task) -> int:
        ident = self.key_of(task) if self.key_of is not None else (self.vhash(task.params), task.day)
        return self.failures.get(ident, 0)

    def base_task(self, day) -> Task:
        return Task(params={}, day=day, phase=FLOOR_PHASE, control='base')

    def base_retry(self, task, retry=RETRY) -> bool:
        """True for a base task whose latest row is failed and that failed fewer than BASE_RETRY_LIMIT times."""
        return (bool(retry) and _is_base(task) and self.status(task) == 'failed'
                and self.failed_count(task) < BASE_RETRY_LIMIT)

    def base_state(self, day, unfrozen=None, retry=RETRY) -> Tuple[str, str]:
        """(state, why) of a day's base replay: 'done'; 'new' (no row); 'retry' (latest row failed under the limit,
        or blocked on a frozen day); 'held' (the day is in `unfrozen` and its base has a row: nothing is handed out);
        'exhausted' (failed BASE_RETRY_LIMIT times)."""
        t = self.base_task(day)
        r = self.row(t)
        s = r.status if r is not None else None
        unfrozen = unfrozen or {}
        if s == 'done':
            return 'done', ''
        if s is None or s == 'stale':
            if day in unfrozen and s is None:
                return 'new', f'{day}: {unfrozen[day]}'
            return 'new', ''
        if day in unfrozen:
            return 'held', f'{day}: {unfrozen[day]}'
        if s == 'failed':
            n = self.failed_count(t)
            if n >= BASE_RETRY_LIMIT or not retry:
                return 'exhausted', f'{day}: the base replay failed {n} time(s); its variants are held'
            return 'retry', f'{day}: the base replay failed {n} time(s); replaying it again before its variants'
        return 'retry', f'{day}: the base replay came back {s}; replaying it again before its variants'

    def pending(self, tasks, retry=RETRY):
        out = []
        for t in tasks:
            s = self.status(t)
            if s is None or s in retry or self.base_retry(t, retry):
                out.append(t)
        return out


def _is_base(task):
    return getattr(task, 'control', None) == 'base' or not getattr(task, 'params', None)


# ----------------------------------------------------------------------------- days

def search_days(study) -> List[str]:
    return [d.day for d in study.days if d.role == 'search']


def screening_days(study, n=SCREENING_DAYS) -> List[str]:
    """Up to `n` search days covering as many distinct user-typed tag sets as possible (first day of each tag set in
    study order, then the remaining days in order). Days never carry inferred strata (DESIGN.md 4.5)."""
    days = [d for d in study.days if d.role == 'search']
    chosen, seen = [], set()
    for d in days:
        sig = tuple(sorted(d.tags))
        if sig not in seen and len(chosen) < n:
            seen.add(sig)
            chosen.append(d.day)
    for d in days:
        if len(chosen) >= n:
            break
        if d.day not in chosen:
            chosen.append(d.day)
    return chosen


def validation_days(study, phase) -> List[str]:
    """The days of a validate phase: phase.days '' / 'holdout' -> the study's hold-out days (role holdout: quarantined
    days have left it), or a comma list that must name hold-out days."""
    spec = (phase.days or '').strip()
    holdout = study.holdout_days()
    if not spec or spec == 'holdout':
        return holdout
    wanted = [d.strip() for d in spec.split(',') if d.strip()]
    bad = [d for d in wanted if d not in set(holdout)]
    if bad:
        raise SearchError(f'phase validate: days {", ".join(bad)} are not hold-out days of this study (a search day '
                          f'cannot validate the search; a quarantined day is excluded from scoring)')
    return wanted


def phase_days(study, phase) -> List[str]:
    """The days a phase runs on: search days; 'screening' -> screening_days(); a comma list -> those search days;
    floor -> search + hold-out days; validate -> validation_days()."""
    if phase.kind == FLOOR_PHASE:
        return [d.day for d in study.days if d.role in ('search', 'holdout')]
    if phase.kind == VALIDATE_PHASE:
        return validation_days(study, phase)
    spec = (phase.days or '').strip()
    if not spec or spec == 'search':
        return search_days(study)
    if spec == 'screening':
        return screening_days(study)
    if spec == 'holdout':
        raise SearchError(f'phase {phase.kind}: hold-out days never enter a search phase (DESIGN.md 5.4)')
    wanted = [d.strip() for d in spec.split(',') if d.strip()]
    allowed = set(search_days(study))
    bad = [d for d in wanted if d not in allowed]
    if bad:
        raise SearchError(f'phase {phase.kind}: days {", ".join(bad)} are not search days of this study')
    return wanted


# ----------------------------------------------------------------------------- points

def lhs_design(n, names, seed=DEFAULT_SEED) -> List[Dict[str, float]]:
    """A Latin hypercube in the unit cube: n points, every parameter visits each of the n strata once; the
    stratum order and the position inside a stratum come from random.Random(seed)."""
    rng = random.Random(seed)
    names = list(names)
    cols = {}
    for name in names:
        perm = list(range(n))
        rng.shuffle(perm)
        cols[name] = [(perm[j] + rng.random()) / n for j in range(n)]
    return [{name: cols[name][j] for name in names} for j in range(n)]


def _apply_frozen(space, point, frozen, phone):
    """A decoded full point -> the point with frozen parameters removed (chain members pinned at the phone's value)."""
    out = {}
    for name in space.names:
        if name not in point and name not in frozen:
            continue
        if name in frozen:
            v = frozen[name] if frozen[name] is not None else phone.get(name)
            if name in space.chained and v is not None:
                out[name] = spc.snap(space[name], v)
            continue
        out[name] = point[name]
    return out


def _frozen_value(frozen, phone, name):
    v = frozen.get(name)
    return v if v is not None else phone.get(name)


def lhs_points(space, n=DEFAULT_LHS_N, seed=DEFAULT_SEED, frozen=None, phone=None) -> Tuple[List[dict], List[str]]:
    """`n` Latin-hypercube points over the live parameters, legal, snapped, deduplicated, in design order;
    the notes say how many points the grid size or a frozen chain member cut."""
    frozen = dict(frozen or {})
    phone = dict(phone or {})
    live = [name for name in space.names if name not in frozen]
    notes = []
    if not live:
        return [], ['lhs: every parameter is frozen; nothing to sample']
    pts, seen, dup, cut = [], set(), 0, 0
    for u in lhs_design(n, live, seed):
        full = dict(u)
        for name in frozen:
            v = _frozen_value(frozen, phone, name)
            full[name] = _unit(space[name], spc.snap(space[name], v)) if v is not None else 0.5
        point = _apply_frozen(space, space.decode(full), frozen, phone)
        if not space.satisfies(point):
            cut += 1
            continue
        vh = contracts.vhash(space.normalize(point))
        if vh in seen:
            dup += 1
            continue
        seen.add(vh)
        pts.append(space.normalize(point))
    if dup:
        notes.append(f'lhs: {dup} of {n} points fell on a grid point already drawn (grid smaller than the design) and were dropped')
    if cut:
        notes.append(f'lhs: {cut} of {n} points violated a chain constraint next to a frozen member and were dropped')
    return pts, notes


def oaat_points(space, phase, frozen=None, phone=None) -> List[dict]:
    """The one-at-a-time levels of a phase: `phase.grid` values when imported from an optimizer file (the phone's
    value included, as the optimizer runs it), else `phase.points` levels around the phone's value (centre excluded)."""
    frozen = frozen or {}
    phone = phone or {}
    if phase.grid:
        pts = [{name: v} for name, vals in phase.grid.items() if name in space and name not in frozen for v in vals]
    else:
        centre = {name: v for name, v in phone.items() if name not in frozen}
        sub = _live_space(space, frozen)
        pts = sub.oaat_points(phase.points or DEFAULT_LEVELS, centre)
    return [space.normalize(p) for p in pts if space.satisfies(p)]


def _live_space(space, frozen):
    """A ParamSpace over the live parameters; chains with a frozen member are dropped (the frozen member is pinned
    explicitly by _apply_frozen and the full space re-checks every point)."""
    if not frozen:
        return space
    live = {name: space[name] for name in space.names if name not in frozen}
    if not live:
        return None
    chains = [c.tokens() for c in space.constraints if all(n in live for n in c.names)]
    return spc.ParamSpace(live, chains, check_locked=False)


def grid_points(space, phase, frozen=None, phone=None) -> List[dict]:
    """Cartesian product over `phase.grid` (imported) or the full step grid of the live parameters; frozen
    parameters are left out (chain members pinned), chains filter, duplicates dropped, space order."""
    frozen = dict(frozen or {})
    phone = dict(phone or {})
    grid = None
    if phase.grid:
        grid = {name: list(vals) for name, vals in phase.grid.items() if name in space and name not in frozen}
        if not grid:
            return []
    else:
        sub = _live_space(space, frozen)
        if sub is None:
            return []
        grid = {name: sub[name].values() for name in sub.names}
    pts, seen = [], set()
    for point in space.grid_points(grid):               # partial points: chains with a missing member pass here
        point = _apply_frozen(space, dict(point), frozen, phone)
        if not space.satisfies(point):
            continue
        point = space.normalize(point)
        vh = contracts.vhash(point)
        if vh in seen:
            continue
        seen.add(vh)
        pts.append(point)
    return pts


def phase_points(space, phase, frozen=None, phone=None) -> Tuple[List[dict], List[str]]:
    """The points of a built phase (floor: [] - its tasks are the base replays) and the notes about them."""
    if phase.kind == FLOOR_PHASE:
        return [], []
    if phase.kind == 'oaat':
        pts = oaat_points(space, phase, frozen, phone)
        live = sorted({next(iter(p)) for p in pts})
        return pts, [f'oaat: {len(pts)} level(s) over {len(live)} parameter(s)'
                     + (f', {len(frozen)} frozen' if frozen else '')]
    if phase.kind == 'grid':
        pts = grid_points(space, phase, frozen, phone)
        return pts, [f'grid: {len(pts)} point(s)' + (f', {len(frozen)} parameter(s) frozen' if frozen else '')]
    if phase.kind == 'lhs':
        n = phase.n or DEFAULT_LHS_N
        seed = DEFAULT_SEED if phase.seed is None else phase.seed
        pts, notes = lhs_points(space, n, seed, frozen, phone)
        return pts, [f'lhs: {len(pts)} point(s) of {n} asked, seed {seed}'
                     + (f', {len(frozen)} parameter(s) frozen' if frozen else '')] + notes
    if phase.kind == VALIDATE_PHASE:
        raise SearchError('the validate points come from the trial rows: phase_tasks(..., trials=...) / validate_points')
    raise SearchError(f'phase kind {phase.kind!r} is not built here ({LATER_ISSUE.get(phase.kind, "later wave")})')


def validate_points(study, trials, phase, space=None, settings=None) -> Tuple[List[dict], List[str]]:
    """The points of a validate phase: the top `phase.top` variants of the search review order over the done rows
    (round.trial_dicts: the latest done row per key, controls left out, base relabelled), feasible only, in that
    order; and the notes. The same ranking round.analyze / metrics.validate_variants starts from."""
    from hindcast import round as rd                        # round imports study and metrics only: no cycle
    space = space or spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    top = int(phase.top or mt.DEFAULT_VALIDATE_TOP)
    sdays = search_days(study)
    rows = [r for r in _rows(trials) if r.day in set(sdays)]
    dicts = rd.trial_dicts(rows)
    if not dicts:
        return [], [f'validate: no done rows on the search days yet; the top {top} is not known']
    objective = study.objective.to_dict() if hasattr(study.objective, 'to_dict') else dict(study.objective or {})
    ranking = mt.rank_variants(dicts, study.question, objective, sdays)
    params_of = {d['variant']: d['params'] for d in dicts}
    points = []
    for name in ranking.order[:top]:
        p = params_of.get(name) or {}
        if p:
            points.append(space.normalize(p) if all(k in space for k in p) else dict(p))
    notes = [f'validate: top {len(points)} of {len(ranking.order)} feasible variant(s) of the search review order '
             f'({len(sdays)} search day(s)) on the validation days']
    if len(points) < top:
        notes.append(f'validate: {top} asked, {len(points)} feasible on the search days')
    return points, notes


def phase_tasks(study, phase, frozen=None, settings=None, space=None, round_no=None, trials=None) -> Tuple[List[Task], List[str]]:
    """Every task of a phase: the base replay of each of its days first (phase 'floor', control 'base'), then the
    points, point-major. The tasks are complete, not filtered by what is done (see RowLookup.pending). A validate
    phase needs `trials` (its points are the search's top-N; without rows it has base tasks only)."""
    space = space or spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    phone = _phone(space, settings)
    days = phase_days(study, phase)
    tasks = [Task(params={}, day=d, phase=FLOOR_PHASE, control='base', round=round_no) for d in days]
    if phase.kind == VALIDATE_PHASE:
        points, notes = validate_points(study, trials or [], phase, space, settings)
        if not days:
            notes = ['validate: no hold-out days reserved (or all quarantined); nothing to validate']
    else:
        points, notes = phase_points(space, phase, frozen, phone)
    for point in points:
        for d in days:
            tasks.append(Task(params=dict(point), day=d, phase=phase.kind, round=round_no))
    return tasks, notes


# ----------------------------------------------------------------------------- dead parameters

def _effect_ratio(metrics_, floors, components=DEAD_COMPONENTS) -> Optional[float]:
    """max over the components present of |effect| / effective floor; None when none of them is in the metrics."""
    best = None
    for key in components:
        v = metrics_.get(key)
        if v is None:
            continue
        fkey = mt.FLOOR_COMPONENTS[key]
        r = abs(float(v)) / mt.effective_floor(floors.get(fkey), fkey)
        best = r if best is None else max(best, r)
    return best


def dead_parameters(study, trials, phase, frozen=None, settings=None, space=None, key_of=None,
                    factor=DEAD_FACTOR) -> Tuple[List[DeadParameter], List[str]]:
    """The dead parameters of a complete oaat phase and the notes about parameters that could not be judged.

    Per parameter and screening day: the base row and every level settled (done or failed), at least one level
    done; ratio = max over the done levels of _effect_ratio(); dead when the largest ratio over all days is below
    `factor`. `frozen` are the parameters frozen before this phase (they were not screened here)."""
    if phase.kind != 'oaat':
        return [], []
    space = space or spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    phone = _phone(space, settings)
    lookup = RowLookup(study, trials, space, key_of)
    days = phase_days(study, phase)
    points = oaat_points(space, phase, frozen, phone)
    by_param: Dict[str, List[dict]] = {}
    for p in points:
        by_param.setdefault(next(iter(p)), []).append(p)
    dead, notes = [], []
    for name, levels in by_param.items():
        item = name.split(' ', 1)[1]
        ratio_max, judged_days, failed, incomplete, unmetered = 0.0, 0, 0, [], 0
        for d in days:
            base = lookup.base(d)
            if base is None:
                incomplete.append(f'{d}: no base replay')
                continue
            floors = mt.floors_from_base(base.metrics)
            done_here = settled_here = 0
            for p in levels:
                r = lookup.row(Task(params=p, day=d, phase='oaat'))
                if r is None or r.status not in SETTLED:
                    incomplete.append(f'{d}: {space.variant_name(p)} not replayed yet')
                    continue
                settled_here += 1
                if r.status == 'failed':
                    failed += 1
                    continue
                ratio = _effect_ratio(r.metrics, floors)
                if ratio is None:
                    unmetered += 1
                    continue
                done_here += 1
                ratio_max = max(ratio_max, ratio)
            if done_here:
                judged_days += 1
            elif not settled_here:
                incomplete.append(f'{d}: no level replayed')
        if incomplete:
            notes.append(f'{item}: screen incomplete, not judged ({"; ".join(incomplete[:3])}'
                         + (f'; +{len(incomplete) - 3} more' if len(incomplete) > 3 else '') + ')')
            continue
        if unmetered:
            notes.append(f'{item}: {unmetered} level(s) without the vs-base metrics; not judged')
            continue
        if judged_days < len(days) or judged_days == 0:
            notes.append(f'{item}: judged on {judged_days} of {len(days)} screening day(s) only; kept live')
            continue
        if ratio_max < factor:
            why = (f'{NOT_TESTABLE}: every level of {item} stays inside {factor:g} x floor on {judged_days} screening '
                   f'day(s), largest effect {ratio_max:.2f} x floor'
                   + (f' ({failed} level(s) failed to replay)' if failed else '')
                   + '; frozen at the phone\'s value for the later phases')
            dead.append(DeadParameter(key=name, why=why, floor_ratio=round(ratio_max, 3)))
        elif failed:
            notes.append(f'{item}: {failed} level(s) failed to replay; judged on the rest (largest effect {ratio_max:.2f} x floor)')
    return dead, notes


# ----------------------------------------------------------------------------- the plan

@dataclass
class PhasePlan:
    index: int
    kind: str
    state: str                                 # study.json's state
    days: List[str]
    total: int                                 # tasks of the phase (base tasks of its days included)
    done: int
    failed: int
    pending: int                               # to evaluate, never evaluated in any context (or blocked)
    complete: bool                             # nothing pending or stale (done or failed everywhere)
    skipped: bool = False                      # not built here (halving, optuna, ...)
    frozen: Dict[str, Any] = field(default_factory=dict)       # parameters frozen BEFORE this phase (name -> phone value)
    dead: List[DeadParameter] = field(default_factory=list)    # parameters this (complete oaat) phase froze
    notes: List[str] = field(default_factory=list)
    stale: int = 0                             # to evaluate again: rows exist, but only of another context / `stale`

    def to_dict(self):
        return {'index': self.index, 'kind': self.kind, 'state': self.state, 'days': list(self.days), 'total': self.total,
                'done': self.done, 'failed': self.failed, 'pending': self.pending, 'stale': self.stale,
                'complete': self.complete,
                'skipped': self.skipped, 'frozen': dict(self.frozen), 'dead': [d.to_dict() for d in self.dead],
                'notes': list(self.notes)}


@dataclass
class SearchPlan:
    phases: List[PhasePlan]
    current: Optional[PhasePlan]               # the first phase with pending tasks; None when everything is settled
    frozen: Dict[str, Any]                     # every parameter frozen so far (name -> phone value or None)
    dead: List[DeadParameter]

    def to_dict(self):
        return {'phases': [p.to_dict() for p in self.phases], 'current': self.current.index if self.current else None,
                'frozen': dict(self.frozen), 'dead': [d.to_dict() for d in self.dead]}


def plan(study, trials, settings=None, key_of=None, retry=RETRY) -> SearchPlan:
    """Walk the phases in order: tasks, done/failed/pending counts from the rows, dead parameters of every complete
    oaat phase carried as `frozen` into the later phases. The phase states in study.json are reported, not used:
    completeness comes from the rows so a resumed run continues where it stopped."""
    space = spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    phone = _phone(space, settings)
    lookup = RowLookup(study, trials, space, key_of)
    frozen: Dict[str, Any] = {}
    dead_all: List[DeadParameter] = []
    plans: List[PhasePlan] = []
    current = None
    for i, ph in enumerate(study.phases):
        if ph.kind not in BUILT_KINDS:
            plans.append(PhasePlan(index=i, kind=ph.kind, state=ph.state, days=[], total=0, done=0, failed=0, pending=0,
                                   complete=False, skipped=True, frozen=dict(frozen),
                                   notes=[f'{ph.kind}: not built yet (backlog {LATER_ISSUE.get(ph.kind, "later")}); skipped']))
            continue
        tasks, notes = phase_tasks(study, ph, frozen, settings, space, trials=trials)
        statuses = [lookup.status(t) for t in tasks]
        done = sum(1 for s in statuses if s == 'done')
        failed = sum(1 for s in statuses if s == 'failed')
        todo = lookup.pending(tasks, retry)
        stale = sum(1 for t in todo if lookup.is_stale(t))
        pending = len(todo) - stale
        pp = PhasePlan(index=i, kind=ph.kind, state=ph.state, days=phase_days(study, ph), total=len(tasks), done=done,
                       failed=failed, pending=pending, complete=not todo, frozen=dict(frozen), notes=notes, stale=stale)
        if ph.kind == 'oaat' and pp.complete:
            dead, dnotes = dead_parameters(study, trials, ph, frozen, settings, space, key_of)
            pp.dead = dead
            pp.notes += dnotes
            for d in dead:
                frozen[d.key] = phone.get(d.key)
                dead_all.append(d)
        if current is None and not pp.complete:
            current = pp
        plans.append(pp)
    return SearchPlan(phases=plans, current=current, frozen=frozen, dead=dead_all)


def next_batch(study, trials, settings=None, key_of=None, batch_size=None, round_no=None, retry=RETRY,
               unfrozen=None, held=None) -> List[Task]:
    """The tasks to evaluate next: the pending tasks of the first phase that has any (base replays first, then the
    points, point-major), cut to `batch_size` when given; [] when every built phase is settled or everything left
    is held. A day's variants are handed out only while its base is done or has no row yet; `unfrozen` and `held`:
    see the module docstring."""
    p = plan(study, trials, settings, key_of, retry)
    if p.current is None:
        return []
    ph = study.phases[p.current.index]
    space = spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    tasks, _ = phase_tasks(study, ph, p.current.frozen, settings, space, round_no, trials=trials)
    lookup = RowLookup(study, trials, space, key_of)
    pending = lookup.pending(tasks, retry)
    states = {}
    out = []
    for t in pending:
        if t.day not in states:
            states[t.day] = lookup.base_state(t.day, unfrozen, retry)
        state, why = states[t.day]
        if _is_base(t):
            ok = state in ('new', 'retry')
        else:
            ok = state == 'done' or (state == 'new' and t.day not in (unfrozen or {}))
        if ok:
            out.append(t)
        elif held is not None and why:
            held.setdefault(t.day, why)
    out.sort(key=lambda t: 0 if t.control == 'base' else 1)            # stable: base first, then design order
    if batch_size:
        out = out[:max(1, int(batch_size))]
    return out


def untried_keys(study, trials, settings=None, key_of=None) -> set:
    """The keys (or (vhash, day) pairs without key_of) of every task of the built phases that has no row yet:
    what a stopped run still had to do (STATE.json queue_len, so `hc study status` keeps the planned count)."""
    space = spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    lookup = RowLookup(study, trials, space, key_of)
    p = plan(study, trials, settings, key_of)
    out = set()
    for pp in p.phases:
        if pp.skipped:
            continue
        tasks, _ = phase_tasks(study, study.phases[pp.index], pp.frozen, settings, space, trials=trials)
        for t in tasks:
            if lookup.row(t) is None:
                out.add(key_of(t) if key_of is not None else (lookup.vhash(t.params), t.day))
    return out


# ----------------------------------------------------------------------------- round.json

def _effect_numbers(mean: dict) -> Dict[str, float]:
    out = {}
    for k in ('rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U', 'changed_vs_base'):
        v = mean.get(k) if mean else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), 3)
    return out


def round_for_phase(study, trials, phase_index, round_no, started=None, settings=None, key_of=None,
                    finished=None) -> Round:
    """contracts.Round for one phase from the done rows: every point of the phase as a Candidate with the verdict
    of metrics.rank_variants / leaderboard_rows (candidate, tie, infeasible; 'dead' for the levels of a parameter
    the phase froze; the base row as 'base'), the dead parameters frozen so far (this phase and every earlier one:
    study.build_status reads status.dead_parameters from the latest round.json), the per-day floors and the front
    (review order for `explore`, the Pareto front otherwise). The orchestrator writes it with write_round()."""
    ph = study.phases[phase_index]
    space = spc.ParamSpace.from_study(study, settings=settings, check_locked=False)
    p = plan(study, trials, settings, key_of)
    pp = p.phases[phase_index]
    phone = _phone(space, settings)
    tasks, notes = phase_tasks(study, ph, pp.frozen, settings, space, trials=trials)
    days = pp.days
    lookup = RowLookup(study, trials, space, key_of)
    wanted = {lookup.vhash(t.params) for t in tasks}
    rows = []
    for t in tasks:
        r = lookup.row(t)
        if r is not None and r.status == 'done' and r.metrics:
            rows.append(r)
    for d in days:                                             # the base rows, whatever phase replayed them
        b = lookup.base(d)
        if b is not None and b not in rows:
            rows.append(b)
    dict_rows = []
    for r in rows:
        dr = r.to_dict()
        if r.vhash == BASE_VHASH or r.control == 'base':
            dr['variant'] = mt.BASE_VARIANT
        dict_rows.append(dr)
    dead = [d for earlier in p.phases[:phase_index + 1] for d in earlier.dead]   # cumulative: status.json reads it
    dead_keys = {d.key for d in dead}
    variants: List[Candidate] = []
    front: List[str] = []
    floors: Dict[str, Dict[str, float]] = {}
    if dict_rows:
        objective = study.objective.to_dict() if hasattr(study.objective, 'to_dict') else dict(study.objective or {})
        ranking = mt.rank_variants(dict_rows, study.question, objective, days)
        notes += ranking.warnings
        vhash_of = {}
        for r in rows:
            label = mt.BASE_VARIANT if (r.vhash == BASE_VHASH or r.control == 'base') else r.variant
            vhash_of.setdefault(label, r.vhash)
        for d, f in ranking.floors.items():
            floors[d] = {k: round(float(v), 4) for k, v in f.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        for lb in mt.leaderboard_rows(ranking):
            params = lb['params'] or {}
            verdict, why = lb['verdict'], lb['why']
            if params and set(params) <= dead_keys and verdict != 'infeasible':
                verdict = 'dead'
                why = f'{NOT_TESTABLE}: a level of a frozen parameter; ' + why
            variants.append(Candidate(vhash=vhash_of.get(lb['variant'], contracts.vhash(space.normalize(params) if params else {})),
                                      variant=lb['variant'], verdict=verdict, why=why[:600], params=params, rank=lb['rank'],
                                      days=len(lb['days']), feasible=lb['feasible'], effect=_effect_numbers(lb['mean']),
                                      flags=list(lb['flags'] or [])[:8]))
        base_days = [d for d in days if lookup.base(d) is not None]
        if base_days:
            variants.insert(0, Candidate(vhash=BASE_VHASH, variant=mt.BASE_VARIANT, verdict='base',
                                         why=f'the phone\'s own settings replayed on {len(base_days)} day(s): the floor every '
                                             f'other variant is measured against', params={}, days=len(base_days), feasible=True))
        order = ranking.front or ranking.order
        front = [vhash_of[v] for v in order if v in vhash_of]
        if study.question == 'explore':
            notes.append('review order, not a ranking by outcome')
    if wanted and not rows:
        notes.append('no done rows yet for this phase')
    rnd = Round(round=int(round_no), phase=ph.kind, started=started or now_iso(), finished=finished or now_iso(),
                trials=len([r for r in rows if r.vhash != BASE_VHASH]), variants=variants, dead_parameters=dead,
                floors=floors, front=front, notes=notes)
    return rnd.validate()


def write_round(study_dir, rnd: Round) -> str:
    """round.json by atomic rename; returns the path."""
    sd = study_dir if isinstance(study_dir, StudyDir) else StudyDir(study_dir)
    rnd.validate().save(sd.round_path)
    return sd.round_path


# ----------------------------------------------------------------------------- text and CLI

def plan_text(p: SearchPlan, study=None) -> str:
    """The plan as the human reads it (`hc search`); every line passes the vocabulary lint."""
    lines = []
    if study is not None:
        lines.append(f'study {study.name}   question: {study.question}   {len(study.space)} parameter(s), '
                     f'{len(search_days(study))} search day(s)')
    for pp in p.phases:
        mark = '>' if p.current is not None and pp.index == p.current.index else ' '
        if pp.skipped:
            lines.append(f'{mark} {pp.index}: {pp.kind:<8} skipped - {pp.notes[0] if pp.notes else "not built"}')
            continue
        lines.append(f'{mark} {pp.index}: {pp.kind:<8} {pp.done} done, {pp.failed} failed, {pp.pending} pending, '
                     f'{pp.stale} stale of {pp.total} on {len(pp.days)} day(s)' + (' - complete' if pp.complete else ''))
        for n in pp.notes:
            lines.append(f'      {n}')
        for d in pp.dead:
            lines.append(f'      {d.key}: {d.why}')
    if p.frozen:
        lines.append(f'{NOT_TESTABLE}, frozen: ' + ', '.join(p.frozen))
    if p.current is None:
        lines.append('every built phase is settled; nothing pending')
        if any(pp.kind == VALIDATE_PHASE and not pp.skipped for pp in p.phases):
            lines.append('the validate round names the variants recommended for review (`hc round`, `hc study status`)')
    else:
        lines.append(f'next: phase {p.current.index} ({p.current.kind}), {p.current.pending} task(s) pending, '
                     f'{p.current.stale} stale')
    if any(pp.stale for pp in p.phases):
        lines.append('stale = replayed under another code or settings stamp; `hc study run --on-stale reeval` '
                     'replays them again')
    return '\n'.join(lines) + '\n'


def register(subparsers):
    """Add the `search` subcommand (cli.py calls this); its `func` is run_search."""
    p = subparsers.add_parser('search', help='the search plan of a study: phases, pending tasks, frozen parameters')
    p.add_argument('name', help='study name (studies/<name>) or a study directory')
    p.add_argument('--json', action='store_true', help='the plan as JSON instead of text')
    p.add_argument('--batch', type=int, default=None, help='also list the next N tasks')
    p.add_argument('--write-round', action='store_true',
                   help='write round.json for the current (or last) phase from the done rows')
    p.add_argument('--studies-dir', default=st.STUDIES_DIR, help='where studies/<name>/ live (default software/studies)')
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH, help='ns_settings.json')
    p.set_defaults(func=run_search)
    return p


def runner_for(sd, study, settings, settings_path=contracts.NS_SETTINGS_PATH, freeze_sha_of=None, **runner_kw):
    """The Runner `hc study run` builds (the live tree's code_sha / settings_sha), for its key_of and find_stale.
    Constructing it takes no lock and writes nothing. `freeze_sha_of` (replay.freeze_sha_of(...)) is what the
    orchestrator (H-305) and `hc search` pass so the keys carry the frozen day's sha and both agree on what is done;
    without it the Runner's default ('' for every day) is used. `runner_kw` go to study.Runner (budget_s, log,
    on_stale, guards, ...)."""
    return st.Runner(sd, study, settings=settings, settings_path=settings_path, freeze_sha_of=freeze_sha_of,
                     **runner_kw)


def run_search(args, out=None):
    """`hc search`: print the plan (text or --json); with --write-round also write round.json. Exit 0; a broken
    study raises StudyError (cli.py maps it to exit 3)."""
    write = out or (lambda text: sys.stdout.write(str(text) + '\n'))
    sd = StudyDir(st.resolve_study_dir(args.name, getattr(args, 'studies_dir', st.STUDIES_DIR)))
    settings = spc.load_settings(getattr(args, 'settings', contracts.NS_SETTINGS_PATH))
    study = sd.load_study(settings)
    trials = sd.trials()
    from hindcast import replay
    runner = runner_for(sd, study, settings, getattr(args, 'settings', contracts.NS_SETTINGS_PATH),
                        freeze_sha_of=replay.freeze_sha_of(study.source or 'ns:', settings,
                                                           getattr(args, 'frozen_dir', None)))
    key_of = runner.key_of
    stale_reasons, _ = runner.find_stale(trials)
    p = plan(study, trials, settings=settings, key_of=key_of)
    batch = (next_batch(study, trials, settings=settings, key_of=key_of, batch_size=args.batch)
             if getattr(args, 'batch', None) else [])
    written = None
    if getattr(args, 'write_round', False):
        index = p.current.index if p.current is not None else max(
            (pp.index for pp in p.phases if not pp.skipped), default=None)
        if index is not None:
            rnd = round_for_phase(study, trials, index, round_no=len(study.phases), settings=settings, key_of=key_of)
            written = write_round(sd, rnd)
    if getattr(args, 'json', False):
        data = p.to_dict()
        data['batch'] = [{'params': t.params, 'day': t.day, 'phase': t.phase, 'control': t.control} for t in batch]
        data['stale_context'] = list(stale_reasons)
        if written:
            data['round_json'] = written
        write(json.dumps(data, indent=1, ensure_ascii=False))
        return EXIT_OK
    write(plan_text(p, study).rstrip('\n'))
    for line in stale_reasons:
        write(f'stale context: {line}')
    if batch:
        write(f'next {len(batch)} task(s):')
        for t in batch:
            label = 'base' if not t.params else spc.variant_name(t.params, list(study.space))
            write(f'  {t.day}  {label}' + ('  [base]' if t.control == 'base' else ''))
    if written:
        write(f'round.json written: {written}')
    write('')
    write(HONESTY_STATEMENT)
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.search', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StudyError as e:
        sys.stderr.write(f'refused (exit {StudyError.exit_code}): {e}\n')
        return StudyError.exit_code


__all__ = ['DEAD_FACTOR', 'DEAD_COMPONENTS', 'DEFAULT_LEVELS', 'DEFAULT_LHS_N', 'DEFAULT_SEED', 'SCREENING_DAYS',
           'SearchError', 'RowLookup', 'PhasePlan', 'SearchPlan', 'search_days', 'screening_days', 'phase_days',
           'validation_days', 'validate_points', 'VALIDATE_PHASE', 'SEARCH_KINDS',
           'lhs_design', 'lhs_points', 'oaat_points', 'grid_points', 'phase_points', 'phase_tasks', 'dead_parameters',
           'plan', 'next_batch', 'untried_keys', 'BASE_RETRY_LIMIT', 'round_for_phase', 'write_round', 'plan_text', 'register', 'runner_for', 'run_search']


if __name__ == '__main__':
    sys.exit(main())
