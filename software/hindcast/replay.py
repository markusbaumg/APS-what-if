"""
replay.py  -  one (variant, day) replay through emulator_core.parameters_known() with the frozen source (Hindcast H-102)

    from hindcast import replay
    spec = replay.TrialSpec(day='2026-09-06', params={'profile dura_ISF_weight': 0.5}, base_lines=[], report_dir=report)
    res = replay.run_trial(spec)              # -> TrialResult(status 'done' | 'failed' | 'blocked', metrics, files, ...)
    row_fields = res.to_row()                 # what study.Runner's evaluate(task) returns
    ctx = replay.ReplayContext.from_study(study_dir, study, settings)
    runner.run(tasks, ctx.evaluate)           # the serial path; pool.py runs ctx.spec_for(task) in worker processes

What one trial does, in order (DESIGN.md section 4.4):
  1. `freeze.blocked_reason(day)` - a day that is not frozen, frozen from a non-final cache or stale against the
     working tree gives `status: blocked` without touching the core (H-104).
  2. The VDF is written into the trial's work directory: the study's base lines plus one row per searched parameter
     through `emulator_optimize.write_vdf` (space.write_variant_vdf), so a Hindcast variant is the optimizer's.
     The file is named after the short vhash (the CSV header carries no label, so the name is free); the variant
     label goes into the echo banner of the `.log`.
  3. `ns_source.NightscoutSource` is replaced on the real module object by `freeze.FrozenSource` (bound to the
     frozen/ root of the spec) for the duration of the call, inside `freeze.offline_guard(enabled=True)`, so no
     socket can be opened whatever HINDCAST_OFFLINE says (a test monkeypatches socket.socket to raise and passes).
     `source='live-day'` installs `DayLiveSource` instead: the live reconstruction over the whole UTC day(s) of the
     window, filtered to the window - the comparison partner of `hc check` (see freeze.py: a live source built for a
     sub-window differs from the whole-day reconstruction in the 2nd-3rd decimal of the smoothed deltas, so the frozen
     replay must be compared with the whole-day live source). `source='live'` installs the plain live source for the
     window (`LIVE_SOURCE`, the real class captured at import) explicitly, also inside a pool worker that has the
     frozen source installed for good. The class used is recorded as the flag `source:<mode>:<class name>`.
  4. `emulator_core.parameters_known('ns:', 'Windows/<outputs>/noplot/.', vdf, start, end, {}, banner, '.',
     datetime(1970, 1, 1))` with stdout redirected into `runs/<run name>.out` (or a buffer without runs_dir), after
     `set_tty(0, 0, 'batch')` once per process. The window is the UTC day (`days.day_window`) unless the spec names
     one; both labels are in the `to_iso` form, so the result files are `<start>_<end>.<vdf name>.csv|.txt|.log|.delta`
     plus `<start>_<end>.orig.txt` in the work directory (`ns_source.window_label`).
  5. Return element 1 of the tuple: `SYNTAX` (a VDF row failed to eval during a loop), `UTF8`, `Z` (VDF pre-scan
     failed, source failed, fewer than two loops) -> `status: failed` with the tail of the captured stdout (the
     `sub_issue()` text) as `error`; the search treats the point as infeasible and the study continues (the Runner
     stops after three consecutive failures). A missing CSV after a non-failed return is a failed trial too.
  6. `emulator_core.Pred` and `FlowChart` are copied immediately after the call (module globals keyed
     `round(ms / 1000, 1)`, filled unconditionally in TreatLoop, also on noplot). Missing or empty -> the flag
     `no_pred`, the trial still counts: the metrics fall back to the recorded-BG rule (`lowside_no_pred`).
  7. `metrics.trial_metrics(csv, base=<base csv of the day>, caps, min_guard from Pred Levels, tdd_phone_U from the
     frozen meta, bridged = the rows inside the meta's CGM-bridged stretches (H-714) -> agreement_bridged_pct /
     agreement_unbridged_pct for the quarantine)`; without a base CSV (the base trial itself, or the orchestrator
     did not hand one over) the trial is compared with itself and flagged `no_base_csv`. A base whose loop count differs is not used (flag
     `base_loop_mismatch:<n>/<m>`); the quarantine (days.py, H-204) and the orchestrator's guard read `metrics.loops`.
  8. Tier-2 files through export.py when `report_dir` is set: `series/<day>/<vhash>.json` always, `series/<day>/orig.json`
     once per day, `pred/<day>/<vhash>.json` + `.text.json` (and the phone's `orig` pair once per day) when `pred` is
     asked. `files` in the row are paths relative to the study directory (`report/series/...`).
  9. The transcripts (`.txt`, `.log`, `.delta`, `.orig.txt`) and the CSV are deleted unless `keep_transcripts`
     (then the work directory is `runs/<run name>/` and stays; `TrialSpec.run_name()` is a sha1 over the full trial
     key, day, window, control, control instance and source, so concurrent trials never share a path). `keep_csv`
     names a path the CSV is copied to before the cleanup: the orchestrator keeps the base CSV of each day this way
     (`ReplayContext.base_csv_path`) because every other trial of the day is compared with it; that path is removed
     before anything else, so a failed or blocked trial never leaves an earlier run's CSV there. `runs/<run name>.out`
     is kept for failed trials (and with keep_transcripts), removed otherwise.

Seconds per day-variant (author's machine: Python 3.14, Apple silicon, 18 cores, one process, empty VDF, the
default outputs with noplot), measured 2026-10-17 on 2026-09-06 with run_trial() (wall clock incl. metrics):
    frozen,   whole day (1311 loops)                         5.4 s
    live,     whole day (1311 loops, reconstruction incl.)  29.6 s
    frozen,   06-09Z (180 loops)                             1.0 s   (the whole 2 MB day file is read, then 180 x TreatLoop)
    live,     06-09Z window-only source                      4.5 s   (NOT bit-equal to frozen, see below)
    live-day, whole-day source filtered to 06-09Z           24.2 s   (bit-equal to frozen: the hc check partner)
The frozen replay spends its time in TreatLoop / determine_basal (the reconstruction is gone): ~3-4 ms per loop.
The pool (pool.py) scales that by the worker count.

Bit-for-bit acceptance (tests/test_replay.py). The frozen 06-09Z CSV is byte-identical to the CSV of a live replay
whose source is the whole-day reconstruction (`source='live-day'`). A live source built for the window alone differs
in `dura avg.` on 40 of 180 loops and in one loop's insulinReq/SMB/TBR/ISF (the window-start dependence of ns_glucose /
ns_meal documented in freeze.py, a fidelity item of the reconstruction, not of the replay), so it is not the
comparison partner; the test records that difference instead of asserting it away.

Nothing here opens a socket, writes to the phone, Nightscout or ns_settings.json, or edits the core.
"""
import atexit
import contextlib
import datetime
import glob
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

import ns_source                                                                            # noqa: E402
from ns_client import day_bounds, parse_iso, to_iso                                         # noqa: E402
from emulator_optimize import DEFAULT_OUTPUTS, effective_cap, read_result_csv              # noqa: E402

from hindcast import contracts, days, export, freeze                                        # noqa: E402
from hindcast import metrics as hm                                                          # noqa: E402
from hindcast import space as spc                                                           # noqa: E402

FAILED_STATUSES = ('SYNTAX', 'UTF8', 'Z')
SOURCES = ('frozen', 'live', 'live-day')
ERROR_TAIL = 1500                                       # characters of the captured stdout kept as the error text
FROZEN_SECONDS_NOTE = ('2026-09-06, one process: frozen whole day 1311 loops 5.4 s, live whole day 29.6 s; '
                       'frozen 06-09Z 1.0 s, live window-only 4.5 s, live-day filtered 24.2 s')


class ReplayError(RuntimeError):
    """A replay could not be set up (bad spec, unwritable work dir); a core failure is a failed TrialResult, not this."""


# ----------------------------------------------------------------------------- the spec and the result

@dataclass
class TrialSpec:
    """Everything one worker needs to replay one (variant, day); plain data, picklable for pool.py.

    day            UTC day 'YYYY-MM-DD' (the frozen file; the default window is the whole day)
    params         {'<array> <item>': value} of the variant ({} = base)
    base_lines     the study's base VDF rows (every variant carries them)
    outputs        emulator option tokens (the optimizer's default carries every factor column metrics.py reads)
    window         (start_iso, end_iso) to replay instead of the whole day (tests, hc check)
    names          space order for the variant label and the VDF row order (None = sorted by name)
    key / vhash / variant / control   identity as study.Runner computes it (filled when None: vhash and variant)
    pred           write pred/<day>/<vhash>.json (+ .text.json and the phone's orig pair once per day)
    keep_transcripts  keep .txt/.log/.delta/.orig.txt/.csv under runs/<run_name()>/
    report_dir     studies/<study>/report (tier-2 files); None = no series/pred files
    runs_dir       studies/<study>/runs (stdout capture, kept transcripts); None = a temp dir, stdout in memory
    base_csv       the base replay's CSV of the same day (metrics vs base); None = compared with itself
    keep_csv       copy the result CSV to this path before the cleanup (the orchestrator does this for base)
    url            'ns:' (the settings' Nightscout) - the frozen host comes from it
    settings_path  ns_settings.json (caps, TDD history, frozen host)
    frozen_dir     frozen/ root (None = HINDCAST_FROZEN_DIR or software/frozen)
    source         'frozen' (default) | 'live-day' (whole-day live reconstruction filtered to the window) | 'live'
    require_final  blocked unless the frozen day is from a final cache (H-104)
    tdd_phone_U    the phone's TDD of the day (None = frozen meta character.tdd_U when the window is the whole day)
    """
    day: str
    params: dict = field(default_factory=dict)
    base_lines: list = field(default_factory=list)
    outputs: str = DEFAULT_OUTPUTS
    window: Optional[tuple] = None
    names: Optional[list] = None
    key: Optional[str] = None
    vhash: Optional[str] = None
    variant: Optional[str] = None
    control: Optional[str] = None
    pred: bool = False
    keep_transcripts: bool = False
    report_dir: Optional[str] = None
    runs_dir: Optional[str] = None
    base_csv: Optional[str] = None
    keep_csv: Optional[str] = None
    url: str = 'ns:'
    settings_path: str = contracts.NS_SETTINGS_PATH
    frozen_dir: Optional[str] = None
    source: str = 'frozen'
    require_final: bool = True
    tdd_phone_U: Optional[float] = None
    files_root: Optional[str] = None                 # `files` paths are relative to this (default: parent of report_dir)
    instance: Optional[str] = None                   # a control's instance (task.meta['instance']); part of run_name()
    blocked: Optional[str] = None                    # set by ReplayContext when the day's base failed in this run:
                                                     # run_trial returns status 'blocked' with this text, no core call

    def __post_init__(self):
        if not days.is_day(self.day):
            raise ReplayError(f'"{self.day}" is not a UTC day (YYYY-MM-DD)')
        if self.source not in SOURCES:
            raise ReplayError(f'source must be one of {SOURCES}, not {self.source!r}')
        self.params = dict(self.params or {})
        self.base_lines = [str(l).rstrip('\n') for l in (self.base_lines or []) if str(l).strip()]
        if self.vhash is None:
            self.vhash = contracts.vhash(self.params)
        if self.variant is None:
            self.variant = 'base' if not self.params else spc.variant_name(self.params, self.names)
        if self.window is not None:
            self.window = (str(self.window[0]), str(self.window[1]))

    # ---- derived
    def resolved_window(self):
        """(start_iso, end_iso) in the core's label form; the UTC day when no window is given."""
        if self.window is None:
            return days.day_window(self.day)
        start, end = ns_source.resolve_window(self.window[0], self.window[1])
        return to_iso(start), to_iso(end)

    def whole_day(self):
        return self.window is None or tuple(self.resolved_window()) == tuple(days.day_window(self.day))

    def window_label(self):
        start, end = self.resolved_window()
        return ns_source.window_label(start, end)

    def vdf_name(self):
        """<short vhash>.vdf: file-system safe, short, unique per variant."""
        return contracts.short(self.vhash) + '.vdf'

    def vdf_lines(self):
        """The VDF text rows (base lines + one row per parameter) as a list of lines."""
        return [l for l in spc.vdf_text(self.params, self.base_lines, names=self.names).split('\n') if l.strip()]

    def short_key(self):
        """12-character display form of the trial (log lines); NOT unique across days without a key: use run_name()
        for anything on disk."""
        return contracts.short(self.key or self.vhash)

    def run_name(self):
        """40-hex name of this trial's files under runs/ (`<run_name>.out`, `<run_name>/` with keep_transcripts).
        A sha1 over the full trial key (vhash when there is none) plus day, resolved window, control, control instance
        and source, so two trials that run at the same time - the same variant on two days in two pool workers, two
        instances of a control - never share a stdout capture, a work directory or a VDF."""
        start, end = self.resolved_window()
        ident = [self.key or '', self.vhash, self.day, start, end, self.control or '', str(self.instance or ''),
                 self.source]
        return hashlib.sha1(json.dumps(ident, separators=(',', ':')).encode('utf-8')).hexdigest()

    def assignment(self):
        return spc.assignment(self.params, self.names)


@dataclass
class TrialResult:
    """What run_trial() returns; `to_row()` is the dict study.Runner expects from evaluate(task)."""
    status: str                                     # 'done' | 'failed' | 'blocked'
    day: str
    vhash: str
    variant: str
    params: dict = field(default_factory=dict)
    key: Optional[str] = None
    control: Optional[str] = None
    metrics: dict = field(default_factory=dict)
    seconds: Optional[float] = None
    error: Optional[str] = None
    flags: list = field(default_factory=list)
    files: dict = field(default_factory=dict)
    worker: Optional[int] = None
    loops: Optional[int] = None
    csv: Optional[str] = None                       # the kept CSV (keep_csv or keep_transcripts), else None
    stdout_path: Optional[str] = None               # runs/<run_name>.out when kept
    core_status: Optional[str] = None               # parameters_known's element 1 (a loop label, or the sentinel)
    rss_mb: Optional[float] = None                  # the process's max RSS after the trial (memory watch of the pool)
    pid: Optional[int] = None

    def to_row(self):
        """The evaluate() contract of study.Runner: status, metrics, seconds, error, flags, feasible, files, worker."""
        return {'status': self.status, 'metrics': dict(self.metrics), 'seconds': self.seconds, 'error': self.error,
                'flags': list(self.flags), 'feasible': None, 'files': dict(self.files), 'worker': self.worker}

    def to_dict(self):
        return asdict(self)


# ----------------------------------------------------------------------------- process state

_TTY_SET = False
_WORKDIR = None


def ensure_core():
    """Import emulator_core once per process and call set_tty(0, 0, 'batch') (log_msg/sub_issue need it)."""
    global _TTY_SET
    import emulator_core as ec
    if not _TTY_SET:
        ec.set_tty(0, 0, 'batch')
        _TTY_SET = True
    return ec


def process_workdir():
    """One temp directory per process for the transcripts of trials that are not kept; removed at exit."""
    global _WORKDIR
    if _WORKDIR is None or not os.path.isdir(_WORKDIR):
        _WORKDIR = tempfile.mkdtemp(prefix=f'hindcast_replay_{os.getpid()}_')
        atexit.register(shutil.rmtree, _WORKDIR, True)
    return _WORKDIR


def rss_mb():
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(v / (1024.0 if sys.platform != 'darwin' else 1024.0 * 1024.0), 1)
    except Exception:                                # pragma: no cover
        return None


# ----------------------------------------------------------------------------- the sources

class DayLiveSource(ns_source.NightscoutSource):
    """The live source built over the whole UTC day(s) the window touches, yielding only the loops inside the window:
    dict-equal to the frozen day (which is the whole-day reconstruction), unlike a live source built for the
    sub-window itself (freeze.py docstring). Used by `source='live-day'` and hc check."""

    def __init__(self, url, start_iso, end_iso, settings=None, log_msg=print, verbose=True):
        super().__init__(url, start_iso, end_iso, settings=settings, log_msg=log_msg, verbose=verbose)
        self.want = self._window()
        wdays = days.days_of_window(*self.want)
        self._resolved = (day_bounds(wdays[0])[0], day_bounds(wdays[-1])[1])

    def loops(self):
        start, end = self.want
        for loop in super().loops():
            ms = loop.get('now_ms')
            if ms is not None and start <= ms < end:
                yield loop


def frozen_source_class(base_dir=None):
    """FrozenSource bound to a frozen/ root (the core constructs the class without a base_dir argument)."""
    if base_dir is None:
        return freeze.FrozenSource

    class BoundFrozenSource(freeze.FrozenSource):
        def __init__(self, url, start_iso, end_iso, settings=None, log_msg=print, verbose=True, **kw):
            kw.setdefault('base_dir', base_dir)
            super().__init__(url, start_iso, end_iso, settings=settings, log_msg=log_msg, verbose=verbose, **kw)

    BoundFrozenSource.__name__ = 'FrozenSource'
    return BoundFrozenSource


# The real ns_source.NightscoutSource, captured before anything swaps the module attribute (freeze.py captures it at
# its import, which happens before any install). `source='live'` installs exactly this class: a pool worker has the
# frozen source installed for its whole life (pool.default_init), so "keep whatever is installed" would replay frozen
# data under a 'live' label.
LIVE_SOURCE = getattr(freeze, '_LIVE_SOURCE', None) or ns_source.NightscoutSource


def source_class(source='frozen', frozen_dir=None):
    """The class `source_installed(source)` puts on ns_source.NightscoutSource."""
    if source not in SOURCES:
        raise ReplayError(f'source must be one of {SOURCES}, not {source!r}')
    if source == 'frozen':
        return frozen_source_class(frozen_dir)
    if source == 'live-day':
        return DayLiveSource
    cls = LIVE_SOURCE
    if isinstance(cls, type) and issubclass(cls, freeze.FrozenSource):
        raise ReplayError('source "live" asked, but the captured live class is a FrozenSource')
    return cls


def source_flag(source, cls):
    """The TrialResult flag naming the source a trial actually replayed: 'source:<mode>:<class name>'."""
    return f'source:{source}:{getattr(cls, "__name__", type(cls).__name__)}'


@contextlib.contextmanager
def source_installed(source='frozen', frozen_dir=None):
    """Inside the block `ns_source.NightscoutSource` (the real module attribute scanNightscout reads) is the
    requested source class - explicitly for every mode, also 'live' (LIVE_SOURCE), whatever was installed before -
    and the block gets that class; restored afterwards. Also the offline guard: no socket in any mode."""
    cls = source_class(source, frozen_dir)
    previous = ns_source.NightscoutSource
    ns_source.NightscoutSource = cls
    try:
        with freeze.offline_guard(enabled=True):
            yield cls
    finally:
        ns_source.NightscoutSource = previous


def install_frozen(frozen_dir=None):
    """Install the frozen source for the rest of the process (pool workers); returns the live class."""
    return freeze.install_frozen_source() if frozen_dir is None else _install(frozen_source_class(frozen_dir))


def _install(cls):
    live = ns_source.NightscoutSource
    ns_source.NightscoutSource = cls
    return live


# ----------------------------------------------------------------------------- the core call

def echo_banner(spec, freeze_sha=''):
    """The echo text parameters_known writes at the top of the .log (versions, hashes, identity of the trial)."""
    lines = ['=' * 66, 'Hindcast replay (software/hindcast/replay.py)', '-' * 66]
    try:
        for name, stamp in contracts.core_versions().items():
            lines.append(f' dated: {stamp}       module name: {name}')
    except Exception as e:                           # pragma: no cover - the stamps are informational
        lines.append(f' version stamps unavailable: {e}')
    start, end = spec.resolved_window()
    lines += ['-' * 66,
              f'day                   {spec.day}', f'window                {start} .. {end}',
              f'variant               {spec.variant}', f'vhash                 {spec.vhash}',
              f'key                   {spec.key or "-"}', f'source                {spec.source}',
              f'freeze_sha            {freeze_sha or "-"}',
              f'code_sha              {contracts.code_sha()}',
              f'outputs               {spec.outputs}', '=' * 66, '']
    return '\n'.join(lines)


def result_paths(workdir, label, vdf_name):
    """The files parameters_known writes for a VDF `<vdf_name>` over the window `label` in `workdir`."""
    var = vdf_name[:-4] if vdf_name.endswith('.vdf') else vdf_name
    base = os.path.join(workdir, label)
    return {'csv': f'{base}.{var}.csv', 'txt': f'{base}.{var}.txt', 'log': f'{base}.{var}.log',
            'delta': f'{base}.{var}.delta', 'orig': f'{base}.orig.txt'}


def call_core(spec, vdf_path, banner, out_stream, used=None):
    """parameters_known() with the source installed and stdout redirected; -> (return tuple, Pred, FlowChart).
    `used` (a list) receives the source class that was installed on ns_source for the call."""
    ec = ensure_core()
    start, end = spec.resolved_window()
    arg2 = 'Windows/' + spec.outputs.strip('/') + ('' if 'noplot' in spec.outputs.split('/') else '/noplot') + '/.'
    with contextlib.redirect_stdout(out_stream), source_installed(spec.source, spec.frozen_dir) as cls:
        if used is not None:
            used.append(cls)
        ret = ec.parameters_known(spec.url, arg2, vdf_path, start, end, {}, banner, '.', datetime.datetime(1970, 1, 1))
    pred = getattr(ec, 'Pred', None)
    flows = getattr(ec, 'FlowChart', None)
    pred = dict(pred) if isinstance(pred, dict) and pred else None       # copied at once: the next call resets them
    flows = dict(flows) if isinstance(flows, dict) and flows else None
    return ret, pred, flows


# ----------------------------------------------------------------------------- metrics inputs

def min_guard_list(pred, rows):
    """Emulated minGuardBG per CSV row from Pred[...]['Levels'] (None where the loop carries none)."""
    out = []
    for t in export.loop_times(rows):
        f = export.pred_lookup(pred, t) if pred else None
        levels = f.get('Levels') if isinstance(f, dict) else None
        out.append(hm.min_guard_from_levels(levels if isinstance(levels, dict) else None))
    return out


def bridged_flags(rows, meta):
    """Per CSV row: the loop lies in a stretch replayed on CGM readings bridged from devicestatus (the frozen meta's
    character.cgm_bridged_stretches, H-714; ms bounds, inclusive); None when the meta names no stretch."""
    stretches = ((meta or {}).get('character') or {}).get('cgm_bridged_stretches') or []
    if not stretches:
        return None
    return [any(a / 1000.0 - 0.5 <= t <= b / 1000.0 + 0.5 for a, b, _ in stretches) for t in export.loop_times(rows)]


def caps_for(spec, settings):
    a = spec.assignment()
    return (effective_cap(a, spec.base_lines, settings, 'autoISF_max'),
            effective_cap(a, spec.base_lines, settings, 'autoISF_min'))


def _load_settings(path):
    try:
        return days.load_settings(path)
    except (OSError, ValueError):
        return {}


def log_problem(log_path, n=600):
    """The 'Problem in VDF-file ...' block the core writes into the .log on a SYNTAX result ('' when absent): the
    stdout only points at the .log, which the cleanup deletes, so the failed row carries the block itself."""
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except OSError:
        return ''
    i = text.rfind('Problem in VDF-file')
    if i < 0:
        return ''
    block = ' '.join(l.strip() for l in text[i:].splitlines() if l.strip() and set(l.strip()) != {'*'})
    return block[:n]


def _tail(text, n=ERROR_TAIL):
    text = (text or '').strip()
    return text[-n:] if len(text) > n else text


def _rel(path, root):
    try:
        return os.path.relpath(path, root)
    except ValueError:                               # pragma: no cover - different drives on Windows
        return path


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ----------------------------------------------------------------------------- run_trial

def run_trial(spec, worker=None):
    """Replay one TrialSpec; never raises for a core failure (that is a failed TrialResult). Module docstring."""
    t0 = time.time()
    res = TrialResult(status='failed', day=spec.day, vhash=spec.vhash, variant=spec.variant, params=dict(spec.params),
                      key=spec.key, control=spec.control, worker=worker, pid=os.getpid())
    settings = _load_settings(spec.settings_path)
    if spec.keep_csv:
        _remove(spec.keep_csv)                       # a failed/blocked trial must not leave an earlier run's CSV there

    # 0. blocked by the orchestrator (the base replay of this day failed in this run)?
    if spec.blocked:
        res.status, res.error = 'blocked', str(spec.blocked)
        res.seconds = round(time.time() - t0, 3)
        return res

    # 1. blocked?
    freeze_sha = ''
    meta = None
    if spec.source == 'frozen':
        try:
            reason = freeze.blocked_reason(spec.day, spec.url, settings, base_dir=spec.frozen_dir,
                                           settings_path=spec.settings_path, require_final=spec.require_final)
        except (ValueError, OSError) as e:
            reason = f'{spec.day}: frozen state unknown ({e})'
        if reason:
            res.status, res.error = 'blocked', reason
            res.seconds = round(time.time() - t0, 3)
            return res
        meta = freeze.read_meta(spec.day, days.host_of(spec.url, settings), spec.frozen_dir) or {}
        freeze_sha = meta.get('sha1', '')

    # 2. work dir and VDF
    if spec.keep_transcripts and spec.runs_dir:
        workdir = os.path.join(spec.runs_dir, spec.run_name())
        os.makedirs(workdir, exist_ok=True)
    else:
        workdir = process_workdir()
    label = spec.window_label()
    vdf_path = os.path.join(workdir, spec.vdf_name())
    paths = result_paths(workdir, label, spec.vdf_name())
    for p in paths.values():
        _remove(p)                                   # never read a stale result of an earlier trial in this dir
    try:
        spc.write_variant_vdf(vdf_path, spec.params, spec.base_lines, names=spec.names)
    except OSError as e:
        res.error, res.seconds = f'cannot write the VDF {vdf_path}: {e}', round(time.time() - t0, 3)
        return res

    # 3./4. the core, stdout captured
    out_path = None
    if spec.runs_dir:
        os.makedirs(spec.runs_dir, exist_ok=True)
        out_path = os.path.join(spec.runs_dir, f'{spec.run_name()}.out')
        out_stream = open(out_path, 'w', encoding='utf-8')
    else:
        out_stream = io.StringIO()
    banner = echo_banner(spec, freeze_sha)
    used = []
    try:
        try:
            ret, pred, flows = call_core(spec, vdf_path, banner, out_stream, used=used)
        except Exception as e:                       # a crash inside the core is a failed trial, not a dead worker
            import traceback
            out_stream.write('\n' + traceback.format_exc())
            ret, pred, flows = (0, f'EXC {type(e).__name__}: {e}', 0, '', '', 0, '', None), None, None
    finally:
        if out_path:
            out_stream.close()
            captured = open(out_path, 'r', encoding='utf-8', errors='replace').read()
        else:
            captured = out_stream.getvalue()
    seconds_core = time.time() - t0
    if used:
        res.flags.append(source_flag(spec.source, used[0]))
    core_status = str(ret[1]) if isinstance(ret, tuple) and len(ret) > 1 else str(ret)
    res.core_status = core_status

    # 5. status
    failed_reason = None
    if core_status in FAILED_STATUSES or core_status.startswith('EXC '):
        failed_reason = core_status
    elif not os.path.isfile(paths['csv']):
        failed_reason = 'no CSV written'
    if failed_reason:
        res.status = 'failed'
        problem = log_problem(paths['log'])
        res.error = f'{failed_reason}: {_tail(captured)}'.strip() + (f'\n{problem}' if problem else '')
        res.stdout_path = out_path
        res.seconds = round(time.time() - t0, 3)
        if not spec.keep_transcripts:
            for p in list(paths.values()) + [vdf_path]:
                _remove(p)
        return res

    try:
        # 6. Pred / FlowChart
        if pred is None:
            res.flags.append('no_pred')
        names, rows = read_result_csv(paths['csv'])
        res.loops = len(rows)
        if meta and spec.whole_day() and meta.get('loops') not in (None, len(rows)):
            res.flags.append(f'loops_ne_frozen:{len(rows)}/{meta.get("loops")}')

        # 7. metrics
        cap_max, cap_min = caps_for(spec, settings)
        tdd = spec.tdd_phone_U
        if tdd is None and meta and spec.whole_day():
            tdd = ((meta.get('character') or {}).get('tdd_U'))
        mg = min_guard_list(pred, rows) if pred else None
        br = bridged_flags(rows, meta)
        base = spec.base_csv if (spec.base_csv and os.path.isfile(spec.base_csv)) else None
        if base is None and spec.params:
            res.flags.append('no_base_csv')
        try:
            m = hm.trial_metrics(paths['csv'], base=base, autoisf_max=cap_max, autoisf_min=cap_min, min_guard=mg,
                                 tdd_phone_U=tdd, bridged=br)
        except ValueError as e:                      # loop counts differ from the base: compare with itself, say so
            if base is None:
                raise
            nb = len(read_result_csv(base)[1])
            res.flags.append(f'base_loop_mismatch:{len(rows)}/{nb}')
            m = hm.trial_metrics(paths['csv'], base=None, autoisf_max=cap_max, autoisf_min=cap_min, min_guard=mg,
                                 tdd_phone_U=tdd, bridged=br)
            m['base_loop_mismatch'] = str(e)[:200]
        for f in m.pop('flags', []) or []:
            if f not in res.flags:
                res.flags.append(f)
        m['seconds_core'] = round(seconds_core, 3)
        res.metrics = m

        # 8. tier-2 files
        if spec.report_dir:
            root = spec.files_root or os.path.dirname(os.path.abspath(spec.report_dir))
            window = spec.resolved_window()
            emul, orig = export.series_from_rows(names, rows, day=spec.day, vhash=spec.vhash, variant=spec.variant,
                                                 pred=pred, window=window)
            path, _ = export.write_tier2(spec.report_dir, emul)
            res.files['series'] = _rel(path, root)
            path, _ = export.write_tier2(spec.report_dir, orig, overwrite=False)
            res.files['orig'] = _rel(path, root)
            if spec.pred and pred:
                txt = paths['txt'] if os.path.isfile(paths['txt']) else None
                doc, text = export.pred_from_globals(spec.day, spec.vhash, rows, pred, flows, transcript=txt,
                                                     variant=spec.variant, window=window)
                path, _ = export.write_tier2(spec.report_dir, doc)
                res.files['pred'] = _rel(path, root)
                path, _ = export.write_tier2(spec.report_dir, text)
                res.files['pred_text'] = _rel(path, root)
                orig_txt = paths['orig'] if os.path.isfile(paths['orig']) else None
                pdoc, ptext = export.pred_orig_from_globals(spec.day, rows, pred, transcript=orig_txt, window=window)
                path, _ = export.write_tier2(spec.report_dir, pdoc, overwrite=False)
                res.files['pred_orig'] = _rel(path, root)
                path, _ = export.write_tier2(spec.report_dir, ptext, overwrite=False)
                res.files['pred_orig_text'] = _rel(path, root)
            elif spec.pred:
                res.flags.append('pred_not_written')

        # 9. keep / clean
        if spec.keep_csv:
            os.makedirs(os.path.dirname(os.path.abspath(spec.keep_csv)) or '.', exist_ok=True)
            shutil.copyfile(paths['csv'], spec.keep_csv)
            res.csv = spec.keep_csv
        if spec.keep_transcripts:
            res.csv = res.csv or paths['csv']
            res.stdout_path = out_path
        else:
            for p in list(paths.values()) + [vdf_path]:
                _remove(p)
            if out_path:
                _remove(out_path)
        res.status = 'done'
    except Exception as e:                           # the replay ran; the post-processing failed: one failed row
        import traceback
        res.status = 'failed'
        res.error = f'{type(e).__name__}: {e}\n{_tail(traceback.format_exc(), 600)}'
        res.stdout_path = out_path
        if not spec.keep_transcripts:
            for p in list(paths.values()) + [vdf_path]:
                _remove(p)
    res.seconds = round(time.time() - t0, 3)
    res.rss_mb = rss_mb()
    return res


# ----------------------------------------------------------------------------- the study context (tasks -> specs)

@dataclass
class ReplayContext:
    """The per-study constants a replay needs, and the task -> spec -> row glue for study.Runner / pool.py.

        ctx = ReplayContext.from_study(sd, study, settings, pred=False, keep_transcripts=False)
        spec = ctx.spec_for(task)                 # study.Task -> TrialSpec (base CSV of the day attached when known)
        res = run_trial(spec); ctx.absorb(res)    # remembers the base CSV path of the day
        runner.run(tasks, ctx.evaluate)           # serial: evaluate = spec_for + run_trial + absorb + to_row

    Base CSVs live under runs/base/<day>.csv (kept by `keep_csv`); a variant of a day whose base has not run yet is
    flagged `no_base_csv`, so the orchestrator runs the base tasks of a round first (pool.run_tasks does).
    A base replay that fails (or is blocked) in this run invalidates the day for this run: absorb() drops the day's
    base CSV (also one pre-loaded from an earlier run; run_trial already removed the file at keep_csv) and records the
    reason in `base_failed`, and every later non-base spec of that day carries `blocked`, so its trial returns
    status 'blocked' instead of being compared with a stale base. A later successful base of the day clears it."""
    base_lines: list = field(default_factory=list)
    outputs: str = DEFAULT_OUTPUTS
    names: Optional[list] = None
    report_dir: Optional[str] = None
    runs_dir: Optional[str] = None
    url: str = 'ns:'
    settings_path: str = contracts.NS_SETTINGS_PATH
    frozen_dir: Optional[str] = None
    pred: bool = False
    keep_transcripts: bool = False
    source: str = 'frozen'
    require_final: bool = True
    window: Optional[tuple] = None
    files_root: Optional[str] = None
    key_of: Any = None                              # callable(task) -> key (study.Runner.key_of); optional
    base_csvs: dict = field(default_factory=dict)   # day -> path (filled by absorb(); pre-filled from runs/base/)
    base_failed: dict = field(default_factory=dict)  # day -> why the base replay of the day failed in this run

    @classmethod
    def from_study(cls, study_dir, study, settings=None, **kw):
        """study_dir: study.StudyDir (or a path); study: contracts.Study. kw override any field."""
        path = getattr(study_dir, 'path', study_dir)
        runs = kw.pop('runs_dir', os.path.join(path, 'runs'))
        report = kw.pop('report_dir', os.path.join(path, 'report'))
        ctx = cls(base_lines=list(study.base_vdf_lines), outputs=study.outputs or DEFAULT_OUTPUTS,
                  names=list(study.space), report_dir=report, runs_dir=runs, url=study.source or 'ns:',
                  files_root=path, **kw)
        base_dir = os.path.join(runs, 'base')
        if os.path.isdir(base_dir):
            for fn in os.listdir(base_dir):
                if fn.endswith('.csv') and days.is_day(fn[:-4]):
                    ctx.base_csvs[fn[:-4]] = os.path.join(base_dir, fn)
        return ctx

    def base_csv_path(self, day):
        return os.path.join(self.runs_dir or process_workdir(), 'base', f'{day}.csv')

    def is_base(self, task):
        return not task.params or getattr(task, 'control', None) == 'base'

    def spec_for(self, task):
        """study.Task -> TrialSpec. The base task of a day keeps its CSV at runs/base/<day>.csv."""
        params = dict(task.params or {})
        base = self.is_base(task)
        key = None
        if self.key_of is not None:
            try:
                key = self.key_of(task)
            except Exception:
                key = None
        meta = getattr(task, 'meta', None) or {}
        blocked = None
        if not base and task.day in self.base_failed:
            blocked = (f'{task.day}: the base replay of the day did not finish in this run, so no trial of the day is '
                       f'compared with an earlier base ({self.base_failed[task.day]})')
        return TrialSpec(day=task.day, params=params, base_lines=list(self.base_lines), outputs=self.outputs,
                         window=meta.get('window', self.window), names=self.names, key=key,
                         variant=getattr(task, 'variant', None), control=getattr(task, 'control', None),
                         pred=bool(meta.get('pred', self.pred)), keep_transcripts=self.keep_transcripts,
                         report_dir=self.report_dir, runs_dir=self.runs_dir,
                         base_csv=None if base else self.base_csvs.get(task.day),
                         keep_csv=self.base_csv_path(task.day) if base else None,
                         url=self.url, settings_path=self.settings_path, frozen_dir=self.frozen_dir,
                         source=self.source, require_final=self.require_final, files_root=self.files_root,
                         instance=meta.get('instance'), blocked=blocked)

    def absorb(self, result):
        """Remember a finished base replay's CSV for the variants of that day; a base that did not finish
        invalidates the day for this run (class docstring)."""
        if not result.params or result.control == 'base':
            if result.status == 'done' and result.csv:
                self.base_csvs[result.day] = result.csv
                self.base_failed.pop(result.day, None)
            elif result.status != 'done':
                self.base_csvs.pop(result.day, None)
                why = ' '.join(str(result.error or result.status).split())
                self.base_failed[result.day] = f'{result.status}: {why[:200]}'
        return result

    def evaluate(self, task):
        """The serial evaluate(task) for study.Runner.run."""
        res = run_trial(self.spec_for(task))
        self.absorb(res)
        return res.to_row()


def freeze_sha_of(url='ns:', settings=None, base_dir=None):
    """callable(day) -> the frozen day's sha1 ('' when not frozen): the Runner's `freeze_sha_of` argument."""
    settings = settings if settings is not None else _load_settings(contracts.NS_SETTINGS_PATH)
    host = days.host_of(url, settings)

    def of(day):
        meta = freeze.read_meta(day, host, base_dir)
        return (meta or {}).get('sha1', '') or ''
    return of


# ----------------------------------------------------------------------------- CLI (one replay, for hand checks)

def register(subparsers):
    """`hc replay <day> [--param "profile k" v ...] [--window a b] [--source frozen|live-day|live] [--pred] [--keep]`:
    one trial, the metrics printed. cli.py calls this; the parser's func is run_replay."""
    p = subparsers.add_parser('replay', help='replay one variant on one frozen day and print its metrics',
                              description='One (variant, day) replay through emulator_core.parameters_known() with the '
                                          'frozen source; no socket is opened. Prints the metrics as JSON.')
    p.add_argument('day', help='UTC day YYYY-MM-DD (frozen)')
    p.add_argument('--param', action='append', nargs=2, metavar=('"ARRAY ITEM"', 'VALUE'), default=[])
    p.add_argument('--base-vdf', help='VDF rows every variant carries')
    p.add_argument('--window', nargs=2, metavar=('START', 'END'), help='ISO bounds instead of the whole day')
    p.add_argument('--source', choices=SOURCES, default='frozen')
    p.add_argument('--out', help='report directory for the tier-2 files (none = metrics only)')
    p.add_argument('--runs', help='runs directory for stdout capture / kept transcripts (default: temp)')
    p.add_argument('--pred', action='store_true')
    p.add_argument('--keep', action='store_true', help='keep the transcripts (needs --runs)')
    p.add_argument('--base-csv', help='the base replay CSV of the day (metrics vs base)')
    p.add_argument('--frozen-dir', default=None)
    p.add_argument('--url', default='ns:')
    p.set_defaults(func=run_replay)
    return p


def run_replay(args):
    import json
    import emulator_optimize as _opt
    params = {}
    for name, value in args.param or []:
        contracts.split_param(name)
        params[name] = _opt.parse_literal(value)
    base_lines = []
    if args.base_vdf:
        with open(args.base_vdf, 'r', encoding='utf-8') as fh:
            base_lines = [l.rstrip('\n') for l in fh if l.strip()]
    spec = TrialSpec(day=args.day, params=params, base_lines=base_lines, window=tuple(args.window) if args.window else None,
                     source=args.source, report_dir=args.out, runs_dir=args.runs, pred=args.pred,
                     keep_transcripts=bool(args.keep and args.runs), base_csv=args.base_csv, frozen_dir=args.frozen_dir,
                     url=args.url)
    res = run_trial(spec)
    out = res.to_dict()
    print(json.dumps(out, indent=1, default=str))
    print('')
    print(contracts.HONESTY_STATEMENT)
    return 0 if res.status == 'done' else 2


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='python -m hindcast.replay')
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
