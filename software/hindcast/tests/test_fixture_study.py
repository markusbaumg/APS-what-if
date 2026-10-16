"""
test_fixture_study.py  -  H-003: tests/fixtures/study_example/ (built by make_fixtures.py) against the contracts

Two layers. The committed fixture directory is checked on its own (no CSVs needed): every document loads strictly
through contracts.py, the series documents pass export.validate_series, the trials rows reproduce the documented
table of NIGHTSCOUT_SOURCE.md (base +1.37 U, dura_ISF_weight 0.5 -0.4 U vs base in the rising phase, 9 loops with a
smaller SMB, factor on the cap 9.7 % -> 9.4 %, 1.5 reached in 3.8 %), status.json / round.json carry the review
order and every human-readable string passes the vocabulary lint. The second layer rebuilds the fixture from the
result CSVs (skipped without results/example_2026-09-06/) with the network blocked and compares it with the
committed one, so a stale committed fixture is found.
"""
import argparse
import json
import os
import socket

import pytest

from hindcast import contracts, export, lint, study as st
from hindcast import metrics as M
from hindcast.contracts import Round, State, Status, Study, read_trials
from hindcast.tests import make_fixtures as mf
from hindcast.tests.conftest import EXAMPLE_VARIANTS, STUDY_EXAMPLE_DIR

DAY = '2026-09-06'
V05_13 = 'autoISF_max_1.3__dura_ISF_weight_0.5'
V07_13 = 'autoISF_max_1.3__dura_ISF_weight_0.7'
V05_15 = 'autoISF_max_1.5__dura_ISF_weight_0.5'
V07_15 = 'autoISF_max_1.5__dura_ISF_weight_0.7'
STAMPS = ('created', 'code_sha', 'settings_sha', 'core_versions')      # study.json fields that follow the working tree


# ----------------------------------------------------------------------------- the committed fixture

@pytest.fixture(scope='module')
def study(fixture_study_dir):
    return Study.load(os.path.join(fixture_study_dir, 'study.json'))


@pytest.fixture(scope='module')
def trials(fixture_study_dir):
    return read_trials(os.path.join(fixture_study_dir, 'trials.jsonl'), strict=True)


@pytest.fixture(scope='module')
def by_variant(trials):
    return {r.variant: r for r in trials}


@pytest.fixture(scope='module')
def status(fixture_study_dir):
    return Status.load(os.path.join(fixture_study_dir, 'status.json'))


@pytest.fixture(scope='module')
def round_doc(fixture_study_dir):
    return Round.load(os.path.join(fixture_study_dir, 'round.json'))


@pytest.fixture(scope='module')
def manifest(fixture_study_dir):
    with open(os.path.join(fixture_study_dir, 'MANIFEST.json'), encoding='utf-8') as fh:
        return json.load(fh)


def test_fixture_dir_is_the_conftest_path(fixture_study_dir):
    assert os.path.abspath(fixture_study_dir) == os.path.abspath(STUDY_EXAMPLE_DIR) == os.path.abspath(mf.OUT_DIR)
    for name in ('study.json', 'trials.jsonl', 'status.json', 'round.json', 'STATE.json', 'MANIFEST.json'):
        assert os.path.isfile(os.path.join(fixture_study_dir, name)), name


def test_study_json(study):
    study.validate()
    assert study.name == 'study_example' and study.question == 'explore'
    assert [d.day for d in study.days] == [DAY] and study.search_days() == [DAY] and study.holdout_days() == []
    assert set(study.space) == {'profile autoISF_max', 'profile dura_ISF_weight'}
    assert study.space['profile autoISF_max'].values() == [1.3, 1.5]
    assert study.space['profile dura_ISF_weight'].values() == [0.5, 0.7]
    assert study.space['profile autoISF_max'].phone == 1.3                # the phone's value that morning (profile_history)
    assert [p.kind for p in study.phases] == ['grid'] and study.phases[0].state == 'done'
    assert study.phases[0].grid == {'profile autoISF_max': [1.3, 1.5], 'profile dura_ISF_weight': [0.5, 0.7]}
    assert study.base_vdf_lines == [] and study.constraints == []
    assert study.host == 'ns.vivikusi.ch' and study.created == '2026-09-08T01:22:00Z'
    assert any('freeze_sha is empty' in n for n in study.notes)
    for note in study.notes:
        assert lint.lint_findings(note, 'line') == []


def test_trials_rows(trials, by_variant, study):
    assert [r.variant for r in trials] == list(EXAMPLE_VARIANTS)           # base first, then the grid in name order
    assert [r.trial for r in trials] == [0, 1, 2, 3, 4]
    assert len({r.key for r in trials}) == 5
    for r in trials:
        r.validate()
        assert r.status == 'done' and r.day == DAY and r.freeze_sha == '' and r.round == 1
        assert r.code_sha == study.code_sha and r.settings_sha == study.settings_sha
        assert r.key == r.make_key(study.objective_version)
        assert r.metrics['loops'] == 340 and r.feasible is True
        assert r.flags == r.metrics['flags']
        assert len(r.to_jsonl_line().encode('utf-8')) < contracts.TRIAL_LINE_MAX_BYTES
    base = by_variant['base']
    assert base.control == 'base' and base.phase == 'floor' and base.params == {} and base.vhash == contracts.BASE_VHASH
    assert base.metrics['is_base'] is True and 'orig' in base.files
    for v in EXAMPLE_VARIANTS[1:]:
        r = by_variant[v]
        assert r.control is None and r.phase == 'grid' and r.metrics['is_base'] is False
        assert set(r.params) == set(study.space) and r.variant == mf.spc.variant_name(r.params, list(study.space))


def test_documented_table_from_trials_alone(by_variant):
    m = {v: r.metrics for v, r in by_variant.items()}
    assert round(m['base']['delta_U'], 2) == 1.37                      # the replay bias of the base VDF
    assert round(m[V05_13]['delta_vs_base_U'], 1) == -0.4             # dura 0.5 asks for 0.4 U less than base ...
    assert round(m[V05_13]['rise_vs_base_U'], 2) == -0.36             # ... in the rising phase
    assert abs(m[V05_13]['rest_vs_base_U']) < M.TOL_U
    assert m[V05_13]['smb_less'] == 9 and m[V05_13]['smb_more_vs_base'] == 0
    assert round(m['base']['at_autoISF_max_pct'], 1) == 9.7
    assert round(m[V05_13]['at_autoISF_max_pct'], 1) == 9.4
    assert round(m[V05_15]['at_autoISF_max_pct'], 1) == 3.8
    assert round(m[V07_15]['delta_vs_base_U'], 2) == 0.01
    for key, val in m['base'].items():                                # 1.3 / 0.7 were the phone's settings: identical to base
        if key in ('flags', 'is_base'):
            continue
        assert m[V07_13][key] == pytest.approx(val) if isinstance(val, float) else m[V07_13][key] == val, key
    assert m[V07_13]['changed_vs_base_loops'] == 0 and m[V07_13]['delta_vs_base_U'] == 0.0


def test_series_documents(fixture_study_dir, trials, by_variant):
    docs = {}
    for r in trials:
        assert r.files['series'] == export.TIER2_PATHS['series'].format(day=DAY, vhash=r.vhash)
        path = os.path.join(fixture_study_dir, r.files['series'])
        docs[r.variant] = export.read_series(path)                       # validates the tier-2 contract
        assert docs[r.variant]['kind'] == 'series' and docs[r.variant]['variant'] == r.variant
        assert docs[r.variant]['loops'] == r.metrics['loops'] == 340
        assert docs[r.variant]['window'] == ['2026-09-06T06:00:00Z', '2026-09-06T12:00:00Z']
        assert 'no_pred' in docs[r.variant]['flags']                       # no replay: no Pred globals, and it says so
    orig = export.read_series(os.path.join(fixture_study_dir, by_variant['base'].files['orig']))
    assert orig['kind'] == 'orig' and orig['variant'] == 'phone'
    assert len({tuple(d['t']) for d in list(docs.values()) + [orig]}) == 1  # one loop axis for every document
    assert sum(docs['base']['changed']) == by_variant['base'].metrics['changed_loops'] == 26
    assert sum(docs[V05_13]['changed']) == by_variant[V05_13].metrics['changed_loops'] == 38
    assert docs['base']['smb'] == docs[V07_13]['smb'] and docs['base']['tbr'] == docs[V07_13]['tbr']
    assert sum(1 for a, b in zip(docs[V05_13]['smb'], orig['smb']) if a < b - 1e-9) == 9
    assert all(v is None for v in docs['base']['min_guard'])
    assert not any(e['kind'] == 'low' for e in orig['events'])            # no recorded low that morning
    assert sum(orig['low_ahead']) == 0


def test_state_and_status(fixture_study_dir, status, trials):
    state = State.load(os.path.join(fixture_study_dir, 'STATE.json'))
    state.validate()
    assert state.exit_code == 0 and state.stop_reason == 'finished' and state.trials_done == 5 and state.queue_len == 0
    status.validate()
    assert len(status.to_compact_json().encode('utf-8')) <= contracts.STATUS_MAX_BYTES
    assert status.study == 'study_example' and status.question == 'explore' and status.phase == 'grid'
    assert status.trials_done == status.trials_planned == 5 and status.trials_failed == status.trials_blocked == 0
    assert status.running is False and status.exit_code == 0 and status.pid is None
    assert status.days == {DAY: 'search'} and status.heartbeat == state.heartbeat
    assert round(status.floor['floor_total_U'], 2) == 1.37 and status.floor['floor_loops'] == 26
    assert round(status.floor['floor_rise_U'], 2) == 1.38 and status.floor['agreement_pct'] == pytest.approx(92.353, abs=1e-3)
    assert [t.variant for t in status.top] == [V05_13, V05_15, V07_15, V07_13]
    assert [t.rank for t in status.top] == [1, 2, 3, 4]
    assert all(t.verdict == 'tie' for t in status.top)                   # every point is inside 2 x the rise floor
    assert status.top[0].effect['rise_vs_base_U'] == pytest.approx(-0.358, abs=1e-3)
    assert status.dead_parameters == ['profile autoISF_max', 'profile dura_ISF_weight']
    rebuilt = st.build_status(st.StudyDir(fixture_study_dir))
    assert rebuilt.to_dict() == status.to_dict()


def test_round_json(round_doc, by_variant, status):
    round_doc.validate()
    assert round_doc.round == 1 and round_doc.phase == 'grid' and round_doc.trials == 5
    assert [c.variant for c in round_doc.variants] == ['base', V05_13, V05_15, V07_15, V07_13]
    base = round_doc.variants[0]
    assert base.verdict == 'base' and base.vhash == contracts.BASE_VHASH and base.rank is None
    assert base.effect['delta_U'] == pytest.approx(1.374, abs=1e-3)
    for c in round_doc.variants[1:]:
        assert c.verdict == 'tie' and c.feasible is True and c.days == 1
        assert c.vhash == by_variant[c.variant].vhash == contracts.vhash(c.params)
        assert contracts.BASE_VHASH in c.tie_with
        assert set(c.effect) == {'rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U', 'changed_vs_base'}
    assert round_doc.front == [by_variant[v].vhash for v in (V05_13, V05_15, V07_15, V07_13)]
    assert [contracts.short(v) for v in round_doc.front] == [t.vhash for t in status.top]
    floors = round_doc.floors[DAY]
    assert floors['floor_total_U'] == pytest.approx(1.374, abs=1e-3) and floors['loops'] == 340
    assert floors == {k: pytest.approx(v) for k, v in M.floors_from_base(by_variant['base'].metrics).items() if v is not None}
    dead = {d.key: d for d in round_doc.dead_parameters}
    assert set(dead) == {'profile autoISF_max', 'profile dura_ISF_weight'}
    assert dead['profile dura_ISF_weight'].floor_ratio < M.TIE_FACTOR
    assert '1.5' in dead['profile autoISF_max'].why and '0.5' in dead['profile dura_ISF_weight'].why
    assert any(n.startswith('Review order, not a ranking by outcome.') for n in round_doc.notes)


def test_every_human_readable_string_passes_the_lint(round_doc, status, study):
    texts = [c.why for c in round_doc.variants] + [d.why for d in round_doc.dead_parameters] + list(round_doc.notes)
    texts += [t.why for t in status.top] + list(study.notes)
    for text in texts:
        assert lint.lint_findings(text, 'line') == [], text
    assert lint.lint_findings(st.status_text(status), 'line') == []
    for verdict in {c.verdict for c in round_doc.variants}:
        assert lint.lint_findings(st.verdict_label(verdict), 'line') == []


def test_manifest_matches_the_files(fixture_study_dir, manifest, trials):
    assert manifest['day'] == DAY and manifest['loops'] == 340 and manifest['base_vhash'] == contracts.BASE_VHASH
    assert set(manifest['sources']) == set(EXAMPLE_VARIANTS)
    assert manifest['vhash'] == {r.variant: r.vhash for r in trials}
    assert manifest['review_order'] == [V05_13, V05_15, V07_15, V07_13]
    assert round(manifest['table']['base']['delta_U'], 2) == 1.37 and manifest['table'][V05_13]['smb_less'] == 9
    on_disk = set()
    for root, _, files in os.walk(fixture_study_dir):
        for name in files:
            if name != 'MANIFEST.json':
                on_disk.add(os.path.relpath(os.path.join(root, name), fixture_study_dir).replace(os.sep, '/'))
    assert on_disk == set(manifest['files'])
    for rel, info in manifest['files'].items():
        path = os.path.join(fixture_study_dir, rel)
        assert os.path.getsize(path) == info['bytes'], rel
        assert mf._sha1_file(path) == info['sha1'], rel


# ----------------------------------------------------------------------------- rebuilding from the CSVs

@pytest.fixture(scope='module')
def rebuilt(tmp_path_factory, example_results_dir):
    """The fixture rebuilt into a temp dir from the result CSVs with every socket refused (no replay, no network)."""
    out = str(tmp_path_factory.mktemp('study_example'))
    real_socket = socket.socket

    class NoSocket(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError('make_fixtures opened a socket')

    socket.socket = NoSocket
    try:
        manifest = mf.build_fixture_study(out, example_results_dir, log=lambda *a: None)
    finally:
        socket.socket = real_socket
    return out, manifest


def _load_json(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def test_rebuild_reads_only_the_csvs_and_matches_the_committed_fixture(rebuilt, fixture_study_dir, manifest):
    out, new_manifest = rebuilt
    assert new_manifest['sources'] == manifest['sources'] and new_manifest['vhash'] == manifest['vhash']
    assert new_manifest['table'] == manifest['table'] and new_manifest['review_order'] == manifest['review_order']
    # the series documents depend on the CSVs alone: byte-identical
    for rel in [k for k in manifest['files'] if k.startswith('series/')]:
        with open(os.path.join(out, rel), 'rb') as a, open(os.path.join(fixture_study_dir, rel), 'rb') as b:
            assert a.read() == b.read(), rel
    # round.json and STATE.json carry no working-tree stamp
    assert _load_json(os.path.join(out, 'round.json')) == _load_json(os.path.join(fixture_study_dir, 'round.json'))
    assert _load_json(os.path.join(out, 'STATE.json')) == _load_json(os.path.join(fixture_study_dir, 'STATE.json'))
    assert _load_json(os.path.join(out, 'status.json')) == _load_json(os.path.join(fixture_study_dir, 'status.json'))
    # study.json and the trial keys follow code_sha / settings_sha of the working tree; everything else is equal
    new_study, old_study = (Study.load(os.path.join(d, 'study.json')) for d in (out, fixture_study_dir))
    assert {k: v for k, v in new_study.to_dict().items() if k not in STAMPS} == \
           {k: v for k, v in old_study.to_dict().items() if k not in STAMPS}
    new_rows = read_trials(os.path.join(out, 'trials.jsonl'))
    old_rows = read_trials(os.path.join(fixture_study_dir, 'trials.jsonl'))
    assert len(new_rows) == len(old_rows) == 5
    for a, b in zip(new_rows, old_rows):
        assert (a.variant, a.params, a.day, a.status, a.phase, a.control, a.files, a.flags) == \
               (b.variant, b.params, b.day, b.status, b.phase, b.control, b.files, b.flags)
        assert a.metrics == b.metrics, a.variant
    if new_study.code_sha == old_study.code_sha and new_study.settings_sha == old_study.settings_sha:
        assert [r.key for r in new_rows] == [r.key for r in old_rows]
    else:
        pytest.xfail('the core or ns_settings.json changed since the fixture was committed: regenerate it with '
                     'python -m hindcast.tests.make_fixtures (keys and study stamps differ, the numbers do not)')


def test_rebuilt_trials_equal_metrics_py_on_the_csvs(rebuilt, example_csvs, ns_settings_path):
    out, _ = rebuilt
    settings = _load_json(ns_settings_path)
    rows = {r.variant: r for r in read_trials(os.path.join(out, 'trials.jsonl'))}
    base = M.LoopSeries.from_csv(example_csvs['base'])
    for variant, path in example_csvs.items():
        params = rows[variant].params
        a = mf.spc.assignment(params)
        m = M.trial_metrics(path, None if variant == 'base' else base,
                            M.effective_cap(a, [], settings, 'autoISF_max'), M.effective_cap(a, [], settings, 'autoISF_min'))
        assert rows[variant].metrics == m, variant


def test_example_rows_and_helpers(example_results_dir):
    rows = mf.example_rows(example_results_dir)
    assert [v for v, _, _, _ in rows] == list(EXAMPLE_VARIANTS)
    assert rows[0][1] == {} and rows[1][1] == {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5}
    assert mf.grid_of({v: p for v, p, _, _ in rows}) == {'profile autoISF_max': [1.3, 1.5], 'profile dura_ISF_weight': [0.5, 0.7]}
    assert mf.created_stamp({'created': 'Tue, 08 Sep 2026 03:22:00 +0200'}, 'x') == '2026-09-08T01:22:00Z'
    assert mf.created_stamp({}, '2026-09-06T13:00:00Z') == '2026-09-06T13:00:00Z'
    assert mf.host_of({'url': 'https://ns.example.org/'}, {}) == 'ns.example.org'
    with pytest.raises(mf.FixtureError):
        mf.find_variants(os.path.dirname(example_results_dir))              # results/ itself holds no run


def test_cli_entry_points(tmp_path, example_results_dir, capsys):
    out = str(tmp_path / 'via_main')
    assert mf.main(['--out', out, '--results', example_results_dir]) == 0
    text = capsys.readouterr().out
    assert lint.contains_honesty_statement(text) and 'review order:' in text
    assert os.path.isfile(os.path.join(out, 'status.json'))
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    mf.register(sub)
    args = parser.parse_args(['fixtures', '--out', str(tmp_path / 'via_register'), '--results', example_results_dir])
    assert args.func is mf.run_fixtures and args.func(args) == 0
    assert os.path.isfile(str(tmp_path / 'via_register' / 'round.json'))


def test_cli_refuses_a_dir_without_a_run(tmp_path, capsys):
    (tmp_path / 'empty').mkdir()
    assert mf.main(['--out', str(tmp_path / 'out'), '--results', str(tmp_path / 'empty')]) == mf.EXIT_REFUSED
    assert 'no result CSVs' in capsys.readouterr().err
    assert not os.path.exists(str(tmp_path / 'out'))
