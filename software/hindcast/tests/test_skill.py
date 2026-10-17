"""
test_skill.py  -  H-501: the Claude skill v0 (software/hindcast/skill/ + hindcast/skill_install.py).

- every skill file passes hindcast.lint kind 'line' (vocabulary.md's quoted refused wording excepted, and only that);
- references/vocabulary.md is generated from vocabulary.json and parses back to it (round trip), and is current;
- `hc skill install` copies the tree (vocabulary.md freshly rendered), leaves extra files alone, refuses a bad file;
- SKILL.md: frontmatter triggers, the 12 protocol steps, the never-list, every `hc` command it names exists;
- the acceptance dry run: against the fixture study the skill's check-in step yields exactly the line SKILL.md
  documents, and its review step writes a linted card, with every CSV / .txt read refused.
"""
import argparse
import builtins
import io
import json
import os
import re
import shutil

import pytest

from hindcast import cli
from hindcast import lint
from hindcast import skill_install as si
from hindcast.tests.conftest import NS_SETTINGS, STUDY_EXAMPLE_DIR

SKILL_MD = os.path.join(si.SKILL_SRC, 'SKILL.md')
REFERENCES = ('protocol.md', 'vocabulary.md', 'parameters.md', 'reading-guide.md', 'github.md')
SETTINGS_ARG = NS_SETTINGS if os.path.isfile(NS_SETTINGS) else os.devnull


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _skill_text():
    return _read(SKILL_MD)


# ----------------------------------------------------------------------------- the tree and the lint

def test_skill_tree_has_skill_md_and_the_five_references():
    files = si.skill_files()
    assert 'SKILL.md' in files
    for name in REFERENCES:
        assert os.path.join('references', name) in files


@pytest.mark.parametrize('rel', ['SKILL.md'] + [os.path.join('references', r) for r in REFERENCES])
def test_every_skill_file_passes_the_line_lint(rel):
    text = _read(os.path.join(si.SKILL_SRC, rel))
    assert lint.lint_findings(si.lintable_text(text), 'line') == []


def test_lint_skill_is_clean():
    assert si.lint_skill() == {}


def test_only_vocabulary_md_switches_the_lint_off_and_only_for_the_may_not_list():
    for rel in si.skill_files():
        text = _read(os.path.join(si.SKILL_SRC, rel))
        if rel != os.path.join('references', 'vocabulary.md'):
            assert si.LINT_OFF not in text and si.LINT_ON not in text, rel
    text = _read(si.VOCABULARY_MD)
    assert text.count(si.LINT_OFF) == 1 and text.count(si.LINT_ON) == 1
    block = text.split(si.LINT_OFF, 1)[1].split(si.LINT_ON, 1)[0].strip().splitlines()
    vocab = lint.load_vocabulary()
    assert len(block) == len(vocab['may_not'])
    assert all(line.startswith('- **') for line in block)
    # the block really is refused wording: without the markers the file would not pass
    assert lint.lint_findings(text, 'line') != []


def test_lintable_text_drops_only_the_marked_block():
    text = 'a\n%s\nThis is the best one.\n%s\nb' % (si.LINT_OFF, si.LINT_ON)
    assert si.lintable_text(text) == 'a\nb'


# ----------------------------------------------------------------------------- vocabulary.md generator

def test_vocabulary_round_trip():
    vocab = lint.load_vocabulary()
    doc = si.parse_vocabulary(si.render_vocabulary(vocab))
    assert doc['honesty_statement'] == vocab['honesty_statement'] == lint.HONESTY_STATEMENT
    assert doc['may'] == vocab['may']
    assert doc['may_not'] == [e['phrase'] for e in vocab['may_not']]
    assert doc['privacy'] == [e['name'] for e in vocab['privacy']]


def test_vocabulary_follows_the_json():
    vocab = lint.load_vocabulary()
    vocab['may'] = vocab['may'] + ['would have held the temp basal']
    vocab['may_not'] = vocab['may_not'] + [{'phrase': 'perfect', 'why': 'a value judgement', 'example': 'A perfect day.'}]
    doc = si.parse_vocabulary(si.render_vocabulary(vocab))
    assert doc['may'][-1] == 'would have held the temp basal'
    assert doc['may_not'][-1] == 'perfect'


def test_committed_vocabulary_md_is_current():
    assert si.vocabulary_is_current(), 'run `python -m hindcast.skill_install vocabulary`'


def test_write_vocabulary_reports_change(tmp_path):
    path = str(tmp_path / 'references' / 'vocabulary.md')
    assert si.write_vocabulary(path) is True
    assert si.write_vocabulary(path) is False
    assert _read(path) == si.render_vocabulary()


# ----------------------------------------------------------------------------- install

def test_install_copies_the_tree(tmp_path):
    dest = str(tmp_path / '.claude' / 'skills' / 'hindcast')
    os.makedirs(dest)
    with open(os.path.join(dest, 'local-notes.md'), 'w', encoding='utf-8') as fh:
        fh.write('mine\n')
    written, extra = si.install(dest)
    assert sorted(written) == sorted(si.skill_files())
    assert extra == ['local-notes.md']                                    # left alone, reported
    assert os.path.isfile(os.path.join(dest, 'local-notes.md'))
    for rel in si.skill_files():
        assert _read(os.path.join(dest, rel)) == _read(os.path.join(si.SKILL_SRC, rel)), rel
    assert _read(os.path.join(dest, 'references', 'vocabulary.md')) == si.render_vocabulary()


def test_install_dry_run_writes_nothing(tmp_path):
    dest = str(tmp_path / 'skill')
    written, extra = si.install(dest, dry_run=True)
    assert written and extra == []
    assert not os.path.exists(dest)


def test_install_refuses_a_file_that_fails_the_lint(tmp_path):
    src = str(tmp_path / 'src')
    shutil.copytree(si.SKILL_SRC, src)
    with open(os.path.join(src, 'references', 'protocol.md'), 'a', encoding='utf-8') as fh:
        fh.write('\nThis variant is the best one.\n')
    with pytest.raises(si.InstallRefused, match='protocol.md'):
        si.install(str(tmp_path / 'dest'), src=src)
    assert not os.path.exists(str(tmp_path / 'dest'))


def test_hc_skill_subcommands_through_register(tmp_path, capsys):
    parser = argparse.ArgumentParser(prog='hc')
    sub = parser.add_subparsers(dest='command')
    si.register(sub)
    dest = str(tmp_path / 'installed')
    args = parser.parse_args(['skill', 'install', '--dest', dest])
    assert args.func(args) == si.EXIT_OK
    assert os.path.isfile(os.path.join(dest, 'SKILL.md'))
    args = parser.parse_args(['skill', 'vocabulary', '--check'])
    assert args.func(args) == si.EXIT_OK
    args = parser.parse_args(['skill', 'lint'])
    assert args.func(args) == si.EXIT_OK
    args = parser.parse_args(['skill', 'checkin', str(tmp_path / 'no_such_study')])
    assert args.func(args) == si.EXIT_REFUSED
    capsys.readouterr()


def test_main_is_the_same_parser(capsys):
    assert si.main(['checkin', STUDY_EXAMPLE_DIR]) == si.EXIT_OK
    assert capsys.readouterr().out.strip() == _documented_checkin_line()


# ----------------------------------------------------------------------------- SKILL.md content

def _frontmatter(text):
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    assert m, 'SKILL.md must start with YAML frontmatter'
    fields = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip()
    return fields


def test_frontmatter_name_and_triggers():
    fm = _frontmatter(_skill_text())
    assert fm['name'] == 'hindcast'
    for trigger in ('optimize settings', 'what-if study', 'which autoISF setting', '/hindcast'):
        assert trigger in fm['description'], trigger


def test_twelve_protocol_steps_and_the_never_list():
    text = _skill_text()
    protocol = text.split('## The protocol in 12 steps', 1)[1].split('\n## ', 1)[0]
    steps = re.findall(r'^\s*(\d+)\. \*\*', protocol, re.M)
    assert steps == [str(i) for i in range(1, 13)]
    never = text.split('## The never-list', 1)[1].split('\n## ', 1)[0]
    for word in ('phone', 'Nightscout', 'ns_settings.json', 'glucose outcome', 'applied-on-phone',
                 'Never apply a proposal', 'CSV'):
        assert word in never, word
    assert lint.HONESTY_STATEMENT in text


def _command_block():
    text = _skill_text().split('## Commands', 1)[1]
    return text.split('```', 2)[1]


def _subparsers_of(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def test_every_hc_command_named_in_skill_md_exists():
    parser = cli.build_parser()
    top = _subparsers_of(parser)
    if 'skill' not in top:                                    # until the integrator wires hc skill into cli.py
        si.register(parser._subparsers._group_actions[0])
        top = _subparsers_of(parser)
    seen = 0
    for line in _command_block().splitlines():
        line = line.strip()
        if not line.startswith('hc '):
            continue
        words = line.split()
        cmd = words[1]
        assert cmd in top, line
        sub = _subparsers_of(top[cmd])
        if sub:
            assert words[2] in sub, line                      # e.g. hc study run, hc gh round, hc skill checkin
        seen += 1
    assert seen >= 14


def test_commands_not_built_are_named_as_such():
    text = _skill_text()
    tail = text.split('Not built yet', 1)[1]
    for missing in ('hc gh propose', 'hc gh sync', 'hc export --db-rows'):
        assert missing in tail
    top = _subparsers_of(cli.build_parser())
    assert 'propose' not in _subparsers_of(top['gh'])         # if this fails, H-603 landed: update SKILL.md



NOT_BUILT = ('hc gh propose', 'hc gh sync', 'hc export --db-rows')     # named as "not built yet" (H-603, H-504)
DOC_FILES = ('SKILL.md',) + tuple(os.path.join('references', n) for n in REFERENCES if n != 'vocabulary.md')


def _hc_mentions(text):
    """Every `hc ...` the text names: lines of code blocks (indented continuation lines joined) and inline spans."""
    out = []
    parts = text.split('```')
    for i, part in enumerate(parts):
        if i % 2:                                              # inside a code block
            current = None
            for line in part.splitlines():
                if line.strip().startswith('hc '):
                    if current:
                        out.append(current)
                    current = line.strip()
                elif current and line.startswith(' ') and line.strip().startswith(('-', '[', '|')):
                    current += ' ' + line.strip()
                else:
                    if current:
                        out.append(current)
                    current = None
            if current:
                out.append(current)
        else:
            out += re.findall(r'`(hc [^`]+)`', part)
    return [m for m in out if not m.startswith(NOT_BUILT)]


def _help(capsys, argv):
    capsys.readouterr()
    try:
        rc = cli.main(argv + ['--help'])
    except SystemExit as e:
        rc = e.code
    out = capsys.readouterr()
    assert rc in (0, None), (argv, out.err)
    return out.out


@pytest.mark.parametrize('rel', DOC_FILES)
def test_every_hc_command_and_flag_in_the_skill_exists(rel, capsys):
    """Each `hc <command> [<sub>]` named in SKILL.md and the references answers --help, and every --flag on that
    line appears in the help of that command (forwarded commands included)."""
    parser = cli.build_parser()
    top = _subparsers_of(parser)
    assert 'skill' in top                                      # hc skill is wired into cli.py
    mentions = _hc_mentions(_read(os.path.join(si.SKILL_SRC, rel)))
    assert mentions, rel
    helps = {}
    for m in mentions:
        tokens = m.split()
        cmd = tokens[1].strip('`,.;:')
        assert cmd in top, (rel, m)
        subs = _subparsers_of(top[cmd])
        sub, flags = None, []
        for tok in tokens[2:]:
            clean = tok.strip('[]()`,.;:')
            if subs and clean in subs:
                sub = clean
            elif clean.startswith('--'):
                flags.append((sub, clean.split('=')[0]))
            elif subs and sub is None and re.fullmatch(r'[a-z][a-z-]*', clean):
                raise AssertionError(f'{rel}: "{m}": hc {cmd} has no subcommand {clean}')
        for key in {(cmd, sub)} | {(cmd, s) for s, _ in flags}:
            if key not in helps:
                helps[key] = _help(capsys, [k for k in key if k])
        for s, flag in flags:
            assert flag in helps[(cmd, s)], f'{rel}: "{m}": {flag} is not an option of hc {cmd} {s or ""}'


def test_install_defaults_to_user_scope_and_project_is_opt_in(tmp_path, monkeypatch, capsys):
    assert si.DEFAULT_DEST == os.path.join(os.path.expanduser('~'), '.claude', 'skills', 'hindcast')
    assert si.PROJECT_DEST == os.path.join(si.REPO_DIR, '.claude', 'skills', 'hindcast')
    user, project = tmp_path / 'home' / 'hindcast', tmp_path / 'repo' / 'hindcast'
    monkeypatch.setattr(si, 'DEFAULT_DEST', str(user))
    monkeypatch.setattr(si, 'PROJECT_DEST', str(project))
    parser = argparse.ArgumentParser(prog='hc')
    si.register(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['skill', 'install'])
    assert si.install_dest(args) == str(user)
    assert args.func(args) == si.EXIT_OK and os.path.isfile(user / 'SKILL.md') and not project.exists()
    args = parser.parse_args(['skill', 'install', '--project', '--dry-run'])
    assert si.install_dest(args) == str(project)
    assert args.func(args) == si.EXIT_OK and not project.exists()
    with pytest.raises(SystemExit):
        parser.parse_args(['skill', 'install', '--project', '--dest', str(tmp_path / 'x')])
    capsys.readouterr()


def test_tier_c_is_weak_evidence_and_needs_the_unlock_in_the_skill():
    skill = _skill_text()
    guide = _read(os.path.join(si.SKILL_SRC, 'references', 'reading-guide.md'))
    for text in (skill, guide, si.render_vocabulary()):
        assert 'weak evidence' in text.lower() or 'weak-evidence' in text.lower()
        for key in ('sens', 'carb_ratio', 'current_basal'):
            assert key in text
    assert '--unlock' in skill and '--unlock-reason' in skill
    card = si.render_vocabulary().split('## Card template', 1)[1]
    assert 'weak-evidence caveat' in card


def test_github_md_names_no_unbuilt_action_as_done():
    text = _read(os.path.join(si.SKILL_SRC, 'references', 'github.md'))
    assert 'The agent closes it' not in text
    assert 'when it opens an issue' not in text
    for para in re.split(r'\n\s*\n', text):
        low = para.lower()
        if para.lstrip().startswith('#'):
            continue
        if ('close' in low and 'campaign' in low) or 'finding issue' in low or 'opens' in low:
            assert 'not built yet' in low and 'h-603' in low, para


# ----------------------------------------------------------------------------- parameters.md vs the spaces

def test_parameters_md_names_every_space_key_with_its_aaps_names():
    text = _read(os.path.join(si.SKILL_SRC, 'references', 'parameters.md'))
    with open(os.path.join(si.HINDCAST_DIR, 'prefs_map.json'), encoding='utf-8') as fh:
        by_vdf = {e['vdf']: e for e in json.load(fh)['keys'].values()}
    for tier in ('A', 'B'):
        with open(os.path.join(si.HINDCAST_DIR, 'spaces', 'autoisf_tier%s.json' % tier), encoding='utf-8') as fh:
            space = json.load(fh)['space']
        for key in space:
            rows = [r for r in text.splitlines() if r.startswith('| `%s` |' % key)]
            assert len(rows) == 1, key
            entry = by_vdf.get(key, {})
            if entry.get('title'):
                assert '| %s |' % entry['title'] in rows[0], key
            if entry.get('pref_key'):
                assert '`%s`' % entry['pref_key'] in rows[0], key


# ----------------------------------------------------------------------------- the acceptance dry run

def _documented_checkin_line():
    text = _skill_text().split('## Check-in cadence', 1)[1]
    return text.split('```', 2)[1].strip()


def test_checkin_line_of_the_fixture_matches_skill_md():
    line = si.checkin_line(si.read_status(STUDY_EXAMPLE_DIR))
    assert line == _documented_checkin_line()
    assert '\n' not in line
    assert lint.lint_findings(line, 'line') == []


def test_checkin_line_variants():
    status = si.read_status(STUDY_EXAMPLE_DIR)
    status.update(running=True, trials_failed=2, stale_context=['code_sha'], dead_parameters=[],
                  floor={'agreement_pct': 96.1})
    status['top'][0]['verdict'] = 'recommended'
    line = si.checkin_line(status, artifact_url='https://claude.ai/artifact/x')
    assert ', 2 failed' in line and 'running, heartbeat 2026-09-08T01:22:25Z' in line
    assert '(recommended for review)' in line and 'fidelity floor' not in line
    assert line.endswith('stale context; https://claude.ai/artifact/x')
    status.update(top=[], floor={})
    assert 'review order: none yet' in si.checkin_line(status) and 'floor: not measured yet' in si.checkin_line(status)


@pytest.fixture
def no_csv_reads(monkeypatch):
    """Refuse every open() of a result CSV or a transcript (.csv, .txt): the skill must not need one."""
    real_open, real_io_open = builtins.open, io.open
    refused = []

    def guarded(opener):
        def _open(file, *a, **kw):
            name = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else ''
            if isinstance(name, bytes):
                name = name.decode()
            if name.lower().endswith(('.csv', '.txt')):
                refused.append(name)
                raise AssertionError('the skill read %s' % name)
            return opener(file, *a, **kw)
        return _open

    monkeypatch.setattr(builtins, 'open', guarded(real_open))
    monkeypatch.setattr(io, 'open', guarded(real_io_open))
    return refused


def test_dry_run_against_the_fixture_study(tmp_path, no_csv_reads, capsys):
    study = str(tmp_path / 'study_example')
    shutil.copytree(STUDY_EXAMPLE_DIR, study)
    # step 5, check in: the one line, from status.json alone
    assert si.main(['checkin', study]) == si.EXIT_OK
    assert capsys.readouterr().out.strip() == _documented_checkin_line()
    # step 8, review: the card, as SKILL.md's command block writes it
    assert cli.main(['card', study, '--rank', '1', '--settings', SETTINGS_ARG]) == 0
    out = capsys.readouterr().out
    card_path = os.path.join(study, 'cards', '1.md')
    assert os.path.isfile(card_path) and os.path.isfile(os.path.join(study, 'cards', '1.vdf'))
    card = _read(card_path)
    assert card.startswith('# Candidate 1: dura_ISF_weight 0.5 (phone: 0.7)')
    assert 'verdict: **tie**' in card
    assert lint.lint_findings(card, 'card') == []
    assert lint.HONESTY_STATEMENT in out
    assert no_csv_reads == []
