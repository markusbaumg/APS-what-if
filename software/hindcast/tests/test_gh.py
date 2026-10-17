"""Tests for hindcast.gh: backlog parsing, idempotent labels/issues/epics against a mocked gh, dry-run.

Nothing here calls the real `gh`: every Gh instance gets a FakeRunner. The one test that touches the
CLI (`needs_gh`) only runs `gh --version`.
"""
import json
import re
import subprocess

import pytest

from hindcast import gh as ghmod
from hindcast.gh import Block, Gh, GhError, LABELS, REPO, parse_backlog


class FakeRunner:
    """Stands in for subprocess: answers list/view commands from its state, records creates/edits."""

    def __init__(self, labels=(), issues=()):
        self.labels = list(labels)
        self.issues = [dict(i) for i in issues]
        self.comments = {}                      # issue number -> [comment bodies]
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
            fields = cmd[cmd.index('--json') + 1].split(',')
            issue = next(i for i in self.issues if i['number'] == n)
            out = {}
            if 'body' in fields:
                out['body'] = issue['body']
            if 'comments' in fields:
                out['comments'] = [{'body': b, 'author': {'login': 'x'}} for b in self.comments.get(n, [])]
            return json.dumps(out)
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
        if sub == ('issue', 'comment'):
            n = int(cmd[3])
            assert any(i['number'] == n for i in self.issues), 'comment on unknown issue %d' % n
            self.comments.setdefault(n, []).append(input_text)
            return 'https://github.com/%s/issues/%d#issuecomment-%d\n' % (REPO, n, len(self.comments[n]))
        raise AssertionError('unexpected gh command %r' % (cmd,))

    def created(self):
        return [c for c, _ in self.calls if c[1:3] == ['issue', 'create']]

    def edited(self):
        return [c for c, _ in self.calls if c[1:3] == ['issue', 'edit']]

    def commented(self):
        return [c for c, _ in self.calls if c[1:3] == ['issue', 'comment']]


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
    assert [e.hid for e in epics] == ['H-%d00' % i for i in range(10)] + ['H-1000']
    assert len(tasks) == 66
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


# ----------------------------------------------------------------------------- campaign issue and round comments (H-602)

from hindcast import contracts as c                                                          # noqa: E402
from hindcast import lint                                                                    # noqa: E402
from hindcast import space as sp                                                             # noqa: E402
from hindcast import study as st                                                             # noqa: E402
from hindcast.tests.conftest import NS_SETTINGS                                              # noqa: E402

SETTINGS = sp.load_settings(NS_SETTINGS)
SPACE = {'profile autoISF_max': {'type': 'float', 'low': 1.1, 'high': 1.8, 'step': 0.05},
         'profile dura_ISF_weight': {'type': 'float', 'low': 0.3, 'high': 1.0, 'step': 0.05},
         'profile enable_pp_ISF': {'type': 'bool'}}
CAMPAIGN_LABELS_READY = list(LABELS)               # a repo where `hc gh labels` ran already


def make_study(tmp_path, name='autoISF_oct', days=tuple('2026-09-%02d' % d for d in range(1, 8)), **kw):
    kw.setdefault('phases', [c.Phase(kind='oaat', points=5), c.Phase(kind='lhs', n=24, seed=1),
                             c.Phase(kind='validate', top=8, days='holdout')])
    kw.setdefault('holdout', ['2026-09-05'] if '2026-09-05' in days else [])
    kw.setdefault('tags', {'2026-09-05': ['weekend', 'sport']} if '2026-09-05' in days else {})
    kw.setdefault('constraints', [['profile dura_ISF_weight', '<=', 'profile autoISF_max']])
    sd = st.new_study(name, list(days), SPACE, settings=SETTINGS, studies_dir=str(tmp_path), **kw)
    return sd, sd.load_study(SETTINGS)


def make_round(sd, round_no=1, n=6, phase='lhs', whys=None, notes=(), dead=True):
    variants = []
    for i in range(n):
        params = {'profile autoISF_max': round(1.3 + i * 0.05, 2), 'profile dura_ISF_weight': 0.5}
        why = (whys or ['asks for more insulin in rising loops'])[i % len(whys or [1])]
        variants.append(c.Candidate(vhash=c.vhash(params), variant=sp.variant_name(params),
                                    verdict=c.VERDICTS[i % len(c.VERDICTS)], why=why, params=params, rank=i + 1))
    rnd = c.Round(round=round_no, phase=phase, started=c.now_iso(), trials=n, variants=variants,
                  front=[v.vhash for v in variants], notes=list(notes),
                  dead_parameters=[c.DeadParameter('profile dura_ISF_weight', 'all points inside 1.5 x floor')] if dead else [])
    rnd.validate().save(sd.round_path)
    return rnd


def issue_lint(text):
    return [str(f) for f in lint.lint_findings(text, 'issue')]


def test_campaign_body_is_the_protocol_and_passes_the_issue_lint(tmp_path):
    sd, study = make_study(tmp_path, base_vdf_lines=['profile  enable_BG_acceleration  False'], notes=['first campaign'])
    body = ghmod.campaign_body(study, 'https://claude.ai/artifact/abc123')
    assert issue_lint(body) == []
    assert body.startswith(ghmod.campaign_marker('autoISF_oct'))
    assert '| `profile autoISF_max` | autoISF max | float | 1.1 .. 1.8 step 0.05 |' in body
    assert '| `profile enable_pp_ISF` | - | bool | false / true | - |' in body
    assert 'Days: 7 (6 search, 1 hold-out), 2026-09-01 .. 2026-09-07' in body
    assert 'Day tags (typed by the user): 2026-09-05: weekend, sport' in body
    assert 'Question: explore: nothing is ranked, the leaderboard is in review order' in body
    assert 'Phases: oaat (5 levels), lhs (24 points), validate (top 8, holdout)' in body
    assert 'Budget: 6 h, 4 worker(s)' in body and 'Constraints: `profile dura_ISF_weight <= profile autoISF_max`' in body
    assert 'Base VDF rows every variant carries: `profile  enable_BG_acceleration  False`' in body
    assert 'Artifact: https://claude.ai/artifact/abc123' in body and 'Note: first campaign' in body
    assert 'code_sha %s' % c.short(study.code_sha) in body
    assert ghmod.HONESTY in body and 'Privacy:' in body and 'Not testable with this method: basal rates' in body
    # no medical data: no glucose or insulin numbers, no loop counts, no host name
    assert ' U' not in body.replace(' UKF', '') and 'loops' not in body.replace('in about 4 % of loops', '')
    assert 'mmol' not in body and study.host not in body.split(ghmod.HONESTY)[0] if study.host else True
    # deterministic: the same study renders byte-identical (that is what makes `ensure_campaign` idempotent)
    assert ghmod.campaign_body(study, 'https://claude.ai/artifact/abc123') == body
    assert 'Artifact: not published yet' in ghmod.campaign_body(study)


@pytest.mark.parametrize('question', c.QUESTIONS)
def test_campaign_body_every_question_passes_lint(tmp_path, question):
    _, study = make_study(tmp_path, name='q_' + question, question=question)
    body = ghmod.campaign_body(study)
    assert issue_lint(body) == [] and question in body


def test_ensure_campaign_creates_once_then_updates_only_on_change(tmp_path):
    sd, study = make_study(tmp_path)
    fake = FakeRunner(labels=['hindcast'])
    g = Gh(runner=fake)
    action, row, note = ghmod.ensure_campaign(g, sd, study, artifact_url='https://claude.ai/artifact/a1',
                                              linter=ghmod.study_linter(study))
    assert (action, note) == ('created', '') and row['number'] == 101
    cmd = fake.created()[0]
    assert cmd[cmd.index('--title') + 1] == 'Campaign: autoISF_oct'
    assert [cmd[i + 1] for i, x in enumerate(cmd) if x == '--label'] == ['hindcast', 'campaign']
    assert 'campaign' in fake.labels                      # the missing label was created first
    assert fake.issues[-1]['body'] == ghmod.campaign_body(study, 'https://claude.ai/artifact/a1')
    # same state: exists, no edit
    action, row, _ = ghmod.ensure_campaign(Gh(runner=fake), sd, study, artifact_url='https://claude.ai/artifact/a1')
    assert action == 'exists' and row['number'] == 101 and fake.edited() == [] and len(fake.created()) == 1
    # the artifact link changed: the body is rewritten in place
    action, row, _ = ghmod.ensure_campaign(Gh(runner=fake), sd, study, artifact_url='https://claude.ai/artifact/a2')
    assert action == 'updated' and len(fake.edited()) == 1 and 'artifact/a2' in fake.issues[0]['body']
    # --no-update leaves a differing body alone
    action, _, _ = ghmod.ensure_campaign(Gh(runner=fake), sd, study, artifact_url='https://claude.ai/artifact/a3', update=False)
    assert action == 'exists' and len(fake.edited()) == 1
    # the artifact link defaults to status.json's artifact_url
    status = st.build_status(sd)
    status.artifact_url = 'https://claude.ai/artifact/from-status'
    sd.write_status(status)
    action, _, _ = ghmod.ensure_campaign(Gh(runner=fake), sd, study)
    assert action == 'updated' and 'artifact/from-status' in fake.issues[0]['body']


def test_ensure_campaign_dry_run_and_lint_refusal(tmp_path, capsys):
    sd, study = make_study(tmp_path)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    action, row, _ = ghmod.ensure_campaign(Gh(runner=fake, dry_run=True), sd, study)
    assert action == 'would-create' and row['number'] is None
    assert fake.created() == [] and [cmd[1:3] for cmd, _ in fake.calls] == [['issue', 'list'], ['label', 'list']]
    assert capsys.readouterr().out.count('DRY-RUN') == 1
    refusing = lambda body: 'privacy ...' if 'Protocol' in body else None    # noqa: E731
    action, row, note = ghmod.ensure_campaign(Gh(runner=fake), sd, study, linter=refusing)
    assert (action, row, note) == ('refused', None, 'privacy ...') and fake.created() == []
    # an existing issue whose refreshed body fails the lint is left as it is
    ghmod.ensure_campaign(Gh(runner=fake), sd, study)
    action, row, note = ghmod.ensure_campaign(Gh(runner=fake), sd, study, artifact_url='https://x', linter=refusing)
    assert action == 'refused' and row['number'] == 101 and fake.edited() == []


def test_round_body_words_and_sanitised_free_text(tmp_path):
    sd, study = make_study(tmp_path)
    rnd = make_round(sd, round_no=3, n=12, whys=['asks for more insulin in rising loops', 'SMB total +2.65 U more than base',
                                                 'the factor would have sat on the cap'],
                     notes=['fidelity floor held', 'Microbolusing 0.3U in the drill-down'])
    status = st.build_status(sd)
    linter = ghmod.study_linter(study)
    assert linter is not None
    body = ghmod.round_body(study, status, rnd, 3, None, linter)
    assert issue_lint(body) == []
    assert body.startswith(ghmod.round_marker('autoISF_oct', 3)) and '### Round 3 - phase lhs' in body
    assert '(12 in this round)' in body and 'variants in this round: 12 (' in body
    assert '[recommended for review]' in body and '[recommended]' not in body
    assert 'dropped parameters (not testable on these days): `profile dura_ISF_weight` - all points inside 1.5 x floor' in body
    assert '2.65 U' not in body and 'Microbolusing' not in body and body.count(ghmod.SEE_ARTIFACT) >= 2
    # an envelope-limited variant gets its own public label instead of its why (which lists U amounts)
    assert '[envelope-limited] - beyond the validity envelope on some days, not judged' in body
    # a tie whose why fails the lint falls back to what a tie means, not to 'see the artifact'
    assert '[tie] - indistinguishable from base within the noise floor' in body
    assert 'the factor would have sat on the cap' in body and '- note: fidelity floor held' in body
    import re
    assert len(re.findall(r'\n  \d+\. `', body)) == ghmod.ROUND_MAX_VARIANTS and '... 2 more in the artifact' in body
    assert 'review order (not a ranking by outcome)' in body and 'Artifact: not published yet' in body
    for word in ('better', 'best', 'optimal', 'safer', 'U/day', 'mmol'):
        assert word not in body
    # every verdict word of the contract renders lint-clean
    for verdict in c.VERDICTS:
        assert lint.lint_findings('[%s]' % st.verdict_label(verdict), 'issue') == []


def test_post_round_at_most_one_comment_per_round(tmp_path):
    sd, study = make_study(tmp_path)
    make_round(sd, round_no=1)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    linter = ghmod.study_linter(study)
    action, row, note = ghmod.post_round(Gh(runner=fake), sd, study, artifact_url='https://claude.ai/artifact/r', linter=linter)
    assert (action, note) == ('posted', '') and row['number'] == 101
    assert len(fake.created()) == 1                       # the campaign issue was opened on the way
    assert fake.issues[0]['title'] == 'Campaign: autoISF_oct' and 'artifact/r' in fake.issues[0]['body']
    assert len(fake.commented()) == 1 and fake.comments[101][0].startswith(ghmod.round_marker('autoISF_oct', 1))
    assert issue_lint(fake.comments[101][0]) == []
    # the same round again: nothing is posted
    action, row, note = ghmod.post_round(Gh(runner=fake), sd, study, artifact_url='https://claude.ai/artifact/r', linter=linter)
    assert action == 'exists' and 'round 1' in note and len(fake.commented()) == 1 and fake.edited() == []
    # round 2 gets its own comment, and only one
    make_round(sd, round_no=2, phase='halving')
    for _ in range(2):
        action, _, _ = ghmod.post_round(Gh(runner=fake), sd, study, artifact_url='https://claude.ai/artifact/r', linter=linter)
    assert action == 'exists' and len(fake.comments[101]) == 2
    assert '### Round 2 - phase halving' in fake.comments[101][1]
    # --round N names another round than round.json: the comment carries the counts only
    action, _, _ = ghmod.post_round(Gh(runner=fake), sd, study, round_no=5, linter=linter)
    assert action == 'posted' and 'no round.json yet' in fake.comments[101][2]
    assert fake.comments[101][2].startswith(ghmod.round_marker('autoISF_oct', 5))


def test_post_round_dry_run_posts_nothing(tmp_path, capsys):
    sd, study = make_study(tmp_path)
    make_round(sd, round_no=1)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    action, row, _ = ghmod.post_round(Gh(runner=fake, dry_run=True), sd, study, linter=ghmod.study_linter(study))
    assert action == 'would-post' and row['number'] is None
    assert fake.created() == [] and fake.commented() == [] and fake.comments == {}
    out = capsys.readouterr().out
    assert out.count('DRY-RUN') == 2 and 'issue create' in out and 'issue comment' in out
    # with an existing campaign issue the comments are read (read-only) and the comment is still only printed
    fake2 = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    ghmod.post_round(Gh(runner=fake2), sd, study)
    action, row, _ = ghmod.post_round(Gh(runner=fake2, dry_run=True), sd, study, round_no=2)
    assert action == 'would-post' and row['number'] == 101 and len(fake2.comments[101]) == 1
    assert [cmd[1:3] for cmd, _ in fake2.calls].count(['issue', 'comment']) == 1


def test_post_round_without_round_json(tmp_path):
    sd, study = make_study(tmp_path)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    action, row, note = ghmod.post_round(Gh(runner=fake), sd, study)
    assert action == 'no-round' and row is None and 'no round.json' in note and fake.created() == []
    c.State(phase='oaat', trials_done=5, queue_len=3, pid=4711, heartbeat=c.now_iso(), round=2, exit_code=2,
            stop_reason='guard: sentinel replay differs from base, see 2026-09-06T09:00:37Z').save(sd.state_path)
    action, row, _ = ghmod.post_round(Gh(runner=fake), sd, study, linter=ghmod.study_linter(study))
    assert action == 'posted'
    body = fake.comments[101][0]
    assert body.startswith(ghmod.round_marker('autoISF_oct', 2)) and 'phase oaat' in body
    assert '- run: stopped by a guard, exit 2 (sentinel replay differs from base' in body
    assert issue_lint(body) == []


def test_post_round_refused_bodies_are_not_posted(tmp_path):
    sd, study = make_study(tmp_path)
    make_round(sd, round_no=1)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    refuse_rounds = lambda body: 'may not say' if 'hindcast-round' in body else None       # noqa: E731
    action, row, note = ghmod.post_round(Gh(runner=fake), sd, study, linter=refuse_rounds)
    assert (action, row, note) == ('refused', None, 'may not say') and fake.created() == [] and fake.commented() == []
    refuse_campaign = lambda body: 'privacy' if 'hindcast-campaign' in body else None       # noqa: E731
    action, row, note = ghmod.post_round(Gh(runner=fake), sd, study, linter=refuse_campaign)
    assert action == 'refused' and note.startswith('campaign body:') and fake.commented() == []


def test_study_linter_private_repo_keeps_only_the_transcript_rules(tmp_path):
    sd, study = make_study(tmp_path)
    public = ghmod.study_linter(study)
    assert public('The variant asks for 2.65 U more.') and public('Microbolusing 0.3U')
    assert public('autoISF_max 1.3 -> 1.5, replayed over 7 days, 240 trials done') is None
    study.private_repo = True
    private = ghmod.study_linter(study)
    assert private('The variant asks for 2.65 U more.') is None
    assert private('Microbolusing 0.3U') and private('predBGs: [120, 118]') and private('This variant is better.')
    assert ghmod.study_linter(study, no_lint=True) is None


def test_run_campaign_and_round_cli(tmp_path, monkeypatch, capsys):
    """run_campaign / run_round through main() with subprocess mocked: --dry-run never creates, edits or comments."""
    sd, study = make_study(tmp_path)
    make_round(sd, round_no=1)
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        assert cmd[1:3] in (['issue', 'list'], ['label', 'list'], ['issue', 'view']), cmd
        if cmd[1:3] == ['label', 'list']:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{'name': n} for n in LABELS]), stderr='')
        return subprocess.CompletedProcess(cmd, 0, stdout='[]', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    rc = ghmod.main(['campaign', 'autoISF_oct', '--studies-dir', str(tmp_path), '--dry-run', '--pause', '0',
                     '--artifact', 'https://claude.ai/artifact/x'])
    out = capsys.readouterr().out
    assert rc == 0 and 'would-create' in out and 'Campaign: autoISF_oct' in out and out.count('DRY-RUN') == 1
    rc = ghmod.main(['round', str(sd.path), '--dry-run', '--pause', '0'])
    out = capsys.readouterr().out
    assert rc == 0 and 'would-post' in out and out.count('DRY-RUN') == 2
    assert all(cmd[1:3] in (['issue', 'list'], ['label', 'list'], ['issue', 'view']) for cmd in seen)
    # no such study: exit 3, nothing touched
    rc = ghmod.main(['campaign', 'nothere', '--studies-dir', str(tmp_path), '--dry-run'])
    assert rc == 3 and 'refused (exit 3)' in capsys.readouterr().out
    # the same subcommands through `hc gh ...` (cli.py picks up register())
    from hindcast import cli
    rc = cli.main(['gh', 'campaign', 'autoISF_oct', '--studies-dir', str(tmp_path), '--dry-run', '--pause', '0'])
    assert rc == 0 and 'would-create' in capsys.readouterr().out
    rc = cli.main(['gh', 'round', 'autoISF_oct', '--studies-dir', str(tmp_path), '--dry-run', '--pause', '0'])
    assert rc == 0 and 'would-post' in capsys.readouterr().out
    rc = cli.main(['gh', 'labels', '--dry-run'])
    assert rc == 0 and 'existing' in capsys.readouterr().out
    rc = cli.main(['gh', 'round', 'nothere', '--studies-dir', str(tmp_path)])
    assert rc == 3


def test_register_exposes_every_subcommand():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    ghmod.register(sub)
    for cmd, fn in (('labels', ghmod.run_labels), ('issues', ghmod.run_issues), ('epics', ghmod.run_epics),
                    ('campaign', ghmod.run_campaign), ('round', ghmod.run_round)):
        argv = ['gh', cmd] + (['s1'] if cmd in ('campaign', 'round') else [])
        args = parser.parse_args(argv + ['--dry-run'])
        assert args.func is fn and args.dry_run and args.gh_command == cmd
    args = parser.parse_args(['gh', 'round', 's1', '--round', '4', '--artifact', 'https://x', '--no-update'])
    assert args.round == 4 and args.artifact == 'https://x' and args.no_update
    with pytest.raises(SystemExit):
        parser.parse_args(['gh'])


def test_campaign_footer_and_question_texts_pass_lint():
    assert issue_lint(ghmod.CAMPAIGN_FOOTER) == []
    for text in ghmod.QUESTION_TEXT.values():
        assert issue_lint(text) == []
    assert issue_lint(ghmod.FOOTER) == []


@pytest.mark.needs_gh
def test_gh_cli_present_read_only():
    out = subprocess.run(['gh', '--version'], capture_output=True, text=True).stdout
    assert out.startswith('gh version')


def test_campaign_notes_that_fail_the_lint_are_withheld_not_blocking(tmp_path):
    sd, study = make_study(tmp_path, notes=['first campaign', 'the variant gave 2.65 U more', '', 'Microbolusing 0.3U'])
    linter = ghmod.study_linter(study)
    assert linter is not None
    body = ghmod.campaign_body(study, None, linter)
    assert issue_lint(body) == [] and 'Note: first campaign' in body
    assert 'Note: 2 more note(s): %s' % ghmod.SEE_ARTIFACT in body and '2.65' not in body and 'Microbolusing' not in body
    assert body.count('Note: ') == 2                       # the empty note is skipped, not counted
    # without a linter the notes are verbatim (and the whole body would then be refused by the lint)
    assert '2.65 U' in ghmod.campaign_body(study)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    action, _, note = ghmod.ensure_campaign(Gh(runner=fake), sd, study, linter=linter)
    assert (action, note) == ('created', '') and issue_lint(fake.issues[-1]['body']) == []


def test_round_notes_collapse_into_one_withheld_line(tmp_path):
    sd, study = make_study(tmp_path)
    rnd = make_round(sd, round_no=1, notes=['fidelity floor held', 'SMB total +2.65 U', 'Microbolusing 0.3U'])
    body = ghmod.round_body(study, st.build_status(sd), rnd, 1, None, ghmod.study_linter(study))
    assert '- note: fidelity floor held' in body and '- note: 2 more note(s): %s' % ghmod.SEE_ARTIFACT in body
    assert body.count('- note: ') == 2 and issue_lint(body) == []


def test_campaign_day_tags_that_fail_the_lint_are_withheld(tmp_path):
    """User-typed day tags go into a public issue: each is linted on its own like the notes; a refused tag is
    withheld and counted, the others stay."""
    tags = {'2026-09-05': ['weekend', 'Microbolusing 0.3U'], '2026-09-06': ['bolus 2.65 U'], '2026-09-07': ['sport']}
    sd, study = make_study(tmp_path, tags=tags)
    linter = ghmod.study_linter(study)
    assert linter is not None
    body = ghmod.campaign_body(study, None, linter)
    assert issue_lint(body) == []
    assert 'Microbolusing' not in body and '2.65' not in body
    assert 'Day tags (typed by the user): 2026-09-05: weekend; 2026-09-07: sport; 2 tags withheld (%s)' % (
        ghmod.SEE_ARTIFACT) in body
    assert '2026-09-06:' not in body                       # a day whose every tag was withheld is not listed
    # all tags pass: no withheld clause; without a linter the tags are verbatim
    _, clean = make_study(tmp_path, name='clean', tags={'2026-09-05': ['weekend', 'sport']})
    assert 'withheld' not in ghmod.campaign_body(clean, None, linter)
    assert 'Microbolusing 0.3U' in ghmod.campaign_body(study)
    action, _, note = ghmod.ensure_campaign(Gh(runner=FakeRunner(labels=CAMPAIGN_LABELS_READY)), sd, study, linter=linter)
    assert (action, note) == ('created', '')


# ----------------------------------------------------------------------------- no quantities in public bodies (W3 fix)

MVP_FLOORS = {'2026-09-06': {'floor_smb_U': 0.3, 'floor_tbr_U': 0.015, 'floor_total_U': 0.285, 'floor_rise_U': 0.295,
                             'floor_rest_U': 0.01, 'floor_P_U': 0.0, 'floor_loops': 30.0, 'agreement_pct': 97.483,
                             'loops': 1311.0}}
# the shapes round.py wrote into studies/mvp/round.json (the 10th line used to reach the public comment verbatim:
# 0.06 U and 0.02 U passed the old 'amounts under 0.5 U' threshold of the insulin-units rule)
MVP_VARIANTS = [
    ('candidate', {'profile autoISF_max': 1.4, 'profile dura_ISF_weight': 0.5},
     'would have asked for 0.06 U less than base per day in the other loops (1.3 x floor) over 1 day; rising loops -0.02 U',
     {'rise_vs_base_U': -0.02, 'rest_vs_base_U': -0.064, 'delta_vs_base_U': -0.084, 'changed_vs_base': 70.0,
      'env_day_pct_tdd': 0.349, 'review_score': 1.285}),
    ('candidate', {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5},
     'would have asked for 0.86 U less than base per day in rising loops (2.9 x floor) over 1 day; the other loops -0.11 U',
     {'rise_vs_base_U': -0.86, 'rest_vs_base_U': -0.111, 'changed_vs_base': 42.0, 'review_score': 2.912}),
    ('recommended', {'profile autoISF_max': 1.5, 'profile dura_ISF_weight': 0.9},
     'feasible on all days, in the top 3 on at least 80 % of the leave-one-day-out folds: recommended for review',
     {'rise_vs_base_U': 3.216, 'rest_vs_base_U': 0.349, 'review_score': 10.889}),
    ('infeasible', {'profile autoISF_max': 1.6, 'profile dura_ISF_weight': 0.9},
     "infeasible: 2026-09-06: out of envelope: 1.05 U vs base in a 3 h window (limit 1.0 U); out of envelope: 16.4 % "
     "of the phone's TDD vs base (limit 15.0 %) | 2026-09-07: loop count 1311 differs from base 1310",
     {'rise_vs_base_U': 3.562, 'rest_vs_base_U': 0.402, 'review_score': 12.061}),
    ('tie', {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.7},
     'indistinguishable from base within the noise floor on every component and every day',
     {'rise_vs_base_U': 0.0, 'rest_vs_base_U': 0.0, 'review_score': 0.0}),
]


def make_mvp_round(sd, round_no=1):
    variants = [c.Candidate(vhash=c.vhash(p), variant=sp.variant_name(p), verdict=v, why=why, params=p, rank=i + 1,
                            days=1, effect=eff) for i, (v, p, why, eff) in enumerate(MVP_VARIANTS)]
    rnd = c.Round(round=round_no, phase='grid', started=c.now_iso(), trials=len(variants), variants=variants,
                  floors=MVP_FLOORS, front=[v.vhash for v in variants])
    rnd.validate().save(sd.round_path)
    return rnd


def test_round_verdict_lines_carry_direction_and_floor_multiple_not_amounts(tmp_path):
    sd, study = make_study(tmp_path)
    rnd = make_mvp_round(sd)
    linter = ghmod.study_linter(study)
    # the old line is refused by the issue lint now (it used to pass and was posted)
    assert linter(MVP_VARIANTS[0][2]) and {'insulin units', 'U/day'} <= {
        f.name for f in lint.lint_findings(MVP_VARIANTS[0][2], 'issue')}
    body = ghmod.round_body(study, st.build_status(sd), rnd, 1, None, linter)
    assert issue_lint(body) == []
    lines = {l.split('] - ', 1)[0].split('`')[1]: l.split('] - ', 1)[1] for l in body.splitlines() if '] - ' in l}
    assert lines['autoISF_max_1.4__dura_ISF_weight_0.5'] == (
        'would have asked for less insulin than base in the other loops (1.3 x the floor) over 1 day; '
        'rising loops: within the floor')
    assert lines['autoISF_max_1.3__dura_ISF_weight_0.5'] == (
        'would have asked for less insulin than base in rising loops (2.9 x the floor) over 1 day; the other loops: less')
    assert lines['autoISF_max_1.5__dura_ISF_weight_0.9'] == (
        'would have asked for more insulin than base in rising loops (10.9 x the floor) over 1 day; the other loops: more; '
        'recommended for review')
    assert lines['autoISF_max_1.6__dura_ISF_weight_0.9'] == (
        'infeasible on 2 days: a loop count different from base; out of envelope')
    assert lines['autoISF_max_1.3__dura_ISF_weight_0.7'].startswith('indistinguishable from base within the noise floor')
    assert ghmod.SEE_ARTIFACT not in ''.join(lines.values())          # nothing withheld: phrased, not hidden
    import re
    assert not re.search(r'\d\s?U\b', body) and 'TDD' not in body and '1311' not in body
    # the amounts stay in the private study files
    on_disk = sd.read_round()
    assert on_disk.variants[0].why == MVP_VARIANTS[0][2] and on_disk.variants[0].effect['rest_vs_base_U'] == -0.064


def test_verdict_text_without_effect_or_floors_falls_back():
    cand = c.Candidate(vhash='a' * 40, variant='v', verdict='candidate', why='would have asked for 0.3 U more',
                       days=2, effect={})
    linter = ghmod.study_linter()
    assert ghmod.verdict_text(cand, None, linter) == ghmod.SEE_ARTIFACT      # no numbers to phrase: withheld
    cand.effect = {'rise_vs_base_U': 0.4, 'rest_vs_base_U': -0.01}
    assert ghmod.verdict_text(cand, None, linter) == (
        'would have asked for more insulin than base in rising loops over 2 days; the other loops: less')
    inf = c.Candidate(vhash='b' * 40, variant='w', verdict='infeasible', why='infeasible: 2026-09-06: the replay failed (boom 3 U)')
    assert ghmod.verdict_text(inf, None, linter) == 'infeasible on 1 day: the replay failed'


def _inject(fn, extra):
    def wrapped(*a, **k):
        return fn(*a, **k) + extra
    return wrapped


@pytest.mark.parametrize('extra', ['\n- rising loops -0.02 U', '\n- 0.06 U less per day', '\n- 114 loops changed',
                                   '\n- 3.6 mmol', "\n- 0.3 % of the phone's TDD"])
def test_post_round_lints_the_final_body_and_refuses_an_injected_quantity(tmp_path, monkeypatch, extra):
    sd, study = make_study(tmp_path)
    make_mvp_round(sd)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    monkeypatch.setattr(ghmod, 'round_body', _inject(ghmod.round_body, extra))
    # default linter (None = the study's issue lint): refused, nothing created or commented
    action, row, note = ghmod.post_round(Gh(runner=fake), sd, study)
    assert action == 'refused' and 'privacy' in note and fake.created() == [] and fake.commented() == []
    action, _, _ = ghmod.post_round(Gh(runner=fake), sd, study, linter=ghmod.study_linter(study))
    assert action == 'refused' and fake.commented() == []
    # through the CLI too
    monkeypatch.setattr(ghmod, '_gh_from_args', lambda args: Gh(runner=fake))
    assert ghmod.main(['round', str(sd.path), '--pause', '0']) == ghmod.EXIT_FAILED and fake.commented() == []


def test_ensure_campaign_lints_the_final_body_and_refuses_an_injected_quantity(tmp_path, monkeypatch):
    sd, study = make_study(tmp_path)
    make_mvp_round(sd)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    monkeypatch.setattr(ghmod, 'campaign_body', _inject(ghmod.campaign_body, '\nSMB total 0.06 U'))
    action, row, note = ghmod.ensure_campaign(Gh(runner=fake), sd, study)
    assert action == 'refused' and 'insulin units' in note and fake.created() == []
    action, _, note = ghmod.post_round(Gh(runner=fake), sd, study)
    assert action == 'refused' and note.startswith('campaign body:') and fake.created() == [] and fake.commented() == []
    monkeypatch.setattr(ghmod, '_gh_from_args', lambda args: Gh(runner=fake))
    assert ghmod.main(['campaign', str(sd.path), '--pause', '0']) == ghmod.EXIT_FAILED and fake.created() == []


def test_unlinted_posting_is_refused_for_a_public_study(tmp_path, monkeypatch):
    sd, study = make_study(tmp_path)
    make_mvp_round(sd)
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    assert ghmod.ensure_campaign(Gh(runner=fake), sd, study, linter=False) == ('refused', None, ghmod.NO_LINT_PUBLIC)
    assert ghmod.post_round(Gh(runner=fake), sd, study, linter=False)[0] == 'refused'
    monkeypatch.setattr(ghmod, '_gh_from_args', lambda args: Gh(runner=fake))
    assert ghmod.main(['round', str(sd.path), '--no-lint', '--pause', '0']) == ghmod.EXIT_FAILED
    assert ghmod.main(['campaign', str(sd.path), '--no-lint', '--pause', '0']) == ghmod.EXIT_FAILED
    assert fake.created() == [] and fake.commented() == []
    # a private-repo study may post unlinted on request
    study.private_repo = True
    action, _, _ = ghmod.post_round(Gh(runner=fake), sd, study, linter=False)
    assert action == 'posted' and len(fake.commented()) == 1


def test_study_linter_fails_closed_without_the_lint_module(tmp_path, monkeypatch):
    sd, study = make_study(tmp_path)
    make_mvp_round(sd)
    monkeypatch.setattr(ghmod, 'default_linter', lambda: None)
    linter = ghmod.study_linter(study)
    assert linter('a clean body') == ghmod.LINT_UNAVAILABLE
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    action, _, note = ghmod.post_round(Gh(runner=fake), sd, study)
    assert action == 'refused' and fake.created() == [] and fake.commented() == []


# ----------------------------------------------------------------------------- round comment: run word, controls (verifier fixes)

def _status_with(sd, exit_code, reason):
    c.State(phase='grid', trials_done=1, queue_len=0, pid=4711, heartbeat=c.now_iso(), round=1, exit_code=exit_code,
            stop_reason=reason).save(sd.state_path)
    return st.build_status(sd)


def test_run_line_word_follows_the_real_exit_reason(tmp_path):
    """A finished run is not 'stopped'; a STOP file, a guard, a refusal and a budget stop each get their own word,
    and the reason's own lead word is not repeated."""
    sd, study = make_study(tmp_path)
    linter = ghmod.study_linter(study)
    cases = [
        (0, 'finished: every built phase is settled; sentinel equals base on 2026-09-06 (1 check)',
         '- run: finished, exit 0 (every built phase is settled; sentinel equals base on 2026-09-06 (1 check))'),
        (0, 'finished', '- run: finished, exit 0'),
        (0, st.STOP_REASON, '- run: stopped by the STOP file, exit 0'),
        (0, st.STOP_REASON + '; null equals base on 2026-09-06 (1 check)',
         '- run: stopped by the STOP file, exit 0 (null equals base on 2026-09-06 (1 check))'),
        (0, 'budget exhausted', '- run: stopped, exit 0 (budget exhausted)'),
        (2, 'guard: 3 consecutive failed trials', '- run: stopped by a guard, exit 2 (3 consecutive failed trials)'),
        (3, 'refused: a locked key', '- run: refused, exit 3 (a locked key)'),
        (0, '', '- run: stopped, exit 0 (no reason recorded)'),
    ]
    for code, reason, want in cases:
        status = _status_with(sd, code, reason)
        assert ghmod.run_line(status, linter) == want
        body = ghmod.round_body(study, status, None, 1, None, linter)
        assert want in body and 'stopped, exit 0 (finished' not in body and issue_lint(body) == []


def _row(trial, round_no, control=None, params=None):
    params = params if params is not None else {'profile autoISF_max': 1.3 + trial / 100}
    import hashlib
    return c.TrialRow(trial=trial, key=hashlib.sha1(b'k%d' % trial).hexdigest(), phase='lhs', vhash=c.vhash(params),
                      variant='v%d' % trial, params=params, day='2026-09-06', status='blocked', error='test row',
                      code_sha='c', settings_sha='s', freeze_sha='f', round=round_no, control=control)


def test_round_controls_are_counted_next_to_the_variants(tmp_path):
    """The round's variants leave the sentinel / null replays out while the trial counts include them: the comment
    says 'plus N control replays' so the two numbers add up."""
    sd, study = make_study(tmp_path)
    for i in range(4):
        sd.append(_row(i, 2))
    sd.append(_row(4, 2, control='sentinel'))
    sd.append(_row(5, 2, control='null'))
    sd.append(_row(6, 1, control='sentinel'))                                   # another round: not counted
    assert ghmod.round_controls(sd, 2) == {'sentinel': 1, 'null': 1}
    assert ghmod.round_controls(sd, 3) == {} and ghmod.round_controls(sd, None) == {}
    rnd = make_round(sd, round_no=2, n=4)
    linter = ghmod.study_linter(study)
    body = ghmod.round_body(study, st.build_status(sd), rnd, 2, None, linter, controls=ghmod.round_controls(sd, 2))
    assert re.search(r'- variants in this round: 4 \([^)]*\), plus 2 control replays \(sentinel 1, null 1\)', body)
    assert issue_lint(body) == []
    one = ghmod.round_body(study, st.build_status(sd), rnd, 2, None, linter, controls={'sentinel': 1})
    assert 'plus 1 control replay (sentinel 1)' in one
    assert 'control replay' not in ghmod.round_body(study, st.build_status(sd), rnd, 2, None, linter)
    # post_round counts them itself
    fake = FakeRunner(labels=CAMPAIGN_LABELS_READY)
    action, _, _ = ghmod.post_round(Gh(runner=fake), sd, study, linter=linter)
    assert action == 'posted' and 'plus 2 control replays (sentinel 1, null 1)' in fake.comments[101][0]
