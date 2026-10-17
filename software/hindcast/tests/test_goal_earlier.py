"""
test_goal_earlier.py  -  docs/hindcast/GOAL.md on the decision level: the manual-bolus coverage (metrics.py,
replay.manual_boluses, round.json effect, the card line) and question 'earlier' end to end (review order = Pareto
front first then the scalar, the validate top-N and its direction test, the earlier wording of round.json and the
cards, the weights-sensitivity numbers in round.json). Offline; a synthetic study where explore and earlier order
the same variants differently.
"""
import json
import os

import pytest

from hindcast import cards, lint, replay
from hindcast import contracts as c
from hindcast import metrics as M
from hindcast import round as rnd
from hindcast.tests import test_round as tr

S = ['2026-09-01', '2026-09-02', '2026-09-03']
H = ['2026-09-05', '2026-09-06']
MIN = 60.0


def lint_clean(text, kind='line'):
    findings = lint.lint_findings(text, kind)
    assert not findings, [str(f) for f in findings]


# ----------------------------------------------------------------------------- manual-bolus coverage (metrics)

def test_coverage_counts_the_smb_of_the_hour_before_against_base():
    t = [i * 5 * MIN for i in range(36)]                                 # 3 h of 5-min loops
    var = [0.0] * 36
    ref = [0.0] * 36
    var[10], var[11], ref[11] = 0.4, 0.6, 0.2                            # 50 and 55 min
    var[1] = 5.0                                                         # 5 min: more than 60 min before the bolus
    cov, detail = M.manual_bolus_coverage(t, var, ref, [(70 * MIN, 1.5)])
    assert cov['manual_boluses'] == 1 and cov['manual_U'] == 1.5
    assert cov['manual_presmb_U'] == pytest.approx(1.0) and cov['manual_presmb_base_U'] == pytest.approx(0.2)
    assert cov['coverage_U_vs_base'] == pytest.approx(0.8)
    assert cov['boluses_preempted'] == 1                                 # 0.8 >= 0.5 x 1.5
    assert detail[0]['covered_pct'] == pytest.approx(53.3, abs=0.1)
    # a loop at the bolus time itself is not "before"; covered_pct is capped at 100
    var[14] = 9.0                                                        # 70 min = the bolus
    cov, detail = M.manual_bolus_coverage(t, var, ref, [(70 * MIN, 0.5)])
    assert cov['manual_presmb_U'] == pytest.approx(1.0) and detail[0]['covered_pct'] == 100.0


def test_close_boluses_never_share_an_smb():
    t = [i * 5 * MIN for i in range(36)]
    var = [0.0] * 36
    var[8] = 1.0                                                         # 40 min
    cov, detail = M.manual_bolus_coverage(t, var, [0.0] * 36, [(45 * MIN, 1.0), (60 * MIN, 1.0)])
    assert [d['presmb_U'] for d in detail] == [1.0, 0.0]                 # the later window starts at the first bolus
    assert cov['manual_presmb_U'] == pytest.approx(1.0) and cov['boluses_preempted'] == 1


def test_no_bolus_gives_zeros_and_unknown_leaves_the_keys_out():
    t = [i * 5 * MIN for i in range(12)]
    s = M.LoopSeries.from_arrays(t, [120.0] * 12, smb_emul=[0.1] * 12, target_emul=[100.0] * 12)
    m = M.trial_metrics(None, series=s, manual_boluses=[])
    assert all(m[k] == 0 for k in M.MANUAL_KEYS)
    m = M.trial_metrics(None, series=s)
    assert not any(k in m for k in M.MANUAL_KEYS)
    # base is compared with itself: the coverage of the base replay is 0 by definition
    m = M.trial_metrics(None, series=s, manual_boluses=[(30 * MIN, 2.0)])
    assert m['manual_boluses'] == 1 and m['manual_U'] == 2.0 and m['coverage_U_vs_base'] == 0.0
    assert m['manual_presmb_U'] == m['manual_presmb_base_U'] == pytest.approx(0.6)


def test_trial_metrics_coverage_vs_base_series():
    t = [i * 5 * MIN for i in range(24)]
    base = M.LoopSeries.from_arrays(t, [150.0] * 24, smb_emul=[0.0] * 24, target_emul=[100.0] * 24)
    smb = [0.0] * 24
    smb[20], smb[21] = 0.5, 0.5
    var = M.LoopSeries.from_arrays(t, [150.0] * 24, smb_emul=smb, target_emul=[100.0] * 24)
    m = M.trial_metrics(None, base=base, series=var, manual_boluses=[(110 * MIN, 1.5), (115 * MIN + 1, 0.0)])
    assert m['manual_boluses'] == 1                                      # a 0 U entry is no bolus
    assert m['coverage_U_vs_base'] == pytest.approx(1.0) and m['boluses_preempted'] == 1


# ----------------------------------------------------------------------------- manual boluses from the cache

def _cache(tmp_path, day, recs):
    d = tmp_path / 'treatments'
    d.mkdir(parents=True, exist_ok=True)
    (d / f'{day}.json').write_text(json.dumps(recs), encoding='utf-8')
    return str(tmp_path)


def test_manual_boluses_reads_only_boluses_the_user_entered(tmp_path):
    day = '2026-09-06'
    recs = [
        {'type': 'NORMAL', 'eventType': 'Meal Bolus', 'insulin': 1.5, 'created_at': '2026-09-06T07:00:00.000Z'},
        {'type': 'NORMAL', 'eventType': 'Meal Bolus', 'insulin': 1.5, 'created_at': '2026-09-06T07:00:00.000Z'},  # dup
        {'type': 'NORMAL', 'eventType': 'Correction Bolus', 'insulin': 0.5, 'created_at': '2026-09-06T12:00:00Z'},
        {'type': 'SMB', 'eventType': 'Correction Bolus', 'insulin': 0.3, 'created_at': '2026-09-06T07:10:00Z'},
        {'type': 'NORMAL', 'eventType': 'Temp Basal', 'rate': 0.5, 'insulin': None, 'created_at': '2026-09-06T08:00:00Z'},
        {'type': 'NORMAL', 'eventType': 'Meal Bolus', 'insulin': 2.0, 'isValid': False, 'created_at': '2026-09-06T09:00:00Z'},
        {'type': 'NORMAL', 'eventType': 'Meal Bolus', 'insulin': 3.0, 'created_at': '2026-09-05T23:00:00Z'},  # other day
    ]
    root = _cache(tmp_path, day, recs)
    got = replay.manual_boluses(day, cache_root=root)
    assert [u for _, u in got] == [1.5, 0.5]
    assert got[0][0] == pytest.approx(replay.parse_iso('2026-09-06T07:00:00Z') / 1000.0)
    got = replay.manual_boluses(day, ('2026-09-06T06:00:00Z', '2026-09-06T09:00:00Z'), cache_root=root)
    assert [u for _, u in got] == [1.5]
    assert replay.manual_boluses('2026-09-07', cache_root=root) is None     # no day file: unknown, not "none"


def test_manual_boluses_on_the_real_cache_when_present():
    """The real cache (offline, read-only) when it is there: 2026-09-06 carries two Meal Boluses of 6 U together."""
    try:
        got = replay.manual_boluses('2026-09-06', settings=replay._load_settings(c.NS_SETTINGS_PATH))
    except Exception as e:                                               # pragma: no cover
        pytest.skip(f'no settings: {e}')
    if got is None:
        pytest.skip('the Nightscout cache of 2026-09-06 is not on this machine')
    assert len(got) == 2 and sum(u for _, u in got) == pytest.approx(6.0)


# ----------------------------------------------------------------------------- earlier vs explore (metrics)

# floors (test_round.mk base): rise 0.2 U, rest 0.05 U
SYN = {
    'calm': dict(rise=0.0, rest=-0.5),               # explore 10 x floor (rest); earlier dominated by 'up'
    'up': dict(rise=0.8, rest=0.0),                  # explore 4.0; earlier front, scalar 0.80
    'up_rest': dict(rise=1.2, rest=0.3),             # explore 6.0; earlier front, scalar 0.60
    'down': dict(rise=-1.0, rest=0.0),               # explore 5.0; earlier dominated, scalar -1.00
    'up_low': dict(rise=0.95, rest=0.0, lowside=2),  # explore 4.75; earlier front, scalar 0.35
}
MANUAL = {'manual_boluses': 2, 'manual_U': 3.0, 'manual_presmb_base_U': 1.0}


def syn_rows():
    rows = [{'variant': 'base', 'day': d, 'metrics': tr.mk(base=True, **MANUAL, manual_presmb_U=1.0,
                                                           coverage_U_vs_base=0.0, boluses_preempted=0), 'params': {}}
            for d in S + H]
    for name, kw in SYN.items():
        cov = max(kw['rise'], 0.0) * 0.75
        for d in S + H:
            rows.append({'variant': name, 'day': d, 'params': {'profile x': name},
                         'metrics': tr.mk(**kw, **MANUAL, manual_presmb_U=1.0 + cov, coverage_U_vs_base=cov,
                                          boluses_preempted=int(cov >= 0.75))})
    return rows


def test_explore_and_earlier_order_differently():
    ex = M.rank_variants(syn_rows(), 'explore', None, S)
    ea = M.rank_variants(syn_rows(), 'earlier', None, S)
    assert ex.order == ['calm', 'up_rest', 'down', 'up_low', 'up']
    assert set(ea.front) == {'up', 'up_rest', 'up_low'}
    assert ea.order == ['up', 'up_rest', 'up_low', 'calm', 'down']     # front by scalar, then the rest by scalar
    assert [ea.variants[n].scalar for n in ea.order] == pytest.approx([0.8, 0.6, 0.35, 0.0, -1.0])
    assert ea.variants['up'].mean['coverage_U_vs_base'] == pytest.approx(0.6)     # the aggregate means carry it
    assert ea.variants['up'].mean['manual_boluses'] == 2
    rows = M.leaderboard_rows(ea)
    assert all(k in rows[0]['mean'] for k in M.MANUAL_KEYS)


def test_validate_top_is_the_front_then_the_scalar():
    ea = M.rank_variants(syn_rows(), 'earlier', None, S)
    assert M.validate_top(ea, 2) == ['up', 'up_rest']                   # a front larger than N keeps its N best scalars
    assert M.validate_top(ea, 4) == ['up', 'up_rest', 'up_low', 'calm']
    ex = M.rank_variants(syn_rows(), 'explore', None, S)
    assert M.validate_top(ex, 2) == ['calm', 'up_rest']
    assert M.validate_variants(syn_rows(), 'earlier', None, S, H, top=4).validated == M.validate_top(ea, 4)


def test_the_direction_of_earlier_is_more_on_rises():
    ex = M.validate_variants(syn_rows(), 'explore', None, S, H, top=5)
    ea = M.validate_variants(syn_rows(), 'earlier', None, S, H, top=5)
    # 'down' asks for less on rises on every day: explore follows its own sign and holds, earlier asks rise > 0
    assert ex.per_variant['down']['component'] == 'rise_vs_base_U' and ex.per_variant['down']['holds'] == 2
    assert ea.per_variant['down']['direction'] == 1 and ea.per_variant['down']['holds'] == 0
    assert ea.per_variant['calm']['holds'] == 0                         # rise 0: no direction at all
    assert ea.per_variant['up']['holds'] == 2 and ea.per_variant['up']['direction'] == 1
    assert any('direction holds on 0 of 2' in r for r in ea.per_variant['down']['reasons'])


def test_recommend_for_earlier_needs_more_on_rises():
    rows = syn_rows()
    r = M.rank_variants(rows, 'earlier', None, S)
    folds = M.leave_one_day_out(rows, 'earlier', None, S)
    M.recommend(r, folds, ['down'], None)
    assert any('same sign of rise_vs_base_U on 0 %' in x for x in r.variants['down'].recommend_reasons)


def test_weights_sensitivity_moves_the_scalar_order():
    ea = M.rank_variants(syn_rows(), 'earlier', None, S)
    sens = M.weights_sensitivity(ea)
    assert sens['weights'] == {'rest': 2.0, 'lowside': 0.3} and sens['factors'] == [0.5, 1.5]
    assert sens['order'] == ['up', 'up_rest', 'up_low', 'calm', 'down']
    assert set(sens['by_weight']) == {'rest', 'lowside'} and set(sens['by_weight']['rest']) == {'0.5', '1.5'}
    # rest x 0.5: up_rest 1.2 - 0.3 = 0.9 passes up 0.8
    assert sens['by_weight']['rest']['0.5']['order'][:2] == ['up_rest', 'up']
    assert sens['by_weight']['rest']['0.5']['same_top'] is False
    # lowside x 0.5: up_low 0.95 - 0.3 = 0.65 passes up_rest 0.6
    assert sens['by_weight']['lowside']['0.5']['order'][:3] == ['up', 'up_low', 'up_rest']
    assert sens['by_weight']['lowside']['1.5']['same_top'] is True
    assert M.weights_sensitivity(M.rank_variants(syn_rows(), 'explore', None, S)) is None
    assert set(M.weights_sensitivity(M.rank_variants(syn_rows(), 'gentler', None, S))['by_weight']) == {'rest', 'zero_temp'}


def test_rule_text_states_the_earlier_validate_rule():
    text = M.ranking_rule_text('earlier', None, None, validate=True)
    assert 'the Pareto front first, then the scalar' in text and 'more insulin than base in rising loops' in text
    lint_clean(text)


# ----------------------------------------------------------------------------- round.json and the card

EARLIER_VARIANTS = {
    'up': ({'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5},
           dict(rise=0.8, rest=0.0, **MANUAL, manual_presmb_U=1.6, coverage_U_vs_base=0.6, boluses_preempted=1)),
    'up_rest': ({'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.6},
                dict(rise=1.2, rest=0.3, **MANUAL, manual_presmb_U=1.9, coverage_U_vs_base=0.9, boluses_preempted=1)),
    'up_low': ({'profile autoISF_max': 1.4, 'profile dura_ISF_weight': 0.5},
               dict(rise=0.95, rest=0.0, lowside=2, **MANUAL, manual_presmb_U=1.7, coverage_U_vs_base=0.7,
                    boluses_preempted=1)),
    'calm': ({'profile autoISF_max': 1.4, 'profile dura_ISF_weight': 0.6},
             dict(rise=0.0, rest=-0.5, **MANUAL, manual_presmb_U=1.0, coverage_U_vs_base=0.0, boluses_preempted=0)),
}


@pytest.fixture
def earlier_study(tmp_path):
    sd, study = tr.make_study_with_rows(tmp_path, variants=EARLIER_VARIANTS, holdout=['2026-09-05'], question='earlier')
    return sd, study, rnd.analyze(study, sd.trials(), tr.SETTINGS)


def vhash_of(name):
    return c.vhash(EARLIER_VARIANTS[name][0])


def test_round_json_carries_the_earlier_order_coverage_and_sensitivity(earlier_study):
    sd, study, a = earlier_study
    assert a.order[:3] == [vhash_of('up'), vhash_of('up_rest'), vhash_of('up_low')]
    doc = rnd.build_round(sd, round_no=1, phase='lhs', settings=tr.SETTINGS, analysis=a)
    by = {cand.variant: cand for cand in doc.variants}
    up = next(cand for cand in doc.variants if cand.vhash == vhash_of('up'))
    assert up.effect['coverage_U_vs_base'] == pytest.approx(0.6) and up.effect['manual_boluses'] == 2
    assert up.effect['manual_U'] == 3.0 and up.effect['boluses_preempted'] == 1
    assert list(up.effect)[:4] == ['rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U', 'changed_vs_base']
    sens = doc.sensitivity
    assert sens['order'][0] == vhash_of('up') and sens['by_weight']['rest']['0.5']['order'][0] == vhash_of('up_rest')
    rnd.write_round(sd, doc)
    with open(sd.round_path, encoding='utf-8') as fh:
        back = c.Round.from_dict(json.load(fh))
    assert back.sensitivity == sens
    for cand in by.values():
        lint_clean(cand.why)


def test_earlier_why_wording(earlier_study):
    _sd, _study, a = earlier_study
    rep = a.reports[vhash_of('up_low')]
    assert rnd.earlier_why(rep).startswith('more insulin asked on rises +0.95 U/day (4.7 x floor); extra elsewhere 0.00 U/day; '
                              'low-side flags 2.0/day; on the Pareto front, scalar +0.35')
    calm = a.reports[vhash_of('calm')]
    assert calm.verdict == 'candidate' and calm.why.startswith('more insulin asked on rises +0.00 U/day')
    assert 'behind the Pareto front' in calm.why
    assert any('same sign of rise_vs_base_U on 0 %' in r for r in calm.recommend_reasons)
    lint_clean(rnd.earlier_why(rep))
    lint_clean(calm.why)


def test_explore_round_has_no_sensitivity(tmp_path):
    sd, study = tr.make_study_with_rows(tmp_path, variants=EARLIER_VARIANTS, holdout=[], question='explore')
    a = rnd.analyze(study, sd.trials(), tr.SETTINGS)
    assert a.sensitivity is None
    assert rnd.build_round(sd, round_no=1, phase='lhs', settings=tr.SETTINGS, analysis=a).sensitivity is None


def test_earlier_card_has_the_pareto_weights_and_manual_bolus_lines(earlier_study):
    _sd, _study, a = earlier_study
    rep = a.reports[vhash_of('up')]
    text = cards.card_text(rep, a, tr.SETTINGS)
    lines = text.splitlines()
    pareto = next(l for l in lines if l.startswith('- Pareto position:'))
    assert pareto.startswith('- Pareto position: on the Pareto front (3 of 4 feasible variants); more insulin asked on '
                             'rises +0.80 U/day, extra elsewhere 0.00 U/day, low-side flags 0.0/day; scalar +0.80, '
                             'position 1 of 4 by the scalar.')
    weights = next(l for l in lines if l.startswith('- Weights'))
    assert 'rest 2, lowside 0.3' in weights and 'in the scalar top 5 in 4 of 4 settings' in weights
    manual = next(l for l in lines if l.startswith('- Manual boluses:'))
    assert manual.startswith('- Manual boluses: on these days you gave 6 boluses, 9.00 U; under this variant the loop '
                             'would already have delivered 4.80 U of them as SMBs in the hour before (base: 3.00 U), '
                             '+1.80 U vs base; 3 of 6 with at least 50 % of the bolus asked for earlier than base.')
    assert manual.endswith(cards.MANUAL_LIMIT)
    assert '(the Pareto front first, then the scalar)' in text
    cards.lint_card(text)


def test_card_without_coverage_keys_has_no_manual_line(tmp_path):
    sd, study = tr.make_study_with_rows(tmp_path, variants={'A': tr.VARIANTS['A']}, holdout=[])
    a = rnd.analyze(study, sd.trials(), tr.SETTINGS)
    text = cards.card_text(a.reports[tr.vh('A')], a, tr.SETTINGS)
    assert '- Manual boluses' not in text and '- Pareto position' not in text and '- Weights' not in text


def test_manual_line_with_no_bolus_and_partial_days():
    class R:
        days = ['d1', 'd2']
        per_day = {'d1': {k: 0 for k in M.MANUAL_KEYS}, 'd2': {}}
    line = cards.manual_bolus_line(R(), R.days)
    assert line == ('- Manual boluses: none on the 1 replayed search day (known on 1 of 2 search days: the treatments '
                    'of the others were not read).')
    lint_clean(line)
