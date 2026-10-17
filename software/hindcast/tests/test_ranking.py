"""
test_ranking.py  -  H-202: floors, ties, feasibility, Pareto front, review order, scalar, recommended_for_review,
ranking-rule text, on constructed trial sets (and once on the fixture CSVs when they are present)
"""
import json
import re

import pytest

from hindcast import metrics as M

DAY = '2026-09-06'
DAYS5 = ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-05']

MAY_NOT = [r'\bbetter\b', r'\bbest\b', r'\boptimal\b', r'\bsafer\b', r'fewer lows', r'fewer highs', r'better control',
           r'time[- ]in[- ]range', r'HbA1c', r'would have prevented', r'recommended setting(?!s? for review)']


def no_forbidden_words(text):
    low = text.lower()
    for pat in MAY_NOT:
        assert re.search(pat, low, re.IGNORECASE) is None, (pat, text)


def mk(rise=0.0, rest=0.0, loops=1000, P=0.0, env3=0.0, envd=0.0, cap=10.0, over=0, lowside=0, zt=0, changed=50,
       base=False, delta=None, smb=None, tbr=None, **extra):
    """a metrics dict as trial_metrics() writes it, with the fields the ranking reads"""
    m = {'loops': loops, 'rise_vs_base_U': rise, 'rest_vs_base_U': rest,
         'delta_vs_base_U': rise + rest if delta is None else delta,
         'smb_vs_base_U': rise + rest if smb is None else smb, 'tbr_vs_base_U': 0.0 if tbr is None else tbr,
         'changed_loops': changed, 'changed_vs_base': 0 if base else changed - 40,
         'prehypo_extra_U': P, 'prehypo_extra_vs_phone_U': 0.1 if base else 0.0,
         'env_3h_max_U': env3, 'env_day_pct_tdd': envd, 'cap_binding_pct': cap, 'smb_over_maxbolus_loops': over,
         'lowside_flags': lowside, 'zero_temp_vs_base': zt, 'max_single_smb_U': 0.3, 'is_base': base, 'flags': []}
    if base:
        # the floors: |base - phone| = 0.2 U on rise, 0.05 U on rest, 0.25 total, 40 loops
        m.update({'smb_emul_U': 4.2, 'smb_orig_U': 4.0, 'tbr_emul_U': 0.5, 'tbr_orig_U': 0.45,
                  'delta_U': 0.25, 'rise_emul_U': 3.2, 'rise_orig_U': 3.0, 'rest_emul_U': 1.5, 'rest_orig_U': 1.45,
                  'changed_loops': 40, 'agreement_pct': 96.0})
    m.update(extra)
    return m


def rows_one_day(variants, day=DAY):
    out = [{'variant': 'base', 'day': day, 'metrics': mk(base=True), 'params': {}}]
    for name, m in variants.items():
        out.append({'variant': name, 'day': day, 'metrics': m, 'params': {'profile x': name}})
    return out


# ----------------------------------------------------------------------------- floors and ties
def test_floors_from_base():
    f = M.floors_from_base(mk(base=True))
    assert f['floor_rise_U'] == pytest.approx(0.2) and f['floor_rest_U'] == pytest.approx(0.05)
    assert f['floor_total_U'] == pytest.approx(0.25) and f['floor_smb_U'] == pytest.approx(0.2)
    assert f['floor_tbr_U'] == pytest.approx(0.05) and f['floor_loops'] == 40 and f['floor_P_U'] == pytest.approx(0.1)
    assert f['agreement_pct'] == 96.0 and f['loops'] == 1000


def test_effective_floor_and_tie():
    assert M.effective_floor(0.0) == M.FLOOR_MIN_U and M.effective_floor(0.3) == 0.3
    assert M.effective_floor(0.0, 'floor_loops') == 1
    assert M.is_tie(0.39, 0.2) and not M.is_tie(0.4, 0.2)             # tie below 2 x floor
    assert M.is_tie(-0.39, 0.2)
    assert M.is_tie(0.09, 0.0) and not M.is_tie(0.11, 0.0)            # floor 0 widened to one increment


def test_variant_inside_floor_is_tie_with_base():
    floors = M.floors_from_base(mk(base=True))
    inside = mk(rise=0.3, rest=0.05, changed=45)                      # 0.3 < 0.4, 0.05 < 0.1, 5 < 80 loops
    assert M.tie_with_base(inside, floors)
    assert all(M.tie_components(inside, {k: 0.0 for k in M.FLOOR_COMPONENTS}, floors).values())
    outside = mk(rise=0.9, rest=0.0, changed=45)
    assert not M.tie_with_base(outside, floors)
    ties = M.tie_components(outside, inside, floors)
    assert ties['rise_vs_base_U'] is False and ties['rest_vs_base_U'] is True
    r = M.rank_variants(rows_one_day({'in': inside, 'out': outside}), 'explore')
    assert r.variants['in'].tie_with_base and not r.variants['out'].tie_with_base
    board = {b['variant']: b for b in M.leaderboard_rows(r)}
    assert board['in']['verdict'] == 'tie' and board['out']['verdict'] == 'candidate'
    assert r.order == ['out', 'in']                                   # review order: larger |effect|/floor first


def test_effect_over_floor_picks_largest_component():
    floors = M.floors_from_base(mk(base=True))
    assert M.effect_over_floor(mk(rise=0.4, rest=0.0), floors) == (pytest.approx(2.0), 'rise_vs_base_U')
    assert M.effect_over_floor(mk(rise=0.1, rest=-0.2), floors) == (pytest.approx(4.0), 'rest_vs_base_U')


# ----------------------------------------------------------------------------- feasibility
def test_feasibility_reasons():
    base = mk(base=True)
    floors = M.floors_from_base(base)
    assert M.feasibility_day(mk(), base, floors) == []
    assert 'before a recorded low' in M.feasibility_day(mk(P=0.2), base, floors)[0]        # limit = 0 + floor_P 0.1
    assert M.feasibility_day(mk(P=0.1), base, floors) == []                                 # at the limit: feasible
    assert 'envelope' in M.feasibility_day(mk(env3=1.2), base, floors)[0]
    assert 'envelope' in M.feasibility_day(mk(envd=16.0), base, floors)[0]
    assert 'maxBolus' in M.feasibility_day(mk(over=2), base, floors)[0]
    assert 'cap' in M.feasibility_day(mk(cap=50.0), base, floors)[0]
    assert M.feasibility_day(mk(cap=49.9), base, floors) == []
    assert 'loop count' in M.feasibility_day(mk(loops=999), base, floors)[0]
    assert 'failed' in M.feasibility_day({'loops': 0, 'error': 'SYNTAX'}, base, floors)[-1]
    # thresholds from study.json
    obj = {'envelope': {'max_3h_U': 2.0}, 'feasible': {'cap_binding_max_pct': 30}}
    assert M.feasibility_day(mk(env3=1.2), base, floors, obj) == []
    assert 'cap' in M.feasibility_day(mk(cap=35.0), base, floors, obj)[0]
    for reason in M.feasibility_day(mk(P=0.5, env3=3, envd=40, over=1, cap=90, loops=1), base, floors):
        no_forbidden_words(reason)


def test_out_of_envelope_never_in_front_or_order():
    variants = {'wild': mk(rise=3.0, rest=0.0, env3=2.5, envd=30),   # would dominate everything
                'mild': mk(rise=0.6, rest=0.0), 'calm': mk(rise=0.5, rest=0.1)}
    for q in ('earlier', 'gentler'):
        r = M.rank_variants(rows_one_day(variants), q)
        assert 'wild' not in r.front and 'wild' not in r.order and 'wild' in r.infeasible
        assert any('envelope' in x for x in r.variants['wild'].infeasible_reasons[DAY])
        assert r.variants['wild'].rank is None
    r = M.rank_variants(rows_one_day(variants), 'explore')
    assert 'wild' not in r.order and r.order == ['mild', 'calm']
    board = M.leaderboard_rows(r)
    assert board[-1]['variant'] == 'wild' and board[-1]['verdict'] == 'infeasible' and 'envelope' in board[-1]['why']


def test_missing_day_or_missing_base_is_infeasible():
    rows = rows_one_day({'a': mk(rise=0.5)}) + [{'variant': 'a', 'day': '2026-09-07', 'metrics': mk(rise=0.5)},
                                                 {'variant': 'b', 'day': '2026-09-07', 'metrics': mk(rise=0.5)}]
    r = M.rank_variants(rows, 'explore')
    assert r.warnings and 'no base trial on 2026-09-07' in r.warnings[0]
    assert not r.variants['a'].feasible and 'no base replay on this day' in r.variants['a'].infeasible_reasons['2026-09-07']
    assert 'not replayed on this day' in r.variants['b'].infeasible_reasons[DAY]
    assert r.variants['b'].missing_days == [DAY]
    rows.append({'variant': 'base', 'day': '2026-09-07', 'metrics': mk(base=True)})
    r = M.rank_variants(rows, 'explore')
    assert r.variants['a'].feasible and r.order == ['a']
    assert M.rank_variants(rows, 'explore', days=[DAY]).days == [DAY]  # restriction to the search days
    rows.append({'variant': 'c', 'day': DAY, 'metrics': mk(rise=9), 'status': 'failed'})
    assert 'c' not in M.rank_variants(rows, 'explore').variants      # failed rows are ignored


# ----------------------------------------------------------------------------- Pareto, scalar, review order
def test_pareto_front_and_dominance():
    pts = {'a': (1.0, 1.0, 0.0), 'b': (0.5, 2.0, 0.0), 'c': (1.0, 1.0, 1.0), 'd': (2.0, 2.0, 0.0), 'e': (1.0, 1.0, 0.0)}
    assert M.dominates((1, 1, 0), (1, 1, 1)) and not M.dominates((1, 1, 0), (1, 1, 0))
    assert M.pareto_front(pts) == ['a', 'b', 'e']                      # c dominated by a, d by a; equal points both stay


def test_pareto_objectives_and_scalar():
    agg = {'rise_vs_base_U': 0.5, 'rest_vs_base_U': -0.1, 'lowside_flags': 2, 'zero_temp_vs_base': -3}
    assert M.pareto_objectives(agg, 'earlier') == (-0.5, 0.0, 2.0)
    assert M.pareto_objectives(agg, 'gentler') == (0.5, 0.1, 0.0)
    assert M.scalar_objective(agg, 'earlier') == pytest.approx(0.5 - 2.0 * 0.0 - 0.3 * 2)
    agg2 = {'rise_vs_base_U': 0.5, 'rest_vs_base_U': 0.2, 'lowside_flags': 0, 'zero_temp_vs_base': 4}
    assert M.scalar_objective(agg2, 'earlier') == pytest.approx(0.5 - 2.0 * 0.2)
    assert M.scalar_objective(agg2, 'earlier', {'rest': 1.0}) == pytest.approx(0.5 - 0.2)
    assert M.scalar_objective(agg2, 'gentler') == pytest.approx(-0.5 - 2.0 * 0.2 - 0.3 * 4)
    with pytest.raises(ValueError):
        M.pareto_objectives(agg, 'explore')
    with pytest.raises(ValueError):
        M.scalar_objective(agg, 'explore')
    with pytest.raises(ValueError):
        M.rank_variants([], 'sooner')


def test_earlier_ranking_front_first_then_scalar():
    variants = {'more_rise': mk(rise=0.8, rest=0.0),                 # front
                'more_rise_rest': mk(rise=1.0, rest=0.3),            # front (more rise, more rest)
                'dominated': mk(rise=0.7, rest=0.1),                 # dominated by more_rise
                'low_side': mk(rise=0.8, rest=0.0, lowside=3)}       # dominated by more_rise
    r = M.rank_variants(rows_one_day(variants), 'earlier', {'scalar_weights': {'rest': 2.0, 'lowside': 0.3}})
    assert set(r.front) == {'more_rise', 'more_rise_rest'}
    assert r.order[:2] == ['more_rise', 'more_rise_rest']            # scalar 0.8 vs 1.0 - 0.6 = 0.4
    assert r.order[2:] == ['dominated', 'low_side']                  # 0.7 - 0.2 = 0.5 vs 0.8 - 0.9 = -0.1
    assert [r.variants[n].rank for n in r.order] == [1, 2, 3, 4]
    assert r.variants['more_rise'].pareto and not r.variants['dominated'].pareto
    assert '"earlier"' in r.rule_text and 'weights' in r.rule_text


def test_gentler_is_the_mirror():
    variants = {'less': mk(rise=-0.8, rest=0.0), 'less_zt': mk(rise=-0.9, rest=0.0, zt=5), 'more': mk(rise=0.5)}
    r = M.rank_variants(rows_one_day(variants), 'gentler')
    assert set(r.front) == {'less', 'less_zt'} and r.order[-1] == 'more'
    assert r.order[0] == 'less'                                       # 0.8 vs 0.9 - 1.5


def test_review_order_explore():
    variants = {'big': mk(rise=1.0, rest=0.0), 'rest': mk(rise=0.0, rest=0.3), 'dead': mk(rise=0.05, rest=0.01)}
    r = M.rank_variants(rows_one_day(variants), 'explore')
    assert r.order == ['rest', 'big', 'dead']                         # 0.3/0.05 = 6 > 1.0/0.2 = 5 > 0.25
    assert r.variants['rest'].review_component == 'rest_vs_base_U'
    assert r.front == [] and all(r.variants[n].scalar is None for n in r.order)
    assert r.rule_text.startswith('Review order, not a ranking by outcome.')
    assert r.variants['dead'].tie_with_base


def test_aggregate_days():
    agg = M.aggregate_days({'d1': mk(rise=0.5, rest=0.1), 'd2': mk(rise=0.3, rest=-0.1), 'd3': mk(rise=-0.1, rest=0.0)})
    assert agg['n_days'] == 3 and agg['days'] == ['d1', 'd2', 'd3']
    assert agg['mean']['rise_vs_base_U'] == pytest.approx(0.7 / 3)
    assert agg['min']['rise_vs_base_U'] == -0.1 and agg['max']['rise_vs_base_U'] == 0.5
    assert agg['sign_frac']['rise_vs_base_U'] == pytest.approx(2 / 3)
    assert agg['sign_frac']['rest_vs_base_U'] == pytest.approx(0.0)  # mean 0 but not all zero
    assert agg['sign_frac']['tbr_vs_base_U'] == 1.0                   # all zero
    assert 'flags' not in agg['mean'] and 'is_base' not in agg['mean']


# ----------------------------------------------------------------------------- multi-day: folds and recommendation
def multi_day_rows(signs_c=(1, 1, 1, 1, 1), days=DAYS5):
    rows = []
    for d, sc in zip(days, signs_c):
        rows.append({'variant': 'base', 'day': d, 'metrics': mk(base=True)})
        rows.append({'variant': 'A', 'day': d, 'metrics': mk(rise=1.0, rest=0.0)})
        rows.append({'variant': 'B', 'day': d, 'metrics': mk(rise=0.8, rest=0.0)})
        rows.append({'variant': 'C', 'day': d, 'metrics': mk(rise=0.6 * sc, rest=0.0)})
        rows.append({'variant': 'D', 'day': d, 'metrics': mk(rise=0.45, rest=0.0)})
    return rows


def test_leave_one_day_out_and_recommend():
    rows = multi_day_rows()
    r = M.rank_variants(rows, 'explore')
    assert r.order == ['A', 'B', 'C', 'D'] and r.days == DAYS5
    folds = M.leave_one_day_out(rows, 'explore')
    assert set(folds) == set(DAYS5) and all(order == ['A', 'B', 'C', 'D'] for order in folds.values())
    hold = M.rank_variants(rows, 'explore', days=[DAYS5[-1]])         # stand-in for a hold-out ranking
    rec = M.recommend(r, folds, hold.order)
    assert rec['A'] == (True, []) and rec['B'][0] and rec['C'][0]
    assert not rec['D'][0] and 'top-3' in rec['D'][1][0]
    assert r.variants['A'].recommended_for_review and not r.variants['D'].recommended_for_review
    board = {b['variant']: b for b in M.leaderboard_rows(r)}
    assert board['A']['verdict'] == 'recommended' and board['D']['verdict'] == 'candidate'
    # no hold-out days reserved: never recommended, the reason says why
    rec = M.recommend(r, folds, None)
    assert not rec['A'][0] and 'no hold-out days' in rec['A'][1]
    # not in the hold-out top 5
    assert not M.recommend(r, folds, ['X1', 'X2', 'X3', 'X4', 'X5'])['A'][0]
    assert M.leave_one_day_out(rows_one_day({'A': mk(rise=1)}), 'explore') == {}      # one day: no folds


def test_recommend_sign_stability():
    rows = multi_day_rows(signs_c=(1, 1, 1, 1, -1))                   # C flips sign on one of five days: 80 % -> ok
    r = M.rank_variants(rows, 'explore')
    folds = M.leave_one_day_out(rows, 'explore')
    rec = M.recommend(r, folds, r.order)
    assert rec['C'][0], rec['C']
    rows = multi_day_rows(signs_c=(1, 1, -1), days=DAYS5[:3])         # 67 %: not stable
    r = M.rank_variants(rows, 'explore')
    rec = M.recommend(r, M.leave_one_day_out(rows, 'explore'), r.order)
    assert not rec['C'][0] and any('same sign' in x for x in rec['C'][1])
    assert rec['A'][0]


def test_recommend_needs_feasible_on_all_days():
    rows = multi_day_rows()
    rows.append({'variant': 'A', 'day': '2026-09-06', 'metrics': mk(rise=1.0, P=0.5)})
    rows.append({'variant': 'base', 'day': '2026-09-06', 'metrics': mk(base=True)})
    for v in 'BCD':
        rows.append({'variant': v, 'day': '2026-09-06', 'metrics': mk(rise=0.5)})
    r = M.rank_variants(rows, 'explore')
    assert not r.variants['A'].feasible and 'A' in r.infeasible
    rec = M.recommend(r, M.leave_one_day_out(rows, 'explore'), r.order)
    assert not rec['A'][0] and 'not feasible on every day' in rec['A'][1]


# ----------------------------------------------------------------------------- the printed rule
def test_ranking_rule_text_vocabulary():
    for q in M.QUESTIONS:
        text = M.ranking_rule_text(q)
        no_forbidden_words(text)
        assert 'Feasible' in text and 'tie' in text and 'for review' in text and 'nothing here changes the phone' in text
    assert M.ranking_rule_text('explore').startswith('Review order, not a ranking by outcome.')
    assert 'weights' not in M.ranking_rule_text('explore').split('\n')[1]
    custom = M.ranking_rule_text('earlier', {'scalar_weights': {'rest': 1.5}, 'envelope': {'max_3h_U': 0.8}})
    assert '1.5 x max(rest, 0)' in custom and 'at most 0.8 U' in custom
    with_floors = M.ranking_rule_text('explore', None, {DAY: M.floors_from_base(mk(base=True))})
    assert f'{DAY}: rise 0.20 U, rest 0.05 U, total 0.25 U, 40 loops' in with_floors and '96.0 %' in with_floors


def test_leaderboard_rows_are_json_and_clean():
    r = M.rank_variants(rows_one_day({'a': mk(rise=0.5), 'b': mk(rise=0.1, env3=5)}), 'explore')
    rows = M.leaderboard_rows(r)
    json.dumps(rows)
    json.dumps(r.to_dict())
    for row in rows:
        no_forbidden_words(row['why'])
        assert row['verdict'] in ('recommended', 'candidate', 'tie', 'infeasible')


def test_default_objective_matches_design():
    assert M.DEFAULT_OBJECTIVE['scalar_weights']['rest'] == 2.0 and M.DEFAULT_OBJECTIVE['scalar_weights']['lowside'] == 0.3
    assert M.DEFAULT_OBJECTIVE['envelope'] == {'max_3h_U': 1.0, 'max_day_pct_tdd': 15.0}
    assert M.DEFAULT_OBJECTIVE['feasible']['cap_binding_max_pct'] == 50.0
    assert M.TIE_FACTOR == 2.0


# ----------------------------------------------------------------------------- the fixture run through the ranking
def test_fixture_study_ranking(example_csvs, ns_settings_path):
    with open(ns_settings_path, encoding='utf-8') as fh:
        settings = json.load(fh)
    base = M.LoopSeries.from_csv(example_csvs['base'])
    rows = []
    for v, path in example_csvs.items():
        a = [] if v == 'base' else [('profile autoISF_max', float(v.split('__')[0].rsplit('_', 1)[1])),
                                    ('profile dura_ISF_weight', float(v.split('__')[1].rsplit('_', 1)[1]))]
        m = M.trial_metrics(path, None if v == 'base' else base, M.effective_cap(a, [], settings, 'autoISF_max'),
                            M.effective_cap(a, [], settings, 'autoISF_min'))
        rows.append({'variant': v, 'day': DAY, 'metrics': m, 'params': dict(a)})
    r = M.rank_variants(rows, 'explore')
    assert r.infeasible == [] and len(r.order) == 4
    assert r.order[0] == 'autoISF_max_1.3__dura_ISF_weight_0.5'      # the largest |effect| / floor
    assert r.order[-1] == 'autoISF_max_1.3__dura_ISF_weight_0.7'     # identical to base: 0 / floor
    f = r.floors[DAY]
    assert round(f['floor_total_U'], 2) == 1.37 and f['floor_loops'] == 26
    # on one 6 h morning the floor on rise is the whole replay bias (1.38 U): every variant is a tie with base
    assert all(r.variants[n].tie_with_base for n in r.order)
    assert all(b['verdict'] == 'tie' for b in M.leaderboard_rows(r))
    for q in ('earlier', 'gentler'):
        rq = M.rank_variants(rows, q)                      # inside the 1.38 U floor on rise: no variant is a step
        assert rq.infeasible == [] and set(rq.order) == set(r.order)
        assert rq.front == [] and set(rq.no_step) == set(r.order) and M.validate_top(rq) == []


# ----------------------------------------------------------------------------- H-715: one review order everywhere
def two_day_rows_where_review_score_and_floor_multiple_disagree():
    """A: 4 x floor on rise on day 1 and 4 x floor on rest on day 2 (review_score 4, floor multiple 2 on either
    component); B: 3 x floor on rise on both days (review_score 3, floor multiple 3). Floors: rise 0.2, rest 0.05."""
    rows = []
    for d, a in zip(DAYS5[:2], (mk(rise=0.8, rest=0.0), mk(rise=0.0, rest=0.2))):
        rows.append({'variant': 'base', 'day': d, 'metrics': mk(base=True)})
        rows.append({'variant': 'A', 'day': d, 'metrics': a, 'params': {'profile x': 'A'}})
        rows.append({'variant': 'B', 'day': d, 'metrics': mk(rise=0.6, rest=0.0), 'params': {'profile x': 'B'}})
    return rows


def test_review_order_is_the_floor_multiple_not_the_review_score():
    rows = two_day_rows_where_review_score_and_floor_multiple_disagree()
    r = M.rank_variants(rows, 'explore', days=DAYS5[:2])
    a, b = r.variants['A'], r.variants['B']
    assert a.review_score == pytest.approx(4.0) and b.review_score == pytest.approx(3.0)   # kept for the record
    assert a.floor_multiple == pytest.approx(2.0) and b.floor_multiple == pytest.approx(3.0)
    assert b.review_component == 'rise_vs_base_U' and b.floor_multiples['rise_vs_base_U'] == pytest.approx(3.0)
    assert r.order == ['B', 'A'] and b.rank == 1 and a.rank == 2
    lb = M.leaderboard_rows(r)
    assert [x['variant'] for x in lb] == ['B', 'A']
    assert lb[0]['floor_multiple'] == pytest.approx(3.0) and lb[0]['review_score'] == pytest.approx(3.0)
    assert lb[1]['floor_multiple'] == pytest.approx(2.0) and lb[1]['review_score'] == pytest.approx(4.0)
    assert lb[0]['why'].endswith('3.0 x floor') and lb[1]['why'].endswith('2.0 x floor')
    assert lb[0]['floor_multiples']['rise_vs_base_U'] == pytest.approx(3.0)


def test_validate_top_n_follows_the_floor_multiple():
    rows = two_day_rows_where_review_score_and_floor_multiple_disagree()
    hold = DAYS5[2]
    rows += [{'variant': 'base', 'day': hold, 'metrics': mk(base=True)},
             {'variant': 'A', 'day': hold, 'metrics': mk(rise=0.8), 'params': {'profile x': 'A'}},
             {'variant': 'B', 'day': hold, 'metrics': mk(rise=0.6), 'params': {'profile x': 'B'}}]
    val = M.validate_variants(rows, 'explore', None, DAYS5[:2], [hold], top=1)
    assert val.validated == ['B']                       # the review_score order would have validated A


def test_review_component_of_takes_the_largest_floor_multiple():
    assert M.review_component_of({'rise_vs_base_U': 1.0, 'rest_vs_base_U': 2.5}) == ('rest_vs_base_U', 2.5)
    assert M.review_component_of({'rise_vs_base_U': 2.0, 'rest_vs_base_U': 2.0}) == ('rise_vs_base_U', 2.0)
    assert M.review_component_of({'rise_vs_base_U': None, 'rest_vs_base_U': None},
                                 ['rest_vs_base_U', 'rest_vs_base_U', 'rise_vs_base_U']) == ('rest_vs_base_U', 0.0)
    assert M.review_component_of({}) == (M.REVIEW_COMPONENTS[0], 0.0)
