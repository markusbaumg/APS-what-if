"""
test_round.py  -  H-502: round.json / status.json producers (hindcast/round.py) on constructed trial sets: the
verdict rules (base, pending, infeasible, recommended, dead, tie, candidate), the dead-parameter rule, strata and
hold-out ranks, ties between variants, the size limits, the vocabulary of every `why`, the CLI entry.
Offline; `make_study_with_rows()` is also what test_cards.py builds on.
"""
import json
import os

import pytest

from hindcast import contracts as c
from hindcast import lint
from hindcast import metrics as M
from hindcast import round as rnd
from hindcast import space as sp
from hindcast import study as st
from hindcast.tests.conftest import NS_SETTINGS

SETTINGS = sp.load_settings(NS_SETTINGS) or {'safety': {}, 'autoisf': {'autoISF_max': 1.5, 'dura_ISF_weight': 0.7}}
SPACE = {'profile autoISF_max': {'type': 'float', 'low': 1.3, 'high': 1.5, 'step': 0.1},
         'profile dura_ISF_weight': {'type': 'float', 'low': 0.5, 'high': 0.9, 'step': 0.1}}
SEARCH = ['2026-09-01', '2026-09-02', '2026-09-03']
HOLDOUT = ['2026-09-05']
TAGS = {'2026-09-01': ['workday'], '2026-09-02': ['workday'], '2026-09-03': ['workday'], '2026-09-05': ['weekend', 'sport']}
PHONE_MAX = sp.phone_value('profile autoISF_max', SETTINGS) or 1.5
PHONE_DURA = sp.phone_value('profile dura_ISF_weight', SETTINGS) or 0.7

# the constructed variants: params and the per-day metrics overrides
VARIANTS = {
    'A': ({'profile autoISF_max': PHONE_MAX, 'profile dura_ISF_weight': 0.5}, dict(rise=-0.9, rest=0.02)),
    'B': ({'profile autoISF_max': PHONE_MAX, 'profile dura_ISF_weight': 0.6}, dict(rise=-0.85, rest=0.01)),
    'tie': ({'profile autoISF_max': PHONE_MAX, 'profile dura_ISF_weight': 0.8}, dict(rise=0.3, rest=0.0, changed=43)),
    'dead1': ({'profile autoISF_max': 1.3, 'profile dura_ISF_weight': PHONE_DURA}, dict(rise=0.2, rest=0.0, changed=43)),
    'dead2': ({'profile autoISF_max': 1.4, 'profile dura_ISF_weight': PHONE_DURA}, dict(rise=0.1, rest=0.0, changed=42)),
    'wild': ({'profile autoISF_max': PHONE_MAX, 'profile dura_ISF_weight': 0.9}, dict(rise=2.0, rest=0.5, env3=1.4)),
}


def mk(rise=0.0, rest=0.0, loops=1000, P=0.0, env3=0.0, envd=0.0, cap=10.0, over=0, lowside=0, zt=0, changed=50,
       base=False, more=3, less=41, **extra):
    """a metrics dict as trial_metrics() writes it, with the fields the ranking and the cards read"""
    m = {'loops': loops, 'rise_vs_base_U': rise, 'rest_vs_base_U': rest, 'delta_vs_base_U': rise + rest,
         'smb_vs_base_U': rise + rest, 'tbr_vs_base_U': 0.0, 'changed_loops': changed,
         'changed_vs_base': 0 if base else changed - 40, 'prehypo_extra_U': P,
         'prehypo_extra_vs_phone_U': 0.1 if base else 0.0, 'env_3h_max_U': env3, 'env_day_pct_tdd': envd,
         'cap_binding_pct': cap, 'smb_over_maxbolus_loops': over, 'lowside_flags': lowside, 'zero_temp_vs_base': zt,
         'max_single_smb_U': 0.3, 'maxbolus_phone_min_U': 0.7, 'smb_more_vs_base': 0 if base else more,
         'smb_less_vs_base': 0 if base else less, 'is_base': base, 'flags': ['lowside_no_pred']}
    if base:
        # the floors: |base - phone| = 0.2 U on rise, 0.05 U on rest, 0.25 total, 40 loops
        m.update({'smb_emul_U': 4.2, 'smb_orig_U': 4.0, 'tbr_emul_U': 0.5, 'tbr_orig_U': 0.45, 'delta_U': 0.25,
                  'rise_emul_U': 3.2, 'rise_orig_U': 3.0, 'rest_emul_U': 1.5, 'rest_orig_U': 1.45,
                  'changed_loops': 40, 'agreement_pct': 96.0})
    m.update(extra)
    return m


def make_study_with_rows(tmp_path, name='s1', variants=VARIANTS, days=SEARCH, holdout=HOLDOUT, tags=TAGS,
                         skip=(), question='explore', failed=()):
    """A study directory with base + variant rows on every day. `skip` = (variant key, day) pairs left out
    (pending), `failed` = (variant key, day) pairs recorded as failed rows."""
    all_days = list(days) + list(holdout)
    tags = {d: t for d, t in (tags or {}).items() if d in all_days}
    sd = st.new_study(name, all_days, SPACE, holdout=list(holdout), tags=tags, question=question,
                      settings=SETTINGS, studies_dir=str(tmp_path))
    study = sd.load_study(SETTINGS)
    space = sp.ParamSpace.from_study(study, settings=SETTINGS, check_locked=False)
    n = 0
    for d in list(days) + list(holdout):
        n += 1
        sd.append(c.TrialRow(trial=n, key=c.key({}, d, study.settings_sha, study.code_sha, '', 1), phase='grid',
                             vhash=c.BASE_VHASH, variant='base', params={}, day=d, status='done',
                             code_sha=study.code_sha, settings_sha=study.settings_sha, freeze_sha='',
                             metrics=mk(base=True), control='base'))
        for key, (params, kw) in variants.items():
            if (key, d) in skip:
                continue
            n += 1
            status, metrics, error = 'done', mk(**kw), None
            if (key, d) in failed:
                status, metrics, error = 'failed', {}, 'SYNTAX: row 1 of the VDF'
            sd.append(c.TrialRow(trial=n, key=c.key(params, d, study.settings_sha, study.code_sha, '', 1), phase='grid',
                                 vhash=c.vhash(params), variant=space.variant_name(params), params=params, day=d,
                                 status=status, code_sha=study.code_sha, settings_sha=study.settings_sha,
                                 freeze_sha='', metrics=metrics, error=error))
    return sd, study


def vh(key):
    return c.vhash(VARIANTS[key][0])


# ----------------------------------------------------------------------------- trial rows -> dicts

def test_trial_dicts_relabel_base_and_drop_controls(tmp_path):
    sd, study = make_study_with_rows(tmp_path, variants={'A': VARIANTS['A']}, days=['2026-09-01'], holdout=[])
    params = VARIANTS['A'][0]
    common = dict(phase='grid', day='2026-09-01', code_sha=study.code_sha, settings_sha=study.settings_sha, freeze_sha='')
    sd.append(c.TrialRow(trial=90, key='9' * 40, vhash=c.vhash(params), variant='sentinel', params=params, status='done',
                         metrics=mk(base=True), control='sentinel', **common))
    sd.append(c.TrialRow(trial=91, key='8' * 40, vhash=c.vhash(params), variant='null', params=params, status='done',
                         metrics=mk(base=True), control='null', **common))
    sd.append(c.TrialRow(trial=92, key='7' * 40, vhash=c.vhash(params), variant='x', params=params, status='failed',
                         error='boom', **common))
    dicts = rnd.trial_dicts(sd.trials())
    assert len(dicts) == 2 and {d['variant'] for d in dicts} == {M.BASE_VARIANT, 'autoISF_max_%s__dura_ISF_weight_0.5' % PHONE_MAX}
    base = [d for d in dicts if d['variant'] == M.BASE_VARIANT]
    assert len(base) == 1 and base[0]['vhash'] == c.BASE_VHASH and base[0]['params'] == {}
    assert all(d['status'] == 'done' for d in dicts)
    assert rnd.failed_by_variant(sd.trials()) == {}            # the failed row's key has no done row, but it is not latest-by-key


# ----------------------------------------------------------------------------- verdicts

def test_verdicts_on_the_constructed_study(tmp_path):
    sd, study = make_study_with_rows(tmp_path, skip=(('tie', '2026-09-03'),))
    a = rnd.analyze(study, sd.trials(), SETTINGS)
    rep = {k: a.reports[vh(k)] for k in VARIANTS}
    assert a.base.verdict == 'base'
    assert rep['A'].verdict == 'recommended' and rep['A'].recommended and rep['A'].rank == 1
    assert rep['B'].verdict == 'recommended' and rep['B'].rank == 2
    assert rep['wild'].verdict == 'infeasible' and rep['wild'].rank is None and 'envelope' in rep['wild'].why
    assert rep['dead1'].verdict == 'dead' and rep['dead2'].verdict == 'dead'
    assert [d.key for d in a.dead] == ['profile autoISF_max'] and a.dead[0].floor_ratio == pytest.approx(1.0)
    assert 'not testable on these days' in a.dead[0].why
    assert rep['tie'].verdict == 'pending' and '2 of 3 search days' in rep['tie'].why
    # the order holds feasible variants only, best review score first
    assert a.order[:2] == [vh('A'), vh('B')] and vh('wild') not in a.order
    assert a.front == a.order                                    # explore: the front is the review order
    # ties: A and B differ by 0.05 U on rise (< 2 x 0.2) and 0.01 U on rest -> a tie within the noise floor
    assert vh('B') in rep['A'].tie_with and vh('A') in rep['B'].tie_with
    assert vh('wild') not in rep['A'].tie_with
    # strata and hold-out
    assert rep['A'].strata_ranks == {'sport': 1, 'weekend': 1, 'workday': 1}
    assert rep['A'].strata_days['workday'] == 3 and rep['A'].holdout_rank == 1 and rep['A'].holdout_total >= 2
    assert rep['wild'].holdout_rank is None                      # infeasible there too
    cand = rnd.candidate_of(rep['A'])
    assert 'rank:holdout=1' in cand.flags and 'rank:workday=1' in cand.flags and 'lowside_no_pred' in cand.flags
    assert set(cand.effect) <= set(rnd.EFFECT_KEYS) and cand.effect['rise_vs_base_U'] == -0.9
    assert cand.days == 3 and cand.tie_with == [vh('B')]


def test_tie_and_candidate_verdicts(tmp_path):
    variants = {'tie': VARIANTS['tie'],
                'cand': ({'profile autoISF_max': PHONE_MAX, 'profile dura_ISF_weight': 0.5}, dict(rise=-0.7, rest=0.0)),
                'cand2': ({'profile autoISF_max': PHONE_MAX, 'profile dura_ISF_weight': 0.6}, dict(rise=0.9, rest=-0.3))}
    sd, study = make_study_with_rows(tmp_path, variants=variants, holdout=[])
    a = rnd.analyze(study, sd.trials(), SETTINGS)
    tie = a.reports[c.vhash(variants['tie'][0])]
    assert tie.verdict == 'tie' and tie.tie_with_base and 'indistinguishable from base' in tie.why
    cand = a.reports[c.vhash(variants['cand'][0])]
    assert cand.verdict == 'candidate' and not cand.recommended     # no hold-out days: never recommended
    assert 'no hold-out days' in cand.recommend_reasons
    assert cand.why.startswith('would have asked for 0.70 U less than base per day in rising loops')
    assert '3.5 x floor' in cand.why and 'over 3 days' in cand.why
    cand2 = a.reports[c.vhash(variants['cand2'][0])]
    # rest -0.3 U is 6 x its 0.05 U floor, rise +0.9 U only 4.5 x: the rest component leads the sentence
    assert cand2.why.startswith('would have asked for 0.30 U less than base per day in the other loops (6.0 x floor)')
    assert 'rising loops +0.90 U' in cand2.why
    # dura_ISF_weight has three one-parameter points, one of them (0.9 x floor on cand2's rest) far outside the floor
    assert a.dead == [] and 'profile dura_ISF_weight' not in [d.key for d in a.dead]


def test_failed_rows_make_a_variant_infeasible_with_the_error(tmp_path):
    sd, study = make_study_with_rows(tmp_path, variants={'A': VARIANTS['A']}, holdout=[], failed=(('A', '2026-09-02'),))
    a = rnd.analyze(study, sd.trials(), SETTINGS)
    rep = a.reports[vh('A')]
    assert rep.verdict == 'infeasible' and 'the replay failed' in rep.why and 'SYNTAX' in rep.why
    assert rep.failed == {'2026-09-02': 'SYNTAX: row 1 of the VDF'}


def test_dead_parameter_needs_two_points_and_the_floor_rule(tmp_path):
    one = {'dead1': VARIANTS['dead1']}
    sd, study = make_study_with_rows(tmp_path, variants=one, holdout=[])
    a = rnd.analyze(study, sd.trials(), SETTINGS)
    assert a.dead == [] and a.reports[vh('dead1')].verdict == 'tie'          # one point: not screened, a tie
    live = {'p1': ({'profile autoISF_max': 1.3}, dict(rise=0.2)), 'p2': ({'profile autoISF_max': 1.4}, dict(rise=0.7))}
    sd, study = make_study_with_rows(tmp_path, name='s2', variants=live, holdout=[])
    a = rnd.analyze(study, sd.trials(), SETTINGS)
    assert a.dead == []                                                      # 0.7 / 0.2 = 3.5 x floor on one point
    assert a.reports[c.vhash(live['p2'][0])].verdict == 'candidate'
    assert any('profile autoISF_max' in w for w in a.warnings) is False     # 3.5 >= 2 x 1.5: no "reaches at most" note


def test_question_earlier_front_is_the_pareto_front(tmp_path):
    variants = {'up': ({'profile dura_ISF_weight': 0.5}, dict(rise=0.9, rest=0.0)),
                'up2': ({'profile dura_ISF_weight': 0.6}, dict(rise=0.6, rest=0.3)),
                'down': ({'profile dura_ISF_weight': 0.9}, dict(rise=-0.9, rest=0.0))}
    sd, study = make_study_with_rows(tmp_path, variants=variants, holdout=[], question='earlier')
    a = rnd.analyze(study, sd.trials(), SETTINGS)
    assert a.front == [c.vhash(variants['up'][0])] and a.order[0] == c.vhash(variants['up'][0])
    doc = rnd.build_round(sd, round_no=2, phase='optuna', settings=SETTINGS, analysis=a)
    assert doc.front == a.front and doc.notes[:1] != ['review order, not a ranking by outcome']


# ----------------------------------------------------------------------------- round.json and status.json

def test_produce_writes_round_and_status_within_limits(tmp_path):
    sd, study = make_study_with_rows(tmp_path)
    doc, status = rnd.produce(sd, round_no=1, phase='grid', settings=SETTINGS, now='2026-10-17T06:00:00Z')
    assert os.path.isfile(sd.round_path) and os.path.isfile(sd.status_path)
    loaded = c.Round.load(sd.round_path).validate()
    assert loaded.round == 1 and loaded.phase == 'grid' and loaded.trials == 4 + 6 * 4
    assert [v.verdict for v in loaded.variants][0] == 'base'
    assert [v.vhash for v in loaded.variants[1:1 + len(doc.front)]] == doc.front
    assert os.path.getsize(sd.round_path) <= c.ROUND_MAX_BYTES_PER_VARIANT * len(loaded.variants)
    assert set(loaded.floors) == set(SEARCH) and loaded.floors['2026-09-01']['floor_rise_U'] == 0.2
    assert loaded.dead_parameters[0].key == 'profile autoISF_max'
    assert loaded.notes[0] == 'review order, not a ranking by outcome'
    with open(sd.status_path, 'rb') as fh:
        raw = fh.read()
    assert len(raw) <= c.STATUS_MAX_BYTES
    s2 = c.Status.from_json(raw.decode('utf-8')).validate()
    assert s2.dead_parameters == ['profile autoISF_max'] and s2.top[0].vhash == c.short(vh('A'))
    assert s2.top[0].verdict == 'recommended' and s2.trials_done == 28 and len(s2.top) <= 5
    assert status.to_dict() == s2.to_dict()
    # a second produce() is idempotent
    doc2, _ = rnd.produce(sd, round_no=1, phase='grid', settings=SETTINGS, now='2026-10-17T06:00:00Z')
    assert doc2.to_dict() == doc.to_dict()


def test_round_defaults_come_from_state_json(tmp_path):
    sd, study = make_study_with_rows(tmp_path, variants={'A': VARIANTS['A']}, holdout=[])
    sd.write_state(c.State(phase='lhs', trials_done=4, queue_len=0, pid=1, heartbeat=c.now_iso(), round=7))
    doc = rnd.build_round(sd, settings=SETTINGS)
    assert doc.round == 7 and doc.phase == 'lhs'


@pytest.mark.parametrize('kind', ['line', 'card'])
def test_every_why_and_note_passes_the_vocabulary_lint(tmp_path, kind):
    sd, study = make_study_with_rows(tmp_path, skip=(('tie', '2026-09-03'),), failed=(('dead2', '2026-09-01'),))
    doc = rnd.build_round(sd, round_no=1, phase='grid', settings=SETTINGS)
    verdicts = {v.verdict for v in doc.variants}
    assert {'base', 'recommended', 'infeasible', 'dead', 'pending', 'tie'} <= verdicts | {'tie'}
    texts = [v.why for v in doc.variants] + [d.why for d in doc.dead_parameters] + doc.notes + [rnd.round_text(doc)]
    for text in texts:
        findings = lint.lint_findings(text, 'line')
        assert findings == [], (text, [str(f) for f in findings])
    assert lint.lint_findings(rnd.round_text(doc), 'card') == []            # the text carries the honesty paragraph
    assert '[recommended for review]' in rnd.round_text(doc) and '[recommended]' not in rnd.round_text(doc)


def test_cli_main_writes_both_files(tmp_path, capsys):
    sd, study = make_study_with_rows(tmp_path, variants={'A': VARIANTS['A']}, holdout=[])
    rc = rnd.main([sd.path, '--round', '3', '--phase', 'grid', '--settings', NS_SETTINGS if os.path.isfile(NS_SETTINGS) else os.devnull,
                   '--studies-dir', str(tmp_path), '--print'])
    out = capsys.readouterr().out
    assert rc == 0 and 'round.json' in out and 'status.json' in out and c.HONESTY_STATEMENT in out
    assert json.load(open(sd.round_path))['round'] == 3
    rc = rnd.main([str(tmp_path / 'nope'), '--studies-dir', str(tmp_path)])
    assert rc == st.EXIT_REFUSED
