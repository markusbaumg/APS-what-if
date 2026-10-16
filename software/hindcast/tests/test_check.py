"""
test_check.py  -  `hc check`, the regression gate with golden metrics (backlog H-902)

Offline unit tests (compare, golden files, table, lint, exit codes) run everywhere. The replay tests need the frozen
day 2026-09-06 (`hc freeze 2026-09-06`, offline from the cache) and skip cleanly without it; the full gate with the
frozen == live comparison also needs the cached days; the two tests against the committed golden file are marked
slow (`--run-slow`, ~15-25 s) so that a fidelity change in progress does not fail the quick suite before its PR
re-baselines.
"""
import argparse
import json
import os

import pytest

from hindcast import check, freeze, lint
from hindcast.tests.conftest import CHECK_WINDOW, GOLDEN_DIR

DAY = '2026-09-06'
COMMITTED_GOLDEN = os.path.join(GOLDEN_DIR, '2026-09-06T06-09.base.json')


def _frozen_ok(day=DAY):
    try:
        return freeze.blocked_reason(day) is None
    except Exception:
        return False


needs_frozen = pytest.mark.skipif(not _frozen_ok(), reason=f'{DAY} not frozen (or stale): run `hc freeze {DAY}`')

METRICS = {'loops': 180, 'smb_exact_pct': 99.444444, 'smb_within_pct': 100.0, 'insreq_within_pct': 99.444444,
           'rate_within_pct': 100.0, 'isf_within_pct': 100.0, 'agreement_pct': 99.444444, 'smb_emul_U': 2.6,
           'smb_orig_U': 2.55}


def _golden(metrics=METRICS, **kw):
    kw.setdefault('code_sha', 'c' * 40)
    kw.setdefault('settings_sha', 's' * 40)
    kw.setdefault('versions', {})
    return check.golden_doc(dict(metrics), DAY, check.default_window(DAY), 'test', csv_sha1='a' * 40,
                            freeze_sha='f' * 40, **kw)


def _rows(now, golden=None):
    return {r['key']: r for r in check.compare(golden or _golden(), now)}


# ----------------------------------------------------------------------------- windows and names

def test_window_and_golden_name():
    assert check.default_window(DAY) == CHECK_WINDOW
    assert check.golden_name(DAY, (6, 9)) == '2026-09-06T06-09.base.json'
    assert check.default_window(DAY, (0, 24)) == ('2026-09-06T00:00:00Z', '2026-09-07T00:00:00Z')
    assert check.window_hours(DAY, CHECK_WINDOW) == (6, 9)
    for bad in ((9, 6), (-1, 3), (6, 25)):
        with pytest.raises(check.CheckError):
            check.default_window(DAY, bad)
    with pytest.raises(check.CheckError):
        check.default_window('2026-9-6')


# ----------------------------------------------------------------------------- compare

def test_compare_identical_is_ok():
    rows = check.compare(_golden(), METRICS)
    assert [r['key'] for r in rows] == list(check.GATE_KEYS)
    assert all(r['result'] == 'ok' and not r['failed'] for r in rows)


def test_rounded_golden_values_do_not_read_as_a_rise():
    now = dict(METRICS, smb_exact_pct=100.0 * 179 / 180, agreement_pct=100.0 * 179 / 180)
    assert all(r['result'] == 'ok' for r in check.compare(_golden(), now))


def test_min_gate_drop_is_a_regression_and_a_rise_is_up():
    r = _rows(dict(METRICS, smb_exact_pct=98.888889, insreq_within_pct=100.0))
    assert r['smb_exact_pct']['result'] == 'REGRESSION' and r['smb_exact_pct']['failed']
    assert r['smb_exact_pct']['delta'] == pytest.approx(-0.555555, abs=1e-6)
    assert r['insreq_within_pct']['result'] == 'up' and not r['insreq_within_pct']['failed']


def test_abs_gate_moves_either_way_and_tolerance_holds():
    assert _rows(dict(METRICS, smb_emul_U=2.64))['smb_emul_U']['result'] == 'ok'
    assert _rows(dict(METRICS, smb_emul_U=2.66))['smb_emul_U']['result'] == 'MOVED'
    assert _rows(dict(METRICS, smb_emul_U=2.54))['smb_emul_U']['result'] == 'MOVED'
    assert _rows(dict(METRICS, smb_orig_U=2.552))['smb_orig_U']['failed']


def test_equal_gate_and_missing_values():
    r = _rows(dict(METRICS, loops=179, rate_within_pct=None))
    assert r['loops']['result'] == 'REGRESSION'
    assert r['rate_within_pct']['result'] == 'MISSING' and r['rate_within_pct']['failed']


def test_tolerances_come_from_the_golden_file():
    g = _golden()
    g['gates']['smb_exact_pct']['tol'] = 1.0                  # a re-baseline that loosened one gate keeps it
    assert _rows(dict(METRICS, smb_exact_pct=98.888889), g)['smb_exact_pct']['result'] == 'ok'


# ----------------------------------------------------------------------------- golden files

def test_golden_round_trip(tmp_path):
    path = check.write_golden(str(tmp_path / 'g' / check.golden_name()), _golden())
    doc = check.load_golden(path)
    assert doc['metrics'] == METRICS and doc['window'] == list(CHECK_WINDOW)
    assert set(doc['gates']) == set(check.GATE_KEYS)


def test_golden_doc_refuses_missing_metrics():
    with pytest.raises(check.CheckError):
        _golden(dict(METRICS, smb_emul_U=None))


def test_load_golden_refuses_missing_and_foreign_files(tmp_path):
    with pytest.raises(check.CheckError, match='--write-golden'):
        check.load_golden(str(tmp_path / 'none.json'))
    p = tmp_path / 'x.json'
    p.write_text(json.dumps({'format': 99, 'metrics': {}}))
    with pytest.raises(check.CheckError):
        check.load_golden(str(p))
    p.write_text('{not json')
    with pytest.raises(check.CheckError):
        check.load_golden(str(p))


def test_committed_golden_file_is_valid():
    if not os.path.isfile(COMMITTED_GOLDEN):
        pytest.skip('tests/golden/2026-09-06T06-09.base.json not written yet')
    doc = check.load_golden(COMMITTED_GOLDEN)
    assert doc['day'] == DAY and tuple(doc['window']) == CHECK_WINDOW
    assert set(check.GATE_KEYS) <= set(doc['metrics'])
    assert doc['reason'] and doc['code_sha'] and doc['csv_sha1']
    assert set(doc) >= {'design_section_9', 'gates', 'freeze_sha', 'settings_sha', 'live_checked'}


# ----------------------------------------------------------------------------- exit codes without a replay

def test_missing_golden_is_not_checked(tmp_path):
    rep = check.check(golden_dir=str(tmp_path), live=False)
    assert rep.exit_code == check.EXIT_NOT_CHECKED and rep.status == 'not_checked'
    assert 'no golden file' in rep.error


def test_write_without_reason_is_refused(tmp_path):
    rep = check.check(golden_dir=str(tmp_path), write=True, live=False)
    assert rep.exit_code == check.EXIT_NOT_CHECKED and '--reason' in rep.error
    assert not os.listdir(tmp_path)


def test_golden_of_another_window_is_refused(tmp_path):
    g = _golden()
    g['window'] = ['2026-09-06T06:00:00Z', '2026-09-06T12:00:00Z']
    path = check.write_golden(str(tmp_path / check.golden_name()), g)
    rep = check.check(golden_file=path, live=False)
    assert rep.exit_code == check.EXIT_NOT_CHECKED and 'not of' in rep.error


def test_unfrozen_day_is_not_checked(tmp_path):
    path = check.write_golden(str(tmp_path / check.golden_name()), _golden())
    rep = check.check(golden_file=path, live=False, frozen_dir=str(tmp_path / 'frozen'))
    assert rep.exit_code == check.EXIT_NOT_CHECKED and 'not frozen' in rep.error and '--refreeze' in rep.error


# ----------------------------------------------------------------------------- csv difference

def test_csv_difference_names_columns_not_values(tmp_path):
    a, b = tmp_path / 'a.csv', tmp_path / 'b.csv'
    a.write_text('x;y;z\n1;2;3\n4;5;6\n')
    b.write_text('x;y;z\n1;2;3\n4;9;6\n')
    assert check.csv_difference(str(a), str(a))['equal']
    d = check.csv_difference(str(a), str(b))
    assert not d['equal'] and d['rows_differing'] == 1 and len(d['columns']) == 1
    assert '9' not in json.dumps(d['columns']) and '5' not in json.dumps(d['columns'])


# ----------------------------------------------------------------------------- the table

def _report(**kw):
    rep = check.CheckReport(day=DAY, window=CHECK_WINDOW, status='ok', exit_code=0, golden_path=COMMITTED_GOLDEN,
                            golden={'written': '2026-10-17T00:00:00Z', 'code_sha': 'c' * 40, 'reason': 'test'},
                            rows=check.compare(_golden(), kw.pop('metrics', METRICS)),
                            live={'status': 'equal', 'seconds': 20.0}, frozen={'status': 'done', 'seconds': 0.4})
    for k, v in kw.items():
        setattr(rep, k, v)
    return rep


def test_table_passes_the_issue_lint_and_hides_amounts():
    text = check.format_table(_report())
    assert lint.lint_findings(text, 'issue') == []
    assert lint.contains_honesty_statement(text)
    assert '2.600' not in text and '| 180 |' not in text            # amounts and loop counts only as deltas
    assert 'no regression' in text and 'equal byte for byte' in text
    assert len(text.splitlines()) <= 30                              # one screen


def test_table_full_shows_amounts():
    text = check.format_table(_report(), full=True)
    assert '2.600' in text and '| 180 |' in text


def test_table_regression_and_live_difference_lint_clean():
    rep = _report(metrics=dict(METRICS, smb_exact_pct=97.0, smb_emul_U=2.9), status='regression', exit_code=1,
                  live={'status': 'differs', 'rows_differing': 3, 'columns': ['dura avg.', 'SMB emul']})
    text = check.format_table(rep)
    assert 'REGRESSION' in text and 'MOVED' in text and 'DIFFERS' in text and 'dura avg.' in text
    assert lint.lint_findings(text, 'issue') == []


def test_table_not_checked():
    rep = check.CheckReport(day=DAY, window=CHECK_WINDOW, error='2026-09-06 is not frozen')
    text = check.format_table(rep)
    assert 'NOT CHECKED' in text and lint.lint_findings(text, 'issue') == []


# ----------------------------------------------------------------------------- CLI wiring

def test_register_and_parse():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    check.register(sub)
    args = parser.parse_args(['check', '--hours', '6-9', '--no-live', '--write-golden', '--reason', 'H-703'])
    assert args.func is check.run_check and args.hours == (6, 9) and args.no_live and args.reason == 'H-703'
    assert parser.parse_args(['check']).day == check.DEFAULT_DAY


def test_cli_dispatches_check():
    from hindcast import cli
    parser = cli.build_parser()
    args = parser.parse_args(['check', '--no-live'])
    assert args.func is check.run_check


def test_run_check_json_on_missing_golden(tmp_path, capsys):
    parser = argparse.ArgumentParser()
    check.register(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['check', '--golden', str(tmp_path / 'none.json'), '--no-live', '--json'])
    assert check.run_check(args) == check.EXIT_NOT_CHECKED
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'not_checked'


# ----------------------------------------------------------------------------- with the frozen day

@pytest.mark.slow
@needs_frozen
def test_frozen_gate_against_the_committed_golden(tmp_path):
    """The no-live gate on the current tree. A regression here means the tree moved the base replay: either fix it
    or re-baseline with `hc check --write-golden --reason <fidelity issue>` in the PR that names the issue."""
    if not os.path.isfile(COMMITTED_GOLDEN):
        pytest.skip('no committed golden file')
    rep = check.check(live=False, keep_dir=str(tmp_path))
    print('\n' + check.format_table(rep))
    assert rep.frozen['status'] == 'done', rep.error
    assert rep.exit_code == check.EXIT_OK, [(r['key'], r['result'], r['delta']) for r in rep.failed_gates]
    assert rep.live == {'status': 'skipped'}


@needs_frozen
def test_a_tightened_golden_reports_a_regression(tmp_path):
    rep0 = check.check(live=False)
    if rep0.exit_code == check.EXIT_NOT_CHECKED:
        pytest.skip(rep0.error)
    g = check.golden_doc(dict(rep0.metrics, smb_exact_pct=rep0.metrics['smb_exact_pct'] + 0.5,
                              smb_emul_U=rep0.metrics['smb_emul_U'] - 0.2), DAY, check.default_window(DAY), 'test')
    path = check.write_golden(str(tmp_path / check.golden_name()), g)
    rep = check.check(golden_file=path, live=False)
    assert rep.exit_code == check.EXIT_REGRESSION and rep.status == 'regression'
    assert {r['key'] for r in rep.failed_gates} == {'smb_exact_pct', 'smb_emul_U'}


@needs_frozen
def test_write_golden_round_trip(tmp_path):
    rep = check.check(golden_dir=str(tmp_path), write=True, reason='test re-baseline', live=False)
    assert rep.exit_code == check.EXIT_OK and rep.wrote_golden
    doc = check.load_golden(rep.golden_path)
    assert doc['live_checked'] is False and doc['reason'] == 'test re-baseline'
    again = check.check(golden_dir=str(tmp_path), live=False)
    assert again.exit_code == check.EXIT_OK
    assert 'result CSV identical to the golden run' in again.notes


@pytest.mark.slow
@pytest.mark.needs_cache('2026-09-05', DAY)
@needs_frozen
def test_hc_check_full_gate(tmp_path):
    """Acceptance H-902: base replay of the frozen 06-09Z window matches the committed golden metrics and the frozen
    CSV equals the live (whole-day reconstruction) CSV byte for byte."""
    if not os.path.isfile(COMMITTED_GOLDEN):
        pytest.skip('no committed golden file')
    rep = check.check(keep_dir=str(tmp_path))
    text = check.format_table(rep)
    print('\n' + text)
    assert rep.live['status'] == 'equal', rep.live
    assert rep.exit_code == check.EXIT_OK, [(r['key'], r['result'], r['delta']) for r in rep.failed_gates]
    assert lint.lint_findings(text, 'issue') == []
