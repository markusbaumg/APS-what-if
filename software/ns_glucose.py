"""
ns_glucose.py  -  rebuild the AAPS/autoISF GlucoseStatus from Nightscout data.

Contract (see NIGHTSCOUT_SOURCE.md):
    gh = GlucoseHistory(entries, devicestatus, settings)
    gs = gh.status_at(now_ms, record)      -> dict or None

Keys of the returned dict (all glucose in mg/dl, deltas in mg/dl per 5 minutes):
    glucose, date, delta, short_avgdelta, long_avgdelta, noise,
    dura_ISF_minutes, dura_ISF_average,
    parabola_fit_minutes, parabola_fit_correlation, parabola_fit_a0, parabola_fit_a1, parabola_fit_a2,
    parabola_fit_last_delta, parabola_fit_next_delta, bg_acceleration

`glucose` is the value the loop saw (record['openaps']['suggested']['bg'] when the record is given,
otherwise the rebuilt smoothed value), `date` the timestamp (entries.date) of the CGM reading the loop ran on:
the newest reading at or before now_ms, or an older one the loop is pinned to (below).  None is returned when
the newest reading is older than 12 minutes.

Loop-to-reading pin: a sluggish phone sometimes runs determine-basal on a reading 1-5 readings (60-320 s) older
than the newest one already on the server (11 % of the loops on 2026-10-15, 0-2 % on normal days).  With the
record given, status_at takes the newest of the last PIN_READINGS (6) readings whose rebuilt smoothed bg equals
suggested.bg within PIN_TOL (0.05 mg/dl) as the cutoff and rebuilds delta, short/long avgdelta and dura_ISF from
the buckets of that reading (date = that reading); the Libre raw 1-minute parabola fit still uses the newest
reading (settings 'glucose': {'pin_fit': 'newest'}, the default; 'pinned' fits on the pinned reading too).  No
candidate matches -> newest reading.  Counters: gh.pinned {readings back: loops}, gh.pin_unmatched; the Kalman
state (learned R) is left as smoothing at the chosen cutoff leaves it, probes of other cutoffs are undone.

CGM bridge (H-714; settings 'glucose': {'bridge': true}, the default): when the server lacks CGM entries while the
phone kept looping, each devicestatus loop's suggested.bg becomes a reading at the loop's run time (deliverAt;
duplicate uploads once), flagged as bridged (gh.bridged_ts; status_at sets gh.last_bridged when the reading it used is
one, ns_source then marks the loop 'cgm_bridged').  A loop is bridged when no real reading lies within MAX_AGE_MS
(12 min) before it, or when it runs inside a real-reading gap longer than 12 min, more than BRIDGE_ANCHOR_MS (90 s)
after the gap's last reading, with a bg differing (PIN_TOL) from the loop that ran on that reading.  suggested.bg is
the phone's SMOOTHED value: bridged buckets are interpolated without whole-mg/dl rounding, are not 'filled' while
their two readings are at most BRIDGE_SPAN_MS (30 min: the phone loops every 1-5 min and pauses up to ~16 min at
night with 1-minute CGM on the phone) apart, take part in the Kalman update (the state runs through the stretch, so
the join is continuous) but never in the innovation statistics that teach the learned R, and are re-smoothed by
the RTS pass ('bridge_smoothed': 'kalman', default; 'phone' keeps the phone's value - measured on 2026-10-13 with
the entries of 00:00-01:55Z removed: 'kalman' reproduces the phone's tick on 68 of 118 bridged loops, 'phone' on
46, the decisions agree about equally, 47.5 vs 49.2 %).  The parabola fit
runs on the bridged values like on raw 1-minute readings.  Limits (measured, NIGHTSCOUT_SOURCE.md): the phone's
values are forward-filter estimates, not the RTS-smoothed history the phone used for its deltas, and the fork's
parabola fit sees smoothed instead of raw values with the loop pauses as gaps, so acce_ISF / ISF of the bridged
loops agree with the phone far less often than on real CGM.  On the 42 cached days (2026-08-30..10-16) no loop
runs without CGM entries: every gap is a gap of both collections, nothing is bridged.

What is ported (AAPS 3.4.2 fork "aisf3.2.1", commit 346a68550c of nightscout/AndroidAPS, and master):
    AutosensDataStoreObject.createBucketedData        1-minute Libre data -> 5-minute buckets (interpolated,
                                                      rounded to whole mg/dl, "filledGap" flag), 5-min path too
    UnscentedKalmanFilterPlugin.smooth                 the smoothing plugin the loop runs (adaptive UKF + RTS,
                                                      learned R carried from loop to loop like the app does)
    DeltaCalculator.calculateDeltas                    delta (2.5..7.5 min), short_avgdelta (2.5..17.5),
                                                      long_avgdelta (17.5..42.5) from the smoothed buckets
    GlucoseStatusCalculatorAutoIsf (fork)              dura_ISF_minutes/average (5 % band on smoothed buckets),
                                                      parabola fit on the RAW 1-minute readings ("use1MinuteRaw"
                                                      for Libre2/Libre3 when 3 readings span < 3 min, 10 min minimum
                                                      fit length, 47 min maximum, a0/a1/a2 in mg/dl per 5-min unit,
                                                      no rounding - asRounded() is commented out in the fork)

Which series / bucketing and why (result of --validate, see the bottom of this docstring):
    * raw `entries.sgv` are the only input; the loop's `suggested.bg` is NOT fed back into the series because
      AAPS re-smooths the whole window on every run (the RTS backward pass changes past values) - the value a
      past loop saw is not the value the present loop uses for that bucket.  Exception: the CGM bridge above,
      only where the server has no entries at all.
    * the 5-minute bucket grid is anchored at the newest CGM reading of every loop ("moving" grid).  In the
      Kotlin source the anchor (`referenceTime`) is fixed once per app start, but the devicestatus data shows
      `suggested.bg` changing on every 1-minute loop, which is only possible with a grid that moves with the
      newest reading; a fixed grid reproduces `tick` far worse (see --validate --grid=<seconds> to try one).
    * the parabola fit uses the raw 1-minute readings exactly like the fork (this is what reproduces the
      'acce_ISF adaptation is' and 'Parabolic fit extrapolates' lines), not the 5-minute buckets of master.

Validation: `python ns_glucose.py --validate 2026-09-06T00:00:00Z 2026-09-07T00:00:00Z`
compares against devicestatus consoleError lines (tick, dura_ISF minutes, acce_ISF factor, parabolic extremum).
Residual mismatch rates are printed by the CLI and quoted in NIGHTSCOUT_SOURCE.md.
    dura_ISF average: the phone never prints it; it is checked through the printed dura_ISF factor
    (OpenAPSAutoISFPlugin.autoISF: 1 + dura/60 * dura_ISF_weight/target_bg * (avg - target_bg), target_bg = the
    active temp target or the profile target = suggested.targetBG).  The factor is rounded to 2 decimals, so the
    average is only determined to +/- 0.005 * 60 * target / (dura * weight) mg/dl (4.3 mg/dl at 10 min, 2.2 at
    20 min); the validation therefore recomputes the factor from the rebuilt average and checks that the rebuilt
    average lies in the quantisation band.  The dura loop itself (running average of the smoothed 5-min buckets
    inside the +/-5 % band of the running average, non-filled buckets only, 13-min gap stop) is identical in
    master and in the fork (GlucoseStatusCalculatorAutoIsf.kt) and identical to emulator_core.getHistBG.
    Duplicate devicestatus uploads (same deliverAt, different _id; ~15 % of the records) are counted once.
Data needed before the first loop: ns_source loads CGM entries with an 8 h lookback; the bucketed/smoothed
series is rebuilt per loop from at most KALMAN_WINDOW_MS (34 h) of readings, but 8 h reproduce the 34 h result
to < 0.05 mg/dl/5min on delta from the first loop of a window (the Kalman state and the learned R only depend
on the last ~4 h), so no loop before the window start is needed.  After a CGM gap the first loops get
dura_ISF_minutes 0 and a 0-correlation parabola like the phone; status_at returns None while the newest
reading is older than 12 min or fewer than 3 readings exist.

Standard library only (runs on the phone under QPython).
"""
import bisect
import json
import math
import sys
from collections import deque

FIVE_MIN = 5 * 60 * 1000
MAX_AGE_MS = 12 * 60 * 1000                  # no CGM value within 12 min -> loop not reproducible
KALMAN_WINDOW_MS = 34 * 60 * 60 * 1000       # AAPS loads 24 h + 10 h (max DIA) of readings for the buckets
IRREGULAR_DATA_MS = 30 * 1000                # AutosensDataStoreObject.IRREGULAR_DATA_SEC
PIN_READINGS = 6                             # status_at: candidate cutoffs = the newest reading and 5 older ones
PIN_TOL = 0.05                               # mg/dl: rebuilt smoothed bg == suggested.bg
BRIDGE_SPAN_MS = 30 * 60 * 1000              # CGM bridge: a bucket between loop readings <= 30 min apart is not "filled"
BRIDGE_ANCHOR_MS = 90 * 1000                 # CGM bridge: the loop run on the last reading before a gap follows it within 90 s


# ============================================================================ helpers
def _entry_ms(rec):
    v = rec.get('date')
    if isinstance(v, (int, float)):
        return int(v)
    if 'mills' in rec:
        return int(rec['mills'])
    from ns_client import parse_iso
    if 'dateString' in rec:
        return parse_iso(rec['dateString'])
    if 'created_at' in rec:
        return parse_iso(rec['created_at'])
    return 0


def _kround(x):
    """Kotlin roundToLong(): round half away from zero"""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


class Bucket:
    """InMemoryGlucoseValue"""
    __slots__ = ('ts', 'value', 'smoothed', 'filled', 'bridged')

    def __init__(self, ts, value, filled=False, bridged=False):
        self.ts = ts
        self.value = float(value)
        self.smoothed = None
        self.filled = filled
        self.bridged = bridged              # value is the phone's smoothed bg bridged from devicestatus (see CGM bridge)

    @property
    def recalculated(self):
        return self.value if self.smoothed is None else self.smoothed


# ============================================================================ bucketing (AutosensDataStoreObject)
def is_about_5min_data(ts_desc):
    """AutosensDataStoreObject.isAbout5minData on timestamps newest first"""
    if len(ts_desc) < 3:
        return True
    total = 0
    for i in range(1, len(ts_desc)):
        diff = ts_desc[i - 1] - ts_desc[i]
        diff %= FIVE_MIN
        if diff > 150 * 1000:
            diff -= FIVE_MIN
        total += diff
        if abs(diff) > IRREGULAR_DATA_MS:
            return False
    return (total // len(ts_desc)) // 1000 < 1


def bucketed_recalculated(ts_asc, val_asc, i_newest, grid_ms, br_asc=None):
    """AutosensDataStoreObject.createBucketedDataRecalculated.
    ts_asc/val_asc: readings ascending; i_newest: index of the newest reading to use;
    grid_ms: a time on the 5-minute grid (the app's referenceTime). Returns buckets newest first.
    br_asc: optional flags (ascending) of readings bridged from devicestatus; a bucket is bridged when the nearer of
    its two readings is, a bucket between two bridged readings is interpolated without the whole-mg/dl rounding
    (the phone's values are smoothed, not CGM readings), and a bucket next to a bridged reading is not "filled"
    while its two readings are at most BRIDGE_SPAN_MS apart (the loops run every 1-5 min, the phone's own
    readings every minute: dura_ISF must not stop at every bridged bucket)."""
    if i_newest < 2:
        return None
    t_new = ts_asc[i_newest]
    # adjustToReferenceTime
    diff = abs(t_new - grid_ms) % FIVE_MIN
    if diff > 150 * 1000:
        t = t_new + abs(diff - FIVE_MIN)
    else:
        t = t_new - diff
    if t > t_new:
        t -= FIVE_MIN
    out = []
    t_old = ts_asc[0]
    while t >= t_old:
        # findNewer: reading with the smallest timestamp >= t (within the readings up to i_newest)
        j = bisect.bisect_left(ts_asc, t, 0, i_newest + 1)
        if j > i_newest:
            break
        if ts_asc[j] == t:
            out.append(Bucket(t, val_asc[j], bridged=bool(br_asc and br_asc[j])))
        else:
            k = j - 1                      # findOlder: largest timestamp <= t
            if k < 0:
                break
            dt_new = ts_asc[j] - t
            dt_old = t - ts_asc[k]
            bg = val_asc[j] - dt_new / float(ts_asc[j] - ts_asc[k]) * (val_asc[j] - val_asc[k])
            filled = min(dt_old, dt_new) > IRREGULAR_DATA_MS
            if br_asc and (br_asc[j] or br_asc[k]) and ts_asc[j] - ts_asc[k] <= BRIDGE_SPAN_MS:
                filled = False              # the phone had 1-minute readings here; only the server lacks them
            if br_asc and br_asc[j] and br_asc[k]:
                out.append(Bucket(t, bg, filled=filled, bridged=True))
            else:
                out.append(Bucket(t, float(_kround(bg)), filled=filled,
                                  bridged=bool(br_asc and br_asc[j if dt_new <= dt_old else k])))
        t -= FIVE_MIN
    return out


def bucketed_5min(ts_asc, val_asc, i_newest, grid_ms, br_asc=None):
    """AutosensDataStoreObject.createBucketedData5min (regular 5-minute CGMs); falls back to the
    recalculated variant when the normalisation would move a value by more than 90 s.  br_asc as there."""
    if i_newest < 2:
        return None
    br = (lambda i: bool(br_asc[i])) if br_asc else (lambda i: False)
    b = [Bucket(ts_asc[i_newest], val_asc[i_newest], bridged=br(i_newest))]
    j = 0
    for i in range(i_newest - 1, -1, -1):
        bg_time = ts_asc[i]
        last_time = ts_asc[i + 1]
        elapsed = int((bg_time - last_time) / 60000)          # Kotlin Long division truncates
        if abs(elapsed) > 8:
            last_value = val_asc[i + 1]
            elapsed = abs(elapsed)
            while elapsed > 5:
                next_time = last_time - FIVE_MIN
                j += 1
                gap_delta = val_asc[i] - last_value
                next_bg = last_value + 5.0 / elapsed * gap_delta
                b.append(Bucket(next_time, float(_kround(next_bg)), filled=True, bridged=br(i) and br(i + 1)))
                elapsed -= 5
                last_value = next_bg
                last_time = next_time
            j += 1
            b.append(Bucket(bg_time, val_asc[i], bridged=br(i)))
        elif abs(elapsed) > 2:
            j += 1
            b.append(Bucket(bg_time, val_asc[i], bridged=br(i)))
        else:
            b[j].value = (b[j].value + val_asc[i]) / 2
    oldest = b[-1]
    diff = abs(oldest.ts - grid_ms) % FIVE_MIN
    oldest.ts = oldest.ts + abs(diff - FIVE_MIN) if diff > 150 * 1000 else oldest.ts - diff
    for i in range(len(b) - 2, -1, -1):
        cur, prev = b[i], b[i + 1]
        adjusted = (cur.ts - prev.ts - FIVE_MIN) / 1000
        if abs(adjusted) > 90:
            return bucketed_recalculated(ts_asc, val_asc, i_newest, grid_ms, br_asc)
        cur.ts = prev.ts + FIVE_MIN
    return b


# ============================================================================ smoothing (UnscentedKalmanFilterPlugin)
class KalmanSmoother:
    """Port of AAPS UnscentedKalmanFilterPlugin (adaptive UKF, RTS backward pass).
    The learned measurement noise R and the innovation window persist between calls, as in the app."""
    Q_FIXED = (1.0, 0.0, 0.0, 0.40)
    R_INIT, R_MIN, R_MAX, R_EFF_MAX = 25.0, 16.0, 196.0, 400.0
    INNOV_WINDOW = 48
    CHI2 = 15.13
    OUTLIER_ABS = 65.0
    MAX_G_VAR, MAX_R_VAR = 400.0, 4.0
    INNOV_RESET, INNOV_VALIDATION = 12.0, 15
    MINOR_GAP, MAJOR_GAP, RATE_TAU = 7.0, 60.0, 30.0
    RATE_DAMPING = 0.98

    def __init__(self):
        n = 2
        alpha, beta, kappa = 1.0, 0.0, 3.0
        lam = alpha * alpha * (n + kappa) - n
        self.gamma = math.sqrt(n + lam)
        self.Wm = [lam / (n + lam)] + [1.0 / (2.0 * (n + lam))] * (2 * n)
        self.Wc = [lam / (n + lam) + (1 - alpha * alpha + beta)] + [1.0 / (2.0 * (n + lam))] * (2 * n)
        self.learnedR = self.R_INIT
        self.innovations = deque()
        self.raw_innov = deque()
        self.last_processed = 0

    # --- persistence / reset ---------------------------------------------------------------
    def _should_reset(self, ts):
        if self.last_processed == 0:
            return True
        dt_min = (ts - self.last_processed) / 60000.0
        if dt_min < 0 or dt_min > 1440.0:
            return True
        if len(self.innovations) >= self.INNOV_VALIDATION:
            if sum(self.innovations) / len(self.innovations) > self.INNOV_RESET:
                return True
        return False

    def reset(self):
        self.learnedR = self.R_INIT
        self.innovations.clear()

    # --- UKF core -----------------------------------------------------------------------------
    def _sigma(self, x, P):
        a, b, d = P[0], (P[1] + P[2]) / 2.0, P[3]
        l11 = math.sqrt(max(a, 1e-9))
        l21 = b / l11
        disc = d - l21 * l21
        if disc < -1e-9:
            s = (math.sqrt(max(a, 0.1)), 0.0, 0.0, math.sqrt(max(d, 0.01)))
        else:
            s = (l11, l21, 0.0, math.sqrt(max(disc, 1e-9)))
        g = self.gamma
        return [(x[0], x[1]),
                (x[0] + g * s[0], x[1] + g * s[1]), (x[0] + g * s[2], x[1] + g * s[3]),
                (x[0] - g * s[0], x[1] - g * s[1]), (x[0] - g * s[2], x[1] - g * s[3])]

    def _predict(self, x, P, Q, dt):
        sp = self._sigma(x, P)
        damp = self.RATE_DAMPING
        spp = [(p[0] + p[1] * dt, p[1] * damp) for p in sp]
        Wm, Wc = self.Wm, self.Wc
        x0 = sum(Wm[i] * spp[i][0] for i in range(5))
        x1 = sum(Wm[i] * spp[i][1] for i in range(5))
        P0 = P1 = P3 = 0.0
        for i in range(5):
            d0 = spp[i][0] - x0
            d1 = spp[i][1] - x1
            P0 += Wc[i] * d0 * d0
            P1 += Wc[i] * d0 * d1
            P3 += Wc[i] * d1 * d1
        qs = dt / 5.0
        P0 += Q[0] * qs
        P3 += Q[3] * qs
        return [x0, x1], [max(P0, 0.1), P1, P1, max(P3, 0.001)]

    def _update(self, xp, Pp, z, R):
        sp = self._sigma(xp, Pp)
        Wm, Wc = self.Wm, self.Wc
        zpred = sum(Wm[i] * sp[i][0] for i in range(5))
        Pzz = sum(Wc[i] * (sp[i][0] - zpred) ** 2 for i in range(5)) + R
        if Pzz < 1e-6:
            return list(xp), list(Pp)
        Pxz0 = Pxz1 = 0.0
        for i in range(5):
            dz = sp[i][0] - zpred
            Pxz0 += Wc[i] * (sp[i][0] - xp[0]) * dz
            Pxz1 += Wc[i] * (sp[i][1] - xp[1]) * dz
        K0, K1 = Pxz0 / Pzz, Pxz1 / Pzz
        innov = z - zpred
        x = [xp[0] + K0 * innov, min(max(xp[1] + K1 * innov, -4.0), 4.0)]
        P = [max(Pp[0] - K0 * Pzz * K0, 0.1), Pp[1] - K0 * Pzz * K1, Pp[2] - K1 * Pzz * K0, max(Pp[3] - K1 * Pzz * K1, 0.001)]
        return x, P

    @staticmethod
    def _med(vals):
        s = sorted(vals)
        n = len(s)
        return (s[n // 2] + s[(n - 1) // 2]) / 2 if n % 2 == 0 else s[n // 2]

    def _adapt_R(self, R):
        if len(self.innovations) < 8:
            return R
        avg = self._med(self.innovations)
        if any(v > 9.0 for v in self.innovations):
            return min(max(R, self.R_MIN), self.R_MAX)
        if avg >= 1.1 or avg <= 0.9:
            R = R + 0.06 * (self._med(self.raw_innov) - R)
        return min(max(R, self.R_MIN), self.R_MAX)

    def _gain(self, P, Pp, dt):
        damp = self.RATE_DAMPING
        PFt00 = P[0] + P[1] * dt
        PFt01 = P[1] * damp
        PFt10 = P[2] + P[3] * dt
        PFt11 = P[3] * damp
        det = Pp[0] * Pp[3] - Pp[1] * Pp[2]
        if abs(det) < 1e-10:
            return (0.0, 0.0, 0.0, 0.0)
        i00, i01, i10, i11 = Pp[3] / det, -Pp[1] / det, -Pp[2] / det, Pp[0] / det
        return (PFt00 * i00 + PFt01 * i10, PFt00 * i01 + PFt01 * i11,
                PFt10 * i00 + PFt11 * i10, PFt10 * i01 + PFt11 * i11)

    # --- segments ------------------------------------------------------------------------------
    @staticmethod
    def _segments(data):
        if len(data) < 2:
            return []
        segs = []
        start = 0
        for i in range(len(data) - 1):
            td = (data[i].ts - data[i + 1].ts) / 60000.0
            if td > KalmanSmoother.MAJOR_GAP or td < 2.0 or data[i].value == 38.0:
                if i - start >= 2:
                    segs.append((start, i))
                start = i + 1
        if len(data) - start >= 2:
            segs.append((start, len(data) - 1))
        return segs

    def _process_segment(self, data, s, e):
        size = e - s + 1
        if size < 2:
            data[s].smoothed = max(data[s].value, 39.0)
            return
        rate = 0.0
        if e > 0:
            dt = (data[e - 1].ts - data[e].ts) / 60000.0
            if 3.0 <= dt <= 7.0:
                rate = min(max((data[e - 1].value - data[e].value) / dt, -4.0), 4.0)
        x = [data[e].value, rate]
        P = [16.0, 0.0, 0.0, 1.0]
        Q = self.Q_FIXED
        R = self.learnedR
        states = []                      # newest first: (x, P, xPred, PPred, dt)
        fwd = [0.0] * size
        fwd[size - 1] = x[0]
        consec = 0
        last_sign = 0
        for i in range(e - 1, s - 1, -1):
            dt = (data[i].ts - data[i + 1].ts) / 60000.0
            if self.MINOR_GAP < dt <= self.MAJOR_GAP:
                qs = dt / 5.0
                P[0] = min(P[0] + Q[0] * qs, self.MAX_G_VAR)
                P[3] = min(P[3] + Q[3] * qs, self.MAX_R_VAR)
                x[1] *= math.exp(-dt / self.RATE_TAU)
            P[0] = min(max(P[0], 0.1), self.MAX_G_VAR)
            P[3] = min(max(P[3], 0.001), self.MAX_R_VAR)
            dtc = min(max(dt, 3.5), 6.5)
            xp, Pp = self._predict(x, P, Q, dtc)
            before = (list(x), list(P), list(xp), list(Pp), dtc)
            z = data[i].value
            if z <= 38.0:
                x, P = list(xp), list(Pp)
                fwd[i - s] = x[0]
                states.insert(0, before)
                continue
            innov = z - xp[0]
            innov_var = Pp[0] + R
            norm = innov / math.sqrt(innov_var)
            mahal = innov * innov / innov_var
            sign = 1 if norm > 0 else (-1 if norm < 0 else 0)
            if abs(norm) > 3.0 and sign != 0 and sign == last_sign:
                consec += 1
            elif abs(norm) > 3.0 and sign != 0:
                consec = 1
            else:
                consec = 0
            last_sign = sign
            R_eff = min(R * max(1.0, mahal / self.CHI2), self.R_EFF_MAX)
            qscale = min(max(abs(norm), 1.0), 3.0) if consec >= 2 else 1.0
            if qscale > 1.0:
                tq = [Q[0] * min(qscale, 2.0), 0.0, 0.0, Q[3] * qscale]
                xpe, Ppe = self._predict(x, P, tq, dtc)
            else:
                xpe, Ppe = xp, Pp
            x, P = self._update(xpe, Ppe, z, R_eff)
            if data[i].bridged:             # the phone's smoothed value: tracks the state, never teaches R
                fwd[i - s] = x[0]
                states.insert(0, before)
                continue
            self.innovations.appendleft(mahal)
            self.raw_innov.appendleft(innov * innov)
            if len(self.innovations) > self.INNOV_WINDOW:
                self.innovations.pop()
            if len(self.raw_innov) > self.INNOV_WINDOW:
                self.raw_innov.pop()
            if abs(norm) <= 3.0:
                R = self._adapt_R(R)
            fwd[i - s] = x[0]
            states.insert(0, before)
        self.learnedR = R
        sm = list(fwd)
        if size >= 3 and states:
            steps = min(size - 1, len(states))
            xs = [fwd[0], x[1]]
            for i in range(1, steps + 1):
                st = states[i - 1]
                C = self._gain(st[1], st[3], st[4])
                dx0 = xs[0] - st[2][0]
                dx1 = xs[1] - st[2][1]
                xs = [fwd[i] + C[0] * dx0 + C[1] * dx1, st[0][1] + C[2] * dx0 + C[3] * dx1]
                sm[i] = xs[0]
        for i in range(s, e + 1):
            data[i].smoothed = max(sm[i - s], 39.0)

    def smooth(self, data):
        """data: buckets newest first; sets .smoothed like the plugin (None where no segment covers a point)"""
        if not data:
            return data
        if self._should_reset(data[0].ts):
            self.reset()
        segs = self._segments(data)
        if not segs:
            for b in data:
                b.smoothed = max(b.value, 39.0)
            return data
        self.last_processed = data[0].ts
        for s, e in segs:
            self._process_segment(data, s, e)
        return data


# ============================================================================ deltas (DeltaCalculator)
def calculate_deltas(data):
    """delta, short_avgdelta, long_avgdelta from buckets newest first (recalculated values)"""
    if len(data) < 2:
        return 0.0, 0.0, 0.0
    now = data[0]
    now_v = now.recalculated
    last, short, long_ = [], [], []
    for i in range(1, len(data)):
        then = data[i]
        if then.recalculated > 39.0:
            minutes_ago = (now.ts - then.ts) / 60000.0
            avg_del = (now_v - then.recalculated) / minutes_ago * 5
            if 2.5 <= minutes_ago <= 7.5:
                last.append(avg_del)
            if 2.5 <= minutes_ago <= 17.5:
                short.append(avg_del)
            if 17.5 <= minutes_ago <= 42.5:
                long_.append(avg_del)
            elif minutes_ago > 42.5:
                break
    avg = lambda a: sum(a) / len(a) if a else 0.0
    short_avg = avg(short)
    return (avg(last) if last else short_avg), short_avg, avg(long_)


# ============================================================================ dura_ISF (5 % band)
def dura_isf(data):
    """GlucoseStatusCalculatorAutoIsf: minutes the smoothed buckets stayed within +/-5 % of their running average"""
    now = data[0]
    now_date = now.ts
    bw = 0.05
    sum_bg = now.recalculated
    old_avg = sum_bg
    minutes = 0
    n = 1
    for i in range(1, len(data)):
        b = data[i]
        if b.value > 39 and not b.filled:
            n += 1
            mins = (now_date - b.ts) / 60000.0
            if _kround(mins) - minutes > 13:
                break
            v = b.recalculated
            if old_avg * (1 - bw) < v < old_avg * (1 + bw):
                sum_bg += v
                old_avg = sum_bg / n
                minutes = _kround(mins)
            else:
                break
    return float(minutes), old_avg


# ============================================================================ parabola fit (fork, raw 1-min readings)
def parabola_fit(ts_desc, val_desc, fsl_min_dur, filled_desc=None):
    """GlucoseStatusCalculatorAutoIsf (fork 346a68550c): least-squares parabola over the newest readings,
    extended backwards point by point up to 47 min; the fit with the best R^2 wins (ties -> the longer one).
    ts_desc/val_desc newest first; fsl_min_dur = minimum fit length in minutes (10 for 1-min Libre, 15 else).
    Returns dict(minutes, corr, a0, a1, a2, deltaPl, deltaPn, accel) with a1/a2 per 5-minute unit."""
    res = dict(minutes=0.0, corr=0.0, a0=0.0, a1=0.0, a2=0.0, deltaPl=0.0, deltaPn=0.0, accel=0.0)
    size = len(ts_desc)
    if size <= 3:
        return res
    sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
    time0 = ts_desc[0]
    ti_last = 0.0
    corr_max = 0.0
    n = 0
    xs, ys = [], []                          # accepted points, for the correlation loop
    for i in range(size):
        if filled_desc is not None and filled_desc[i]:
            continue
        bg = val_desc[i]
        if bg <= 39:
            continue
        n += 1
        ti = (ts_desc[i] - time0) / 1000.0    # seconds, scaleTime = 1
        if -ti > 47 * 60:
            break
        if ti < ti_last - 11.0 * 60:          # CGM gap > 11 min
            if i < 3 or -ti < fsl_min_dur * 60:
                res.update(minutes=-ti_last / 60.0, corr=0.0, a0=0.0, a1=0.0, a2=0.0, deltaPl=0.0, deltaPn=0.0, accel=0.0)
            break
        ti_last = ti
        sx += ti
        sx2 += ti * ti
        sx3 += ti ** 3
        sx4 += ti ** 4
        sy += bg
        sxy += ti * bg
        sx2y += ti * ti * bg
        xs.append(ti / 300.0)
        ys.append(bg)
        if n > 3 and -ti > fsl_min_dur * 60:
            detH = sx4 * (sx2 * n - sx * sx) - sx3 * (sx3 * n - sx * sx2) + sx2 * (sx3 * sx - sx2 * sx2)
        else:
            detH = 0.0
        if detH != 0.0:
            detA = sx2y * (sx2 * n - sx * sx) - sxy * (sx3 * n - sx * sx2) + sy * (sx3 * sx - sx2 * sx2)
            detB = sx4 * (sxy * n - sy * sx) - sx3 * (sx2y * n - sy * sx2) + sx2 * (sx2y * sx - sxy * sx2)
            detC = sx4 * (sx2 * sy - sx * sxy) - sx3 * (sx3 * sy - sx * sx2y) + sx2 * (sx3 * sxy - sx2 * sx2y)
            a = detA / detH * 300.0 ** 2
            b = detB / detH * 300.0
            c = detC / detH
            y_mean = sy / n
            ss = 0.0
            sres = 0.0
            for j in range(len(xs)):
                ss += (ys[j] - y_mean) ** 2
                sres += (ys[j] - (a * xs[j] * xs[j] + b * xs[j] + c)) ** 2
            r2 = 1 - sres / ss if ss != 0.0 else 0.0
            if r2 >= corr_max:
                corr_max = r2
                res.update(minutes=-ti / 60.0, corr=r2, a0=c, a1=b, a2=a, accel=2 * a,
                           deltaPl=-(a - b), deltaPn=a + b)
    return res


# ============================================================================ the history object
class GlucoseHistory:
    """See module docstring. settings may carry an optional 'glucose' section:
        {'bucket_grid': 'moving' | <unix ms of a grid time>, 'smoothing': 'kalman' | 'none',
         'libre_1min_fit': true, 'pin_readings': 6 (1 = no pin), 'pin_fit': 'newest' | 'pinned',
         'bridge': true (CGM bridge from devicestatus), 'bridge_smoothed': 'kalman' | 'phone'}"""

    def __init__(self, entries, devicestatus, settings):
        cfg = (settings or {}).get('glucose', {}) if isinstance(settings, dict) else {}
        self.grid = cfg.get('bucket_grid', 'moving')
        self.smoothing = cfg.get('smoothing', 'kalman')
        self.libre_1min_fit = cfg.get('libre_1min_fit', True)
        pts = {}
        for r in entries or []:
            if r.get('type', 'sgv') != 'sgv' or r.get('isValid') is False:
                continue
            v = r.get('sgv')
            if v is None or not v > 0:
                continue
            ms = _entry_ms(r)
            if ms:
                pts[ms] = float(v)                # de-duplicate on timestamp
        self.loop_bg = {}
        for r in devicestatus or []:
            sug = ((r.get('openaps') or {}).get('suggested') or {})
            if 'bg' in sug and sug.get('deliverAt'):
                self.loop_bg[sug['deliverAt']] = sug['bg']
        # CGM bridge (see the module docstring): inside a stretch without CGM entries the phone's own bg of a loop
        # becomes a reading at the loop's run time, flagged as bridged
        self.bridge = cfg.get('bridge', True)
        self.bridge_smoothed = cfg.get('bridge_smoothed', 'kalman')
        self.bridged_ts = set()
        if self.bridge:
            self._bridge(pts, devicestatus)
        self.ts = sorted(pts)
        self.val = [pts[t] for t in self.ts]
        self.br = [t in self.bridged_ts for t in self.ts] if self.bridged_ts else None
        self.last_bridged = False                           # the reading the last status_at used is bridged
        self.kalman = KalmanSmoother()
        self._last_status = None
        self._last_key = None
        # loop-to-reading pin (see status_at): how many readings back to search, what the fit uses, counters
        self.pin_readings = int(cfg.get('pin_readings', PIN_READINGS))
        self.pin_fit = cfg.get('pin_fit', 'newest')         # Libre raw parabola fit: 'newest' reading | 'pinned' one
        self.pinned = {}                                    # readings back -> loops (back >= 1 only)
        self.pin_unmatched = 0                              # loops whose suggested.bg no candidate reproduced
        self.last_index = None                              # index into ts of the reading the last status used

    @staticmethod
    def _loop_readings(devicestatus):
        """[(run time ms, suggested.bg)] of the loops (deliverAt, else timestamp; duplicates once), ascending"""
        from ns_client import parse_iso
        out = {}
        for r in devicestatus or []:
            sug = ((r.get('openaps') or {}).get('suggested') or {})
            bg = sug.get('bg')
            stamp = sug.get('deliverAt') or sug.get('timestamp')
            if not stamp or isinstance(bg, bool) or not isinstance(bg, (int, float)) or not bg > 39:
                continue
            try:
                ms = parse_iso(stamp)
            except ValueError:
                continue
            out.setdefault(ms, float(bg))
        return sorted(out.items())

    def _bridge(self, pts, devicestatus):
        """add the bridged readings to pts {ms: sgv}: a loop gets one when no real reading lies within MAX_AGE_MS
        before it, or when it runs inside a real-reading gap longer than MAX_AGE_MS, more than BRIDGE_ANCHOR_MS after
        the gap's last reading, with a bg that differs from the bg of the last loop run within BRIDGE_ANCHOR_MS of
        that reading (the phone had a newer reading than the server)"""
        real = sorted(pts)
        anchor = {}                                         # index of the real reading -> bg of the loop run on it
        for ms, bg in self._loop_readings(devicestatus):
            if ms in pts:
                continue
            i = bisect.bisect_right(real, ms) - 1
            if i < 0 or ms - real[i] > MAX_AGE_MS:
                bridged = True
            elif i + 1 < len(real) and real[i + 1] - real[i] > MAX_AGE_MS:
                if ms - real[i] <= BRIDGE_ANCHOR_MS:      # ran on that reading (or an older one)
                    anchor[i] = bg
                    bridged = False
                else:
                    bridged = i not in anchor or abs(bg - anchor[i]) > PIN_TOL
            else:
                bridged = False
            if bridged:
                pts[ms] = bg
                self.bridged_ts.add(ms)

    def bridged_in(self, start_ms, end_ms):
        """number of bridged readings with start <= ts < end"""
        return sum(1 for t in self.bridged_ts if start_ms <= t < end_ms)

    # ------------------------------------------------------------------ core
    def _newest_index(self, now_ms):
        i = bisect.bisect_right(self.ts, now_ms) - 1
        return i

    def buckets_at(self, now_ms):
        """bucketed + smoothed series (newest first) the loop at now_ms works with, or None"""
        i = self._newest_index(now_ms)
        if i < 2 or now_ms - self.ts[i] > MAX_AGE_MS:
            return None
        return self._buckets_index(i)

    def _kalman_state(self):
        k = self.kalman
        return (k.learnedR, deque(k.innovations), deque(k.raw_innov), k.last_processed)

    def _set_kalman_state(self, st):
        k = self.kalman
        k.learnedR, k.innovations, k.raw_innov, k.last_processed = st[0], deque(st[1]), deque(st[2]), st[3]

    def pin_index(self, now_ms, record=None):
        """index into ts of the reading the loop at now_ms ran on.  A sluggish phone sometimes runs
        determine-basal on a reading 1-5 minutes older than the newest one already uploaded; the phone's
        suggested.bg identifies it: the newest of the last PIN_READINGS readings whose rebuilt smoothed bg
        equals suggested.bg within PIN_TOL.  The newest reading when no record/bg is given or none matches
        (counted in pin_unmatched).  Leaves the Kalman state as smoothing at the chosen cutoff left it."""
        i = self._newest_index(now_ms)
        if i < 2 or now_ms - self.ts[i] > MAX_AGE_MS:
            return i
        sug = ((record or {}).get('openaps') or {}).get('suggested') or {}
        want = sug.get('bg')
        if not isinstance(want, (int, float)) or self.pin_readings <= 1:
            return i
        before = self._kalman_state()
        data = self._buckets_index(i)
        if data and abs(data[0].recalculated - want) < PIN_TOL:
            return i
        newest = (self._last_key, self._last_status, self._kalman_state())
        for back in range(1, self.pin_readings):
            j = i - back
            if j < 2 or now_ms - self.ts[j] > MAX_AGE_MS:
                break
            self._set_kalman_state(before)
            self._last_key = None
            d = self._buckets_index(j)
            if d and abs(d[0].recalculated - want) < PIN_TOL:
                self.pinned[back] = self.pinned.get(back, 0) + 1
                return j
        self.pin_unmatched += 1
        self._last_key, self._last_status = newest[0], newest[1]
        self._set_kalman_state(newest[2])
        return i

    def _buckets_index(self, i):
        key = (self.ts[i], self.grid)
        if self._last_key == key:
            return self._last_status
        lo = bisect.bisect_left(self.ts, self.ts[i] - KALMAN_WINDOW_MS)
        ts = self.ts[lo:i + 1]
        val = self.val[lo:i + 1]
        br = self.br[lo:i + 1] if self.br else None
        n = len(ts) - 1
        grid = ts[n] if self.grid == 'moving' else int(self.grid)
        if is_about_5min_data(ts[::-1]):
            data = bucketed_5min(ts, val, n, grid, br)
        else:
            data = bucketed_recalculated(ts, val, n, grid, br)
        if not data:
            return None
        if self.smoothing == 'kalman':
            self.kalman.smooth(data)
        if br and self.bridge_smoothed == 'phone':
            for b in data:                      # bridged buckets already are the phone's smoothed values
                if b.bridged:
                    b.smoothed = max(b.value, 39.0)
        self._last_key = key
        self._last_status = data
        return data

    def status_at(self, now_ms, record=None):
        self.last_bridged = False
        i = self._newest_index(now_ms)
        if i < 0 or now_ms - self.ts[i] > MAX_AGE_MS:
            return None
        i_newest = i
        i = self.pin_index(now_ms, record) if i >= 2 else i
        data = self._buckets_index(i) if i >= 2 else None
        if not data:
            return None
        self.last_index = i
        self.last_bridged = bool(self.br and self.br[i])
        now = data[0]
        glucose = now.recalculated
        if record is not None:
            sug = ((record.get('openaps') or {}).get('suggested') or {})
            if isinstance(sug.get('bg'), (int, float)):
                glucose = float(sug['bg'])
        if len(data) == 1:
            return self._pack(glucose, self.ts[i], 0.0, 0.0, 0.0, 0.0, now.value,
                              dict(minutes=0.0, corr=0.0, a0=now.value, a1=0.0, a2=0.0, deltaPl=0.0, deltaPn=0.0, accel=0.0))
        delta, short_avg, long_avg = calculate_deltas(data)
        dura_min, dura_avg = dura_isf(data)
        # parabola: Libre 1-minute raw readings when three readings span < 3 min (fork), else the buckets
        # a pinned loop: bg, deltas and dura_ISF come from the buckets of the older reading (the autosens data the
        # phone had computed), but the fork reads the raw 1-minute readings for the parabola straight from the
        # database, where the newest reading already is ('pin_fit' 'newest', default; measured better than
        # 'pinned' on 2026-10-13..15, see NIGHTSCOUT_SOURCE.md).  The 5-minute bucket fit stays on the pinned series.
        if self.pin_fit == 'newest' and i != i_newest and self.libre_1min_fit \
                and self.ts[i_newest] - self.ts[i_newest - 2] < 3 * 60000:
            i = i_newest
        use_raw = self.libre_1min_fit and i >= 2 and self.ts[i] - self.ts[i - 2] < 3 * 60000
        if use_raw:
            lo = max(0, bisect.bisect_left(self.ts, self.ts[i] - 48 * 60000))
            ts_desc = self.ts[lo:i + 1][::-1]
            val_desc = self.val[lo:i + 1][::-1]
            fit = parabola_fit(ts_desc, val_desc, 10)
        else:
            fit = parabola_fit([b.ts for b in data], [b.recalculated for b in data], 15, [b.filled for b in data])
        return self._pack(glucose, self.ts[self.last_index], delta, short_avg, long_avg, dura_min, dura_avg, fit)

    @staticmethod
    def _pack(glucose, date, delta, short_avg, long_avg, dura_min, dura_avg, fit):
        return {
            'glucose': glucose, 'date': date, 'delta': delta, 'short_avgdelta': short_avg, 'long_avgdelta': long_avg,
            'noise': 0.0, 'dura_ISF_minutes': dura_min, 'dura_ISF_average': dura_avg,
            'parabola_fit_minutes': fit['minutes'], 'parabola_fit_correlation': fit['corr'],
            'parabola_fit_a0': fit['a0'], 'parabola_fit_a1': fit['a1'], 'parabola_fit_a2': fit['a2'],
            'parabola_fit_last_delta': fit['deltaPl'], 'parabola_fit_next_delta': fit['deltaPn'],
            'bg_acceleration': fit['accel'],
        }


# ============================================================================ validation against consoleError
def _tick(delta):
    """DetermineBasalAutoISF: tick = "+"+round(delta) if delta > -0.5 else round(delta)  (Kotlin round = half even)"""
    r = int(round(delta))
    return ('+%d' % r) if delta > -0.5 else ('%d' % r)


def expected_acce_isf(gs, target_bg, profile_target, glucose, accel_w, brake_w):
    """re-run the acce_ISF part of autoISF() on a glucose status -> (factor or None if by-passed, extremum text)"""
    fit_corr = gs['parabola_fit_correlation']
    bg_acce = gs['bg_acceleration']
    a0, a1, a2 = gs['parabola_fit_a0'], gs['parabola_fit_a1'], gs['parabola_fit_a2']
    acce_weight = 1
    extremum = None
    if a2 != 0 and fit_corr >= 0.9:
        minmax_delta = -a1 / 2 / a2 * 5
        minmax_value = round(a0 - minmax_delta * minmax_delta / 25 * a2, 1)
        minmax_delta = round(minmax_delta, 1)
        if minmax_delta > 0 and bg_acce < 0:
            extremum = ('maximum', minmax_value, minmax_delta)
        elif minmax_delta > 0 and bg_acce > 0:
            extremum = ('minimum', minmax_value, minmax_delta)
            if minmax_delta <= 30 and minmax_value < target_bg:
                acce_weight = -brake_w
    if fit_corr < 0.9:
        return None, extremum
    fit_share = 10 * (fit_corr - 0.9)
    cap_weight = 1
    if acce_weight == 1 and glucose < profile_target:
        if bg_acce > 0:
            if bg_acce > 1:
                cap_weight = 0.5
            acce_weight = brake_w
        elif bg_acce < 0:
            acce_weight = accel_w
    elif acce_weight == 1:
        if bg_acce < 0:
            acce_weight = brake_w
        elif bg_acce > 0:
            acce_weight = accel_w
    return 1 + bg_acce * cap_weight * acce_weight * fit_share, extremum


def _short(x):
    """determine_basal.short(): drop a trailing .0"""
    s = str(x)
    return s[:-2] if s.endswith('.0') else s


def validate(start_iso, end_iso, url='ns:', grid='moving', smoothing='kalman', verbose=True):
    import re
    from ns_client import NightscoutClient, parse_iso, to_iso
    from ns_source import load_settings, LOOKBACK_MS
    settings = load_settings()
    ns_cfg = settings.get('nightscout', {})
    if url in ('ns', 'ns:', ''):
        url = ns_cfg['url']
    settings = dict(settings)
    settings['glucose'] = {'bucket_grid': grid if grid == 'moving' else int(grid), 'smoothing': smoothing}
    ns = NightscoutClient(url, cache_dir=ns_cfg.get('cache_dir'), token=ns_cfg.get('token'), verbose=verbose)
    start, end = parse_iso(start_iso), parse_iso(end_iso)
    entries = ns.entries(start - max(LOOKBACK_MS, KALMAN_WINDOW_MS), end)
    devicestatus = [r for r in ns.devicestatus(start, end)
                    if isinstance(r.get('openaps'), dict) and isinstance(r['openaps'].get('suggested'), dict)]
    gh = GlucoseHistory(entries, devicestatus, settings)
    aisf = settings.get('autoisf', {})
    accel_w = aisf.get('bgAccel_ISF_weight', 0.09)
    brake_w = aisf.get('bgBrake_ISF_weight', 0.03)
    mmol = settings.get('nightscout', {}).get('units', 'mmol').startswith('mmol')

    re_dura = re.compile(r'dura_ISF adaptation is ([0-9.]+) because ISF ([0-9.]+) did not do it for ([0-9.]+)m')
    re_dura_bp = re.compile(r'dura_ISF by-passed; bg is only ([0-9.]+)m at level ([0-9.]+)')
    re_acce = re.compile(r'acce_ISF adaptation is ([-0-9.]+)')
    re_acce_bp = re.compile(r'acce_ISF adaptation by-passed as correlation ([-0-9.]+) is too low')
    re_para = re.compile(r'Parabolic fit extrapolates a (maximum|minimum) of ([-0-9.]+) in about ([-0-9.]+) minutes')

    class Stat:
        def __init__(self, name, tol):
            self.name, self.tol, self.n, self.exact, self.within, self.worst = name, tol, 0, 0, 0, []

        def add(self, stamp, want, got, extra='', tol=None):
            self.n += 1
            d = abs(want - got)
            if d < 1e-9:
                self.exact += 1
            if d <= (self.tol if tol is None else tol) + 1e-9:
                self.within += 1
            else:
                self.worst.append((d, stamp, want, got, extra))

        def report(self):
            if self.n == 0:
                return '%-28s no samples' % self.name
            self.worst.sort(reverse=True)
            s = '%-28s n=%5d  exact %5.1f%%  within %-5g %5.1f%%  mismatch %4d' % (
                self.name, self.n, 100.0 * self.exact / self.n, self.tol, 100.0 * self.within / self.n, self.n - self.within)
            for d, stamp, want, got, extra in self.worst[:5]:
                s += '\n      %s  orig %-9s rebuilt %-9s %s' % (stamp, _short(round(want, 3)), _short(round(got, 3)), extra)
            return s

    st_tick = Stat('tick (delta, mg/dl/5m)', 1)
    st_bg = Stat('glucose (smoothed bg)', 1.0)
    st_dura = Stat('dura_ISF minutes', 5)
    st_dura_bp = Stat('dura_ISF by-passed minutes', 5)
    st_dura_f = Stat('dura_ISF factor (rebuilt avg)', 0.01)
    st_dura_avg = Stat('dura_ISF avg in factor band', 0.0)
    st_acce = Stat('acce_ISF factor', 0.02)
    st_corr = Stat('by-passed correlation', 0.02)
    st_para_v = Stat('parabola extremum value', 0.2 if mmol else 2)
    st_para_m = Stat('parabola extremum minutes', 2)
    acce_state_ok = acce_state_n = 0
    acce_state_bad = []
    skipped = 0
    duplicates = 0
    seen = set()
    for rec in devicestatus:
        sug = rec['openaps']['suggested']
        stamp = sug.get('deliverAt')
        if not stamp or 'bg' not in sug:
            continue
        if stamp in seen:                    # AAPS uploads some devicestatus records twice (same deliverAt, new _id)
            duplicates += 1
            continue
        seen.add(stamp)
        now_ms = parse_iso(stamp)
        gs = gh.status_at(now_ms, rec)
        if gs is None:
            skipped += 1
            continue
        ce = sug.get('consoleError') or []
        if isinstance(ce, str):
            ce = ce.split('\n')
        data = gh.buckets_at(gh.ts[gh.last_index])          # the cached series of the reading status_at chose
        st_bg.add(stamp, float(sug['bg']), data[0].recalculated)
        if sug.get('tick') is not None:
            want = int(str(sug['tick']).replace('+', ''))
            st_tick.add(stamp, want, int(_tick(gs['delta']).replace('+', '')), 'delta=%.2f' % gs['delta'])
        target = float(sug.get('targetBG') or 0) or 0.0
        acce_want = None
        for line in ce:
            m = re_dura.match(line)
            if m:
                st_dura.add(stamp, float(m.group(3)), gs['dura_ISF_minutes'])
                # dura_ISF = 1 + dura/60 * weight/target * (avg - target)   (OpenAPSAutoISFPlugin.autoISF)
                # The phone prints the factor rounded to 2 decimals, so the average can only be recovered
                # to +/- 0.005/k mg/dl (k = dura/60 * weight/target: 4.3 mg/dl at 10 min, 2.2 at 20 min,
                # 1.4 at 30 min with target 100 and weight 0.7).  Two checks: the factor recomputed from the
                # rebuilt average (using the original minutes) must match the printed one within one rounding
                # step, and the rebuilt average must lie inside the quantisation band of the printed factor.
                w = aisf.get('dura_ISF_weight', 0.7)
                dura_orig = float(m.group(3))
                if target and dura_orig > 0 and w:
                    k = dura_orig / 60 * w / target
                    f_orig = float(m.group(1))
                    f_rebuilt = round(1 + k * (gs['dura_ISF_average'] - target), 2)
                    st_dura_f.add(stamp, f_orig, f_rebuilt, 'avg %.1f target %g dura %gm' % (gs['dura_ISF_average'], target, dura_orig))
                    avg_mid = target + (f_orig - 1) / k
                    st_dura_avg.add(stamp, avg_mid, gs['dura_ISF_average'], 'orig factor %s band +/-%.1f' % (m.group(1), 0.005 / k),
                                    tol=0.005 / k)
                continue
            m = re_dura_bp.match(line)
            if m:
                st_dura_bp.add(stamp, float(m.group(1)), gs['dura_ISF_minutes'])
                continue
            m = re_acce.match(line)
            if m:
                acce_want = float(m.group(1))
                continue
            m = re_acce_bp.match(line)
            if m:
                acce_want = ('bypass', float(m.group(1)))
                continue
            m = re_para.match(line)
            if m:
                exp_f, extremum = expected_acce_isf(gs, target, target, gs['glucose'], accel_w, brake_w)
                if extremum is None:
                    st_para_v.add(stamp, float(m.group(2)), -999, 'no extremum rebuilt (corr %.3f a2 %.3f)' % (gs['parabola_fit_correlation'], gs['parabola_fit_a2']))
                else:
                    v = extremum[1] / 18.0 if mmol else extremum[1]
                    st_para_v.add(stamp, float(m.group(2)), round(v, 1), 'kind %s/%s' % (m.group(1), extremum[0]))
                    st_para_m.add(stamp, float(m.group(3)), extremum[2])
        if acce_want is not None:
            exp_f, extremum = expected_acce_isf(gs, target, target, gs['glucose'], accel_w, brake_w)
            acce_state_n += 1
            if isinstance(acce_want, tuple):
                if exp_f is None:
                    acce_state_ok += 1
                    st_corr.add(stamp, acce_want[1], round(gs['parabola_fit_correlation'], 3))
                else:
                    acce_state_bad.append((stamp, 'orig by-passed corr %.3f, rebuilt factor %.2f corr %.3f' % (acce_want[1], exp_f, gs['parabola_fit_correlation'])))
            else:
                if exp_f is None:
                    acce_state_bad.append((stamp, 'orig factor %.2f, rebuilt by-passed corr %.3f' % (acce_want, gs['parabola_fit_correlation'])))
                else:
                    acce_state_ok += 1
                    st_acce.add(stamp, acce_want, round(exp_f, 2), 'accel %.3f corr %.3f' % (gs['bg_acceleration'], gs['parabola_fit_correlation']))
    lines = ['ns_glucose validation %s .. %s  grid=%s smoothing=%s  loops=%d (duplicate uploads %d) skipped=%d' % (
        start_iso, end_iso, grid, smoothing, len(devicestatus) - duplicates, duplicates, skipped)]
    for st in (st_bg, st_tick, st_dura, st_dura_bp, st_dura_f, st_dura_avg, st_acce, st_corr, st_para_v, st_para_m):
        lines.append(st.report())
    lines.append('%-28s %d loops %s, unmatched (kept newest) %d' % (
        'pinned to an older reading', sum(gh.pinned.values()),
        '{%s}' % ', '.join('%d back: %d' % kv for kv in sorted(gh.pinned.items())), gh.pin_unmatched))
    if acce_state_n:
        lines.append('%-28s n=%5d  agree %5.1f%%  mismatch %4d' % ('acce_ISF active/by-passed', acce_state_n,
                                                                    100.0 * acce_state_ok / acce_state_n, len(acce_state_bad)))
        for stamp, why in acce_state_bad[:5]:
            lines.append('      %s  %s' % (stamp, why))
    text = '\n'.join(lines)
    if verbose:
        print(text)
    return {'tick': st_tick, 'bg': st_bg, 'dura': st_dura, 'dura_f': st_dura_f, 'dura_avg': st_dura_avg,
            'acce': st_acce, 'para_v': st_para_v, 'text': text}


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--grid') and not a.startswith('--smoothing')]
    opts = dict(a[2:].split('=', 1) for a in sys.argv[1:] if a.startswith('--') and '=' in a)
    if len(args) >= 3 and args[0] == '--validate':
        validate(args[1], args[2], args[3] if len(args) > 3 else 'ns:',
                 grid=opts.get('grid', 'moving'), smoothing=opts.get('smoothing', 'kalman'))
    else:
        print(__doc__)
        print('usage: python ns_glucose.py --validate <startISO> <endISO> [url] [--grid=moving|<unix ms>] [--smoothing=kalman|none]')
        sys.exit(1)
