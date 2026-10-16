"""
test_quarantine.py  -  day quarantine of days.py (backlog H-204)

Constructed pieces for the four rules and the thresholds from study.json; the real-cache checks read the frozen
metas and ns_settings.json and skip without them. Every reason string must pass hindcast.lint ('line' and 'issue').
"""
import pytest

from hindcast import contracts, days, freeze, lint

SETTINGS = {'profile_history': {'changes': [
    {'when': '2026-09-07T20:23:00+02:00', 'parameter': 'autoISF_max', 'from': 1.3, 'to': 1.5},
    {'when': '2026-09-04T21:20:00+02:00', 'parameter': 'profile ISF (mmol/U)', 'from': 4.9, 'to': 5.1},
]}}


def make_study(days_=(), space=None, objective=None):
    kw = {} if objective is None else {'objective': objective}
    return contracts.Study(name='q', created='2026-10-17T00:00:00Z', source='ns:', host='h', settings_sha='0' * 40,
                           code_sha='0' * 40, days=list(days_) or [contracts.StudyDay('2026-09-06')],
                           space=space or {'profile autoISF_max': contracts.SpaceParam('float', 1.1, 1.8, 0.05)}, **kw)


def _lint_clean(reasons):
    for r in reasons:
        lint.lint_text(r, kind='line')
        lint.lint_text(r, kind='issue')


def test_clean_day_has_no_reasons():
    assert days.quarantine_reasons('2026-09-06', settings=SETTINGS, searched_keys=['profile autoISF_max'],
                                   base_metrics={'agreement_pct': 96.1}, meta={'character': {'cgm_gap_min': 71}},
                                   loop_counts={'a': 1311, 'b': 1311}) == []


def test_each_rule_alone():
    low = days.quarantine_reasons('2026-09-06', base_metrics={'agreement_pct': 93.9})
    assert len(low) == 1 and 'base agreement 93.9 %' in low[0] and '94' in low[0]
    assert days.quarantine_reasons('2026-09-06', base_metrics={'agreement_pct': 94.0}) == []
    gap = days.quarantine_reasons('2026-09-03', meta={'character': {'cgm_gap_min': 139}})
    assert gap == ['CGM gap 139 min (threshold 120 min)']
    assert days.quarantine_reasons('2026-09-03', meta={'character': {'cgm_gap_min': 120}}) == []
    hist = days.quarantine_reasons('2026-09-07', settings=SETTINGS, searched_keys=['profile autoISF_max'])
    assert len(hist) == 1 and 'autoISF_max changed 1.3->1.5 inside the day' in hist[0]
    loops = days.quarantine_reasons('2026-09-06', loop_counts=[1311, 1311, 1310])
    assert len(loops) == 1 and 'loop counts differ' in loops[0]
    _lint_clean(low + gap + hist + loops)


def test_history_rule_needs_a_searched_key_inside_the_day():
    # the change is on 2026-09-07 local 20:23 = 18:23Z: inside that UTC day only
    assert days.quarantine_reasons('2026-09-06', settings=SETTINGS, searched_keys=['profile autoISF_max']) == []
    assert days.quarantine_reasons('2026-09-08', settings=SETTINGS, searched_keys=['profile autoISF_max']) == []
    assert days.quarantine_reasons('2026-09-07', settings=SETTINGS, searched_keys=['profile dura_ISF_weight']) == []
    # 'profile ISF (mmol/U)' is not a searchable key (sens is locked): never a hit for a searched item
    assert days.history_hits(SETTINGS, '2026-09-04', ['profile autoISF_max', 'profile dura_ISF_weight']) == []
    assert days.searched_items(['profile autoISF_max', 'dura_ISF_weight']) == {'autoISF_max', 'dura_ISF_weight'}


def test_cgm_gap_from_raw_entries():
    start, _ = days.day_bounds('2026-09-06')
    five = 5 * 60 * 1000
    gap_at, gap = 100, 150 * 60 * 1000                         # one 150 min hole, readings every 5 min otherwise
    entries = [{'date': start + i * five, 'sgv': 120} for i in range(gap_at)]
    t = start + (gap_at - 1) * five + gap
    while t < start + 86400000:
        entries.append({'date': t, 'sgv': 120})
        t += five
    reasons = days.quarantine_reasons('2026-09-06', entries=entries)
    assert len(reasons) == 1 and reasons[0].startswith('CGM gap 150 min')


def test_thresholds_from_study_json_and_overrides():
    assert days.quarantine_thresholds() == days.QUARANTINE_THRESHOLDS
    obj = contracts.Objective(quarantine={'agreement_pct_min': 90.0, 'cgm_gap_max_min': 180})
    study = make_study(objective=obj)
    thr = days.quarantine_thresholds(study)
    assert thr == {'agreement_pct_min': 90.0, 'cgm_gap_max_min': 180.0}
    assert days.quarantine_thresholds({'objective': {'quarantine': {'agreement_pct_min': 80, 'junk': 1}}})[
        'agreement_pct_min'] == 80.0
    assert days.quarantine_thresholds(None, {'cgm_gap_max_min': 'x'}) == days.QUARANTINE_THRESHOLDS
    assert days.quarantine_reasons('2026-09-06', base_metrics={'agreement_pct': 92}, thresholds=thr) == []


class Row:
    def __init__(self, day, vhash, loops, agreement=None, params=None, control=None, status='done', key=None):
        self.day, self.vhash, self.status, self.params, self.control = day, vhash, status, params or {}, control
        self.key = key or f'{vhash}|{day}'
        self.metrics = {'loops': loops}
        if agreement is not None:
            self.metrics['agreement_pct'] = agreement


def _study():
    days_ = [contracts.StudyDay(day='2026-09-05', role='holdout'), contracts.StudyDay(day='2026-09-06'),
             contracts.StudyDay(day='2026-09-07')]
    return make_study(days_)


def test_quarantine_from_trials_apply_and_listing():
    study = _study()
    rows = [Row('2026-09-05', 'b', 1098, agreement=91.0, control='base'), Row('2026-09-05', 'v', 1098, params={'x': 1}),
            Row('2026-09-06', 'b', 1311, agreement=96.0, control='base'), Row('2026-09-06', 'v', 1311, params={'x': 1}),
            Row('2026-09-06', 'f', 0, status='failed'),
            Row('2026-09-07', 'b', 1200, agreement=97.0), Row('2026-09-07', 'v', 1199, params={'x': 1})]
    metas = {'2026-09-06': {'character': {'cgm_gap_min': 71}}}
    reasons = days.quarantine_from_trials(study, rows, metas=metas, settings=SETTINGS)
    assert reasons['2026-09-06'] == []
    assert len(reasons['2026-09-05']) == 1 and 'base agreement 91.0 %' in reasons['2026-09-05'][0]
    assert len(reasons['2026-09-07']) == 2                       # history change + loop counts
    _lint_clean([r for rs in reasons.values() for r in rs])

    changed = days.apply_quarantine(study, reasons)
    assert [d for d, _ in changed] == ['2026-09-05', '2026-09-07']
    roles = {d.day: d.role for d in study.days}
    assert roles == {'2026-09-05': 'quarantined', '2026-09-06': 'search', '2026-09-07': 'quarantined'}
    assert study.days[0].quarantine_reason.startswith('was holdout: base agreement')
    assert days.scoring_days(study) == ['2026-09-06']
    assert days.apply_quarantine(study, reasons) == []          # idempotent
    assert study.days[0].quarantine_reason.startswith('was holdout: ')
    contracts.Study.from_json(study.to_json())                   # still a valid study.json

    listing = days.quarantine_listing(reasons)
    assert listing[0].startswith('quarantined days (2;') and any('2026-09-07' in l for l in listing)
    assert days.quarantine_listing({'2026-09-06': []})[0] == 'quarantined days: none'
    for line in listing:
        lint.lint_text(line, kind='line')

    del reasons['2026-09-05']
    assert days.apply_quarantine(study, reasons, restore=True) == [('2026-09-05', [])]
    assert study.days[0].role == 'holdout' and study.days[0].quarantine_reason is None


def _metas(day_list):
    try:
        settings = days.load_settings()
        host = days.host_of('ns:', settings)
    except Exception:
        return None, None
    metas = {d: freeze.read_meta(d, host) for d in day_list}
    if any(m is None for m in metas.values()):
        return None, None
    return settings, metas


def test_real_frozen_days_and_profile_history():
    """2026-09-03 has a CGM gap over two hours, 2026-09-06 does not, 2026-09-07 holds the autoISF_max change."""
    settings, metas = _metas(['2026-09-03', '2026-09-06', '2026-09-07'])
    if metas is None:
        pytest.skip('frozen metas of 2026-09-03/06/07 or ns_settings.json missing')
    searched = ['profile autoISF_max']
    got = {d: days.quarantine_reasons(d, settings=settings, searched_keys=searched, meta=metas[d]) for d in metas}
    assert got['2026-09-06'] == []
    assert any(r.startswith('CGM gap') for r in got['2026-09-03'])
    assert any('autoISF_max' in r for r in got['2026-09-07'])
    _lint_clean([r for rs in got.values() for r in rs])


def test_quarantine_from_trials_uses_latest_row_per_key():
    """A done row superseded by a later 'stale' row of the same key (--on-stale reeval) no longer counts."""
    study = make_study([contracts.StudyDay('2026-09-06')])
    rows = [Row('2026-09-06', 'b', 1311, agreement=90.0, control='base'),
            Row('2026-09-06', 'b', 1311, agreement=90.0, control='base', status='stale'),
            Row('2026-09-06', 'v', 1311, params={'x': 1}),
            Row('2026-09-06', 'w', 1290, params={'x': 2}),
            Row('2026-09-06', 'w', 1290, params={'x': 2}, status='stale')]
    assert days.quarantine_from_trials(study, rows, settings=SETTINGS) == {'2026-09-06': []}
    # the reverse order (stale first, re-evaluated done row last) counts again
    rows = [rows[1], rows[0], rows[2], rows[4], rows[3]]
    reasons = days.quarantine_from_trials(study, rows, settings=SETTINGS)['2026-09-06']
    assert len(reasons) == 2 and 'base agreement 90.0 %' in reasons[0] and 'loop counts differ' in reasons[1]


def test_quarantine_from_trials_real_trial_rows():
    study = make_study([contracts.StudyDay('2026-09-06')])
    k = contracts.key({}, '2026-09-06', '0' * 40, '0' * 40, 'f' * 40, 1)
    base = dict(key=k, trial=1, phase='a', vhash=contracts.BASE_VHASH, variant='base', params={}, day='2026-09-06',
                control='base', settings_sha='0' * 40, code_sha='0' * 40, freeze_sha='f' * 40)
    done = contracts.TrialRow(status='done', metrics={'loops': 1311, 'agreement_pct': 90.0}, **base)
    stale = contracts.TrialRow(status='stale', **base)
    assert days.quarantine_from_trials(study, [done, stale], settings=SETTINGS) == {'2026-09-06': []}
    assert len(days.quarantine_from_trials(study, [stale, done], settings=SETTINGS)['2026-09-06']) == 1
