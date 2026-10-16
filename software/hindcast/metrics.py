"""
metrics.py  -  Hindcast per-trial metrics (H-201) and the ranking rule (H-202) on top of emulator_optimize

Everything here is a DECISION-LEVEL counterfactual on recorded inputs (DESIGN.md section 5): the replay
hands every variant the glucose, IOB and COB the phone's own settings produced, so the numbers say what
the loop would have asked for, never what glucose would have done.  The realised glucose after a loop is
used in exactly two places and both are positional: the two hours before a recorded low (constraint P)
and the hindsight covariates stored for shading.  It is never multiplied into a score.

Layers
    LoopSeries                 the per-loop columns of one result CSV (phone and emulated side by side) plus the
                               optional per-loop inputs the CSV cannot carry (emulated minGuardBG, zero-temp flag,
                               SMB-disabled-by-TT flag); `LoopSeries.from_csv()` reads the 43-column emulator CSV
                               through emulator_optimize.read_result_csv, `from_arrays()` builds synthetic series.
    trial_metrics(...)         one metrics dict per (variant, day): emulator_optimize.metrics_from_csv() verbatim
                               plus the section 5.2 additions, with a `flags` list naming every fallback taken.
    floors_from_base(...)      floor_X = |X_base - X_phone| per component; floor_loops = changed_loops(base).
    rank_variants(...)         feasibility, ties, Pareto front / review order / scalar, recommended_for_review,
                               and the printed ranking-rule text (section 5.3).

Units: insulin in U, rates in U/h, glucose in mg/dl inside this module (the CSV is mg/dl); the thresholds
are defined in mmol/l where the design states them and converted once (MMOL_TO_MGDL = 18.0, as ns_profile).

Give per loop = SMB + rate-in-force x dt with dt capped at 30 min (the optimizer's convention, reused through
`loop_intervals_h()`).  The CSV's TBR columns are "rate in force" (a new temp, else the running one), so a 0
means "no temp or a zero temp"; `zero_temp_vs_base` therefore prefers the explicit per-loop `zero_temp`
input and falls back to the rate columns with the flag `zero_temp_rate_proxy`.

OBJECTIVE_VERSION is part of every trial key (contracts.key()); bump it when a metric definition or a
feasibility rule changes, so that old trials.jsonl rows are not compared with new ones.
"""
import math
import os
import statistics
import sys
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HERE)
if SOFTWARE_DIR not in sys.path:                      # the emulator scripts are plain siblings of this package
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')              # emulator_optimize sets it anyway; emulator_core insists on it

import emulator_optimize as _opt                       # noqa: E402  imported, never copied (DESIGN.md 4.1)

OBJECTIVE_VERSION = 1

# ----------------------------------------------------------------------------- constants (DESIGN.md 5.1 / 5.2)
MMOL_TO_MGDL = 18.0
EPS = _opt.EPS                                         # 0.001 U: "equal" for SMB counts, as the optimizer
TOL_U = 0.05                                           # SMB and insulinReq tolerance (one bolus increment)
TOL_RATE_U_H = 0.05                                    # temp basal rate tolerance
TOL_ISF_MGDL = 1.0                                     # variable_sens tolerance
LOW_BG_MGDL = 3.9 * MMOL_TO_MGDL                       # 70.2: "a recorded low"
LOWSIDE_BG_MGDL = 5.0 * MMOL_TO_MGDL                   # 90.0: recorded BG below this and falling = low side
HIGH_BG_MGDL = 180.0                                   # hindsight covariate "a high followed"
PREHYPO_S = 120 * 60                                   # constraint P: the two hours before a recorded low
LOW_FOLLOW_S = PREHYPO_S                               # covariate: a low within 2 h
HIGH_FOLLOW_S = 180 * 60                               # covariate: a high within 3 h
ENVELOPE_WINDOW_S = 3 * 3600                           # E: sliding 3 h window
FALL_LOOKBACK_S = 300                                  # "falling" = bg minus the bg 5 min earlier < 0 (the optimizer's delta)
FLOOR_MIN_U = 0.05                                     # a floor below one bolus increment is treated as one increment
FLOOR_MIN_LOOPS = 1
TIE_FACTOR = 2.0                                       # |difference| < TIE_FACTOR x floor = tie (5.1, 5.3 rule 4)
BASE_VARIANT = 'base'
QUESTIONS = ('explore', 'earlier', 'gentler')

DEFAULT_OBJECTIVE = {
    'scalar_weights': {'rest': 2.0, 'lowside': 0.3, 'zero_temp': 0.3},
    'envelope': {'max_3h_U': 1.0, 'max_day_pct_tdd': 15.0},
    'feasible': {'cap_binding_max_pct': 50.0},
    'recommend': {'fold_top': 3, 'fold_share': 0.8, 'sign_share': 0.8, 'holdout_top': 5},
}

# metric key -> floor key: the components that get a floor, a tie test and a floor band on the page
FLOOR_COMPONENTS = {
    'rise_vs_base_U': 'floor_rise_U',
    'rest_vs_base_U': 'floor_rest_U',
    'delta_vs_base_U': 'floor_total_U',
    'smb_vs_base_U': 'floor_smb_U',
    'tbr_vs_base_U': 'floor_tbr_U',
    'changed_vs_base': 'floor_loops',
}
REVIEW_COMPONENTS = ('rise_vs_base_U', 'rest_vs_base_U')     # review order: |effect| / floor on the larger of the two


def _opt_metrics_from_csv(path, autoisf_max=None, autoisf_min=None):
    """indirection so tests can monkeypatch; keeps `metrics_from_csv` the optimizer's"""
    return _opt.metrics_from_csv(path, autoisf_max, autoisf_min)


metrics_from_csv = _opt.metrics_from_csv              # re-exported for callers that only want the optimizer's dict
read_result_csv = _opt.read_result_csv
effective_cap = _opt.effective_cap


# ----------------------------------------------------------------------------- per-loop series
def loop_intervals_h(t: Sequence[float]) -> List[float]:
    """interval to the next loop in hours, last loop = median interval, every interval capped at 30 min
    (emulator_optimize.metrics_from_csv's convention: a CGM gap is not a 1 h temp)"""
    n = len(t)
    if n == 0:
        return []
    gaps = [t[i + 1] - t[i] for i in range(n - 1) if t[i + 1] > t[i]]
    med = statistics.median(gaps) if gaps else 60.0
    dt_h = [((t[i + 1] - t[i]) if i < n - 1 and t[i + 1] > t[i] else med) / 3600.0 for i in range(n)]
    return [min(d, 0.5) for d in dt_h]


def _opt_list(values, n):
    if values is None:
        return [None] * n
    values = list(values)
    if len(values) != n:
        raise ValueError(f'per-loop input has {len(values)} entries, the series {n} loops')
    return values


@dataclass
class LoopSeries:
    """The per-loop columns of one result CSV, phone ('phone') and emulated ('emul') side by side.

    t              unix seconds of the loop (`UNIX time`)
    bg             recorded glucose used by the loop, mg/dl (`bg accel`)
    smb_*, insreq_* U; rate_* U/h (rate IN FORCE, see module docstring); isf_* mg/dl per U (`ISF orig` / `ISF emul`);
    maxbolus_phone the phone's maxBolus of the loop (0 = not echoed / SMB not allowed), `max bolus orig`
    target_emul    emulated target (mid of low/high), mg/dl; iob, iobth_emul as in the CSV
    isf_factor_*   final autoISF factor
    min_guard      emulated minGuardBG per loop in mg/dl, None where unknown (from Pred[...]['Levels'] via the replay;
                   the CSV has none).  `lowside_flags` falls back to the recorded-BG rule where it is None.
    zero_temp      True where the emulated decision set a 0 U/h temp (from the reason/Flows), None = unknown
    smb_off        True where SMB was disabled by the temp-target parity on the phone and the variant alike, None = unknown
    """
    t: List[float]
    bg: List[float]
    smb_phone: List[float]
    smb_emul: List[float]
    insreq_phone: List[float]
    insreq_emul: List[float]
    rate_phone: List[float]
    rate_emul: List[float]
    isf_phone: List[float] = field(default_factory=list)
    isf_emul: List[float] = field(default_factory=list)
    maxbolus_phone: List[float] = field(default_factory=list)
    target_emul: List[float] = field(default_factory=list)
    iob: List[float] = field(default_factory=list)
    iobth_emul: List[float] = field(default_factory=list)
    isf_factor_phone: List[float] = field(default_factory=list)
    isf_factor_emul: List[float] = field(default_factory=list)
    min_guard: List[Optional[float]] = field(default_factory=list)
    zero_temp: Optional[List[bool]] = None
    smb_off: Optional[List[bool]] = None
    source: str = ''

    def __post_init__(self):
        n = len(self.t)
        for name in ('bg', 'smb_phone', 'smb_emul', 'insreq_phone', 'insreq_emul', 'rate_phone', 'rate_emul'):
            if len(getattr(self, name)) != n:
                raise ValueError(f'LoopSeries.{name} has {len(getattr(self, name))} entries, t has {n}')
        for name in ('isf_phone', 'isf_emul', 'maxbolus_phone', 'target_emul', 'iob', 'iobth_emul',
                     'isf_factor_phone', 'isf_factor_emul'):
            if not getattr(self, name):
                setattr(self, name, [0.0] * n)
            elif len(getattr(self, name)) != n:
                raise ValueError(f'LoopSeries.{name} has {len(getattr(self, name))} entries, t has {n}')
        self.min_guard = _opt_list(self.min_guard or None, n)
        if self.zero_temp is not None:
            self.zero_temp = [bool(v) for v in _opt_list(self.zero_temp, n)]
        if self.smb_off is not None:
            self.smb_off = [bool(v) for v in _opt_list(self.smb_off, n)]

    @property
    def n(self) -> int:
        return len(self.t)

    @property
    def dt_h(self) -> List[float]:
        return loop_intervals_h(self.t)

    @property
    def give_phone(self) -> List[float]:
        return [s + r * d for s, r, d in zip(self.smb_phone, self.rate_phone, self.dt_h)]

    @property
    def give_emul(self) -> List[float]:
        return [s + r * d for s, r, d in zip(self.smb_emul, self.rate_emul, self.dt_h)]

    @classmethod
    def from_csv(cls, path, min_guard=None, zero_temp=None, smb_off=None) -> 'LoopSeries':
        """the 43-column emulator CSV (3 header lines, ';') -> LoopSeries; optional per-loop inputs aligned to its rows"""
        names, rows = _opt.read_result_csv(path)
        need = ['UNIX time', 'bg accel', 'target low emul', 'target high emul', 'iob', 'eff. iobTH emul',
                'final ISF orig', 'final ISF emul', 'ISF orig', 'ISF emul', 'Ins. Req. orig', 'Ins. Req. emul',
                'max bolus orig', 'SMB orig', 'SMB emul', 'TBR orig', 'TBR emul']
        missing = [c for c in need if c not in names]
        if missing:
            raise KeyError(f'{path}: CSV columns not found: {missing}')
        f = _opt.fnum
        tgt = [(lo + hi) / 2 for lo, hi in zip(f(rows, 'target low emul'), f(rows, 'target high emul'))]
        return cls(t=f(rows, 'UNIX time'), bg=f(rows, 'bg accel'),
                   smb_phone=f(rows, 'SMB orig'), smb_emul=f(rows, 'SMB emul'),
                   insreq_phone=f(rows, 'Ins. Req. orig'), insreq_emul=f(rows, 'Ins. Req. emul'),
                   rate_phone=f(rows, 'TBR orig'), rate_emul=f(rows, 'TBR emul'),
                   isf_phone=f(rows, 'ISF orig'), isf_emul=f(rows, 'ISF emul'),
                   maxbolus_phone=f(rows, 'max bolus orig'), target_emul=tgt,
                   iob=f(rows, 'iob'), iobth_emul=f(rows, 'eff. iobTH emul'),
                   isf_factor_phone=f(rows, 'final ISF orig'), isf_factor_emul=f(rows, 'final ISF emul'),
                   min_guard=min_guard, zero_temp=zero_temp, smb_off=smb_off, source=str(path))

    @classmethod
    def from_arrays(cls, t, bg, smb_phone=None, smb_emul=None, rate_phone=None, rate_emul=None,
                    insreq_phone=None, insreq_emul=None, **kw) -> 'LoopSeries':
        """synthetic series for tests: every omitted column is zeros"""
        n = len(t)
        z = lambda v: list(v) if v is not None else [0.0] * n   # noqa: E731
        return cls(t=list(t), bg=list(bg), smb_phone=z(smb_phone), smb_emul=z(smb_emul),
                   insreq_phone=z(insreq_phone), insreq_emul=z(insreq_emul),
                   rate_phone=z(rate_phone), rate_emul=z(rate_emul), **kw)


def min_guard_from_levels(levels: Optional[dict]) -> Optional[float]:
    """emulated minGuardBG (mg/dl) from one Pred[...]['Levels'] dict, or None.

    determine_basal.py stores `minGuardBG` only when the guard disabled SMB (blend case) and `minGuardBG1` for
    every SMB-off case; a loop with SMB enabled carries neither.  Callers that have the emulated reason text can
    use `min_guard_from_reason()` instead, which covers every loop."""
    if not levels:
        return None
    for key in ('minGuardBG', 'minGuardBG1'):
        v = levels.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def min_guard_from_reason(reason: Optional[str], out_units: str = 'mg/dl') -> Optional[float]:
    """emulated minGuardBG (mg/dl) scraped from determine_basal.py's reason text ('..., minGuardBG 86, ...');
    the value is printed in the profile's units (convert_bg), so pass out_units='mmol/L' for an mmol profile."""
    if not reason or 'minGuardBG' not in reason:
        return None
    tail = reason.split('minGuardBG', 1)[1].strip()
    if tail.startswith('<'):                          # the 'minGuardBG 3.4<3.8' form of the LGS branch starts with the value
        return None
    num = ''
    for ch in tail:
        if ch.isdigit() or ch in '.-':
            num += ch
        else:
            break
    try:
        v = float(num)
    except ValueError:
        return None
    return v * MMOL_TO_MGDL if str(out_units).replace('\\', '').lower().startswith('mmol') else v


# ----------------------------------------------------------------------------- positional helpers
def _recorded_lows(t, bg, low=LOW_BG_MGDL):
    return [ti for ti, b in zip(t, bg) if b < low]


def hindsight_covariates(t: Sequence[float], bg: Sequence[float]) -> Dict[str, List[int]]:
    """positional covariates per loop, 0/1 - stored in the series for shading and filtering, never scored:
    before_low_2h  a recorded BG < 3.9 mmol follows within 2 h (the P window)
    low_within_2h  alias of before_low_2h (the drill-down badge "a low followed")
    high_within_3h a recorded BG > 180 mg/dl follows within 3 h"""
    n = len(t)
    lows = _recorded_lows(t, bg)
    highs = [ti for ti, b in zip(t, bg) if b > HIGH_BG_MGDL]
    before_low = [0] * n
    before_high = [0] * n
    j = 0
    for i in range(n):
        while j < len(lows) and lows[j] <= t[i]:
            j += 1
        if j < len(lows) and lows[j] - t[i] <= LOW_FOLLOW_S:
            before_low[i] = 1
    j = 0
    for i in range(n):
        while j < len(highs) and highs[j] <= t[i]:
            j += 1
        if j < len(highs) and highs[j] - t[i] <= HIGH_FOLLOW_S:
            before_high[i] = 1
    return {'before_low_2h': before_low, 'low_within_2h': list(before_low), 'high_within_3h': before_high}


def prehypo_extra_U(t: Sequence[float], bg: Sequence[float], give: Sequence[float], give_ref: Sequence[float]) -> float:
    """P: sum over the loops in the 120 min before a recorded BG < 3.9 mmol of max(give - give_ref, 0).
    'Any additional insulin before a low that actually happened' - the low itself is positional, never scored."""
    mask = hindsight_covariates(t, bg)['before_low_2h']
    return sum(max(a - b, 0.0) for a, b, m in zip(give, give_ref, mask) if m)


def envelope(t: Sequence[float], give: Sequence[float], give_ref: Sequence[float], tdd_ref_U: Optional[float]) -> Dict[str, Optional[float]]:
    """E: env_3h_max_U = max over sliding 3 h windows [t_i, t_i + 3 h) of |sum(give - give_ref)|,
    env_3h_signed_U the signed sum of that window, env_day_U = sum over the whole series,
    env_day_pct_tdd = |env_day_U| / tdd_ref_U x 100 (None when the TDD is unknown)."""
    n = len(t)
    diff = [a - b for a, b in zip(give, give_ref)]
    best = 0.0
    best_signed = 0.0
    j = 0
    running = 0.0
    for i in range(n):
        if j < i:                                     # window start moved past the old end: restart the sum
            j = i
            running = 0.0
        while j < n and t[j] - t[i] < ENVELOPE_WINDOW_S:
            running += diff[j]
            j += 1
        if abs(running) > best:
            best, best_signed = abs(running), running
        running -= diff[i]
    day = sum(diff)
    pct = None
    if tdd_ref_U is not None and tdd_ref_U > 0:
        pct = 100.0 * abs(day) / tdd_ref_U
    return {'env_3h_max_U': best, 'env_3h_signed_U': best_signed, 'env_day_U': day, 'env_day_pct_tdd': pct}


def falling_flags(t: Sequence[float], bg: Sequence[float]) -> List[bool]:
    """bg minus the bg 5 min earlier (nearest earlier loop) < 0; the optimizer's delta rule"""
    n = len(t)
    out = [False] * n
    j = 0
    for i in range(n):
        while j < i and t[i] - t[j + 1] >= FALL_LOOKBACK_S:
            j += 1
        out[i] = i > 0 and (bg[i] - bg[j]) < 0
    return out


def changed_flags(s: LoopSeries, ref: Optional[LoopSeries] = None) -> List[bool]:
    """per loop: SMB (0.05 U), insulinReq (0.05 U) or rate (0.05 U/h) of the emulated decision differ from the
    phone's (ref=None) or from another series' emulated decision (ref given)"""
    if ref is None:
        smb_r, req_r, rate_r = s.smb_phone, s.insreq_phone, s.rate_phone
    else:
        _same_length(s, ref)
        smb_r, req_r, rate_r = ref.smb_emul, ref.insreq_emul, ref.rate_emul
    return [abs(a - b) > TOL_U + 1e-9 or abs(c - d) > TOL_U + 1e-9 or abs(e - f) > TOL_RATE_U_H + 1e-9
            for a, b, c, d, e, f in zip(s.smb_emul, smb_r, s.insreq_emul, req_r, s.rate_emul, rate_r)]


def _same_length(a: LoopSeries, b: LoopSeries):
    if a.n != b.n:
        raise ValueError(f'loop counts differ: {a.n} vs {b.n} loops (state leak or failed run? DESIGN.md 5.1)')


def agreement(s: LoopSeries) -> Dict[str, float]:
    """the no-change agreement of the emulated decisions with the phone's, in % of loops (meaningful for base):
    smb_exact_pct (< 0.001 U), smb_within_pct (0.05 U), insreq_within_pct (0.05 U), rate_within_pct (0.05 U/h),
    isf_within_pct (1 mg/dl), agreement_pct = all four within tolerance"""
    n = s.n
    if n == 0:
        return {k: None for k in ('smb_exact_pct', 'smb_within_pct', 'insreq_within_pct', 'rate_within_pct',
                                  'isf_within_pct', 'agreement_pct')}
    smb_ok = [abs(a - b) <= TOL_U + 1e-9 for a, b in zip(s.smb_emul, s.smb_phone)]
    req_ok = [abs(a - b) <= TOL_U + 1e-9 for a, b in zip(s.insreq_emul, s.insreq_phone)]
    rate_ok = [abs(a - b) <= TOL_RATE_U_H + 1e-9 for a, b in zip(s.rate_emul, s.rate_phone)]
    isf_ok = [abs(a - b) <= TOL_ISF_MGDL + 1e-9 for a, b in zip(s.isf_emul, s.isf_phone)]
    pct = lambda flags: 100.0 * sum(1 for v in flags if v) / n   # noqa: E731
    return {'smb_exact_pct': pct(abs(a - b) < EPS for a, b in zip(s.smb_emul, s.smb_phone)),
            'smb_within_pct': pct(smb_ok), 'insreq_within_pct': pct(req_ok), 'rate_within_pct': pct(rate_ok),
            'isf_within_pct': pct(isf_ok), 'agreement_pct': pct(all(x) for x in zip(smb_ok, req_ok, rate_ok, isf_ok))}


# ----------------------------------------------------------------------------- per-trial metrics
def trial_metrics(csv_path, base=None, autoisf_max=None, autoisf_min=None, min_guard=None, zero_temp=None,
                  smb_off=None, tdd_phone_U=None, series: Optional[LoopSeries] = None) -> dict:
    """One metrics dict per (variant, day) = emulator_optimize.metrics_from_csv(csv_path, caps) + the section 5.2
    additions.  `base` is the base replay of the same day (LoopSeries or CSV path); None means "this trial IS the
    base", then every *_vs_base metric is 0 and the vs-phone values are what floors_from_base() reads.
    `autoisf_max` / `autoisf_min`: number or cap_at(ms) from emulator_optimize.effective_cap (profile_history aware).
    `min_guard`, `zero_temp`, `smb_off`: optional per-loop lists aligned to the CSV rows (see LoopSeries).
    `tdd_phone_U`: the phone's TDD of the day for env_day_pct_tdd; None -> the window's total_orig_U with the flag
    `tdd_from_window`.  `series`: pass a ready LoopSeries instead of reading csv_path again (csv_path may then be None
    only if the optimizer metrics are not wanted - they need the file).

    Flags (list of strings, always present): `lowside_no_pred` (no emulated minGuardBG on any loop: the recorded-BG rule
    alone), `lowside_pred_partial` (some loops), `zero_temp_rate_proxy`, `tt_smb_off_unknown`, `tdd_from_window`,
    `cap_unknown` (no autoISF_max/min cap given: cap_binding_pct is None), `maxbolus_unknown` (the phone echoed no
    maxBolus on any loop)."""
    s = series if series is not None else LoopSeries.from_csv(csv_path, min_guard, zero_temp, smb_off)
    if series is not None:
        for name, val in (('min_guard', min_guard), ('zero_temp', zero_temp), ('smb_off', smb_off)):
            if val is not None:
                setattr(s, name, _opt_list(val, s.n) if name == 'min_guard' else [bool(v) for v in _opt_list(val, s.n)])
    m = {'loops': s.n}
    if csv_path is not None:
        m.update(_opt_metrics_from_csv(csv_path, autoisf_max, autoisf_min))
    else:
        m.update(_series_optimizer_metrics(s, autoisf_max, autoisf_min))
    flags = []
    if s.n == 0:
        m['flags'] = ['no_loops']
        return m
    if isinstance(base, str):
        base = LoopSeries.from_csv(base)
    is_base = base is None
    ref = s if is_base else base
    _same_length(s, ref)

    give_e, give_p, give_b = s.give_emul, s.give_phone, ref.give_emul
    rise_mask = _rise_mask(s)
    m.update(agreement(s))
    changed_phone = changed_flags(s)
    m['changed_loops'] = sum(changed_phone)
    m['changed_loops_base'] = sum(changed_flags(ref))
    m['changed_vs_base'] = m['changed_loops'] - m['changed_loops_base']
    m['changed_vs_base_loops'] = 0 if is_base else sum(changed_flags(s, ref))     # loops where variant and base differ
    m['smb_vs_base_U'] = sum(s.smb_emul) - sum(ref.smb_emul)
    m['tbr_vs_base_U'] = sum(r * d for r, d in zip(s.rate_emul, s.dt_h)) - sum(r * d for r, d in zip(ref.rate_emul, ref.dt_h))
    m['delta_vs_base_U'] = sum(give_e) - sum(give_b)
    m['rise_vs_base_U'] = sum(a - b for a, b, r in zip(give_e, give_b, rise_mask) if r)
    m['rest_vs_base_U'] = sum(a - b for a, b, r in zip(give_e, give_b, rise_mask) if not r)
    m['rise_vs_phone_U'] = sum(a - b for a, b, r in zip(give_e, give_p, rise_mask) if r)
    m['rest_vs_phone_U'] = sum(a - b for a, b, r in zip(give_e, give_p, rise_mask) if not r)
    m['smb_more_vs_base'] = sum(1 for a, b in zip(s.smb_emul, ref.smb_emul) if a > b + EPS)
    m['smb_less_vs_base'] = sum(1 for a, b in zip(s.smb_emul, ref.smb_emul) if a < b - EPS)

    # low-side proxy: emulated minGuardBG (when known) or the recorded-BG rule, only on loops that SMB more than the phone
    known = sum(1 for v in s.min_guard if v is not None)
    if known == 0:
        flags.append('lowside_no_pred')
    elif known < s.n:
        flags.append('lowside_pred_partial')
    falling = falling_flags(s.t, s.bg)
    low_side = [(mg is not None and mg < LOW_BG_MGDL) or (b < LOWSIDE_BG_MGDL and f)
                for mg, b, f in zip(s.min_guard, s.bg, falling)]
    m['lowside_flags'] = sum(1 for a, b, ls in zip(s.smb_emul, s.smb_phone, low_side) if a > b + EPS and ls)
    m['lowside_rule'] = 'recorded_bg_only' if known == 0 else ('pred_and_recorded_bg' if known == s.n else 'pred_partial')

    # P and E (vs base = the metric; vs phone = what the base's floor is made of)
    m['prehypo_extra_U'] = prehypo_extra_U(s.t, s.bg, give_e, give_b)
    m['prehypo_extra_vs_phone_U'] = prehypo_extra_U(s.t, s.bg, give_e, give_p)
    m['prehypo_loops'] = sum(hindsight_covariates(s.t, s.bg)['before_low_2h'])
    m['lows_recorded'] = len(_recorded_lows(s.t, s.bg))
    tdd = tdd_phone_U
    if tdd is None:
        tdd = sum(give_p)
        flags.append('tdd_from_window')
    m['tdd_phone_U'] = tdd
    m.update(envelope(s.t, give_e, give_b, tdd))
    env_phone = envelope(s.t, give_e, give_p, tdd)
    m['env_3h_vs_phone_U'] = env_phone['env_3h_max_U']
    m['env_day_pct_tdd_vs_phone'] = env_phone['env_day_pct_tdd']

    # zero temps: explicit indicator, else the rate-in-force proxy
    if s.zero_temp is not None and ref.zero_temp is not None:
        zt_e, zt_b = s.zero_temp, ref.zero_temp
    else:
        flags.append('zero_temp_rate_proxy')
        zt_e = [r <= 1e-9 and rb > 1e-9 for r, rb in zip(s.rate_emul, ref.rate_emul)]       # variant 0 where base ran a temp
        zt_b = [rb <= 1e-9 and r > 1e-9 for r, rb in zip(s.rate_emul, ref.rate_emul)]
    m['zero_temp_vs_base'] = sum(1 for a, b in zip(zt_e, zt_b) if a and not b) - sum(1 for a, b in zip(zt_e, zt_b) if b and not a)

    # S: cap binding
    at_max, at_min = m.get('at_autoISF_max_pct'), m.get('at_autoISF_min_pct')
    if at_max is None and at_min is None:
        m['cap_binding_pct'] = None
        flags.append('cap_unknown')
    else:
        m['cap_binding_pct'] = (at_max or 0.0) + (at_min or 0.0)

    # maxBolus safety reading
    m['max_single_smb_U'] = max(s.smb_emul)
    mb_known = [mb for mb in s.maxbolus_phone if mb > 0]
    if not mb_known:
        flags.append('maxbolus_unknown')
        m['smb_over_maxbolus_loops'] = None
        m['maxbolus_phone_min_U'] = None
    else:
        m['smb_over_maxbolus_loops'] = sum(1 for a, mb in zip(s.smb_emul, s.maxbolus_phone) if mb > 0 and a > mb + EPS)
        m['maxbolus_phone_min_U'] = min(mb_known)

    if s.smb_off is None:
        m['tt_smb_off_pct'] = None
        flags.append('tt_smb_off_unknown')
    else:
        m['tt_smb_off_pct'] = 100.0 * sum(1 for v in s.smb_off if v) / s.n
    m['is_base'] = is_base
    m['flags'] = flags
    return m


def _rise_mask(s: LoopSeries) -> List[bool]:
    """bg above the emulated target and rising (the optimizer's rise/rest split)"""
    n = s.n
    out = [False] * n
    j = 0
    for i in range(n):
        while j < i and s.t[i] - s.t[j + 1] >= FALL_LOOKBACK_S:
            j += 1
        delta = s.bg[i] - s.bg[j] if i > 0 else 0.0
        out[i] = s.bg[i] > s.target_emul[i] and delta > 0
    return out


def _series_optimizer_metrics(s: LoopSeries, autoisf_max=None, autoisf_min=None) -> dict:
    """the optimizer's sums for a synthetic LoopSeries (no CSV); same definitions as metrics_from_csv"""
    n = s.n
    m = {}
    if n == 0:
        return m
    dt = s.dt_h
    m['smb_orig_U'] = sum(s.smb_phone)
    m['smb_emul_U'] = sum(s.smb_emul)
    m['tbr_orig_U'] = sum(r * d for r, d in zip(s.rate_phone, dt))
    m['tbr_emul_U'] = sum(r * d for r, d in zip(s.rate_emul, dt))
    m['total_orig_U'] = m['smb_orig_U'] + m['tbr_orig_U']
    m['total_emul_U'] = m['smb_emul_U'] + m['tbr_emul_U']
    m['delta_U'] = m['total_emul_U'] - m['total_orig_U']
    m['delta_pct'] = 100.0 * m['delta_U'] / m['total_orig_U'] if m['total_orig_U'] > 0 else 0.0
    m['smb_more'] = sum(1 for a, b in zip(s.smb_emul, s.smb_phone) if a > b + EPS)
    m['smb_less'] = sum(1 for a, b in zip(s.smb_emul, s.smb_phone) if a < b - EPS)
    m['smb_equal'] = n - m['smb_more'] - m['smb_less']
    m['smb_loops_orig'] = sum(1 for v in s.smb_phone if v > EPS)
    m['smb_loops_emul'] = sum(1 for v in s.smb_emul if v > EPS)
    m['smb_max_emul_U'] = max(s.smb_emul)
    m['isf_factor_orig_mean'] = statistics.fmean(s.isf_factor_phone)
    m['isf_factor_emul_mean'] = statistics.fmean(s.isf_factor_emul)
    m['isf_factor_emul_max'] = max(s.isf_factor_emul)
    m['isf_factor_emul_min'] = min(s.isf_factor_emul)
    caps_max = _opt.cap_series(autoisf_max, s.t)
    caps_min = _opt.cap_series(autoisf_min, s.t)
    m['at_autoISF_max_pct'] = 100.0 * sum(1 for v, c in zip(s.isf_factor_emul, caps_max) if c and v >= c - 0.005) / n if caps_max else None
    m['at_autoISF_min_pct'] = 100.0 * sum(1 for v, c in zip(s.isf_factor_emul, caps_min) if c and v <= c + 0.005) / n if caps_min else None
    m['isf_emul_mean'] = statistics.fmean(s.isf_emul)
    m['iob_over_TH_pct'] = 100.0 * sum(1 for a, b in zip(s.iob, s.iobth_emul) if b > 0 and a > b) / n
    m['iob_max'] = max(s.iob)
    rise = _rise_mask(s)
    gp, ge = s.give_phone, s.give_emul
    m['rise_loops'] = sum(rise)
    m['rise_orig_U'] = sum(g for g, r in zip(gp, rise) if r)
    m['rise_emul_U'] = sum(g for g, r in zip(ge, rise) if r)
    m['rest_orig_U'] = sum(g for g, r in zip(gp, rise) if not r)
    m['rest_emul_U'] = sum(g for g, r in zip(ge, rise) if not r)
    m['bg_mean'] = statistics.fmean(s.bg)
    m['bg_gt180_pct'] = 100.0 * sum(1 for v in s.bg if v > 180) / n
    m['bg_lt70_pct'] = 100.0 * sum(1 for v in s.bg if v < 70) / n
    return m


# ----------------------------------------------------------------------------- H-202: floors and ties
def floors_from_base(base_metrics: dict) -> Dict[str, float]:
    """floor_X = |X_base - X_phone| per component of one day (ShadowLoop's rule), from the base trial's metrics:
    floor_smb_U, floor_tbr_U, floor_total_U, floor_rise_U, floor_rest_U, floor_P_U (the base's own pre-hypo extra
    vs the phone), floor_loops = changed_loops(base), plus agreement_pct for the banner."""
    g = lambda k: base_metrics.get(k) or 0.0   # noqa: E731
    return {'floor_smb_U': abs(g('smb_emul_U') - g('smb_orig_U')),
            'floor_tbr_U': abs(g('tbr_emul_U') - g('tbr_orig_U')),
            'floor_total_U': abs(g('delta_U')),
            'floor_rise_U': abs(g('rise_emul_U') - g('rise_orig_U')),
            'floor_rest_U': abs(g('rest_emul_U') - g('rest_orig_U')),
            'floor_P_U': g('prehypo_extra_vs_phone_U'),
            'floor_loops': float(g('changed_loops')),
            'agreement_pct': base_metrics.get('agreement_pct'),
            'loops': base_metrics.get('loops')}


def effective_floor(floor_value: Optional[float], key: str = '') -> float:
    """a floor smaller than one bolus increment (0.05 U) or one loop is widened to that, so that a component the base
    reproduces perfectly does not turn every difference into a finding"""
    lo = FLOOR_MIN_LOOPS if key == 'floor_loops' else FLOOR_MIN_U
    return max(float(floor_value or 0.0), lo)


def is_tie(difference: float, floor_value: Optional[float], key: str = '') -> bool:
    """|difference| < TIE_FACTOR x effective floor -> the two values are a tie on this component"""
    return abs(difference) < TIE_FACTOR * effective_floor(floor_value, key)


def tie_components(metrics_a: dict, metrics_b: dict, floors: dict) -> Dict[str, bool]:
    """per FLOOR_COMPONENTS: is the difference between two trials of the same day a tie"""
    out = {}
    for key, fkey in FLOOR_COMPONENTS.items():
        a, b = metrics_a.get(key), metrics_b.get(key)
        if a is None or b is None:
            continue
        out[key] = is_tie(a - b, floors.get(fkey), fkey)
    return out


def tie_with_base(metrics: dict, floors: dict) -> bool:
    """a trial whose *_vs_base differences are all inside TIE_FACTOR x floor is a tie with base on that day"""
    ties = tie_components(metrics, {k: 0.0 for k in FLOOR_COMPONENTS}, floors)
    return bool(ties) and all(ties.values())


def effect_over_floor(metrics: dict, floors: dict, components: Sequence[str] = REVIEW_COMPONENTS) -> Tuple[float, str]:
    """(|effect| / floor on the component where that ratio is largest, that component)"""
    best, which = 0.0, components[0]
    for key in components:
        v = metrics.get(key)
        if v is None:
            continue
        r = abs(v) / effective_floor(floors.get(FLOOR_COMPONENTS[key]), FLOOR_COMPONENTS[key])
        if r > best:
            best, which = r, key
    return best, which


# ----------------------------------------------------------------------------- H-202: feasibility, aggregation
def _objective(objective: Optional[dict]) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_OBJECTIVE.items()}
    for k, v in (objective or {}).items():
        if isinstance(v, dict) and k in cfg:
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def feasibility_day(metrics: dict, base_metrics: dict, floors: dict, objective: Optional[dict] = None) -> List[str]:
    """the infeasibility reasons of one trial on one day (empty = feasible), DESIGN.md 5.3 rule 1:
    P <= P_base + floor_P; inside the envelope E; max_single_smb_U <= the phone's maxBolus; cap_binding_pct < 50 %;
    loop count equal to base.  Reasons are short labels for the greyed leaderboard row."""
    cfg = _objective(objective)
    reasons = []
    if metrics.get('loops') != base_metrics.get('loops'):
        reasons.append(f'loop count {metrics.get("loops")} differs from base {base_metrics.get("loops")}')
    if 'error' in metrics or metrics.get('loops') in (None, 0):
        reasons.append('failed replay')
        return reasons
    p = metrics.get('prehypo_extra_U')
    if p is not None:
        p_base = base_metrics.get('prehypo_extra_U') or 0.0
        limit = p_base + effective_floor(floors.get('floor_P_U'))
        if p > limit + 1e-9:
            reasons.append(f'asks for {p:.2f} U more than base in the two hours before a recorded low (limit {limit:.2f} U)')
    env3 = metrics.get('env_3h_max_U')
    if env3 is not None and env3 > cfg['envelope']['max_3h_U'] + 1e-9:
        reasons.append(f'out of envelope: {env3:.2f} U vs base in a 3 h window (limit {cfg["envelope"]["max_3h_U"]} U)')
    envd = metrics.get('env_day_pct_tdd')
    if envd is not None and envd > cfg['envelope']['max_day_pct_tdd'] + 1e-9:
        reasons.append(f'out of envelope: {envd:.1f} % of the phone\'s TDD vs base (limit {cfg["envelope"]["max_day_pct_tdd"]} %)')
    over = metrics.get('smb_over_maxbolus_loops')
    if over:
        reasons.append(f'emulated SMB above the phone\'s maxBolus in {over} loops')
    cap = metrics.get('cap_binding_pct')
    if cap is not None and cap >= cfg['feasible']['cap_binding_max_pct']:
        reasons.append(f'factor on a cap in {cap:.1f} % of loops (not really tested; limit {cfg["feasible"]["cap_binding_max_pct"]} %)')
    return reasons


def aggregate_days(by_day: Dict[str, dict]) -> dict:
    """metrics of one variant over its days -> {'days': [...], 'mean': {...}, 'min': {...}, 'max': {...},
    'sign_frac': {...}} ; sign_frac = share of days with the same sign as the mean (1.0 when all agree, 0 for 0)"""
    days = sorted(by_day)
    keys = set()
    for m in by_day.values():
        keys.update(k for k, v in m.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
    mean, mn, mx, sign = {}, {}, {}, {}
    for k in sorted(keys):
        vals = [by_day[d][k] for d in days if isinstance(by_day[d].get(k), (int, float)) and not isinstance(by_day[d].get(k), bool)]
        if not vals:
            continue
        mean[k] = statistics.fmean(vals)
        mn[k], mx[k] = min(vals), max(vals)
        if mean[k] == 0:
            sign[k] = 1.0 if all(v == 0 for v in vals) else 0.0
        else:
            s = 1 if mean[k] > 0 else -1
            sign[k] = sum(1 for v in vals if (v > 0) - (v < 0) == s) / len(vals)
    return {'days': days, 'n_days': len(days), 'mean': mean, 'min': mn, 'max': mx, 'sign_frac': sign}


# ----------------------------------------------------------------------------- H-202: Pareto, review order, scalar
def pareto_objectives(agg_mean: dict, question: str) -> Tuple[float, ...]:
    """the objectives of DESIGN.md 5.3 rule 3, every one to be MINIMISED:
    earlier: (-rise_vs_base_U, max(rest_vs_base_U, 0), lowside_flags)
    gentler: (rise_vs_base_U, |rest_vs_base_U|, max(zero_temp_vs_base, 0))"""
    rise = agg_mean.get('rise_vs_base_U', 0.0) or 0.0
    rest = agg_mean.get('rest_vs_base_U', 0.0) or 0.0
    if question == 'earlier':
        return (-rise, max(rest, 0.0), float(agg_mean.get('lowside_flags', 0.0) or 0.0))
    if question == 'gentler':
        return (rise, abs(rest), max(float(agg_mean.get('zero_temp_vs_base', 0.0) or 0.0), 0.0))
    raise ValueError(f'no Pareto objectives for question {question!r} (explore has no ranking)')


def dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


def pareto_front(points: Dict[str, Sequence[float]]) -> List[str]:
    """names of the non-dominated points (all objectives minimised), in input order"""
    names = list(points)
    return [n for n in names if not any(m != n and dominates(points[m], points[n]) for m in names)]


def scalar_objective(agg_mean: dict, question: str, weights: Optional[dict] = None) -> float:
    """the TPE scalarisation (larger = preferred), weights from study.json and shown on the page:
    earlier: rise_vs_base_U - w_rest x max(rest_vs_base_U, 0) - w_lowside x lowside_flags
    gentler: -rise_vs_base_U - w_rest x |rest_vs_base_U| - w_zero_temp x max(zero_temp_vs_base, 0)"""
    w = dict(DEFAULT_OBJECTIVE['scalar_weights'])
    w.update(weights or {})
    rise = agg_mean.get('rise_vs_base_U', 0.0) or 0.0
    rest = agg_mean.get('rest_vs_base_U', 0.0) or 0.0
    if question == 'earlier':
        return rise - w['rest'] * max(rest, 0.0) - w['lowside'] * float(agg_mean.get('lowside_flags', 0.0) or 0.0)
    if question == 'gentler':
        return -rise - w['rest'] * abs(rest) - w['zero_temp'] * max(float(agg_mean.get('zero_temp_vs_base', 0.0) or 0.0), 0.0)
    raise ValueError(f'no scalar for question {question!r} (explore has no ranking)')


# ----------------------------------------------------------------------------- H-202: the ranking
@dataclass
class VariantSummary:
    variant: str
    params: dict
    days: List[str]
    feasible: bool
    infeasible_reasons: Dict[str, List[str]]          # day -> reasons
    tie_with_base: bool                               # on every day
    tie_days: Dict[str, bool]
    review_score: float                               # mean over days of |effect| / floor on the larger component
    review_component: str
    mean: dict                                        # aggregate_days()['mean']
    sign_frac: dict
    pareto: bool = False
    scalar: Optional[float] = None
    rank: Optional[int] = None                        # 1-based position in `order`; None when infeasible
    recommended_for_review: bool = False
    recommend_reasons: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    missing_days: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class Ranking:
    question: str
    objective: dict
    base_variant: str
    days: List[str]
    floors: Dict[str, dict]                            # day -> floors_from_base()
    variants: Dict[str, VariantSummary]
    order: List[str]                                   # feasible variants in review order / ranked order; base excluded
    infeasible: List[str]
    front: List[str]
    rule_text: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        return d


def _rows_by_variant_day(rows: Iterable[dict], base_variant: str, days: Optional[Sequence[str]]) -> Tuple[Dict[str, Dict[str, dict]], Dict[str, dict]]:
    """trials.jsonl-like rows (variant, day, metrics[, status, params]) -> {variant: {day: metrics}}, {variant: params}"""
    table: Dict[str, Dict[str, dict]] = {}
    params: Dict[str, dict] = {}
    for r in rows:
        if r.get('status', 'done') != 'done':
            continue
        if days is not None and r['day'] not in days:
            continue
        table.setdefault(r['variant'], {})[r['day']] = r['metrics']
        params.setdefault(r['variant'], r.get('params', {}))
    return table, params


def rank_variants(rows: Iterable[dict], question: str = 'explore', objective: Optional[dict] = None,
                  days: Optional[Sequence[str]] = None, base_variant: str = BASE_VARIANT) -> Ranking:
    """DESIGN.md 5.3 on trials.jsonl-like rows: {variant, day, metrics, [params], [status]} (status != done ignored).
    `days` restricts the evaluation (search days; hold-out days are ranked by a second call).
    explore -> review order (|effect| / floor, descending), earlier/gentler -> Pareto front first (by scalar inside the
    front), then the rest by scalar.  Infeasible variants are never in `order`; variants without a base row on a day
    are infeasible on that day ("no base"); the recommended_for_review flag needs leave_one_day_out() and the hold-out
    ranking and is set by recommend()."""
    if question not in QUESTIONS:
        raise ValueError(f'question must be one of {QUESTIONS}, not {question!r}')
    cfg = _objective(objective)
    table, params = _rows_by_variant_day(rows, base_variant, days)
    base_days = table.get(base_variant, {})
    all_days = sorted(set(d for v in table.values() for d in v)) if days is None else sorted(days)
    floors = {d: floors_from_base(base_days[d]) for d in all_days if d in base_days}
    warnings = [f'no base trial on {d}: every variant is infeasible there' for d in all_days if d not in base_days]
    variants: Dict[str, VariantSummary] = {}
    for name, by_day in table.items():
        if name == base_variant:
            continue
        reasons: Dict[str, List[str]] = {}
        ties: Dict[str, bool] = {}
        scores: List[float] = []
        comps: List[str] = []
        vflags = set()
        for d in all_days:
            m = by_day.get(d)
            if m is None:
                reasons[d] = ['not replayed on this day']
                continue
            vflags.update(m.get('flags', []))
            if d not in floors:
                reasons[d] = ['no base replay on this day']
                continue
            r = feasibility_day(m, base_days[d], floors[d], cfg)
            if r:
                reasons[d] = r
            ties[d] = tie_with_base(m, floors[d])
            sc, comp = effect_over_floor(m, floors[d])
            scores.append(sc)
            comps.append(comp)
        agg = aggregate_days({d: m for d, m in by_day.items() if d in all_days})
        comp = max(set(comps), key=comps.count) if comps else REVIEW_COMPONENTS[0]
        variants[name] = VariantSummary(
            variant=name, params=params.get(name, {}), days=agg['days'], feasible=not reasons,
            infeasible_reasons=reasons, tie_with_base=bool(ties) and all(ties.values()), tie_days=ties,
            review_score=statistics.fmean(scores) if scores else 0.0, review_component=comp,
            mean=agg['mean'], sign_frac=agg['sign_frac'], flags=sorted(vflags),
            missing_days=[d for d in all_days if d not in by_day])
    feasible = [n for n, v in variants.items() if v.feasible]
    infeasible = [n for n, v in variants.items() if not v.feasible]
    front: List[str] = []
    if question == 'explore':
        order = sorted(feasible, key=lambda n: (-variants[n].review_score, n))
    else:
        pts = {n: pareto_objectives(variants[n].mean, question) for n in feasible}
        front = pareto_front(pts)
        for n in feasible:
            variants[n].scalar = scalar_objective(variants[n].mean, question, cfg['scalar_weights'])
            variants[n].pareto = n in front
        order = sorted(feasible, key=lambda n: (0 if n in front else 1, -(variants[n].scalar or 0.0), n))
    for i, n in enumerate(order, 1):
        variants[n].rank = i
    return Ranking(question=question, objective=cfg, base_variant=base_variant, days=all_days, floors=floors,
                   variants=variants, order=order, infeasible=infeasible, front=front,
                   rule_text=ranking_rule_text(question, cfg, floors), warnings=warnings)


def leave_one_day_out(rows: Sequence[dict], question: str = 'explore', objective: Optional[dict] = None,
                      days: Optional[Sequence[str]] = None, base_variant: str = BASE_VARIANT) -> Dict[str, List[str]]:
    """{left-out day: order} - the ranking recomputed with that day removed; one fold per day (needs >= 2 days)"""
    rows = list(rows)
    all_days = sorted(set(r['day'] for r in rows)) if days is None else sorted(days)
    folds = {}
    if len(all_days) < 2:
        return folds
    for d in all_days:
        keep = [x for x in all_days if x != d]
        folds[d] = rank_variants(rows, question, objective, keep, base_variant).order
    return folds


def recommend(ranking: Ranking, folds: Dict[str, List[str]], holdout_order: Optional[Sequence[str]] = None,
              objective: Optional[dict] = None) -> Dict[str, Tuple[bool, List[str]]]:
    """DESIGN.md 5.3 rule 5 for every variant of `ranking`: recommended for review only if feasible on all days,
    top-3 on >= 80 % of the leave-one-day-out folds, same sign of the primary effect on >= 80 % of days, and rank on
    the hold-out days within the top 5 (holdout_order None = no hold-out days reserved -> that condition fails,
    with the reason named).  Sets `recommended_for_review` / `recommend_reasons` on the summaries and returns them."""
    cfg = _objective(objective or ranking.objective)['recommend']
    out = {}
    for name, v in ranking.variants.items():
        reasons = []
        if not v.feasible:
            reasons.append('not feasible on every day')
        if folds:
            share = sum(1 for order in folds.values() if name in order[:cfg['fold_top']]) / len(folds)
            if share < cfg['fold_share'] - 1e-9:
                reasons.append(f'top-{cfg["fold_top"]} on {share:.0%} of the leave-one-day-out folds (needs {cfg["fold_share"]:.0%})')
        else:
            reasons.append('fewer than two days: no leave-one-day-out folds')
        primary = v.review_component if ranking.question == 'explore' else 'rise_vs_base_U'
        sign = v.sign_frac.get(primary, 0.0)
        if len(v.days) < 2:
            reasons.append('fewer than two days: sign stability not testable')
        elif sign < cfg['sign_share'] - 1e-9:
            reasons.append(f'same sign of {primary} on {sign:.0%} of days (needs {cfg["sign_share"]:.0%})')
        if holdout_order is None:
            reasons.append('no hold-out days')
        elif name not in list(holdout_order)[:cfg['holdout_top']]:
            reasons.append(f'not in the top {cfg["holdout_top"]} on the hold-out days')
        v.recommended_for_review = not reasons
        v.recommend_reasons = reasons
        out[name] = (not reasons, reasons)
    return out


def _fmt_floor(v):
    return f'{v:.2f}'


def ranking_rule_text(question: str = 'explore', objective: Optional[dict] = None, floors: Optional[Dict[str, dict]] = None) -> str:
    """the ranking rule as printed next to the leaderboard (DESIGN.md 5.3; vocabulary of section 3)"""
    cfg = _objective(objective)
    env, feas, w = cfg['envelope'], cfg['feasible'], cfg['scalar_weights']
    lines = []
    if question == 'explore':
        lines.append('Review order, not a ranking by outcome.')
        lines.append('Question "explore": nothing is ranked. Variants are listed by |effect| / floor, descending, where the'
                     ' effect is the insulin asked for vs base (rise or rest, whichever is larger against its floor).')
    elif question == 'earlier':
        lines.append('Question "earlier" (declared study setting): the Pareto front of maximise rise_vs_base_U,'
                     ' minimise max(rest_vs_base_U, 0), minimise lowside_flags comes first; inside the front and after'
                     f' it the order follows the scalar rise - {w["rest"]} x max(rest, 0) - {w["lowside"]} x lowside_flags.')
    else:
        lines.append('Question "gentler" (declared study setting): the Pareto front of minimise rise_vs_base_U,'
                     ' minimise |rest_vs_base_U|, minimise the increase of zero temps vs base comes first; inside the'
                     f' front and after it the order follows the scalar -rise - {w["rest"]} x |rest| - {w["zero_temp"]} x zero_temp'
                     ' increase.')
    if question != 'explore':
        lines.append('The weights are a study setting shown here, not a finding; the weights-sensitivity panel shows the'
                     ' order under +/-50 % of each weight.')
    lines.append(f'Feasible = no more insulin than base plus the floor in the two hours before a recorded low on every day,'
                 f' inside the envelope on every day (at most {env["max_3h_U"]} U vs base in any 3 h window and'
                 f' {env["max_day_pct_tdd"]} % of the phone\'s TDD per day), no emulated SMB above the phone\'s maxBolus,'
                 f' factor on a cap in fewer than {feas["cap_binding_max_pct"]} % of loops, loop count equal to base.'
                 ' Infeasible variants are shown greyed with the reason, never ranked.')
    lines.append(f'Floor per day and component = |base - phone|; a difference smaller than {TIE_FACTOR:g} x floor is a tie'
                 ' (grey). Variants inside the floor on every component are indistinguishable from base within the noise'
                 ' floor; parameters with every point inside the floor on all days are not testable on these days.')
    lines.append('Recommended for review only if feasible on all days, top-3 on at least 80 % of the leave-one-day-out'
                 ' folds, the same sign of the primary effect on at least 80 % of days, and within the top 5 on the'
                 ' hold-out days. Settings are for you and your care team to review; nothing here changes the phone.')
    if floors:
        parts = []
        for d in sorted(floors):
            f = floors[d]
            parts.append(f'{d}: rise {_fmt_floor(f["floor_rise_U"])} U, rest {_fmt_floor(f["floor_rest_U"])} U,'
                         f' total {_fmt_floor(f["floor_total_U"])} U, {int(f["floor_loops"])} loops changed by the replay'
                         + (f', base agreement {f["agreement_pct"]:.1f} %' if f.get('agreement_pct') is not None else ''))
        lines.append('Floors - ' + '; '.join(parts) + '.')
    return '\n'.join(lines)


def leaderboard_rows(ranking: Ranking) -> List[dict]:
    """flat rows for a table or round.json: one per variant (base excluded), ranked first, then infeasible, with the
    verdict word (recommended / candidate / tie / infeasible) and a `why` string"""
    out = []
    for name in ranking.order + ranking.infeasible:
        v = ranking.variants[name]
        if not v.feasible:
            verdict, why = 'infeasible', '; '.join(f'{d}: {", ".join(r)}' for d, r in sorted(v.infeasible_reasons.items()))
        elif v.recommended_for_review:
            verdict, why = 'recommended', 'feasible on all days, stable across folds, days and hold-out'
        elif v.tie_with_base:
            verdict, why = 'tie', 'indistinguishable from base within the noise floor on every day'
        else:
            verdict, why = 'candidate', (f'{v.review_component} {v.mean.get(v.review_component, 0.0):+.2f} U vs base,'
                                         f' {v.review_score:.1f} x floor')
        out.append({'variant': name, 'rank': v.rank, 'params': v.params, 'days': v.days, 'verdict': verdict, 'why': why,
                    'pareto': v.pareto, 'scalar': v.scalar, 'review_score': v.review_score,
                    'review_component': v.review_component, 'feasible': v.feasible,
                    'recommended_for_review': v.recommended_for_review, 'tie_with_base': v.tie_with_base,
                    'mean': {k: v.mean.get(k) for k in ('rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U',
                                                        'changed_vs_base', 'lowside_flags', 'prehypo_extra_U',
                                                        'env_3h_max_U', 'env_day_pct_tdd', 'zero_temp_vs_base',
                                                        'cap_binding_pct', 'max_single_smb_U')},
                    'flags': v.flags})
    return out


__all__ = ['OBJECTIVE_VERSION', 'LoopSeries', 'trial_metrics', 'metrics_from_csv', 'read_result_csv', 'effective_cap',
           'loop_intervals_h', 'hindsight_covariates', 'prehypo_extra_U', 'envelope', 'changed_flags', 'agreement',
           'min_guard_from_levels', 'min_guard_from_reason', 'floors_from_base', 'effective_floor', 'is_tie',
           'tie_components', 'tie_with_base', 'effect_over_floor', 'feasibility_day', 'aggregate_days',
           'pareto_objectives', 'pareto_front', 'dominates', 'scalar_objective', 'rank_variants', 'leave_one_day_out',
           'recommend', 'ranking_rule_text', 'leaderboard_rows', 'Ranking', 'VariantSummary', 'DEFAULT_OBJECTIVE',
           'FLOOR_COMPONENTS', 'REVIEW_COMPONENTS', 'TIE_FACTOR', 'BASE_VARIANT', 'QUESTIONS']
