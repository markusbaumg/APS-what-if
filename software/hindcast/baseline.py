"""
baseline.py  -  the observed baseline toward the end goal (docs/hindcast/GOAL.md, `hc baseline`)

GOAL.md asks for a loop that is more aggressive and quicker, fully closed (no manual boluses) with time in range
above 90 %. Hindcast can never compute a counterfactual glucose statistic; what it can do is report what the
phone's own settings produced, from the Nightscout day cache, as the OBSERVED baseline every later step is
compared against once a setting has run on the phone. Every value here is a fact about the past and is labelled
'observed'; none of it enters a score, a ranking or a card (DESIGN.md section 3).

    from hindcast import baseline
    baseline.day_stats('2026-09-06')            -> the observed record of one UTC day (see DAY_KEYS)
    baseline.build(['2026-09-06', ...])         -> {'label': 'observed', 'days': [...], 'summary': {...}, ...}
    baseline.render_markdown(b)                 -> BASELINE.md as a string (linted, kind 'baseline')
    baseline.strip_label(stats)                 -> 'observed TIR 87.3 %' for the day strip of the report

Inputs (read only, never fetched; stdlib only): <cache>/entries/<day>.json (sgv, mg/dl), <cache>/treatments/<day>.json
and <cache>/devicestatus/<day>.json (+ <day+1>, the upload lag; hindcast.days.day_loop_stamps). A record belongs
to the UTC day its timestamp falls in.

CGM (5-min grid). The UTC day is cut into 288 slots of 5 min; a slot's value is the mean of the sgv readings in it
(the Libre 3 sends one a minute), a slot without a reading is a gap. Percentages are shares of the CGM minutes
(covered slots x 5), gaps are excluded from the denominator and reported (`cgm_gap_min`, `cgm_coverage_pct`).
Ranges use the consensus mg/dl equivalents of the mmol/l limits (AAPS stores mg/dl):
    tir      3.9-10.0 mmol/l  = 70 <= v <= 180 mg/dl
    below    < 3.9            = v < 70
    below3   < 3.0            = v < 54
    above    > 10.0           = v > 180
    above139 > 13.9           = v > 250
mean glucose in mmol/l (mg/dl / 18, AAPS's factor), CV = population standard deviation / mean of the slot values.
A day with no entries file has every CGM value None and `cgm_note` 'no CGM file'.

Treatments (typing of this AAPS / Nightscout pair): SMB = type 'SMB' (eventType 'Correction Bolus'); manual bolus =
type 'NORMAL' with eventType 'Meal Bolus' or 'Correction Bolus' and insulin > 0 (the temp basals are type NORMAL
too and are not boluses); carb entry = any treatment with carbs > 0. Records with isValid False are skipped,
duplicates (same identifier / _id) count once.

Loop: `loop` is True when the cached devicestatus holds at least one loop run (openaps.suggested) inside the day,
`loop_runs` their number, `loop_hours` the hours they cover (each run stands for the time to the next one, at most
5 min); all None when the day has no devicestatus file. A day with a handful of runs is a day 'with a loop' but
not one that looped: the summary counts both (days_with_loop, days_looped_full = loop_hours >= 20).

Period summary: median and mean over the days that have the value (None excluded), plus the day counts.

Wording: every Markdown line passes hindcast.lint kind 'baseline' (the vocabulary rules; 'time in range' / TIR
only on a line that also says 'observed'), and the whole page carries the honesty statement.

Privacy: the page and the JSON are Nightscout-derived (glucose statistics, insulin units and bolus counts per day,
one person's data), so `--write` puts BOTH into the git-ignored studies directory: <studies dir>/BASELINE.md and
<studies dir>/baseline.json (default software/studies/, where report.find_baseline reads the JSON). The public
repository (docs/) is refused: `--write docs` (or a --docs-path under docs/) exits 3 unless `--public` is given,
and then a warning on stderr says that per-day glucose and insulin figures enter the public repository.

CLI:  hc baseline [DAYS] [--write [studies|docs]] [--public] [--json PATH] [--cache-dir DIR] [--frozen-dir DIR]
                  [--studies-dir DIR]
      DAYS defaults to the frozen days of the host inside DEFAULT_SPAN; without --write the page is printed.
"""
import argparse
import datetime
import json
import os
import statistics
import sys

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
REPO_DIR = os.path.dirname(SOFTWARE_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)

from hindcast import days as dy                                                             # noqa: E402
from hindcast import lint                                                                   # noqa: E402

__all__ = ['is_public_path', 'PAGE_NAME', 'LABEL', 'DEFAULT_SPAN', 'DAY_KEYS', 'SUMMARY_KEYS', 'day_stats', 'cgm_stats', 'treatment_stats',
           'build', 'summarize', 'render_markdown', 'lint_markdown', 'strip_label', 'default_days', 'register',
           'main']

LABEL = 'observed'
DEFAULT_SPAN = '2026-08-30..2026-10-16'                 # the frozen loop days of the first campaign
DOCS_DIR = os.path.join(REPO_DIR, 'docs')
DOCS_PATH = os.path.join(DOCS_DIR, 'hindcast', 'BASELINE.md')   # public: only with --public
PAGE_NAME = 'BASELINE.md'                               # <studies dir>/BASELINE.md, the default of --write
JSON_NAME = 'baseline.json'                             # what report.find_baseline looks for
PUBLIC_WARNING = ('hc baseline: WARNING: --public writes per-day glucose and insulin figures (observed TIR, mean '
                  'glucose, manual bolus and SMB units per day) into %s, inside the public repository; review the page '
                  'before you commit it')
PUBLIC_REFUSED = ('hc baseline: refused: %s is inside docs/, the public repository; the page carries per-day glucose '
                  'and insulin figures. Without --public it goes to the git-ignored studies directory (--write); '
                  'pass --public to write it there anyway')
GOAL_TIR_PCT = 90.0

SLOT_MIN = 5
SLOT_MS = SLOT_MIN * 60 * 1000
MGDL_PER_MMOL = 18.0
LOW_MGDL = 70                                           # < 3.9 mmol/l
VERY_LOW_MGDL = 54                                      # < 3.0 mmol/l
HIGH_MGDL = 180                                         # > 10.0 mmol/l
VERY_HIGH_MGDL = 250                                    # > 13.9 mmol/l
MANUAL_EVENT_TYPES = ('Meal Bolus', 'Correction Bolus')

CGM_KEYS = ('tir_pct', 'below_3_9_pct', 'below_3_0_pct', 'above_10_pct', 'above_13_9_pct', 'mean_mmol', 'cv_pct',
            'cgm_minutes', 'cgm_gap_min', 'cgm_coverage_pct')
TREATMENT_KEYS = ('manual_boluses', 'manual_bolus_U', 'smb_U', 'smb_count', 'carb_entries')
LOOP_KEYS = ('loop', 'loop_runs', 'loop_hours')
# the short names hindcast/report.py (observed_days, OBSERVED_KEYS) reads: alias -> key of this module
REPORT_ALIASES = {'tbr_pct': 'below_3_9_pct', 'manual_U': 'manual_bolus_U'}
DAY_KEYS = ('day', 'label') + CGM_KEYS + ('cgm_note',) + TREATMENT_KEYS + LOOP_KEYS + tuple(REPORT_ALIASES)
SUMMARY_KEYS = CGM_KEYS + TREATMENT_KEYS + ('loop_hours',)
FULL_LOOP_HOURS = 20.0                                  # a day 'looped all day' for the day counts


# ----------------------------------------------------------------------------- reading the cache

def _read_list(cache_root, coll, day):
    try:
        with open(os.path.join(cache_root, coll, day + '.json'), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _valid(rec):
    return isinstance(rec, dict) and rec.get('isValid', True) is not False


def _dedupe(records):
    seen, out = set(), []
    for rec in records:
        key = rec.get('identifier') or rec.get('_id')
        if key is None:
            key = (rec.get('date'), rec.get('created_at'), rec.get('type'), rec.get('eventType'), rec.get('insulin'),
                   rec.get('carbs'))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


# ----------------------------------------------------------------------------- CGM

def slot_values(cgm, start_ms, end_ms):
    """[mean mg/dl or None] per 5-min slot of [start, end) from (ms, sgv) pairs (hindcast.days.entries_in)."""
    n = (end_ms - start_ms) // SLOT_MS
    sums, counts = [0.0] * n, [0] * n
    seen = set()
    for ms, sgv in cgm:
        if ms in seen or not (start_ms <= ms < end_ms):
            continue
        seen.add(ms)
        i = (ms - start_ms) // SLOT_MS
        sums[i] += sgv
        counts[i] += 1
    return [sums[i] / counts[i] if counts[i] else None for i in range(n)]


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def cgm_stats(cgm, start_ms, end_ms):
    """The observed CGM numbers of [start, end) on the 5-min grid (see the module text); percentages None when
    there is no CGM minute."""
    slots = slot_values(cgm, start_ms, end_ms)
    vals = [v for v in slots if v is not None]
    n = len(vals)
    out = {
        'tir_pct': _pct(sum(1 for v in vals if LOW_MGDL <= v <= HIGH_MGDL), n),
        'below_3_9_pct': _pct(sum(1 for v in vals if v < LOW_MGDL), n),
        'below_3_0_pct': _pct(sum(1 for v in vals if v < VERY_LOW_MGDL), n),
        'above_10_pct': _pct(sum(1 for v in vals if v > HIGH_MGDL), n),
        'above_13_9_pct': _pct(sum(1 for v in vals if v > VERY_HIGH_MGDL), n),
        'mean_mmol': round(statistics.fmean(vals) / MGDL_PER_MMOL, 2) if n else None,
        'cv_pct': round(100.0 * statistics.pstdev(vals) / statistics.fmean(vals), 1) if n else None,
        'cgm_minutes': n * SLOT_MIN,
        'cgm_gap_min': (len(slots) - n) * SLOT_MIN,
        'cgm_coverage_pct': _pct(n, len(slots)),
    }
    return out


def _empty_cgm():
    return {k: None for k in CGM_KEYS}


# ----------------------------------------------------------------------------- treatments

def treatment_stats(treatments, start_ms, end_ms):
    """Manual boluses (count, units), SMB units and count, carb entries of the treatments inside [start, end)."""
    manual_n, manual_u, smb_u, smb_n, carbs_n = 0, 0.0, 0.0, 0, 0
    for t in _dedupe([t for t in treatments or [] if _valid(t)]):
        ms = dy._treatment_ms(t)
        if ms is None or not (start_ms <= ms < end_ms):
            continue
        insulin = t.get('insulin')
        insulin = float(insulin) if isinstance(insulin, (int, float)) else 0.0
        if t.get('type') == 'SMB' and insulin > 0:
            smb_u += insulin
            smb_n += 1
        elif t.get('type') == 'NORMAL' and t.get('eventType') in MANUAL_EVENT_TYPES and insulin > 0:
            manual_u += insulin
            manual_n += 1
        carbs = t.get('carbs')
        if isinstance(carbs, (int, float)) and carbs > 0:
            carbs_n += 1
    return {'manual_boluses': manual_n, 'manual_bolus_U': round(manual_u, 2), 'smb_U': round(smb_u, 2),
            'smb_count': smb_n, 'carb_entries': carbs_n}


# ----------------------------------------------------------------------------- one day

_CACHE_ROOT = None


def _default_cache_root():
    global _CACHE_ROOT
    if _CACHE_ROOT is None:
        _CACHE_ROOT = dy.cache_dir()
    return _CACHE_ROOT


def day_stats(day, cache_root=None):
    """The observed record of one UTC day (keys DAY_KEYS, 'label' = 'observed') from the Nightscout day cache.
    Read only; a missing file leaves its values None (CGM: cgm_note 'no CGM file'; treatments: all None;
    devicestatus: loop None)."""
    if not dy.is_day(day):
        raise ValueError('not a UTC day (YYYY-MM-DD): %r' % (day,))
    root = cache_root or _default_cache_root()
    start, end = dy.day_bounds(day)
    rec = {'day': day, 'label': LABEL}
    entries = _read_list(root, 'entries', day)
    if entries is None:
        rec.update(_empty_cgm())
        rec['cgm_note'] = dy.NO_CGM_FILE
    else:
        rec.update(cgm_stats(dy.entries_in([e for e in entries if _valid(e)], start, end), start, end))
        rec['cgm_note'] = None if rec['cgm_minutes'] else 'no CGM reading'
    treatments = _read_list(root, 'treatments', day)
    if treatments is None:
        rec.update({k: None for k in TREATMENT_KEYS})
    else:
        rec.update(treatment_stats(treatments, start, end))
    stamps = dy.day_loop_stamps(day, cache_root=root)
    rec['loop'] = None if stamps is None else bool(stamps)
    rec['loop_runs'] = None if stamps is None else len(stamps)
    rec['loop_hours'] = None if stamps is None else loop_hours(stamps, end)
    for alias, key in REPORT_ALIASES.items():
        rec[alias] = rec.get(key)
    return {k: rec.get(k) for k in DAY_KEYS}


def loop_hours(stamps, end_ms):
    """Hours of the day the phone looped; the one definition lives in hindcast.days.loop_hours (each run stands
    for the time to the next one, at most 5 min, the last one up to the day end)."""
    return dy.loop_hours(stamps, end_ms)


def strip_label(stats):
    """The day-strip text of the report: 'observed TIR 87.3 %' ('observed TIR n/a' without CGM)."""
    v = (stats or {}).get('tir_pct')
    return '%s TIR %s' % (LABEL, 'n/a' if v is None else '%.1f %%' % v)


# ----------------------------------------------------------------------------- the period

def summarize(rows):
    """{key: {'median', 'mean', 'days'}} over the rows that have the key (None excluded), plus the day counts."""
    out = {}
    for k in SUMMARY_KEYS:
        vals = [r[k] for r in rows if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
        out[k] = {'median': round(statistics.median(vals), 2) if vals else None,
                  'mean': round(statistics.fmean(vals), 2) if vals else None, 'days': len(vals)}
    out['days'] = len(rows)
    out['days_with_loop'] = sum(1 for r in rows if r.get('loop'))
    out['days_looped_full'] = sum(1 for r in rows if (r.get('loop_hours') or 0) >= FULL_LOOP_HOURS)
    out['days_with_cgm'] = sum(1 for r in rows if r.get('cgm_minutes'))
    out['days_without_manual_bolus'] = sum(1 for r in rows if r.get('manual_boluses') == 0)
    out['days_tir_over_goal'] = sum(1 for r in rows if (r.get('tir_pct') or 0) > GOAL_TIR_PCT)
    return out


def frozen_days(host=None, frozen_dir=None, span=DEFAULT_SPAN):
    """The days with a frozen meta under frozen/<host>/ inside `span` (ascending)."""
    if frozen_dir is None:
        base = os.environ.get('HINDCAST_FROZEN_DIR') or os.path.join(SOFTWARE_DIR, 'frozen')
        frozen_dir = os.path.join(base, host or dy.host_of())
    want = set(dy.parse_days(span)) if span else None
    try:
        names = os.listdir(frozen_dir)
    except OSError:
        return []
    out = sorted(n[:-len('.meta.json')] for n in names if n.endswith('.meta.json'))
    return [d for d in out if dy.is_day(d) and (want is None or d in want)]


def default_days(frozen_dir=None):
    """The frozen days inside DEFAULT_SPAN, or every day of DEFAULT_SPAN when none is frozen."""
    return frozen_days(frozen_dir=frozen_dir) or dy.parse_days(DEFAULT_SPAN)


def build(day_list, cache_root=None, source=None, now=None):
    """The observed baseline of `day_list`: {'label', 'generated', 'source', 'definitions', 'days', 'summary'}."""
    root = cache_root or _default_cache_root()
    rows = [day_stats(d, cache_root=root) for d in day_list]
    now = now or datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    return {
        'label': LABEL,
        'range_mmol': [round(LOW_MGDL / MGDL_PER_MMOL, 1), round(HIGH_MGDL / MGDL_PER_MMOL, 1)],
        'generated': now,
        'source': source or 'Nightscout day cache %s' % os.path.basename(os.path.normpath(root)),
        'definitions': {
            'grid_min': SLOT_MIN, 'tir_mgdl': [LOW_MGDL, HIGH_MGDL], 'below_3_9_mgdl': LOW_MGDL,
            'below_3_0_mgdl': VERY_LOW_MGDL, 'above_10_mgdl': HIGH_MGDL, 'above_13_9_mgdl': VERY_HIGH_MGDL,
            'mgdl_per_mmol': MGDL_PER_MMOL, 'manual_bolus': "type NORMAL, eventType %s, insulin > 0"
            % ' / '.join(MANUAL_EVENT_TYPES), 'smb': "type SMB", 'carb_entry': 'carbs > 0',
            'percent_of': 'CGM minutes (gaps excluded)'},
        'days': rows,
        'summary': summarize(rows),
    }


# ----------------------------------------------------------------------------- Markdown

def _f(v, nd=1):
    if v is None:
        return '-'
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    if isinstance(v, int):
        return str(v)
    return ('%.' + str(nd) + 'f') % v


_DAY_COLUMNS = (            # key, header, decimals
    ('tir_pct', 'observed TIR 3.9-10 %', 1),
    ('below_3_9_pct', '< 3.9 %', 1),
    ('below_3_0_pct', '< 3.0 %', 1),
    ('above_10_pct', '> 10 %', 1),
    ('above_13_9_pct', '> 13.9 %', 1),
    ('mean_mmol', 'mean mmol/l', 1),
    ('cv_pct', 'CV %', 1),
    ('cgm_gap_min', 'CGM gap min', 0),
    ('manual_boluses', 'manual boluses', 0),
    ('manual_bolus_U', 'manual U', 2),
    ('smb_U', 'SMB U', 2),
    ('carb_entries', 'carb entries', 0),
    ('loop_hours', 'loop h', 1),
)

_SUMMARY_ROWS = (           # key, label, decimals, goal
    ('tir_pct', 'observed time in range 3.9-10.0 mmol/l, % of CGM minutes', 1, '> 90'),
    ('below_3_9_pct', 'observed time below 3.9 mmol/l, %', 1, 'not raised by any step'),
    ('below_3_0_pct', 'observed time below 3.0 mmol/l, %', 1, ''),
    ('above_10_pct', 'observed time above 10.0 mmol/l, %', 1, ''),
    ('above_13_9_pct', 'observed time above 13.9 mmol/l, %', 1, ''),
    ('mean_mmol', 'observed mean glucose, mmol/l', 2, ''),
    ('cv_pct', 'observed CV, %', 1, ''),
    ('cgm_gap_min', 'observed CGM gap minutes per day (excluded)', 0, ''),
    ('manual_boluses', 'observed manual boluses per day', 1, '0'),
    ('manual_bolus_U', 'observed manual bolus units per day', 2, '0'),
    ('smb_U', 'observed SMB units per day', 2, ''),
    ('carb_entries', 'observed carb entries per day', 1, '0 (UAM only)'),
    ('loop_hours', 'observed hours with loop runs in Nightscout per day', 1, '24 (fully closed)'),
)


def render_markdown(b):
    """BASELINE.md for the baseline `b` (build()). Not linted here: lint_markdown() does that."""
    rows, s = b['days'], b['summary']
    first, last = (rows[0]['day'], rows[-1]['day']) if rows else ('-', '-')
    out = ['# Hindcast observed baseline', '',
           'Generated by `hc baseline --write` on %s from the %s; do not edit by hand.' % (b['generated'], b['source']),
           'Every number on this page is observed: what the phone\'s own settings produced on these days, the',
           'reference the end goal (`GOAL.md`) is tracked against once a setting has run on the phone. None of it',
           'is a replay result, and no replay ever produces a number of this kind.', '',
           '> ' + lint.HONESTY_STATEMENT, '',
           '## Period summary (observed, %d UTC days %s to %s)' % (s['days'], first, last), '',
           '| observed | median | mean | days | GOAL.md |', '|---|---:|---:|---:|---|']
    for key, label, nd, goal in _SUMMARY_ROWS:
        v = s[key]
        out.append('| %s | %s | %s | %d | %s |' % (label, _f(v['median'], nd), _f(v['mean'], nd), v['days'], goal))
    out += ['',
            'Observed day counts: %d days, %d with a loop run in the cached devicestatus (%d of them looping at least '
            '%d h), %d with CGM, %d without a manual bolus, %d above the GOAL.md mark of %d %% observed TIR.'
            % (s['days'], s['days_with_loop'], s['days_looped_full'], FULL_LOOP_HOURS, s['days_with_cgm'],
               s['days_without_manual_bolus'], s['days_tir_over_goal'], GOAL_TIR_PCT), '',
            '## Per day (observed, UTC days)', '',
            '| day | ' + ' | '.join(h for _, h, _ in _DAY_COLUMNS) + ' |',
            '|---|' + '|'.join('---:' for _ in _DAY_COLUMNS) + '|']
    for r in rows:
        cells = [_f(r[k], nd) for k, _, nd in _DAY_COLUMNS]
        out.append('| %s | %s |' % (r['day'] + (' (%s)' % r['cgm_note'] if r.get('cgm_note') else ''),
                                     ' | '.join(cells)))
    out += ['', '## Definitions (observed values)', '',
            '- CGM: the Nightscout sgv entries of the UTC day on a %d-min grid (mean of the readings in a slot); slots '
            'without a reading are gaps, excluded from every percentage and listed as CGM gap minutes.' % SLOT_MIN,
            '- Ranges in mg/dl as AAPS stores them: observed TIR %d-%d, below 3.9 = under %d, below 3.0 = under %d, '
            'above 10 = over %d, above 13.9 = over %d; mean glucose = mg/dl / %d; CV = standard deviation / mean.'
            % (LOW_MGDL, HIGH_MGDL, LOW_MGDL, VERY_LOW_MGDL, HIGH_MGDL, VERY_HIGH_MGDL, MGDL_PER_MMOL),
            '- Manual bolus: a treatment of type NORMAL with eventType %s and insulin above zero; SMB: type SMB; '
            'carb entry: any treatment with carbs above zero. Invalid records are skipped.' % ' or '.join(MANUAL_EVENT_TYPES),
            '- Loop h: hours of the UTC day covered by loop runs uploaded to Nightscout (cached devicestatus records with '
            'a loop result); each run stands for the time to the next one, at most 5 min. A phone that looped offline '
            'and never uploaded counts as not looping here.',
            '- Summary: median and mean over the days that have the value.',
            '']
    return '\n'.join(out)


def lint_markdown(text):
    """Every line through hindcast.lint kind 'baseline', then the whole page with the honesty paragraph required.
    Raises lint.LintError on the first problem; None when clean."""
    for line in text.split('\n'):
        if line.startswith('> ') and lint.contains_honesty_statement(line):
            continue
        lint.lint_text(line, kind='baseline')
    lint.lint_text(text, kind='baseline', require_honesty=True)
    return None


# ----------------------------------------------------------------------------- CLI

def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    os.replace(tmp, path)


def run(args):
    """`hc baseline`: 0 ok, 3 refused (lint, bad day spec)."""
    try:
        day_list = dy.parse_days(args.days) if args.days else default_days(args.frozen_dir)
    except ValueError as e:
        print('hc baseline: %s' % e, file=sys.stderr)
        return 3
    b = build(day_list, cache_root=args.cache_dir)
    text = render_markdown(b)
    try:
        lint_markdown(text)
    except lint.LintError as e:
        print('hc baseline: refused by the lint: %s' % e, file=sys.stderr)
        return 3
    if not args.write:
        sys.stdout.write(text)
        return 0
    studies = args.studies_dir or _studies_dir()
    page_path = args.docs_path or (DOCS_PATH if args.write == 'docs' else os.path.join(studies, PAGE_NAME))
    json_path = args.json or os.path.join(studies, JSON_NAME)
    for path in (page_path, json_path):
        if is_public_path(path):
            if not getattr(args, 'public', False):
                print(PUBLIC_REFUSED % path, file=sys.stderr)
                return 3
            print(PUBLIC_WARNING % path, file=sys.stderr)
    _write(page_path, text)
    _write(json_path, json.dumps(b, indent=1, sort_keys=False) + '\n')
    print('hc baseline: %d observed days -> %s and %s' % (len(day_list), page_path, json_path))
    return 0


def is_public_path(path):
    """Is `path` inside the repository's docs/ (public, committed)?"""
    real, docs = os.path.realpath(os.path.abspath(path)), os.path.realpath(DOCS_DIR)
    return real == docs or real.startswith(docs + os.sep)


def _studies_dir():
    from hindcast import study as st
    return st.STUDIES_DIR


def register(subparsers):
    p = subparsers.add_parser('baseline', help='the observed baseline toward GOAL.md (CGM, boluses, SMB per day) '
                                               'from the Nightscout cache')
    p.add_argument('days', nargs='?', default=None,
                   help='UTC days (2026-09-01..2026-09-07, as hc freeze); default the frozen days in %s' % DEFAULT_SPAN)
    p.add_argument('--write', nargs='?', const='studies', choices=('studies', 'docs'), default=None,
                   help='write <studies dir>/BASELINE.md and <studies dir>/baseline.json (git-ignored) instead of '
                        'printing the page; "--write docs" targets docs/hindcast/BASELINE.md and needs --public')
    p.add_argument('--public', action='store_true',
                   help='allow writing into docs/ (the public repository): per-day glucose and insulin figures would '
                        'be committed; a warning is printed')
    p.add_argument('--json', default=None, help='the JSON path (default <studies dir>/baseline.json)')
    p.add_argument('--studies-dir', default=None, help='where the page and the JSON go (default software/studies)')
    p.add_argument('--cache-dir', default=None, help='the Nightscout host cache (default ns_cache/<host>/)')
    p.add_argument('--frozen-dir', default=None, help='frozen/<host>/ for the default day list')
    p.add_argument('--docs-path', default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=run)
    return p


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.baseline')
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(['baseline'] + list(sys.argv[1:] if argv is None else argv))
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
