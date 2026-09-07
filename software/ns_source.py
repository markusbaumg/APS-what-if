"""
ns_source.py  -  replay driver: turns Nightscout records into the per-loop input sets of determine_basal.py

    src = NightscoutSource('https://ns.vivikusi.ch', '2026-09-06T06:00:00Z', '2026-09-06T12:00:00Z')
    for loop in src.loops():
        ...   # see NIGHTSCOUT_SOURCE.md for the keys of `loop`

`emulator_core.scanNightscout()` consumes this generator. The composition of the three
reconstruction modules (ns_glucose, ns_iob, ns_profile) and everything that is taken
straight from the devicestatus record (original result, autosens ratio, COB, activity
monitor state, autoISF version) lives here.
"""
import json
import os
import re
import sys

from ns_client import NightscoutClient, parse_iso, to_iso
import ns_glucose
import ns_iob
import ns_profile

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ns_settings.json')
LOOKBACK_MS = 8 * 60 * 60 * 1000            # treatments / CGM history needed before the first loop (DIA 6 h + margin)

_re_autosens = re.compile(r'^Autosens ratio:\s*([0-9.]+)')
_re_version = re.compile(r'^start AutoISF\s+([0-9.]+)')


def load_settings(path=None):
    with open(path or SETTINGS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


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
        self.client = NightscoutClient(url, cache_dir=ns_cfg.get('cache_dir'), token=ns_cfg.get('token'),
                                       api_secret_sha1=ns_cfg.get('api_secret_sha1'), verbose=verbose)
        self._loaded = False
        self.skipped = {}                   # reason -> count, reported at the end

    # ------------------------------------------------------------------ data loading
    def _window(self):
        if self.start_ms is None or self.end_ms is None:
            # open window: default to the last 24 hours on the server
            import time
            end = self.end_ms or int(time.time() * 1000)
            start = self.start_ms or end - 24 * 60 * 60 * 1000
            return start, end
        return self.start_ms, self.end_ms

    def load(self):
        start, end = self._window()
        self.log_msg(f'Nightscout window {to_iso(start)} .. {to_iso(end)} from {self.url}')
        self.devicestatus = [r for r in self.client.devicestatus(start, end)
                             if isinstance(r.get('openaps'), dict) and isinstance(r['openaps'].get('suggested'), dict)]
        self.treatments = self.client.treatments(start - LOOKBACK_MS, end)
        self.entries = self.client.entries(start - LOOKBACK_MS, end)
        self.ns_profiles = self.client.profiles()
        self.profiles = ns_profile.ProfileBuilder(self.ns_profiles, self.treatments, self.settings)
        self.iob = ns_iob.IobCalculator(self.treatments, self.profiles.basal_at, self.settings)
        self.glucose = ns_glucose.GlucoseHistory(self.entries, self.devicestatus, self.settings)
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
        for rec in self.devicestatus:
            sug = rec['openaps']['suggested']
            stamp = sug.get('deliverAt') or sug.get('timestamp')
            if not stamp or 'reason' not in sug:
                self._skip('no result in record')
                continue
            if 'predBGs' not in sug or 'bg' not in sug:
                self._skip('loop aborted by AAPS (no bg/predBGs): ' + str(sug.get('reason', ''))[:60])
                continue
            now_ms = parse_iso(stamp)
            console_error = sug.get('consoleError') or []
            if isinstance(console_error, str):
                console_error = console_error.split('\n')

            glucose_status = self.glucose.status_at(now_ms, rec)
            if glucose_status is None:
                self._skip('no CGM value within 12 min')
                continue
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

            meal_data = self.meal_data(now_ms, sug, iob_array[0]['lastBolusTime'])

            utc_offset = rec.get('utcOffset', sug.get('utcOffset', 0)) or 0
            utc_offset_h = utc_offset / 60.0 if abs(utc_offset) > 24 else float(utc_offset)

            pump_status = str(((rec.get('pump') or {}).get('status') or {}).get('status', ''))
            micro_bolus_allowed = not ('Open' in pump_status or 'Suspend' in pump_status or 'Disabled' in pump_status)

            yield {
                'record': rec, 'suggested': sug, 'stamp': stamp, 'now_ms': now_ms, 'utcOffset': utc_offset_h,
                'glucose_status': glucose_status, 'iob_array': iob_array, 'currenttemp': currenttemp,
                'lastTempAge': last_temp_age, 'profile': profile, 'activity_monitor': activity_monitor,
                'autosens_data': autosens_data, 'meal_data': meal_data, 'microBolusAllowed': micro_bolus_allowed,
                'console_error': console_error, 'autoISF_version': version,
            }
        if self.skipped:
            self.log_msg('\nNightscout loops skipped: ' + ', '.join(f'{v} x {k}' for k, v in self.skipped.items()))

    def meal_data(self, now_ms, sug, last_bolus_time):
        """oref meal data: COB as computed by AAPS, carbs entered during the last 6 h, deviation slopes default"""
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
        return {'carbs': round(carbs, 1), 'mealCOB': round(float(cob), 1), 'lastCarbTime': last_carb_time,
                'lastBolusTime': last_bolus_time, 'slopeFromMaxDeviation': 0, 'slopeFromMinDeviation': 999,
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
