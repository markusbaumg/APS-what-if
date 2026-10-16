"""
make_report_fixtures.py  -  generate tests/fixtures/report/ (backlog H-002)

    cd software && ../.venv/bin/python -m hindcast.tests.make_report_fixtures [--csv <result csv>] [--no-replay]

Source: variants/2026-09-06T06-00-00Z_2026-09-06T09-00-00Z.no_change.csv (180 loops, 06-09Z) when present, else
results/example_2026-09-06/2026-09-06T06-00-00Z.base.csv (340 loops, 06-12Z). The series documents need only the CSV.
The pred documents need the Pred / FlowChart globals of a replay: when the Nightscout day cache for the window is
present (and --no-replay is not given) the script replays the empty VDF over the CSV's window in a temp dir with
`emulator_core.parameters_known()` and builds pred/<day>/<vhash>.json and pred/<day>/orig.json from the globals and
the two transcripts. The replay runs inside `freeze.offline_guard(enabled=True)`: the NightscoutClient opens no
socket whatever HINDCAST_OFFLINE says, every cache file present is served as it is (final or not) and a missing one
raises OfflineError instead of being fetched, so the fixtures never depend on the network. MANIFEST.json records
sources, sizes and hashes.
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(os.path.dirname(TESTS_DIR))
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts, export                                                      # noqa: E402
from hindcast.tests import conftest                                                         # noqa: E402
from emulator_optimize import read_result_csv, DEFAULT_OUTPUTS                              # noqa: E402

OUT_DIR = os.path.join(TESTS_DIR, 'fixtures', 'report')
EXAMPLE_BASE_CSV = os.path.join(conftest.RESULTS_EXAMPLE_DIR, conftest.EXAMPLE_BASE + '.base.csv')


def pick_csv():
    if os.path.isfile(conftest.NO_CHANGE_CSV):
        return conftest.NO_CHANGE_CSV, conftest.CHECK_WINDOW
    if os.path.isfile(EXAMPLE_BASE_CSV):
        return EXAMPLE_BASE_CSV, conftest.EXAMPLE_WINDOW
    sys.exit('no source CSV: neither %s nor %s' % (conftest.NO_CHANGE_CSV, EXAMPLE_BASE_CSV))


def replay(window, workdir):
    """Replay the empty VDF over `window` in `workdir`; -> (Pred, FlowChart, csv path, txt path, orig txt path, seconds).
    Runs inside freeze.offline_guard(enabled=True): no socket is opened, a missing cache file is an OfflineError."""
    import emulator_core as ec
    from hindcast import freeze
    ec.set_tty(0, 0, 'batch')
    vdf = os.path.join(workdir, 'no_change.vdf')
    open(vdf, 'w').close()
    banner = 'Hindcast fixture replay (hindcast/tests/make_report_fixtures.py)\n'
    t0 = time.time()
    buf = io.StringIO()
    with freeze.offline_guard(enabled=True), contextlib.redirect_stdout(buf):
        res = ec.parameters_known('ns:', 'Windows/' + DEFAULT_OUTPUTS + '/noplot/.', vdf, window[0], window[1], {},
                                  banner, '.', datetime.datetime(1970, 1, 1))
    if res[1] in ('SYNTAX', 'UTF8', 'Z'):
        sys.exit('replay failed: %s\n%s' % (res[1], buf.getvalue()[-2000:]))
    files = os.listdir(workdir)
    csv = [f for f in files if f.endswith('.no_change.csv')]
    txt = [f for f in files if f.endswith('.no_change.txt')]
    orig = [f for f in files if f.endswith('.orig.txt')]
    if not (csv and txt and orig):
        sys.exit('replay left no result files in %s: %s' % (workdir, files))
    return (dict(ec.Pred), dict(ec.FlowChart), os.path.join(workdir, csv[0]), os.path.join(workdir, txt[0]),
            os.path.join(workdir, orig[0]), round(time.time() - t0, 1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--csv', help='result CSV to build the series from (default: the no_change replay CSV)')
    ap.add_argument('--out', default=OUT_DIR)
    ap.add_argument('--no-replay', action='store_true', help='series only, even when the cache is present')
    args = ap.parse_args(argv)

    csv_path, window = (args.csv, None) if args.csv else pick_csv()
    names, rows = read_result_csv(csv_path)
    if not rows:
        sys.exit('no loops in ' + csv_path)
    day = export.day_of(export.loop_times(rows)[0])
    manifest = {'generated': contracts.now_iso(), 'source_csv': os.path.relpath(csv_path, SOFTWARE_DIR), 'day': day,
                'loops': len(rows), 'window': list(window) if window else None, 'code_sha': contracts.code_sha(),
                'settings_sha': contracts.settings_sha() if os.path.isfile(contracts.NS_SETTINGS_PATH) else None,
                'base_vhash': contracts.BASE_VHASH, 'files': {}, 'replay': None}

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)

    pred = flows = txt = orig_txt = None
    can_replay = window and not args.no_replay and conftest.day_is_cached(day)
    if can_replay:
        work = tempfile.mkdtemp(prefix='hindcast_fixture_')
        try:
            pred, flows, replay_csv, txt, orig_txt, seconds = replay(window, work)
            # the fixture set must be self-consistent (series, levels, reason of one replay), so when the replay runs
            # its CSV is the source; the committed CSV is the reference and the columns that differ are recorded
            r_names, r_rows = read_result_csv(replay_csv)
            differing = sorted({k for ra, rb in zip(rows, r_rows) for k in names if ra.get(k) != rb.get(k)})
            manifest['replay'] = {'seconds': seconds, 'loops': len(pred), 'reference_csv': manifest['source_csv'],
                                  'reference_loops': len(rows), 'columns_differing_from_reference': differing}
            manifest['source_csv'] = 'replay of the empty VDF over the window (current core)'
            if differing:
                print('note: the fresh replay differs from %s in %s (the core or the settings changed since it was '
                      'written); the fixtures are built from the replay' % (manifest['replay']['reference_csv'], differing))
            names, rows = r_names, r_rows
            pred_doc, pred_text = export.pred_from_globals(day, contracts.BASE_VHASH, rows, pred, flows, transcript=txt,
                                                           variant='no_change', window=window)
            pred_orig, pred_orig_text = export.pred_orig_from_globals(day, rows, pred, transcript=orig_txt, window=window)
            for doc in (pred_doc, pred_text, pred_orig, pred_orig_text):
                path, size = export.write_tier2(args.out, doc)
                manifest['files'][os.path.relpath(path, args.out)] = size
        finally:
            shutil.rmtree(work, ignore_errors=True)
    else:
        print('pred files skipped:', 'cache for %s not present' % day if window else 'no window for a custom CSV'
              if not args.no_replay else '--no-replay')

    emul, orig = export.series_from_rows(names, rows, day=day, vhash=contracts.BASE_VHASH, variant='no_change',
                                         pred=pred, window=window)
    for doc in (emul, orig):
        path, size = export.write_tier2(args.out, doc)
        manifest['files'][os.path.relpath(path, args.out)] = size
    with open(os.path.join(args.out, 'MANIFEST.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=1)
        fh.write('\n')
    for name, size in sorted(manifest['files'].items()):
        print('%8.1f kB  %s' % (size / 1000.0, name))
    print('loops %d, day %s, replay %s' % (len(rows), day, manifest['replay']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
