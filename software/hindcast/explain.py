"""
explain.py  -  the loop drill-down as text: `hc explain <study> <iso> --variant <vhash>` (backlog H-502; DESIGN.md
section 6 page 4 and 7.2)

    hc explain autoISF_oct 2026-09-06T09:00:37Z --variant a91f           [--day D] [--console] [--flows] [--report-dir R]
    python -m hindcast.explain <study> <iso> ...

    from hindcast import explain
    ex = explain.explain(report_dir, '2026-09-06T06:08:00Z', vhash)       # LoopExplanation (data)
    print(explain.render(ex))                                             # the text, honesty statement last

Reads only the tier-2 files export.py writes under studies/<study>/report/ (never a CSV or a transcript; the
directory is --report-dir, else <study>/report when it holds series/, else <study> itself, resolve_report_dir()):
series/<day>/<vhash>.json and orig.json (always), series/<day>/<base vhash>.json (the base replay, when present),
pred/<day>/<vhash>.json + .text.json and pred/<day>/orig.json + .text.json (finalists only, or runs with --pred;
without them the reason diff, the prediction fans, the console lines and the flow boxes are reported as missing,
nothing fails). The loop is the one whose `t` is nearest the given time within TOL_S; a time after midnight is also
looked for in the previous UTC day's documents (loops past 24:00 belong to the day they started in).

The text (render()): a header (time, BG with the 5-min delta, IOB, COB, target, profile ISF, autosens, dura; the
positional badges "a recorded low followed within 2 h" / "a high followed within 3 h" and the orig events near the
loop); the decision pair phone | base replay | variant (insulinReq, SMB, maxBolus, TBR, final ISF factor, ISF,
eventualBG, minPredBG, minGuardBG, SMB-off reason; differing cells marked with *); the autoISF factor chain
(acce, bg, pp, dura, drift -> final) of the variant and of the base; a summary of both prediction fans (first, min,
last of IOB / ZT / UAM / COB); the word-level diff of the phone's reason and the variant's (`[-removed-]`,
`{+added+}`); with --console the console lines of both sides with their line diff; with --flows the flowchart
boxes with their JS references; the honesty statement last. Numbers are the files' (mg/dl, U); BG also in mmol/l.

Vocabulary: the module's own strings follow DESIGN.md section 3 (a test lints the rendering of the fixture); the
reason and console texts are the loop's own words and are printed as data.
"""
import argparse
import datetime
import difflib
import glob
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import export                                                                 # noqa: E402
from hindcast import study as st                                                            # noqa: E402
from hindcast.contracts import BASE_VHASH, HONESTY_STATEMENT                                # noqa: E402

TOL_S = 90.0                              # the nearest loop must lie within this of the asked time
EVENT_WINDOW_S = 30 * 60                  # orig events listed when within this of the loop
MGDL_PER_MMOL = export.MGDL_PER_MMOL
DECISION_ROWS = (('insulinReq', 'ins_req', 'U'), ('SMB', 'smb', 'U'), ('maxBolus', 'max_bolus', 'U'),
                 ('TBR', 'tbr', 'U/h'), ('final ISF factor', 'isf_factor', ''), ('ISF', 'isf', 'mg/dl per U'),
                 ('eventualBG', 'eventual_bg', 'mg/dl'), ('minPredBG', 'min_pred_bg', 'mg/dl'),
                 ('minGuardBG', 'min_guard', 'mg/dl'), ('SMB off', 'smb_off', ''))
FACTOR_ORDER = ('acce', 'bg', 'pp', 'dura', 'drift')
SERIES_FACTORS = {'acce': 'f_acce', 'bg': 'f_bg', 'pp': 'f_pp', 'dura': 'f_dura', 'drift': 'f_drift'}
FANS = ('iob', 'zt', 'uam', 'cob')


class ExplainError(Exception):
    """No document / no loop for the asked time and variant."""


# ----------------------------------------------------------------------------- time and files

def parse_when(text, day=None):
    """ISO 'YYYY-MM-DDTHH:MM[:SS[.fff]][Z|+hh:mm]' or 'HH:MM[:SS]' (with `day`) -> unix seconds (UTC when no offset)."""
    s = text.strip()
    if 'T' not in s and ' ' not in s and day:
        s = f'{day}T{s}'
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise ExplainError(f'cannot read the time {text!r}: give an ISO stamp such as 2026-09-06T09:00:37Z') from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def resolve_vhash(report_dir, day, text):
    """A full or short vhash (prefix), 'base' or '' -> the full vhash of a series file of that day."""
    if not text or text in ('base', 'phone'):
        return BASE_VHASH
    if len(text) == 40:
        return text
    hits = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(report_dir, 'series', day, text + '*.json'))
                  if not os.path.basename(p).startswith('orig'))
    if len(hits) != 1:
        raise ExplainError(f'variant {text!r}: {len(hits)} series files match under {os.path.join(report_dir, "series", day)}')
    return hits[0]


def _load(path):
    if not os.path.isfile(path):
        return None
    try:
        return export.read_series(path)
    except (OSError, ValueError, contracts.ContractError):
        return None


def load_docs(report_dir, day, vhash):
    """{'series', 'orig', 'base', 'pred', 'pred_text', 'pred_orig', 'pred_orig_text', 'pred_base'} (None when missing)."""
    p = lambda kind, vh=None: export.tier2_path(report_dir, kind, day, vh)   # noqa: E731
    return {'series': _load(p('series', vhash)), 'orig': _load(p('orig')),
            'base': _load(p('series', BASE_VHASH)) if vhash != BASE_VHASH else None,
            'pred': _load(p('pred', vhash)), 'pred_text': _load(p('pred_text', vhash)),
            'pred_orig': _load(p('pred_orig')), 'pred_orig_text': _load(p('pred_orig_text')),
            'pred_base': _load(p('pred', BASE_VHASH)) if vhash != BASE_VHASH else None}


def find_loop(doc, t_rel, tol_s=TOL_S):
    """Index of the loop whose `t` is nearest t_rel (seconds since the day's 00:00 UTC) within tol_s, else None."""
    ts = doc.get('t') or []
    if not ts:
        return None
    i = min(range(len(ts)), key=lambda k: abs(ts[k] - t_rel))
    return i if abs(ts[i] - t_rel) <= tol_s else None


# ----------------------------------------------------------------------------- the explanation

@dataclass
class LoopExplanation:
    day: str
    vhash: str
    variant: str
    index: int
    loops: int
    t_abs: float
    iso: str
    header: Dict[str, Any] = field(default_factory=dict)
    badges: List[str] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    phone: Dict[str, Any] = field(default_factory=dict)
    base: Optional[Dict[str, Any]] = None
    emul: Dict[str, Any] = field(default_factory=dict)
    factors: Dict[str, Optional[float]] = field(default_factory=dict)
    factors_base: Dict[str, Optional[float]] = field(default_factory=dict)
    fans: Dict[str, Dict[str, Optional[list]]] = field(default_factory=dict)      # side -> fan -> values
    levels: Optional[dict] = None
    reason_phone: Optional[str] = None
    reason_emul: Optional[str] = None
    reason_base: Optional[str] = None
    console_phone: Optional[List[str]] = None
    console_emul: Optional[List[str]] = None
    flows: Optional[List[list]] = None
    has_pred: bool = False                       # a prediction file of either side was found
    missing: List[str] = field(default_factory=list)

    @property
    def differs(self):
        """The decision rows where the variant differs from the phone (rows the phone's side does not carry, such as
        its levels, are not differences)."""
        return [label for label, key, _ in DECISION_ROWS
                if self.phone.get(key) is not None and _differ(self.phone.get(key), self.emul.get(key))]

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


def _differ(a, b, tol=0.005):
    if a is None or b is None:
        return (a is None) != (b is None)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) > tol
    return a != b


def _at(doc, key, i):
    v = doc.get(key) if doc else None
    return v[i] if isinstance(v, list) and i < len(v) else None


def _decision(doc, i):
    return {key: _at(doc, key, i) for _, key, _ in DECISION_ROWS}


def _factors_of(series, pred, i):
    out = {}
    f = _at(pred, 'factors', i) if pred else None
    for name in FACTOR_ORDER:
        v = f.get(name) if isinstance(f, dict) else None
        out[name] = v if v is not None else _at(series, SERIES_FACTORS[name], i)
    out['final'] = (f.get('final') if isinstance(f, dict) and f.get('final') is not None else _at(series, 'isf_factor', i))
    out['delta'] = f.get('delta') if isinstance(f, dict) else None
    return out


def word_diff(a, b):
    """Word-level diff of two texts -> [(op, token)] with op in '=', '-', '+' (difflib on whitespace tokens)."""
    ta, tb = (a or '').split(), (b or '').split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ta, b=tb, autojunk=False).get_opcodes():
        if tag == 'equal':
            out += [('=', w) for w in ta[i1:i2]]
        else:
            out += [('-', w) for w in ta[i1:i2]]
            out += [('+', w) for w in tb[j1:j2]]
    return out


def render_diff(ops):
    """[-removed-] {+added+} rendering; runs of the same op are joined."""
    parts, cur_op, cur = [], None, []

    def flush():
        if not cur:
            return
        text = ' '.join(cur)
        parts.append(text if cur_op == '=' else (f'[-{text}-]' if cur_op == '-' else f'{{+{text}+}}'))
    for op, w in ops:
        if op != cur_op:
            flush()
            cur_op, cur = op, []
        cur.append(w)
    flush()
    return ' '.join(parts)


def explain(report_dir, when, vhash=BASE_VHASH, day=None, tol_s=TOL_S):
    """LoopExplanation for the loop nearest `when` (unix seconds or ISO text) of `vhash` (full / short / 'base')."""
    t_abs = parse_when(when, day) if isinstance(when, str) else float(when)
    days = [day] if day else [export.day_of(t_abs), export.day_of(t_abs - 86400)]
    last_error = None
    for d in days:
        try:
            vh = resolve_vhash(report_dir, d, vhash)
        except ExplainError as e:
            last_error = e
            continue
        docs = load_docs(report_dir, d, vh)
        series = docs['series']
        if series is None:
            last_error = ExplainError(f'no series file for variant {contracts.short(vh)} on {d} under {report_dir}')
            continue
        i = find_loop(series, t_abs - export.day_start(d), tol_s)
        if i is None:
            last_error = ExplainError(f'no loop within {tol_s:.0f} s of {export.iso_of(t_abs)} in {series["loops"]} loops of {d}')
            continue
        return _build(d, vh, docs, i, t_abs)
    raise last_error or ExplainError('nothing to explain')


def _build(day, vhash, docs, i, t_asked):
    series, orig, base = docs['series'], docs['orig'], docs['base']
    pred, pred_text, pred_orig, pred_orig_text, pred_base = (docs['pred'], docs['pred_text'], docs['pred_orig'],
                                                              docs['pred_orig_text'], docs['pred_base'])
    t_rel = series['t'][i]
    t_abs = export.day_start(day) + t_rel
    ex = LoopExplanation(day=day, vhash=vhash, variant=series.get('variant', ''), index=i, loops=series['loops'],
                         t_abs=t_abs, iso=export.iso_of(t_abs))
    if orig is None:
        ex.missing.append('series/%s/orig.json (the phone\'s side)' % day)
    oi = find_loop(orig, t_rel, 1.0) if orig else None
    if orig is not None and oi is None:
        ex.missing.append('the phone\'s row for this loop in orig.json')
    bg = _at(orig, 'bg', oi) if oi is not None else None
    prev = None
    if oi is not None and oi > 0:
        for k in range(oi - 1, -1, -1):
            if orig['t'][oi] - orig['t'][k] >= 270:
                prev = _at(orig, 'bg', k)
                break
    ex.header = {'bg': bg, 'bg_delta_5m': (bg - prev) if bg is not None and prev is not None else None,
                 'iob': _at(orig, 'iob', oi), 'cob': _at(orig, 'cob', oi), 'act': _at(orig, 'act', oi),
                 'target_lo': _at(orig, 'tar_lo', oi), 'target_hi': _at(orig, 'tar_hi', oi),
                 'isf_prof': _at(orig, 'isf_prof', oi), 'autosens': _at(orig, 'autosens', oi),
                 'dura_min': _at(orig, 'dura_min', oi), 'dura_avg': _at(orig, 'dura_avg', oi),
                 'target_emul': (_at(series, 'tar_lo', i), _at(series, 'tar_hi', i)), 'changed': _at(series, 'changed', i),
                 'asked': export.iso_of(t_asked)}
    if oi is not None:
        if _at(orig, 'low_ahead', oi):
            ex.badges.append('a recorded low (below 3.9 mmol/l) followed within 2 h - positional, not a score')
        if _at(orig, 'high_ahead', oi):
            ex.badges.append('a recorded high (above 180 mg/dl) followed within 3 h - positional, not a score')
        ex.events = [e for e in orig.get('events') or [] if isinstance(e.get('t'), (int, float))
                     and abs(e['t'] - t_rel) <= EVENT_WINDOW_S]
    ex.phone = _decision(orig, oi) if oi is not None else {}
    for key in ('eventual_bg', 'min_pred_bg', 'min_guard', 'smb_off'):
        ex.phone[key] = None                                  # the phone's levels live only in its reason text
    ex.emul = _decision(series, i)
    if base is not None:
        bi = find_loop(base, t_rel, 1.0)
        ex.base = _decision(base, bi) if bi is not None else None
        ex.factors_base = _factors_of(base, pred_base, bi) if bi is not None else {}
    ex.factors = _factors_of(series, pred, i)
    ex.has_pred = pred is not None or (pred_orig is not None and oi is not None)
    if pred is not None:
        ex.fans['variant'] = {fan: _at(pred, fan, i) for fan in FANS}
        ex.levels = _at(pred, 'levels', i)
        ex.reason_emul = _at(pred, 'reason', i)
    else:
        ex.missing.append('pred/%s/%s.json (predictions, levels and reason of the variant: finalists only, or --pred)'
                          % (day, contracts.short(vhash)))
    if pred_base is not None and base is not None and ex.base is not None:
        ex.reason_base = _at(pred_base, 'reason', find_loop(base, t_rel, 1.0))
    if pred_orig is not None and oi is not None:
        ex.fans['phone'] = {fan: _at(pred_orig, fan, oi) for fan in FANS}
        ex.reason_phone = _at(pred_orig, 'reason', oi)
    else:
        ex.missing.append('pred/%s/orig.json (the phone\'s predictions and reason)' % day)
    if pred_text is not None:
        lines, boxes = export.expand_text(pred_text, i)
        ex.console_emul, ex.flows = lines, boxes
    if pred_orig_text is not None and oi is not None:
        ex.console_phone, _ = export.expand_text(pred_orig_text, oi)
    return ex


# ----------------------------------------------------------------------------- rendering

def _mmol(v):
    return None if v is None else v / MGDL_PER_MMOL


def _bgtxt(v):
    return '-' if v is None else f'{v:.0f} mg/dl ({_mmol(v):.1f} mmol/l)'


def _cell(v, unit):
    if v is None:
        return '-'
    if isinstance(v, str):
        return v
    if unit == 'mg/dl':
        return f'{v:.0f}'
    if unit in ('U', 'U/h'):
        return f'{v:.2f}'
    return f'{v:.2f}' if isinstance(v, float) else str(v)


def _fan_summary(values):
    if not values:
        return 'none'
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return 'none'
    return f'{vals[0]:.0f} -> min {min(vals):.0f} -> {vals[-1]:.0f} mg/dl over {len(vals)} steps'


def _factor_chain(f):
    if not f or all(v is None for v in f.values()):
        return 'not available'
    parts = [f'{name} {f[name]:.2f}' if f.get(name) is not None else f'{name} -' for name in FACTOR_ORDER]
    final = f.get('final')
    return ' -> '.join(parts) + f' => final {final:.2f}' if final is not None else ' -> '.join(parts) + ' => final -'


def render(ex, console=False, flows=False):
    """The drill-down as text; the honesty statement last."""
    h = ex.header
    out = [f'loop {ex.iso} (loop {ex.index + 1} of {ex.loops} on {ex.day}; asked {h.get("asked", "")}), variant '
           f'{ex.variant} [{contracts.short(ex.vhash)}]' + ('' if ex.vhash != BASE_VHASH else ' = the base replay')]
    delta = h.get('bg_delta_5m')
    out.append(f'  BG {_bgtxt(h.get("bg"))}' + (f', {delta:+.0f} mg/dl vs 5 min earlier' if delta is not None else '')
               + (f'; IOB {h["iob"]:.2f} U' if h.get('iob') is not None else '')
               + (f'; COB {h["cob"]:.0f} g' if h.get('cob') is not None else '')
               + (f'; target {_mmol(h["target_lo"]):.1f}-{_mmol(h["target_hi"]):.1f} mmol/l' if h.get('target_lo') is not None else ''))
    out.append((f'  profile ISF {h["isf_prof"]:.1f} mg/dl per U' if h.get('isf_prof') is not None else '  profile ISF -')
               + (f'; autosens {h["autosens"]:.2f}' if h.get('autosens') is not None else '')
               + (f'; dura {h["dura_min"]:.0f} min around {_mmol(h["dura_avg"]):.1f} mmol/l' if h.get('dura_min') is not None and h.get('dura_avg') is not None else '')
               + (f'; decision changed vs the phone: {"yes" if h.get("changed") else "no"}' if h.get('changed') is not None else ''))
    for b in ex.badges:
        out.append(f'  badge: {b}')
    for e in ex.events:
        out.append(f'  event {e.get("kind")}: {e.get("label")} (t {e.get("t")})')
    out += ['', 'decision pair (phone | base replay | variant; * = variant differs from the phone)']
    cols = ['phone', 'base', 'variant'] if ex.base is not None else ['phone', 'variant']
    out.append('  %-18s %s' % ('', ' '.join(f'{c:>14}' for c in cols)))
    for label, key, unit in DECISION_ROWS:
        vals = [ex.phone.get(key)] + ([ex.base.get(key)] if ex.base is not None else []) + [ex.emul.get(key)]
        mark = '*' if _differ(ex.phone.get(key), ex.emul.get(key)) and ex.phone.get(key) is not None else ' '
        out.append('  %-18s %s %s' % (f'{label} {unit}'.strip(), ' '.join(f'{_cell(v, unit):>14}' for v in vals), mark))
    out += ['', 'autoISF factor chain']
    out.append('  variant: ' + _factor_chain(ex.factors))
    if ex.factors_base:
        out.append('  base:    ' + _factor_chain(ex.factors_base))
    if ex.levels:
        lv = ex.levels
        bits = [f'{k} {lv[k]}' for k in ('eventualBG', 'minPredBG', 'minIOBPredBG', 'minZTGuardBG', 'minUAMPredBG',
                                           'minCOBPredBG', 'threshold') if lv.get(k) is not None]
        if lv.get('SMBoff'):
            bits.append(f'SMB off: {lv.get("type")} ({lv.get("SMBoff")})')
        out.append('  levels: ' + ', '.join(bits))
    if ex.fans:
        out += ['', 'prediction fans (first -> min -> last)']
        for side in ('phone', 'variant'):
            if side in ex.fans:
                for fan in FANS:
                    out.append(f'  {side:<8} {fan.upper():<4} {_fan_summary(ex.fans[side].get(fan))}')
    out += ['', 'reason']
    if ex.reason_phone is not None or ex.reason_emul is not None:
        out.append('  phone:   ' + (ex.reason_phone or '-'))
        out.append('  variant: ' + (ex.reason_emul or '-'))
        if ex.reason_phone and ex.reason_emul:
            out.append('  diff:    ' + render_diff(word_diff(ex.reason_phone, ex.reason_emul)))
    elif ex.has_pred:
        out.append('  not recorded (the prediction files of this loop carry no reason text)')
    else:
        out.append('  not available (no prediction files for this loop)')
    if console:
        out += ['', 'console lines (variant; lines the phone did not print marked +, lines only the phone printed marked -)']
        if ex.console_emul is None:
            out.append('  not available')
        else:
            phone_set = set(ex.console_phone or [])
            for line in ex.console_emul:
                out.append(('  + ' if ex.console_phone is not None and line not in phone_set else '    ') + line)
            for line in ex.console_phone or []:
                if line not in set(ex.console_emul):
                    out.append('  - ' + line)
    if flows:
        out += ['', 'flowchart boxes (title | indent | JS ref)']
        if not ex.flows:
            out.append('  not available')
        else:
            for title, indent, adr in ex.flows:
                try:
                    depth = int(str(indent).lstrip('+'))
                except ValueError:
                    depth = 0
                out.append('  ' + '  ' * max(0, depth) + str(title).replace('\n', ' ') + f'   [{adr}]')
    if ex.missing:
        out += ['', 'missing: ' + '; '.join(ex.missing)]
    out += ['', HONESTY_STATEMENT]
    return '\n'.join(out) + '\n'


# ----------------------------------------------------------------------------- CLI

def _default_vhash(sd):
    """The first vhash of round.json's front, else base."""
    rnd = sd.read_round() if sd is not None else None
    if rnd is not None:
        for vh in rnd.front:
            if vh != BASE_VHASH:
                return vh
    return BASE_VHASH


def resolve_report_dir(sd, report_dir=None):
    """Where the tier-2 documents of a study live: `report_dir` (--report-dir) when given, else <study>/report when
    it holds series/, else the study directory itself when it holds series/ (the committed fixture study keeps
    series/ at its root), else <study>/report (so the error names the default place)."""
    if report_dir is not None:
        return report_dir
    for cand in (sd.report_dir, sd.path):
        if os.path.isdir(os.path.join(cand, 'series')):
            return cand
    return sd.report_dir


def register(subparsers):
    """Add the `explain` subcommand (cli.py's convention: `func(args) -> int`)."""
    p = subparsers.add_parser('explain', help='the drill-down of one loop as text (decision pair, factor chain, reason diff)')
    p.add_argument('name', help='study name or directory (its report/ holds the series and pred files)')
    p.add_argument('when', help='loop time, ISO UTC such as 2026-09-06T09:00:37Z (or HH:MM with --day)')
    p.add_argument('--variant', default=None, help='vhash (full or short prefix) or "base"; default: the first of round.json\'s front')
    p.add_argument('--day', default=None, help='UTC day of the documents (default: from the time stamp)')
    p.add_argument('--report-dir', default=None, help='the report directory (default: studies/<study>/report if it holds series/, else the study directory)')
    p.add_argument('--console', action='store_true', help='also print the console lines')
    p.add_argument('--flows', action='store_true', help='also print the flowchart boxes')
    p.add_argument('--json', action='store_true', help='print the explanation as JSON instead of text')
    p.add_argument('--studies-dir', default=st.STUDIES_DIR)
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH)
    p.set_defaults(func=run_explain)
    return p


def run_explain(args):
    """`hc explain <study> <iso> --variant <vhash>`: 0 printed, 3 nothing to explain (missing file / loop)."""
    sd = None
    report_dir = args.report_dir
    try:
        if report_dir is None:
            sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
            sd.require()
            report_dir = resolve_report_dir(sd)
        elif os.path.isdir(os.path.join(args.studies_dir, args.name)) or os.path.isdir(args.name):
            sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
        vhash = args.variant if args.variant else _default_vhash(sd)
        ex = explain(report_dir, args.when, vhash, day=args.day)
    except (st.StudyError, ExplainError, OSError, contracts.ContractError) as e:
        sys.stderr.write(f'hc explain: {e}\n')
        return st.EXIT_REFUSED
    if getattr(args, 'json', False):
        sys.stdout.write(json.dumps(ex.to_dict(), ensure_ascii=False, indent=1, allow_nan=False) + '\n')
    else:
        sys.stdout.write(render(ex, console=args.console, flows=args.flows))
    return st.EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.explain', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(['explain'] + list(sys.argv[1:] if argv is None else argv))
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
