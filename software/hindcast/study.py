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
    studies/<study>/guards.json     the guards' private details (never published): whether the sentinel could be
                                    scheduled (and why not, per day), the loop counts of an unequal-loop stop

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
skipped by contracts.read_trials. Where a key has several rows the last one counts. The rule holds for the
sentinel / null controls too: a control's key carries its instance, `r<round>.t<first trial number of the run>:s<n>`
(or `:null`), so the controls of a re-run after a kill -9 are new keys by design (the controls run again in every
run: they check the process that does the replays), and Runner.run still drops any scheduled control whose key
already has a done row. So no key ever gets a second done row.

Session (H-305). `Runner.run(tasks, ...)` alone takes the lock, writes STATE.json and releases with its exit code.
The orchestrator runs many rounds under ONE lock instead: `runner.open_session()` once (lock, STOP left by an
earlier run removed, STATE.json), every `runner.run(...)` inside it leaves lock and STATE alone apart from phase /
queue_len / heartbeat and log a 'round N recorded' line (no exit), and `runner.close_session(exit_code, reason,
stop_file, before_release)` releases once with the real exit code: the STOP file first (when the flag says it
stopped the run), then before_release (tier 1 and the report, the lock still held), the release last. Between two
rounds the study therefore stays locked and STATE.json has no exit_code (`hc study status`: running). A STOP file
is removed only when a whole run starts (acquire) and by the run it stopped, before that run releases. Its stop
reason is STOP_REASON ('stopped: STOP file') inside a round and between rounds alike.

Exit codes (DESIGN.md section 4.5): 0 finished or stopped (STOP file, Ctrl-C, budget), 2 stopped by a guard
(GuardStop: three consecutive failures, disk below the minimum, a sentinel / null control that differs from base,
unequal loop counts on a day, stale_context; also an unexpected exception of the batch evaluation, whose type and
first line become the stop reason), 3 configuration refused (StudyError: locked key, unknown VDF key, lock held,
bad study).

Guards (backlog H-302, DESIGN.md sections 5.1 and 6; details in the Runner and Guards docstrings):
    sentinel        the phone's values of the searched parameters as explicit VDF rows (ns_settings.json with
                    profile_history rewound to the day's start) must reproduce the day's base replay on every loop
                    and every CSV column; after each base replay of the run, at each phase start, and every
                    `sentinel_every` (study.json) tasks
    null            one searched parameter at the phone's value; once per run (= round); same comparison
    loop counts     a done row whose metrics.loops differs from its day's base row stops the run
    stale_context   code_sha / settings_sha / freeze_sha on every row (and in the key), and metrics_version (not in
                    the key: metrics.METRICS_VERSION, a done row of another version - missing = 1 - is stale under
                    its own key); at the start of a run the done rows are compared with the live context: `--on-stale stop` (default) replays nothing and
                    exits 2, `--on-stale reeval` appends a `stale` row over each old done row, replays the affected
                    days' base first, then continues (the rest lazily, as the search asks for it); a change during
                    a run lets the round finish, then exits 2
Every guard stop writes its reason (for a control: the day, the first differing loop with its time and the
differing column names) into STATE.json and status.json `stop_reason`. That reason can reach the public campaign
issue (gh.py), so it never carries loop counts or cell values (DESIGN.md section 3, privacy): those go into the
row's `error` (trials.jsonl) and, for loop counts, guards.json.
A sentinel that cannot be scheduled (sentinel_params leaves out every searched parameter on a day) disables the
sentinel and null controls for that day; that is never silent: guards.json `sentinel` names the days and the
reason per parameter, every row of the run on such a day carries the flag `sentinel_not_schedulable`,
RunResult.guards lists it, and `hc study status` prints it (status_text, from guards.json).

What is NOT here: the search phases (search.py, H-304), the replay (replay.py, H-102), the pool (pool.py) and the
orchestrator that strings them together (H-305). `Runner.run(tasks, evaluate)` is the loop they plug into:
`evaluate(task)` returns the row fields of one evaluation, the Runner does lock, STATE, STOP, resume, exit codes,
status.json. The orchestrator (orchestrator.py, H-305) uses the batch hook `Runner.run(tasks, None,
batch_evaluate=..., on_record=...)` instead: the whole queue goes to the worker pool, results come back in
completion order and are recorded one by one with the same guards. `hc study run --dry` still runs the study's
queue through `dry_evaluate()`, which records every task as `blocked` (nothing is replayed).

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
GUARDS_JSON = 'guards.json'                          # private guard details (not part of STATE/status contracts)
GUARDS_LOOP_COUNTS_MAX = 20                          # unequal-loop entries kept in guards.json
LOCK_STALE_S = 600                                   # heartbeat older than this: the lock may be taken over
LOCK_ATTEMPTS = 5                                    # acquire rounds before giving up (the lock keeps changing hands)
LOCK_RETRY_S = 0.05                                  # pause between those rounds
TAKEOVER_STALE_S = 60                                # a takeover mutex older than this was left by a killed taker
MIN_DISK_MB = 2048                                   # DESIGN.md: < 2 GB free stops the run (exit 2)
MAX_CONSECUTIVE_FAILURES = 3
EXIT_OK, EXIT_GUARD, EXIT_REFUSED = 0, 2, 3
ON_STALE = ('stop', 'reeval')                        # hc study run --on-stale (default stop)
CONTROL_KINDS = ('sentinel', 'null')                 # the automatic controls of DESIGN.md section 5.1 (H-302)
STOP_REASON_MAX = 400                                # characters of a stop reason in STATE.json / status.json
STOP_REASON = 'stopped: STOP file'                   # the one stop reason of a STOP file (inside a round and between)
FINISHED = 'finished'                                # RunResult.stop_reason of a run (or round) that ran its queue out
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


class RunStop(Exception):
    """A clean stop noticed while waiting for a result (STOP file, budget): exit code 0. Raised by Runner.idle(),
    which the orchestrator's pool calls when no result arrived for a while; Runner.run turns it into its result."""
    exit_code = EXIT_OK

    def __init__(self, reason, stop_file=False):
        self.reason = reason
        self.stop_file = bool(stop_file)                 # True: the STOP file asked for it (close removes the file)
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
        self.guards_path = os.path.join(self.path, GUARDS_JSON)
        self.runs_dir = os.path.join(self.path, 'runs')
        self.report_dir = os.path.join(self.path, 'report')
        self.closing = None                          # (exit_code, stop_reason) while close_session publishes

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

    # ---- guards.json (private details of the guards; never copied into status.json or an issue)
    def read_guards(self):
        """The guards.json dict, {} when missing or unreadable (never an exception)."""
        try:
            with open(self.guards_path, encoding='utf-8') as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update_guards(self, **sections):
        """Merge `sections` into guards.json (atomic rename); a value None removes that section."""
        data = self.read_guards()
        for k, v in sections.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        data['updated'] = now_iso()
        contracts.write_json_atomic(self.guards_path, data)
        return data

    def add_loop_count_guard(self, entry):
        data = self.read_guards()
        items = list(data.get('loop_counts') or []) + [entry]
        return self.update_guards(loop_counts=items[-GUARDS_LOOP_COUNTS_MAX:])

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
    """One (variant, day) evaluation the orchestrator asks for. `params` {} with control 'base' is the base replay.
    The controls 'sentinel' and 'null' (H-302) carry `meta['instance']`, which makes every scheduled control its own
    trial key (see Runner.key_of), and their params are the phone's values as they are, never snapped onto the grid."""
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


def is_control(task_or_row):
    """True for the sentinel and null controls (not variants: round.py leaves them out of the ranking)."""
    return getattr(task_or_row, 'control', None) in CONTROL_KINDS


def is_base_task(task):
    return getattr(task, 'control', None) == 'base' or (not task.params and not is_control(task))


def _clip(text, n=STOP_REASON_MAX):
    text = str(text)
    return text if len(text) <= n else text[:n - 3] + '...'


# ----------------------------------------------------------------------------- stale_context (H-302)

def live_context(settings_path=NS_SETTINGS_PATH, software_dir=SOFTWARE_DIR, fallback_settings_sha=''):
    """(code_sha, settings_sha) of the working tree right now: contracts.code_sha over determine_basal.py,
    emulator_core.py and ns_*.py; contracts.settings_sha over the scrubbed ns_settings.json at `settings_path`
    (`fallback_settings_sha` when that file does not exist)."""
    code = contracts.code_sha(software_dir)
    try:
        sett = contracts.settings_sha(settings_path) if os.path.isfile(settings_path) else fallback_settings_sha
    except (OSError, ValueError):
        sett = fallback_settings_sha
    return code, sett


def current_metrics_version():
    """metrics.METRICS_VERSION (lazy: metrics imports emulator_optimize)"""
    from hindcast import metrics as _m
    return int(_m.METRICS_VERSION)


def metrics_stale(row, version=None):
    """True for a done row whose metrics_version (missing = 1) differs from the current metrics.METRICS_VERSION"""
    v = current_metrics_version() if version is None else version
    return row.status == 'done' and int(getattr(row, 'metrics_version', 1) or 1) != v


def find_stale(study, rows, code_sha, settings_sha, freeze_sha_of=None, metrics_version=None):
    """-> (reasons, stale) for the rows of a study against the live context.

    metrics_version (default metrics.METRICS_VERSION): a done row made with another metrics version is stale like
    a code_sha mismatch, although its key is the same (the version is not in the key): `--on-stale reeval`
    supersedes it with a `stale` row, so the key is handed out again and re-replayed.

    The reference is the context the study's results were produced under: every latest `done` row (per key) whose
    code_sha or settings_sha differs from the live one, or whose freeze_sha differs from `freeze_sha_of(row.day)`
    (a day re-frozen, or its meta changed), is stale. Without any done row the reference is study.json's own stamps
    (nothing can be mixed yet, but the change is still reported). After `--on-stale reeval` the re-evaluated rows
    carry the live context, so the next run is not stale again although study.json keeps its creation stamps.
    `reasons` are short lines for STATE.json / status.json; `stale` is [(row, [field, ...])]."""
    latest = StudyDir.latest_by_key(rows)
    done = [r for r in latest.values() if r.status == 'done']
    if not done:
        reasons = []
        if study.code_sha and code_sha and study.code_sha != code_sha:
            reasons.append(f'code_sha changed since study.json: {contracts.short(study.code_sha)} -> {contracts.short(code_sha)}')
        if study.settings_sha and settings_sha and study.settings_sha != settings_sha:
            reasons.append(f'settings_sha changed since study.json: {contracts.short(study.settings_sha)} -> '
                           f'{contracts.short(settings_sha)}')
        return reasons, []
    freeze_now = {}

    def frozen(day):
        if day not in freeze_now:
            try:
                freeze_now[day] = freeze_sha_of(day) if freeze_sha_of else None
            except Exception:                                # pragma: no cover - an unreadable meta is not a guard
                freeze_now[day] = None
        return freeze_now[day]

    counts, stale = {}, []
    mv = current_metrics_version() if metrics_version is None else int(metrics_version)
    for r in sorted(done, key=lambda r: r.trial):
        why = []
        rv = int(getattr(r, 'metrics_version', 1) or 1)
        if rv != mv:
            why.append(('metrics_version', str(rv), str(mv)))
        if code_sha and r.code_sha != code_sha:
            why.append(('code_sha', r.code_sha, code_sha))
        if settings_sha and r.settings_sha != settings_sha:
            why.append(('settings_sha', r.settings_sha, settings_sha))
        f = frozen(r.day)
        if f is not None and r.freeze_sha != f:
            why.append((f'freeze_sha of {r.day}', r.freeze_sha, f))
        if why:
            stale.append((r, [w[0] for w in why]))
            for w in why:
                counts[w] = counts.get(w, 0) + 1
    reasons = [f'{name} changed: {contracts.short(old) or "none"} -> {contracts.short(new) or "none"} ({n} done rows)'
               for (name, old, new), n in counts.items()]
    return reasons, stale


# ----------------------------------------------------------------------------- the controls (H-302)

def day_start_ms(day):
    d = datetime.datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp() * 1000)


def sentinel_params(study, settings, day):
    """-> (params, skipped): the phone's value of every searched parameter ns_settings.json carries, rewound through
    `profile_history` to the start of `day` (space.phone_value with at_ms), as they are (no grid snapping), in space
    order. `skipped` names the parameters left out, with the reason: Nightscout carries the value (the profile
    store, not a settings section: an explicit row would not be what base reads), or profile_history changes it
    inside the day (one explicit row cannot follow an in-day change; days.py quarantines such a day)."""
    from hindcast import days as dy
    at = day_start_ms(day)
    params, skipped = {}, []
    for name in study.space:
        v = spc.phone_value(name, settings or {}, at_ms=at)
        if v is None:
            skipped.append(f'{name}: not in ns_settings.json')
            continue
        if dy.history_hits(settings or {}, day, [name]):
            skipped.append(f'{name}: profile_history changes it inside {day}')
            continue
        params[name] = v
    return params, skipped


def first_loop_difference(csv_a, csv_b, decimal='.'):
    """None when the two emulator CSVs agree on every loop row (all columns), else a dict describing the first loop
    that differs: {'loop': index, 'time': ISO or the CSV's time cell, 'columns': [(name, a, b), ...] (up to 4),
    'differing_loops': n, 'loops': (len a, len b)}. Unequal row counts are a difference at the first missing row."""
    from emulator_optimize import read_result_csv
    names_a, rows_a = read_result_csv(csv_a, decimal)
    names_b, rows_b = read_result_csv(csv_b, decimal)
    names = names_a or names_b
    unix = next((n for n in names if 'UNIX' in n), None)
    tname = names[1] if len(names) > 1 else None

    def when(row):
        try:
            return datetime.datetime.fromtimestamp(float(row[unix]), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except (KeyError, TypeError, ValueError, OSError):
            return str(row.get(tname)) if tname else '?'

    first, columns, differing = None, [], 0
    for i, (ra, rb) in enumerate(zip(rows_a, rows_b)):
        cols = [(n, ra.get(n), rb.get(n)) for n in names if ra.get(n) != rb.get(n)]
        if cols:
            differing += 1
            if first is None:
                first, columns = i, cols
    if len(rows_a) != len(rows_b):
        n = min(len(rows_a), len(rows_b))
        if first is None:
            first = n
            row = (rows_a if len(rows_a) > n else rows_b)[n]
            return {'loop': n, 'time': when(row), 'columns': [], 'differing_loops': differing,
                    'loops': (len(rows_a), len(rows_b))}
    if first is None:
        return None
    return {'loop': first, 'time': when(rows_a[first]), 'columns': columns[:4], 'differing_loops': differing,
            'loops': (len(rows_a), len(rows_b))}


def _fmt_cell(v):
    if isinstance(v, float):
        return f'{v:.6g}'
    return str(v)


def guard_text(kind, day, diff, kept=None, detail=False):
    """The one-line guard stop reason: which control, which day, which loop and which columns differ from base.

    detail=False (default): the stop reason for STATE.json / status.json, which gh.py may post into the public
    campaign issue; it names the loop's index and time and the differing column names only (no loop counts, no
    cell values; passes lint kind 'issue'). detail=True: the row's `error` (trials.jsonl, private), with the cell
    values, the number of differing loops and the loop counts of both CSVs."""
    la, lb = diff.get('loops', (None, None))
    head = f'{kind} differs from base on {day} at loop {diff["loop"]} ({diff["time"]})'
    cols = diff.get('columns', [])[:3]
    if detail:
        if la != lb and not cols:
            body = f'{kind} has {la} loops, base {lb}'
        else:
            body = '; '.join(f'{n}: {_fmt_cell(a)} vs base {_fmt_cell(b)}' for n, a, b in cols)
            body += f'; {diff.get("differing_loops", 0)} of {lb} loops differ'
    elif la != lb and not cols:
        body = f'the loop counts of {kind} and base differ (see the row\'s error)'
    else:
        body = 'columns ' + ', '.join(n for n, _, _ in cols) + ' (values in the row\'s error)'
    text = f'{head}: {body}' + (f' (control CSV kept: {kept})' if kept else '')
    return _clip(text + '. State leaked between in-process replays or the VDF path differs from the profile path')


class Guards:
    """The sentinel and null controls of DESIGN.md section 5.1 (H-302).

        guards = Guards.from_context(ctx, study, settings)        # replay.ReplayContext: the real replays
        runner = Runner(sd, study, guards=guards)
        runner.run(tasks, ctx.evaluate)                          # controls are scheduled into the run

    sentinel  the phone's values of the searched parameters as explicit VDF rows (sentinel_params: ns_settings.json
              with profile_history at the day's start); must reproduce the day's base replay loop by loop, every
              column of the CSV. Scheduled after every base replay the run makes (DESIGN.md section 6 step 0: base
              + sentinel on every day), before the first task of each phase, and before the next task after
              `sentinel_every` (study.json) tasks.
    null      one searched parameter set to the phone's value (rotating with the round number); once per run
              (= round), right after the first sentinel; must equal base the same way.
    per day   in the phases of PER_DAY_PHASES ('validate', H-308) the phase-start and every-N rules do not apply:
              a day gets one sentinel for the whole study, before its first variant task, unless one was scheduled
              on that day in this run (its base replay brings one when the base runs in the same batch) or the
              study already has a done sentinel row on that day (`covered_days`, Runner.run passes them from
              trials.jsonl), so later validate rounds do not repeat it; the null of a validate round runs with its
              first sentinel (none when every day of the round is covered).
    quarantine no control is ever scheduled on a day the study has quarantined (study.json role 'quarantined';
              the quarantine is judged before the tasks are planned, and the orchestrator's after_base_wave drops
              the controls of a day it quarantines in the same round).

    A mismatch is a guard stop (Runner: exit 2, the differing loop's index, time and column names in status.json's
    stop_reason; the cell values and loop counts only in the row's error); a control that fails while its day's
    base CSV exists is one too. A day on which sentinel_params leaves out every searched parameter gets no control;
    schedule() records it in `not_schedulable` (day -> reasons) and the Runner reports it (guards.json, row flag
    `sentinel_not_schedulable`, RunResult.guards, `hc study status`). A control that is blocked (day not frozen) or has no base CSV
    to compare with is recorded with the flag `guard_unchecked: ...` and the run goes on.

    `control_evaluate(task) -> dict` returns the evaluate() fields plus `csv` (the control's kept CSV);
    `base_csv_of(day) -> path or None`. from_context() builds both from a replay.ReplayContext; tests pass their own.
    For the pool path the orchestrator sends `control_spec(task)` through the pool and calls `verdict(task, out)`
    itself (Runner.record does that)."""

    def __init__(self, study, settings=None, settings_path=NS_SETTINGS_PATH, control_evaluate=None, base_csv_of=None,
                 sentinel_every=None, floor_sentinel=True, null=True, keep_matching_csv=False, ctx=None,
                 control_dir=None):
        self.study = study
        self.settings = settings if settings is not None else spc.load_settings(settings_path)
        self.control_evaluate = control_evaluate
        self.base_csv_of = base_csv_of or (lambda day: None)
        self.sentinel_every = int(sentinel_every or getattr(study, 'sentinel_every', 25) or 25)
        self.floor_sentinel = floor_sentinel
        self.null = null
        self.keep_matching_csv = keep_matching_csv
        self.ctx = ctx
        self.control_dir = control_dir
        self._params = {}
        self.not_schedulable = {}            # day -> [reason per parameter]: no sentinel / null possible that day

    @classmethod
    def from_context(cls, ctx, study, settings=None, **kw):
        """Guards over a replay.ReplayContext: controls are replayed with run_trial in this process, their CSV is
        kept under runs/controls/ until the comparison, the base CSV is the one the context keeps per day."""
        control_dir = os.path.join(ctx.runs_dir or os.path.join(SOFTWARE_DIR, 'runs'), 'controls')
        g = cls(study, settings=settings, ctx=ctx, control_dir=control_dir, **kw)

        def base_csv_of(day):
            p = ctx.base_csvs.get(day)
            if p and os.path.isfile(p):
                return p
            p = ctx.base_csv_path(day)
            return p if os.path.isfile(p) else None
        g.base_csv_of = base_csv_of
        g.control_evaluate = g._replay_control
        return g

    # ---- the control tasks
    def phone_params(self, day):
        if day not in self._params:
            self._params[day] = sentinel_params(self.study, self.settings, day)
        return self._params[day]

    def sentinel_task(self, day, phase, round_=None, instance=''):
        """The sentinel Task, or None when sentinel_params leaves out every searched parameter on `day`; that day is
        then recorded in `not_schedulable` (the Runner writes it to guards.json and flags the day's rows)."""
        params, skipped = self.phone_params(day)
        if not params:
            self.not_schedulable[day] = list(skipped) or ['no searched parameter']
            return None
        return Task(params=dict(params), day=day, phase=phase, control='sentinel', round=round_, variant='sentinel',
                    meta={'instance': str(instance), 'skipped': list(skipped)})

    def null_task(self, day, phase, round_=None, instance='', index=None):
        params, _ = self.phone_params(day)
        if not params:
            return None
        names = list(params)
        name = names[(index if index is not None else (round_ or 0)) % len(names)]
        return Task(params={name: params[name]}, day=day, phase=phase, control='null', round=round_, variant='null',
                    meta={'instance': str(instance), 'parameter': name})

    PER_DAY_PHASES = ('validate',)

    def quarantined_days(self):
        """The study's days with role 'quarantined' (listed, not scored: no control runs there)."""
        return {d.day for d in (getattr(self.study, 'days', None) or []) if getattr(d, 'role', None) == 'quarantined'}

    @staticmethod
    def sentinel_days_done(rows):
        """The days with a done sentinel row (latest row per key) in `rows` (TrialRow list): covered_days."""
        latest = StudyDir.latest_by_key(rows) if rows else {}
        return {r.day for r in latest.values() if r.control == 'sentinel' and r.status == 'done'}

    def schedule(self, tasks, run_tag='run', covered_days=None):
        """The tasks with the controls inserted (see the class docstring). Controls already in `tasks` pass through
        (except on a quarantined day). `covered_days`: days that already have a done sentinel in this study (the
        per-day rule skips them). Days on which no sentinel can be built end up in `not_schedulable` (reset per
        call)."""
        out, since, phases, seq = [], 0, set(), 0
        sentinel_days = set()
        covered = set(covered_days or ())
        skip = self.quarantined_days()
        self.not_schedulable = {}
        null_due = self.null

        def add_sentinel(t):
            nonlocal seq, since, null_due
            if t.day in skip:
                return
            s = self.sentinel_task(t.day, t.phase, t.round, f'{run_tag}:s{seq}')
            if s is None:
                return
            seq += 1
            since = 0
            sentinel_days.add(t.day)
            out.append(s)
            if null_due:
                n = self.null_task(t.day, t.phase, t.round, f'{run_tag}:null')
                if n is not None:
                    out.append(n)
                null_due = False

        for t in tasks:
            if is_control(t):
                if t.day in skip:
                    continue
                if t.control == 'sentinel':
                    sentinel_days.add(t.day)
                out.append(t)
                continue
            params, skipped = self.phone_params(t.day)
            if not params:                                   # no control can ever cover this day: say so
                self.not_schedulable[t.day] = list(skipped) or ['no searched parameter']
            if is_base_task(t):
                out.append(t)
                if self.floor_sentinel:
                    add_sentinel(t)
                continue
            if t.phase in self.PER_DAY_PHASES:              # one sentinel per day for the whole study
                phases.add(t.phase)
                if t.day not in sentinel_days and t.day not in covered:
                    add_sentinel(t)
                    sentinel_days.add(t.day)                 # also when none could be built: do not retry per task
                out.append(t)
                since += 1
                continue
            if t.phase not in phases:
                phases.add(t.phase)
                if not (out and out[-1].control in CONTROL_KINDS and out[-1].day == t.day):
                    add_sentinel(t)
            elif since >= self.sentinel_every:
                add_sentinel(t)
            out.append(t)
            since += 1
        return out

    def guard_notes(self):
        """One line per day without a schedulable sentinel: 'sentinel: not schedulable on <day> (<reasons>)'."""
        return [f'sentinel: not schedulable on {d} ({"; ".join(r)})' for d, r in sorted(self.not_schedulable.items())]

    # ---- evaluation
    def control_csv_path(self, task):
        tag = re.sub(r'[^A-Za-z0-9._-]', '_', str((task.meta or {}).get('instance', '')))[:40]
        return os.path.join(self.control_dir or os.path.join(SOFTWARE_DIR, 'runs', 'controls'),
                            f'{task.day}.{task.control}.{contracts.short(contracts.vhash(task.params))}.{tag}.csv')

    def control_spec(self, task):
        """The replay.TrialSpec of a control: the context's spec with the CSV kept for the comparison, no tier-2
        files (a control is not a variant of the report) and the day's base CSV for the metrics."""
        import dataclasses
        if self.ctx is None:
            raise StudyError('Guards.control_spec needs a replay context (Guards.from_context)')
        spec = self.ctx.spec_for(task)
        return dataclasses.replace(spec, keep_csv=self.control_csv_path(task), report_dir=None,
                                   base_csv=self.base_csv_of(task.day))

    def _replay_control(self, task):
        from hindcast import replay
        res = replay.run_trial(self.control_spec(task))
        out = res.to_row()
        out['csv'] = res.csv
        return out

    def evaluate(self, task):
        if self.control_evaluate is None:
            raise StudyError('Guards: no control evaluator (use Guards.from_context or pass control_evaluate)')
        return self.control_evaluate(task) or {}

    def verdict(self, task, out):
        """-> (stop_reason or None, flags) for one control's evaluate() output."""
        kind, day = task.control, task.day
        status = out.get('status') or ('done' if out.get('metrics') else 'failed')
        flags = []
        skipped = (task.meta or {}).get('skipped') or []
        if skipped:
            flags.append(f'sentinel_left_out: {len(skipped)}')
        if status == 'blocked':
            return None, flags + ['guard_unchecked: blocked']
        base = self.base_csv_of(day)
        if not base:
            return None, flags + ['guard_unchecked: no base CSV of the day']
        csv = out.get('csv')
        if status != 'done' or not csv or not os.path.isfile(csv):
            err = _clip(out.get('error') or 'no CSV', 200)
            return _clip(f'{kind} failed on {day} while the base replay of the day ran: {err}'), flags + ['guard_failed']
        diff = first_loop_difference(csv, base)
        if diff is None:
            if not self.keep_matching_csv:
                try:
                    os.remove(csv)
                except OSError:
                    pass
            return None, flags + ['equals_base']
        kept = csv
        root = os.path.dirname(os.path.dirname(self.control_dir)) if self.control_dir else None
        if root and csv.startswith(root + os.sep):
            kept = os.path.relpath(csv, root)
        out['guard_detail'] = guard_text(kind, day, diff, kept, detail=True)    # the row's error (private)
        return guard_text(kind, day, diff, kept), flags + ['differs_from_base']


@dataclass
class RunResult:
    exit_code: int
    stop_reason: str
    done: int = 0                   # rows appended by this run (controls included)
    skipped: int = 0                # tasks whose key already had a done row
    failed: int = 0
    blocked: int = 0
    controls: int = 0               # sentinel / null rows appended
    marked_stale: int = 0           # done rows superseded by a `stale` row (--on-stale reeval)
    already_done: int = 0           # keys of the study with a done row when this run started (what resume skips)
    stale_context: list = field(default_factory=list)
    guards: list = field(default_factory=list)          # guard notes of the run (Guards.guard_notes), [] = none
    stop_file: bool = False         # True when the STOP file stopped it (an explicit flag, never read from the text)


class Runner:
    """The loop every phase runs through: lock, STATE.json, STOP, resume, exit codes, status.json, and the guards of
    H-302 (stale_context, equal loop counts, sentinel / null controls).

        runner = Runner(StudyDir(path), guards=Guards.from_context(ctx, study, settings))
        result = runner.run(tasks, evaluate, on_stale='stop')    # -> RunResult(exit_code, stop_reason, ...)

    `evaluate(task)` returns a dict with any of: status ('done' | 'failed' | 'blocked'; default 'done' when
    metrics are present else 'failed'), metrics, seconds, error, flags, feasible, files, worker. It may raise
    GuardStop(reason) (exit 2). Any other exception becomes a `failed` row; MAX_CONSECUTIVE_FAILURES in a row is a
    guard stop. `freeze_sha_of(day)` gives the frozen day's sha for the key ('' until the day is frozen).

    Context per row. Every row carries the code_sha / settings_sha / freeze_sha the run started with: code_sha and
    settings_sha are the live working tree's (contracts.code_sha, the settings file at `settings_path`) when the
    run starts, unless the constructor pins them; freeze_sha is read once per day per run. These are in the key.

    stale_context. At the start of each run, find_stale() compares the done rows (or, without any, study.json's
    stamps) with the live context. `on_stale='stop'` (default): nothing is replayed, STATE/status say why, exit 2.
    `on_stale='reeval'`: every stale done row is superseded by a `stale` row (same key, appended, so round.py and
    the ranking no longer read it), the base replays of the affected days are put first, then the run's tasks
    (whose keys carry the new context) follow; the rest is re-evaluated lazily, when the search asks for it.
    A change during the run (the code, the settings file, or a frozen day's meta) is noticed after the trial in
    flight; the run finishes its tasks (the round) with the context it started with, then stops with exit 2 in both
    modes: the process still holds the old code, so the rows of that round are marked stale by the next run.

    Equal loop counts. A done row whose metrics.loops differs from the base row of its day (same context) is a
    guard stop: state leaked between in-process replays, or the reconstruction changed under the run. The stop
    reason names the day, trial and variant only; the counts go into the row's error (flag `unequal_loops`) and
    guards.json `loop_counts`, never into stop_reason (gh.py may post it into the public issue).

    Controls. With `guards` (Guards), the sentinel / null controls are scheduled into the run (Guards.schedule) and
    evaluated through guards.evaluate; a control that differs from base stops the run with exit 2 after its row is
    appended, the differing loop in STATE/status stop_reason (count- and value-free) and in the row's error (with
    the values). Days without a schedulable sentinel: see Runner.note_guards."""

    def __init__(self, study_dir, study=None, settings=None, freeze_sha_of=None, code_sha=None, settings_sha=None,
                 worker=None, min_disk_mb=MIN_DISK_MB, max_failures=MAX_CONSECUTIVE_FAILURES, status_every=1,
                 budget_s=None, log=None, settings_path=NS_SETTINGS_PATH, on_stale='stop', guards=None,
                 software_dir=SOFTWARE_DIR):
        self.sd = study_dir if isinstance(study_dir, StudyDir) else StudyDir(study_dir)
        self.study = study or self.sd.load_study(settings)
        self.space = spc.ParamSpace.from_study(self.study, settings=settings, check_locked=True)
        self.freeze_sha_of = freeze_sha_of or (lambda day: '')
        self.settings_path = settings_path
        self.software_dir = software_dir
        self._pinned = (code_sha, settings_sha)
        if on_stale not in ON_STALE:
            raise StudyError(f'--on-stale must be one of {", ".join(ON_STALE)}, not {on_stale!r}')
        self.on_stale = on_stale
        self.guards = guards
        self.worker = worker
        self.min_disk_mb = min_disk_mb
        self.max_failures = max_failures
        self.status_every = status_every
        self.budget_s = budget_s
        self.deadline = None                             # absolute time.time() limit (the orchestrator's budget)
        self.session = False                             # True between open_session() and close_session()
        self.log = log or (lambda text: None)
        self.started_at = None
        self.state = None
        self._freeze = {}
        self._base_loops = {}
        self._not_schedulable = {}                       # day -> reasons, from the guards of the current run
        self.code_sha, self.settings_sha = self.live_context()
        self.stale_context = []

    # ---- context
    def live_context(self):
        """(code_sha, settings_sha) for this run: the pinned constructor values, else the working tree's."""
        code, sett = self._pinned
        if code and sett:
            return code, sett
        live_code, live_sett = live_context(self.settings_path, self.software_dir, self.study.settings_sha)
        return code or live_code, sett or live_sett

    def refresh_context(self):
        """Re-read the context at the start of a run (a Runner may run several rounds)."""
        self.code_sha, self.settings_sha = self.live_context()
        self._freeze = {}
        self._base_loops = {}
        return self.code_sha, self.settings_sha

    def freeze_sha(self, day):
        """The frozen day's sha1 as this run first read it (the snapshot every row of the run carries)."""
        if day not in self._freeze:
            self._freeze[day] = self.freeze_sha_of(day) or ''
        return self._freeze[day]

    def find_stale(self, rows=None):
        rows = self.sd.trials() if rows is None else rows
        return find_stale(self.study, rows, self.code_sha, self.settings_sha, lambda d: self.freeze_sha_of(d) or '')

    @staticmethod
    def stale_stop_reason(reasons):
        """The --on-stale stop reason (Runner.run and the orchestrator's pre-check say the same)."""
        return ('stale_context: ' + '; '.join(reasons) + '. Nothing was replayed; the rows of this study were made '
                'with another context. Re-run with --on-stale reeval to mark them stale and re-evaluate (base first), '
                'or start a new study')

    def context_drift(self):
        """Lines naming what changed since the run started: code / settings (unless pinned), frozen metas of the
        days this run has used. [] = nothing."""
        out = []
        code, sett = self._pinned
        live_code, live_sett = live_context(self.settings_path, self.software_dir, self.study.settings_sha)
        if not code and live_code != self.code_sha:
            out.append(f'code_sha changed during the run: {contracts.short(self.code_sha)} -> {contracts.short(live_code)}')
        if not sett and live_sett != self.settings_sha:
            out.append(f'settings_sha changed during the run: {contracts.short(self.settings_sha)} -> '
                       f'{contracts.short(live_sett)}')
        for day, sha in sorted(self._freeze.items()):
            now = self.freeze_sha_of(day) or ''
            if now != sha:
                out.append(f'freeze_sha of {day} changed during the run: {contracts.short(sha) or "none"} -> '
                           f'{contracts.short(now) or "none"}')
        return out

    def mark_stale(self, stale, trial_no):
        """Append a `stale` row for each (row, fields) of find_stale (same key, so it supersedes the done row);
        returns the next trial number."""
        for r, fields_ in stale:
            new = TrialRow.from_dict(r.to_dict(), strict=False)
            new.trial, new.status, new.finished = trial_no, 'stale', now_iso()
            new.flags = (list(r.flags) + ['stale: ' + ', '.join(fields_)])[-12:]
            try:
                self.sd.append(new)
            except StudyError:                                # the line limit: keep the identity, drop the numbers
                new.metrics, new.files = {}, {}
                self.sd.append(new)
            trial_no += 1
        return trial_no

    # ---- identity
    def params_of(self, task):
        """task.params snapped onto the grid (ParamSpace.normalize, space order): an off-grid float such as
        1.4500000001 names the grid point 1.45, so key, vhash, variant label and the stored params agree. An unknown
        key is a configuration error (StudyError, exit 3). The controls keep the phone's values as they are (a
        phone value between grid points must reach the VDF unchanged, or the sentinel would not be the phone)."""
        if not task.params:
            return {}
        unknown = [n for n in task.params if n not in self.space]
        if unknown:
            raise StudyError(f'task {task.day}: "{unknown[0]}" is not a searched parameter')
        if is_control(task):
            return {n: task.params[n] for n in self.space.names if n in task.params}
        try:
            return self.space.normalize(task.params)
        except ContractError as e:
            raise StudyError(f'task {task.day}: {e}') from None

    def key_of(self, task):
        """contracts.key over params, day, settings_sha, code_sha, freeze_sha, objective_version. A control's day
        part is '<day>#<control>#<instance>', so a control never shares a key with the variant of the same params
        (round.py would otherwise drop that variant) and every scheduled control is a trial of its own."""
        day = task.day
        if is_control(task):
            day = f'{task.day}#{task.control}#{(task.meta or {}).get("instance", "")}'
        return contracts.key(self.params_of(task), day, self.settings_sha, self.code_sha,
                             self.freeze_sha(task.day), self.study.objective_version)

    def vhash_of(self, task):
        return contracts.vhash(self.params_of(task))

    def variant_of(self, task):
        if task.variant:
            return task.variant
        if is_control(task):
            return task.control
        params = self.params_of(task)
        return 'base' if not params else self.space.variant_name(params)

    # ---- lock
    def acquire(self, phase='start', queue_len=0):
        """Take the study lock or raise LockHeld (exit 3). Removes a STOP file addressed to an earlier run: acquire
        is called once per whole run (a standalone Runner.run, or open_session for the orchestrator's rounds), so a
        STOP written while the run is going is never removed by it.

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

    def open_session(self, phase='start', queue_len=0):
        """Take the lock for a run of several rounds (the orchestrator): acquire() once; every run() until
        close_session() works under this lock and leaves it held. LockHeld (exit 3) as acquire()."""
        self.refresh_context()
        self.acquire(phase, queue_len)
        self.session = True
        return self.state

    def close_session(self, exit_code, stop_reason, stop_file=False, before_release=None):
        """Release the session's lock once, with the run's real exit code and reason, in this order:
        1. the STOP file is removed when `stop_file` says it stopped the run (an explicit flag, never read from the
           reason text): it was addressed to this run, and removing it while the lock is still ours cannot eat a
           STOP meant for a run that starts after the release;
        2. `before_release()` (the orchestrator: tier 1 and the report page) runs while the lock is still held, so
           no other run can start and write rows while the page is built; during it build_status() in this
           process already reports the final exit code and reason (StudyDir.closing), STATE.json on disk does
           not yet (a recorded exit_code would free the lock for other processes);
        3. release(): STATE.json exit_code / stop_reason, status.json, the LOCK removed - last."""
        self.session = False
        if stop_file:
            self.sd.clear_stop()
        if before_release is not None:
            self.sd.closing = (exit_code, _clip(stop_reason))
            try:
                self.heartbeat()
                before_release()
            except Exception as e:                       # the page is a convenience; the release must happen
                self.log(f'before release: {type(e).__name__}: {e}')
            finally:
                self.sd.closing = None
        return self.release(exit_code, stop_reason)

    def time_left(self):
        """Seconds of budget left (None = no budget): the session's deadline, else budget_s from acquire()."""
        if self.deadline is not None:
            return self.deadline - time.time()
        if self.budget_s is not None and self.started_at is not None:
            return self.budget_s - (time.time() - self.started_at)
        return None

    def heartbeat(self, **updates):
        """Rewrite STATE.json with a fresh heartbeat (call it from long evaluations too)."""
        if self.state is None:
            return None
        for k, v in updates.items():
            setattr(self.state, k, v)
        self.state.heartbeat = now_iso()
        left = self.time_left()
        if left is not None:
            self.state.budget_left_s = round(max(0.0, left), 1)
        self.state.disk_free_mb = self._disk()
        self.sd.write_state(self.state)
        return self.state

    def idle(self):
        """Called while waiting for a result that has not come (the pool wakes at least every 30 s): fresh
        heartbeat, then RunStop on a STOP file or an exhausted budget, GuardStop on low disk."""
        self.heartbeat()
        probe = RunResult(exit_code=EXIT_OK, stop_reason=FINISHED)
        if self._stop_check(probe):
            raise RunStop(probe.stop_reason, stop_file=probe.stop_file)
        return None

    def release(self, exit_code, stop_reason):
        if self.state is None:
            return None
        self.state.exit_code = exit_code
        self.state.stop_reason = _clip(stop_reason)
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

    # ---- guards on rows
    def base_loops(self, day):
        """metrics.loops of the day's base row in this run's context (None when there is none yet)."""
        if day not in self._base_loops:
            r = self.sd.latest_by_key(self.sd.trials()).get(self.key_of(Task(params={}, day=day, control='base')))
            loops = (r.metrics or {}).get('loops') if r is not None and r.status == 'done' else None
            if loops is None:
                return None
            self._base_loops[day] = loops
        return self._base_loops[day]

    def loop_mismatch(self, task, row):
        """(base_loops, row_loops) when a done row's loop count differs from its day's base row, else None.
        A base row sets the day's reference."""
        if row.status != 'done':
            return None
        loops = (row.metrics or {}).get('loops')
        if loops is None:
            return None
        if is_base_task(task):
            self._base_loops[task.day] = loops
            return None
        base = self.base_loops(task.day)
        if base is not None and loops != base:
            return base, loops
        return None

    @staticmethod
    def loop_stop_reason(day, row):
        """The public, count-free stop reason of the unequal-loop guard (passes lint kind 'issue'); the counts are in
        the row's error and guards.json `loop_counts`."""
        return (f'loop counts differ between variants on {day} (trial {row.trial}, {row.variant}); the counts are in '
                f'that row\'s error and in guards.json. State leaked between in-process replays or the frozen day '
                f'changed under the run')

    def check_loops(self, task, row):
        """GuardStop when a done row's loop count differs from its day's base row (DESIGN.md section 5.1). The
        reason carries no loop counts (it may reach the public issue); record() keeps them in the row's error."""
        if self.loop_mismatch(task, row) is not None:
            raise GuardStop(self.loop_stop_reason(task.day, row))

    def note_guards(self, guards, run_tag, rnd, result):
        """Record whether the sentinel could be scheduled for every day of the run: guards.json `sentinel`
        ({schedulable, days: {day: [reason per parameter]}, run, round}), RunResult.guards, the log; the rows of
        an unschedulable day get the flag `sentinel_not_schedulable` (record)."""
        self._not_schedulable = dict(getattr(guards, 'not_schedulable', {}) or {})
        notes = guards.guard_notes() if hasattr(guards, 'guard_notes') else []
        result.guards = list(notes)
        entry = {'schedulable': not self._not_schedulable, 'days': self._not_schedulable, 'run': run_tag,
                 'round': rnd, 'at': now_iso()}
        try:
            self.sd.update_guards(sentinel=entry)
        except (OSError, ValueError) as e:                   # pragma: no cover
            self.log(f'guards.json not written: {e}')
        for n in notes:
            self.log(f'{self.sd.name}: {n} - the sentinel and null controls do not cover that day')

    # ---- the loop
    def pending(self, tasks):
        """(task, key) for the tasks whose key has no done row yet, in order."""
        done = self.sd.done_keys()
        return [(t, k) for t, k in ((t, self.key_of(t)) for t in tasks) if k not in done]

    def _reeval_order(self, todo, stale_days, rnd):
        """Base replays first: the pending base tasks, plus a base task for every day with stale rows that is not in
        the run (its floor must be re-measured before the variants of the day are compared with it)."""
        base = [(t, k) for t, k in todo if is_base_task(t)]
        rest = [(t, k) for t, k in todo if not is_base_task(t)]
        have = {t.day for t, _ in base}
        done = self.sd.done_keys()
        for d in sorted(stale_days - have):
            t = Task(params={}, day=d, phase='floor', control='base', round=rnd)
            k = self.key_of(t)
            if k not in done:
                base.append((t, k))
        return base + rest

    def record(self, task, key, out, trial_no, started, t0, phase, guards=None):
        """Build, append and check one row from evaluate()'s output (the pool path calls this per result).
        Returns (row, stop) where stop is a guard stop reason or None; the row is appended in either case."""
        seconds = out.get('seconds', round(time.time() - t0, 3))
        status = out.get('status') or ('done' if out.get('metrics') else 'failed')
        params = self.params_of(task)
        flags = list(out.get('flags') or [])
        error = out.get('error') or ('evaluation returned no metrics' if status == 'failed' else None)
        stop = None
        if guards is not None and is_control(task):
            stop, gflags = guards.verdict(task, out)
            flags = gflags + flags                           # the guard's verdict first (the row keeps 16 flags)
            if stop:
                error = out.get('guard_detail') or stop      # the detail (values, counts) stays in the private row
        if task.day in self._not_schedulable:
            flags = ['sentinel_not_schedulable'] + flags
        row = TrialRow(trial=trial_no, key=key, phase=task.phase or phase, vhash=contracts.vhash(params),
                       variant=self.variant_of(task), params=params, day=task.day, status=status,
                       code_sha=self.code_sha, settings_sha=self.settings_sha,
                       freeze_sha=self.freeze_sha(task.day), seconds=seconds,
                       worker=out.get('worker', self.worker), rung=task.rung, round=task.round,
                       started=started, finished=now_iso(), metrics=dict(out.get('metrics') or {}),
                       feasible=out.get('feasible'), flags=flags[:16],
                       files=dict(out.get('files') or {}), error=error, control=task.control,
                       metrics_version=int(out.get('metrics_version') or current_metrics_version()))
        mismatch = self.loop_mismatch(task, row) if stop is None else None
        if mismatch is not None:
            base, loops = mismatch
            detail = (f'unequal loop counts on {task.day}: base {base}, {row.variant} {loops} (trial {row.trial})')
            row.flags = (['unequal_loops'] + list(row.flags))[:16]
            row.error = detail + (f'; {row.error}' if row.error else '')
            stop = self.loop_stop_reason(task.day, row)
        self.sd.append(row)
        if mismatch is not None:
            try:
                self.sd.add_loop_count_guard({'day': task.day, 'trial': row.trial, 'variant': row.variant,
                                              'base': mismatch[0], 'loops': mismatch[1], 'at': now_iso()})
            except (OSError, ValueError) as e:               # pragma: no cover - the row has the counts anyway
                self.log(f'guards.json not written: {e}')
        return row, stop

    def _stop_check(self, result):
        """None to go on, else sets result.stop_reason and returns True (STOP file, budget); GuardStop on low disk."""
        if self.sd.stop_requested():
            result.stop_reason, result.stop_file = STOP_REASON, True
            return True
        left = self.time_left()
        if left is not None and left <= 0:
            result.stop_reason = 'budget exhausted'
            return True
        disk = self._disk()
        if disk is not None and self.min_disk_mb and disk < self.min_disk_mb:
            raise GuardStop(f'disk below {self.min_disk_mb} MB free ({disk:.0f} MB)')
        return None

    def run(self, tasks, evaluate, phase=None, on_stale=None, guards=None, batch_evaluate=None, on_record=None):
        """Evaluate `tasks` in order; see the class docstring. Returns RunResult; never raises for lock/guard
        conditions (they are the exit codes), only for a broken study (StudyError, exit 3) before the lock.

        Batch hook (H-305, the orchestrator's pool path). `batch_evaluate(tasks)` - when given, `evaluate` is not
        called - receives the whole queue of the run after resume and control scheduling (base tasks, sentinel and
        null controls included, in order) and returns an iterator of (task, out) pairs in completion order, `out`
        being what evaluate(task) / guards.evaluate(task) would return (a control's `out` carries `csv` for the
        verdict). Every pair is recorded exactly like a serial result (record(): row, guards, loop counts); STOP,
        budget and disk are checked before the first pair is requested and after every recorded pair, so a stop
        loses only the trials in flight (they have no row and are re-run on resume). The iterator may raise
        RunStop (Runner.idle: STOP / budget noticed while waiting, exit 0) or GuardStop (exit 2); any other
        exception out of it is an exit 2 whose stop reason names the exception type and its first line (lint-
        checked for the public issue; the full text goes to the log), after the rows recorded so far. On any exit
        from the loop the iterator's close() is called (a generator's finally: the orchestrator terminates its pool
        there). `on_record(task, row, index, total)` is called after each appended row (index 1-based, total = the
        queue length of this run), both paths; its exceptions are logged, never fatal.

        Inside a session (open_session) the lock is neither taken nor released here: phase, queue_len and the
        heartbeat of STATE.json are updated and the lock stays with the session; a STOP file stays where it is
        (close_session removes it, told by RunResult.stop_file); the log gets a 'round N recorded' line, the
        session's close has the exit line."""
        on_stale = on_stale or self.on_stale
        if on_stale not in ON_STALE:
            raise StudyError(f'--on-stale must be one of {", ".join(ON_STALE)}, not {on_stale!r}')
        guards = guards if guards is not None else self.guards
        tasks = list(tasks)
        phase = phase or (tasks[0].phase if tasks else 'idle')
        self.refresh_context()
        reasons, stale = self.find_stale()
        self.stale_context = list(reasons)
        rows0 = self.sd.trials()
        done0 = self.sd.done_keys(rows0)
        todo = [(t, k) for t, k in ((t, self.key_of(t)) for t in tasks) if k not in done0]
        result = RunResult(exit_code=EXIT_OK, stop_reason=FINISHED, skipped=len(tasks) - len(todo),
                           stale_context=list(reasons), already_done=len(done0))
        own = not self.session
        if own:
            try:
                self.acquire(phase, queue_len=len(todo))
            except LockHeld as e:
                self.log(str(e))
                return RunResult(exit_code=EXIT_REFUSED, stop_reason=str(e), skipped=result.skipped,
                                 already_done=result.already_done)
        else:
            self.state.exit_code, self.state.stop_reason = None, None
            self.state.stale_context = list(self.stale_context)
            self.state.trials_done = len(done0)
            self.heartbeat(phase=phase, queue_len=len(todo))
        trial_no = self.sd.next_trial_number(rows0)
        failures = 0
        drift = []
        rnd = next((t.round for t in tasks if t.round is not None), None)
        self._not_schedulable = {}
        batch_iter = None
        try:
            if reasons:
                if on_stale == 'stop':
                    raise GuardStop(self.stale_stop_reason(reasons))
                trial_no = self.mark_stale(stale, trial_no)
                result.marked_stale = len(stale)
                # a metrics_version stale row keeps its key: it was 'done' when todo was built, hand it out again
                done_now = self.sd.done_keys()
                todo = [(t, k) for t, k in ((t, self.key_of(t)) for t in tasks) if k not in done_now]
                result.skipped = len(tasks) - len(todo)
                self.state.stale_context = list(reasons) + [f're-evaluating: {len(stale)} rows marked stale, base first']
                todo = self._reeval_order(todo, {r.day for r, _ in stale}, rnd)
                self.log(f'{self.sd.name}: stale context ({"; ".join(reasons)}); {len(stale)} rows marked stale, '
                         f'base replays first')
            if guards is not None:
                # the run tag is unique per run (round number + the first trial number of this run): the controls
                # of a re-run after a kill -9 are new keys, never a second done row of an old key
                run_tag = (f'r{rnd}.t{trial_no}' if rnd is not None else f'run{trial_no}')
                sched = guards.schedule([t for t, _ in todo], run_tag, covered_days=Guards.sentinel_days_done(self.sd.trials()))
                keys = {id(t): k for t, k in todo}
                todo = [(t, keys.get(id(t)) or self.key_of(t)) for t in sched]
                done_now = self.sd.done_keys()
                todo = [(t, k) for t, k in todo if not (is_control(t) and k in done_now)]
                self.note_guards(guards, run_tag, rnd, result)
                self.heartbeat(queue_len=len(todo))
            if batch_evaluate is not None:
                keys = {id(t): k for t, k in todo}
                if self._stop_check(result) or not todo:
                    batch_iter = iter(())
                else:
                    batch_iter = batch_evaluate([t for t, _ in todo])
                source = ((t, keys.get(id(t)) or self.key_of(t), out) for t, out in batch_iter)
            else:
                source = ((t, k, None) for t, k in todo)
            for i, (task, key, pre) in enumerate(source):
                if batch_iter is None and self._stop_check(result):
                    break
                control = is_control(task) and guards is not None
                if pre is not None:                          # the batch path: the trial has finished already
                    started = now_iso()
                    t0 = time.time()
                    out = pre or {}
                else:
                    self.heartbeat(phase=task.phase or phase, queue_len=len(todo) - i, last_key=key)
                    started = now_iso()
                    t0 = time.time()
                    try:
                        out = (guards.evaluate(task) if control else evaluate(task)) or {}
                    except (GuardStop, RunStop, KeyboardInterrupt):
                        raise
                    except Exception as e:                   # a broken evaluation is a failed trial, not a crash
                        out = {'status': 'failed', 'error': f'{type(e).__name__}: {e}'[:1000]}
                row, stop = self.record(task, key, out, trial_no, started, t0, phase, guards if control else None)
                status = row.status
                trial_no += 1
                result.done += 1
                result.controls += 1 if is_control(task) else 0
                if status == 'failed':
                    result.failed += 1
                    failures += 1
                elif status == 'blocked':
                    result.blocked += 1
                    failures = 0
                else:
                    failures = 0
                self.state.trials_done += 1 if status == 'done' else 0
                new_drift = [d for d in self.context_drift() if d not in drift]
                if new_drift:
                    drift += new_drift
                    self.state.stale_context = list(self.state.stale_context) + new_drift
                    self.log(f'{self.sd.name}: ' + '; '.join(new_drift) + ' - finishing the round, then stopping')
                self.heartbeat(queue_len=max(0, len(todo) - i - 1), last_key=key)
                if self.status_every and result.done % self.status_every == 0:
                    try:
                        self.sd.write_status()
                    except Exception as e:                   # pragma: no cover
                        self.log(f'status.json not written: {e}')
                if on_record is not None:
                    try:
                        on_record(task, row, i + 1, len(todo))
                    except Exception as e:                   # reporting must never stop a run
                        self.log(f'on_record failed: {type(e).__name__}: {e}')
                if stop:
                    raise GuardStop(stop)
                if failures >= self.max_failures:
                    raise GuardStop(f'{failures} consecutive failed trials (last: {row.error})')
                if batch_iter is not None and i + 1 < len(todo) and self._stop_check(result):
                    break
            if drift and result.stop_reason == FINISHED:
                raise GuardStop('stale_context: ' + '; '.join(drift) + '. The round was finished with the context it '
                                'started with; the next run reports its rows as stale (--on-stale stop) or '
                                're-evaluates them (--on-stale reeval)')
        except GuardStop as e:
            result.exit_code, result.stop_reason = EXIT_GUARD, _clip(f'guard: {e.reason}')
        except RunStop as e:
            result.exit_code, result.stop_reason = EXIT_OK, _clip(e.reason)
            result.stop_file = e.stop_file
        except KeyboardInterrupt:
            result.exit_code, result.stop_reason = EXIT_OK, 'interrupted (Ctrl-C); the trial in flight is lost, re-run resumes'
        except StudyError as e:                              # a row refused / a task off the space: configuration
            result.exit_code, result.stop_reason = EXIT_REFUSED, _clip(f'refused: {e}')
            self.log(f'{self.sd.name}: refused: {e}')
        except Exception as e:                               # the evaluation machinery itself broke (pool, spec ...)
            self.log(f'{self.sd.name}: the evaluation raised {type(e).__name__}: {e}')
            result.exit_code, result.stop_reason = EXIT_GUARD, exception_reason(e)
        finally:
            close = getattr(batch_iter, 'close', None)
            if close is not None:                            # the pool path: stop the work still in flight
                try:
                    close()
                except Exception as e:                       # pragma: no cover
                    self.log(f'batch iterator close failed: {e}')
            result.stale_context = list(self.state.stale_context) if self.state else result.stale_context
            if own:
                if result.stop_file:                     # addressed to this run: removed before the lock goes
                    self.sd.clear_stop()
                self.release(result.exit_code, result.stop_reason)
            else:
                self.heartbeat()
        counts = (f'{result.done} rows appended, {result.already_done} already done, {result.failed} failed, '
                  f'{result.blocked} blocked')
        if own:
            self.log(f'{self.sd.name}: {result.stop_reason}; {counts}; exit {result.exit_code}')
        else:                                            # a round of a session: the exit line is the session's close
            self.log(f'{self.sd.name}: round{"" if rnd is None else f" {rnd}"} recorded: {counts}'
                     + ('' if result.stop_reason == FINISHED else f'; {result.stop_reason}'))
        return result


def exception_reason(exc):
    """The public stop reason for an unexpected exception of the evaluation machinery: 'guard: the evaluation
    raised <Type>: <first line>' with the home directory shortened to ~; when the first line does not pass the lint
    (kind 'issue': the reason may reach the public campaign issue) only the type is named (the log has the text)."""
    first = (str(exc).strip().splitlines() or [''])[0]
    home = os.path.expanduser('~')
    if home and home != '~':
        first = first.replace(home, '~')
    first = _clip(first, 200)
    text = f'guard: the evaluation raised {type(exc).__name__}' + (f': {first}' if first else '')
    try:
        from hindcast import lint
        if lint.lint_findings(text, 'issue'):
            text = f'guard: the evaluation raised {type(exc).__name__} (message in the log of the run)'
    except Exception:                                        # pragma: no cover - lint absent: type only
        text = f'guard: the evaluation raised {type(exc).__name__} (message in the log of the run)'
    return _clip(text)

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


def aggregate_floor(rows, days=None):
    """Status.floor: metrics.study_floor over the base rows (latest done per day): max |base - phone| per component
    and min agreement_pct. `days` = the scored days (search + hold-out, never a quarantined day: quarantined days are
    listed, not scored, DESIGN.md 5.4 phase 0); None = every day with a base row. build_status passes search +
    hold-out days, so this floor is wider than (or equal to) round.json's `floors`, which cover the scored search
    days only (the days the review order is ranked on); the page labels each with its day set."""
    try:
        from hindcast.metrics import floors_from_base, study_floor
    except Exception:                                        # pragma: no cover - metrics.py absent
        return {}
    by_day = {}
    for r in rows:
        if r.status != 'done' or not r.metrics or not (r.control == 'base' or r.vhash == BASE_VHASH):
            continue
        by_day[r.day] = floors_from_base(r.metrics)
    return {k: _round3(v) for k, v in study_floor(by_day, days).items()}


ELLIPSIS = '...'


def cut_words(text, limit):
    """`text` in at most `limit` characters: unchanged when it fits, else cut at the last word boundary (space,
    or after ',' ';' ':') before limit - 3, trailing punctuation dropped, plus '...' - never mid-word."""
    text = str(text or '')
    if len(text) <= limit:
        return text
    room = max(1, limit - len(ELLIPSIS))
    head = text[:room + 1]
    cut = max(head.rfind(' '), -1)
    head = head[:cut] if cut > 0 else text[:room]
    head = head.rstrip(' ,;:-(')
    return head + ELLIPSIS


def guard_lines(study_dir):
    """The guard notes of guards.json for `hc study status`: one line per day without a schedulable sentinel
    ('sentinel: not schedulable on <day> (<reason per parameter>)'). [] when the last run could schedule it."""
    sd = study_dir if isinstance(study_dir, StudyDir) else StudyDir(study_dir)
    sent = sd.read_guards().get('sentinel') or {}
    if sent.get('schedulable', True):
        return []
    days = sent.get('days') or {}
    return [f'sentinel: not schedulable on {d} ({"; ".join(map(str, r))})' for d, r in sorted(days.items())]


def _day_label(d, not_frozen):
    """status.days value: the role; 'quarantined: <reason>'; '<role>, not frozen' (or 'partial freeze' / 'stale
    freeze') for a day the orchestrator found unreplayable at the start of its last round (guards.json)."""
    if d.role == 'quarantined' and d.quarantine_reason:
        return f'quarantined: {d.quarantine_reason}'
    why = (not_frozen or {}).get(d.day)
    if why and d.role != 'quarantined':
        text = str(why)
        label = ('not frozen' if 'not frozen' in text else 'partial freeze' if 'non-final' in text
                 else 'stale freeze' if 'stale' in text else 'not replayable')
        return f'{d.role}, {label}'
    return d.role


def build_status(study_dir, now=None):
    """Status (<= 2 KB) from study.json, STATE.json, trials.jsonl and, when present, round.json. While this
    process closes its session (StudyDir.closing, Runner.close_session) the final exit code and reason are taken
    from there: STATE.json gets them only at the release."""
    sd = study_dir if isinstance(study_dir, StudyDir) else StudyDir(study_dir)
    sd.require()
    not_frozen = sd.read_guards().get('not_frozen') or {}
    study = Study.load(sd.study_json, strict=False)
    state = sd.read_state()
    closing = getattr(sd, 'closing', None)
    if closing is not None and state is not None and state.pid == os.getpid() and state.exit_code is None:
        state.exit_code, state.stop_reason = closing
    latest = sd.latest_by_key(sd.trials())
    rows = list(latest.values())
    holder = sd.lock_holder(now)
    counts = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    done = counts.get('done', 0)
    mv = current_metrics_version()
    # to re-evaluate under the same key: done rows of another metrics version, and the `stale` rows --on-stale reeval
    # wrote for them (a code / settings / freeze change makes new keys instead; its old rows stay `stale` for good).
    # Controls (their keys carry the run, they are never handed out again) and rows of quarantined days (no phase
    # schedules them) are left out: the count is what a reeval run would replay.
    scored = set(study.search_days()) | set(study.holdout_days())
    stale_n = sum(1 for r in rows if r.control not in CONTROL_KINDS and r.day in scored
                  and (metrics_stale(r, mv) or (r.status == 'stale' and (r.flags or [''])[-1] == 'stale: metrics_version')))
    planned = len(latest)                                    # every key attempted so far (done, failed, blocked, stale)
    if state is not None:
        phase = state.phase
        planned += max(0, int(state.queue_len or 0))         # plus what the run still had queued: a live run's queue,
                                                             # or what a stopped run left (0 after a finished run)
    else:
        pending = [p.kind for p in study.phases if p.state == 'pending']
        phase = pending[0] if pending else ('done' if study.phases else 'new')
    status = Status(study=study.name, question=study.question, phase=phase, trials_done=done, trials_planned=planned,
                    running=holder is not None, heartbeat=(state.heartbeat if state else study.created),
                    trials_failed=counts.get('failed', 0), trials_blocked=counts.get('blocked', 0),
                    trials_stale=stale_n, pid=(state.pid if state and holder else None), round=(state.round if state else None),
                    stale_context=list(state.stale_context) if state else [],
                    floor=aggregate_floor(rows, set(study.search_days()) | set(study.holdout_days())),
                    days={d.day: _day_label(d, not_frozen) for d in study.days},
                    exit_code=(state.exit_code if state else None), stop_reason=(state.stop_reason if state else None),
                    report=(os.path.join('report', 'index.html') if os.path.isfile(os.path.join(sd.report_dir, 'index.html')) else None))
    rnd = sd.read_round()
    if rnd is not None:
        from hindcast import metrics as _m                   # lazy: metrics imports emulator_optimize
        status.dead_parameters = [d.key for d in rnd.dead_parameters]
        by_vhash = {c.vhash: c for c in rnd.variants}
        order = ([v for v in rnd.front if v in by_vhash]                     # no step: candidates, then the
                 or [c.vhash for c in rnd.variants                            # envelope-limited ones; never a tie
                     if c.verdict in ('recommended', 'candidate', 'envelope-limited')])
        if rnd.validation:                                   # validate round: recommended for review first, then the order
            order = ([v for v in order if by_vhash[v].verdict == 'recommended']
                     + [v for v in order if by_vhash[v].verdict != 'recommended'])
        for vh in order[:5]:
            c = by_vhash[vh]
            effect = dict(list((c.effect or {}).items())[:4])
            vline = _m.validation_line(c.validation)
            status.top.append(TopItem(vhash=contracts.short(c.vhash), variant=c.variant, verdict=c.verdict,
                                      why=cut_words(c.why or c.verdict, 120), rank=c.rank,
                                      effect={k: _round3(v) for k, v in effect.items()},
                                      validation=cut_words(vline, 80) if vline else None))
    status.guards = guard_lines(sd)                          # not a Status field (contract): status_text only
    # fit the 2 KB limit: the lower-value fields go first (round.json, study.json and guards.json keep all of them):
    # effect numbers down to two, the stop reason, the quarantine reasons, the effect numbers, then the why texts
    # (cut at a word boundary with an ellipsis, never mid-word), and only then a whole top item
    def fits():
        return len(status.to_compact_json().encode('utf-8')) <= STATUS_MAX_BYTES

    def shorten_why(limit):
        if any(len(t.why) > limit for t in status.top):
            for t in status.top:
                t.why = cut_words(t.why, limit)
            return True
        return False

    for _ in range(24):
        if fits():
            break
        if status.top and any(len(t.effect) > 2 for t in status.top):
            for t in status.top:
                t.effect = dict(list(t.effect.items())[:2])
        elif status.stop_reason and len(status.stop_reason) > 160:
            status.stop_reason = cut_words(status.stop_reason, 160)
        elif any(':' in v for v in status.days.values()):
            status.days = {d: v.split(':', 1)[0] for d, v in status.days.items()}
        elif status.top and any(t.effect for t in status.top):
            for t in status.top:
                t.effect = {}
        elif status.top and shorten_why(80):
            pass
        elif status.top and shorten_why(60):
            pass
        elif status.top and any(t.validation and len(t.validation) > 48 for t in status.top):
            for t in status.top:
                if t.validation:
                    t.validation = cut_words(t.validation, 48)
        elif status.top and shorten_why(40):
            pass
        elif status.top:
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
             f'{status.trials_blocked} blocked, {status.trials_stale} stale'
             + (' (to re-evaluate: hc study run --on-stale reeval)' if status.trials_stale else ''),
             f'running: {"yes, pid %s" % status.pid if status.running else "no"}   heartbeat: {status.heartbeat}']
    if status.exit_code is not None:
        lines.append(f'last run: exit {status.exit_code} ({status.stop_reason})')
    if status.stale_context:
        lines.append('stale context: ' + '; '.join(status.stale_context))
    for g in getattr(status, 'guards', None) or []:
        lines.append(f'guards: {g} - the sentinel and null controls did not run on that day')
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
            if t.validation:
                lines.append(f'     {t.validation}')
    lines += ['', HONESTY_STATEMENT]
    return '\n'.join(lines) + '\n'
