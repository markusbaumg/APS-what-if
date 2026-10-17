"""
pool.py  -  the worker pool of a Hindcast run: one emulator_core per process, spawn start method (backlog H-103)

    from hindcast import pool, replay
    with pool.WorkerPool(workers=4) as wp:                     # spawn; each worker imports emulator_core once
        for res in wp.run(specs):                              # TrialResult objects, streamed as they finish
            ...
    for task, res in pool.run_tasks(ctx, tasks, workers=4):   # study.Task list: base tasks first, then the rest
        runner_row = res.to_row()

Why a pool and why spawn. emulator_core is module-global state (CLAUDE.md): sequential `parameters_known()` calls in
one process are clean, concurrent ones are not, so parallelism means one interpreter per worker. `spawn` (not fork)
because the parent may have imported emulator_core, Tk or matplotlib already; a spawned child starts from a fresh
interpreter, runs `worker_init()` once - PYTHONUTF8=1, `import emulator_core`, `set_tty(0, 0, 'batch')`, the frozen
source installed on the real ns_source module - and then replays task after task with `replay.run_trial()`. That
install is only the default: every trial installs its own spec's source for the core call (replay.source_installed,
also `source='live'`, which puts the real class back explicitly) and restores the worker's afterwards, and the class
used travels back as the flag `source:<mode>:<class>`. Every trial's files under runs/ are named by
`TrialSpec.run_name()`, unique per trial key, day, window, control and instance, so two workers never share one.

Rules (DESIGN.md section 4.2, backlog H-103):
    workers         capped at cpu_count() - 1 (never 0): `worker_count()`
    max_tasks_per_child   200 by default: a worker is replaced after that many trials (module-global lists of the core,
                    matplotlib caches, anything that might creep); the RSS of the worker after each trial travels
                    back in `TrialResult.rss_mb` so the orchestrator can log it
    nice            os.nice(5) in every worker (the laptop stays usable during a six-hour run)
    streaming       `run()` is a generator over `imap_unordered(chunksize=1)`: a result is yielded the moment its
                    worker finishes, so trials.jsonl grows while the pool is busy and a kill loses one trial per
                    worker at most
    failures        an exception inside a worker is caught in `worker_run()` and comes back as ONE failed TrialResult
                    with the traceback as `error`; the pool and the study continue (the Runner's three-failure guard
                    decides). KeyboardInterrupt in the parent terminates the pool.
    serial twin     `run_serial()` evaluates the same specs in this process with the same function, so a test can
                    assert that 2 workers x 4 trials give the metrics of the serial run (they do: the replay is
                    deterministic given the frozen file, the VDF and the settings).
    waking          `run(specs, wake_s=30, trial_timeout_s=600, on_wake=f)` (what the orchestrator passes) submits at
                    most `workers` specs at a time with apply_async and waits for the next result with a timeout:
                    when nothing arrived for `wake_s` seconds, `on_wake()` is called (the Runner's idle(): heartbeat,
                    STOP, budget, disk; it may raise to stop the run), so a hard-dead worker never blocks the parent
                    for ever. A trial that has not answered after `trial_timeout_s` comes back as one failed
                    TrialResult ("no answer within N s"), the pool is terminated (that kills the hung worker) and
                    started again, and the other trials in flight are submitted again. Without these arguments
                    `run()` streams through imap_unordered as before. The serial path cannot time out (the trial
                    runs in the parent).

`run_tasks(ctx, tasks, workers)` is the task-level helper for the orchestrator: it turns study.Task objects into
TrialSpecs through a replay.ReplayContext, runs the base tasks of each day first (every other trial of a day is
compared with that base CSV, kept under runs/base/<day>.csv), then the rest, and yields (task, result) pairs in
completion order after `ctx.absorb(result)`.

Measured 2026-10-17 (Apple silicon, Python 3.14, 2026-09-06 06-09Z, 180 loops, empty VDF): a spawned worker is ready
in ~1.5 s (interpreter + emulator_core import); 2 workers x 4 trials equal the serial run (tests/test_pool.py);
one worker over 100 trials of 06:00-06:30Z: max RSS 38.4 MB after trial 10, 39.0 MB after trial 100 (flat).
"""
import multiprocessing
import os
import sys
import time
import traceback

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import replay                                                                 # noqa: E402

DEFAULT_MAX_TASKS_PER_CHILD = 200
DEFAULT_WAKE_S = 30                                      # the parent wakes at least this often while waiting
DEFAULT_TRIAL_TIMEOUT_S = 600                            # a trial without an answer after this is failed
DEFAULT_NICE = 5
START_METHOD = 'spawn'

_RUN = None                                              # the per-trial function inside a worker (set by worker_init)


class PoolError(RuntimeError):
    """The pool could not be started or used as asked."""


def worker_count(requested=None, cpu=None):
    """The number of workers to use: `requested` (default 4) capped at cpu_count() - 1, at least 1."""
    cpu = cpu if cpu is not None else (os.cpu_count() or 2)
    cap = max(1, cpu - 1)
    want = 4 if requested is None else int(requested)
    return max(1, min(want, cap))


# ----------------------------------------------------------------------------- inside a worker

def default_init(frozen_dir=None):
    """What every emulator worker does once: PYTHONUTF8, import emulator_core + set_tty, frozen source installed."""
    os.environ['PYTHONUTF8'] = '1'
    replay.ensure_core()
    replay.install_frozen(frozen_dir)


def worker_init(run, init, init_args, nice):
    """Pool initializer: lower the priority, run `init(*init_args)`, remember `run` for worker_run()."""
    global _RUN
    _RUN = run
    if nice:
        try:
            os.nice(int(nice))
        except (OSError, AttributeError):             # pragma: no cover - no nice on this platform
            pass
    if init is not None:
        init(*init_args)


def worker_index():
    """1-based index of this pool worker (0 in the parent / a plain process)."""
    ident = getattr(multiprocessing.current_process(), '_identity', ()) or ()
    return int(ident[0]) if ident else 0


def _failed_result(spec, exc, tb):
    err = f'worker exception {type(exc).__name__}: {exc}\n{tb}'
    err = err[-1500:]
    get = lambda k, d=None: getattr(spec, k, d) if not isinstance(spec, dict) else spec.get(k, d)   # noqa: E731
    return replay.TrialResult(status='failed', day=str(get('day', '1970-01-01')), vhash=str(get('vhash', '') or ''),
                              variant=str(get('variant', '') or ''), params=dict(get('params', {}) or {}),
                              key=get('key'), control=get('control'), error=err, worker=worker_index(), pid=os.getpid(),
                              rss_mb=replay.rss_mb())


def worker_run(spec):
    """The function the pool maps: `run(spec, worker=<index>)`; any exception becomes one failed TrialResult."""
    run = _RUN or replay.run_trial
    try:
        try:
            return run(spec, worker=worker_index())
        except TypeError as e:
            if 'worker' not in str(e):
                raise
            return run(spec)                         # a run function without the worker keyword
    except KeyboardInterrupt:
        raise
    except BaseException as e:                       # noqa: BLE001 - a worker never dies on a trial
        return _failed_result(spec, e, traceback.format_exc())


# ----------------------------------------------------------------------------- the pool

class WorkerPool:
    """A spawn pool of emulator workers. `run` is the per-spec function (default replay.run_trial; must be picklable,
    i.e. a module-level function), `init` the per-worker initializer (default default_init(frozen_dir); None skips
    the emulator set-up, for tests of the pool itself)."""

    def __init__(self, workers=None, max_tasks_per_child=DEFAULT_MAX_TASKS_PER_CHILD, nice=DEFAULT_NICE, frozen_dir=None,
                 run=None, init=default_init, init_args=None, context=START_METHOD, log=None):
        self.workers = worker_count(workers)
        self.max_tasks_per_child = max_tasks_per_child
        self.nice = nice
        self.frozen_dir = frozen_dir
        self.run_fn = run or replay.run_trial
        self.init = init
        self.init_args = tuple(init_args) if init_args is not None else ((frozen_dir,) if init is default_init else ())
        self.context = context
        self.log = log or (lambda text: None)
        self._pool = None
        self.started_at = None
        self.results = 0
        self.restarts = 0                                    # pool restarts after a trial timeout

    # ---- lifecycle
    def start(self):
        if self._pool is not None:
            return self
        ctx = multiprocessing.get_context(self.context)
        self._pool = ctx.Pool(processes=self.workers, initializer=worker_init,
                              initargs=(self.run_fn, self.init, self.init_args, self.nice),
                              maxtasksperchild=self.max_tasks_per_child or None)
        self.started_at = time.time()
        self.log(f'pool: {self.workers} worker(s), {self.context}, max {self.max_tasks_per_child} trials per worker, nice {self.nice}')
        return self

    def close(self):
        """Finish the queued work (none, once run() is exhausted) and join the workers."""
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None

    def terminate(self):
        if self._pool is not None:
            self._pool.terminate()
            self._pool.join()
            self._pool = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.terminate()
        return False

    # ---- work
    def run(self, specs, ordered=False, wake_s=None, trial_timeout_s=None, on_wake=None):
        """Generator over the results of `specs`, in completion order (ordered=True: in submission order, still
        streamed). Each item is whatever `run(spec)` returned (a TrialResult for the default). With `wake_s`,
        `trial_timeout_s` or `on_wake` the waking scheduler is used (module docstring, completion order)."""
        self.start()
        specs = list(specs)
        if not specs:
            return
        if not ordered and (wake_s or trial_timeout_s or on_wake is not None):
            yield from self._run_waking(specs, wake_s or DEFAULT_WAKE_S, trial_timeout_s, on_wake)
            return
        mapper = self._pool.imap if ordered else self._pool.imap_unordered
        for res in mapper(worker_run, specs, chunksize=1):
            self.results += 1
            yield res

    def _run_waking(self, specs, wake_s, trial_timeout_s, on_wake):
        """At most `workers` specs in flight (so the submit time is the start time of the trial), results from the
        pool's callbacks through a queue read with a timeout; see the module docstring."""
        import queue
        results = queue.Queue()
        todo = list(enumerate(specs))
        inflight = {}                                        # index -> (spec, submitted at)
        generation = [0]

        def submit(i, spec):
            g = generation[0]
            self._pool.apply_async(worker_run, (spec,),
                                   callback=lambda r, i=i, g=g: results.put((g, i, r, None)),
                                   error_callback=lambda e, i=i, g=g: results.put((g, i, None, e)))
            inflight[i] = (spec, time.time())

        while todo or inflight:
            while todo and len(inflight) < self.workers:
                submit(*todo.pop(0))
            wait = float(wake_s)
            if trial_timeout_s:
                oldest = min(t for _, t in inflight.values())
                wait = min(wait, oldest + float(trial_timeout_s) - time.time())
            try:
                g, i, res, err = results.get(timeout=max(0.01, wait))
            except queue.Empty:
                now = time.time()
                late = [i for i, (_, t) in inflight.items()
                        if trial_timeout_s and now - t >= float(trial_timeout_s)]
                if late:
                    for i in late:
                        spec, t = inflight.pop(i)
                        self.log(f'pool: a trial gave no answer within {trial_timeout_s:g} s; the pool is restarted')
                        res = _failed_result(spec, TimeoutError(f'no answer within {trial_timeout_s:g} s (worker '
                                                                f'hung or dead); the pool was restarted'), '')
                        res.error = res.error.rstrip('\n')
                        res.seconds = round(now - t, 3)
                        self.results += 1
                        yield res
                    again = [(i, s) for i, (s, _) in sorted(inflight.items())]
                    inflight.clear()
                    self.terminate()                         # kills the hung worker with the others
                    generation[0] += 1                       # late callbacks of the old pool are ignored
                    self.start()
                    self.restarts += 1
                    todo = again + todo
                    continue
                if on_wake is not None:
                    on_wake()
                continue
            if g != generation[0] or i not in inflight:
                continue
            spec, _ = inflight.pop(i)
            if err is not None:
                res = _failed_result(spec, err, '')
            self.results += 1
            yield res

    def map(self, specs):
        """All results in submission order (a list)."""
        return list(self.run(specs, ordered=True))


# ----------------------------------------------------------------------------- serial twin and the task-level helper

def run_serial(specs, run=None):
    """The same specs, the same function, this process; a generator in submission order. Exceptions become failed
    results exactly as in a worker."""
    global _RUN
    previous = _RUN
    _RUN = run or replay.run_trial
    try:
        for spec in specs:
            yield worker_run(spec)
    finally:
        _RUN = previous


def _serial(workers):
    """True when `workers` asks for this process only (1 or less, or a machine with one usable core)."""
    return (workers is not None and int(workers) <= 1) or worker_count(workers) <= 1


def run_specs(specs, workers=1, run=None, **pool_kw):
    """Serial when workers <= 1, else a WorkerPool; a generator of results (completion order with a pool)."""
    if _serial(workers):
        yield from run_serial(specs, run)
        return
    with WorkerPool(workers=workers, run=run, **pool_kw) as wp:
        yield from wp.run(specs)


def _spec_failure(task, exc):
    """A failed TrialResult for a task whose spec could not be built (the row names the exception)."""
    params = dict(getattr(task, 'params', None) or {})
    return replay.TrialResult(status='failed', day=str(getattr(task, 'day', '1970-01-01')), vhash='',
                              variant=str(getattr(task, 'variant', '') or ''), params=params,
                              control=getattr(task, 'control', None),
                              error=f'the trial could not be prepared: {type(exc).__name__}: {exc}'[:1000])


def run_tasks(ctx, tasks, workers=1, pool=None, log=None, spec_of=None, wave_filter=None, run_kw=None, **pool_kw):
    """study.Task list through a replay.ReplayContext: base tasks first (their CSVs are what the other trials of a
    day are compared with), then the rest; yields (task, TrialResult) in completion order after ctx.absorb().

    `spec_of(task) -> TrialSpec` replaces ctx.spec_for (H-305: the orchestrator routes the sentinel / null controls
    through study.Guards.control_spec, which keeps the control's CSV for the verdict). The specs of a wave are built
    when the wave starts, so the second wave sees the base CSVs (and base failures) the first one absorbed. A task
    whose spec cannot be built (spec_of raises) comes back as one failed result of its own; the wave goes on.
    `wave_filter(tasks) -> tasks` is called when the second wave starts, after every base result of the first wave
    was consumed (the orchestrator drops the variants of a day the base agreement just quarantined); a task it drops
    is not run and not yielded. `run_kw` go to pool.run (wake_s, trial_timeout_s, on_wake)."""
    spec_of = spec_of or ctx.spec_for
    tasks = list(tasks)
    base = [t for t in tasks if ctx.is_base(t)]
    rest = [t for t in tasks if not ctx.is_base(t)]
    log = log or (lambda text: None)
    own = pool is None and not _serial(workers)
    if own:
        pool = WorkerPool(workers=workers, frozen_dir=ctx.frozen_dir, log=log, **pool_kw).start()
    try:
        for n, wave in enumerate((base, rest)):
            if n and wave_filter is not None:
                wave = list(wave_filter(wave))
            if not wave:
                continue
            specs, ready, broken = [], [], []
            for t in wave:
                try:
                    specs.append(spec_of(t))
                    ready.append(t)
                except Exception as e:                       # one bad task, not a broken wave
                    broken.append((t, _spec_failure(t, e)))
            for task, res in broken:
                log(f'{res.day} {res.variant}: failed before the replay ({res.error})')
                yield task, res
            wave = ready
            if not wave:
                continue
            if pool is not None:
                results = pool.run(specs, **(run_kw or {}))
                pairs = ((_match(specs, wave, r), r) for r in results)
            else:
                pairs = zip(wave, run_serial(specs))
            for task, res in pairs:
                ctx.absorb(res)
                log(f'{res.day} {res.variant}: {res.status} in {res.seconds} s (worker {res.worker}, rss {res.rss_mb} MB)')
                yield task, res
    finally:
        if own:
            pool.close()


def _match(specs, tasks, res):
    """The task a result belongs to (by key when the specs carry one, else by day + vhash; the first unmatched)."""
    for i, s in enumerate(specs):
        if s is None:
            continue
        if (s.key and s.key == res.key) or (not s.key and s.day == res.day and s.vhash == res.vhash):
            specs[i] = None
            return tasks[i]
    return None
