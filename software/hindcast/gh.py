"""
gh.py  -  GitHub issue workflow for Hindcast through the `gh` CLI (backlog H-601, H-602; DESIGN.md section 8)

    python -m hindcast.gh labels                                     [--dry-run] [--update]
    python -m hindcast.gh issues --from ../docs/hindcast/BACKLOG.md  [--dry-run] [--only H-6] [--no-lint]
    python -m hindcast.gh epics  --from ../docs/hindcast/BACKLOG.md  [--dry-run] [--only H-6] [--no-update]
    python -m hindcast.gh campaign <study>  [--artifact URL] [--dry-run] [--no-update] [--studies-dir DIR]
    python -m hindcast.gh round    <study>  [--artifact URL] [--dry-run] [--round N]   [--studies-dir DIR]
    (cwd software/; `hc gh <command> ...` is the same parser, wired through register(subparsers))

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
    campaign one issue per study (H-602), title 'Campaign: <study>', labels hindcast + campaign. The body is the
             protocol: a table of the searched parameters (AAPS preference title from prefs_map.json, type, range,
             the phone's value), constraints, day count by role, question, phases, budget, base VDF rows, the
             code/settings stamps, the artifact link (--artifact or status.json's artifact_url) and the honesty
             statement. No medical data: numbers in insulin units, glucose values, loop counts and transcript
             excerpts stay in the private artifact. Idempotent: found by title; an existing issue's body is
             rewritten only when the rendered body differs (--no-update keeps it).
    round    at most one comment per round on the campaign issue (H-602): phase, trials done / planned / failed /
             blocked, the round's variants with their verdict words (`recommended` is rendered 'recommended for
             review'), dropped parameters, the review order, the run state and the artifact link. Reads
             round.json (the ranking step's file) and status.json / STATE.json; without a round.json the comment
             covers STATE.json's round number (--round N names it). A comment is identified by a hidden marker
             `<!-- hindcast-round: <study> round <n> -->`, so a second call for the same round posts nothing
             (action 'exists'). The campaign issue is created first when it does not exist yet.

Every call goes through Gh.run(), which prefixes `gh`, appends `-R <repo>` to label/issue commands and in
--dry-run executes read-only commands (list, view) but only prints the mutating ones (create, edit,
comment). Tests pass a `runner` callable instead of subprocess. The repo is named explicitly because `gh` in
this clone resolves the default repository to the upstream fork (ga-zelle), not to markusbaumg/APS-what-if.

Privacy (DESIGN.md section 8, the repository is public): bodies may name preference keys and values,
never glucose values, insulin per day, loop counts per day, transcripts or prediction arrays. When
`hindcast.lint` exists, every body passes `lint.lint_text(body, kind='issue')` first and a refused body is
reported and NOT posted (--no-lint skips the check; the backlog text itself is already public in the repo).
Free text that other modules wrote (a candidate's `why`, a dead parameter's `why`, a stop reason, a round
note) is linted on its own first and replaced by 'see the artifact' when it fails, so a round comment stays
postable; the whole body is linted again before posting. A study with `private_repo: true` relaxes the lint
to the transcript rules only (`study_linter`).

Exit codes of the run_* functions: 0 done (or nothing to do), 1 a body was refused by the lint or `gh` failed,
3 the study does not exist / cannot be read (EXIT_REFUSED, like the other hc commands).

Not covered here (H-603): proposal/finding issues and the label read-back. They should reuse Gh,
ensure_labels(), HONESTY and `study_linter()` from this module.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts                                                              # noqa: E402
from hindcast import study as st                                                            # noqa: E402

REPO = 'markusbaumg/APS-what-if'
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 3
PREFS_MAP_PATH = os.path.join(HINDCAST_DIR, 'prefs_map.json')
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

    def comments(self, number):
        """The bodies of the comments on issue `number`, oldest first (read-only, also in dry-run)."""
        text = self.run(['issue', 'view', str(number), '--json', 'comments'])
        data = json.loads(text) if text.strip() else {}
        return [c.get('body', '') if isinstance(c, dict) else str(c) for c in data.get('comments', [])]

    def comment_issue(self, number, body):
        """gh issue comment; dry-run prints instead. Returns the comment url ('' in dry-run)."""
        return self.run(['issue', 'comment', str(number), '--body-file', '-'], input_text=body).strip()

    def issue_url(self, number):
        return 'https://github.com/%s/issues/%s' % (self.repo, number) if number else ''


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


# ----------------------------------------------------------------------------- campaign issue and round comments (H-602)

CAMPAIGN_LABELS = ('hindcast', 'campaign')
QUESTION_TEXT = {                                        # DESIGN.md section 4.5, a declared study setting, no value judgement
    'explore': 'explore: nothing is ranked, the leaderboard is in review order',
    'earlier': 'earlier: the user is looking for more insulin in rising-above-target loops and none elsewhere',
    'gentler': 'gentler: the user is looking for less insulin in rising loops',
}
SEE_ARTIFACT = 'see the artifact'                        # replaces free text that fails the lint on its own
ROUND_MAX_VARIANTS = 10                                  # review-order lines per comment
CAMPAIGN_FOOTER = (
    "\n\n---\n"
    "Privacy: this repository is public, so this issue and its comments name preference keys and values, study ids, "
    "day counts, trial counts, phase and verdict words and links only; glucose values, insulin amounts, loop counts, "
    "transcript excerpts and prediction arrays stay in the private artifact. Honesty statement: docs/hindcast/DESIGN.md "
    "section 3. Rounds are reported as comments below, at most one per round."
)

_prefs_map_cache = None


def campaign_title(study_name):
    """The exact issue title of a study's campaign issue (the idempotence key)."""
    return 'Campaign: %s' % study_name


def campaign_marker(study_name):
    return '<!-- hindcast-campaign: %s -->' % study_name


def round_marker(study_name, round_no):
    """Hidden first line of a round comment; `post_round` posts nothing when a comment carries it already."""
    return '<!-- hindcast-round: %s round %s -->' % (study_name, round_no)


def _pref_title(name):
    """AAPS preference title for a VDF key ('profile autoISF_max' -> 'autoISF max') from prefs_map.json, or ''."""
    global _prefs_map_cache
    if _prefs_map_cache is None:
        try:
            with open(PREFS_MAP_PATH, encoding='utf-8') as fh:
                _prefs_map_cache = json.load(fh).get('keys', {})
        except (OSError, ValueError):
            _prefs_map_cache = {}
    key = name.split(' ', 1)[1] if ' ' in name else name
    entry = _prefs_map_cache.get(key) or {}
    return entry.get('title') or ''


def _fmt(value):
    """Numbers without a trailing .0 noise, bools lower-case (the VDF spelling is Python's; the table is prose)."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value == int(value) and abs(value) < 1e6:
        return str(int(value))
    return str(value)


def _param_range(param):
    if param.type == 'bool':
        return 'false / true'
    if param.type == 'choice':
        return ', '.join(_fmt(v) for v in (param.choices or []))
    return '%s .. %s step %s' % (_fmt(param.low), _fmt(param.high), _fmt(param.step))


def _phase_text(phase):
    bits = []
    if phase.points:
        bits.append('%d levels' % phase.points)
    if phase.n:
        bits.append('%d points' % phase.n)
    if phase.rungs:
        bits.append('rungs ' + '/'.join(str(r) for r in phase.rungs))
    if phase.sampler:
        bits.append(phase.sampler)
    if phase.top:
        bits.append('top %d' % phase.top)
    if phase.days:
        bits.append(phase.days)
    if phase.grid:
        bits.append('%d parameters' % len(phase.grid))
    text = phase.kind + (' (%s)' % ', '.join(bits) if bits else '')
    return text if phase.state == 'pending' else '%s [%s]' % (text, phase.state)


def _artifact_line(artifact_url):
    return 'Artifact: %s' % artifact_url if artifact_url else 'Artifact: not published yet (the numbers live there)'


def campaign_body(study, artifact_url=None, linter=None):
    """The campaign issue body for a contracts.Study: protocol table, days, question, phases, budget, stamps,
    artifact link, honesty statement and the privacy footer. Deterministic for a given study + url + linter, so an
    unchanged study renders byte-identical and `ensure_campaign` leaves the issue alone. With a linter, each free
    `study.notes` entry and each user-typed day tag that fails it on its own is withheld ('see the artifact',
    'n tags withheld'); the rest of the body is
    generated text that passes the issue lint by construction."""
    roles = {}
    for d in study.days:
        roles[d.role] = roles.get(d.role, 0) + 1
    role_text = ', '.join('%d %s' % (roles[r], 'hold-out' if r == 'holdout' else r)
                          for r in ('search', 'holdout', 'quarantined') if r in roles)
    days_sorted = sorted(d.day for d in study.days)
    tags, tags_withheld = _day_tag_items(study.days, linter)
    lines = [campaign_marker(study.name),
             'Hindcast study `%s`: the protocol of one replay campaign over recorded days. One comment per round at '
             'most follows below; the final summary closes the issue.' % study.name,
             '',
             'Created: %s' % study.created,
             'Question: %s' % QUESTION_TEXT.get(study.question, study.question),
             '',
             '### Protocol',
             '',
             '| parameter | AAPS preference | type | range | phone |',
             '|---|---|---|---|---|']
    for name, p in study.space.items():
        lines.append('| `%s` | %s | %s | %s | %s |' % (name, _pref_title(name) or '-', p.type, _param_range(p),
                                                       _fmt(p.phone) if p.phone is not None else '-'))
    lines.append('')
    if study.constraints:
        lines.append('Constraints: ' + '; '.join('`%s`' % ' '.join(c) for c in study.constraints))
    lines.append('Days: %d (%s), %s .. %s' % (len(study.days), role_text, days_sorted[0], days_sorted[-1])
                 if study.days else 'Days: none')
    if tags or tags_withheld:
        lines.append('Day tags (typed by the user): ' + '; '.join(
            tags + (['%d tag%s withheld (%s)' % (tags_withheld, '' if tags_withheld == 1 else 's', SEE_ARTIFACT)]
                    if tags_withheld else [])))
    lines.append('Phases: ' + (', '.join(_phase_text(p) for p in study.phases) if study.phases else 'none'))
    lines.append('Budget: %s h, %d worker(s); a sentinel replay every %d trials' % (_fmt(study.budget.hours),
                                                                                    study.budget.workers, study.sentinel_every))
    if study.base_vdf_lines:
        lines.append('Base VDF rows every variant carries: ' + '; '.join('`%s`' % l.strip() for l in study.base_vdf_lines))
    lines.append('Stamps: code_sha %s, settings_sha %s, objective v%d, contract v%d' % (
        contracts.short(study.code_sha) if study.code_sha else '-', contracts.short(study.settings_sha) if study.settings_sha else '-',
        study.objective_version, study.contract_version))
    lines.append(_artifact_line(artifact_url))
    lines.append('Not testable with this method: ' + ', '.join(contracts.NOT_TESTABLE) + '.')
    lines += _note_lines(study.notes, linter, 'Note: ')
    lines += ['', HONESTY]
    return '\n'.join(lines) + CAMPAIGN_FOOTER


def _safe_text(text, linter, fallback=SEE_ARTIFACT):
    """`text` when it passes the linter on its own, else `fallback` (free text another module wrote)."""
    text = ' '.join(str(text or '').split())
    if not text:
        return fallback
    if linter is not None and linter(text):
        return fallback
    return text


def _day_tag_items(days, linter):
    """(['<day>: tag, tag', ...], n_withheld). Tags are typed by the user, so with a linter each one is linted on
    its own like a note; a refused tag is withheld (counted, never shown) and a day left without tags is omitted."""
    items, withheld = [], 0
    for d in days:
        kept = []
        for tag in d.tags or ():
            if not str(tag or '').strip():
                continue
            text = _safe_text(tag, linter, fallback=None)
            if text is None:
                withheld += 1
            else:
                kept.append(text)
        if kept:
            items.append('%s: %s' % (d.day, ', '.join(kept)))
    return items, withheld


def _note_lines(notes, linter, prefix):
    """One line per free-text note; the notes the linter refuses collapse into one 'see the artifact' line."""
    out, withheld = [], 0
    for note in notes:
        if not str(note or '').strip():
            continue
        text = _safe_text(note, linter, fallback=None)
        if text is None:
            withheld += 1
        else:
            out.append(prefix + text)
    if withheld:
        out.append(prefix + '%d more note(s): %s' % (withheld, SEE_ARTIFACT))
    return out


def round_body(study, status, rnd, round_no, artifact_url=None, linter=None):
    """One round comment: phase, trials, variants by verdict word, dropped parameters, review order, run state,
    artifact link. `rnd` (contracts.Round) may be None (then STATE.json's round number is reported alone)."""
    phase = rnd.phase if rnd is not None else status.phase
    lines = [round_marker(study.name, round_no),
             '### Round %s - phase %s' % (round_no, phase),
             '',
             '- trials: %d done, %d planned, %d failed, %d blocked%s' % (
                 status.trials_done, status.trials_planned, status.trials_failed, status.trials_blocked,
                 (' (%d in this round)' % rnd.trials) if rnd is not None and rnd.trials else '')]
    if rnd is not None:
        counts = {}
        for c in rnd.variants:
            label = st.verdict_label(c.verdict)
            counts[label] = counts.get(label, 0) + 1
        lines.append('- variants in this round: %d%s' % (
            len(rnd.variants), (' (' + ', '.join('%s %d' % (k, v) for k, v in counts.items()) + ')') if counts else ''))
        if rnd.dead_parameters:
            lines.append('- dropped parameters (not testable on these days): ' + '; '.join(
                '`%s` - %s' % (d.key, _safe_text(d.why, linter)) for d in rnd.dead_parameters))
        else:
            lines.append('- dropped parameters: none')
        by_vhash = {c.vhash: c for c in rnd.variants}
        order = [by_vhash[v] for v in rnd.front if v in by_vhash] or [c for c in rnd.variants if c.verdict != 'base']
        if order:
            lines.append('- review order (not a ranking by outcome):')
            for i, c in enumerate(order[:ROUND_MAX_VARIANTS], 1):
                lines.append('  %d. `%s` [%s] - %s' % (c.rank if c.rank is not None else i, c.variant,
                                                      st.verdict_label(c.verdict), _safe_text(c.why, linter)))
            if len(order) > ROUND_MAX_VARIANTS:
                lines.append('  ... %d more in the artifact' % (len(order) - ROUND_MAX_VARIANTS))
        lines += _note_lines(rnd.notes, linter, '- note: ')
    else:
        lines.append('- no round.json yet: the ranking step has not written this round, the counts above are from STATE.json')
    if status.dead_parameters and rnd is None:
        lines.append('- dropped parameters so far: ' + ', '.join('`%s`' % k for k in status.dead_parameters))
    if status.floor.get('agreement_pct') is not None:
        lines.append('- base replay vs phone, worst day: agreement %s %%' % _fmt(status.floor['agreement_pct']))
    if status.running:
        lines.append('- run: active')
    elif status.exit_code is not None:
        lines.append('- run: stopped, exit %d (%s)' % (status.exit_code, _safe_text(status.stop_reason, linter, 'no reason recorded')))
    else:
        lines.append('- run: not active')
    if status.stale_context:
        lines.append('- stale context: ' + '; '.join(_safe_text(s, linter) for s in status.stale_context))
    lines.append('- heartbeat: %s' % status.heartbeat)
    lines.append('- ' + _artifact_line(artifact_url))
    lines += ['', 'Insulin deltas, per-loop series and predictions are in the private artifact, not here.']
    return '\n'.join(lines)


def study_linter(study=None, no_lint=False):
    """The body linter for a study: `default_linter()` (kind 'issue'); with study.private_repo only the transcript
    privacy rules (Microbolusing, predBGs) stay on, DESIGN.md section 8. None when lint is unavailable or no_lint."""
    if no_lint:
        return None
    base = default_linter()
    if base is None or study is None or not getattr(study, 'private_repo', False):
        return base
    try:
        from hindcast import lint
        relaxed = [e['name'] for e in lint.load_vocabulary()['privacy'] if e['name'] not in ('Microbolusing', 'predBGs')]
    except Exception:
        return base

    def _lint(body):
        try:
            lint.lint_text(body, kind='issue', ignore=relaxed)
        except Exception as exc:
            return str(exc)
        return None
    return _lint


def load_study_dir(name_or_path, studies_dir=st.STUDIES_DIR):
    """(StudyDir, Study) for a study name or directory; StudyError when it does not exist."""
    sd = st.StudyDir(st.resolve_study_dir(name_or_path, studies_dir))
    sd.require()
    try:
        study = contracts.Study.load(sd.study_json, strict=False)
    except (OSError, ValueError, contracts.ContractError) as e:
        raise st.StudyError('%s: %s' % (sd.study_json, e)) from None
    return sd, study


def _status_of(sd):
    """build_status(sd) for the counts, plus the artifact_url of the status.json on disk (build_status never sets it)."""
    status = st.build_status(sd)
    try:
        on_disk = contracts.Status.load(sd.status_path, strict=False)
        status.artifact_url = on_disk.artifact_url
        status.proposals = on_disk.proposals
    except (OSError, ValueError, contracts.ContractError):
        pass
    return status


def ensure_campaign(gh, sd, study=None, artifact_url=None, update=True, linter=None):
    """Create or refresh the campaign issue of a study. Returns (action, issue_row_or_None, note); action is one of
    'created', 'would-create', 'exists', 'updated', 'would-update', 'refused' (note = the lint finding)."""
    if study is None:
        study = contracts.Study.load(sd.study_json, strict=False)
    if artifact_url is None:
        artifact_url = _status_of(sd).artifact_url if os.path.isfile(sd.status_path) else None
    title = campaign_title(study.name)
    body = campaign_body(study, artifact_url, linter)
    problem = linter(body) if linter is not None else None
    found = gh.find_issue(title)
    if found is None:
        if problem:
            return 'refused', None, problem
        gh.ensure_labels({k: LABELS[k] for k in CAMPAIGN_LABELS})
        row = gh.create_issue(title, body, list(CAMPAIGN_LABELS))
        return ('would-create' if gh.dry_run else 'created'), row, ''
    if update and found.get('number'):
        current = gh.issue_body(found['number'])
        if current.strip() != body.strip():
            if problem:
                return 'refused', found, problem
            gh.edit_issue_body(found['number'], body)
            return ('would-update' if gh.dry_run else 'updated'), found, ''
    return 'exists', found, ''


def post_round(gh, sd, study=None, artifact_url=None, round_no=None, linter=None, update_campaign=True):
    """Post this round's comment on the study's campaign issue unless one exists. Returns (action, row, note):
    'posted', 'would-post', 'exists' (the marker is on a comment already), 'no-round' (no round.json and no
    STATE.json round, nothing to report), 'refused' (lint). The campaign issue is created when missing."""
    if study is None:
        study = contracts.Study.load(sd.study_json, strict=False)
    status = _status_of(sd)
    if artifact_url is None:
        artifact_url = status.artifact_url
    rnd = sd.read_round()
    if round_no is None:
        round_no = rnd.round if rnd is not None else status.round
    if round_no is None:
        return 'no-round', None, 'no round.json and no round number in STATE.json'
    if rnd is not None and rnd.round != round_no:
        rnd = None                                       # round.json belongs to another round: report the counts only
    body = round_body(study, status, rnd, round_no, artifact_url, linter)
    if linter is not None:
        problem = linter(body)
        if problem:
            return 'refused', None, problem
    action, row, note = ensure_campaign(gh, sd, study, artifact_url, update=update_campaign, linter=linter)
    if action == 'refused':
        return 'refused', row, 'campaign body: ' + note
    number = row.get('number') if row else None
    marker = round_marker(study.name, round_no)
    if number and any(marker in c for c in gh.comments(number)):
        return 'exists', row, 'round %s already has its comment' % round_no
    if number or gh.dry_run:
        gh.comment_issue(number if number else '<new>', body)
        return ('would-post' if gh.dry_run else 'posted'), row, ''
    return 'refused', row, 'campaign issue has no number'


# ----------------------------------------------------------------------------- CLI: run_* and the parser (hc gh ...)

def _gh_from_args(args):
    return Gh(repo=getattr(args, 'repo', REPO), dry_run=getattr(args, 'dry_run', False),
              pause=getattr(args, 'pause', 1.0))


def _summary(gh, report):
    counts = {}
    for _, action, _, _ in report:
        counts[action] = counts.get(action, 0) + 1
    gh.say('summary: ' + ', '.join('%s %d' % kv for kv in sorted(counts.items())))
    return EXIT_FAILED if counts.get('refused') else EXIT_OK


def run_labels(args):
    gh = _gh_from_args(args)
    try:
        rep = gh.ensure_labels(update=getattr(args, 'update', False))
    except GhError as exc:
        gh.say('error: %s' % exc)
        return EXIT_FAILED
    for k in ('created', 'existing', 'updated'):
        if rep[k]:
            gh.say('%-9s %s' % (k, ', '.join(rep[k])))
    return EXIT_OK


def _backlog_linter(gh, args):
    linter = None if args.no_lint else default_linter()
    if linter is None and not args.no_lint:
        gh.say('note: hindcast.lint not available, bodies are posted unlinted')
    return linter


def run_issues(args):
    gh = _gh_from_args(args)
    try:
        blocks = load_backlog(args.backlog)
        rep = create_task_issues(gh, blocks, only=args.only, linter=_backlog_linter(gh, args))
    except GhError as exc:
        gh.say('error: %s' % exc)
        return EXIT_FAILED
    return _summary(gh, rep)


def run_epics(args):
    gh = _gh_from_args(args)
    try:
        blocks = load_backlog(args.backlog)
        rep = sync_epics(gh, blocks, only=args.only, update=not args.no_update, linter=_backlog_linter(gh, args))
    except GhError as exc:
        gh.say('error: %s' % exc)
        return EXIT_FAILED
    return _summary(gh, rep)


def _report_study_action(gh, verb, action, row, note):
    number = '#%d' % row['number'] if row and row.get('number') else ''
    url = gh.issue_url(row['number']) if row and row.get('number') else ''
    gh.say('%-13s %-6s %s%s%s' % (action, number, verb, ('   ' + url) if url else '', ('   ' + note) if note else ''))


def run_campaign(args):
    """`hc gh campaign <study>`: 0 done, 1 refused by the lint / gh failed, 3 no such study."""
    gh = _gh_from_args(args)
    try:
        sd, study = load_study_dir(args.study, getattr(args, 'studies_dir', st.STUDIES_DIR))
    except st.StudyError as exc:
        gh.say('refused (exit %d): %s' % (EXIT_REFUSED, exc))
        return EXIT_REFUSED
    linter = study_linter(study, no_lint=getattr(args, 'no_lint', False))
    if linter is None and not getattr(args, 'no_lint', False):
        gh.say('note: hindcast.lint not available, the body is posted unlinted')
    try:
        action, row, note = ensure_campaign(gh, sd, study, artifact_url=getattr(args, 'artifact', None),
                                            update=not getattr(args, 'no_update', False), linter=linter)
    except GhError as exc:
        gh.say('error: %s' % exc)
        return EXIT_FAILED
    _report_study_action(gh, campaign_title(study.name), action, row, note)
    return EXIT_FAILED if action == 'refused' else EXIT_OK


def run_round(args):
    """`hc gh round <study>`: 0 posted / already there / nothing to report, 1 refused or gh failed, 3 no such study."""
    gh = _gh_from_args(args)
    try:
        sd, study = load_study_dir(args.study, getattr(args, 'studies_dir', st.STUDIES_DIR))
    except st.StudyError as exc:
        gh.say('refused (exit %d): %s' % (EXIT_REFUSED, exc))
        return EXIT_REFUSED
    linter = study_linter(study, no_lint=getattr(args, 'no_lint', False))
    if linter is None and not getattr(args, 'no_lint', False):
        gh.say('note: hindcast.lint not available, the comment is posted unlinted')
    try:
        action, row, note = post_round(gh, sd, study, artifact_url=getattr(args, 'artifact', None),
                                       round_no=getattr(args, 'round', None), linter=linter,
                                       update_campaign=not getattr(args, 'no_update', False))
    except GhError as exc:
        gh.say('error: %s' % exc)
        return EXIT_FAILED
    _report_study_action(gh, 'round comment on ' + campaign_title(study.name), action, row, note)
    return EXIT_FAILED if action == 'refused' else EXIT_OK


def _add_common(p):
    p.add_argument('--repo', default=REPO)
    p.add_argument('--dry-run', action='store_true', help='print creating/editing/commenting commands instead of running')
    p.add_argument('--pause', type=float, default=1.0, help='seconds between creating calls (default 1)')


def _add_backlog_options(p):
    p.add_argument('--from', dest='backlog', default=BACKLOG_DEFAULT, help='BACKLOG.md path')
    p.add_argument('--only', action='append', default=[], metavar='H-6', help='H-id prefix filter (repeatable)')
    p.add_argument('--no-lint', action='store_true', help='skip hindcast.lint on the bodies')


def _add_study_options(p):
    p.add_argument('study', help='study name under --studies-dir, or a study directory')
    p.add_argument('--studies-dir', default=st.STUDIES_DIR, help='where studies/<name>/ live (default software/studies)')
    p.add_argument('--artifact', default=None, metavar='URL', help='the private artifact link (default: status.json artifact_url)')
    p.add_argument('--no-lint', action='store_true', help='skip hindcast.lint on the body (not for the public repo)')


def add_subcommands(sub):
    """Add labels|issues|epics|campaign|round to an argparse subparsers object; each parser's default `func` is the
    matching run_* function (`register` and `main` both build on this)."""
    a = sub.add_parser('labels', help='create the missing labels (--update rewrites colour/description)')
    a.add_argument('--update', action='store_true', help='rewrite colour/description of existing labels')
    _add_common(a)
    a.set_defaults(func=run_labels)

    b = sub.add_parser('issues', help='one task issue per BACKLOG.md block, once')
    _add_backlog_options(b)
    _add_common(b)
    b.set_defaults(func=run_issues)

    c = sub.add_parser('epics', help='one epic issue per backlog section with its task list')
    _add_backlog_options(c)
    c.add_argument('--no-update', action='store_true', help='never edit an existing epic body')
    _add_common(c)
    c.set_defaults(func=run_epics)

    d = sub.add_parser('campaign', help="open or refresh the study's campaign issue (protocol table, no medical data)")
    _add_study_options(d)
    d.add_argument('--no-update', action='store_true', help='never edit an existing campaign body')
    _add_common(d)
    d.set_defaults(func=run_campaign)

    e = sub.add_parser('round', help='post this round\'s comment on the campaign issue (at most one per round)')
    _add_study_options(e)
    e.add_argument('--round', type=int, default=None, help='round number (default: round.json, else STATE.json)')
    e.add_argument('--no-update', action='store_true', help='never edit an existing campaign body on the way')
    _add_common(e)
    e.set_defaults(func=run_round)
    return sub


def register(subparsers):
    """Add `gh` with its labels|issues|epics|campaign|round subcommands to an argparse subparsers object (cli.py calls
    this); each subcommand's default `func(args) -> int` is the matching run_* function. Returns the `gh` parser."""
    p = subparsers.add_parser('gh', help='GitHub labels, backlog issues, campaign issue and round comments',
                              description=__doc__.split('\n\n')[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='gh_command', metavar='labels|issues|epics|campaign|round')
    sub.required = True
    add_subcommands(sub)
    return p


def build_parser(prog='hc gh'):
    """The standalone parser of `python -m hindcast.gh` (same subcommands as `hc gh`)."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.split('\n\n')[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='gh_command', metavar='labels|issues|epics|campaign|round')
    sub.required = True
    add_subcommands(sub)
    return parser


def main(argv=None):
    """Standalone entry: `python -m hindcast.gh <command> ...`; returns the exit code of the run_* function."""
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    sys.exit(main())
