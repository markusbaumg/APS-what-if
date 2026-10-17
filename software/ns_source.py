"""
ns_source.py  -  replay driver: turns Nightscout records into the per-loop input sets of determine_basal.py

    src = NightscoutSource('https://ns.vivikusi.ch', '2026-09-06T06:00:00Z', '2026-09-06T12:00:00Z')
    for loop in src.loops():
        ...   # see NIGHTSCOUT_SOURCE.md for the keys of `loop`

`emulator_core.scanNightscout()` consumes this generator. The composition of the four
reconstruction modules (ns_glucose, ns_iob, ns_profile, ns_meal) and everything that is taken
straight from the devicestatus record (original result, autosens ratio, COB, activity
monitor state, autoISF version) lives here.

Notes
* devicestatus is loaded with a 2 h lookback (DS_LOOKBACK_MS): ns_iob recovers the planned duration of the
  running temp basals from openaps.enacted / pump.extended, ns_meal needs suggested.isfMgdlForCarbs and one
  hour of deviation history before the first loop.  Only loops whose run time (suggested.deliverAt) lies in
  [start, end) are replayed; the upload lag created_at - deliverAt (up to ~30 s) is covered by fetching
  UPLOAD_LAG_MS beyond the window end, so two adjacent windows replay every loop exactly once.
* AAPS uploads ~15 % of the devicestatus records twice (new _id, same deliverAt); the second copy is skipped
  and counted as 'duplicate upload'.
* microBolusAllowed: AAPS hands determine_basal `isSMBModeEnabled(tempBasalFallback.not())`; the fallback (a loop
  re-run after a failed SMB delivery) is visible in Nightscout only through the echo 'SMB disabled
  (!microBolusAllowed)', so that line (or an open loop / suspended pump status) sets the flag to False.
* meal_data.slopeFromMaxDeviation / slopeFromMinDeviation come from ns_meal.DeviationHistory (AAPS autosens
  worker), pinned to the UAM duration the phone printed for the loop - with the MealData defaults (0, 999)
  the UAM prediction collapses and the replay disables most SMBs.
* the profile store is served from the cache when the cache file is newer than the window end.
"""
import json
import os
import re
import sys

from ns_client import NightscoutClient, parse_iso, to_iso
import ns_glucose
import ns_iob
import ns_meal
import ns_profile

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ns_settings.json')
LOOKBACK_MS = 8 * 60 * 60 * 1000            # treatments / CGM history needed before the first loop (DIA 6 h + margin)
DS_LOOKBACK_MS = 2 * 60 * 60 * 1000         # devicestatus before the window: planned temp durations, isfMgdlForCarbs
                                            # and the deviation history (1 h) of the first loops
UPLOAD_LAG_MS = 2 * 60 * 1000               # created_at - deliverAt of a devicestatus upload (median 6 s, max ~30 s)

_re_autosens = re.compile(r'^Autosens ratio:\s*([0-9.]+)')
_re_version = re.compile(r'^start AutoISF\s+([0-9.]+)')


def load_settings(path=None):
    with open(path or SETTINGS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def resolve_window(start_iso, end_iso, now_ms=None):
    """(start_ms, end_ms) of a replay window; the emulator's open bounds (start '2000-..', end '2099-..') default to
    end = now and start = end - 24 h"""
    start = parse_iso(start_iso) if start_iso[:4] != '2000' else None
    end = parse_iso(end_iso) if end_iso[:4] != '2099' else None
    if start is None or end is None:
        import time
        end = end or (now_ms if now_ms is not None else int(time.time() * 1000))
        start = start or end - 24 * 60 * 60 * 1000
    return start, end


def window_label(start_iso, end_iso, now_ms=None):
    """'<start>_<end>' with ':' -> '-' (whole seconds, UTC) - the base name of the result files of a Nightscout run"""
    start, end = resolve_window(start_iso, end_iso, now_ms)
    return (to_iso(start)[:19] + 'Z_' + to_iso(end)[:19] + 'Z').replace(':', '-')


def bridged_line(readings, loops):
    """the end-of-run counter of the CGM bridge (ns_glucose): readings made from devicestatus, loops replayed on one"""
    return f'CGM readings bridged from devicestatus: {readings} ({loops} loops)'


def loop_ms(rec):
    """run time of a devicestatus record: suggested.deliverAt (the loop's own clock), else created_at (upload)"""
    sug = (rec.get('openaps') or {}).get('suggested') or {}
    stamp = sug.get('deliverAt') or sug.get('timestamp') or rec.get('created_at')
    try:
        return parse_iso(stamp) if stamp else None
    except ValueError:
        return None


class NightscoutSource:
    def __init__(self, url, start_iso, end_iso, settings=None, log_msg=print, verbose=True):
        self.settings = settings or load_settings()
        ns_cfg = self.settings.get('nightscout', {})
        if url in ('ns', 'ns:', '') or url.startswith('ns:'):
            url = ns_cfg.get('url')
        self.url = url
        self.log_msg = log_msg
        self.start_ms = parse_iso(start_iso) if start_iso[:4] != '2000' else None   # emulator default = open start
        self.end_ms = parse_iso(end_iso) if end_iso[:4] != '2099' else None
        self._resolved = None                                                     # open bounds fixed at first use
        self.client = NightscoutClient(url, cache_dir=ns_cfg.get('cache_dir'), token=ns_cfg.get('token'),
                                       api_secret_sha1=ns_cfg.get('api_secret_sha1'), verbose=verbose)
        self._loaded = False
        self.skipped = {}                   # reason -> count, reported at the end
        self.bridged_loops = 0              # loops replayed on a CGM reading bridged from devicestatus

    # ------------------------------------------------------------------ data loading
    def _window(self):
        if self._resolved is None:
            if self.start_ms is None or self.end_ms is None:
                # open window: default to the last 24 hours on the server (fixed once, so that load() and the
                # loop filter agree)
                self._resolved = resolve_window('2000' if self.start_ms is None else to_iso(self.start_ms),
                                                '2099' if self.end_ms is None else to_iso(self.end_ms))
            else:
                self._resolved = (self.start_ms, self.end_ms)
        return self._resolved

    def load(self):
        start, end = self._window()
        self.log_msg(f'Nightscout window {to_iso(start)} .. {to_iso(end)} from {self.url}')
        # created_at (upload) lags deliverAt (run) by a few seconds: fetch a little beyond the end and select by
        # the run time, so that a loop belongs to exactly one window
        self.devicestatus_all = [r for r in self.client.devicestatus(start - DS_LOOKBACK_MS, end + UPLOAD_LAG_MS)
                                 if isinstance(r.get('openaps'), dict) and isinstance(r['openaps'].get('suggested'), dict)]
        self.devicestatus_all = [r for r in self.devicestatus_all if (loop_ms(r) or 0) < end]
        self.devicestatus = [r for r in self.devicestatus_all if (loop_ms(r) or -1) >= start]
        self.treatments = self.client.treatments(start - LOOKBACK_MS, end)
        self.entries = self.client.entries(start - LOOKBACK_MS, end)
        self.ns_profiles = self.client.profiles(newer_than=end)     # a store cached after the window end is final
        # the running Profile Switch (percentage / timeshift / profileJson) and a long Temporary Target may have
        # started long before the 8 h look-back: add the newest of each from before it (ProfileBuilder drops an
        # expired TT / temporary switch itself), so the window does not fall back to the 100 % store profile
        self.profile_treatments = [t for t in (self.client.latest_treatment(et, start - LOOKBACK_MS)
                                               for et in ('Profile Switch', 'Temporary Target')) if t] + self.treatments
        self.profiles = ns_profile.ProfileBuilder(self.ns_profiles, self.profile_treatments, self.settings)
        self.iob = ns_iob.IobCalculator(self.treatments, self.profiles.basal_at, self.settings,
                                        dia_at=self.profiles.dia_hours, devicestatus=self.devicestatus_all)
        self.glucose = ns_glucose.GlucoseHistory(self.entries, self.devicestatus_all, self.settings)
        self.deviations = ns_meal.DeviationHistory(self.glucose, self.iob, self.devicestatus_all,
                                                   lambda ms: self.profiles.active_profile(ms).isf_mgdl(ms))
        self._loaded = True
        self.log_msg(f'  {len(self.devicestatus)} loop runs, {len(self.entries)} CGM values, {len(self.treatments)} treatments')

    # ------------------------------------------------------------------ per loop
    def _skip(self, why):
        self.skipped[why] = self.skipped.get(why, 0) + 1

    @staticmethod
    def autosens_ratio(console_error, default):
        for line in console_error:
            m = _re_autosens.match(line)
            if m:
                return float(m.group(1))
        return default

    def loops(self):
        if not self._loaded:
            self.load()
        autosens = 1.0
        seen = set()
        start, end = self._window()
        for rec in self.devicestatus:
            sug = rec['openaps']['suggested']
            stamp = sug.get('deliverAt') or sug.get('timestamp')
            if not stamp or 'reason' not in sug:
                self._skip('no result in record')
                continue
            if stamp in seen:                       # AAPS uploads ~15 % of the records twice (new _id, same loop)
                self._skip('duplicate upload')
                continue
            seen.add(stamp)
            if 'predBGs' not in sug or 'bg' not in sug:
                self._skip('loop aborted by AAPS (no bg/predBGs): ' + str(sug.get('reason', ''))[:60])
                continue
            now_ms = parse_iso(stamp)
            if not (start <= now_ms < end):         # contract: only loops run inside the window
                self._skip('outside window')
                continue
            console_error = sug.get('consoleError') or []
            if isinstance(console_error, str):
                console_error = console_error.split('\n')

            glucose_status = self.glucose.status_at(now_ms, rec)
            if glucose_status is None:
                self._skip('no CGM value within 12 min')
                continue
            cgm_bridged = self.glucose.last_bridged
            autosens = self.autosens_ratio(console_error, autosens)
            autosens_data = {'ratio': autosens}

            profile = self.profiles.profile_at(now_ms, console_error)
            activity_monitor = self.profiles.activity_monitor_json(console_error, now_ms)
            version = self.profiles.autoisf_version(console_error)
            if version:
                profile['autoISF_version'] = version

            # sensitivity ratio used by AAPS for the temp-basal part of IOB = last autosens result
            iob_array = self.iob.iob_array(now_ms, autosens)
            iob_array[0]['lastBolusTime'] = self.iob.last_bolus_time(now_ms)
            currenttemp = self.iob.current_temp(now_ms)
            last_temp = self.iob.last_temp(now_ms)
            last_temp_age = round((now_ms - last_temp['date']) / 60000) if last_temp and last_temp.get('date') else 0
            currenttemp = {'rate': currenttemp['rate'], 'duration': currenttemp['duration']}

            meal_data = self.meal_data(now_ms, sug, iob_array[0]['lastBolusTime'], glucose_status, iob_array[0], console_error)

            utc_offset = rec.get('utcOffset', sug.get('utcOffset', 0)) or 0
            utc_offset_h = utc_offset / 60.0 if abs(utc_offset) > 24 else float(utc_offset)

            pump_status = str(((rec.get('pump') or {}).get('status') or {}).get('status', '')).lower()
            micro_bolus_allowed = not ('open' in pump_status or 'suspend' in pump_status or 'disabled' in pump_status)
            if micro_bolus_allowed and any('SMB disabled (!microBolusAllowed)' in str(l) for l in console_error):
                micro_bolus_allowed = False         # tempBasalFallback re-run after a failed SMB delivery
                self._skip('SMB fallback loop (replayed without SMB)')

            loop = {
                'record': rec, 'suggested': sug, 'stamp': stamp, 'now_ms': now_ms, 'utcOffset': utc_offset_h,
                'glucose_status': glucose_status, 'iob_array': iob_array, 'currenttemp': currenttemp,
                'lastTempAge': last_temp_age, 'profile': profile, 'activity_monitor': activity_monitor,
                'autosens_data': autosens_data, 'meal_data': meal_data, 'microBolusAllowed': micro_bolus_allowed,
                'console_error': console_error, 'autoISF_version': version,
            }
            if cgm_bridged:                         # only when set: loops on real CGM keep their exact shape
                loop['cgm_bridged'] = True
                self.bridged_loops += 1
            yield loop
        if self.skipped:
            self.log_msg('\nNightscout loops skipped: ' + ', '.join(f'{v} x {k}' for k, v in self.skipped.items()))
        pinned = sum(getattr(self.glucose, 'pinned', {}).values())
        unmatched = getattr(self.glucose, 'pin_unmatched', 0)
        if pinned or unmatched:                             # ns_glucose loop-to-reading pin (sluggish phone)
            self.log_msg(f'Nightscout loops pinned to an older CGM reading (phone ran late): {pinned}'
                         f', suggested.bg reproduced by no recent reading: {unmatched}')
        bridged = self.glucose.bridged_in(start, end) if hasattr(self.glucose, 'bridged_in') else 0
        if bridged or self.bridged_loops:
            self.log_msg(bridged_line(bridged, self.bridged_loops))

    def meal_data(self, now_ms, sug, last_bolus_time, glucose_status=None, iob_now=None, console_error=None):
        """oref meal data: COB as computed by AAPS, carbs entered during the last 6 h, deviation slopes rebuilt by
        ns_meal (AAPS autosens worker) and pinned to the UAM duration the phone printed for this loop"""
        cutoff_h = self.settings.get('safety', {}).get('absorption_cutoff', 6)
        window_start = now_ms - cutoff_h * 60 * 60 * 1000
        carbs = 0.0
        last_carb_time = 0
        for t in self.treatments:
            c = t.get('carbs')
            if not c or t.get('isValid') is False:
                continue
            ms = parse_iso(t['created_at']) if 'created_at' in t else int(t.get('date', 0))
            if window_start <= ms <= now_ms:
                carbs += float(c)
                last_carb_time = max(last_carb_time, ms)
        cob = sug.get('COB', 0) or 0
        uci = printed = None
        if glucose_status is not None and iob_now is not None and isinstance(sug.get('variable_sens'), (int, float)):
            # uci exactly as determine_basal computes it, with the phone's own sens (variable_sens)
            bgi = round(-iob_now['activity'] * float(sug['variable_sens']) * 5, 2)
            uci = round(min(glucose_status['delta'], glucose_status['short_avgdelta']) - bgi, 1)
            _, printed = ns_meal.printed_uam(console_error)
        s_max, s_min = self.deviations.slopes_at(now_ms, uci, printed)
        return {'carbs': round(carbs, 1), 'mealCOB': round(float(cob), 1), 'lastCarbTime': last_carb_time,
                'lastBolusTime': last_bolus_time, 'slopeFromMaxDeviation': s_max, 'slopeFromMinDeviation': s_min,
                'bwCarbs': False, 'bwFound': False}


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: python ns_source.py <start ISO> <end ISO> [url]   - dumps the first loop input set as JSON')
        sys.exit(1)
    src = NightscoutSource(sys.argv[3] if len(sys.argv) > 3 else 'ns:', sys.argv[1], sys.argv[2])
    n = 0
    for loop in src.loops():
        if n == 0:
            d = dict(loop)
            d.pop('record')
            print(json.dumps(d, indent=1, default=str)[:6000])
        n += 1
    print('loops:', n)
