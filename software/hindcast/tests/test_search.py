"""Tests for hindcast/search.py (backlog H-304): oaat / grid / lhs counts against emulator_optimize.combinations(),
lhs reproducible by seed, next_batch (base first, resume, settled vs retried rows, key_of), the dead-parameter
freeze after oaat with its round.json report ("not testable on these days"), later phase kinds skipped, the
vocabulary lint on every human-facing string, and the `hc search` command. Everything runs offline from synthetic
trial rows; no replay, no socket."""
import argparse
import json

import pytest

import emulator_optimize as opt
from hindcast import contracts as c
from hindcast import lint
from hindcast import replay
from hindcast import search
from hindcast import space as sp
from hindcast import study as st
from hindcast.contracts import Phase, Study, TrialRow
from hindcast.tests.conftest import NS_SETTINGS

SETTINGS = sp.load_settings(NS_SETTINGS) or {'safety': {}}
SPACE = {'profile autoISF_max': {'type': 'float', 'low': 1.3, 'high': 1.5, 'step': 0.1, 'phone': 1.5},
         'profile dura_ISF_weight': {'type': 'float', 'low': 0.5, 'high': 0.7, 'step': 0.1, 'phone': 0.7}}
DAYS = ('2026-09-05', '2026-09-06')
AMAX, DURA = 'profile autoISF_max', 'profile dura_ISF_weight'

BASE_METRICS = {'loops': 180, 'smb_emul_U': 2.7, 'smb_orig_U': 2.65, 'tbr_emul_U': 1.0, 'tbr_orig_U': 1.0,
                'delta_U': 0.05, 'rise_emul_U': 2.0, 'rise_orig_U': 1.95, 'rest_emul_U': 1.7, 'rest_orig_U': 1.7,
                'changed_loops': 3, 'agreement_pct': 98.3}


def effect_metrics(e, k=0):
    """variant metrics with a rise effect of `e` U vs base and `k` more changed loops"""
    return {'loops': 180, 'rise_vs_base_U': e, 'rest_vs_base_U': 0.0, 'delta_vs_base_U': e, 'smb_vs_base_U': e,
            'tbr_vs_base_U': 0.0, 'changed_vs_base': k}


def make_study(tmp_path, name='s', phases=None, days=DAYS, space=None, **kw):
    sd = st.new_study(name, list(days), space or SPACE, phases=phases or [Phase(kind='oaat', points=5)],
                      settings=SETTINGS, studies_dir=str(tmp_path), **kw)
    return sd, sd.load_study(SETTINGS)


def phone_point(study):
    return sp.ParamSpace.from_study(study, settings=SETTINGS, check_locked=False).phone_point(SETTINGS)


class Rows:
    """synthetic trials.jsonl rows for a study, keyed like Runner (freeze_sha '' by default; a callable
    day -> sha, e.g. replay.freeze_sha_of(...), keys them the way `hc study run` / `hc search` do on frozen days)"""

    def __init__(self, study, freeze_sha=''):
        self.study = study
        self.space = sp.ParamSpace.from_study(study, settings=SETTINGS, check_locked=False)
        self.freeze_of = freeze_sha if callable(freeze_sha) else (lambda day, sha=freeze_sha: sha)
        self.rows = []

    def add(self, params, day, metrics, status='done', control=None, phase='oaat', error=None, code_sha=None):
        params = self.space.normalize(params) if params else {}
        row = TrialRow(trial=len(self.rows), key=c.key(params, day, self.study.settings_sha, code_sha or self.study.code_sha,
                                                        self.freeze_of(day), self.study.objective_version),
                       phase=phase, vhash=c.vhash(params), variant='base' if not params else self.space.variant_name(params),
                       params=params, day=day, status=status, code_sha=code_sha or self.study.code_sha,
                       settings_sha=self.study.settings_sha, freeze_sha=self.freeze_of(day), metrics=dict(metrics or {}),
                       control=control, error=error).validate()
        self.rows.append(row)
        return row

    def base(self, days=DAYS):
        for d in days:
            self.add({}, d, BASE_METRICS, control='base', phase='floor')

    def key_of(self, task):
        params = self.space.normalize(task.params) if task.params else {}
        return c.key(params, task.day, self.study.settings_sha, self.study.code_sha, self.freeze_of(task.day),
                     self.study.objective_version)


def screen_all(rows, study, alive=0.4, dead=0.02, days=DAYS, skip=()):
    """every oaat level done on every day: autoISF_max levels move 0.4 U, dura levels 0.02 U (inside 1.5 x floor)"""
    space = rows.space
    for p in space.oaat_points(5, phone_point(study)):
        name = next(iter(p))
        for d in days:
            if (name, p[name], d) in skip:
                continue
            rows.add(p, d, effect_metrics(alive if name == AMAX else dead, 10 if name == AMAX else 0))


def assert_lint_clean(text):
    findings = lint.lint_findings(text, 'line')
    assert not findings, [str(f) for f in findings]


# ----------------------------------------------------------------------------- counts vs the optimizer

def test_grid_count_matches_optimizer_combinations(grid_autoisf, tmp_path):
    space, phase, mode = sp.load_grid_file(grid_autoisf)
    with open(grid_autoisf, encoding='utf-8') as fh:
        params = json.load(fh)['parameters']
    sd, study = make_study(tmp_path, phases=[phase], days=('2026-09-06',), space=space)
    tasks, notes = search.phase_tasks(study, study.phases[0], settings=SETTINGS)
    # the optimizer's combinations() carry the base entry; Hindcast's base task is the floor replay of the day
    assert len(tasks) == len(opt.combinations(params, 'grid')) == 16
    assert tasks[0].control == 'base' and tasks[0].params == {} and tasks[0].phase == 'floor'
    names = {sp.variant_name(t.params, list(params)) for t in tasks[1:]}
    assert names == {opt.variant_name(a, params) for a in opt.combinations(params, 'grid')[1:]}
    assert notes == ['grid: 15 point(s)']


def test_oaat_from_grid_count_matches_optimizer_one_at_a_time(grid_autoisf, tmp_path):
    space, _, _ = sp.load_grid_file(grid_autoisf)
    with open(grid_autoisf, encoding='utf-8') as fh:
        params = json.load(fh)['parameters']
    phase = Phase(kind='oaat', grid=params)
    sd, study = make_study(tmp_path, phases=[phase], days=('2026-09-06',), space=space)
    tasks, _ = search.phase_tasks(study, phase, settings=SETTINGS)
    assert len(tasks) == len(opt.combinations(params, 'one_at_a_time')) == 9
    assert all(len(t.params) == 1 for t in tasks[1:])


def test_oaat_levels_around_the_phone_value(tmp_path):
    sd, study = make_study(tmp_path)
    tasks, notes = search.phase_tasks(study, study.phases[0], settings=SETTINGS)
    points = [t.params for t in tasks if t.params]
    assert len(tasks) == 2 + 4 * 2                       # base per day + (2 + 2 levels) x 2 days
    assert all(len(p) == 1 for p in points)
    assert {AMAX: 1.5} not in points and {DURA: 0.7} not in points      # the phone's value is the centre, excluded
    assert {AMAX: 1.3} in points and {DURA: 0.5} in points
    # point-major: both days of one point before the next point
    days_of_first = [t.day for t in tasks[2:4]]
    assert days_of_first == list(DAYS) and tasks[2].params == tasks[3].params
    assert_lint_clean('\n'.join(notes))


def test_oaat_levels_count_on_a_long_grid():
    ps = sp.ParamSpace({'profile autoISF_max': {'type': 'float', 'low': 1.0, 'high': 2.0, 'step': 0.05, 'phone': 1.5}})
    for levels in (5, 6, 7):
        pts = search.oaat_points(ps, Phase(kind='oaat', points=levels), phone={'profile autoISF_max': 1.5})
        assert len(pts) in (levels - 1, levels)           # the centre is excluded when it is one of the chosen levels
        assert all(sp.on_grid(ps['profile autoISF_max'], p['profile autoISF_max']) for p in pts)


def test_floor_phase_covers_search_and_holdout_days(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='floor'), Phase(kind='oaat')], holdout=['2026-09-05'])
    tasks, _ = search.phase_tasks(study, study.phases[0], settings=SETTINGS)
    assert [(t.day, t.control) for t in tasks] == [('2026-09-05', 'base'), ('2026-09-06', 'base')]
    tasks, _ = search.phase_tasks(study, study.phases[1], settings=SETTINGS)
    assert {t.day for t in tasks} == {'2026-09-06'}      # hold-out days never enter a search phase


# ----------------------------------------------------------------------------- lhs

def tier_a():
    return sp.ParamSpace(sp._TIER_A, sp._TIER_A_CONSTRAINTS)


def test_lhs_is_reproducible_by_seed_and_legal():
    ps = tier_a()
    a, notes = search.lhs_points(ps, 24, seed=7)
    b, _ = search.lhs_points(ps, 24, seed=7)
    other, _ = search.lhs_points(ps, 24, seed=8)
    assert a == b and a != other
    assert len(a) == 24 and notes == []
    for p in a:
        assert ps.violations(p) == [], ps.violations(p)
        assert list(p) == ps.names
    assert len({c.vhash(p) for p in a}) == 24


def test_lhs_stratifies_every_parameter():
    ps = tier_a()
    pts, _ = search.lhs_points(ps, 12, seed=3)
    for name in ps.names:
        if name in ps.chained:
            continue
        distinct = {p[name] for p in pts}
        assert len(distinct) >= 6, (name, distinct)       # 12 strata over >= 11 grid values: at least half distinct


def test_lhs_design_covers_each_stratum_once():
    design = search.lhs_design(8, ['a', 'b'], seed=1)
    for name in ('a', 'b'):
        strata = sorted(int(u[name] * 8) for u in design)
        assert strata == list(range(8))


def test_lhs_shortfall_on_a_small_grid_is_reported():
    ps = sp.ParamSpace(SPACE)
    pts, notes = search.lhs_points(ps, 10, seed=1)
    assert 1 <= len(pts) <= 9                             # 3 x 3 grid points at most
    assert any('dropped' in n for n in notes)
    assert_lint_clean('\n'.join(notes))


def test_lhs_frozen_parameters_are_omitted_or_pinned():
    ps = tier_a()
    phone = {n: p.phone for n, p in ps.space.items()}
    phone.update({'profile smb_delivery_ratio': 0.2, 'profile iob_threshold_percent': 80})
    frozen = {'profile smb_delivery_ratio': 0.2, 'profile iob_threshold_percent': 80}
    pts, notes = search.lhs_points(ps, 16, seed=5, frozen=frozen, phone=phone)
    assert pts
    for p in pts:
        assert 'profile iob_threshold_percent' not in p                  # unchained: left out = the phone's own value
        assert p['profile smb_delivery_ratio'] == 0.2                     # chain member: pinned so the chain stays whole
        assert ps.satisfies(p)
    assert_lint_clean('\n'.join(notes))


def test_lhs_with_everything_frozen_is_empty():
    ps = sp.ParamSpace(SPACE)
    pts, notes = search.lhs_points(ps, 5, frozen={AMAX: 1.5, DURA: 0.7}, phone={AMAX: 1.5, DURA: 0.7})
    assert pts == [] and notes


# ----------------------------------------------------------------------------- next_batch

def test_next_batch_base_first_then_points(tmp_path):
    sd, study = make_study(tmp_path)
    batch = search.next_batch(study, [], settings=SETTINGS, round_no=1)
    assert all(isinstance(t, st.Task) for t in batch)
    assert [t.control for t in batch[:2]] == ['base', 'base'] and {t.day for t in batch[:2]} == set(DAYS)
    assert len(batch) == 10 and all(t.round == 1 for t in batch)
    assert all(t.phase == 'oaat' for t in batch[2:])
    assert search.next_batch(study, [], settings=SETTINGS, batch_size=3, round_no=1) == batch[:3]


def test_next_batch_resumes_and_distinguishes_settled_from_retried(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    rows.add({AMAX: 1.3}, DAYS[0], effect_metrics(0.4))
    rows.add({AMAX: 1.3}, DAYS[1], {}, status='failed', error='SYNTAX')
    rows.add({AMAX: 1.4}, DAYS[0], {}, status='blocked', error='day not frozen')
    rows.add({AMAX: 1.4}, DAYS[1], {}, status='stale', error='code changed')
    batch = search.next_batch(study, rows.rows, settings=SETTINGS)
    pairs = {(tuple(t.params.items()), t.day) for t in batch}
    assert all(t.control is None for t in batch)                          # base rows are done
    assert ((('profile autoISF_max', 1.3),), DAYS[0]) not in pairs       # done
    assert ((('profile autoISF_max', 1.3),), DAYS[1]) not in pairs       # failed = settled, not handed out again
    assert ((('profile autoISF_max', 1.4),), DAYS[0]) in pairs           # blocked = retried
    assert ((('profile autoISF_max', 1.4),), DAYS[1]) in pairs           # stale = retried
    assert len(batch) == 10 - 2 - 2
    strict = search.next_batch(study, rows.rows, settings=SETTINGS, retry=())
    assert len(strict) == 10 - 2 - 4


def test_next_batch_ignores_rows_of_another_code_sha_without_key_of(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    for d in DAYS:
        rows.add({}, d, BASE_METRICS, control='base', phase='floor', code_sha='0' * 40)
    batch = search.next_batch(study, rows.rows, settings=SETTINGS)
    assert sum(1 for t in batch if t.control == 'base') == 2             # stale rows do not count as done


def test_next_batch_with_key_of_follows_the_runner_rule(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study, freeze_sha='a' * 40)
    rows.base()
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of)
    assert sum(1 for t in batch if t.control == 'base') == 0
    other = Rows(study, freeze_sha='b' * 40)                               # a re-frozen day: a different key
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=other.key_of)
    assert sum(1 for t in batch if t.control == 'base') == 2


def test_runner_evaluates_a_batch_and_the_plan_moves_on(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat'), Phase(kind='grid')])
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)

    def evaluate(task):
        return {'metrics': dict(BASE_METRICS) if not task.params else effect_metrics(0.3, 5), 'seconds': 0.0}

    batch = search.next_batch(study, sd.trials(), settings=SETTINGS, key_of=runner.key_of, round_no=1)
    assert runner.run(batch, evaluate).done == 10
    p = search.plan(study, sd.trials(), settings=SETTINGS, key_of=runner.key_of)
    assert p.phases[0].complete and p.phases[0].done == 10 and p.phases[0].dead == []
    assert p.current.index == 1 and p.current.kind == 'grid'
    batch = search.next_batch(study, sd.trials(), settings=SETTINGS, key_of=runner.key_of, round_no=2)
    assert batch and all(t.control is None for t in batch)                # the base rows carry over between phases
    # the full 3 x 3 grid: a two-key grid point {max 1.3, dura 0.7} is another variant than the one-key screen
    # point {max 1.3} (which leaves dura at the phone's value of each day), so nothing is deduplicated here
    assert len(batch) == 9 * 2
    assert all(t.round == 2 for t in batch)


# ----------------------------------------------------------------------------- dead parameters

def test_dead_parameter_is_frozen_and_reported(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat'), Phase(kind='lhs', n=6, seed=2), Phase(kind='grid')])
    rows = Rows(study)
    rows.base()
    screen_all(rows, study)
    p = search.plan(study, rows.rows, settings=SETTINGS)
    oaat = p.phases[0]
    assert oaat.complete and [d.key for d in oaat.dead] == [DURA]
    assert oaat.dead[0].why.startswith(search.NOT_TESTABLE)
    assert oaat.dead[0].floor_ratio == pytest.approx(0.4, abs=1e-6)     # 0.02 U over the 0.05 U effective floor
    assert p.frozen == {DURA: 0.7}
    assert p.current.index == 1
    for pp in p.phases[1:]:
        assert pp.frozen == {DURA: 0.7}
    batch = search.next_batch(study, rows.rows, settings=SETTINGS)
    assert batch and all(set(t.params) == {AMAX} for t in batch)         # dura left out = replayed at the phone's value
    # the grid phase after it is the autoISF_max grid alone; its two oaat points are already done
    tasks, _ = search.phase_tasks(study, study.phases[2], frozen=p.frozen, settings=SETTINGS)
    assert sorted(t.params[AMAX] for t in tasks if t.params) == [1.3, 1.3, 1.4, 1.4, 1.5, 1.5]


def test_alive_parameter_is_not_frozen(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    screen_all(rows, study, alive=0.4, dead=0.3)                           # 0.3 U = 6 x floor: alive
    dead, notes = search.dead_parameters(study, rows.rows, study.phases[0], settings=SETTINGS)
    assert dead == [] and notes == []


def test_dead_test_looks_at_every_component(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    space = rows.space
    for p in space.oaat_points(5, phone_point(study)):
        for d in DAYS:                                                     # no insulin moved, but 20 loops changed
            rows.add(p, d, effect_metrics(0.0, 20 if next(iter(p)) == DURA else 0))
    dead, _ = search.dead_parameters(study, rows.rows, study.phases[0], settings=SETTINGS)
    assert [d.key for d in dead] == [AMAX]                                 # dura moved 20 loops over a 3-loop floor


def test_incomplete_screen_keeps_the_parameter_live(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    screen_all(rows, study, skip={(DURA, 0.5, DAYS[1])})
    dead, notes = search.dead_parameters(study, rows.rows, study.phases[0], settings=SETTINGS)
    assert dead == []
    assert any('screen incomplete' in n and 'dura_ISF_weight' in n for n in notes)
    p = search.plan(study, rows.rows, settings=SETTINGS)
    assert not p.phases[0].complete and p.current.index == 0 and p.frozen == {}


def test_missing_base_row_keeps_the_parameter_live(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base(days=DAYS[:1])
    screen_all(rows, study)
    dead, notes = search.dead_parameters(study, rows.rows, study.phases[0], settings=SETTINGS)
    assert dead == [] and all('no base replay' in n for n in notes)


def test_failed_level_is_judged_on_the_rest(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    screen_all(rows, study, skip={(DURA, 0.5, DAYS[1])})
    rows.add({DURA: 0.5}, DAYS[1], {}, status='failed', error='SYNTAX')
    dead, notes = search.dead_parameters(study, rows.rows, study.phases[0], settings=SETTINGS)
    assert [d.key for d in dead] == [DURA] and '1 level(s) failed' in dead[0].why


def test_rows_without_vs_base_metrics_are_not_judged(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    for p in rows.space.oaat_points(5, phone_point(study)):
        for d in DAYS:
            rows.add(p, d, {'loops': 180, 'smb_emul_U': 1.0})
    dead, notes = search.dead_parameters(study, rows.rows, study.phases[0], settings=SETTINGS)
    assert dead == [] and len(notes) == 2 and all('without the vs-base metrics' in n for n in notes)


def test_dead_parameters_of_a_non_oaat_phase_are_none(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='grid')])
    assert search.dead_parameters(study, [], study.phases[0], settings=SETTINGS) == ([], [])


# ----------------------------------------------------------------------------- round.json

def test_round_for_phase_reports_dead_parameters_and_verdicts(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat'), Phase(kind='lhs', n=4)])
    rows = Rows(study)
    rows.base()
    screen_all(rows, study)
    rnd = search.round_for_phase(study, rows.rows, 0, round_no=1, started='2026-10-16T20:00:00Z', settings=SETTINGS)
    assert rnd.phase == 'oaat' and rnd.round == 1 and rnd.trials == 8
    assert [d.key for d in rnd.dead_parameters] == [DURA]
    assert search.NOT_TESTABLE in rnd.dead_parameters[0].why
    by_variant = {v.variant: v for v in rnd.variants}
    assert by_variant['base'].verdict == 'base' and by_variant['base'].vhash == c.BASE_VHASH
    assert {v.verdict for v in rnd.variants if v.params.get(DURA)} == {'dead'}
    assert {v.verdict for v in rnd.variants if v.params.get(AMAX)} == {'candidate'}
    assert all(v.days == 2 for v in rnd.variants)
    assert set(rnd.floors) == set(DAYS) and rnd.floors[DAYS[0]]['floor_rise_U'] == pytest.approx(0.05)
    assert rnd.front and all(len(v) == 40 for v in rnd.front)
    assert 'review order, not a ranking by outcome' in rnd.notes
    for v in rnd.variants:
        assert v.effect.get('rise_vs_base_U') is not None or v.verdict == 'base'
    path = search.write_round(sd, rnd)
    assert sd.read_round() is not None and path == sd.round_path
    status = st.build_status(sd)
    assert status.dead_parameters == [DURA]
    assert_lint_clean(st.status_text(status))


def test_round_for_phase_without_rows_validates(tmp_path):
    sd, study = make_study(tmp_path)
    rnd = search.round_for_phase(study, [], 0, round_no=1, settings=SETTINGS)
    assert rnd.variants == [] and rnd.dead_parameters == [] and 'no done rows yet for this phase' in rnd.notes


def test_round_for_phase_marks_infeasible(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    screen_all(rows, study)
    m = dict(effect_metrics(0.4, 10), env_3h_max_U=5.0)                   # far outside the envelope
    rows.add({AMAX: 1.3}, DAYS[0], m)
    rnd = search.round_for_phase(study, rows.rows, 0, round_no=2, settings=SETTINGS)
    v = next(v for v in rnd.variants if v.params == {AMAX: 1.3})
    assert v.verdict == 'infeasible' and 'out of envelope' in v.why and v.feasible is False


# ----------------------------------------------------------------------------- phases, days

def test_later_phase_kinds_are_skipped_with_the_issue_number(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat'), Phase(kind='halving', rungs=[2, 'all']),
                                             Phase(kind='optuna', sampler='nsga2', n=10), Phase(kind='validate', top=3)])
    rows = Rows(study)
    rows.base()
    screen_all(rows, study)
    p = search.plan(study, rows.rows, settings=SETTINGS)
    assert [pp.skipped for pp in p.phases] == [False, True, True, True]
    assert 'H-306' in p.phases[1].notes[0] and 'H-307' in p.phases[2].notes[0] and 'H-308' in p.phases[3].notes[0]
    assert p.current is None
    assert search.next_batch(study, rows.rows, settings=SETTINGS) == []
    with pytest.raises(search.SearchError):
        search.phase_points(rows.space, Phase(kind='halving'))


def test_screening_days_cover_distinct_tag_sets(tmp_path):
    days = ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-05']
    tags = {'2026-09-01': ['workday'], '2026-09-02': ['workday'], '2026-09-03': ['weekend', 'sport'],
            '2026-09-04': ['workday'], '2026-09-05': ['sick']}
    sd, study = make_study(tmp_path, days=days, tags=tags, holdout=['2026-09-05'])
    assert search.screening_days(study) == ['2026-09-01', '2026-09-03', '2026-09-02']
    assert search.screening_days(study, n=2) == ['2026-09-01', '2026-09-03']
    assert search.phase_days(study, Phase(kind='oaat', days='screening')) == ['2026-09-01', '2026-09-03', '2026-09-02']
    assert search.phase_days(study, Phase(kind='oaat')) == days[:4]
    assert search.phase_days(study, Phase(kind='lhs', days='2026-09-02, 2026-09-04')) == ['2026-09-02', '2026-09-04']
    with pytest.raises(search.SearchError):
        search.phase_days(study, Phase(kind='lhs', days='2026-09-05'))     # a hold-out day
    with pytest.raises(search.SearchError):
        search.phase_days(study, Phase(kind='lhs', days='holdout'))


def test_phase_state_in_study_json_is_reported_not_trusted(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat', state='done'), Phase(kind='grid')])
    p = search.plan(study, [], settings=SETTINGS)
    assert p.phases[0].state == 'done' and not p.phases[0].complete and p.current.index == 0


# ----------------------------------------------------------------------------- vocabulary

def test_every_human_facing_string_passes_the_line_lint(tmp_path):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat'), Phase(kind='lhs', n=10, seed=1),
                                             Phase(kind='grid'), Phase(kind='optuna', sampler='tpe', n=5)])
    rows = Rows(study)
    rows.base()
    screen_all(rows, study, skip={(AMAX, 1.4, DAYS[1])})
    rows.add({AMAX: 1.4}, DAYS[1], {}, status='failed', error='SYNTAX')
    p = search.plan(study, rows.rows, settings=SETTINGS)
    texts = [search.plan_text(p, study)]
    for pp in p.phases:
        texts += pp.notes + [d.why for d in pp.dead]
    rnd = search.round_for_phase(study, rows.rows, 0, round_no=1, settings=SETTINGS)
    texts += rnd.notes + [v.why for v in rnd.variants] + [d.why for d in rnd.dead_parameters]
    for text in texts:
        assert_lint_clean(text)
    assert_lint_clean(search.__doc__)


# ----------------------------------------------------------------------------- hc search

def frozen_rows(study):
    """Rows keyed with the frozen days' sha1, as `hc study run` and `hc search` key them."""
    return Rows(study, freeze_sha=replay.freeze_sha_of('ns:', SETTINGS))


def test_register_and_run_search(tmp_path, capsys):
    sd, study = make_study(tmp_path, phases=[Phase(kind='oaat'), Phase(kind='lhs', n=4)])
    rows = frozen_rows(study)
    rows.base()
    screen_all(rows, study)
    for r in rows.rows:
        sd.append(r)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    search.register(sub)
    args = parser.parse_args(['search', 's', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS, '--batch', '3',
                              '--write-round'])
    assert args.func is search.run_search
    assert search.run_search(args) == 0
    out = capsys.readouterr().out
    assert 'next: phase 1 (lhs)' in out and 'next 2 task(s):' in out and 'round.json written' in out
    assert 'autoISF_max_1.5' in out                       # the one lhs point not already replayed by the screen
    assert search.NOT_TESTABLE in out and out.rstrip().endswith(c.HONESTY_STATEMENT)
    assert_lint_clean(out)
    assert sd.read_round() is not None and [d.key for d in sd.read_round().dead_parameters] == [DURA]
    args = parser.parse_args(['search', 's', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS, '--json'])
    assert search.run_search(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['current'] == 1 and data['frozen'] == {DURA: 0.7} and data['phases'][0]['complete'] is True
    assert search.main(['search', 'missing-study', '--studies-dir', str(tmp_path)]) == st.EXIT_REFUSED


def _search_json(tmp_path, capsys, *extra):
    parser = argparse.ArgumentParser()
    search.register(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['search', 's', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS, '--json',
                              *extra])
    assert search.run_search(args) == 0
    return json.loads(capsys.readouterr().out)


def test_run_search_uses_the_live_context_after_a_code_change(tmp_path, capsys):
    """study.json keeps its creation stamps; rows written after a code change (or --on-stale reeval) carry the live
    tree's stamps. `hc search` must judge them like the Runner does, not by study.json's stamps."""
    sd, study = make_study(tmp_path)
    rows = frozen_rows(study)                             # keyed with the live stamps new_study wrote
    rows.base()
    screen_all(rows, study)
    for r in rows.rows:
        sd.append(r)
    study.code_sha = '2' * 40                             # study.json from before the code change
    sd.write_study(study)
    data = _search_json(tmp_path, capsys, '--batch', '5')
    ph = data['phases'][0]
    assert ph['done'] == ph['total'] == 10 and ph['pending'] == 0 and ph['stale'] == 0 and ph['complete'] is True
    assert data['batch'] == []


def test_run_search_shows_rows_of_another_code_sha_as_stale(tmp_path, capsys):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    old = '1' * 40
    for d in DAYS:
        rows.add({}, d, BASE_METRICS, control='base', phase='floor', code_sha=old)
    rows.add({AMAX: 1.3}, DAYS[0], effect_metrics(0.4), code_sha=old)
    for r in rows.rows:
        sd.append(r)
    data = _search_json(tmp_path, capsys, '--batch', '20')
    ph = data['phases'][0]
    assert ph['done'] == 0 and ph['stale'] == 3 and ph['pending'] == 7 and ph['complete'] is False
    assert len(data['batch']) == 10 and [t['control'] for t in data['batch'][:2]] == ['base', 'base']
    assert any('code_sha changed' in line for line in data['stale_context'])
    parser = argparse.ArgumentParser()
    search.register(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['search', 's', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS])
    assert search.run_search(args) == 0
    out = capsys.readouterr().out
    assert '0 done, 0 failed, 7 pending, 3 stale of 10' in out and 'stale context: code_sha changed' in out
    assert_lint_clean(out)


def test_plan_counts_a_stale_status_row_as_stale(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.base()
    rows.add({AMAX: 1.3}, DAYS[0], effect_metrics(0.4))
    rows.add({AMAX: 1.3}, DAYS[0], {}, status='stale')                  # superseded by mark_stale (same key)
    p = search.plan(study, rows.rows, settings=SETTINGS, key_of=rows.key_of)
    assert (p.phases[0].done, p.phases[0].stale, p.phases[0].pending) == (2, 1, 7)
    assert_lint_clean(search.plan_text(p, study))


def test_run_search_keys_carry_the_freeze_sha_like_the_orchestrator(tmp_path, capsys):
    """`hc search` builds its Runner with replay.freeze_sha_of, as `hc study run` does: rows keyed without the
    frozen day's sha (another context) are stale for both, rows keyed with it are done for both."""
    sd, study = make_study(tmp_path)
    of = replay.freeze_sha_of('ns:', SETTINGS)
    assert all(of(d) for d in DAYS), 'the test days must be frozen'
    rows = Rows(study)                                    # freeze_sha '' on frozen days
    rows.base()
    screen_all(rows, study)
    for r in rows.rows:
        sd.append(r)
    ph = _search_json(tmp_path, capsys)['phases'][0]
    assert ph['done'] == 0 and ph['stale'] == 10 and ph['complete'] is False
    runner = search.runner_for(sd, study, SETTINGS, NS_SETTINGS, freeze_sha_of=of)
    p = search.plan(study, sd.trials(), settings=SETTINGS, key_of=runner.key_of)
    assert (p.phases[0].done, p.phases[0].stale) == (ph['done'], ph['stale'])      # the orchestrator's view
    # the frozen-sha rows: done for both
    sd2, study2 = make_study(tmp_path, name='s2')
    rows2 = frozen_rows(study2)
    rows2.base()
    screen_all(rows2, study2)
    for r in rows2.rows:
        sd2.append(r)
    parser = argparse.ArgumentParser()
    search.register(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['search', 's2', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS, '--json'])
    assert search.run_search(args) == 0
    ph2 = json.loads(capsys.readouterr().out)['phases'][0]
    assert ph2['done'] == ph2['total'] == 10 and ph2['complete'] is True


# ----------------------------------------------------------------------------- the base of a day comes first (H-305 fix)

def _variants(batch, day):
    return [t for t in batch if t.day == day and t.params]


def test_a_failed_base_is_retried_alone_and_its_variants_held(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    rows.add({}, DAYS[0], {}, status='failed', control='base', phase='floor', error='SYNTAX (fake)')
    rows.base(days=DAYS[1:])
    held = {}
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of, held=held)
    assert [t.day for t in batch if t.control == 'base'] == [DAYS[0]]               # retried, not settled
    assert _variants(batch, DAYS[0]) == [] and _variants(batch, DAYS[1])            # held vs handed out
    assert 'failed 1 time(s)' in held[DAYS[0]] and DAYS[1] not in held
    assert search.plan(study, rows.rows, settings=SETTINGS, key_of=rows.key_of).phases[0].complete is False
    # the variants that came back blocked because the base failed are re-planned once the base is done
    for p in [t.params for t in _variants(batch, DAYS[1])][:2]:
        rows.add(p, DAYS[0], {}, status='blocked', error='the base replay of the day did not finish')
    rows.add({}, DAYS[0], BASE_METRICS, control='base', phase='floor')
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of)
    assert len(_variants(batch, DAYS[0])) == 4 and not any(t.control == 'base' for t in batch)
    assert_lint_clean('\n'.join(held.values()))


def test_a_base_that_failed_the_retry_limit_holds_its_day(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    for _ in range(search.BASE_RETRY_LIMIT):
        rows.add({}, DAYS[0], {}, status='failed', control='base', phase='floor', error='SYNTAX (fake)')
    rows.base(days=DAYS[1:])
    screen_all(rows, study, days=DAYS[1:])
    held = {}
    assert search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of, held=held) == []
    assert f'failed {search.BASE_RETRY_LIMIT} time(s)' in held[DAYS[0]]
    assert search.plan(study, rows.rows, settings=SETTINGS, key_of=rows.key_of).phases[0].complete is False


def test_an_unfrozen_day_gets_one_blocked_base_then_nothing(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    unfrozen = {DAYS[0]: f'{DAYS[0]} is not frozen; run `hc freeze {DAYS[0]}`'}
    held = {}
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of, unfrozen=unfrozen, held=held)
    assert [t.day for t in batch if t.control == 'base'] == list(DAYS) and _variants(batch, DAYS[0]) == []
    assert 'not frozen' in held[DAYS[0]]
    rows.add({}, DAYS[0], {}, status='blocked', control='base', phase='floor', error=unfrozen[DAYS[0]])
    held = {}
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of, unfrozen=unfrozen, held=held)
    assert not [t for t in batch if t.day == DAYS[0]] and 'not frozen' in held[DAYS[0]]
    # frozen now: the blocked base is handed out again (alone), then the variants
    batch = search.next_batch(study, rows.rows, settings=SETTINGS, key_of=rows.key_of, unfrozen={})
    assert [t.day for t in batch if t.control == 'base'] == list(DAYS) and _variants(batch, DAYS[0]) == []


def test_untried_keys_counts_what_is_left(tmp_path):
    sd, study = make_study(tmp_path)
    rows = Rows(study)
    assert len(search.untried_keys(study, rows.rows, settings=SETTINGS, key_of=rows.key_of)) == 10
    rows.base()
    rows.add({AMAX: 1.3}, DAYS[0], {}, status='failed', error='SYNTAX (fake)')
    assert len(search.untried_keys(study, rows.rows, settings=SETTINGS, key_of=rows.key_of)) == 7
