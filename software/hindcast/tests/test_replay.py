"""
test_replay.py  -  replay.run_trial() around parameters_known() with the frozen source (backlog H-102)

Offline unit tests run everywhere. The replay tests need the frozen day 2026-09-06 under software/frozen/<host>/
(`hc freeze 2026-09-06`, offline from the Nightscout cache) and skip cleanly without it; the live comparisons also
need the cached days and are marked slow (`--run-slow`, ~30 s).
"""
import filecmp
import os
import socket

import pytest

from hindcast import contracts, days, freeze, replay
from hindcast.tests.conftest import CHECK_WINDOW

DAY = '2026-09-06'
SHORT = ('2026-09-06T06:00:00Z', '2026-09-06T07:00:00Z')        # the quick replay window
SHORT_LOOPS = 59                                                 # loops of 2026-09-06 in SHORT (frozen meta)


def _frozen_ok(day=DAY):
    try:
        return freeze.blocked_reason(day) is None
    except Exception:                                            # no settings, no frozen dir, ...
        return False


needs_frozen = pytest.mark.skipif(not _frozen_ok(), reason=f'{DAY} not frozen (or stale): run `hc freeze {DAY}`')


# ----------------------------------------------------------------------------- offline unit tests

def test_spec_validation_and_identity():
    with pytest.raises(replay.ReplayError):
        replay.TrialSpec(day='2026-9-6')
    with pytest.raises(replay.ReplayError):
        replay.TrialSpec(day=DAY, source='socket')
    base = replay.TrialSpec(day=DAY)
    assert base.variant == 'base' and base.vhash == contracts.vhash({})
    var = replay.TrialSpec(day=DAY, params={'profile autoISF_max': 1.5}, base_lines=['', 'profile autoISF_min 0.9\n'])
    assert var.variant != 'base' and var.vhash != base.vhash
    assert var.base_lines == ['profile autoISF_min 0.9']
    assert var.vdf_name() == contracts.short(var.vhash) + '.vdf'
    assert any(l.split()[:3] == ['profile', 'autoISF_max', '1.5'] for l in var.vdf_lines())
    assert base.resolved_window() == days.day_window(DAY) and base.whole_day()
    w = replay.TrialSpec(day=DAY, window=CHECK_WINDOW)
    assert not w.whole_day()
    assert w.window_label() == '2026-09-06T06-00-00Z_2026-09-06T09-00-00Z'


def test_result_paths_follow_the_core_naming():
    p = replay.result_paths('/w', 'L', 'abc.vdf')
    assert p == {'csv': '/w/L.abc.csv', 'txt': '/w/L.abc.txt', 'log': '/w/L.abc.log', 'delta': '/w/L.abc.delta',
                 'orig': '/w/L.orig.txt'}


def test_log_problem_extracts_the_vdf_block(tmp_path):
    log = tmp_path / 'x.log'
    log.write_text('banner\n\nloop execution in row=1\n*******\nProblem in VDF-file in row 1 reading\n'
                   'profile autoISF_max  1.5 +\n\nerror message is:<class \'SyntaxError\'>\n*******\n', encoding='utf-8')
    text = replay.log_problem(str(log))
    assert text.startswith('Problem in VDF-file in row 1') and 'SyntaxError' in text and '***' not in text
    assert replay.log_problem(str(tmp_path / 'missing.log')) == ''


def test_source_installed_swaps_and_restores_the_real_module_attribute():
    import ns_source
    live = ns_source.NightscoutSource
    with replay.source_installed('frozen', '/nowhere'):
        cls = ns_source.NightscoutSource
        assert cls is not live and issubclass(cls, freeze.FrozenSource)
        assert cls('ns:', SHORT[0], SHORT[1]).base_dir == '/nowhere'
    assert ns_source.NightscoutSource is live
    with pytest.raises(RuntimeError):
        with replay.source_installed('live-day'):
            assert ns_source.NightscoutSource is replay.DayLiveSource
            raise RuntimeError('boom')
    assert ns_source.NightscoutSource is live                    # restored on an exception too


def test_core_still_uses_the_attributes_hindcast_relies_on():
    """The frozen injection needs no core edit only while scanNightscout constructs ns_source.NightscoutSource by
    attribute and the core keeps Pred / FlowChart as module globals (DESIGN.md section 4.4)."""
    import emulator_core as ec
    src = open(ec.__file__, encoding='utf-8').read()
    assert 'ns_source.NightscoutSource(' in src
    assert 'ns_source.resolve_window(' in src and 'ns_source.window_label(' in src
    assert hasattr(ec, 'parameters_known') and hasattr(ec, 'set_tty')
    assert 'global Pred, FlowChart' in src


def test_unfrozen_day_is_blocked_without_touching_the_core(tmp_path):
    res = replay.run_trial(replay.TrialSpec(day=DAY, frozen_dir=str(tmp_path)))
    assert res.status == 'blocked' and 'not frozen' in res.error
    row = res.to_row()
    assert set(row) == {'status', 'metrics', 'seconds', 'error', 'flags', 'feasible', 'files', 'worker', 'metrics_version'}
    assert row['metrics_version'] == replay.hm.METRICS_VERSION
    assert row['status'] == 'blocked' and row['metrics'] == {}


def test_echo_banner_names_the_trial():
    spec = replay.TrialSpec(day=DAY, params={'profile autoISF_max': 1.5}, key='k' * 40)
    text = replay.echo_banner(spec, 'f' * 40)
    assert spec.variant in text and spec.vhash in text and 'k' * 40 in text and 'f' * 40 in text


def test_context_spec_for_attaches_the_base_csv(tmp_path):
    from hindcast.study import Task
    ctx = replay.ReplayContext(runs_dir=str(tmp_path / 'runs'), report_dir=str(tmp_path / 'report'))
    base = ctx.spec_for(Task(params={}, day=DAY, control='base'))
    assert base.keep_csv == os.path.join(str(tmp_path / 'runs'), 'base', f'{DAY}.csv') and base.base_csv is None
    var = Task(params={'profile autoISF_max': 1.5}, day=DAY)
    assert ctx.spec_for(var).base_csv is None
    done = replay.TrialResult(status='done', day=DAY, vhash=base.vhash, variant='base', csv=base.keep_csv)
    ctx.absorb(done)
    assert ctx.spec_for(var).base_csv == base.keep_csv and ctx.spec_for(var).keep_csv is None


def test_run_name_is_unique_per_day_window_control_and_instance():
    """Finding (1): the per-trial file name must not fall back to a per-variant string."""
    p = {'profile autoISF_max': 1.5}
    a, b = replay.TrialSpec(day=DAY, params=p), replay.TrialSpec(day='2026-09-05', params=p)
    assert a.short_key() == b.short_key()                         # the display form is per variant without a key
    assert a.run_name() != b.run_name()                           # the file name is not
    assert len(a.run_name()) == 40 and a.run_name() == replay.TrialSpec(day=DAY, params=p).run_name()
    names = {replay.TrialSpec(day=DAY, params=p, **kw).run_name() for kw in (
        {}, {'window': SHORT}, {'control': 'base'}, {'control': 'base', 'instance': 1},
        {'control': 'base', 'instance': 2}, {'source': 'live-day'}, {'key': 'k' * 40}, {'key': 'j' * 40})}
    assert len(names) == 8
    k1 = replay.TrialSpec(day=DAY, params=p, key='k' * 40)
    assert k1.run_name() != replay.TrialSpec(day='2026-09-05', params=p, key='k' * 40).run_name()


class StubLiveSource:
    """Stands in for the real ns_source.NightscoutSource: no network, records that it was constructed."""
    built = []

    def __init__(self, url, start_iso, end_iso, settings=None, log_msg=print, verbose=True, **kw):
        StubLiveSource.built.append((url, start_iso, end_iso))

    def load(self):
        raise RuntimeError('stub live source: no data')

    def loops(self):
        return iter(())


def test_live_source_is_installed_explicitly_over_a_permanent_frozen_install(monkeypatch):
    """Finding (2): a pool worker has the frozen source installed for good; source='live' must not keep it."""
    import ns_source
    monkeypatch.setattr(replay, 'LIVE_SOURCE', StubLiveSource)
    real = ns_source.NightscoutSource
    try:
        replay.install_frozen('/nowhere')                         # what pool.default_init does in every worker
        frozen = ns_source.NightscoutSource
        assert issubclass(frozen, freeze.FrozenSource)
        with replay.source_installed('live') as cls:
            assert cls is StubLiveSource and ns_source.NightscoutSource is StubLiveSource
        assert ns_source.NightscoutSource is frozen               # the worker's install is restored afterwards
        monkeypatch.setattr(replay, 'LIVE_SOURCE', freeze.FrozenSource)
        with pytest.raises(replay.ReplayError):
            with replay.source_installed('live'):
                pass
    finally:
        ns_source.NightscoutSource = real
    assert replay.source_class('live-day') is replay.DayLiveSource
    assert not issubclass(getattr(freeze, '_LIVE_SOURCE', real), freeze.FrozenSource)


def test_live_trial_in_a_frozen_process_replays_the_live_class_and_says_so(monkeypatch, tmp_path):
    import ns_source
    monkeypatch.setattr(replay, 'LIVE_SOURCE', StubLiveSource)
    StubLiveSource.built.clear()
    real = ns_source.NightscoutSource
    try:
        replay.install_frozen()
        res = replay.run_trial(replay.TrialSpec(day=DAY, window=SHORT, source='live', runs_dir=str(tmp_path / 'runs')))
    finally:
        ns_source.NightscoutSource = real
    assert StubLiveSource.built, 'the core did not construct the live class'
    assert 'source:live:StubLiveSource' in res.flags
    assert res.status == 'failed'                                 # the stub has no loops: an honest failure, not frozen data


def test_failed_base_blocks_the_day_for_this_run(tmp_path):
    """Finding (3): a base CSV from an earlier run must not be reused when this run's base replay of the day fails."""
    from types import SimpleNamespace
    from hindcast.study import Task
    runs = tmp_path / 'runs'
    (runs / 'base').mkdir(parents=True)
    stale = runs / 'base' / f'{DAY}.csv'
    stale.write_text('an earlier run\n', encoding='utf-8')
    study = SimpleNamespace(base_vdf_lines=[], outputs=None, space=['profile autoISF_max'], source='ns:')
    ctx = replay.ReplayContext.from_study(str(tmp_path), study, frozen_dir=str(tmp_path / 'frozen'))
    assert ctx.base_csvs[DAY] == str(stale)                       # pre-loaded from the earlier run
    base_task = Task(params={}, day=DAY, control='base')
    var_task = Task(params={'profile autoISF_max': 1.5}, day=DAY)
    base = replay.run_trial(ctx.spec_for(base_task))              # fails here: the day is not frozen under tmp
    assert base.status != 'done'
    assert not stale.exists()                                     # the stale file at keep_csv is gone
    ctx.absorb(base)
    assert DAY not in ctx.base_csvs and DAY in ctx.base_failed
    spec = ctx.spec_for(var_task)
    assert spec.base_csv is None and spec.blocked and 'base replay of the day did not finish' in spec.blocked
    res = replay.run_trial(spec)
    assert res.status == 'blocked' and res.error == spec.blocked and res.metrics == {}
    assert ctx.evaluate(var_task)['status'] == 'blocked'
    assert ctx.spec_for(base_task).blocked is None                # the base itself may be retried
    assert ctx.spec_for(Task(params={}, day='2026-09-05', control='base')).blocked is None
    assert ctx.spec_for(Task(params={'profile autoISF_max': 1.5}, day='2026-09-05')).blocked is None  # other days
    ctx.absorb(replay.TrialResult(status='done', day=DAY, vhash=base.vhash, variant='base', control='base',
                                  csv=str(stale)))
    assert DAY not in ctx.base_failed and ctx.spec_for(var_task).blocked is None


# ----------------------------------------------------------------------------- replays on the frozen day

@pytest.fixture
def no_socket(monkeypatch):
    """Any socket creation raises: a frozen replay must not need one."""
    class SocketOpened(AssertionError):
        pass

    def refuse(*a, **k):
        raise SocketOpened('a socket was opened during a frozen replay')
    monkeypatch.setattr(socket, 'socket', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    return SocketOpened


@needs_frozen
def test_frozen_replay_opens_no_socket_and_cleans_up(no_socket, tmp_path):
    report = tmp_path / 'study' / 'report'
    res = replay.run_trial(replay.TrialSpec(day=DAY, window=SHORT, report_dir=str(report)))
    assert res.status == 'done', res.error
    assert res.loops == SHORT_LOOPS and res.metrics['loops'] == SHORT_LOOPS
    assert res.core_status not in replay.FAILED_STATUSES
    assert res.files['series'] == f'report/series/{DAY}/{res.vhash}.json'
    assert os.path.isfile(tmp_path / 'study' / res.files['series'])
    assert os.path.isfile(tmp_path / 'study' / res.files['orig'])
    assert 'pred' not in res.files                               # pred only on request
    assert 'no_pred' not in res.flags                            # Pred is filled also on noplot
    left = [f for f in os.listdir(replay.process_workdir()) if f.startswith(spec_label())]
    assert left == []                                            # transcripts and CSV deleted


def spec_label():
    return replay.TrialSpec(day=DAY, window=SHORT).window_label()


@needs_frozen
def test_pred_written_on_request_and_transcripts_kept(tmp_path):
    report, runs = tmp_path / 'report', tmp_path / 'runs'
    spec = replay.TrialSpec(day=DAY, window=SHORT, params={'profile autoISF_max': 1.5}, report_dir=str(report),
                            runs_dir=str(runs), pred=True, keep_transcripts=True, key='a' * 40)
    res = replay.run_trial(spec)
    assert res.status == 'done', res.error
    for k in ('series', 'orig', 'pred', 'pred_text', 'pred_orig', 'pred_orig_text'):
        assert os.path.isfile(tmp_path / res.files[k]), k
    kept = os.listdir(runs / spec.run_name())
    for ext in ('.csv', '.txt', '.log', '.orig.txt'):
        assert any(f.endswith(ext) for f in kept), ext
    assert res.csv and os.path.isfile(res.csv)
    assert os.path.isfile(res.stdout_path)


@needs_frozen
def test_syntax_error_fails_the_trial_and_the_next_one_runs(no_socket, tmp_path):
    bad = replay.run_trial(replay.TrialSpec(day=DAY, window=SHORT, base_lines=['profile autoISF_max  1.5 +  ### broken'],
                                            runs_dir=str(tmp_path / 'runs')))
    assert bad.status == 'failed' and bad.core_status == 'SYNTAX'
    assert bad.error.startswith('SYNTAX:') and 'Problem in VDF-file' in bad.error and 'SyntaxError' in bad.error
    assert bad.metrics == {} and os.path.isfile(bad.stdout_path)  # runs/<key>.out kept for a failed trial
    good = replay.run_trial(replay.TrialSpec(day=DAY, window=SHORT, params={'profile autoISF_max': 1.5}))
    assert good.status == 'done' and good.loops == SHORT_LOOPS            # the study continues: no state left behind


@needs_frozen
def test_variant_vs_base_metrics(tmp_path):
    base_csv = str(tmp_path / 'base.csv')
    base = replay.run_trial(replay.TrialSpec(day=DAY, window=CHECK_WINDOW, keep_csv=base_csv))
    assert base.status == 'done' and base.csv == base_csv and base.loops == 180
    assert base.metrics['changed_vs_base'] == 0
    var = replay.run_trial(replay.TrialSpec(day=DAY, window=CHECK_WINDOW, params={'profile autoISF_max': 1.5},
                                            base_csv=base_csv))
    assert var.status == 'done' and 'no_base_csv' not in var.flags and var.loops == 180
    again = replay.run_trial(replay.TrialSpec(day=DAY, window=CHECK_WINDOW, keep_csv=str(tmp_path / 'again.csv')))
    assert filecmp.cmp(base_csv, str(tmp_path / 'again.csv'), shallow=False)   # sequential replays are clean


@pytest.mark.slow
@pytest.mark.needs_cache('2026-09-05', DAY)
@needs_frozen
def test_frozen_equals_live_replay_bit_for_bit(tmp_path):
    """Acceptance H-102: the frozen replay of the 06-09Z empty VDF equals the live replay's CSV byte for byte. The
    live partner is the whole-day reconstruction filtered to the window (source='live-day'), the same input set the
    frozen day stores; the window-only live source is recorded, not asserted (replay.py docstring)."""
    out = {}
    for src in ('frozen', 'live-day', 'live'):
        res = replay.run_trial(replay.TrialSpec(day=DAY, window=CHECK_WINDOW, source=src,
                                                keep_csv=str(tmp_path / f'{src}.csv')))
        assert res.status == 'done', (src, res.error)
        assert res.loops == 180
        out[src] = res
    assert filecmp.cmp(out['frozen'].csv, out['live-day'].csv, shallow=False)
    m_f = {k: v for k, v in out['frozen'].metrics.items() if k != 'seconds_core'}
    m_l = {k: v for k, v in out['live-day'].metrics.items() if k != 'seconds_core'}
    assert m_f == m_l
    print(f"\nseconds 06-09Z: frozen {out['frozen'].seconds}, live-day {out['live-day'].seconds}, "
          f"live window-only {out['live'].seconds}; frozen == live window-only: "
          f"{filecmp.cmp(out['frozen'].csv, out['live'].csv, shallow=False)}")
