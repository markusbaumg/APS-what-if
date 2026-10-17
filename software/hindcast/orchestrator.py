"""
orchestrator.py  -  `hc study run`: the phases of a study, round by round, through the worker pool (backlog H-305;
DESIGN.md sections 4.3, 4.5, 5.4 and 10)

    from hindcast import orchestrator
    code = orchestrator.run_study(study_dir, args)           # what cli.py's `hc study run` calls; -> exit code
    orch = orchestrator.Orchestrator(sd, workers=4, batch=None, hours=None, until=None, on_stale='stop')
    code = orch.run()

One run, in order:
    1. study.json is loaded (a locked key is refused, exit 3). The Runner comes from search.runner_for with
       `freeze_sha_of = replay.freeze_sha_of(study.source, settings)`, so every key carries the frozen day's sha1
       and `hc study run` and the search agree on what is done. A replay.ReplayContext (base CSVs under
       runs/base/<day>.csv, tier-2 files under report/) and study.Guards.from_context (sentinel / null controls)
       are built over that Runner's key_of.
    2. One lock for the whole run: Runner.open_session() takes it once (a STOP left by an earlier run is removed
       here and only here), every round runs inside it, and Runner.close_session() releases it once with the real
       exit code. Between rounds STATE.json says 'planning round N' with a fresh heartbeat and no exit code, so
       `hc study status` shows the run as running and a second `hc study run` is refused (exit 3).
    3. `--grow`: freeze.freeze_days(<study days>, new=True, offline=True) at most once per hour (stamp in
       grow.json), between rounds only (a freeze inside a round would be a freeze_sha drift = stale_context stop).
    4. A round starts with the freeze check of every search / hold-out day (freeze.blocked_reason; the days that
       cannot be replayed go to guards.json `not_frozen`, status.json lists them as '<role>, not frozen') and a
       STOP / budget check (study.STOP_REASON 'stopped: STOP file', exit 0, the same reason as inside a round). search.plan() mirrors phase completeness into study.json
       (done / skipped), search.next_batch() gives the pending tasks of the first unfinished phase (base replays
       first, cut to --batch; a day whose latest base row is failed or blocked gets its base alone and its
       variants wait; an unfrozen day gets one blocked base row and then nothing until it is frozen), and
       Runner.run(batch, batch_evaluate=..., on_record=...) records them. The batch hook (study.Runner.run) hands
       the run's whole queue - base tasks, the sentinel / null controls the Guards scheduled, the variants - to
       pool.run_tasks(ctx, tasks, pool=<WorkerPool>, spec_of=...) (base wave first, then the rest; controls go
       through Guards.control_spec so their CSV is kept for the verdict) and gets (task, row fields) back in
       completion order. Lock, STATE.json, STOP, resume, budget, the disk guard (< study.MIN_DISK_MB = 2 GB free on
       the study's volume: exit 2), three consecutive failed trials (exit 2), the control verdicts and the loop
       count guard all stay the Runner's; status.json is rewritten after every trial (Runner.status_every = 1).
       While the pool has no result for WAKE (30 s) the Runner's idle() runs (heartbeat, STOP, budget, disk); a
       trial without an answer after --trial-timeout (600 s) is a failed row and the pool is restarted.
       As soon as the base rows of the round are in, the base-agreement rule of the quarantine is applied to their
       days (after_base_wave): a day below the floor is quarantined before any of its variants is replayed.
    5. After each round: days.quarantine_from_trials / apply_quarantine (a quarantined day leaves the search days:
       study.json role 'quarantined' with the reason; the next round's plan no longer schedules it), round.json +
       status.json (round.produce: verdicts, dead parameters, floors), the text of the would-be campaign comment
       (gh.round_body with the study's linter) into round_comment.md - nothing is posted, the skill posts it - and
       tier 1 (export.build_tier1 -> report/tier1.json).
    6. The loop ends when every built phase is settled, the budget (--hours / --until HH:MM local / study.json
       budget, the smallest; `--hours 0` is refused, exit 3) is used up, STOP / Ctrl-C, a guard, or a round that
       makes no progress (nothing to hand out, or the same keys again after a round without a done or failed row:
       exit 2, the reason names the held days and why, e.g. not frozen, base failed 3 times). Tier 1 is also
       rewritten every TIER1_EVERY (10) trials. On exit, in this order (Runner.close_session): the STOP file is
       removed when it stopped the run (an explicit flag, not a match on the reason text), tier 1 is rewritten
       and the report rendered (report.render_report, not single-file) while the lock is still held, then the
       release writes STATE.json (stop_reason with the control verdicts appended, "sentinel equals base on
       <day> ...") and status.json and removes the lock; round_comment.md, the exit line, the honesty statement
       and the path of the report follow.

One line per trial on stdout: '<n>/<total> <variant> <day> <status> <seconds>s <metric summary>' (n counts the
rows this process appended; total = n so far + what the current round still has queued).

Resumability (proved by tests/test_orchestrator.py and by the kill test below). A row is appended only when its
trial has finished, so kill -9 loses the trials in flight (one per worker at most), Ctrl-C the same (the workers
ignore SIGINT, the parent's Runner catches KeyboardInterrupt, releases the lock and exits 0), a STOP file the
trials still in the pool's queue (the Runner breaks after the trial it recorded; the generator's close()
terminates the pool). The next `hc study run` takes the lock over (dead pid), and every key with a done row is
skipped - the controls included: their key carries the run (round number and first trial number of the run), so a
re-run's sentinel / null are new keys, never a second done row of an old one. A failed base row is persisted as
such and handed out again (up to search.BASE_RETRY_LIMIT) before any variant of its day, also by the next
process. After a kill -9 the workers exit by themselves when the parent's pipes close.

Measured 2026-10-17 (Apple silicon, 18 cores, Python 3.14; frozen 2026-09-06, 1311 loops, whole UTC day):
    hc study new mvp --from-grid variants/grid_autoISF.json --days 2026-09-06 --question explore
    hc study run mvp --workers 4       18 rows (base, sentinel, null, 15 grid variants) in 19.1 s wall clock
                                       (29.5 s on a second run while other test suites shared the machine); one
                                       trial 2.2-3.9 s inside a worker, spawn + pool start ~1.5 s; sentinel and
                                       null equal to base on every loop; base agreement 97.5 %, floor_loops 30
    kill -9 after 5 rows, re-run       (studies/mvp_kill) rows 5 -> 20: the 13 missing variants plus a fresh
                                       sentinel and null (controls run again in every run by design); 16 variant
                                       keys, none twice; the re-run took 12.3 s; the 4 orphaned workers exited by
                                       themselves within 10 s of the kill

Nothing here posts to GitHub, opens a socket (freeze --new runs offline) or changes the phone or ns_settings.json.
"""
import datetime
import json
import os
import signal
import sys
import time

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import pool as pl                                                             # noqa: E402
from hindcast import study as st                                                            # noqa: E402
from hindcast.contracts import HONESTY_STATEMENT, now_iso                                   # noqa: E402
from hindcast.study import EXIT_GUARD, EXIT_OK, EXIT_REFUSED, StudyError                    # noqa: E402

TIER1_EVERY = 10                         # trials between two tier-1 rewrites (and on exit)
GROW_EVERY_S = 3600                      # freeze --new at most hourly
GROW_JSON = 'grow.json'
ROUND_COMMENT = 'round_comment.md'
TIER1_JSON = 'tier1.json'                # report/tier1.json


def _worker_init(frozen_dir):
    """Pool initializer: Ctrl-C is the parent's business (it releases the lock and terminates the pool); the worker
    ignores SIGINT so a Ctrl-C never kills a trial half-way through its stdout capture."""
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):                            # pragma: no cover
        pass
    pl.default_init(frozen_dir)


def seconds_until(hhmm, now=None):
    """'HH:MM' local -> seconds from now until that time (today, else tomorrow). StudyError on a bad value."""
    try:
        h, m = (int(x) for x in str(hhmm).split(':'))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        raise StudyError(f'--until {hhmm}: expected HH:MM (local time)') from None
    now = now or datetime.datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def budget_seconds(hours=None, until=None, study=None):
    """The smallest of --hours, --until and (when neither is given) study.json's budget.hours; None = no budget.
    `--hours 0` (or less) is refused (StudyError, exit 3): it is not "no budget", and a run that may replay nothing
    is better not started (study.json's budget.hours must be positive already, contracts.Budget)."""
    secs = []
    if hours is not None:
        if not float(hours) > 0:
            raise StudyError(f'--hours {hours:g}: the budget must be positive (leave --hours out to use study.json\'s '
                             f'budget, or stop a run with `hc study stop`)')
        secs.append(float(hours) * 3600.0)
    if until:
        secs.append(seconds_until(until))
    if not secs and study is not None and getattr(study.budget, 'hours', None):
        secs.append(float(study.budget.hours) * 3600.0)
    return min(secs) if secs else None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _signed(v, dp=2):
    return ('+' if v > 0 else '') + f'{v:.{dp}f}'


def metric_summary(row):
    """The tail of the per-trial line: a few decision-level numbers of the row (no glucose outcome)."""
    m = row.metrics or {}
    if row.status != 'done':
        text = ' '.join(str(row.error or row.status).split())
        return text[:100]
    if row.control in st.CONTROL_KINDS:
        verdict = next((f for f in row.flags if f in ('equals_base', 'differs_from_base') or f.startswith('guard_')), '')
        return f'{row.control} {verdict.replace("_", " ")}'.strip()
    parts = []
    if _num(m.get('loops')):
        parts.append(f'loops {int(m["loops"])}')
    if row.control == 'base' or not row.params:
        if _num(m.get('agreement_pct')):
            parts.append(f'agreement {m["agreement_pct"]:.1f} %')
        if _num(m.get('changed_loops')):
            parts.append(f'changed {int(m["changed_loops"])}')
        return ', '.join(parts)
    if _num(m.get('changed_vs_base')):
        parts.append(f'changed {_signed(m["changed_vs_base"], 0)} vs base')
    for k, label in (('rise_vs_base_U', 'rise'), ('rest_vs_base_U', 'rest')):
        if _num(m.get(k)):
            parts.append(f'{label} {_signed(m[k])} U')
    if row.feasible is False:
        parts.append('infeasible')
    return ', '.join(parts)


class Orchestrator:
    """One `hc study run` (see the module docstring). Every argument has the CLI's default; `out` receives the
    per-trial lines and the closing text (default: stdout)."""

    def __init__(self, study_dir, settings=None, settings_path=contracts.NS_SETTINGS_PATH, workers=None, batch=None,
                 hours=None, until=None, on_stale='stop', pred=False, grow=False, frozen_dir=None,
                 min_disk_mb=st.MIN_DISK_MB, max_failures=st.MAX_CONSECUTIVE_FAILURES, tier1_every=TIER1_EVERY,
                 render=True, out=None, pool_factory=None, window=None, wake_s=pl.DEFAULT_WAKE_S,
                 trial_timeout_s=pl.DEFAULT_TRIAL_TIMEOUT_S, between_rounds=None):
        from hindcast import space as spc
        self.sd = study_dir if isinstance(study_dir, st.StudyDir) else st.StudyDir(study_dir)
        self.settings_path = settings_path
        self.settings = settings if settings is not None else spc.load_settings(settings_path)
        self.study = self.sd.load_study(self.settings)       # a locked key is refused here (exit 3)
        self.workers_req = workers if workers is not None else getattr(self.study.budget, 'workers', None)
        self.workers = pl.worker_count(self.workers_req)
        self.batch = batch
        self.budget_s = budget_seconds(hours, until, self.study)
        self.on_stale = on_stale
        self.pred = pred
        self.grow = grow
        self.frozen_dir = frozen_dir
        self.min_disk_mb = min_disk_mb
        self.max_failures = max_failures
        self.tier1_every = tier1_every
        self.render = render
        self.out = out or (lambda text: (sys.stdout.write(str(text) + '\n'), sys.stdout.flush()))
        self.pool_factory = pool_factory
        self.window = tuple(window) if window else None      # (start_iso, end_iso) instead of whole days (tests)
        self.wake_s = wake_s                                 # the pool wakes the Runner at least this often
        self.trial_timeout_s = trial_timeout_s               # a trial without an answer after this is failed
        self.between_rounds = between_rounds                 # callable(orchestrator) after each round (tests)
        self.unfrozen = {}                                   # day -> freeze.blocked_reason, refreshed every round
        self._unfrozen_written = False
        self.held = {}                                       # day -> why its tasks were held in the last plan
        self._round_base = {}                                # day -> base metrics that arrived in this round
        self.early_quarantined = []                          # (day, reasons) quarantined right after their base
        self.already_done = 0
        self._pool = None
        self.appended = 0                                    # rows appended by this process
        self._round_total = 0
        self._round_first = 0
        self.round_no = 0
        self.report_path = None
        self.results = []

    # ---- setup
    def log(self, text):
        self.out(text)

    def build(self):
        from hindcast import replay, search
        self.freeze_sha_of = replay.freeze_sha_of(self.study.source or 'ns:', self.settings, self.frozen_dir)
        self.runner = search.runner_for(self.sd, self.study, self.settings, self.settings_path,
                                        freeze_sha_of=self.freeze_sha_of, log=self.log, on_stale=self.on_stale,
                                        min_disk_mb=self.min_disk_mb, max_failures=self.max_failures, status_every=1)
        self.ctx = replay.ReplayContext.from_study(self.sd, self.study, self.settings, pred=self.pred,
                                                   key_of=self.runner.key_of, settings_path=self.settings_path,
                                                   frozen_dir=self.frozen_dir, window=self.window)
        self.guards = st.Guards.from_context(self.ctx, self.study, self.settings)
        self.runner.guards = self.guards
        return self

    # ---- the pool
    def pool(self):
        if self.workers <= 1 and self.pool_factory is None:
            return None                                      # pool.run_tasks runs serially in this process
        if self._pool is None:
            if self.pool_factory is not None:
                self._pool = self.pool_factory(self)
            else:
                self._pool = pl.WorkerPool(workers=self.workers, frozen_dir=self.ctx.frozen_dir, init=_worker_init,
                                           init_args=(self.ctx.frozen_dir,), log=self.log)
            self._pool.start()
        return self._pool

    def terminate_pool(self):
        if self._pool is not None:
            try:
                self._pool.terminate()
            finally:
                self._pool = None

    def close_pool(self):
        if self._pool is not None:
            try:
                self._pool.close()
            finally:
                self._pool = None

    def spec_of(self, task):
        if st.is_control(task):
            return self.guards.control_spec(task)
        return self.ctx.spec_for(task)

    def batch_evaluate(self, tasks):
        """The Runner's batch hook: (task, row fields) in completion order; the pool is terminated when the Runner
        stops consuming early (STOP, budget, guard, Ctrl-C), so no queued trial runs on without a recorder."""
        self._round_total = len(tasks)
        self._round_first = self.appended
        self._round_base = {}
        self.runner.heartbeat(round=self.round_no)
        finished = False
        pool = self.pool()
        run_kw = ({'wake_s': self.wake_s, 'trial_timeout_s': self.trial_timeout_s, 'on_wake': self.runner.idle}
                  if pool is not None else None)
        try:
            for task, res in pl.run_tasks(self.ctx, tasks, workers=self.workers, pool=pool, spec_of=self.spec_of,
                                          wave_filter=self.after_base_wave, run_kw=run_kw):
                if st.is_base_task(task) and res.status == 'done':
                    self._round_base[task.day] = dict(res.metrics or {})
                out = res.to_row()
                if st.is_control(task):
                    out['csv'] = res.csv
                yield task, out
            finished = True
        finally:
            if not finished:
                self.terminate_pool()

    def on_record(self, task, row, index, total):
        self.appended += 1
        n = self.appended
        whole = self._round_first + total
        secs = f'{row.seconds:.1f}' if _num(row.seconds) else '?'
        self.out(f'{n}/{whole} {row.variant} {row.day} {row.status} {secs}s {metric_summary(row)}'.rstrip())
        if self.tier1_every and n % self.tier1_every == 0:
            self.write_tier1()

    # ---- between rounds
    def mirror_phases(self, p):
        for ph in p.phases:
            want = 'skipped' if ph.skipped else ('done' if ph.complete else None)
            if want and self.study.phases[ph.index].state != want:
                self.sd.update_phase_state(ph.index, want)
                self.study.phases[ph.index].state = want

    def after_base_wave(self, tasks):
        """pool.run_tasks' wave filter: the base rows of this round are recorded; the base-agreement rule of the
        quarantine (days.quarantine_reasons with the base metrics only) is applied to their days right away, and the
        variants of a day it quarantines are dropped from the round (the controls still run). The other rules
        (CGM gap, profile_history, loop counts) stay per round (quarantine())."""
        from hindcast import days as dy
        if not self._round_base:
            return tasks
        thr = dy.quarantine_thresholds(self.study)
        hit = {}
        for day, m in self._round_base.items():
            why = dy.quarantine_reasons(day, base_metrics=m, thresholds=thr)
            if why:
                hit[day] = why
        if not hit:
            return tasks
        before = json.dumps(self.study.to_dict(), sort_keys=True)
        changed = dy.apply_quarantine(self.study, hit)
        if json.dumps(self.study.to_dict(), sort_keys=True) != before:
            self.sd.write_study(self.study)
        for day, why in changed:
            self.early_quarantined.append((day, why))
            self.log(f'{self.sd.name}: {day} quarantined after its base replay (listed, excluded from scoring; its '
                     f'variants are not replayed): {"; ".join(why)}')
        gone = {d.day for d in self.study.days if d.role == dy.QUARANTINE_ROLE}
        return [t for t in tasks if st.is_control(t) or t.day not in gone]

    def blocked_days(self):
        """{day: freeze.blocked_reason} for the study's search and hold-out days that cannot be replayed now (not
        frozen, frozen from a partial cache, stale): checked at the start of every round, kept in guards.json
        `not_frozen` so `hc study status` lists them."""
        from hindcast import freeze
        out = {}
        for d in self.study.days:
            if d.role not in ('search', 'holdout'):
                continue
            try:
                why = freeze.blocked_reason(d.day, self.study.source or 'ns:', self.settings, base_dir=self.frozen_dir,
                                            settings_path=self.settings_path,
                                            require_final=getattr(self.ctx, 'require_final', True))
            except (ValueError, OSError) as e:
                why = f'{d.day}: frozen state unknown ({e})'
            if why:
                out[d.day] = why
        if out != self.unfrozen or not self._unfrozen_written:
            self._unfrozen_written = True
            try:
                self.sd.update_guards(not_frozen=out or None)
            except (OSError, ValueError) as e:               # pragma: no cover
                self.log(f'guards.json not written: {e}')
            for day in sorted(set(out) - set(self.unfrozen)):
                self.log(f'{self.sd.name}: {day} cannot be replayed now ({out[day]}); its tasks are set aside until '
                         f'the freeze exists')
        self.unfrozen = out
        return out

    def no_progress_reason(self, phase_kind, causes):
        """The guard stop reason when a round has nothing new to do; it names the cause (lint-checked: it may reach
        the public campaign issue)."""
        from hindcast import lint
        head = f'guard: the search makes no progress in phase {phase_kind}'
        if causes:
            parts = [str(c) for c in causes]
            text = head + ': ' + '; '.join(parts)
        else:
            text = head + ': the same tasks were handed out again and came back blocked'
        if lint.lint_findings(text, 'issue'):
            text = head + f' ({len(causes)} day(s) held; see `hc study status`)'
        return st._clip(text)

    def quarantine(self):
        """days.quarantine_from_trials + apply_quarantine; study.json rewritten when a role or reason changed.
        Returns [(day, reasons)] of the days whose role changed."""
        from hindcast import days as dy, freeze
        host = dy.host_of(self.study.source or 'ns:', self.settings)
        metas = {d.day: freeze.read_meta(d.day, host, self.frozen_dir) for d in self.study.days}
        reasons = dy.quarantine_from_trials(self.study, self.sd.trials(), metas, self.settings)
        before = json.dumps(self.study.to_dict(), sort_keys=True)
        changed = dy.apply_quarantine(self.study, reasons)
        if json.dumps(self.study.to_dict(), sort_keys=True) != before:
            self.sd.write_study(self.study)
        for day, why in changed:
            self.log(f'{self.sd.name}: {day} quarantined (listed, excluded from scoring): {"; ".join(why)}')
        return changed

    def write_round(self, phase, started):
        from hindcast import round as rd
        try:
            doc, status = rd.produce(self.sd, round_no=self.round_no, phase=phase, settings=self.settings,
                                     started=started)
        except Exception as e:                               # the verdict step must not lose the rows
            self.log(f'round.json not written: {type(e).__name__}: {e}')
            return None
        self.write_round_comment(doc, status)
        return doc

    def write_round_comment(self, doc=None, status=None):
        """round_comment.md: the body gh.post_round would post (gh.round_body + the study's linter); not posted."""
        try:
            from hindcast import gh
            status = status or st.build_status(self.sd)
            doc = doc if doc is not None else self.sd.read_round()
            round_no = doc.round if doc is not None else (status.round or self.round_no)
            linter = gh.study_linter(self.study)
            body = gh.round_body(self.study, status, doc, round_no, status.artifact_url, linter,
                                 controls=gh.round_controls(self.sd, round_no))
            problem = linter(body) if linter is not None else None
            path = os.path.join(self.sd.path, ROUND_COMMENT)
            if problem:
                self.log(f'{ROUND_COMMENT} not written: the round comment was refused by the lint ({problem})')
                return None
            tmp = f'{path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(body.rstrip('\n') + '\n')
            os.replace(tmp, path)
            return path
        except Exception as e:                               # pragma: no cover - a convenience file
            self.log(f'{ROUND_COMMENT} not written: {type(e).__name__}: {e}')
            return None

    def write_tier1(self):
        """report/tier1.json from export.build_tier1 (study, latest rows, status, round, frozen metas)."""
        try:
            from hindcast import days as dy, export, freeze
            rows = list(st.StudyDir.latest_by_key(self.sd.trials()).values())
            rows.sort(key=lambda r: r.trial if r.trial is not None else 0)
            host = dy.host_of(self.study.source or 'ns:', self.settings)
            metas = {}
            for d in self.study.days:
                m = freeze.read_meta(d.day, host, self.frozen_dir)
                if m is not None:
                    metas[d.day] = m
            rnd = self.sd.read_round()
            doc = export.build_tier1(self.study, rows, st.build_status(self.sd), [rnd] if rnd else [], metas)
            path = os.path.join(self.sd.report_dir, TIER1_JSON)
            export.write_json(path, doc)
            return path
        except Exception as e:                               # pragma: no cover - the rows are the truth
            self.log(f'tier 1 not written: {type(e).__name__}: {e}')
            return None

    def grow_due(self, now=None):
        now = now or time.time()
        path = os.path.join(self.sd.path, GROW_JSON)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                last = float(json.load(fh).get('last_s', 0))
        except (OSError, ValueError, AttributeError):
            last = 0.0
        return now - last >= GROW_EVERY_S

    def grow_step(self):
        """freeze --new over the study's days, offline, at most hourly (grow.json); returns the outcomes."""
        if not self.grow or not self.grow_due():
            return None
        from hindcast import freeze
        results = freeze.freeze_days([d.day for d in self.study.days], url=self.study.source or 'ns:',
                                     settings=self.settings, new=True, offline=True, log_msg=lambda *_: None,
                                     base_dir=self.frozen_dir, settings_path=self.settings_path)
        contracts.write_json_atomic(os.path.join(self.sd.path, GROW_JSON),
                                    {'last': now_iso(), 'last_s': time.time(),
                                     'outcomes': [[d, o, str(x)[:120]] for d, o, x in results]})
        frozen = [d for d, o, _ in results if o == 'frozen']
        if frozen:
            self.log(f'{self.sd.name}: freeze --new froze {", ".join(frozen)}')
        return results

    def _close(self, exit_code, reason, stop_file=False):
        """The one release of the run's lock: STATE.json exit_code / stop_reason (with the control verdicts), the
        round, and queue_len = the tasks of the built phases that still have no row (so a stopped study keeps its
        planned count in `hc study status`); then status.json (Runner.release). Runner.close_session removes the
        STOP file first when `stop_file` (the STOP file stopped the run), runs publish() - tier 1 and the report -
        under the lock, and releases last."""
        from hindcast import search
        notes = self.control_notes()
        text = reason + ('; ' + '; '.join(notes) if notes else '')
        phase = None
        try:
            trials = self.sd.trials()
            left = len(search.untried_keys(self.study, trials, settings=self.settings, key_of=self.runner.key_of))
            p = search.plan(self.study, trials, settings=self.settings, key_of=self.runner.key_of)
            phase = p.current.kind if p.current is not None else 'done'
        except Exception:                                    # pragma: no cover - a count, not a guard
            left = 0
        if self.runner.state is not None:
            self.runner.state.round = self.round_no or self.runner.state.round
            self.runner.state.queue_len = left
            if phase:
                self.runner.state.phase = phase
        self.runner.close_session(exit_code, st._clip(text), stop_file=stop_file, before_release=self.publish)

    def publish(self):
        """Tier 1 and the report page, called by Runner.close_session while the lock is still held (build_status
        already reports the final exit code and reason there)."""
        self.write_tier1()
        if self.render:
            try:
                from hindcast import report
                res = report.render_report(self.sd, settings=self.settings, settings_path=self.settings_path,
                                           frozen_dir=self.frozen_dir, log=self.log)
                self.report_path = res.path
            except Exception as e:                           # the page is regenerated by `hc report` anyway
                self.log(f'report not rendered ({type(e).__name__}: {e}); run `hc report {self.sd.name}`')

    def control_notes(self):
        """'sentinel equals base on <day> (n checks)' / 'null ...' / '... differs from base ...' from the rows."""
        latest = st.StudyDir.latest_by_key(self.sd.trials()).values()
        seen = {}
        for r in latest:
            if r.control not in st.CONTROL_KINDS:
                continue
            if 'equals_base' in r.flags:
                verdict = 'equals base'
            elif 'differs_from_base' in r.flags:
                verdict = 'differs from base'
            elif r.status != 'done':
                verdict = r.status
            else:
                verdict = 'unchecked'
            key = (r.control, verdict)
            seen.setdefault(key, {}).setdefault(r.day, 0)
            seen[key][r.day] += 1
        out = []
        for (kind, verdict), by_day in sorted(seen.items()):
            out.append(f'{kind} {verdict} on ' + ', '.join(f'{d} ({n} check{"s" if n != 1 else ""})'
                                                          for d, n in sorted(by_day.items())))
        return out

    # ---- the run
    def run(self):
        """The whole run under one lock (Runner.open_session ... close_session): rounds until the phases are
        settled, a stop, a guard, or no progress. Returns the exit code."""
        from hindcast import search
        self.build()
        exit_code, reason = EXIT_OK, 'finished: every built phase is settled'
        stop_file = False                                    # True: the STOP file stopped the run (close removes it)
        deadline = time.time() + self.budget_s if self.budget_s is not None else None
        prev = self.sd.read_round()
        self.round_no = prev.round if prev is not None else 0
        previous_keys = None
        progressed = True
        t_start = time.time()
        try:
            self.runner.open_session('planning round 1')
        except st.LockHeld as e:
            self.log(str(e))
            return self.finish(EXIT_REFUSED, str(e), t_start, refused=True)
        self.runner.deadline = deadline
        self.already_done = len(self.sd.done_keys())
        self.log(f'{self.sd.name}: {self.workers} worker(s), budget '
                 + (f'{self.budget_s / 3600:.2f} h' if self.budget_s is not None else 'none')
                 + (f', batch {self.batch}' if self.batch else '') + f', on-stale {self.on_stale}'
                 + f'; {self.already_done} key(s) already done')
        try:
            while True:
                self.grow_step()
                self.blocked_days()
                trials = self.sd.trials()
                p = search.plan(self.study, trials, settings=self.settings, key_of=self.runner.key_of)
                self.mirror_phases(p)
                if p.current is None:
                    break
                if self.sd.stop_requested():
                    reason, stop_file = st.STOP_REASON, True
                    break
                if deadline is not None and time.time() >= deadline:
                    reason = 'budget exhausted'
                    break
                self.round_no += 1
                started = now_iso()
                self.held = {}
                batch = search.next_batch(self.study, trials, settings=self.settings, key_of=self.runner.key_of,
                                          batch_size=self.batch, round_no=self.round_no, unfrozen=self.unfrozen,
                                          held=self.held)
                keys = [self.runner.key_of(t) for t in batch]
                for day, why in sorted(self.held.items()):
                    self.log(f'{self.sd.name}: round {self.round_no}: held: {why}')
                if not batch or (keys == previous_keys and not progressed):
                    exit_code = EXIT_GUARD
                    causes = [why for _, why in sorted(self.held.items())]
                    reason = self.no_progress_reason(p.current.kind, causes)
                    self.round_no -= 1
                    break
                previous_keys = keys
                self.runner.heartbeat(phase=f'round {self.round_no}', round=self.round_no)
                self.log(f'{self.sd.name}: round {self.round_no}, phase {p.current.kind}, {len(batch)} task(s)')
                result = self.runner.run(batch, None, phase=p.current.kind, batch_evaluate=self.batch_evaluate,
                                         on_record=self.on_record)
                self.results.append(result)
                latest = st.StudyDir.latest_by_key(self.sd.trials())
                progressed = any(latest.get(k) is not None and latest[k].status in ('done', 'failed') for k in keys)
                if result.exit_code == EXIT_REFUSED:
                    exit_code, reason = EXIT_REFUSED, result.stop_reason
                    break
                self.quarantine()
                self.write_round(p.current.kind, started)
                self.write_tier1()
                if result.exit_code != EXIT_OK or result.stop_reason != st.FINISHED:
                    exit_code, reason, stop_file = result.exit_code, result.stop_reason, result.stop_file
                    break
                self.runner.heartbeat(phase=f'planning round {self.round_no + 1}', round=self.round_no,
                                      queue_len=0)
                if self.between_rounds is not None:
                    self.between_rounds(self)
            self.mirror_phases(search.plan(self.study, self.sd.trials(), settings=self.settings,
                                           key_of=self.runner.key_of))
        except KeyboardInterrupt:                            # between rounds (inside a round the Runner catches it)
            exit_code, reason = EXIT_OK, 'interrupted (Ctrl-C); re-run resumes'
        except st.GuardStop as e:                            # the Runner's idle() between rounds (disk)
            exit_code, reason = EXIT_GUARD, st._clip(f'guard: {e.reason}')
        except Exception as e:                               # the orchestration itself broke: exit 2, lock released
            self.log(f'{self.sd.name}: the orchestrator raised {type(e).__name__}: {e}')
            exit_code, reason = EXIT_GUARD, st.exception_reason(e)
        return self.finish(exit_code, reason, t_start, stop_file=stop_file)

    def finish(self, exit_code, reason, t_start, refused=False, stop_file=False):
        """Pool down, then (unless the lock was never ours) the one close: STOP file (when it stopped the run),
        tier 1 and report under the lock, the release (STATE.json, status.json - which then points at the page -,
        LOCK removed) last; round_comment.md and the exit line after it."""
        self.terminate_pool()
        if not refused:
            self._close(exit_code, reason, stop_file)
            if self.round_no:
                self.write_round_comment()                   # with the run's final state
        wall = time.time() - t_start
        self.log('')
        self.log(f'{self.sd.name}: exit {exit_code} ({reason}); {self.appended} row(s) appended, {self.already_done} '
                 f'key(s) already done when the run started; {wall:.1f} s')
        if not refused:
            for n in self.control_notes():
                self.log(f'  controls: {n}')
            self.log(f'  report: {self.report_path or os.path.join(self.sd.report_dir, TIER1_JSON)}')
        self.log('')
        self.log(HONESTY_STATEMENT)
        return exit_code


def run_study(study_dir, args):
    """cli.py's `hc study run` entry: args carries hours, until, workers, batch, on_stale, pred, grow, settings
    (and the hidden min_disk_mb / frozen_dir). Returns the exit code."""
    orch = Orchestrator(study_dir, settings_path=getattr(args, 'settings', contracts.NS_SETTINGS_PATH),
                        workers=getattr(args, 'workers', None), batch=getattr(args, 'batch', None),
                        hours=getattr(args, 'hours', None), until=getattr(args, 'until', None),
                        on_stale=getattr(args, 'on_stale', 'stop') or 'stop', pred=bool(getattr(args, 'pred', False)),
                        grow=bool(getattr(args, 'grow', False)), frozen_dir=getattr(args, 'frozen_dir', None),
                        min_disk_mb=getattr(args, 'min_disk_mb', None) or st.MIN_DISK_MB,
                        render=not getattr(args, 'no_report', False), window=getattr(args, 'window', None),
                        trial_timeout_s=getattr(args, 'trial_timeout', None) or pl.DEFAULT_TRIAL_TIMEOUT_S)
    return orch.run()


__all__ = ['TIER1_EVERY', 'GROW_EVERY_S', 'Orchestrator', 'run_study', 'budget_seconds', 'seconds_until',
           'metric_summary']
