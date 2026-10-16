"""
check.py  -  `hc check`, the no-change replay as the regression gate with golden metrics (Hindcast H-902)

    cd software && ../.venv/bin/python -m hindcast check                    # 2026-09-06 06-09Z vs tests/golden/
    hc check --no-live                                                       # skip the frozen == live comparison (~20 s)
    hc check --refreeze                                                      # refreeze a stale frozen day first (offline)
    hc check --write-golden --reason "H-703: SMB ratio from int(bg)"         # re-baseline (only in a PR naming the issue)
    hc check --full                                                          # absolute values too (local use, not for a PR)
    hc check --json                                                          # the report as JSON

What it does, in order (DESIGN.md section 9):
  1. The day must be frozen and fresh (`freeze.blocked_reason`); `--refreeze` rebuilds a stale or missing frozen day
     offline from the Nightscout cache (`freeze.freeze_day(force=True)`). Not frozen and no --refreeze -> exit 3.
  2. The base replay (empty VDF) of the window (default 06:00-09:00Z) with the frozen source, through
     `replay.run_trial` - the same call every study trial makes, so the gate tests what the studies run.
  3. The same window with `source='live-day'` (the whole-day live reconstruction filtered to the window, read from
     the cache inside the offline guard): the two CSVs must be equal byte for byte. A live replay built for the window
     alone is NOT the partner (replay.py docstring: window-start dependence of ns_glucose / ns_meal). Missing cache ->
     the live row is `failed` and the gate fails; `--no-live` skips it on purpose (printed as skipped).
  4. The metrics are compared with the golden file `tests/golden/<day>T<hh>-<hh>.base.json`, gate by gate (GATES):
       min    an agreement percentage: regression when it drops below golden - tol; a rise beyond tol is reported as
              `up` (not a failure; re-baseline in the PR of the fidelity issue that moved it)
       abs    an amount (the SMB totals): regression when |now - golden| > tol, in either direction (`moved`)
       equal  the loop count
     DESIGN.md section 9 names absolute thresholds (SMB exact >= 97 %, within 0.05 U >= 99 %, insulinReq within
     0.05 U = 100 %, variable_sens within 1 mg/dl = 100 %, SMB total 2.65 U +- 0.05). The tree is being moved by the
     algorithm-fidelity work, so the gate compares with what was MEASURED when the golden file was written, and the
     design numbers are printed beside it for reference only (`design` column, never the gate).
  5. Prints a one-screen Markdown table for PR descriptions: percentages, deltas against golden, and the result per
     gate. The default output passes `hindcast.lint` for kind 'issue' (no insulin amounts, no loop counts, no glucose
     values: amounts are printed as deltas only, the unit in the column head); `--full` adds the absolute values for a
     local look. The honesty statement closes the output.

Exit codes: 0 no regression, 1 regression (a gate failed, the frozen replay failed, or frozen != live),
3 not checked (day not frozen or stale without --refreeze, no golden file, golden for another window, bad arguments).

Re-baselining (`--write-golden`) needs `--reason` (name the fidelity issue) and refuses when the frozen replay failed
or the live comparison did not pass (unless --no-live, then the file records `live_checked: false`). The golden file
carries the measured gate metrics, the tolerances, the design numbers, the CSV sha1 and the hashes of the code,
settings and frozen input it was measured on (`code_sha`, `settings_sha`, `freeze_sha`, version stamps).

Nothing here opens a socket, writes to the phone, Nightscout or ns_settings.json, or edits the core.
"""
import argparse
import datetime
import hashlib
import io
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts, days, freeze, replay                                       # noqa: E402

GOLDEN_DIR = os.path.join(HINDCAST_DIR, 'tests', 'golden')
GOLDEN_FORMAT = 1
DEFAULT_DAY = '2026-09-06'
DEFAULT_HOURS = (6, 9)                                   # 06:00-09:00Z, 180 loops on 2026-09-06
EPS = 1e-5                                               # golden values are stored rounded to 6 decimals

EXIT_OK, EXIT_REGRESSION, EXIT_NOT_CHECKED = 0, 1, 3


class CheckError(RuntimeError):
    """The check could not run (exit 3): not frozen, no golden file, golden for another window, bad arguments."""


@dataclass(frozen=True)
class Gate:
    key: str                  # metrics.trial_metrics key
    label: str                # row label (no units inside numbers: the PR output must pass the privacy lint)
    kind: str                 # 'min' | 'abs' | 'equal'
    tol: float                # tolerance of the gate (in the metric's unit)
    unit: str                 # '%' | 'U' | ''
    design: Optional[str]     # DESIGN.md section 9 threshold as text (reference only)
    private: bool = False     # an absolute value that never goes into a PR (amounts, counts): printed with --full


GATES = (
    Gate('loops', 'loop count', 'equal', 0, '', 'as golden', private=True),
    Gate('smb_exact_pct', 'SMB exact', 'min', 0.0, '%', '>= 97 %'),
    Gate('smb_within_pct', 'SMB within 0.05 U', 'min', 0.0, '%', '>= 99 %'),
    Gate('insreq_within_pct', 'insulinReq within 0.05 U', 'min', 0.0, '%', '= 100 %'),
    Gate('rate_within_pct', 'TBR rate within 0.05 U/h', 'min', 0.0, '%', None),
    Gate('isf_within_pct', 'variable_sens within its tolerance', 'min', 0.0, '%', '= 100 %'),
    Gate('agreement_pct', 'all four within tolerance', 'min', 0.0, '%', None),
    Gate('smb_emul_U', 'SMB total, emulated', 'abs', 0.05, 'U', '2.65 +- 0.05', private=True),
    Gate('smb_orig_U', 'SMB total, phone (frozen input)', 'abs', 0.001, 'U', None, private=True),
)
GATE_KEYS = tuple(g.key for g in GATES)
DESIGN_SECTION_9 = {'smb_exact_pct': 97.0, 'smb_within_pct': 99.0, 'insreq_within_pct': 100.0,
                    'isf_within_pct': 100.0, 'smb_emul_U': [2.65, 0.05]}


# ----------------------------------------------------------------------------- paths and golden files

def default_window(day=DEFAULT_DAY, hours=DEFAULT_HOURS):
    """(start_iso, end_iso) of the check window: `hours` (start, end) UTC of `day`; an end of 24 is midnight after."""
    if not days.is_day(day):
        raise CheckError(f'"{day}" is not a UTC day (YYYY-MM-DD)')
    h0, h1 = int(hours[0]), int(hours[1])
    if not 0 <= h0 < h1 <= 24:
        raise CheckError(f'hours {h0}-{h1}: expected 0 <= start < end <= 24')
    d = datetime.datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
    fmt = lambda t: t.strftime('%Y-%m-%dT%H:%M:%SZ')     # noqa: E731
    return fmt(d + datetime.timedelta(hours=h0)), fmt(d + datetime.timedelta(hours=h1))


def window_hours(day, window):
    """(start_h, end_h) of a window inside `day` (end 24 = midnight after); CheckError for other windows."""
    d = datetime.datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
    out = []
    for iso in window:
        t = datetime.datetime.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        h = (t - d).total_seconds() / 3600.0
        if h != int(h) or not 0 <= h <= 24:
            raise CheckError(f'window bound {iso}: hc check windows are whole UTC hours of {day}')
        out.append(int(h))
    if out[0] >= out[1]:
        raise CheckError(f'window {window[0]} .. {window[1]} is empty')
    return tuple(out)


def golden_name(day=DEFAULT_DAY, hours=DEFAULT_HOURS):
    """'2026-09-06T06-09.base.json'."""
    return f'{day}T{int(hours[0]):02d}-{int(hours[1]):02d}.base.json'


def golden_path(day=DEFAULT_DAY, hours=DEFAULT_HOURS, golden_dir=GOLDEN_DIR):
    return os.path.join(golden_dir, golden_name(day, hours))


def load_golden(path):
    """The golden document; CheckError when missing or not a golden file of this format."""
    if not os.path.isfile(path):
        raise CheckError(f'no golden file {os.path.relpath(path, SOFTWARE_DIR)}; write one with '
                         f'`hc check --write-golden --reason "<fidelity issue>"`')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise CheckError(f'{path}: not JSON ({e})') from None
    if not isinstance(doc, dict) or doc.get('format') != GOLDEN_FORMAT or not isinstance(doc.get('metrics'), dict):
        raise CheckError(f'{path}: not a golden file of format {GOLDEN_FORMAT}')
    return doc


def golden_doc(metrics, day, window, reason, csv_sha1=None, freeze_sha=None, live_checked=True, gates=GATES,
               code_sha=None, settings_sha=None, versions=None):
    """The golden document for measured `metrics` (only the gate keys are stored)."""
    missing = [g.key for g in gates if metrics.get(g.key) is None]
    if missing:
        raise CheckError(f'the replay produced no value for {", ".join(missing)}; golden not written')
    return {
        'format': GOLDEN_FORMAT,
        'about': 'hc check golden metrics (H-902): the base replay (empty VDF) of the frozen window as measured on '
                 'the code below; re-baselined only by `hc check --write-golden` in a PR that names the fidelity '
                 'issue that moved them. DESIGN.md section 9 numbers are kept for reference, the gate uses these.',
        'day': day, 'window': list(window), 'written': contracts.now_iso(), 'reason': reason,
        'code_sha': code_sha or contracts.code_sha(),
        'settings_sha': settings_sha or _settings_sha(),
        'freeze_sha': freeze_sha or '', 'csv_sha1': csv_sha1 or '', 'live_checked': bool(live_checked),
        'versions': versions if versions is not None else _versions(),
        'metrics': {g.key: _plain(metrics[g.key]) for g in gates},
        'gates': {g.key: {'kind': g.kind, 'tol': g.tol} for g in gates},
        'design_section_9': dict(DESIGN_SECTION_9),
    }


def write_golden(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    contracts.write_json_atomic(path, doc)
    return path


def _plain(v):
    return round(float(v), 6) if isinstance(v, float) else v


def _settings_sha():
    try:
        return contracts.settings_sha()
    except (OSError, ValueError):
        return ''


def _versions():
    try:
        return contracts.core_versions()
    except Exception:                                   # a version stamp is information, never a reason to fail
        return {}


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- comparison

def compare(golden, metrics, gates=GATES):
    """One row per gate: {key, label, kind, unit, tol, design, private, golden, now, delta, result, failed}.
    result: 'ok' | 'up' (min gate risen beyond tol) | 'REGRESSION' (min gate dropped, equal differs)
            | 'MOVED' (abs gate outside tol, either way) | 'MISSING' (no value now or in golden). Tolerances come from
    the golden file when it carries them (so a re-baseline keeps them), else from GATES."""
    gtol = (golden or {}).get('gates') or {}
    gm = (golden or {}).get('metrics') or {}
    rows = []
    for g in gates:
        tol = float((gtol.get(g.key) or {}).get('tol', g.tol))
        old, new = gm.get(g.key), (metrics or {}).get(g.key)
        row = {'key': g.key, 'label': g.label, 'kind': g.kind, 'unit': g.unit, 'tol': tol, 'design': g.design,
               'private': g.private, 'golden': old, 'now': new, 'delta': None}
        if old is None or new is None:
            row['result'] = 'MISSING'
        else:
            d = float(new) - float(old)
            row['delta'] = d
            if g.kind == 'equal':
                row['result'] = 'ok' if abs(d) <= tol + EPS else 'REGRESSION'
            elif g.kind == 'min':
                row['result'] = 'REGRESSION' if d < -tol - EPS else ('up' if d > tol + EPS else 'ok')
            else:
                row['result'] = 'ok' if abs(d) <= tol + EPS else 'MOVED'
        row['failed'] = row['result'] in ('REGRESSION', 'MOVED', 'MISSING')
        rows.append(row)
    return rows


def csv_difference(path_a, path_b):
    """Where two result CSVs differ, without any value: {'equal', 'lines_a', 'lines_b', 'rows_differing', 'columns'}
    (column names of the differing cells, header from the third header row of the emulator CSV)."""
    with open(path_a, 'rb') as fa, open(path_b, 'rb') as fb:
        a, b = fa.read(), fb.read()
    if a == b:
        return {'equal': True, 'rows_differing': 0, 'columns': []}
    la, lb = a.decode('utf-8', 'replace').splitlines(), b.decode('utf-8', 'replace').splitlines()
    names = []
    try:
        from emulator_optimize import read_result_csv
        names, _ = read_result_csv(path_a)
    except Exception:
        names = []
    cols, rows = set(), 0
    for x, y in zip(la, lb):
        if x == y:
            continue
        rows += 1
        for i, (u, v) in enumerate(zip(x.split(';'), y.split(';'))):
            if u != v:
                cols.add(names[i] if i < len(names) else f'column {i + 1}')
    return {'equal': False, 'lines_a': len(la), 'lines_b': len(lb), 'rows_differing': rows + abs(len(la) - len(lb)),
            'columns': sorted(cols)}


# ----------------------------------------------------------------------------- the check

@dataclass
class CheckReport:
    day: str
    window: tuple
    status: str = 'not_checked'                  # 'ok' | 'regression' | 'not_checked'
    exit_code: int = EXIT_NOT_CHECKED
    rows: list = field(default_factory=list)     # compare() rows
    live: dict = field(default_factory=dict)     # {'status': equal|differs|failed|skipped, ...}
    frozen: dict = field(default_factory=dict)   # {'status', 'seconds', 'error', 'flags', 'csv_sha1'}
    golden_path: str = ''
    golden: dict = field(default_factory=dict)   # the golden file's identity (no metrics)
    notes: list = field(default_factory=list)
    error: Optional[str] = None
    metrics: dict = field(default_factory=dict)  # the measured gate metrics
    wrote_golden: bool = False

    def to_dict(self):
        return asdict(self)

    @property
    def failed_gates(self):
        return [r for r in self.rows if r['failed']]


def _golden_identity(doc):
    return {k: doc.get(k) for k in ('day', 'window', 'written', 'reason', 'code_sha', 'settings_sha', 'freeze_sha',
                                    'csv_sha1', 'live_checked')}


def _refreeze(day, url, frozen_dir, log):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            meta = freeze.freeze_day(day, url, force=True, log_msg=lambda *a: None, base_dir=frozen_dir)
    except freeze.FreezeError as e:
        raise CheckError(f'refreeze of {day} refused: {e}') from None
    log(f'refroze {day} offline from the cache (frozen sha {contracts.short(meta.get("sha1"))})')
    return meta


def check(day=DEFAULT_DAY, hours=DEFAULT_HOURS, golden_file=None, golden_dir=GOLDEN_DIR, live=True, write=False,
          reason=None, refreeze=False, frozen_dir=None, url='ns:', settings_path=contracts.NS_SETTINGS_PATH,
          keep_dir=None, log=None):
    """Run the gate; returns a CheckReport (never raises for a failed replay: that is a regression). CheckError is
    turned into status 'not_checked' / exit 3 with the reason in `error`. `keep_dir` keeps frozen.csv / live.csv."""
    log = log or (lambda *_: None)
    window = default_window(day, hours) if days.is_day(day) else (None, None)
    rep = CheckReport(day=day, window=window)
    try:
        if write and not (reason or '').strip():
            raise CheckError('--write-golden needs --reason naming the fidelity issue that moved the numbers')
        rep.golden_path = golden_file or golden_path(day, hours, golden_dir)
        golden = None
        if not write:
            golden = load_golden(rep.golden_path)
            if golden.get('day') != day or tuple(golden.get('window') or ()) != tuple(window):
                raise CheckError(f'{rep.golden_path} is the golden file of {golden.get("day")} '
                                 f'{golden.get("window")}, not of {day} {window}')
            rep.golden = _golden_identity(golden)
        settings = days.load_settings(settings_path)
        blocked = freeze.blocked_reason(day, url, settings, base_dir=frozen_dir, settings_path=settings_path)
        if blocked:
            if not refreeze:
                raise CheckError(blocked + ' (or pass --refreeze)')
            _refreeze(day, url, frozen_dir, log)
            rep.notes.append(f'{day} was refrozen before the check ({blocked.split("(", 1)[-1].rstrip(")")})')
            blocked = freeze.blocked_reason(day, url, settings, base_dir=frozen_dir, settings_path=settings_path)
            if blocked:
                raise CheckError(blocked)
    except CheckError as e:
        rep.error, rep.status, rep.exit_code = str(e), 'not_checked', EXIT_NOT_CHECKED
        return rep

    meta = freeze.read_meta(day, days.host_of(url, settings), frozen_dir) or {}
    freeze_sha = meta.get('sha1', '')
    tmp = keep_dir or tempfile.mkdtemp(prefix='hindcast_check_')
    os.makedirs(tmp, exist_ok=True)
    try:
        # 2. frozen base replay
        log(f'replaying base on the frozen {day} {window[0][11:16]}-{window[1][11:16]}Z window ...')
        f_csv = os.path.join(tmp, 'frozen.csv')
        res = replay.run_trial(replay.TrialSpec(day=day, window=window, url=url, settings_path=settings_path,
                                                frozen_dir=frozen_dir, source='frozen', keep_csv=f_csv))
        rep.frozen = {'status': res.status, 'seconds': res.seconds, 'error': res.error, 'flags': list(res.flags)}
        if res.status != 'done':
            rep.status, rep.exit_code = 'regression', EXIT_REGRESSION
            rep.error = f'the frozen base replay {res.status}: {res.error}'
            return rep
        rep.frozen['csv_sha1'] = file_sha1(f_csv)
        rep.metrics = {k: res.metrics.get(k) for k in GATE_KEYS}

        # 3. live partner
        if live:
            log('replaying the same window with the live whole-day reconstruction (cache, offline) ...')
            l_csv = os.path.join(tmp, 'live.csv')
            lres = replay.run_trial(replay.TrialSpec(day=day, window=window, url=url, settings_path=settings_path,
                                                     source='live-day', keep_csv=l_csv))
            if lres.status != 'done':
                rep.live = {'status': 'failed', 'seconds': lres.seconds, 'error': lres.error}
            else:
                diff = csv_difference(f_csv, l_csv)
                rep.live = {'status': 'equal' if diff['equal'] else 'differs', 'seconds': lres.seconds, **diff}
        else:
            rep.live = {'status': 'skipped'}

        # 4. golden
        if write:
            if rep.live['status'] in ('failed', 'differs'):
                rep.status, rep.exit_code = 'regression', EXIT_REGRESSION
                rep.error = f'golden not written: frozen vs live {rep.live["status"]}'
                return rep
            doc = golden_doc(res.metrics, day, window, reason.strip(), csv_sha1=rep.frozen['csv_sha1'],
                             freeze_sha=freeze_sha, live_checked=live)
            write_golden(rep.golden_path, doc)
            rep.wrote_golden = True
            rep.golden = _golden_identity(doc)
            golden = doc
        rep.rows = compare(golden, rep.metrics)
        if golden.get('freeze_sha') and freeze_sha and golden['freeze_sha'] != freeze_sha:
            rep.notes.append('the frozen input differs from the one the golden file was measured on '
                             '(refreeze after a settings or ns_*.py change): a move may come from the input')
        if golden.get('csv_sha1'):
            rep.notes.append('result CSV ' + ('identical to' if golden['csv_sha1'] == rep.frozen['csv_sha1']
                                              else 'differs from') + ' the golden run')
        if golden.get('code_sha') and golden['code_sha'] != contracts.code_sha():
            rep.notes.append(f'code changed since the golden run (golden code_sha {contracts.short(golden["code_sha"])}, '
                             f'now {contracts.short(contracts.code_sha())})')
        live_bad = rep.live.get('status') in ('failed', 'differs')
        bad = bool(rep.failed_gates) or live_bad
        rep.status = 'regression' if bad else 'ok'
        rep.exit_code = EXIT_REGRESSION if bad else EXIT_OK
        return rep
    finally:
        if not keep_dir:
            shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- output

def _num(v, unit):
    if v is None:
        return '-'
    if unit == '%':
        return f'{float(v):.1f} %'
    if unit == 'U':
        return f'{float(v):.3f}'
    return f'{int(v)}' if float(v) == int(v) else f'{float(v):g}'


def _delta(v, unit):
    if v is None:
        return '-'
    if unit == '%':
        return f'{float(v):+.1f}'
    if unit == 'U':
        return f'{float(v):+.3f}'
    return f'{int(v):+d}' if float(v) == int(v) else f'{float(v):+g}'


def _gate_text(row):
    tol = row['tol']
    if row['kind'] == 'equal':
        return 'equal'
    if row['kind'] == 'min':
        return 'not below golden' if tol == 0 else f'not below golden - {tol:g}'
    return f'within {tol:g} of golden'


def format_table(rep, full=False):
    """The one-screen Markdown output for PR descriptions (passes hindcast.lint kind 'issue' unless `full`)."""
    w0, w1 = (rep.window[0] or '')[11:16], (rep.window[1] or '')[11:16]
    out = [f'### hc check: base replay (empty VDF), {rep.day} {w0}-{w1} UTC, frozen source', '']
    g = rep.golden or {}
    if g:
        out.append(f'golden `{os.path.basename(rep.golden_path)}` written {str(g.get("written") or "-")[:10]}, '
                   f'code_sha `{contracts.short(g.get("code_sha"))}`, reason: {g.get("reason") or "-"}')
    out.append(f'now: code_sha `{contracts.short(contracts.code_sha())}`')
    out.append('')
    if rep.error and not rep.rows:
        out.append(f'**{"NOT CHECKED" if rep.exit_code == EXIT_NOT_CHECKED else "REGRESSION"}**: {rep.error}')
    if rep.rows:
        out.append('| gate | golden | now | delta | rule | DESIGN 9 | result |')
        out.append('|---|---:|---:|---:|---|---|---|')
        for r in rep.rows:
            unit_head = ' (U, delta only)' if r['unit'] == 'U' and not full else (' (U)' if r['unit'] == 'U' else '')
            show = full or not r['private']
            gold = _num(r['golden'], r['unit']) if show else '.'
            now = _num(r['now'], r['unit']) if show else '.'
            design = r['design'] or '-'
            out.append(f'| {r["label"]}{unit_head} | {gold} | {now} | {_delta(r["delta"], r["unit"])} | '
                       f'{_gate_text(r)} | {design} | {r["result"]} |')
    live = rep.live or {}
    if live:
        st = live.get('status')
        if st == 'equal':
            txt = 'equal byte for byte'
        elif st == 'differs':
            cols = ', '.join(live.get('columns') or []) or '-'
            txt = f'DIFFERS ({live.get("rows_differing")} rows; columns: {cols})'
        elif st == 'failed':
            txt = 'FAILED: ' + str(live.get('error') or '')[:200].replace('\n', ' ')
        else:
            txt = 'skipped (--no-live)'
        out.append(f'| frozen CSV == live-day CSV | | | | bit for bit | DESIGN 9 | {txt} |' if rep.rows
                   else f'frozen CSV == live-day CSV: {txt}')
    out.append('')
    for n in rep.notes:
        out.append(f'- {n}')
    secs = [f'frozen {rep.frozen.get("seconds")} s' if rep.frozen.get('seconds') is not None else None,
            f'live-day {live.get("seconds")} s' if live.get('seconds') is not None else None]
    secs = [s for s in secs if s]
    if secs:
        out.append('- replay time: ' + ', '.join(secs))
    if rep.wrote_golden:
        out.append(f'- golden file written: `{os.path.relpath(rep.golden_path, SOFTWARE_DIR)}`')
    out.append('')
    nfail = len(rep.failed_gates) + (1 if live.get('status') in ('failed', 'differs') else 0)
    if rep.status == 'ok':
        ups = [r['label'] for r in rep.rows if r['result'] == 'up']
        verdict = f'**no regression** ({len(rep.rows) + (1 if live.get("status") == "equal" else 0)} gates passed)'
        if ups:
            verdict += ('; risen above golden: ' + ', '.join(ups) +
                        ' (re-baseline with --write-golden in the PR of the fidelity issue that raised it)')
    elif rep.status == 'regression':
        verdict = f'**REGRESSION** ({nfail} gate(s) failed)' + (f': {rep.error}' if rep.error and rep.rows else '')
    else:
        verdict = '**NOT CHECKED** (exit 3)'
    out.append(verdict)
    out.append('')
    out.append(contracts.HONESTY_STATEMENT)
    return '\n'.join(out) + '\n'


# ----------------------------------------------------------------------------- CLI

def _hours(text):
    try:
        a, b = text.split('-', 1)
        return int(a), int(b)
    except ValueError:
        raise argparse.ArgumentTypeError(f'--hours {text}: expected H-H, e.g. 6-9') from None


def register(subparsers):
    """`hc check [--day D] [--hours 6-9] [--no-live] [--refreeze] [--write-golden --reason R] [--full] [--json]`;
    cli.py calls this, the parser's func is run_check."""
    p = subparsers.add_parser('check', help='regression gate: base replay vs the golden metrics',
                              description='Replays base (empty VDF) on the frozen window, compares the agreement '
                                          'with tests/golden/<day>T<hh>-<hh>.base.json, checks frozen == live CSV '
                                          'byte for byte and prints a table for PR descriptions. Exit 1 on regression, '
                                          '3 when it could not check.')
    p.add_argument('--day', default=DEFAULT_DAY, help=f'UTC day (default {DEFAULT_DAY}, must be frozen)')
    p.add_argument('--hours', type=_hours, default=DEFAULT_HOURS, help='UTC hours of the window, default 6-9')
    p.add_argument('--golden', default=None, help='golden file (default tests/golden/<day>T<hh>-<hh>.base.json)')
    p.add_argument('--no-live', action='store_true', help='skip the frozen == live comparison (saves ~20 s)')
    p.add_argument('--refreeze', action='store_true', help='refreeze a stale or missing frozen day first (offline)')
    p.add_argument('--write-golden', action='store_true', help='re-baseline: write the golden file from this run')
    p.add_argument('--reason', default=None, help='with --write-golden: the fidelity issue that moved the numbers')
    p.add_argument('--full', action='store_true', help='also print the absolute amounts (local use, not for a PR)')
    p.add_argument('--json', action='store_true', help='print the report as JSON')
    p.add_argument('--keep', default=None, metavar='DIR', help='keep frozen.csv and live.csv in DIR')
    p.add_argument('--frozen-dir', default=None)
    p.add_argument('--url', default='ns:')
    p.set_defaults(func=run_check)
    return p


def run_check(args):
    log = (lambda m: sys.stderr.write(m + '\n')) if not getattr(args, 'json', False) else (lambda m: None)
    try:
        rep = check(day=args.day, hours=args.hours, golden_file=args.golden, live=not args.no_live,
                    write=args.write_golden, reason=args.reason, refreeze=args.refreeze,
                    frozen_dir=args.frozen_dir, url=args.url, keep_dir=args.keep, log=log)
    except CheckError as e:                               # default_window on a bad --day
        sys.stderr.write(f'hc check: {e}\n')
        return EXIT_NOT_CHECKED
    if args.json:
        sys.stdout.write(json.dumps(rep.to_dict(), indent=1, default=str) + '\n')
    else:
        sys.stdout.write(format_table(rep, full=args.full))
    return rep.exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.check')
    sub = parser.add_subparsers(dest='command')
    register(sub)
    args = parser.parse_args(['check'] + list(argv if argv is not None else sys.argv[1:]))
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
