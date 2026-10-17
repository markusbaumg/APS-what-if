"""
test_lint.py  -  backlog H-205: every may phrase passes, every may-not phrase is refused, the privacy rules refuse
their examples and accept their `passes` wording, a sample card passes and the same card with "safer" fails.
Offline, no fixtures needed.
"""
import json
import os
import subprocess
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(os.path.dirname(TESTS_DIR))
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)

from hindcast import lint                                              # noqa: E402
from hindcast.lint import (HONESTY_STATEMENT, KINDS, LintError, Finding, contains_honesty_statement,   # noqa: E402
                           lint_findings, lint_text, render_vocabulary_markdown, strip_honesty)

VOCAB = json.load(open(lint.VOCABULARY_PATH, encoding='utf-8'))
MAY = VOCAB['may']
MAY_NOT = VOCAB['may_not']
PRIVACY = VOCAB['privacy']

SAMPLE_CARD = """# Candidate 1 (review order, not a ranking by outcome)

Change for review: dura ISF weight (profile dura_ISF_weight) 0.7 -> 0.5. Current value from ns_settings.json.

Replayed over 6 days (2026-09-01..2026-09-06), hold-out 2026-09-05.
rise_vs_base_U -0.41 U (floor 0.08 U), rest_vs_base_U +0.02 U (floor 0.05 U): the variant would have asked for
less insulin in rising phases and the same elsewhere; the loop would have microbolused in 9 fewer loops.
P: no additional insulin in the two hours before a recorded low on any day. E: inside the envelope.
Cap binding 9.4 % of loops; the factor would have sat on the cap less often than base.
Largest single emulated SMB 0.6 U, phone maxBolus 1.1 U. A low followed at 09:41 on 2026-09-06 (positional).
Verdict: candidate; indistinguishable within the noise floor from candidate 3 on the weekend days (tie).
Recommended for review by you and your care team.

""" + HONESTY_STATEMENT + """

VDF:
profile     dura_ISF_weight                 0.5
"""

SAMPLE_ISSUE = """Campaign: study autoISF_oct

| parameter | range | step |
|---|---|---|
| profile autoISF_max | 1.1..1.8 | 0.05 |
| profile dura_ISF_weight | 0.3..1.0 | 0.05 |

Days: 6 search, 1 hold-out. Question: explore. Budget: 6 h, 4 workers. Phase: lhs, 48 trials done.
Verdicts so far: autoISF_max not testable on these days; dura_ISF_weight candidate. Results: see the private artifact.
"""


def with_honesty(text):
    return text + '\n\n' + HONESTY_STATEMENT


# ----------------------------------------------------------------------------- vocabulary data

def test_vocabulary_has_the_design_lists():
    for phrase in ('would have asked for', 'would have microbolused', 'would have set a zero temp',
                   'the factor would have sat on the cap', 'indistinguishable within the noise floor',
                   'not testable on these days', 'review order'):
        assert phrase in MAY
    names = {e['phrase'] for e in MAY_NOT}
    for phrase in ('better', 'best', 'optimal', 'safer', 'fewer lows', 'fewer highs', 'better control',
                   'time in range', 'HbA1c', 'hypo count', 'would have prevented'):
        assert phrase in names
    assert any('future' in n for n in names)
    assert any('recommended setting' in n for n in names)
    privacy = {e['name'] for e in PRIVACY}
    for name in ('U/day', 'mmol', 'mg/dl', 'BG-like number next to a time', 'Microbolusing', 'predBGs'):
        assert name in privacy


def test_every_entry_has_an_example_and_a_reason():
    for e in MAY_NOT + PRIVACY:
        assert e.get('example'), e
        assert e.get('why'), e
    for e in PRIVACY:
        assert e.get('passes'), e


def test_honesty_statement_is_the_design_paragraph():
    assert HONESTY_STATEMENT.startswith("Hindcast replays the loop's decisions under other settings")
    assert HONESTY_STATEMENT.endswith('not medical advice.')
    assert 'about 4 % of loops' in HONESTY_STATEMENT


# ----------------------------------------------------------------------------- may phrases

@pytest.mark.parametrize('phrase', MAY)
@pytest.mark.parametrize('kind', KINDS)
def test_may_phrase_passes_every_kind(phrase, kind):
    text = 'On the search days the variant %s.' % phrase
    if kind in ('card', 'proposal'):
        text = with_honesty(text)
    assert lint_text(text, kind=kind) is None
    assert lint_findings(text, kind=kind) == []


# ----------------------------------------------------------------------------- may-not phrases

@pytest.mark.parametrize('entry', MAY_NOT, ids=[e['phrase'] for e in MAY_NOT])
@pytest.mark.parametrize('kind', KINDS)
def test_may_not_example_is_refused_in_every_kind(entry, kind):
    text = with_honesty(entry['example'])
    with pytest.raises(LintError) as info:
        lint_text(text, kind=kind)
    err = info.value
    assert err.kind == kind
    assert err.findings[0].rule == 'may-not'
    assert err.findings[0].name == entry['phrase']
    assert err.phrase and err.phrase.lower() in entry['example'].lower()
    assert err.phrase in str(err)


@pytest.mark.parametrize('phrase', ['better', 'best', 'optimal', 'safer', 'fewer lows', 'fewer highs',
                                    'better control', 'time in range', 'HbA1c', 'hypo count',
                                    'would have prevented', 'recommended setting'])
def test_literal_design_phrase_is_refused(phrase):
    with pytest.raises(LintError):
        lint_text(with_honesty('The variant is %s.' % phrase), kind='card')


def test_matching_is_case_insensitive_and_whole_word():
    with pytest.raises(LintError):
        lint_text(with_honesty('BEST on the weekend.'))
    with pytest.raises(LintError):
        lint_text(with_honesty('Safer at night.'))
    # a longer word that contains a banned one is not the banned word
    assert lint_text(with_honesty('the safety reading is printed on every card')) is None
    assert lint_text(with_honesty('higher_ISFrange_weight stays at 0.4')) is None
    assert lint_text(with_honesty('the loop would have asked for more insulin before the recorded low')) is None


def test_recommended_needs_for_review():
    assert lint_text(with_honesty('Recommended for review: dura_ISF_weight 0.5.')) is None
    assert lint_text(with_honesty('recommended setting for your review')) is None
    with pytest.raises(LintError) as info:
        lint_text(with_honesty('We recommend dura_ISF_weight 0.5.'))
    assert info.value.findings[0].name.startswith('recommended setting')
    with pytest.raises(LintError):
        lint_text(with_honesty('Recommended: 1.4. Review it with your team.'))      # 'for review' is not in that sentence


def test_future_statements_refused_but_past_counterfactuals_pass():
    with pytest.raises(LintError):
        lint_text(with_honesty('autoISF_max 1.4 will reduce the morning rise.'))
    with pytest.raises(LintError):
        lint_text(with_honesty('this is going to help'))
    assert lint_text(with_honesty('the loop would have set a zero temp in 4 more loops')) is None


def test_glucose_outcome_claims_refused():
    for s in ('glucose would have stayed flat', 'BG would have been lower', 'leads to lower glucose',
              'no lows on the weekend', 'avoids the highs after lunch', 'improves the morning', 'worse on Sunday',
              'tighter control', '3 % more time in range', 'a lower A1c', 'hypoglycemia drops'):
        with pytest.raises(LintError, match='may not say'):
            lint_text(with_honesty(s))


def test_all_findings_reported_in_text_order():
    text = with_honesty('A better cap. Safer too. Optimal even.')
    findings = lint_findings(text)
    assert [f.name for f in findings] == ['better', 'safer', 'optimal']
    assert findings[0].start < findings[1].start < findings[2].start
    with pytest.raises(LintError, match=r'\(\+2 more\)'):
        lint_text(text)


# ----------------------------------------------------------------------------- honesty paragraph

def test_card_and_proposal_require_the_honesty_paragraph():
    for kind in ('card', 'proposal'):
        with pytest.raises(LintError, match='honesty paragraph missing'):
            lint_text('The variant would have asked for less insulin.', kind=kind)
        assert lint_text('The variant would have asked for less insulin.', kind=kind, require_honesty=False) is None
    for kind in ('issue', 'line'):
        assert lint_text('The variant would have asked for less insulin.', kind=kind) is None
        with pytest.raises(LintError, match='honesty paragraph missing'):
            lint_text('The variant would have asked for less insulin.', kind=kind, require_honesty=True)


def test_honesty_paragraph_is_recognised_whitespace_insensitively_and_stripped():
    wrapped = HONESTY_STATEMENT.replace(' says ', '\n  says ').replace(' floor ', '\n\n floor ')
    assert contains_honesty_statement(wrapped)
    assert contains_honesty_statement(HONESTY_STATEMENT.upper())
    assert not contains_honesty_statement(HONESTY_STATEMENT.replace('not medical advice', 'medical advice'))
    assert strip_honesty('x ' + wrapped + ' y').split() == ['x', 'y']
    # its own wording ('what glucose would have done', 'about 4 % of loops') never trips a rule, in any kind
    for kind in KINDS:
        assert lint_text(HONESTY_STATEMENT, kind=kind) == None  # noqa: E711


def test_honesty_paragraph_does_not_shield_text_around_it():
    with pytest.raises(LintError, match="'better'"):
        lint_text(HONESTY_STATEMENT + ' This one is better.')


# ----------------------------------------------------------------------------- the sample card

def test_sample_card_passes_and_safer_fails():
    assert lint_text(SAMPLE_CARD, kind='card') is None
    assert lint_text(SAMPLE_CARD, kind='proposal') is None
    bad = SAMPLE_CARD.replace('Verdict: candidate;', 'Verdict: candidate, safer;')
    with pytest.raises(LintError) as info:
        lint_text(bad, kind='card')
    assert info.value.phrase == 'safer'
    assert info.value.findings[0].name == 'safer'
    assert 'safer' in str(info.value)


def test_sample_card_as_issue_is_refused_for_privacy_only():
    findings = lint_findings(SAMPLE_CARD, kind='issue')
    assert findings and all(f.rule == 'privacy' for f in findings)
    assert {'insulin units', 'loop count', 'BG-like number next to a time'} & {f.name for f in findings}


# ----------------------------------------------------------------------------- privacy rules (issue kind)

@pytest.mark.parametrize('entry', PRIVACY, ids=[e['name'] for e in PRIVACY])
def test_privacy_example_refused_in_issue_only(entry):
    with pytest.raises(LintError) as info:
        lint_text(entry['example'], kind='issue')
    assert any(f.rule == 'privacy' and f.name == entry['name'] for f in info.value.findings)
    for kind in ('card', 'proposal', 'line'):
        text = with_honesty(entry['example']) if kind != 'line' else entry['example']
        assert not [f for f in lint_findings(text, kind=kind) if f.rule == 'privacy']


@pytest.mark.parametrize('entry', PRIVACY, ids=[e['name'] for e in PRIVACY])
def test_privacy_passes_wording_accepted(entry):
    assert [f for f in lint_findings(entry['passes'], kind='issue') if f.name == entry['name']] == []


def test_privacy_specifics():
    ok = ['autoISF_max 1.3 -> 1.5 since the second day', 'the 2026-09-06T09:00:37Z loop differs',
          'window 2026-09-06T06:00:00Z_2026-09-06T09:00:00Z', 'within 0.05 U of the phone', 'started 19:42, 240 trials done',
          'replayed over 7 days; 16 variants; 4 workers; cap binding 9.7 % of loops', 'profile_history change at 20:23 local',
          'a low followed at 09:41 (positional, see the artifact)', 'autoISF_max 1.5, dura_ISF_weight 0.7', '12 trials failed']
    for s in ok:
        assert lint_findings(s, kind='issue') == [], s
    bad = {'SMB total +4.05 U/day': 'U/day', 'TDD 38': 'U/day', 'minGuard 3.6 mmol': 'mmol', 'BG 65 mg/dl': 'mg/dl',
           'BG 65 mg / dl': 'mg/dl', 'at 09:00 it was 5.4': 'BG-like number next to a time',
           '180 at 09:00': 'BG-like number next to a time', '09:00:37Z, 180 and falling': 'BG-like number next to a time',
           'Microbolusing 0.3U': 'Microbolusing', 'insulinReq: 0.45': 'Microbolusing', 'minGuardBG 3.6 < 3.8': 'Microbolusing',
           'predBGs: [120, 118]': 'predBGs', 'UAMpredBGs': 'predBGs', 'asks for 2.65 U more': 'insulin units',
           '1 U more': 'insulin units', 'basal 0.9 U/h': 'insulin units', '1311 loops': 'loop count', '1 311 loops': 'loop count',
           'loops: 1311': 'loop count', '12 more loops': 'loop count', '45 g carbs': 'carbs', '12 % > 180': 'glucose statistic',
           'pct_lt70 2.1': 'glucose statistic', 'within 0.5 U of the phone': 'insulin units',
           'within 2.65 U': 'insulin units', 'differs by 0.05 U': 'insulin units'}
    for s, name in bad.items():
        assert name in {f.name for f in lint_findings(s, kind='issue')}, s


# the round-comment leak (gh.round_body posted candidate `why` lines with amounts under the old 0.5 U threshold):
# every insulin amount, U per day, TDD share, glucose unit and loop count is refused in an issue, however small
REFUSED_IN_ISSUE = [
    ('would have asked for 0.06 U less than base per day in the other loops (1.3 x floor)', 'insulin units'),
    ('would have asked for 0.06 U less than base per day in the other loops (1.3 x floor)', 'U/day'),
    ('rising loops -0.02 U', 'insulin units'),
    ('the other loops +0.35 U', 'insulin units'),
    ('0.3U', 'insulin units'),
    ('0,3 U', 'insulin units'),
    ('basal 0.45 U/h', 'insulin units'),
    ('\u2212 0.4 U', 'insulin units'),
    ('3 U', 'insulin units'),
    ('U per day', 'U/day'),
    ('2.46 U more than base per day', 'U/day'),
    ('+4.05 U / day', 'U/day'),
    ('16.4 % of the phone\'s TDD vs base', 'U/day'),
    ('3 % of total daily insulin', 'U/day'),
    ('the total daily dose', 'U/day'),
    ('3.6 mmol', 'mmol'),
    ('3.6mmol/L', 'mmol'),
    ('65mg/dl', 'mg/dl'),
    ('65 mg/dL', 'mg/dl'),
    ('BG 65 mgdl', 'mg/dl'),
    ('114 loops changed', 'loop count'),
    ('loops changed: 114', 'loop count'),
    ('loops differ in 12', 'loop count'),
    ('1 loop', 'loop count'),
    ('+12 loops', 'loop count'),
    ('12 of 1311 loops', 'loop count'),
    ('1,311 loops', 'loop count'),
    ('12 other loops', 'loop count'),
    ('loop count 1311 differs from base 1310', 'loop count'),
    ('a loop count of 1311', 'loop count'),
]


@pytest.mark.parametrize('text,name', REFUSED_IN_ISSUE, ids=['%d-%s' % (i, n) for i, (_, n) in enumerate(REFUSED_IN_ISSUE)])
def test_issue_privacy_refuses_every_quantity(text, name):
    assert name in {f.name for f in lint_findings(text, kind='issue')}, text


ALLOWED_IN_ISSUE = [
    'would have asked for less insulin than base in rising loops (2.9 x the floor) over 1 day; the other loops: less',
    'asked for less insulin in rising loops, 2.9 x the floor',
    'UAM', '2 UAM loops differ', 'enable_UAM true', 'the UAM prediction',
    'SMB within 0.05 U', 'TBR rate within 0.05 U/h', 'insulinReq within 0.05 U >= 99 %',
    'max_single_smb_U', 'floor_rise_U', 'rise_vs_base_U and rest_vs_base_U', 'parameter U', 'the U/h unit is in the artifact',
    'the 09:00 loop differs', 'the 2026-09-06T09:00:37Z loop differs, see the artifact', 'the loop differs',
    'loops differ from base, see the artifact', 'cap binding 9.7 % of loops', 'agreement 97.483 %',
    'autoISF_max 1.3 -> 1.5', 'over 1 day', '240 trials done', 'smb_delivery_ratio_bg_range 36',
    'the loop would have disabled SMB in N more loops', 'commolution',
]


@pytest.mark.parametrize('text', ALLOWED_IN_ISSUE)
def test_issue_privacy_false_positives(text):
    assert lint_findings(text, kind='issue') == [], text


def test_sample_issue_passes():
    assert lint_text(SAMPLE_ISSUE, kind='issue') is None


# ----------------------------------------------------------------------------- ignore, kinds, API shape

def test_ignore_skips_named_rules_only():
    body = 'acceptance: a 3.5 mmol reading 90 min before; +4 U/day bias; or better'
    assert lint_text(body, kind='issue', ignore=('mmol', 'U/day', 'insulin units', 'better')) is None
    with pytest.raises(LintError, match='mmol'):
        lint_text(body, kind='issue', ignore=('U/day', 'insulin units', 'better'))
    assert lint_text('card without the paragraph', kind='card', ignore=('honesty',)) is None


def test_backlog_style_body_needs_ignore():
    body = ('Body: synthetic series tests for P and E (a +0.5 U SMB 90 min before a 3.5 mmol reading -> P = 0.5).\n'
            'Acceptance: SMB exact agreement rises; bias (35 loops, +4 U/day on the first day).')
    names = {f.name for f in lint_findings(body, kind='issue')}
    assert {'mmol', 'U/day', 'loop count', 'insulin units'} <= names


def test_unknown_kind_and_non_string_rejected():
    with pytest.raises(ValueError, match='lint kind'):
        lint_text('x', kind='page')
    with pytest.raises(TypeError):
        lint_text(None, kind='card')


def test_gh_py_calling_convention():
    """gh.default_linter() calls lint_text(body, kind='issue') and treats a raise as the refusal, any truthy
    non-bool return as a problem: success must therefore be None."""
    assert lint_text('Phase lhs, 48 trials done; see the artifact.', kind='issue') is None
    try:
        lint_text('SMB total +4 U/day', kind='issue')
    except Exception as exc:                                  # the way gh.py catches it
        assert 'U/day' in str(exc)
    else:
        pytest.fail('no raise')


def test_finding_is_a_frozen_dataclass_with_a_readable_str():
    f = lint_findings(with_honesty('the best day'))[0]
    assert isinstance(f, Finding)
    assert (f.rule, f.name, f.phrase) == ('may-not', 'best', 'best')
    assert 'may not say' in str(f) and 'best' in str(f)
    with pytest.raises(Exception):
        f.phrase = 'x'


def test_render_vocabulary_markdown_lists_everything():
    md = render_vocabulary_markdown()
    assert md.startswith('# Hindcast vocabulary')
    assert HONESTY_STATEMENT in md
    for p in MAY:
        assert '"%s"' % p in md
    for e in MAY_NOT:
        assert '**%s**' % e['phrase'] in md
    for e in PRIVACY:
        assert '**%s**' % e['name'] in md
    # the generated reference must itself be clean vocabulary apart from the rule names it documents
    assert 'do not edit' in md


def test_cli(tmp_path):
    good = tmp_path / 'good.md'
    good.write_text(SAMPLE_CARD, encoding='utf-8')
    bad = tmp_path / 'bad.md'
    bad.write_text(SAMPLE_CARD.replace('candidate;', 'safer;'), encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=SOFTWARE_DIR)
    r = subprocess.run([sys.executable, '-m', 'hindcast.lint', '--kind', 'card', str(good)], capture_output=True, text=True, env=env, cwd=SOFTWARE_DIR)
    assert r.returncode == 0 and 'ok (card)' in r.stdout
    r = subprocess.run([sys.executable, '-m', 'hindcast.lint', '--kind', 'card', str(good), str(bad)], capture_output=True, text=True, env=env, cwd=SOFTWARE_DIR)
    assert r.returncode == 1 and 'safer' in r.stderr
    r = subprocess.run([sys.executable, '-m', 'hindcast.lint', '--markdown'], capture_output=True, text=True, env=env, cwd=SOFTWARE_DIR)
    assert r.returncode == 0 and r.stdout.startswith('# Hindcast vocabulary')


@pytest.mark.parametrize('text', ['padding-bottom: env(safe-area-inset-bottom);',
                                  'padding: env(SAFE-AREA-INSET-TOP) 16px', 'the safe-area inset of the phone'])
def test_css_safe_area_is_not_a_safety_claim(text):
    assert lint_findings(text, 'line') == []


@pytest.mark.parametrize('text', ['this is safer', 'a safe setting', 'the safest cap', 'safe-ish values',
                                  'Safe.', 'safer-area'])
def test_safe_words_are_still_refused(text):
    assert [f.name for f in lint_findings(text, 'line')] == ['safer']
