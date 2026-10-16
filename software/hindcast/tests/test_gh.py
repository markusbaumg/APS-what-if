"""Tests for hindcast.gh: backlog parsing, idempotent labels/issues/epics against a mocked gh, dry-run.

Nothing here calls the real `gh`: every Gh instance gets a FakeRunner. The one test that touches the
CLI (`needs_gh`) only runs `gh --version`.
"""
import json
import subprocess

import pytest

from hindcast import gh as ghmod
from hindcast.gh import Block, Gh, GhError, LABELS, REPO, parse_backlog


class FakeRunner:
    """Stands in for subprocess: answers list/view commands from its state, records creates/edits."""

    def __init__(self, labels=(), issues=()):
        self.labels = list(labels)
        self.issues = [dict(i) for i in issues]
        self.calls = []
        self.next_number = 100 + len(self.issues)

    def __call__(self, cmd, input_text=None):
        self.calls.append((cmd, input_text))
        assert cmd[0] == 'gh'
        sub = tuple(cmd[1:3])
        if cmd[1] in ('issue', 'label'):
            assert '-R' in cmd and cmd[cmd.index('-R') + 1] == REPO, cmd
        if sub == ('label', 'list'):
            return json.dumps([{'name': n, 'color': '000000', 'description': ''} for n in self.labels])
        if sub == ('label', 'create'):
            self.labels.append(cmd[3])
            return ''
        if sub == ('issue', 'list'):
            return json.dumps([{'number': i['number'], 'title': i['title'], 'state': i.get('state', 'OPEN'),
                                'labels': [{'name': l} for l in i.get('labels', [])]} for i in self.issues])
        if sub == ('issue', 'view'):
            n = int(cmd[3])
            return json.dumps({'body': next(i['body'] for i in self.issues if i['number'] == n)})
        if sub == ('issue', 'create'):
            self.next_number += 1
            title = cmd[cmd.index('--title') + 1]
            labels = [cmd[i + 1] for i, c in enumerate(cmd) if c == '--label']
            self.issues.append({'number': self.next_number, 'title': title, 'labels': labels,
                                'body': input_text, 'state': 'OPEN'})
            return 'https://github.com/%s/issues/%d\n' % (REPO, self.next_number)
        if sub == ('issue', 'edit'):
            n = int(cmd[3])
            next(i for i in self.issues if i['number'] == n)['body'] = input_text
            return ''
        raise AssertionError('unexpected gh command %r' % (cmd,))

    def created(self):
        return [c for c, _ in self.calls if c[1:3] == ['issue', 'create']]

    def edited(self):
        return [c for c, _ in self.calls if c[1:3] == ['issue', 'edit']]


MINI = """# head

## E6 — GitHub workflow (epic `H-600`) · labels: hindcast, epic, mvp

Intro line one.

### H-601 · hc gh labels|epics: create labels · **MVP**
labels: hindcast, mvp
Body: `gh.py` via the `gh` CLI; idempotent label creation.
Depends on: nothing.

### H-603 · Proposal and finding issues
labels: hindcast
Body:
- first bullet
- second bullet
Depends on: H-602.

---

## E7 — Algorithm fidelity (epic `H-700`) · labels: hindcast, epic, algorithm-fidelity

### H-701 · SMB interval tolerance · **MVP** (raises the floor)
labels: algorithm-fidelity, blocked-core
Body: text.

---

## Later (not scheduled)

- **H-L1** not a block.

## Dependency order

```
day 1   H-601
```
"""


@pytest.fixture(scope='module')
def blocks():
    return parse_backlog(MINI)


# ----------------------------------------------------------------------------- parsing

def test_parse_mini_blocks(blocks):
    kinds = [(b.kind, b.hid) for b in blocks]
    assert kinds == [('epic', 'H-600'), ('task', 'H-601'), ('task', 'H-603'), ('epic', 'H-700'), ('task', 'H-701')]
    epic = blocks[0]
    assert epic.title == 'Epic E6: GitHub workflow'
    assert epic.issue_title == 'H-600 Epic E6: GitHub workflow'
    assert epic.labels == ['hindcast', 'epic', 'mvp'] and epic.mvp
    assert epic.body == 'Intro line one.'
    assert epic.tasks == ['H-601', 'H-603']
    t = blocks[1]
    assert t.issue_title == 'H-601 hc gh labels|epics: create labels'
    assert t.mvp and t.labels == ['hindcast', 'mvp'] and t.epic == 'H-600'
    assert t.body == '`gh.py` via the `gh` CLI; idempotent label creation.\nDepends on: nothing.'
    t3 = blocks[2]
    assert not t3.mvp and t3.body.startswith('- first bullet\n- second bullet')
    t7 = blocks[4]
    assert t7.title == 'SMB interval tolerance' and t7.mvp
    assert t7.labels == ['algorithm-fidelity', 'blocked-core'] and t7.epic == 'H-700'
    assert blocks[3].body == ''                     # E7 has no intro in MINI
    assert not any(b.hid.startswith('H-L') for b in blocks)


def test_parse_rejects_duplicates_and_orphans():
    with pytest.raises(GhError, match='duplicate'):
        parse_backlog(MINI + '\n## E8 — X (epic `H-601`) · labels: hindcast\n')
    with pytest.raises(GhError, match='outside an epic'):
        parse_backlog('### H-999 · lonely\nlabels: hindcast\nBody: x\n')
    with pytest.raises(GhError, match='no labels'):
        parse_backlog('## E1 — A (epic `H-100`) · labels: epic\n### H-101 · t\nBody: x\n')
    with pytest.raises(GhError, match='no Body'):
        parse_backlog('## E1 — A (epic `H-100`) · labels: epic\n### H-101 · t\nlabels: hindcast\n')


def test_parse_real_backlog(backlog_md):
    blocks = ghmod.load_backlog(backlog_md)
    epics = [b for b in blocks if b.kind == 'epic']
    tasks = [b for b in blocks if b.kind == 'task']
    assert [e.hid for e in epics] == ['H-%d00' % i for i in range(10)]
    assert len(tasks) == 61
    assert len({b.issue_title for b in blocks}) == len(blocks)
    assert sum(len(e.tasks) for e in epics) == len(tasks)
    for t in tasks:
        assert t.labels and t.body and t.epic
        assert ' · ' not in t.title and '**' not in t.title
        assert not t.body.startswith('Body:')
    by_id = {b.hid: b for b in blocks}
    assert by_id['H-601'].issue_title == ('H-601 hc gh labels|epics: create labels and the epic issues '
                                          'from this backlog')
    assert by_id['H-903'].title == 'pytest scaffold, requirements-dev.txt, fixture policy'
    assert by_id['H-000'].issue_title == 'H-000 Epic E0: Contracts and fixtures'
    assert by_id['H-701'].mvp and not by_id['H-702'].mvp
    assert by_id['H-800'].labels == ['hindcast', 'epic', 'blocked-core']
    unknown = {l for b in blocks for l in b.labels} - set(LABELS) - {'bug', 'documentation', 'enhancement'}
    assert not unknown, unknown


def test_select_blocks(blocks):
    assert [b.hid for b in ghmod.select_blocks(blocks, ['H-6'])] == ['H-600', 'H-601', 'H-603']
    assert [b.hid for b in ghmod.select_blocks(blocks, ['H-701', 'H-603'])] == ['H-603', 'H-701']
    assert ghmod.select_blocks(blocks, []) == list(blocks)


# ----------------------------------------------------------------------------- labels

def test_ensure_labels_creates_only_missing():
    fake = FakeRunner(labels=['hindcast', 'bug', 'mvp'])
    g = Gh(runner=fake)
    rep = g.ensure_labels()
    assert set(rep['existing']) == {'hindcast', 'mvp'}
    assert set(rep['created']) == set(LABELS) - {'hindcast', 'mvp'}
    assert rep['updated'] == []
    creates = [c for c, _ in fake.calls if c[1:3] == ['label', 'create']]
    assert len(creates) == len(LABELS) - 2
    assert all('--force' not in c for c in creates)
    # second call: nothing to do
    rep2 = Gh(runner=fake).ensure_labels()
    assert rep2['created'] == [] and set(rep2['existing']) == set(LABELS)


def test_ensure_labels_update_uses_force():
    fake = FakeRunner(labels=list(LABELS))
    rep = Gh(runner=fake).ensure_labels(update=True)
    assert set(rep['updated']) == set(LABELS) and rep['created'] == []
    assert all('--force' in c for c, _ in fake.calls if c[1:3] == ['label', 'create'])


def test_labels_dry_run_executes_nothing_mutating(capsys):
    fake = FakeRunner(labels=['hindcast'])
    g = Gh(runner=fake, dry_run=True)
    rep = g.ensure_labels()
    assert 'epic' in rep['created']
    assert [c[1:3] for c, _ in fake.calls] == [['label', 'list']]          # only the read
    assert fake.labels == ['hindcast']
    assert 'DRY-RUN' in capsys.readouterr().out
    assert sum(1 for _, _, executed in g.commands if not executed) == len(LABELS) - 1


# ----------------------------------------------------------------------------- issues

def test_create_task_issues_once_and_dedupe(blocks):
    fake = FakeRunner(issues=[
        {'number': 7, 'title': 'H-601 hc gh labels|epics: create labels', 'labels': ['hindcast'], 'body': 'old',
         'state': 'CLOSED'},                                   # exact title, closed: still a match
        {'number': 8, 'title': 'H-701 SMB interval tolerance 12 s -> 6 s', 'labels': [], 'body': 'x'},
    ])                                                         # same id, reworded: still a match
    g = Gh(runner=fake)
    rep = ghmod.create_task_issues(g, blocks)
    actions = {b.hid: a for b, a, _, _ in rep}
    assert actions == {'H-601': 'exists', 'H-603': 'created', 'H-701': 'exists-id'}
    assert [b.hid for b, a, _, n in rep if n][0] == 'H-701'
    created = fake.created()
    assert len(created) == 1
    cmd = created[0]
    assert cmd[cmd.index('--title') + 1] == 'H-603 Proposal and finding issues'
    assert cmd[cmd.index('--label') + 1] == 'hindcast'
    assert '--body-file' in cmd and cmd[cmd.index('--body-file') + 1] == '-'
    body = fake.issues[-1]['body']
    assert body.startswith('- first bullet\n- second bullet\nDepends on: H-602.')
    assert 'Epic: H-600.' in body and 'Honesty statement' in body
    assert fake.issues[-1]['number'] == 103
    # a second run against the same state creates nothing
    fake2 = FakeRunner(issues=fake.issues)
    rep2 = ghmod.create_task_issues(Gh(runner=fake2), blocks)
    assert {a for _, a, _, _ in rep2} <= {'exists', 'exists-id'}
    assert fake2.created() == []


def test_create_task_issues_dry_run_creates_nothing(blocks, capsys):
    fake = FakeRunner()
    g = Gh(runner=fake, dry_run=True)
    rep = ghmod.create_task_issues(g, blocks, only=['H-6'])
    assert [a for _, a, _, _ in rep] == ['would-create', 'would-create']
    assert fake.created() == [] and fake.issues == []
    assert [c[1:3] for c, _ in fake.calls] == [['issue', 'list']]
    out = capsys.readouterr().out
    assert out.count('DRY-RUN') == 2 and '[body' in out
    assert all(r is not None and r['number'] is None for _, _, r, _ in rep)


def test_linter_refuses_body(blocks):
    fake = FakeRunner()
    refused = lambda body: 'contains "U/day"' if 'idempotent' in body else None  # noqa: E731
    rep = ghmod.create_task_issues(Gh(runner=fake), blocks, linter=refused)
    actions = {b.hid: (a, n) for b, a, _, n in rep}
    assert actions['H-601'] == ('refused', 'contains "U/day"')
    assert actions['H-603'][0] == 'created' and actions['H-701'][0] == 'created'
    assert len(fake.created()) == 2


def test_default_linter_without_lint_module(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == 'hindcast.lint' or (name == 'hindcast' and a and a[2] and 'lint' in a[2]):
            raise ImportError('no lint yet')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    assert ghmod.default_linter() is None


def test_find_issue_whitespace_insensitive():
    fake = FakeRunner(issues=[{'number': 1, 'title': 'H-001  contracts.py:   x', 'body': ''}])
    g = Gh(runner=fake)
    assert g.find_issue('H-001 contracts.py: x')['number'] == 1
    assert g.find_issue('H-002 other', 'H-002') is None
    assert g.find_issue('H-001 renamed', 'H-001')['number'] == 1


# ----------------------------------------------------------------------------- epics

def test_sync_epics_task_list_and_update(blocks):
    fake = FakeRunner()
    g = Gh(runner=fake)
    ghmod.create_task_issues(g, blocks, only=['H-601'])         # one of E6's two tasks exists
    rep = ghmod.sync_epics(g, blocks, only=['H-6'])
    assert [(b.hid, a) for b, a, _, _ in rep] == [('H-600', 'created')]
    body = fake.issues[-1]['body']
    assert body.startswith('Intro line one.\n\n### Tasks\n')
    assert fake.issues[-1]['title'] == 'H-600 Epic E6: GitHub workflow'
    assert '- [ ] #101 H-601 hc gh labels|epics: create labels' in body
    assert '- [ ] H-603 Proposal and finding issues (issue not created yet)' in body
    cmd = fake.created()[-1]
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == '--label'] == ['hindcast', 'epic', 'mvp']
    # same state again: exists, body unchanged, no edit
    g2 = Gh(runner=fake)
    rep2 = ghmod.sync_epics(g2, blocks, only=['H-6'])
    assert [a for _, a, _, _ in rep2] == ['exists'] and fake.edited() == []
    # H-603 gets created -> the epic body is refreshed with its number
    ghmod.create_task_issues(g2, blocks, only=['H-603'])
    rep3 = ghmod.sync_epics(Gh(runner=fake), blocks, only=['H-6'])
    assert [a for _, a, _, _ in rep3] == ['updated']
    assert len(fake.edited()) == 1
    epic_row = next(i for i in fake.issues if i['number'] == 102)
    assert '- [ ] #103 H-603 Proposal and finding issues' in epic_row['body']
    assert 'not created yet' not in epic_row['body']
    # --no-update leaves a differing body alone
    epic_row['body'] = 'hand edited'
    rep4 = ghmod.sync_epics(Gh(runner=fake), blocks, only=['H-6'], update=False)
    assert [a for _, a, _, _ in rep4] == ['exists'] and len(fake.edited()) == 1


def test_sync_epics_dry_run(blocks, capsys):
    fake = FakeRunner(issues=[{'number': 5, 'title': 'H-700 Epic E7: Algorithm fidelity', 'body': 'stale',
                               'labels': ['epic']}])
    rep = ghmod.sync_epics(Gh(runner=fake, dry_run=True), blocks)
    assert {b.hid: a for b, a, _, _ in rep} == {'H-600': 'would-create', 'H-700': 'would-update'}
    assert fake.created() == [] and fake.edited() == []
    assert fake.issues[0]['body'] == 'stale'
    assert capsys.readouterr().out.count('DRY-RUN') == 2


def test_epic_body_lists_every_task_of_real_backlog(backlog_md):
    blocks = ghmod.load_backlog(backlog_md)
    epic = next(b for b in blocks if b.hid == 'H-600')
    tasks = ghmod.tasks_of(blocks, 'H-600')
    body = ghmod.epic_body(epic, tasks, {'H-601': 12, 'H-602': None, 'H-603': None})
    assert body.count('- [ ]') == 3 and '#12 H-601' in body and body.count('not created yet') == 2
    assert body.startswith('From') or body.startswith('`') or body.startswith('Freeze') or body.strip()


# ----------------------------------------------------------------------------- plumbing

def test_run_appends_repo_only_for_issue_and_label():
    fake = FakeRunner()
    g = Gh(runner=fake)
    g.run(['issue', 'list', '--json', 'number'])
    assert fake.calls[-1][0][-2:] == ['-R', REPO]
    calls = []
    g2 = Gh(runner=lambda cmd, inp=None: calls.append(cmd) or 'gh version 2\n')
    g2.run(['--version'])
    assert calls == [['gh', '--version']]


def test_subprocess_runner_raises_gherror(monkeypatch):
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='HTTP 404: Not Found')
    monkeypatch.setattr(subprocess, 'run', failing)
    with pytest.raises(GhError, match='404'):
        ghmod._subprocess_runner(['gh', 'issue', 'list'])


def test_number_from_url():
    assert ghmod._number_from_url('https://github.com/%s/issues/42\n'.strip() % REPO) == 42
    assert ghmod._number_from_url('') is None


def test_main_dry_run_issues_with_fake_subprocess(tmp_path, monkeypatch, capsys):
    """The CLI end to end: --dry-run must issue no create/edit through subprocess."""
    path = tmp_path / 'BACKLOG.md'
    path.write_text(MINI, encoding='utf-8')
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        assert cmd[1:3] in (['issue', 'list'], ['label', 'list']), cmd
        return subprocess.CompletedProcess(cmd, 0, stdout='[]', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    rc = ghmod.main(['issues', '--from', str(path), '--dry-run', '--no-lint', '--pause', '0'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'summary: would-create 3' in out and out.count('DRY-RUN') == 3
    rc = ghmod.main(['epics', '--from', str(path), '--dry-run', '--no-lint', '--pause', '0'])
    assert rc == 0 and 'would-create 2' in capsys.readouterr().out
    rc = ghmod.main(['labels', '--dry-run'])
    assert rc == 0 and 'created' in capsys.readouterr().out
    assert all(c[1:3] in (['issue', 'list'], ['label', 'list']) for c in seen)


def test_main_reports_gherror(tmp_path, monkeypatch, capsys):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 4, stdout='', stderr='not logged in')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert ghmod.main(['labels', '--dry-run']) == 1
    assert 'error:' in capsys.readouterr().out


def test_honesty_matches_design(design_md):
    text = open(design_md, encoding='utf-8').read()
    quoted = ' '.join(l.lstrip('> ').strip() for l in text.splitlines() if l.startswith('> Hindcast replays')
                      or (l.startswith('> ') and 'medical advice' in l) or l.startswith('> phone') or l.startswith('> cannot')
                      or l.startswith('> the later') or l.startswith('> ones.') or l.startswith('> are ties'))
    assert quoted == ghmod.HONESTY


@pytest.mark.needs_gh
def test_gh_cli_present_read_only():
    out = subprocess.run(['gh', '--version'], capture_output=True, text=True).stdout
    assert out.startswith('gh version')
