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
    for path, body in inlined.items():
        doc = json.loads(body)
        assert export.validate_series(doc) == []
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
