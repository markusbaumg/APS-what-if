"""
make_fixtures.py  -  build tests/fixtures/study_example/, the fixture study directory (backlog H-003)

    cd software && ../.venv/bin/python -m hindcast.tests.make_fixtures [--out DIR] [--results DIR] [--settings PATH]
    hc fixtures [...]            (the same through cli.py once the integrator wires `register()`)

The fixture study is what the page (E4) and the skill (E5) develop against: a complete `studies/<study>/` directory
of DESIGN.md section 4.5 built from the five result CSVs of the documented emulator_optimize run
(`results/example_2026-09-06/`, 2026-09-06 06:00-12:00Z, 340 loops, git-ignored) and nothing else: no replay, no
Nightscout, no network. Every number is derived by the wave-1 modules, never typed in:

    study.json       contracts.Study: the grid of the run as a `choice` space (from the <variant>.vdf rows), one search
                     day, one `grid` phase marked done, question `explore`, stamps from summary.json
    trials.jsonl     one contracts.TrialRow per variant, base first (phase `floor`, control `base`), metrics =
                     metrics.trial_metrics() of its CSV against the base CSV (caps via emulator_optimize.effective_cap,
                     profile_history aware), feasibility from metrics.feasibility_day(); `freeze_sha` is '' because
                     the run replayed the live Nightscout source, not a frozen day
    series/<day>/<vhash>.json, series/<day>/orig.json      export.series_from_csv() (tier-2 contract of H-002)
    round.json       contracts.Round from metrics.rank_variants(question='explore') + leaderboard_rows(): verdict and
                     `why` per variant, floors, review order, the dead-parameter record, the ranking-rule text as notes
    STATE.json       the finished run's state (exit 0, `finished`), so status.json reads as a completed study
    status.json      study.build_status() over the files above (<= 2 KB)
    MANIFEST.json    sources, sizes and sha1 of every file written, the code/settings stamps, the table numbers

The trials reproduce the documented table (NIGHTSCOUT_SOURCE.md, summary.txt): base +1.37 U, dura_ISF_weight 0.5
-0.4 U vs base in the rising phase, 9 loops with a smaller SMB, factor on the cap 9.7 % -> 9.4 %, 1.5 reached in
3.8 %; test_fixture_study.py asserts them from trials.jsonl alone, so the committed fixture carries them to a clone
without the CSVs. Every human-readable string written here passes hindcast.lint (kind 'line') before it is written.
"""
import argparse
import datetime
import email.utils
import hashlib
import json
import os
import shutil
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(os.path.dirname(TESTS_DIR))
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts, export, lint, study as st                                     # noqa: E402
from hindcast import metrics as M                                                           # noqa: E402
from hindcast import space as spc                                                           # noqa: E402
from hindcast.contracts import (Candidate, ContractError, DeadParameter, Phase, Round, State, Study, StudyDay,  # noqa: E402
                                TrialRow)
from hindcast.tests import conftest                                                         # noqa: E402

OUT_DIR = conftest.STUDY_EXAMPLE_DIR
STUDY_NAME = 'study_example'
RESULTS_DIR = conftest.RESULTS_EXAMPLE_DIR
BASE_VARIANT = M.BASE_VARIANT
EXIT_OK, EXIT_REFUSED = 0, 3


class FixtureError(RuntimeError):
    """The fixture cannot be built from what is on disk; the message names the missing or inconsistent file."""


# ----------------------------------------------------------------------------- the example run on disk

def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(text):
    return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)


def _shift(iso, seconds):
    return _iso(_parse_iso(iso) + datetime.timedelta(seconds=seconds))


def read_summary(results_dir):
    """summary.json of the optimizer run as a dict, or {} when absent (the CSVs alone then name the run)."""
    path = os.path.join(results_dir, 'summary.json')
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def find_variants(results_dir):
    """{variant: csv path} of every <base>.<variant>.csv in the results dir, base first, the rest sorted."""
    out = {}
    for name in sorted(os.listdir(results_dir)):
        if not name.endswith('.csv') or name.endswith('.orig.csv') or name == 'summary.csv':
            continue
        stem = name[:-4]
        if '.' not in stem:
            continue
        base, variant = stem.split('.', 1)
        out.setdefault(base, {})[variant] = os.path.join(results_dir, name)
    if not out:
        raise FixtureError(f'{results_dir}: no result CSVs (<base>.<variant>.csv)')
    if len(out) > 1:
        raise FixtureError(f'{results_dir}: result CSVs of several runs: {sorted(out)}')
    (base, variants), = out.items()
    if BASE_VARIANT not in variants:
        raise FixtureError(f'{results_dir}: no {base}.{BASE_VARIANT}.csv')
    ordered = {BASE_VARIANT: variants[BASE_VARIANT]}
    ordered.update((v, p) for v, p in sorted(variants.items()) if v != BASE_VARIANT)
    return ordered


def variant_params(results_dir, variant):
    """The parameters of a variant from its <variant>.vdf rows ({} for base); the file name is the fallback."""
    vdf = os.path.join(results_dir, variant + '.vdf')
    if os.path.isfile(vdf):
        with open(vdf, encoding='utf-8') as fh:
            return spc.parse_vdf_text(fh.read())
    if variant == BASE_VARIANT:
        return {}
    params = {}
    for part in variant.split('__'):
        item, value = part.rsplit('_', 1)
        import emulator_optimize as _opt
        params[f'profile {item}'] = _opt.parse_literal(value)
    return params


def grid_of(params_by_variant):
    """{name: sorted distinct values} over the variants' parameters, names in order of first appearance."""
    grid = {}
    for params in params_by_variant.values():
        for name, value in params.items():
            grid.setdefault(name, [])
            if value not in grid[name]:
                grid[name].append(value)
    return {name: sorted(values) for name, values in grid.items()}


def created_stamp(summary, fallback_iso):
    """summary.json's `created` (RFC 2822, the optimizer's format) as UTC ISO, else the fallback."""
    text = summary.get('created')
    if text:
        try:
            return _iso(email.utils.parsedate_to_datetime(text))
        except (TypeError, ValueError):
            pass
    return fallback_iso


def host_of(summary, settings):
    url = summary.get('url') or (settings.get('nightscout') or {}).get('url') or ''
    return url.split('://', 1)[-1].split('/', 1)[0]


def _lint(text):
    """Refuse (LintError) any human-readable string that breaks the vocabulary; return it unchanged."""
    lint.lint_text(text, 'line')
    return text


# ----------------------------------------------------------------------------- metrics, ranking, dead parameters

def example_rows(results_dir=RESULTS_DIR, settings=None):
    """[(variant, params, csv path, metrics)] for the run's variants, base first; metrics against the base CSV with
    the caps emulator_optimize.effective_cap derives (grid value, else ns_settings.json rewound by profile_history)."""
    settings = settings if settings is not None else spc.load_settings()
    csvs = find_variants(results_dir)
    base_series = M.LoopSeries.from_csv(csvs[BASE_VARIANT])
    out = []
    for variant, path in csvs.items():
        params = variant_params(results_dir, variant)
        assignment = spc.assignment(params)
        m = M.trial_metrics(path, None if variant == BASE_VARIANT else base_series,
                            M.effective_cap(assignment, [], settings, 'autoISF_max'),
                            M.effective_cap(assignment, [], settings, 'autoISF_min'))
        out.append((variant, params, path, m))
    return out


def dead_parameters(ranking, grid, phone_point, day):
    """DESIGN.md 5.3 rule 2: a parameter whose every tried value (other than the phone's) ties with base on the day is
    'not testable on these days'. floor_ratio = the largest |effect| / floor among those points."""
    out = []
    for name, values in grid.items():
        phone = phone_point.get(name)
        varied = [v for v in ranking.variants.values() if name in v.params and v.params[name] != phone]
        if not varied or not all(v.tie_with_base for v in varied):
            continue
        tried = sorted({v.params[name] for v in varied})
        why = (f'every tried value ({", ".join(str(t) for t in tried)}) is indistinguishable from base within the '
               f'noise floor on {day}: not testable on these days')
        out.append(DeadParameter(key=name, why=_lint(why), floor_ratio=round(max(v.review_score for v in varied), 3)))
    return out


def _effect(m, keys):
    return {k: round(float(m[k]), 3) for k in keys if isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool)}


def build_round(rows, metrics_by_variant, vhash_by_variant, grid, phone_point, day, started, finished):
    """contracts.Round of the one grid round: base first with verdict `base`, then the leaderboard rows."""
    ranking = M.rank_variants(rows, 'explore')
    base_m = metrics_by_variant[BASE_VARIANT]
    floors = ranking.floors[day]
    variants = [Candidate(vhash=contracts.BASE_VHASH, variant=BASE_VARIANT, verdict='base', params={}, days=1, feasible=True,
                          why=_lint("the phone's own settings replayed; its distance to the phone's decisions is the floor"),
                          effect=_effect(base_m, ('delta_U', 'rise_vs_phone_U', 'rest_vs_phone_U', 'changed_loops')),
                          flags=list(base_m.get('flags', [])))]
    for lb in M.leaderboard_rows(ranking):
        m = metrics_by_variant[lb['variant']]
        ties = [contracts.BASE_VHASH] if lb['tie_with_base'] else []
        for other in ranking.variants.values():
            if other.variant == lb['variant']:
                continue
            tc = M.tie_components(m, metrics_by_variant[other.variant], floors)
            if tc and all(tc.values()):
                ties.append(vhash_by_variant[other.variant])
        variants.append(Candidate(vhash=vhash_by_variant[lb['variant']], variant=lb['variant'], verdict=lb['verdict'],
                                  why=_lint(lb['why']), params=dict(lb['params']), rank=lb['rank'], days=1,
                                  feasible=lb['feasible'],
                                  effect=_effect(m, ('rise_vs_base_U', 'rest_vs_base_U', 'delta_vs_base_U', 'changed_vs_base')),
                                  flags=list(lb['flags']), tie_with=ties))
    notes = [_lint(line) for line in ranking.rule_text.split('\n')]
    rnd = Round(round=1, phase='grid', started=started, finished=finished, trials=len(rows), variants=variants,
                dead_parameters=dead_parameters(ranking, grid, phone_point, day),
                floors={day: {k: float(v) for k, v in floors.items() if v is not None}},
                front=[vhash_by_variant[name] for name in ranking.order], notes=notes)
    return rnd.validate(), ranking


# ----------------------------------------------------------------------------- the directory

def _sha1_file(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        h.update(fh.read())
    return h.hexdigest()


def build_fixture_study(out_dir=OUT_DIR, results_dir=RESULTS_DIR, settings_path=contracts.NS_SETTINGS_PATH,
                        settings=None, log=print):
    """Build the fixture study directory at `out_dir` (replaced when it exists) from the result CSVs in `results_dir`.
    Returns the manifest dict (also written as MANIFEST.json). Raises FixtureError when the run is incomplete,
    ContractError / LintError when a document or a string breaks its contract."""
    settings = settings if settings is not None else spc.load_settings(settings_path)
    summary = read_summary(results_dir)
    rows = example_rows(results_dir, settings)
    params_by_variant = {v: p for v, p, _, _ in rows}
    metrics_by_variant = {v: m for v, _, _, m in rows}
    grid = grid_of(params_by_variant)
    names = list(grid)
    for variant, params in params_by_variant.items():                  # the optimizer's label and ours must agree
        label = spc.variant_name(params, names)
        if label != variant:
            raise FixtureError(f'{variant}: the VDF rows give the variant name {label!r}')
    vhash_by_variant = {v: contracts.vhash(p) for v, p in params_by_variant.items()}

    # the day and the window
    base_csv = next(path for v, _, path, _ in rows if v == BASE_VARIANT)
    _, base_rows = export.read_result_csv(base_csv)
    if not base_rows:
        raise FixtureError(f'{base_csv}: no loops')
    t_abs = export.loop_times(base_rows)
    day = export.day_of(t_abs[0])
    window = (summary.get('start') or export.iso_of(t_abs[0]), summary.get('end') or export.iso_of(t_abs[-1]))
    created = created_stamp(summary, _shift(window[1], 3600))
    seconds_of = {r['variant']: (r.get('metrics') or {}).get('seconds') for r in summary.get('results', [])}
    loops = len(base_rows)
    start_ms = int(_parse_iso(window[0]).timestamp() * 1000)
    phone_point = {n: spc.phone_value(n, settings, start_ms) for n in names}

    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    sd = st.StudyDir(out_dir)
    manifest = {'generated': contracts.now_iso(), 'results_dir': os.path.relpath(results_dir, SOFTWARE_DIR),
                'sources': {v: os.path.relpath(p, SOFTWARE_DIR) for v, _, p, _ in rows}, 'day': day,
                'window': list(window), 'loops': loops, 'code_sha': contracts.code_sha(),
                'settings_sha': contracts.settings_sha(settings=settings) if settings else '',
                'base_vhash': contracts.BASE_VHASH, 'vhash': vhash_by_variant, 'files': {}, 'table': {}}

    # study.json
    space = spc.space_from_grid(grid)
    for name, p in space.items():
        p.phone = phone_point.get(name)
    notes = [
        f'Fixture study built by hindcast/tests/make_fixtures.py from {manifest["results_dir"]} (the documented '
        f'emulator_optimize run): the result CSVs are read, nothing is replayed, no network.',
        'freeze_sha is empty: the example run replayed the live Nightscout source through emulator_optimize, not a '
        'frozen day.',
        f'The window is {window[0]}..{window[1]}, not a whole day; the floors are those of this window.',
    ]
    study = Study.new(STUDY_NAME, [StudyDay(day=day)], space, source=summary.get('url') or 'ns:',
                      host=host_of(summary, settings), question='explore',
                      phases=[Phase(kind='grid', grid=grid, state='done')],
                      budget=contracts.Budget(hours=1.0, workers=1), settings_path=settings_path,
                      notes=[_lint(n) for n in notes])
    study.created = created
    if not settings:
        study.settings_sha = ''
    study.validate(settings or None)
    sd.write_study(study)

    # series/<day>/*.json and trials.jsonl
    orig_doc = None
    ranking_rows = []
    trial_started = created
    for i, (variant, params, path, m) in enumerate(rows):
        vhash = vhash_by_variant[variant]
        emul, orig = export.series_from_csv(path, day=day, vhash=vhash, variant=variant, window=window)
        s_path, _ = export.write_tier2(out_dir, emul)
        files = {'series': os.path.relpath(s_path, out_dir)}
        if orig_doc is None:
            o_path, _ = export.write_tier2(out_dir, orig)
            orig_doc = orig
            files['orig'] = os.path.relpath(o_path, out_dir)
        elif {k: v for k, v in orig.items() if k != 'vhash'} != {k: v for k, v in orig_doc.items() if k != 'vhash'}:
            log(f'note: the phone side of {variant} differs from the base CSV; series/{day}/orig.json is the base one')
        seconds = seconds_of.get(variant)
        finished = _shift(trial_started, float(seconds or 0))
        is_base = variant == BASE_VARIANT
        feasible = True if is_base else not M.feasibility_day(m, metrics_by_variant[BASE_VARIANT],
                                                              M.floors_from_base(metrics_by_variant[BASE_VARIANT]))
        row = TrialRow(trial=i, key=contracts.key(params, day, study.settings_sha, study.code_sha, '', M.OBJECTIVE_VERSION),
                       phase='floor' if is_base else 'grid', vhash=vhash, variant=variant, params=params, day=day,
                       status='done', code_sha=study.code_sha, settings_sha=study.settings_sha, freeze_sha='',
                       seconds=seconds, worker=1, round=1, started=trial_started, finished=finished, metrics=m,
                       feasible=feasible, flags=list(m.get('flags', [])), files=files, control='base' if is_base else None,
                       metrics_version=M.METRICS_VERSION)
        sd.append(row)
        ranking_rows.append({'variant': variant, 'day': day, 'metrics': m, 'params': params, 'status': 'done'})
        trial_started = finished

    # round.json, STATE.json, status.json
    rnd, ranking = build_round(ranking_rows, metrics_by_variant, vhash_by_variant, grid, phone_point, day, created, trial_started)
    contracts.write_json_atomic(sd.round_path, rnd.to_dict())
    sd.write_state(State(phase='grid', trials_done=len(rows), queue_len=0, pid=0, heartbeat=trial_started,
                         started=created, round=1, exit_code=0, stop_reason='finished'))
    status = sd.write_status()
    _lint(st.status_text(status))

    base_m = metrics_by_variant[BASE_VARIANT]
    manifest['table'] = {v: {'delta_U': round(m['delta_U'], 3), 'delta_vs_base_U': round(m['delta_vs_base_U'], 3),
                             'rise_vs_base_U': round(m['rise_vs_base_U'], 3), 'smb_less': m['smb_less'],
                             'at_autoISF_max_pct': None if m.get('at_autoISF_max_pct') is None else round(m['at_autoISF_max_pct'], 3)}
                         for v, m in metrics_by_variant.items()}
    manifest['floors'] = {k: v for k, v in M.floors_from_base(base_m).items()}
    manifest['review_order'] = list(ranking.order)
    for root, _, files in os.walk(out_dir):
        for name in files:
            if name == 'MANIFEST.json':
                continue
            p = os.path.join(root, name)
            manifest['files'][os.path.relpath(p, out_dir).replace(os.sep, '/')] = {'bytes': os.path.getsize(p), 'sha1': _sha1_file(p)}
    with open(os.path.join(out_dir, 'MANIFEST.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False, sort_keys=False)
        fh.write('\n')
    return manifest


# ----------------------------------------------------------------------------- command line

def register(subparsers):
    """Add `fixtures` to an argparse subparsers object (cli.py convention: the parser's default `func` returns the
    exit code)."""
    p = subparsers.add_parser('fixtures', help='rebuild tests/fixtures/study_example from results/example_2026-09-06',
                              description=__doc__.split('\n\n')[1])
    p.add_argument('--out', default=OUT_DIR, help=f'the fixture study directory (default {os.path.relpath(OUT_DIR, SOFTWARE_DIR)})')
    p.add_argument('--results', default=RESULTS_DIR, help='the optimizer run to read (five CSVs, the .vdf files, summary.json)')
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH, help='ns_settings.json (caps and the phone values)')
    p.set_defaults(func=run_fixtures)
    return p


def run_fixtures(args, log=print):
    """`hc fixtures` / `python -m hindcast.tests.make_fixtures`: exit 0 when written, 3 when the run or a contract refuses."""
    try:
        manifest = build_fixture_study(args.out, args.results, args.settings, log=log)
    except (FixtureError, ContractError, lint.LintError, OSError) as e:
        print(f'fixtures: {e}', file=sys.stderr)
        return EXIT_REFUSED
    for rel, info in sorted(manifest['files'].items()):
        log('%8.1f kB  %s' % (info['bytes'] / 1000.0, rel))
    log(f'{len(manifest["sources"])} variants, {manifest["loops"]} loops, day {manifest["day"]}, '
        f'window {manifest["window"][0]}..{manifest["window"][1]} -> {args.out}')
    log('review order: ' + ', '.join(manifest['review_order']))
    log('')
    log(contracts.HONESTY_STATEMENT)
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.tests.make_fixtures', description=__doc__.split('\n\n')[1])
    sub = parser.add_subparsers(dest='command')
    register(sub)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != 'fixtures':
        argv = ['fixtures'] + argv
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
