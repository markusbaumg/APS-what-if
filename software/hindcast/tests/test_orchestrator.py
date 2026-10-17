"""
test_orchestrator.py  -  `hc study run`, the orchestrator (backlog H-305)

Two layers. The mechanics (rounds, guards, STOP, Ctrl-C, disk, three failures, no-progress, quarantine, tier 1,
round_comment.md, the per-trial line) run with a fake replay.run_trial in this process (workers=1, the serial twin
of pool.run_tasks), so they need no frozen data and take well under a second each. The real path - a spawn pool
over the frozen 2026-09-06, sentinel/null equal to base, and the kill -9 / Ctrl-C resume through the CLI in a
subprocess - uses a 30-minute window of that day and skips when it is not frozen.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from hindcast import cli, contracts, freeze, lint, orchestrator, replay
from hindcast import study as st
from hindcast.tests.conftest import GRID_AUTOISF, NS_SETTINGS, SOFTWARE_DIR

DAY = '2026-09-06'
DAY2 = '2026-09-05'
DAY3 = '2026-09-02'
SHORT = ('2026-09-06T06:00:00Z', '2026-09-06T06:30:00Z')
LINE_RE = re.compile(r'^\d+/\d+ \S+ \d{4}-\d{2}-\d{2} (done|failed|blocked) [\d.?]+s')
PYTHON = sys.executable


def _frozen_ok(day=DAY):
    try:
        return freeze.blocked_reason(day) is None
    except Exception:
        return False


needs_frozen = pytest.mark.skipif(not _frozen_ok(), reason=f'{DAY} not frozen (or stale): run `hc freeze {DAY}`')


def new_study(tmp_path, name='s', days=DAY):
    code = cli.main(['study', 'new', name, '--from-grid', GRID_AUTOISF, '--days', days, '--question', 'explore',
                     '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS])
    assert code == 0
    return st.StudyDir(os.path.join(str(tmp_path), name))


def rows_of(sd):
    return sd.trials()


# ----------------------------------------------------------------------------- the fake replay

class Fake:
    """A stand-in for replay.run_trial: decisions-level metrics only, configurable failures and side effects."""

    def __init__(self, agreement=None, fail=lambda spec: False, side=None):
        self.calls = []
        self.agreement = agreement or {}
        self.fail = fail
        self.side = side
        self.blocked_days = {}               # day -> reason: every trial of the day comes back blocked
        self.controls_done = False           # True: controls come back done (no CSV: 'guard_unchecked')

    def __call__(self, spec, worker=None):
        self.calls.append(spec)
        if self.side is not None:
            self.side(self, spec)
        if spec.blocked:
            return replay.TrialResult(status='blocked', day=spec.day, vhash=spec.vhash, variant=spec.variant,
                                      params=spec.params, key=spec.key, control=spec.control, error=spec.blocked)
        if spec.day in self.blocked_days:
            return replay.TrialResult(status='blocked', day=spec.day, vhash=spec.vhash, variant=spec.variant,
                                      params=spec.params, key=spec.key, control=spec.control,
                                      error=self.blocked_days[spec.day])
        if spec.control in st.CONTROL_KINDS and self.controls_done:
            return replay.TrialResult(status='done', day=spec.day, vhash=spec.vhash, variant=spec.variant,
                                      params=spec.params, key=spec.key, control=spec.control, seconds=0.01,
                                      metrics={'loops': 100, 'changed_loops': 3, 'agreement_pct': 99.0}, loops=100)
        if spec.control in st.CONTROL_KINDS:                 # no CSV: the guard records 'guard_unchecked'
            return replay.TrialResult(status='blocked', day=spec.day, vhash=spec.vhash, variant=spec.variant,
                                      params=spec.params, key=spec.key, control=spec.control, error='fake control')
        if self.fail(spec):
            return replay.TrialResult(status='failed', day=spec.day, vhash=spec.vhash, variant=spec.variant,
                                      params=spec.params, key=spec.key, control=spec.control, error='SYNTAX (fake)',
                                      seconds=0.01)
        base = not spec.params
        m = {'loops': 100, 'changed_loops': 3 if base else 10, 'agreement_pct': self.agreement.get(spec.day, 99.0)}
        if not base:
            m.update({'changed_vs_base': 7, 'rise_vs_base_U': 0.5, 'rest_vs_base_U': -0.1})
        return replay.TrialResult(status='done', day=spec.day, vhash=spec.vhash, variant=spec.variant, params=spec.params,
                                  key=spec.key, control=spec.control, metrics=m, seconds=0.01, loops=100)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(replay, 'run_trial', f)
    # the fake replays every day; the orchestrator's round-start freeze check follows Fake.blocked_days
    monkeypatch.setattr(freeze, 'blocked_reason', lambda day, *a, **k: f.blocked_days.get(day))
    return f


def orch(sd, lines=None, **kw):
    kw.setdefault('workers', 1)
    kw.setdefault('render', False)
    kw.setdefault('min_disk_mb', 1)
    lines = lines if lines is not None else []
    return orchestrator.Orchestrator(sd, settings_path=NS_SETTINGS, out=lines.append, **kw), lines


# ----------------------------------------------------------------------------- small pieces

def test_budget_and_until():
    assert orchestrator.budget_seconds(hours=0.5) == 1800.0
    s = orchestrator.seconds_until('00:00')
    assert 0 < s <= 86400
    assert orchestrator.budget_seconds(hours=48, until='00:00') == pytest.approx(s, abs=2)
    with pytest.raises(st.StudyError):
        orchestrator.seconds_until('25:99')


def test_module_docstring_and_summaries_pass_the_lint():
    assert not lint.lint_findings(orchestrator.__doc__, 'line')
    row = contracts.TrialRow(trial=1, key='k', phase='grid', vhash='v', variant='a_1', params={'profile a': 1},
                             day=DAY, status='done', code_sha='c', settings_sha='s', freeze_sha='f',
                             metrics={'loops': 10, 'changed_vs_base': 4, 'rise_vs_base_U': 0.3, 'rest_vs_base_U': -0.2})
    text = orchestrator.metric_summary(row)
    assert text == 'loops 10, changed +4 vs base, rise +0.30 U, rest -0.20 U'
    assert not lint.lint_findings(text, 'line')


# ----------------------------------------------------------------------------- mechanics with the fake replay

def test_full_run_rounds_status_round_comment_tier1(tmp_path, fake):
    sd = new_study(tmp_path)
    o, lines = orch(sd, batch=6)
    assert o.run() == 0
    rows = rows_of(sd)
    variants = [r for r in rows if r.control not in st.CONTROL_KINDS]
    assert len(variants) == 16 and len({r.key for r in variants}) == 16       # 15 grid points + base, once each
    assert o.round_no == 3                                                    # 6 + 6 + 4 tasks
    trial_lines = [l for l in lines if LINE_RE.match(l)]
    assert len(trial_lines) == len(rows)
    for l in lines:
        assert not lint.lint_findings(l, 'line'), l
    assert lines[-1] == contracts.HONESTY_STATEMENT
    status = json.load(open(sd.status_path))
    assert status['exit_code'] == 0 and status['round'] == 3 and status['floor']
    assert os.path.isfile(os.path.join(sd.path, 'round_comment.md'))
    body = open(os.path.join(sd.path, 'round_comment.md')).read()
    assert 'hindcast-round: s round 3' in body
    tier1 = json.load(open(os.path.join(sd.report_dir, 'tier1.json')))
    assert len(tier1['trials']) == len(rows) and tier1['honesty'] == contracts.HONESTY_STATEMENT
    assert json.load(open(sd.study_json))['phases'][0]['state'] == 'done'
    # a second run has nothing to do and replays nothing
    n = len(fake.calls)
    o2, lines2 = orch(sd)
    assert o2.run() == 0 and len(fake.calls) == n and len(rows_of(sd)) == len(rows)


def test_three_consecutive_failures_stop_with_exit_2(tmp_path, fake):
    fake.fail = lambda spec: bool(spec.params)
    sd = new_study(tmp_path)
    o, lines = orch(sd)
    assert o.run() == st.EXIT_GUARD
    state = sd.read_state()
    assert state.exit_code == 2 and '3 consecutive failed trials' in state.stop_reason
    assert sum(1 for r in rows_of(sd) if r.status == 'failed') == 3
    assert sd.lock_holder() is None


def test_disk_guard_stops_with_exit_2(tmp_path, fake):
    sd = new_study(tmp_path)
    o, _ = orch(sd, min_disk_mb=10 ** 12)
    assert o.run() == st.EXIT_GUARD
    assert 'disk below' in sd.read_state().stop_reason and rows_of(sd) == []


def test_stop_file_then_resume_without_redoing(tmp_path, fake):
    def side(f, spec):
        if len(f.calls) == 4:
            sd.request_stop('test')
    fake.side = side
    sd = new_study(tmp_path)
    o, _ = orch(sd)
    assert o.run() == 0
    assert sd.read_state().stop_reason.startswith(st.STOP_REASON)
    first = rows_of(sd)
    assert len(first) == 4 and not os.path.exists(sd.stop_path if hasattr(sd, 'stop_path') else os.path.join(sd.path, 'STOP'))
    fake.side = None
    o2, _ = orch(sd)
    assert o2.run() == 0
    rows = rows_of(sd)
    variants = [r for r in rows if r.control not in st.CONTROL_KINDS]
    assert len(variants) == 16 and len({r.key for r in variants}) == 16       # nothing done was replayed again


def test_ctrl_c_inside_a_round_is_exit_0_and_resumable(tmp_path, fake):
    def side(f, spec):
        if len(f.calls) == 5:
            raise KeyboardInterrupt
    fake.side = side
    sd = new_study(tmp_path)
    o, _ = orch(sd)
    assert o.run() == 0
    assert 'interrupted' in sd.read_state().stop_reason and sd.lock_holder() is None
    before = [r.key for r in rows_of(sd) if r.control not in st.CONTROL_KINDS]
    fake.side = None
    o2, _ = orch(sd)
    assert o2.run() == 0
    after = [r.key for r in rows_of(sd) if r.control not in st.CONTROL_KINDS]
    assert after[:len(before)] == before and len(after) == len(set(after)) == 16


def test_no_progress_is_a_guard_stop(tmp_path, fake):
    fake.side = lambda f, spec: setattr(spec, 'blocked', 'day not frozen (fake)')
    sd = new_study(tmp_path)
    o, lines = orch(sd)
    assert o.run() == st.EXIT_GUARD
    reason = sd.read_state().stop_reason
    # round 1: everything comes back blocked; round 2: the base alone again (its variants held); round 3 would
    # hand out the same base again without progress: the stop names the cause
    assert 'no progress' in reason and 'base replay came back blocked' in reason and o.round_no == 2
    assert all(r.status == 'blocked' for r in rows_of(sd))
    assert not lint.lint_findings(reason, 'issue')


def test_quarantined_day_leaves_the_search(tmp_path, fake):
    fake.agreement = {DAY2: 50.0}
    sd = new_study(tmp_path, days=f'{DAY2},{DAY}')
    o, lines = orch(sd, batch=4)                    # round 1 = the two base replays + two points
    assert o.run() == 0
    study = json.load(open(sd.study_json))
    roles = {d['day']: d['role'] for d in study['days']}
    assert roles == {DAY2: 'quarantined', DAY: 'search'}
    q = next(d for d in study['days'] if d['day'] == DAY2)
    assert 'agreement' in q['quarantine_reason']
    later = [r for r in rows_of(sd) if r.round is not None and r.round > 1 and r.control not in st.CONTROL_KINDS]
    assert later and all(r.day == DAY for r in later)
    assert json.load(open(sd.status_path))['days'][DAY2].startswith('quarantined')
    assert any('quarantined' in l for l in lines)


def test_dry_flag_keeps_the_dry_evaluator(tmp_path, capsys):
    sd = new_study(tmp_path)
    code = cli.main(['study', 'run', 's', '--dry', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS])
    assert code == 0
    assert rows_of(sd) and all(r.status == 'blocked' for r in rows_of(sd))
    assert '--dry: nothing is replayed' in capsys.readouterr().out


def test_grow_runs_freeze_new_at_most_hourly(tmp_path, fake, monkeypatch):
    calls = []

    def freeze_days(days, **kw):
        calls.append((list(days), kw.get('new'), kw.get('offline')))
        return [(d, 'kept', 'already frozen') for d in days]
    monkeypatch.setattr(freeze, 'freeze_days', freeze_days)
    sd = new_study(tmp_path)
    o, _ = orch(sd, grow=True, batch=8)
    assert o.run() == 0
    assert calls == [([DAY], True, True)]                    # several rounds, one freeze --new
    assert json.load(open(os.path.join(sd.path, 'grow.json')))['outcomes'][0][1] == 'kept'


# ----------------------------------------------------------------------------- the real replay (frozen 2026-09-06)

def _real_run_checks(sd, out, wall):
    rows = rows_of(sd)
    assert all(r.status == 'done' for r in rows), [r.error for r in rows if r.status != 'done']
    controls = [r for r in rows if r.control in st.CONTROL_KINDS]
    assert {r.control for r in controls} == {'sentinel', 'null'} and all('equals_base' in r.flags for r in controls)
    assert len({r.metrics['loops'] for r in rows}) == 1                      # equal loop counts on the day
    assert 'sentinel equals base' in sd.read_state().stop_reason
    assert len([l for l in out.splitlines() if LINE_RE.match(l)]) == len(rows)
    assert out.rstrip().endswith(contracts.HONESTY_STATEMENT)
    assert wall < 120


def _cli_run(study_dir_parent, name, *extra, **popen_kw):
    cmd = [PYTHON, '-m', 'hindcast', 'study', 'run', name, '--workers', '2', '--window', *SHORT, '--no-report',
           '--studies-dir', str(study_dir_parent), '--settings', NS_SETTINGS, *extra]
    return subprocess.Popen(cmd, cwd=SOFTWARE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=dict(os.environ, PYTHONUTF8='1'), **popen_kw)


def _wait_rows(sd, n, proc, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(sd.trials_path) and len(rows_of(sd)) >= n:
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


@needs_frozen
def test_real_pool_run_on_a_short_window(tmp_path):
    """A spawn pool of 2 workers through the CLI (a subprocess, so the pools of this test process are not touched:
    multiprocessing numbers pool workers per parent process, test_pool.py asserts worker indices 1..4)."""
    sd = new_study(tmp_path)
    t0 = time.time()
    proc = _cli_run(tmp_path, 's')
    out, _ = proc.communicate(timeout=180)
    assert proc.returncode == 0, out[-2000:]
    _real_run_checks(sd, out, time.time() - t0)


@needs_frozen
@pytest.mark.parametrize('how', ['kill9', 'sigint'])
def test_kill_and_interrupt_leave_a_resumable_study(tmp_path, how):
    sd = new_study(tmp_path, name='k')
    proc = _cli_run(tmp_path, 'k', start_new_session=True)
    assert _wait_rows(sd, 4, proc), proc.communicate()[0][-2000:]
    if how == 'kill9':
        os.kill(proc.pid, signal.SIGKILL)
    else:
        os.killpg(proc.pid, signal.SIGINT)                    # Ctrl-C: the whole process group, workers included
    out, _ = proc.communicate(timeout=60)
    if how == 'sigint':
        assert proc.returncode == 0 and 'interrupted' in out
        assert sd.lock_holder() is None
    before = [r.key for r in rows_of(sd) if r.control not in st.CONTROL_KINDS]
    assert 0 < len(before) < 16
    proc2 = _cli_run(tmp_path, 'k')
    out2, _ = proc2.communicate(timeout=180)
    assert proc2.returncode == 0, out2[-2000:]
    after = [r.key for r in rows_of(sd) if r.control not in st.CONTROL_KINDS]
    assert after[:len(before)] == before and len(after) == len(set(after)) == 16   # done trials not re-done
    assert 'exit 0 (finished' in out2


# ----------------------------------------------------------------------------- review fixes (wave 3)

def _status_json(sd):
    return json.load(open(sd.status_path))


def _done_rows_per_key(sd):
    counts = {}
    for r in rows_of(sd):
        if r.status == 'done':
            counts[r.key] = counts.get(r.key, 0) + 1
    return counts


def test_one_lock_and_state_across_rounds(tmp_path, fake):
    """(1) Between two rounds the study stays locked and STATE.json has no exit code: another process sees it
    running and a second `hc study run` is refused (exit 3). The lock is released once, with the real exit code."""
    sd = new_study(tmp_path)
    seen = []

    def gap(o):
        state, lock = sd.read_state(), sd.read_lock()
        seen.append((state.exit_code, state.pid, state.phase, lock and lock['pid']))
        if len(seen) == 1:
            probe = subprocess.run([PYTHON, '-c', 'import sys; from hindcast import study as st; '
                                    'print(st.StudyDir(sys.argv[1]).is_running())', sd.path],
                                   cwd=SOFTWARE_DIR, capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8='1'))
            seen.append(probe.stdout.strip())
            second = subprocess.run([PYTHON, '-m', 'hindcast', 'study', 'run', 's', '--dry', '--studies-dir',
                                     str(tmp_path), '--settings', NS_SETTINGS], cwd=SOFTWARE_DIR, capture_output=True,
                                    text=True, env=dict(os.environ, PYTHONUTF8='1'))
            seen.append(second.returncode)
    o, _ = orch(sd, batch=6, between_rounds=gap)
    assert o.run() == 0 and o.round_no == 3
    assert seen[0] == (None, os.getpid(), 'planning round 2', os.getpid())
    assert seen[1] == 'True' and seen[2] == st.EXIT_REFUSED
    assert all(x[0] is None and x[3] == os.getpid() for x in seen[3:])            # the second gap too
    state = sd.read_state()
    assert state.exit_code == 0 and state.stop_reason.startswith('finished') and sd.read_lock() is None


def test_stop_between_rounds_is_honoured(tmp_path, fake):
    """(2) A STOP written between rounds is neither removed nor ignored: exit 0, 'stopped: STOP file', no further
    round; the STOP file is removed by the close, before the release."""
    sd = new_study(tmp_path)
    o, _ = orch(sd, batch=6, between_rounds=lambda o: sd.request_stop('test'))
    assert o.run() == 0
    state = sd.read_state()
    assert state.stop_reason.startswith('stopped: STOP file') and o.round_no == 1
    assert {r.round for r in rows_of(sd)} == {1} and not sd.stop_requested() and sd.read_lock() is None
    status = _status_json(sd)
    assert status['trials_planned'] > status['trials_done']                         # (9) the plan is kept


def test_failed_base_is_retried_before_its_variants_also_across_processes(tmp_path, fake):
    """(3) A failed base row is not settled: it is handed out again (alone) and the day's variants wait for it,
    also in a new process (no in-memory base_failed). No variant is ever compared with a missing base."""
    failed_once = []

    def fail(spec):
        if not spec.params and not failed_once:
            failed_once.append(spec.key)
            return True
        return False
    fake.fail = fail
    sd = new_study(tmp_path)
    o, _ = orch(sd, between_rounds=lambda o: sd.request_stop('test'))               # round 1 only
    assert o.run() == 0
    rows = rows_of(sd)
    assert [r.status for r in rows if r.control == 'base'] == ['failed']
    assert all(r.status == 'blocked' for r in rows if r.control is None)            # held in this run
    calls = len(fake.calls)
    o2, lines = orch(sd)                                                            # a new process: nothing in memory
    assert o2.run() == 0
    second = fake.calls[calls:]
    assert not second[0].params                                                     # the base first ...
    first_variant = next(i for i, sp in enumerate(second) if sp.params and sp.control is None)
    assert any(not sp.params and sp.control in (None, 'base') for sp in second[:first_variant])
    rows = rows_of(sd)
    base_done = next(r.trial for r in rows if r.control == 'base' and r.status == 'done')
    variants_done = [r for r in rows if r.control is None and r.status == 'done']
    assert len(variants_done) == 15 and all(r.trial > base_done for r in variants_done)
    assert any('held: ' in l and 'failed 1 time(s)' in l for l in lines)


def test_kill_mid_round_never_gives_a_key_two_done_rows(tmp_path, fake):
    """(4) kill -9 in the middle of round 1 (simulated: an exception no handler catches, the lock stays with the
    pid), re-run with the same round number: the controls are new keys by design and no key gets a second done
    row."""
    class Killed(BaseException):
        pass

    fake.controls_done = True
    sd = new_study(tmp_path)
    o, _ = orch(sd)
    recorded = o.on_record

    def on_record(*a):                                   # the kill lands after the 5th row (sentinel + null among them)
        recorded(*a)
        if o.appended == 5:
            raise Killed()
    o.on_record = on_record
    with pytest.raises(Killed):
        o.run()
    assert sd.read_state().exit_code is None and sd.read_round() is None             # no release, no round.json
    assert len(rows_of(sd)) == 5
    o2, _ = orch(sd)
    assert o2.run() == 0
    counts = _done_rows_per_key(sd)
    assert counts and max(counts.values()) == 1, {k: n for k, n in counts.items() if n > 1}
    controls = [r for r in rows_of(sd) if r.control in st.CONTROL_KINDS]
    assert len(controls) >= 3 and len({r.key for r in controls}) == len(controls)
    assert len([r for r in rows_of(sd) if r.control is None and r.status == 'done']) == 15


def test_unfrozen_day_is_set_aside_and_named(tmp_path, fake):
    """(6) A day that is not frozen gets one blocked base row, its variants are never handed out, status lists the
    day as not frozen, the frozen day runs to the end, and the no-progress stop names the day. (DAY3 has no CGM gap
    in its real frozen meta, so the per-round quarantine leaves it in the search.)"""
    fake.blocked_days = {DAY3: f'{DAY3} is not frozen; run `hc freeze {DAY3}`'}
    sd = new_study(tmp_path, days=f'{DAY3},{DAY}')
    o, lines = orch(sd, batch=4)
    assert o.run() == st.EXIT_GUARD
    reason = sd.read_state().stop_reason
    assert 'no progress' in reason and DAY3 in reason and 'not frozen' in reason
    assert not lint.lint_findings(reason, 'issue')
    day2 = [r for r in rows_of(sd) if r.day == DAY3 and r.control not in st.CONTROL_KINDS]
    assert len(day2) == 1 and day2[0].control == 'base' and day2[0].status == 'blocked'
    done1 = [r for r in rows_of(sd) if r.day == DAY and r.control not in st.CONTROL_KINDS and r.status == 'done']
    assert len(done1) == 16
    status = _status_json(sd)
    assert status['days'][DAY3] == 'search, not frozen' and status['days'][DAY] == 'search'
    assert 'not frozen' in st.status_text(st.build_status(sd))


class HangPool:
    """A pool whose trials never return: run() only wakes the Runner (on_wake) every wake_s seconds."""

    def __init__(self, stop_after=None, sd=None):
        self.wakes = 0
        self.kw = None
        self.stop_after = stop_after
        self.sd = sd
        self.terminated = False

    def start(self):
        return self

    def terminate(self):
        self.terminated = True

    close = terminate

    def run(self, specs, wake_s=None, trial_timeout_s=None, on_wake=None):
        self.kw = (wake_s, trial_timeout_s)
        while True:
            time.sleep(wake_s)
            self.wakes += 1
            if self.stop_after and self.wakes == self.stop_after:
                self.sd.request_stop('test')
            on_wake()
        yield                                                                       # pragma: no cover


def test_waiting_for_a_hung_pool_still_sees_stop_and_heartbeats(tmp_path, fake):
    """(7) While no result comes, the Runner is woken (heartbeat, STOP, budget): a STOP ends the run with exit 0."""
    sd = new_study(tmp_path)
    hp = HangPool(stop_after=3, sd=sd)
    beats = []
    o, _ = orch(sd, workers=2, pool_factory=lambda o: hp, wake_s=0.05, trial_timeout_s=7)
    real_idle = st.Runner.idle

    def idle(self):
        beats.append(sd.read_state().exit_code)                                    # still running at every wake
        return real_idle(self)
    st.Runner.idle = idle
    try:
        t0 = time.time()
        assert o.run() == 0
    finally:
        st.Runner.idle = real_idle
    assert time.time() - t0 < 10 and hp.wakes == 3 and hp.kw == (0.05, 7) and hp.terminated
    assert sd.read_state().stop_reason.startswith(st.STOP_REASON) and rows_of(sd) == []
    assert beats == [None, None, None] and sd.read_lock() is None


def test_waiting_for_a_hung_pool_stops_at_the_budget(tmp_path, fake):
    sd = new_study(tmp_path)
    hp = HangPool()
    o, _ = orch(sd, workers=2, pool_factory=lambda o: hp, wake_s=0.05, hours=0.3 / 3600)
    t0 = time.time()
    assert o.run() == 0
    assert sd.read_state().stop_reason.startswith('budget exhausted') and time.time() - t0 < 5 and hp.wakes >= 2


def test_hours_zero_is_refused(tmp_path, fake):
    """(8) --hours 0 is not 'no budget': refused with exit 3 before anything runs."""
    sd = new_study(tmp_path)
    with pytest.raises(st.StudyError, match='must be positive'):
        orch(sd, hours=0)
    code = cli.main(['study', 'run', 's', '--hours', '0', '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS,
                     '--no-report'])
    assert code == st.EXIT_REFUSED and rows_of(sd) == [] and sd.read_state() is None


def test_resume_reports_the_real_already_done_count(tmp_path, fake):
    """(9) After a STOP the status keeps the planned count; the resumed run reports how many keys were done."""
    def side(f, spec):
        if len(f.calls) == 4:
            sd.request_stop('test')
    fake.side = side
    sd = new_study(tmp_path)
    o, _ = orch(sd)
    assert o.run() == 0
    status = _status_json(sd)
    done = len(sd.done_keys())
    assert status['trials_done'] == done and status['trials_planned'] >= 16 > done
    fake.side = None
    o2, lines = orch(sd)
    assert o2.run() == 0
    assert any(f'{done} key(s) already done' in l for l in lines)
    assert any(f'rows appended, {done} already done' in l for l in lines)
    final = _status_json(sd)
    assert final['trials_done'] == final['trials_planned'] - final['trials_blocked'] - final['trials_failed']


def test_low_base_agreement_quarantines_before_the_variants(tmp_path, fake):
    """(10) Without --batch round 1 holds every variant; a day whose base agreement is below the floor is
    quarantined as soon as its base row is in, so none of its variants is replayed."""
    fake.agreement = {DAY2: 50.0}
    sd = new_study(tmp_path, days=f'{DAY2},{DAY}')
    o, lines = orch(sd)
    assert o.run() == 0
    day2 = [r for r in rows_of(sd) if r.day == DAY2 and r.control is None]
    assert day2 == []
    assert [d for d in json.load(open(sd.study_json))['days'] if d['day'] == DAY2][0]['role'] == 'quarantined'
    assert len([r for r in rows_of(sd) if r.day == DAY and r.control is None and r.status == 'done']) == 15
    assert any('quarantined after its base replay' in l for l in lines)
    # no control runs on a day quarantined in the same round; the round's null moves to the next sentinel
    controls = [r for r in rows_of(sd) if r.control in ('sentinel', 'null')]
    assert controls and not [r for r in controls if r.day == DAY2]
    assert [r.control for r in controls if r.day == DAY].count('null') == 1


# ----------------------------------------------------------------------------- close order, round lines, comment words (verifier fixes)

def test_report_is_rendered_under_the_lock_after_the_stop_file_is_gone(tmp_path, fake, monkeypatch):
    """(5) The close removes STOP first, writes tier 1 and renders the report while the lock is still held (the page
    already sees the final exit code and reason), then releases; round_comment.md says 'stopped by the STOP
    file'."""
    from hindcast import report
    sd = new_study(tmp_path)
    seen = {}

    def render(sd_, **kw):
        status = st.build_status(sd_)
        seen.update(stop=sd.stop_requested(), lock=(sd.read_lock() or {}).get('pid'),
                    disk_exit=sd.read_state().exit_code, exit=status.exit_code,
                    reason=status.stop_reason.split(';')[0], tier1=os.path.isfile(os.path.join(sd.report_dir,
                                                                                               'tier1.json')))
        return report.ReportResult(path=os.path.join(sd.report_dir, 'index.html'), bytes=0, out_dir=sd.report_dir)
    monkeypatch.setattr(report, 'render_report', render)
    o, _ = orch(sd, batch=6, render=True, between_rounds=lambda o: sd.request_stop('test'))
    assert o.run() == 0
    assert seen == {'stop': False, 'lock': os.getpid(), 'disk_exit': None, 'exit': 0, 'reason': st.STOP_REASON,
                    'tier1': True}
    state = sd.read_state()
    assert state.exit_code == 0 and state.stop_reason.startswith(st.STOP_REASON) and sd.read_lock() is None
    body = open(os.path.join(sd.path, 'round_comment.md')).read()
    assert '- run: stopped by the STOP file, exit 0' in body and 'run: stopped, exit 0' not in body


def test_session_rounds_log_round_lines_and_one_exit_line(tmp_path, fake):
    """(3) Every round logs 'round N recorded: ...'; 'finished' / 'exit' appear once, in the orchestrator's close.
    (1, 2) The finished run's comment says 'run: finished' and counts the control replays next to the variants."""
    sd = new_study(tmp_path)
    o, lines = orch(sd, batch=6)
    assert o.run() == 0 and o.round_no == 3
    rounds = [l for l in lines if ' recorded: ' in l]
    assert [l.split(' recorded:')[0] for l in rounds] == ['s: round 1', 's: round 2', 's: round 3']
    exits = [l for l in lines if re.search(r'\bexit \d\b', l)]
    assert len(exits) == 1 and exits[0].startswith('s: exit 0 (finished: every built phase is settled')
    assert not any(l.startswith('s: finished;') for l in lines)
    body = open(os.path.join(sd.path, 'round_comment.md')).read()
    assert '- run: finished, exit 0 (every built phase is settled' in body and 'run: stopped' not in body
    m = re.search(r'- variants in this round: (\d+)(?: \([^)]*\))?, plus (\d+) control replays \(sentinel (\d+), '
                  r'null (\d+)\)', body)
    assert m, body
    in_round = [r for r in rows_of(sd) if r.round == 3]
    assert int(m.group(2)) == sum(1 for r in in_round if r.control in st.CONTROL_KINDS) == 2
    assert not lint.lint_findings(body, 'issue')


def test_every_finished_round_writes_the_cards_through_write_cards(tmp_path, fake, monkeypatch):
    """H-716: the orchestrator calls cards.write_cards (the function of `hc card`) with the round's analysis at
    the end of every round; the cards on disk equal what `hc card` writes."""
    from hindcast import cards
    calls = []
    original = cards.write_cards

    def spy(study_dir, **kw):
        calls.append(kw)
        return original(study_dir, **kw)
    monkeypatch.setattr(cards, 'write_cards', spy)
    sd = new_study(tmp_path)
    o, lines = orch(sd, batch=6)
    assert o.run() == 0
    assert len(calls) == o.round_no == 3
    assert all(kw.get('analysis') is not None and not kw.get('ranks') and not kw.get('vhashes') for kw in calls)
    out = os.path.join(sd.path, cards.CARDS_DIR)
    on_disk = sorted(os.listdir(out)) if os.path.isdir(out) else []
    a = cards.analysis_of(sd, o.settings)
    want = cards.card_order(a)[:cards.DEFAULT_TOP]
    assert want and len(on_disk) == 2 * len(want)                       # <n>.md + <n>.vdf per card number
    by_hand = str(tmp_path / 'by_hand')
    original(sd, settings=o.settings, analysis=a, out_dir=by_hand)
    assert sorted(os.listdir(by_hand)) == on_disk
    for name in on_disk:
        assert open(os.path.join(out, name), encoding='utf-8').read() == open(os.path.join(by_hand, name), encoding='utf-8').read()


# ----------------------------------------------------------------------------- metrics_version (stale under the same key)

def test_metrics_version_change_is_stale_stop_then_reeval(tmp_path, fake, monkeypatch):
    from hindcast import metrics as hm
    sd = new_study(tmp_path)
    o, _ = orch(sd)
    assert o.run() == 0
    done0 = [r for r in st.StudyDir.latest_by_key(sd.trials()).values() if r.status == 'done']
    assert done0 and all(r.metrics_version == hm.METRICS_VERSION for r in done0)
    keys0 = {r.key for r in done0}
    monkeypatch.setattr(hm, 'METRICS_VERSION', hm.METRICS_VERSION + 1)
    status = st.build_status(sd)
    assert status.trials_stale == len(done0) and f'{len(done0)} stale' in st.status_text(status)

    n = len(fake.calls)                                         # --on-stale stop: nothing replayed, exit 2, count named
    o2, lines2 = orch(sd)
    assert o2.run() == 2 and len(fake.calls) == n
    s = _status_json(sd)
    assert f'metrics_version changed: {hm.METRICS_VERSION - 1} -> {hm.METRICS_VERSION} ({len(done0)} done rows)' \
        in s['stop_reason']
    assert not lint.lint_findings(s['stop_reason'], 'line')

    o3, lines3 = orch(sd, on_stale='reeval')                    # reeval: same keys replayed again, base first
    assert o3.run() == 0
    new_calls = fake.calls[n:]
    assert not new_calls[0].params                              # the base replay of the day first
    latest = st.StudyDir.latest_by_key(sd.trials())
    assert keys0 <= set(latest) and all(latest[k].status == 'done' and latest[k].metrics_version == hm.METRICS_VERSION
                                        for k in keys0)
    assert st.build_status(sd).trials_stale == 0
    rnd = sd.read_round()
    assert rnd is not None and rnd.trials >= len(done0)         # the round was rebuilt from the new rows
    assert json.load(open(sd.study_json))['phases'][0]['state'] == 'done'
    n2 = len(fake.calls)                                        # and the next run has nothing to do
    o4, _ = orch(sd)
    assert o4.run() == 0 and len(fake.calls) == n2
