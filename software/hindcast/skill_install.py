"""
skill_install.py  -  the Hindcast Claude skill: vocabulary generator, lint of the skill text, `hc skill install`
and the one-line check-in (backlog H-501, DESIGN.md section 7)

    hc skill install   [--project | --dest DIR] [--dry-run]
                                                     copy software/hindcast/skill/ -> ~/.claude/skills/hindcast/
                                                     (user scope, the default of DESIGN.md section 7.1); --project
                                                     installs into <repo>/.claude/skills/hindcast/ instead, which
                                                     is the opt-in once the maintainers agree
    hc skill vocabulary [--check]                    regenerate skill/references/vocabulary.md from vocabulary.json
    hc skill lint                                    lint every skill file with hindcast.lint kind 'line'
    hc skill checkin <study> [--artifact URL]        print the one-line check-in from status.json (<= 2 KB)

    (`python -m hindcast.skill_install <command> ...` is the same parser; cli.py wires `hc skill` through
    register(subparsers), whose subcommands call run_skill(args).)

The source of the skill lives in `software/hindcast/skill/` (SKILL.md + references/). `references/vocabulary.md` is
GENERATED from `hindcast/vocabulary.json` by render_vocabulary(); never edit it by hand. Its "May not say"
section quotes the refused wording on purpose, between the markers LINT_OFF / LINT_ON; lintable_text() drops
what lies between them, so every other line of every skill file passes `lint.lint_findings(text, 'line')`.

`install` refuses (exit 2) when a skill file fails that lint or vocabulary.md is stale, writes a freshly rendered
vocabulary.md into the destination, overwrites the other files and reports (never deletes) files in the destination
that the source does not have. It writes nothing outside the destination directory. Nothing here changes the
phone, Nightscout or ns_settings.json.

checkin_line(status) is the check-in format of SKILL.md ("Check-in"): one line built from status.json alone, so a
check-in never reads a CSV, a transcript or trials.jsonl into context.

Exit codes: 0 done, 2 refused by the lint / stale vocabulary, 3 no such study or status.json.
"""
import argparse
import json
import os
import re
import shutil
import sys

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
REPO_DIR = os.path.dirname(SOFTWARE_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import lint                                                                   # noqa: E402

SKILL_NAME = 'hindcast'
SKILL_SRC = os.path.join(HINDCAST_DIR, 'skill')
VOCABULARY_MD = os.path.join(SKILL_SRC, 'references', 'vocabulary.md')
USER_DEST = os.path.join(os.path.expanduser('~'), '.claude', 'skills', SKILL_NAME)     # DESIGN.md 7.1: user scope
PROJECT_DEST = os.path.join(REPO_DIR, '.claude', 'skills', SKILL_NAME)                 # opt-in: hc skill install --project
DEFAULT_DEST = USER_DEST
STUDIES_DIR = os.path.join(SOFTWARE_DIR, 'studies')
LINT_OFF = '<!-- lint:off (quoted refused wording) -->'
LINT_ON = '<!-- lint:on -->'
FIDELITY_FLOOR_PCT = 94.0                 # DESIGN.md 7.3 step 3: base agreement below this on a day -> finding issue
EXIT_OK, EXIT_LINT, EXIT_REFUSED = 0, 2, 3
_SKIP = ('__pycache__', '.DS_Store')


# ----------------------------------------------------------------------------- vocabulary.md

def render_vocabulary(vocab=None):
    """references/vocabulary.md as a string, generated from vocabulary.json (parse_vocabulary reverses it)."""
    v = vocab or lint.load_vocabulary()
    out = ['# Hindcast vocabulary', '',
           'GENERATED from `software/hindcast/vocabulary.json` by `hc skill vocabulary`',
           '(`hindcast/skill_install.py`); do not edit by hand. `hindcast/lint.py` applies the same file to every',
           'card, proposal, issue body and check-in line, so wording that passes here passes there.', '',
           '## Honesty statement', '',
           'Verbatim on every page, card and proposal, and at the end of every sitting:', '',
           '> ' + v['honesty_statement'], '',
           '## May say', '',
           'Decision-level wording: what the loop would have asked for, never a glucose outcome.', '']
    out += ['- "%s"' % p for p in v['may']]
    out += ['', '## May not say', '',
            'Each entry: the refused wording, why, and a sentence the lint refuses. Say what the loop would have',
            'asked for instead.', '', LINT_OFF]
    for e in v['may_not']:
        out.append('- **%s**: %s. Refused: "%s"' % (e['phrase'], e.get('why', ''), e.get('example', '')))
    out += [LINT_ON, '',
            '## Privacy (GitHub issues and comments; the repository is public)', '',
            'Preference names and values may appear. Glucose values, insulin units per day, loop counts per day,',
            'transcript excerpts and prediction arrays may not; link to the private artifact instead.', '']
    for e in v['privacy']:
        line = '- **%s**: %s' % (e['name'], e.get('why', ''))
        if e.get('passes'):
            line += '. Passes: "%s"' % e['passes']
        out.append(line)
    out += ['', '## Verdict words', '',
            '`base`, `dead` (say "not testable on these days"), `candidate`, `tie`, `infeasible` (say why: P,',
            'envelope, maxBolus, cap, loop count), `recommended` (always written "recommended for review"),',
            '`pending`.', '',
            '## Card template (what `hc card` writes; never rewrite a card by hand)', '',
            '1. Title: the changed keys with the phone value, "Candidate N: key new (phone: old)".',
            '2. Settings table: AAPS preference title, VDF key, phone (current), candidate (proposed), unit.',
            '3. Days replayed by role (search / hold-out) with the day tags.',
            '4. What the loop would have asked for vs base: rising loops and the other loops, each next to its',
            '   floor, ties named as ties; loops changed by direction; low-side flags; zero temps.',
            '5. Constraints and safety readings: P, envelope E, cap binding, largest single emulated SMB next to',
            '   the phone\'s maxBolus.',
            '6. Rank by stratum and hold-out, leave-one-day-out share, the reason when it is not recommended for review.',
            '7. Ties, the honesty statement, the VDF text.',
            '8. A study with unlocked keys (tier C: profile sens, carb_ratio, current_basal; tier S: the caps)',
            '   carries the weak-evidence caveat `hc study new` printed, next to the honesty statement: the',
            '   replay keeps the history the phone\'s own profile produced and cannot judge a different one.', '']
    return '\n'.join(out)


def _quoted(line):
    m = re.match(r'^- "(.*)"$', line)
    return m.group(1) if m else None


def parse_vocabulary(text):
    """The rendered vocabulary.md back to {'honesty_statement', 'may', 'may_not', 'privacy'} (phrases / names)."""
    doc = {'honesty_statement': None, 'may': [], 'may_not': [], 'privacy': []}
    section = None
    for line in text.splitlines():
        if line.startswith('## '):
            section = line[3:].strip().lower()
            continue
        if section == 'honesty statement' and line.startswith('> '):
            doc['honesty_statement'] = line[2:]
        elif section == 'may say' and _quoted(line) is not None:
            doc['may'].append(_quoted(line))
        elif section == 'may not say' and line.startswith('- **'):
            doc['may_not'].append(line[4:].split('**:', 1)[0])
        elif section and section.startswith('privacy') and line.startswith('- **'):
            doc['privacy'].append(line[4:].split('**:', 1)[0])
    return doc


def vocabulary_is_current(path=VOCABULARY_MD):
    try:
        with open(path, encoding='utf-8') as fh:
            return fh.read() == render_vocabulary()
    except OSError:
        return False


def write_vocabulary(path=VOCABULARY_MD):
    """(Re)write references/vocabulary.md; returns True when the file changed."""
    text = render_vocabulary()
    try:
        with open(path, encoding='utf-8') as fh:
            if fh.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    return True


# ----------------------------------------------------------------------------- lint of the skill text

def skill_files(src=SKILL_SRC):
    """Every file of the skill source tree, as paths relative to `src`, sorted."""
    found = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP)
        for f in sorted(files):
            if f not in _SKIP:
                found.append(os.path.relpath(os.path.join(root, f), src))
    return found


def lintable_text(text):
    """`text` without the LINT_OFF ... LINT_ON blocks (the quoted refused wording of vocabulary.md)."""
    out, on = [], True
    for line in text.splitlines():
        if line.strip() == LINT_OFF:
            on = False
        elif line.strip() == LINT_ON:
            on = True
        elif on:
            out.append(line)
    return '\n'.join(out)


def lint_skill(src=SKILL_SRC):
    """{relative path: [lint.Finding, ...]} for every skill file that fails `lint` kind 'line' ({} = clean)."""
    bad = {}
    for rel in skill_files(src):
        if not rel.endswith('.md'):
            continue
        with open(os.path.join(src, rel), encoding='utf-8') as fh:
            findings = lint.lint_findings(lintable_text(fh.read()), 'line')
        if findings:
            bad[rel] = findings
    return bad


# ----------------------------------------------------------------------------- install

class InstallRefused(Exception):
    pass


def install(dest=DEFAULT_DEST, src=SKILL_SRC, dry_run=False, log=None):
    """Copy the skill tree to `dest` (vocabulary.md freshly rendered). Returns (written, extra): the relative paths
    written (or that would be, with dry_run) and the files in `dest` the source does not have (left alone).
    Raises InstallRefused when a file fails the lint or the source vocabulary.md is stale."""
    log = log or (lambda text: None)
    bad = lint_skill(src)
    if bad:
        raise InstallRefused('; '.join('%s: %s' % (rel, f[0]) for rel, f in bad.items()))
    vocab_rel = os.path.relpath(VOCABULARY_MD, SKILL_SRC)
    if os.path.abspath(src) == os.path.abspath(SKILL_SRC) and not vocabulary_is_current():
        raise InstallRefused('%s is stale: run `hc skill vocabulary` first' % vocab_rel)
    rels = skill_files(src)
    if vocab_rel not in rels:
        rels.append(vocab_rel)
    written = []
    for rel in sorted(rels):
        target = os.path.join(dest, rel)
        written.append(rel)
        if dry_run:
            log('would write ' + target)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if rel == vocab_rel:
            with open(target, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(render_vocabulary())
        else:
            shutil.copyfile(os.path.join(src, rel), target)
        log('wrote ' + target)
    extra = [rel for rel in skill_files(dest) if rel not in rels] if os.path.isdir(dest) else []
    for rel in extra:
        log('left alone (not in the source): ' + os.path.join(dest, rel))
    return written, extra


# ----------------------------------------------------------------------------- the one-line check-in

def read_status(study_dir):
    """status.json of a study directory as a dict (the only file a check-in reads)."""
    with open(os.path.join(study_dir, 'status.json'), encoding='utf-8') as fh:
        return json.load(fh)


def _short_key(name):
    return name.split(' ', 1)[1] if ' ' in name else name


def checkin_line(status, artifact_url=None):
    """The one-line check-in of SKILL.md from a status.json dict:
    '<study> round R, phase P: D/N trials[, F failed][, B blocked]; floor: base agrees with the phone in A % of loops
    [(below the 94 % fidelity floor)]; review order 1: <variant> (<verdict>)[; not testable on these days: k, ...];
    <run state>[; stale context][; <artifact>]'. Every part uses decision-level words only (lint kind 'line')."""
    parts = []
    head = '%s round %s, phase %s: %s/%s trials' % (status.get('study'), status.get('round', 0), status.get('phase'),
                                                   status.get('trials_done', 0), status.get('trials_planned', 0))
    for key, word in (('trials_failed', 'failed'), ('trials_blocked', 'blocked')):
        if status.get(key):
            head += ', %s %s' % (status[key], word)
    parts.append(head)
    agreement = (status.get('floor') or {}).get('agreement_pct')
    if agreement is None:
        parts.append('floor: not measured yet')
    else:
        text = 'floor: base agrees with the phone in %.1f %% of loops' % agreement
        if agreement < FIDELITY_FLOOR_PCT:
            text += ' (below the %g %% fidelity floor)' % FIDELITY_FLOOR_PCT
        parts.append(text)
    top = status.get('top') or []
    if top:
        t = top[0]
        verdict = 'recommended for review' if t.get('verdict') == 'recommended' else t.get('verdict')
        parts.append('review order 1: %s (%s)' % (t.get('variant'), verdict))
    else:
        parts.append('review order: none yet')
    dead = status.get('dead_parameters') or []
    if dead:
        parts.append('not testable on these days: ' + ', '.join(_short_key(d) for d in dead))
    if status.get('running'):
        parts.append('running, heartbeat %s' % status.get('heartbeat'))
    else:
        parts.append(status.get('stop_reason') or 'not running')
    if status.get('stale_context'):
        parts.append('stale context')
    url = artifact_url or status.get('artifact_url')
    if url:
        parts.append(url)
    line = '; '.join(parts)
    lint.lint_text(line, 'line')
    return line


# ----------------------------------------------------------------------------- CLI

def _resolve_study(name, studies_dir):
    if os.path.isdir(name):
        return name
    path = os.path.join(studies_dir, name)
    return path if os.path.isdir(path) else None


def run_skill(args):
    """`hc skill <install|vocabulary|lint|checkin>` -> exit code."""
    out = lambda text: sys.stdout.write(text + '\n')                                   # noqa: E731
    cmd = getattr(args, 'skill_command', None)
    if cmd == 'vocabulary':
        if args.check:
            ok = vocabulary_is_current()
            out('%s: %s' % (VOCABULARY_MD, 'current' if ok else 'stale (run `hc skill vocabulary`)'))
            return EXIT_OK if ok else EXIT_LINT
        out('%s: %s' % (VOCABULARY_MD, 'rewritten' if write_vocabulary() else 'unchanged'))
        return EXIT_OK
    if cmd == 'lint':
        bad = lint_skill()
        for rel, findings in bad.items():
            for f in findings:
                sys.stderr.write('%s: %s\n' % (rel, f))
        if not bad:
            out('%d skill file(s) pass the lint (kind line)' % len(skill_files()))
        return EXIT_LINT if bad else EXIT_OK
    if cmd == 'install':
        try:
            dest = install_dest(args)
            written, extra = install(dest, dry_run=args.dry_run, log=out)
        except InstallRefused as e:
            sys.stderr.write('hc skill install: refused: %s\n' % e)
            return EXIT_LINT
        out('%s %d file(s) to %s%s' % ('would copy' if args.dry_run else 'copied', len(written), dest,
                                       ', %d extra file(s) left alone' % len(extra) if extra else ''))
        return EXIT_OK
    if cmd == 'checkin':
        sd = _resolve_study(args.name, args.studies_dir)
        try:
            status = read_status(sd) if sd else None
        except (OSError, ValueError) as e:
            sys.stderr.write('hc skill checkin: %s\n' % e)
            status = None
        if status is None:
            sys.stderr.write('hc skill checkin: no status.json for %s (run `hc round` or `hc study run` first)\n' % args.name)
            return EXIT_REFUSED
        out(checkin_line(status, args.artifact))
        return EXIT_OK
    sys.stderr.write('hc skill: give install, vocabulary, lint or checkin\n')
    return EXIT_REFUSED


def install_dest(args):
    """--dest wins, then --project (<repo>/.claude/skills/hindcast), else the user scope ~/.claude/skills/hindcast."""
    if getattr(args, 'dest', None):
        return args.dest
    return PROJECT_DEST if getattr(args, 'project', False) else DEFAULT_DEST


def add_subcommands(sub):
    i = sub.add_parser('install', help='copy software/hindcast/skill/ to ~/.claude/skills/hindcast/ (user scope)')
    where = i.add_mutually_exclusive_group()
    where.add_argument('--dest', default=None, help='destination directory (default ~/.claude/skills/hindcast)')
    where.add_argument('--project', action='store_true',
                       help='install into <repo>/.claude/skills/hindcast (project scope, opt-in)')
    i.add_argument('--dry-run', action='store_true', help='list what would be written, write nothing')
    i.set_defaults(func=run_skill)
    v = sub.add_parser('vocabulary', help='regenerate skill/references/vocabulary.md from vocabulary.json')
    v.add_argument('--check', action='store_true', help='exit 2 when the file is stale, write nothing')
    v.set_defaults(func=run_skill)
    k = sub.add_parser('lint', help="lint every skill file with hindcast.lint kind 'line'")
    k.set_defaults(func=run_skill)
    c = sub.add_parser('checkin', help='the one-line check-in from studies/<study>/status.json')
    c.add_argument('name', help='study name under --studies-dir, or a study directory')
    c.add_argument('--artifact', default=None, metavar='URL', help='the artifact link to append')
    c.add_argument('--studies-dir', default=STUDIES_DIR)
    c.set_defaults(func=run_skill)
    return sub


def register(subparsers):
    """Add `skill` with install|vocabulary|lint|checkin (cli.py's convention: each `func(args) -> int`)."""
    p = subparsers.add_parser('skill', help='the Claude skill: install, regenerate vocabulary.md, lint, one-line check-in')
    sub = p.add_subparsers(dest='skill_command', metavar='install|vocabulary|lint|checkin')
    sub.required = True
    add_subcommands(sub)
    return p


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.skill_install', description=__doc__.split('\n\n')[1])
    sub = parser.add_subparsers(dest='skill_command', metavar='install|vocabulary|lint|checkin')
    sub.required = True
    add_subcommands(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
