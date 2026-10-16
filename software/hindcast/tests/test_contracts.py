"""Tests for hindcast/contracts.py (backlog H-001): round trips, validation, hashes, schema files, size limits."""
import json
import os

import pytest

from hindcast import contracts as c
from hindcast.contracts import (Budget, Candidate, ContractError, DeadParameter, Objective, Phase, Round, SpaceParam,
                                State, Status, Study, StudyDay, TopItem, TrialRow)

SHA = '0' * 40


def make_study(**over):
    kw = dict(name='autoISF_oct', created='2026-10-16T19:10:00Z', source='ns:', host='ns.vivikusi.ch',
              settings_sha=SHA, code_sha=SHA,
              days=[StudyDay('2026-09-06', tags=['workday']), StudyDay('2026-09-05', 'holdout', ['weekend', 'sport'])],
              space={'profile autoISF_max': SpaceParam('float', 1.1, 1.8, 0.05),
                     'profile dura_ISF_weight': SpaceParam('float', 0.3, 1.0, 0.05),
                     'profile enable_pp_ISF': SpaceParam('bool')},
              constraints=[['profile autoISF_max', '<=', 'profile dura_ISF_weight']],
              phases=[Phase('oaat', points=5), Phase('lhs', n=24), Phase('halving', rungs=[2, 5, 'all']),
                      Phase('optuna', sampler='nsga2', n=200), Phase('validate', top=8, days='holdout')],
              budget=Budget(6.0, 4), sentinel_every=25)
    kw.update(over)
    return Study(**kw)


def make_row(**over):
    params = {'profile autoISF_max': 1.45, 'profile dura_ISF_weight': 0.55}
    kw = dict(trial=37, key=c.key(params, '2026-09-06', SHA, SHA, SHA, 1), phase='lhs', vhash=c.vhash(params),
              variant='autoISF_max_1.45__dura_ISF_weight_0.55', params=params, day='2026-09-06', status='done',
              seconds=2.7, worker=3, code_sha=SHA, settings_sha=SHA, freeze_sha=SHA,
              metrics={'loops': 1311, 'changed_loops': 97, 'rise_vs_base_U': -0.41, 'rest_vs_base_U': 0.02},
              feasible=True, files={'series': 'series/2026-09-06/%s.json' % c.vhash(params)})
    kw.update(over)
    return TrialRow(**kw)


def make_candidate(i):
    params = {'profile autoISF_max': 1.1 + 0.05 * i, 'profile dura_ISF_weight': 0.3 + 0.05 * i}
    return Candidate(vhash=c.vhash(params), variant='autoISF_max_%.2f__dura_ISF_weight_%.2f' % (1.1 + 0.05 * i, 0.3 + 0.05 * i),
                     verdict='candidate' if i % 2 else 'tie', why='effect 3.1 x floor on rise, same sign on 4 of 5 days',
                     params=params, rank=i + 1, days=5, feasible=True,
                     effect={'rise_vs_base_U': -0.41 - i / 10, 'rest_vs_base_U': 0.02, 'changed_vs_base': 12 + i,
                             'lowside_flags': 0, 'prehypo_extra_U': 0.0, 'env_3h_max_U': 0.35, 'env_day_pct_tdd': 1.6,
                             'cap_binding_pct': 9.4, 'max_single_smb_U': 0.7},
                     flags=[], tie_with=[c.vhash({'profile autoISF_max': 1.3})] if i % 3 == 0 else [])


def make_top(i):
    cand = make_candidate(i)
    return TopItem(vhash=cand.vhash, variant=cand.variant, verdict=cand.verdict, why=cand.why, rank=i + 1,
                   effect={'rise_vs_base_U': -0.41 - i / 10, 'rest_vs_base_U': 0.02, 'changed_vs_base': 12 + i})


# ----------------------------------------------------------------------------- round trips

def test_study_round_trip():
    s = make_study().validate()
    text = s.to_json()
    again = Study.from_json(text)
    assert again == s
    assert again.to_json() == text
    assert json.loads(text)['phases'][0] == {'kind': 'oaat', 'state': 'pending', 'points': 5}   # None options omitted


def test_study_new_fills_stamps(tmp_path):
    s = Study.new('demo', ['2026-09-06'], {'profile autoISF_max': {'type': 'float', 'low': 1.1, 'high': 1.8, 'step': 0.05}})
    s.validate()
    assert len(s.code_sha) == 40 and s.created.endswith('Z')
    assert set(s.core_versions) >= {'emulator_core.py', 'determine_basal.py'}
    assert s.search_days() == ['2026-09-06'] and s.holdout_days() == []


def test_trial_row_round_trip_and_jsonl(tmp_path):
    row = make_row().validate()
    line = row.to_jsonl_line()
    assert line.endswith('\n') and len(line) < 4096
    path = tmp_path / 'trials.jsonl'
    c.append_trial(path, row)
    c.append_trial(path, make_row(trial=38, day='2026-09-05', key=SHA))
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write('{"trial": 39, "truncated')                                 # kill -9 mid-write
    rows = c.read_trials(path)
    assert [r.trial for r in rows] == [37, 38]
    assert rows[0] == row


def test_state_status_round_round_trips():
    st = State(phase='lhs', trials_done=12, queue_len=30, pid=4711, heartbeat='2026-10-16T19:10:00Z',
               stale_context=[], last_key=SHA, budget_left_s=1200.0, disk_free_mb=52000.0).validate()
    assert State.from_json(st.to_json()) == st
    status = Status(study='autoISF_oct', question='explore', phase='lhs', trials_done=12, trials_planned=60, running=True,
                    heartbeat='2026-10-16T19:10:00Z', floor={'smb_U': 4.05, 'ins_req_U': 0.3, 'tbr_U': 0.1,
                                                            'loops': 51, 'agreement_pct': 96.1},
                    days={'2026-09-06': 'search', '2026-09-05': 'holdout'}, top=[make_top(0)]).validate()
    assert Status.from_json(status.to_json()) == status
    rnd = Round(round=3, phase='lhs', started='2026-10-16T19:10:00Z', trials=24, variants=[make_candidate(i) for i in range(3)],
                dead_parameters=[DeadParameter('profile enable_pp_ISF', 'all points inside 1.5 x floor on 3 days', 0.4)],
                front=[make_candidate(1).vhash]).validate()
    assert Round.from_json(rnd.to_json()) == rnd


def test_save_is_atomic_and_loadable(tmp_path):
    st = State(phase='floor', trials_done=0, queue_len=5, pid=1, heartbeat='2026-10-16T19:10:00Z')
    path = tmp_path / 'STATE.json'
    st.save(path)
    assert State.load(path) == st
    assert not [p for p in os.listdir(tmp_path) if p.endswith('.tmp')]


# ----------------------------------------------------------------------------- validation

def test_locked_key_is_refused_with_its_name():
    s = make_study()
    s.space['profile max_iob'] = SpaceParam('float', 5, 9, 1)
    with pytest.raises(ContractError) as e:
        s.validate()
    assert 'profile max_iob' in str(e.value) and 'locked' in str(e.value)


@pytest.mark.parametrize('name', ['profile sens', 'profile carb_ratio', 'profile current_basal', 'profile dia',
                                  'profile bolus_increment', 'profile lgsThreshold', 'profile max_basal',
                                  'new_parameter SMBRatio', 'new_parameter AAPS_Version'])
def test_locked_list(name):
    assert c.locked_reason(name) is not None


@pytest.mark.parametrize('name', ['profile autoISF_max', 'profile dura_ISF_weight', 'profile smb_delivery_ratio',
                                  'profile enable_pp_ISF', 'profile iob_threshold_percent'])
def test_searchable_keys(name):
    assert c.locked_reason(name) is None


def test_locked_reason_reads_settings_safety_section():
    settings = {'safety': {'brand_new_guard': 1, '_about': 'x'}}
    assert c.locked_reason('profile brand_new_guard', settings)
    assert c.locked_reason('profile brand_new_guard') is None
    s = make_study()
    s.space['profile brand_new_guard'] = SpaceParam('int', 0, 3, 1)
    s.validate()                                                             # unknown to the static list
    with pytest.raises(ContractError, match='brand_new_guard'):
        s.validate(settings)


def test_non_profile_groups_and_bad_names_refused():
    assert 'input' in c.locked_reason('iob_data iob')
    assert 'unknown VDF group' in c.locked_reason('nonsense key')
    assert '<array> <item>' in c.locked_reason('autoISF_max')


def test_study_semantic_problems_are_all_named():
    s = make_study(days=[StudyDay('2026-09-06'), StudyDay('2026-09-06'), StudyDay('bad-day')], sentinel_every=0,
                   constraints=[['profile autoISF_max', '>', 'profile nope']],
                   base_vdf_lines=['profile autoISF_max 1.3 ### fixed'])
    s.space['profile x'] = SpaceParam('float', 2, 1, 0)
    s.space['profile y'] = SpaceParam('choice')
    problems = s.problems()
    text = '\n'.join(problems)
    for needle in ('listed twice', 'bad-day', 'sentinel_every', 'operator ">"', 'profile nope', 'sets a searched parameter',
                   'low 2 must be below high 1', 'step must be positive', 'non-empty list of choices'):
        assert needle in text, needle


def test_from_dict_is_strict():
    d = make_study().to_dict()
    d['surprise'] = 1
    with pytest.raises(ContractError, match='surprise'):
        Study.from_dict(d)
    assert Study.from_dict(d, strict=False).name == 'autoISF_oct'
    d = make_study().to_dict()
    d['question'] = 'better'
    with pytest.raises(ContractError, match='question'):
        Study.from_dict(d)
    d = make_study().to_dict()
    d['budget']['workers'] = 'four'
    with pytest.raises(ContractError, match='budget.workers'):
        Study.from_dict(d)
    d = make_study().to_dict()
    del d['days']
    with pytest.raises(ContractError, match='missing required'):
        Study.from_dict(d)


def test_trial_row_problems():
    with pytest.raises(ContractError, match='vhash does not match'):
        make_row(vhash=SHA).validate()
    with pytest.raises(ContractError, match='error text'):
        make_row(status='failed', metrics={}).validate()
    with pytest.raises(ContractError, match='needs metrics'):
        make_row(metrics={}).validate()
    make_row(status='failed', metrics={}, error='SYNTAX: row 1 did not eval').validate()
    with pytest.raises(ContractError, match='exceeds'):
        make_row(metrics={'big': 'x' * 5000}).to_jsonl_line()


def test_state_exit_code_rule():
    State(phase='lhs', trials_done=1, queue_len=0, pid=1, heartbeat='x', exit_code=2).validate()
    with pytest.raises(ContractError, match='exit_code'):
        State(phase='lhs', trials_done=1, queue_len=0, pid=1, heartbeat='x', exit_code=1).validate()


def test_nan_never_reaches_a_file():
    row = make_row(metrics={'loops': 10, 'x': float('nan')})
    assert json.loads(row.to_compact_json())['metrics']['x'] is None


# ----------------------------------------------------------------------------- hashes

def test_vhash_and_key_are_canonical():
    a = {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5}
    b = {'profile dura_ISF_weight': 0.50, 'profile autoISF_max': 1.30}
    assert c.vhash(a) == c.vhash(b) and len(c.vhash(a)) == 40
    assert c.vhash({'profile autoISF_max': 1}) != c.vhash({'profile autoISF_max': 1.0})       # the VDF rows differ too
    assert c.vhash({}) == c.BASE_VHASH
    assert c.canonical_params({'profile enable_pp_ISF': True, 'profile autoISF_max': 1.3}) == \
        'profile autoISF_max 1.3\nprofile enable_pp_ISF True'
    k = c.key(a, '2026-09-06', 'S', 'C', 'F', 1)
    assert k == c.key(b, '2026-09-06', 'S', 'C', 'F', 1)
    assert k != c.key(a, '2026-09-05', 'S', 'C', 'F', 1)
    assert k != c.key(a, '2026-09-06', 'S', 'C', 'F', 2)
    assert k != c.key(a, '2026-09-06', 'S', 'C2', 'F', 1)
    assert c.short(k) == k[:12]


def test_code_sha_covers_core_and_ns_modules(tmp_path):
    base = c.code_sha()
    assert len(base) == 40 and base == c.code_sha()
    for name in ('determine_basal.py', 'emulator_core.py', 'ns_source.py', 'ns_iob.py'):
        (tmp_path / name).write_text('x')
    before = c.code_sha(str(tmp_path))
    (tmp_path / 'ns_iob.py').write_text('y')
    assert c.code_sha(str(tmp_path)) != before
    (tmp_path / 'ns_iob.py').write_text('x')
    assert c.code_sha(str(tmp_path)) == before
    (tmp_path / 'emulator_batch.py').write_text('z')                      # not part of the evaluation
    assert c.code_sha(str(tmp_path)) == before


def test_settings_sha_blanks_the_secrets():
    plain = {'nightscout': {'url': 'https://x', 'token': None, 'api_secret_sha1': None}, 'autoisf': {'autoISF_max': 1.5}}
    secret = {'nightscout': {'url': 'https://x', 'token': 'abc-123', 'api_secret_sha1': 'deadbeef'}, 'autoisf': {'autoISF_max': 1.5}}
    assert c.settings_sha(settings=plain) == c.settings_sha(settings=secret)
    changed = {'nightscout': {'url': 'https://x', 'token': None, 'api_secret_sha1': None}, 'autoisf': {'autoISF_max': 1.3}}
    assert c.settings_sha(settings=plain) != c.settings_sha(settings=changed)
    assert secret['nightscout']['token'] == 'abc-123'                     # scrub works on a copy
    if os.path.isfile(c.NS_SETTINGS_PATH):
        assert len(c.settings_sha()) == 40


# ----------------------------------------------------------------------------- schema files

def test_schema_files_are_current():
    stale = c.stale_schemas()
    assert not stale, 'regenerate with: python -m hindcast.contracts --write-schemas  (stale: %s)' % stale
    for name in c.DOCUMENTS:
        with open(os.path.join(c.SCHEMAS_DIR, name + '.schema.json'), encoding='utf-8') as fh:
            schema = json.load(fh)
        assert schema['type'] == 'object' and schema['additionalProperties'] is False
        assert 'required' in schema


def test_schema_mirrors_dataclass():
    schema = c.json_schema(Study)
    props = schema['properties']
    assert set(props) == {f.name for f in c.dataclasses.fields(Study)}
    assert schema['required'] == ['name', 'created', 'source', 'host', 'settings_sha', 'code_sha', 'days', 'space']
    assert props['question'] == {'enum': ['explore', 'earlier', 'gentler'], 'default': 'explore'}
    assert props['days'] == {'type': 'array', 'items': {'$ref': '#/$defs/StudyDay'}}
    assert props['space'] == {'type': 'object', 'additionalProperties': {'$ref': '#/$defs/SpaceParam'}}
    assert set(schema['$defs']) == {'StudyDay', 'SpaceParam', 'Phase', 'Budget', 'Objective'}
    assert schema['$defs']['Phase']['properties']['points'] == {'type': ['integer', 'null']}
    trial = c.json_schema(TrialRow)['properties']
    assert trial['status'] == {'enum': list(c.TRIAL_STATUSES)}
    assert trial['metrics'] == {'type': 'object', 'additionalProperties': {}}


def test_documents_match_schema_shapes():
    """Every example document only uses keys the schema lists (a stdlib stand-in for a schema validator)."""
    def check(obj, schema, defs, path):
        if '$ref' in schema:
            schema = defs[schema['$ref'].rsplit('/', 1)[1]]
        if 'anyOf' in schema:
            return                                                             # unions are covered by from_dict
        if schema.get('type') == 'object' and 'properties' in schema:
            assert isinstance(obj, dict), path
            extra = set(obj) - set(schema['properties'])
            assert not extra, f'{path}: {extra}'
            for k, v in obj.items():
                check(v, schema['properties'][k], defs, f'{path}.{k}')
        elif schema.get('type') == 'object':
            for k, v in obj.items():
                check(v, schema['additionalProperties'], defs, f'{path}.{k}')
        elif 'array' in (schema.get('type') if isinstance(schema.get('type'), list) else [schema.get('type')]):
            if obj is not None:
                for i, v in enumerate(obj):
                    check(v, schema['items'], defs, f'{path}[{i}]')
    docs = [make_study(), make_row(), State(phase='x', trials_done=0, queue_len=0, pid=1, heartbeat='h'),
            Status(study='s', question='explore', phase='x', trials_done=0, trials_planned=1, running=False, heartbeat='h',
                   top=[make_top(0)]),
            Round(round=1, phase='x', started='h', variants=[make_candidate(1)], dead_parameters=[DeadParameter('k', 'w')])]
    for doc in docs:
        schema = c.json_schema(type(doc))
        check(doc.to_dict(), schema, schema.get('$defs', {}), type(doc).__name__)


# ----------------------------------------------------------------------------- size limits

def test_round_json_for_10_variants_is_under_20_kb():
    rnd = Round(round=5, phase='lhs', started='2026-10-16T19:10:00Z', finished='2026-10-16T19:40:00Z', trials=50,
                variants=[make_candidate(i) for i in range(10)],
                dead_parameters=[DeadParameter('profile enable_pp_ISF', 'all points inside 1.5 x floor on 3 days', 0.4)],
                floors={'2026-09-0%d' % d: {'smb_U': 4.05, 'ins_req_U': 0.3, 'tbr_U': 0.1, 'loops': 51, 'agreement_pct': 96.1}
                        for d in range(1, 8)},
                front=[make_candidate(i).vhash for i in range(3)],
                notes=['review order, not a ranking by outcome'])
    rnd.validate()
    assert len(rnd.to_compact_json().encode('utf-8')) < 20 * 1024
    assert len(rnd.to_json().encode('utf-8')) < 20 * 1024                   # even indented


def test_status_json_limit_is_enforced():
    status = Status(study='autoISF_oct', question='explore', phase='lhs', trials_done=120, trials_planned=600, running=True,
                    heartbeat='2026-10-16T19:10:00Z', pid=4711, round=4,
                    floor={'smb_U': 4.05, 'ins_req_U': 0.3, 'tbr_U': 0.1, 'loops': 51, 'agreement_pct': 96.1},
                    days={'2026-09-0%d' % d: ('search' if d != 5 else 'quarantined: cgm gap 131 min') for d in range(1, 8)},
                    dead_parameters=['profile enable_pp_ISF'], stale_context=[], exit_code=None,
                    report='report/index.html', artifact_url='https://claude.ai/artifact/0123456789abcdef0123456789abcdef',
                    top=[make_top(i) for i in range(5)])
    status.validate()
    assert len(status.to_compact_json().encode('utf-8')) <= c.STATUS_MAX_BYTES
    status.top = [make_top(i) for i in range(6)]
    with pytest.raises(ContractError, match='at most 5'):
        status.validate()
    status.top = [make_top(0)]
    status.top[0].why = 'x' * 121
    with pytest.raises(ContractError, match='1..120'):
        status.validate()
    status.top[0].why = 'tie with base inside the floor on every day'
    status.days = {'2026-09-%02d' % d: 'quarantined: profile_history change of a searched key inside the day' for d in range(1, 31)}
    with pytest.raises(ContractError, match='limit 2048'):
        status.validate()
    status.days = {}
    status.top[0].vhash = c.short(status.top[0].vhash)
    status.validate()


def test_honesty_statement_and_vocabulary_constants():
    assert 'cannot say what glucose would have done' in c.HONESTY_STATEMENT
    assert 'not medical advice' in c.HONESTY_STATEMENT
    for banned in ('better', 'safer', 'optimal', 'fewer lows'):
        assert banned not in c.HONESTY_STATEMENT.lower().replace('overstate', '')
    assert 'carb ratio' in c.NOT_TESTABLE and 'basal rates' in c.NOT_TESTABLE
