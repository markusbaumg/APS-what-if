"""
gh.py  -  GitHub issue workflow for Hindcast through the `gh` CLI (backlog H-601; DESIGN.md section 8)

    python -m hindcast.gh labels                                     [--dry-run] [--update]
    python -m hindcast.gh issues --from ../docs/hindcast/BACKLOG.md  [--dry-run] [--only H-6] [--no-lint]
    python -m hindcast.gh epics  --from ../docs/hindcast/BACKLOG.md  [--dry-run] [--only H-6] [--no-update]
    (cwd software/; `hc gh ...` forwards to main(argv))

What it does
    labels   creates every label of LABELS that the repo does not have yet (existing ones are left alone;
             --update rewrites colour and description with `gh label create --force`).
    issues   one task issue per `### H-nnn · <title>` block of BACKLOG.md, title '<H-id> <title>' (the
             ' · **MVP**' flag dropped), labels from the block's `labels:` line, body = the block's `Body:`
             text plus a conventions footer. Idempotent: a block whose title already exists (any state) is
             skipped; a block whose H-id already heads an existing title is skipped too and reported, so a
             rewording in the backlog never creates a second issue.
    epics    one issue per `## En — <name> (epic `H-n00`) · labels: ...` section, title '<H-id> Epic En: <name>'
             (the format the first, hand-made epic issues already carry),
             body = the section's intro text + a task list `- [ ] #<number> <task title>` of its task issues
             (tasks not created yet are listed by title). An existing epic's body is rewritten only when the
             task list changed (--no-update keeps it).

Every call goes through Gh.run(), which prefixes `gh`, appends `-R <repo>` to label/issue commands and in
--dry-run executes read-only commands (list) but only prints the mutating ones (create, edit). Tests pass
a `runner` callable instead of subprocess. The repo is named explicitly because `gh` in this clone
resolves the default repository to the upstream fork (ga-zelle), not to markusbaumg/APS-what-if.

Privacy (DESIGN.md section 8, the repository is public): bodies may name preference keys and values,
never glucose values, insulin per day, loop counts per day, transcripts or prediction arrays. When
`hindcast.lint` exists, every body passes `lint.lint_text(body, kind='issue')` first and a refused body is
reported and NOT posted (--no-lint skips the check; the backlog text itself is already public in the repo).

Not covered here (later issues): campaign/round comments (H-602), proposal/finding issues and the
label read-back (H-603). They should reuse Gh, ensure_labels() and HONESTY from this module.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

REPO = 'markusbaumg/APS-what-if'
BACKLOG_DEFAULT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                '..', '..', 'docs', 'hindcast', 'BACKLOG.md'))

# name -> (colour, description); the three GitHub defaults bug/documentation/enhancement already exist
LABELS = {
    'hindcast':           ('0E8A16', 'Hindcast scenario studio / settings optimizer (software/hindcast)'),
    'epic':               ('3E4B9E', 'Epic: one per backlog section, task list of its issues'),
    'mvp':                ('FBCA04', 'Part of the MVP cut (DESIGN.md section 10)'),
    'algorithm-fidelity': ('D93F0B', 'Emulator vs phone decision divergence (raises the noise floor)'),
    'blocked-core':       ('B60205', 'Needs a change in emulator_core.py / determine_basal.py (maintainers)'),
    'campaign':           ('1D76DB', 'One study run: protocol table, one comment per round'),
    'proposal':           ('5319E7', 'A settings candidate for human review (never applied by the tool)'),
    'finding':            ('C5DEF5', 'Fidelity drop, saturating cap, dead parameter, hand-off'),
    'accepted':           ('0E8A16', 'Proposal accepted by the human for review on the phone'),
    'rejected':           ('E4E669', 'Proposal rejected by the human'),
    'applied-on-phone':   ('006B75', 'Proposal applied on the phone; profile_history reminder due'),
}

# DESIGN.md section 3, verbatim; every card, proposal and campaign body quotes it (H-602/H-603 import it)
HONESTY = (
    "Hindcast replays the loop's decisions under other settings over the glucose and insulin history that the "
    "phone's own settings produced. It says where and how much the loop would have asked differently; it "
    "cannot say what glucose would have done. A variant that boluses earlier is charged for its bolus and for "
    "the later ones the phone actually gave, so deltas overstate aggressive settings and understate gentle "
    "ones. The base replay disagrees with the phone in about 4 % of loops; differences smaller than that floor "
    "are ties. Nothing here changes the phone; settings are for you and your care team to review. This is one "
    "person's insulin dosing, not medical advice."
)

FOOTER = (
    "\n\n---\n"
    "Conventions (docs/hindcast/BACKLOG.md): integration branch `hindcast` off `nightscout-source`, one PR per "
    "issue, new code only under `software/hindcast/` unless the issue says otherwise; PRs that can change an "
    "evaluation paste `hc check` before/after. Privacy: this repository is public, so issue text names "
    "preference keys and values only, never glucose values, insulin per day, loop counts per day or transcript "
    "excerpts. Honesty statement: docs/hindcast/DESIGN.md section 3."
)

_RE_EPIC = re.compile(r'^## (E\d+) — (.+?) \(epic `(H-\d+)`\)\s*·\s*labels:\s*(.*?)\s*$')
_RE_TASK = re.compile(r'^### (H-\d+)\s*·\s*(.+?)\s*$')
_RE_SECTION = re.compile(r'^## ')


class GhError(RuntimeError):
    """A `gh` command failed, or the backlog cannot be turned into issues."""


@dataclass
class Block:
    """One issue-to-be from BACKLOG.md. kind 'epic' (a `##` section) or 'task' (a `###` block)."""
    hid: str                      # 'H-601'
    title: str                    # 'hc gh labels|epics: create labels and the epic issues from this backlog'
    labels: list                  # ['hindcast', 'mvp']
    body: str                     # the Body: text (tasks) or the intro prose (epics), without the footer
    kind: str = 'task'
    epic: str = ''                # the enclosing epic's hid for a task
    mvp: bool = False
    tasks: list = field(default_factory=list)   # epics: hids of their tasks in document order

    @property
    def issue_title(self):
        """The exact GitHub title: '<H-id> <title>'. Dedupe matches this, then the '<H-id> ' prefix."""
        return '%s %s' % (self.hid, self.title)


# ----------------------------------------------------------------------------- BACKLOG.md

def _split_labels(text):
    return [x.strip().strip('`') for x in text.split(',') if x.strip()]


def parse_backlog(text):
    """BACKLOG.md text -> list[Block] in document order (epics first within their section, then their tasks).

    Recognised: `## En — Name (epic `H-n00`) · labels: a, b` section headers and `### H-nnn · Title[ · **MVP**]`
    blocks followed by a `labels:` line and a `Body:` paragraph that runs to the next header or `---`.
    The 'Later' bullets (H-L1 ...) and the dependency-order section are not blocks. Raises GhError on a
    duplicate H-id, a task outside an epic section, or a task without labels/body.
    """
    blocks, seen = [], set()
    epic = None
    task = None
    body_lines, in_body = [], False
    intro_lines = []

    def close_task():
        nonlocal task, body_lines, in_body
        if task is not None:
            task.body = '\n'.join(body_lines).strip('\n')
            if not task.body:
                raise GhError('%s has no Body: text' % task.hid)
            if not task.labels:
                raise GhError('%s has no labels: line' % task.hid)
            blocks.append(task)
        task, body_lines, in_body = None, [], False

    def close_epic():
        nonlocal epic, intro_lines
        close_task()
        if epic is not None:
            epic.body = '\n'.join(intro_lines).strip('\n')
        epic, intro_lines = None, []

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _RE_EPIC.match(line)
        if m:
            close_epic()
            name, hid = 'Epic %s: %s' % (m.group(1), m.group(2)), m.group(3)
            if hid in seen:
                raise GhError('duplicate id %s in backlog' % hid)
            seen.add(hid)
            epic = Block(hid, name, _split_labels(m.group(4)), '', kind='epic',
                         mvp='mvp' in _split_labels(m.group(4)))
            blocks.append(epic)
            continue
        if _RE_SECTION.match(line):                      # 'Later', 'Dependency order', ...: not an epic
            close_epic()
            continue
        m = _RE_TASK.match(line)
        if m:
            close_task()
            if epic is None:
                raise GhError('%s appears outside an epic section' % m.group(1))
            hid = m.group(1)
            if hid in seen:
                raise GhError('duplicate id %s in backlog' % hid)
            seen.add(hid)
            parts = [p.strip() for p in m.group(2).split(' · ')]
            flags = [p.lower() for p in parts[1:]]            # '**MVP**', '**MVP** (raises the floor)'
            task = Block(hid, parts[0], [], '', kind='task', epic=epic.hid,
                         mvp=any(f.startswith('**mvp**') for f in flags))
            epic.tasks.append(hid)
            continue
        if task is not None:
            if not in_body and line.lower().startswith('labels:'):
                task.labels = _split_labels(line.split(':', 1)[1])
                continue
            if not in_body and line.startswith('Body:'):
                in_body = True
                rest = line[len('Body:'):].strip()
                if rest:
                    body_lines.append(rest)
                continue
            if line.strip() == '---':
                close_task()
                continue
            if in_body:
                body_lines.append(line)
            continue
        if epic is not None and line.strip() != '---':
            intro_lines.append(line)
    close_epic()
    return blocks


def load_backlog(path=BACKLOG_DEFAULT):
    with open(path, encoding='utf-8') as fh:
        return parse_backlog(fh.read())


def tasks_of(blocks, epic_hid):
    return [b for b in blocks if b.kind == 'task' and b.epic == epic_hid]


def select_blocks(blocks, only=None):
    """Blocks whose hid starts with one of `only` ('H-6' -> H-600, H-601, ...); all when only is empty."""
    if not only:
        return list(blocks)
    return [b for b in blocks if any(b.hid.startswith(o) for o in only)]


# ----------------------------------------------------------------------------- gh wrapper

def _subprocess_runner(cmd, input_text=None):
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise GhError('%s failed (%d): %s' % (' '.join(cmd[:3]), proc.returncode, proc.stderr.strip()))
    return proc.stdout


class Gh:
    """Thin wrapper around the gh CLI.

    runner(cmd: list[str], input_text: str | None) -> stdout str; defaults to subprocess. `commands` keeps
    every command the instance ran or (dry-run) would have run, as (cmd, input_text, executed) tuples.
    `pause` seconds sleep after each creating call (GitHub's content-creation rate limit); 0 in tests.
    """
    MUTATING = {('issue', 'create'), ('issue', 'edit'), ('issue', 'close'), ('issue', 'comment'),
                ('label', 'create'), ('label', 'edit'), ('label', 'delete')}

    def __init__(self, repo=REPO, dry_run=False, runner=None, pause=0.0, out=None):
        self.repo = repo
        self.dry_run = dry_run
        self.runner = runner or _subprocess_runner
        self.pause = pause
        self.out = out or sys.stdout
        self.commands = []
        self._issues = None
        self._dry_number = 0

    def say(self, text):
        print(text, file=self.out)

    def run(self, args, input_text=None):
        """gh <args> -R <repo>; dry-run prints mutating commands instead of running them and returns ''."""
        cmd = ['gh'] + list(args)
        if args and args[0] in ('issue', 'label'):
            cmd += ['-R', self.repo]
        mutating = tuple(args[:2]) in self.MUTATING
        if self.dry_run and mutating:
            self.commands.append((cmd, input_text, False))
            self.say('DRY-RUN  %s%s' % (' '.join(_quote(c) for c in cmd),
                                        '   [body %d chars]' % len(input_text) if input_text else ''))
            return ''
        self.commands.append((cmd, input_text, True))
        result = self.runner(cmd, input_text)
        if mutating and self.pause:
            time.sleep(self.pause)
        return result

    # labels
    def labels(self):
        text = self.run(['label', 'list', '--limit', '200', '--json', 'name,color,description'])
        return {row['name']: row for row in (json.loads(text) if text.strip() else [])}

    def ensure_labels(self, labels=LABELS, update=False):
        """Create the missing labels; returns {'created': [...], 'existing': [...], 'updated': [...]}."""
        have = self.labels()
        report = {'created': [], 'existing': [], 'updated': []}
        for name, (color, desc) in labels.items():
            if name in have and not update:
                report['existing'].append(name)
                continue
            args = ['label', 'create', name, '--color', color, '--description', desc]
            if name in have:
                args.append('--force')
                report['updated'].append(name)
            else:
                report['created'].append(name)
            self.run(args)
        return report

    # issues
    def issues(self, refresh=False):
        """All issues (open and closed, no PRs) as dicts {number, title, state, labels: [names]}; cached."""
        if self._issues is None or refresh:
            text = self.run(['issue', 'list', '--state', 'all', '--limit', '1000',
                             '--json', 'number,title,state,labels'])
            rows = json.loads(text) if text.strip() else []
            for row in rows:
                row['labels'] = [l['name'] if isinstance(l, dict) else l for l in row.get('labels', [])]
            self._issues = rows
        return self._issues

    def find_issue(self, title, hid=None):
        """The existing issue with exactly this title, else the one whose title starts with '<hid> '."""
        want = ' '.join(title.split())
        for row in self.issues():
            if ' '.join(row['title'].split()) == want:
                return row
        if hid:
            for row in self.issues():
                if row['title'].startswith(hid + ' '):
                    return row
        return None

    def issue_body(self, number):
        text = self.run(['issue', 'view', str(number), '--json', 'body'])
        return json.loads(text).get('body', '') if text.strip() else ''

    def create_issue(self, title, body, labels):
        """gh issue create; returns {number, title, url, labels, state}. Dry-run: number None."""
        args = ['issue', 'create', '--title', title, '--body-file', '-']
        for name in labels:
            args += ['--label', name]
        url = self.run(args, input_text=body).strip()
        number = _number_from_url(url) if url else None
        row = {'number': number, 'title': title, 'state': 'OPEN', 'labels': list(labels), 'url': url}
        if self._issues is not None:
            self._issues.append(row)
        return row

    def edit_issue_body(self, number, body):
        self.run(['issue', 'edit', str(number), '--body-file', '-'], input_text=body)


def _quote(s):
    return s if re.match(r'^[\w./:=@,-]+$', s) else "'" + s.replace("'", "'\\''") + "'"


def _number_from_url(url):
    m = re.search(r'/(\d+)/?$', url)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------- lint hook

def default_linter():
    """lint_text(body) -> None | refusal text, from hindcast.lint when importable (H-205), else None."""
    try:
        from hindcast import lint          # noqa: WPS433  optional module written by the metrics agent
    except Exception:
        return None
    fn = getattr(lint, 'lint_text', None)
    if fn is None:
        return None

    def _lint(body):
        try:
            result = fn(body, kind='issue')
        except Exception as exc:          # lint raises with the offending phrase
            return str(exc)
        if result and not isinstance(result, bool):
            return str(result)            # or returns a list / string of problems
        return None
    return _lint


# ----------------------------------------------------------------------------- high level

def task_body(block):
    deps = '' if block.epic == '' else '\nEpic: %s.' % block.epic
    return block.body + deps + FOOTER


def epic_body(block, tasks, numbers):
    """Intro text + task list. numbers: {hid: issue number or None}; None -> listed by title only."""
    lines = [block.body.strip(), '', '### Tasks', '']
    for t in tasks:
        n = numbers.get(t.hid)
        ref = '#%d ' % n if n else ''
        suffix = '' if n else ' (issue not created yet)'
        lines.append('- [ ] %s%s%s' % (ref, t.issue_title, suffix))
    return '\n'.join(lines).strip('\n') + FOOTER


def ensure_issue(gh, block, body, linter=None):
    """Create the issue for `block` unless one exists. Returns (action, issue_row_or_None, note).

    action: 'exists' (exact title), 'exists-id' (same H-id, different wording), 'refused' (lint),
            'created' (or dry-run: 'would-create').
    """
    found = gh.find_issue(block.issue_title, block.hid)
    if found is not None:
        same = ' '.join(found['title'].split()) == ' '.join(block.issue_title.split())
        return ('exists' if same else 'exists-id', found,
                '' if same else 'existing title: %r' % found['title'])
    if linter is not None:
        problem = linter(body)
        if problem:
            return 'refused', None, problem
    row = gh.create_issue(block.issue_title, body, block.labels)
    return ('would-create' if gh.dry_run else 'created'), row, ''


def create_task_issues(gh, blocks, only=None, linter=None):
    """`issues` command: every task block once. Returns list of (block, action, row, note)."""
    report = []
    for block in select_blocks([b for b in blocks if b.kind == 'task'], only):
        action, row, note = ensure_issue(gh, block, task_body(block), linter)
        report.append((block, action, row, note))
        gh.say('%-13s %-6s %s%s' % (action, '#%d' % row['number'] if row and row.get('number') else '',
                                   block.issue_title, ('   ' + note) if note else ''))
    return report


def sync_epics(gh, blocks, only=None, update=True, linter=None):
    """`epics` command: one issue per epic with the task list of its (existing) task issues."""
    report = []
    for epic in select_blocks([b for b in blocks if b.kind == 'epic'], only):
        tasks = tasks_of(blocks, epic.hid)
        numbers = {}
        for t in tasks:
            row = gh.find_issue(t.issue_title, t.hid)
            numbers[t.hid] = row['number'] if row else None
        body = epic_body(epic, tasks, numbers)
        action, row, note = ensure_issue(gh, epic, body, linter)
        if action in ('exists', 'exists-id') and update and row.get('number'):
            current = gh.issue_body(row['number'])
            if current.strip() != body.strip():
                if linter is not None and linter(body):
                    action, note = 'refused', linter(body)
                else:
                    gh.edit_issue_body(row['number'], body)
                    action = 'would-update' if gh.dry_run else 'updated'
        report.append((epic, action, row, note))
        gh.say('%-13s %-6s %s   (%d tasks, %d with issue numbers)%s'
               % (action, '#%d' % row['number'] if row and row.get('number') else '', epic.issue_title,
                  len(tasks), sum(1 for n in numbers.values() if n), ('   ' + note) if note else ''))
    return report


def main(argv=None):
    p = argparse.ArgumentParser(prog='hc gh', description=__doc__.split('\n\n')[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['labels', 'issues', 'epics'])
    p.add_argument('--from', dest='backlog', default=BACKLOG_DEFAULT, help='BACKLOG.md path')
    p.add_argument('--repo', default=REPO)
    p.add_argument('--dry-run', action='store_true', help='print creating/editing commands instead of running')
    p.add_argument('--only', action='append', default=[], metavar='H-6', help='H-id prefix filter (repeatable)')
    p.add_argument('--update', action='store_true', help='labels: rewrite colour/description of existing labels')
    p.add_argument('--no-update', action='store_true', help='epics: never edit an existing epic body')
    p.add_argument('--no-lint', action='store_true', help='skip hindcast.lint on the bodies')
    p.add_argument('--pause', type=float, default=1.0, help='seconds between creating calls (default 1)')
    a = p.parse_args(argv)

    gh = Gh(repo=a.repo, dry_run=a.dry_run, pause=a.pause)
    linter = None if a.no_lint else default_linter()
    if a.command != 'labels' and linter is None and not a.no_lint:
        gh.say('note: hindcast.lint not available, bodies are posted unlinted')
    try:
        if a.command == 'labels':
            rep = gh.ensure_labels(update=a.update)
            for k in ('created', 'existing', 'updated'):
                if rep[k]:
                    gh.say('%-9s %s' % (k, ', '.join(rep[k])))
            return 0
        blocks = load_backlog(a.backlog)
        if a.command == 'issues':
            rep = create_task_issues(gh, blocks, only=a.only, linter=linter)
        else:
            rep = sync_epics(gh, blocks, only=a.only, update=not a.no_update, linter=linter)
        counts = {}
        for _, action, _, _ in rep:
            counts[action] = counts.get(action, 0) + 1
        gh.say('summary: ' + ', '.join('%s %d' % kv for kv in sorted(counts.items())))
        return 1 if counts.get('refused') else 0
    except GhError as exc:
        gh.say('error: %s' % exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
