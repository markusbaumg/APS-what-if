"""
test_validate.py  -  H-308, the validate phase: the plan (top-N of the search review order on the hold-out days,
base first, hold-out days never in a search phase), the completion rule, the Kendall tau and leave-one-day-out maths
on a toy table, the three conditions of recommended for review, round.json / status.json / cards / tier-1 fields,
one sentinel per hold-out day, and resume-after-finish through the orchestrator with the fake replay. Offline.
"""
import json
import os

import pytest

from hindcast import cards, cli, contracts as c, lint, orchestrator, report, search
from hindcast import metrics as M
from hindcast import round as rnd
from hindcast import space as sp
from hindcast import study as st
from hindcast.contracts import Phase
from hindcast.tests import test_orchestrator as to
from hindcast.tests import test_round as tr
from hindcast.tests import test_search as ts
from hindcast.tests.conftest import GRID_AUTOISF, NS_SETTINGS

S = ['2026-09-01', '2026-09-02', '2026-09-03']
H = ['2026-09-05', '2026-09-06', '2026-09-07']


def lint_clean(text):
    findings = lint.lint_findings(text, 'line')
    assert not findings, [str(f) for f in findings]


# ----------------------------------------------------------------------------- Kendall tau

def test_kendall_tau_on_small_orders():
    assert M.kendall_tau(['a', 'b', 'c'], ['a', 'b', 'c']) == 1.0
    assert M.kendall_tau(['a', 'b', 'c'], ['c', 'b', 'a']) == -1.0
    assert M.kendall_tau(['a', 'b', 'c'], ['b', 'a', 'c']) == pytest.approx(1 / 3)
    assert M.kendall_tau(['a', 'b', 'c', 'd'], ['a', 'c', 'x']) == 1.0       # only the common items count
    assert M.kendall_tau(['a'], ['a']) is None and M.kendall_tau([], ['a', 'b']) is None


# ----------------------------------------------------------------------------- the toy table

def toy_rows():
    """Floors (test_round.mk base): rise 0.2 U, so the review score is |rise| / 0.2. Search scores: A 4.5, B 4.0,
    E 3.67 (2.0 on the first day only), C 3.5, D 3.0. top=4 validates A, B, E, C.
      A  holds on every hold-out day, feasible, stable                -> recommended for review
      B  rise flips sign on two of three hold-out days                -> direction condition fails
      E  falls to rank 5 when the first search day is left out: top-3 on 5 of 6 folds (83 %) -> still recommended
         under DESIGN.md 5.3 rule 5 (fold_share 80 %); a stricter objective.recommend makes it fail
      C  out of envelope on the second hold-out day                   -> feasibility condition fails"""
    per = {
        'A': {d: 0.9 for d in S + H},
        'B': dict({d: 0.8 for d in S}, **{H[0]: 0.8, H[1]: -0.3, H[2]: -0.4}),
        'E': dict({S[0]: 2.0, S[1]: 0.1, S[2]: 0.1}, **{d: 0.8 for d in H}),
        'C': {d: 0.7 for d in S + H},
        'D': {d: 0.6 for d in S},
    }
    rows = [{'variant': 'base', 'day': d, 'metrics': tr.mk(base=True), 'params': {}} for d in S + H]
    for name, by_day in per.items():
        for d, rise in by_day.items():
            extra = {'env3': 1.4} if (name == 'C' and d == H[1]) else {}
            rows.append({'variant': name, 'day': d, 'metrics': tr.mk(rise=rise, rest=0.0, **extra),
                         'params': {'profile x': name}})
    return rows


def test_validate_variants_on_the_toy_table():
    val = M.validate_variants(toy_rows(), 'explore', None, S, H, top=4)
    assert val.validated == ['A', 'B', 'E', 'C']                       # D (rank 5) is not validated
    assert val.search_rank == {'A': 1, 'B': 2, 'E': 3, 'C': 4}
    assert val.holdout_order == ['A', 'E', 'B']                        # C infeasible on a hold-out day: not ranked
    assert val.tau == pytest.approx(1 / 3)                             # (A,B) (A,E) concordant, (B,E) discordant
    assert set(val.folds) == set(S + H) and len(val.folds) == 6
    # leaving out the first search day ranks the whole population over the other two search days
    assert val.folds[S[0]] == ['A', 'B', 'C', 'D', 'E']
    a, b, e, cc = (val.per_variant[n] for n in 'ABEC')
    assert (a['holdout_rank'], a['holdout_total'], a['holds'], a['holdout_days']) == (1, 3, 3, 3)
    assert (a['lodo_min'], a['lodo_max'], a['lodo_unranked']) == (1, 2, 0)
    assert (e['lodo_min'], e['lodo_max']) == (1, 5)
    assert b['holds'] == 1 and b['flip_days'] == H[1:] and b['direction'] == 1 and b['component'] == 'rise_vs_base_U'
    assert cc['feasible_all_days'] is False and cc['infeasible_days'] == [H[1]]
    assert cc['lodo_unranked'] == 2                                     # the hold-out folds that keep H[1]
    assert set(a) == set(M.VALIDATION_KEYS)
    assert val.recommended == ['A', 'E']                               # rule 5: E is top-3 on 5 of 6 folds
    assert e['fold_share'] == pytest.approx(0.833) and e['fold_top'] == 3 and e['holdout_top'] == 5
    assert val.summary()['recommended_for_review'] == 2 and val.summary()['tau'] == pytest.approx(0.333)


def test_the_three_conditions_each_name_their_reason():
    val = M.validate_variants(toy_rows(), 'explore', None, S, H, top=4)
    pv = val.per_variant
    assert pv['A']['recommended_for_review'] and pv['A']['reasons'] == []
    assert not pv['B']['recommended_for_review']
    assert 'direction flips on 2 of 3 hold-out days' in pv['B']['reasons']
    assert any(r.startswith('direction holds on 1 of 3') for r in pv['B']['reasons'])
    assert pv['E']['recommended_for_review'] and pv['E']['reasons'] == []   # the old 'worst fold <= N' rule is gone
    assert not pv['C']['recommended_for_review']
    assert any(r.startswith('not feasible on 1 day(s): ' + H[1]) for r in pv['C']['reasons'])
    for v in pv.values():
        for r in v['reasons']:
            lint_clean(r)
        lint_clean(M.validation_line(v))
    assert M.validation_line(pv['A']) == 'hold-out: rank 1 of 3, direction holds on 3/3 days, LODO range 1-2'


def test_rule_5_uses_the_objective_recommend_config():
    """fold_top / fold_share / sign_share / holdout_top come from objective.recommend, every failed one is named."""
    strict = {'recommend': {'fold_share': 0.9}}
    pv = M.validate_variants(toy_rows(), 'explore', strict, S, H, top=4).per_variant
    assert pv['A']['recommended_for_review']
    assert pv['E']['reasons'] == ['top-3 on 83 % of the 6 leave-one-day-out folds (needs 90 %)']
    pv = M.validate_variants(toy_rows(), 'explore', {'recommend': {'holdout_top': 1}}, S, H, top=4).per_variant
    assert pv['A']['recommended_for_review']
    assert pv['E']['reasons'] == ['hold-out rank 2 (needs within the top 1)']
    pv = M.validate_variants(toy_rows(), 'explore', {'recommend': {'fold_top': 1}}, S, H, top=4).per_variant
    assert any(r.startswith('top-1 on ') for r in pv['E']['reasons'])
    # the sign share of the objective: B holds on 1 of 3 hold-out days, a share of 30 % lets it through that condition
    val = M.validate_variants(toy_rows(), 'explore', {'recommend': {'sign_share': 0.3}}, S, H, top=4)
    assert val.sign_share == 0.3
    assert not any(r.startswith('direction') for r in val.per_variant['B']['reasons'])
    # several failed conditions are all named
    both = {'recommend': {'fold_share': 0.9, 'holdout_top': 1}}
    assert len(M.validate_variants(toy_rows(), 'explore', both, S, H, top=4).per_variant['E']['reasons']) == 2
    for v in pv.values():
        for r in v['reasons']:
            lint_clean(r)


def test_a_tie_with_base_is_never_recommended_for_review():
    """T stays inside 2 x floor (0.2 U rise floor) on every search and hold-out day: validated (the top N is the top
    N), but never recommended for review; the other variants are unaffected."""
    rows = toy_rows()
    for d in S + H:
        rows.append({'variant': 'T', 'day': d, 'metrics': tr.mk(rise=0.3, rest=0.0), 'params': {'profile x': 'T'}})
    val = M.validate_variants(rows, 'explore', {'recommend': {'fold_top': 6, 'holdout_top': 6}}, S, H, top=6)
    assert 'T' in val.validated
    t = val.per_variant['T']
    assert t['tie_with_base'] is True and not t['recommended_for_review']
    assert t['reasons'][0] == M.TIE_REASON
    assert val.per_variant['A']['recommended_for_review']


def test_recommended_variants_carry_the_inside_floor_caveat():
    """the caveat counts the holding hold-out days on which the effect stays inside 2 x that day's floor"""
    rows = toy_rows()
    for r in rows:
        if r['variant'] == 'A' and r['day'] == H[0]:
            r['metrics'] = tr.mk(rise=0.3, rest=0.0)                    # holds (positive), inside 2 x 0.2 U
    val = M.validate_variants(rows, 'explore', None, S, H, top=4)
    a = val.per_variant['A']
    assert a['recommended_for_review'] and a['holds'] == 3 and a['inside_floor_days'] == 1
    cav = M.validation_caveat(a)
    assert cav.startswith('caveat: inside 2 x floor on 1 of the 3 holding hold-out days')
    lint_clean(cav)
    assert M.validation_caveat(val.per_variant['E']).startswith('caveat: outside 2 x floor on every one of the 3')
    assert M.validation_caveat(None) is None


def test_ranking_rule_text_states_rule_5():
    text = M.ranking_rule_text('explore', {'recommend': {'fold_top': 2, 'holdout_top': 4}}, validate=True)
    assert 'after the validate phase' in text and 'in the top 2 on at least 80 % of the leave-one-day-out folds' in text
    assert 'feasible on every search and hold-out day' in text and 'within the top 4 on the hold-out days' in text
    assert 'at least 80 % of the hold-out days' in text and 'A tie with base is never recommended for review' in text
    assert 'quarantined days are listed, not scored' in text
    lint.lint_text(text, 'line')
    # without a validate round the H-202 text stays as it was at the default config
    plain = M.ranking_rule_text('explore')
    assert ('Recommended for review only if feasible on all days, top-3 on at least 80 % of the leave-one-day-out'
            ' folds, the same sign of the primary effect on at least 80 % of days, and within the top 5 on the'
            ' hold-out days.') in plain


def test_a_missing_hold_out_row_blocks_the_recommendation():
    rows = [r for r in toy_rows() if not (r['variant'] == 'A' and r['day'] == H[2])]
    pv = M.validate_variants(rows, 'explore', None, S, H, top=4).per_variant['A']
    assert not pv['recommended_for_review'] and pv['missing_days'] == 1
    assert 'not replayed on 1 of 3 hold-out days yet' in pv['reasons']


def test_without_hold_out_days_nothing_is_recommended():
    val = M.validate_variants(toy_rows(), 'explore', None, S, [], top=4)
    assert val.folds == {} and val.recommended == []
    assert 'no hold-out days' in val.per_variant['A']['reasons']


# ----------------------------------------------------------------------------- the plan

HOLD = ('2026-09-07', '2026-09-08')


def validate_study(tmp_path, top=2, days='holdout'):
    return ts.make_study(tmp_path, phases=[Phase(kind='oaat', points=5), Phase(kind='validate', top=top, days=days)],
                         days=ts.DAYS + HOLD, holdout=list(HOLD))


def screened(study):
    rows = ts.Rows(study)
    rows.base(ts.DAYS)
    ts.screen_all(rows, study)
    return rows


def test_hold_out_days_never_enter_a_search_phase(tmp_path):
    sd, study = validate_study(tmp_path)
    assert search.phase_days(study, study.phases[0]) == list(ts.DAYS)
    tasks, _ = search.phase_tasks(study, study.phases[0], settings=ts.SETTINGS)
    assert not {t.day for t in tasks} & set(HOLD)
    with pytest.raises(search.SearchError):
        search.phase_days(study, Phase(kind='lhs', days='holdout'))
    with pytest.raises(search.SearchError):
        search.phase_days(study, Phase(kind='oaat', days=HOLD[0]))
    assert search.phase_days(study, study.phases[1]) == list(HOLD)
    assert search.phase_days(study, Phase(kind='validate', days=HOLD[1])) == [HOLD[1]]
    with pytest.raises(search.SearchError):                            # a search day cannot validate the search
        search.phase_days(study, Phase(kind='validate', days=ts.DAYS[0]))
    study.days[-1].role = 'quarantined'
    assert search.phase_days(study, study.phases[1]) == [HOLD[0]]


def test_validate_plan_is_the_top_n_on_the_hold_out_days_base_first(tmp_path):
    sd, study = validate_study(tmp_path, top=2)
    rows = screened(study)
    p = search.plan(study, rows.rows, settings=ts.SETTINGS)
    oaat, val = p.phases
    assert oaat.complete and not val.skipped and not val.complete
    assert p.current is val and val.days == list(HOLD)
    assert val.total == 2 + 2 * 2                                     # base on 2 hold-out days + top 2 x 2 days
    # the top 2 of the search review order (metrics.rank_variants over the search days)
    dicts = rnd.trial_dicts(rows.rows)
    order = M.rank_variants(dicts, 'explore', None, list(ts.DAYS)).order
    params = {d['variant']: d['params'] for d in dicts}
    batch = search.next_batch(study, rows.rows, settings=ts.SETTINGS)
    assert [t.control for t in batch[:2]] == ['base', 'base'] and {t.day for t in batch[:2]} == set(HOLD)
    assert [(t.params, t.day) for t in batch[2:]] == [(params[order[0]], HOLD[0]), (params[order[0]], HOLD[1]),
                                                      (params[order[1]], HOLD[0]), (params[order[1]], HOLD[1])]
    assert all(t.phase == 'validate' for t in batch[2:])
    for n in val.notes:
        lint_clean(n)
    lint_clean(search.plan_text(p, study))


def test_validate_completes_when_every_variant_day_is_settled(tmp_path):
    sd, study = validate_study(tmp_path, top=2)
    rows = screened(study)
    batch = search.next_batch(study, rows.rows, settings=ts.SETTINGS)
    for t in batch[:-1]:
        if t.control == 'base':
            rows.add({}, t.day, ts.BASE_METRICS, control='base', phase='floor')
        else:
            rows.add(t.params, t.day, ts.effect_metrics(0.4, 10), phase='validate')
    p = search.plan(study, rows.rows, settings=ts.SETTINGS)
    assert not p.phases[1].complete and p.phases[1].pending == 1
    assert search.next_batch(study, rows.rows, settings=ts.SETTINGS) == batch[-1:]
    t = batch[-1]
    rows.add(t.params, t.day, ts.effect_metrics(0.4, 10), phase='validate')
    p = search.plan(study, rows.rows, settings=ts.SETTINGS)
    assert p.phases[1].complete and p.current is None
    assert search.next_batch(study, rows.rows, settings=ts.SETTINGS) == []
    assert 'recommended for review' in search.plan_text(p, study)


def test_validate_without_hold_out_days_is_complete_with_a_note(tmp_path):
    sd, study = ts.make_study(tmp_path, phases=[Phase(kind='oaat', points=5), Phase(kind='validate', top=2)])
    rows = screened(study)
    p = search.plan(study, rows.rows, settings=ts.SETTINGS)
    assert p.phases[1].complete and p.phases[1].total == 0 and 'no hold-out days' in p.phases[1].notes[0]


def test_cv_stays_skipped_and_names_the_validate_round(tmp_path):
    sd, study = ts.make_study(tmp_path, phases=[Phase(kind='oaat', points=5), Phase(kind='cv')])
    p = search.plan(study, screened(study).rows, settings=ts.SETTINGS)
    assert p.phases[1].skipped and 'H-308' in p.phases[1].notes[0]


# ----------------------------------------------------------------------------- the sentinel per hold-out day

def test_the_sentinel_runs_once_on_every_hold_out_day():
    study = type('S', (), {'sentinel_every': 25})()
    g = st.Guards(study, settings={}, null=True)
    g.phone_params = lambda day: ({'profile autoISF_max': 1.5}, [])
    tasks = [st.Task(params={'profile autoISF_max': v}, day=d, phase='validate') for v in (1.3, 1.4) for d in H]
    out = g.schedule(tasks)
    sentinels = [t for t in out if t.control == 'sentinel']
    assert sorted(t.day for t in sentinels) == sorted(H)               # once per hold-out day, not per task
    assert [t.control for t in out].count('null') == 1
    for d in H:                                                        # before the first variant of its day
        first = next(i for i, t in enumerate(out) if t.day == d and t.control is None)
        assert any(t.control == 'sentinel' and t.day == d for t in out[:first])
    # a search phase keeps the old cadence: one sentinel at the phase start
    out2 = g.schedule([st.Task(params={'profile autoISF_max': 1.3}, day=d, phase='lhs') for d in H])
    assert [t.control for t in out2].count('sentinel') == 1


def test_the_validate_sentinel_runs_once_per_day_for_the_whole_study():
    """a later validate round does not repeat the sentinel on a day that already has a done one (covered_days), and
    the phase start brings no extra sentinel on a covered day"""
    study = type('S', (), {'sentinel_every': 25})()
    g = st.Guards(study, settings={}, null=True)
    g.phone_params = lambda day: ({'profile autoISF_max': 1.5}, [])
    tasks = [st.Task(params={'profile autoISF_max': v}, day=d, phase='validate', round=2) for v in (1.3, 1.4) for d in H]
    out = g.schedule(tasks, 'r2', covered_days={H[0], H[1]})
    assert [t.day for t in out if t.control == 'sentinel'] == [H[2]]
    out = g.schedule(tasks, 'r3', covered_days=set(H))
    assert [t for t in out if t.control] == []                         # every hold-out day covered: no control
    # covered_days from trials.jsonl rows: done sentinel rows only (latest per key)
    mk = lambda key, day, status, control: c.TrialRow(trial=1, key=key, phase='validate', vhash='0' * 40, variant='s',
                                                        params={}, day=day, status=status, control=control, code_sha='',
                                                        settings_sha='', freeze_sha='')
    rows = [mk('a' * 40, H[0], 'done', 'sentinel'), mk('b' * 40, H[1], 'failed', 'sentinel'),
            mk('c' * 40, H[2], 'done', None), mk('d' * 40, S[0], 'done', 'sentinel'), mk('d' * 40, S[0], 'stale', 'sentinel')]
    assert st.Guards.sentinel_days_done(rows) == {H[0]}


def test_no_control_is_scheduled_on_a_quarantined_day():
    day = lambda d, role: type('D', (), {'day': d, 'role': role})()
    study = type('S', (), {'sentinel_every': 25, 'days': [day(H[0], 'holdout'), day(H[1], 'quarantined'),
                                                          day(H[2], 'holdout')]})()
    g = st.Guards(study, settings={}, null=True)
    g.phone_params = lambda d: ({'profile autoISF_max': 1.5}, [])
    tasks = [st.Task(params={}, day=H[1], phase='floor', control='base')]
    tasks += [st.Task(params={'profile autoISF_max': 1.3}, day=d, phase='validate') for d in H]
    tasks += [st.Task(params={'profile autoISF_max': 1.5}, day=H[1], phase='validate', control='sentinel')]
    out = g.schedule(tasks)
    assert not [t for t in out if t.control in ('sentinel', 'null') and t.day == H[1]]
    assert sorted(t.day for t in out if t.control == 'sentinel') == [H[0], H[2]]
    assert [t.control for t in out].count('null') == 1


# ----------------------------------------------------------------------------- round.json, status, cards, tier 1

VALIDATE_VARIANTS = {
    'A': ({'profile autoISF_max': tr.PHONE_MAX, 'profile dura_ISF_weight': 0.5}, dict(rise=-0.9, rest=0.02)),
    'B': ({'profile autoISF_max': tr.PHONE_MAX, 'profile dura_ISF_weight': 0.6}, dict(rise=-0.85, rest=0.01)),
    'wild': ({'profile autoISF_max': tr.PHONE_MAX, 'profile dura_ISF_weight': 0.9}, dict(rise=2.0, rest=0.5, env3=1.4)),
}


def validated_study_dir(tmp_path, flip_b=True):
    """test_round's constructed study with a validate phase (top 2) and two hold-out days; B flips its direction
    on the second hold-out day."""
    sd, study = tr.make_study_with_rows(tmp_path, variants=VALIDATE_VARIANTS, holdout=['2026-09-05', '2026-09-06'],
                                        tags={})
    study.phases.append(Phase(kind='validate', top=2, days='holdout'))
    sd.write_study(study)
    if flip_b:
        rows = sd.trials()
        for r in rows:
            if r.variant != 'base' and r.params == VALIDATE_VARIANTS['B'][0] and r.day == '2026-09-06':
                r.metrics = tr.mk(rise=0.85, rest=0.01)
        with open(sd.trials_path, 'w', encoding='utf-8') as fh:
            for r in rows:
                fh.write(r.to_compact_json() + '\n')
    return sd, sd.load_study(tr.SETTINGS)


def test_round_status_cards_and_tier1_carry_the_validation(tmp_path):
    sd, study = validated_study_dir(tmp_path)
    doc, status = rnd.produce(sd, round_no=3, phase='validate', settings=tr.SETTINGS)
    by = {v.variant: v for v in doc.variants}
    a = by[next(n for n in by if n.endswith('0.5'))]
    b = by[next(n for n in by if n.endswith('0.6'))]
    assert a.verdict == 'recommended' and a.validation['recommended_for_review']
    assert 'recommended for review' in a.why
    assert b.verdict == 'candidate' and 'direction flips on 1 of 2 hold-out days' in b.why
    assert set(a.validation) == set(M.VALIDATION_KEYS)
    assert doc.validation['top'] == 2 and doc.validation['recommended_for_review'] == 1
    assert doc.validation['holdout_days'] == ['2026-09-05', '2026-09-06'] and doc.validation['tau'] is not None
    assert not doc.problems()
    for v in doc.variants:
        if v.verdict != 'base' and v.verdict != 'infeasible':
            assert len(v.to_compact_json().encode()) <= c.ROUND_MAX_BYTES_PER_VARIANT
        lint_clean(v.why)
    for n in doc.notes:
        lint_clean(n)
    # round.json round-trips the new fields
    back = c.Round.from_dict(json.load(open(sd.round_path)))
    assert back.validation == doc.validation and back.variants[1].validation == doc.variants[1].validation
    # status.json: the top list names the validation line, under 2 KB
    top = {t.variant: t for t in status.top}
    assert top[a.variant].validation.startswith('hold-out: rank 1 of ') and top[a.variant].verdict == 'recommended'
    assert len(status.to_compact_json().encode()) <= c.STATUS_MAX_BYTES
    text = st.status_text(status)
    assert 'hold-out: rank' in text
    lint.lint_text(text, 'line')
    # cards
    analysis = rnd.analyze(study, sd.trials(), tr.SETTINGS)
    card = cards.card_text(analysis.reports[a.vhash], analysis, settings=tr.SETTINGS)
    assert 'hold-out: rank 1 of' in card and 'LODO range' in card and 'Kendall tau' in card
    cards.lint_card(card)
    card_b = cards.card_text(analysis.reports[b.vhash], analysis, settings=tr.SETTINGS)
    assert 'flips on: 2026-09-06' in card_b
    cards.lint_card(card_b)
    # tier 1: data['parameters'].variants[*].validation and the study-level block
    rows = sd.trials()
    ps = report.parameter_space(study, rows, [doc])
    pv = {v['vhash']: v for v in ps['variants']}
    assert pv[a.vhash]['validation']['recommended_for_review'] is True
    assert pv[a.vhash]['validation']['line'].startswith('hold-out: rank 1')
    assert pv[a.vhash]['verdict'] == 'recommended'
    assert ps['validation']['top'] == 2
    assert any('after the validate phase' in line for line in ps['rule_lines'])


def test_without_a_validate_phase_the_older_rule_still_applies(tmp_path):
    sd, study = tr.make_study_with_rows(tmp_path, variants=VALIDATE_VARIANTS, holdout=['2026-09-05'], tags={})
    analysis = rnd.analyze(study, sd.trials(), tr.SETTINGS)
    assert analysis.validation is None
    assert all(r.validation is None for r in analysis.reports.values())
    doc = rnd.build_round(sd, round_no=1, phase='grid', settings=tr.SETTINGS)
    assert doc.validation is None and 'validation' not in doc.to_dict()


# ----------------------------------------------------------------------------- resume after finish (orchestrator)

def test_a_finished_study_resumed_runs_only_the_validate_phase(tmp_path, fake, monkeypatch):
    hold = to.DAY3                                                     # frozen without a CGM gap: stays a hold-out day
    code = cli.main(['study', 'new', 'v', '--from-grid', GRID_AUTOISF, '--days', f'{to.DAY},{hold}',
                     '--holdout', hold, '--phase', 'validate:top=2,days=holdout', '--question', 'explore',
                     '--studies-dir', str(tmp_path), '--settings', NS_SETTINGS])
    assert code == 0
    sd = st.StudyDir(os.path.join(str(tmp_path), 'v'))
    # the build before H-308: validate is not built, the study finishes after the grid
    monkeypatch.setattr(search, 'BUILT_KINDS', ('floor', 'oaat', 'grid', 'lhs'))
    o, lines = to.orch(sd)
    assert o.run() == 0
    assert json.load(open(sd.study_json))['phases'][-1]['state'] == 'skipped'
    first = sd.trials()
    assert not [r for r in first if r.day == hold]                     # the hold-out day was never touched
    assert {d['day']: d['role'] for d in json.load(open(sd.study_json))['days']}[hold] == 'holdout'
    grid_keys = {r.key for r in first if r.control not in st.CONTROL_KINDS}
    monkeypatch.undo()
    monkeypatch.setattr(to.replay, 'run_trial', fake)                  # undo() removed the fake fixture's patches
    monkeypatch.setattr(to.freeze, 'blocked_reason', lambda day, *a, **k: fake.blocked_days.get(day))
    n_calls = len(fake.calls)
    o2, lines2 = to.orch(sd)
    assert o2.run() == 0
    new = sd.trials()[len(first):]
    specs = fake.calls[n_calls:]
    assert {s.day for s in specs} == {hold}                            # nothing on the search day again
    variants = [r for r in new if r.control is None]
    assert len(variants) == 2 and {r.phase for r in variants} == {'validate'}
    assert [r.control for r in new].count('base') == 1                 # the hold-out base, once
    assert [r.control for r in new].count('sentinel') == 1             # one sentinel on the hold-out day
    assert not ({r.key for r in new} & grid_keys)
    assert json.load(open(sd.study_json))['phases'][-1]['state'] == 'done'
    exit_line = next(l for l in lines2 if l.startswith('v: exit 0'))
    assert 'validated variants recommended for review on 1 hold-out days' in exit_line
    for l in lines2:
        lint_clean(l)
    doc = sd.read_round()
    assert doc.phase == 'validate' and doc.validation['validated'] == 2
    # a third run has nothing left
    n = len(sd.trials())
    o3, _ = to.orch(sd)
    assert o3.run() == 0 and len(sd.trials()) == n


@pytest.fixture
def fake(monkeypatch):
    f = to.Fake()
    monkeypatch.setattr(to.replay, 'run_trial', f)
    monkeypatch.setattr(to.freeze, 'blocked_reason', lambda day, *a, **k: f.blocked_days.get(day))
    return f


# ----------------------------------------------------------------------------- one rule-5 sentence on every surface

def test_validation_summary_carries_the_rule_5_config_and_the_panel_states_it():
    """Validation.summary() carries fold_top / fold_share / sign_share / holdout_top; report.validation_lines and
    ranking_rule_text phrase rule 5 with ONE builder (metrics.recommend_rule_text), shares as '80 %'."""
    obj = {'recommend': {'fold_top': 2, 'fold_share': 0.75, 'holdout_top': 4}}
    val = M.validate_variants(toy_rows(), 'explore', obj, S, H, top=4)
    summ = val.summary()
    assert (summ['fold_top'], summ['fold_share'], summ['sign_share'], summ['holdout_top']) == (2, 0.75, 0.8, 4)
    rule = M.recommend_rule_text(summ, validate=True)
    assert rule == M.recommend_rule_text(obj, validate=True)
    assert 'in the top 2 on at least 75 % of the leave-one-day-out folds' in rule
    assert 'at least 80 % of the hold-out days' in rule and 'within the top 4 on the hold-out days' in rule
    lines = report.validation_lines(summ)
    assert any(rule in x for x in lines), 'the panel states the configured rule'
    assert not any('every fold keeps' in x for x in lines), 'the old rule is gone'
    assert rule in M.ranking_rule_text('explore', obj, validate=True), 'the page cannot contradict itself'
    for x in lines:
        lint_clean(x)
    # an older round.json block without the four keys falls back to the defaults
    old = report.validation_lines({'top': 3, 'validated': 3, 'search_days': 2, 'holdout_days': H, 'folds': 5})
    assert any(M.recommend_rule_text(None, validate=True) in x for x in old)
    assert M.share_pct(0.8) == '80 %' and M.share_pct(0.833, 0) == '83 %' and M.share_pct(0.825) == '82.5 %'


def test_non_validate_recommended_why_reads_objective_recommend():
    rep = rnd.VariantReport(vhash='a' * 40, variant='x', params={}, verdict='pending', why='', days=S,
                            feasible=True, tie_with_base=False, recommended=True, validation=None)
    obj = {'recommend': {'fold_top': 4, 'fold_share': 0.9, 'sign_share': 0.7, 'holdout_top': 6}}
    verdict, why = rnd.verdict_of(rep, [], S, obj)
    assert verdict == 'recommended'
    assert why == M.recommend_rule_text(obj, validate=False) + ': recommended for review'
    assert 'top-4 on at least 90 %' in why and 'at least 70 % of days' in why and 'top 6 on the hold-out' in why
    assert 'top 3' not in why
    lint_clean(why)
    _verdict, default_why = rnd.verdict_of(rep, [], S)
    assert 'top-3 on at least 80 %' in default_why and 'within the top 5' in default_why


# ----------------------------------------------------------------------------- H-715 / H-716 on a study directory
H715_VARIANTS = {
    'A': ({'profile autoISF_max': tr.PHONE_MAX, 'profile dura_ISF_weight': 0.5}, dict(rise=0.8, rest=0.0)),
    'B': ({'profile autoISF_max': tr.PHONE_MAX, 'profile dura_ISF_weight': 0.6}, dict(rise=0.6, rest=0.0)),
}


def h715_study_dir(tmp_path):
    """A: 4 x floor on rise on the first search day, 4 x floor on rest on the other two (review_score 4, floor
    multiple 8/3 on rest); B: 3 x floor on rise on every day (review_score 3, floor multiple 3). Validate top 1."""
    sd, study = tr.make_study_with_rows(tmp_path, variants=H715_VARIANTS, holdout=['2026-09-05', '2026-09-06'], tags={})
    study.phases.append(Phase(kind='validate', top=1, days='holdout'))
    sd.write_study(study)
    rows = sd.trials()
    for r in rows:
        if r.params == H715_VARIANTS['A'][0] and r.day in ('2026-09-02', '2026-09-03'):
            r.metrics = tr.mk(rise=0.0, rest=0.2)
    with open(sd.trials_path, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(r.to_compact_json() + '\n')
    return sd, sd.load_study(tr.SETTINGS)


def test_one_review_order_on_every_surface(tmp_path):
    sd, study = h715_study_dir(tmp_path)
    a_vh, b_vh = c.vhash(H715_VARIANTS['A'][0]), c.vhash(H715_VARIANTS['B'][0])
    analysis = rnd.analyze(study, sd.trials(), tr.SETTINGS)
    ra, rb = analysis.reports[a_vh], analysis.reports[b_vh]
    assert ra.review_score > rb.review_score                         # the old key would put A first
    assert rnd.floor_multiple_of(ra) == pytest.approx(8 / 3) and rnd.floor_multiple_of(rb) == pytest.approx(3.0)
    assert ra.review_component == 'rest_vs_base_U'
    assert analysis.order == [b_vh, a_vh] and rb.rank == 1           # rank_variants
    assert analysis.validation.validated == [rb.variant]             # the validate top-N (metrics)
    points, _ = search.validate_points(study, sd.trials(), study.phases[-1], settings=tr.SETTINGS)
    assert points == [H715_VARIANTS['B'][0]]                         # the validate top-N (search plan)
    doc, status = rnd.produce(sd, round_no=2, phase='validate', settings=tr.SETTINGS)
    assert [v.vhash for v in doc.variants if v.verdict != 'base'][:2] == [b_vh, a_vh]
    assert doc.front[:2] == [b_vh, a_vh]
    by = {v.vhash: v for v in doc.variants}
    assert by[b_vh].effect['floor_multiple'] == pytest.approx(3.0) and by[a_vh].effect['floor_multiple'] == pytest.approx(2.667)
    assert '(2.7 x floor)' in by[a_vh].why                           # the why prints the review order key
    assert status.top[0].vhash == c.short(b_vh)                      # status.json top
    assert [r.vhash for r in cards.card_order(analysis)][:2] == [b_vh, a_vh]
    ps = report.parameter_space(study, sd.trials(), [doc])           # the page's leaderboard
    assert [v['vhash'] for v in ps['variants']][:2] == [b_vh, a_vh]
    first = ps['variants'][0]
    assert first['floor_multiple'] == pytest.approx(3.0) and first['floor_multiples']['rise_vs_base_U'] == pytest.approx(3.0)
    assert ps['variants'][1]['review_score'] > first['review_score']  # kept in the row for the record
    js = open(os.path.join(report.TEMPLATES_DIR, 'views', 'parameters.js'), encoding='utf-8').read()
    assert "text: 'x floor'" in js and 'fmt.num(v.floor_multiple' in js and 'fmt.num(v.review_score' not in js
