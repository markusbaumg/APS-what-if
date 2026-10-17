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
    validate_variants(...)     the validate phase (H-308): hold-out rank, per-day direction, leave-one-day-out rank
                               range over search + hold-out days, Kendall tau, and the rule that decides what is
                               recommended for review once a study has hold-out rows of its validated top-N.

Change counts (per (variant, day); a loop "changes" when SMB, insulinReq (0.05 U) or the rate (0.05 U/h) differ,
`changed_flags()`); the two *_vs_base counts are different quantities and both names are part of the contract:
    changed_loops              loops where this replay's emulated decision differs from the phone's.
    changed_loops_base         the same count for the base replay of the day (its value is the floor `floor_loops`).
    changed_vs_base            changed_loops - changed_loops_base: a NET difference of two "differs from the phone"
                               counts, signed, 0 for base. It is the effect component with the floor `floor_loops`
                               (FLOOR_COMPONENTS), aggregated into the variant mean and the candidate `effect`, and
                               what a card prints as "Loops differing from the phone: +N per day vs base". A variant
                               that moves 10 loops while restoring 10 others to the phone's decision has 0 here.
    changed_vs_base_loops      loops where the variant's emulated decision differs from the base replay's emulated
                               decision (changed_flags(s, base)), unsigned, 0 for base: the per-loop size of the
                               variant's footprint. Stored in the trial metrics only; no ranking rule, round, card or
                               page reads it (the fixture-study tests use it as the no-state-leak check).

Units: insulin in U, rates in U/h, glucose in mg/dl inside this module (the CSV is mg/dl); the thresholds
are defined in mmol/l where the design states them and converted once (MMOL_TO_MGDL = 18.0, as ns_profile).

Give per loop = SMB + rate-in-force x dt with dt capped at 30 min (the optimizer's convention, reused through
`loop_intervals_h()`).  The CSV's TBR columns are "rate in force" (a new temp, else the running one), so a 0
means "no temp or a zero temp"; `zero_temp_vs_base` therefore prefers the explicit per-loop `zero_temp`
input and falls back to the rate columns with the flag `zero_temp_rate_proxy`.

OBJECTIVE_VERSION is part of every trial key (contracts.key()); bump it when a metric definition or a
feasibility rule changes, so that old trials.jsonl rows are not compared with new ones.
METRICS_VERSION is written into every new trials.jsonl row (`metrics_version`, missing = 1). It is NOT part of the
key: a done row whose metrics_version differs from the current one is stale exactly like a code_sha mismatch
(study.find_stale), so `hc study run --on-stale reeval` re-replays it under the same key and `--on-stale stop` names
the count and stops. Bump it when the metrics a replay writes change while the decisions do not.
    1  the original section 5.2 set
    2  pre-hypo constraint P: a recorded low episode whose first reading comes within
       objective.prehypo['exclude_after_manual_h'] hours (default 4.0, 0 = off) after a manual bolus is
       manual-attributed and opens no P window; new keys prehypo_lows, prehypo_lows_manual, prehypo_manual_U,
       prehypo_exclude_h

Pre-hypo rule (P, DESIGN.md 5.2). A low episode = consecutive loops with recorded BG < 3.9 mmol. Every episode marks
its 2 h before-window for shading (before_low_2h); only the episodes NOT within exclude_after_manual_h after a
manual bolus open a window for P (prehypo_extra_U, prehypo_extra_vs_phone_U, prehypo_loops). The reason: a low that
follows a bolus the user entered is not a test of what the loop asked for, and charging a variant for it made
every step toward more insulin on rises infeasible on those days. Loop-only lows keep the rule as before.
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
METRICS_VERSION = 2                                    # see the module docstring; trials.jsonl `metrics_version`

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
PREHYPO_EXCLUDE_AFTER_MANUAL_H = 4.0                   # P: a low starting this long after a manual bolus is not counted
#REHYPO_EXCLUDE_AFTER_MANUAL_H = 3.0                   # 2026-10-17 morning value; 4 h covers the Oct 10 low 3 h 06 after a 3 U bolus
LOW_FOLLOW_S = PREHYPO_S                               # covariate: a low within 2 h
HIGH_FOLLOW_S = 180 * 60                               # covariate: a high within 3 h
ENVELOPE_WINDOW_S = 3 * 3600                           # E: sliding 3 h window
FALL_LOOKBACK_S = 300                                  # "falling" = bg minus the bg 5 min earlier < 0 (the optimizer's delta)
FLOOR_MIN_U = 0.05                                     # a floor below one bolus increment is treated as one increment
FLOOR_MIN_LOOPS = 1
TIE_FACTOR = 2.0                                       # |difference| < TIE_FACTOR x floor = tie (5.1, 5.3 rule 4)
BASE_VARIANT = 'base'
QUESTIONS = ('explore', 'earlier', 'gentler')
MANUAL_WINDOW_S = 60 * 60                              # manual-bolus coverage: the SMBs of the hour before the bolus
PREEMPT_SHARE = 0.5                                    # a bolus counts as pre-empted when coverage >= 0.5 x its units
# the per-day manual-bolus keys of trial_metrics (always present, 0 on a day without a manual bolus); docstring of
# manual_bolus_coverage. Decision level: what the loop would have asked before the user acted, never a glucose claim.
MANUAL_KEYS = ('manual_boluses', 'manual_U', 'manual_presmb_U', 'manual_presmb_base_U', 'coverage_U_vs_base',
               'boluses_preempted')
SENSITIVITY_FACTORS = (0.5, 1.5)                       # DESIGN.md 5.3 rule 3: the order under +/-50 % of each weight
SENSITIVITY_TOP = 5
QUESTION_WEIGHTS = {'earlier': ('rest', 'lowside'), 'gentler': ('rest', 'zero_temp')}

DEFAULT_OBJECTIVE = {
    'scalar_weights': {'rest': 2.0, 'lowside': 0.3, 'zero_temp': 0.3},
    'envelope': {'max_3h_U': 1.0, 'max_day_pct_tdd': 15.0},
    'feasible': {'cap_binding_max_pct': 50.0},
    'recommend': {'fold_top': 3, 'fold_share': 0.8, 'sign_share': 0.8, 'holdout_top': 5},
    'prehypo': {'exclude_after_manual_h': PREHYPO_EXCLUDE_AFTER_MANUAL_H},
}
PREHYPO_KEYS = ('prehypo_lows', 'prehypo_lows_manual', 'prehypo_manual_U')    # per-day keys of the pre-hypo rule

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


def low_episodes(t: Sequence[float], bg: Sequence[float], low: float = LOW_BG_MGDL) -> List[Tuple[int, int]]:
    """(first index, last index) of every recorded low episode: consecutive loops with BG < 3.9 mmol are one episode"""
    out: List[Tuple[int, int]] = []
    prev = False
    for i, b in enumerate(bg):
        is_low = b < low
        if is_low and not prev:
            out.append((i, i))
        elif is_low:
            out[-1] = (out[-1][0], i)
        prev = is_low
    return out


def prehypo_exclude_hours(objective: Optional[dict] = None) -> float:
    """objective['prehypo']['exclude_after_manual_h'] (default PREHYPO_EXCLUDE_AFTER_MANUAL_H; 0 disables)"""
    v = ((objective or {}).get('prehypo') or {}).get('exclude_after_manual_h', PREHYPO_EXCLUDE_AFTER_MANUAL_H)
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return PREHYPO_EXCLUDE_AFTER_MANUAL_H


def manual_attributed_lows(t: Sequence[float], bg: Sequence[float],
                           manual_boluses: Optional[Sequence[Tuple[float, float]]],
                           exclude_after_manual_h: Optional[float] = None) -> dict:
    """The pre-hypo rule's split of the recorded low episodes (module docstring):
    episodes       [(first, last)] index pairs (low_episodes)
    manual         [bool] per episode: its first reading comes within exclude_after_manual_h hours after a manual
                   bolus (bolus time <= first reading); False everywhere when the hours are 0 or the boluses unknown
    manual_U       the units of the manual boluses in the hours before the manual-attributed episodes (each bolus once)
    known          False when manual_boluses is None (not known: nothing is excluded)
    hours          the window used"""
    hours = PREHYPO_EXCLUDE_AFTER_MANUAL_H if exclude_after_manual_h is None else max(0.0, float(exclude_after_manual_h))
    eps = low_episodes(t, bg)
    known = manual_boluses is not None
    bol = sorted((float(a), float(b)) for a, b in (manual_boluses or []) if b and b > 0)
    manual = [False] * len(eps)
    used = set()
    if known and hours > 0:
        span = hours * 3600.0
        for k, (i0, _i1) in enumerate(eps):
            t0 = t[i0]
            hit = [j for j, (tb, _u) in enumerate(bol) if tb <= t0 and t0 - tb <= span]
            if hit:
                manual[k] = True
                used.update(hit)
    return {'episodes': eps, 'manual': manual, 'manual_U': sum(bol[j][1] for j in sorted(used)), 'known': known,
            'hours': hours}


def _before_mask(t: Sequence[float], times: Sequence[float], horizon_s: float) -> List[int]:
    """1 for the loops followed by one of `times` (sorted) within horizon_s, strictly after the loop"""
    n = len(t)
    out = [0] * n
    j = 0
    for i in range(n):
        while j < len(times) and times[j] <= t[i]:
            j += 1
        if j < len(times) and times[j] - t[i] <= horizon_s:
            out[i] = 1
    return out


def hindsight_covariates(t: Sequence[float], bg: Sequence[float],
                         manual_boluses: Optional[Sequence[Tuple[float, float]]] = None,
                         exclude_after_manual_h: Optional[float] = None) -> Dict[str, List[int]]:
    """positional covariates per loop, 0/1 - stored in the series for shading and filtering, never scored:
    before_low_2h         a recorded BG < 3.9 mmol follows within 2 h (every low, the drill-down shading)
    low_within_2h         alias of before_low_2h (the drill-down badge "a low followed")
    before_manual_low_2h  a reading of a manual-attributed low episode follows within 2 h (not counted for P)
    prehypo_window        a reading of a counted (loop-only) low episode follows within 2 h: the P window
    high_within_3h        a recorded BG > 180 mg/dl follows within 3 h
    Without manual_boluses every low is counted (prehypo_window == before_low_2h)."""
    split = manual_attributed_lows(t, bg, manual_boluses, exclude_after_manual_h)
    lows = _recorded_lows(t, bg)
    counted, manual = [], []
    for (i0, i1), is_manual in zip(split['episodes'], split['manual']):
        (manual if is_manual else counted).extend(t[i0:i1 + 1])
    highs = [ti for ti, b in zip(t, bg) if b > HIGH_BG_MGDL]
    before_low = _before_mask(t, lows, LOW_FOLLOW_S)
    return {'before_low_2h': before_low, 'low_within_2h': list(before_low),
            'before_manual_low_2h': _before_mask(t, manual, LOW_FOLLOW_S),
            'prehypo_window': _before_mask(t, counted, PREHYPO_S),
            'high_within_3h': _before_mask(t, highs, HIGH_FOLLOW_S)}


def prehypo_extra_U(t: Sequence[float], bg: Sequence[float], give: Sequence[float], give_ref: Sequence[float],
                    manual_boluses: Optional[Sequence[Tuple[float, float]]] = None,
                    exclude_after_manual_h: Optional[float] = None) -> float:
    """P: sum over the loops in the 120 min before a counted recorded BG < 3.9 mmol of max(give - give_ref, 0).
    'Any additional insulin before a low that actually happened' - the low itself is positional, never scored.
    A low episode starting within exclude_after_manual_h after a manual bolus is not counted (module docstring)."""
    mask = hindsight_covariates(t, bg, manual_boluses, exclude_after_manual_h)['prehypo_window']
    return sum(max(a - b, 0.0) for a, b, m in zip(give, give_ref, mask) if m)


def prehypo_rule_phrase(hours: Optional[float] = None) -> str:
    """'lows within 3 h after a manual bolus are not counted' (the pre-hypo rule as every surface prints it)"""
    h = PREHYPO_EXCLUDE_AFTER_MANUAL_H if hours is None else float(hours)
    if h <= 0:
        return 'every recorded low counts, also one after a manual bolus'
    return f'lows within {h:g} h after a manual bolus are not counted'


def prehypo_counts(per_day: Dict[str, dict], days: Optional[Iterable[str]] = None) -> dict:
    """The low episodes of the days from their BASE metrics (day -> metrics): lows (episodes), manual (excluded),
    manual_days {day: excluded episodes} (only days with any), counted_days, unknown [days whose row has no
    prehypo_lows / prehypo_lows_manual: metrics_version 1 or no manual-bolus cache]"""
    out = {'lows': 0, 'manual': 0, 'manual_days': {}, 'unknown': [], 'days': 0}
    for d in (sorted(per_day) if days is None else days):
        m = per_day.get(d)
        if not m:
            continue
        out['days'] += 1
        lows, man = m.get('prehypo_lows'), m.get('prehypo_lows_manual')
        if not isinstance(lows, (int, float)) or isinstance(lows, bool):
            out['unknown'].append(d)
            continue
        out['lows'] += int(lows)
        if isinstance(man, (int, float)) and not isinstance(man, bool):
            out['manual'] += int(man)
            if man:
                out['manual_days'][d] = int(man)
        else:
            out['unknown'].append(d)
    return out


def prehypo_label(counts: Optional[dict] = None, hours: Optional[float] = None) -> str:
    """'P (insulin before a recorded low; lows within 3 h after a manual bolus are not counted: 2 of 9 recorded low
    episodes on these days)' - the head of every P line (cards, page). Counting the recorded episodes is positional:
    the same for every variant of a day, never an outcome."""
    head = f'P (insulin before a recorded low; {prehypo_rule_phrase(hours)}'
    h = PREHYPO_EXCLUDE_AFTER_MANUAL_H if hours is None else float(hours)
    if counts is None or h <= 0:
        return head + ')'
    if counts['unknown'] and not counts['lows']:
        return head + ': not yet counted on these days, re-evaluate the rows)'
    tail = f': {counts["manual"]} of {counts["lows"]} recorded low episodes on these days'
    if counts['unknown']:
        tail += f', {len(counts["unknown"])} day(s) not counted yet'
    return head + tail + ')'


def prehypo_note(counts: dict, hours: Optional[float] = None, where: str = 'the search days') -> str:
    """The round note: 'pre-hypo constraint P: lows within 3 h after a manual bolus are not counted; 2 of 9 recorded
    low episodes on the search days excluded (2026-10-04: 2)'."""
    h = PREHYPO_EXCLUDE_AFTER_MANUAL_H if hours is None else float(hours)
    text = f'pre-hypo constraint P: {prehypo_rule_phrase(h)}'
    if h <= 0:
        return text
    if counts['unknown'] and not counts['lows']:
        return text + f'; the low episodes of {where} are not counted yet (rows of metrics version 1: re-evaluate)'
    text += f'; {counts["manual"]} of {counts["lows"]} recorded low episodes on {where} excluded'
    if counts['manual_days']:
        text += ' (' + ', '.join(f'{d}: {n}' for d, n in sorted(counts['manual_days'].items())) + ')'
    else:
        text += ' (no day)'
    if counts['unknown']:
        text += f'; not counted yet on {", ".join(counts["unknown"])}'
    return text


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


# ----------------------------------------------------------------------------- manual-bolus coverage (GOAL.md)
def manual_bolus_coverage(t: Sequence[float], smb_variant: Sequence[float], smb_ref: Sequence[float],
                          boluses: Optional[Sequence[Tuple[float, float]]],
                          window_s: float = MANUAL_WINDOW_S) -> Tuple[dict, List[dict]]:
    """Decision-level coverage of the manual boluses of one (variant, day) (docs/hindcast/GOAL.md, "no manual
    bolusing"). `boluses` = [(unix seconds, units)] of the boluses the user entered (Nightscout type NORMAL, eventType
    Meal Bolus / Correction Bolus; replay.manual_boluses reads them from the cache), `smb_variant` / `smb_ref` the
    emulated SMB per loop of the variant and of the base replay of the day.

    Per bolus at t with U units: pre_U = the emulated SMB units of the loops in [t - 60 min, t) - the window starts no
    earlier than the previous manual bolus, so SMBs between two close boluses are credited to the later one only and
    never twice -, coverage_U = pre_U(variant) - pre_U(base), covered_pct = min(100, coverage_U / U x 100).
    Per day: manual_boluses (count), manual_U, manual_presmb_U / manual_presmb_base_U (sums of pre_U), coverage_U_vs_base
    (sum), boluses_preempted (boluses with coverage_U >= PREEMPT_SHARE x U). All 0 without a manual bolus.

    The honest limit: this is what the loop would have asked before the user acted, on the recorded glucose. A bolus
    the loop pre-empts changes the glucose that follows, and with it the later decisions and the bolus itself, so the
    coverage is a direction, not a prediction."""
    n = len(t)
    if len(smb_variant) != n or len(smb_ref) != n:
        raise ValueError('manual_bolus_coverage: per-loop lists differ in length from t')
    out = {k: 0 for k in MANUAL_KEYS}
    for k in ('manual_U', 'manual_presmb_U', 'manual_presmb_base_U', 'coverage_U_vs_base'):
        out[k] = 0.0
    detail: List[dict] = []
    prev = None
    t0 = t[0] if n else None
    for tb, units in sorted((float(a), float(b)) for a, b in (boluses or []) if b and b > 0):
        if t0 is not None and tb < t0:                      # a look-back bolus before the first loop (P rule only)
            continue
        lo = tb - window_s if prev is None else max(tb - window_s, prev)
        prev = tb
        pre_v = sum(v for ti, v in zip(t, smb_variant) if lo <= ti < tb)
        pre_b = sum(v for ti, v in zip(t, smb_ref) if lo <= ti < tb)
        cov = pre_v - pre_b
        out['manual_boluses'] += 1
        out['manual_U'] += units
        out['manual_presmb_U'] += pre_v
        out['manual_presmb_base_U'] += pre_b
        out['coverage_U_vs_base'] += cov
        if cov >= PREEMPT_SHARE * units - 1e-9:
            out['boluses_preempted'] += 1
        detail.append({'t': tb, 'U': round(units, 3), 'presmb_U': round(pre_v, 3), 'presmb_base_U': round(pre_b, 3),
                       'coverage_U': round(cov, 3), 'covered_pct': round(min(100.0, 100.0 * cov / units), 1)})
    return out, detail


# ----------------------------------------------------------------------------- per-trial metrics
def trial_metrics(csv_path, base=None, autoisf_max=None, autoisf_min=None, min_guard=None, zero_temp=None,
                  smb_off=None, tdd_phone_U=None, series: Optional[LoopSeries] = None,
                  manual_boluses: Optional[Sequence[Tuple[float, float]]] = None,
                  prehypo_exclude_h: Optional[float] = None) -> dict:
    """One metrics dict per (variant, day) = emulator_optimize.metrics_from_csv(csv_path, caps) + the section 5.2
    additions.  `base` is the base replay of the same day (LoopSeries or CSV path); None means "this trial IS the
    base", then every *_vs_base metric is 0 and the vs-phone values are what floors_from_base() reads.
    `autoisf_max` / `autoisf_min`: number or cap_at(ms) from emulator_optimize.effective_cap (profile_history aware).
    `min_guard`, `zero_temp`, `smb_off`: optional per-loop lists aligned to the CSV rows (see LoopSeries).
    `tdd_phone_U`: the phone's TDD of the day for env_day_pct_tdd; None -> the window's total_orig_U with the flag
    `tdd_from_window`.  `series`: pass a ready LoopSeries instead of reading csv_path again (csv_path may then be None
    only if the optimizer metrics are not wanted - they need the file).
    `manual_boluses`: [(unix s, U)] of the day's manual boluses (replay.manual_boluses) for the MANUAL_KEYS of
    manual_bolus_coverage() (the per-bolus list stays out of the row: trials.jsonl lines are < 4 KB). An empty list =
    known, none given: every key 0, so every day of a study carries them. None = not known (no cache): the keys are
    left out, never a 0 that would read as "no manual bolus" (replay.py flags the row `manual_boluses_unknown`).
    `prehypo_exclude_h`: the pre-hypo rule's hours (objective.prehypo.exclude_after_manual_h; None = 4.0, 0 = off):
    a low episode starting that long after one of `manual_boluses` opens no P window. With manual_boluses None
    nothing is excluded, the flag `prehypo_manual_unknown` says so and prehypo_lows_manual / prehypo_manual_U are
    None.

    Flags (list of strings, always present): `lowside_no_pred` (no emulated minGuardBG on any loop: the recorded-BG rule
    alone), `lowside_pred_partial` (some loops), `zero_temp_rate_proxy`, `tt_smb_off_unknown`, `tdd_from_window`,
    `cap_unknown` (no autoISF_max/min cap given: cap_binding_pct is None), `maxbolus_unknown` (the phone echoed no
    maxBolus on any loop), `prehypo_manual_unknown` (see prehypo_exclude_h)."""
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
    # the change counts, see "Change counts" in the module docstring: changed_vs_base is the signed net difference
    # of the two vs-phone counts, changed_vs_base_loops the unsigned number of loops where variant and base differ
    m['changed_loops'] = sum(changed_phone)                                         # variant vs phone
    m['changed_loops_base'] = sum(changed_flags(ref))                               # base vs phone
    m['changed_vs_base'] = m['changed_loops'] - m['changed_loops_base']             # net, signed (floor_loops)
    m['changed_vs_base_loops'] = 0 if is_base else sum(changed_flags(s, ref))     # per loop: variant vs base
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
    hours = PREHYPO_EXCLUDE_AFTER_MANUAL_H if prehypo_exclude_h is None else max(0.0, float(prehypo_exclude_h))
    cov_h = hindsight_covariates(s.t, s.bg, manual_boluses, hours)
    split = manual_attributed_lows(s.t, s.bg, manual_boluses, hours)
    window = cov_h['prehypo_window']
    m['prehypo_extra_U'] = sum(max(a - b, 0.0) for a, b, w in zip(give_e, give_b, window) if w)
    m['prehypo_extra_vs_phone_U'] = sum(max(a - b, 0.0) for a, b, w in zip(give_e, give_p, window) if w)
    m['prehypo_loops'] = sum(window)
    m['lows_recorded'] = len(_recorded_lows(s.t, s.bg))
    m['prehypo_lows'] = len(split['episodes'])
    m['prehypo_exclude_h'] = hours
    if split['known']:
        m['prehypo_lows_manual'] = sum(split['manual'])
        m['prehypo_manual_U'] = round(split['manual_U'], 3)
    else:
        m['prehypo_lows_manual'] = None
        m['prehypo_manual_U'] = None
        flags.append('prehypo_manual_unknown')
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
    # manual boluses: what the variant's SMBs in the hour before each one add to the base's (decision level)
    if manual_boluses is not None:
        cov, _detail = manual_bolus_coverage(s.t, s.smb_emul, ref.smb_emul, manual_boluses)
        m.update({k: (round(v, 3) if isinstance(v, float) else v) for k, v in cov.items()})   # trials.jsonl < 4 KB
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


def floor_multiple(per_day: Dict[str, dict], floors: Dict[str, dict], component: str,
                   days: Optional[Iterable[str]] = None) -> Optional[float]:
    """THE floor multiple of one component of one variant (every 'x floor' a verdict `why`, a card or a page prints
    for a component comes from here): |X_d| / effective floor of day d, averaged over the days that have both a
    metric and a floor (DESIGN.md 5.4 phase 1: mean |effect| / floor across days). `per_day` = day -> metrics,
    `floors` = day -> floors_from_base() of the scored (non-quarantined) days only; `days` restricts further.
    None when no day has both."""
    fkey = FLOOR_COMPONENTS[component]
    ratios = []
    for d in (sorted(per_day) if days is None else days):
        m, f = per_day.get(d), floors.get(d)
        if m is None or f is None:
            continue
        v = m.get(component)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        ratios.append(abs(v) / effective_floor(f.get(fkey), fkey))
    return statistics.fmean(ratios) if ratios else None


def floor_band(floors: Dict[str, dict], component: str, days: Optional[Iterable[str]] = None) -> Optional[Tuple[float, float]]:
    """(smallest, largest) effective floor of `component` over `days` (default: every day of `floors`), or None."""
    fkey = FLOOR_COMPONENTS[component]
    vals = [effective_floor(floors[d].get(fkey), fkey) for d in (sorted(floors) if days is None else days) if d in floors]
    return (min(vals), max(vals)) if vals else None


def tie_every_day(per_day: Dict[str, dict], floors: Dict[str, dict], component: str,
                  days: Optional[Iterable[str]] = None) -> Optional[bool]:
    """is the component inside TIE_FACTOR x that day's floor on every day with a metric and a floor (None: no day)"""
    fkey = FLOOR_COMPONENTS[component]
    seen = []
    for d in (sorted(per_day) if days is None else days):
        m, f = per_day.get(d), floors.get(d)
        if m is None or f is None or not isinstance(m.get(component), (int, float)):
            continue
        seen.append(is_tie(m[component], f.get(fkey), fkey))
    return all(seen) if seen else None


def study_floor(floors_by_day: Dict[str, dict], days: Optional[Iterable[str]] = None) -> Dict[str, float]:
    """The study-level floor (status.json `floor`, "worst day"): max |base - phone| per component and the smallest
    agreement_pct over `floors_by_day` (day -> floors_from_base()). Pass only the scored days: quarantined days
    are listed, not scored (DESIGN.md 5.4 phase 0), so they never widen the floor; `days` restricts further."""
    agg: Dict[str, float] = {}
    keep = None if days is None else set(days)
    for d, f in sorted(floors_by_day.items()):
        if keep is not None and d not in keep:
            continue
        for k, v in f.items():
            if v is None or k == 'loops':
                continue
            if k == 'agreement_pct':
                agg[k] = v if k not in agg else min(agg[k], v)
            else:
                agg[k] = v if k not in agg else max(agg[k], v)
    return agg


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
def share_pct(x, dp: Optional[int] = None) -> str:
    """A share 0..1 as the public sentences print it: '80 %' (a space before the sign). dp None = as configured
    (format g: 0.8 -> '80 %', 0.825 -> '82.5 %'), else rounded to `dp` decimals (measured shares: 0.833 -> '83 %')."""
    v = float(x) * 100.0
    return (f'{v:g}' if dp is None else f'{v:.{dp}f}') + ' %'


def recommend_config(objective: Optional[dict] = None) -> dict:
    """objective.recommend with the defaults filled in: {fold_top, fold_share, sign_share, holdout_top}. Also takes
    a dict that already is such a config (round.json's study-level validation block carries the four keys)."""
    src = objective or {}
    if 'recommend' in src or not any(k in src for k in DEFAULT_OBJECTIVE['recommend']):
        return dict(_objective(src)['recommend'])
    cfg = dict(DEFAULT_OBJECTIVE['recommend'])
    cfg.update({k: src[k] for k in cfg if src.get(k) is not None})
    return cfg


RULE5_TIE = 'A tie with base is never recommended for review.'


def recommend_rule_text(objective: Optional[dict] = None, validate: bool = True) -> str:
    """DESIGN.md 5.3 rule 5 as one clause, from the configured numbers (recommend_config): the ONE sentence builder
    behind ranking_rule_text, report.validation_lines, round.py's 'recommended' why and the cards, so no surface
    states another rule. validate: the H-308 rule (feasible on every search and hold-out day, the sign over the
    hold-out days); otherwise the H-202 rule of recommend() (feasible on all days, the sign over all days)."""
    rec = recommend_config(objective)
    if validate:
        return (f'feasible on every search and hold-out day, in the top {rec["fold_top"]} on at least'
                f' {share_pct(rec["fold_share"])} of the leave-one-day-out folds, the same sign of the primary effect'
                f' on at least {share_pct(rec["sign_share"])} of the hold-out days, and within the top'
                f' {rec["holdout_top"]} on the hold-out days')
    return (f'feasible on all days, top-{rec["fold_top"]} on at least {share_pct(rec["fold_share"])} of the'
            f' leave-one-day-out folds, the same sign of the primary effect on at least {share_pct(rec["sign_share"])}'
            f' of days, and within the top {rec["holdout_top"]} on the hold-out days')


def _objective(objective: Optional[dict]) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_OBJECTIVE.items()}
    for k, v in (objective or {}).items():
        if isinstance(v, dict) and k in cfg:
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


ENVELOPE_PREFIX = 'out of envelope'                     # the reason text of an envelope E breach (feasibility_split)


def feasibility_split(metrics: dict, base_metrics: dict, floors: dict, objective: Optional[dict] = None
                      ) -> Tuple[List[str], List[str]]:
    """(hard reasons, envelope reasons) of one trial on one day, DESIGN.md 5.3 rule 1. Hard: loop count equal to
    base, a failed replay, P <= P_base + floor_P, max_single_smb_U <= the phone's maxBolus, cap_binding_pct < 50 %.
    Envelope E (at most max_3h_U vs base in any 3 h window, at most max_day_pct_tdd of the phone's TDD): a VALIDITY
    envelope, not a safety rule - beyond it the replay cannot judge, so a variant that breaks only E on some days is
    'envelope-limited' (rank_variants), not infeasible. Reasons are short labels for the greyed leaderboard row."""
    cfg = _objective(objective)
    hard, env = [], []
    if metrics.get('loops') != base_metrics.get('loops'):
        hard.append(f'loop count {metrics.get("loops")} differs from base {base_metrics.get("loops")}')
    if 'error' in metrics or metrics.get('loops') in (None, 0):
        hard.append('failed replay')
        return hard, env
    p = metrics.get('prehypo_extra_U')
    if p is not None:
        p_base = base_metrics.get('prehypo_extra_U') or 0.0
        limit = p_base + effective_floor(floors.get('floor_P_U'))
        if p > limit + 1e-9:
            hours = metrics.get('prehypo_exclude_h', base_metrics.get('prehypo_exclude_h'))
            rule = (f'; {prehypo_rule_phrase(hours)}' if isinstance(hours, (int, float)) and hours > 0 else '')
            hard.append(f'asks for {p:.2f} U more than base in the two hours before a recorded low (limit {limit:.2f} U'
                        f'{rule})')
    env3 = metrics.get('env_3h_max_U')
    if env3 is not None and env3 > cfg['envelope']['max_3h_U'] + 1e-9:
        env.append(f'{ENVELOPE_PREFIX}: {env3:.2f} U vs base in a 3 h window (limit {cfg["envelope"]["max_3h_U"]} U)')
    envd = metrics.get('env_day_pct_tdd')
    if envd is not None and envd > cfg['envelope']['max_day_pct_tdd'] + 1e-9:
        env.append(f'{ENVELOPE_PREFIX}: {envd:.1f} % of the phone\'s TDD vs base (limit {cfg["envelope"]["max_day_pct_tdd"]} %)')
    over = metrics.get('smb_over_maxbolus_loops')
    if over:
        hard.append(f'emulated SMB above the phone\'s maxBolus in {over} loops')
    cap = metrics.get('cap_binding_pct')
    if cap is not None and cap >= cfg['feasible']['cap_binding_max_pct']:
        hard.append(f'factor on a cap in {cap:.1f} % of loops (not really tested; limit {cfg["feasible"]["cap_binding_max_pct"]} %)')
    return hard, env


def feasibility_day(metrics: dict, base_metrics: dict, floors: dict, objective: Optional[dict] = None) -> List[str]:
    """the infeasibility reasons of one trial on one day (empty = feasible), DESIGN.md 5.3 rule 1:
    P <= P_base + floor_P; inside the envelope E; max_single_smb_U <= the phone's maxBolus; cap_binding_pct < 50 %;
    loop count equal to base. feasibility_split() keeps the envelope reasons apart (envelope-limited)."""
    hard, env = feasibility_split(metrics, base_metrics, floors, objective)
    if 'failed replay' in hard:
        return hard
    head = [r for r in hard if r.startswith('loop count') or r.startswith('asks for')]
    return head + env + [r for r in hard if r not in head]


def envelope_day_record(metrics: dict, reasons: Sequence[str]) -> dict:
    """What an envelope-limited variant reports for one day beyond E: the signed sum of its largest 3 h window vs
    base (env_3h_signed_U, else env_3h_max_U), the share of the phone's TDD, the reason labels."""
    signed = metrics.get('env_3h_signed_U')
    if not isinstance(signed, (int, float)) or isinstance(signed, bool):
        signed = metrics.get('env_3h_max_U')
    pct = metrics.get('env_day_pct_tdd')
    return {'env_3h_U': None if signed is None else round(float(signed), 3),
            'env_day_pct_tdd': None if pct is None else round(float(pct), 1), 'reasons': list(reasons)}


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
    review_score: float                               # mean over days of the per-day max |effect| / floor (for the
                                                      # record only: no surface orders by it, H-715)
    review_component: str                             # the component with the largest floor multiple
    mean: dict                                        # aggregate_days()['mean']
    sign_frac: dict
    pareto: bool = False
    scalar: Optional[float] = None
    rank: Optional[int] = None                        # 1-based position in `order`; None when infeasible
    recommended_for_review: bool = False
    recommend_reasons: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    missing_days: List[str] = field(default_factory=list)
    floor_multiples: Dict[str, Optional[float]] = field(default_factory=dict)   # REVIEW_COMPONENTS -> floor_multiple()
    floor_multiple: float = 0.0                       # floor_multiples[review_component]: THE review order key (H-715)
    envelope_limited: bool = False                    # passes every hard check on every day, beyond E on some (not all)
    envelope_days: Dict[str, dict] = field(default_factory=dict)   # day beyond E -> envelope_day_record()
    step: Optional[bool] = None                       # earlier / gentler: rise_vs_base_U beyond the floor in the
                                                      # question's direction (None for explore)
    step_floor_U: Optional[float] = None              # the mean over the scored days of the effective floor_rise_U

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
    envelope_limited: List[str] = field(default_factory=list)      # subset of `infeasible`: only E broken, on some days
    no_step: List[str] = field(default_factory=list)               # earlier / gentler: feasible, not a step (a tie)

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


def review_component_of(floor_multiples: Dict[str, Optional[float]], day_components: Sequence[str] = ()) -> Tuple[str, float]:
    """(review component, its floor multiple) of one variant: the component of REVIEW_COMPONENTS with the largest
    floor_multiple() (DESIGN.md 5.3 rule 2: |effect| / floor on the largest-magnitude component; on equal multiples
    the first of REVIEW_COMPONENTS). Without any multiple: the component most days picked (effect_over_floor) and 0."""
    present = [(floor_multiples[k], -i, k) for i, k in enumerate(REVIEW_COMPONENTS)
               if isinstance(floor_multiples.get(k), (int, float)) and not isinstance(floor_multiples.get(k), bool)]
    if present:
        fm, _, comp = max(present)
        return comp, float(fm)
    comps = list(day_components)
    return (max(set(comps), key=comps.count) if comps else REVIEW_COMPONENTS[0]), 0.0


def review_order_key(v: 'VariantSummary') -> Tuple[float, str]:
    """THE review order (H-715; DESIGN.md 5.3 rule 2): floor multiple on the review component, descending, then the
    variant label. rank_variants (explore), and with it leaderboard_rows, the validate top-N (validate_variants,
    search.validate_points), round.json / status.json `top`, the cards and the page, all follow this key; the
    review_score stays in the rows for the record only."""
    return (-(v.floor_multiple or 0.0), v.variant)


def rank_variants(rows: Iterable[dict], question: str = 'explore', objective: Optional[dict] = None,
                  days: Optional[Sequence[str]] = None, base_variant: str = BASE_VARIANT) -> Ranking:
    """DESIGN.md 5.3 on trials.jsonl-like rows: {variant, day, metrics, [params], [status]} (status != done ignored).
    `days` restricts the evaluation (search days; hold-out days are ranked by a second call).
    explore -> review order (review_order_key: the floor multiple on the review component, descending), earlier/gentler -> Pareto front first (by scalar inside the
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
        hard_days: set = set()
        env_days: Dict[str, dict] = {}
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
                hard, env = feasibility_split(m, base_days[d], floors[d], cfg)
                if hard:
                    hard_days.add(d)
                elif env:
                    env_days[d] = envelope_day_record(m, env)
            ties[d] = tie_with_base(m, floors[d])
            sc, comp = effect_over_floor(m, floors[d])
            scores.append(sc)
            comps.append(comp)
        agg = aggregate_days({d: m for d, m in by_day.items() if d in all_days})
        fms = {k: floor_multiple(by_day, floors, k, all_days) for k in REVIEW_COMPONENTS}
        comp, fm = review_component_of(fms, comps)
        variants[name] = VariantSummary(
            variant=name, params=params.get(name, {}), days=agg['days'], feasible=not reasons,
            infeasible_reasons=reasons, tie_with_base=bool(ties) and all(ties.values()), tie_days=ties,
            review_score=statistics.fmean(scores) if scores else 0.0, review_component=comp,
            mean=agg['mean'], sign_frac=agg['sign_frac'], flags=sorted(vflags),
            missing_days=[d for d in all_days if d not in by_day], floor_multiples=fms, floor_multiple=fm,
            envelope_limited=bool(env_days) and set(reasons) == set(env_days) and len(env_days) < len(all_days),
            envelope_days=env_days)
    feasible = [n for n, v in variants.items() if v.feasible]
    infeasible = [n for n, v in variants.items() if not v.feasible]
    envelope_limited = [n for n in infeasible if variants[n].envelope_limited]
    front: List[str] = []
    no_step: List[str] = []
    if question == 'explore':
        order = sorted(feasible, key=lambda n: review_order_key(variants[n]))
    else:
        for n in variants:                           # envelope-limited too: the card says on which side it is
            variants[n].step_floor_U = step_floor(floors, variants[n].days)
            variants[n].step = is_step(variants[n].mean, question, variants[n].step_floor_U)
        steps = [n for n in feasible if variants[n].step]
        no_step = [n for n in feasible if not variants[n].step]
        pts = {n: pareto_objectives(variants[n].mean, question) for n in steps}
        front = pareto_front(pts)                    # among the steps only: a non-step is never on the front
        for n in feasible:
            variants[n].scalar = scalar_objective(variants[n].mean, question, cfg['scalar_weights'])
            variants[n].pareto = n in front
        order = sorted(feasible, key=lambda n: (0 if n in front else 1 if variants[n].step else 2,
                                                -(variants[n].scalar or 0.0), n))
    for i, n in enumerate(order, 1):
        variants[n].rank = i
    return Ranking(question=question, objective=cfg, base_variant=base_variant, days=all_days, floors=floors,
                   variants=variants, order=order, infeasible=infeasible, front=front,
                   rule_text=ranking_rule_text(question, cfg, floors), warnings=warnings,
                   envelope_limited=envelope_limited, no_step=no_step)


QUESTION_DIRECTION = {'earlier': 1, 'gentler': -1}     # the fixed direction of rise_vs_base_U a step asks for


def step_floor(floors: Dict[str, dict], days: Optional[Iterable[str]] = None) -> Optional[float]:
    """The floor a step on rises must clear: the mean over the scored days (`days`, default every day of `floors`)
    of the effective per-day floor_rise_U; None when no day has a floor."""
    vals = [effective_floor(floors[d].get('floor_rise_U')) for d in (sorted(floors) if days is None else days)
            if d in floors]
    return statistics.fmean(vals) if vals else None


def is_step(agg_mean: dict, question: str, floor_U: Optional[float]) -> Optional[bool]:
    """Is a variant a step of the question (GOAL.md): earlier = rise_vs_base_U above the floor (the loop would
    have asked for more insulin on rises than the noise floor allows to see), gentler = below -floor. A variant
    inside the floor asks for no more (or no less) on rises and is a tie, never a front member. None for explore."""
    if question not in QUESTION_DIRECTION:
        return None
    rise = agg_mean.get('rise_vs_base_U', 0.0) or 0.0
    return QUESTION_DIRECTION[question] * rise > (floor_U if floor_U is not None else effective_floor(None)) + 1e-12


def validate_top(ranking: Ranking, top: Optional[int] = None) -> List[str]:
    """THE top-N a validate round replays on the hold-out days (validate_variants and search.validate_points both
    call this): explore: the first N of the review order (review_order_key). earlier / gentler: the Pareto front
    members first (in scalar order), then the best scalar of the other steps, up to N in all; a variant that is not
    a step (rise inside the floor, or the wrong direction) is never validated. N (phase.top) is applied after the
    front: a front larger than N keeps its first N by the scalar, and validate_top_note() says so."""
    top = int(top or DEFAULT_VALIDATE_TOP)
    if ranking.question == 'explore':
        return list(ranking.order[:top])
    front = [n for n in ranking.order if n in set(ranking.front)]
    rest = [n for n in ranking.order if n not in set(ranking.front) and ranking.variants[n].step]
    return (front + rest)[:top]


def validate_top_note(ranking: Ranking, top: Optional[int] = None) -> Optional[str]:
    """The note of a validate round whose Pareto front is larger than N (earlier / gentler), else None."""
    top = int(top or DEFAULT_VALIDATE_TOP)
    if ranking.question == 'explore' or len(ranking.front) <= top:
        return None
    return (f'validate: the Pareto front has {len(ranking.front)} members, more than the top {top} the validate phase'
            f' replays; the first {top} by the scalar were validated, the other {len(ranking.front) - top} were not')


def primary_direction(question: str, mean_value) -> int:
    """The sign a validate round's direction test asks of the primary component on a hold-out day: earlier always
    +1 (the loop would have asked for more insulin than base in rising loops: rise_vs_base_U > 0, GOAL.md), else
    the sign of the search mean (explore: its review component; gentler: rise_vs_base_U)."""
    if question == 'earlier':
        return 1
    return _sign(mean_value)


def weights_sensitivity(ranking: Ranking, top: int = SENSITIVITY_TOP,
                        factors: Sequence[float] = SENSITIVITY_FACTORS) -> Optional[dict]:
    """DESIGN.md 5.3 rule 3, the weights-sensitivity panel: the scalar order of the feasible variants at the study's
    weights and under factor x each weight of the question (earlier: rest, lowside; gentler: rest, zero_temp), the
    others held. None for explore (no scalar). The Pareto front does not depend on the weights; the scalar orders
    inside and after it do. Returns {'question', 'weights', 'top', 'order': [top names at the stated weights],
    'by_weight': {weight: {'0.5': {'order': [top names], 'tau': Kendall tau of the whole scalar order vs the
    stated one, 'same_top': bool}, '1.5': {...}}}} - names as rank_variants knows them (round.py maps to vhashes)."""
    if ranking.question not in QUESTION_WEIGHTS:
        return None
    w = dict(DEFAULT_OBJECTIVE['scalar_weights'])
    w.update((ranking.objective or {}).get('scalar_weights') or {})
    feas = [n for n in ranking.order]

    def order_at(weights):
        sc = {n: scalar_objective(ranking.variants[n].mean, ranking.question, weights) for n in feas}
        return sorted(feas, key=lambda n: (-sc[n], n))
    stated = order_at(w)
    out = {'question': ranking.question, 'weights': {k: w[k] for k in QUESTION_WEIGHTS[ranking.question]},
           'top': int(top), 'factors': [float(f) for f in factors], 'order': stated[:top], 'by_weight': {}}
    for key in QUESTION_WEIGHTS[ranking.question]:
        per = {}
        for f in factors:
            ww = dict(w)
            ww[key] = w[key] * f
            o = order_at(ww)
            tau = kendall_tau(stated, o)
            per[f'{f:g}'] = {'order': o[:top], 'tau': None if tau is None else round(tau, 3),
                             'same_top': o[:top] == stated[:top]}
        out['by_weight'][key] = per
    return out


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
    """DESIGN.md 5.3 rule 5 for every variant of `ranking`: recommended for review only if not a tie with base
    (rule 4: a tie is never recommended for review), feasible on all days,
    top-fold_top on >= fold_share of the leave-one-day-out folds, same sign of the primary effect on >= sign_share
    of days, and rank on the hold-out days within the top holdout_top (objective.recommend, defaults 3, 80 %, 80 %,
    5; recommend_rule_text(validate=False) states it) (holdout_order None = no hold-out days reserved -> that condition fails,
    with the reason named).  Sets `recommended_for_review` / `recommend_reasons` on the summaries and returns them."""
    cfg = _objective(objective or ranking.objective)['recommend']
    out = {}
    for name, v in ranking.variants.items():
        reasons = []
        if not v.feasible:
            reasons.append('not feasible on every day')
        if v.tie_with_base:
            reasons.append(TIE_REASON)
        if folds:
            share = sum(1 for order in folds.values() if name in order[:cfg['fold_top']]) / len(folds)
            if share < cfg['fold_share'] - 1e-9:
                reasons.append(f'top-{cfg["fold_top"]} on {share_pct(share, 0)} of the leave-one-day-out folds'
                               f' (needs {share_pct(cfg["fold_share"])})')
        else:
            reasons.append('fewer than two days: no leave-one-day-out folds')
        if v.step is False:
            reasons.append(no_step_reason(ranking.question))
        primary = v.review_component if ranking.question == 'explore' else 'rise_vs_base_U'
        sign = v.sign_frac.get(primary, 0.0)
        if ranking.question == 'earlier':            # the direction of earlier is fixed: more on rises (rise > 0)
            sign = sign if (v.mean.get(primary) or 0.0) > 0 else 0.0
        if len(v.days) < 2:
            reasons.append('fewer than two days: sign stability not testable')
        elif sign < cfg['sign_share'] - 1e-9:
            reasons.append(f'same sign of {primary} on {share_pct(sign, 0)} of days (needs {share_pct(cfg["sign_share"])})')
        if holdout_order is None:
            reasons.append('no hold-out days')
        elif name not in list(holdout_order)[:cfg['holdout_top']]:
            reasons.append(f'not in the top {cfg["holdout_top"]} on the hold-out days')
        v.recommended_for_review = not reasons
        v.recommend_reasons = reasons
        out[name] = (not reasons, reasons)
    return out


# ----------------------------------------------------------------------------- H-308: the validate phase
DEFAULT_VALIDATE_TOP = 8                               # validate: top-N of the search review order when phase.top is None
VALIDATE_SIGN_SHARE = DEFAULT_OBJECTIVE['recommend']['sign_share']   # the default of objective.recommend.sign_share
FLIP_DAYS_MAX = 10                                     # hold-out days listed by name per variant (round.json size)
TIE_REASON = 'indistinguishable from base within the noise floor on every component and every day'
VALIDATION_KEYS = ('search_rank', 'holdout_rank', 'holdout_total', 'component', 'direction', 'holds', 'holdout_days',
                   'flip_days', 'missing_days', 'lodo_min', 'lodo_max', 'lodo_folds', 'lodo_unranked',
                   'fold_top', 'fold_share', 'holdout_top', 'inside_floor_days', 'tie_with_base',
                   'feasible_all_days', 'infeasible_days', 'recommended_for_review', 'reasons')


def kendall_tau(order_a: Sequence[str], order_b: Sequence[str]) -> Optional[float]:
    """Kendall's tau (tau-a; two strict orders have no ties) between two orders over the items both contain:
    (concordant - discordant) / (n (n - 1) / 2); None for fewer than two common items."""
    in_b = {x: i for i, x in enumerate(order_b)}
    common = [x for x in order_a if x in in_b]
    n = len(common)
    if n < 2:
        return None
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            if in_b[common[i]] < in_b[common[j]]:
                conc += 1
            else:
                disc += 1
    return (conc - disc) / (n * (n - 1) / 2)


def _sign(v) -> int:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return 0
    return (v > 0) - (v < 0)


@dataclass
class Validation:
    """The validate round of one study (H-308): the top-N of the search review order replayed on the hold-out days.

    validated        variant names, the top-N of the search order (feasible on the search days), in that order
    search_rank      name -> rank in the search order
    holdout_order    the order of rank_variants over the hold-out days (validated variants only)
    scored_days      search + hold-out days; the leave-one-day-out folds run over these
    folds            left-out day -> order over the other scored days
    per_variant      name -> dict with VALIDATION_KEYS (what round.json's Candidate.validation carries)
    tau              Kendall tau between the search order and the hold-out order of the validated variants"""
    top: int
    search_days: List[str]
    holdout_days: List[str]
    scored_days: List[str]
    validated: List[str]
    search_rank: Dict[str, int]
    holdout_ranking: Optional['Ranking']
    holdout_order: List[str]
    full_ranking: Optional['Ranking']
    folds: Dict[str, List[str]]
    per_variant: Dict[str, dict]
    tau: Optional[float]
    sign_share: float = VALIDATE_SIGN_SHARE
    fold_top: int = DEFAULT_OBJECTIVE['recommend']['fold_top']
    fold_share: float = DEFAULT_OBJECTIVE['recommend']['fold_share']
    holdout_top: int = DEFAULT_OBJECTIVE['recommend']['holdout_top']

    @property
    def recommended(self) -> List[str]:
        return [n for n in self.validated if self.per_variant.get(n, {}).get('recommended_for_review')]

    def summary(self) -> dict:
        """The study-level numbers (round.json `validation`), with the rule-5 config the validation ran with
        (fold_top, fold_share, sign_share, holdout_top) so report.validation_lines phrases the rule from them."""
        return {'top': self.top, 'validated': len(self.validated), 'search_days': len(self.search_days),
                'holdout_days': list(self.holdout_days), 'folds': len(self.folds),
                'tau': None if self.tau is None else round(self.tau, 3),
                'recommended_for_review': len(self.recommended), 'sign_share': self.sign_share,
                'fold_top': self.fold_top, 'fold_share': self.fold_share, 'holdout_top': self.holdout_top}


def validation_line(v: Optional[dict]) -> Optional[str]:
    """'hold-out: rank 2 of 8, direction holds on 14/16 days, LODO range 1-3' (cards, status.json top), or None."""
    if not v:
        return None
    rank = (f'rank {v["holdout_rank"]} of {v["holdout_total"]}' if v.get('holdout_rank') is not None
            else 'not ranked')
    lodo = (f'LODO range {v["lodo_min"]}-{v["lodo_max"]}' if v.get('lodo_min') is not None else 'LODO not ranked')
    return f'hold-out: {rank}, direction holds on {v.get("holds", 0)}/{v.get("holdout_days", 0)} days, {lodo}'


def validate_variants(rows: Sequence[dict], question: str = 'explore', objective: Optional[dict] = None,
                      search_days: Sequence[str] = (), holdout_days: Sequence[str] = (), top: Optional[int] = None,
                      base_variant: str = BASE_VARIANT, sign_share: Optional[float] = None) -> Validation:
    """H-308 on trials.jsonl-like rows: the top-N feasible variants of the search review order (rank_variants over
    `search_days`; N = `top`, default DEFAULT_VALIDATE_TOP) replayed on `holdout_days` (the scored, non-quarantined
    hold-out days):

    hold-out rank        rank_variants over the hold-out days, the validated variants (and base) only
    direction            per hold-out day the sign of the variant's review component (rise or rest vs base, the one
                         its search review order used; 'rise_vs_base_U' for earlier / gentler) against the sign asked
                         (primary_direction: +1 for earlier - rise_vs_base_U > 0 on the hold-out day -, else the sign of
                         its search mean); a day holds when the signs are equal and not 0, a day without a done row is
                         missing (it does not hold)
    top-N                validate_top(): explore the first N of the review order; earlier / gentler the Pareto front
                         first, then the best scalar, up to N
    leave-one-day-out    one fold per scored day (search + hold-out). Leaving out a search day ranks EVERY variant
                         over the other search days (the only days all of them were replayed on); leaving out a
                         hold-out day ranks the validated variants over the search days plus the other hold-out days.
                         A fold that does not rank the variant (infeasible on a day it contains) does not count as top
    tau                  Kendall tau between the search order and the hold-out order (validated variants on both)

    recommended_for_review = DESIGN.md 5.3 rule 5 with the objective's `recommend` config (fold_top, fold_share,
    sign_share, holdout_top; defaults 3, 80 %, 80 %, 5): not a tie with base (rule 4: inside the noise floor on every
    component and every search day is never recommended for review), feasible on every search AND hold-out day,
    top-`fold_top` on >= `fold_share` of the leave-one-day-out folds, the same sign of the primary effect on >=
    `sign_share` of the hold-out days, and a hold-out rank within the top `holdout_top`. Every condition that fails
    is named in `reasons`. `inside_floor_days` is a caveat, not a condition: on how many of the hold-out days on
    which the direction holds the effect stays inside TIE_FACTOR x that day's floor. `sign_share` overrides the
    objective's value."""
    cfg = _objective(objective)
    rec = cfg['recommend']
    share_needed = float(rec['sign_share'] if sign_share is None else sign_share)
    fold_top, fold_needed, holdout_top = int(rec['fold_top']), float(rec['fold_share']), int(rec['holdout_top'])
    top = int(top or DEFAULT_VALIDATE_TOP)
    rows = list(rows)
    search_days = sorted(search_days)
    holdout_days = sorted(d for d in holdout_days if d not in set(search_days))
    search = rank_variants(rows, question, objective, search_days, base_variant)
    validated = validate_top(search, top)
    search_rank = {n: search.variants[n].rank for n in validated}
    keep = set(validated) | {base_variant}
    vrows = [r for r in rows if r['variant'] in keep]
    scored = sorted(set(search_days) | set(holdout_days))
    holdout_ranking = rank_variants(vrows, question, objective, holdout_days, base_variant) if holdout_days else None
    holdout_order = list(holdout_ranking.order) if holdout_ranking is not None else []
    holdout_floors = holdout_ranking.floors if holdout_ranking is not None else {}
    full = rank_variants(vrows, question, objective, scored, base_variant) if holdout_days else None
    folds: Dict[str, List[str]] = {}
    if holdout_days:
        if len(search_days) >= 2:
            for d in search_days:
                folds[d] = rank_variants(rows, question, objective, [x for x in search_days if x != d], base_variant).order
        for d in holdout_days:
            folds[d] = rank_variants(vrows, question, objective, [x for x in scored if x != d], base_variant).order
    table, _ = _rows_by_variant_day(vrows, base_variant, holdout_days)
    per: Dict[str, dict] = {}
    for name in validated:
        sv = search.variants[name]
        comp = sv.review_component if question == 'explore' else 'rise_vs_base_U'
        fkey = FLOOR_COMPONENTS[comp]
        want = primary_direction(question, sv.mean.get(comp))
        by_day = table.get(name, {})
        flips, missing, holds, inside = [], [], 0, 0
        for d in holdout_days:
            m = by_day.get(d)
            if m is None:
                missing.append(d)
            elif want != 0 and _sign(m.get(comp)) == want:
                holds += 1
                if d in holdout_floors and is_tie(m.get(comp) or 0.0, holdout_floors[d].get(fkey), fkey):
                    inside += 1
            else:
                flips.append(d)
        ranks = [order.index(name) + 1 for order in folds.values() if name in order]
        unranked = len(folds) - len(ranks)
        in_top = sum(1 for order in folds.values() if name in order[:fold_top])
        fold_share = in_top / len(folds) if folds else None
        fv = full.variants.get(name) if full is not None else None
        infeasible_days = sorted(fv.infeasible_reasons) if fv is not None else []
        feasible_all = fv is not None and fv.feasible
        n_hold = len(holdout_days)
        hr = (holdout_order.index(name) + 1) if name in holdout_order else None
        reasons = []                                 # one entry per failed condition (details in brackets)
        if sv.tie_with_base:
            reasons.append(TIE_REASON)
        if sv.step is False:
            reasons.append(no_step_reason(question))
        if not holdout_days:
            reasons.append('no hold-out days')
        if missing:
            reasons.append(f'not replayed on {len(missing)} of {n_hold} hold-out days yet')
        if fv is not None and not feasible_all:
            reasons.append(f'not feasible on {len(infeasible_days)} day(s): {", ".join(infeasible_days[:4])}'
                           + (' ...' if len(infeasible_days) > 4 else ''))
        if folds:
            if fold_share < fold_needed - 1e-9:
                reasons.append(f'top-{fold_top} on {share_pct(fold_share, 0)} of the {len(folds)} leave-one-day-out'
                               f' folds (needs {share_pct(fold_needed)}'
                               + (f'; not ranked in {unranked}' if unranked else '') + ')')
        elif holdout_days:
            reasons.append('fewer than two scored days: no leave-one-day-out folds')
        if n_hold:
            if holds / n_hold < share_needed - 1e-9:
                reasons.append(f'direction holds on {holds} of {n_hold} hold-out days (needs {share_pct(share_needed)})')
                if flips:
                    reasons.append(f'direction flips on {len(flips)} of {n_hold} hold-out days')
            if hr is None:
                reasons.append('not ranked on the hold-out days')
            elif hr > holdout_top:
                reasons.append(f'hold-out rank {hr} (needs within the top {holdout_top})')
        ok = not reasons
        per[name] = {'search_rank': search_rank[name], 'holdout_rank': hr, 'holdout_total': len(holdout_order),
                     'component': comp, 'direction': want, 'holds': holds, 'holdout_days': n_hold,
                     'flip_days': flips[:FLIP_DAYS_MAX], 'missing_days': len(missing),
                     'lodo_min': min(ranks) if ranks else None, 'lodo_max': max(ranks) if ranks else None,
                     'lodo_folds': len(folds), 'lodo_unranked': unranked, 'fold_top': fold_top,
                     'fold_share': None if fold_share is None else round(fold_share, 3), 'holdout_top': holdout_top,
                     'inside_floor_days': inside, 'tie_with_base': bool(sv.tie_with_base),
                     'feasible_all_days': feasible_all,
                     'infeasible_days': infeasible_days[:FLIP_DAYS_MAX], 'recommended_for_review': ok,
                     'reasons': reasons}
    tau = kendall_tau(validated, holdout_order)
    return Validation(top=top, search_days=search_days, holdout_days=holdout_days, scored_days=scored,
                      validated=validated, search_rank=search_rank, holdout_ranking=holdout_ranking,
                      holdout_order=holdout_order, full_ranking=full, folds=folds, per_variant=per, tau=tau,
                      sign_share=share_needed, fold_top=fold_top, fold_share=fold_needed, holdout_top=holdout_top)


def no_step_reason(question: str) -> str:
    """The reason a variant that is not a step of the question is never recommended for review."""
    if question == 'gentler':
        return 'not a gentler step: asks for no less insulin on rises than the floor'
    return 'not an earlier step: asks for no more insulin on rises than the floor'


def validation_caveat(v: Optional[dict]) -> Optional[str]:
    """The caveat line of a variant recommended for review (H-308): on how many of the hold-out days on which the
    direction holds the effect stayed inside TIE_FACTOR x that day's floor; None when there is nothing to say."""
    if not v or not v.get('holds'):
        return None
    k = int(v.get('inside_floor_days') or 0)
    if not k:
        return f'caveat: outside {TIE_FACTOR:g} x floor on every one of the {v["holds"]} holding hold-out days'
    return f'caveat: inside {TIE_FACTOR:g} x floor on {k} of the {v["holds"]} holding hold-out days'


def _fmt_floor(v):
    return f'{v:.2f}'


def ranking_rule_text(question: str = 'explore', objective: Optional[dict] = None, floors: Optional[Dict[str, dict]] = None,
                      validate: bool = False) -> str:
    """the ranking rule as printed next to the leaderboard (DESIGN.md 5.3; vocabulary of section 3); `validate`: the
    study has a validate round (H-308), whose rule decides recommended for review"""
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
    if question == 'earlier' and validate:
        lines.append('Validate for "earlier": the Pareto front first, then the scalar, up to N variants; the direction'
                     ' holds on a hold-out day when the loop would have asked for more insulin than base in rising'
                     ' loops there.')
    lines.append(f'Feasible = no more insulin than base plus the floor in the two hours before a recorded low on every day'
                 f' ({prehypo_rule_phrase(prehypo_exclude_hours(cfg))}),'
                 f' inside the envelope on every day (at most {env["max_3h_U"]} U vs base in any 3 h window and'
                 f' {env["max_day_pct_tdd"]} % of the phone\'s TDD per day), no emulated SMB above the phone\'s maxBolus,'
                 f' factor on a cap in fewer than {feas["cap_binding_max_pct"]} % of loops, loop count equal to base.'
                 ' Infeasible variants are shown greyed with the reason, never ranked.')
    lines.append(f'Floor per day and component = |base - phone|; a difference smaller than {TIE_FACTOR:g} x floor is a tie'
                 ' (grey). Variants inside the floor on every component are indistinguishable from base within the noise'
                 ' floor; parameters with every point inside the floor on all days are not testable on these days.')
    if validate:
        lines.append('Recommended for review only after the validate phase: one of the top N of the search review order'
                     ' (replayed on the hold-out days, never seen by the search) that is '
                     + recommend_rule_text(cfg, validate=True) + '. ' + RULE5_TIE + ' Settings are for'
                     ' you and your care team to review; nothing here changes the phone.')
    else:                                            # byte-identical to the H-202 text at the default config
        lines.append('Recommended for review only if ' + recommend_rule_text(cfg, validate=False) + '. Settings are'
                     ' for you and your care team to review; nothing here changes the phone.')
    if validate:
        n = len(floors or {})
        over = f'over the {n} scored search {"day" if n == 1 else "days"}' if n else 'over the scored search days'
        lines.append(f'Floors and every x floor multiple of the review order are computed {over} only (hold-out days'
                     ' get their own floors in the hold-out ranking): quarantined days are listed, not scored. A'
                     ' multiple is the mean over days of |effect| / that day\'s floor.')
    if floors:
        parts = []
        for d in sorted(floors):
            f = floors[d]
            parts.append(f'{d}: rise {_fmt_floor(f["floor_rise_U"])} U, rest {_fmt_floor(f["floor_rest_U"])} U,'
                         f' total {_fmt_floor(f["floor_total_U"])} U, {int(f["floor_loops"])} loops changed by the replay'
                         + (f', base agreement {f["agreement_pct"]:.1f} %' if f.get('agreement_pct') is not None else ''))
        lines.append('Floors - ' + '; '.join(parts) + '.')
    return '\n'.join(lines)


def envelope_days_text(envelope_days: Dict[str, dict], limit: Optional[int] = None) -> str:
    """'2026-09-04 (+1.6 U in 3 h), 2026-09-12 (17 % of TDD)' for the days beyond the envelope (largest 3 h window
    first); `limit` cuts the list with ', ...'."""
    def size(item):
        r = item[1]
        return -abs(r.get('env_3h_U') or 0.0), item[0]
    parts = []
    for day, r in sorted(envelope_days.items(), key=size):
        u = r.get('env_3h_U')
        if u is not None and any('3 h window' in x for x in r.get('reasons', [])):
            parts.append(f'{day} ({u:+.1f} U in 3 h)')
        elif r.get('env_day_pct_tdd') is not None:
            parts.append(f'{day} ({r["env_day_pct_tdd"]:.0f} % of TDD)')
        else:
            parts.append(day)
    if limit is not None and len(parts) > limit:
        parts = parts[:limit] + ['...']
    return ', '.join(parts)


def envelope_max_3h(envelope_days: Dict[str, dict]) -> Optional[float]:
    """The largest 3 h window vs base (signed, by magnitude) over the days beyond the envelope, or None."""
    vals = [r['env_3h_U'] for r in envelope_days.values() if r.get('env_3h_U') is not None]
    return max(vals, key=abs) if vals else None


ENVELOPE_DAYS_MAX = 10                                 # days listed per envelope-limited variant in round.json (size)


def envelope_summary(envelope_days: Dict[str, dict], n_days: int, limit: int = ENVELOPE_DAYS_MAX) -> dict:
    """round.json's Candidate.envelope of an envelope-limited variant: the days beyond E (largest 3 h window first,
    at most `limit`), their count, the largest 3 h window vs base and how many of the `n_days` stayed inside."""
    items = sorted(envelope_days.items(), key=lambda kv: (-abs(kv[1].get('env_3h_U') or 0.0), kv[0]))
    return {'days': {d: {k: r[k] for k in ('env_3h_U', 'env_day_pct_tdd') if r.get(k) is not None}
                     for d, r in items[:limit]},
            'n_days': len(envelope_days), 'max_3h_U': envelope_max_3h(envelope_days),
            'inside_days': int(n_days) - len(envelope_days)}


def envelope_limited_text(envelope_days: Dict[str, dict], limit: Optional[int] = 6) -> str:
    """The `why` of an envelope-limited variant: 'beyond what the replay can judge on 2 days: 2026-09-04 (+1.6 U in
    3 h), ...; inside the envelope and every hard check on the other days'."""
    n = len(envelope_days)
    return (f'beyond what the replay can judge on {n} day{"s" if n != 1 else ""}: '
            f'{envelope_days_text(envelope_days, limit)}; passes the pre-hypo, maxBolus, cap and loop-count checks on'
            f' every day')


def leaderboard_rows(ranking: Ranking) -> List[dict]:
    """flat rows for a table or round.json: one per variant (base excluded), ranked first, then envelope-limited,
    then infeasible, with the verdict word (recommended / candidate / tie / envelope-limited / infeasible) and a
    `why` string"""
    out = []
    lim = set(ranking.envelope_limited)
    for name in (ranking.order + list(ranking.envelope_limited)
                 + [n for n in ranking.infeasible if n not in lim]):
        v = ranking.variants[name]
        if v.envelope_limited:
            verdict, why = 'envelope-limited', envelope_limited_text(v.envelope_days)
        elif not v.feasible:
            verdict, why = 'infeasible', '; '.join(f'{d}: {", ".join(r)}' for d, r in sorted(v.infeasible_reasons.items()))
        elif v.tie_with_base:                        # rule 4 before rule 5: a tie is never recommended for review
            verdict, why = 'tie', 'indistinguishable from base within the noise floor on every day'
        elif v.step is False:
            verdict, why = 'tie', no_step_reason(ranking.question)
        elif v.recommended_for_review:
            verdict, why = 'recommended', 'feasible on all days, stable across folds, days and hold-out'
        else:
            verdict, why = 'candidate', (f'{v.review_component} {v.mean.get(v.review_component, 0.0):+.2f} U vs base,'
                                         f' {v.floor_multiple:.1f} x floor')
        out.append({'variant': name, 'rank': v.rank, 'params': v.params, 'days': v.days, 'verdict': verdict, 'why': why,
                    'pareto': v.pareto, 'scalar': v.scalar, 'floor_multiple': v.floor_multiple,
                    'floor_multiples': dict(v.floor_multiples), 'review_score': v.review_score,
                    'review_component': v.review_component, 'feasible': v.feasible,
                    'recommended_for_review': v.recommended_for_review, 'tie_with_base': v.tie_with_base,
                    'envelope_limited': v.envelope_limited, 'envelope_days': dict(v.envelope_days), 'step': v.step,
                    'mean': {k: v.mean.get(k) for k in ('rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U',
                                                        'changed_vs_base', 'lowside_flags', 'prehypo_extra_U',
                                                        'env_3h_max_U', 'env_day_pct_tdd', 'zero_temp_vs_base',
                                                        'cap_binding_pct', 'max_single_smb_U') + MANUAL_KEYS},
                    'flags': v.flags})
    return out


__all__ = ['OBJECTIVE_VERSION', 'METRICS_VERSION', 'PREHYPO_EXCLUDE_AFTER_MANUAL_H', 'PREHYPO_KEYS', 'low_episodes',
           'manual_attributed_lows', 'prehypo_exclude_hours', 'prehypo_rule_phrase', 'prehypo_counts', 'prehypo_label',
           'prehypo_note', 'LoopSeries', 'trial_metrics', 'metrics_from_csv', 'read_result_csv', 'effective_cap',
           'loop_intervals_h', 'hindsight_covariates', 'prehypo_extra_U', 'envelope', 'changed_flags', 'agreement',
           'min_guard_from_levels', 'min_guard_from_reason', 'floors_from_base', 'effective_floor', 'is_tie',
           'tie_components', 'tie_with_base', 'effect_over_floor', 'feasibility_day', 'aggregate_days',
           'pareto_objectives', 'pareto_front', 'dominates', 'scalar_objective', 'rank_variants', 'leave_one_day_out',
           'recommend', 'ranking_rule_text', 'leaderboard_rows', 'Ranking', 'VariantSummary', 'DEFAULT_OBJECTIVE',
           'kendall_tau', 'validate_variants', 'validation_line', 'validation_caveat', 'Validation',
           'DEFAULT_VALIDATE_TOP', 'VALIDATE_SIGN_SHARE', 'VALIDATION_KEYS', 'TIE_REASON', 'floor_multiple',
           'floor_band', 'tie_every_day', 'review_component_of', 'review_order_key', 'study_floor', 'share_pct', 'recommend_config', 'recommend_rule_text',
           'RULE5_TIE', 'manual_bolus_coverage', 'MANUAL_KEYS', 'MANUAL_WINDOW_S', 'PREEMPT_SHARE', 'validate_top',
           'primary_direction', 'weights_sensitivity', 'validate_top_note', 'step_floor', 'is_step', 'no_step_reason',
           'QUESTION_DIRECTION', 'feasibility_split', 'envelope_days_text', 'envelope_max_3h', 'envelope_summary', 'ENVELOPE_DAYS_MAX', 'envelope_limited_text', 'envelope_day_record', 'ENVELOPE_PREFIX', 'SENSITIVITY_FACTORS', 'SENSITIVITY_TOP', 'QUESTION_WEIGHTS',
           'FLOOR_COMPONENTS', 'REVIEW_COMPONENTS', 'TIE_FACTOR', 'BASE_VARIANT', 'QUESTIONS']
