"""
test_cards.py  -  H-206: candidate cards (hindcast/cards.py): the documented wording, preference names from
prefs_map.json (never guessed), the VDF text, the lint gate (a card with a forbidden word is not written),
selection by rank / vhash, the CLI through `hc card`, and - when the git-ignored results/example_2026-09-06 run is
present - a card for `dura_ISF_weight 0.5` from the five fixture CSVs.
"""
import os

import pytest

from hindcast import cards
from hindcast import cli
from hindcast import contracts as c
from hindcast import lint
from hindcast import metrics as M
from hindcast import round as rnd
from hindcast import space as sp
from hindcast import study as st
from hindcast.tests.conftest import NS_SETTINGS
from hindcast.tests.test_round import PHONE_DURA, PHONE_MAX, SETTINGS, VARIANTS, make_study_with_rows, vh

SETTINGS_ARG = NS_SETTINGS if os.path.isfile(NS_SETTINGS) else os.devnull


@pytest.fixture
def analysed(tmp_path):
    sd, study = make_study_with_rows(tmp_path)
    return sd, study, cards.analysis_of(sd, SETTINGS)


# ----------------------------------------------------------------------------- wording

def test_card_text_matches_the_documented_wording(analysed):
    sd, study, a = analysed
    rep = a.reports[vh('A')]
    text = cards.card_text(rep, a, SETTINGS)
    assert text.startswith(f'# Candidate 1: dura_ISF_weight 0.5 (phone: {cards.fmt_value(PHONE_DURA)})')
    assert 'autoISF_max' not in text.splitlines()[0]                        # a key at the phone's value is not in the title
    assert 'verdict: **recommended for review**' in text
    assert 'the loop would have asked for 0.88 U less per day than base' in text
    assert '- rising loops: -0.90 U vs base per day; floor 0.20 U; 4.5 x floor' in text
    assert '- the other loops: +0.02 U vs base per day; floor 0.05 U; 0.4 x floor (tie: inside 2 x floor)' in text
    assert '123 loops with a smaller SMB than base and 9 with a larger one (over all 3 days)' in text
    assert 'Low-side flags: 0 loops' in text
    assert 'P (insulin before a recorded low): within the limit on every day' in text
    assert 'E (validity envelope): inside on every day' in text
    assert f'the factor sat on a cap in 10.0 % of loops (autoISF_max {cards.fmt_value(PHONE_MAX)})' in text
    assert 'Largest single emulated SMB: 0.30 U; the phone\'s maxBolus on these days was at least 0.70 U' in text
    assert '| workday | 3 | 1 |' in text and '| sport | 1 | 1 |' in text
    assert 'Hold-out days (2026-09-05): review order 1 of' in text
    assert 'Leave-one-day-out: in the top 3 on 100 % of 3 folds' in text
    assert 'Not distinguishable from candidate 2 (' in text and 'within the noise floor.' in text
    assert c.HONESTY_STATEMENT in text
    assert '| dura ISF weight | `profile dura_ISF_weight` |' in text          # the AAPS preference title
    assert '| autoISF max | `profile autoISF_max` |' in text
    assert '- 3 search days: 2026-09-01 (workday)' in text and '- 1 hold-out day: 2026-09-05 (weekend, sport)' in text
    vdf = text.split('## VDF')[1]
    assert 'profile      dura_ISF_weight                  0.5' in vdf
    assert sp.parse_vdf_text(cards.vdf_text_of(rep, a)) == rep.params
    assert lint.lint_findings(text, 'card') == []


def test_cards_for_every_verdict_pass_the_lint(analysed):
    sd, study, a = analysed
    for rep in a.ordered():
        text = cards.card_text(rep, a, SETTINGS)
        findings = lint.lint_findings(text, 'card')
        assert findings == [], (rep.variant, [str(f) for f in findings])
        assert st.verdict_label(rep.verdict) in text
    wild = a.reports[vh('wild')]
    text = cards.card_text(wild, a, SETTINGS)
    assert text.startswith('# Variant ' + c.short(vh('wild'))) and 'out of envelope' in text
    assert 'no review order (verdict infeasible)' in text
    dead = a.reports[vh('dead1')]
    assert 'not testable on these days' in cards.card_text(dead, a, SETTINGS)


def test_pref_label_never_guesses():
    prefs = {'keys': {'autoISF_max': {'title': 'autoISF max', 'unit': 'factor'},
                      'pp_ISF_hours': {'title': None, 'unit': 'h', 'note': 'title not verified'}}}
    assert cards.pref_label('profile autoISF_max', prefs) == ('autoISF max', 'factor', True)
    assert cards.pref_label('profile pp_ISF_hours', prefs) == ('pp_ISF_hours', 'h', False)
    assert cards.pref_label('profile no_such_key', prefs) == ('no_such_key', '', False)
    assert cards.pref_entry('new_parameter autoISF_max', prefs) is None
    real = cards.load_prefs_map()
    assert cards.pref_label('profile dura_ISF_weight', real)[0] == 'dura ISF weight'
    assert 'activity_ratio' not in real['keys']


def test_unverified_title_is_listed_on_the_card(tmp_path):
    variants = {'v': ({'profile pp_ISF_hours': 3}, dict(rise=-0.7))}
    sd = st.new_study('u1', ['2026-09-01'], {'profile pp_ISF_hours': {'type': 'int', 'low': 0, 'high': 6, 'step': 1}},
                      settings=SETTINGS, studies_dir=str(tmp_path))
    study = sd.load_study(SETTINGS)
    from hindcast.tests.test_round import mk
    for i, (params, metrics) in enumerate(((({}, mk(base=True))), (variants['v'][0], mk(rise=-0.7)))):
        sd.append(c.TrialRow(trial=i, key=c.key(params, '2026-09-01', '', '', '', 1), phase='grid', vhash=c.vhash(params),
                             variant='base' if not params else 'pp_ISF_hours_3', params=params, day='2026-09-01',
                             status='done', code_sha='', settings_sha='', freeze_sha='', metrics=metrics,
                             control='base' if not params else None))
    a = cards.analysis_of(sd, SETTINGS)
    text = cards.card_text(a.reports[c.vhash(variants['v'][0])], a, SETTINGS)
    assert '| pp_ISF_hours | `profile pp_ISF_hours` |' in text
    assert 'No verified preference title in prefs_map.json for: pp_ISF_hours' in text
    assert lint.lint_findings(text, 'card') == []


# ----------------------------------------------------------------------------- writing and the lint gate

def test_write_cards_writes_md_and_vdf(analysed):
    sd, study, a = analysed
    written, refused = cards.write_cards(sd, settings=SETTINGS, analysis=a)
    assert refused == [] and [w[0] for w in written] == ['1', '2', '3', '4', '5']       # the ranked variants, base never
    for label, md, vdf in written:
        assert os.path.isfile(md) and os.path.isfile(vdf) and md.endswith(f'cards{os.sep}{label}.md')
        with open(md, encoding='utf-8') as fh:
            assert c.HONESTY_STATEMENT in fh.read()
    with open(written[0][2], encoding='utf-8') as fh:
        assert sp.parse_vdf_text(fh.read()) == a.reports[vh('A')].params
    written, _ = cards.write_cards(sd, ranks=[2], vhashes=[c.short(vh('wild'))], settings=SETTINGS, analysis=a,
                                   out_dir=str(sd.path + '/elsewhere'))
    assert [w[0] for w in written] == ['2', c.short(vh('wild'))]
    with pytest.raises(cards.CardError):
        cards.select_reports(a, ranks=[99])
    with pytest.raises(cards.CardError):
        cards.select_reports(a, vhashes=['zzzz'])


def test_a_card_that_fails_the_lint_is_not_written(analysed, monkeypatch, tmp_path):
    sd, study, a = analysed
    original = cards.card_text

    def tainted(report, analysis, settings=None, prefs_map=None, number=None):
        text = original(report, analysis, settings, prefs_map, number)
        return text.replace('## Ties', 'This variant is safer and gives fewer lows.\n\n## Ties') if report.rank == 1 else text
    monkeypatch.setattr(cards, 'card_text', tainted)
    out_dir = str(tmp_path / 'cards_out')
    written, refused = cards.write_cards(sd, ranks=[1, 2], settings=SETTINGS, analysis=a, out_dir=out_dir)
    assert [w[0] for w in written] == ['2'] and len(refused) == 1 and refused[0][0] == '1'
    assert 'safer' in refused[0][1] and not os.path.exists(os.path.join(out_dir, '1.md'))
    assert not os.path.exists(os.path.join(out_dir, '1.vdf'))
    # a card without the honesty paragraph is refused too
    with pytest.raises(cards.CardError):
        cards.write_card(os.path.join(out_dir, 'x.md'), '# Candidate 9\n\nwould have asked for less.\n')
    assert not os.path.exists(os.path.join(out_dir, 'x.md'))


def test_hc_card_cli(tmp_path, capsys):
    sd, study = make_study_with_rows(tmp_path, name='clistudy')
    rc = cli.main(['card', 'clistudy', '--rank', '1', '--studies-dir', str(tmp_path), '--settings', SETTINGS_ARG])
    out = capsys.readouterr().out
    assert rc == 0 and 'card 1:' in out and c.HONESTY_STATEMENT in out
    assert os.path.isfile(os.path.join(sd.path, 'cards', '1.md'))
    rc = cli.main(['card', 'nostudy', '--studies-dir', str(tmp_path), '--settings', SETTINGS_ARG])
    assert rc == st.EXIT_REFUSED
    rc = cli.main(['card', 'clistudy', '--rank', '42', '--studies-dir', str(tmp_path), '--settings', SETTINGS_ARG])
    assert rc == st.EXIT_REFUSED and 'refused' in capsys.readouterr().err


# ----------------------------------------------------------------------------- the fixture CSVs

def test_card_from_the_example_results(example_csvs, example_vdfs, tmp_path):
    """The documented example: dura_ISF_weight 0.5 asks for less in rising loops; its card from the five CSVs."""
    settings = SETTINGS
    base_lines = []
    day = '2026-09-06'
    grid = {'autoISF_max_1.3__dura_ISF_weight_0.5': {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5},
            'autoISF_max_1.3__dura_ISF_weight_0.7': {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.7},
            'autoISF_max_1.5__dura_ISF_weight_0.5': {'profile autoISF_max': 1.5, 'profile dura_ISF_weight': 0.5},
            'autoISF_max_1.5__dura_ISF_weight_0.7': {'profile autoISF_max': 1.5, 'profile dura_ISF_weight': 0.7}}
    sd = st.new_study('example', [day], {'profile autoISF_max': {'type': 'float', 'low': 1.3, 'high': 1.5, 'step': 0.1},
                                          'profile dura_ISF_weight': {'type': 'float', 'low': 0.5, 'high': 0.7, 'step': 0.1}},
                      settings=settings, studies_dir=str(tmp_path))
    study = sd.load_study(settings)
    base_csv = example_csvs['base']
    rows = []
    for i, (variant, csv) in enumerate(example_csvs.items()):
        params = grid.get(variant, {})
        cap = M.effective_cap([(k, v) for k, v in params.items()], base_lines, settings, 'autoISF_max')
        metrics = M.trial_metrics(csv, base=None if variant == 'base' else base_csv, autoisf_max=cap)
        rows.append(c.TrialRow(trial=i, key=c.key(params, day, study.settings_sha, study.code_sha, '', 1), phase='grid',
                               vhash=c.vhash(params), variant=variant, params=params, day=day, status='done',
                               code_sha=study.code_sha, settings_sha=study.settings_sha, freeze_sha='', metrics=metrics,
                               control='base' if variant == 'base' else None))
    for r in rows:
        sd.append(r)
    a = cards.analysis_of(sd, settings)
    rep = a.reports[c.vhash(grid['autoISF_max_1.3__dura_ISF_weight_0.5'])]
    m = rep.per_day[day]
    assert m['rise_vs_base_U'] < -0.3 and abs(m['rest_vs_base_U']) < 0.05        # the documented table: -0.4 U, all in rise
    text = cards.card_text(rep, a, settings)
    assert 'dura_ISF_weight 0.5 (phone: ' in text.splitlines()[0]
    assert 'the loop would have asked for' in text and 'less per day than base' in text
    assert f'{int(m["smb_less_vs_base"])} loops with a smaller SMB than base and {int(m["smb_more_vs_base"])} with a larger one' in text
    assert 'the factor sat on a cap in' in text
    assert lint.lint_findings(text, 'card') == []
    written, refused = cards.write_cards(sd, settings=settings, analysis=a, vhashes=[rep.vhash])
    assert refused == [] and len(written) == 1
    doc, status = rnd.produce(sd, round_no=1, phase='grid', settings=settings)
    assert any(v.vhash == rep.vhash for v in doc.variants) and os.path.getsize(sd.status_path) <= 2048


def test_unlocked_study_card_puts_the_weak_evidence_caveat_under_the_honesty_statement(analysed):
    sd, study, a = analysed
    rep = a.reports[vh('A')]
    assert cards.unlock_lines(a.study) == []
    a.study.unlocked = ['profile sens']
    a.study.unlock_reason = 'a profile scale study, reviewed by hand'
    text = cards.card_text(rep, a, settings=SETTINGS, prefs_map={})
    honesty = text.index(c.HONESTY_STATEMENT)
    caveat = a.study.unlock_caveat()
    weak = text.index(caveat[0].upper() + caveat[1:])
    glucose = text.index(c.NO_GLUCOSE_RESPONSE.replace('this page', 'this card'))
    assert honesty < weak < glucose < text.index('## VDF'), 'caveat first under the honesty statement'
    assert 'so this card shows only where' in text and 'so this page shows' not in text
    cards.lint_card(text)
