"""Tests for hindcast/export.py (backlog H-002): series / pred / orig documents, rounding, embedding, fixtures."""
import json
import math
import os

import pytest

from hindcast import contracts, export
from hindcast.tests.conftest import FIXTURES_DIR, NO_CHANGE_CSV

REPORT_FIXTURES = os.path.join(FIXTURES_DIR, 'report')
DAY = '2026-09-06'
T0 = export.day_start(DAY)

# the 43 CSV column names as read_result_csv joins them
COLUMNS = ['id', 'UTC time', 'Z', 'UNIX time', 'bg accel', 'bg brake', 'target low orig', 'target high orig',
           'target low emul', 'target high emul', 'cob', 'iob', 'eff. iobTH emul', 'tol. iobTH emul', 'act',
           'auto sens orig', 'final ISF orig', 'dura min- utes', 'dura avg.', 'lin.fit min- utes', 'lin.fit avg-Δ',
           'parab fit correl', 'parab fit durat', 'parab fit last-Δ', 'parab fit next-Δ', 'auto sens emul',
           'acce ISF emul', 'bg ISF emul', 'pp ISF emul', 'drift ISF emul', 'dura ISF emul', 'final ISF emul',
           'ISF orig', 'ISF prof', 'ISF emul', 'Ins. Req. orig', 'Ins. Req. emul', 'max bolus orig', 'max bolus emul',
           'SMB orig', 'SMB emul', 'TBR orig', 'TBR emul']


def make_rows(n=6, step_s=60.0, start_s=T0 + 6 * 3600, **over):
    """Synthetic CSV rows (every column present) with per-column overrides {column: list}."""
    rows = []
    for i in range(n):
        r = {c: 0.0 for c in COLUMNS}
        r.update({'id': float(i), 'UTC time': '06:%02d:00' % i, 'Z': 'Z', 'UNIX time': start_s + i * step_s,
                  'bg accel': 120.0 + i, 'bg brake': None, 'target low orig': 100.8, 'target high orig': 100.8,
                  'target low emul': 100.8, 'target high emul': 100.8, 'iob': 1.0, 'eff. iobTH emul': 5.6,
                  'tol. iobTH emul': 7.28, 'act': 0.0123456, 'auto sens orig': 1.0, 'final ISF orig': 1.1,
                  'auto sens emul': 1.0, 'acce ISF emul': 0.994, 'bg ISF emul': 1.0779, 'pp ISF emul': 1.0,
                  'drift ISF emul': 1.0, 'dura ISF emul': 1.1877, 'final ISF emul': 1.18, 'ISF orig': 77.76,
                  'ISF prof': 91.8, 'ISF emul': 77.76, 'Ins. Req. orig': 0.12, 'Ins. Req. emul': 0.13,
                  'max bolus orig': 0.7, 'max bolus emul': 0.7, 'SMB orig': 0.05, 'SMB emul': 0.05,
                  'TBR orig': 0.0, 'TBR emul': 0.0, 'dura min- utes': 45.0, 'dura avg.': 136.84})
        for col, values in over.items():
            r[col] = values[i]
        rows.append(r)
    return rows


def make_pred(rows, skip=()):
    """Synthetic emulator_core.Pred / FlowChart globals for `rows` (keys round(ms/1000, 1))."""
    pred, flows = {}, {}
    for i, r in enumerate(rows):
        if i in skip:
            continue
        k = export.pred_key(r['UNIX time'])
        levels = {'eventualBG': 138, 'minPredBG': 111, 'minIOBPredBG': 84, 'minZTGuardBG': 76, 'avgPredBG': 111,
                  'threshold': 70.0, 'maxDelta': 27.2765, 'naive_eventualBG': 61}
        if i == 1:
            levels.update({'SMBoff': 'minGuard(64) <   \n   threshold(69.0)', 'type': 'threshold', 'value': 69.0,
                           'minGuardBG1': 64, 'source': 'UAM', 'timePos': 43})
        pred[k] = {'IOBpredBGs': [136, 135, 134.4], 'ZTpredBGs': [136, 132], 'UAMpredBGs': [136, 136], 'COBpredBGs': [136, 131],
                   'IOB': [136, 135], 'ZT': [136, 132], 'UAM': [136, 137], 'COB': [],
                   'Levels': levels, 'acceISF': 0.99388, 'BZ_ISF': 1.07791, 'pp_ISF': 1, 'dura_ISF': 1.18773, 'drift_ISF': 1,
                   'Delta_ISF': 1, 'emulISF': 77.766, 'emuliobTH': 56.0}
        flows[k] = [{'title': 'checking\ninput data sets', 'indent': '0', 'adr': '55'},
                    {'title': 'Autosens ratio 1.0', 'indent': '1', 'adr': '140-x'}]
    return pred, flows


TRANSCRIPT = """AAPS scan from no_change for SMB comparison created on Fri, 16 Oct 2026 20:59:58 +0200
FILE=ns:

========== loop in row 1 ========== of Nightscout ns:
  created at= {s0}
---------- Script Debug --------------------
Autosens ratio: 1.0;
ISF unchanged: 91.8
----------------------------------
start AutoISF 3.2.0
----------------------------------
final ISF factor is 1.18
---------- Reason --------------------------
COB: 0.0, Dev: 1.5; insulinReq 0.1
second reason line

========== loop in row 2 ========== of Nightscout ns:
  created at= {s1}
---------- Script Debug --------------------
Autosens ratio: 1.0;
final ISF factor is 1.17
---------- Reason --------------------------
Microbolusing 0.1U.
"""


def transcript_file(tmp_path, rows):
    stamps = [export.iso_of(r['UNIX time']).replace('Z', '.328Z') for r in rows[:2]]
    path = tmp_path / 'x.no_change.txt'
    path.write_text(TRANSCRIPT.format(s0=stamps[0], s1=stamps[1]), encoding='utf-8')
    return str(path)


# ----------------------------------------------------------------------------- helpers

def test_rnd_rules():
    assert export.rnd(136.0, 'bg') == 136 and isinstance(export.rnd(136.0, 'bg'), int)
    assert export.rnd(136.38250235, 'bg') == 136.4
    assert export.rnd(0.1234567, 'insulin') == 0.123
    assert export.rnd(1.18773, 'factor') == 1.19
    assert export.rnd(45.0, 'minutes') == 45
    assert export.rnd(1788674445.34, 't') == 1788674445.3
    assert export.rnd(None, 'bg') is None and export.rnd(float('nan'), 'bg') is None
    assert export.rnd(float('inf'), 'bg') is None and export.rnd(True, 'bg') is None and export.rnd('x', 'bg') is None


def test_day_helpers():
    assert export.day_of(1788674445.3) == DAY
    assert export.day_start(DAY) == 1788652800.0
    assert export.iso_of(1788674445.3) == '2026-09-06T06:00:45Z'
    assert export.pred_key(1788674445.3) == 1788674445.3
    assert export.pred_key(1788674445.34) == 1788674445.3


def test_embed_json_escapes_and_refuses_nan():
    text = export.embed_json({'s': '</script><b>', 'n': [1, 2.5, None]})
    assert '</' not in text and '<\\/script>' in text
    assert json.loads(text.replace('<\\/', '</')) == {'s': '</script><b>', 'n': [1, 2.5, None]}
    with pytest.raises(ValueError):
        export.embed_json({'x': float('nan')})
    tag = export.script_tag('tier1', {'a': 1})
    assert tag.startswith('<script type="application/json" id="tier1">') and tag.endswith('</script>')


# ----------------------------------------------------------------------------- series from synthetic rows

def test_series_from_rows_shape_and_rounding():
    rows = make_rows(6, **{'SMB emul': [0.05, 0.05, 0.1, 0.0, 0.0, 0.3], 'TBR emul': [0.0, 0.0, 0.0, 0.0, 0.1, 0.0],
                           'Ins. Req. emul': [0.13, 0.12, 0.2, 0.12, 0.12, 0.12]})
    emul, orig = export.series_from_rows(COLUMNS, rows)
    assert emul['kind'] == 'series' and orig['kind'] == 'orig' and emul['day'] == DAY
    assert emul['vhash'] == contracts.BASE_VHASH and orig['variant'] == 'phone'
    assert emul['loops'] == 6 and len(emul['t']) == 6 and emul['t'][0] == 21600 and emul['t'][1] == 21660
    assert emul['smb'] == [0.05, 0.05, 0.1, 0, 0, 0.3]
    assert emul['changed'] == [0, 0, 1, 0, 1, 1]              # SMB +0.05 is inside the tolerance; 0.1, TBR 0.1, 0.3 are not
    assert emul['isf_factor'] == [1.18] * 6 and emul['f_dura'] == [1.19] * 6 and emul['isf'] == [77.8] * 6
    assert emul['flags'] == ['no_pred'] and emul['min_guard'] == [None] * 6 and emul['smb_off'] == [None] * 6
    assert emul['units']['bg'] == 'mg/dl' and emul['window'] == ['2026-09-06T06:00:00Z', '2026-09-06T06:05:00Z']
    assert orig['bg'] == [120, 121, 122, 123, 124, 125] and orig['act'] == [0.012] * 6 and orig['dura_min'] == [45] * 6
    assert orig['low_ahead'] == [0] * 6 and orig['events'] == []
    assert export.validate_series(emul) == [] and export.validate_series(orig) == []
    for doc in (emul, orig):
        json.dumps(doc, allow_nan=False)


def test_series_uses_brake_column_and_explicit_day():
    rows = make_rows(3, **{'bg accel': [120.0, None, 122.0], 'bg brake': [None, 118.5, None]})
    emul, orig = export.series_from_rows(COLUMNS, rows, day='2026-09-05', vhash='ab' * 20, variant='v1')
    assert orig['bg'] == [120, 118.5, 122]
    assert emul['t'][0] == 86400 + 21600                      # relative to the given day's midnight, past 86400
    assert emul['vhash'] == 'ab' * 20 and emul['variant'] == 'v1'


def test_events_and_hindsight():
    bg = [120.0, 118.0, 69.0, 65.0, 80.0, 190.0, 185.0, 100.0]
    t = [T0 + 6 * 3600 + x for x in (0, 60, 90 * 60, 91 * 60, 92 * 60, 93 * 60, 300 * 60, 301 * 60)]
    cob = [0.0, 0.0, 0.0, 30.0, 29.0, 29.0, 0.0, 0.0]
    tgt = [100.8, 100.8, 100.8, 97.2, 97.2, 97.2, 97.2, 100.8]
    rows = make_rows(8, **{'bg accel': bg, 'UNIX time': t, 'cob': cob, 'target low orig': tgt, 'target high orig': tgt})
    emul, orig = export.series_from_rows(COLUMNS, rows, events=[{'kind': 'carbs', 't': 21600, 't_end': None, 'value': 30,
                                                                'label': '30 g carbs entered'}])
    kinds = [(e['kind'], e['t']) for e in orig['events']]
    assert ('carbs', 21600) in kinds
    assert ('cgm_gap', 21660) in kinds and ('low', 21600 + 90 * 60) in kinds
    assert ('cob_rise', 21600 + 91 * 60) in kinds and ('target_change', 21600 + 91 * 60) in kinds
    assert ('target_change', 21600 + 301 * 60) in kinds
    assert [e for e in orig['events'] if e['kind'] == 'low'][0]['label'] == 'recorded BG 3.8 mmol/l (below 3.9) followed here'
    assert kinds == sorted(kinds, key=lambda k: (k[1], k[0]))
    # loop 0 and 1: a low follows within 120 min (at 90 min); loop 4 (92 min): no further low; high at 93 min within 3 h
    assert orig['low_ahead'] == [1, 1, 1, 0, 0, 0, 0, 0]
    assert orig['high_ahead'] == [1, 1, 1, 1, 1, 0, 0, 0]          # loop 5 is itself the high; nothing > 180 follows it within 3 h


def test_low_threshold_is_the_metrics_constant_so_a_70_0_reading_counts():
    from hindcast import metrics
    assert export.LOW_MGDL == metrics.LOW_BG_MGDL == pytest.approx(70.2)        # 3.9 mmol, one constant
    assert export.HIGH_MGDL == metrics.HIGH_BG_MGDL and export.LOW_AHEAD_S == metrics.LOW_FOLLOW_S
    assert export.HIGH_AHEAD_S == metrics.HIGH_FOLLOW_S and export.MGDL_PER_MMOL == metrics.MMOL_TO_MGDL
    bg = [120.0, 110.0, 70.0, 80.0, 71.0, 90.0]                                # 70.0 is below 3.9 mmol; 71.0 is not
    t = [T0 + 6 * 3600 + x for x in (0, 60 * 60, 90 * 60, 91 * 60, 200 * 60, 201 * 60)]
    rows = make_rows(6, **{'bg accel': bg, 'UNIX time': t})
    emul, orig = export.series_from_rows(COLUMNS, rows)
    lows = [e for e in orig['events'] if e['kind'] == 'low']
    assert [e['t'] for e in lows] == [21600 + 90 * 60] and lows[0]['value'] == 70.0
    assert lows[0]['label'] == 'recorded BG 3.9 mmol/l (below 3.9) followed here'
    assert orig['low_ahead'] == [1, 1, 0, 0, 0, 0]                            # the P window of the 70.0 reading
    # metrics (the P window) and export (the positional covariate) agree on the same readings
    assert metrics.hindsight_covariates(t, bg)['before_low_2h'] == orig['low_ahead']
    assert metrics._recorded_lows(t, bg) == [t[2]]
    low_i, _ = export.hindsight(t, bg)
    assert low_i == orig['low_ahead']


def test_fixture_replay_runs_inside_the_offline_guard(tmp_path, monkeypatch):
    """make_report_fixtures.replay() must not reach the network even when HINDCAST_OFFLINE allows it."""
    import emulator_core as ec
    import ns_client
    from hindcast import freeze
    from hindcast.tests import make_report_fixtures as mrf
    seen = {}

    def fake_parameters_known(source, options, vdf, start, stop, *rest):
        try:
            ns_client.NightscoutClient._get(None, 'api/v1/entries.json', {'count': 1})
        except freeze.OfflineError:
            seen['guarded'] = True
        except Exception as e:                                                   # the unpatched client (self is None)
            seen['guarded'] = type(e).__name__
        workdir = os.path.dirname(vdf)
        for name in ('x.no_change.csv', 'x.no_change.txt', 'x.orig.txt'):
            open(os.path.join(workdir, name), 'w').close()
        return (None, stop) + (None,) * 6

    original_get = ns_client.NightscoutClient._get
    monkeypatch.setattr(ec, 'parameters_known', fake_parameters_known)
    monkeypatch.setattr(ec, 'Pred', {1.0: {}}, raising=False)
    monkeypatch.setattr(ec, 'FlowChart', {1.0: []}, raising=False)
    monkeypatch.setenv('HINDCAST_OFFLINE', '0')
    pred, flows, csv, txt, orig, seconds = mrf.replay(('2026-09-06T06:00:00Z', '2026-09-06T09:00:00Z'), str(tmp_path))
    assert seen.get('guarded') is True, seen
    assert pred == {1.0: {}} and csv.endswith('x.no_change.csv') and txt.endswith('x.no_change.txt') and orig.endswith('x.orig.txt')
    assert ns_client.NightscoutClient._get is original_get                      # restored after the block
    assert 'no network' not in mrf.__doc__ and 'offline_guard' in mrf.__doc__


def test_series_with_pred_levels():
    rows = make_rows(4)
    pred, _ = make_pred(rows, skip=(3,))
    emul, _ = export.series_from_rows(COLUMNS, rows, pred=pred)
    assert emul['eventual_bg'] == [138, 138, 138, None] and emul['min_pred_bg'][0] == 111
    assert emul['min_guard'] == [None, 64, None, None] and emul['smb_off'] == [None, 'threshold', None, None]
    assert emul['flags'] == ['pred_partial:3/4']


def test_series_missing_column_is_named():
    with pytest.raises(KeyError, match='SMB emul'):
        export.series_from_rows([c for c in COLUMNS if c != 'SMB emul'], make_rows(2))


# ----------------------------------------------------------------------------- pred documents

def test_transcript_blocks_and_matching(tmp_path):
    rows = make_rows(3)
    path = transcript_file(tmp_path, rows)
    blocks = export.transcript_blocks(path)
    assert [b['row'] for b in blocks] == [1, 2]
    assert blocks[0]['console'] == ['Autosens ratio: 1.0;', 'ISF unchanged: 91.8', 'start AutoISF 3.2.0', 'final ISF factor is 1.18']
    assert blocks[0]['reason'] == 'COB: 0.0, Dev: 1.5; insulinReq 0.1 second reason line'
    assert blocks[1]['reason'] == 'Microbolusing 0.1U.'
    assert abs(blocks[0]['t'] - rows[0]['UNIX time']) < 1.0
    matched = export.match_blocks(blocks, export.loop_times(rows))
    assert matched[0] is blocks[0] and matched[1] is blocks[1] and matched[2] is None


def test_pred_from_globals(tmp_path):
    rows = make_rows(3, cob=[0.0, 12.0, 0.0])
    pred, flows = make_pred(rows, skip=(2,))
    doc, text = export.pred_from_globals(DAY, contracts.BASE_VHASH, rows, pred, flows, transcript=transcript_file(tmp_path, rows),
                                         variant='no_change', window=('2026-09-06T06:00:00Z', '2026-09-06T09:00:00Z'))
    assert doc['kind'] == 'pred' and text['kind'] == 'pred_text' and doc['t'] == text['t']
    assert doc['iob'][0] == [136, 135, 134.4] and doc['zt'][0] == [136, 132]
    assert doc['cob'][0] is None and doc['cob'][1] == [136, 131]          # no COB -> no COB prediction
    assert doc['levels'][1]['minGuardBG1'] == 64 and doc['levels'][1]['SMBoff'].startswith('minGuard')
    assert doc['levels'][0]['maxDelta'] == 27.3 and doc['levels'][1]['timePos'] == 43
    assert doc['factors'][0] == {'acce': 0.99, 'bg': 1.08, 'pp': 1, 'dura': 1.19, 'drift': 1, 'delta': 1, 'final': 1.18}
    assert doc['isf'][0] == 77.8 and doc['iob_th'][0] == 5.6
    assert doc['reason'][0].startswith('COB: 0.0') and doc['reason'][2] is None
    assert doc['iob'][2] is None and doc['levels'][2] is None
    assert doc['flags'] == ['pred_partial:2/3'] and text['flags'] == ['pred_partial:2/3']
    lines, boxes = export.expand_text(text, 0)
    assert lines[-1] == 'final ISF factor is 1.18' and boxes[0] == ['checking\ninput data sets', '0', '55']
    assert text['console'][0][0] == text['console'][1][0]                 # 'Autosens ratio: 1.0;' shared through the table
    assert len(text['boxes']) == 2 and text['flows'][0] == [0, 1] and text['flows'][2] is None
    assert export.validate_series(doc) == [] and export.validate_series(text) == []
    assert export.expand_text(text, 2) == (None, None)


def test_pred_orig_from_globals(tmp_path):
    rows = make_rows(2)
    pred, _ = make_pred(rows)
    doc, text = export.pred_orig_from_globals(DAY, rows, pred, transcript=transcript_file(tmp_path, rows))
    assert doc['kind'] == 'pred_orig' and doc['variant'] == 'phone' and doc['vhash'] == contracts.BASE_VHASH
    assert doc['iob'][0] == [136, 135] and doc['uam'][0] == [136, 137] and doc['cob'][0] is None
    assert doc['reason'][1] == 'Microbolusing 0.1U.'
    assert text['kind'] == 'pred_orig_text' and export.expand_text(text, 1)[0] == ['Autosens ratio: 1.0;', 'final ISF factor is 1.17']
    assert export.validate_series(doc) == [] and export.validate_series(text) == []


# ----------------------------------------------------------------------------- validation and files

def test_validate_series_catches_shape_errors():
    emul, _ = export.series_from_rows(COLUMNS, make_rows(3))
    assert export.validate_series(emul) == []
    bad = dict(emul, smb=[0.0, 0.0])
    assert any('smb has 2 entries' in p for p in export.validate_series(bad))
    bad = dict(emul, t=[3, 2, 1])
    assert any('ascending' in p for p in export.validate_series(bad))
    bad = dict(emul, kind='other')
    assert any('unknown kind' in p for p in export.validate_series(bad))
    bad = dict(emul, isf=[float('nan'), 1, 2])
    assert any('NaN' in p for p in export.validate_series(bad))
    bad = dict(emul)
    del bad['changed']
    assert any('missing series key changed' in p for p in export.validate_series(bad))
    text = {'format': 1, 'kind': 'pred_text', 'day': DAY, 'vhash': 'x', 'variant': 'v', 'loops': 1, 't': [1],
            'strings': ['a'], 'boxes': [], 'console': [[0, 1]], 'flows': [None]}
    assert any('console[0] indexes outside' in p for p in export.validate_series(text))
    assert export.validate_series('nope') == ['document is not an object']


def test_write_and_read_tier2(tmp_path):
    rows = make_rows(3)
    emul, orig = export.series_from_rows(COLUMNS, rows)
    path, size = export.write_tier2(str(tmp_path), emul)
    assert path == os.path.join(str(tmp_path), 'series', DAY, contracts.BASE_VHASH + '.json') and size > 0
    assert export.read_series(path) == emul
    opath, osize = export.write_tier2(str(tmp_path), orig)
    assert opath.endswith(os.path.join('series', DAY, 'orig.json'))
    assert export.write_tier2(str(tmp_path), orig, overwrite=False) == (opath, None)
    assert not [p for p in os.listdir(os.path.dirname(path)) if p.endswith('.tmp')]
    with pytest.raises(contracts.ContractError, match='smb has'):
        export.write_tier2(str(tmp_path), dict(emul, smb=[1.0]))
    assert export.tier2_path('r', 'pred_text', DAY, 'abc') == os.path.join('r', 'pred', DAY, 'abc.text.json')
    assert export.tier2_path('r', 'pred_orig_text', DAY) == os.path.join('r', 'pred', DAY, 'orig.text.json')


def test_build_tier1_embeds():
    study = contracts.Study(name='demo', created='2026-10-16T19:10:00Z', source='ns:', host='h', settings_sha='0' * 40,
                            code_sha='0' * 40, days=[contracts.StudyDay(DAY)],
                            space={'profile autoISF_max': contracts.SpaceParam('float', 1.1, 1.8, 0.05)})
    params = {'profile autoISF_max': 1.3}
    row = contracts.TrialRow(trial=0, key=contracts.key(params, DAY, '0' * 40, '0' * 40, '0' * 40, 1), phase='grid',
                             vhash=contracts.vhash(params), variant='autoISF_max_1.3', params=params, day=DAY, status='done',
                             code_sha='0' * 40, settings_sha='0' * 40, freeze_sha='0' * 40, metrics={'loops': 180},
                             files={'series': 'series/%s/%s.json' % (DAY, contracts.vhash(params))})
    tier1 = export.build_tier1(study, [row], generated='2026-10-16T20:00:00Z')
    back = json.loads(export.embed_json(tier1).replace('<\\/', '</'))
    assert back['honesty'] == contracts.HONESTY_STATEMENT and back['study']['name'] == 'demo'
    assert back['trials'][0]['vhash'] == row.vhash and back['paths']['series'] == 'series/{day}/{vhash}.json'
    assert back['base_vhash'] == contracts.BASE_VHASH and 'profile max_iob' in back['locked']
    assert 'carb ratio' in back['not_testable'] and back['status'] is None and back['rounds'] == []


# ----------------------------------------------------------------------------- the real CSV and the fixture set

@pytest.fixture(scope='module')
def real_series():
    if not os.path.isfile(NO_CHANGE_CSV):
        pytest.skip('no_change replay CSV missing under software/variants/')
    return export.series_from_csv(NO_CHANGE_CSV, variant='no_change')


def test_real_csv_series(real_series):
    emul, orig = real_series
    assert emul['loops'] == 180 and emul['day'] == DAY
    assert 21600 <= emul['t'][0] < 21700 and emul['t'][-1] < 32400 and emul['t'] == sorted(emul['t'])
    assert export.validate_series(emul) == [] and export.validate_series(orig) == []
    assert sum(emul['changed']) <= 12                              # the no-change replay: few loops differ from the phone
    assert all(v is None or abs(v * 1000 - round(v * 1000)) < 1e-6 for v in emul['smb'])
    assert all(v is None or abs(v * 100 - round(v * 100)) < 1e-6 for v in emul['isf_factor'])
    assert all(v is None or abs(v * 10 - round(v * 10)) < 1e-6 for v in orig['bg'])
    assert len(export.embed_json(emul)) / 180 <= 100               # bytes per loop budget of the series contract
    assert len(export.embed_json(orig)) / 180 <= 100


@pytest.fixture(scope='module')
def fixture_docs():
    manifest = os.path.join(REPORT_FIXTURES, 'MANIFEST.json')
    if not os.path.isfile(manifest):
        pytest.skip('tests/fixtures/report missing: run python -m hindcast.tests.make_report_fixtures')
    with open(manifest, encoding='utf-8') as fh:
        m = json.load(fh)
    docs = {rel: export.read_series(os.path.join(REPORT_FIXTURES, rel)) for rel in m['files']}
    return m, docs


def test_fixture_set_is_consistent(fixture_docs):
    manifest, docs = fixture_docs
    kinds = {d['kind'] for d in docs.values()}
    assert {'series', 'orig'} <= kinds
    ts = {tuple(d['t']) for d in docs.values()}
    assert len(ts) == 1, 'every fixture document shares one loop axis'
    for rel, size in manifest['files'].items():
        assert os.path.getsize(os.path.join(REPORT_FIXTURES, rel)) == size
        d = docs[rel]
        assert d['day'] == manifest['day'] and d['loops'] == manifest['loops']
        assert rel == export.TIER2_PATHS[d['kind']].format(day=d['day'], vhash=d['vhash'])
    series = next(d for d in docs.values() if d['kind'] == 'series')
    assert series['vhash'] == contracts.BASE_VHASH == manifest['base_vhash']
    assert len(export.embed_json(series)) / series['loops'] <= 100
    if 'pred' in kinds:
        pred = next(d for d in docs.values() if d['kind'] == 'pred')
        text = next(d for d in docs.values() if d['kind'] == 'pred_text')
        assert pred['flags'] == [] and series['flags'] == []
        assert len(export.embed_json(pred)) / pred['loops'] <= 1000     # ~1.2 MB per full day-variant
        assert sum(1 for s in series['smb_off'] if s) >= 1 and all(l is not None for l in pred['levels'])
        assert all(r for r in pred['reason']) and all(c for c in text['console'])
        lines, boxes = export.expand_text(text, 0)
        assert any('final ISF factor' in l for l in lines) and boxes[0][2] == '55'
        pred_orig = next(d for d in docs.values() if d['kind'] == 'pred_orig')
        assert all(r for r in pred_orig['reason']) and all(a for a in pred_orig['iob'])
