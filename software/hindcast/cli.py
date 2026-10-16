"""
cli.py  -  `hc`, the Hindcast command (DESIGN.md section 4.6)

    cd software && ../.venv/bin/python -m hindcast <command> ...        # alias hc='.../.venv/bin/python -m hindcast'

    hc study new <name> --days 2026-09-01..09-07 [--holdout 2026-09-05] [--tag 2026-09-05=weekend,sport]
                        --param "profile autoISF_max" 1.1:1.8:0.05 | --from-grid variants/grid_autoISF.json
                        | --space-file hindcast/spaces/autoisf_tierA.json | --tier A
                        [--constraint "profile a <= profile b"] [--phase oaat:points=5] [--question explore|earlier|gentler]
                        [--base-vdf file.vdf] [--hours 6] [--workers 4] [--sentinel-every 25] [--note text]
    hc study run    <name> [--hours H] [--until HH:MM] [--workers N] [--on-stale stop|reeval]
    hc study status <name> [--json]
    hc study stop   <name>
    hc check [--refreeze] [--no-live] [--write-golden --reason TEXT]          regression gate (H-902)
    hc replay <day> ...     hc search <name>     hc round <name>                one replay, the search plan, the verdict step
    hc report <name> | hc serve <name> | hc explain <name> <iso> | hc card <name>
    hc gh ... | hc lint ... | hc freeze ... | hc warm ... | hc export (not built yet, H-504)

Dispatcher only: the study commands live in study.py (H-301) and space.py (H-303); every other command is handed
to its module when that module exists in this package and skipped with "not built yet (H-nnn)" and exit code 1
when it does not, so the command set grows without touching this file. A module takes part by exposing
`register(subparsers)` (it adds its own subparsers whose `func(args) -> int` the dispatcher calls) or a
`main(argv) -> int` (forwarded the remaining arguments). `hc study run` hands over to `hindcast.orchestrator`
(H-305, `run_study(study_dir, args) -> exit code`) when that exists; until then it runs the study's queue through
`study.dry_evaluate` (every task recorded as blocked, nothing replayed) so that lock, STATE.json, STOP and resume
are exercised.

Exit codes: 0 finished/stopped, 2 stopped by a guard, 3 configuration refused (locked key, lock held, bad study),
1 command not available. The honesty statement (DESIGN.md section 3) closes every human-readable output.
"""
import argparse
import datetime
import importlib
import os
import sys

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import space as spc                                                           # noqa: E402
from hindcast import study as st                                                            # noqa: E402
from hindcast.study import EXIT_GUARD, EXIT_OK, EXIT_REFUSED, GuardStop, StudyError          # noqa: E402

EXIT_UNAVAILABLE = 1

# command -> (candidate modules in order, backlog id, one-line help); the first module that imports is used
LAZY_COMMANDS = {
    'freeze':  (('hindcast.freeze',), 'H-101', 'freeze cached Nightscout days into frozen/<host>/<day>.loops.jsonl.gz'),
    'warm':    (('hindcast.freeze', 'hindcast.warm'), 'H-104', 'fill the Nightscout day cache (the only network step)'),
    'check':   (('hindcast.check',), 'H-902', 'regression gate: base replay vs the golden metrics'),
    'report':  (('hindcast.report',), 'H-401', 'render studies/<study>/report/'),
    'serve':   (('hindcast.report', 'hindcast.serve'), 'H-401', 'stdlib http.server on report/'),
    'explain': (('hindcast.explain', 'hindcast.status'), 'H-502', 'the drill-down of one loop as text'),
    'card':    (('hindcast.cards', 'hindcast.card', 'hindcast.export'), 'H-205', 'linted candidate cards -> cards/<rank>.md'),
    'round':   (('hindcast.round',), 'H-502', 'write round.json and status.json from trials.jsonl'),
    'search':  (('hindcast.search',), 'H-304', 'the search plan of a study: phases, pending tasks, frozen parameters'),
    'replay':  (('hindcast.replay',), 'H-102', 'replay one variant on one frozen day and print its metrics'),
    'export':  (('hindcast.export',), 'H-504', 'db rows / tier-1 JSON for the skill'),
    'gh':      (('hindcast.gh',), 'H-601', 'GitHub labels, backlog issues, campaign issue and round comments'),
    'lint':    (('hindcast.lint',), 'H-205', 'the may / may-not vocabulary and privacy lint'),
}


def _out(text=''):
    sys.stdout.write(str(text) + '\n')


def _err(text):
    sys.stderr.write(str(text) + '\n')


# ----------------------------------------------------------------------------- hc study new

def _add_common(p):
    p.add_argument('--studies-dir', default=st.STUDIES_DIR, help='where studies/<name>/ live (default software/studies)')
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH,
                   help='ns_settings.json (the phone values and the live safety lock list)')


def build_space(args, settings):
    """--param / --from-grid / --space-file / --tier -> (space dict, constraints, phases_from_grid)."""
    space, constraints, phases = {}, [], []
    if getattr(args, 'tier', None):
        path = spc.DEFAULT_SPACE_FILES.get(args.tier.upper())
        if not path:
            raise StudyError(f'--tier {args.tier}: known tiers are {", ".join(spc.DEFAULT_SPACE_FILES)}')
        s, c, _ = spc.load_space_file(path, settings)
        space.update(s)
        constraints += c
    if getattr(args, 'space_file', None):
        s, c, _ = spc.load_space_file(args.space_file, settings)
        space.update(s)
        constraints += c
    if getattr(args, 'from_grid', None):
        s, phase, _ = spc.load_grid_file(args.from_grid)
        space.update(s)
        phases.append(phase)
    for name, spec in getattr(args, 'param', None) or []:
        contracts.split_param(name)
        space[name] = spc.parse_param_spec(spec)
    for text in getattr(args, 'constraint', None) or []:
        constraints.append(st.parse_constraint(text))
    if not space:
        raise StudyError('give the searched parameters: --param "<array> <item>" <spec>, --from-grid, --space-file or --tier')
    return space, constraints, phases


def cmd_study_new(args):
    settings = spc.load_settings(args.settings)
    try:
        space, constraints, phases = build_space(args, settings)
    except (contracts.ContractError, OSError, ValueError) as e:
        raise StudyError(str(e)) from None
    phases += [st.parse_phase(p) for p in args.phase or []]
    days = st.parse_days(args.days)
    holdout = st.parse_days(args.holdout) if args.holdout else []
    tags = {}
    for spec in args.tag or []:
        day, t = st.parse_tag(spec)
        tags.setdefault(day, []).extend(t)
    base_lines = st.read_vdf_lines(args.base_vdf) if args.base_vdf else []
    extra = {}
    if args.sentinel_every is not None:
        extra['sentinel_every'] = args.sentinel_every
    sd = st.new_study(args.name, days, space, constraints, phases or None, holdout, tags, args.question, base_lines,
                      settings=settings, settings_path=args.settings, studies_dir=args.studies_dir, source=args.source,
                      budget={'hours': args.hours, 'workers': args.workers}, notes=args.note or [], **extra)
    study = st.Study.load(sd.study_json)
    _out(f'created {sd.study_json}')
    _out(f'  {len(study.days)} day(s): ' + ', '.join(f'{d.day} ({d.role}{", " + ",".join(d.tags) if d.tags else ""})' for d in study.days))
    _out(f'  {len(study.space)} parameter(s): ' + ', '.join(study.space))
    for c in study.constraints:
        _out('  constraint: ' + ' '.join(c))
    _out('  phases: ' + ', '.join(p.kind + (f'({p.points} levels)' if p.points else f'({p.n})' if p.n else '') for p in study.phases))
    _out(f'  question: {study.question}   budget: {study.budget.hours} h, {study.budget.workers} worker(s)')
    _out(f'  code_sha {contracts.short(study.code_sha)}   settings_sha {contracts.short(study.settings_sha)}')
    _out('')
    _out(contracts.HONESTY_STATEMENT)
    return EXIT_OK


# ----------------------------------------------------------------------------- hc study run / status / stop

def _seconds_until(hhmm):
    """'HH:MM' local -> seconds from now until that time (today, else tomorrow)."""
    try:
        h, m = (int(x) for x in hhmm.split(':'))
    except ValueError:
        raise StudyError(f'--until {hhmm}: expected HH:MM') from None
    now = datetime.datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def budget_seconds(args, study):
    """--hours / --until -> seconds for the Runner; None = the study's budget."""
    secs = []
    if getattr(args, 'hours', None) is not None:
        secs.append(args.hours * 3600.0)
    if getattr(args, 'until', None):
        secs.append(_seconds_until(args.until))
    if not secs and study.budget.hours:
        secs.append(study.budget.hours * 3600.0)
    return min(secs) if secs else None


def cmd_study_run(args):
    sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
    settings = spc.load_settings(args.settings)
    study = sd.load_study(settings)                          # a locked key in study.json is refused here (exit 3)
    try:
        orchestrator = importlib.import_module('hindcast.orchestrator')
    except ImportError:
        orchestrator = None
    if orchestrator is not None and hasattr(orchestrator, 'run_study'):
        return int(orchestrator.run_study(sd, args))
    _out(f'{sd.name}: the orchestrator (backlog H-305) is not built yet; nothing is replayed. The study queue is run '
         f'through the dry evaluator: every (variant, day) is recorded as blocked, the lock, STATE.json, STOP and '
         f'resume rules are exercised, and a later real run re-evaluates the blocked rows.')
    tasks = st.study_tasks(study, settings)
    runner = st.Runner(sd, study, settings=settings, budget_s=budget_seconds(args, study), log=_out)
    result = runner.run(tasks, st.dry_evaluate)
    if result.exit_code == EXIT_REFUSED:
        _err('refused: ' + result.stop_reason)
    _out('')
    _out(contracts.HONESTY_STATEMENT)
    return result.exit_code


def cmd_study_status(args):
    sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
    status = st.build_status(sd)
    if args.json:
        text = status.to_compact_json()
        _out(text)
    else:
        sys.stdout.write(st.status_text(status))
    return EXIT_OK


def cmd_study_stop(args):
    sd = st.StudyDir(st.resolve_study_dir(args.name, args.studies_dir))
    sd.require()
    holder = sd.lock_holder()
    path = sd.request_stop()
    if holder:
        _out(f'STOP written ({path}); the run ({holder}) finishes its current trial, writes status.json and exits 0.')
    else:
        _out(f'STOP written ({path}), but no run is active on this study; the next `hc study run` removes it when it starts.')
    return EXIT_OK


def register_study(subparsers):
    p = subparsers.add_parser('study', help='create, run, inspect and stop a study (studies/<name>/)')
    sub = p.add_subparsers(dest='study_command', metavar='new|run|status|stop')
    sub.required = True

    n = sub.add_parser('new', help='write studies/<name>/study.json (locked keys are refused, exit 3)')
    n.add_argument('name')
    n.add_argument('--days', required=True, help='UTC days: 2026-09-01..09-07, 2026-09-06, comma lists')
    n.add_argument('--holdout', help='days reserved for validation (subset of --days)')
    n.add_argument('--tag', action='append', metavar='DAY=TAG,TAG', help='day character typed by you, e.g. 2026-09-05=weekend,sport')
    n.add_argument('--param', action='append', nargs=2, metavar=('"ARRAY ITEM"', 'SPEC'),
                   help='a searched parameter: low:high:step (float / int), bool, or a comma list of values')
    n.add_argument('--from-grid', help='import an emulator_optimize grid JSON as a grid (or oaat) phase')
    n.add_argument('--space-file', help='a spaces/*.json file (see hindcast/spaces/)')
    n.add_argument('--tier', help='the documented default space: A or B')
    n.add_argument('--constraint', action='append', help='ordering chain, e.g. "profile smb_delivery_ratio_min <= profile smb_delivery_ratio"')
    n.add_argument('--phase', action='append', help='oaat[:points=5] | grid | lhs:n=24[,seed=1] | halving:rungs=2/5/all | validate:top=8,days=holdout')
    n.add_argument('--question', choices=contracts.QUESTIONS, default='explore')
    n.add_argument('--base-vdf', help='VDF rows every variant carries (must not set a searched key)')
    n.add_argument('--hours', type=float, default=6.0)
    n.add_argument('--workers', type=int, default=4)
    n.add_argument('--sentinel-every', type=int, default=None)
    n.add_argument('--source', default='ns:')
    n.add_argument('--note', action='append')
    _add_common(n)
    n.set_defaults(func=cmd_study_new)

    r = sub.add_parser('run', help='run (or resume) the study; Ctrl-C, kill or a STOP file any time')
    r.add_argument('name')
    r.add_argument('--hours', type=float, default=None)
    r.add_argument('--until', help='stop at HH:MM local time')
    r.add_argument('--workers', type=int, default=None)
    r.add_argument('--on-stale', choices=('stop', 'reeval'), default='stop')
    r.add_argument('--pred', action='store_true', help='keep predictions for every trial (orchestrator option)')
    _add_common(r)
    r.set_defaults(func=cmd_study_run)

    s = sub.add_parser('status', help='<= 2 KB summary: phase, trials, floor, review order, heartbeat')
    s.add_argument('name')
    s.add_argument('--json', action='store_true')
    _add_common(s)
    s.set_defaults(func=cmd_study_status)

    q = sub.add_parser('stop', help='write the STOP file: the run finishes its current trial and exits 0')
    q.add_argument('name')
    _add_common(q)
    q.set_defaults(func=cmd_study_stop)


# ----------------------------------------------------------------------------- the other commands, lazily

def _import_first(modules):
    for name in modules:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                _err(f'{name}: {e}')            # the module exists but one of its own imports is missing: say so
        except Exception as e:                  # a sibling's import-time bug must not take the dispatcher down
            _err(f'{name}: import failed: {type(e).__name__}: {e}')
    return None


def _forward(module, command):
    def run(args):
        rest = list(args.rest or [])
        return int(module.main(rest) or 0)
    return run


def _placeholder(command, issue):
    def run(args):
        _err(f'hc {command}: not built yet (backlog {issue})')
        return EXIT_UNAVAILABLE
    return run


def register_lazy(subparsers):
    """Add every command of LAZY_COMMANDS: through the module's register(), its main(), or as a placeholder."""
    for command, (modules, issue, help_) in LAZY_COMMANDS.items():
        if command in subparsers.choices:
            continue
        module = _import_first(modules)
        if module is not None and hasattr(module, 'register'):
            try:
                module.register(subparsers)
            except Exception as e:                           # a sibling's bug must not take the dispatcher down
                _err(f'{module.__name__}.register failed: {e}')
            if command in subparsers.choices:
                continue
        if module is not None and hasattr(module, 'main') and command == module.__name__.rsplit('.', 1)[-1]:
            p = subparsers.add_parser(command, help=help_, add_help=False)
            p.add_argument('rest', nargs=argparse.REMAINDER)
            p.set_defaults(func=_forward(module, command), forward=True)       # main() gets every remaining argument
            continue
        p = subparsers.add_parser(command, help=f'{help_} (not built yet, {issue})')
        p.add_argument('rest', nargs=argparse.REMAINDER)
        p.set_defaults(func=_placeholder(command, issue))


def build_parser():
    parser = argparse.ArgumentParser(prog='hc', description=__doc__.split('\n\n')[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='store_true', help='print the emulator version stamps and exit')
    sub = parser.add_subparsers(dest='command', metavar='<command>')
    register_study(sub)
    register_lazy(sub)
    return parser


def main(argv=None):
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if getattr(args, 'forward', False):                      # a sibling's main(argv) parses its own options (also --help)
        args.rest = list(unknown) + list(args.rest or [])
    elif unknown:
        parser.error('unrecognized arguments: ' + ' '.join(unknown))
    if args.version:
        try:
            for k, v in contracts.core_versions().items():
                _out(f'{v}  {k}')
        except Exception as e:
            _err(f'version stamps unavailable: {e}')
        _out(f'hindcast contract {contracts.CONTRACT_VERSION}, code_sha {contracts.short(contracts.code_sha())}')
        return EXIT_OK
    if not getattr(args, 'command', None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args) or 0)
    except StudyError as e:
        _err(f'refused (exit {EXIT_REFUSED}): {e}')
        return EXIT_REFUSED
    except GuardStop as e:
        _err(f'stopped by a guard (exit {EXIT_GUARD}): {e.reason}')
        return EXIT_GUARD
    except contracts.ContractError as e:
        _err(f'refused (exit {EXIT_REFUSED}): {e}')
        return EXIT_REFUSED
    except KeyboardInterrupt:
        _err('interrupted')
        return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
