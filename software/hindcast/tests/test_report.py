"""
test_report.py  -  hc report / hc serve (backlog H-401) and the Study overview data (H-402)

Offline: renders the committed fixture study (tests/fixtures/study_example, H-003) into tmp_path; skipped when the
fixture is missing. The browser side (layout at 400 / 1200 px, both themes) is checked by hand with headless Chrome;
here the page contract is checked statically: tokens and theme guards, the script allow-list, the tier-1 JSON, the
tier-2 placement and inlining, the lint on every generated sentence and on the page's own strings.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import types
import urllib.request

import pytest

from hindcast import contracts, export, lint, report
from hindcast.tests import conftest

FIXTURE = conftest.STUDY_EXAMPLE_DIR
TEMPLATES = report.TEMPLATES_DIR
GENERATED = '2026-10-17T00:00:00Z'

needs_fixture = pytest.mark.skipif(not os.path.isfile(os.path.join(FIXTURE, 'study.json')),
                                   reason='fixture study missing (python -m hindcast.tests.make_fixtures)')


def _tier1(text):
    m = re.search(r'<script type="application/json" id="tier1">(.*?)</script>', text, re.S)
    assert m, 'tier-1 script block missing'
    return json.loads(m.group(1))


def _settings():
    return {'nightscout': {'units': 'mmol'}, '_assumed': {'flags.example': 'inferred from the echoes, not exported'}}


def _render(tmp_path, **kw):
    kw.setdefault('settings', _settings())
    kw.setdefault('frozen_dir', str(tmp_path / 'no_frozen'))
    kw.setdefault('generated', GENERATED)
    kw.setdefault('log', lambda *_: None)
    out = tmp_path / kw.pop('out_name', 'report')
    return report.render_report(FIXTURE, out_dir=str(out), **kw)


def _visible_text(page):
    body = re.sub(r'(?is)<script.*?</script>|<style.*?</style>', ' ', page)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body))


# ----------------------------------------------------------------------------- script / style allow-list

@pytest.mark.parametrize('src', [
    report.ECHARTS_URL,
    'https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js',
    'https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6.17/dist/plot.umd.min.js',
    'https://unpkg.com/react@18.3.1/umd/react.production.min.js',
    'https://cdn.tailwindcss.com/3.4.17',
    'https://code.jquery.com/jquery-3.7.1.min.js',
    'views/extra.js',
])
def test_allowed_scripts(src):
    assert report.script_problem(src) is None


@pytest.mark.parametrize('src, why', [
    ('http://cdnjs.cloudflare.com/ajax/libs/echarts/6.1.0/echarts.min.js', 'https'),
    ('https://esm.sh/echarts@6.1.0', 'not on the allowed list'),
    ('https://cdnjs.cloudflare.com/ajax/libs/echarts/latest/echarts.min.js', 'exact'),
    ('https://cdnjs.cloudflare.com/ajax/libs/echarts/6.1/echarts.min.js', 'exact'),
    ('https://cdn.jsdelivr.net/npm/react@18/umd/react.production.min.js', 'exact'),
    ('https://cdn.jsdelivr.net/npm/react@latest', 'exact'),
    ('https://unpkg.com/react', 'exact'),
    ('https://cdn.tailwindcss.com', 'exact'),
    ('//cdnjs.cloudflare.com/ajax/libs/echarts/6.1.0/echarts.min.js', 'protocol-relative'),
])
def test_refused_scripts(src, why):
    problem = report.script_problem(src)
    assert problem and why in problem


def test_check_external_resources_scans_scripts_and_styles():
    page = ('<link rel="stylesheet" href="https://example.com/x.css"><link rel="stylesheet" '
            'href="https://fonts.googleapis.com/css2?family=Inter"><script src="https://unpkg.com/react"></script>'
            '<script type="application/json" id="d">{"a":"<script src=\\"https://evil.example/x.js\\">"}</script>')
    problems = report.check_external_resources(page)
    assert len(problems) == 2
    assert any('example.com' in p for p in problems) and any('unpkg.com/react' in p for p in problems)


def test_render_refuses_a_bad_template_script(tmp_path, monkeypatch):
    if not os.path.isfile(os.path.join(FIXTURE, 'study.json')):
        pytest.skip('fixture study missing')
    monkeypatch.setattr(report, 'ECHARTS_URL', 'https://cdnjs.cloudflare.com/ajax/libs/echarts/latest/echarts.min.js')
    with pytest.raises(report.ReportError, match='exact'):
        _render(tmp_path)
    assert not (tmp_path / 'report' / 'index.html').exists()


def test_inline_guard():
    with pytest.raises(report.ReportError, match='</script'):
        report._guard_inline('var s = "</script>";', 'x.js', 'script')


# ----------------------------------------------------------------------------- templates: the page contract

def _read(name):
    with open(os.path.join(TEMPLATES, name), encoding='utf-8') as fh:
        return fh.read()


def _tokens(block):
    return set(re.findall(r'--([a-z0-9-]+)\s*:', block))


def test_tokens_and_theme_guards():
    css = _read('page.css')
    root = re.search(r'(?s)^:root\s*\{(.*?)\n\}', css, re.M).group(1)
    media = re.search(r'(?s)@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{(.*?)\}\s*\}', css)
    attr = re.search(r'(?s):root\[data-theme="dark"\]\s*\{(.*?)\}', css)
    assert media and attr, 'both dark guards are required'
    light, dark_media, dark_attr = _tokens(root), _tokens(media.group(1)), _tokens(attr.group(1))
    assert dark_media == dark_attr, 'the OS-dark and the toggle-dark blocks must define the same tokens'
    assert dark_media <= light, 'a token defined only in a dark block: %s' % sorted(dark_media - light)
    assert 'color-scheme: dark' in media.group(1) and 'color-scheme: dark' in attr.group(1)
    assert re.search(r'(?s)body\s*\{[^}]*background:\s*var\(--page\)', css)
    series = sorted(t for t in light if re.fullmatch(r'series-\d+', t))
    assert series == ['series-1', 'series-2', 'series-3'], 'at most three variant colours'
    assert 'padding-left: max(16px, env(safe-area-inset-left, 0px))' in css
    assert 'padding-right: max(16px, env(safe-area-inset-right, 0px))' in css
    # no literal colour outside the token blocks (rgba borders live in the tokens too)
    rest = css[attr.end():]
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', rest), 'literal colour outside the token blocks'


def test_theme_js_reads_every_token_it_uses():
    js = _read('theme.js')
    css_tokens = _tokens(re.search(r'(?s)^:root\s*\{(.*?)\n\}', _read('page.css'), re.M).group(1))
    listed = set(re.findall(r"'([a-z0-9-]+)'", re.search(r'(?s)var TOKENS = \[(.*?)\];', js).group(1)))
    assert listed <= css_tokens, sorted(listed - css_tokens)
    assert 'getComputedStyle' in js and 'prefers-color-scheme: dark' in js and "attributeFilter: ['data-theme']" in js
    code = re.sub(r'(?s)/\*.*?\*/', ' ', js)
    assert 'registerTheme' in code and "init(rec" not in code
    assert not re.search(r"registerTheme\(\s*'dark'|, 'dark'", code), "ECharts' built-in dark theme is never used"


def test_page_html_template():
    tpl = _read('page.html')
    assert '<title>$TITLE</title>' in tpl
    assert 'name="claude:viewport-fit-sides" content="cover"' in tpl
    assert tpl.count('<script src=') == 1 and '$ECHARTS_URL' in tpl
    assert re.search(r'<p class="honesty"[^>]*>.*\$HONESTY', tpl), 'honesty paragraph is plain text, not in <details>'
    assert '<details' not in tpl.split('$HONESTY')[0][-400:]


def _js_strings(js):
    js = re.sub(r'(?s)/\*.*?\*/', ' ', js)
    js = re.sub(r'(?m)^\s*//.*$', ' ', js)
    return re.findall(r"'((?:[^'\\\n]|\\.)*)'", js)


@pytest.mark.parametrize('name', ['page.js', 'theme.js'])
def test_page_strings_pass_the_lint(name):
    strings = [s for s in _js_strings(_read(name)) if ' ' in s]
    assert strings, 'no strings extracted'
    for s in strings:
        assert not lint.lint_findings(s, 'line'), s


def test_node_syntax_check():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    for name in ('page.js', 'theme.js'):
        subprocess.run([node, '--check', os.path.join(TEMPLATES, name)], check=True, capture_output=True)


# ----------------------------------------------------------------------------- rendering the fixture

@needs_fixture
def test_render_fixture(tmp_path):
    res = _render(tmp_path)
    text = open(res.path, encoding='utf-8').read()
    assert res.bytes == len(text.encode('utf-8'))
    assert text.startswith('<!doctype html>')
    assert '<title>study_example</title>' in text
    assert report.external_scripts(text) == [report.ECHARTS_URL]
    assert report.check_external_resources(text) == []
    assert lint.contains_honesty_statement(_visible_text(text))
    data = _tier1(text)
    assert data['honesty'] == contracts.HONESTY_STATEMENT
    assert data['generated'] == GENERATED
    assert data['study']['name'] == 'study_example'
    assert len(data['trials']) == 5 and data['base_vhash'] == contracts.BASE_VHASH
    assert data['assumed'] == {'flags.example': 'inferred from the echoes, not exported'}
    assert data['page']['units'] == 'mmol' and data['page']['single_file'] is False
    assert data['days'] == {}                                  # no frozen dir given
    # tier-2: the five series and orig are copied next to the page, the rows point at them
    assert len(res.tier2) == 6
    for r in data['trials']:
        p = r['files']['series']
        assert p == 'series/2026-09-06/%s.json' % r['vhash']
        assert os.path.isfile(os.path.join(res.out_dir, *p.split('/')))
    base = [r for r in data['trials'] if r['vhash'] == contracts.BASE_VHASH][0]
    assert base['files']['orig'] == 'series/2026-09-06/orig.json'
    assert 'per-loop data loaded on demand' in text
    assert not re.search(r'data-tier2=', text)
    # re-rendering keeps the copies (nothing changed) and yields the same page
    again = _render(tmp_path)
    assert open(again.path, encoding='utf-8').read() == text


@needs_fixture
def test_overview_numbers_of_the_fixture(tmp_path):
    data = _tier1(open(_render(tmp_path).path, encoding='utf-8').read())
    ov = data['overview']
    d = ov['days']['2026-09-06']
    assert d['has_base'] and d['loops'] == 340 and d['hours'] == 6.0
    assert d['floor_loops'] == 26 and d['floor_rise_U'] == pytest.approx(1.38, abs=0.001)
    assert d['tie_rise_U'] == pytest.approx(2.76, abs=0.001) and d['tie_rest_U'] == pytest.approx(0.10)
    assert ov['summary']['agreement_min_pct'] == pytest.approx(92.353, abs=0.001)
    assert ov['summary']['trials_live'] == 5 and ov['summary']['trials_frozen'] == 0
    assert '92.4 % of 340 loops' in ov['banner'] and 'disagrees in 26' in ov['banner']
    assert '+1.45 U over 6.0 h' in ov['banner']
    assert 'wider than the "about 4 %"' in ov['banner']
    assert ov['ties_text'].startswith('All 4 variants of round 1 are indistinguishable from base')
    assert 'not testable on these days' in ov['ties_text']
    for s in report.page_sentences(data):
        lint.lint_text(s, 'line')


@needs_fixture
def test_overview_without_round_or_base(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    os.remove(sd / 'round.json')
    rows = [l for l in open(sd / 'trials.jsonl', encoding='utf-8') if '"control":"base"' not in l]
    open(sd / 'trials.jsonl', 'w', encoding='utf-8').writelines(rows)
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={}, frozen_dir=str(tmp_path / 'nf'),
                               generated=GENERATED, log=lambda *_: None)
    ov = _tier1(open(res.path, encoding='utf-8').read())['overview']
    assert ov['banner'] is None and ov['ties_text'] is None
    assert ov['days']['2026-09-06'] == {'has_base': False}


@needs_fixture
def test_two_days_and_frozen_meta(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    study = json.load(open(sd / 'study.json'))
    study['days'].append({'day': '2026-09-07', 'role': 'holdout', 'tags': ['weekend']})
    json.dump(study, open(sd / 'study.json', 'w'))
    extra = []
    for line in open(sd / 'trials.jsonl', encoding='utf-8'):
        r = json.loads(line)
        r['day'] = '2026-09-07'
        r['trial'] += 10
        r['key'] = contracts.key(r['params'], r['day'], r['settings_sha'], r['code_sha'], r['freeze_sha'], 1)
        r['files'] = {}
        extra.append(json.dumps(r) + '\n')
    open(sd / 'trials.jsonl', 'a', encoding='utf-8').writelines(extra)
    frozen = tmp_path / 'frozen' / 'ns.vivikusi.ch'
    frozen.mkdir(parents=True)
    json.dump({'day': '2026-09-07', 'loops': 1300, 'final': True, 'character': {'tdd_U': 20.0, 'carbs_g': 30},
               'confounders': ['cgm gap 30 min 10:00-10:30'], 'sha1': 'x'}, open(frozen / '2026-09-07.meta.json', 'w'))
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={}, frozen_dir=str(tmp_path / 'frozen'),
                               generated=GENERATED, log=lambda *_: None)
    data = _tier1(open(res.path, encoding='utf-8').read())
    assert set(data['overview']['days']) == {'2026-09-06', '2026-09-07'}
    assert data['overview']['summary']['days_with_base'] == 2
    assert data['overview']['days']['2026-09-07']['hours'] is None        # no series document for that day
    assert 'on its worst day' in data['overview']['banner']
    assert data['days']['2026-09-07']['confounders'] == ['cgm gap 30 min 10:00-10:30']
    assert 'sha1' not in data['days']['2026-09-07']
    assert data['overview']['summary']['frozen_days'] == 1
    assert all(r['files'] == {} for r in data['trials'] if r['day'] == '2026-09-07')


@needs_fixture
def test_missing_tier2_file_is_dropped(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    victim = 'series/2026-09-06/bd3c184c0eb117ac77f3dc39a545561816c609d3.json'
    os.remove(sd / victim)
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={}, frozen_dir=str(tmp_path / 'nf'),
                               generated=GENERATED, log=lambda *_: None)
    data = _tier1(open(res.path, encoding='utf-8').read())
    row = [r for r in data['trials'] if r['vhash'].startswith('bd3c184c')][0]
    assert 'series' not in row['files'] and victim not in res.tier2 and len(res.tier2) == 5


@needs_fixture
def test_render_into_the_study_report_dir(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    res = report.render_report(str(sd), settings={}, frozen_dir=str(tmp_path / 'nf'), generated=GENERATED,
                               log=lambda *_: None)
    assert res.path == str(sd / 'report' / 'index.html')
    assert (sd / 'report' / 'series' / '2026-09-06' / 'orig.json').is_file()
    assert set(res.files) == set(res.tier2)


# ----------------------------------------------------------------------------- --single-file, --artifact

@needs_fixture
def test_single_file_inlines_base_orig_and_top(tmp_path):
    res = _render(tmp_path, single_file=True, variants='top1')
    text = open(res.path, encoding='utf-8').read()
    inlined = dict(re.findall(r'<script type="application/json" data-tier2="([^"]+)">(.*?)</script>', text, re.S))
    top1 = 'series/2026-09-06/bd3c184c0eb117ac77f3dc39a545561816c609d3.json'
    assert set(inlined) == {'series/2026-09-06/orig.json',
                            'series/2026-09-06/%s.json' % contracts.BASE_VHASH, top1}
    packed = {path: json.loads(body) for path, body in inlined.items()}
    for path, doc in report.unpack_inline(packed).items():          # page.js expands the packed copies the same way
        with open(os.path.join(FIXTURE, *path.split('/')), encoding='utf-8') as fh:
            src = json.load(fh)
        assert export.validate_series(src) == []
        assert doc == {k: v for k, v in src.items() if k not in report.INLINE_DROP_FIELDS}
    assert sorted(res.inlined) == sorted(inlined) and res.tier2 == {}
    data = _tier1(text)
    assert data['page']['single_file'] is True and sorted(data['page']['inlined']) == sorted(inlined)
    assert 'single file, 3 per-loop documents inlined' in text
    assert not (tmp_path / 'report' / 'series').exists()


@needs_fixture
def test_single_file_selection_options(tmp_path):
    every = _render(tmp_path, single_file=True, variants='all', out_name='a')
    assert len(every.inlined) == 6
    named = _render(tmp_path, single_file=True, variants='autoISF_max_1.5__dura_ISF_weight_0.7', out_name='b')
    assert any('e1336f715a3d' in p for p in named.inlined) and len(named.inlined) == 3
    with pytest.raises(report.ReportError, match='--variants nope'):
        _render(tmp_path, single_file=True, variants='nope', out_name='c')
    with pytest.raises(report.ReportError, match='--days 2026-01-01'):
        _render(tmp_path, single_file=True, days='2026-01-01', out_name='d')


@needs_fixture
def test_single_file_size_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(report, 'SINGLE_FILE_WARN_BYTES', 1000)
    logged = []
    res = _render(tmp_path, single_file=True, log=logged.append)
    assert res.warnings and '12 MB' in res.warnings[0] and logged and logged[0].startswith('warning:')
    monkeypatch.setattr(report, 'ARTIFACT_MAX_BYTES', 1000)
    res = _render(tmp_path, single_file=True, out_name='r2')
    assert '16 MB artifact limit' in res.warnings[0]


@needs_fixture
def test_artifact_form(tmp_path):
    res = _render(tmp_path, artifact=True)
    text = open(res.path, encoding='utf-8').read()
    assert not re.search(r'(?i)<!doctype|<html|<head>|<body|<meta charset|name="viewport"', text)
    assert '<title>study_example</title>' in text[:300]
    assert 'claude:viewport-fit-sides' in text
    assert _tier1(text)['page']['artifact'] is True
    assert set(res.files) == set(res.tier2) and all(os.path.isfile(p) for p in res.files.values())


def test_strip_skeleton():
    page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="x">\n'
            '<title>T</title>\n</head>\n<body>\n<p>x</p>\n</body>\n</html>\n')
    assert report.strip_skeleton(page) == '<title>T</title>\n<p>x</p>\n'


# ----------------------------------------------------------------------------- views, serve, CLI

@needs_fixture
def test_views_directory_is_appended(tmp_path, monkeypatch):
    views = tmp_path / 'views'
    views.mkdir()
    (views / 'b.js').write_text('window.HC_B = 1;')
    (views / 'a.js').write_text('window.HC_A = 1;')
    monkeypatch.setattr(report, 'VIEWS_DIR', str(views))
    text = open(_render(tmp_path).path, encoding='utf-8').read()
    assert text.index('/* a.js */') < text.index('/* b.js */') < text.index('window.HC.boot();')
    assert text.index('/* page.js') < text.index('/* a.js */')


@needs_fixture
def test_serve_serves_the_page_and_tier2(tmp_path):
    res = _render(tmp_path)
    server = report.make_server(res.out_dir, port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen('http://127.0.0.1:%d/index.html' % port, timeout=5) as r:
            assert r.status == 200 and r.headers['Cache-Control'] == 'no-cache'
            assert b'<title>study_example</title>' in r.read()
        with urllib.request.urlopen('http://127.0.0.1:%d/series/2026-09-06/orig.json' % port, timeout=5) as r:
            assert json.loads(r.read())['kind'] == 'orig'
    finally:
        server.shutdown()
        server.server_close()


def test_cli_registers_report_and_serve():
    from hindcast import cli
    parser = cli.build_parser()
    args = parser.parse_args(['report', 'some_study', '--single-file', '--variants', 'top2'])
    assert args.func is report.run_report and args.single_file and args.variants == 'top2'
    args = parser.parse_args(['serve', 'some_study', '--port', '0'])
    assert args.func is report.run_serve and args.port == 0


@needs_fixture
def test_run_report_cli(tmp_path):
    lines = []
    args = report.argparse.Namespace(study=FIXTURE, out=str(tmp_path / 'o'), single_file=False, days=None, variants='top3',
                                     pred=None, artifact=True, settings=conftest.NS_SETTINGS,
                                     frozen_dir=str(tmp_path / 'nf'), studies_dir=str(tmp_path))
    assert report.run_report(args, log=lines.append) == 0
    assert lines[0].startswith('wrote ') and lines[-1] == contracts.HONESTY_STATEMENT
    assert any('files map for the Artifact publish' in l for l in lines)
    args.study = str(tmp_path / 'nothing_here')
    assert report.run_report(args, log=lines.append) == 3


# ----------------------------------------------------------------------------- H-403 Parameter space view

VIEW_JS = os.path.join(TEMPLATES, 'views', 'parameters.js')


def _view_css(js):
    """the CSS the Parameters view injects (one quoted rule per array entry of `var CSS = [...]`)"""
    block = re.search(r"(?s)var CSS = \[(.*?)\]\.join", js).group(1)
    return '\n'.join(re.findall(r"'((?:[^'\\\n]|\\.)*)'", block))


def test_parameters_view_strings_pass_the_lint():
    js = open(VIEW_JS, encoding='utf-8').read()
    strings = [s for s in _js_strings(js) if ' ' in s]
    assert len(strings) > 40, 'no strings extracted'
    for s in strings:
        assert not lint.lint_findings(s, 'line'), s


def test_parameters_view_contract():
    js = open(VIEW_JS, encoding='utf-8').read()
    assert "HC.registerView('parameters'" in js, 'replaces the H-401 placeholder under the same id'
    code = re.sub(r'(?s)/\*.*?\*/', ' ', js)
    assert 'http' not in code.replace("'http", ''), 'the view loads nothing from the network'
    for hook in ('setFilter', 'onSelect', 'refresh', 'model'):
        assert re.search(r'\b%s:\s*function' % hook, code), hook
    css = _view_css(js)
    rules = [r for r in css.split('\n') if r.strip()]
    assert rules and all(r.startswith('#view-parameters') or r.startswith('@media') for r in rules), 'styles stay scoped'
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(', css), 'literal colour in the view CSS (use page.css tokens)'
    tokens = set(re.findall(r'var\(--([a-z0-9-]+)\)', css))
    css_tokens = _tokens(re.search(r'(?s)^:root\s*\{(.*?)\n\}', _read('page.css'), re.M).group(1))
    assert tokens <= css_tokens, sorted(tokens - css_tokens)
    used = set(re.findall(r"\bt\['([a-z0-9-]+)'\]|\bt\.([a-z]+)\b", code))
    theme_tokens = set(re.findall(r"'([a-z0-9-]+)'", re.search(r'(?s)var TOKENS = \[(.*?)\];', _read('theme.js')).group(1)))
    for a, b in used:
        assert (a or b) in theme_tokens, a or b
    assert "type: 'heatmap'" in code and 'markArea' in code and "yAxisIndex: 1" not in code, 'no dual axis'


def test_parameters_view_node_syntax():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    subprocess.run([node, '--check', VIEW_JS], check=True, capture_output=True)


@needs_fixture
def test_parameter_space_of_the_fixture(tmp_path):
    text = open(_render(tmp_path).path, encoding='utf-8').read()
    assert '/* parameters.js */' in text and text.index('/* page.js') < text.index('/* parameters.js */')
    data = _tier1(text)
    for key in ('honesty', 'trials', 'rounds', 'overview', 'assumed', 'page'):      # nothing existing was dropped
        assert key in data
    ps = data['parameters']
    assert ps['question'] == 'explore' and ps['days'] == ['2026-09-06'] and ps['tie_factor'] == 2.0
    assert [p['key'] for p in ps['params']] == ['profile autoISF_max', 'profile dura_ISF_weight']
    assert ps['base']['values'] == {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.7}
    tie = ps['floors']['2026-09-06']['tie']
    assert tie['rise_vs_base_U'] == pytest.approx(2.76, abs=0.001) and tie['rest_vs_base_U'] == pytest.approx(0.10)
    assert tie['changed_vs_base'] == 52
    assert [c['key'] for c in ps['components']][:2] == ['rise_vs_base_U', 'rest_vs_base_U']
    assert len(ps['variants']) == 4 and contracts.BASE_VHASH not in ps['order']
    assert ps['order'] == data['rounds'][0]['front'], 'review order = |effect| / floor, as the round file'
    first = ps['variants'][0]
    assert first['position'] == 1 and first['verdict'] == 'tie' and first['feasible'] is True
    assert first['values'] == {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5}
    assert first['per_day']['2026-09-06']['rise_vs_base_U'] == pytest.approx(-0.358, abs=0.001)
    assert first['mean']['changed_vs_base'] == 12 and first['review_component'] == 'rest_vs_base_U'
    assert ps['rule_lines'][0] == 'Review order, not a ranking by outcome.'
    assert any(line.startswith('Floors - 2026-09-06') for line in ps['rule_lines'])
    assert ps['warnings'] == []
    for s in report.page_sentences(data):
        lint.lint_text(s, 'line')


@needs_fixture
def test_parameter_space_infeasible_variant_and_oaat_values(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    rows = []
    for line in open(sd / 'trials.jsonl', encoding='utf-8'):
        r = json.loads(line)
        if r['variant'] == 'autoISF_max_1.5__dura_ISF_weight_0.7':
            r['metrics']['cap_binding_pct'] = 61.0                    # a factor on its cap: not really tested
        if r['variant'] == 'autoISF_max_1.3__dura_ISF_weight_0.5':
            r['params'] = {'profile dura_ISF_weight': 0.5}            # one-at-a-time row: the other key is the phone's
        rows.append(json.dumps(r) + '\n')
    open(sd / 'trials.jsonl', 'w', encoding='utf-8').writelines(rows)
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={}, frozen_dir=str(tmp_path / 'nf'),
                               generated=GENERATED, log=lambda *_: None)
    ps = _tier1(open(res.path, encoding='utf-8').read())['parameters']
    bad = [v for v in ps['variants'] if v['variant'] == 'autoISF_max_1.5__dura_ISF_weight_0.7'][0]
    assert bad['feasible'] is False and bad['verdict'] == 'infeasible' and bad['rank'] is None
    assert 'cap' in bad['reasons']['2026-09-06'][0] and bad['why'].startswith('2026-09-06: ')
    assert ps['variants'][-1] is not None and ps['variants'][-1]['vhash'] == bad['vhash'], 'infeasible rows come last'
    oaat = [v for v in ps['variants'] if v['variant'] == 'autoISF_max_1.3__dura_ISF_weight_0.5'][0]
    assert oaat['values'] == {'profile autoISF_max': 1.3, 'profile dura_ISF_weight': 0.5}


@needs_fixture
def test_parameter_space_without_base(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    rows = [l for l in open(sd / 'trials.jsonl', encoding='utf-8') if '"control":"base"' not in l]
    open(sd / 'trials.jsonl', 'w', encoding='utf-8').writelines(rows)
    os.remove(sd / 'round.json')
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={}, frozen_dir=str(tmp_path / 'nf'),
                               generated=GENERATED, log=lambda *_: None)
    ps = _tier1(open(res.path, encoding='utf-8').read())['parameters']
    assert ps['floors'] == {} and ps['base']['days'] == []
    assert len(ps['variants']) == 4 and all(v['feasible'] is False for v in ps['variants'])
    assert any('no base' in w for w in ps['warnings'])


# ----------------------------------------------------------------------------- H-404 Scenario comparison view

SCN_JS = os.path.join(TEMPLATES, 'views', 'scenarios.js')


def test_scenarios_view_strings_pass_the_lint():
    js = open(SCN_JS, encoding='utf-8').read()
    css = set(_view_css(js).split('\n'))                   # selectors are not prose (env(safe-area-...) is CSS)
    strings = [s for s in _js_strings(js) if ' ' in s and s not in css]
    assert len(strings) > 40, 'no strings extracted'
    for s in strings:
        assert not lint.lint_findings(s, 'line'), s


def test_scenarios_view_contract():
    js = open(SCN_JS, encoding='utf-8').read()
    assert "HC.registerView('scenarios'" in js, 'replaces the H-401 placeholder under the same id'
    code = re.sub(r'(?s)/\*.*?\*/', ' ', js)
    assert 'http' not in code, 'the view loads nothing from the network'
    assert 'HC.loadTier2(' in code, 'series come through the tier-2 loader (with its not-from-disk fallback)'
    for hook in ('add', 'replace', 'remove', 'setMode', 'onLoop', 'focusLoop', 'refresh', 'buildOption'):
        assert re.search(r'\b%s:\s*(function|[a-zA-Z]+)' % hook, code), hook
    css = _view_css(js)
    rules = [r for r in css.split('\n') if r.strip()]
    assert rules and all(r.startswith('#view-scenarios') or r.startswith('@media') for r in rules), 'styles stay scoped'
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(', css), 'literal colour in the view CSS (use page.css tokens)'
    assert not re.search(r"#[0-9a-fA-F]{6}\b|rgba?\(", code), 'literal colour in the view code'
    tokens = set(re.findall(r'var\(--([a-z0-9-]+)\)', css))
    css_tokens = _tokens(re.search(r'(?s)^:root\s*\{(.*?)\n\}', _read('page.css'), re.M).group(1))
    assert tokens <= css_tokens, sorted(tokens - css_tokens)
    used = set(re.findall(r"(?<![.\w])t\['([a-z0-9-]+)'\]|(?<![.\w])t\.([a-z]+)\b", code))
    theme_tokens = set(re.findall(r"'([a-z0-9-]+)'", re.search(r'(?s)var TOKENS = \[(.*?)\];', _read('theme.js')).group(1)))
    for a, b in used:
        assert (a or b) in theme_tokens, a or b
    assert "link: [{ xAxisIndex: 'all' }]" in code and "type: 'slider'" in code
    assert "var TAGS = ['A', 'B', 'C'];" in code and 'Three is the most this chart compares' in code
    assert "'series-4'" not in code and 'yAxisIndex: 1' not in code



def test_scenarios_view_node_syntax():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    subprocess.run([node, '--check', SCN_JS], check=True, capture_output=True)


_SMOKE = r"""
const fs = require('fs'), vm = require('vm');
const [js, dataPath] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
const num = (v, d) => (v === null || v === undefined || v !== v) ? '-' : Number(v).toFixed(d === undefined ? 2 : d);
const HC = { registerView() {}, h() {}, theme: { onChange() {}, current() { return {}; } },
             fmt: { num, signed: (v, d) => (v > 0 ? '+' : v < 0 ? '-' : '') + num(Math.abs(v), d) },
             data: { scenarios: input.scenarios, page: { units: 'mmol' } } };
const ctx = { window: { HC }, console, JSON, Math };
vm.runInNewContext(fs.readFileSync(js, 'utf8'), ctx);
const S = HC.scenarios, t = {};
['series-1', 'series-2', 'series-3', 'phone', 'ink', 'ink-2', 'muted', 'grid', 'axis', 'surface', 'div-neg', 'div-mid', 'div-pos'].forEach(k => t[k] = '<' + k + '>');
const ents = input.ents.map((e, i) => ({ tag: 'ABC'[i], slot: i + 1, day: input.day, vhash: e.vhash, e: e.entity, doc: e.doc }));
const origs = {}; origs[input.day] = input.orig;
const opt = S.buildOption(t, [input.day], ents, origs, input.narrow, null);
const colours = new Set();
opt.series.forEach(s => { if (s.lineStyle) colours.add(s.lineStyle.color); if (s.itemStyle) colours.add(s.itemStyle.color); });
const sample = opt.series.filter(s => s.type === 'custom').map(s => { let n = 0; s.data.forEach((d, i) => { const r = s.renderItem({}, { value: k => d[k], coord: p => [p[0] / 1000, -p[1] * 100], size: () => [3, 0] }); if (r) n++; }); return n; });
console.log(JSON.stringify({ grids: opt.grid.length, x: opt.xAxis.length, y: opt.yAxis.length,
  mismatched: opt.series.filter(s => s.xAxisIndex !== s.yAxisIndex).length, colours: [...colours], titles: opt.title.map(x => x.text),
  zoomAxes: opt.dataZoom[0].xAxisIndex.length, zooms: opt.dataZoom.length, link: opt.axisPointer.link, drawn: sample,
  caps: opt.series.filter(s => s.name === 'cap').map(s => s.markLine.data.map(d => d.yAxis)),
  tip: opt.tooltip.formatter([{ axisValue: input.orig.t[10] * 1000 }]).length }));
"""


@needs_fixture
def test_scenarios_option_builder_on_the_fixture(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    res = _render(tmp_path)
    data = _tier1(open(res.path, encoding='utf-8').read())
    sc = data['scenarios']
    out = tmp_path / 'report'
    day = '2026-09-06'
    picks = [e for e in sc['entities'] if day in e['series']][:3]
    inp = {'scenarios': sc, 'day': day, 'narrow': False,
           'orig': json.load(open(out / sc['days'][0]['orig'])),
           'ents': [{'vhash': e['vhash'], 'entity': e, 'doc': json.load(open(out / e['series'][day]))} for e in picks]}
    (tmp_path / 'in.json').write_text(json.dumps(inp))
    (tmp_path / 'smoke.js').write_text(_SMOKE)
    r = subprocess.run([node, str(tmp_path / 'smoke.js'), SCN_JS, str(tmp_path / 'in.json')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o['grids'] == o['x'] == o['y'] == 6 + 3, 'one x and one y axis per panel: 6 panels + one strip per entity'
    assert o['mismatched'] == 0, 'every series sits on its own grid: no dual axis'
    assert o['zoomAxes'] == o['grids'] and o['link'] == [{'xAxisIndex': 'all'}]
    variant_colours = {c for c in o['colours'] if c and c.startswith('<series-')}
    assert variant_colours == {'<series-1>', '<series-2>', '<series-3>'}, 'three slots, three colours, never a fourth'
    assert '<phone>' in o['colours']
    assert any(t.startswith('A SMB minus the phone') for t in o['titles']) and len(o['titles']) == 9
    assert o['drawn'][0] > 0, 'the phone has SMB stems on the fixture day'
    assert sorted(o['caps'][0]) == [1.3, 1.5], 'one cap hairline per distinct autoISF_max (phone 1.3, a variant 1.5)'
    assert o['tip'] > 100
    for s in o['titles']:
        assert not lint.lint_findings(s, 'line'), s


@needs_fixture
def test_scenario_index_of_the_fixture(tmp_path):
    res = _render(tmp_path)
    data = _tier1(open(res.path, encoding='utf-8').read())
    for key in ('honesty', 'trials', 'rounds', 'overview', 'assumed', 'page', 'parameters'):     # nothing dropped
        assert key in data
    sc = data['scenarios']
    assert sc['slots'] == 3 and sc['smb_tol_U'] == 0.05 and sc['iob_tolerance'] == 1.3
    assert sc['days'] == [{'day': '2026-09-06', 'role': 'search', 'tags': [], 'confounders': [],
                           'orig': 'series/2026-09-06/orig.json'}]
    ents = sc['entities']
    assert len(ents) == 5 and ents[0]['base'] and ents[0]['vhash'] == contracts.BASE_VHASH
    assert [e['vhash'] for e in ents[1:]] == data['parameters']['order'], 'variants in review order after base'
    for e in ents:
        for path in e['series'].values():
            assert path in res.tier2, path                  # every named series is next to the page
    assert sc['phone']['caps']['autoISF_max'] == 1.3        # the study space's phone value wins over settings
    b85 = [e for e in ents if e['vhash'].startswith('b85a419e')][0]
    assert b85['caps']['autoISF_max'] == 1.5 and b85['label'] == 'autoISF_max 1.5, dura_ISF_weight 0.5'
    same = [e for e in ents if e['variant'] == 'autoISF_max_1.3__dura_ISF_weight_0.7'][0]
    assert same['label'].endswith("(the phone's values)")
    assert sc['defaults'] == [{'vhash': data['parameters']['order'][0], 'day': '2026-09-06'},
                              {'vhash': contracts.BASE_VHASH, 'day': '2026-09-06'}]
    for s in report.page_sentences(data):
        lint.lint_text(s, 'line')


def test_scenario_phone_value_falls_back_to_settings():
    study = contracts.Study.load(os.path.join(FIXTURE, 'study.json'), strict=False) if os.path.isfile(
        os.path.join(FIXTURE, 'study.json')) else None
    if study is None:
        pytest.skip('fixture study missing')
    assert report._phone_value(study, {'autoisf': {'autoISF_min': 0.4}}, 'autoISF_min') == 0.4
    assert report._phone_value(study, {}, 'autoISF_min') is None
    assert report._phone_value(study, {'autoisf': {'autoISF_max': 1.9}}, 'autoISF_max') == 1.3
    assert report._short_label({'profile a': 1, 'profile b': 2}, {'profile a': 1}) == 'b 2'


@needs_fixture
def test_scenario_index_without_series_or_base(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    shutil.rmtree(sd / 'series')
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={}, frozen_dir=str(tmp_path / 'nf'),
                               generated=GENERATED, log=lambda *_: None)
    sc = _tier1(open(res.path, encoding='utf-8').read())['scenarios']
    assert sc['defaults'] == [] and sc['days'][0]['orig'] is None
    assert all(e['series'] == {} for e in sc['entities'])


# ----------------------------------------------------------------------------- H-405 Loop drill-down

DD_JS = os.path.join(TEMPLATES, 'views', 'drilldown.js')
PRED_FIXTURE = os.path.join(os.path.dirname(FIXTURE), 'report')
DD_PREFIXES = ('#hc-drilldown', '#view-loop', '#view-scenarios table.twin', 'html.hc-dd-open', '@media')


def test_drilldown_view_strings_pass_the_lint():
    js = open(DD_JS, encoding='utf-8').read()
    css = set(_view_css(js).split('\n'))
    strings = [s for s in _js_strings(js) if ' ' in s and s not in css]
    assert len(strings) > 40, 'no strings extracted'
    for s in strings:
        assert not lint.lint_findings(s, 'line'), s


def test_drilldown_view_contract():
    js = open(DD_JS, encoding='utf-8').read()
    assert "HC.registerView('loop'" in js, 'replaces the H-401 placeholder under the same id'
    code = re.sub(r'(?s)/\*.*?\*/', ' ', js)
    assert 'http' not in code, 'the view loads nothing from the network'
    assert 'HC.loadTier2(' in code and 'fetch(' not in code, 'tier-2 documents come through the loader only'
    for hook in ('open', 'close', 'onOpen', 'onClose', 'buildModel', 'fanOption', 'wordDiff', 'lineDiff', 'parseReason', 'lowAfter'):
        assert re.search(r'\b%s:\s*(function|[a-zA-Z]+)' % hook, code), hook
    css = _view_css(js)
    rules = [r for r in css.split('\n') if r.strip()]
    assert rules and all(r.startswith(DD_PREFIXES) for r in rules), 'styles stay scoped'
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(', css), 'literal colour in the view CSS (use page.css tokens)'
    assert not re.search(r"#[0-9a-fA-F]{6}\b|rgba?\(", code), 'literal colour in the view code'
    tokens = set(re.findall(r'var\(--([a-z0-9-]+)\)', css))
    css_tokens = _tokens(re.search(r'(?s)^:root\s*\{(.*?)\n\}', _read('page.css'), re.M).group(1))
    assert tokens <= css_tokens, sorted(tokens - css_tokens)
    used = set(re.findall(r"(?<![.\w])t\['([a-z0-9-]+)'\]|(?<![.\w])t\.([a-z]+)\b", code))
    theme_tokens = set(re.findall(r"'([a-z0-9-]+)'", re.search(r'(?s)var TOKENS = \[(.*?)\];', _read('theme.js')).group(1)))
    for a, b in used:
        assert (a or b) in theme_tokens, a or b
    assert 'inset 3px 0 0 var(--series-1)' in css, 'differing cells carry a 3 px stripe in the variant colour'
    assert "'aria-modal': 'true'" in code and "ev.key === 'Escape'" in code and 'opener' in code
    assert 'yAxisIndex' not in code and "'series-4'" not in code, 'one axis, three slots at most'
    assert 'max-width: 759px' in css and 'width: 100%' in css, 'full screen at phone width'


def test_drilldown_view_node_syntax():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    subprocess.run([node, '--check', DD_JS], check=True, capture_output=True)


_DD_SMOKE = r"""
const fs = require('fs'), vm = require('vm');
const [js, dir, base] = process.argv.slice(2);
const rd = p => JSON.parse(fs.readFileSync(dir + '/' + p, 'utf8'));
const num = (v, d) => (v === null || v === undefined || v !== v) ? '-' : Number(v).toFixed(d === undefined ? 2 : d);
const HC = { registerView() {}, h() {}, theme: { onChange() {}, current() { return {}; } },
             fmt: { num, int: v => String(v), signed: (v, d) => (v > 0 ? '+' : v < 0 ? '-' : '') + num(Math.abs(v), d) },
             data: { page: { units: 'mmol' } } };
const document = { readyState: 'complete', addEventListener() {}, getElementById() { return null; } };
const ctx = { window: { HC }, document, console, JSON, Math, Uint32Array, setTimeout() {}, Promise };
vm.runInNewContext(fs.readFileSync(js, 'utf8'), ctx);
const D = HC.drilldown;
const docs = { orig: rd('series/2026-09-06/orig.json'), series: rd('series/2026-09-06/' + base + '.json'), base: null,
               pred: rd('pred/2026-09-06/' + base + '.json'), predText: rd('pred/2026-09-06/' + base + '.text.json'),
               predOrig: rd('pred/2026-09-06/orig.json'), predOrigText: rd('pred/2026-09-06/orig.text.json') };
const m = D.buildModel({ day: '2026-09-06', index: 5, vhash: base }, docs, { isBase: true, units: 'mmol' });
const t = {}; ['series-1', 'series-2', 'series-3', 'phone', 'ink', 'ink-2', 'muted'].forEach(k => t[k] = '<' + k + '>');
const opt = D.fanOption(t, m, 'mmol', 1, false);
const colours = [...new Set(opt.series.map(s => s.lineStyle.color))];
const low = D.lowAfter({ t: [0, 60, 120, 9000], bg: [100, 90, 69, 60], events: [] }, 0, 70.2, 7200);
const lowEv = D.lowAfter({ t: [0, 60], bg: [100, 90], events: [{ kind: 'low', t: 3000, value: 66 }] }, 0, 70.2, 7200);
const none = D.lowAfter({ t: [0, 60, 8000], bg: [100, 90, 60], events: [] }, 0, 70.2, 7200);
const wd = D.wordDiff('insulinReq 0.12; setting 60m low temp', 'insulinReq 0.13; setting 120m low temp');
const ld = D.lineDiff(['ISF unchanged: 5.1', 'CR: 13.0', 'only phone'], ['ISF unchanged: 91.8', 'CR: 13.0', 'only variant']);
console.log(JSON.stringify({
  rows: m.rows.map(r => [r.label, r.phone, r.variant, r.differs]), ref: m.ref, t: m.t, idx: m.index,
  fans: [!!m.fans.phone, !!m.fans.variant, m.fans.phone && m.fans.phone.iob.length],
  reason: !!m.reason.diff, consoleRuns: m.console.diff.length, x: Array.isArray(opt.xAxis), y: Array.isArray(opt.yAxis),
  guards: opt.series[0].markLine.data.map(d => d.yAxis), colours, dashes: opt.series.map(s => s.lineStyle.type),
  names: opt.series.map(s => s.name), low, lowEv, none,
  wd: wd.map(r => r.op + (r.a || r.b)), ld: ld.map(r => r.op),
  parsed: D.parseReason('COB: 0, minPredBG 6.1, minGuardBG 7.4, IOBpredBG 4.7; Eventual BG 7.7 >= 5.6', 'mmol'),
  parsedMg: D.parseReason('minGuardBG 16, minPredBG: 111', 'mg/dl')
}));
"""


def test_drilldown_model_and_fans_on_the_pred_fixture(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    if not os.path.isfile(os.path.join(PRED_FIXTURE, 'pred', '2026-09-06', 'orig.json')):
        pytest.skip('tests/fixtures/report missing')
    (tmp_path / 'smoke.js').write_text(_DD_SMOKE)
    r = subprocess.run([node, str(tmp_path / 'smoke.js'), DD_JS, PRED_FIXTURE, contracts.BASE_VHASH],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    labels = [x[0] for x in o['rows']]
    assert labels == ['insulinReq', 'SMB', 'maxBolus', 'TBR', 'final ISF factor', 'eventualBG', 'minPredBG', 'minGuardBG']
    assert o['ref'] == 'phone' and o['idx'] == 5, 'the base variant is read against the phone'
    phone = dict((x[0], x[1]) for x in o['rows'])
    assert all(isinstance(phone[k], (int, float)) for k in ('eventualBG', 'minPredBG', 'minGuardBG')), 'read from the reason text'
    assert o['fans'][0] and o['fans'][1] and o['fans'][2] > 10
    assert o['reason'] and o['consoleRuns'] > 10
    assert o['x'] is False and o['y'] is False, 'one x and one y axis: never a dual axis'
    assert o['guards'] == [3.8, 5.4], 'guard hairlines at 3.8 / 5.4 mmol/l'
    assert set(o['colours']) == {'<phone>', '<series-1>'}, 'the phone grey, the variant in its slot colour'
    assert 'solid' in o['dashes'] and [6, 4] in o['dashes'], 'IOB solid, ZT dashed'
    assert all(n.split(' ')[0] in ('phone', 'variant') for n in o['names'])
    assert o['low'] == {'t': 120, 'bg': 69} and o['lowEv'] == {'t': 3000, 'bg': 66} and o['none'] is None, \
        'the first recorded low within 2 h, positional only'
    assert '-0.12;' in o['wd'] and '+0.13;' in o['wd'] and '-60m' in o['wd'] and '+120m' in o['wd'] and '=setting' in o['wd']
    assert o['ld'] == ['~', '=', '-', '+'], 'lines aligned on wording, numbers compared word by word'
    assert o['parsed'] == {'eventual': 138.6, 'minPred': 109.8, 'minGuard': 133.2}
    assert o['parsedMg'] == {'eventual': None, 'minPred': 111, 'minGuard': 16}
    cob = [d for n, d in zip(o['names'], o['dashes']) if n.endswith(' COB')]
    assert all(d == 'solid' for d in cob), 'dashes are reserved for ZT / UAM: COB is a thin solid line'


_CLICK_SMOKE = r"""
const fs = require('fs'), vm = require('vm');
const [scn, dd] = process.argv.slice(2);
const num = (v, d) => (v === null || v === undefined || v !== v) ? '-' : Number(v).toFixed(d === undefined ? 2 : d);
const HC = { registerView() {}, h() { return null; }, theme: { onChange() {}, current() { return {}; } },
             fmt: { num, int: v => String(v), signed: (v, d) => (v > 0 ? '+' : v < 0 ? '-' : '') + num(Math.abs(v), d) },
             data: { scenarios: { days: [], entities: [] }, page: { units: 'mmol' } } };
const listeners = {}, timers = [];
const document = { readyState: 'complete', activeElement: null, body: {},
                   addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
                   getElementById() { return null; }, querySelector() { return null; } };
const ctx = { window: { HC, innerWidth: 1200 }, document, console, JSON, Math, Uint32Array, Promise,
              setTimeout(fn) { timers.push(fn); }, requestAnimationFrame() {} };
vm.runInNewContext(fs.readFileSync(scn, 'utf8'), ctx);
vm.runInNewContext(fs.readFileSync(dd, 'utf8'), ctx);
const calls = [];
HC.drilldown.open = spec => { calls.push({ day: spec.day, t: spec.t, index: spec.index, vhash: spec.vhash }); return Promise.resolve(null); };
timers.splice(0).forEach(fn => fn());                    // the drill-down hooks HC.scenarios.onLoop
// a chart stub with ECharts' finder semantics: an axis finder converts ONE number (an [x, y] pair gives NaN)
const day = '2026-09-06', t = []; for (let k = 0; k < 40; k++) t.push(21600 + 300 * k);
const t0 = t[0] * 1000, span = (t[t.length - 1] - t[0]) * 1000, L = 50, W = 1000;
let onClick = null;
const chart = {
  getZr() { return { on(type, fn) { if (type === 'click') onClick = fn; } }; },
  getOption() { return { grid: [{}, {}, {}] }; },
  containPixel(f, p) { const g = f.gridIndex; return p[0] >= L && p[0] <= L + W && p[1] >= g * 100 && p[1] <= g * 100 + 80; },
  convertFromPixel(f, v) {
    if (f.xAxisIndex === undefined || typeof v !== 'number') return NaN;
    return t0 + (v - L) / W * span;
  }
};
const f = { model: { days: [day], origs: { [day]: { t } }, ents: [{ vhash: 'v2', day, slot: 2 }] }, rec: { chart }, group: null };
HC.scenarios._wire(f);
const px = k => L + (t[k] - t[0]) * 1000 / span * W;
onClick({ offsetX: px(10), offsetY: 140 });              // a loop in the second panel
onClick({ offsetX: px(3), offsetY: 285 });               // below the last panel (the zoom slider): nothing
const viaLoopAt = HC.scenarios.loopAt(chart, f.model, px(25), 20);
// a row of the Scenarios table twin names its own loop and variant
const attrs = { 'data-day': day, 'data-t': String(t[7]), 'data-index': '7', 'data-vhash': 'dayvar', 'data-slot': '0' };
const tr = { getAttribute: k => (k in attrs ? attrs[k] : null), closest() { return null; } };
const target = { closest: sel => sel.indexOf('#view-scenarios details.twin tbody tr') === 0 ? tr : null };
(listeners.click || []).forEach(fn => fn({ target }));
// a days-mode variant without a slot is drawn in ink, not in slot 1's colour
const tk = {}; ['series-1', 'series-2', 'series-3', 'phone', 'ink', 'ink-2', 'muted', 'grid', 'axis', 'surface', 'div-neg', 'div-mid', 'div-pos'].forEach(k => tk[k] = '<' + k + '>');
const orig = { t, bg: t.map(() => 120), smb: t.map(() => 0), ins_req: t.map(() => 0.1), tbr: t.map(() => 1), isf_factor: t.map(() => 1), iob: t.map(() => 1) };
const doc = Object.assign({}, orig, { smb: t.map(() => 0.2), iob_th: t.map(() => 3) });
const opt = HC.scenarios.buildOption(tk, [day], [{ tag: 'variant', slot: 0, day, vhash: 'dayvar', e: { label: 'x', caps: {} }, doc }], { [day]: orig }, false, null);
const colours = new Set(); opt.series.forEach(s => { if (s.lineStyle) colours.add(s.lineStyle.color); if (s.itemStyle) colours.add(s.itemStyle.color); });
console.log(JSON.stringify({ calls, viaLoopAt, t10: t[10], t7: t[7], colours: [...colours].filter(Boolean) }));
"""


def test_scenarios_click_and_twin_rows_open_the_drilldown(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not on PATH')
    (tmp_path / 'click.js').write_text(_CLICK_SMOKE)
    r = subprocess.run([node, str(tmp_path / 'click.js'), SCN_JS, DD_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o['calls'][0] == {'day': '2026-09-06', 't': o['t10'], 'index': 10, 'vhash': 'v2'}, \
        'a click on a panel opens the drill-down at that loop, for the lowest slot shown'
    assert len(o['calls']) == 2, 'a click outside every panel (the zoom slider) opens nothing'
    assert o['viaLoopAt'] == 25
    assert o['calls'][1] == {'day': '2026-09-06', 't': o['t7'], 'index': 7, 'vhash': 'dayvar'}, \
        'a table twin row opens its own variant (days mode: the day variant, not slot A)'
    variant = {c for c in o['colours'] if c.startswith('<series-')}
    assert not variant and '<ink>' in o['colours'], 'a variant without a slot is drawn in ink'


def test_view_details_from_the_wave3_review():
    scn = re.sub(r'(?s)/\*.*?\*/', ' ', open(SCN_JS, encoding='utf-8').read())
    assert 'convertFromPixel({ xAxisIndex: 0 }, [' not in scn, 'an axis finder takes one number'
    assert 'slotOf(vh) || 1' not in scn, 'a days-mode variant without a slot never borrows slot 1'
    legend = [r for r in _view_css(scn).split('\n') if r.startswith('#view-scenarios .sc-legend span')]
    assert legend and not any('nowrap' in r for r in legend), 'legend items wrap at 400 px'
    dd = open(DD_JS, encoding='utf-8').read()
    assert 'keepFocus: true' in dd and 'if (!spec.keepFocus) s.close.focus()' in dd, 'the variant select keeps the focus'
    cob = [r for r in _view_css(dd).split('\n') if '.sw.cob' in r]
    assert cob and 'dashed' not in cob[0] and 'dotted' not in cob[0], 'the COB swatch is solid like its line'
    assert 'dash-dot' not in dd and 'dashdot' not in dd
    par = open(VIEW_JS, encoding='utf-8').read()
    body = re.search(r'(?s)function verdictPill\(v\) \{(.*?)\n  \}', par).group(1)
    assert 'pill good' not in body and 'pill warning' not in body, 'status colours are for feasibility only'


@needs_fixture
def test_drilldown_index_of_the_fixture(tmp_path):
    data = _tier1(open(_render(tmp_path).path, encoding='utf-8').read())
    dd = data['drilldown']
    assert dd['pred'] == {} and dd['pred_orig'] == {}, 'the fixture study has no prediction documents'
    assert dd['orig'] == {'2026-09-06': 'series/2026-09-06/orig.json'}
    assert set(dd['series']['2026-09-06']) == {e['vhash'] for e in data['scenarios']['entities']}
    assert dd['guards_mmol'] == [3.8, 5.4] and dd['guards_mgdl'] == [68.4, 97.2]
    assert dd['low_mgdl'] == 70.2 and dd['low_follow_s'] == 7200 and dd['tol']['insulin'] == 0.05
    assert dd['base_vhash'] == contracts.BASE_VHASH
    page = open(os.path.join(tmp_path, 'report', 'index.html'), encoding='utf-8').read()
    assert '/* drilldown.js' in page and page.index('/* drilldown.js') < page.index('/* parameters.js */')


@needs_fixture
def test_drilldown_index_names_pred_documents(tmp_path):
    if not os.path.isdir(os.path.join(PRED_FIXTURE, 'pred')):
        pytest.skip('tests/fixtures/report missing')
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    shutil.copytree(os.path.join(PRED_FIXTURE, 'pred'), sd / 'report' / 'pred')
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings={'nightscout': {'units': 'mmol'}},
                               frozen_dir=str(tmp_path / 'nf'), generated=GENERATED, log=lambda *_: None)
    dd = _tier1(open(res.path, encoding='utf-8').read())['drilldown']
    b = contracts.BASE_VHASH
    assert dd['pred'] == {'2026-09-06': {b: 'pred/2026-09-06/%s.json' % b}}
    assert dd['pred_text'] == {'2026-09-06': {b: 'pred/2026-09-06/%s.text.json' % b}}
    assert dd['pred_orig'] == {'2026-09-06': 'pred/2026-09-06/orig.json'}
    assert dd['pred_orig_text'] == {'2026-09-06': 'pred/2026-09-06/orig.text.json'}
    for p in ('pred/2026-09-06/orig.json', 'pred/2026-09-06/%s.text.json' % b):
        assert os.path.isfile(tmp_path / 'out' / p), 'placed next to the page for the lazy fetch'


# ----------------------------------------------------------------------------- no Nightscout address on the page

def _private_hits(page):
    """'vivikusi' anywhere, and every https:// URL other than the pinned ECharts script."""
    hits = re.findall(r'(?i)vivikusi', page)
    hits += [u for u in re.findall(r'https://[^\s"\'<>)]+', page) if u != report.ECHARTS_URL]
    return hits


@needs_fixture
@pytest.mark.parametrize('mode', ['normal', 'single_file', 'artifact'])
def test_page_never_carries_the_nightscout_host(tmp_path, mode):
    with open(os.path.join(FIXTURE, 'study.json'), encoding='utf-8') as fh:
        assert 'vivikusi' in fh.read()                       # the fixture does carry it: the scrub is exercised
    settings = dict(_settings(), nightscout={'units': 'mmol', 'url': 'https://ns.vivikusi.ch'})
    kw = {'single_file': mode == 'single_file', 'artifact': mode == 'artifact'}
    res = _render(tmp_path, settings=settings, **kw)
    with open(res.path, encoding='utf-8') as fh:
        page = fh.read()
    assert _private_hits(page) == []
    data = _tier1(page)
    assert data['study']['source'] == 'Nightscout' and not data['study']['host']
    assert data['page']['source'] == 'Nightscout' and not data['page']['host']


def test_source_label_and_scrub():
    assert report.source_label('ns:') == 'Nightscout'
    assert report.source_label('https://ns.example.org') == 'Nightscout'
    assert report.source_label('/home/me/aapsLogs/*.zip') == 'AAPS logfiles'
    study = types.SimpleNamespace(source='https://ns.example.org/', host='ns.example.org')
    private = report.private_strings(study, {'nightscout': {'url': 'https://other.example.net'}})
    assert set(private) >= {'ns.example.org', 'other.example.net'}
    doc = report.scrub_private({'note': 'from https://ns.example.org/api/v1 and other.example.net', 'n': 1}, private)
    assert doc == {'note': 'from Nightscout and Nightscout', 'n': 1}
    assert report.private_problems('<p>NS.EXAMPLE.ORG</p>', private) == ['ns.example.org']


def _settings_host():
    """The host of nightscout.url in the real ns_settings.json (looked up, never written into a test)."""
    settings = report.load_settings(contracts.NS_SETTINGS_PATH)
    return report._host_of(((settings.get('nightscout') or {}).get('url')) or '')


@needs_fixture
@pytest.mark.parametrize('single_file', [False, True])
def test_page_never_carries_the_settings_host(tmp_path, single_file):
    """The settings file is read at render time (settings_path, no settings dict): its Nightscout host appears
    nowhere on the page - tier-1 JSON, page.host, inlined tier-2 copies, the views."""
    host = _settings_host()
    if not host:
        pytest.skip('ns_settings.json names no nightscout.url')
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    with open(sd / 'study.json', encoding='utf-8') as fh:
        doc = json.load(fh)
    doc['source'], doc['host'] = 'https://%s/' % host, host
    doc['notes'] = list(doc.get('notes') or []) + ['replayed from https://%s/api/v1' % host]
    with open(sd / 'study.json', 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), single_file=single_file, variants='all',
                               settings_path=contracts.NS_SETTINGS_PATH, frozen_dir=str(tmp_path / 'nf'),
                               generated=GENERATED, log=lambda *_: None)
    with open(res.path, encoding='utf-8') as fh:
        page = fh.read()
    assert host.lower() not in page.lower()
    data = _tier1(page)
    assert host.lower() not in json.dumps(data).lower()
    assert data['page']['host'] == '' and data['study']['host'] == ''
    for p in res.tier2:                                       # the copies next to the page carry no host either
        with open(os.path.join(res.out_dir, *p.split('/')), encoding='utf-8') as fh:
            assert host.lower() not in fh.read().lower()


@needs_fixture
def test_single_file_pack_round_trip_and_slim_tier1(tmp_path):
    res = _render(tmp_path, single_file=True, variants='all')
    text = open(res.path, encoding='utf-8').read()
    packed = {p: json.loads(b) for p, b in
              re.findall(r'<script type="application/json" data-tier2="([^"]+)">(.*?)</script>', text, re.S)}
    assert len(packed) == 6
    refs = [v for d in packed.values() for v in d.values() if isinstance(v, dict) and '$ref' in v]
    assert refs, 'the variants share arrays with the orig / base documents of the day'
    assert all(r['$ref'] in packed for r in refs)
    assert not any(k in d for d in packed.values() for k in report.INLINE_DROP_FIELDS)
    orig = packed['series/2026-09-06/orig.json']
    assert not any(isinstance(v, dict) and '$ref' in v for v in orig.values()), 'the orig document is complete'
    for path, doc in report.unpack_inline(packed).items():
        with open(os.path.join(FIXTURE, *path.split('/')), encoding='utf-8') as fh:
            src = json.load(fh)
        assert doc == {k: v for k, v in src.items() if k not in report.INLINE_DROP_FIELDS}, path
    # the dropped fields are the ones no view reads
    views = ''.join(open(os.path.join(TEMPLATES, n), encoding='utf-8').read() for n in ('page.js',)) + \
        ''.join(open(f, encoding='utf-8').read() for f in sorted(
            os.path.join(report.VIEWS_DIR, n) for n in os.listdir(report.VIEWS_DIR) if n.endswith('.js')))
    for k in report.INLINE_DROP_FIELDS:
        assert not re.search(r"""[.'"]%s\b(?!_)""" % re.escape(k), views), k
    # tier 1: slim trial rows, metrics on the base rows only, rounds without the per-variant copy
    data = _tier1(text)
    for r in data['trials']:
        assert set(r) <= set(report.PAGE_TRIAL_KEYS) | {'metrics'}
        assert ('metrics' in r) == bool(r.get('control') or r['vhash'] == contracts.BASE_VHASH)
    assert all('variants' not in r for r in data['rounds']) and data['rounds'][0]['front']


def test_pack_inline_keeps_unlike_arrays_and_types():
    n = report.INLINE_REF_MIN
    o = {'kind': 'orig', 'day': 'd', 'vhash': contracts.BASE_VHASH, 't': list(range(n)), 'smb': [0] * n}
    b = {'kind': 'series', 'day': 'd', 'vhash': contracts.BASE_VHASH, 't': list(range(n)), 'smb': [0.0] * n,
         'changed': [False] * n, 'autosens': [1] * n}
    v = {'kind': 'series', 'day': 'd', 'vhash': 'v' * 40, 't': list(range(n)), 'smb': [0.0] * (n - 1) + [0.5],
         'changed': [True] * n}
    docs = {'o': o, 'b': b, 'v': v}
    packed = report.pack_inline(docs)
    assert packed['o'] == o and packed['b']['t'] == {'$ref': 'o'}
    assert isinstance(packed['b']['smb'], list), '0.0 is not the int 0 of the orig document'
    assert packed['v']['smb'] == {'$ref': 'b', '$diff': [n - 1, 0.5]}
    assert isinstance(packed['v']['changed'], list), 'every entry differs: the array is kept'
    un = report.unpack_inline(packed)
    assert un['v'] == v and un['b'] == {k: x for k, x in b.items() if k != 'autosens'}


def _unlocked_copy(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    with open(sd / 'study.json', encoding='utf-8') as fh:
        doc = json.load(fh)
    doc['unlocked'] = ['profile sens']
    doc['unlock_reason'] = 'a profile scale study, reviewed by hand'
    with open(sd / 'study.json', 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return sd


@needs_fixture
def test_unlocked_study_shows_the_weak_evidence_caveat(tmp_path):
    plain = open(_render(tmp_path, out_name='plain').path, encoding='utf-8').read()
    assert 'id="weak-evidence"' not in plain and _tier1(plain)['page']['caveat'] is None
    sd = _unlocked_copy(tmp_path)
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings=_settings(),
                               frozen_dir=str(tmp_path / 'nf'), generated=GENERATED, log=lambda *_: None)
    page = open(res.path, encoding='utf-8').read()
    band = re.search(r'<div class="honesty-band">(.*?)</div>\s*</div>', page, re.S).group(1)
    assert 'id="honesty"' in band and 'id="weak-evidence"' in band, 'next to the honesty paragraph'
    caveat = _tier1(page)['page']['caveat']
    assert 'profile sens' in caveat and 'cannot judge a different profile' in caveat
    assert 'no glucose response' in caveat and report.NO_GLUCOSE_RESPONSE in caveat
    assert '<strong>Weak evidence:</strong>' in band and report._escape(caveat.partition(':')[2]) in band
    lint.lint_text(caveat, 'line')


def test_validation_lines_quote_tau_as_a_caveat():
    v = {'top': 3, 'validated': 3, 'search_days': 7, 'holdout_days': ['2026-09-10', '2026-09-11'], 'folds': 9,
         'tau': 0.333, 'recommended_for_review': 1, 'sign_share': 0.75}
    lines = report.validation_lines(v)
    assert lines[0] == 'Hold-out validation: 3 validated, 1 recommended for review, Kendall tau +0.33 over 9 days.'
    assert any('a caveat on how stable the order is, not a statistic' in x for x in lines)
    assert any('9 leave-one-day-out folds over the 7 search and 2 hold-out days' in x for x in lines)
    assert report.validation_lines(None) == [] and report.validation_lines({}) == []
    assert 'Kendall tau not computed' in report.validation_lines(dict(v, tau=None))[0]
    for x in lines:
        lint.lint_text(x, 'line')


@needs_fixture
def test_parameters_carry_the_validation_panel(tmp_path):
    sd = tmp_path / 'study'
    shutil.copytree(FIXTURE, sd)
    with open(sd / 'round.json', encoding='utf-8') as fh:
        rnd = json.load(fh)
    rnd['validation'] = {'top': 2, 'validated': 2, 'search_days': 1, 'holdout_days': ['2026-09-07'], 'folds': 2,
                         'tau': 1.0, 'recommended_for_review': 1, 'sign_share': 0.75}
    target = next(c for c in rnd['variants'] if c['verdict'] != 'base')
    target['validation'] = {'search_rank': 1, 'holdout_rank': 1, 'holdout_total': 2, 'component': 'rest_vs_base_U',
                            'direction': -1, 'holds': 1, 'holdout_days': 1, 'flip_days': [], 'missing_days': [],
                            'lodo_min': 1, 'lodo_max': 1, 'lodo_folds': 2, 'lodo_unranked': 0,
                            'feasible_all_days': True, 'infeasible_days': [], 'recommended_for_review': True,
                            'reasons': []}
    with open(sd / 'round.json', 'w', encoding='utf-8') as fh:
        json.dump(rnd, fh)
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings=_settings(),
                               frozen_dir=str(tmp_path / 'nf'), generated=GENERATED, log=lambda *_: None)
    ps = _tier1(open(res.path, encoding='utf-8').read())['parameters']
    assert ps['validation']['validated'] == 2
    assert ps['validation_lines'][0].startswith('Hold-out validation: 2 validated, 1 recommended for review')
    row = next(v for v in ps['variants'] if v['vhash'] == target['vhash'])
    assert row['validation']['line'].startswith('hold-out: rank 1 of 2')
    js = open(os.path.join(report.VIEWS_DIR, 'parameters.js'), encoding='utf-8').read()
    assert 'validation_lines' in js and 'v.validation.line' in js


def test_scenario_badges_use_the_shared_pill():
    js = open(os.path.join(report.VIEWS_DIR, 'scenarios.js'), encoding='utf-8').read()
    body = js[js.index('function badgesFor'):]
    body = body[:body.index('\n  }\n')]
    assert "class: 'chip" not in body and body.count("class: 'pill") >= 4
    css = open(os.path.join(TEMPLATES, 'page.css'), encoding='utf-8').read()
    assert re.search(r'^\.pill \{', css, re.M)
    pjs = open(os.path.join(report.VIEWS_DIR, 'parameters.js'), encoding='utf-8').read()
    assert "'pill serious' : 'pill'" in pjs                   # the verdict pills: the same class


# ----------------------------------------------------------------------------- scored days, floor multiples, caveats

def _base_row(day, agreement, extra_rise=0.0):
    from hindcast.tests.test_round import mk
    m = dict(mk(base=True), agreement_pct=agreement, loops=280, smb_exact_pct=agreement - 5,
             rise_emul_U=3.0 + extra_rise)
    return types.SimpleNamespace(status='done', metrics=m, control='base', vhash=contracts.BASE_VHASH, day=day,
                                 freeze_sha='')


def test_overview_floor_leaves_quarantined_days_out_but_lists_them():
    """DESIGN.md 5.4 phase 0: a quarantined day is listed with its reason, never the worst day of the banner."""
    S = types.SimpleNamespace
    study = S(days=[S(day='2026-09-01', role='search', quarantine_reason=None),
                    S(day='2026-09-02', role='holdout', quarantine_reason=None),
                    S(day='2026-09-24', role='quarantined', quarantine_reason='cgm gap 3 h')])
    rows = [_base_row('2026-09-01', 97.0, 0.1), _base_row('2026-09-02', 96.0, 0.2), _base_row('2026-09-24', 75.0, 3.0)]
    ov = report.overview_numbers(study, rows, [], {'2026-09-01': 24.0, '2026-09-02': 24.0})
    s = ov['summary']
    assert s['agreement_min_day'] == '2026-09-02' and s['agreement_min_pct'] == 96.0
    assert s['floor_rise_max_U'] == pytest.approx(0.2) and s['tie_rise_U'] == pytest.approx(0.4)
    assert s['scored_days_with_base'] == 2 and s['search_days_with_base'] == 1 and s['days_with_base'] == 3
    assert s['quarantined'] == [{'day': '2026-09-24', 'reason': 'cgm gap 3 h'}]
    assert '75.0 %' not in ov['banner'] and '2026-09-24' in ov['banner']
    assert 'cover the 2 scored days (search and hold-out' in ov['banner']
    assert 'cover the 1 scored search day only' in ov['banner'] and 'listed, not scored' in ov['banner']
    assert ov['days']['2026-09-24']['has_base'], 'the day card still shows its numbers'
    lint.lint_text(ov['banner'], 'line')
    # the same day set as status.json's floor (study.build_status: search + hold-out)
    from hindcast import study as st
    floor = st.aggregate_floor(rows, {'2026-09-01', '2026-09-02'})
    assert floor['agreement_pct'] == s['agreement_min_pct']
    only_q = report.overview_numbers(study, rows[2:], [], {})
    assert only_q['summary']['scored_days_with_base'] == 0 and 'No scored day' in only_q['banner']
    js = open(os.path.join(report.TEMPLATES_DIR, 'page.js'), encoding='utf-8').read()
    assert 's.quarantined' in js and 'quarantined, not scored' in js


def test_fallback_why_prints_the_floor_multiple_not_the_review_score():
    s = types.SimpleNamespace(floor_multiples={'rise_vs_base_U': 3.25}, review_component='rise_vs_base_U',
                              review_score=9.9)
    assert report._floor_multiple_of(s) == 3.25
    s.floor_multiples = {}
    assert report._floor_multiple_of(s) == 0.0                 # H-715: never the review score, not even as a fallback
    src = open(report.__file__, encoding='utf-8').read()
    assert "{s.review_score:.1f} x floor" not in src


def test_validation_block_carries_the_caveat_of_a_recommended_variant():
    from hindcast import metrics as hm
    v = {'search_rank': 1, 'holdout_rank': 1, 'holdout_total': 2, 'holds': 3, 'holdout_days': 3,
         'inside_floor_days': 1, 'lodo_min': 1, 'lodo_max': 2, 'recommended_for_review': True, 'reasons': []}
    out = report._validation_of(types.SimpleNamespace(validation=v))
    assert out['caveat'] == hm.validation_caveat(v) and out['caveat'].startswith('caveat: inside 2 x floor on 1')
    assert report._validation_of(types.SimpleNamespace(validation=dict(v, recommended_for_review=False)))['caveat'] is None
    for k in hm.VALIDATION_KEYS:
        assert k in report._validation_of.__doc__, k
    js = open(os.path.join(report.VIEWS_DIR, 'parameters.js'), encoding='utf-8').read()
    assert 'v.validation.caveat' in js
    assert report.NO_GLUCOSE_RESPONSE == contracts.NO_GLUCOSE_RESPONSE


# ----------------------------------------------------------------------------- GOAL.md: end goal, question earlier,
#                                                                               observed baseline, bolus coverage

def _copy_fixture(tmp_path, question=None, round_edit=None):
    sd = tmp_path / 'studies' / 'study'
    shutil.copytree(FIXTURE, sd)
    if question:
        with open(sd / 'study.json', encoding='utf-8') as fh:
            s = json.load(fh)
        s['question'] = question
        with open(sd / 'study.json', 'w', encoding='utf-8') as fh:
            json.dump(s, fh)
    if round_edit:
        with open(sd / 'round.json', encoding='utf-8') as fh:
            rnd = json.load(fh)
        round_edit(rnd)
        with open(sd / 'round.json', 'w', encoding='utf-8') as fh:
            json.dump(rnd, fh)
    return sd


def _render_copy(tmp_path, sd, **kw):
    res = report.render_report(str(sd), out_dir=str(tmp_path / 'out'), settings=_settings(),
                               frozen_dir=str(tmp_path / 'nf'), generated=GENERATED, log=lambda *_: None, **kw)
    text = open(res.path, encoding='utf-8').read()
    return text, _tier1(text)


def test_end_goal_sentence_passes_the_lint_and_names_no_outcome_statistic():
    lint.lint_text(report.END_GOAL, 'line')
    assert report.END_GOAL.startswith('End goal (Markus, 2026-10-17):')
    for must in ('aggressive', 'quicker', 'without manual boluses', '90 %', 'observed on the phone only',
                 'never computed here'):
        assert must in report.END_GOAL, must
    assert '<p class="honesty goal"' in report.goal_html(report.END_GOAL) and report.goal_html(None) == ''


@needs_fixture
def test_end_goal_sits_under_the_honesty_paragraph(tmp_path):
    text = open(_render(tmp_path).path, encoding='utf-8').read()
    assert text.index('id="honesty"') < text.index('id="end-goal"') < text.index('<nav class="tabs"')
    assert _tier1(text)['page']['goal'] == report.END_GOAL
    tpl = _read('page.html')
    assert tpl.index('$HONESTY') < tpl.index('$GOAL') < tpl.index('<nav')


@needs_fixture
def test_explore_study_has_no_pareto_block(tmp_path):
    ps = _tier1(open(_render(tmp_path).path, encoding='utf-8').read())['parameters']
    assert ps['pareto'] is None and ps['coverage'] is None


@needs_fixture
def test_question_earlier_carries_the_front_order_weights_and_a_missing_sensitivity_note(tmp_path):
    sd = _copy_fixture(tmp_path, 'earlier')
    _text, data = _render_copy(tmp_path, sd)
    pa = data['parameters']['pareto']
    assert pa['question'] == 'earlier' and [a['key'] for a in pa['axes']] == ['rise_vs_base_U', 'rest_pos_U', 'lowside_flags']
    assert [a['direction'] for a in pa['axes']] == ['up', 'down', 'down']
    assert pa['weights'] == {'rest': 2.0, 'lowside': 0.3} and pa['base'] == {'x': 0.0, 'y': 0.0, 'z': 0.0}
    assert pa['scalar_text'] == 'rise_vs_base_U - 2.0 x max(rest_vs_base_U, 0) - 0.3 x lowside_flags'
    pts = {p['variant']: p for p in pa['points']}
    assert len(pts) == 4 and all(pts[p['variant']]['front'] for p in pa['order'] if p['front'])
    # the marked front is the metrics front of the three objectives among the steps (rise above the floor: one 6 h
    # morning with a 1.38 U floor on rise has none, so the front is empty), coordinates are the means it used
    from hindcast import metrics as hm
    feas = {n: (-p['x'], p['y'], p['z']) for n, p in pts.items() if p['feasible'] and p['step']}
    assert sorted(hm.pareto_front(feas)) == sorted(n for n, p in pts.items() if p['front'])
    assert all(p['step'] for p in pa['points'] if p['front']) and 'among the' in pa['order_text']
    assert all(p['y'] >= 0 for p in pa['points'])
    # the review order: front first, then by the scalar; the leaderboard ranks follow it
    ranks = [o['rank'] for o in pa['order']]
    assert ranks == sorted(ranks) and ranks[0] == 1
    fronts = [o['front'] for o in pa['order']]
    assert fronts == sorted(fronts, reverse=True)
    lb = {v['variant']: v['position'] for v in data['parameters']['variants']}
    assert all(lb[o['variant']] == o['rank'] for o in pa['order']), 'the leaderboard rows follow the same order'
    se = pa['sensitivity']
    assert se['available'] is False and se['note'] == report.SENSITIVITY_MISSING and se['weights'] == []
    assert 'predates' in se['note']
    for s in report.page_sentences(data):
        lint.lint_text(s, 'line')


@needs_fixture
def test_question_earlier_reads_the_round_sensitivity_block(tmp_path):
    def edit(rnd):
        labels = [c['variant'] for c in rnd['variants'] if c['verdict'] != 'base']
        vh = {c['variant']: c['vhash'] for c in rnd['variants']}
        rnd['sensitivity'] = {'rest': {'0.5': list(reversed(labels)), '1.5': [vh[x][:12] for x in labels]},
                              'lowside': {0.5: labels, '1.5': labels}}
    sd = _copy_fixture(tmp_path, 'earlier', edit)
    _text, data = _render_copy(tmp_path, sd)
    se = data['parameters']['pareto']['sensitivity']
    assert se['available'] is True and [w['weight'] for w in se['weights']] == ['rest', 'lowside']
    rest = se['weights'][0]
    assert rest['value'] == 2.0 and all('_' in x for x in rest['1.5']), 'short vhashes mapped to labels'
    assert len(rest['0.5']) <= report.SENSITIVITY_TOP
    for s in report.page_sentences(data):
        lint.lint_text(s, 'line')


def test_sensitivity_view_same_top_and_missing_factor():
    variants = [{'vhash': 'a' * 40, 'variant': 'va'}, {'vhash': 'b' * 40, 'variant': 'vb'}]
    out = report.sensitivity_view('earlier', {'rest': 2.0, 'lowside': 0.3}, ['va', 'vb'],
                                  {'rest': {'0.5': ['va', 'vb'], '1.5': ['b' * 12, 'va']}, 'lowside': {'0.5': ['va']}},
                                  variants)
    rest, low = out['weights']
    assert rest['1.5'] == ['vb', 'va'] and rest['same_top'] is False
    assert low['1.5'] is None and low['same_top'] is False
    assert out['note'].startswith('0 of 2 weights leave the top 5 of the scalar order unchanged')
    lint.lint_text(out['note'], 'line')
    same = report.sensitivity_view('earlier', {}, ['va'], {'rest': {'0.5': ['va'], '1.5': ['va']}}, variants)
    assert same['weights'][0]['same_top'] is True
    assert report.sensitivity_view('explore', {}, [], None, variants)['available'] is False
    assert report.pareto_view('explore', object(), []) is None


def test_read_round_extras_tolerates_missing_and_odd_files(tmp_path):
    sd = types.SimpleNamespace(round_path=str(tmp_path / 'round.json'))
    assert report.read_round_extras(sd) == {'sensitivity': None}
    (tmp_path / 'round.json').write_text('[1, 2]')
    assert report.read_round_extras(sd) == {'sensitivity': None}
    (tmp_path / 'round.json').write_text(json.dumps({'sensitivity': {'rest': {'0.5': ['x']}}}))
    assert report.read_round_extras(sd)['sensitivity'] == {'rest': {'0.5': ['x']}}


@needs_fixture
def test_coverage_columns_from_the_round_effect(tmp_path):
    def edit(rnd):
        for i, c in enumerate(rnd['variants']):
            if c['verdict'] != 'base':
                c['effect']['coverage_U_vs_base'] = 0.25 * i
                c['effect']['boluses_preempted'] = 0.5
    sd = _copy_fixture(tmp_path, None, edit)
    _text, data = _render_copy(tmp_path, sd)
    ps = data['parameters']
    assert [c['key'] for c in ps['coverage']['columns']] == ['coverage_U_vs_base', 'boluses_preempted']
    assert ps['coverage']['columns'][0]['head'] == 'would have pre-empted (U)'
    assert 'changes the glucose that followed' in ps['coverage']['caveat']
    assert all(v['coverage'].get('boluses_preempted') == 0.5 for v in ps['variants'])
    for s in report.page_sentences(data):
        lint.lint_text(s, 'line')


def test_coverage_prefers_the_trial_metrics_mean():
    c = types.SimpleNamespace(effect={'coverage_U_vs_base': 9.0, 'boluses_preempted': 2})
    out = report._coverage_of({'mean': {'coverage_U_vs_base': 0.4}}, c)
    assert out == {'coverage_U_vs_base': 0.4, 'boluses_preempted': 2}
    assert report._coverage_of({'mean': {}}, None) == {}


@needs_fixture
def test_observed_days_from_a_baseline_json_next_to_the_studies(tmp_path):
    sd = _copy_fixture(tmp_path)
    (tmp_path / 'studies' / 'baseline.json').write_text(json.dumps(
        {'days': [{'day': '2026-09-06', 'tir_pct': 87.456, 'tbr_pct': 1.2, 'manual_boluses': 2, 'manual_U': 3.5,
                   'note': 'ignored'}]}))
    _text, data = _render_copy(tmp_path, sd)
    obs = data['observed']
    assert obs['days'] == {'2026-09-06': {'tir_pct': 87.46, 'tbr_pct': 1.2, 'manual_boluses': 2, 'manual_U': 3.5}}
    assert obs['range_mmol'] == [3.9, 10.0] and obs['note'] == report.OBSERVED_NOTE
    assert data['scenarios']['days'][0]['observed'] == obs['days']['2026-09-06']
    lint.lint_text(obs['note'], 'line')


@needs_fixture
def test_observed_days_absent_or_unreadable_degrade_silently(tmp_path, monkeypatch):
    sd = _copy_fixture(tmp_path)
    _text, data = _render_copy(tmp_path, sd)
    assert data['observed'] is None and 'observed' not in data['scenarios']['days'][0]
    monkeypatch.setattr(report, '_baseline_module', lambda: None)       # no reader module: the JSON alone
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    _text, data = _render_copy(tmp_path, sd, baseline_path=str(bad))
    assert data['observed']['days'] == {}
    # a day the JSON lacks is asked from hindcast.baseline.day_stats when that module exists; failures are silent
    fake = types.ModuleType('hindcast.baseline')
    fake.day_stats = lambda day: {'tir_pct': 91.0, 'manual_boluses': 0, 'tbr_pct': None}
    monkeypatch.setattr(report, '_baseline_module', lambda: fake)
    empty = tmp_path / 'empty.json'
    empty.write_text('{}')
    _text, data = _render_copy(tmp_path, sd, baseline_path=str(empty))
    assert data['observed']['days'] == {'2026-09-06': {'tir_pct': 91.0, 'manual_boluses': 0}}
    fake.day_stats = lambda day: 1 / 0
    _text, data = _render_copy(tmp_path, sd, baseline_path=str(empty))
    assert data['observed']['days'] == {}


def test_baseline_days_shapes():
    row = {'tir_pct': 90}
    assert report._baseline_days({'days': {'2026-09-06': row}}) == {'2026-09-06': row}
    assert report._baseline_days({'2026-09-06': row, '_about': 'x'}) == {'2026-09-06': row}
    assert report._baseline_days({'days': [dict(row, day='2026-09-06')]})['2026-09-06']['tir_pct'] == 90
    assert report._baseline_days(None) == {} and report._baseline_days({'days': 3}) == {}


def test_goal_views_contract():
    pjs = open(os.path.join(report.VIEWS_DIR, 'parameters.js'), encoding='utf-8').read()
    for needle in ("id: 'pv-pareto'", 'function paretoOption', 'function sensitivityPanel', 'se.available',
                   "'Review order under the declared weights'", 'covCols', 'cov.caveat', 'pa.scalar_text'):
        assert needle in pjs, needle
    assert pjs.index('var pareto = renderPareto(el, ps)') < pjs.index('panels = renderPanels(el, ps)'), 'the front comes first on the page'
    page = _read('page.js')
    assert 'HC.observedText = function' in page and "text: 'observed'" in page
    sjs = open(os.path.join(report.VIEWS_DIR, 'scenarios.js'), encoding='utf-8').read()
    assert "'observed: ' + HC.observedText" in sjs
    css = _read('page.css')
    assert '.honesty.goal' in css and '.observed-tag' in css


def test_sensitivity_view_reads_the_round_sensitivity_shape():
    """round.round_sensitivity: metrics.weights_sensitivity with vhashes, by_weight{w: {f: {order, tau, same_top}}}"""
    variants = [{'vhash': c * 40, 'variant': 'v' + c} for c in 'abc']
    sens = {'question': 'earlier', 'weights': {'rest': 2.0, 'lowside': 0.3}, 'top': 2, 'factors': [0.5, 1.5],
            'order': ['a' * 40, 'b' * 40],
            'by_weight': {'rest': {'0.5': {'order': ['b' * 40, 'a' * 40], 'tau': 0.333, 'same_top': False},
                                   '1.5': {'order': ['a' * 40, 'b' * 40], 'tau': 1.0, 'same_top': True}},
                          'lowside': {'0.5': {'order': ['a' * 40, 'b' * 40], 'tau': 1.0, 'same_top': True},
                                      '1.5': {'order': ['a' * 40, 'b' * 40], 'tau': 1.0, 'same_top': True}}}}
    out = report.sensitivity_view('earlier', {'rest': 2.0, 'lowside': 0.3}, ['vc', 'va', 'vb'], sens, variants)
    assert out['available'] and out['declared_top'] == ['va', 'vb'], 'the recorded scalar order, not the front order'
    rest, low = out['weights']
    assert rest['0.5'] == ['vb', 'va'] and rest['tau'] == {'0.5': 0.33, '1.5': 1.0} and rest['same_top'] is False
    assert low['same_top'] is True and low['value'] == 0.3
    assert out['note'].startswith('1 of 2 weights')
    lint.lint_text(out['note'], 'line')
