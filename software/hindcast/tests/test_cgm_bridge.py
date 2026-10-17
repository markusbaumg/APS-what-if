"""
test_cgm_bridge.py  -  the CGM bridge from devicestatus (backlog H-714): ns_glucose bridged readings, the loop flag,
the days.py character / badge / quarantine rule, metrics.bridged_agreement and the frozen-file counters

Constructed data only (no cache needed): a 1-minute CGM series with a stretch removed while the "phone" kept
looping and uploading suggested.bg.
"""
import gzip
import json
import math

from ns_client import to_iso
import ns_glucose
import ns_source
from hindcast import days, freeze, lint, metrics as hm

MIN = 60 * 1000
H = 60 * MIN
DAY = '2026-09-06'
START = 1788652800000                       # 2026-09-06T00:00:00Z


def bg_at(ms):
    return 120.0 + 30.0 * math.sin((ms - START) / (90.0 * MIN))


def entries(skip=None):
    out = []
    for i in range(0, 10 * 60):                                   # 1-min readings 00:00 .. 10:00
        ms = START + i * MIN + 5000
        if skip and skip[0] <= ms < skip[1]:
            continue
        out.append({'type': 'sgv', 'date': ms, 'sgv': round(bg_at(ms))})
    return out


def devicestatus(a_ms, b_ms, step=MIN):
    out = []
    t = a_ms
    while t < b_ms:
        out.append({'openaps': {'suggested': {'deliverAt': to_iso(t + 20000), 'bg': bg_at(t + 20000),
                                              'reason': 'x', 'predBGs': {}}}})
        t += step
    return out


def test_no_bridge_without_a_gap():
    gh = ns_glucose.GlucoseHistory(entries(), devicestatus(START + 4 * H, START + 5 * H), {})
    assert gh.bridged_ts == set() and gh.br is None
    gs = gh.status_at(START + 4 * H + 30 * MIN + 20000, devicestatus(START + 4 * H + 30 * MIN, START + 4 * H + 31 * MIN)[0])
    assert gs is not None and gh.last_bridged is False


def test_bridged_readings_fill_the_gap_and_flag_the_loops():
    gap = (START + 5 * H, START + 7 * H)
    ds = devicestatus(START + 4 * H, START + 8 * H)
    plain = ns_glucose.GlucoseHistory(entries(gap), ds, {'glucose': {'bridge': False}})
    gh = ns_glucose.GlucoseHistory(entries(gap), ds, {})
    inside = [r for r in ds if gap[0] + 15 * MIN < ns_source.loop_ms(r) < gap[1]]
    rec = inside[len(inside) // 2]
    now = ns_source.loop_ms(rec)
    assert plain.status_at(now, rec) is None                      # today: 'no CGM value within 12 min'
    gs = gh.status_at(now, rec)
    assert gs is not None and gh.last_bridged is True and gs['date'] == now
    assert gs['glucose'] == rec['openaps']['suggested']['bg']
    assert gs['dura_ISF_minutes'] >= 0 and abs(gs['delta']) < 10
    # the loops right after the gap's last real reading ran on it (anchor) and are not bridged; every later one is
    assert min(gh.bridged_ts) > gap[0] and max(gh.bridged_ts) < gap[1]
    assert gh.bridged_in(gap[0], gap[1]) == len(gh.bridged_ts)
    # a loop on real data after the join is not flagged
    after = [r for r in ds if ns_source.loop_ms(r) > gap[1] + 30 * MIN][0]
    assert gh.status_at(ns_source.loop_ms(after), after) is not None and gh.last_bridged is False


def test_kalman_does_not_learn_r_from_bridged_values():
    gap = (START + 5 * H, START + 7 * H)
    ds = devicestatus(START + 4 * H, START + 8 * H)
    gh = ns_glucose.GlucoseHistory(entries(gap), ds, {})
    data = gh.buckets_at(START + 6 * H + 20000)
    assert data and data[0].bridged
    assert any(b.bridged for b in data) and any(not b.bridged for b in data)
    assert not any(b.filled for b in data if b.bridged)           # loops <= 10 min apart: not 'filled' for dura_ISF


def make_loop(ms, bridged=False):
    loop = {'now_ms': ms, 'utcOffset': 2.0, 'console_error': [], 'autoISF_version': '3.2.1'}
    if bridged:
        loop['cgm_bridged'] = True
    return loop


class Src:
    def __init__(self, ents):
        self.entries = ents
        self.treatments = []
        self.skipped = {}


def _day_with_bridge():
    gap = (START + 2 * H, START + 4 * H + 30 * MIN)              # 150 min without entries
    ents = [{'date': START + i * MIN, 'sgv': 100} for i in range(24 * 60) if not gap[0] <= START + i * MIN < gap[1]]
    loops = [make_loop(START + i * 2 * MIN, bridged=gap[0] + 3 * MIN <= START + i * 2 * MIN < gap[1])
             for i in range(12 * 30)]
    return gap, ents, loops


def test_character_and_badge_of_a_bridged_stretch():
    gap, ents, loops = _day_with_bridge()
    src = Src(ents)
    ch = days.character(DAY, src, loops)
    assert ch['cgm_gap_min'] == 151                               # the raw gap (01:59 .. 04:30) stays what it was
    assert ch['cgm_bridged_loops'] == sum(1 for l in loops if l.get('cgm_bridged'))
    assert ch['cgm_bridged_stretches'][0][0] == gap[0] + 4 * MIN
    assert ch['cgm_bridged_min'] == ch['cgm_bridged_stretches'][0][2] == 144
    assert ch['cgm_gap_unbridged_min'] < 15
    out = days.confounders(DAY, {}, src, loops)
    assert 'CGM bridged from phone 144 min 04h04-06h28 local' in out
    assert not any(s.startswith('cgm gap 151') for s in out)
    for s in out:
        lint.lint_text(s, kind='line')
        lint.lint_text(s, kind='issue')
    # a day without bridged loops keeps its record shape
    plain = days.character(DAY, src, [make_loop(l['now_ms']) for l in loops])
    assert not any(k.startswith('cgm_bridged') or k == 'cgm_gap_unbridged_min' for k in plain)


def test_quarantine_counts_a_bridged_stretch_only_when_it_agrees():
    meta = {'character': {'cgm_gap_min': 150, 'cgm_gap_unbridged_min': 4}}
    good = days.quarantine_reasons(DAY, meta=meta, base_metrics={'agreement_pct': 96.0, 'agreement_bridged_pct': 96.5,
                                                                 'agreement_unbridged_pct': 96.0})
    assert good == []
    bad = days.quarantine_reasons(DAY, meta=meta, base_metrics={'agreement_pct': 95.0, 'agreement_bridged_pct': 47.9,
                                                                'agreement_unbridged_pct': 97.3})
    assert len(bad) == 1 and bad[0].startswith('CGM gap 150 min (threshold 120 min)') and '47.9 % vs 97.3 %' in bad[0]
    unknown = days.quarantine_reasons(DAY, meta=meta, base_metrics={'agreement_pct': 96.0})
    assert len(unknown) == 1 and 'not measured' in unknown[0]
    # an old meta (no bridge keys) and a residual gap above the threshold behave as before
    assert days.quarantine_reasons(DAY, meta={'character': {'cgm_gap_min': 150}}) == ['CGM gap 150 min (threshold 120 min)']
    rest = days.quarantine_reasons(DAY, meta={'character': {'cgm_gap_min': 300, 'cgm_gap_unbridged_min': 130}},
                                   base_metrics={'agreement_bridged_pct': 99.0, 'agreement_unbridged_pct': 96.0})
    assert rest == ['CGM gap 130 min (threshold 120 min)']
    for r in bad + unknown + rest:
        lint.lint_text(r, kind='line')
        lint.lint_text(r, kind='issue')


def test_bridged_agreement_split():
    s = hm.LoopSeries.from_arrays(t=[0, 60, 120, 180], bg=[100] * 4, smb_phone=[0, 0, 0.5, 0], smb_emul=[0, 0, 0, 0])
    assert hm.bridged_agreement(s, None) == {} and hm.bridged_agreement(s, [False] * 4) == {}
    got = hm.bridged_agreement(s, [False, False, True, True])
    assert got == {'bridged_loops': 2, 'agreement_bridged_pct': 50.0, 'agreement_unbridged_pct': 100.0}
    m = hm.trial_metrics(None, series=s, bridged=[False, False, True, True])
    assert m['agreement_bridged_pct'] == 50.0 and m['agreement_pct'] == 75.0
    assert 'agreement_bridged_pct' not in hm.trial_metrics(None, series=s)


def test_frozen_source_prints_the_bridge_counter(tmp_path):
    host = 'h.example'
    d = tmp_path / host
    d.mkdir()
    a = START + 3 * H
    events = [make_loop(a), make_loop(a + MIN, bridged=True), {'dropped': 'no result in record', 'ms': a + 2 * MIN,
                                                              'cgm_bridged': True},
              make_loop(a + 3 * MIN, bridged=True), {'dropped': 'duplicate upload', 'ms': a + 3 * MIN}]
    with gzip.open(d / (DAY + '.loops.jsonl.gz'), 'wt') as fh:
        for ev in events:
            fh.write(json.dumps(ev) + '\n')
    (d / (DAY + '.meta.json')).write_text(json.dumps({'day': DAY, 'loops': 3}))           # an old meta: no 'bridged'
    lines = []
    src = freeze.FrozenSource('https://h.example', to_iso(START), to_iso(START + 24 * H),
                              settings={'nightscout': {'url': 'https://h.example'}}, log_msg=lines.append,
                              base_dir=str(tmp_path))
    got = list(src.loops())
    assert [l.get('cgm_bridged', False) for l in got] == [False, True, True]
    assert ns_source.bridged_line(3, 2) in lines
    assert ns_source.bridged_line(3, 2) == 'CGM readings bridged from devicestatus: 3 (2 loops)'
