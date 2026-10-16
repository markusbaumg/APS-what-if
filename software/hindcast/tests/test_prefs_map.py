"""
test_prefs_map.py  -  backlog H-004: every key of the autoisf / smb / activity_monitor / flags sections of
ns_settings.json resolves in prefs_map.json; the keys without a verified title are LISTED (printed and
returned by `unmapped_titles()`), not failed. Also: every `pref_key` the map names exists in the scrubbed
AAPS export, units come from the declared `_units`, and the common profile keys the design locks are present.
Offline; reads only committed JSON files.
"""
import json
import os
import re
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
HINDCAST_DIR = os.path.dirname(TESTS_DIR)
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)

PREFS_MAP = os.path.join(HINDCAST_DIR, 'prefs_map.json')
NS_SETTINGS = os.path.join(SOFTWARE_DIR, 'ns_settings.json')
AAPS_PREFS = os.path.join(SOFTWARE_DIR, 'aaps_preferences.json')

SECTIONS = ('autoisf', 'smb', 'activity_monitor', 'flags')
COMMON_PROFILE_KEYS = ('sens', 'carb_ratio', 'current_basal', 'dia', 'max_iob', 'max_basal', 'min_bg', 'max_bg',
                       'target_bg', 'max_daily_safety_multiplier', 'current_basal_safety_multiplier', 'autosens_min',
                       'autosens_max', 'lgsThreshold', 'bolus_increment', 'out_units')
FIELDS = ('vdf', 'settings_section', 'pref_key', 'screen', 'title', 'unit')


def load(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def lookup(prefs_map, vdf_key):
    """The card's resolution rule: 'profile <key>' or a bare key -> entry or None (documented in _about)."""
    key = vdf_key.split(None, 1)[1] if vdf_key.startswith('profile ') else vdf_key
    return prefs_map['keys'].get(key)


def settings_keys(settings, section):
    return [k for k in settings.get(section, {}) if not k.startswith('_')]


def unmapped_titles(prefs_map, settings, sections=SECTIONS):
    """[(section, key, reason)] for every settings key whose entry is missing or has no verified title."""
    out = []
    for section in sections:
        for key in settings_keys(settings, section):
            entry = prefs_map['keys'].get(key)
            if entry is None:
                out.append((section, key, 'no entry in prefs_map.json'))
            elif entry.get('title') is None:
                out.append((section, key, entry.get('note') or 'title null'))
    return out


@pytest.fixture(scope='module')
def prefs_map():
    return load(PREFS_MAP)


@pytest.fixture(scope='module')
def settings():
    return load(NS_SETTINGS)


@pytest.fixture(scope='module')
def export_keys():
    return set(load(AAPS_PREFS)['preferences'])


def test_file_shape(prefs_map):
    assert isinstance(prefs_map['_about'], list)
    assert prefs_map['_verified']['against_strings_xml'] is False      # flip to True only after a strings.xml diff
    assert set(prefs_map['_fields']) >= set(FIELDS)
    assert prefs_map['keys']
    for key, entry in prefs_map['keys'].items():
        for f in FIELDS:
            assert f in entry, (key, f)
        assert entry['vdf'] == 'profile ' + key, key
        assert entry['title'] is None or (isinstance(entry['title'], str) and entry['title'].strip()), key
        assert entry['pref_key'] is None or isinstance(entry['pref_key'], str), key


def test_units_are_declared(prefs_map):
    units = set(prefs_map['_units'])
    for key, entry in prefs_map['keys'].items():
        assert entry['unit'] is None or entry['unit'] in units, (key, entry['unit'])


@pytest.mark.parametrize('section', SECTIONS)
def test_every_settings_key_has_an_entry(prefs_map, settings, section):
    keys = settings_keys(settings, section)
    assert keys, section
    missing = [k for k in keys if k not in prefs_map['keys']]
    assert missing == [], 'ns_settings.json %s keys without a prefs_map entry: %s' % (section, missing)
    for k in keys:
        assert prefs_map['keys'][k]['settings_section'] == section, k


def test_unmapped_titles_are_listed_not_guessed(prefs_map, settings, capsys):
    """The acceptance criterion of H-004: every key resolves or is listed as unmapped. The list is printed with
    `pytest -s`; the assertion only guards against silent guessing (an unmapped key must carry a note)."""
    unmapped = unmapped_titles(prefs_map, settings)
    mapped = sum(len(settings_keys(settings, s)) for s in SECTIONS) - len(unmapped)
    print('\nprefs_map.json: %d of %d section keys carry a verified title; unmapped:' % (mapped, mapped + len(unmapped)))
    for section, key, reason in unmapped:
        print('  %-17s %-36s %s' % (section, key, reason))
    for section, key, reason in unmapped:
        assert reason != 'no entry in prefs_map.json', key
        assert reason != 'title null', 'unmapped key %s needs a note saying why' % key
    assert mapped >= 20, 'the map lost its verified titles'


def test_pref_keys_exist_in_the_scrubbed_export(prefs_map, export_keys):
    for key, entry in prefs_map['keys'].items():
        if entry['pref_key'] is not None:
            assert entry['pref_key'] in export_keys, (key, entry['pref_key'])


def test_export_values_agree_with_settings_where_the_map_links_them(prefs_map, settings):
    """A linked pref_key must describe the same quantity: compare the export value with the ns_settings.json value
    for booleans and for numbers that are unit-free (the documented traps are excluded by their notes)."""
    export = load(AAPS_PREFS)['preferences']
    skip = {'half_basal_exercise_target', 'smb_delivery_ratio_bg_range', 'smb_max_range_extension',
            'low_temptarget_lowers_sensitivity', 'autoISF_max'}          # _assumed / _overrides / profile_history
    checked = 0
    for section in SECTIONS:
        for key in settings_keys(settings, section):
            entry = prefs_map['keys'][key]
            if entry['pref_key'] is None or key in skip:
                continue
            exp, mine = export[entry['pref_key']], settings[section][key]
            if isinstance(mine, bool):
                assert exp == str(mine).lower(), (key, exp, mine)
            else:
                assert abs(float(exp) - float(mine)) < 1e-9, (key, exp, mine)
            checked += 1
    assert checked >= 25


def test_common_profile_keys_present(prefs_map):
    for key in COMMON_PROFILE_KEYS:
        assert key in prefs_map['keys'], key
    for key in ('sens', 'carb_ratio', 'current_basal', 'dia'):
        e = prefs_map['keys'][key]
        assert e['settings_section'] == 'profile' and e['title'] and 'locked' in e.get('note', ''), key
    assert prefs_map['keys']['max_iob']['unit'] == 'U'
    assert prefs_map['keys']['max_basal']['unit'] == 'U/h'
    assert prefs_map['keys']['sens']['unit'] == 'glucose per U'


def test_lookup_rule(prefs_map):
    assert lookup(prefs_map, 'profile autoISF_max')['title'] == 'autoISF max'
    assert lookup(prefs_map, 'autoISF_max') is lookup(prefs_map, 'profile autoISF_max')
    assert lookup(prefs_map, 'profile no_such_key') is None
    assert lookup(prefs_map, 'new_parameter SMBRatio') is None            # other VDF groups never resolve


def test_documented_titles(prefs_map):
    """The titles the design and the card example rely on."""
    k = prefs_map['keys']
    assert k['dura_ISF_weight']['title'] == 'dura ISF weight'
    assert k['autoISF_max']['title'] == 'autoISF max' and k['autoISF_min']['title'] == 'autoISF min'
    assert k['enable_autoISF']['pref_key'] == 'openapsama_enable_autoISF'
    assert k['allowSMB_with_high_temptarget']['pref_key'] == 'enableSMB_with_high_temptarget'
    assert k['enableUAM']['pref_key'] == 'use_uam'
    assert k['maxSMBBasalMinutes']['pref_key'] == 'smbmaxminutes'
    assert k['half_basal_exercise_target']['unit'] == 'mmol/L'
    assert k['smb_delivery_ratio_bg_range']['unit'] == 'mg/dl'


def test_unverified_or_absent_preferences_have_no_title(prefs_map):
    """'never guess': an entry whose note says the wording is not verified, that AAPS hard-codes the value, or
    that the key is emulator-only / derived, must carry title null (the card then prints the VDF key)."""
    for key, entry in prefs_map['keys'].items():
        note = (entry.get('note') or '').lower()
        if re.search(r'not verified|hard-coded|emulator|no aaps preference|not a preference|computed per loop', note):
            assert entry['title'] is None, (key, entry['title'], note)
        if entry['settings_section'] in ('derived', 'emulator'):
            assert entry['title'] is None and entry['pref_key'] is None, key


def test_map_is_json_with_no_duplicate_keys():
    text = open(PREFS_MAP, encoding='utf-8').read()
    seen = []

    def hook(pairs):
        keys = [k for k, _ in pairs]
        dupes = {k for k in keys if keys.count(k) > 1}
        seen.extend(dupes)
        return dict(pairs)
    json.loads(text, object_pairs_hook=hook)
    assert seen == []
