"""
ns_prefs_import.py  -  read an AAPS preferences export (encrypted "aaps_encrypted" v1 format or the
already decrypted key/value JSON), strip everything secret or noisy, and

  1. write software/aaps_preferences.json   (scrubbed full export, for reference / diffing)
  2. update the matching values in software/ns_settings.json - except the keys listed in its
     '_overrides' section (values fitted to the phone's echoes, e.g. smb_delivery_ratio_bg_range);
     for those the export value is only reported

usage:  ../.venv/bin/python ns_prefs_import.py <export.json> [--password <pw>] [--no-settings]
        password: --password, else environment variable AAPS_EXPORT_PASSWORD, else interactive prompt.
        Needs the 'cryptography' package for encrypted files (pip install cryptography); the decrypted
        JSON can also be produced elsewhere and passed in directly.

AAPS encryption (CryptoUtil.kt): PBKDF2-HMAC-SHA1, 50000 iterations, salt = security.salt (hex),
AES-256-GCM, content = base64( 1 byte iv length | iv | ciphertext+tag ).
"""
import base64
import getpass
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, 'ns_settings.json')
OUT = os.path.join(HERE, 'aaps_preferences.json')

SECRET = re.compile(r'secret|password|token|otp|ltk|bluetooth|refresh|email|ssid|patient_name|folder_id|aaps_directory|'
                    r'pod_state|serial|uniqueId', re.I)
NOISE = re.compile(r'^(Monitor_|ExamTask_|DisabledTo_|Objectives|ns_.*last_sync|xdrip_.*last_sync|.*_last_synced_id|'
                   r'fsl_last_|last_|next|snoozedTo|Monitor)', re.I)


def decrypt_export(doc, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = bytes.fromhex(doc['security']['salt'])
    blob = base64.b64decode(doc['content'])
    ivlen = blob[0]
    iv, ct = blob[1:1 + ivlen], blob[1 + ivlen:]
    key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), salt, 50000, 32)
    plain = AESGCM(key).decrypt(iv, ct, None)
    if hashlib.sha256(plain).hexdigest() != doc['security'].get('content_hash', hashlib.sha256(plain).hexdigest()):
        raise ValueError('content hash mismatch after decryption')
    return json.loads(plain.decode('utf-8'))


def load_export(path, password=None):
    with open(path, 'r', encoding='utf-8') as f:
        doc = json.load(f)
    meta = {}
    if isinstance(doc, dict) and doc.get('format') == 'aaps_encrypted':
        meta = doc.get('metadata', {})
        if password is None:
            password = os.environ.get('AAPS_EXPORT_PASSWORD') or getpass.getpass('AAPS export master password: ')
        prefs = decrypt_export(doc, password)
    else:
        prefs = doc
    return prefs, meta


def scrub(prefs):
    out = {}
    for k in sorted(prefs):
        if SECRET.search(k) or NOISE.search(k):
            continue
        out[k] = prefs[k]
    return out


# ----------------------------------------------------------------------------- automations (temp targets)
def _parse_trigger(t):
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except json.JSONDecodeError:
            return t
    typ = t.get('type', '')
    data = t.get('data', {})
    if typ == 'TriggerConnector':
        parts = [_parse_trigger(x) for x in data.get('triggerList', [])]
        return '(' + f' {data.get("connectorType", "AND")} '.join(parts) + ')'
    if typ == 'TriggerBg':
        return f'bg {data.get("comparator", "")} {round(data.get("bg", 0), 1)} {data.get("units", "")}'
    if typ == 'TriggerDelta':
        return f'{data.get("deltaType", "delta")} {data.get("comparator", "")} {round(data.get("value", 0), 2)} {data.get("units", "")}'
    if typ == 'TriggerTimeRange':
        s, e = data.get('start', 0), data.get('end', 0)
        return f'time {s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}'
    if typ == 'TriggerRecurringTime':
        return 'recurring ' + json.dumps(data)
    return typ.replace('Trigger', '') + (' ' + json.dumps(data) if data else '')


def automations(prefs):
    raw = prefs.get('AUTOMATION_EVENTS')
    if not raw:
        return []
    try:
        events = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    out = []
    for ev in events:
        acts = []
        for a in ev.get('actions', []):
            try:
                a = json.loads(a) if isinstance(a, str) else a
            except json.JSONDecodeError:
                acts.append(str(a)[:80])
                continue
            typ, data = a.get('type', ''), a.get('data', {})
            if typ == 'ActionStartTempTarget':
                acts.append(f'TT {round(data.get("value", 0), 1)} {data.get("units", "")} for {data.get("durationInMinutes", 0)} min')
            elif typ == 'ActionStopTempTarget':
                acts.append('stop TT')
            else:
                acts.append(typ.replace('Action', '') + (' ' + json.dumps(data) if data and len(json.dumps(data)) < 60 else ''))
        out.append({'title': ev.get('title'), 'enabled': ev.get('enabled'), 'trigger': _parse_trigger(ev.get('trigger', '')), 'actions': acts})
    return out


# ----------------------------------------------------------------------------- mapping export key -> settings
MAP = {
    ('nightscout', 'url'): 'nsclientinternal_url',
    ('nightscout', 'units'): 'units',
    ('safety', 'max_iob'): 'openapsmb_max_iob',
    ('safety', 'max_basal'): 'openapsma_max_basal',
    ('safety', 'max_daily_safety_multiplier'): 'openapsama_max_daily_safety_multiplier',
    ('safety', 'current_basal_safety_multiplier'): 'openapsama_current_basal_safety_multiplier',
    ('safety', 'lgsThreshold'): 'lgsThreshold',
    ('safety', 'treatmentssafety_maxbolus'): 'treatmentssafety_maxbolus',
    ('safety', 'treatmentssafety_maxcarbs'): 'treatmentssafety_maxcarbs',
    ('safety', 'autosens_min'): 'autosens_min',
    ('safety', 'autosens_max'): 'autosens_max',
    ('safety', 'use_autosens'): 'openapsama_useautosens',
    ('safety', 'dynisf_adjust_sensitivity'): 'dynisf_adjust_sensitivity',
    ('safety', 'DynISFAdjust'): 'DynISFAdjust',
    ('safety', 'openaps_smb_min_5m_carbimpact'): 'openaps_smb_min_5m_carbimpact',
    ('safety', 'min_5m_carbimpact'): 'openapsama_min_5m_carbimpact',
    ('safety', 'absorption_cutoff'): 'absorption_cutoff',
    ('safety', 'carbsReqThreshold'): 'carbsReqThreshold',
    ('safety', 'bolussnooze_dia_divisor'): 'bolussnooze_dia_divisor',
    ('smb', 'enableSMB_always'): 'enableSMB_always',
    ('smb', 'enableSMB_with_COB'): 'enableSMB_with_COB',
    ('smb', 'enableSMB_with_temptarget'): 'enableSMB_with_temptarget',
    ('smb', 'enableSMB_after_carbs'): 'enableSMB_after_carbs',
    ('smb', 'allowSMB_with_high_temptarget'): 'enableSMB_with_high_temptarget',
    ('smb', 'enableUAM'): 'use_uam',
    ('smb', 'enableSMB_EvenOn_OddOff_always'): 'Enable alternative activation of SMB always',
    ('smb', 'SMBInterval'): 'smbinterval',
    ('smb', 'maxSMBBasalMinutes'): 'smbmaxminutes',
    ('smb', 'maxUAMSMBBasalMinutes'): 'uamsmbmaxminutes',
    ('smb', 'smb_delivery_ratio'): 'openapsama_smb_delivery_ratio',
    ('smb', 'smb_delivery_ratio_min'): 'openapsama_smb_delivery_ratio_min',
    ('smb', 'smb_delivery_ratio_max'): 'openapsama_smb_delivery_ratio_max',
    ('smb', 'smb_max_range_extension'): 'openapsama_smb_max_range_extension',
    ('smb', 'iob_threshold_percent'): 'iob_threshold_percent',
    ('autoisf', 'enable_autoISF'): 'openapsama_enable_autoISF',
    ('autoisf', 'autoISF_max'): 'autoISF_max',
    ('autoisf', 'autoISF_min'): 'autoISF_min',
    ('autoisf', 'bgAccel_ISF_weight'): 'bgAccel_ISF_weight',
    ('autoisf', 'bgBrake_ISF_weight'): 'bgBrake_ISF_weight',
    ('autoisf', 'dura_ISF_weight'): 'dura_ISF_weight',
    ('autoisf', 'pp_ISF_weight'): 'pp_ISF_weight',
    ('autoisf', 'lower_ISFrange_weight'): 'lower_ISFrange_weight',
    ('autoisf', 'higher_ISFrange_weight'): 'higher_ISFrange_weight',
    ('activity_monitor', 'activity_detection'): 'activity_detection',
    ('activity_monitor', 'activity_scale_factor'): 'activity_scale_factor',
    ('activity_monitor', 'inactivity_scale_factor'): 'inactivity_scale_factor',
    ('activity_monitor', 'ignore_inactivity_overnight'): 'ignore_inactivity_overnight',
    ('activity_monitor', 'inactivity_idle_start'): 'inactivity_idle_start',
    ('activity_monitor', 'inactivity_idle_end'): 'inactivity_idle_end',
    ('activity_monitor', 'steps_activity'): 'steps_activity_detected',
    ('activity_monitor', 'steps_inactivity'): 'steps_inactivity_detected',
    ('flags', 'high_temptarget_raises_sensitivity'): 'high_temptarget_raises_sensitivity',
    ('flags', 'low_temptarget_lowers_sensitivity'): 'low_temptarget_lowers_sensitivity',
    ('flags', 'sensitivity_raises_target'): 'sensitivity_raises_target',
    ('flags', 'resistance_lowers_target'): 'resistance_lowers_target',
    ('flags', 'exercise_mode'): 'exercise_mode',
    ('flags', 'calibration_end'): 'calibration_end',
    ('flags', 'calibration_stops_SMB'): 'calibration_stops_SMB',
    ('flags', 'fsl_exp1_factor'): 'fsl_exp1_factor',
    ('glucose', 'always_use_shortavg'): 'always_use_shortavg',
}
INSULIN = {'InsulinLyumjevPlugin': ('Lyumjev', 45), 'InsulinOrefUltraRapidActingPlugin': ('Ultra-Rapid', 55),
           'InsulinOrefRapidActingPlugin': ('Rapid-Acting', 75), 'InsulinOrefFreePeakPlugin': ('Free-Peak', None)}


def coerce(v):
    """AAPS stores many preferences as strings: turn '1.0' / 'true' back into numbers and booleans"""
    if isinstance(v, str):
        t = v.strip()
        if t.lower() in ('true', 'false'):
            return t.lower() == 'true'
        try:
            return int(t)
        except ValueError:
            pass
        try:
            return float(t)
        except ValueError:
            return v
    return v


def update_settings(prefs, meta, source_name):
    with open(SETTINGS, 'r', encoding='utf-8') as f:
        settings = json.load(f)
    changed = []
    units_mmol = str(prefs.get('units', 'mg/dl')).lower().startswith('mmol')
    # keys fitted to the phone's echoes (ns_settings.json '_overrides') are kept; the export value is only reported
    overrides = {k: v for k, v in (settings.get('_overrides') or {}).items() if not k.startswith('_')}

    def put(section, key, val):
        sec = settings.setdefault(section, {})
        dotted = section + '.' + key
        if dotted in overrides:
            if sec.get(key) != val:
                print(f'  keeping {dotted} = {sec.get(key)} (in _overrides; the export says {val})')
            return
        if sec.get(key) != val:
            changed.append(f'{dotted}: {sec.get(key)} -> {val}')
            sec[key] = val

    for (section, key), pref_key in MAP.items():
        if pref_key not in prefs:
            continue
        put(section, key, coerce(prefs[pref_key]))
    # values that need a unit conversion
    if 'openapsama_smb_delivery_ratio_bg_range' in prefs:
        v = round(float(coerce(prefs['openapsama_smb_delivery_ratio_bg_range'])) * (18.0 if units_mmol else 1.0), 2)
        put('smb', 'smb_delivery_ratio_bg_range', v)
    if 'half_basal_exercise_target' in prefs:                       # stored in mg/dl by AAPS
        mgdl = float(coerce(prefs['half_basal_exercise_target']))
        put('flags', 'half_basal_exercise_target_mgdl', round(mgdl, 1))
        put('flags', 'half_basal_exercise_target', round(mgdl / 18.0, 1) if units_mmol else round(mgdl, 1))
    for plugin, (name, peak) in INSULIN.items():
        if coerce(prefs.get('ConfigBuilder_Enabled_INSULIN_' + plugin)) is True:
            if peak is None:
                peak = coerce(prefs.get('insulin_oref_peak', 75))
            if settings['insulin'].get('type') != name or settings['insulin'].get('peak_minutes') != peak:
                changed.append(f'insulin: {settings["insulin"].get("type")}/{settings["insulin"].get("peak_minutes")} -> {name}/{peak}')
            settings['insulin']['type'] = name
            settings['insulin']['peak_minutes'] = peak
    auto = automations(prefs)
    if auto:
        settings['temp_target_automations']['from_export'] = auto
    settings['_import'] = {'source': source_name, 'created_at': meta.get('created_at'), 'aaps_version': meta.get('aaps_version'),
                           'device': meta.get('device_model'), 'tool': 'ns_prefs_import.py'}
    with open(SETTINGS, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write('\n')
    return changed


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    path = argv[1]
    password = None
    if '--password' in argv:
        password = argv[argv.index('--password') + 1]
    prefs, meta = load_export(path, password)
    scrubbed = scrub(prefs)
    doc = {'_about': 'AAPS preferences export, decrypted and scrubbed by ns_prefs_import.py '
                     '(secrets, pod state, sync counters and UI statistics removed)',
           'metadata': meta, 'automations': automations(prefs), 'preferences': scrubbed}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write('\n')
    print(f'wrote {OUT}: {len(scrubbed)} of {len(prefs)} keys kept')
    if '--no-settings' not in argv:
        changed = update_settings(prefs, meta, os.path.basename(path))
        print(f'updated {SETTINGS}: {len(changed)} value(s) changed')
        for c in changed:
            print('  ', c)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
