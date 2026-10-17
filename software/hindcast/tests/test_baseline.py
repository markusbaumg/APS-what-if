"""
test_baseline.py  -  the observed baseline (hindcast/baseline.py, `hc baseline`) on a synthetic cache directory

Nothing here reads the real Nightscout cache: every test writes its own <tmp>/ns.example/{entries,treatments,
devicestatus}/<day>.json and points baseline at it.
"""
import json
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(os.path.dirname(TESTS_DIR))
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)

from hindcast import baseline as bl                                                         # noqa: E402
from hindcast import cli                                                                    # noqa: E402
from hindcast import lint                                                                   # noqa: E402
from ns_client import day_bounds, to_iso                                                    # noqa: E402

DAY = '2026-09-06'
DAY2 = '2026-09-07'
MIN = 60 * 1000


def _write(root, coll, day, records):
    os.makedirs(os.path.join(root, coll), exist_ok=True)
    with open(os.path.join(root, coll, day + '.json'), 'w', encoding='utf-8') as fh:
        json.dump(records, fh)


def _sgv(ms, v, **kw):
    rec = {'date': ms, 'sgv': v, 'type': 'sgv', 'units': 'mg/dl', 'isValid': True}
    rec.update(kw)
    return rec


def _treat(ms, **kw):
    rec = {'created_at': to_iso(ms), 'date': ms, 'isValid': True, 'identifier': 'id-%d-%s' % (ms, kw.get('type'))}
    rec.update(kw)
    return rec


def _ds(ms):
    return {'created_at': to_iso(ms), 'openaps': {'suggested': {'deliverAt': to_iso(ms), 'bg': 120, 'reason': 'x'}}}


def make_day(root, day, low_slots=12, high_slots=24, vhigh_slots=6, gap_slots=6, loops=True):
    """288 slots: low_slots at 60 mg/dl (of them 6 at 50), high_slots at 200, vhigh_slots at 260, gap_slots empty
    (at the end of the day), the rest at 120. Two readings per slot (minute 0 and 2), one duplicate."""
    start, end = day_bounds(day)
    entries = []
    for i in range(288 - gap_slots):
        if i < low_slots:
            v = 50 if i < 6 else 60
        elif i < low_slots + high_slots:
            v = 200
        elif i < low_slots + high_slots + vhigh_slots:
            v = 260
        else:
            v = 120
        t = start + i * 5 * MIN
        entries += [_sgv(t, v), _sgv(t + 2 * MIN, v)]
    entries.append(_sgv(start, 999))                         # same timestamp again: counted once (the first)
    entries.append(_sgv(start + 3 * MIN, 400, isValid=False))   # invalid: skipped
    entries.append(_sgv(end + MIN, 30))                      # next day: ignored
    _write(root, 'entries', day, entries)
    treatments = [
        _treat(start + 60 * MIN, type='NORMAL', eventType='Meal Bolus', insulin=2.5),
        _treat(start + 120 * MIN, type='NORMAL', eventType='Correction Bolus', insulin=1.0),
        _treat(start + 130 * MIN, type='NORMAL', eventType='Temp Basal', rate=0.5, absolute=0.5, duration=30),
        _treat(start + 140 * MIN, type='SMB', eventType='Correction Bolus', insulin=0.3),
        _treat(start + 145 * MIN, type='SMB', eventType='Correction Bolus', insulin=0.25),
        _treat(start + 150 * MIN, type='SMB', eventType='Correction Bolus', insulin=0.5, isValid=False),
        _treat(start + 160 * MIN, eventType='Carb Correction', carbs=12),
        _treat(start - 10 * MIN, type='NORMAL', eventType='Meal Bolus', insulin=4.0),        # previous day
    ]
    treatments.append(dict(treatments[3]))                                                     # duplicate identifier
    _write(root, 'treatments', day, treatments)
    if loops:
        _write(root, 'devicestatus', day, [_ds(start + i * 5 * MIN) for i in range(12)])      # one hour of runs
    return start, end


@pytest.fixture
def cache(tmp_path):
    root = str(tmp_path / 'ns.example')
    make_day(root, DAY)
    make_day(root, DAY2, low_slots=0, high_slots=0, vhigh_slots=0, gap_slots=0, loops=False)
    return root


# ----------------------------------------------------------------------------- one day

def test_day_stats_cgm_on_the_5_min_grid(cache):
    s = bl.day_stats(DAY, cache_root=cache)
    assert set(s) == set(bl.DAY_KEYS) and s['label'] == 'observed' and s['day'] == DAY
    n = 288 - 6
    assert s['cgm_minutes'] == n * 5 and s['cgm_gap_min'] == 30
    assert s['cgm_coverage_pct'] == round(100 * n / 288, 1)
    # slot 0: readings 50, 50 and the duplicate timestamp ignored -> 50
    assert s['below_3_9_pct'] == round(100 * 12 / n, 1)
    assert s['below_3_0_pct'] == round(100 * 6 / n, 1)
    assert s['above_10_pct'] == round(100 * 30 / n, 1)
    assert s['above_13_9_pct'] == round(100 * 6 / n, 1)
    assert s['tir_pct'] == round(100 * (n - 12 - 30) / n, 1)
    vals = [50] * 6 + [60] * 6 + [200] * 24 + [260] * 6 + [120] * (n - 42)
    mean = sum(vals) / n
    assert s['mean_mmol'] == round(mean / 18.0, 2)
    sd = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
    assert s['cv_pct'] == round(100 * sd / mean, 1)
    assert s['cgm_note'] is None
    assert s['tbr_pct'] == s['below_3_9_pct']                        # the names report.observed_days reads


def test_range_edges_inclusive():
    start, end = day_bounds(DAY)
    cgm = [(start, 70.0), (start + 5 * MIN, 180.0), (start + 10 * MIN, 69.9), (start + 15 * MIN, 180.1),
           (start + 20 * MIN, 54.0), (start + 25 * MIN, 250.0)]
    s = bl.cgm_stats(cgm, start, end)
    assert s['tir_pct'] == round(100 * 2 / 6, 1)            # 70 and 180 are in range
    assert s['below_3_0_pct'] == 0.0 and s['above_13_9_pct'] == 0.0
    assert s['below_3_9_pct'] == round(100 * 2 / 6, 1)      # 69.9 and 54
    assert s['above_10_pct'] == round(100 * 2 / 6, 1)       # 180.1 and 250


def test_day_stats_treatments(cache):
    s = bl.day_stats(DAY, cache_root=cache)
    assert s['manual_U'] == 3.5
    assert s['manual_boluses'] == 2 and s['manual_bolus_U'] == 3.5          # temp basal (NORMAL) is not a bolus
    assert s['smb_count'] == 2 and s['smb_U'] == 0.55                        # invalid and duplicate skipped
    assert s['carb_entries'] == 1


def test_day_stats_loop(cache):
    s = bl.day_stats(DAY, cache_root=cache)
    assert s['loop'] is True and s['loop_runs'] == 12 and s['loop_hours'] == 1.0
    s2 = bl.day_stats(DAY2, cache_root=cache)
    assert s2['loop'] is None and s2['loop_runs'] is None and s2['loop_hours'] is None
    assert s2['tir_pct'] == 100.0 and s2['cgm_gap_min'] == 0


def test_missing_files(tmp_path):
    root = str(tmp_path / 'empty')
    os.makedirs(root)
    s = bl.day_stats(DAY, cache_root=root)
    assert s['cgm_note'] == 'no CGM file' and s['tir_pct'] is None and s['cgm_minutes'] is None
    assert s['manual_boluses'] is None and s['loop'] is None
    assert bl.strip_label(s) == 'observed TIR n/a'
    with pytest.raises(ValueError):
        bl.day_stats('2026-9-6', cache_root=root)


def test_strip_label_passes_baseline_lint_only(cache):
    text = bl.strip_label(bl.day_stats(DAY, cache_root=cache))
    assert text.startswith('observed TIR ') and text.endswith(' %')
    assert lint.lint_text(text, kind='baseline') is None
    with pytest.raises(lint.LintError):
        lint.lint_text(text, kind='line')


# ----------------------------------------------------------------------------- the period and the page

def test_summarize_median_mean_and_counts(cache):
    b = bl.build([DAY, DAY2], cache_root=cache, now='2026-10-17T06:00Z')
    s = b['summary']
    assert s['days'] == 2 and s['days_with_loop'] == 1 and s['days_with_cgm'] == 2
    assert s['days_without_manual_bolus'] == 0 and s['days_tir_over_goal'] == 1
    t1, t2 = (r['tir_pct'] for r in b['days'])
    assert s['tir_pct']['median'] == round((t1 + t2) / 2, 2) == s['tir_pct']['mean']
    assert s['loop_hours'] == {'median': 1.0, 'mean': 1.0, 'days': 1}                   # None excluded
    assert b['label'] == 'observed' and b['source'].endswith('ns.example') and b['range_mmol'] == [3.9, 10.0]
    json.dumps(b)                                                                        # serialisable


def test_markdown_lints_line_by_line_and_carries_honesty(cache):
    b = bl.build([DAY, DAY2], cache_root=cache, now='2026-10-17T06:00Z')
    text = bl.render_markdown(b)
    assert bl.lint_markdown(text) is None
    assert lint.contains_honesty_statement(text)
    assert '| %s |' % DAY in text and 'observed time in range' in text
    for line in text.split('\n'):                                    # every TIR mention says 'observed' on its line
        if 'TIR' in line or 'time in range' in line.lower():
            assert 'observed' in line.lower()
    with pytest.raises(lint.LintError):                              # the same page is refused as a card
        lint.lint_text(text, kind='card')
    with pytest.raises(lint.LintError):
        bl.lint_markdown(text.replace(lint.HONESTY_STATEMENT, ''))


def test_lint_baseline_kind():
    assert 'baseline' in lint.KINDS
    assert lint.lint_text('observed TIR 86.2 % on 2026-09-06', kind='baseline') is None
    assert lint.lint_text('Observed time in range 86 %.', kind='baseline') is None
    for refused in ('Time in range rises by 3 %.',                    # no 'observed' on the line
                    'observed TIR 86 %\nTIR would rise',             # the second line lacks it
                    'observed TIR 86 %, a better setting'):          # other rules still apply
        with pytest.raises(lint.LintError):
            lint.lint_text(refused, kind='baseline')
    for kind in ('card', 'proposal', 'issue', 'line'):               # the exemption is the baseline kind's only
        assert lint.lint_findings('observed TIR 86 %', kind=kind, require_honesty=False)
    assert not lint.lint_findings('mmol 5.4 at 09:00', kind='baseline')        # no privacy rules (private page)


# ----------------------------------------------------------------------------- days and CLI

def test_frozen_days_default(tmp_path):
    fz = tmp_path / 'frozen' / 'ns.example'
    fz.mkdir(parents=True)
    for d in ('2026-08-29', '2026-08-30', '2026-09-06', '2026-10-17'):
        (fz / (d + '.meta.json')).write_text('{}')
    (fz / 'notes.txt').write_text('')
    assert bl.frozen_days(frozen_dir=str(fz)) == ['2026-08-30', '2026-09-06']
    assert bl.default_days(str(fz)) == ['2026-08-30', '2026-09-06']
    assert bl.frozen_days(frozen_dir=str(tmp_path / 'none')) == []


def test_cli_prints_and_writes(cache, tmp_path, capsys):
    assert cli.main(['baseline', DAY + '..' + DAY2, '--cache-dir', cache]) == 0
    out = capsys.readouterr().out
    assert out.startswith('# Hindcast observed baseline') and DAY2 in out
    docs = tmp_path / 'docs' / 'BASELINE.md'
    studies = tmp_path / 'studies'
    assert cli.main(['baseline', DAY, '--cache-dir', cache, '--write', 'docs', '--docs-path', str(docs),
                     '--studies-dir', str(studies)]) == 0
    assert docs.read_text(encoding='utf-8').startswith('# Hindcast observed baseline')
    data = json.loads((studies / 'baseline.json').read_text(encoding='utf-8'))
    assert [r['day'] for r in data['days']] == [DAY] and data['label'] == 'observed'
    assert b'\r' not in docs.read_bytes()
    assert cli.main(['baseline', 'not-a-day', '--cache-dir', cache]) == 3
    assert cli.main(['baseline', DAY, '--cache-dir', cache, '--json', str(tmp_path / 'b.json'), '--write', 'docs',
                     '--docs-path', str(docs)]) == 0
    assert (tmp_path / 'b.json').exists()


def test_module_main(cache, capsys):
    assert bl.main([DAY, '--cache-dir', cache]) == 0
    assert 'observed' in capsys.readouterr().out
