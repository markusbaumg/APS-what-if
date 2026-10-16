"""
emulator_optimize.py  -  run a grid of what-if settings over one Nightscout window and rank the variants

    python emulator_optimize.py --grid variants/grid_autoISF.json --start 2026-09-06T00:00:00Z --end 2026-09-07T00:00:00Z
                                [--base-vdf variants/no_change.vdf] [--url https://ns.vivikusi.ch]
                                [--outputs "bg/iob/ISFs/insReq/SMB/basal"] [--label mylabel]
                                [--mode grid|one_at_a_time] [--param "profile autoISF_max" 1.3 1.5 ...] [--dry-run]
                                [--overwrite]

Grid file (JSON)
    {"parameters": {"profile autoISF_max": [1.2, 1.3, 1.4, 1.5, 1.6],
                    "profile dura_ISF_weight": [0.5, 0.7, 0.9]},
     "mode": "grid"}                                  # or "one_at_a_time"
    A parameter key is '<array> <item>' exactly as in a VDF line; the values are Python literals
    (numbers, true/false, strings).  --param on the command line overrides / adds a parameter.
    mode grid          = every combination (cartesian product)
    mode one_at_a_time = each value of each parameter alone, the other parameters stay at the base VDF
    The base VDF itself is always run too (variant 'base') - it shows the replay bias against the
    original loop, which every other variant carries as well.

What happens
    results/<label>/<variant>.vdf                       = base VDF lines + one line per parameter
    results/<label>/<start>_<end>.<variant>.csv/.txt/.log = the normal emulator outputs (written by
                                                          emulator_core.parameters_known, 'noplot')
    results/<label>/summary.csv                         = one row per variant: parameters + metrics
    results/<label>/summary.txt                         = the table below (grid order, base first)
    results/<label>/summary.json                        = the same, machine readable
    <label> defaults to <grid name>_<start>_<end>; a results/<label>/ that already holds a summary.csv is
    not reused unless --overwrite is given (or a different --label).
    All Nightscout data comes from the ns_cache (the first variant fills it), so a finished run
    is reproducible offline.

Metrics (per variant, from the CSV; U = insulin units)
    loops                    number of replayed loop runs
    smb_orig_U / smb_emul_U  sum of the SMBs the phone gave / the emulator would have given
    tbr_orig_U / tbr_emul_U  temp basal rate IN FORCE x time to the next loop, summed (capped at 30 min per
                             loop; last loop: median interval).  'TBR orig' is the rate the phone set in that
                             loop or, when it set none, the running temp (emulator_core.basalFromReason), so
                             both columns are 'rate in force', not 'temps newly set by the loop'.
    total_orig_U / total_emul_U = SMB + TBR;  delta_U = total_emul_U - total_orig_U;  delta_pct
    delta_vs_base_U          delta_U minus the delta_U of the base variant (the replay bias removed)
    smb_more / smb_less / smb_equal   loops with emulated SMB >, <, == original SMB (0.001 U)
    smb_loops_orig / smb_loops_emul   loops with an SMB > 0
    isf_factor_emul_mean/max/min      final autoISF factor of the emulated loop (orig = phone)
    at_autoISF_max_pct / at_autoISF_min_pct   loops whose emulated factor sits on the cap (grid value, else base VDF
                             line, else ns_settings.json with profile_history applied at each loop's time)
    isf_emul_mean            mean emulated final ISF (mg/dl per U)
    iob_over_TH_pct / iob_over_tol_pct  loops where the ORIGINAL iob (the replay has no other) exceeds the
                             emulated effective iobTH / the 130 % tolerance
    rise_orig_U / rise_emul_U   insulin (SMB+TBR) given while bg > emulated target AND bg rising
                             (bg minus the bg 5 min earlier > 0)
    rest_orig_U / rest_emul_U   insulin given in all other loops (bg below target or falling)
    bg_mean, bg_gt180_pct, bg_lt70_pct   character of the window (identical for all variants)

READ THE METRICS AS PROXIES.  The replay feeds every variant the glucose, IOB and COB history that was
recorded with the ORIGINAL settings; it cannot simulate how glucose would have responded to the
different insulin.  A variant that shows +3 U/day says "on this day, in these situations, the loop would
have asked for 3 U more", not "glucose would have been better".  For the same reason summary.txt does NOT
rank: sorting by delta would simply reward whatever delivers less (weaker weights, SMB off).  The table is
in grid order with the base variant first and a 'vs base' column; judge WHEN the extra insulin comes
(rise vs rest split, iobTH hits, loops at the factor cap) rather than the delta alone, run several days
of different character, and check the per-loop transcripts (.txt) of the candidates.
"""
import argparse
import datetime
import itertools
import json
import os
import statistics
import sys
import time
from email.utils import formatdate

os.environ['PYTHONUTF8'] = '1'                       # required by emulator_core, must precede its import

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# os.chdir(HERE) happens in main() right before the replay, after the command line paths were made absolute

from emulator_core import parameters_known, set_tty, get_version_core        # noqa: E402
from determine_basal import get_version_determine_basal                       # noqa: E402

#ERSION = '2026-09-08 12:00'                         # <start>_<end> labels, no reuse of a results dir, grid order + vs-base column
VERSION = '2026-10-16 12:00'                         # VDF field separators, window resolved once, 'Z' = failed, cap per loop time, absolute paths
DEFAULT_OUTPUTS = 'seconds/bg/iob/autosens/acce/bg_ISF/pp/dura/ISFs/insReq/SMB/basal'
EPS = 0.001


def get_version_optimize(echo_msg):
    echo_msg['emulator_optimize.py'] = VERSION       # grid runner over the Nightscout replay
    return echo_msg


# ----------------------------------------------------------------------------- grid handling
def parse_literal(text):
    """command line value -> Python literal (number, bool, else string)"""
    t = text.strip()
    if t.lower() in ('true', 'false'):
        return t.lower() == 'true'
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return t


def load_grid(path, cli_params, cli_mode):
    grid = {'parameters': {}, 'mode': 'grid'}
    if path:
        with open(path, 'r', encoding='utf-8') as f:
            grid.update(json.load(f))
    params = dict(grid.get('parameters', {}))
    for name, values in cli_params:
        params[name] = [parse_literal(v) for v in values]
    for name in params:
        if len(name.split()) != 2:
            sys.exit(f'grid parameter "{name}" must be "<array> <item>" (VDF syntax)')
        if not isinstance(params[name], list) or not params[name]:
            sys.exit(f'grid parameter "{name}" needs a non-empty list of values')
    mode = cli_mode or grid.get('mode', 'grid')
    if mode not in ('grid', 'one_at_a_time'):
        sys.exit(f'unknown mode "{mode}" (grid | one_at_a_time)')
    return params, mode


def vdf_value(value):
    """Python literal -> text that setVariant() evals back to the same value"""
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, (int, float)):
        return repr(value)
    return repr(str(value))


def variant_name(assignment, params):
    """assignment = list of (name, value) -> file-system safe label"""
    if not assignment:
        return 'base'
    items = [n.split()[1] for n in params]
    parts = []
    for name, value in assignment:
        array, item = name.split()
        key = item if items.count(item) == 1 else array + '.' + item
        parts.append(f'{key}_{vdf_value(value)}'.replace("'", '').replace(' ', ''))
    return '__'.join(parts)


def combinations(params, mode):
    """-> list of assignments; an assignment is a list of (name, value) tuples"""
    names = list(params)
    out = [[]]                                                       # the base VDF alone
    if mode == 'grid':
        for values in itertools.product(*[params[n] for n in names]):
            out.append(list(zip(names, values)))
    else:
        for n in names:
            for v in params[n]:
                out.append([(n, v)])
    return out


def write_vdf(path, base_lines, assignment, label):
    with open(path, 'w', encoding='utf-8') as f:
        for line in base_lines:
            f.write(line.rstrip('\n') + '\n')
        for name, value in assignment:
            array, item = name.split()
            # setVariant() splits the row at the first blank after each field: the blanks between the fields are
            # mandatory, the padding only keeps the columns aligned for short names
            f.write(f'{array:<12} {item:<32} {vdf_value(value):<16} ### emulator_optimize {label}\n')


# ----------------------------------------------------------------------------- CSV reading
def read_result_csv(path, decimal='.'):
    """emulator CSV (3 header lines, ';' separated) -> (column names, rows as dicts of floats/None)"""
    with open(path, 'r', encoding='utf-8') as f:
        lines = [l.rstrip('\n') for l in f]
    if len(lines) < 4:
        return [], []
    heads = [l.split(';') for l in lines[:3]]
    ncol = max(len(h) for h in heads)
    names = []
    for i in range(ncol):
        words = []
        for h in heads:
            if i < len(h):
                words += h[i].split()
        names.append(' '.join(words))
    rows = []
    for line in lines[3:]:
        cells = line.split(';')
        if not cells or not cells[0].strip().isdigit():                  # Minimum/Maximum/Totals rows
            continue
        row = {}
        for i, name in enumerate(names):
            cell = cells[i].strip() if i < len(cells) else ''
            if decimal != '.':
                cell = cell.replace(decimal, '.')
            try:
                row[name] = float(cell)
            except ValueError:
                row[name] = cell if cell else None
        rows.append(row)
    return names, rows


def col(rows, name, default=0.0):
    return [r.get(name) if r.get(name) is not None else default for r in rows]


def fnum(rows, name):
    vals = col(rows, name, None)
    return [v if isinstance(v, float) else 0.0 for v in vals]


# ----------------------------------------------------------------------------- metrics
def cap_series(cap, t):
    """cap = None | number | cap_at(ms) from effective_cap() -> the cap valid at each loop (list) or None"""
    if cap is None:
        return None
    if callable(cap):
        caps = [cap(int(ti * 1000)) for ti in t]
    else:
        caps = [float(cap)] * len(t)
    return caps if any(c is not None for c in caps) else None


def metrics_from_csv(path, autoisf_max=None, autoisf_min=None):
    """autoisf_max / autoisf_min: a number, or a function of the loop time (unix ms) - see effective_cap()"""
    names, rows = read_result_csv(path)
    m = {'loops': len(rows)}
    if not rows:
        return m
    need = ['UNIX time', 'bg accel', 'target low emul', 'target high emul', 'iob', 'eff. iobTH emul',
            'tol. iobTH emul', 'final ISF orig', 'final ISF emul', 'ISF emul', 'SMB orig', 'SMB emul',
            'TBR orig', 'TBR emul']
    missing = [n for n in need if n not in names]
    if missing:
        raise KeyError(f'{path}: CSV columns not found: {missing}; found {names}')

    t = fnum(rows, 'UNIX time')
    bg = fnum(rows, 'bg accel')
    tgt = [(lo + hi) / 2 for lo, hi in zip(fnum(rows, 'target low emul'), fnum(rows, 'target high emul'))]
    iob = fnum(rows, 'iob')
    iobth = fnum(rows, 'eff. iobTH emul')
    iobtol = fnum(rows, 'tol. iobTH emul')
    f_orig = fnum(rows, 'final ISF orig')
    f_emul = fnum(rows, 'final ISF emul')
    isf_emul = fnum(rows, 'ISF emul')
    smb_o = fnum(rows, 'SMB orig')
    smb_e = fnum(rows, 'SMB emul')
    tbr_o = fnum(rows, 'TBR orig')
    tbr_e = fnum(rows, 'TBR emul')
    n = len(rows)

    # interval to the next loop in hours (last loop: median interval)
    gaps = [t[i + 1] - t[i] for i in range(n - 1) if t[i + 1] > t[i]]
    med = statistics.median(gaps) if gaps else 60.0
    dt_h = [((t[i + 1] - t[i]) if i < n - 1 and t[i + 1] > t[i] else med) / 3600.0 for i in range(n)]
    dt_h = [min(d, 0.5) for d in dt_h]                                 # a CGM gap is not a 1 h temp
    tbr_o_U = [r * d for r, d in zip(tbr_o, dt_h)]
    tbr_e_U = [r * d for r, d in zip(tbr_e, dt_h)]

    m['smb_orig_U'] = sum(smb_o)
    m['smb_emul_U'] = sum(smb_e)
    m['tbr_orig_U'] = sum(tbr_o_U)
    m['tbr_emul_U'] = sum(tbr_e_U)
    m['total_orig_U'] = m['smb_orig_U'] + m['tbr_orig_U']
    m['total_emul_U'] = m['smb_emul_U'] + m['tbr_emul_U']
    m['delta_U'] = m['total_emul_U'] - m['total_orig_U']
    m['delta_pct'] = 100.0 * m['delta_U'] / m['total_orig_U'] if m['total_orig_U'] > 0 else 0.0
    m['smb_more'] = sum(1 for a, b in zip(smb_e, smb_o) if a > b + EPS)
    m['smb_less'] = sum(1 for a, b in zip(smb_e, smb_o) if a < b - EPS)
    m['smb_equal'] = n - m['smb_more'] - m['smb_less']
    m['smb_loops_orig'] = sum(1 for v in smb_o if v > EPS)
    m['smb_loops_emul'] = sum(1 for v in smb_e if v > EPS)
    m['smb_max_emul_U'] = max(smb_e)
    m['isf_factor_orig_mean'] = statistics.fmean(f_orig)
    m['isf_factor_emul_mean'] = statistics.fmean(f_emul)
    m['isf_factor_emul_max'] = max(f_emul)
    m['isf_factor_emul_min'] = min(f_emul)
    caps_max = cap_series(autoisf_max, t)                                # per loop: profile_history may change the cap inside the window
    caps_min = cap_series(autoisf_min, t)
    m['at_autoISF_max_pct'] = 100.0 * sum(1 for v, c in zip(f_emul, caps_max) if c and v >= c - 0.005) / n if caps_max else None
    m['at_autoISF_min_pct'] = 100.0 * sum(1 for v, c in zip(f_emul, caps_min) if c and v <= c + 0.005) / n if caps_min else None
    m['isf_emul_mean'] = statistics.fmean(isf_emul)
    m['iob_over_TH_pct'] = 100.0 * sum(1 for a, b in zip(iob, iobth) if b > 0 and a > b) / n
    m['iob_over_tol_pct'] = 100.0 * sum(1 for a, b in zip(iob, iobtol) if b > 0 and a > b) / n
    m['iob_max'] = max(iob)

    # rise / rest split: delta = bg now - bg 5 min earlier (nearest loop), rising above target vs the rest
    rise_o = rise_e = rest_o = rest_e = 0.0
    rise_loops = 0
    j = 0
    for i in range(n):
        while j < i and t[i] - t[j + 1] >= 300:
            j += 1
        delta = bg[i] - bg[j] if i > 0 else 0.0
        give_o = smb_o[i] + tbr_o_U[i]
        give_e = smb_e[i] + tbr_e_U[i]
        if bg[i] > tgt[i] and delta > 0:
            rise_o += give_o
            rise_e += give_e
            rise_loops += 1
        else:
            rest_o += give_o
            rest_e += give_e
    m['rise_loops'] = rise_loops
    m['rise_orig_U'] = rise_o
    m['rise_emul_U'] = rise_e
    m['rest_orig_U'] = rest_o
    m['rest_emul_U'] = rest_e
    m['bg_mean'] = statistics.fmean(bg)
    m['bg_gt180_pct'] = 100.0 * sum(1 for v in bg if v > 180) / n
    m['bg_lt70_pct'] = 100.0 * sum(1 for v in bg if v < 70) / n
    return m


METRIC_ORDER = ['loops', 'smb_orig_U', 'smb_emul_U', 'tbr_orig_U', 'tbr_emul_U', 'total_orig_U', 'total_emul_U',
                'delta_U', 'delta_pct', 'delta_vs_base_U', 'smb_more', 'smb_less', 'smb_equal', 'smb_loops_orig', 'smb_loops_emul',
                'smb_max_emul_U', 'isf_factor_orig_mean', 'isf_factor_emul_mean', 'isf_factor_emul_max',
                'isf_factor_emul_min', 'at_autoISF_max_pct', 'at_autoISF_min_pct', 'isf_emul_mean',
                'iob_over_TH_pct', 'iob_over_tol_pct', 'iob_max', 'rise_loops', 'rise_orig_U', 'rise_emul_U',
                'rest_orig_U', 'rest_emul_U', 'bg_mean', 'bg_gt180_pct', 'bg_lt70_pct', 'seconds']


def fmt(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f'{v:.3f}'.rstrip('0').rstrip('.') if abs(v) < 1e6 else f'{v:.1f}'
    return str(v)


# ----------------------------------------------------------------------------- settings helpers
def effective_cap(assignment, base_lines, settings, item):
    """-> cap_at(ms): autoISF_max / autoISF_min valid for a variant at loop time ms (unix ms):
    grid value > base VDF line > ns_settings.json value, rewound by profile_history for loops before a change -
    the rule ns_profile.ProfileBuilder._apply_history applies to the profile of every loop, so the cap of the base
    variant follows the settings history inside the window"""
    fixed = None
    for name, value in assignment:
        if name.split()[1] == item:
            fixed = float(value)
    if fixed is None:
        for line in base_lines:
            parts = line.split('###')[0].split()
            if len(parts) >= 3 and parts[0] == 'profile' and parts[1] == item:
                try:
                    fixed = float(parts[2])
                except ValueError:
                    pass
    if fixed is not None:
        return lambda ms: fixed
    value = settings.get('autoisf', {}).get(item)
    if value is None:
        return lambda ms: None
    from ns_profile import ProfileBuilder
    history = [h for h in ProfileBuilder._parse_history(settings.get('profile_history', {})) if h[1] == item]

    def cap_at(ms):
        for when, key, old, new in history:                   # ascending: the earliest future change wins
            if when > ms:
                try:
                    return float(old)
                except (TypeError, ValueError):
                    return None
        return float(value)
    return cap_at


def echo_message(url, outputs, start, end, grid_path, label):
    m = '=' * 66 + '\nEcho of software versions used\n' + '-' * 66
    m += '\n emulator home directory       ' + HERE + os.sep
    echo = {}
    echo = get_version_optimize(echo)
    echo = get_version_core(echo)
    echo = get_version_determine_basal(echo)
    for ele in echo:
        m += '\n dated: ' + echo[ele] + '       module name: ' + ele
    m += '\n' + '-' * 66 + '\nEcho of execution parameters used\n' + '-' * 66
    m += '\nNightscout source     ' + url
    m += '\nOutput options        ' + outputs
    m += '\nDecimal symbol        .'
    m += '\nStart of time window  ' + start
    m += '\nEnd of time window    ' + end
    m += '\nGrid file             ' + str(grid_path)
    m += '\nRun label             ' + label
    m += '\n' + '=' * 66 + '\n'
    return m


# ----------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description='grid runner for the Nightscout replay of the AAPS emulator')
    ap.add_argument('--grid', help='JSON grid file (see module docstring)')
    ap.add_argument('--start', required=True, help='first loop, ISO UTC e.g. 2026-09-06T00:00:00Z')
    ap.add_argument('--end', required=True, help='end of window (exclusive), ISO UTC')
    ap.add_argument('--base-vdf', default=os.path.join(HERE, 'variants', 'no_change.vdf'))
    ap.add_argument('--url', default=None, help='Nightscout url; default from ns_settings.json')
    ap.add_argument('--outputs', default=DEFAULT_OUTPUTS, help='emulator output options ("noplot" is added)')
    ap.add_argument('--label', default=None, help='results/<label>/ ; default <grid name>_<start>_<end>')
    ap.add_argument('--overwrite', action='store_true', help='reuse a results/<label>/ that already holds a summary.csv')
    ap.add_argument('--mode', choices=['grid', 'one_at_a_time'], default=None, help='overrides the grid file')
    ap.add_argument('--param', nargs='+', action='append', default=[], metavar=('NAME', 'VALUE'),
                    help='"<array> <item>" followed by its values; overrides / adds a grid parameter')
    ap.add_argument('--dry-run', action='store_true', help='only write the VDFs')
    args = ap.parse_args(argv)

    if not args.grid and not args.param:
        ap.error('give --grid and/or --param')
    cli_params = []
    for p in args.param:
        if len(p) < 2:
            ap.error('--param needs "<array> <item>" and at least one value')
        cli_params.append((p[0], p[1:]))
    # user paths are relative to the caller's cwd; make them absolute before main() changes into HERE
    if args.grid:
        args.grid = os.path.abspath(args.grid)
    args.base_vdf = os.path.abspath(args.base_vdf)
    params, mode = load_grid(args.grid, cli_params, args.mode)

    settings = {}
    try:
        with open(os.path.join(HERE, 'ns_settings.json'), 'r', encoding='utf-8') as f:
            settings = json.load(f)
    except (OSError, ValueError):
        pass
    url = args.url or settings.get('nightscout', {}).get('url') or 'ns:'

    base_vdf = args.base_vdf
    if not os.path.isfile(base_vdf):
        sys.exit(f'base VDF {base_vdf} not found (an empty .vdf is the "no change" base)')
    with open(base_vdf, 'r', encoding='utf-8') as f:
        base_lines = [l for l in f if l.strip()]

    # resolve the window ONCE: open bounds ('2000-..' / '2099-..') become concrete UTC stamps here, so every variant
    # replays the same loops and the result files carry the same <start>_<end> label (parameters_known would resolve
    # 'now' again for each run)
    from ns_source import resolve_window, window_label
    from ns_client import to_iso
    start_ms, end_ms = resolve_window(args.start, args.end)
    start, end = to_iso(start_ms)[:19] + 'Z', to_iso(end_ms)[:19] + 'Z'
    fn_label = window_label(start, end)              # how parameters_known names the result files: <start>_<end>
    label = args.label or (os.path.splitext(os.path.basename(args.grid))[0] if args.grid else 'params') + '_' + fn_label
    out_dir = os.path.join(HERE, 'results', label)
    if os.path.exists(os.path.join(out_dir, 'summary.csv')) and not args.overwrite:
        sys.exit(f'{out_dir} already holds a summary.csv - give another --label or --overwrite')
    os.makedirs(out_dir, exist_ok=True)
    outputs = args.outputs.strip('/')
    if 'noplot' not in outputs.split('/'):
        outputs += '/noplot'
    arg2 = 'Windows/' + outputs + '/.'

    assignments = combinations(params, mode)
    plan = []
    for a in assignments:
        name = variant_name(a, params)
        vdf = os.path.join(out_dir, name + '.vdf')
        write_vdf(vdf, base_lines, a, label)
        plan.append((name, a, vdf))
    print(f'{len(plan)} variants ({mode}) in {out_dir}')
    for name, a, vdf in plan:
        print('  ' + name)
    if args.dry_run:
        print('dry run: VDFs written, nothing replayed')
        return 0

    os.chdir(HERE)                                    # emulator_core writes relative to the VDF path
    set_tty(0, 0, 'batch')
    msg = echo_message(url, arg2, start, end, args.grid, label)
    results = []
    loop_counts = {}
    for k, (name, a, vdf) in enumerate(plan, 1):
        print(f'\n---------- variant {k}/{len(plan)}: {name}')
        csv_path = os.path.join(out_dir, fn_label + '.' + name + '.csv')
        if os.path.exists(csv_path):                  # from an earlier --overwrite run: never read it as this run's result
            os.remove(csv_path)
        entries = {}                                  # fresh per variant (parameters_known keeps the last rows otherwise)
        t0 = time.time()
        ret = parameters_known(url, arg2, vdf, start, end, entries, msg, '.', datetime.datetime(1970, 1, 1))
        seconds = time.time() - t0
        status = ret[1]
        if status in ('SYNTAX', 'UTF8', 'Z') or not os.path.exists(csv_path):     # 'Z': VDF pre-scan failed or < 2 loops
            print(f'\nvariant {name} failed: status {status}, csv {"found" if os.path.exists(csv_path) else "missing"}')
            m = {'loops': 0, 'error': status}
        else:
            m = metrics_from_csv(csv_path,
                                 effective_cap(a, base_lines, settings, 'autoISF_max'),
                                 effective_cap(a, base_lines, settings, 'autoISF_min'))
        m['seconds'] = round(seconds, 1)
        loop_counts[name] = m.get('loops', 0)
        results.append({'variant': name, 'params': dict(a), 'vdf': vdf, 'csv': csv_path, 'metrics': m})
        print(f'\n  {m.get("loops", 0)} loops in {seconds:.1f} s: SMB orig {fmt(m.get("smb_orig_U"))} / emul {fmt(m.get("smb_emul_U"))} U,'
              f' TBR orig {fmt(m.get("tbr_orig_U"))} / emul {fmt(m.get("tbr_emul_U"))} U, delta {fmt(m.get("delta_U"))} U')

    counts = set(loop_counts.values())
    if len(counts) > 1:
        print('\nWARNING: loop counts differ between variants (state leak or failed run?): ' + str(loop_counts))
    base_delta = next((r['metrics'].get('delta_U') for r in results if r['variant'] == 'base'), None)
    for r in results:
        if base_delta is not None and r['metrics'].get('delta_U') is not None:
            r['metrics']['delta_vs_base_U'] = r['metrics']['delta_U'] - base_delta

    # ---- summary files
    names = list(params)
    param_cols = [n.split()[1] for n in names]
    metric_keys = [k for k in METRIC_ORDER if any(k in r['metrics'] for r in results)]
    with open(os.path.join(out_dir, 'summary.csv'), 'w', encoding='utf-8') as f:
        f.write(';'.join(['variant'] + param_cols + metric_keys) + '\n')
        for r in results:
            cells = [r['variant']] + [fmt(r['params'].get(n)) for n in names] + [fmt(r['metrics'].get(k)) for k in metric_keys]
            f.write(';'.join(cells) + '\n')
    with open(os.path.join(out_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump({'label': label, 'url': url, 'start': start, 'end': end, 'mode': mode,
                   'parameters': params, 'base_vdf': base_vdf, 'created': formatdate(localtime=True),
                   'results': results}, f, indent=1)

    ordered = [r for r in results if r['variant'] == 'base'] + [r for r in results if r['variant'] != 'base']   # grid order, base first
    text = ranking_text(ordered, names, label, url, start, end, mode, loop_counts)
    with open(os.path.join(out_dir, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write(text)
    print('\n' + text)
    print(f'results in {out_dir}')
    return 0


def ranking_text(ranked, names, label, url, start, end, mode, loop_counts):
    items = [n.split()[1] for n in names]
    w = max([len(r['variant']) for r in ranked] + [8])
    lines = [f'emulator_optimize  {label}   {url}   {start} .. {end}   mode {mode}',
             'grid order, base first - NOT a ranking: a smaller delta only means the variant asks for less insulin,',
             'the replay cannot tell whether that would have been better (see NIGHTSCOUT_SOURCE.md)',
             '']
    head = f'{"#":>2} {"variant":<{w}} ' + ' '.join(f'{it:>16}' for it in items)
    head += f' {"loops":>5} {"SMB o/e U":>13} {"TBR o/e U":>13} {"delta U":>8} {"vs base":>8} {"d%":>6} {"SMB >/</=":>11}'
    head += f' {"ISFf mean/max":>14} {"@max%":>6} {"iob>TH%":>7} {"rise o/e U":>13} {"rest o/e U":>13}'
    lines.append(head)
    lines.append('-' * len(head))
    for i, r in enumerate(ranked, 1):
        m = r['metrics']
        p = r['params']
        line = f'{i:>2} {r["variant"]:<{w}} ' + ' '.join(f'{fmt(p.get(n)):>16}' for n in names)
        if 'error' in m:
            line += f'  FAILED ({m["error"]})'
            lines.append(line)
            continue
        if not m.get('loops') or 'smb_orig_U' not in m:           # CSV without data rows: nothing to sum up
            line += f' {0:>5}  no loops'
            lines.append(line)
            continue
        line += f' {m["loops"]:>5} {fmt(m["smb_orig_U"]) + "/" + fmt(m["smb_emul_U"]):>13}'
        vs = f'{m["delta_vs_base_U"]:>+8.2f}' if m.get('delta_vs_base_U') is not None else f'{"-":>8}'
        line += f' {fmt(m["tbr_orig_U"]) + "/" + fmt(m["tbr_emul_U"]):>13} {m["delta_U"]:>+8.2f} {vs} {m["delta_pct"]:>+6.1f}'
        line += f' {str(m["smb_more"]) + "/" + str(m["smb_less"]) + "/" + str(m["smb_equal"]):>11}'
        line += f' {fmt(round(m["isf_factor_emul_mean"], 2)) + "/" + fmt(m["isf_factor_emul_max"]):>14}'
        line += f' {fmt(round(m["at_autoISF_max_pct"], 1)) if m.get("at_autoISF_max_pct") is not None else "-":>6}'
        line += f' {m["iob_over_TH_pct"]:>7.1f} {fmt(round(m["rise_orig_U"], 2)) + "/" + fmt(round(m["rise_emul_U"], 2)):>13}'
        line += f' {fmt(round(m["rest_orig_U"], 2)) + "/" + fmt(round(m["rest_emul_U"], 2)):>13}'
        lines.append(line)
    lines.append('')
    base = next((r for r in ranked if r['variant'] == 'base'), None)
    if base and 'delta_U' in base['metrics']:
        lines.append(f'replay bias of the base VDF (same settings as the phone): delta {base["metrics"]["delta_U"]:+.2f} U'
                     f' - the "vs base" column has it removed')
    if len(set(loop_counts.values())) > 1:
        lines.append('WARNING: loop counts differ between variants: ' + str(loop_counts))
    lines.append('columns: SMB/TBR/rise/rest = original/emulated U; TBR = temp rate IN FORCE (new temp, else the running one) x time')
    lines.append('to the next loop, capped at 30 min; SMB >/</= loops where the emulated SMB is larger/smaller/equal;')
    lines.append('ISFf = emulated final autoISF factor; @max% = loops at autoISF_max; iob>TH% = loops with iob above the emulated iobTH')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    sys.exit(main())
