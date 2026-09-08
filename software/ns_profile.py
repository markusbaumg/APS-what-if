"""
ns_profile.py  -  build the oref1/autoISF `profile` object from the Nightscout profile store,
temporary targets, profile switches and ns_settings.json.

Contract (see NIGHTSCOUT_SOURCE.md):
    pb = ProfileBuilder(ns_profiles, treatments, settings)
    pb.basal_at(ms)                              -> scheduled basal U/h at ms (incl. profile-switch percentage)
    pb.dia_hours(ms)                             -> DIA hours of the active profile at ms
    pb.profile_at(now_ms, console_error=None)    -> full profile dict for determine_basal.py  (mg/dl inside)
    pb.activity_monitor_json(console_error, now_ms) -> {'recentSteps5Minutes', 'recentSteps10Minutes', 'recentSteps15Minutes',
                                                        'recentSteps30Minutes', 'recentSteps60Minutes', 'phone_moved', 'time_since_start'}
    pb.autoisf_version(console_error)            -> '3.2.0' from 'start AutoISF 3.2.0' or settings default

Keys determine_basal.py reads from profile (grep "profile\\[" determine_basal.py):
    sens current_basal max_iob temptargetSet maxSMBBasalMinutes iob_threshold_percent carb_ratio autoISF_version
    A52_risk_enable target_bg maxUAMSMBBasalMinutes skip_neutral_temps out_units min_bg max_bg bgBrake_ISF_weight
    smb_delivery_ratio_min smb_delivery_ratio_max remainingCarbsFraction remainingCarbsCap meal_addon exercise_mode
    enableSMB_with_temptarget enableSMB_with_COB enableSMB_always enableSMB_after_carbs calibrationStart calibrationIgnore
    calibrationDuration bgAccel_ISF_weight autoISF_min allowSMB_with_high_temptarget time_since_start SMBInterval
    smb_max_range_extension smb_delivery_ratio smb_delivery_ratio_bg_range sensitivity_raises_target resistance_lowers_target
    profile_percentage pp_ISF_weight pp_ISF_hours phone_moved parabola_fit_source meal_type_weight max_daily_safety_multiplier
    max_daily_basal max_basal lower_ISFrange_weight low_temptarget_lowers_sensitivity inactivity_scale_factor
    inactivity_idle_start inactivity_idle_end ignore_inactivity_overnight higher_ISFrange_weight high_temptarget_raises_sensitivity
    half_basal_exercise_target enableUAM enableSMB_EvenOn_OddOff_always enable_pp_ISF_always enable_dura_ISF_with_COB
    enable_autoISF dura_ISF_weight drift_ISF_weight delta_ISFrange_weight current_basal_safety_multiplier carbsReqThreshold
    bolus_increment autosens_max autoISF_max adv_target_adjustments activity_scale_factor activity_detection dia lgsThreshold
    (+ recentSteps* / phone_moved / time_since_start merged in by emulator_core.get_profile from the activity monitor json;
     profile_at() already contains them when a console_error is given, so the merge is idempotent)
    Emulator-only keys that are deliberately NOT produced: new_parameter, state (setVariant), full_basal_exercise_target
    (gz_proto prototype switch), key_activity_detection (old name).

Rules (ported from nightscout/AndroidAPS master, see the notes at the functions)
    * glucose values inside the profile are mg/dl (NS mmol * 18); out_units = 'mmol/L' when NS units are mmol
    * active NS profile at time t: newest profile document with startDate <= t, its defaultProfile (or settings.nightscout.profile_name);
      a 'Profile Switch' treatment (profile, profileJson, percentage, timeshift, duration) that is newer overrides while active
      (ProfileSealed.PS: basal * pct/100, ISF and CR * 100/pct, targets unscaled, schedule shifted by timeshift hours)
    * temp target: newest 'Temporary Target' treatment with created_at <= t < created_at + duration (TemporaryTargetDao
      .getTemporaryTargetActiveAt) -> min_bg = targetBottom, max_bg = targetTop, target_bg = (low+high)/2, temptargetSet = True;
      duration 0 = cancel
    * settings.profile_history.changes: a change {when, parameter, from, to} means the profile key `parameter` had value `from`
      before `when` (only for keys that exist in the settings sections, e.g. autoISF_max)
    * activity monitor: reproduce the state printed in consoleError with synthetic step counts (thresholds in determine_basal.activityMonitor)
    * max_basal follows OpenAPSAutoISFPlugin.applyBasalConstraints: min(max(pref, max_daily_basal), floor(cur_mult*basal*100)/100,
      floor(max_daily_basal*daily_mult*100)/100)
    * current_basal = pump base basal rate = scheduled basal rounded to the pump basal step (settings.safety.basal_step, default 0.05)

Validation: `python ns_profile.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z [url]`
compares against consoleError: 'profile.sens: X, sens: Y', 'ISF unchanged: X' / 'ISF from X to Y', 'Basal unchanged: X' /
'adjusting basal from X to Y', 'CR: X', suggested.targetBG, 'SMB enabled; current target X has even decimal',
'Activity monitor disabled: tempTarget' vs the Temporary Target treatments, 'User setting iobTH=..', 'limited by autoISF_max X',
and re-runs determine_basal.activityMonitor on the synthetic step counts.
Standard library only (runs under QPython); matplotlib is never imported here.
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo          # stdlib >= 3.9; needs a tz database (may be missing on a phone)
except ImportError:                        # pragma: no cover
    ZoneInfo = None

from ns_client import parse_iso

MMOL_TO_MGDL = 18.0
DAY_SECONDS = 24 * 60 * 60

# hard limits of AAPS (HardLimits.kt); verifyHardLimits clamps the value into [min, max]
LIMIT_MIN_BG = (80.0, 180.0)
LIMIT_MAX_BG = (90.0, 200.0)
LIMIT_TARGET_BG = (80.0, 200.0)
LIMIT_TEMP_MIN_BG = (72.0, 180.0)
LIMIT_TEMP_MAX_BG = (72.0, 270.0)
LIMIT_TEMP_TARGET_BG = (72.0, 200.0)

# settings sections that are copied 1:1 into the profile
SETTINGS_SECTIONS = ('insulin', 'safety', 'smb', 'autoisf', 'activity_monitor', 'flags')

# key names of the activity monitor json (AAPS 3.3+ naming, see determine_basal.activityMonitor)
AM_KEYS = ('recentSteps5Minutes', 'recentSteps10Minutes', 'recentSteps15Minutes', 'recentSteps30Minutes',
           'recentSteps60Minutes', 'phone_moved', 'time_since_start')

# synthetic step counts per detected state: (5m, 10m, 15m, 30m, 60m) - chosen inside the threshold bands of activityMonitor
AM_STEPS = {
    'activity':           (400, 400, 400, 1600, 2600),     # > 300 / > 300 / > 300 / > 1500 / > 2500
    'partial_activity':   (250, 250, 250, 600, 900),       # > 200 / > 200 / > 200 / > 500 / > 800
    'neutral':            (50, 50, 50, 150, 300),          # 60m > 200 and nothing above the partial thresholds
    'partial_inactivity': (0, 0, 0, 0, 100),               # 50 <= 60m <= 200
    'inactivity':         (0, 0, 0, 0, 0),                 # 60m < 50
}

_re_version = re.compile(r'start AutoISF\s+([0-9][0-9.]*)')
_re_initialising = re.compile(r'Activity monitor initialising for\s+(-?\d+)\s+more minutes')


# ----------------------------------------------------------------------------- helpers
def _clamp(value, limits):
    """HardLimits.verifyHardLimits: clamp into [min, max]"""
    lo, hi = limits
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _round_to(value, step):
    """Round.roundTo(value, step) of AAPS (round half up in units of step)"""
    return math.floor(value / step + 0.5) * step if step else value


def _to_mgdl(value, units):
    if units and str(units).lower().startswith('mmol'):
        return value * MMOL_TO_MGDL
    return value


def _to_mgdl_detect(value):
    """profileUtil.convertToMgdlDetect: values below 40 are treated as mmol/l"""
    return value * MMOL_TO_MGDL if value < 40 else value


def _time_as_seconds(entry):
    """seconds from midnight of a profile schedule entry (timeAsSeconds or 'HH:MM')"""
    if 'timeAsSeconds' in entry and entry['timeAsSeconds'] is not None:
        return int(entry['timeAsSeconds'])
    t = str(entry.get('time', '00:00'))
    parts = t.split(':')
    return int(parts[0]) * 3600 + (int(parts[1]) if len(parts) > 1 else 0) * 60


def _schedule(entries):
    """NS schedule list -> sorted list of (seconds_from_midnight, value)"""
    out = []
    for e in entries or []:
        try:
            out.append((_time_as_seconds(e), float(e['value'])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    if not out:
        out = [(0, 0.0)]
    return out


def _value_by_seconds(schedule, seconds_from_midnight, multiplier=1.0, timeshift_hours=0):
    """List<Block>.blockValueBySeconds (BlockExtension.kt): value of the block containing the shifted second"""
    shifted = (seconds_from_midnight - int(timeshift_hours) * 3600 + DAY_SECONDS) % DAY_SECONDS
    value = schedule[-1][1]
    for start, v in schedule:
        if start <= shifted:
            value = v
        else:
            break
    return value * multiplier


def _treatment_ms(t):
    if 'created_at' in t and isinstance(t['created_at'], str):
        return parse_iso(t['created_at'])
    if 'mills' in t:
        return int(t['mills'])
    if isinstance(t.get('date'), (int, float)):
        return int(t['date'])
    return 0


def _doc_ms(doc):
    for key in ('startDate', 'created_at'):
        v = doc.get(key)
        if isinstance(v, str):
            try:
                return parse_iso(v)
            except ValueError:
                pass
    if isinstance(doc.get('date'), (int, float)):
        return int(doc['date'])
    if isinstance(doc.get('mills'), (int, float)):
        return int(doc['mills'])
    return 0


def _timeshift_hours(value):
    """NS 'timeshift' of a Profile Switch: hours (AAPS); tolerate minutes / milliseconds"""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if abs(v) >= 3600000:
        v = v / 3600000.0
    elif abs(v) > 24:
        v = v / 60.0
    return int(v)


class _LocalClock:
    """seconds from midnight in the profile time zone (MidnightUtils.secondsFromMidnight ignoring DST jumps)"""

    def __init__(self, tzname, utc_offset_minutes):
        self.tz = None
        if tzname and ZoneInfo is not None:
            try:
                self.tz = ZoneInfo(tzname)
            except Exception:                                   # no tz database on this system
                self.tz = None
        if self.tz is None:
            self.tz = timezone(timedelta(minutes=utc_offset_minutes or 0))

    def seconds_from_midnight(self, ms):
        dt = datetime.fromtimestamp(ms / 1000.0, tz=self.tz)
        return dt.hour * 3600 + dt.minute * 60 + dt.second

    def utc_offset_hours(self, ms):
        dt = datetime.fromtimestamp(ms / 1000.0, tz=self.tz)
        off = dt.utcoffset()
        return off.total_seconds() / 3600.0 if off is not None else 0.0


class _PureProfile:
    """one named profile of the NS store (PureProfile): schedules in profile units, dia hours, timezone"""

    def __init__(self, name, store_entry, doc_utc_offset_minutes=0):
        self.name = name
        self.units = str(store_entry.get('units') or 'mg/dl')
        self.dia = float(store_entry.get('dia') or 0) or None
        self.basal = _schedule(store_entry.get('basal'))
        self.sens = _schedule(store_entry.get('sens'))
        self.carbratio = _schedule(store_entry.get('carbratio'))
        self.target_low = _schedule(store_entry.get('target_low'))
        self.target_high = _schedule(store_entry.get('target_high'))
        self.timezone = store_entry.get('timezone')
        self.clock = _LocalClock(self.timezone, doc_utc_offset_minutes)

    def max_daily_basal(self):
        """ProfileSealed.getMaxDailyBasal: max block amount (without profile-switch percentage)"""
        return max(v for _, v in self.basal)


class _Active:
    """the effective profile at one instant: pure profile + percentage + timeshift (ProfileSealed.PS semantics)"""

    def __init__(self, pure, percentage=100, timeshift=0, source='store'):
        self.pure = pure
        self.percentage = int(percentage or 100) or 100
        self.timeshift = int(timeshift or 0)
        self.source = source

    def _sec(self, ms):
        return self.pure.clock.seconds_from_midnight(ms)

    def basal(self, ms):
        return _value_by_seconds(self.pure.basal, self._sec(ms), self.percentage / 100.0, self.timeshift)

    def isf_mgdl(self, ms):
        return _to_mgdl(_value_by_seconds(self.pure.sens, self._sec(ms), 100.0 / self.percentage, self.timeshift), self.pure.units)

    def ic(self, ms):
        return _value_by_seconds(self.pure.carbratio, self._sec(ms), 100.0 / self.percentage, self.timeshift)

    def target_low_mgdl(self, ms):
        return _to_mgdl(_value_by_seconds(self.pure.target_low, self._sec(ms), 1.0, self.timeshift), self.pure.units)

    def target_high_mgdl(self, ms):
        return _to_mgdl(_value_by_seconds(self.pure.target_high, self._sec(ms), 1.0, self.timeshift), self.pure.units)

    def target_mgdl(self, ms):
        return (self.target_low_mgdl(ms) + self.target_high_mgdl(ms)) / 2.0


# ----------------------------------------------------------------------------- the builder
class ProfileBuilder:
    def __init__(self, ns_profiles, treatments, settings):
        self.settings = settings or {}
        ns_cfg = self.settings.get('nightscout', {})
        self.profile_name = ns_cfg.get('profile_name') or None
        self.docs = []                                   # (start_ms, doc) ascending
        for doc in ns_profiles or []:
            if not isinstance(doc, dict) or not isinstance(doc.get('store'), dict) or not doc['store']:
                continue
            self.docs.append((_doc_ms(doc), doc))
        self.docs.sort(key=lambda x: x[0])
        self.switches = []                               # (start_ms, treatment) ascending, Profile Switch
        self.temp_targets = []                           # (start_ms, treatment) ascending, Temporary Target
        for t in treatments or []:
            if not isinstance(t, dict) or t.get('isValid') is False:
                continue
            et = t.get('eventType')
            if et == 'Profile Switch':
                self.switches.append((_treatment_ms(t), t))
            elif et == 'Temporary Target':
                self.temp_targets.append((_treatment_ms(t), t))
        self.switches.sort(key=lambda x: x[0])
        self.temp_targets.sort(key=lambda x: x[0])
        self._pure_cache = {}
        self._history = self._parse_history(self.settings.get('profile_history', {}))
        self._warned = set()

    # ------------------------------------------------------------------ profile documents / switches
    def _pure_from_doc(self, doc, name):
        key = (id(doc), name)
        if key not in self._pure_cache:
            store = doc['store']
            if name not in store:
                if name not in self._warned:
                    self._warned.add(name)
                    print(f'ns_profile: profile "{name}" not in store {list(store)}; using "{doc.get("defaultProfile")}"', file=sys.stderr)
                name = doc.get('defaultProfile') if doc.get('defaultProfile') in store else next(iter(store))
            self._pure_cache[key] = _PureProfile(name, store[name], doc.get('utcOffset') or 0)
        return self._pure_cache[key]

    def _pure_from_switch(self, t, start_ms):
        key = ('ps', t.get('identifier') or t.get('_id') or start_ms)
        if key not in self._pure_cache:
            name = t.get('profile') or t.get('originalProfileName') or ''
            pure = None
            pj = t.get('profileJson')
            if pj:
                try:
                    entry = json.loads(pj) if isinstance(pj, str) else pj
                    pure = _PureProfile(name, entry, t.get('utcOffset') or 0)
                    doc = self._doc_at(start_ms)
                    if doc is not None:                     # local time = phone time zone = store time zone, not the json's
                        pure.clock = self._pure_from_doc(doc, doc.get('defaultProfile')).clock
                except (ValueError, TypeError):
                    pure = None
            if pure is None:
                doc = self._doc_at(start_ms)
                pure = self._pure_from_doc(doc, name) if doc else None
            self._pure_cache[key] = pure
        return self._pure_cache[key]

    def _doc_at(self, ms):
        """newest profile store document with startDate <= ms (or the oldest one if none)"""
        chosen = None
        for start, doc in self.docs:
            if start <= ms:
                chosen = doc
            else:
                break
        if chosen is None and self.docs:
            chosen = self.docs[0][1]
        return chosen

    def active_profile(self, ms):
        """effective profile at ms: newest of (profile store document, Profile Switch) that is not an expired temporary switch"""
        candidates = [(start, 'doc', doc) for start, doc in self.docs if start <= ms]
        for start, t in self.switches:
            if start <= ms:
                dur = float(t.get('duration') or 0)
                if dur > 0 and start + dur * 60000 <= ms:
                    continue                                    # temporary switch already over
                candidates.append((start, 'ps', t))
        if not candidates:
            if self.docs:
                doc = self.docs[0][1]
                return _Active(self._pure_from_doc(doc, self.profile_name or doc.get('defaultProfile')))
            raise ValueError('ns_profile: no profile document and no profile switch available')
        candidates.sort(key=lambda c: c[0])
        start, kind, obj = candidates[-1]
        if kind == 'doc':
            return _Active(self._pure_from_doc(obj, self.profile_name or obj.get('defaultProfile')), 100, 0, 'store')
        pure = self._pure_from_switch(obj, start)
        if pure is None:
            doc = self._doc_at(ms)
            pure = self._pure_from_doc(doc, self.profile_name or doc.get('defaultProfile'))
        return _Active(pure, obj.get('percentage') or 100, _timeshift_hours(obj.get('timeshift')), 'switch')

    # ------------------------------------------------------------------ temp targets
    def temp_target_at(self, ms):
        """TemporaryTargetDao.getTemporaryTargetActiveAt: newest TT with timestamp <= ms < timestamp + duration
        -> {'low', 'high', 'target', 'start', 'end', 'reason'} in mg/dl, or None"""
        for start, t in reversed(self.temp_targets):
            if start > ms:
                continue
            dur = t.get('durationInMilliseconds')
            if dur is None:
                dur = float(t.get('duration') or 0) * 60000
            low = t.get('targetBottom')
            high = t.get('targetTop')
            if not dur or low is None or high is None:
                # cancel record: whatever was running was ended here (AAPS shortens the running TT instead)
                return None
            if start + dur > ms:
                units = t.get('units') or ('mmol' if float(low) < 40 else 'mg/dl')
                low = _to_mgdl(float(low), units)
                high = _to_mgdl(float(high), units)
                return {'low': low, 'high': high, 'target': (low + high) / 2.0, 'start': start,
                        'end': start + dur, 'reason': t.get('reason')}
            return None                                     # newest TT that started before ms is already over
        return None

    # ------------------------------------------------------------------ settings history
    @staticmethod
    def _parse_history(history):
        out = []
        for ch in (history or {}).get('changes', []) or []:
            try:
                out.append((parse_iso(str(ch['when'])), str(ch['parameter']), ch.get('from'), ch.get('to')))
            except (KeyError, ValueError, TypeError):
                continue
        out.sort(key=lambda x: x[0])
        return out

    def _apply_history(self, profile, ms):
        """value of a settings key at ms: the current setting unless a later change says it was `from` before `when`"""
        done = set()
        for when, key, old, new in self._history:        # ascending: the earliest future change wins
            if when > ms and key in profile and key not in done:
                profile[key] = old
                done.add(key)

    # ------------------------------------------------------------------ public contract
    def basal_at(self, ms):
        return self.active_profile(ms).basal(ms)

    def dia_hours(self, ms):
        act = self.active_profile(ms)
        dia = act.pure.dia
        if not dia:
            dia = self.settings.get('insulin', {}).get('dia_hours') or 6.0
        min_dia = self.settings.get('insulin', {}).get('min_dia_hours')
        if min_dia and dia < min_dia:
            dia = float(min_dia)                             # InsulinOrefBasePlugin enforces a minimum DIA
        return float(dia)

    def autoisf_version(self, console_error):
        for line in console_error or []:
            m = _re_version.search(line)
            if m:
                return m.group(1)
        return str(self.settings.get('autoisf', {}).get('autoISF_version') or '')

    @staticmethod
    def activity_state(console_error):
        """('class', extra) of the 'Activity monitor ...' line in consoleError; class None if there is no such line"""
        for line in console_error or []:
            if not line.startswith('Activity monitor'):
                continue
            if 'disabled in settings' in line:
                return 'disabled_settings', None
            if 'disabled: tempTarget' in line:
                return 'temptarget', None
            if 'not to be carried' in line:
                return 'phone_not_moved', None
            m = _re_initialising.search(line)
            if m:
                return 'initialising', int(m.group(1))
            if 'sleeping state' in line:
                return 'sleeping_state', None
            if 'sleeping hours' in line:
                return 'sleeping_hours', None
            if 'bg < target' in line:
                return 'bg_below_target', None
            if 'partial activity' in line:
                return 'partial_activity', None
            if 'partial inactivity' in line:
                return 'partial_inactivity', None
            if 'detected activity' in line:
                return 'activity', None
            if 'detected inactivity' in line:
                return 'inactivity', None
            if 'neutral' in line:
                return 'neutral', None
            return 'unknown', line
        return None, None

    def activity_monitor_json(self, console_error, now_ms):
        state, extra = self.activity_state(console_error)
        steps = AM_STEPS['neutral']
        phone_moved = True
        time_since_start = 1440                               # long after start: initialisation over
        if state == 'phone_not_moved':
            phone_moved = False
            steps = AM_STEPS['inactivity']
        elif state == 'initialising':
            time_since_start = max(0, 60 - extra)
            steps = AM_STEPS['inactivity']
        elif state in ('sleeping_state', 'sleeping_hours', 'bg_below_target'):
            steps = AM_STEPS['inactivity']                    # recentSteps60Minutes <= 200
        elif state in AM_STEPS:
            steps = AM_STEPS[state]
        elif state == 'temptarget':
            steps = AM_STEPS['neutral']
        return {'recentSteps5Minutes': steps[0], 'recentSteps10Minutes': steps[1], 'recentSteps15Minutes': steps[2],
                'recentSteps30Minutes': steps[3], 'recentSteps60Minutes': steps[4],
                'phone_moved': phone_moved, 'time_since_start': time_since_start}

    def profile_at(self, now_ms, console_error=None):
        act = self.active_profile(now_ms)
        pure = act.pure
        s = self.settings
        safety = s.get('safety', {})
        profile = {}
        # settings sections 1:1 (private '_' keys skipped)
        for section in SETTINGS_SECTIONS:
            for k, v in (s.get(section) or {}).items():
                if not k.startswith('_'):
                    profile[k] = v
        self._apply_history(profile, now_ms)

        # ---- NS profile (OpenAPSAutoISFPlugin.invoke)
        min_bg = _clamp(_round_to(act.target_low_mgdl(now_ms), 0.1), LIMIT_MIN_BG)
        max_bg = _clamp(_round_to(act.target_high_mgdl(now_ms), 0.1), LIMIT_MAX_BG)
        target_bg = _clamp(act.target_mgdl(now_ms), LIMIT_TARGET_BG)
        tt = self.temp_target_at(now_ms)
        if tt:
            min_bg = _clamp(tt['low'], LIMIT_TEMP_MIN_BG)
            max_bg = _clamp(tt['high'], LIMIT_TEMP_MAX_BG)
            target_bg = _clamp(tt['target'], LIMIT_TEMP_TARGET_BG)
        basal = act.basal(now_ms)
        basal_step = float(safety.get('basal_step') or 0.05)
        current_basal = round(_round_to(basal, basal_step), 3)   # pump.baseBasalRate (pump resolution)
        max_daily_basal = pure.max_daily_basal()
        # applyBasalConstraints (OpenAPSAutoISFPlugin / SafetyPlugin)
        max_basal_pref = float(safety.get('max_basal') or 0)
        max_basal = max(max_basal_pref, max_daily_basal)
        cur_mult = float(safety.get('current_basal_safety_multiplier') or 4)
        daily_mult = float(safety.get('max_daily_safety_multiplier') or 3)
        max_basal = min(max_basal, math.floor(cur_mult * basal * 100) / 100.0, math.floor(max_daily_basal * daily_mult * 100) / 100.0)

        units_mmol = str(pure.units).lower().startswith('mmol') or str(s.get('nightscout', {}).get('units', '')).lower().startswith('mmol')
        profile.update({
            'dia': self.dia_hours(now_ms),
            'min_5m_carbimpact': safety.get('openaps_smb_min_5m_carbimpact', 8),
            'max_daily_basal': max_daily_basal,
            'max_basal': round(max_basal, 3),
            'min_bg': min_bg,
            'max_bg': max_bg,
            'target_bg': target_bg,
            'carb_ratio': act.ic(now_ms),
            'sens': act.isf_mgdl(now_ms),
            'current_basal': current_basal,
            'temptargetSet': bool(tt),
            'out_units': 'mmol/L' if units_mmol else 'mg/dl',
            'profile_percentage': act.percentage,
            'autoISF_version': self.autoisf_version(console_error),
            'remainingCarbsFraction': 1,                          # oref default; emulator_core.get_profile sets it too
        })
        if 'lgsThreshold' in profile and profile['lgsThreshold'] is not None:
            profile['lgsThreshold'] = int(_to_mgdl_detect(float(profile['lgsThreshold'])))
        if 'maxCOB' not in profile:
            profile['maxCOB'] = 120
        if 'calibrationIgnore' not in profile:
            profile['calibrationIgnore'] = True
        profile.setdefault('calibrationStart', 0)
        profile.setdefault('calibrationDuration', 20)
        profile.setdefault('exercise_mode', False)
        # high_temptarget_raises_sensitivity = exerciseMode || pref
        profile['high_temptarget_raises_sensitivity'] = bool(profile.get('exercise_mode')) or bool(profile.get('high_temptarget_raises_sensitivity'))
        # activity monitor state of this loop (neutral state when no consoleError is available)
        profile.update(self.activity_monitor_json(console_error or [], now_ms))
        state, _ = self.activity_state(console_error or [])
        if state == 'disabled_settings':
            profile['activity_detection'] = False
        return profile


# ----------------------------------------------------------------------------- validation
def _validate(start_iso, end_iso, url=None):
    import ns_client
    settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ns_settings.json')
    with open(settings_file, 'r', encoding='utf-8') as f:
        settings = json.load(f)
    ns_cfg = settings.get('nightscout', {})
    url = url or ns_cfg.get('url')
    ns = ns_client.NightscoutClient(url, cache_dir=ns_cfg.get('cache_dir'), token=ns_cfg.get('token'),
                                    api_secret_sha1=ns_cfg.get('api_secret_sha1'))
    start_ms, end_ms = parse_iso(start_iso), parse_iso(end_iso)
    lookback = 8 * 60 * 60 * 1000
    devicestatus = ns.devicestatus(start_ms, end_ms)
    treatments = ns.treatments(start_ms - lookback, end_ms)
    docs = ns.profiles()
    pb = ProfileBuilder(docs, treatments, settings)

    import determine_basal
    import tempfile
    ce_file = os.path.join(tempfile.gettempdir(), 'ns_profile_validate_ce.txt')
    determine_basal.my_ce_file(ce_file)

    re_psens = re.compile(r'^profile\.sens:\s*([0-9.]+)')
    re_isf_unch = re.compile(r'^ISF unchanged:\s*([0-9.]+)')
    re_isf_from = re.compile(r'^ISF from\s*([0-9.]+)\s+to')
    re_bas_unch = re.compile(r'^Basal unchanged:\s*([0-9.]+)')
    re_bas_from = re.compile(r'^[Aa]djusting basal from\s*([0-9.]+)\s+to')
    re_cr = re.compile(r'^CR:\s*([0-9.]+)')
    re_parity = re.compile(r'^SMB (enabled|disabled); current target\s*([0-9.]+)')
    re_iobth = re.compile(r'^User setting iobTH=([0-9.]+)%')
    re_aimax = re.compile(r'limited by autoISF_max\s*([0-9.]+)')
    re_am_first = re.compile(r'^(Activity monitor[^:,]*)')

    stats = {}        # metric -> dict(n, ok, worst list)
    am_classes = {}   # original activity monitor line class -> [n, ok]

    def note(metric, ok, diff, stamp, detail):
        st = stats.setdefault(metric, {'n': 0, 'ok': 0, 'worst': []})
        st['n'] += 1
        if ok:
            st['ok'] += 1
        else:
            st['worst'].append((abs(diff) if diff is not None else 0, stamp, detail))

    def am_class(line):
        """class of an 'Activity monitor ...' line: text up to the first number / ratio"""
        line = re.sub(r'\s*sensitivity ratio.*$', '', line)
        line = re.sub(r'\d+', 'N', line)
        return line.strip().rstrip(':,').strip()

    n_loops = 0
    tt_lines = {'tt_line_no_tt': 0, 'tt_no_line': 0}
    for rec in devicestatus:
        sug = (rec.get('openaps') or {}).get('suggested') or {}
        stamp = sug.get('deliverAt') or sug.get('timestamp')
        if not stamp or 'bg' not in sug or 'predBGs' not in sug:
            continue
        now_ms = parse_iso(stamp)
        ce = sug.get('consoleError') or []
        if isinstance(ce, str):
            ce = ce.split('\n')
        n_loops += 1
        prof = pb.profile_at(now_ms, ce)
        mmol = prof['out_units'] == 'mmol/L'
        f = MMOL_TO_MGDL if mmol else 1.0
        tol_bg = 0.05 * f + 1e-6

        am_line = None
        for line in ce:
            m = re_psens.match(line)
            if m:
                x = float(m.group(1)) * f
                note('sens vs profile.sens', abs(prof['sens'] - x) <= tol_bg, prof['sens'] - x, stamp, f'sens {prof["sens"]:.2f} vs {x:.2f}')
                continue
            m = re_isf_unch.match(line) or re_isf_from.match(line)
            if m:
                x = float(m.group(1)) * f
                note('sens vs ISF unchanged/from', abs(prof['sens'] - x) <= tol_bg, prof['sens'] - x, stamp, f'sens {prof["sens"]:.2f} vs {x:.2f}')
                continue
            m = re_bas_unch.match(line) or re_bas_from.match(line)
            if m:
                x = float(m.group(1))
                note('current_basal vs Basal unchanged/from', abs(prof['current_basal'] - x) <= 0.0051, prof['current_basal'] - x, stamp,
                     f'basal {prof["current_basal"]} vs {x}')
                continue
            m = re_cr.match(line)
            if m:
                x = float(m.group(1))
                note('carb_ratio vs CR', abs(prof['carb_ratio'] - x) <= 0.051, prof['carb_ratio'] - x, stamp, f'CR {prof["carb_ratio"]} vs {x}')
                continue
            m = re_parity.match(line)
            if m:
                x = float(m.group(2))
                mine = round(prof['target_bg'] / 18.0, 1) if mmol else round(prof['target_bg'], 0)
                note('target parity line vs target_bg', abs(mine - x) < 0.001, mine - x, stamp, f'target {mine} vs {x} ({m.group(1)})')
                continue
            m = re_iobth.match(line)
            if m:
                x = float(m.group(1))
                note('iob_threshold_percent vs iobTH line', abs(prof['iob_threshold_percent'] - x) < 0.001, prof['iob_threshold_percent'] - x, stamp,
                     f'iobTH {prof["iob_threshold_percent"]} vs {x}')
                continue
            m = re_aimax.search(line)
            if m:
                x = float(m.group(1))
                note('autoISF_max vs limited-by line', abs(prof['autoISF_max'] - x) < 0.001, prof['autoISF_max'] - x, stamp,
                     f'autoISF_max {prof["autoISF_max"]} vs {x}')
                continue
            if line.startswith('Activity monitor') and am_line is None:
                am_line = line
        # target_bg vs suggested.targetBG
        tbg = sug.get('targetBG')
        if isinstance(tbg, (int, float)) and tbg > 0:
            note('target_bg vs suggested.targetBG', abs(prof['target_bg'] - tbg) <= 0.06, prof['target_bg'] - tbg, stamp,
                 f'target_bg {prof["target_bg"]:.2f} vs {tbg:.2f} TT={pb.temp_target_at(now_ms)}')
        # temptargetSet vs 'Activity monitor disabled: tempTarget'
        if am_line is not None and prof.get('activity_detection'):
            says_tt = 'disabled: tempTarget' in am_line
            note('temptargetSet vs AM tempTarget line', says_tt == prof['temptargetSet'], 1, stamp,
                 f'temptargetSet {prof["temptargetSet"]} vs line "{am_line}"')
            if says_tt and not prof['temptargetSet']:
                tt_lines['tt_line_no_tt'] += 1
            if prof['temptargetSet'] and not says_tt:
                tt_lines['tt_no_line'] += 1
        # activity monitor reproduction
        if am_line is not None:
            try:
                open(ce_file, 'w').close()
                utc_offset = rec.get('utcOffset', sug.get('utcOffset', 0)) or 0
                utc_offset_h = utc_offset / 60.0 if abs(utc_offset) > 24 else float(utc_offset)
                bg = float(sug.get('bg', 0))
                target_bg = (prof['min_bg'] + prof['max_bg']) / 2.0
                determine_basal.activityMonitor(prof, bg, target_bg, now_ms, utc_offset_h, {})
                with open(ce_file, 'r', encoding='utf-8') as fh:
                    mine = [l.strip() for l in fh if l.startswith('Activity monitor')]
                mine_line = mine[0] if mine else '(no line)'
                ok = am_class(mine_line) == am_class(am_line)
                note('activityMonitor class', ok, 1, stamp, f'emulated "{mine_line}" vs original "{am_line}"')
                cls = am_classes.setdefault(am_class(am_line), [0, 0])
                cls[0] += 1
                cls[1] += 1 if ok else 0
            except Exception as e:                       # pragma: no cover
                note('activityMonitor class', False, 1, stamp, f'exception {e!r} for "{am_line}"')

    print(f'ns_profile validation {start_iso} .. {end_iso}: {n_loops} loops, {len(treatments)} treatments, {len(docs)} profile docs')
    for metric in sorted(stats):
        st = stats[metric]
        pct = 100.0 * st['ok'] / st['n'] if st['n'] else 0
        print(f'  {metric:<42s} n={st["n"]:5d}  agree={st["ok"]:5d}  ({pct:6.2f}%)')
        worst = sorted(st['worst'], key=lambda w: -w[0])[:5]
        for w in worst:
            print(f'      worst: {w[1]}  {w[2]}')
    print(f'  TT line without treatment: {tt_lines["tt_line_no_tt"]}, treatment without TT line: {tt_lines["tt_no_line"]}')
    if am_classes:
        print('  activity monitor line classes (original -> reproduced):')
        for cls, (n, ok) in sorted(am_classes.items(), key=lambda kv: -kv[1][0]):
            print(f'      {n:5d} / {ok:5d} ok   {cls}')
    try:
        os.remove(ce_file)
    except OSError:
        pass
    return stats


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == '--validate':
        _validate(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        print(__doc__)
        sys.exit(1)
