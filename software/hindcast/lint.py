"""
lint.py  -  the Hindcast vocabulary and privacy lint (backlog H-205, DESIGN.md sections 3 and 8)

Every user-facing text Hindcast generates passes through here before it is written or posted: candidate
cards (`hc card`), proposals (`hc gh propose`), GitHub issue bodies and round comments (`gh.py`), and the
one-line check-in strings of the skill. The rules are data in `vocabulary.json` next to this module; this
module only applies them.

    from hindcast.lint import lint_text, lint_findings, LintError, HONESTY_STATEMENT

    lint_text(text, kind='card')              -> None, or raises LintError naming the first offending phrase
    lint_findings(text, kind='card')          -> [Finding, ...]  (every problem, never raises; [] = clean)
    strip_honesty(text)                       -> text without the verbatim honesty paragraph
    contains_honesty_statement(text)          -> bool (whitespace-insensitive)
    render_vocabulary_markdown()              -> the skill's references/vocabulary.md as a string

Kinds (`KINDS`):
    'card'      candidate card: vocabulary rules + the honesty paragraph must be present
    'proposal'  proposal body:  vocabulary rules + the honesty paragraph must be present
    'issue'     anything posted to GitHub: vocabulary rules + privacy rules (the repository is public)
    'line'      a one-line chat / status string: vocabulary rules only

Contract:
    * The honesty paragraph (`HONESTY_STATEMENT`) is removed from the text before any rule runs, so its own
      wording ("what glucose would have done") never trips a rule, and its presence is what the card and
      proposal kinds require (`require_honesty`; default by kind, see above).
    * Matching is case-insensitive on the raw text (Markdown is not parsed). A may-not entry matches its
      `pattern` (a regex) or, without one, the phrase as whole words.
    * `lint_text` raises on the FIRST finding in text order; `lint_findings` returns them all. The exception
      text names the rule, the phrase as found and a short snippet, so gh.py / hc can print it as the refusal.
    * `ignore` names rules to skip (the `phrase` of a may-not entry, the `name` of a privacy entry, or
      'honesty'). The one expected use is `gh.py issues --from BACKLOG.md`: the backlog bodies were written
      by the maintainer and are public in the repository already, yet mention `mmol`, `U/day` and loop
      counts in their acceptance criteria. Generated text never needs it.
    * Returns None on success (gh.py treats any truthy non-bool return value as a refusal).

CLI:  python -m hindcast.lint --kind card FILE...     exit 1 and the findings on stderr if any file is refused
      python -m hindcast.lint --markdown               print references/vocabulary.md
"""
import json
import os
import re
import sys
from dataclasses import dataclass

__all__ = ['KINDS', 'HONESTY_STATEMENT', 'Finding', 'LintError', 'load_vocabulary', 'lint_text',
           'lint_findings', 'strip_honesty', 'contains_honesty_statement', 'render_vocabulary_markdown']

VOCABULARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vocabulary.json')
KINDS = ('card', 'proposal', 'issue', 'line')
_HONESTY_REQUIRED = {'card': True, 'proposal': True, 'issue': False, 'line': False}
_PRIVACY_APPLIES = {'card': False, 'proposal': False, 'issue': True, 'line': False}
_SNIPPET = 40


def load_vocabulary(path=VOCABULARY_PATH):
    """The parsed vocabulary.json (a fresh dict each call; the module caches its own compiled copy)."""
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


_VOCAB = load_vocabulary()
HONESTY_STATEMENT = _VOCAB['honesty_statement']
MAY = tuple(_VOCAB['may'])


def _compile(entry, name_key):
    pattern = entry.get('pattern') or r'\b' + re.escape(entry[name_key]) + r'\b'
    return re.compile(pattern, re.IGNORECASE)


_MAY_NOT = tuple((e['phrase'], _compile(e, 'phrase'), e.get('why', '')) for e in _VOCAB['may_not'])
_PRIVACY = tuple((e['name'], _compile(e, 'name'), e.get('why', '')) for e in _VOCAB['privacy'])
_WS = re.compile(r'\s+')
_HONESTY_RE = re.compile(r'\s+'.join(re.escape(w) for w in HONESTY_STATEMENT.split()), re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One lint problem: `rule` is 'may-not', 'privacy' or 'honesty'; `name` the entry; `phrase` the text as
    found (empty for a missing honesty paragraph); `start` its offset in the text given to the lint."""
    rule: str
    name: str
    phrase: str
    start: int
    snippet: str
    why: str = ''

    def __str__(self):
        if self.rule == 'honesty':
            return 'honesty paragraph missing: the card/proposal must carry DESIGN.md section 3 verbatim'
        label = 'may not say' if self.rule == 'may-not' else 'privacy'
        text = '%s %r (rule %r)' % (label, self.phrase, self.name)
        if self.snippet:
            text += ' in "...%s..."' % self.snippet
        if self.why:
            text += ': ' + self.why
        return text


class LintError(ValueError):
    """Raised by lint_text; `findings` holds every problem, str(exc) names the first one."""

    def __init__(self, findings, kind):
        self.findings = tuple(findings)
        self.kind = kind
        first = str(self.findings[0]) if self.findings else 'refused'
        more = '' if len(self.findings) < 2 else ' (+%d more)' % (len(self.findings) - 1)
        super().__init__('%s text refused: %s%s' % (kind, first, more))

    @property
    def phrase(self):
        return self.findings[0].phrase if self.findings else ''


def strip_honesty(text):
    """`text` without the verbatim honesty paragraph (whitespace-insensitive; every occurrence)."""
    return _HONESTY_RE.sub(' ', text)


def contains_honesty_statement(text):
    """True when the honesty paragraph appears verbatim (any whitespace, case-insensitive)."""
    return _HONESTY_RE.search(text) is not None


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError('lint kind must be one of %s, not %r' % (', '.join(KINDS), kind))


def lint_findings(text, kind='card', *, require_honesty=None, ignore=()):
    """Every problem in `text` for `kind`, in text order; [] when clean. Never raises for content.

    require_honesty: None = the kind's default (card/proposal yes, issue/line no). ignore: rule names to
    skip ('honesty', a may-not `phrase` or a privacy `name`)."""
    _check_kind(kind)
    if not isinstance(text, str):
        raise TypeError('lint expects a str, not %s' % type(text).__name__)
    ignored = set(ignore or ())
    if require_honesty is None:
        require_honesty = _HONESTY_REQUIRED[kind]
    findings = []
    if require_honesty and 'honesty' not in ignored and not contains_honesty_statement(text):
        findings.append(Finding('honesty', 'honesty', '', 0, '', 'required for kind %r' % kind))
    # the honesty paragraph is vetted wording: blank it (same length, offsets stay valid) before the rules run
    body = _HONESTY_RE.sub(lambda m: ' ' * (m.end() - m.start()), text)
    rules = [('may-not', name, rx, why) for name, rx, why in _MAY_NOT if name not in ignored]
    if _PRIVACY_APPLIES[kind]:
        rules += [('privacy', name, rx, why) for name, rx, why in _PRIVACY if name not in ignored]
    for rule, name, rx, why in rules:
        for m in rx.finditer(body):
            lo, hi = max(0, m.start() - _SNIPPET), min(len(body), m.end() + _SNIPPET)
            snippet = _WS.sub(' ', body[lo:hi]).strip()
            findings.append(Finding(rule, name, m.group(0), m.start(), snippet, why))
    findings.sort(key=lambda f: (f.start, f.rule != 'honesty'))
    return findings


def lint_text(text, kind='card', *, require_honesty=None, ignore=()):
    """Raise LintError naming the first offending phrase; return None when `text` is clean for `kind`."""
    findings = lint_findings(text, kind, require_honesty=require_honesty, ignore=ignore)
    if findings:
        raise LintError(findings, kind)
    return None


def render_vocabulary_markdown(vocab=None):
    """references/vocabulary.md for the skill, generated from vocabulary.json (H-501 copies the output)."""
    v = vocab or _VOCAB
    out = ['# Hindcast vocabulary', '',
           'Generated from `software/hindcast/vocabulary.json` by `python -m hindcast.lint --markdown`; do not edit',
           'by hand. `lint.py` refuses any card, proposal or issue body that breaks a rule below.', '',
           '## Honesty statement (verbatim on every page, card and proposal)', '', '> ' + v['honesty_statement'], '',
           '## May say', '']
    out += ['- "%s"' % p for p in v['may']]
    out += ['', '## May not say', '']
    for e in v['may_not']:
        out.append('- **%s** - %s' % (e['phrase'], e.get('why', '')))
    out += ['', '## Privacy (GitHub issues and comments; the repository is public)', '',
            'Preference names and values may appear. Glucose values, insulin units per day, loop counts per day,',
            'transcript excerpts and prediction arrays may not; link to the private artifact instead.', '']
    for e in v['privacy']:
        line = '- **%s** - %s' % (e['name'], e.get('why', ''))
        if e.get('passes'):
            line += ' (passes: "%s")' % e['passes']
        out.append(line)
    out += ['', '## Card template', '',
            'Current -> proposed values with the AAPS preference names; days replayed; `rise_vs_base_U` and',
            '`rest_vs_base_U` with the floor; loops changed by direction; P and E status; cap-binding share; the',
            'largest single emulated SMB next to the phone\'s maxBolus; per-stratum and hold-out rank; ties named as',
            'ties; the honesty statement; the VDF text. Verdict words: dead, candidate, tie, infeasible,',
            'recommended for review.', '']
    return '\n'.join(out)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog='python -m hindcast.lint', description=__doc__.split('\n\n')[0])
    p.add_argument('files', nargs='*', help='text files to lint')
    p.add_argument('--kind', choices=KINDS, default='card')
    p.add_argument('--ignore', action='append', default=[], help='rule name to skip (repeatable)')
    p.add_argument('--markdown', action='store_true', help='print references/vocabulary.md and exit')
    a = p.parse_args(argv)
    if a.markdown:
        sys.stdout.write(render_vocabulary_markdown())
        return 0
    if not a.files:
        p.error('give files to lint or --markdown')
    rc = 0
    for path in a.files:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        findings = lint_findings(text, a.kind, ignore=a.ignore)
        if findings:
            rc = 1
            for f in findings:
                sys.stderr.write('%s: %s\n' % (path, f))
        else:
            sys.stdout.write('%s: ok (%s)\n' % (path, a.kind))
    return rc


if __name__ == '__main__':
    sys.exit(main())
