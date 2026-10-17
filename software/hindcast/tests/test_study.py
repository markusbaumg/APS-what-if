"""Tests for hindcast/study.py (backlog H-301): the study directory, trials.jsonl append and resume, STATE.json by
atomic rename, the pid + heartbeat lock with takeover, STOP, exit codes 0/2/3, status.json <= 2 KB, and the two
acceptance runs with subprocesses: kill -9 mid-trial loses at most one trial; a second concurrent run exits 3."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from hindcast import contracts as c
from hindcast import space as sp
from hindcast import study as st
from hindcast.contracts import Phase, State, TrialRow
from hindcast.tests.conftest import GRID_AUTOISF, NS_SETTINGS, SOFTWARE_DIR

SETTINGS = sp.load_settings(NS_SETTINGS)
SPACE = {'profile autoISF_max': {'type': 'float', 'low': 1.3, 'high': 1.5, 'step': 0.1},
         'profile dura_ISF_weight': {'type': 'float', 'low': 0.5, 'high': 0.7, 'step': 0.1}}


def make_study(tmp_path, name='s1', days=('2026-09-06',), space=None, phases=None, **kw):
    return st.new_study(name, list(days), space or SPACE, phases=phases or [Phase(kind='grid')],
                        settings=SETTINGS or {'safety': {}}, studies_dir=str(tmp_path), **kw)


def ok_evaluate(task):
    return {'metrics': {'loops': 180, 'smb_emul_U': 1.0, 'smb_orig_U': 1.0, 'delta_U': 0.0,
                        'changed_loops': 3, 'agreement_pct': 98.3}, 'seconds': 0.01}


# ----------------------------------------------------------------------------- parsers

def test_parse_days():
    assert st.parse_days('2026-09-01..09-03') == ['2026-09-01', '2026-09-02', '2026-09-03']
    assert st.parse_days('2026-08-30..2026-09-01') == ['2026-08-30', '2026-08-31', '2026-09-01']
    assert st.parse_days('2026-09-06,2026-09-05,2026-09-06') == ['2026-09-06', '2026-09-05']
    assert st.parse_days('2026-09-01..03') == ['2026-09-01', '2026-09-02', '2026-09-03']
    for bad in ('2026-9-1', '2026-09-03..2026-09-01', '2026-02-30', 'yesterday'):
        with pytest.raises(st.StudyError):
            st.parse_days(bad)


def test_parse_tag_constraint_phase():
    assert st.parse_tag('2026-09-05=weekend, sport') == ('2026-09-05', ['weekend', 'sport'])
    with pytest.raises(st.StudyError):
        st.parse_tag('weekend')
    assert st.parse_constraint('profile a <= profile b < profile c') == ['profile a', '<=', 'profile b', '<', 'profile c']
    with pytest.raises(st.StudyError):
        st.parse_constraint('profile a <= b')
    with pytest.raises(st.StudyError):
        st.parse_constraint('profile a')
    p = st.parse_phase('lhs:n=24,seed=7')
    assert p.kind == 'lhs' and p.n == 24 and p.seed == 7
    p = st.parse_phase('halving:rungs=2/5/all')
    assert p.rungs == [2, 5, 'all']
    assert st.parse_phase('validate:top=8,days=holdout').days == 'holdout'
    with pytest.raises(st.StudyError):
        st.parse_phase('oaat:bogus=1')


def test_resolve_study_dir(tmp_path):
    assert st.resolve_study_dir('demo', str(tmp_path)) == os.path.join(str(tmp_path), 'demo')
    d = tmp_path / 'x'
    d.mkdir()
    assert st.resolve_study_dir(str(d)) == str(d)
    assert st.resolve_study_dir(str(d / 'study.json')) == str(d)
    with pytest.raises(st.StudyError):
        st.resolve_study_dir('no spaces allowed', str(tmp_path))


# ----------------------------------------------------------------------------- hc study new

def test_new_study_writes_study_json(tmp_path):
    sd = make_study(tmp_path, days=('2026-09-05', '2026-09-06'), holdout=['2026-09-05'],
                    tags={'2026-09-05': ['weekend', 'sport']})
    assert sd.exists()
    study = sd.load_study(SETTINGS)
    assert [d.role for d in study.days] == ['holdout', 'search'] and study.days[0].tags == ['weekend', 'sport']
    assert study.search_days() == ['2026-09-06'] and study.phases[0].kind == 'grid'
    assert len(study.code_sha) == 40
    if SETTINGS:
        assert study.space['profile autoISF_max'].phone == SETTINGS['autoisf']['autoISF_max']
    with open(sd.study_json, encoding='utf-8') as fh:
        c.Study.from_dict(json.load(fh)).validate()


def test_new_study_refuses_locked_key_naming_it(tmp_path):
    with pytest.raises(st.StudyError) as e:
        make_study(tmp_path, space={'profile autoISF_max': SPACE['profile autoISF_max'],
                                    'profile max_iob': {'type': 'float', 'low': 5, 'high': 10, 'step': 1}})
    assert 'profile max_iob' in str(e.value) and 'autoISF_max' not in str(e.value)
    assert e.value.exit_code == 3
    assert not (tmp_path / 's1').exists()                                   # nothing half-written
    with pytest.raises(st.StudyError) as e:
        make_study(tmp_path, space={'new_parameter SMBRatio': {'type': 'float', 'low': 0.3, 'high': 0.6, 'step': 0.1}})
    assert 'new_parameter' in str(e.value)


def test_new_study_other_refusals(tmp_path):
    make_study(tmp_path)
    with pytest.raises(st.StudyError, match='never overwritten'):
        make_study(tmp_path)
    with pytest.raises(st.StudyError, match='outside --days'):
        make_study(tmp_path, name='s2', holdout=['2026-09-05'])
    with pytest.raises(st.StudyError, match='shadowed'):
        make_study(tmp_path, name='s3', base_vdf_lines=['profile  autoISF_max  1.3'])
    with pytest.raises(st.StudyError, match='constraint'):
        make_study(tmp_path, name='s4', constraints=[['profile autoISF_max', '<=', 'profile nothere']])


LOCKED_BASE_ROWS = ['profile  max_iob  15', 'profile  sens  40', 'new_parameter  SMBRatio  0.9']   # the reviewer's bypass


def test_base_vdf_rows_on_locked_keys_are_refused_naming_them(tmp_path):
    with pytest.raises(st.StudyError) as e:
        make_study(tmp_path, base_vdf_lines=LOCKED_BASE_ROWS + ['profile  enable_pp_ISF  False'])
    text = str(e.value)
    assert e.value.exit_code == 3 and 'DESIGN.md section 3' in text
    for key in ('profile max_iob', 'profile sens', 'new_parameter SMBRatio'):
        assert key in text, key
    assert 'enable_pp_ISF' not in text                                       # the allowed row is not blamed
    assert not (tmp_path / 's1').exists()                                   # nothing half-written
    # each row alone is refused too, also one that is only locked through the settings' safety section
    for row in LOCKED_BASE_ROWS:
        with pytest.raises(st.StudyError, match=row.split()[1]):
            make_study(tmp_path, base_vdf_lines=[row])
    with pytest.raises(st.StudyError, match='profile my_safety_key'):
        st.new_study('s9', ['2026-09-06'], SPACE, phases=[Phase(kind='grid')], studies_dir=str(tmp_path),
                     settings={'safety': {'my_safety_key': 1}}, base_vdf_lines=['profile  my_safety_key  2'])
    # rows of other groups and unlocked profile keys pass (state/temp/STAIR and a tab-separated row included)
    sd = make_study(tmp_path, name='ok', base_vdf_lines=['profile  enable_pp_ISF  False', 'state  note  "x"',
                                                         'temp  x  1', 'STAIR  s  [0, 1]', 'profile\tautoISF_min\t0.7'])
    assert sd.load_study(SETTINGS).base_vdf_lines[-1] == 'profile\tautoISF_min\t0.7'


def test_locked_base_vdf_row_in_existing_study_json_is_refused_on_load(tmp_path):
    sd = make_study(tmp_path)
    with open(sd.study_json, encoding='utf-8') as fh:
        data = json.load(fh)
    data['base_vdf_lines'] = ['profile  max_iob  15']
    with open(sd.study_json, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    with pytest.raises(st.StudyError) as e:
        sd.load_study(SETTINGS)
    assert 'profile max_iob' in str(e.value) and e.value.exit_code == 3
    with pytest.raises(c.ContractError, match='profile max_iob'):
        c.Study.load(sd.study_json).validate()                              # without settings too (built-in list)
    with pytest.raises(st.StudyError):
        st.Runner(sd, settings=SETTINGS)
    assert c.locked_vdf_row_reason('profile  autoISF_max  1.3') is None
    assert c.locked_vdf_row_reason('new_parameter  anything  1') and c.locked_vdf_row_reason('profile dia 5')
    assert c.locked_vdf_row_reason('') is None and c.locked_vdf_row_reason('profile max_iob') is None   # not a row


def test_locked_key_in_existing_study_json_is_refused_on_load(tmp_path):
    sd = make_study(tmp_path)
    with open(sd.study_json, encoding='utf-8') as fh:
        data = json.load(fh)
    data['space']['profile max_iob'] = {'type': 'float', 'low': 5, 'high': 10, 'step': 1}
    with open(sd.study_json, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    with pytest.raises(st.StudyError) as e:
        sd.load_study(SETTINGS)
    assert 'profile max_iob' in str(e.value) and e.value.exit_code == 3
    with pytest.raises(st.StudyError):
        st.Runner(sd, settings=SETTINGS)


# ----------------------------------------------------------------------------- the runner: resume, STATE, STOP, exit codes

def test_runner_appends_rows_and_resumes(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    assert tasks[0].control == 'base' and tasks[0].params == {} and len(tasks) == 1 + 9
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    res = runner.run(tasks, ok_evaluate)
    assert (res.exit_code, res.stop_reason, res.done, res.skipped) == (0, 'finished', 10, 0)
    rows = sd.trials()
    assert len(rows) == 10 and [r.trial for r in rows] == list(range(10))
    assert all(r.status == 'done' and r.key == runner.key_of(t) for r, t in zip(rows, tasks))
    assert rows[0].variant == 'base' and rows[0].vhash == c.BASE_VHASH and rows[0].phase == 'floor'
    assert rows[1].variant == 'autoISF_max_1.3__dura_ISF_weight_0.5'
    state = sd.read_state()
    assert state.exit_code == 0 and state.trials_done == 10 and state.queue_len == 0 and state.pid == os.getpid()
    assert not sd.is_running()
    # resume: every key is done, nothing is evaluated again
    calls = []
    res2 = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, lambda t: calls.append(t) or ok_evaluate(t))
    assert res2.skipped == 10 and res2.done == 0 and calls == [] and len(sd.trials()) == 10
    # a failed/blocked row is re-evaluated, the newer row supersedes it
    bad = tasks[3]
    st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run([bad], lambda t: {'status': 'blocked', 'error': 'not frozen'})
    assert len(sd.trials()) == 10                                               # key was done: skipped
    keyed = sd.latest_by_key(sd.trials())
    assert keyed[runner.key_of(bad)].status == 'done'


def test_state_json_is_written_by_atomic_rename(tmp_path, monkeypatch):
    sd = make_study(tmp_path)
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, 'replace', lambda a, b: replaced.append((a, b)) or real(a, b))
    sd.write_state(State(phase='x', trials_done=0, queue_len=0, pid=1, heartbeat=c.now_iso()))
    assert any(b.endswith('STATE.json') and a.endswith('.tmp') for a, b in replaced)
    assert not [f for f in os.listdir(sd.path) if f.endswith('.tmp')]


def test_stop_file_stops_after_current_trial(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)

    def evaluate(task):
        if len(sd.trials()) == 2:                                               # during the third trial
            sd.request_stop()
        return ok_evaluate(task)

    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, evaluate)
    assert res.exit_code == 0 and res.stop_reason == st.STOP_REASON and res.stop_file and res.done == 3
    assert not sd.stop_requested()                                              # consumed
    assert sd.read_state().stop_reason == st.STOP_REASON
    res2 = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, ok_evaluate)
    assert res2.done == 7 and res2.skipped == 3 and len(sd.done_keys()) == 10


def test_stale_stop_file_is_cleared_on_start(tmp_path):
    sd = make_study(tmp_path)
    sd.request_stop('left over')
    study = sd.load_study(SETTINGS)
    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(st.study_tasks(study, SETTINGS)[:2], ok_evaluate)
    assert res.done == 2 and res.stop_reason == 'finished'


def test_guard_stop_exit_2(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)

    def evaluate(task):
        if task.params.get('profile autoISF_max') == 1.4:
            raise st.GuardStop('sentinel differs from base in loop 2026-09-06T09:00:37Z')
        return ok_evaluate(task)

    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, evaluate)
    assert res.exit_code == 2 and res.stop_reason.startswith('guard: sentinel')
    assert sd.read_state().exit_code == 2
    status = st.build_status(sd)
    assert status.exit_code == 2 and 'sentinel' in status.stop_reason and not status.running


def test_three_consecutive_failures_exit_2(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, lambda t: {'status': 'failed', 'error': 'SYNTAX'})
    assert res.exit_code == 2 and res.failed == 3 and 'consecutive' in res.stop_reason
    assert all(r.status == 'failed' and r.error == 'SYNTAX' for r in sd.trials())
    # an exception inside evaluate is a failed row too, and a success resets the counter
    n = [0]

    def flaky(task):
        n[0] += 1
        if n[0] % 2:
            raise RuntimeError('boom')
        return ok_evaluate(task)

    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, flaky)
    assert res.exit_code == 0 and res.failed == 5 and res.done == 10


def test_disk_guard(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=10 ** 9).run(st.study_tasks(study, SETTINGS), ok_evaluate)
    assert res.exit_code == 2 and 'disk' in res.stop_reason and res.done == 0


def test_budget_stops_with_exit_0(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0, budget_s=0.2)
    res = runner.run(st.study_tasks(study, SETTINGS), lambda t: time.sleep(0.15) or ok_evaluate(t))
    assert res.exit_code == 0 and res.stop_reason == 'budget exhausted' and 0 < res.done < 10


# ----------------------------------------------------------------------------- the lock

def test_lock_rules(tmp_path):
    sd = make_study(tmp_path)
    now = c.now_iso()
    assert st.lock_holder(None) is None
    live = State(phase='grid', trials_done=1, queue_len=5, pid=os.getppid(), heartbeat=now)
    assert st.lock_holder(live) is not None                                     # fresh heartbeat, pid alive
    assert st.lock_holder(State(phase='grid', trials_done=1, queue_len=5, pid=os.getppid(), heartbeat=now, exit_code=0)) is None
    old = State(phase='grid', trials_done=1, queue_len=5, pid=os.getppid(), heartbeat='2000-01-01T00:00:00Z')
    assert st.lock_holder(old) is None                                          # takeover: heartbeat > 10 min
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    dead = State(phase='grid', trials_done=1, queue_len=5, pid=proc.pid, heartbeat=now)
    assert st.lock_holder(dead) is None                                         # takeover: pid gone (kill -9 case)
    assert st.lock_holder(State(phase='grid', trials_done=1, queue_len=5, pid=1, heartbeat='garbage')) is None
    sd.write_state(live)
    assert sd.is_running()
    study = sd.load_study(SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    with pytest.raises(st.LockHeld) as e:
        runner.acquire()
    assert e.value.exit_code == 3 and str(os.getppid()) in str(e.value)
    res = runner.run(st.study_tasks(study, SETTINGS), ok_evaluate)
    assert res.exit_code == 3 and res.done == 0 and sd.trials() == []
    assert sd.read_state().pid == os.getppid()                                  # untouched


def test_heartbeat_refreshes_state(tmp_path):
    sd = make_study(tmp_path)
    runner = st.Runner(sd, settings=SETTINGS, min_disk_mb=0)
    runner.acquire('oaat', queue_len=3)
    s1 = sd.read_state()
    assert s1.phase == 'oaat' and s1.queue_len == 3 and s1.exit_code is None and sd.is_running() is False  # own pid
    assert sd.read_lock()['pid'] == os.getpid()
    runner.acquire('grid', queue_len=1)                                         # the same process, next phase
    assert sd.read_lock()['pid'] == os.getpid() and sd.read_state().phase == 'grid'
    runner.heartbeat(queue_len=2)
    assert sd.read_state().queue_len == 2
    runner.release(0, 'test')
    assert sd.read_state().exit_code == 0 and os.path.isfile(sd.status_path)
    assert sd.read_lock() is None                                               # a clean release leaves no LOCK


def test_lock_file_is_created_atomically_and_taken_over(tmp_path):
    sd = make_study(tmp_path)
    assert sd.read_lock() is None and sd.try_create_lock(pid=os.getppid()) is True
    assert sd.try_create_lock() is False                                         # exists: exactly one creator
    assert sd.read_lock() == {'pid': os.getppid(), 'heartbeat': sd.read_lock()['heartbeat']}
    assert not [f for f in os.listdir(sd.path) if f.endswith('.tmp')]
    # a LOCK of a live pid with no STATE.json yet (the moment after creation) holds the study
    holder = sd.lock_holder()
    assert holder is not None and str(os.getppid()) in holder and 'STATE.json not written yet' in holder
    runner = st.Runner(sd, settings=SETTINGS, min_disk_mb=0)
    with pytest.raises(st.LockHeld):
        runner.acquire()
    assert sd.read_lock()['pid'] == os.getppid()                                # untouched
    # ... unless that LOCK is older than the stale limit
    assert sd.lock_holder(now=time.time() + st.LOCK_STALE_S + 1) is None
    # a LOCK of a dead pid is free and taken over; the new LOCK names the taker
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    assert sd.steal_lock() is True and sd.try_create_lock(pid=proc.pid) is True
    assert sd.lock_holder() is None
    runner.acquire('grid', queue_len=0)
    assert sd.read_lock()['pid'] == os.getpid() and sd.read_state().pid == os.getpid()
    assert not os.path.exists(sd.lock_path + '.takeover')                       # the takeover mutex is released
    assert sd.remove_lock(pid=12345) is False and sd.remove_lock() is True and sd.read_lock() is None
    assert sd.steal_lock() is False                                             # nothing to steal: gone
    # a taker never removes a lock that is held, and gives way while another taker holds the mutex
    sd.try_create_lock(pid=os.getppid())
    assert sd.take_over_lock() is False and sd.read_lock()['pid'] == os.getppid()
    sd.steal_lock()
    sd.try_create_lock(pid=proc.pid)
    os.mkdir(sd.lock_path + '.takeover')
    assert sd.take_over_lock() is False and sd.read_lock()['pid'] == proc.pid    # mutex taken: re-check later
    os.utime(sd.lock_path + '.takeover', (time.time() - 3600, time.time() - 3600))
    assert sd.take_over_lock() is False and not os.path.exists(sd.lock_path + '.takeover')   # stale mutex cleared
    assert sd.take_over_lock() is True and sd.read_lock()['pid'] == os.getpid()
    sd.remove_lock()
    # a LOCK with a dead pid plus a STATE.json with exit_code: free (the kill -9 and the clean-exit cases)
    sd.try_create_lock(pid=proc.pid)
    sd.write_state(State(phase='grid', trials_done=1, queue_len=5, pid=proc.pid, heartbeat=c.now_iso()))
    assert sd.lock_holder() is None
    sd.steal_lock()
    sd.try_create_lock(pid=os.getppid())
    sd.write_state(State(phase='grid', trials_done=1, queue_len=5, pid=os.getppid(), heartbeat=c.now_iso(), exit_code=0))
    assert sd.lock_holder() is None
    sd.write_state(State(phase='grid', trials_done=1, queue_len=5, pid=os.getppid(), heartbeat=c.now_iso()))
    assert sd.lock_holder() is not None                                         # live pid, fresh heartbeat
    with open(sd.lock_path, 'w') as fh:
        fh.write('garbage')                                                     # unreadable LOCK: free (taken over)
    assert sd.read_lock() == {'pid': 0, 'heartbeat': ''} and sd.lock_holder() is None


ACQUIRE_SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, %(software)r)
    from hindcast import study as st
    sd = st.StudyDir(sys.argv[1])
    go = sys.argv[2]
    runner = st.Runner(sd, settings={}, min_disk_mb=0)
    print('READY', flush=True)
    while not os.path.exists(go):
        pass                                                                # spin: both start within microseconds
    try:
        runner.acquire('race', queue_len=0)
        print('ACQUIRED', os.getpid(), sd.read_lock()['pid'], flush=True)
        time.sleep(0.5)
        runner.release(0, 'race test')
    except st.LockHeld as e:
        print('HELD', str(e), flush=True)
''') % {'software': SOFTWARE_DIR}


@pytest.mark.parametrize('stale', [False, True], ids=['no lock', 'stale lock of a dead pid'])
def test_two_simultaneous_acquires_let_exactly_one_through(tmp_path, stale):
    """Two processes call acquire() within microseconds of each other (three rounds): exactly one acquires, the
    other is refused with LockHeld naming the winner. With `stale` the study starts with a LOCK of a dead pid (the
    kill -9 case) so both go through the takeover path."""
    sd = twenty_task_study(tmp_path)
    script = tmp_path / 'acquire.py'
    script.write_text(ACQUIRE_SCRIPT, encoding='utf-8')
    env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=SOFTWARE_DIR)
    for round_ in range(3):
        if stale:
            dead = subprocess.Popen([sys.executable, '-c', 'pass'])
            dead.wait()
            sd.remove_lock(pid=sd.read_lock()['pid']) if sd.read_lock() else None
            assert sd.try_create_lock(pid=dead.pid) is True
        go = tmp_path / f'GO{round_}'
        procs = [subprocess.Popen([sys.executable, str(script), sd.path, str(go)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for p in procs:
                assert p.stdout.readline().strip() == 'READY'
            go.write_text('go')
            outs = [p.communicate(timeout=60) for p in procs]
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
        lines = [o.strip() for o, _ in outs]
        assert all(p.returncode == 0 for p in procs), outs
        acquired = [l for l in lines if l.startswith('ACQUIRED')]
        held = [l for l in lines if l.startswith('HELD')]
        assert len(acquired) == 1 and len(held) == 1, lines
        _, pid, lock_pid = acquired[0].split()
        assert pid == lock_pid and pid in held[0] and 'another run holds this study' in held[0]
        assert sd.read_state().pid == int(pid) and sd.read_state().exit_code == 0 and sd.read_lock() is None
        assert not os.path.exists(sd.lock_path + '.takeover')
        assert not [f for f in os.listdir(sd.path) if f.endswith(('.tmp', '.stale'))]


# ----------------------------------------------------------------------------- status.json

def test_status_fits_2kb_and_counts_latest_rows(tmp_path):
    sd = make_study(tmp_path, days=('2026-09-05', '2026-09-06'), holdout=['2026-09-05'])
    status = st.build_status(sd)
    assert status.trials_done == 0 and status.phase == 'grid' and not status.running
    assert status.days == {'2026-09-05': 'holdout', '2026-09-06': 'search'}
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, lambda t: {'status': 'blocked', 'error': 'not frozen'})
    status = st.build_status(sd)
    assert status.trials_blocked == 10 and status.trials_done == 0 and status.trials_planned == 10
    st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, ok_evaluate)
    status = st.build_status(sd)
    assert status.trials_done == 10 and status.trials_blocked == 0 and status.trials_planned == 10
    assert status.floor['agreement_pct'] == 98.3 and status.floor['floor_loops'] == 3.0
    sd.write_status(status)
    size = os.path.getsize(sd.status_path)
    assert size <= 2048
    with open(sd.status_path, encoding='utf-8') as fh:
        c.Status.from_dict(json.load(fh)).validate()
    text = st.status_text(status)
    assert c.HONESTY_STATEMENT in text and 'review order' not in text
    for word in ('better', 'optimal', 'safer', 'fewer lows'):
        assert word not in text


def test_status_shows_an_unschedulable_sentinel_from_guards_json(tmp_path):
    """guards.json is private (not a STATE/status contract field): `hc study status` prints its sentinel note, the
    status.json contract is unchanged, and a schedulable sentinel prints nothing."""
    sd = make_study(tmp_path)
    assert st.guard_lines(sd) == [] and sd.read_guards() == {}
    sd.update_guards(sentinel={'schedulable': False, 'days': {'2026-09-06': ['profile autoISF_max: not in ns_settings.json']}})
    status = st.build_status(sd)
    text = st.status_text(status)
    assert 'guards: sentinel: not schedulable on 2026-09-06 (profile autoISF_max: not in ns_settings.json)' in text
    assert 'guards' not in status.to_dict()
    sd.write_status(status)
    c.Status.load(sd.status_path).validate()
    sd.update_guards(sentinel={'schedulable': True, 'days': {}})
    assert 'not schedulable' not in st.status_text(st.build_status(sd))
    sd.update_guards(sentinel=None)
    assert 'sentinel' not in sd.read_guards()


def test_status_top_from_round_json_is_trimmed(tmp_path):
    sd = make_study(tmp_path, days=tuple(f'2026-09-{d:02d}' for d in range(1, 8)))
    variants = []
    for i in range(10):
        params = {'profile autoISF_max': 1.3 + i * 0.01, 'profile dura_ISF_weight': 0.5}
        variants.append(c.Candidate(vhash=c.vhash(params), variant=f'autoISF_max_{1.3 + i * 0.01:.2f}__dura_ISF_weight_0.5_long_label_{i}',
                                    verdict='candidate', why='asks for more insulin in rising loops ' * 6, params=params, rank=i + 1,
                                    effect={'rise_vs_base_U': 0.4, 'rest_vs_base_U': 0.01, 'changed_vs_base': 12, 'lowside_flags': 0,
                                            'extra': 1.0}))
    rnd = c.Round(round=1, phase='grid', started=c.now_iso(), variants=variants, front=[v.vhash for v in variants],
                  dead_parameters=[c.DeadParameter('profile dura_ISF_weight', 'all points inside 1.5 x floor')])
    rnd.save(sd.round_path)
    status = st.build_status(sd)
    assert 1 <= len(status.top) <= 5 and all(len(t.effect) <= 4 and len(t.why) <= 120 for t in status.top)
    assert status.dead_parameters == ['profile dura_ISF_weight']
    assert len(status.to_compact_json().encode('utf-8')) <= 2048
    # fill the status up to the limit (one more day would overflow): the file on disk must be the compact form the
    # limit was checked on; with json.dump's default separators it would be 6-8 % larger and break the limit
    i = 0
    while len(status.to_compact_json().encode('utf-8')) <= 2048 - 32:
        status.days[f'2027-01-{i % 28 + 1:02d}.{i}'] = 'search'
        i += 1
    status.validate()
    compact = status.to_compact_json().encode('utf-8')
    assert 2048 - 32 < len(compact) <= 2048
    assert len(json.dumps(status.to_dict(), ensure_ascii=False).encode('utf-8')) > 2048
    sd.write_status(status)
    size = os.path.getsize(sd.status_path)
    assert size <= 2048 and size == len(compact)
    with open(sd.status_path, 'rb') as fh:
        assert fh.read() == compact
    assert c.Status.from_json(compact.decode('utf-8')).validate().top == status.top
    text = st.status_text(status)
    assert all(t.variant in text for t in status.top)


def test_cut_words_cuts_at_a_word_boundary_with_an_ellipsis():
    text = 'search review order 4, feasible on every search and hold-out day, direction holds on 13/13 days'
    assert st.cut_words(text, 200) == text
    out = st.cut_words(text, 40)
    assert out == 'search review order 4, feasible on...' and len(out) <= 40
    assert st.cut_words('abcdefghijklmnopqrstuvwxyz', 10) == 'abcdefg...'     # one long word: a hard cut
    for n in range(10, len(text)):                                    # the first word fits from 9 on
        cut = st.cut_words(text, n)
        assert len(cut) <= n and cut.endswith('...')
        head = cut[:-3]
        assert text.startswith(head) and text[len(head)] in ' ,;:-('


def test_status_fitter_drops_lower_value_fields_before_cutting_why(tmp_path):
    """the H-308 observation: a 'why' cut at 40 characters mid-phrase. The fitter drops effect numbers, the stop
    reason length and the quarantine reasons first, shortens the why at a word boundary with an ellipsis, and never
    cuts mid-word"""
    sd = make_study(tmp_path, days=tuple(f'2026-09-{d:02d}' for d in range(1, 15)))
    why = ('search review order 4, feasible on every search and hold-out day, in the top 3 on 100% of 19 '
           'leave-one-day-out folds, direction holds on 13/13 hold-out days, hold-out rank 2: recommended for review')
    variants = []
    for i in range(5):
        params = {'profile autoISF_max': 1.3 + i * 0.01, 'profile dura_ISF_weight': 0.5}
        variants.append(c.Candidate(vhash=c.vhash(params), variant=f'autoISF_max_{1.3 + i * 0.01:.2f}__dura_ISF_weight_0.5_{i}',
                                    verdict='recommended', why=why, params=params, rank=i + 1,
                                    effect={'rise_vs_base_U': 0.4, 'rest_vs_base_U': 0.01, 'changed_vs_base': 12},
                                    validation={'holdout_rank': 1, 'holdout_total': 8, 'holds': 13, 'holdout_days': 13,
                                                'lodo_min': 1, 'lodo_max': 4}))
    c.Round(round=1, phase='validate', started=c.now_iso(), variants=variants, front=[v.vhash for v in variants],
            validation={'top': 8}).save(sd.round_path)
    status = st.build_status(sd)
    assert len(status.to_compact_json().encode('utf-8')) <= 2048 and status.top
    for t in status.top:
        if t.why != why[:len(t.why)] or len(t.why) < len(why):
            assert t.why.endswith('...'), t.why
            head = t.why[:-3]
            assert why.startswith(head) and why[len(head)] in ' ,;:-(', t.why
            assert t.effect == {}                                    # the numbers went before the words did
        assert t.validation and t.validation.startswith('hold-out: rank 1 of 8')


@pytest.mark.parametrize('verdict', c.VERDICTS)
def test_status_text_passes_the_line_lint_for_every_verdict(verdict):
    from hindcast import lint
    status = c.Status(study='s1', question='explore', phase='grid', trials_done=12, trials_planned=20, running=True,
                      heartbeat=c.now_iso(), pid=4711, trials_failed=1, trials_blocked=2, exit_code=None,
                      stale_context=['code_sha changed since study.json: 0123456789ab -> ba9876543210'],
                      floor={'smb_U': 0.05, 'agreement_pct': 96.1, 'floor_loops': 3.0},
                      days={'2026-09-05': 'holdout', '2026-09-06': 'search', '2026-09-07': 'quarantined: cgm gap 4 h'},
                      dead_parameters=['profile dura_ISF_weight'],
                      top=[c.TopItem(vhash='ab' * 20, variant='autoISF_max_1.4__dura_ISF_weight_0.6', verdict=verdict,
                                     why='asks for more insulin in the two hours before a recorded low', rank=1,
                                     effect={'rise_vs_base_U': 0.4, 'rest_vs_base_U': 0.01})]).validate()
    text = st.status_text(status)
    findings = lint.lint_findings(text, 'line')
    assert findings == [], [str(f) for f in findings]
    assert f'[{st.verdict_label(verdict)}]' in text
    if verdict == 'recommended':
        assert '[recommended for review]' in text and '[recommended]' not in text
    assert c.HONESTY_STATEMENT in text
    exit_status = c.Status(study='s1', question='explore', phase='grid', trials_done=1, trials_planned=1, running=False,
                           heartbeat=c.now_iso(), exit_code=2, stop_reason='guard: 3 consecutive failed trials')
    assert lint.lint_findings(st.status_text(exit_status), 'line') == []


def test_key_and_vhash_normalise_off_grid_params(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    on_grid = {'profile autoISF_max': 1.4, 'profile dura_ISF_weight': 0.6}
    off_grid = {'profile dura_ISF_weight': 0.6000000001, 'profile autoISF_max': 1.4000000001}    # other key order too
    a, b = st.Task(params=on_grid, day='2026-09-06'), st.Task(params=off_grid, day='2026-09-06')
    assert runner.key_of(a) == runner.key_of(b)
    assert runner.vhash_of(b) == c.vhash(on_grid) and runner.params_of(b) == on_grid
    assert runner.variant_of(b) == 'autoISF_max_1.4__dura_ISF_weight_0.6'
    res = runner.run([b], ok_evaluate)
    row = sd.trials()[-1]
    assert res.done == 1 and row.vhash == c.vhash(on_grid) and row.params == on_grid and row.key == runner.key_of(a)
    assert row.variant == 'autoISF_max_1.4__dura_ISF_weight_0.6'
    # the on-grid task is now done: the resume rule recognises it through the same key
    res2 = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run([a], ok_evaluate)
    assert res2.skipped == 1 and res2.done == 0
    # the base task stays the base; a key outside the space is a configuration error (exit 3)
    assert runner.key_of(st.Task(params={}, day='2026-09-06')) == c.key({}, '2026-09-06', runner.settings_sha, runner.code_sha,
                                                                       '', study.objective_version)
    with pytest.raises(st.StudyError, match='profile nothere'):
        runner.key_of(st.Task(params={'profile nothere': 1}, day='2026-09-06'))


# ----------------------------------------------------------------------------- acceptance: kill -9 and concurrent runs

RUN_SCRIPT = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, %(software)r)
    from hindcast import study as st
    from hindcast.contracts import Phase
    sd = st.StudyDir(sys.argv[1])
    sleep = float(sys.argv[2])
    study = sd.load_study({})
    tasks = st.study_tasks(study, {})
    def evaluate(task):
        time.sleep(sleep)
        return {'metrics': {'loops': 180, 'changed_loops': 1}, 'seconds': sleep}
    res = st.Runner(sd, study, settings={}, min_disk_mb=0).run(tasks, evaluate)
    print('RESULT', res.exit_code, res.done, res.skipped, flush=True)
    sys.exit(res.exit_code)
''') % {'software': SOFTWARE_DIR}


def twenty_task_study(tmp_path):
    space = {'profile autoISF_max': {'type': 'float', 'low': 1.1, 'high': 1.5, 'step': 0.1},    # 5 values
             'profile dura_ISF_weight': {'type': 'float', 'low': 0.5, 'high': 0.8, 'step': 0.1}}  # 4 values
    sd = st.new_study('kill', ['2026-09-06'], space, phases=[Phase(kind='grid')], settings={'safety': {}},
                      studies_dir=str(tmp_path))
    assert len(st.study_tasks(sd.load_study({}), {})) == 21                  # 20 grid points + base
    return sd


def start_run(tmp_path, sd, sleep):
    script = tmp_path / 'run.py'
    script.write_text(RUN_SCRIPT, encoding='utf-8')
    env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=SOFTWARE_DIR)
    return subprocess.Popen([sys.executable, str(script), sd.path, str(sleep)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(cond, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_kill_9_mid_trial_loses_at_most_one_trial(tmp_path):
    sd = twenty_task_study(tmp_path)
    proc = start_run(tmp_path, sd, sleep=0.12)
    try:
        assert wait_for(lambda: len(sd.trials()) >= 6), proc.stderr.read()
        time.sleep(0.06)                                                        # land inside a trial, not between two
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    rows_before = sd.trials()
    n_before = len(rows_before)
    assert 6 <= n_before < 21
    assert all(r.status == 'done' for r in rows_before) and len({r.key for r in rows_before}) == n_before
    state = sd.read_state()
    assert state.exit_code is None and sd.lock_holder() is None                 # dead pid: the lock can be taken over
    # the re-run takes over, does only the missing trials and finishes with exit 0
    proc2 = start_run(tmp_path, sd, sleep=0.0)
    out, err = proc2.communicate(timeout=60)
    assert proc2.returncode == 0, err
    result = [l for l in out.splitlines() if l.startswith('RESULT')][0].split()
    exit_code, done, skipped = int(result[1]), int(result[2]), int(result[3])
    assert exit_code == 0 and skipped == n_before and done == 21 - n_before
    rows = sd.trials()
    assert len(rows) == 21 and len({r.key for r in rows}) == 21
    assert [r.trial for r in rows] == list(range(21))
    assert sd.read_state().exit_code == 0 and os.path.getsize(sd.status_path) <= 2048


def test_torn_last_line_does_not_swallow_the_next_row(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks[:2], ok_evaluate)
    with open(sd.trials_path, 'a', encoding='utf-8') as fh:
        fh.write('{"trial": 2, "key": "deadbeef", "truncated')                  # kill -9 inside a write
    assert len(sd.trials()) == 2
    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks[:3], ok_evaluate)
    assert res.done == 1 and res.skipped == 2
    rows = sd.trials()
    assert len(rows) == 3 and rows[-1].trial == 2


def test_second_concurrent_run_exits_3(tmp_path):
    sd = twenty_task_study(tmp_path)
    proc = start_run(tmp_path, sd, sleep=0.5)
    try:
        assert wait_for(lambda: sd.read_state() is not None and sd.read_state().exit_code is None)
        env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=SOFTWARE_DIR)
        second = subprocess.run([sys.executable, '-m', 'hindcast', 'study', 'run', sd.path, '--studies-dir', str(tmp_path)],
                                env=env, cwd=SOFTWARE_DIR, capture_output=True, text=True, timeout=60)
        assert second.returncode == 3, second.stdout + second.stderr
        assert 'another run holds this study' in second.stdout + second.stderr
        assert str(proc.pid) in second.stdout + second.stderr
        # in-process too
        res = st.Runner(sd, settings={}, min_disk_mb=0).run(st.study_tasks(sd.load_study({}), {}), ok_evaluate)
        assert res.exit_code == 3
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    assert sd.read_state().pid == proc.pid                                      # the first run's state was never touched


# ----------------------------------------------------------------------------- session, batch exceptions (wave 3 fixes)

def test_session_holds_one_lock_across_runs_and_keeps_a_stop(tmp_path):
    """open_session ... close_session: the runs inside neither release the lock nor remove a STOP file; the one
    release carries the real exit code and removes the STOP that stopped the run (told by RunResult.stop_file)."""
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    sd.request_stop('left over')
    runner.open_session('planning round 1')
    assert not sd.stop_requested()                                              # a whole run starts: cleared once
    res = runner.run(tasks[:4], ok_evaluate)
    assert res.exit_code == 0 and res.done == 4 and res.already_done == 0
    state = sd.read_state()
    assert state.exit_code is None and sd.read_lock()['pid'] == os.getpid()     # still held between the runs
    sd.request_stop('between rounds')
    res2 = runner.run(tasks, ok_evaluate)                                       # checked before the first trial
    assert res2.stop_reason == st.STOP_REASON and res2.stop_file and res2.done == 0 and res2.already_done == 4
    assert sd.stop_requested() and sd.read_state().exit_code is None            # kept for the orchestrator
    runner.close_session(0, st.STOP_REASON, stop_file=res2.stop_file)
    assert sd.read_state().exit_code == 0 and sd.read_lock() is None and not sd.stop_requested()


def test_an_exception_out_of_batch_evaluate_is_exit_2_with_the_rows_kept(tmp_path):
    """The batch path: rows recorded before the exception stay, the run exits 2 with the exception's type and first
    line (home directory shortened, lint-clean) as the stop reason, and the lock is released."""
    from hindcast import lint
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    home = os.path.expanduser('~')

    def batch_evaluate(queue):
        yield queue[0], ok_evaluate(queue[0])
        yield queue[1], ok_evaluate(queue[1])
        raise RuntimeError(f'pool broke reading {home}/x.json\nsecond line')

    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, None, batch_evaluate=batch_evaluate)
    assert res.exit_code == st.EXIT_GUARD and res.done == 2 and len(sd.trials()) == 2
    assert res.stop_reason == 'guard: the evaluation raised RuntimeError: pool broke reading ~/x.json'
    assert not lint.lint_findings(res.stop_reason, 'issue')
    state = sd.read_state()
    assert state.exit_code == st.EXIT_GUARD and state.stop_reason == res.stop_reason and sd.read_lock() is None


def test_a_runstop_from_the_batch_iterator_is_a_clean_exit(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)

    def batch_evaluate(queue):
        yield queue[0], ok_evaluate(queue[0])
        sd.request_stop('while waiting')
        runner.idle()                                                           # what the pool calls on a wake

    res = runner.run(tasks, None, batch_evaluate=batch_evaluate)
    assert (res.exit_code, res.stop_reason, res.done, res.stop_file) == (0, st.STOP_REASON, 1, True) and not sd.stop_requested()


def test_status_keeps_the_planned_count_after_a_stop(tmp_path):
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)

    def evaluate(task):
        if len(sd.trials()) == 2:
            sd.request_stop()
        return ok_evaluate(task)
    st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0).run(tasks, evaluate)
    status = st.build_status(sd)
    assert status.exit_code == 0 and status.trials_done == 3 and status.trials_planned == 10


# ----------------------------------------------------------------------------- stop reason, close order, round lines (verifier fixes)

def test_close_session_removes_stop_by_the_flag_not_by_the_reason_text(tmp_path):
    """close_session decides on the explicit stop_file flag: a reason that only mentions a STOP file leaves the
    file alone, the flag removes it whatever the reason text says."""
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    runner.open_session('planning round 1')
    sd.request_stop('for the next run')
    runner.close_session(0, 'budget exhausted; the STOP file came later', stop_file=False)
    assert sd.stop_requested() and sd.read_lock() is None
    runner.open_session('planning round 1')                                     # a whole run starts: cleared
    sd.request_stop('now')
    runner.close_session(0, 'any wording', stop_file=True)
    assert not sd.stop_requested() and sd.read_state().stop_reason == 'any wording'


def test_one_stop_reason_inside_a_round_and_from_idle(tmp_path):
    """The STOP file gives study.STOP_REASON and stop_file=True on the serial path and through idle(); a budget
    stop gives neither."""
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    runner.open_session('planning round 1')
    sd.request_stop('x')
    with pytest.raises(st.RunStop) as e:
        runner.idle()
    assert e.value.reason == st.STOP_REASON and e.value.stop_file is True
    runner.close_session(0, e.value.reason, stop_file=e.value.stop_file)
    res = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0, budget_s=0).run(tasks, ok_evaluate)
    assert res.stop_reason == 'budget exhausted' and res.stop_file is False


def test_close_session_order_stop_then_publish_under_the_lock_then_release(tmp_path):
    """Delete STOP first, run before_release (tier 1, report) while the lock is held - build_status in this process
    already shows the final exit code and reason, STATE.json on disk does not - and release last. An exception in
    before_release still releases."""
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = st.study_tasks(study, SETTINGS)
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0)
    runner.open_session('planning round 1')
    sd.request_stop('test')
    res = runner.run(tasks, ok_evaluate)
    assert res.stop_file and sd.stop_requested()
    seen = {}

    def before_release():
        status = st.build_status(sd)
        seen.update(stop=sd.stop_requested(), lock=(sd.read_lock() or {}).get('pid'),
                    disk_exit=sd.read_state().exit_code, exit=status.exit_code, reason=status.stop_reason,
                    running=status.running)
        raise RuntimeError('the page broke')

    runner.close_session(0, res.stop_reason, stop_file=res.stop_file, before_release=before_release)
    assert seen == {'stop': False, 'lock': os.getpid(), 'disk_exit': None, 'exit': 0, 'reason': st.STOP_REASON,
                    'running': False}
    state = sd.read_state()
    assert state.exit_code == 0 and state.stop_reason == st.STOP_REASON and sd.read_lock() is None
    assert sd.closing is None and json.load(open(sd.status_path))['exit_code'] == 0


def test_a_round_inside_a_session_logs_a_round_line_not_an_exit(tmp_path):
    """Inside a session runner.run logs 'round N recorded: ...' (the exit line belongs to the session's close); a
    standalone run keeps its 'finished; ...; exit 0' line."""
    import dataclasses
    sd = make_study(tmp_path)
    study = sd.load_study(SETTINGS)
    tasks = [dataclasses.replace(t, round=4) for t in st.study_tasks(study, SETTINGS)]
    lines = []
    runner = st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0, log=lines.append)
    runner.open_session('planning round 4')
    runner.run(tasks[:2], ok_evaluate)
    sd.request_stop('x')
    runner.run(tasks, ok_evaluate)
    runner.close_session(0, st.STOP_REASON, stop_file=True)
    assert lines == ['s1: round 4 recorded: 2 rows appended, 0 already done, 0 failed, 0 blocked',
                     's1: round 4 recorded: 0 rows appended, 2 already done, 0 failed, 0 blocked; ' + st.STOP_REASON]
    lines.clear()
    st.Runner(sd, study, settings=SETTINGS, min_disk_mb=0, log=lines.append).run(tasks[:3], ok_evaluate)
    assert lines == ['s1: finished; 1 rows appended, 2 already done, 0 failed, 0 blocked; exit 0']
