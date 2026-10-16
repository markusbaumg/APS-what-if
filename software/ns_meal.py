"""
ns_meal.py  -  rebuild the oref1 meal_data deviation slopes (slopeFromMaxDeviation / slopeFromMinDeviation)
from Nightscout data, the way AAPS computes them in the autosens/COB worker.

    dev = DeviationHistory(glucose_history, iob_calc, devicestatus, isf_fallback)
    s_max, s_min = dev.slopes_at(now_ms)         # -> meal_data['slopeFromMaxDeviation'], ['slopeFromMinDeviation']

Why this matters: determine_basal uses  slopeFromDeviations = min(slopeFromMaxDeviation, -slopeFromMinDeviation/3)
to let the UAM carb impact decay.  With the MealData defaults (0, 999) slopeFromDeviations = -333 and the whole
UAM prediction collapses to one tick ('UAM Duration: 0 hours'), minGuardBG drops below the threshold and SMBs are
disabled - the phone printed 'UAM Duration: 3.0 hours' in 45 % of its loops and a shorter, slope-determined value
in most of the rest.

What is ported (nightscout/AndroidAPS master, identical in the fork commit 346a68550c):
    workflow/.../iob/IobCobOref1Worker.kt          per bucket: bg = bucket.recalculated, delta = bg - bucket[i+1],
                                                   avgDelta = (bg - bucket[i+3]) / 3, sens = profile.getIsfMgdlForCarbs,
                                                   bgi = -iob.activity * sens * 5 (plain calculateFromTreatmentsAndTemps),
                                                   deviation = delta - bgi, avgDeviation = round3(avgDelta - bgi);
                                                   slopes from the 11 autosens entries that follow the entry found
                                                   for 'hour ago' (bgTime + 10 s - 60 min): deviationSlope =
                                                   (ad.avgDeviation - avgDeviation) / (ad.time - bgTime) per 5 min,
                                                   slopeFromMaxDeviation = min(0, slope) whenever ad.avgDeviation
                                                   exceeds the running max (start 0), slopeFromMinDeviation =
                                                   max(0, slope) whenever it is below the running min (start 999)
    plugins/main/.../data/AutosensDataStoreObject.kt   roundUpTime (next full minute), getAutosensDataAtTime =
                                                   table[roundUpTime(newest bucket <= time)], getLastAutosensData =
                                                   newest table entry, valid for 11 min
    plugins/main/.../IobCobCalculatorPlugin.getMealDataWithWaitingForCalculationFinish   copies the slopes 1:1
    core/objects/.../ProfileSealed.getIsfMgdlForCarbs  -> OpenAPSAutoISFPlugin.getAverageIsfMgdl = mean of the
                                                   variable_sens cache over the last 24 h; the phone uploads that
                                                   value as suggested.isfMgdlForCarbs, so the value of the newest
                                                   loop at or before the bucket time is used, the profile ISF when
                                                   no loop is known yet

The autosensDataTable of the app is keyed by roundUpTime(bucket timestamp) and an entry is never recomputed
once it exists.  With the 'moving' 5-minute grid of ns_glucose (anchored at the newest reading) a new key
appears with every 1-minute Libre reading, so the table is per-minute dense and the 11 entries after 'hour ago'
are the minutes 59..49 before the bucket - this is reproduced literally: the table is rebuilt incrementally,
one reading at a time, from the oldest loaded reading up to the loop's newest reading (DeviationHistory).

Validation: python ns_meal.py --validate <start ISO> <end ISO>   compares, per loop, the UAM duration that the
rebuilt slopes produce (oref1 formula with uci = round(min(delta, short_avgdelta) - bgi, 1), bgi from
devicestatus.openaps.iob.activity and suggested.variable_sens) with the consoleError line
'UAM Impact: X mmol per 5m; UAM Duration: Y hours' of the phone.  The printed duration is only informative
when 0 < duration < 3.0 h (then it pins slopeFromDeviations to an interval, often < 0.01 wide); 3.0 h means
slope > -uci/35 and 0.0 h means uci <= 0 or slope <= -uci.

Result over the cached week 2026-08-31..09-08 (9440 loops): the rebuilt slopes reproduce the printed duration
exactly in 53 % of all loops and in 20.5 % of the informative ones; they follow the phone's values in sign,
size and trend (typically within 0.05..0.15 per 5 min) but not inside the narrow interval, and no variant
tried (fixed 5-min grids, 5-min instead of 1-min table walk, raw instead of smoothed bucket values) does
better - the residual is the +-1 mg/dl precision of the rebuilt smoothed 5-min series over the 49..59 min
baseline, which the 2-decimal slope resolution amplifies.  Therefore slopes_at() accepts the loop's own uci
and the printed duration and pins the rebuilt pair to the nearest candidate that reproduces the duration
(calibrate_slopes); after that 99.2 % of the loops reproduce the printed duration (the rest are loops whose
rebuilt uci differs from the phone's by 0.1).  The slopes are loop INPUTS (they do not depend on the settings
under test), so reading them back from the original loop's echo is as legitimate as taking the activity
monitor state from the echo.  The remaining freedom inside the interval shows up as +-1..3 mg/dl differences
in the UAM prediction curve of about half the loops.

Standard library only.
"""
import bisect
import math
import sys

_MIN = 60000
_HOUR = 3600000


def _kround(x):
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def round_up_time(ms):
    """AutosensDataStoreObject.roundUpTime: next full minute"""
    ms = int(ms)
    return ms if ms % _MIN == 0 else (ms // _MIN + 1) * _MIN


class DeviationHistory:
    def __init__(self, glucose_history, iob_calc, devicestatus=None, isf_fallback=None):
        """glucose_history: ns_glucose.GlucoseHistory; iob_calc: ns_iob.IobCalculator;
        devicestatus: raw NS records (for suggested.isfMgdlForCarbs); isf_fallback(ms) -> profile ISF mg/dl"""
        self.gh = glucose_history
        self.iob = iob_calc
        self.isf_fallback = isf_fallback
        pts = {}
        for r in devicestatus or []:
            sug = ((r.get('openaps') or {}).get('suggested') or {})
            v = sug.get('isfMgdlForCarbs')
            stamp = sug.get('deliverAt') or sug.get('timestamp')
            if isinstance(v, (int, float)) and v > 0 and stamp:
                from ns_client import parse_iso
                pts[parse_iso(stamp)] = float(v)
        self.isf_ts = sorted(pts)
        self.isf_val = [pts[t] for t in self.isf_ts]
        self.table = {}                      # key (ms, full minute) -> entry dict
        self.keys = []                       # sorted keys
        self._next_reading = 0               # index into glucose_history.ts of the next reading to process

    # ------------------------------------------------------------------ inputs
    def sens_for_carbs(self, ms):
        i = bisect.bisect_right(self.isf_ts, ms) - 1
        if i >= 0 and ms - self.isf_ts[i] <= 24 * _HOUR:
            return self.isf_val[i]
        if self.isf_fallback is not None:
            return float(self.isf_fallback(ms))
        return self.isf_val[0] if self.isf_val else 0.0

    def activity_at(self, ms):
        return self.iob.iob_at(ms, ms, 1.0)['activity']

    # ------------------------------------------------------------------ the worker
    def _process_reading(self, idx):
        """IobCobOref1Worker run after the CGM reading idx arrived: add every bucket whose key is missing"""
        data = self.gh.buckets_at(self.gh.ts[idx])
        if not data or len(data) < 4:
            return
        size = len(data)
        for i in range(size - 4, -1, -1):
            key = round_up_time(data[i].ts)
            if key in self.table:
                continue
            bg = data[i].recalculated
            if bg < 39 or data[i + 3].recalculated < 39:
                continue
            delta = bg - data[i + 1].recalculated
            avg_delta = (bg - data[i + 3].recalculated) / 3.0
            sens = self.sens_for_carbs(key)
            activity = self.activity_at(key)
            bgi = -activity * sens * 5
            deviation = delta - bgi
            avg_deviation = _kround((avg_delta - bgi) * 1000) / 1000.0
            s_max, s_min = 0.0, 999.0
            if i < size - 16:
                hour_ago = key + 10 * 1000 - _HOUR
                prev = None
                for b in data:                               # findPreviousTimeFromBucketedData
                    if b.ts <= hour_ago:
                        prev = b.ts
                        break
                if prev is not None:
                    hkey = round_up_time(prev)
                    if hkey in self.table:
                        max_dev, min_dev = 0.0, 999.0
                        idx0 = bisect.bisect_left(self.keys, hkey)
                        for past in range(1, 12):
                            if idx0 + past >= len(self.keys):
                                break                        # the app would crash here (NPE); nothing to compare
                            ad = self.table[self.keys[idx0 + past]]
                            slope = (ad['avgDeviation'] - avg_deviation) / (ad['time'] - key) * 1000 * 60 * 5
                            if ad['avgDeviation'] > max_dev:
                                s_max = min(0.0, slope)
                                max_dev = ad['avgDeviation']
                            if ad['avgDeviation'] < min_dev:
                                s_min = max(0.0, slope)
                                min_dev = ad['avgDeviation']
            entry = {'time': key, 'bg': bg, 'delta': delta, 'avgDelta': avg_delta, 'sens': sens, 'activity': activity,
                     'bgi': bgi, 'deviation': deviation, 'avgDeviation': avg_deviation,
                     'slopeFromMaxDeviation': s_max, 'slopeFromMinDeviation': s_min}
            self.table[key] = entry
            bisect.insort(self.keys, key)

    def advance_to(self, now_ms):
        """process every reading at or before now_ms that has not been processed yet"""
        n = bisect.bisect_right(self.gh.ts, int(now_ms))
        while self._next_reading < n:
            self._process_reading(self._next_reading)
            self._next_reading += 1

    def last_entry(self, now_ms):
        self.advance_to(now_ms)
        if not self.keys:
            return None
        e = self.table[self.keys[-1]]
        if e['time'] < now_ms - 11 * _MIN:
            return None
        return e

    def slopes_at(self, now_ms, uci=None, printed_duration=None):
        """(slopeFromMaxDeviation, slopeFromMinDeviation) as MealData carries them at now_ms.
        When the loop's own uci (mg/dl per 5 min, as determine_basal computes it) and the UAM duration the
        phone printed ('UAM Impact: ...; UAM Duration: Y hours') are given, the rebuilt pair is pinned to the
        nearest pair that reproduces that duration (see calibrate_slopes); the rebuilt pair is kept when it
        already does."""
        e = self.last_entry(int(now_ms))
        if e is None:
            s_max, s_min = 0.0, 999.0
        else:
            s_max, s_min = e['slopeFromMaxDeviation'], e['slopeFromMinDeviation']
        if uci is not None and printed_duration is not None:
            s_max, s_min = calibrate_slopes(uci, s_max, s_min, printed_duration)
        return s_max, s_min


# ----------------------------------------------------------------------------- calibration by the phone's echo
_CANDIDATES = None


def _candidates():
    """every value slopeFromDeviations can take in determine_basal: round(sMax, 2) or -round(sMin, 2) / 3"""
    global _CANDIDATES
    if _CANDIDATES is None:
        c = {(-k / 100.0, 'max') for k in range(0, 401)}
        c |= {(-k / 300.0, 'min') for k in range(0, 1201)}
        _CANDIDATES = sorted(c)
    return _CANDIDATES


def effective_slope(s_max, s_min):
    return min(round(s_max, 2), -round(s_min, 2) / 3)


def calibrate_slopes(uci, s_max, s_min, printed_duration):
    """Pin the rebuilt slope pair to the phone's printed UAM duration.  The duration is a deterministic
    function of uci and slopeFromDeviations = min(round(sMax,2), -round(sMin,2)/3) (uam_duration below), so the
    printed value confines the phone's slopeFromDeviations to a narrow interval (often < 0.01 wide).  If the
    rebuilt pair already lies in it, it is returned unchanged; otherwise the candidate value nearest to the
    rebuilt one that reproduces the printed duration is imposed on the pair.  uci <= 0 leaves the pair alone
    (the slopes do not matter then)."""
    if uci is None or uci <= 0:
        return s_max, s_min
    cur = effective_slope(s_max, s_min)
    if abs(uam_duration_from_slope(uci, cur) - printed_duration) < 0.01:
        return s_max, s_min
    best = None
    for s, family in _candidates():
        if abs(uam_duration_from_slope(uci, s) - printed_duration) < 0.01:
            d = abs(s - cur)
            if best is None or d < best[0]:
                best = (d, s, family)
    if best is None:
        return s_max, s_min
    s = best[1]
    if best[2] == 'max':
        s_max = s
        if -round(s_min, 2) / 3 < s:
            s_min = -3 * s
    else:
        s_min = -3 * s
        if round(s_max, 2) < s:
            s_max = math.ceil(s * 100 - 1e-9) / 100.0
    return s_max, s_min


def _round_half_up(x, digits=1):
    """Kotlin round(x, 1) of DetermineBasalAutoISF (Math.round: half up); Python's round() is half-to-even and
    would turn the durations 0.25 / 1.25 / 2.25 h (3, 15, 27 ticks) into 0.2 / 1.2 / 2.2 instead of the printed
    0.3 / 1.3 / 2.3"""
    scale = 10 ** digits
    return math.floor(x * scale + 0.5) / scale


def uam_duration_from_slope(uci, slope, ticks=48):
    dur = 0.0
    if uci <= 0:
        return 0.0
    for n in range(1, ticks + 1):                 # UAMpredBGs starts as [bg], so its length is 1 at the first tick
        pred_slope = max(0.0, uci + n * slope)
        pred_max = max(0.0, uci * (1 - n / max(3 * 60 / 5, 1)))
        pred = min(pred_slope, pred_max)
        if pred > 0:
            dur = _round_half_up((n + 1) * 5 / 60)
    return dur


# ----------------------------------------------------------------------------- oref1 UAM duration (for validation)
def uam_duration(uci, s_max, s_min, ticks=48):
    """UAMduration as DetermineBasalAutoISF / determine_basal.py compute it"""
    return uam_duration_from_slope(uci, effective_slope(s_max, s_min), ticks)


_re_uam = None


def printed_uam(console_error):
    """(uci as printed [mg/dl per 5 min], UAM duration [h]) from the consoleError of a loop, or (None, None)"""
    global _re_uam
    if _re_uam is None:
        import re
        _re_uam = re.compile(r'UAM Impact:\s*(-?[0-9.]+) (mmol|mg/dL) per 5m; UAM Duration:\s*([0-9.]+) hours')
    for line in console_error or []:
        m = _re_uam.search(str(line))
        if m:
            uci = float(m.group(1)) * (18.0 if m.group(2) == 'mmol' else 1.0)
            return uci, float(m.group(3))
    return None, None


# ----------------------------------------------------------------------------- validation CLI
def validate(start_iso, end_iso, url='ns:', verbose=True):
    import re
    import time
    import ns_glucose
    import ns_iob
    import ns_profile
    from ns_client import NightscoutClient, parse_iso, to_iso
    from ns_source import load_settings, LOOKBACK_MS

    settings = load_settings()
    cfg = settings.get('nightscout', {})
    if url in ('ns', 'ns:', ''):
        url = cfg.get('url')
    ns = NightscoutClient(url, cache_dir=cfg.get('cache_dir'), token=cfg.get('token'),
                          api_secret_sha1=cfg.get('api_secret_sha1'), refresh_minutes=10 ** 7, verbose=verbose)
    start_ms, end_ms = parse_iso(start_iso), parse_iso(end_iso)
    devicestatus = [r for r in ns.devicestatus(start_ms - 2 * _HOUR, end_ms)
                    if isinstance(r.get('openaps'), dict) and isinstance(r['openaps'].get('suggested'), dict)]
    treatments = ns.treatments(start_ms - LOOKBACK_MS, end_ms)
    entries = ns.entries(start_ms - LOOKBACK_MS, end_ms)
    profiles = ns_profile.ProfileBuilder(ns.profiles(), treatments, settings)
    iob = ns_iob.IobCalculator(treatments, profiles.basal_at, settings, dia_at=profiles.dia_hours, devicestatus=devicestatus)
    gh = ns_glucose.GlucoseHistory(entries, devicestatus, settings)
    dev = DeviationHistory(gh, iob, devicestatus, lambda ms: profiles.active_profile(ms).isf_mgdl(ms))

    re_uam = re.compile(r'UAM Impact:\s*(-?[0-9.]+) (mmol|mg/dL) per 5m; UAM Duration:\s*([0-9.]+) hours')
    n = n_cmp = n_ok = n_ok_short = n_info = n_info_ok = 0
    n_skip = n_uci_bad = n_cal_ok = 0
    uncal = []
    worst = []
    seen = set()
    t0 = time.time()
    for rec in devicestatus:
        sug = rec['openaps']['suggested']
        stamp = sug.get('deliverAt')
        if not stamp or 'bg' not in sug or stamp in seen:
            continue
        now_ms = parse_iso(stamp)
        if now_ms < start_ms or now_ms >= end_ms:
            continue
        seen.add(stamp)
        n += 1
        printed = None
        for line in sug.get('consoleError') or []:
            m = re_uam.search(str(line))
            if m:
                printed = float(m.group(3))
                break
        gs = gh.status_at(now_ms, rec)
        if printed is None or gs is None or 'variable_sens' not in sug:
            n_skip += 1
            continue
        s_max, s_min = dev.slopes_at(now_ms)
        act = ((rec['openaps'].get('iob') or {}).get('activity'))
        if act is None:
            act = iob.iob_at(now_ms, now_ms, 1.0)['activity']
        sens = float(sug['variable_sens'])
        bgi = round(-act * sens * 5, 2)
        min_delta = min(gs['delta'], gs['short_avgdelta'])
        uci = round(min_delta - bgi, 1)
        got = uam_duration(uci, s_max, s_min)
        c_max, c_min = calibrate_slopes(uci, s_max, s_min, printed)
        if abs(uam_duration(uci, c_max, c_min) - printed) < 0.01:
            n_cal_ok += 1
        else:
            uncal.append((stamp, printed, uci, round(s_max, 3), round(s_min, 3)))
        m_uci = re_uam.search(next(str(l) for l in sug.get('consoleError') if re_uam.search(str(l))))
        p_uci = float(m_uci.group(1)) * (18 if m_uci.group(2) == 'mmol' else 1)
        if abs(p_uci - uci) > (1.0 if m_uci.group(2) == 'mmol' else 0.06):
            n_uci_bad += 1
        n_cmp += 1
        ok = abs(got - printed) < 0.01
        if ok:
            n_ok += 1
        if abs(got - printed) <= 0.1:
            n_ok_short += 1
        if 0 < printed < 3.0:
            n_info += 1
            if ok:
                n_info_ok += 1
        if not ok:
            worst.append((abs(got - printed), stamp, printed, got, uci, round(s_max, 3), round(s_min, 3)))
    dt = time.time() - t0
    if verbose:
        print(f'{n} loops ({n_skip} skipped: no UAM line / no CGM), {n_cmp} compared in {dt:.1f} s')
        print(f'uci differs from printed UAM Impact: {n_uci_bad}/{n_cmp}')
        print(f'UAM duration exact:      {n_ok}/{n_cmp} = {100.0 * n_ok / max(n_cmp, 1):.1f} %')
        print(f'UAM duration within 0.1: {n_ok_short}/{n_cmp} = {100.0 * n_ok_short / max(n_cmp, 1):.1f} %')
        print(f'informative loops (0 < printed < 3.0 h, slope decides): {n_info_ok}/{n_info} = '
              f'{100.0 * n_info_ok / max(n_info, 1):.1f} %')
        print(f'after calibration by the printed duration: {n_cal_ok}/{n_cmp} = {100.0 * n_cal_ok / max(n_cmp, 1):.1f} %')
        for u in uncal[:12]:
            print(f'  not calibratable: {u[0]}  printed {u[1]:.1f}  uci {u[2]:.1f}  sMax {u[3]}  sMin {u[4]}')
        worst.sort(reverse=True)
        for w in worst[:15]:
            print(f'  {w[1]}  printed {w[2]:.1f}  rebuilt {w[3]:.1f}  uci {w[4]:.1f}  sMax {w[5]}  sMin {w[6]}')
    return {'loops': n, 'compared': n_cmp, 'exact': n_ok, 'within_0.1': n_ok_short, 'informative': n_info,
            'informative_ok': n_info_ok, 'calibrated_ok': n_cal_ok}


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == '--validate':
        validate(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else 'ns:')
    else:
        print('usage: python ns_meal.py --validate <start ISO> <end ISO> [url]')
