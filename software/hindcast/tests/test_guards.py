"""Tests for the guards of hindcast/study.py (backlog H-302): the sentinel (phone values as explicit VDF rows, with
profile_history at each day) and the null control must equal base to the loop, else exit 2 with the differing loop
in status.json; unequal loop counts stop the run; code_sha / settings_sha / freeze_sha on every row and the
stale_context handling (--on-stale stop: nothing replayed, exit 2; reeval: stale rows appended, base first; a change
during a run: the round finishes, then exit 2).

The offline tests use synthetic emulator CSVs and a COPY of ns_settings.json (never the real file) and a temporary
frozen/ meta; the replay test at the end needs the frozen day 2026-09-06 and --run-slow."""
import json
import os
import shutil

import pytest

from hindcast import contracts as c
from hindcast import freeze, lint
from hindcast import round as rnd
from hindcast import study as st
from hindcast.contracts import Phase
from hindcast.tests.conftest import NS_SETTINGS, SOFTWARE_DIR

DAY, DAY2 = '2026-09-06', '2026-09-05'
SPACE = {'profile autoISF_max': {'type': 'float', 'low': 1.0, 'high': 2.0, 'step': 0.25},
         'profile dura_ISF_weight': {'type': 'float', 'low': 0.5, 'high': 0.9, 'step': 0.1}}
LOOPS = 6
T0 = 1788674445                                               # 2026-09-06T06:00:45Z

pytestmark = pytest.mark.skipif(not os.path.isfile(NS_SETTINGS), reason='ns_settings.json missing')


# ----------------------------------------------------------------------------- helpers

def write_csv(path, n=LOOPS, change_at=None, day=DAY):
    """A minimal emulator CSV: 3 header lines, n loop rows, the Totals row. `change_at` alters one loop's SMB."""
    t0 = T0 + (0 if day == DAY else -86400)
    lines = ['  ;    ; ;     ; Ins.; ',
             '  ; UTC; ; UNIX; Req.; SMB',
             'id;time;Z; time; emul; emul']
    for i in range(n):
        t = t0 + 60 * i
        hh, mm, ss = (t // 3600) % 24, (t // 60) % 60, t % 60
        smb = 0.1 * (i % 3) + (0.25 if i == change_at else 0.0)
        lines.append(f'{i:3d};{hh:02d}:{mm:02d}:{ss:02d};Z; {t}.3; {0.05 * i:.2f}; {smb:.2f}')
    lines.append(';Totals:;;;;')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return path


@pytest.fixture
def env(tmp_path):
    """A study over a copy of ns_settings.json, a temporary frozen/ meta per day, fake evaluators."""
    settings_path = str(tmp_path / 'ns_settings.json')
    shutil.copyfile(NS_SETTINGS, settings_path)
    with open(settings_path, encoding='utf-8') as fh:
        settings = json.load(fh)
    frozen_dir = str(tmp_path / 'frozen')
    from hindcast import days as dy
    host = dy.host_of('ns:', settings)
    for d in (DAY, DAY2):
        meta = freeze.meta_path(d, host, frozen_dir)
        os.makedirs(os.path.dirname(meta), exist_ok=True)
        with open(meta, 'w', encoding='utf-8') as fh:
            json.dump({'day': d, 'loops': LOOPS, 'sha1': 'a' * 39 + d[-1]}, fh)

    def freeze_sha_of(day):
        m = freeze.read_meta(day, host, frozen_dir)
        return (m or {}).get('sha1', '')

    class Env:
        pass
    e = Env()
    e.tmp, e.settings_path, e.settings, e.frozen_dir, e.host = tmp_path, settings_path, settings, frozen_dir, host
    e.freeze_sha_of = freeze_sha_of
    e.calls = []
    e.base_dir = str(tmp_path / 'runs' / 'base')
    e.ctrl_dir = str(tmp_path / 'runs' / 'controls')
    e.leak = {}                    # control kind -> loop index to alter
    e.loops = {}                   # vhash -> loop count override (variants)
    e.ctrl_status = {}             # control kind -> status override

    def evaluate(task):
        e.calls.append(task)
        if st.is_base_task(task):
            write_csv(os.path.join(e.base_dir, f'{task.day}.csv'), day=task.day)
        n = e.loops.get(c.vhash(task.params), LOOPS)
        return {'metrics': {'loops': n, 'changed_loops': 1, 'agreement_pct': 98.0}, 'seconds': 0.01}

    def control_evaluate(task):
        e.calls.append(task)
        status = e.ctrl_status.get(task.control, 'done')
        if status != 'done':
            return {'status': status, 'error': 'SYNTAX in the VDF' if status == 'failed' else 'not frozen'}
        path = os.path.join(e.ctrl_dir, f'{task.day}.{task.control}.{task.meta["instance"].replace(":", "_")}.csv')
        write_csv(path, change_at=e.leak.get(task.control), day=task.day)
        return {'metrics': {'loops': LOOPS, 'changed_loops': 1}, 'seconds': 0.01, 'csv': path}

    def base_csv_of(day):
        p = os.path.join(e.base_dir, f'{day}.csv')
        return p if os.path.isfile(p) else None

    e.evaluate, e.control_evaluate, e.base_csv_of = evaluate, control_evaluate, base_csv_of

    def make_study(name='g1', days=(DAY,), phases=None, **kw):
        return st.new_study(name, list(days), SPACE, phases=phases or [Phase(kind='grid')], settings=settings,
                            settings_path=settings_path, studies_dir=str(tmp_path), **kw)

    def guards(study, **kw):
        kw.setdefault('sentinel_every', 3)
        return st.Guards(study, settings=json.loads(json.dumps(e.settings)), control_evaluate=control_evaluate,
                         base_csv_of=base_csv_of, **kw)

    def runner(sd, study=None, **kw):
        return st.Runner(sd, study, settings=settings, settings_path=settings_path, freeze_sha_of=freeze_sha_of,
                         min_disk_mb=0, **kw)

    e.make_study, e.guards, e.runner = make_study, guards, runner
    return e


def tasks_for(study, days=(DAY,), points=None, round_=1):
    points = points if points is not None else [{'profile autoISF_max': v} for v in (1.0, 1.25, 1.5, 1.75)]
    out = [st.Task(params={}, day=d, phase='floor', control='base', round=round_) for d in days]
    out += [st.Task(params=p, day=d, phase='grid', round=round_) for p in points for d in days]
    return out


def status_json(sd):
    with open(sd.status_path, encoding='utf-8') as fh:
        return json.load(fh)


def edit_settings(e, section, key, value):
    with open(e.settings_path, encoding='utf-8') as fh:
        data = json.load(fh)
    data[section][key] = value
    with open(e.settings_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)


# ----------------------------------------------------------------------------- sentinel parameters

def test_sentinel_params_rewind_profile_history_and_skip_in_day_changes(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    params, skipped = st.sentinel_params(study, env.settings, DAY)
    # autoISF_max went 1.3 -> 1.5 on 2026-09-07 20:23 local: on 2026-09-06 the phone ran 1.3 (off the 0.25 grid)
    assert params == {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': env.settings['autoisf']['dura_ISF_weight']}
    assert skipped == []
    p7, s7 = st.sentinel_params(study, env.settings, '2026-09-07')
    assert 'profile autoISF_max' not in p7 and any('inside 2026-09-07' in s for s in s7)
    assert st.sentinel_params(study, env.settings, '2026-09-08')[0]['profile autoISF_max'] == 1.5


def test_controls_keep_phone_values_and_have_their_own_keys(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    g = env.guards(study)
    r = env.runner(sd, study)
    s1 = g.sentinel_task(DAY, 'grid', 1, 'r1:s0')
    s2 = g.sentinel_task(DAY, 'grid', 1, 'r1:s1')
    assert r.params_of(s1)['profile autoISF_max'] == 1.3                      # not snapped to 1.25
    plain = st.Task(params=dict(s1.params), day=DAY)
    assert r.params_of(plain)['profile autoISF_max'] == 1.25
    keys = {r.key_of(s1), r.key_of(s2), r.key_of(st.Task(params=dict(s1.params), day=DAY, control=None, variant='x'))}
    assert len(keys) == 3
    n = g.null_task(DAY, 'grid', 1, 'r1:null')
    assert n.control == 'null' and len(n.params) == 1
    assert g.null_task(DAY, 'grid', 2, 'r2:null').params != n.params           # rotates with the round


def test_schedule_inserts_floor_phase_start_periodic_sentinels_and_one_null(env):
    sd = env.make_study(days=(DAY, DAY2))
    study = sd.load_study(env.settings)
    g = env.guards(study, sentinel_every=3)
    sched = g.schedule(tasks_for(study, (DAY, DAY2)), 'r1')
    kinds = [(t.control or 'v', t.day) for t in sched]
    assert kinds[:5] == [('base', DAY), ('sentinel', DAY), ('null', DAY), ('base', DAY2), ('sentinel', DAY2)]
    assert sum(1 for t in sched if t.control == 'null') == 1
    # phase start: the first grid task is on DAY, the previous control was on DAY2 -> a sentinel before it
    first_grid = next(i for i, t in enumerate(sched) if t.phase == 'grid' and not t.control)
    assert sched[first_grid - 1].control == 'sentinel' and sched[first_grid - 1].day == DAY
    # every 3 variant tasks another sentinel
    variants_between = 0
    for t in sched[first_grid:]:
        if t.control == 'sentinel':
            assert variants_between == 3
            variants_between = 0
        elif not t.control:
            variants_between += 1
    assert len({t.meta['instance'] for t in sched if st.is_control(t)}) == sum(1 for t in sched if st.is_control(t))


# ----------------------------------------------------------------------------- controls in a run

def test_controls_equal_base_run_finishes(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    res = env.runner(sd, study, guards=env.guards(study)).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 0, res.stop_reason
    rows = sd.trials()
    ctl = [r for r in rows if r.control in ('sentinel', 'null')]
    assert res.controls == len(ctl) >= 3 and all('equals_base' in r.flags for r in ctl)
    assert all(r.status == 'done' for r in ctl)
    assert not os.listdir(env.ctrl_dir)                                        # matching control CSVs are removed
    assert not [r for r in rnd.latest_done_rows(rows) if r.control in ('sentinel', 'null')]
    for r in rows:                                                             # the context on every row
        assert r.code_sha == c.code_sha() and r.settings_sha == c.settings_sha(env.settings_path)
        assert r.freeze_sha == env.freeze_sha_of(r.day)


@pytest.mark.parametrize('kind', ['sentinel', 'null'])
def test_control_differing_from_base_stops_with_the_loop_in_status(env, kind):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    env.leak[kind] = 2
    res = env.runner(sd, study, guards=env.guards(study)).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 2
    s = status_json(sd)
    assert s['exit_code'] == 2
    reason = s['stop_reason']
    assert f'{kind} differs from base on {DAY} at loop 2 (2026-09-06T06:02:45Z)' in reason
    assert 'SMB emul' in reason and 'loops differ' not in reason          # counts and values: the row's error
    assert lint.lint_findings(reason, 'line') == []
    bad = [r for r in sd.trials() if r.control == kind][-1]
    assert reason.startswith('guard: ') and 'differs_from_base' in bad.flags and bad.error.split(': ')[0] in reason
    assert os.listdir(env.ctrl_dir)                                            # the evidence is kept
    assert not any(t.phase == 'grid' and not t.control for t in env.calls)     # stopped before the variants
    assert len(c.Status.load(sd.status_path).to_compact_json().encode()) <= c.STATUS_MAX_BYTES


def test_failed_control_stops_blocked_control_does_not(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    env.ctrl_status['sentinel'] = 'blocked'
    res = env.runner(sd, study, guards=env.guards(study)).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 0
    assert all('guard_unchecked: blocked' in r.flags for r in sd.trials() if r.control == 'sentinel')
    sd2 = env.make_study('g2')
    study2 = sd2.load_study(env.settings)
    env.ctrl_status['sentinel'] = 'failed'
    res = env.runner(sd2, study2, guards=env.guards(study2)).run(tasks_for(study2), env.evaluate)
    assert res.exit_code == 2 and 'sentinel failed on 2026-09-06 while the base replay of the day ran' in res.stop_reason


def test_control_without_base_csv_is_unchecked(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    g = env.guards(study)
    g.base_csv_of = lambda day: None
    res = env.runner(sd, study, guards=g).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 0
    assert all(any(f.startswith('guard_unchecked') for f in r.flags) for r in sd.trials() if st.is_control(r))


def test_unequal_loop_counts_stop_the_run(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    env.loops[c.vhash({'profile autoISF_max': 1.25})] = LOOPS - 1
    res = env.runner(sd, study).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 2
    assert f'loop counts differ between variants on {DAY}' in status_json(sd)['stop_reason']
    assert lint.lint_findings(res.stop_reason, 'line') == []
    bad = [r for r in sd.trials() if 'unequal_loops' in r.flags]
    assert len(bad) == 1 and f'unequal loop counts on {DAY}: base {LOOPS}, autoISF_max_1.25 {LOOPS - 1}' in bad[0].error


def test_first_loop_difference(tmp_path):
    a = write_csv(str(tmp_path / 'a.csv'))
    assert st.first_loop_difference(a, write_csv(str(tmp_path / 'b.csv'))) is None
    d = st.first_loop_difference(write_csv(str(tmp_path / 'c.csv'), change_at=4), a)
    assert d['loop'] == 4 and d['time'] == '2026-09-06T06:04:45Z' and d['columns'][0][0] == 'SMB emul'
    d = st.first_loop_difference(write_csv(str(tmp_path / 'e.csv'), n=LOOPS - 1), a)
    assert d['loop'] == LOOPS - 1 and d['loops'] == (LOOPS - 1, LOOPS)
    assert 'has 5 loops, base 6' in st.guard_text('sentinel', DAY, d, detail=True)
    assert 'loop counts of sentinel and base differ' in st.guard_text('sentinel', DAY, d)
    assert lint.lint_findings(st.guard_text('sentinel', DAY, d), 'issue') == []


# ----------------------------------------------------------------------------- stale_context between rounds

def test_settings_change_between_rounds_stop_then_reeval(env):
    sd = env.make_study(days=(DAY, DAY2))
    study = sd.load_study(env.settings)
    r1 = env.runner(sd, study).run(tasks_for(study, (DAY, DAY2), round_=1), env.evaluate)
    assert r1.exit_code == 0 and r1.done == 10
    edit_settings(env, 'smb', 'smb_delivery_ratio', 0.61)                      # the copy, between rounds

    env.calls.clear()
    r2 = env.runner(sd, study).run(tasks_for(study, (DAY, DAY2), round_=2), env.evaluate)
    assert r2.exit_code == 2 and env.calls == []                               # stop: nothing replayed
    s = status_json(sd)
    assert any('settings_sha changed' in x and '(10 done rows)' in x for x in s['stale_context'])
    assert 'reeval' in s['stop_reason'] and lint.lint_findings(s['stop_reason'], 'line') == []

    r3 = env.runner(sd, study, on_stale='reeval').run(tasks_for(study, (DAY, DAY2), round_=2), env.evaluate)
    assert r3.exit_code == 0 and r3.marked_stale == 10
    assert [t.control for t in env.calls[:2]] == ['base', 'base']              # base first
    latest = sd.latest_by_key(sd.trials())
    old = [r for r in latest.values() if r.settings_sha == study.settings_sha]
    assert len(old) == 10 and all(r.status == 'stale' for r in old)
    assert not [r for r in rnd.latest_done_rows(sd.trials()) if r.settings_sha == study.settings_sha]
    assert any('re-evaluating' in x for x in status_json(sd)['stale_context'])

    env.calls.clear()                                                          # a later run is not stale again
    r4 = env.runner(sd, study).run(tasks_for(study, (DAY, DAY2), round_=3), env.evaluate)
    assert r4.exit_code == 0 and r4.done == 0 and not status_json(sd)['stale_context']


def test_frozen_meta_change_between_rounds_marks_only_that_day(env):
    sd = env.make_study(days=(DAY, DAY2))
    study = sd.load_study(env.settings)
    assert env.runner(sd, study).run(tasks_for(study, (DAY, DAY2)), env.evaluate).exit_code == 0
    meta = freeze.meta_path(DAY, env.host, env.frozen_dir)
    with open(meta, encoding='utf-8') as fh:
        m = json.load(fh)
    m['sha1'] = 'b' * 40                                                        # the day was re-frozen
    with open(meta, 'w', encoding='utf-8') as fh:
        json.dump(m, fh)
    r2 = env.runner(sd, study).run(tasks_for(study, (DAY, DAY2)), env.evaluate)
    assert r2.exit_code == 2
    assert any(f'freeze_sha of {DAY} changed' in x and '(5 done rows)' in x for x in status_json(sd)['stale_context'])
    env.calls.clear()
    r3 = env.runner(sd, study, on_stale='reeval').run(tasks_for(study, (DAY, DAY2)), env.evaluate)
    assert r3.exit_code == 0 and r3.marked_stale == 5
    assert env.calls[0].control == 'base' and env.calls[0].day == DAY
    assert {t.day for t in env.calls} == {DAY}                                 # DAY2's rows are still valid
    new = [r for r in sd.trials() if r.day == DAY and r.status == 'done' and r.trial >= 10]
    assert new and all(r.freeze_sha == 'b' * 40 for r in new)


def test_reeval_runs_the_base_of_a_stale_day_even_when_the_round_does_not_ask(env):
    sd = env.make_study(days=(DAY, DAY2))
    study = sd.load_study(env.settings)
    env.runner(sd, study).run(tasks_for(study, (DAY, DAY2)), env.evaluate)
    edit_settings(env, 'smb', 'smb_delivery_ratio', 0.62)
    env.calls.clear()
    only = [st.Task(params={'profile autoISF_max': 2.0}, day=DAY, phase='grid', round=2)]
    res = env.runner(sd, study, on_stale='reeval').run(only, env.evaluate)
    assert res.exit_code == 0
    assert [(t.control, t.day) for t in env.calls] == [('base', DAY2), ('base', DAY), (None, DAY)]


def test_code_change_between_rounds(env, tmp_path):
    code = tmp_path / 'code'
    code.mkdir()
    names = ['determine_basal.py', 'emulator_core.py'] + [n for n in os.listdir(SOFTWARE_DIR)
                                                         if n.startswith('ns_') and n.endswith('.py')]
    for n in names:
        shutil.copyfile(os.path.join(SOFTWARE_DIR, n), code / n)
    sd = env.make_study(software_dir=str(code))
    study = sd.load_study(env.settings)
    assert study.code_sha == c.code_sha(str(code))
    assert env.runner(sd, study, software_dir=str(code)).run(tasks_for(study), env.evaluate).exit_code == 0
    with open(code / 'determine_basal.py', 'a', encoding='utf-8') as fh:
        fh.write('\n# a core fix\n')
    res = env.runner(sd, study, software_dir=str(code)).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 2 and any('code_sha changed' in x for x in res.stale_context)


def test_no_rows_compares_with_study_json(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    edit_settings(env, 'smb', 'smb_delivery_ratio', 0.63)
    res = env.runner(sd, study).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 2 and 'settings_sha changed since study.json' in res.stop_reason
    res = env.runner(sd, study, on_stale='reeval').run(tasks_for(study), env.evaluate)
    assert res.exit_code == 0 and res.marked_stale == 0


def test_change_during_the_run_finishes_the_round_then_exits_2(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    tasks = tasks_for(study)

    def evaluate(task):
        out = env.evaluate(task)
        if len(env.calls) == 2:
            edit_settings(env, 'smb', 'smb_delivery_ratio', 0.64)
        return out
    res = env.runner(sd, study).run(tasks, evaluate)
    assert res.exit_code == 2 and res.done == len(tasks)                       # the round was finished
    assert 'changed during the run' in res.stop_reason and 'finished' in res.stop_reason
    rows = sd.trials()
    assert len({r.settings_sha for r in rows}) == 1                            # one context per round
    res2 = env.runner(sd, study).run(tasks, env.evaluate)                     # the next run: stale, stop
    assert res2.exit_code == 2 and any('(5 done rows)' in x for x in res2.stale_context)


def test_bad_on_stale_is_refused(env):
    sd = env.make_study()
    with pytest.raises(st.StudyError):
        env.runner(sd, on_stale='ignore')


# ----------------------------------------------------------------------------- the real replay (frozen 2026-09-06)

def _frozen_ok(day=DAY):
    try:
        return freeze.blocked_reason(day) is None
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _frozen_ok(), reason=f'{DAY} not frozen (or stale): run `hc freeze {DAY}`')
def test_real_sentinel_and_null_equal_base_and_a_wrong_sentinel_stops(tmp_path):
    from hindcast import replay
    from hindcast import space as spc
    settings = spc.load_settings(NS_SETTINGS)
    window = ('2026-09-06T06:00:00Z', '2026-09-06T07:00:00Z')
    sd = st.new_study('real', [DAY], SPACE, phases=[Phase(kind='grid')], settings=settings, studies_dir=str(tmp_path))
    study = sd.load_study(settings)
    ctx = replay.ReplayContext.from_study(sd, study, settings, window=window)
    runner = st.Runner(sd, study, settings=settings, freeze_sha_of=replay.freeze_sha_of(settings=settings),
                       min_disk_mb=0)
    ctx.key_of = runner.key_of
    guards = st.Guards.from_context(ctx, study, settings, sentinel_every=25)
    tasks = [st.Task(params={}, day=DAY, phase='floor', control='base', round=1),
             st.Task(params={'profile autoISF_max': 1.5}, day=DAY, phase='grid', round=1)]
    res = runner.run(tasks, ctx.evaluate, guards=guards)
    assert res.exit_code == 0, res.stop_reason
    ctl = [r for r in sd.trials() if st.is_control(r)]
    assert {r.control for r in ctl} == {'sentinel', 'null'}
    assert all('equals_base' in r.flags and r.metrics['loops'] == 59 for r in ctl)

    wrong = json.loads(json.dumps(settings))                                   # the guard's own copy only
    wrong['autoisf']['dura_ISF_weight'] = 3.0
    wrong['autoisf']['autoISF_max'] = 3.0
    g2 = st.Guards.from_context(ctx, study, wrong, sentinel_every=25, null=False)
    res = runner.run([st.Task(params={'profile autoISF_max': 1.75}, day=DAY, phase='grid', round=2)], ctx.evaluate,
                     guards=g2)                                                 # phase start -> a sentinel first
    if res.exit_code == 0:
        pytest.skip('the altered phone values do not change a decision in 06-07Z')
    assert res.exit_code == 2 and f'sentinel differs from base on {DAY} at loop' in status_json(sd)['stop_reason']


# ----------------------------------------------------------------------------- privacy of the guard stop reasons

def _no_numbers_of(text, *numbers):
    import re
    return not any(re.search(r'(?<![\d.])%s(?![\d.])' % n, text) for n in numbers)


def test_unequal_loop_counts_keep_the_counts_out_of_the_stop_reason(env):
    """stop_reason reaches the public campaign issue (gh.py): the per-day loop counts stay in the row's error and
    guards.json, the stop reason is a neutral sentence that passes the issue lint."""
    sd = env.make_study()
    study = sd.load_study(env.settings)
    env.loops[c.BASE_VHASH] = env.loops[c.vhash({'profile autoISF_max': 1.0})] = 1311
    env.loops[c.vhash({'profile autoISF_max': 1.25})] = 1310
    res = env.runner(sd, study).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 2
    for reason in (res.stop_reason, status_json(sd)['stop_reason'], sd.read_state().stop_reason):
        assert f'loop counts differ between variants on {DAY}' in reason
        lint.lint_text(reason, kind='issue')
        assert _no_numbers_of(reason, 1311, 1310), reason
    bad = [r for r in sd.trials() if r.vhash == c.vhash({'profile autoISF_max': 1.25})][-1]
    assert 'unequal_loops' in bad.flags and 'base 1311' in bad.error and '1310' in bad.error
    g = sd.read_guards()
    assert g['loop_counts'][-1]['day'] == DAY
    assert (g['loop_counts'][-1]['base'], g['loop_counts'][-1]['loops']) == (1311, 1310)


@pytest.mark.parametrize('kind', ['sentinel', 'null'])
def test_control_stop_reason_names_columns_not_counts_or_values(env, kind):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    env.leak[kind] = 2
    res = env.runner(sd, study, guards=env.guards(study)).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 2
    reason = status_json(sd)['stop_reason']
    lint.lint_text(reason, kind='issue')
    assert 'SMB emul' in reason and 'loops differ' not in reason and ' vs base ' not in reason
    bad = [r for r in sd.trials() if r.control == kind][-1]
    assert '1 of 6 loops differ' in bad.error and 'SMB emul: 0.45 vs base 0.2' in bad.error


# ----------------------------------------------------------------------------- an unschedulable sentinel is recorded

def test_sentinel_not_schedulable_is_recorded_in_guards_json_rows_and_status(env):
    sd = env.make_study()
    study = sd.load_study(env.settings)
    bare = json.loads(json.dumps(env.settings))
    for name in study.space:                                                   # no searched value in the settings
        section, key = None, name.split()[-1]
        for sec, vals in bare.items():
            if isinstance(vals, dict) and key in vals:
                section = sec
        if section:
            del bare[section][key]
    g = st.Guards(study, settings=bare, control_evaluate=env.control_evaluate, base_csv_of=env.base_csv_of)
    assert st.sentinel_params(study, bare, DAY)[0] == {}
    res = env.runner(sd, study, guards=g).run(tasks_for(study), env.evaluate)
    assert res.exit_code == 0 and res.controls == 0
    assert res.guards and res.guards[0].startswith(f'sentinel: not schedulable on {DAY}')
    gj = sd.read_guards()
    assert gj['sentinel']['schedulable'] is False
    reasons = gj['sentinel']['days'][DAY]
    assert len(reasons) == 2 and all('not in ns_settings.json' in r for r in reasons)
    rows = sd.trials()
    assert rows and all('sentinel_not_schedulable' in r.flags for r in rows if r.day == DAY)
    status = st.build_status(sd)
    text = st.status_text(status)
    assert f'guards: sentinel: not schedulable on {DAY}' in text and 'autoISF_max: not in ns_settings.json' in text
    assert lint.lint_findings(text, 'line') == []
    c.Status.load(sd.status_path)                                              # status.json keeps its contract

    # the next run with a schedulable sentinel clears the note
    sd2 = env.make_study('g2')
    study2 = sd2.load_study(env.settings)
    res2 = env.runner(sd2, study2, guards=env.guards(study2)).run(tasks_for(study2), env.evaluate)
    assert res2.exit_code == 0 and res2.guards == []
    assert sd2.read_guards().get('sentinel', {}).get('schedulable', True) is True
    assert 'not schedulable' not in st.status_text(st.build_status(sd2))


# ----------------------------------------------------------------------------- a real sentinel replay (frozen 2026-09-06)

@pytest.mark.slow
@pytest.mark.skipif(not _frozen_ok(), reason=f'{DAY} not frozen (or stale): run `hc freeze {DAY}`')
def test_real_sentinel_replay_reproduces_the_real_base_to_the_loop(tmp_path):
    """The core claim of the sentinel: the phone's values written as explicit VDF rows (sentinel_params, with
    profile_history rewound to the day) reproduce the base replay (no VDF rows, the profile path) loop by loop,
    every CSV column. Real replays through replay.run_trial on the frozen day, window 06-09Z, no fake evaluator."""
    from emulator_optimize import read_result_csv
    from hindcast import replay
    from hindcast import space as spc
    settings = spc.load_settings(NS_SETTINGS)
    window = ('2026-09-06T06:00:00Z', '2026-09-06T09:00:00Z')
    sd = st.new_study('real_sentinel', [DAY], SPACE, phases=[Phase(kind='grid')], settings=settings,
                      studies_dir=str(tmp_path))
    study = sd.load_study(settings)
    params, skipped = st.sentinel_params(study, settings, DAY)
    assert skipped == [] and set(params) == set(study.space)
    assert params['profile autoISF_max'] == 1.3                    # the rewound phone value, off the 0.25 grid
    names = list(study.space)
    runs = str(tmp_path / 'runs')
    base = replay.run_trial(replay.TrialSpec(day=DAY, params={}, base_lines=list(study.base_vdf_lines), window=window,
                                             names=names, control='base', runs_dir=runs,
                                             keep_csv=str(tmp_path / 'base.csv')))
    assert base.status == 'done', base.error
    spec = replay.TrialSpec(day=DAY, params=dict(params), base_lines=list(study.base_vdf_lines), window=window,
                            names=names, control='sentinel', variant='sentinel', runs_dir=runs,
                            base_csv=base.csv, keep_csv=str(tmp_path / 'sentinel.csv'))
    rows = spec.vdf_lines()
    assert len(rows) >= len(params) and any(r.split()[:2] == ['profile', 'autoISF_max'] and '1.3' in r for r in rows)
    sent = replay.run_trial(spec)
    assert sent.status == 'done', sent.error
    assert sent.csv and base.csv and sent.csv != base.csv

    names_b, rows_b = read_result_csv(base.csv)
    names_s, rows_s = read_result_csv(sent.csv)
    assert names_s == names_b and len(rows_b) > 20                  # a real window, not an empty replay
    assert len(rows_s) == len(rows_b) == base.metrics['loops'] == sent.metrics['loops']
    for i, (rb, rs) in enumerate(zip(rows_b, rows_s)):
        assert rs == rb, f'loop {i}: ' + ', '.join(f'{n}: {rs.get(n)} vs base {rb.get(n)}' for n in names_b
                                                   if rs.get(n) != rb.get(n))
    assert st.first_loop_difference(sent.csv, base.csv) is None

    # the comparison is not vacuous: one searched value off the phone's (dura_ISF_weight 0.5) differs at once
    off = dict(params, **{'profile dura_ISF_weight': 0.5})
    assert off != params
    wrong = replay.run_trial(replay.TrialSpec(day=DAY, params=off, base_lines=list(study.base_vdf_lines), window=window,
                                              names=names, control='sentinel', variant='sentinel_off', runs_dir=runs,
                                              base_csv=base.csv, keep_csv=str(tmp_path / 'off.csv')))
    assert wrong.status == 'done', wrong.error
    diff = st.first_loop_difference(wrong.csv, base.csv)
    assert diff is not None and diff['differing_loops'] > 0
