"""
export.py  -  tier-1 / tier-2 JSON writer of a Hindcast study (backlog H-002: series / pred / orig contract and the
tier split; the db rows, candidate cards and VDF copy of later issues are added to this module by their owners)

Tier split (DESIGN.md section 4.5 and 6)
-----------------------------------------
tier 1   one JSON document embedded in report/index.html (`build_tier1()` + `script_tag()`): study, status, rounds,
         every trials.jsonl row, the day records, the honesty statement and the path templates of the tier-2 files
tier 2   one file per day and variant, fetched on demand (published through the Artifact `files` map):
         report/series/<day>/<vhash>.json       emulated decisions of one variant on one day (columnar)
         report/series/<day>/orig.json          the phone's decisions, recorded BG/IOB/COB, events, hindsight covariates
         report/pred/<day>/<vhash>.json         per-loop prediction arrays, Levels, factor chain, ISF, iobTH, reason
         report/pred/<day>/<vhash>.text.json    per-loop console lines and flowchart boxes (string tables), drill-down only
         report/pred/<day>/orig.json            the phone's predBGs and reason
         report/pred/<day>/orig.text.json       the phone's consoleError lines (string table)
         `vhash` is contracts.vhash(params); the base variant is contracts.BASE_VHASH (vhash of no parameters).
         `tier2_path(report_dir, kind, day, vhash)` builds the paths; TIER2_PATHS holds the templates for the page.

Numbers: rounded per kind (DECIMALS: BG 1 dp, insulin 3 dp, factors 2 dp, t 1 dp), integral values written as
integers, None for missing, never NaN (`allow_nan=False` on every dump).

Columnar series document (every per-loop list has `loops` entries, `t` ascending):

    {"format": 1, "kind": "series", "day": "2026-09-06", "vhash": "...", "variant": "base", "loops": 180,
     "window": [start_iso, end_iso], "units": {...}, "flags": [],
     "t": [...]            seconds since 00:00 UTC of `day` (loops of the next UTC day run past 86400)
     "smb", "ins_req", "tbr", "max_bolus", "iob_th"              insulin (U; tbr in U/h)
     "isf_factor", "autosens", "f_acce", "f_bg", "f_pp", "f_drift", "f_dura"    factors
     "isf", "tar_lo", "tar_hi", "eventual_bg", "min_pred_bg", "min_guard"      BG in mg/dl
     "changed"             1 where SMB, insulinReq or TBR rate differ from the phone by more than 0.05 (U, U, U/h)
     "smb_off"             'threshold' | 'maxDelta' | None: why determine_basal.py disabled SMB (from Pred Levels)}

`eventual_bg`, `min_pred_bg`, `min_guard` and `smb_off` need the `Pred` globals of the replay (`pred=` argument);
without them they are all-None lists and `flags` holds "no_pred". `min_guard` is the `minGuardBG1` that
determine_basal.py exposes only in the loops where it disabled SMB; elsewhere it is None.

`kind: "orig"` (series/<day>/orig.json) has the same `t` and the phone's `smb, ins_req, tbr, max_bolus, isf_factor,
isf, isf_prof, autosens, tar_lo, tar_hi`, plus `bg` (the smoothed value the loop used), `iob`, `cob`, `act`,
`dura_min`, `dura_avg`, the positional covariates `low_ahead` (a recorded BG < 70 mg/dl within the next 120 min)
and `high_ahead` (> 180 within 180 min) and `events`, a list of {"kind", "t", "t_end", "value", "label"} with kinds
`low` (first reading below 70), `cgm_gap` (> 12 min between loops), `target_change` (phone's target changed:
TT set or ended) and `cob_rise` (COB stepped up: carbs entered or the estimate changed). Events the CSV does not
carry (carb grams, TT values, profile switches) are merged in by the caller through `events=`.

Prediction document (`kind: "pred"`), one entry per loop in the order of `t`:

    {"format": 1, "kind": "pred", "day", "vhash", "variant", "loops", "window", "units", "flags", "t": [...],
     "iob": [[...] | null], "zt": ..., "uam": ..., "cob": ...     emulated predBG arrays (mg/dl); cob is null when
                                                                   the loop had no COB (like the phone's predBGs)
     "levels": [{eventualBG, minPredBG, minIOBPredBG, minZTGuardBG, minUAMPredBG, minCOBPredBG, avgPredBG,
                 threshold, maxDelta, naive_eventualBG, SMBoff, type, value, minGuardBG1, source, timePos} | null]
     "factors": [{"acce", "bg", "pp", "dura", "drift", "delta", "final"} | null]   final from the CSV column
     "isf": [...], "iob_th": [...]                                 emulated ISF (mg/dl per U) and iobTH (U)
     "reason": [str | null]}                                       from the variant's .txt transcript
`kind: "pred_text"` (<vhash>.text.json): `"strings": [...]`, `"boxes": [[title, indent, adr], ...]` and per loop
`"console": [[string index, ...] | null]` (what the core echoes for the loop: the scraped original lines first, then
the lines determine_basal.py printed) and `"flows": [[box index, ...] | null]` (the FlowChart boxes in order).
`kind: "pred_orig"` (pred/<day>/orig.json) has `iob/zt/uam/cob` from the phone's predBGs (the IOB/ZT/UAM/COB keys
getOrigPred() copies into Pred) and `reason` from .orig.txt; `kind: "pred_orig_text"` its consoleError as a table.

Embedding: `embed_json()` dumps compact with allow_nan=False and `</` escaped as `<\\/` so the text is safe inside
<script type="application/json">; `script_tag()` wraps it. Files are written compact by atomic rename.

CSV column -> series key (the 43-column header of emulator_core's CSV as `emulator_optimize.read_result_csv`
joins it): SERIES_COLUMNS (emulated side) and ORIG_COLUMNS (phone side) below.

Measured sizes (tests/fixtures/report/, 2026-09-06 06:00-09:00Z, 180 loops at 1-minute intervals, compact JSON,
measured 2026-10-16; a full day of 1311 loops is ~7.3 x these):
    series/2026-09-06/<base vhash>.json      15.2 kB    84 B/loop  ->  ~0.11 MB per full day-variant
    series/2026-09-06/orig.json              14.7 kB    82 B/loop  ->  ~0.11 MB per full day
    pred/2026-09-06/<base vhash>.json       164.8 kB   916 B/loop  ->  ~1.2 MB per full day-variant (finalists only)
    pred/2026-09-06/<base vhash>.text.json  234.8 kB  1.3 kB/loop  ->  ~1.7 MB, loaded only for the console/flow panel
    pred/2026-09-06/orig.json               112.3 kB   624 B/loop  ->  ~0.8 MB per full day
    pred/2026-09-06/orig.text.json           93.0 kB   517 B/loop  ->  ~0.7 MB per full day
Against the backlog's expectations (<= 90 kB per day-variant series, <= 1.2 MB pred): the series meet them for a
5-minute-loop day and overshoot by ~20 % at this user's 1-minute loops because the factor chain and max_bolus ride
along; the pred document meets them only because the console/flow texts were split into the .text.json side file
(with them inline the 180 loops were 888 kB, ~6.5 MB per full day). Regenerate and re-measure with
`python -m hindcast.tests.make_report_fixtures` (MANIFEST.json lists the sizes).
"""
import datetime
import json
import math
import os
import re
import sys

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from emulator_optimize import read_result_csv                                               # noqa: E402
from hindcast import contracts                                                              # noqa: E402
from hindcast import metrics as _metrics                                                    # noqa: E402  the BG thresholds

FORMAT = 1
MGDL_PER_MMOL = _metrics.MMOL_TO_MGDL                    # 18.0
LOW_MGDL = _metrics.LOW_BG_MGDL                          # 3.9 mmol = 70.2 mg/dl: the positional "a low followed" marker,
                                                         # the same constant as the P window (metrics._recorded_lows)
HIGH_MGDL = _metrics.HIGH_BG_MGDL                        # 180 mg/dl
LOW_AHEAD_S = _metrics.LOW_FOLLOW_S                      # 120 min
HIGH_AHEAD_S = _metrics.HIGH_FOLLOW_S                    # 180 min
CGM_GAP_S = 12 * 60
CHANGE_TOL = 0.05                                        # U (SMB, insulinReq) and U/h (TBR): "differ from the phone"
DECIMALS = {'t': 1, 'bg': 1, 'insulin': 3, 'factor': 2, 'carbs': 1, 'minutes': 0, 'act': 3}
UNITS = {'t': 's since 00:00 UTC', 'bg': 'mg/dl', 'insulin': 'U', 'tbr': 'U/h', 'isf': 'mg/dl per U', 'cob': 'g',
         'act': 'U/min', 'dura_min': 'min', 'factor': 'ratio'}

# CSV column (as read_result_csv joins the three header rows) -> (series key, rounding kind)
SERIES_COLUMNS = (
    ('SMB emul', 'smb', 'insulin'), ('Ins. Req. emul', 'ins_req', 'insulin'), ('TBR emul', 'tbr', 'insulin'),
    ('max bolus emul', 'max_bolus', 'insulin'), ('eff. iobTH emul', 'iob_th', 'insulin'),
    ('final ISF emul', 'isf_factor', 'factor'), ('auto sens emul', 'autosens', 'factor'),
    ('acce ISF emul', 'f_acce', 'factor'), ('bg ISF emul', 'f_bg', 'factor'), ('pp ISF emul', 'f_pp', 'factor'),
    ('drift ISF emul', 'f_drift', 'factor'), ('dura ISF emul', 'f_dura', 'factor'),
    ('ISF emul', 'isf', 'bg'), ('target low emul', 'tar_lo', 'bg'), ('target high emul', 'tar_hi', 'bg'),
)
ORIG_COLUMNS = (
    ('SMB orig', 'smb', 'insulin'), ('Ins. Req. orig', 'ins_req', 'insulin'), ('TBR orig', 'tbr', 'insulin'),
    ('max bolus orig', 'max_bolus', 'insulin'), ('iob', 'iob', 'insulin'),
    ('final ISF orig', 'isf_factor', 'factor'), ('auto sens orig', 'autosens', 'factor'),
    ('ISF orig', 'isf', 'bg'), ('ISF prof', 'isf_prof', 'bg'), ('target low orig', 'tar_lo', 'bg'),
    ('target high orig', 'tar_hi', 'bg'), ('cob', 'cob', 'carbs'), ('act', 'act', 'act'),
    ('dura min- utes', 'dura_min', 'minutes'), ('dura avg.', 'dura_avg', 'bg'),
)
PRED_SERIES_KEYS = ('eventual_bg', 'min_pred_bg', 'min_guard', 'smb_off')
FACTOR_KEYS = (('acce', 'acceISF'), ('bg', 'BZ_ISF'), ('pp', 'pp_ISF'), ('dura', 'dura_ISF'), ('drift', 'drift_ISF'),
               ('delta', 'Delta_ISF'))
EMUL_PRED_ARRAYS = (('iob', 'IOBpredBGs'), ('zt', 'ZTpredBGs'), ('uam', 'UAMpredBGs'), ('cob', 'COBpredBGs'))
ORIG_PRED_ARRAYS = (('iob', 'IOB'), ('zt', 'ZT'), ('uam', 'UAM'), ('cob', 'COB'))
TIER2_PATHS = {'series': 'series/{day}/{vhash}.json', 'orig': 'series/{day}/orig.json',
               'pred': 'pred/{day}/{vhash}.json', 'pred_text': 'pred/{day}/{vhash}.text.json',
               'pred_orig': 'pred/{day}/orig.json', 'pred_orig_text': 'pred/{day}/orig.text.json'}
KINDS = tuple(TIER2_PATHS)
TABLE_KEYS = ('strings', 'boxes')                        # per-document tables, not per-loop lists


# ----------------------------------------------------------------------------- helpers

def rnd(value, kind):
    """Round `value` for its kind (DECIMALS); integral results become int; None / NaN / non-numbers -> None."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    v = round(float(value), DECIMALS[kind])
    return int(v) if v == int(v) else v


def day_of(unix_s):
    """UTC date 'YYYY-MM-DD' of a unix time in seconds."""
    return datetime.datetime.fromtimestamp(unix_s, datetime.timezone.utc).strftime('%Y-%m-%d')


def day_start(day):
    """Unix seconds of 00:00 UTC of 'YYYY-MM-DD'."""
    return datetime.datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp()


def iso_of(unix_s):
    return datetime.datetime.fromtimestamp(unix_s, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _col(rows, name):
    return [r.get(name) for r in rows]


def _num(rows, name):
    return [v if isinstance(v, (int, float)) and not isinstance(v, bool) else None for v in _col(rows, name)]


def loop_times(rows):
    """'UNIX time' column as floats (seconds)."""
    return [float(v) for v in _num(rows, 'UNIX time')]


def bg_of(rows):
    """The BG the loop used: 'bg accel' or, when the core moved it to the brake column, 'bg brake'."""
    return [a if a is not None else b for a, b in zip(_num(rows, 'bg accel'), _num(rows, 'bg brake'))]


def changed_flags(rows, tol=CHANGE_TOL):
    """1 where emulated SMB, insulinReq or TBR rate differ from the phone's by more than `tol`."""
    pairs = (('SMB emul', 'SMB orig'), ('Ins. Req. emul', 'Ins. Req. orig'), ('TBR emul', 'TBR orig'))
    flags = [0] * len(rows)
    for e_name, o_name in pairs:
        for i, (e, o) in enumerate(zip(_num(rows, e_name), _num(rows, o_name))):
            if abs((e or 0.0) - (o or 0.0)) > tol + 1e-9:
                flags[i] = 1
    return flags


def hindsight(t, bg):
    """Positional covariates: low_ahead (BG < 70 within 120 min after the loop), high_ahead (> 180 within 180 min)."""
    n = len(t)
    low, high = [0] * n, [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            dt = t[j] - t[i]
            if dt > HIGH_AHEAD_S:
                break
            b = bg[j]
            if b is None:
                continue
            if dt <= LOW_AHEAD_S and b < LOW_MGDL:
                low[i] = 1
            if b > HIGH_MGDL:
                high[i] = 1
    return low, high


def events_from_rows(rows, t0):
    """Events the CSV alone can show, positional only: low, cgm_gap, target_change, cob_rise."""
    t = loop_times(rows)
    bg = bg_of(rows)
    cob = _num(rows, 'cob')
    lo, hi = _num(rows, 'target low orig'), _num(rows, 'target high orig')
    events = []
    for i in range(len(rows)):
        ts = rnd(t[i] - t0, 't')
        if i > 0 and t[i] - t[i - 1] > CGM_GAP_S:
            events.append({'kind': 'cgm_gap', 't': rnd(t[i - 1] - t0, 't'), 't_end': ts,
                           'value': rnd((t[i] - t[i - 1]) / 60.0, 'minutes'),
                           'label': 'no loop for %d min' % round((t[i] - t[i - 1]) / 60.0)})
        if bg[i] is not None and bg[i] < LOW_MGDL and (i == 0 or bg[i - 1] is None or bg[i - 1] >= LOW_MGDL):
            events.append({'kind': 'low', 't': ts, 't_end': None, 'value': rnd(bg[i], 'bg'),
                           'label': 'recorded BG %.1f mmol/l (below 3.9) followed here' % (bg[i] / MGDL_PER_MMOL)})
        if i > 0 and (lo[i], hi[i]) != (lo[i - 1], hi[i - 1]) and lo[i] is not None and hi[i] is not None:
            events.append({'kind': 'target_change', 't': ts, 't_end': None, 'value': [rnd(lo[i], 'bg'), rnd(hi[i], 'bg')],
                           'label': 'target %.1f-%.1f mmol/l' % (lo[i] / MGDL_PER_MMOL, hi[i] / MGDL_PER_MMOL)})
        if i > 0 and cob[i] is not None and cob[i - 1] is not None and cob[i] > cob[i - 1] + 0.5:
            events.append({'kind': 'cob_rise', 't': ts, 't_end': None, 'value': rnd(cob[i] - cob[i - 1], 'carbs'),
                           'label': 'COB rose by %.0f g' % (cob[i] - cob[i - 1])})
    return events


# ----------------------------------------------------------------------------- Pred / FlowChart access

def pred_key(unix_s):
    """The key emulator_core uses for Pred / FlowChart: round(ms / 1000, 1)."""
    return round(round(unix_s * 1000) / 1000, 1)


def pred_lookup(pred_globals, unix_s):
    """Pred[...] entry of a loop, matched on the core's key or the nearest key within 1 s; None when absent."""
    if not pred_globals:
        return None
    k = pred_key(unix_s)
    if k in pred_globals:
        return pred_globals[k]
    for cand in (round(k + d, 1) for d in (-0.1, 0.1, -0.2, 0.2)):
        if cand in pred_globals:
            return pred_globals[cand]
    best = min(pred_globals, key=lambda x: abs(x - unix_s), default=None)
    return pred_globals[best] if best is not None and abs(best - unix_s) <= 1.0 else None


def _bg_list(values):
    if not isinstance(values, (list, tuple)) or not values:
        return None
    return [rnd(v, 'bg') for v in values]


def _levels(fcasts):
    levels = fcasts.get('Levels') if isinstance(fcasts, dict) else None
    if not isinstance(levels, dict) or not levels:
        return None
    out = {}
    for k, v in levels.items():
        if isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = rnd(v, 'bg') if k not in ('timePos', 'timePos2') else int(v)
        else:
            out[k] = str(v)
    return out


def _factors(fcasts, final):
    if not isinstance(fcasts, dict):
        return None
    out = {name: rnd(fcasts.get(key), 'factor') for name, key in FACTOR_KEYS}
    out['final'] = rnd(final, 'factor')
    return out


class _Table:
    """String / box table for the *.text.json documents: value -> stable index."""

    def __init__(self):
        self.items = []
        self.index = {}

    def add(self, value):
        key = value if isinstance(value, str) else tuple(value)
        i = self.index.get(key)
        if i is None:
            i = len(self.items)
            self.index[key] = i
            self.items.append(value)
        return i


# ----------------------------------------------------------------------------- transcripts (.txt / .orig.txt)

_BLOCK_RE = re.compile(r'^=+ loop in row (\d+) =+')
_STAMP_RE = re.compile(r'^\s*created at=\s*(\S+)')


def transcript_blocks(path):
    """Parse an emulator transcript (<base>.<variant>.txt or <base>.orig.txt) into one dict per loop:
    {'row': n, 'stamp': iso, 't': unix seconds, 'console': [lines], 'reason': str}. `console` is what the core
    echoes between the 'Script Debug' and 'Reason' headers (the scraped original lines first, then the lines
    determine_basal.py printed), separator lines removed."""
    blocks = []
    cur = None
    section = None
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            line = raw.rstrip('\n').rstrip('\r')
            m = _BLOCK_RE.match(line)
            if m:
                cur = {'row': int(m.group(1)), 'stamp': None, 't': None, 'console': [], 'reason': ''}
                blocks.append(cur)
                section = None
                continue
            if cur is None:
                continue
            m = _STAMP_RE.match(line)
            if m and cur['stamp'] is None:
                cur['stamp'] = m.group(1)
                try:
                    cur['t'] = datetime.datetime.fromisoformat(m.group(1).replace('Z', '+00:00')).timestamp()
                except ValueError:
                    cur['t'] = None
                continue
            if line.startswith('---------- Script Debug'):
                section = 'console'
                continue
            if line.startswith('---------- Reason'):
                section = 'reason'
                continue
            if section == 'console':
                if line.strip() and not set(line.strip()) <= {'-'}:
                    cur['console'].append(line.rstrip())
            elif section == 'reason':
                if line.strip():
                    cur['reason'] = (cur['reason'] + ' ' + line.strip()).strip()
    return blocks


def match_blocks(blocks, t):
    """-> list aligned with `t`: the transcript block of each loop (nearest stamp within 2 s) or None."""
    by_t = sorted(((b['t'], i) for i, b in enumerate(blocks) if b['t'] is not None))
    out = []
    j = 0
    for ti in t:
        while j + 1 < len(by_t) and abs(by_t[j + 1][0] - ti) <= abs(by_t[j][0] - ti):
            j += 1
        out.append(blocks[by_t[j][1]] if by_t and abs(by_t[j][0] - ti) <= 2.0 else None)
    return out


def _blocks_arg(transcript, t_abs):
    if transcript is None:
        return [None] * len(t_abs)
    blocks = transcript_blocks(transcript) if isinstance(transcript, str) else list(transcript)
    return match_blocks(blocks, t_abs)


# ----------------------------------------------------------------------------- series documents

def _header(kind, day, vhash, variant, t_abs, window=None):
    return {'format': FORMAT, 'kind': kind, 'day': day, 'vhash': vhash, 'variant': variant, 'loops': len(t_abs),
            'window': list(window) if window else ([iso_of(t_abs[0]), iso_of(t_abs[-1])] if t_abs else None),
            'units': dict(UNITS), 'flags': [], 't': [rnd(v - day_start(day), 't') for v in t_abs]}


def series_from_rows(names, rows, day=None, vhash=contracts.BASE_VHASH, variant='base', pred=None, window=None,
                     events=None):
    """(emul_series, orig_series) from a parsed result CSV. `pred` = emulator_core.Pred right after the replay
    (optional). `events` are merged into the orig document's events. `day` defaults to the UTC date of the first
    loop; `t` is relative to its 00:00 UTC even for loops that run past midnight."""
    missing = [c for c, _, _ in SERIES_COLUMNS + ORIG_COLUMNS if c not in names] + \
              [c for c in ('UNIX time', 'bg accel') if c not in names]
    if missing:
        raise KeyError('CSV columns not found: %s' % missing)
    t_abs = loop_times(rows)
    n = len(rows)
    day = day or (day_of(t_abs[0]) if t_abs else '1970-01-01')

    emul = _header('series', day, vhash, variant, t_abs, window)
    for col, key, kind in SERIES_COLUMNS:
        emul[key] = [rnd(v, kind) for v in _num(rows, col)]
    emul['changed'] = changed_flags(rows)
    for key in PRED_SERIES_KEYS:
        emul[key] = [None] * n
    if pred:
        found = 0
        for i, ti in enumerate(t_abs):
            f = pred_lookup(pred, ti)
            levels = _levels(f) if f else None
            if levels is None:
                continue
            found += 1
            emul['eventual_bg'][i] = levels.get('eventualBG')
            emul['min_pred_bg'][i] = levels.get('minPredBG')
            emul['min_guard'][i] = levels.get('minGuardBG1')
            emul['smb_off'][i] = levels.get('type') if levels.get('SMBoff') else None
        if found < n:
            emul['flags'].append('pred_partial:%d/%d' % (found, n))
    else:
        emul['flags'].append('no_pred')

    orig = _header('orig', day, contracts.BASE_VHASH, 'phone', t_abs, window)
    bg = bg_of(rows)
    orig['bg'] = [rnd(v, 'bg') for v in bg]
    for col, key, kind in ORIG_COLUMNS:
        orig[key] = [rnd(v, kind) for v in _num(rows, col)]
    orig['low_ahead'], orig['high_ahead'] = hindsight(t_abs, bg)
    orig['events'] = sorted(events_from_rows(rows, day_start(day)) + list(events or []),
                            key=lambda e: (e.get('t') or 0, e.get('kind', '')))
    return emul, orig


def series_from_csv(path, **kw):
    """series_from_rows() over a result CSV file."""
    names, rows = read_result_csv(path)
    return series_from_rows(names, rows, **kw)


def pred_from_globals(day, vhash, rows, pred, flowchart=None, transcript=None, variant='base', window=None):
    """(pred_doc, text_doc) for pred/<day>/<vhash>.json and .text.json from emulator_core.Pred (+ FlowChart) read
    right after parameters_known(), and the variant's .txt transcript (path or parsed blocks) for reason/console."""
    t_abs = loop_times(rows)
    n = len(rows)
    doc = _header('pred', day, vhash, variant, t_abs, window)
    for key in ('iob', 'zt', 'uam', 'cob', 'levels', 'factors', 'isf', 'iob_th', 'reason'):
        doc[key] = [None] * n
    text = _header('pred_text', day, vhash, variant, t_abs, window)
    text['console'] = [None] * n
    text['flows'] = [None] * n
    strings, boxes = _Table(), _Table()
    blocks = _blocks_arg(transcript, t_abs)
    finals = _num(rows, 'final ISF emul') if rows and 'final ISF emul' in rows[0] else [None] * n
    cobs = _num(rows, 'cob') if rows and 'cob' in rows[0] else [None] * n
    found = 0
    for i, ti in enumerate(t_abs):
        f = pred_lookup(pred, ti)
        if isinstance(f, dict):
            found += 1
            for key, src in EMUL_PRED_ARRAYS:
                doc[key][i] = _bg_list(f.get(src))
            if cobs[i] is not None and cobs[i] <= 0:
                doc['cob'][i] = None                         # no COB, no COB prediction (the phone's predBGs behave so)
            doc['levels'][i] = _levels(f)
            doc['factors'][i] = _factors(f, finals[i])
            doc['isf'][i] = rnd(f.get('emulISF'), 'bg')
            th = f.get('emuliobTH')
            doc['iob_th'][i] = rnd(th / 10.0, 'insulin') if isinstance(th, (int, float)) else None   # the core scales x10 for plotting
        if flowchart:
            flows = pred_lookup(flowchart, ti)
            if isinstance(flows, list):
                text['flows'][i] = [boxes.add([str(b.get('title', '')), str(b.get('indent', '')), str(b.get('adr', ''))])
                                    for b in flows]
        b = blocks[i]
        if b:
            doc['reason'][i] = b['reason'] or None
            text['console'][i] = [strings.add(line) for line in b['console']] or None
    text['strings'], text['boxes'] = strings.items, boxes.items
    if found < n:
        doc['flags'].append('pred_partial:%d/%d' % (found, n))
        text['flags'].append('pred_partial:%d/%d' % (found, n))
    return doc, text


def pred_orig_from_globals(day, rows, pred, transcript=None, window=None):
    """(pred_orig_doc, text_doc) for pred/<day>/orig.json and orig.text.json: the phone's predBGs (IOB/ZT/UAM/COB
    keys of Pred) and the .orig.txt reason / consoleError."""
    t_abs = loop_times(rows)
    n = len(rows)
    doc = _header('pred_orig', day, contracts.BASE_VHASH, 'phone', t_abs, window)
    for key in ('iob', 'zt', 'uam', 'cob', 'reason'):
        doc[key] = [None] * n
    text = _header('pred_orig_text', day, contracts.BASE_VHASH, 'phone', t_abs, window)
    text['console'] = [None] * n
    strings = _Table()
    blocks = _blocks_arg(transcript, t_abs)
    for i, ti in enumerate(t_abs):
        f = pred_lookup(pred, ti)
        if isinstance(f, dict):
            for key, src in ORIG_PRED_ARRAYS:
                doc[key][i] = _bg_list(f.get(src))
        b = blocks[i]
        if b:
            doc['reason'][i] = b['reason'] or None
            text['console'][i] = [strings.add(line) for line in b['console']] or None
    text['strings'] = strings.items
    return doc, text


def expand_text(text_doc, i):
    """(console lines, flow boxes) of loop i from a *.text.json document (None where absent)."""
    console = text_doc.get('console', [None] * len(text_doc['t']))[i]
    flows = text_doc.get('flows', [None] * len(text_doc['t']))[i]
    lines = [text_doc['strings'][k] for k in console] if console is not None else None
    boxes = [text_doc['boxes'][k] for k in flows] if flows is not None else None
    return lines, boxes


# ----------------------------------------------------------------------------- validation and I/O

def validate_series(doc):
    """Problems of a tier-2 document (empty list = fine): header, list lengths, no NaN."""
    problems = []
    if not isinstance(doc, dict):
        return ['document is not an object']
    for k in ('format', 'kind', 'day', 'vhash', 'variant', 'loops', 't'):
        if k not in doc:
            problems.append('missing %s' % k)
    if problems:
        return problems
    if doc['kind'] not in KINDS:
        problems.append('unknown kind %r' % doc['kind'])
    n = doc['loops']
    if len(doc['t']) != n:
        problems.append('t has %d entries, loops says %d' % (len(doc['t']), n))
    if any(a is None or b is None or b < a for a, b in zip(doc['t'], doc['t'][1:])):
        problems.append('t is not ascending')
    for k, v in doc.items():
        if isinstance(v, list) and k not in ('t', 'events', 'flags', 'window') + TABLE_KEYS and len(v) != n:
            problems.append('%s has %d entries, expected %d' % (k, len(v), n))
    if doc['kind'] == 'series':
        for k in [key for _, key, _ in SERIES_COLUMNS] + ['changed'] + list(PRED_SERIES_KEYS):
            if k not in doc:
                problems.append('missing series key %s' % k)
    if doc['kind'] == 'orig':
        for k in [key for _, key, _ in ORIG_COLUMNS] + ['bg', 'events', 'low_ahead', 'high_ahead']:
            if k not in doc:
                problems.append('missing orig key %s' % k)
    if doc['kind'] in ('pred_text', 'pred_orig_text'):
        strings = doc.get('strings')
        boxes = doc.get('boxes', [])
        if not isinstance(strings, list) or 'console' not in doc:
            problems.append('text document needs strings and console')
        else:
            for name, table in (('console', strings), ('flows', boxes)):
                for i, refs in enumerate(doc.get(name) or []):
                    if refs is not None and any(not isinstance(r, int) or r < 0 or r >= len(table) for r in refs):
                        problems.append('%s[%d] indexes outside its table' % (name, i))
                        break
    try:
        json.dumps(doc, allow_nan=False)
    except ValueError as e:
        problems.append('not JSON-serialisable without NaN: %s' % e)
    return problems


_SCRIPT_OPENERS = re.compile(r'<(?=!--|script)', re.IGNORECASE)


def embed_json(obj):
    """Compact JSON safe inside <script type="application/json">: allow_nan=False, '</' escaped as '<\\/', and
    '<!--' and '<script' (any case) escaped as '\\u003c!--' / '\\u003cscript', because '<!--' followed by '<script'
    puts the HTML parser into the double-escaped state where '</script>' no longer closes the element. A '<' can
    only occur inside a JSON string, where both escapes are plain JSON, so JSON.parse reads the value back."""
    text = json.dumps(obj, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return _SCRIPT_OPENERS.sub(r'\\u003c', text.replace('</', '<\\/'))


def script_tag(element_id, obj):
    return '<script type="application/json" id="%s">%s</script>' % (element_id, embed_json(obj))


def write_json(path, obj):
    """Compact JSON by atomic rename; returns the byte size written."""
    text = json.dumps(obj, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    data = text.encode('utf-8')
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, path)
    return len(data)


def tier2_path(report_dir, kind, day, vhash=None):
    """report_dir/<TIER2_PATHS[kind]> for kind in series | orig | pred | pred_text | pred_orig | pred_orig_text."""
    return os.path.join(report_dir, TIER2_PATHS[kind].format(day=day, vhash=vhash or contracts.BASE_VHASH))


def write_tier2(report_dir, doc, overwrite=True):
    """Write a tier-2 document to its place under report_dir; returns (path, bytes or None when kept).
    `overwrite=False` keeps an existing file (the orig documents are written once per day)."""
    problems = validate_series(doc)
    if problems:
        raise contracts.ContractError('tier-2 document: ' + '; '.join(problems))
    path = tier2_path(report_dir, doc['kind'], doc['day'], doc['vhash'])
    if not overwrite and os.path.isfile(path):
        return path, None
    return path, write_json(path, doc)


def read_series(path):
    """Read and validate a tier-2 document (any kind)."""
    with open(path, 'r', encoding='utf-8') as fh:
        doc = json.load(fh)
    problems = validate_series(doc)
    if problems:
        raise contracts.ContractError('%s: %s' % (path, '; '.join(problems)))
    return doc


# ----------------------------------------------------------------------------- tier 1

def build_tier1(study, trials, status=None, rounds=None, days=None, generated=None):
    """The embedded document of report/index.html. `study` is a contracts.Study, `trials` the TrialRow list,
    `status` a Status, `rounds` Round objects, `days` {day: frozen meta dict}. Everything the page needs that is
    not a per-loop series; the tier-2 files are addressed through the templates under `paths`."""
    return {
        'format': FORMAT, 'contract_version': contracts.CONTRACT_VERSION,
        'generated': generated or contracts.now_iso(),
        'honesty': contracts.HONESTY_STATEMENT,
        'not_testable': list(contracts.NOT_TESTABLE),
        'locked': sorted('profile ' + k for k in contracts.LOCKED_PROFILE_KEYS) + ['new_parameter *'],
        'study': study.to_dict() if study is not None else None,
        'status': status.to_dict() if status is not None else None,
        'rounds': [r.to_dict() for r in (rounds or [])],
        'trials': [t.to_dict() for t in (trials or [])],
        'days': dict(days or {}),
        'base_vhash': contracts.BASE_VHASH,
        'paths': dict(TIER2_PATHS),
    }
