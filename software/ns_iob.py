"""
ns_iob.py  -  AAPS insulin-on-board calculation from Nightscout treatments.

Contract (see NIGHTSCOUT_SOURCE.md):
    calc = IobCalculator(treatments, basal_at, settings, dia_at=None, devicestatus=None)
        treatments   : ascending NS treatment records covering at least DIA hours before the first loop
        basal_at     : callable(ms) -> scheduled profile basal U/h (from ns_profile.ProfileBuilder)
        settings     : the ns_settings.json dict  (insulin.peak_minutes, insulin.min_dia_hours, insulin.dia_hours)
        dia_at       : callable(ms) -> DIA hours of the active profile (None -> settings.insulin.dia_hours or 6)
        devicestatus : optional NS devicestatus records of the window; only used to recover the *planned*
                       duration of temp basals (see "planned temp basal end" below). Everything works without it.
    calc.iob_array(now_ms, sensitivity_ratio=1.0)   -> list of 48 dicts, 5 min apart, starting at now
    calc.iob_at(t_ms, now_ms, sensitivity_ratio)     -> one dict
    calc.last_bolus_time(now_ms)                     -> ms (0 if none within DIA, like AAPS)
    calc.current_temp(now_ms)                        -> {'rate': U/h, 'duration': min left (0 = none), 'start': ms}
    calc.last_temp(now_ms)                           -> {'date': start ms, 'rate': U/h, 'duration': full minutes}

Each IOB dict: {'time', 'iob', 'activity', 'basaliob', 'bolussnooze', 'netbasalinsulin', 'hightempinsulin',
                'iobWithZeroTemp': {'iob', 'activity', 'basaliob'}}

Ported from the AAPS master sources (fetched 2026-09-07 from github.com/nightscout/AndroidAPS, re-checked line by
line against the raw files on 2026-09-08: TemporaryBasalExtension.kt, IobCobCalculatorPlugin.kt, TB.kt,
InsulinOrefBasePlugin.kt, IobTotalExtension.kt, OpenAPSAutoISFPlugin.kt):
    InsulinOrefBasePlugin.iobCalcForTreatment   exponential curve  tau = tp*(1-tp/td)/(1-2tp/td), a = 2tau/td,
                                                s = 1/(1-a+(1+a)e^(-td/tau)); IOB forced to 0 for t >= DIA;
                                                DIA = max(profile DIA, HardLimits.MIN_DIA = 5 h); Lyumjev peak = 45 min
    IobCobCalculatorPlugin.calculateIobFromBolusToTime
                                                boluses with  toTime - DIA <= timestamp < toTime, isValid, not PRIMING;
                                                lastBolusTime = newest such bolus with amount > 0 (0 if none within DIA);
                                                bolussnooze: non-SMB boluses evaluated at (toTime-ts)*divisor (divisor 2.0)
    TemporaryBasalExtension.iobCalc (autosens variant)
                                                realDuration = roundToInt((min(time, end) - start)/60000);
                                                ceil(realDuration/5) chunks, mini bolus at chunk centre of
                                                (rate - profile basal(centre) * sensitivity_ratio) * spacing/60 U
                                                (percent temps: rate%/100 * basal - basal * ratio); chunk counts when
                                                centre > time - DIA and centre <= time
    IobCobCalculatorPlugin.getCalculationToTimeTempBasals
                                                temps with  toTime - DIA <= start <= toTime; a temp whose end is after
                                                `now` is cut at `now` (so the future array points assume the running
                                                temp stops at now and no other temp follows)
    IobCobCalculatorPlugin.calculateFromTreatmentsAndTemps (SMB variant)
                                                bolusIob.round(), basalIob.round(), combine(...).round()
                                                iobWithZeroTemp = combine(bolusIob, basalIob + TB(now+1min, 240 min, 0 U/h))
    IobCobCalculatorPlugin.calculateIobArrayForSMB
                                                48 points t = now + i*5 min (no rounding of now)
    IobTotalExtension.round / combine           Round.roundTo(x, 0.001) for iob/basaliob/netbasalinsulin/hightempinsulin,
                                                0.0001 for activity/bolussnooze; roundToLong = round half up
    OpenAPSAutoISFPlugin (currentTemp)          rate = convertedToAbsolute(now), duration = plannedRemainingMinutes
                                                = max(round((end - now)/60000), 0) with kotlin round = half to even
    LoopPlugin.buildAndStoreDeviceStatus        devicestatus.openaps.iob = iobArray[0] of the last run (json: iob,
                                                basaliob, activity, time = lastAPSRun) -> this is what --validate compares
    oref0 lib/iob/index.js                      lastTemp = newest temp with start <= now; duration = full minutes it ran
                                                (cut by the next temp), rounded to 2 decimals

Nightscout record mapping
    boluses      eventType Correction Bolus / Meal Bolus / Bolus / Snack Bolus / SMB / Combo Bolus (any record with
                 insulin > 0 that is not Prime / Site Change / Insulin Change / Temp Basal), isValid != false,
                 type != PRIMING; SMB = type 'SMB' (or eventType 'SMB'); time = `date` ms, else created_at, else mills
    temp basals  eventType 'Temp Basal' (incl. type PUMP_SUSPEND = 0 U/h temps of a suspended pod); rate: `absolute`
                 or `rate` U/h, `percent` (+100) for percent temps; duration from durationInMilliseconds (AAPS uploads
                 the real length in ms - a temp that ran 15 s has duration 0 min but 15297 ms), else duration*60000;
                 a record with 0 ms and no endmills is a cancel marker (ends the previous temp, contributes nothing);
                 a later temp always ends the earlier one at its start (NS already carries the cut duration, enforced
                 again here); records are de-duplicated by pumpId+timestamp (else identifier/_id)
    planned temp basal end
                 NS stores only the *final* length of a temp (cut when the next one started). AAPS at loop time saw
                 the planned end (e.g. 120 min) -> currenttemp.duration. It is recovered from `devicestatus`:
                 openaps.enacted.duration (exact): every temp is matched to the *latest* enacting loop (same rate,
                 deliverAt within 5 min before the temp start) that has not already started another temp;
                 else pump.extended.TempBasalRemaining at pump.clock (+-30 s). Without devicestatus the recorded end
                 is used (current_temp.duration then underestimates the minutes left of a temp that was later
                 replaced; IOB is not affected - AAPS cuts the running temp at now anyway).

Nightscout data gaps seen in the cached week (2026-08-31 .. 09-07) and how they are handled
    * devicestatus is uploaded twice for most loops (same deliverAt, one copy with pump.clock before and one after
      the pump command of that loop). Enacted records are therefore de-duplicated by deliverAt - otherwise the
      second copy claimed the next temp of the same rate (was: 100 wrong planned durations in 3 days).
    * The pump status copy taken 0.03 .. 0.6 s after a temp was cancelled/replaced still shows the old temp
      (pump.extended is AAPS's expected pump state, not yet updated) while the DB row - and NS - is already cut.
      No loop runs at such an instant (deliverAt always precedes its own pump command), so --validate counts these
      snapshots separately (136 of 10414 in the week) instead of as mismatches.
    * A few loops that enacted a temp uploaded no devicestatus record at all (e.g. 2026-09-03 08:12, the temp
      after a meal bolus); their planned duration then comes from TempBasalRemaining of the following records.
    * Omnipod Dash replaces a temp by cancel + set: the recorded end of the old temp is 0.1 .. 1 s before the start
      of the new one. AAPS's own DB has the same gap, so it is kept as is.

Validation (autosens ratio from consoleError, now = suggested.deliverAt which is the `currentTime` of the AAPS
run; devicestatus.openaps.iob.time = lastAPSRun is 0.1 .. 4 s later). Whole cached week 2026-08-31 .. 09-07 UTC,
11136 loops, 1128 boluses, 1525 temp basals, 0.8 s:
    iob       median 0.000 U   p95 0.000 U   max 0.002 U   0 loops > 0.05 U   (against openaps.iob.iob = suggested.IOB)
    basaliob  median 0.000 U   p95 0.000 U   max 0.002 U
    activity  median 0.0000    p95 0.0000    max 0.0001 U/min
    lastBolusTime: 'last bolus X.Xm ago' reproduced in 5365/5365 loops that print it
    current_temp.duration vs pump.extended.TempBasalRemaining at pump.clock: 10278/10414 within 1 min,
    136 post-cut snapshots (see above), 0 real mismatches
    per day 2026-09-02 / 09-04 / 09-06: max |iob err| 0.002 / 0.001 / 0.001 U, lastBolusTime 671/671, 527/527,
    882/882, current_temp 1325+12 / 1304+17 / 1448+14 of 1337 / 1321 / 1462 (ok + snapshots), 0 real mismatches.
Residual differences and why: the remaining +-0.001 .. 0.002 U / 0.0001 U/min are the last-digit rounding of AAPS
(Round.roundTo on values that sit at a rounding tie, plus the few seconds between deliverAt and the exact
millisecond AAPS used for `now`, which shifts a 5-min-chunk boundary of the running temp basal now and then).
Nothing structural is left in the cached week: every bolus and temp basal of those days is on Nightscout.
Known simplification: AAPS evaluates profile.getBasal(chunk centre) on the profile that was active at the temp's
*start* (profileFunction.getProfile(t.timestamp)); this module asks basal_at(centre), i.e. the profile active at
the chunk centre. They differ only for chunks of a temp that spans a profile switch (e.g. the 130 % boost
automation) - one such switch in the cached week, no visible effect at the 0.001 U level.

Run:  python ns_iob.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z [url] [--now iobtime]
"""
import bisect
import json
import math
import os
import re
import sys

BOLUS_IGNORE_EVENTS = ('Prime', 'Site Change', 'Insulin Change', 'Pump Battery Change', 'Sensor Change',
                       'Sensor Start', 'Temp Basal', 'Temporary Target', 'Profile Switch', 'Note', 'Announcement')
_MIN = 60000
ZERO_TEMP_MINUTES = 240


# ----------------------------------------------------------------------------- helpers
def _record_ms(rec):
    v = rec.get('date')
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    v = rec.get('created_at')
    if isinstance(v, str) and v:
        from ns_client import parse_iso
        return parse_iso(v)
    if isinstance(rec.get('mills'), (int, float)):
        return int(rec['mills'])
    return None


def _round_to(x, step):
    """AAPS Round.roundTo: (x/step).roundToLong() (ties away to +inf) * step, exact decimal"""
    if x == 0.0:
        return 0.0
    return round(math.floor(x / step + 0.5) * step, 6)


def _round_half_even(x):
    """kotlin.math.round(Double): ties to even (python round does the same)"""
    return int(round(x))


def _round_to_int(x):
    """kotlin Double.roundToInt(): ties towards positive infinity"""
    return int(math.floor(x + 0.5))


class _Curve:
    """constants of the AAPS exponential insulin model for one (DIA, peak) pair"""
    __slots__ = ('td', 'tau', 'a', 's')

    def __init__(self, td_minutes, tp):
        td = float(td_minutes)
        self.td = td
        self.tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
        self.a = 2 * self.tau / td
        self.s = 1 / (1 - self.a + (1 + self.a) * math.exp(-td / self.tau))

    def iob_activity(self, amount, t):
        """(iobContrib, activityContrib) of `amount` U given t minutes ago (InsulinOrefBasePlugin.iobCalcForTreatment)"""
        if t >= self.td or amount == 0.0:
            return 0.0, 0.0
        tau, td, a, s = self.tau, self.td, self.a, self.s
        e = math.exp(-t / tau)
        act = amount * (s / (tau * tau)) * t * (1 - t / td) * e
        iob = amount * (1 - s * (1 - a) * ((t * t / (tau * td * (1 - a)) - t / tau - 1) * e + 1))
        return iob, act


# ----------------------------------------------------------------------------- the calculator
class IobCalculator:
    def __init__(self, treatments, basal_at, settings, dia_at=None, devicestatus=None):
        ins = (settings or {}).get('insulin', {}) or {}
        self.peak = float(ins.get('peak_minutes') or 45)
        self.min_dia = float(ins.get('min_dia_hours') or 5.0)
        self.default_dia = float(ins.get('dia_hours') or 6.0)
        self.snooze_divisor = float(((settings or {}).get('safety', {}) or {}).get('bolussnooze_dia_divisor', 2.0) or 2.0)
        self.basal_at = basal_at
        self._dia_at = dia_at
        self._curves = {}
        self._basal_cache = {}
        self._ctx = None                              # per (now, ratio) preparation, see _prepare
        self._parse_treatments(treatments or [])
        if devicestatus:
            self.apply_planned_durations(devicestatus)

    # ------------------------------------------------------------------ setup
    def dia_hours(self, ms):
        dia = None
        if self._dia_at is not None:
            try:
                dia = self._dia_at(ms)
            except Exception:
                dia = None
        if not dia:
            dia = self.default_dia
        return max(float(dia), self.min_dia)

    def _curve(self, dia_hours):
        td = dia_hours * 60.0
        c = self._curves.get(td)
        if c is None:
            c = self._curves[td] = _Curve(td, self.peak)
        return c

    _BASAL_CACHE_MAX = 200000                     # ~1 700 minute keys per replayed day; guard against unbounded growth

    def _basal(self, ms):
        # keyed by minute: the profile basal only changes on schedule boundaries / profile switches, and the chunk
        # centres of the zero-temp variants differ per array point (per-millisecond keys grew by ~1 100 per loop)
        key = int(ms) // _MIN
        b = self._basal_cache.get(key)
        if b is None:
            if len(self._basal_cache) >= self._BASAL_CACHE_MAX:
                self._basal_cache.clear()
            b = self._basal_cache[key] = float(self.basal_at(int(ms)))
        return b

    def _parse_treatments(self, treatments):
        boluses = {}
        temps = {}
        for rec in treatments:
            if not isinstance(rec, dict) or rec.get('isValid') is False:
                continue
            et = rec.get('eventType') or ''
            ms = _record_ms(rec)
            if ms is None:
                continue
            key = (rec.get('pumpId'), ms) if rec.get('pumpId') is not None else (rec.get('identifier') or rec.get('_id') or ms)
            if et == 'Temp Basal':
                if 'durationInMilliseconds' in rec and rec['durationInMilliseconds'] is not None:
                    dur = int(rec['durationInMilliseconds'])
                elif rec.get('duration') is not None:
                    dur = int(round(float(rec['duration']) * _MIN))
                else:
                    dur = 0
                if rec.get('percent') is not None and rec.get('absolute') is None:
                    is_abs, rate = False, float(rec['percent']) + 100.0
                elif rec.get('absolute') is not None:
                    is_abs, rate = True, float(rec['absolute'])
                elif rec.get('rate') is not None:
                    is_abs, rate = True, float(rec['rate'])
                else:
                    is_abs, rate = True, 0.0
                cancel = dur <= 0 and not rec.get('endmills')
                temps[key] = {'start': ms, 'end': ms + max(dur, 0), 'planned_end': None, 'rate': rate,
                              'abs': is_abs, 'cancel': cancel, 'chunks': None}
                continue
            insulin = rec.get('insulin')
            if insulin is None or et in BOLUS_IGNORE_EVENTS or str(rec.get('type', '')).upper() == 'PRIMING':
                continue
            try:
                amount = float(insulin)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            is_smb = str(rec.get('type', '')).upper() == 'SMB' or et == 'SMB'
            boluses[key] = (ms, amount, is_smb)

        self.boluses = sorted(boluses.values())
        self.bolus_ts = [b[0] for b in self.boluses]
        self.temps = sorted(temps.values(), key=lambda t: t['start'])
        # a later temp ends the earlier one at its start; cancel markers only end the previous temp
        for i, t in enumerate(self.temps):
            if i + 1 < len(self.temps):
                t['end'] = min(t['end'], self.temps[i + 1]['start'])
            t['end'] = max(t['end'], t['start'])
        self.temps = [t for t in self.temps if not t['cancel']]
        self.temp_ts = [t['start'] for t in self.temps]

    def apply_planned_durations(self, devicestatus):
        """recover the planned end of every temp basal from devicestatus (openaps.enacted, pump.extended)"""
        from ns_client import parse_iso
        if not self.temps:
            return
        # 1. exact: the requested duration of the loop that enacted the temp. A temp is caused by the *latest*
        #    enacting loop before its start (the pump command follows deliverAt by a few seconds). AAPS uploads
        #    most devicestatus records twice (same deliverAt, different pump.clock) -> de-duplicate by loop time,
        #    otherwise the second copy would claim the next temp of the same rate.
        enacted = {}
        for rec in devicestatus:
            en = (rec.get('openaps') or {}).get('enacted') or {}
            if not en or en.get('rate') is None or en.get('duration') is None:
                continue
            try:
                rate, dur = float(en['rate']), float(en['duration'])
            except (TypeError, ValueError):
                continue
            if dur <= 0 or rate < 0:                    # rate -1 / duration -1 = cancel
                continue
            stamp = en.get('deliverAt') or en.get('timestamp') or rec.get('created_at')
            if not stamp:
                continue
            enacted[parse_iso(stamp)] = (rate, dur)
        en_ts = sorted(enacted)
        used = set()                                     # one enacting loop starts at most one temp
        for t in self.temps:
            if not t['abs']:
                continue
            i = bisect.bisect_right(en_ts, t['start'] + 2000)
            while i > 0 and en_ts[i - 1] >= t['start'] - 5 * _MIN:
                i -= 1
                rate, dur = enacted[en_ts[i]]
                if i not in used and abs(rate - t['rate']) < 0.001:
                    used.add(i)
                    t['planned_end'] = t['start'] + int(dur * _MIN)
                    break
        # 2. approximate: minutes remaining reported by the pump status of every devicestatus record
        for rec in devicestatus:
            pump = rec.get('pump') or {}
            ext = pump.get('extended') or {}
            rem = ext.get('TempBasalRemaining')
            clock = pump.get('clock') or rec.get('created_at')
            if rem is None or not clock:
                continue
            cms = parse_iso(clock)
            t = self._temp_active_at(cms)
            if t is not None and t['planned_end'] is None:
                # TempBasalRemaining is floor((end - clock) / min) of a temp planned as whole minutes from its start:
                # rebuild the end on the minute grid of the start (cms + rem may be up to 1 min short)
                t['planned_end'] = t['start'] + int(round((cms + int(rem) * _MIN + 30000 - t['start']) / _MIN)) * _MIN

    # ------------------------------------------------------------------ temp basal chunks (TB.iobCalc)
    def _make_chunks(self, start, end):
        """[(centre_ms, spacing_min, profile_basal_at_centre)] of TB.iobCalc for a temp running start..end"""
        real_minutes = _round_to_int((end - start) / 60000.0)
        if real_minutes <= 0:
            return []
        n = int(math.ceil(real_minutes / 5.0))
        spacing = real_minutes / float(n)
        out = []
        for j in range(n):
            centre = int(start + j * spacing * _MIN + 0.5 * spacing * _MIN)
            out.append((centre, spacing, self._basal(centre)))
        return out

    def _temp_chunks(self, temp, cap):
        """chunks of `temp` cut at `cap` (= min(now, end)); cached when the whole recorded temp is used"""
        if cap >= temp['end']:
            if temp['chunks'] is None:
                temp['chunks'] = self._make_chunks(temp['start'], temp['end'])
            return temp['chunks']
        return self._make_chunks(temp['start'], cap)

    # ------------------------------------------------------------------ per-loop preparation
    def _prepare(self, now_ms, ratio):
        ctx = self._ctx
        if ctx is not None and ctx['now'] == now_ms and ctx['ratio'] == ratio:
            return ctx
        dia_now = self.dia_hours(now_ms)
        ctx = {'now': now_ms, 'ratio': float(ratio), 'dia_now': dia_now, 'curve_now': self._curve(dia_now),
               'zero_curve': self._curve(self.dia_hours(now_ms + _MIN)),
               'zero_chunks': {}, 'temps': []}
        # temps known at run time (started at or before now), each with its chunks cut at now
        hi = bisect.bisect_right(self.temp_ts, now_ms)
        lo = bisect.bisect_left(self.temp_ts, now_ms - int((dia_now + 4.5) * 3600000))   # widest reach of any array point
        for temp in self.temps[lo:hi]:
            chunks = self._temp_chunks(temp, min(temp['end'], now_ms))
            if not chunks:
                continue
            dia = self.dia_hours(temp['start'])
            ctx['temps'].append((temp, chunks, self._curve(dia), int(dia * 3600000)))
        self._ctx = ctx
        return ctx

    def _zero_temp_chunks(self, ctx, t_ms):
        """chunks of the virtual 0 U/h temp basal (now + 1 min, 240 min) as seen from array point t"""
        ch = ctx['zero_chunks'].get(t_ms)
        if ch is None:
            start = ctx['now'] + _MIN
            end = start + ZERO_TEMP_MINUTES * _MIN
            ch = self._make_chunks(start, min(t_ms, end)) if start < t_ms else []
            ctx['zero_chunks'][t_ms] = ch
        return ch

    # ------------------------------------------------------------------ public API
    def iob_at(self, t_ms, now_ms, sensitivity_ratio=1.0):
        ctx = self._prepare(now_ms, sensitivity_ratio)
        ratio = ctx['ratio']
        t_ms = int(t_ms)

        # ---- boluses  (calculateIobFromBolusToTime)
        curve = ctx['curve_now']
        dia_ms = int(ctx['dia_now'] * 3600000)
        b_iob = b_act = b_snooze = 0.0
        last_bolus = 0
        lo = bisect.bisect_left(self.bolus_ts, t_ms - dia_ms)
        hi = min(bisect.bisect_left(self.bolus_ts, t_ms), bisect.bisect_right(self.bolus_ts, now_ms))
        for ts, amount, is_smb in self.boluses[lo:hi]:
            tmin = (t_ms - ts) / 60000.0
            iob, act = curve.iob_activity(amount, tmin)
            b_iob += iob
            b_act += act
            if ts > last_bolus:
                last_bolus = ts
            if not is_smb:
                b_snooze += curve.iob_activity(amount, tmin * self.snooze_divisor)[0]
        b_iob = _round_to(b_iob, 0.001)
        b_act = _round_to(b_act, 0.0001)
        b_snooze = _round_to(b_snooze, 0.0001)

        # ---- temp basals  (getCalculationToTimeTempBasals -> TB.iobCalc)
        t_basaliob = t_act = t_net = t_high = 0.0
        for temp, chunks, tcurve, tdia_ms in ctx['temps']:
            if temp['start'] > t_ms or temp['start'] < t_ms - tdia_ms:
                continue
            dia_ago = t_ms - tdia_ms
            rate, is_abs = temp['rate'], temp['abs']
            for centre, spacing, basal in chunks:
                if centre > dia_ago and centre <= t_ms:
                    net_rate = (rate - basal * ratio) if is_abs else (rate / 100.0 * basal - basal * ratio)
                    size = net_rate * spacing / 60.0
                    iob, act = tcurve.iob_activity(size, (t_ms - centre) / 60000.0)
                    t_basaliob += iob
                    t_act += act
                    t_net += size
                    if size > 0:
                        t_high += size
        t_basaliob = _round_to(t_basaliob, 0.001)
        t_act = _round_to(t_act, 0.0001)
        t_net = _round_to(t_net, 0.001)
        t_high = _round_to(t_high, 0.001)

        # ---- zero temp variant  (TB(now + 1 min, 240 min, 0 U/h) added to the rounded basal IOB)
        z_basaliob, z_act = t_basaliob, t_act
        zcurve = ctx['zero_curve']
        zdia_ago = t_ms - int(zcurve.td * _MIN)
        for centre, spacing, basal in self._zero_temp_chunks(ctx, t_ms):
            if centre > zdia_ago and centre <= t_ms:
                size = (0.0 - basal * ratio) * spacing / 60.0
                iob, act = zcurve.iob_activity(size, (t_ms - centre) / 60000.0)
                z_basaliob += iob
                z_act += act

        return {
            'time': t_ms,
            'iob': _round_to(b_iob + t_basaliob, 0.001),
            'activity': _round_to(b_act + t_act, 0.0001),
            'basaliob': t_basaliob,
            'bolussnooze': b_snooze,
            'netbasalinsulin': t_net,
            'hightempinsulin': t_high,
            'iobWithZeroTemp': {
                'iob': _round_to(b_iob + z_basaliob, 0.001),
                'activity': _round_to(b_act + z_act, 0.0001),
                'basaliob': _round_to(z_basaliob, 0.001),
            },
        }

    def iob_array(self, now_ms, sensitivity_ratio=1.0):
        now_ms = int(now_ms)
        return [self.iob_at(now_ms + i * 5 * _MIN, now_ms, sensitivity_ratio) for i in range(48)]

    def last_bolus_time(self, now_ms):
        """newest bolus (amount > 0) within DIA before now, else 0 - as in calculateIobFromBolusToTime(now)"""
        now_ms = int(now_ms)
        dia_ms = int(self.dia_hours(now_ms) * 3600000)
        hi = bisect.bisect_left(self.bolus_ts, now_ms)
        if hi == 0:
            return 0
        ts = self.bolus_ts[hi - 1]
        return ts if ts >= now_ms - dia_ms else 0

    def _temp_active_at(self, ms):
        """the temp basal whose recorded run (start <= ms < end) covers ms, else None - like the AAPS DB row"""
        i = bisect.bisect_right(self.temp_ts, ms)
        if i == 0:
            return None
        t = self.temps[i - 1]
        return t if t['start'] <= ms < t['end'] else None

    def temp_rate(self, temp, ms):
        return temp['rate'] if temp['abs'] else self._basal(ms) * temp['rate'] / 100.0

    def current_temp(self, now_ms):
        """the temp basal running at now as AAPS handed it to determine_basal (rate U/h, planned minutes left)"""
        now_ms = int(now_ms)
        t = self._temp_active_at(now_ms)
        if t is None:
            return {'rate': 0.0, 'duration': 0, 'start': 0}
        end = t['planned_end'] if t['planned_end'] is not None else t['end']
        end = max(end, t['end'])           # NS end can not exceed the planned end unless the planned one is unknown
        left = max(_round_half_even((end - now_ms) / 60000.0), 0)
        return {'rate': self.temp_rate(t, now_ms), 'duration': left, 'start': t['start']}

    def last_temp(self, now_ms):
        """oref0 lastTemp: most recent temp started at or before now, duration = full minutes it ran / will run"""
        now_ms = int(now_ms)
        i = bisect.bisect_right(self.temp_ts, now_ms)
        if i == 0:
            return {'date': 0, 'rate': 0.0, 'duration': 0}
        t = self.temps[i - 1]
        end = t['end']
        if end > now_ms and t['planned_end'] is not None:
            end = max(t['planned_end'], end)
        return {'date': t['start'], 'rate': self.temp_rate(t, t['start']),
                'duration': round((end - t['start']) / 60000.0, 2)}


# ----------------------------------------------------------------------------- validation CLI
_re_autosens = re.compile(r'^Autosens ratio:\s*([0-9.]+)')
_re_last_bolus = re.compile(r'last bolus\s+(-?[0-9.]+)m ago')


def _pct(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    k = min(len(v) - 1, int(math.ceil(p / 100.0 * len(v)) - 1))
    return v[max(k, 0)]


def validate(start_iso, end_iso, url=None, now_source='deliverAt', settings_path=None, log=print):
    from ns_client import NightscoutClient, parse_iso, to_iso
    settings_path = settings_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ns_settings.json')
    with open(settings_path, 'r', encoding='utf-8') as f:
        settings = json.load(f)
    ns_cfg = settings.get('nightscout', {})
    url = url or ns_cfg.get('url')
    ns = NightscoutClient(url, cache_dir=ns_cfg.get('cache_dir'), token=ns_cfg.get('token'),
                          api_secret_sha1=ns_cfg.get('api_secret_sha1'))
    start_ms, end_ms = parse_iso(start_iso), parse_iso(end_iso)
    lookback = 8 * 3600000
    treatments = ns.treatments(start_ms - lookback, end_ms)
    devicestatus = [r for r in ns.devicestatus(start_ms, end_ms)
                    if isinstance(r.get('openaps'), dict) and isinstance(r['openaps'].get('iob'), dict)]
    log(f'{len(treatments)} treatments, {len(devicestatus)} devicestatus records with openaps.iob, now = {now_source}')

    # flat profile of the validation user; ns_profile is deliberately not imported here
    calc = IobCalculator(treatments, lambda ms: 0.15, settings, dia_at=lambda ms: 6.0, devicestatus=devicestatus)
    n_planned = sum(1 for t in calc.temps if t['planned_end'] is not None)
    log(f'{len(calc.boluses)} boluses, {len(calc.temps)} temp basals ({n_planned} with a planned end from devicestatus)')

    import time as _time
    t0 = _time.time()
    errs = {'iob': [], 'basaliob': [], 'activity': [], 'IOB(array0)': []}
    worst = []
    autosens = 1.0
    lb_ok = lb_n = 0
    lb_bad = []
    ct_ok = ct_n = ct_snap = 0
    ct_bad = []
    for rec in devicestatus:
        o = rec['openaps']
        ref = o['iob']
        sug = o.get('suggested') or {}
        ce = sug.get('consoleError') or []
        if isinstance(ce, str):
            ce = ce.split('\n')
        for line in ce:
            m = _re_autosens.match(line)
            if m:
                autosens = float(m.group(1))
                break
        stamp = sug.get('deliverAt') if now_source == 'deliverAt' else None
        stamp = stamp or ref.get('time') or rec.get('created_at')
        now_ms = parse_iso(stamp)
        mine = calc.iob_at(now_ms, now_ms, autosens)
        e_iob = abs(mine['iob'] - float(ref.get('iob', 0)))
        e_bas = abs(mine['basaliob'] - float(ref.get('basaliob', 0)))
        e_act = abs(mine['activity'] - float(ref.get('activity', 0)))
        errs['iob'].append(e_iob)
        errs['basaliob'].append(e_bas)
        errs['activity'].append(e_act)
        if 'IOB' in sug:
            errs['IOB(array0)'].append(abs(mine['iob'] - float(sug['IOB'])))
        worst.append((max(e_iob, e_bas, e_act * 10), stamp, autosens, mine, ref))
        # lastBolusTime check from the 'last bolus X.Xm ago' console line
        for line in ce:
            m = _re_last_bolus.search(line)
            if m:
                lb_n += 1
                lbt = calc.last_bolus_time(now_ms)
                age = round((now_ms - lbt) / 60000.0, 1)
                if abs(age - float(m.group(1))) <= 0.15:
                    lb_ok += 1
                elif len(lb_bad) < 5:
                    lb_bad.append((stamp, age, m.group(1)))
                break
        # current_temp check: pump.extended.TempBasalRemaining at pump.clock
        pump = rec.get('pump') or {}
        ext = pump.get('extended') or {}
        if ext.get('TempBasalRemaining') is not None and pump.get('clock'):
            ct_n += 1
            clock_ms = parse_iso(pump['clock'])
            ct = calc.current_temp(clock_ms)
            if abs(ct['duration'] - int(ext['TempBasalRemaining'])) <= 1:
                ct_ok += 1
            else:
                # pump status snapshot taken < 2 s after the recorded cut of a temp (cancel / replace by the
                # loop in progress): AAPS's expected pump state was not updated yet, the DB row already was.
                # No loop ever runs at such an instant, so this is not comparable.
                k = bisect.bisect_right(calc.temp_ts, clock_ms)
                prev_end = calc.temps[k - 1]['end'] if k else 0
                if ct['duration'] == 0 and 0 <= clock_ms - prev_end < 2000:
                    ct_snap += 1
                elif len(ct_bad) < 5:
                    ct_bad.append((pump['clock'], ct, ext.get('TempBasalStart'), ext['TempBasalRemaining']))
    dt = _time.time() - t0
    log(f'\n{len(devicestatus)} loops evaluated in {dt:.1f} s  ({to_iso(start_ms)} .. {to_iso(end_ms)})')
    log(f'{"quantity":<12}{"median":>10}{"p95":>10}{"max":>10}{"n>0.05":>9}')
    for k, v in errs.items():
        if v:
            log(f'{k:<12}{_pct(v, 50):>10.4f}{_pct(v, 95):>10.4f}{max(v):>10.4f}{sum(1 for x in v if x > 0.05):>9}')
    log(f'lastBolusTime: {lb_ok}/{lb_n} loops reproduce the "last bolus Xm ago" line' + (f'  e.g. {lb_bad}' if lb_bad else ''))
    log(f'current_temp : {ct_ok}/{ct_n} pump status records reproduce TempBasalRemaining within 1 min; '
        f'{ct_snap} snapshots < 2 s after a temp cut (not comparable), {ct_n - ct_ok - ct_snap} real mismatches'
        + (f'  e.g. {ct_bad}' if ct_bad else ''))
    worst.sort(key=lambda w: -w[0])
    log('\n10 worst loops (score = max(|iob err|, |basaliob err|, 10*|activity err|)):')
    for score, stamp, ratio, mine, ref in worst[:10]:
        log(f'  {stamp}  ratio {ratio:<5} iob {mine["iob"]:.3f}/{ref.get("iob")}  basaliob {mine["basaliob"]:.3f}/{ref.get("basaliob")}'
            f'  activity {mine["activity"]:.4f}/{ref.get("activity")}')
    return errs


if __name__ == '__main__':
    args = [a for a in sys.argv[1:]]
    if len(args) >= 3 and args[0] == '--validate':
        now_source = 'deliverAt'
        if '--now' in args:
            i = args.index('--now')
            now_source = args[i + 1]
            del args[i:i + 2]
        url = args[3] if len(args) > 3 else None
        validate(args[1], args[2], url, now_source=now_source)
    else:
        print(__doc__)
        sys.exit(1)
