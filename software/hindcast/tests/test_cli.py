"""Tests for hindcast/cli.py: the `hc` dispatcher (study new|run|status|stop in-process, exit codes 0/2/3, the honesty
statement in human output, lazy sibling commands tolerated when absent)."""
import json
import os
import subprocess
import sys

import pytest

from hindcast import cli
from hindcast import contracts as c
from hindcast import space as sp
from hindcast import study as st
from hindcast.tests.conftest import GRID_AUTOISF, NS_SETTINGS, SOFTWARE_DIR

SETTINGS = sp.load_settings(NS_SETTINGS)
SETTINGS_ARG = NS_SETTINGS if os.path.isfile(NS_SETTINGS) else os.devnull


def hc(*args, studies_dir=None):
    argv = list(args)
    if studies_dir is not None:
        argv += ['--studies-dir', str(studies_dir), '--settings', SETTINGS_ARG]
    return cli.main(argv)


def test_help_and_version(capsys):
    assert hc() == 0
    out = capsys.readouterr().out
    assert 'study' in out and 'gh' in out and 'lint' in out
    assert hc('--version') == 0
    assert 'code_sha' in capsys.readouterr().out


def test_study_new_run_status_stop(tmp_path, capsys):
    rc = hc('study', 'new', 'demo', '--days', '2026-09-06', '--from-grid', GRID_AUTOISF, '--question', 'explore',
            studies_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and 'created' in out and c.HONESTY_STATEMENT in out
    sd = st.StudyDir(tmp_path / 'demo')
    study = sd.load_study(SETTINGS)
    assert study.phases[0].kind == 'grid' and len(study.space) == 2

    rc = hc('study', 'run', 'demo', '--dry', studies_dir=tmp_path)            # the dry evaluator (H-305 kept it as --dry)
    out = capsys.readouterr().out
    assert rc == 0 and 'nothing is replayed' in out and 'blocked' in out and c.HONESTY_STATEMENT in out
    rows = sd.trials()
    assert len(rows) == 1 + 15 and all(r.status == 'blocked' for r in rows)   # base + the 15 grid points
    assert sd.read_state().exit_code == 0

    rc = hc('study', 'status', 'demo', '--json', studies_dir=tmp_path)
    text = capsys.readouterr().out.strip()
    assert rc == 0 and len(text.encode('utf-8')) <= 2048
    status = c.Status.from_dict(json.loads(text)).validate()
    assert status.trials_blocked == 16 and not status.running and status.exit_code == 0
    assert c.HONESTY_STATEMENT not in text                                     # the JSON stays small; the text carries it

    rc = hc('study', 'status', 'demo', studies_dir=tmp_path)
    text = capsys.readouterr().out
    assert rc == 0 and c.HONESTY_STATEMENT in text and 'blocked' in text

    rc = hc('study', 'stop', 'demo', studies_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and sd.stop_requested() and 'no run is active' in out


def test_study_new_with_params_tier_and_constraint(tmp_path, capsys):
    rc = hc('study', 'new', 'p1', '--days', '2026-09-05..09-06', '--holdout', '2026-09-05', '--tag', '2026-09-05=weekend,sport',
            '--param', 'profile autoISF_max', '1.1:1.8:0.05', '--param', 'profile enable_pp_ISF', 'bool',
            '--param', 'profile autoISF_min', '0.5:1.0:0.1', '--constraint', 'profile autoISF_min <= profile autoISF_max',
            '--phase', 'oaat:points=5', '--phase', 'lhs:n=24,seed=1', '--hours', '2', '--workers', '2', '--sentinel-every', '10',
            studies_dir=tmp_path)
    assert rc == 0, capsys.readouterr()
    study = st.StudyDir(tmp_path / 'p1').load_study(SETTINGS)
    assert study.constraints == [['profile autoISF_min', '<=', 'profile autoISF_max']]
    assert [p.kind for p in study.phases] == ['oaat', 'lhs'] and study.budget.hours == 2 and study.sentinel_every == 10
    assert study.days[0].tags == ['weekend', 'sport'] and study.holdout_days() == ['2026-09-05']
    rc = hc('study', 'new', 'tA', '--days', '2026-09-06', '--tier', 'A', studies_dir=tmp_path)
    assert rc == 0, capsys.readouterr()
    study = st.StudyDir(tmp_path / 'tA').load_study(SETTINGS)
    assert len(study.space) == 10 and len(study.constraints) == 2


def test_study_new_locked_key_exits_3_naming_it(tmp_path, capsys):
    rc = hc('study', 'new', 'bad', '--days', '2026-09-06', '--param', 'profile max_iob', '5:10:1',
            '--param', 'profile autoISF_max', '1.1:1.8:0.05', studies_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 3 and 'profile max_iob' in err and 'autoISF_max' not in err and 'exit 3' in err
    assert not (tmp_path / 'bad').exists()
    rc = hc('study', 'new', 'bad2', '--days', '2026-09-06', '--param', 'new_parameter SMBRatio', '0.3:0.6:0.1', studies_dir=tmp_path)
    assert rc == 3 and 'new_parameter' in capsys.readouterr().err


def test_study_new_base_vdf_on_locked_keys_exits_3_naming_them(tmp_path, capsys):
    vdf = tmp_path / 'base.vdf'
    vdf.write_text('profile  max_iob  15\nprofile  sens  40\nnew_parameter  SMBRatio  0.9\nprofile  enable_pp_ISF  False\n',
                   encoding='utf-8')
    rc = hc('study', 'new', 'bv', '--days', '2026-09-06', '--param', 'profile autoISF_max', '1.1:1.8:0.05',
            '--base-vdf', str(vdf), studies_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 3 and 'exit 3' in err and 'DESIGN.md section 3' in err
    for key in ('profile max_iob', 'profile sens', 'new_parameter SMBRatio'):
        assert key in err, (key, err)
    assert 'enable_pp_ISF' not in err and not (tmp_path / 'bv').exists()
    vdf.write_text('profile  enable_pp_ISF  False\n', encoding='utf-8')
    rc = hc('study', 'new', 'bv', '--days', '2026-09-06', '--param', 'profile autoISF_max', '1.1:1.8:0.05',
            '--base-vdf', str(vdf), studies_dir=tmp_path)
    assert rc == 0, capsys.readouterr()
    # a locked base row written into study.json by hand is refused by `hc study run` too
    path = tmp_path / 'bv' / 'study.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['base_vdf_lines'] = ['profile  enable_pp_ISF  False', 'new_parameter  SMBRatio  0.9']
    path.write_text(json.dumps(data), encoding='utf-8')
    rc = hc('study', 'run', 'bv', studies_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 3 and 'new_parameter SMBRatio' in err and not (tmp_path / 'bv' / 'trials.jsonl').exists()


def test_study_run_refuses_locked_study_json_with_exit_3(tmp_path, capsys):
    hc('study', 'new', 'ok', '--days', '2026-09-06', '--param', 'profile autoISF_max', '1.1:1.8:0.05', studies_dir=tmp_path)
    capsys.readouterr()
    path = tmp_path / 'ok' / 'study.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['space']['profile max_basal'] = {'type': 'float', 'low': 1, 'high': 3, 'step': 1}
    path.write_text(json.dumps(data), encoding='utf-8')
    rc = hc('study', 'run', 'ok', studies_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 3 and 'profile max_basal' in err
    assert not (tmp_path / 'ok' / 'trials.jsonl').exists()
    rc = hc('study', 'status', 'ok', studies_dir=tmp_path)                    # status still reads the file
    assert rc == 0


def test_study_new_other_refusals_exit_3(tmp_path, capsys):
    assert hc('study', 'new', 'x', '--days', '2026-09-06', studies_dir=tmp_path) == 3
    assert 'searched parameters' in capsys.readouterr().err
    assert hc('study', 'new', 'x', '--days', 'tuesday', '--param', 'profile autoISF_max', '1.1:1.8:0.05', studies_dir=tmp_path) == 3
    assert hc('study', 'new', 'x', '--days', '2026-09-06', '--param', 'profile autoISF_max', 'nonsense', studies_dir=tmp_path) == 3
    assert hc('study', 'new', 'x', '--days', '2026-09-06', '--tier', 'Z', studies_dir=tmp_path) == 3
    assert hc('study', 'run', 'nothere', studies_dir=tmp_path) == 3
    assert hc('study', 'status', 'nothere', studies_dir=tmp_path) == 3
    assert hc('study', 'stop', 'nothere', studies_dir=tmp_path) == 3


def test_study_run_resumes_and_honours_stop(tmp_path, capsys):
    hc('study', 'new', 'r', '--days', '2026-09-06', '--param', 'profile autoISF_max', '1.1:1.3:0.1', studies_dir=tmp_path)
    sd = st.StudyDir(tmp_path / 'r')
    sd.request_stop()                                                          # a stale STOP: cleared at start
    assert hc('study', 'run', 'r', '--dry', studies_dir=tmp_path) == 0
    expected = len(st.study_tasks(sd.load_study(SETTINGS), SETTINGS))         # base + the oaat levels (phone value excluded)
    assert 3 <= expected <= 4 and len(sd.trials()) == expected
    n = len(sd.trials())
    assert hc('study', 'run', 'r', '--dry', studies_dir=tmp_path) == 0         # blocked rows are re-evaluated (still blocked)
    assert len(sd.trials()) == 2 * n
    status = st.build_status(sd)
    assert status.trials_blocked == n and status.trials_planned == n          # per key, the latest row counts


def test_placeholder_and_forwarded_commands(capsys):
    import importlib.util
    for cmd, (modules, issue, _) in cli.LAZY_COMMANDS.items():
        if cmd in ('gh', 'lint'):
            continue
        if any(importlib.util.find_spec(m) for m in modules):
            continue                                                           # built since: not a placeholder any more
        assert hc(cmd) == 1
        assert issue in capsys.readouterr().err
    assert hc('lint', '--markdown') == 0                                        # forwarded to hindcast.lint.main
    assert 'may' in capsys.readouterr().out.lower()
    with pytest.raises(SystemExit) as e:
        hc('gh', '--help')                                                     # argparse of the sibling, exit 0
    assert e.value.code == 0


def test_module_entry_point(tmp_path):
    env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=SOFTWARE_DIR)
    r = subprocess.run([sys.executable, '-m', 'hindcast', 'study', 'new', 'sub', '--days', '2026-09-06', '--param',
                        'profile max_iob', '5:10:1', '--studies-dir', str(tmp_path), '--settings', SETTINGS_ARG],
                       env=env, cwd=SOFTWARE_DIR, capture_output=True, text=True, timeout=60)
    assert r.returncode == 3 and 'profile max_iob' in r.stderr
    r = subprocess.run([sys.executable, '-m', 'hindcast'], env=env, cwd=SOFTWARE_DIR, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and 'usage: hc' in r.stdout


# ----------------------------------------------------------------------------- tiers C / S, the explicit unlock, hc skill

TIER_C_LOCKED = ['profile sens', 'profile carb_ratio', 'profile current_basal']


@pytest.mark.parametrize('tier', ['C', 'S', 'c'])
def test_study_new_tier_c_s_without_unlock_exits_3_naming_the_keys(tmp_path, capsys, tier):
    rc = hc('study', 'new', 'locked', '--days', '2026-09-06', '--tier', tier, studies_dir=tmp_path)
    captured = capsys.readouterr()
    assert rc == 3
    assert not (tmp_path / 'locked').exists()
    names = TIER_C_LOCKED if tier.upper() == 'C' else ['profile max_iob', 'profile max_basal']
    for name in names:
        assert f'"{name}"' in captured.err
    assert '--unlock' in captured.err and '--unlock-reason' in captured.err
    assert 'evidence:' in captured.err                         # space_file_notice(meta) is printed with the refusal


def test_study_new_tier_c_with_unlock_writes_the_audit_fields(tmp_path, capsys):
    rc = hc('study', 'new', 'scales', '--days', '2026-09-06', '--tier', 'C',
            '--unlock', 'profile sens', 'profile carb_ratio', '--unlock', 'profile current_basal', 'profile sens',
            '--unlock-reason', 'x', studies_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0, out
    with open(tmp_path / 'scales' / 'study.json', encoding='utf-8') as fh:
        doc = json.load(fh)
    assert doc['unlocked'] == TIER_C_LOCKED                    # union, order kept, duplicates dropped
    assert doc['unlock_reason'] == 'x'
    study = st.StudyDir(tmp_path / 'scales').load_study(SETTINGS)
    assert study.unlock_caveat() in out and 'weak evidence' in out
    assert 'evidence: ' in out                                 # the space file's _evidence line
    assert 'this space needs an explicit unlock' not in out
    assert c.HONESTY_STATEMENT in out
    from hindcast import lint
    for line in out.splitlines():
        if line.strip() and line.strip() != c.HONESTY_STATEMENT:
            assert lint.lint_findings(line, 'line') == [], line


def test_study_new_tier_c_with_sens_only_names_the_rest(tmp_path, capsys):
    rc = hc('study', 'new', 'part', '--days', '2026-09-06', '--tier', 'C', '--unlock', 'profile sens',
            '--unlock-reason', 'x', studies_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 3 and '"profile carb_ratio"' in err and '"profile current_basal"' in err
    assert '"profile sens"' not in err.split('need an explicit unlock:', 1)[-1].split('\n', 1)[0]


@pytest.mark.parametrize('extra', [['--unlock', 'profile sens'], ['--unlock', 'profile sens', '--unlock-reason', '  '],
                                   ['--unlock-reason', 'x']])
def test_unlock_needs_keys_and_a_reason(tmp_path, capsys, extra):
    rc = hc('study', 'new', 'u', '--days', '2026-09-06', '--tier', 'C', *extra, studies_dir=tmp_path)
    assert rc == 3 and '--unlock' in capsys.readouterr().err
    assert not (tmp_path / 'u').exists()


def test_unlock_cannot_lift_a_never_unlockable_key(tmp_path, capsys):
    rc = hc('study', 'new', 'dia', '--days', '2026-09-06', '--param', 'profile dia', '5:9:1',
            '--unlock', 'profile dia', '--unlock-reason', 'x', studies_dir=tmp_path)
    assert rc == 3 and 'profile dia' in capsys.readouterr().err


def test_hc_skill_is_wired(capsys):
    with pytest.raises(SystemExit) as info:
        hc('skill', '--help')
    assert info.value.code == 0
    out = capsys.readouterr().out
    for word in ('install', 'vocabulary', 'checkin'):
        assert word in out
    assert hc('skill', 'vocabulary', '--check') == 0
    capsys.readouterr()
