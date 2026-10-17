"""
test_pool.py  -  the spawn worker pool (backlog H-103)

The pool mechanics are tested with toy run functions (module-level, so a spawned child can import them by name)
and no emulator initialiser. The real 2 workers x 4 trials vs serial check needs the frozen day 2026-09-06 and
skips without it; the 100-trial memory watch is marked slow.
"""
import os
import time

import pytest

from hindcast import freeze, pool, replay
from hindcast.study import Task

DAY = '2026-09-06'
WINDOW = ('2026-09-06T06:00:00Z', '2026-09-06T09:00:00Z')
SHORT = ('2026-09-06T06:00:00Z', '2026-09-06T06:30:00Z')


def _frozen_ok(day=DAY):
    try:
        return freeze.blocked_reason(day) is None
    except Exception:
        return False


needs_frozen = pytest.mark.skipif(not _frozen_ok(), reason=f'{DAY} not frozen (or stale): run `hc freeze {DAY}`')


# ----------------------------------------------------------------------------- toy run functions (picklable by name)

def toy_run(spec, worker=None):
    """{'n': ...} spec -> a TrialResult that carries the worker's pid, niceness and the spec number."""
    if spec.get('boom'):
        raise ValueError(f"toy failure {spec['n']}")
    return replay.TrialResult(status='done', day=DAY, vhash=str(spec['n']), variant=f"v{spec['n']}",
                              metrics={'n': spec['n'], 'nice': os.nice(0)}, worker=worker, pid=os.getpid())


def toy_run_no_worker_kw(spec):
    return replay.TrialResult(status='done', day=DAY, vhash=str(spec['n']), variant='x', metrics={'n': spec['n']})


# ----------------------------------------------------------------------------- mechanics

def test_worker_count_caps():
    assert pool.worker_count(4, cpu=18) == 4
    assert pool.worker_count(32, cpu=8) == 7
    assert pool.worker_count(4, cpu=1) == 1
    assert pool.worker_count(0, cpu=8) == 1
    assert pool.worker_count(None, cpu=3) == 2


def test_serial_twin_turns_exceptions_into_one_failed_row():
    specs = [{'n': 0}, {'n': 1, 'boom': True, 'day': DAY, 'variant': 'v1'}, {'n': 2}]
    out = list(pool.run_serial(specs, run=toy_run))
    assert [r.status for r in out] == ['done', 'failed', 'done']
    assert 'worker exception ValueError: toy failure 1' in out[1].error and out[1].variant == 'v1'
    assert [r.metrics['n'] for r in out if r.status == 'done'] == [0, 2]
    assert [r.metrics['n'] for r in pool.run_serial([{'n': 5}], run=toy_run_no_worker_kw)] == [5]


def test_spawn_pool_streams_results_nice_and_failures():
    specs = [{'n': i, 'boom': i == 3} for i in range(6)]
    with pool.WorkerPool(workers=2, run=toy_run, init=None, nice=5) as wp:
        assert wp.workers == 2
        got = list(wp.run(specs))
    assert len(got) == 6
    failed = [r for r in got if r.status == 'failed']
    assert len(failed) == 1 and 'toy failure 3' in failed[0].error
    done = [r for r in got if r.status == 'done']
    assert sorted(r.metrics['n'] for r in done) == [0, 1, 2, 4, 5]
    assert all(r.pid != os.getpid() for r in done)                 # ran in the children
    assert all(r.worker in (1, 2, 3, 4) for r in done)
    assert all(r.metrics['nice'] >= os.nice(0) + 5 or r.metrics['nice'] >= 19 for r in done)


def test_max_tasks_per_child_replaces_the_worker():
    with pool.WorkerPool(workers=1, run=toy_run, init=None, nice=0, max_tasks_per_child=1) as wp:
        pids = {r.pid for r in wp.run([{'n': i} for i in range(3)])}
    assert len(pids) == 3


def test_map_keeps_submission_order():
    with pool.WorkerPool(workers=2, run=toy_run, init=None, nice=0) as wp:
        assert [r.metrics['n'] for r in wp.map([{'n': i} for i in range(5)])] == [0, 1, 2, 3, 4]


# ----------------------------------------------------------------------------- the real emulator

def _specs(window=WINDOW):
    params = [{}, {'profile autoISF_max': 1.5}, {'profile dura_ISF_weight': 0.5},
              {'profile autoISF_max': 1.5, 'profile dura_ISF_weight': 0.5}]
    return [replay.TrialSpec(day=DAY, params=p, window=window) for p in params]


def _comparable(res):
    return res.vhash, res.status, res.loops, {k: v for k, v in res.metrics.items() if k != 'seconds_core'}, res.flags


@needs_frozen
def test_two_workers_four_trials_equal_serial():
    """Acceptance H-103: 2 workers x 4 trials give equal loop counts and identical metrics to a serial run."""
    serial = sorted(_comparable(r) for r in pool.run_serial(_specs()))
    with pool.WorkerPool(workers=2) as wp:
        parallel = list(wp.run(_specs()))
    assert {r.pid for r in parallel} != {os.getpid()}
    assert sorted(_comparable(r) for r in parallel) == serial
    assert all(s[1] == 'done' and s[2] == 180 for s in serial)


@needs_frozen
def test_run_tasks_runs_base_first_and_compares_with_it(tmp_path):
    ctx = replay.ReplayContext(runs_dir=str(tmp_path / 'runs'), window=WINDOW)
    variant = Task(params={'profile autoISF_max': 1.5}, day=DAY)
    base = Task(params={}, day=DAY, control='base')
    out = list(pool.run_tasks(ctx, [variant, base], workers=2))
    assert [t.control for t, _ in out] == ['base', None]           # base wave first
    assert all(r.status == 'done' for _, r in out)
    assert os.path.isfile(ctx.base_csvs[DAY])                       # kept under runs/base/<day>.csv
    assert 'no_base_csv' not in out[1][1].flags                     # the variant was compared with that base


@needs_frozen
def test_syntax_error_in_a_worker_is_one_failed_row_and_the_pool_goes_on():
    specs = _specs(SHORT)[:2]
    specs.insert(1, replay.TrialSpec(day=DAY, window=SHORT, base_lines=['profile autoISF_max  1.5 +  ### broken']))
    with pool.WorkerPool(workers=2) as wp:
        res = wp.map(specs)
    assert [r.status for r in res] == ['done', 'failed', 'done']
    assert res[1].core_status == 'SYNTAX' and 'Problem in VDF-file' in res[1].error


DAY2 = '2026-09-05'


@pytest.mark.skipif(not (_frozen_ok() and _frozen_ok(DAY2)), reason=f'{DAY} and {DAY2} must be frozen')
def test_same_variant_on_two_days_in_two_workers_keeps_separate_files(tmp_path):
    """Finding (1): two workers replaying the same variant on two days at the same time must not share the stdout
    capture, the work directory or the VDF (keep_transcripts keeps all of them)."""
    runs = tmp_path / 'runs'
    p = {'profile autoISF_max': 1.5}
    specs = [replay.TrialSpec(day=d, params=p, window=(f'{d}T08:00:00Z', f'{d}T08:30:00Z'), runs_dir=str(runs),
                              keep_transcripts=True) for d in (DAY, DAY2)]
    assert specs[0].short_key() == specs[1].short_key()           # no key: the display form is per variant
    with pool.WorkerPool(workers=2, max_tasks_per_child=1) as wp:
        res = wp.map(specs)
    assert [r.status for r in res] == ['done', 'done'], [r.error for r in res]
    outs = [r.stdout_path for r in res]
    assert len(set(outs)) == 2 and all(os.path.isfile(o) for o in outs)
    for r, spec, other in ((res[0], specs[0], DAY2), (res[1], specs[1], DAY)):
        text = open(r.stdout_path, encoding='utf-8', errors='replace').read()
        assert r.stdout_path == str(runs / f'{spec.run_name()}.out')
        assert os.path.dirname(r.csv) == str(runs / spec.run_name())
        assert os.path.isfile(os.path.join(os.path.dirname(r.csv), spec.vdf_name()))
        assert f'{r.day}T08' in os.path.basename(r.csv) and f'{r.day}T08' in text and f'{other}T08' not in text
    assert len({os.path.dirname(r.csv) for r in res}) == 2


@pytest.mark.slow
@needs_frozen
def test_worker_memory_stays_flat_over_100_trials():
    specs = [replay.TrialSpec(day=DAY, params={'profile autoISF_max': round(1.2 + 0.01 * (i % 30), 2)}, window=SHORT)
             for i in range(100)]
    with pool.WorkerPool(workers=1, max_tasks_per_child=0) as wp:
        res = list(wp.run(specs, ordered=True))
    assert all(r.status == 'done' for r in res)
    assert len({r.pid for r in res}) == 1
    rss = [r.rss_mb for r in res]
    print(f'\nworker max RSS after trial 10: {rss[9]} MB, after trial 100: {rss[-1]} MB')
    assert rss[-1] - rss[9] < 60


def toy_run_hang(spec, worker=None):
    """A trial that never returns (a hung worker) when spec['hang']."""
    import time as _t
    if spec.get('hang'):
        _t.sleep(3600)
    return toy_run(spec, worker)


def test_waking_scheduler_times_out_a_hung_trial_and_restarts_the_pool():
    """run(wake_s, trial_timeout_s, on_wake): the parent wakes while waiting, a trial without an answer comes back
    failed after the timeout, the pool is restarted (the hung worker is killed) and the others finish."""
    wakes = []
    specs = [{'n': 0}, {'n': 1, 'hang': True, 'day': DAY, 'variant': 'v1'}, {'n': 2}, {'n': 3}]
    t0 = time.time()
    with pool.WorkerPool(workers=2, run=toy_run_hang, init=None, nice=0) as wp:
        out = list(wp.run(specs, wake_s=0.2, trial_timeout_s=4, on_wake=lambda: wakes.append(time.time())))
        restarts = wp.restarts
    assert time.time() - t0 < 40
    by_status = {}
    for r in out:
        by_status.setdefault(r.status, []).append(r)
    assert sorted(r.metrics['n'] for r in by_status['done']) == [0, 2, 3]
    assert len(by_status['failed']) == 1 and 'no answer within 4 s' in by_status['failed'][0].error
    assert by_status['failed'][0].variant == 'v1' and restarts == 1 and len(wakes) >= 3


def test_on_wake_may_stop_the_wait():
    class Stop(Exception):
        pass

    def wake():
        raise Stop()
    with pool.WorkerPool(workers=1, run=toy_run_hang, init=None, nice=0) as wp:
        gen = wp.run([{'n': 1, 'hang': True}], wake_s=0.2, on_wake=wake)
        with pytest.raises(Stop):
            next(gen)
        wp.terminate()
