"""
study.py  -  the study directory of a Hindcast run (backlog H-301; DESIGN.md sections 4.2, 4.5, 4.6)

    studies/<study>/study.json      written once by `hc study new`, frozen afterwards (contracts.Study)
    studies/<study>/trials.jsonl    append-only, one contracts.TrialRow per (variant, day); O_APPEND, one write() per line
    studies/<study>/LOCK            the lock itself: {pid, heartbeat}, created atomically (hard link of a complete
                                    temp file, O_EXCL semantics), removed on a clean release
    studies/<study>/STATE.json      the run's state and heartbeat (contracts.State), rewritten by atomic rename
    studies/<study>/STOP            a request to stop: the running trial finishes, status is written, exit code 0
    studies/<study>/status.json     <= 2 KB summary for the skill (contracts.Status), rewritten after every trial
    studies/<study>/round.json      written by the ranking step (H-502); read here only to fill status.top

Lock rule (pid + heartbeat). A run owns the study while LOCK names its pid and STATE.json (same pid) has no
exit_code and a heartbeat younger than LOCK_STALE_S (10 min); in the moment between creating LOCK and the first
STATE.json write the LOCK's own stamp is the heartbeat. A second `hc study run` on the same study is refused with
exit code 3 (LockHeld). The lock is taken over when the heartbeat is older than 10 min, or when the pid is no longer
alive on this machine (a `kill -9` leaves a fresh heartbeat and a dead pid), or when the previous run recorded an
exit_code. Acquisition is atomic: the LOCK file can only be created by one process (two simultaneous runs: exactly
one acquires), and a stale LOCK is taken over under a mutex directory (`LOCK.takeover`, os.mkdir) so that only one
taker judges and replaces it, and a plain acquirer that slips in keeps its lock. Taking over also removes a STOP
file left behind: the stop request was addressed to the earlier run.

Resume rule. A key (contracts.key over params, day, settings_sha, code_sha, freeze_sha, objective_version) with
a `done` row is never evaluated again; `failed`, `blocked` and `stale` rows are re-evaluated. A row is appended
only after its evaluation finished, so a `kill -9` loses at most the trial in flight; a partial last line is
skipped by contracts.read_trials. Where a key has several rows the last one counts.

Exit codes (DESIGN.md section 4.5): 0 finished or stopped (STOP file, Ctrl-C), 2 stopped by a guard
(GuardStop: three consecutive failures, disk below the minimum, and the sentinel / null / stale checks the
orchestrator raises), 3 configuration refused (StudyError: locked key, unknown VDF key, lock held, bad study).

What is NOT here: the search phases (search.py, H-304), the replay (replay.py, H-102), the pool (pool.py) and the
orchestrator that strings them together (H-305). `Runner.run(tasks, evaluate)` is the loop they plug into:
`evaluate(task)` returns the row fields of one evaluation, the Runner does lock, STATE, STOP, resume, exit codes,
status.json. Until the orchestrator exists, `cli.py` runs the study's queue through `dry_evaluate()`, which
records every task as `blocked` (nothing is replayed) so that the lock/STATE/STOP machinery can be exercised.

Nothing here changes the phone or ns_settings.json.
"""
import datetime
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast.contracts import (ContractError, Phase, Round, State, Status, Study, StudyDay, TopItem, TrialRow,  # noqa: E402
                                BASE_VHASH, HONESTY_STATEMENT, NS_SETTINGS_PATH, STATUS_MAX_BYTES, append_trial,
                                read_trials, now_iso)
from hindcast import space as spc                                                           # noqa: E402

STUDIES_DIR = os.path.join(SOFTWARE_DIR, 'studies')
STUDY_JSON, TRIALS_JSONL, STATE_JSON, STOP_FILE, STATUS_JSON, ROUND_JSON, LOCK_FILE = (
    'study.json', 'trials.jsonl', 'STATE.json', 'STOP', 'status.json', 'round.json', 'LOCK')
LOCK_STALE_S = 600                                   # heartbeat older than this: the lock may be taken over
LOCK_ATTEMPTS = 5                                    # acquire rounds before giving up (the lock keeps changing hands)
LOCK_RETRY_S = 0.05                                  # pause between those rounds
TAKEOVER_STALE_S = 60                                # a takeover mutex older than this was left by a killed taker
MIN_DISK_MB = 2048                                   # DESIGN.md: < 2 GB free stops the run (exit 2)
MAX_CONSECUTIVE_FAILURES = 3
EXIT_OK, EXIT_GUARD, EXIT_REFUSED = 0, 2, 3
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class StudyError(Exception):
    """Configuration refused: exit code 3. The message names the offending key / file."""
    exit_code = EXIT_REFUSED


class LockHeld(StudyError):
    """Another run owns the study (fresh heartbeat, live pid): exit code 3."""


class GuardStop(Exception):
    """A guard stopped the run: exit code 2. `reason` goes into STATE.json and status.json."""
    exit_code = EXIT_GUARD

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# ----------------------------------------------------------------------------- small parsers (command line -> study.json)

def parse_days(spec):
    """'2026-09-01..09-07', '2026-09-01..2026-09-07', '2026-09-06', comma lists of these -> ['YYYY-MM-DD', ...] (UTC days,
    ascending inside a range, duplicates dropped, order of appearance kept)."""
    out = []
    for part in str(spec).replace(' ', '').split(','):
        if not part:
            continue
        if '..' in part:
            a, b = part.split('..', 1)
            if not _DAY_RE.match(a):
                raise StudyError(f'days: "{a}" is not a YYYY-MM-DD date')
            if re.match(r'^\d{2}-\d{2}$', b):
                b = a[:4] + '-' + b
            elif re.match(r'^\d{2}$', b):
                b = a[:7] + '-' + b
            if not _DAY_RE.match(b):
                raise StudyError(f'days: "{b}" is not a YYYY-MM-DD date')
            try:
                d0, d1 = (datetime.date.fromisoformat(x) for x in (a, b))
            except ValueError as e:
                raise StudyError(f'days: {e}') from None
            if d1 < d0:
                raise StudyError(f'days: range {a}..{b} runs backwards')
            if (d1 - d0).days > 366:
                raise StudyError(f'days: range {a}..{b} is longer than a year')
            d = d0
            while d <= d1:
                out.append(d.isoformat())
                d += datetime.timedelta(days=1)
        else:
            if not _DAY_RE.match(part):
                raise StudyError(f'days: "{part}" is not a YYYY-MM-DD date')
            try:
                datetime.date.fromisoformat(part)                  # a real calendar date
            except ValueError as e:
                raise StudyError(f'days: {e}') from None
            out.append(part)
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def parse_tag(spec):
    """'2026-09-05=weekend,sport' -> ('2026-09-05', ['weekend', 'sport'])."""
    if '=' not in str(spec):
        raise StudyError(f'tag "{spec}": expected <day>=<tag>[,<tag>...]')
    day, tags = str(spec).split('=', 1)
    day = day.strip()
    if not _DAY_RE.match(day):
        raise StudyError(f'tag "{spec}": "{day}" is not a YYYY-MM-DD date')
    return day, [t.strip() for t in tags.split(',') if t.strip()]


_CONSTRAINT_SPLIT = re.compile(r'\s+(<=|<)\s+')


def parse_constraint(text):
    """'profile a <= profile b < profile c' -> ['profile a', '<=', 'profile b', '<', 'profile c'] (space.Constraint tokens)."""
    tokens = [t.strip() for t in _CONSTRAINT_SPLIT.split(str(text).strip())]
    if len(tokens) < 3 or len(tokens) % 2 == 0:
        raise StudyError(f'constraint "{text}": expected "<array> <item> <= <array> <item> [<= ...]"')
    for name in tokens[0::2]:
        try:
            contracts.split_param(name)
        except ContractError as e:
            raise StudyError(f'constraint "{text}": {e}') from None
    return tokens


def parse_phase(spec):
    """'oaat', 'oaat:points=5', 'lhs:n=24,seed=7', 'halving:rungs=2/5/all', 'validate:top=8,days=holdout' -> Phase."""
    import emulator_optimize as _opt
    kind, _, opts = str(spec).strip().partition(':')
    kw = {}
    for item in [o for o in opts.split(',') if o.strip()]:
        if '=' not in item:
            raise StudyError(f'phase "{spec}": option "{item}" needs key=value')
        k, v = (x.strip() for x in item.split('=', 1))
        if k == 'rungs':
            kw[k] = [_opt.parse_literal(x) for x in v.split('/') if x]
        elif k in ('sampler', 'days'):
            kw[k] = v
        else:
            kw[k] = _opt.parse_literal(v)
    try:
        return Phase(kind=kind, **kw)
    except TypeError as e:
        raise StudyError(f'phase "{spec}": {e}') from None


def read_vdf_lines(path):
    """Non-blank lines of a base VDF (what emulator_optimize keeps as base_lines)."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return [line.rstrip('\n') for line in fh if line.strip()]
    except OSError as e:
        raise StudyError(f'base VDF {path}: {e}') from None


# ----------------------------------------------------------------------------- the directory

def resolve_study_dir(name_or_path, studies_dir=STUDIES_DIR):
    """'<name>' -> studies/<name>; a directory (holding or meant to hold study.json) or a study.json path -> that directory."""
    arg = str(name_or_path)
    if arg.endswith(STUDY_JSON) and (os.path.isfile(arg) or os.sep in arg):
        return os.path.abspath(os.path.dirname(arg) or '.')
    if os.path.isdir(arg) or os.sep in arg or arg.startswith('.'):
        return os.path.abspath(arg)
    if not _NAME_RE.match(arg):
        raise StudyError(f'study name "{arg}" must be a short file-system safe label (letters, digits, . _ -)')
    return os.path.join(studies_dir, arg)


def pid_alive(pid):
    """True / False for a pid on this machine; None when it cannot be told (no os.kill, foreign pid)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _parse_iso(text):
    try:
        return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _state_holder(state, now, stale_s):
    """The STATE.json part of the rule: None when free (no state, a recorded exit_code, a heartbeat older than
    `stale_s`, our own pid, or a pid that is no longer alive), else who holds it."""
    if state is None or state.exit_code is not None:
        return None
    hb = _parse_iso(state.heartbeat)
    age = None if hb is None else now - hb
    if age is None or age > stale_s:
        return None
    if state.pid == os.getpid():
        return None                                              # the same process re-acquiring (sequential phases)
    alive = pid_alive(state.pid)
    if alive is False:
        return None
    return f'pid {state.pid}, heartbeat {int(age)} s ago, phase {state.phase}'


def lock_holder(state, now=None, stale_s=LOCK_STALE_S, lock=None):
    """None when the study is free, else a text saying who holds it (pid, heartbeat age).

    `lock` is the LOCK file's {pid, heartbeat} (None when there is none). With a LOCK: free when its pid is ours or
    no longer alive; when STATE.json belongs to that pid the STATE rule decides (exit_code, heartbeat age); when
    STATE.json is not that pid's yet (the holder has just created the LOCK) the LOCK's own stamp is the heartbeat.
    Without a LOCK (a study from before the LOCK file existed) the STATE rule alone decides."""
    now = time.time() if now is None else now
    if lock is None:
        return _state_holder(state, now, stale_s)
    pid = int(lock.get('pid') or 0)
    if pid == os.getpid():
        return None
    if pid and pid_alive(pid) is False:
        return None                                              # the holder died (kill -9): the lock may be taken over
    if state is not None and state.pid == pid:
        return _state_holder(state, now, stale_s)
    hb = _parse_iso(lock.get('heartbeat'))
    age = None if hb is None else now - hb
    if age is None or age > stale_s:
        return None
    return f'pid {pid or "?"}, lock taken {int(age)} s ago, STATE.json not written yet'


@dataclass
class RunResult:
    exit_code: int
    stop_reason: str
    done: int = 0                   # rows appended by this run
    skipped: int = 0                # tasks whose key already had a done row
    failed: int = 0
    blocked: int = 0


class StudyDir:
    """Paths and file rules of one study directory. Construct with the directory (see resolve_study_dir)."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path.rstrip(os.sep))
        self.study_json = os.path.join(self.path, STUDY_JSON)
        self.trials_path = os.path.join(self.path, TRIALS_JSONL)
        self.state_path = os.path.join(self.path, STATE_JSON)
        self.stop_path = os.path.join(self.path, STOP_FILE)
        self.lock_path = os.path.join(self.path, LOCK_FILE)
        self.status_path = os.path.join(self.path, STATUS_JSON)
        self.round_path = os.path.join(self.path, ROUND_JSON)
        self.runs_dir = os.path.join(self.path, 'runs')
        self.report_dir = os.path.join(self.path, 'report')

    def __repr__(self):
        return f'StudyDir({self.path!r})'

    def exists(self):
        return os.path.isfile(self.study_json)

    def require(self):
        if not self.exists():
            raise StudyError(f'{self.path}: no {STUDY_JSON} (create the study with `hc study new`)')
        return self

    # ---- study.json
    def load_study(self, settings=None):
        """The Study, validated (ContractError -> StudyError, exit 3; a locked key is named in the message)."""
        self.require()
        try:
            study = Study.load(self.study_json)
            study.validate(settings)
        except ContractError as e:
            raise StudyError(f'{self.study_json}: {e}') from None
        return study

    def write_study(self, study):
        contracts.write_json_atomic(self.study_json, study.to_dict())

    def update_phase_state(self, index, state):
        """The one field of study.json that changes after creation: phases[index].state."""
        study = Study.load(self.study_json)
        if 0 <= index < len(study.phases):
            study.phases[index].state = state
            self.write_study(study)
        return study

    # ---- trials.jsonl
    def trials(self):
        return read_trials(self.trials_path, strict=False)

    @staticmethod
    def latest_by_key(rows):
        """{key: row} where the last row of a key wins (a re-evaluation supersedes a failed/blocked/stale row)."""
        out = {}
        for r in rows:
            out[r.key] = r
        return out

    def done_keys(self, rows=None):
        rows = self.trials() if rows is None else rows
        return {k for k, r in self.latest_by_key(rows).items() if r.status == 'done'}

    def next_trial_number(self, rows=None):
        rows = self.trials() if rows is None else rows
        return max((r.trial for r in rows), default=-1) + 1

    def append(self, row):
        """Validate and append one TrialRow (O_APPEND, one write). A torn last line (kill -9 inside the previous
        write) is first terminated with a newline so that it is skipped alone and does not swallow this row."""
        try:
            row.validate()
        except ContractError as e:
            raise StudyError(f'trial row refused: {e}') from None
        os.makedirs(self.path, exist_ok=True)
        self.repair_trailing_newline()
        append_trial(self.trials_path, row)
        return row

    def repair_trailing_newline(self):
        """True when trials.jsonl ended without a newline and one was appended (the torn line stays unreadable)."""
        try:
            size = os.path.getsize(self.trials_path)
        except OSError:
            return False
        if size == 0:
            return False
        with open(self.trials_path, 'rb') as fh:
            fh.seek(-1, os.SEEK_END)
            last = fh.read(1)
        if last == b'\n':
            return False
        fd = os.open(self.trials_path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, b'\n')
        finally:
            os.close(fd)
        return True

    # ---- STATE.json / STOP
    def read_state(self):
        """State or None (missing, half-written or foreign file: None, never an exception)."""
        try:
            return State.load(self.state_path, strict=False)
        except (OSError, ValueError, ContractError):
            return None

    def write_state(self, state):
        os.makedirs(self.path, exist_ok=True)
        state.save(self.state_path)                          # atomic rename
        return state

    def request_stop(self, note='hc study stop'):
        os.makedirs(self.path, exist_ok=True)
        with open(self.stop_path, 'w', encoding='utf-8') as fh:
            fh.write(f'{now_iso()} {note}\n')
        return self.stop_path

    def stop_requested(self):
        return os.path.isfile(self.stop_path)

    def clear_stop(self):
        try:
            os.remove(self.stop_path)
        except OSError:
            pass

    # ---- LOCK
    def read_lock(self):
        """{'pid': int, 'heartbeat': iso} of the LOCK file; None when there is none. An unreadable file (not JSON)
        is reported as pid 0 with no heartbeat, which the rule treats as free (takeover)."""
        try:
            with open(self.lock_path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            return {'pid': int(data.get('pid') or 0), 'heartbeat': str(data.get('heartbeat') or '')}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError):
            return {'pid': 0, 'heartbeat': ''}

    def try_create_lock(self, pid=None):
        """Create LOCK holding {pid, heartbeat} atomically; True when this call created it, False when it exists.

        The content is written to a temp file first and hard-linked to LOCK: the link either creates LOCK complete
        (so no reader ever sees a half-written lock) or fails because LOCK exists. Where linking is not possible the
        fallback is os.open(O_CREAT | O_EXCL) and a single write."""
        os.makedirs(self.path, exist_ok=True)
        payload = json.dumps({'pid': int(pid or os.getpid()), 'heartbeat': now_iso()}).encode('utf-8')
        tmp = f'{self.lock_path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'wb') as fh:
                fh.write(payload)
            try:
                os.link(tmp, self.lock_path)
                return True
            except FileExistsError:
                return False
            except (AttributeError, NotImplementedError, OSError):
                pass                                             # no hard links here: O_EXCL below
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    def steal_lock(self):
        """Remove the LOCK file by renaming it away first (of two concurrent removers only one rename succeeds; the
        other finds the file gone and gets False). Only `remove_lock` (own lock) and `take_over_lock` (a lock the
        rule found free, under the takeover mutex) call this. True when this call removed it."""
        stale = f'{self.lock_path}.{os.getpid()}.stale'
        try:
            os.replace(self.lock_path, stale)
        except FileNotFoundError:
            return False
        try:
            os.remove(stale)
        except OSError:
            pass
        return True

    def remove_lock(self, pid=None):
        """Remove LOCK when it is held by `pid` (default: this process); True when removed."""
        lock = self.read_lock()
        if lock is None or lock['pid'] != int(pid or os.getpid()):
            return False
        return self.steal_lock()

    def take_over_lock(self, now=None):
        """Replace a LOCK the rule finds free (dead pid, stale heartbeat, recorded exit) with ours; True when this
        process now holds the lock, False when it must re-check (the lock is held after all, another taker holds the
        takeover mutex, or a plain acquirer created a fresh LOCK in between).

        Takers are serialised by the mutex `LOCK.takeover` (a directory: os.mkdir is atomic), so only one process
        at a time judges and removes a lock; inside the mutex the lock is judged again, renamed away, and the new
        LOCK is created with try_create_lock, which fails (False) when a plain acquirer created one in the gap. A
        mutex left behind by a taker killed inside it is removed once it is older than TAKEOVER_STALE_S."""
        mutex = self.lock_path + '.takeover'
        try:
            os.mkdir(mutex)
        except FileExistsError:
            try:
                if time.time() - os.stat(mutex).st_mtime > TAKEOVER_STALE_S:
                    os.rmdir(mutex)
            except OSError:
                pass
            return False
        try:
            if self.lock_holder(now) is not None:
                return False
            self.steal_lock()                                    # may already be gone (a release in between): fine
            return self.try_create_lock()
        finally:
            try:
                os.rmdir(mutex)
            except OSError:
                pass

    def lock_holder(self, now=None):
        return lock_holder(self.read_state(), now, lock=self.read_lock())

    def is_running(self):
        return self.lock_holder() is not None

    # ---- status.json
    def read_round(self):
        try:
            return Round.load(self.round_path, strict=False)
        except (OSError, ValueError, ContractError):
            return None

    def write_status(self, status=None):
        """status.json in the compact form the 2 KB limit is checked on (Status.to_compact_json), by atomic rename."""
        status = status or build_status(self)
        contracts.write_json_atomic(self.status_path, status.to_dict(), indent=None)
        return status

    def disk_free_mb(self):
        try:
            return shutil.disk_usage(self.path if os.path.isdir(self.path) else os.path.dirname(self.path)).free / 2 ** 20
        except OSError:
            return None


# ----------------------------------------------------------------------------- hc study new

def new_study(name, days, space, constraints=(), phases=None, holdout=(), tags=None, question='explore',
              base_vdf_lines=(), settings=None, settings_path=NS_SETTINGS_PATH, studies_dir=STUDIES_DIR,
              source='ns:', host='', budget=None, notes=(), **extra):
    """Create studies/<name>/study.json. Refuses (StudyError, exit 3): an existing study, a locked or unknown key
    (named), a bad range or constraint, a base VDF row that sets a searched key or a locked key.

    `space` is {name: SpaceParam | dict}; `days` a list of 'YYYY-MM-DD'; `holdout` the subset reserved for
    validation; `tags` {day: [tag, ...]} typed by the user. `settings` (a loaded ns_settings.json) extends the
    locked list with its safety section and fills the `phone` values; when None the file at `settings_path` is read.
    Default phases when none are given: one-at-a-time with 5 levels. Returns the StudyDir."""
    if settings is None:
        settings = spc.load_settings(settings_path)
    sd = StudyDir(resolve_study_dir(name, studies_dir))
    if sd.exists():
        raise StudyError(f'{sd.study_json} exists; choose another name (studies are never overwritten)')
    tags = tags or {}
    unknown = sorted((set(tags) | set(holdout)) - set(days))
    if unknown:
        raise StudyError(f'--holdout/--tag name days outside --days: {", ".join(unknown)}')
    study_days = [StudyDay(day=d, role='holdout' if d in set(holdout) else 'search', tags=list(tags.get(d, [])))
                  for d in days]
    try:                                                     # the locked list, the ranges and the chains, in one place
        ps = spc.ParamSpace(space, constraints, settings=settings)
    except spc.LockedKeyError as e:
        raise StudyError(str(e)) from None
    except ContractError as e:
        raise StudyError(f'space refused: {e}') from None
    shadow = spc.vdf_rows_set_searched(base_vdf_lines, ps.names)
    if shadow:
        raise StudyError('base VDF rows set searched parameters (the search would be shadowed): ' + '; '.join(shadow))
    locked = [f'"{line.strip()}": {reason}' for line, reason in
              ((line, contracts.locked_vdf_row_reason(line, settings)) for line in base_vdf_lines) if reason]
    if locked:                                               # the DESIGN.md section 3 locks hold for the base rows too
        raise StudyError('base VDF rows set locked keys (refused for unattended runs, DESIGN.md section 3):\n  '
                         + '\n  '.join(locked))
    for name_, p in ps.space.items():
        if p.phone is None:
            pv = spc.phone_value(name_, settings)
            if pv is not None:
                p.phone = pv
    phases = list(phases or [Phase(kind='oaat', points=5)])
    budget = budget if isinstance(budget, contracts.Budget) else contracts.Budget(**(budget or {}))
    study = Study.new(sd.name, study_days, ps.space, source=source, host=host, question=question, phases=phases,
                      budget=budget, constraints=[c.tokens() for c in ps.constraints],
                      base_vdf_lines=list(base_vdf_lines), settings_path=settings_path, notes=list(notes), **extra)
    try:
        study.validate(settings)
    except ContractError as e:
        raise StudyError(f'study refused: {e}') from None
    os.makedirs(sd.path, exist_ok=True)
    sd.write_study(study)
    return sd


# ----------------------------------------------------------------------------- tasks and the runner

@dataclass
class Task:
    """One (variant, day) evaluation the orchestrator asks for. `params` {} with control 'base' is the base replay."""
    params: dict
    day: str
    phase: str = 'grid'
    control: Optional[str] = None            # 'base' | 'sentinel' | 'null' | None
    rung: Optional[int] = None
    round: Optional[int] = None
    variant: Optional[str] = None            # filled from the space order when None
    meta: dict = field(default_factory=dict)  # anything the evaluator needs (vdf lines, outputs...), not stored


def dry_evaluate(task):
    """The stand-in evaluator while replay.py (H-102) and the orchestrator (H-305) are not built: nothing is
    replayed, the task is recorded as `blocked` with the reason, so a later real run re-evaluates it."""
    return {'status': 'blocked',
            'error': 'not replayed: replay.py (H-102) / orchestrator (H-305) not built yet; day not frozen'}


class Runner:
    """The loop every phase runs through: lock, STATE.json, STOP, resume, exit codes, status.json.

        runner = Runner(StudyDir(path))
        result = runner.run(tasks, evaluate)        # -> RunResult(exit_code, stop_reason, ...)

    `evaluate(task)` returns a dict with any of: status ('done' | 'failed' | 'blocked'; default 'done' when
    metrics are present else 'failed'), metrics, seconds, error, flags, feasible, files, worker. It may raise
    GuardStop(reason) (exit 2). Any other exception becomes a `failed` row; MAX_CONSECUTIVE_FAILURES in a row is a
    guard stop. `freeze_sha_of(day)` gives the frozen day's sha for the key ('' until freeze.py exists).
    `code_sha`/`settings_sha` default to the study's own stamps; a live mismatch is recorded in STATE.stale_context
    (H-302 decides what to do with it)."""

    def __init__(self, study_dir, study=None, settings=None, freeze_sha_of=None, code_sha=None, settings_sha=None,
                 worker=None, min_disk_mb=MIN_DISK_MB, max_failures=MAX_CONSECUTIVE_FAILURES, status_every=1,
                 budget_s=None, log=None):
        self.sd = study_dir if isinstance(study_dir, StudyDir) else StudyDir(study_dir)
        self.study = study or self.sd.load_study(settings)
        self.space = spc.ParamSpace.from_study(self.study, settings=settings, check_locked=True)
        self.freeze_sha_of = freeze_sha_of or (lambda day: '')
        self.code_sha = code_sha or self.study.code_sha
        self.settings_sha = settings_sha or self.study.settings_sha
        self.worker = worker
        self.min_disk_mb = min_disk_mb
        self.max_failures = max_failures
        self.status_every = status_every
        self.budget_s = budget_s
        self.log = log or (lambda text: None)
        self.started_at = None
        self.state = None
        self.stale_context = self._stale_context()

    def _stale_context(self):
        out = []
        try:
            live = contracts.code_sha()
            if live != self.study.code_sha:
                out.append(f'code_sha changed since study.json: {contracts.short(self.study.code_sha)} -> {contracts.short(live)}')
        except Exception:                                    # pragma: no cover
            pass
        try:
            if os.path.isfile(NS_SETTINGS_PATH):
                live = contracts.settings_sha(NS_SETTINGS_PATH)
                if self.study.settings_sha and live != self.study.settings_sha:
                    out.append(f'settings_sha changed since study.json: {contracts.short(self.study.settings_sha)} -> {contracts.short(live)}')
        except Exception:                                    # pragma: no cover
            pass
        return out

    # ---- identity
    def params_of(self, task):
        """task.params snapped onto the grid (ParamSpace.normalize, space order): an off-grid float such as
        1.4500000001 names the grid point 1.45, so key, vhash, variant label and the stored params agree. An unknown
        key is a configuration error (StudyError, exit 3)."""
        if not task.params:
            return {}
        try:
            return self.space.normalize(task.params)
        except ContractError as e:
            raise StudyError(f'task {task.day}: {e}') from None

    def key_of(self, task):
        return contracts.key(self.params_of(task), task.day, self.settings_sha, self.code_sha,
                             self.freeze_sha_of(task.day), self.study.objective_version)

    def vhash_of(self, task):
        return contracts.vhash(self.params_of(task))

    def variant_of(self, task):
        if task.variant:
            return task.variant
        params = self.params_of(task)
        return 'base' if not params else self.space.variant_name(params)

    # ---- lock
    def acquire(self, phase='start', queue_len=0):
        """Take the study lock or raise LockHeld (exit 3). Removes a STOP file addressed to an earlier run.

        Atomic: with no LOCK present the file is created by exactly one process (StudyDir.try_create_lock, a hard
        link: O_EXCL semantics); a LOCK the rule finds free (dead pid, heartbeat older than LOCK_STALE_S, recorded
        exit_code) is replaced under the takeover mutex (StudyDir.take_over_lock). Whoever loses either race
        re-checks and finds the winner's fresh LOCK: LockHeld."""
        for attempt in range(LOCK_ATTEMPTS):
            if attempt:
                time.sleep(LOCK_RETRY_S)
            holder = self.sd.lock_holder()
            if holder:
                raise LockHeld(f'{self.sd.path}: another run holds this study ({holder}); it is taken over when the '
                               f'heartbeat is older than {LOCK_STALE_S // 60} min or the pid has gone')
            lock = self.sd.read_lock()
            if lock is None:
                if self.sd.try_create_lock():
                    break
                continue                                         # created by someone else just now: re-check (held)
            if lock['pid'] == os.getpid():
                break                                            # ours already (sequential phases in one process)
            if self.sd.take_over_lock():
                break                                            # free but present (dead pid, stale, exited): replaced
        else:
            raise LockHeld(f'{self.sd.path}: the lock changed hands {LOCK_ATTEMPTS} times while acquiring; try again')
        self.sd.clear_stop()
        self.started_at = time.time()
        rows = self.sd.trials()
        self.state = State(phase=phase, trials_done=len(self.sd.done_keys(rows)), queue_len=queue_len, pid=os.getpid(),
                           heartbeat=now_iso(), stale_context=list(self.stale_context), started=now_iso(),
                           budget_left_s=self.budget_s, disk_free_mb=self._disk())
        self.sd.write_state(self.state)
        return self.state

    def heartbeat(self, **updates):
        """Rewrite STATE.json with a fresh heartbeat (call it from long evaluations too)."""
        if self.state is None:
            return None
        for k, v in updates.items():
            setattr(self.state, k, v)
        self.state.heartbeat = now_iso()
        if self.budget_s is not None and self.started_at is not None:
            self.state.budget_left_s = round(max(0.0, self.budget_s - (time.time() - self.started_at)), 1)
        self.state.disk_free_mb = self._disk()
        self.sd.write_state(self.state)
        return self.state

    def release(self, exit_code, stop_reason):
        if self.state is None:
            return None
        self.state.exit_code = exit_code
        self.state.stop_reason = stop_reason
        self.heartbeat()
        try:
            self.sd.write_status()
        except Exception as e:                               # the status is a convenience; the state is the truth
            self.log(f'status.json not written: {e}')
        self.sd.remove_lock()                                # a clean release leaves no LOCK; a kill -9 leaves a dead pid
        return self.state

    def _disk(self):
        mb = self.sd.disk_free_mb()
        return None if mb is None else round(mb, 1)

    # ---- the loop
    def pending(self, tasks):
        """(task, key) for the tasks whose key has no done row yet, in order."""
        done = self.sd.done_keys()
        return [(t, k) for t, k in ((t, self.key_of(t)) for t in tasks) if k not in done]

    def run(self, tasks, evaluate, phase=None):
        """Evaluate `tasks` in order; see the class docstring. Returns RunResult; never raises for lock/guard
        conditions (they are the exit codes), only for a broken study (StudyError, exit 3) before the lock."""
        tasks = list(tasks)
        phase = phase or (tasks[0].phase if tasks else 'idle')
        todo = self.pending(tasks)
        result = RunResult(exit_code=EXIT_OK, stop_reason='finished', skipped=len(tasks) - len(todo))
        try:
            self.acquire(phase, queue_len=len(todo))
        except LockHeld as e:
            self.log(str(e))
            return RunResult(exit_code=EXIT_REFUSED, stop_reason=str(e), skipped=result.skipped)
        trial_no = self.sd.next_trial_number()
        failures = 0
        try:
            for i, (task, key) in enumerate(todo):
                if self.sd.stop_requested():
                    result.stop_reason = 'STOP file'
                    break
                if self.budget_s is not None and time.time() - self.started_at >= self.budget_s:
                    result.stop_reason = 'budget exhausted'
                    break
                disk = self._disk()
                if disk is not None and self.min_disk_mb and disk < self.min_disk_mb:
                    raise GuardStop(f'disk below {self.min_disk_mb} MB free ({disk:.0f} MB)')
                self.heartbeat(phase=task.phase or phase, queue_len=len(todo) - i, last_key=key)
                started = now_iso()
                t0 = time.time()
                try:
                    out = evaluate(task) or {}
                except GuardStop:
                    raise
                except KeyboardInterrupt:
                    raise
                except Exception as e:                       # a broken evaluation is a failed trial, not a crash
                    out = {'status': 'failed', 'error': f'{type(e).__name__}: {e}'[:1000]}
                seconds = out.get('seconds', round(time.time() - t0, 3))
                status = out.get('status') or ('done' if out.get('metrics') else 'failed')
                params = self.params_of(task)
                row = TrialRow(trial=trial_no, key=key, phase=task.phase or phase, vhash=contracts.vhash(params),
                               variant=self.variant_of(task), params=params, day=task.day, status=status,
                               code_sha=self.code_sha, settings_sha=self.settings_sha,
                               freeze_sha=self.freeze_sha_of(task.day), seconds=seconds,
                               worker=out.get('worker', self.worker), rung=task.rung, round=task.round,
                               started=started, finished=now_iso(), metrics=dict(out.get('metrics') or {}),
                               feasible=out.get('feasible'), flags=list(out.get('flags') or []),
                               files=dict(out.get('files') or {}),
                               error=out.get('error') or ('evaluation returned no metrics' if status == 'failed' else None),
                               control=task.control)
                self.sd.append(row)
                trial_no += 1
                result.done += 1
                if status == 'failed':
                    result.failed += 1
                    failures += 1
                elif status == 'blocked':
                    result.blocked += 1
                    failures = 0
                else:
                    failures = 0
                self.state.trials_done += 1 if status == 'done' else 0
                self.heartbeat(queue_len=len(todo) - i - 1, last_key=key)
                if self.status_every and result.done % self.status_every == 0:
                    try:
                        self.sd.write_status()
                    except Exception as e:                   # pragma: no cover
                        self.log(f'status.json not written: {e}')
                if failures >= self.max_failures:
                    raise GuardStop(f'{failures} consecutive failed trials (last: {row.error})')
        except GuardStop as e:
            result.exit_code, result.stop_reason = EXIT_GUARD, f'guard: {e.reason}'
        except KeyboardInterrupt:
            result.exit_code, result.stop_reason = EXIT_OK, 'interrupted (Ctrl-C); the trial in flight is lost, re-run resumes'
        finally:
            self.release(result.exit_code, result.stop_reason)
            if result.stop_reason == 'STOP file':
                self.sd.clear_stop()
        self.log(f'{self.sd.name}: {result.stop_reason}; {result.done} rows appended, {result.skipped} already done, '
                 f'{result.failed} failed, {result.blocked} blocked; exit {result.exit_code}')
        return result


# ----------------------------------------------------------------------------- the dry queue (until H-304/H-305)

def study_tasks(study, settings=None, phases=None):
    """The (variant, day) tasks of a study's pending oaat/grid phases on its search days, base first per day.
    This is the stand-in for search.py (H-304): grid = cartesian product (phase.grid or the full step grid),
    oaat = `points` levels per parameter around the phone's value. Other phase kinds are left to search.py."""
    space = spc.ParamSpace.from_study(study, settings=settings, check_locked=True)
    days = study.search_days()
    tasks = [Task(params={}, day=d, phase='floor', control='base') for d in days]
    seen = {(BASE_VHASH, d) for d in days}
    for ph in (phases if phases is not None else study.phases):
        if ph.state in ('done', 'skipped'):
            continue
        if ph.kind == 'grid':
            points = space.grid_points(ph.grid)
        elif ph.kind == 'oaat':
            if ph.grid:
                points = [{n: v} for n, vals in ph.grid.items() if n in space for v in vals]
                points = [p for p in points if space.satisfies(p)]
            else:
                centre = space.phone_point(settings or {}) if settings else {n: p.phone for n, p in space.space.items() if p.phone is not None}
                points = space.oaat_points(ph.points or 5, centre)
        else:
            continue
        for point in points:
            for d in days:
                vh = contracts.vhash(point)
                if (vh, d) in seen:
                    continue
                seen.add((vh, d))
                tasks.append(Task(params=point, day=d, phase=ph.kind))
    return tasks


# ----------------------------------------------------------------------------- status.json

def _round3(v):
    return None if v is None else round(float(v), 3)


def aggregate_floor(rows):
    """Status.floor: over the base rows of all days, max |base - phone| per component and min agreement_pct."""
    try:
        from hindcast.metrics import floors_from_base
    except Exception:                                        # pragma: no cover - metrics.py absent
        return {}
    agg = {}
    for r in rows:
        if r.status != 'done' or not r.metrics or not (r.control == 'base' or r.vhash == BASE_VHASH):
            continue
        f = floors_from_base(r.metrics)
        for k, v in f.items():
            if v is None:
                continue
            if k == 'agreement_pct':
                agg[k] = v if k not in agg else min(agg[k], v)
            elif k == 'loops':
                continue
            else:
                agg[k] = v if k not in agg else max(agg[k], v)
    return {k: _round3(v) for k, v in agg.items()}


def build_status(study_dir, now=None):
    """Status (<= 2 KB) from study.json, STATE.json, trials.jsonl and, when present, round.json."""
    sd = study_dir if isinstance(study_dir, StudyDir) else StudyDir(study_dir)
    sd.require()
    study = Study.load(sd.study_json, strict=False)
    state = sd.read_state()
    latest = sd.latest_by_key(sd.trials())
    rows = list(latest.values())
    holder = sd.lock_holder(now)
    counts = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    done = counts.get('done', 0)
    planned = len(latest)                                    # every key attempted so far (done, failed, blocked, stale)
    if state is not None:
        phase = state.phase
        if state.exit_code is None:
            planned += state.queue_len                       # plus what the live run still has queued
    else:
        pending = [p.kind for p in study.phases if p.state == 'pending']
        phase = pending[0] if pending else ('done' if study.phases else 'new')
    status = Status(study=study.name, question=study.question, phase=phase, trials_done=done, trials_planned=planned,
                    running=holder is not None, heartbeat=(state.heartbeat if state else study.created),
                    trials_failed=counts.get('failed', 0), trials_blocked=counts.get('blocked', 0),
                    pid=(state.pid if state and holder else None), round=(state.round if state else None),
                    stale_context=list(state.stale_context) if state else [],
                    floor=aggregate_floor(rows),
                    days={d.day: (f'quarantined: {d.quarantine_reason}' if d.role == 'quarantined' and d.quarantine_reason
                                  else d.role) for d in study.days},
                    exit_code=(state.exit_code if state else None), stop_reason=(state.stop_reason if state else None),
                    report=(os.path.join('report', 'index.html') if os.path.isfile(os.path.join(sd.report_dir, 'index.html')) else None))
    rnd = sd.read_round()
    if rnd is not None:
        status.dead_parameters = [d.key for d in rnd.dead_parameters]
        by_vhash = {c.vhash: c for c in rnd.variants}
        order = [v for v in rnd.front if v in by_vhash] or [c.vhash for c in rnd.variants if c.verdict != 'base']
        for vh in order[:5]:
            c = by_vhash[vh]
            effect = dict(list((c.effect or {}).items())[:4])
            status.top.append(TopItem(vhash=contracts.short(c.vhash), variant=c.variant, verdict=c.verdict,
                                      why=(c.why or c.verdict)[:120], rank=c.rank,
                                      effect={k: _round3(v) for k, v in effect.items()}))
    for _ in range(8):                                       # fit the 2 KB limit: shorten, then drop, the top items
        if len(status.to_compact_json().encode('utf-8')) <= STATUS_MAX_BYTES:
            break
        if status.top:
            if any(len(t.why) > 60 for t in status.top):
                for t in status.top:
                    t.why = t.why[:60]
            else:
                status.top.pop()
        elif status.stale_context:
            status.stale_context = [s[:80] for s in status.stale_context[:3]]
        else:
            status.days = dict(list(status.days.items())[:20])
    return status.validate()


VERDICT_LABELS = {'recommended': 'recommended for review'}  # the JSON keeps the contract word; the text passes the lint


def verdict_label(verdict):
    """The verdict word as human text renders it (every label passes lint.lint_findings(..., 'line'))."""
    return VERDICT_LABELS.get(verdict, verdict)


def status_text(status, study=None):
    """The human rendering of `hc study status` (the JSON is for --json). Ends with the honesty statement.
    Verdict words go through `verdict_label` so the text passes the vocabulary lint ('recommended for review')."""
    lines = [f'study {status.study}   question: {status.question}   phase: {status.phase}',
             f'trials: {status.trials_done} done of {status.trials_planned} planned, {status.trials_failed} failed, '
             f'{status.trials_blocked} blocked',
             f'running: {"yes, pid %s" % status.pid if status.running else "no"}   heartbeat: {status.heartbeat}']
    if status.exit_code is not None:
        lines.append(f'last run: exit {status.exit_code} ({status.stop_reason})')
    if status.stale_context:
        lines.append('stale context: ' + '; '.join(status.stale_context))
    lines.append('days: ' + ', '.join(f'{d} ({role})' for d, role in status.days.items()))
    if status.floor:
        lines.append('floor (base vs phone, worst day): ' + ', '.join(f'{k} {v}' for k, v in status.floor.items()))
    if status.dead_parameters:
        lines.append('not testable on these days: ' + ', '.join(status.dead_parameters))
    if status.top:
        lines.append('review order (not a ranking by outcome):')
        for t in status.top:
            eff = ', '.join(f'{k} {v}' for k, v in t.effect.items())
            lines.append(f'  {t.rank if t.rank is not None else "-":>2} {t.variant}  [{verdict_label(t.verdict)}] {t.why}'
                         + (f'  ({eff})' if eff else ''))
    lines += ['', HONESTY_STATEMENT]
    return '\n'.join(lines) + '\n'
