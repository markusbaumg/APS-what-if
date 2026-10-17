"""The "no change" regression gate as one command: `python nochange_check.py <start> <end> [url]`.

Replays the window from Nightscout with an EMPTY variant file (= the phone's own settings) in a temporary
directory and prints how often the emulated ISF, insulinReq, maxBolus, SMB and TBR equal the originals
(the `orig`/`emul` column pairs of the result CSV, see CLAUDE.md). Exit status 1 when a column falls below
its threshold or fewer than two loops were replayed. Interim gate until Hindcast's `hc check` (H-902) with
golden metrics replaces it; the thresholds below are deliberately loose (the 2026-09-06 06-09Z window gives
ISF 99 %, insReq 93 %, maxBolus 99 %, SMB 99 %, TBR 98 %; 09-15Z: 98 / 85 / 99 / 99 / 97 %; 2026-10-10 06-12Z: 99 / 94 / 100 / 100 / 98 %;
exact counts in CLAUDE.md). Stdlib only, meant for `make nochange`.
"""
import glob
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# (label, orig column, emul column, tolerance, minimum share of agreeing loops) - zero-based CSV columns
COLUMNS = [('ISF', 32, 34, 0.05, 0.97), ('insReq', 35, 36, 0.005, 0.80), ('maxBolus', 37, 38, 0.011, 0.90),
           ('SMB', 39, 40, 0.001, 0.93), ('TBR', 41, 42, 0.001, 0.93)]


def agreement(csv_path):
    rows = [line.rstrip('\n').split(';') for line in open(csv_path, encoding='utf-8')]
    data = [r for r in rows[3:] if len(r) >= 43 and r[0].strip()]

    def num(x):
        return float(x.strip() or 0)

    out = {}
    for label, a, b, tol, _ in COLUMNS:
        out[label] = sum(abs(num(r[a]) - num(r[b])) <= tol for r in data)
    return len(data), out


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    start, end = argv[1], argv[2]
    url = argv[3] if len(argv) > 3 else 'ns:'
    env = dict(os.environ, PYTHONUTF8='1')
    with tempfile.TemporaryDirectory(prefix='nochange_') as tmp:
        vdf = os.path.join(tmp, 'no_change.vdf')
        open(vdf, 'w').close()
        cmd = [sys.executable, os.path.join(HERE, 'emulator_batch.py'), url, 'bg/iob/ISFs/insReq/SMB/basal/noplot/.',
               vdf, start, end]
        run = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True)
        tail = [l for l in run.stdout.splitlines()
                if 'loop runs' in l or 'skipped' in l or 'pinned' in l or 'failed' in l.lower()]
        for line in tail[-4:]:
            print(line.strip())
        csvs = glob.glob(os.path.join(tmp, '*.no_change.csv'))
        if run.returncode != 0 or not csvs:
            print('no-change replay produced no CSV; exit', run.returncode)
            print(run.stderr[-2000:])
            return 1
        loops, agree = agreement(csvs[0])
    print(f'\n{"no-change gate":16s} {start} .. {end}: {loops} loops')
    status = 0 if loops >= 2 else 1
    for label, _, _, tol, minimum in COLUMNS:
        share = agree[label] / loops if loops else 0.0
        mark = 'ok ' if share >= minimum else 'LOW'
        status |= share < minimum
        print(f'  {mark} {label:9s} {agree[label]:5d}/{loops}  {100 * share:5.1f} %   (tolerance {tol}, minimum {100 * minimum:.0f} %)')
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv))
