"""
test_explain.py  -  H-502: `hc explain` (hindcast/explain.py) on the committed report fixtures
(tests/fixtures/report: the no-change replay of 2026-09-06 06-09Z) and on synthetic tier-2 documents: loop lookup
with tolerance and the previous-day fallback, the decision pair, the factor chain, the word diff of the reasons,
graceful degradation without prediction files, the vocabulary lint of the rendering, the CLI.
"""
import json
import os

import pytest

from hindcast import cli
from hindcast import contracts as c
from hindcast import explain as ex
from hindcast import export
from hindcast import lint
from hindcast.tests.conftest import FIXTURES_DIR
from hindcast.tests.test_export import make_pred, make_rows

REPORT = os.path.join(FIXTURES_DIR, 'report')
DAY = '2026-09-06'
BASE = c.BASE_VHASH


@pytest.fixture(scope='module')
def fixture_report():
    if not os.path.isfile(export.tier2_path(REPORT, 'series', DAY, BASE)):
        pytest.skip('tests/fixtures/report missing')
    return REPORT


# ----------------------------------------------------------------------------- helpers

def test_parse_when_and_word_diff():
    assert ex.parse_when('2026-09-06T06:08:00Z') == ex.parse_when('2026-09-06T08:08:00+02:00')
    assert ex.parse_when('06:08', day='2026-09-06') == ex.parse_when('2026-09-06T06:08:00Z')
    with pytest.raises(ex.ExplainError):
        ex.parse_when('yesterday at nine')
    ops = ex.word_diff('setting 60m low temp of 0.0U/h.', 'setting 120m low temp of 0U/h.')
    assert ('-', '60m') in ops and ('+', '120m') in ops and ('=', 'low') in ops
    assert ex.render_diff(ops) == 'setting [-60m-] {+120m+} low temp of [-0.0U/h.-] {+0U/h.+}'
    assert ex.render_diff(ex.word_diff('a b', 'a b')) == 'a b'
    assert ex.render_diff(ex.word_diff('', 'new text')) == '{+new text+}'


# ----------------------------------------------------------------------------- the fixture report

def test_explain_fixture_loop(fixture_report):
    e = ex.explain(fixture_report, '2026-09-06T06:08:00Z', 'base')
    assert e.day == DAY and e.vhash == BASE and e.index == 7 and e.loops == 180
    assert e.iso == '2026-09-06T06:07:49Z' and e.header['asked'] == '2026-09-06T06:08:00Z'
    assert e.header['bg'] == 143.8 and e.header['bg_delta_5m'] == pytest.approx(6.0, abs=1.5)
    assert e.header['iob'] == 1.07 and e.header['changed'] == 0
    assert e.phone['smb'] == 0.25 and e.emul['smb'] == 0.25 and e.differs == []
    assert e.base is None                                               # the base replay has no second base column
    assert e.factors['final'] == 1.21 and e.factors['dura'] == 1.21 and e.factors['acce'] == 1
    assert e.levels['eventualBG'] == 197 and e.emul['eventual_bg'] == 197
    assert e.fans['variant']['iob'][0] == 144 and e.fans['phone']['iob'][0] == 144 and e.fans['phone']['cob'] in (None, [])
    assert 'Microbolusing 0.25U' in e.reason_phone and 'Microbolusing 0.25U' in e.reason_emul
    assert e.console_emul and any(line.startswith('final ISF factor is 1.21') for line in e.console_emul)
    assert e.console_phone and e.flows and e.flows[0][2] == '55'
    assert e.missing == []
    text = ex.render(e, console=True, flows=True)
    assert text.startswith('loop 2026-09-06T06:07:49Z (loop 8 of 180 on 2026-09-06')
    assert 'BG 144 mg/dl (8.0 mmol/l)' in text and 'decision pair (phone | base replay | variant' in text
    assert 'acce 1.00 -> bg 1.11 -> pp 1.07 -> dura 1.21 -> drift 1.00 => final 1.21' in text
    assert 'diff:    COB: [-0,-] {+0.0,+}' in text and '[-60m-] {+90m+}' in text
    assert '+ ISF unchanged: 91.8' in text and '- ISF unchanged: 5.1' in text        # the console line diff
    assert 'checking input data sets   [55]' in text and '    Autosens ratio 1.0   [140-x]' in text
    assert text.rstrip().endswith(c.HONESTY_STATEMENT)
    # the module's own wording passes the vocabulary lint (the reason/console lines are data, no may-not word in them)
    assert lint.lint_findings(text, 'line') == []
    # --json round-trips
    json.dumps(e.to_dict(), allow_nan=False)


def test_explain_changed_loop_marks_the_differing_cells(fixture_report):
    series = export.read_series(export.tier2_path(fixture_report, 'series', DAY, BASE))
    i = series['changed'].index(1)
    t_abs = export.day_start(DAY) + series['t'][i]
    e = ex.explain(fixture_report, t_abs, BASE)
    assert e.index == i and e.header['changed'] == 1 and e.differs
    text = ex.render(e)
    assert any(line.rstrip().endswith('*') for line in text.splitlines())
    assert 'decision changed vs the phone: yes' in text


def test_loop_lookup_tolerance_and_vhash_resolution(fixture_report):
    with pytest.raises(ex.ExplainError):
        ex.explain(fixture_report, '2026-09-06T05:00:00Z', 'base')                 # an hour before the window
    with pytest.raises(ex.ExplainError):
        ex.explain(fixture_report, '2026-09-06T06:08:00Z', 'ffff')                 # no such series file
    assert ex.resolve_vhash(fixture_report, DAY, BASE[:8]) == BASE
    assert ex.resolve_vhash(fixture_report, DAY, '') == BASE and ex.resolve_vhash(fixture_report, DAY, 'phone') == BASE
    e = ex.explain(fixture_report, '2026-09-06T06:08:00Z', BASE[:12])
    assert e.vhash == BASE


# ----------------------------------------------------------------------------- synthetic documents

def _write_docs(report_dir, day, vhash, rows, pred=None, flows=None, variant='v'):
    emul, orig = export.series_from_rows(['UNIX time', 'bg accel', 'bg brake'] + [col for col, _, _ in export.SERIES_COLUMNS + export.ORIG_COLUMNS]
                                         + [k for k in rows[0] if k not in ('UNIX time', 'bg accel', 'bg brake')],
                                         rows, day=day, vhash=vhash, variant=variant, pred=pred)
    export.write_tier2(report_dir, emul)
    export.write_tier2(report_dir, orig)
    if pred is not None:
        doc, text = export.pred_from_globals(day, vhash, rows, pred, flows, None, variant=variant)
        export.write_tier2(report_dir, doc)
        export.write_tier2(report_dir, text)
    return emul, orig


def test_previous_day_fallback_and_base_column(tmp_path):
    report = str(tmp_path / 'report')
    t0 = export.day_start(DAY) + 23 * 3600 + 58 * 60                 # 23:58 UTC, six loops a minute apart -> past midnight
    rows = make_rows(n=6, start_s=t0, **{'SMB emul': [0.05, 0.1, 0.3, 0.0, 0.0, 0.0], 'TBR emul': [0, 0, 0, 0.5, 0, 0],
                                         'Ins. Req. emul': [0.12] * 6, 'final ISF orig': [1.18] * 6})
    vhash = c.vhash({'profile dura_ISF_weight': 0.5})
    _write_docs(report, DAY, BASE, make_rows(n=6, start_s=t0), variant='base')
    pred, flows = make_pred(rows, skip=(5,))
    _write_docs(report, DAY, vhash, rows, pred, flows)
    e = ex.explain(report, '2026-09-07T00:00:00Z', vhash[:10])      # the loop at 00:00 belongs to 2026-09-06's documents
    assert e.day == DAY and e.index == 2 and e.emul['smb'] == 0.3 and e.phone['smb'] == 0.05
    assert e.base is not None and e.base['smb'] == 0.05 and e.differs == ['SMB']
    assert e.levels and e.emul['eventual_bg'] == 138
    assert e.fans['variant']['iob'] == [136, 135, 134.4]
    assert any('orig.json (the phone\'s predictions' in m for m in e.missing)   # no pred/orig written
    text = ex.render(e, console=True, flows=True)
    header = [line for line in text.splitlines() if 'phone' in line and 'variant' in line and 'base' in line and 'decision pair' not in line]
    assert header and header[0].split() == ['phone', 'base', 'variant']            # three columns when base exists
    assert 'reason' in text and 'not available (no prediction files' not in text    # the variant has a pred file
    assert 'not recorded (the prediction files of this loop carry no reason text)' in text   # but no transcript
    assert lint.lint_findings(text, 'line') == []
    e2 = ex.explain(report, '2026-09-07T00:01:00Z', vhash)             # the TBR loop
    assert e2.differs == ['SMB', 'TBR'] and e2.emul['tbr'] == 0.5 and e2.emul['smb'] == 0.0   # the phone's SMB is 0.05 at every loop
    e3 = ex.explain(report, '2026-09-07T00:03:00Z', vhash)             # loop without Pred entry (skip=(5,))
    assert e3.levels is None and e3.emul['smb'] == 0.0
    with pytest.raises(ex.ExplainError):
        ex.explain(report, '2026-09-07T00:03:00Z', vhash, day='2026-09-07')   # the wrong day given explicitly


def test_without_prediction_files_the_text_degrades(tmp_path):
    report = str(tmp_path / 'report')
    rows = make_rows(n=4)
    vhash = c.vhash({'profile dura_ISF_weight': 0.6})
    _write_docs(report, DAY, vhash, rows)
    e = ex.explain(report, rows[1]['UNIX time'], vhash)
    assert e.index == 1 and e.levels is None and e.reason_emul is None and e.console_emul is None
    assert e.factors['final'] == 1.18 and e.factors['dura'] == 1.19          # from the series columns
    assert len(e.missing) >= 2
    text = ex.render(e, console=True, flows=True)
    assert 'not available (no prediction files for this loop)' in text
    assert 'missing: pred/2026-09-06/' in text and text.rstrip().endswith(c.HONESTY_STATEMENT)
    assert lint.lint_findings(text, 'line') == []


# ----------------------------------------------------------------------------- CLI

def test_hc_explain_cli(fixture_report, tmp_path, capsys):
    rc = cli.main(['explain', 'anystudy', '2026-09-06T06:08:00Z', '--variant', 'base', '--report-dir', fixture_report,
                   '--studies-dir', str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith('loop 2026-09-06T06:07:49Z') and c.HONESTY_STATEMENT in out
    rc = cli.main(['explain', 'anystudy', '2026-09-06T06:08:00Z', '--variant', 'base', '--report-dir', fixture_report,
                   '--studies-dir', str(tmp_path), '--json'])
    data = json.loads(capsys.readouterr().out)
    assert data['index'] == 7 and data['phone']['smb'] == 0.25
    rc = cli.main(['explain', 'nostudy', '2026-09-06T06:08:00Z', '--studies-dir', str(tmp_path)])
    assert rc == 3 and 'hc explain' in capsys.readouterr().err
    rc = cli.main(['explain', 'anystudy', '2026-09-06T05:00:00Z', '--report-dir', fixture_report, '--studies-dir', str(tmp_path)])
    assert rc == 3


def test_hc_explain_finds_series_at_the_study_root(fixture_study_dir, tmp_path, capsys):
    """Without --report-dir the series are looked up in <study>/report/series, then <study>/series (the committed
    fixture study keeps series/ at its root); --report-dir always wins."""
    import shutil
    from hindcast import study as st
    sd = st.StudyDir(fixture_study_dir)
    assert not os.path.isdir(os.path.join(sd.report_dir, 'series'))
    assert ex.resolve_report_dir(sd) == fixture_study_dir
    assert ex.resolve_report_dir(sd, '/some/where') == '/some/where'
    rc = cli.main(['explain', fixture_study_dir, '2026-09-06T06:00:45Z', '--variant', 'base',
                   '--studies-dir', str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert out.out.startswith('loop 2026-09-06T06:00:45Z') and c.HONESTY_STATEMENT in out.out
    # a study with both: report/series is preferred
    copy = str(tmp_path / 'copy')
    shutil.copytree(fixture_study_dir, copy)
    os.makedirs(os.path.join(copy, 'report', 'series'))
    assert ex.resolve_report_dir(st.StudyDir(copy)) == os.path.join(copy, 'report')
    # neither: the default report dir (and its error message) stays
    shutil.rmtree(os.path.join(copy, 'series'))
    shutil.rmtree(os.path.join(copy, 'report'))
    assert ex.resolve_report_dir(st.StudyDir(copy)) == os.path.join(copy, 'report')
