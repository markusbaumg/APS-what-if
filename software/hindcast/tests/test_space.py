"""Tests for hindcast/space.py (backlog H-303): parameter types and the step grid, optimizer-compatible variant
names and VDF text, ordering constraints (ratio + half-widths decoding), the locked list without override,
vhash stability, the default space files centred on ns_settings.json."""
import json
import os
import random

import pytest

import emulator_optimize as opt
from hindcast import contracts as c
from hindcast import space as sp
from hindcast.contracts import SpaceParam
from hindcast.tests.conftest import GRID_AUTOISF, NS_SETTINGS

SETTINGS = sp.load_settings(NS_SETTINGS)


def tier_a():
    return sp.ParamSpace(sp._TIER_A, sp._TIER_A_CONSTRAINTS)


# ----------------------------------------------------------------------------- specs and types

@pytest.mark.parametrize('text, type_, values', [
    ('1.1:1.8:0.05', 'float', None),
    ('50:100:5', 'int', [50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100]),
    ('bool', 'bool', [False, True]),
    ('true,false', 'bool', [False, True]),
    ('1,2,3,5', 'choice', [1, 2, 3, 5]),
    ('30,60,90', 'choice', [30, 60, 90]),
])
def test_parse_param_spec(text, type_, values):
    p = sp.parse_param_spec(text)
    assert p.type == type_
    if values is not None:
        assert p.values() == values


def test_parse_param_spec_refuses_garbage():
    with pytest.raises(sp.SpaceError):
        sp.parse_param_spec('1.1-1.8')


def test_float_grid_values_are_rounded_decimals():
    p = SpaceParam('float', 1.1, 1.8, 0.05)
    vals = p.values()
    assert vals[0] == 1.1 and vals[-1] == 1.8 and len(vals) == 15
    assert all(v == round(v, 3) for v in vals)
    assert 1.45 in vals and 1.15 in vals


@pytest.mark.parametrize('value, mode, want', [
    (1.44, 'nearest', 1.45), (1.43, 'nearest', 1.45), (1.42, 'nearest', 1.4),
    (1.44, 'floor', 1.4), (1.41, 'ceil', 1.45), (0.5, 'nearest', 1.1), (9.0, 'nearest', 1.8),
])
def test_snap(value, mode, want):
    p = SpaceParam('float', 1.1, 1.8, 0.05)
    assert sp.snap(p, value, mode) == want


def test_snap_int_and_choice():
    assert sp.snap(SpaceParam('int', 50, 100, 5), 72.6) == 75
    assert isinstance(sp.snap(SpaceParam('int', 50, 100, 5), 72.6), int)
    assert sp.snap(SpaceParam('choice', choices=[3, 5]), 5) == 5
    with pytest.raises(sp.SpaceError):
        sp.snap(SpaceParam('choice', choices=[3, 5]), 4)


def test_on_grid():
    p = SpaceParam('float', 1.1, 1.8, 0.05)
    assert sp.on_grid(p, 1.45) and sp.on_grid(p, 1.1) and sp.on_grid(p, 1.8)
    assert not sp.on_grid(p, 1.44) and not sp.on_grid(p, 1.85) and not sp.on_grid(p, 'x')


def test_pick_covers_every_value_in_order():
    p = SpaceParam('choice', choices=['a', 'b', 'c'])
    assert [sp.pick(p, u) for u in (0.0, 0.2, 0.4, 0.7, 1.0)] == ['a', 'a', 'b', 'c', 'c']


# ----------------------------------------------------------------------------- the space, grid and oaat counts

def test_space_definition_errors():
    with pytest.raises(sp.SpaceError):
        sp.ParamSpace({})
    with pytest.raises(sp.SpaceError):
        sp.ParamSpace({'profile autoISF_max': dict(type='float', low=2.0, high=1.0, step=0.1)})
    with pytest.raises(sp.SpaceError):
        sp.ParamSpace({'profile autoISF_max': dict(type='float', low=1.0, high=2.0, step=0.1)},
                      [['profile autoISF_max', '<=', 'profile autoISF_min']])
    with pytest.raises(sp.SpaceError):
        sp.ParamSpace({'profile a': dict(type='bool'), 'profile b': dict(type='bool')}, [['profile a', '<=', 'profile b']])
    with pytest.raises(sp.SpaceError):
        sp.ParamSpace({'profile a': dict(type='bool')}, [['profile a', '=', 'profile a']])


def test_grid_points_match_optimizer_combinations():
    with open(GRID_AUTOISF, encoding='utf-8') as fh:
        params = json.load(fh)['parameters']
    space = sp.ParamSpace(sp.space_from_grid(params))
    points = space.grid_points()
    assert len(points) == len(opt.combinations(params, 'grid')) - 1          # minus the optimizer's base entry
    oaat = space.oaat_points()
    assert len(oaat) == len(opt.combinations(params, 'one_at_a_time')) - 1
    assert all(len(pt) == 1 for pt in oaat)


def test_space_from_grid_detects_bool_and_keeps_choices():
    s = sp.space_from_grid({'profile enable_pp_ISF': [True, False], 'profile pp_ISF_hours': [1, 2, 3]})
    assert s['profile enable_pp_ISF'].type == 'bool'
    assert s['profile pp_ISF_hours'].values() == [1, 2, 3]


def test_load_grid_file_gives_grid_phase():
    space, phase, mode = sp.load_grid_file(GRID_AUTOISF)
    assert mode in ('grid', 'one_at_a_time') and phase.kind in ('grid', 'oaat')
    assert set(phase.grid) == set(space)


def test_oaat_levels_around_phone_value_exclude_centre():
    space = sp.ParamSpace({'profile autoISF_max': dict(type='float', low=1.0, high=2.0, step=0.05)})
    pts = space.oaat_points(levels=5, centre={'profile autoISF_max': 1.5})
    vals = [pt['profile autoISF_max'] for pt in pts]
    assert vals == [1.0, 1.25, 1.75, 2.0]                                   # 5 levels over the range, the centre excluded
    pts = space.oaat_points(levels=5, centre={'profile autoISF_max': 1.52})   # an off-grid centre is snapped first
    assert 1.5 not in [pt['profile autoISF_max'] for pt in pts]
    assert all(sp.on_grid(space['profile autoISF_max'], v) for v in vals)


def test_grid_points_drop_constraint_violations():
    space = sp.ParamSpace({'profile autoISF_min': dict(type='float', low=0.5, high=1.5, step=0.5),
                           'profile autoISF_max': dict(type='float', low=0.5, high=1.5, step=0.5)},
                          [['profile autoISF_min', '<=', 'profile autoISF_max']])
    pts = space.grid_points()
    assert len(pts) == 6                                                      # 3x3 minus the 3 with min > max
    assert all(space.satisfies(pt) for pt in pts)


# ----------------------------------------------------------------------------- constraints: decode / encode

def test_decode_always_satisfies_chain():
    space = tier_a()
    rng = random.Random(7)
    for _ in range(300):
        u = {name: rng.random() for name in space.names}
        pt = space.decode(u)
        assert list(pt) == space.names
        assert space.violations(pt) == [], (u, pt, space.violations(pt))


def test_decode_edges_of_unit_cube():
    space = tier_a()
    for v in (0.0, 1.0):
        pt = space.decode({name: v for name in space.names})
        assert space.violations(pt) == []


def test_strict_chain_steps_away_from_equality():
    space = sp.ParamSpace({'profile a': dict(type='float', low=0.0, high=1.0, step=0.1),
                           'profile b': dict(type='float', low=0.0, high=1.0, step=0.1)},
                          [['profile a', '<', 'profile b']])
    for ub in (0.0, 0.5, 1.0):
        for ua in (0.0, 0.5, 1.0):
            pt = space.decode({'profile a': ua, 'profile b': ub})
            assert pt['profile a'] < pt['profile b'] or (pt['profile b'] == 0.0 and pt['profile a'] == 0.0)


def test_encode_decode_round_trip_for_legal_points():
    space = tier_a()
    rng = random.Random(3)
    for _ in range(100):
        pt = space.decode({name: rng.random() for name in space.names})
        assert space.decode(space.encode(pt)) == pt


def test_violations_and_normalize():
    space = tier_a()
    bad = {'profile smb_delivery_ratio_min': 0.4, 'profile smb_delivery_ratio': 0.2, 'profile smb_delivery_ratio_max': 0.3}
    v = space.violations(bad)
    assert len(v) == 1 and 'constraint' in v[0]
    assert space.violations({'profile autoISF_max': 1.47}) == ['"profile autoISF_max" = 1.47 is not on the step grid / choices']
    assert space.violations({'profile foo': 1}) == ['"profile foo" is not a searched parameter']
    assert space.normalize({'profile autoISF_max': 1.47}) == {'profile autoISF_max': 1.45}
    with pytest.raises(sp.SpaceError):
        space.check(bad)
    assert sp.Constraint(['profile a', '<=', 'profile b']).satisfied({'profile a': 1})      # a partial point


# ----------------------------------------------------------------------------- locked list: no override

@pytest.mark.parametrize('name', ['profile max_iob', 'profile max_basal', 'profile sens', 'profile carb_ratio',
                                  'profile current_basal', 'profile dia', 'profile lgsThreshold', 'profile bolus_increment',
                                  'new_parameter SMBRatio', 'new_parameter AAPS_Version', 'iob_data iob',
                                  'glucose_status delta', 'nosuchgroup x', 'profile'])
def test_locked_and_unsearchable_keys_refused(name):
    with pytest.raises(c.ContractError) as e:
        sp.ParamSpace({name: dict(type='bool')})
    assert name.split()[0] in str(e.value)


def test_locked_key_error_names_every_key():
    with pytest.raises(sp.LockedKeyError) as e:
        sp.refuse_locked(['profile autoISF_max', 'profile max_iob', 'new_parameter SMBRatio'])
    assert e.value.keys == ['profile max_iob', 'new_parameter SMBRatio']
    assert 'profile max_iob' in str(e.value) and 'new_parameter SMBRatio' in str(e.value)
    assert 'autoISF_max' not in str(e.value)


def test_param_space_has_no_override_for_locked_keys():
    import inspect
    sig = inspect.signature(sp.ParamSpace.__init__)
    assert 'allow_locked' not in sig.parameters and 'force' not in sig.parameters and 'override' not in sig.parameters
    # the only way past the lock is the explicit, audited per-study unlock, and it needs a reason
    assert sig.parameters['unlocked'].default == () and sig.parameters['unlock_reason'].default is None
    with pytest.raises(sp.SpaceError, match='unlock reason'):
        sp.ParamSpace({'profile max_iob': dict(type='float', low=3, high=12, step=0.5)}, unlocked=['profile max_iob'])
    with pytest.raises(sp.SpaceError, match='unlock reason'):
        sp.ParamSpace({'profile max_iob': dict(type='float', low=3, high=12, step=0.5)}, unlocked=['profile max_iob'],
                      unlock_reason='   ')
    # check_locked=False exists only for re-reading a study.json; the contract layer refuses such a study anyway
    with pytest.raises(c.ContractError) as e:
        c.Study.new('x', ['2026-09-06'], {'profile max_iob': {'type': 'bool'}}).validate()
    assert 'profile max_iob' in str(e.value)


def test_settings_safety_section_extends_lock():
    settings = {'safety': {'my_new_safety_key': 1}}
    assert sp.locked_reasons(['profile my_new_safety_key']) == []
    assert sp.locked_reasons(['profile my_new_safety_key'], settings)
    with pytest.raises(sp.LockedKeyError):
        sp.ParamSpace({'profile my_new_safety_key': dict(type='bool')}, settings=settings)


def test_default_tiers_contain_no_locked_key():
    settings = SETTINGS or {'safety': {}}
    assert sp.locked_reasons(sp._TIER_A, settings) == []
    assert sp.locked_reasons(sp._TIER_B, settings) == []


# ----------------------------------------------------------------------------- names, VDF text, vhash

def test_variant_name_matches_optimizer():
    with open(GRID_AUTOISF, encoding='utf-8') as fh:
        params = json.load(fh)['parameters']
    space = sp.ParamSpace(sp.space_from_grid(params))
    for assignment in opt.combinations(params, 'grid'):
        assert space.variant_name(dict(assignment)) == opt.variant_name(assignment, params)
    assert space.variant_name({}) == 'base'
    assert space.variant_name({'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5}) == 'autoISF_max_1.3__dura_ISF_weight_0.5'


def test_vdf_text_round_trips_like_setvariant(tmp_path):
    space = tier_a()
    point = {'profile autoISF_max': 1.45, 'profile iob_threshold_percent': 75, 'profile smb_delivery_ratio': 0.2}
    text = sp.vdf_text(point, base_lines=['profile  enable_pp_ISF  True  ### base'], names=space.names)
    parsed = sp.parse_vdf_text(text)
    assert parsed == {'profile enable_pp_ISF': True, 'profile autoISF_max': 1.45, 'profile smb_delivery_ratio': 0.2,
                      'profile iob_threshold_percent': 75}
    # the field separators are what emulator_optimize.write_vdf emits and setVariant splits at
    path = tmp_path / 'v.vdf'
    sp.write_variant_vdf(str(path), point, names=space.names)
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 3 and all(l.startswith('profile ') and '### emulator_optimize hindcast' in l for l in lines)
    for line in lines:
        array, item, value = sp.parse_vdf_row(line)
        assert array == 'profile' and eval(value) == point[f'{array} {item}']       # what setVariant() does
    assert sp.parse_vdf_row('') is None and sp.parse_vdf_row('profile') is None
    assert sp.parse_vdf_text("profile min_bg profile['min_bg'] + 10")['profile min_bg'] == "profile['min_bg'] + 10"


def test_vdf_rows_set_searched():
    hits = sp.vdf_rows_set_searched(['profile  autoISF_max  1.3', 'profile  enable_pp_ISF  True', ''],
                                    ['profile autoISF_max'])
    assert hits == ['profile  autoISF_max  1.3']


def test_vhash_stable_and_normalized():
    space = tier_a()
    assert space.vhash({}) == c.BASE_VHASH == 'da39a3ee5e6b4b0d3255bfef95601890afd80709'
    a = space.vhash({'profile autoISF_max': 1.45, 'profile dura_ISF_weight': 0.7})
    b = space.vhash({'profile dura_ISF_weight': 0.7, 'profile autoISF_max': 1.4500000001})
    assert a == b and len(a) == 40
    assert space.vhash({'profile autoISF_max': 1.5}) != a
    # the same assignment hashed by the optimizer's value text on both sides
    assert c.vhash({'profile autoISF_max': 1.45}) == c.vhash(sp.parse_vdf_text(sp.vdf_text({'profile autoISF_max': 1.45})))


# ----------------------------------------------------------------------------- phone values and default files

@pytest.mark.skipif(not SETTINGS, reason='software/ns_settings.json missing')
def test_phone_values_from_settings():
    assert sp.phone_value('profile autoISF_max', SETTINGS) == SETTINGS['autoisf']['autoISF_max']
    assert sp.phone_value('profile smb_delivery_ratio', SETTINGS) == SETTINGS['smb']['smb_delivery_ratio']
    assert sp.phone_value('profile min_bg', SETTINGS) is None                          # Nightscout carries targets
    assert sp.phone_value('garbage', SETTINGS) is None
    space = tier_a()
    pt = space.phone_point(SETTINGS)
    assert set(pt) == set(space.names) and space.violations(pt) == []


@pytest.mark.skipif(not SETTINGS, reason='software/ns_settings.json missing')
def test_phone_value_rewinds_profile_history():
    changes = SETTINGS.get('profile_history', {}).get('changes') or []
    if not changes:
        pytest.skip('no profile_history in ns_settings.json')
    from ns_profile import ProfileBuilder
    history = ProfileBuilder._parse_history(SETTINGS['profile_history'])
    usable = [(w, k, o, n) for w, k, o, n in history if sp.phone_value('profile ' + k, SETTINGS) is not None]
    if not usable:
        pytest.skip('no profile_history change concerns a settings-section key')
    when, key, old, new = usable[0]                           # the earliest change of that key wins before it
    name = 'profile ' + key
    assert sp.phone_value(name, SETTINGS, at_ms=when - 1) == old
    later = sp.phone_value(name, SETTINGS, at_ms=max(h[0] for h in history) + 1)
    assert later == sp.phone_value(name, SETTINGS)


def test_default_space_files_exist_and_load():
    for tier, path in sp.DEFAULT_SPACE_FILES.items():
        assert os.path.isfile(path), path
        space, constraints, meta = sp.load_space_file(path)
        if tier in sp.REQUIRES_UNLOCK:
            assert meta['requires_unlock'] is True and meta['evidence'] == sp.EVIDENCE_WEAK
            with pytest.raises(sp.LockedKeyError):
                sp.ParamSpace(space, constraints, settings=SETTINGS)
            ps = sp.ParamSpace(space, constraints, settings=SETTINGS, unlocked=meta['locked_keys'], unlock_reason='test')
            assert len(ps) >= 2
        else:
            assert meta['requires_unlock'] is False and meta['evidence'] is None and meta['locked_keys'] == []
            ps = sp.ParamSpace(space, constraints)
            assert len(ps) >= 10
        assert meta['tier'] == tier
        assert all(isinstance(v, str) for v in meta['_about'])
        assert not any(w in ' '.join(meta['_about']).lower() for w in ('better', 'optimal', 'safer', 'recommended setting'))
    space, constraints, _ = sp.load_space_file(sp.DEFAULT_SPACE_FILES['A'])
    assert ['profile smb_delivery_ratio_min', '<=', 'profile smb_delivery_ratio', '<=', 'profile smb_delivery_ratio_max'] in constraints


@pytest.mark.skipif(not SETTINGS, reason='software/ns_settings.json missing')
def test_default_space_files_are_centred_on_the_phone():
    assert sp.stale_default_spaces(settings=SETTINGS) == [], \
        'spaces/*.json differ from ns_settings.json: run `python -m hindcast.space --write-defaults`'
    space, _, _ = sp.load_space_file(sp.DEFAULT_SPACE_FILES['A'], SETTINGS)
    for name, p in space.items():
        assert p.phone == sp.phone_value(name, SETTINGS)
        assert sp.on_grid(p, p.phone) or p.type in ('bool', 'choice'), (name, p.phone)


def test_write_default_spaces_round_trip(tmp_path):
    paths = sp.write_default_spaces(str(tmp_path), settings=SETTINGS or {})
    assert len(paths) == 4 and sp.stale_default_spaces(str(tmp_path), settings=SETTINGS or {}) == []
    assert sp.stale_default_spaces(str(tmp_path / 'nowhere'), settings={}) == [
        'autoisf_tierA.json', 'autoisf_tierB.json', 'profile_tierC.json', 'safety_tierS.json']


# ----------------------------------------------------------------------------- unlock, scale, tiers C and S

def test_refuse_locked_honours_the_unlock_list():
    names = ['profile autoISF_max', 'profile max_iob', 'profile sens']
    with pytest.raises(sp.LockedKeyError) as e:
        sp.refuse_locked(names)
    assert e.value.keys == ['profile max_iob', 'profile sens'] and '--unlock' in str(e.value)
    assert sp.refuse_locked(names, SETTINGS, unlocked=['profile max_iob', 'profile sens']) == names
    with pytest.raises(sp.LockedKeyError) as e:                  # unlocking one key leaves the other locked
        sp.refuse_locked(names, SETTINGS, unlocked=['profile sens'])
    assert e.value.keys == ['profile max_iob']
    assert sp.refuse_locked(['profile max_iob'], SETTINGS, unlocked=['profile  max_iob']) == ['profile max_iob']


@pytest.mark.parametrize('name', ['new_parameter SMBRatio', 'profile dia', 'iob_data iob'])
def test_some_keys_can_never_be_unlocked(name):
    assert c.unlock_refusal(name)
    assert c.locked_reason(name, unlocked=[name])
    with pytest.raises(sp.LockedKeyError):
        sp.ParamSpace({name: dict(type='bool')}, unlocked=[name], unlock_reason='asked for')


def test_param_space_with_unlock_and_reason():
    ps = sp.ParamSpace({'profile max_iob': dict(type='float', low=3, high=12, step=0.5),
                        'profile sens': dict(type='scale', low=0.7, high=1.4, step=0.05)},
                       settings=SETTINGS, unlocked=['profile max_iob', 'profile sens'], unlock_reason='Markus asked')
    assert ps.unlocked == ['profile max_iob', 'profile sens'] and ps.scale_names() == ['profile sens']
    with pytest.raises(sp.LockedKeyError):                       # the plain call keeps refusing (exit 3 path)
        sp.ParamSpace(ps.space, settings=SETTINGS)


def test_scale_param_grid_and_identity():
    p = SpaceParam('scale', 0.7, 1.4, 0.05, target='profile sens')
    vals = p.values()
    assert vals[0] == 0.7 and vals[-1] == 1.4 and 1.0 in vals and len(vals) == 15
    assert sp.snap(p, 1.12) == 1.1 and sp.on_grid(p, 1.05) and not sp.on_grid(p, 1.07)
    ps = sp.ParamSpace({'profile sens': p}, unlocked=['profile sens'], unlock_reason='x')
    assert ps.vhash({'profile sens': 1.1}) == c.vhash({'profile sens': 1.1}) == ps.vhash({'profile sens': 1.1000000001})
    assert c.canonical_params({'profile sens': 1.1}) == 'profile sens 1.1'
    assert ps.variant_name({'profile sens': 1.1}) == 'sens_1.1'
    assert ps.oaat_points(levels=3, centre={'profile sens': 1.0}) == [{'profile sens': 0.7}, {'profile sens': 1.05}, {'profile sens': 1.4}]


@pytest.mark.parametrize('space, needle', [
    ({'profile autoISF_max': dict(type='scale', low=0.7, high=1.4, step=0.05)}, 'type scale is for'),
    ({'profile sens': dict(type='float', low=20, high=80, step=5)}, 'search it as type scale'),
    ({'profile sens': dict(type='scale', low=1.1, high=1.4, step=0.05)}, 'must contain 1.0'),
    ({'profile sens': dict(type='scale', low=0.75, high=1.45, step=0.1)}, 'must contain 1.0'),
    ({'profile sens': dict(type='scale', low=0.0, high=1.4, step=0.05)}, '0 < low < high'),
    ({'profile sens': dict(type='scale', low=0.7, high=1.4, step=0.05, target='profile carb_ratio')}, 'target'),
    ({'profile autoISF_max': dict(type='float', low=1, high=2, step=0.1, target='profile sens')}, 'only for type scale'),
])
def test_scale_definition_errors(space, needle):
    with pytest.raises(sp.SpaceError) as e:
        sp.ParamSpace(space, unlocked=list(space), unlock_reason='x')
    assert needle in str(e.value)


def test_scale_vdf_rows_exact():
    assert sp.scale_expression('profile sens', 1.1) == "profile['sens']*1.1"
    assert sp.scale_expression('profile carb_ratio', 0.85) == "profile['carb_ratio']*0.85"
    assert sp.scale_expression('profile current_basal', 1.1) == "round(profile['current_basal']*1.1/0.05)*0.05"
    assert sp.scale_expression('profile profile_percentage', 1.2) == "profile['profile_percentage']*1.2"
    assert sp.scale_expression('profile sens', 1.0) is None and sp.scale_expression('profile sens', 1) is None
    with pytest.raises(sp.SpaceError):
        sp.scale_expression('profile autoISF_max', 1.1)
    text = sp.vdf_text({'profile sens': 1.1, 'profile current_basal': 0.9, 'profile carb_ratio': 1.0,
                        'profile autoISF_max': 1.3},
                       names=['profile sens', 'profile current_basal', 'profile carb_ratio', 'profile autoISF_max'])
    assert text.splitlines() == [
        "profile      sens                             profile['sens']*1.1 ### emulator_optimize hindcast scale x1.1",
        "profile      current_basal                    round(profile['current_basal']*0.9/0.05)*0.05 "
        "### emulator_optimize hindcast scale x0.9",
        'profile      autoISF_max                      1.3              ### emulator_optimize hindcast',
    ]                                                            # carb_ratio at 1.0 = the phone: no row
    assert sp.parse_vdf_text(text) == {'profile sens': 1.1, 'profile current_basal': 0.9, 'profile autoISF_max': 1.3}
    # what setVariant() does with the row: eval on the freshly parsed profile of the loop
    profile = {'sens': 54.0, 'current_basal': 0.65, 'carb_ratio': 9.0}
    for line in text.splitlines()[:2]:
        array, item, value = sp.parse_vdf_row(line)
        profile[item] = eval(value, {}, {'profile': dict(profile)})
    assert profile['sens'] == pytest.approx(59.4)
    assert profile['current_basal'] == pytest.approx(0.6)          # 0.585 rounded to the 0.05 pump step
    assert sp.PUMP_BASAL_STEP == ((SETTINGS.get('safety') or {}).get('basal_step') or 0.05)


def test_scale_phone_is_one_and_controls_write_no_row():
    for name in ('profile sens', 'profile carb_ratio', 'profile current_basal', 'profile profile_percentage'):
        assert sp.phone_value(name, SETTINGS) == 1.0 and sp.phone_value(name, {}, at_ms=0) == 1.0
    ps = sp.ParamSpace({'profile sens': dict(type='scale', low=0.7, high=1.4, step=0.05),
                        'profile autoISF_max': dict(type='float', low=1.0, high=2.0, step=0.05)},
                       unlocked=['profile sens'], unlock_reason='x')
    point = ps.phone_point(SETTINGS or {'autoisf': {'autoISF_max': 1.5}})
    assert point['profile sens'] == 1.0
    rows = [l for l in sp.vdf_text(point, names=ps.names).splitlines() if l.strip()]
    assert all('sens' not in l for l in rows)                    # the sentinel's scale rows are empty by design


def _settings_value(item):
    for section in sp.SETTINGS_SECTIONS:
        sec = SETTINGS.get(section) or {}
        if item in sec:
            return sec[item]
    return None


@pytest.mark.skipif(not SETTINGS, reason='software/ns_settings.json missing')
def test_tier_c_and_s_files_load_with_phone_semantics():
    space, constraints, meta = sp.load_space_file(sp.DEFAULT_SPACE_FILES['C'], SETTINGS)
    assert set(space) == {'profile sens', 'profile carb_ratio', 'profile current_basal', 'profile profile_percentage'}
    assert meta['requires_unlock'] is True and meta['evidence'] == 'weak: decision-level replay, no glucose response'
    assert meta['locked_keys'] == ['profile sens', 'profile carb_ratio', 'profile current_basal']
    assert sp.space_file_notice(meta)[0] == 'evidence: weak: decision-level replay, no glucose response'
    with open(sp.DEFAULT_SPACE_FILES['C'], encoding='utf-8') as fh:
        raw = json.load(fh)['space']
    for name, p in space.items():
        assert p.type == 'scale' and p.phone == 1.0 and p.target == name
        assert (p.low, p.high, p.step) == (0.7, 1.4, 0.05)
        assert 'weak evidence' in raw[name]['_note'].lower()
    space, constraints, meta = sp.load_space_file(sp.DEFAULT_SPACE_FILES['S'], SETTINGS)
    assert set(space) == {'profile max_iob', 'profile max_basal'}       # maxSMB/UAMSMBBasalMinutes are tier B
    assert meta['requires_unlock'] is True and meta['locked_keys'] == ['profile max_iob', 'profile max_basal']
    assert space['profile max_iob'].phone == SETTINGS['safety']['max_iob'] == 7.0
    assert space['profile max_basal'].phone == SETTINGS['safety']['max_basal'] == 3.0
    assert (space['profile max_iob'].low, space['profile max_iob'].high, space['profile max_iob'].step) == (3.0, 12.0, 0.5)
    assert (space['profile max_basal'].low, space['profile max_basal'].high, space['profile max_basal'].step) == (1.0, 6.0, 0.25)
    for name, p in space.items():
        assert p.phone == _settings_value(name.split()[1]) and sp.on_grid(p, p.phone)
    with open(sp.DEFAULT_SPACE_FILES['S'], encoding='utf-8') as fh:
        raw = json.load(fh)
    assert all('cap binds' in v['_note'] for v in raw['space'].values())
    for tier in ('C', 'S'):
        text = ' '.join(sp.default_space(tier, SETTINGS)['_about']).lower()
        assert not any(w in text for w in ('better', 'optimal', 'safer', 'recommended setting'))


def test_profile_percentage_is_read_by_determine_basal():
    """tier C lists profile_percentage only because determine_basal.py reads it (iobTH_reduction_ratio)."""
    path = os.path.join(sp.SOFTWARE_DIR, 'determine_basal.py')
    with open(path, encoding='utf-8', errors='replace') as fh:
        code = [l for l in fh if "profile['profile_percentage']" in l and not l.lstrip().startswith('#')]
    assert code and 'profile profile_percentage' in sp._TIER_C
