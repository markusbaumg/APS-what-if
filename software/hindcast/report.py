"""
report.py  -  `hc report` and `hc serve`: the study page generator (backlog H-401, first view H-402)

    cd software && ../.venv/bin/python -m hindcast report <study> [--out DIR] [--single-file [--days D,D] [--variants top3]
                                                                  [--pred top5]] [--artifact]
    cd software && ../.venv/bin/python -m hindcast serve  <study> [--port 8765] [--bind 127.0.0.1] [--no-render]

<study> is a name under software/studies/ or a path to a study directory (the fixture study works:
`hc report hindcast/tests/fixtures/study_example --out /tmp/hc_report`).

What it writes (default out = studies/<study>/report/):
    index.html                  one page: templates/page.html filled through string.Template with the CSS, theme.js,
                                page.js, every templates/views/*.js (later views add a file there) and the tier-1 JSON
                                (export.build_tier1 + build_page_data: overview, assumed, parameters, scenarios, drilldown, page) in
                                <script type="application/json" id="tier1">
    series/<day>/<vhash>.json   the tier-2 documents the trials name in `files`, copied next to the page when they live
    series/<day>/orig.json      elsewhere (the fixture keeps them at the study root, the replay under report/); the
    pred/<day>/...              embedded trial rows are rewritten to these page-relative paths
The page fetches tier-2 files relative to itself; from file:// (fetch blocked) it says "not available from disk"
and names `hc serve` and `--single-file`. `--single-file` inlines the selected tier-2 documents as
<script type="application/json" data-tier2="<path>"> (base and the phone always, plus --variants, default top3 of
the review order; prediction documents only with --pred), packed against the day's orig / base document (see
'page size budget', target PAGE_BUDGET_BYTES = 8 MB), and warns above SINGLE_FILE_WARN_BYTES (12 MB; the artifact
limit is 16 MB). `--artifact` writes the page without <!doctype>/<html>/<head>/<body> and the charset/viewport metas
(the Artifact tool wraps its own skeleton) and prints the `files` map for the publish call.

Checks before anything is written: every <script src> must be https on an allowed host (the Artifact CSP list) with
an exact x.y.z version in its path, every external stylesheet must come from fonts.googleapis.com, and no inserted
CSS/JS may contain '</script' or '</style'. ECharts 6.1.0 from cdnjs (ECHARTS_URL) is the only external script the
template loads. Every human-readable sentence this module generates passes hindcast.lint (kind 'line'); the page
carries the honesty paragraph of DESIGN.md section 3 verbatim (contracts.HONESTY_STATEMENT), not collapsible.

Nothing here replays, opens a network connection (serve binds a local socket only) or changes a study file.
"""
import argparse
import datetime
import functools
import glob
import html.parser
import http.server
import json
import os
import re
import shutil
import socketserver
import string
import sys
import urllib.parse
from dataclasses import dataclass, field

HINDCAST_DIR = os.path.dirname(os.path.abspath(__file__))
SOFTWARE_DIR = os.path.dirname(HINDCAST_DIR)
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('PYTHONUTF8', '1')

from hindcast import contracts, export, lint                                                # noqa: E402
from hindcast import metrics as hm                                                          # noqa: E402
from hindcast import study as st                                                            # noqa: E402

TEMPLATES_DIR = os.path.join(HINDCAST_DIR, 'templates')
VIEWS_DIR = os.path.join(TEMPLATES_DIR, 'views')
ECHARTS_VERSION = '6.1.0'
ECHARTS_URL = 'https://cdnjs.cloudflare.com/ajax/libs/echarts/%s/echarts.min.js' % ECHARTS_VERSION
SINGLE_FILE_WARN_BYTES = 12 * 1024 * 1024
ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_PORT = 8765

_SEMVER = r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?'
# host -> regex the URL path must match: the exact version is part of the path (never a bare name, range or tag)
ALLOWED_SCRIPT_HOSTS = {
    'cdnjs.cloudflare.com': re.compile(r'^/ajax/libs/[A-Za-z0-9._-]+/' + _SEMVER + r'/'),
    'cdn.jsdelivr.net':     re.compile(r'^/npm/(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+@' + _SEMVER + r'(?:/|$)'),
    'unpkg.com':            re.compile(r'^/(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+@' + _SEMVER + r'(?:/|$)'),
    'cdn.tailwindcss.com':  re.compile(r'^/' + _SEMVER + r'(?:/|$)'),
    'code.jquery.com':      re.compile(r'^/[A-Za-z0-9._-]*?-' + _SEMVER + r'(?:\.min|\.slim|\.slim\.min)?\.js$'),
}
ALLOWED_STYLE_HOSTS = ('fonts.googleapis.com',)
ORIG_KINDS = ('orig', 'pred_orig', 'pred_orig_text')       # one document per day, not per variant


class ReportError(RuntimeError):
    """The page could not be generated (a refused script/style, a broken template, a missing study)."""


@dataclass
class ReportResult:
    path: str                                    # the written index.html
    bytes: int                                   # its size
    out_dir: str
    tier2: dict = field(default_factory=dict)    # page-relative path -> bytes, every tier-2 file next to the page
    inlined: list = field(default_factory=list)  # page-relative paths inlined (--single-file)
    warnings: list = field(default_factory=list)
    files: dict = field(default_factory=dict)    # the Artifact `files` map: published path -> local source path


# ----------------------------------------------------------------------------- external resource check

class _ResourceScanner(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.styles = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'script' and a.get('src'):
            self.scripts.append(a['src'])
        elif tag == 'link' and 'stylesheet' in (a.get('rel') or '').lower().split() and a.get('href'):
            self.styles.append(a['href'])


def script_problem(src):
    """Why a <script src> is refused, or None. Relative paths (the page's own files) are allowed."""
    if src.startswith('//'):
        return f'{src}: protocol-relative URL'
    u = urllib.parse.urlsplit(src)
    if not u.scheme and not u.netloc:
        return None
    if u.scheme != 'https':
        return f'{src}: only https scripts are allowed'
    rx = ALLOWED_SCRIPT_HOSTS.get(u.hostname or '')
    if rx is None:
        return f'{src}: host {u.hostname} is not on the allowed list ({", ".join(ALLOWED_SCRIPT_HOSTS)})'
    if not rx.match(u.path):
        return f'{src}: no exact x.y.z version in the path (a bare name, range or tag is refused)'
    return None


def style_problem(href):
    u = urllib.parse.urlsplit(href)
    if not u.scheme and not u.netloc:
        return None
    if u.scheme != 'https' or (u.hostname or '') not in ALLOWED_STYLE_HOSTS:
        return f'{href}: external stylesheets only from {", ".join(ALLOWED_STYLE_HOSTS)}'
    return None


def check_external_resources(page_html):
    """Every problem with the page's <script src> and <link rel=stylesheet>; [] when the page is clean."""
    sc = _ResourceScanner()
    sc.feed(page_html)
    sc.close()
    problems = [p for p in (script_problem(s) for s in sc.scripts) if p]
    problems += [p for p in (style_problem(h) for h in sc.styles) if p]
    return problems


def external_scripts(page_html):
    sc = _ResourceScanner()
    sc.feed(page_html)
    sc.close()
    return [s for s in sc.scripts if urllib.parse.urlsplit(s).netloc]


# ----------------------------------------------------------------------------- inputs

def resolve_study(name_or_path, studies_dir=st.STUDIES_DIR):
    """A StudyDir for a name under studies/ or a directory path; ReportError when there is no study.json."""
    if os.path.isdir(name_or_path):
        sd = st.StudyDir(name_or_path)
    else:
        sd = st.StudyDir(st.resolve_study_dir(name_or_path, studies_dir))
    if not sd.exists():
        raise ReportError(f'{sd.path}: no study.json (create the study with `hc study new`)')
    return sd


def load_settings(path=contracts.NS_SETTINGS_PATH):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def assumed_keys(settings):
    """{key: text} of the `_assumed` section of ns_settings.json (values inferred rather than exported)."""
    a = (settings or {}).get('_assumed') or {}
    return {str(k): (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in a.items()}


SOURCE_LABEL_NS = 'Nightscout'
SOURCE_LABEL_LOGS = 'AAPS logfiles'


def source_label(source):
    """What the page shows as the data source: 'Nightscout' for a Nightscout study (never its URL or hostname, the
    page is meant to be shared, DESIGN.md section 3), 'AAPS logfiles' otherwise (never the local path)."""
    s = str(source or '').strip().lower()
    if not s or s.startswith('ns:') or '://' in s:
        return SOURCE_LABEL_NS
    return SOURCE_LABEL_LOGS


def _host_of(url):
    url = str(url or '').strip()
    if '://' not in url:
        return ''
    return (urllib.parse.urlsplit(url).hostname or '').lower()


def private_strings(study=None, settings=None):
    """The Nightscout URLs and hostnames no rendered page may carry: the study's source/host and the settings'
    nightscout.url, longest first (so a URL is replaced before the hostname inside it)."""
    out = set()
    url = str(((settings or {}).get('nightscout') or {}).get('url') or '').strip()
    for u in (url, getattr(study, 'source', '') if study is not None else ''):
        u = str(u or '').strip()
        if '://' in u:
            out.add(u.rstrip('/'))
            if _host_of(u):
                out.add(_host_of(u))
    host = str(getattr(study, 'host', '') or '').strip() if study is not None else ''
    if host:
        out.add(host.lower())
    return sorted((s for s in out if s), key=len, reverse=True)


def _scrub_text(text, private):
    for p in private:
        rx = re.compile(r'(?:https?://)?' + re.escape(p) + r'(?:/[^\s"\'<>)]*)?', re.IGNORECASE)
        text = rx.sub(SOURCE_LABEL_NS, text)
    return text


def scrub_private(obj, private):
    """`obj` with every URL / hostname of `private` replaced by 'Nightscout' (keys and string values, recursively)."""
    if not private:
        return obj
    if isinstance(obj, str):
        return _scrub_text(obj, private)
    if isinstance(obj, dict):
        return {scrub_private(k, private): scrub_private(v, private) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_private(v, private) for v in obj]
    return obj


def private_problems(page_html, private):
    """The private strings still present in a rendered page (case-insensitive); empty = fine."""
    low = page_html.lower()
    return [p for p in private if p.lower() in low]


def glucose_units(settings):
    u = str(((settings or {}).get('nightscout') or {}).get('units') or 'mg/dl').lower()
    return 'mmol' if u.startswith('mmol') else 'mg/dl'


def frozen_meta(days, host, frozen_dir=None):
    """{day: frozen/<host>/<day>.meta.json} for the days that have one (read directly; freeze.py is not imported
    because it pulls the Nightscout modules in). frozen_dir=None -> freeze's default base (HINDCAST_FROZEN_DIR)."""
    base = frozen_dir or os.environ.get('HINDCAST_FROZEN_DIR') or os.path.join(SOFTWARE_DIR, 'frozen')
    out = {}
    if not host:
        return out
    for d in days:
        fn = os.path.join(base, host, d + '.meta.json')
        try:
            with open(fn, encoding='utf-8') as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        out[d] = {k: meta.get(k) for k in ('day', 'loops', 'final', 'partial', 'created', 'character', 'confounders',
                                           'dropped', 'settings_sha', 'ns_code_sha') if k in meta}
    return out


def load_bundle(sd):
    """(Study, [TrialRow latest per key, file order], Status, [Round]) of a study directory."""
    study = contracts.Study.load(sd.study_json, strict=False)
    rows = list(st.StudyDir.latest_by_key(sd.trials()).values())
    rows.sort(key=lambda r: (r.trial if r.trial is not None else 0))
    try:
        status = st.build_status(sd)
    except Exception:                                        # an odd STATE.json must not stop the page
        status = None
        try:
            status = contracts.Status.load(sd.status_path, strict=False)
        except Exception:
            pass
    rnd = sd.read_round()
    return study, rows, status, ([rnd] if rnd is not None else [])


# ----------------------------------------------------------------------------- tier-2 files

def _kind_dest(kind, day, vhash):
    return export.TIER2_PATHS[kind].format(day=day, vhash=vhash or contracts.BASE_VHASH)


def _find_source(sd, rel):
    """A trial's `files` path is relative to the study directory (replay.py: report/series/...); the fixture keeps
    series/ at the study root. Try both, then report/<rel>."""
    for cand in (os.path.join(sd.path, rel), os.path.join(sd.report_dir, rel)):
        if os.path.isfile(cand):
            return cand
    return None


def collect_tier2(sd, rows):
    """{page-relative path: (source path, kind, day, vhash)} for every file the trial rows name, plus the base and
    orig documents at their standard places under the report dir when no row names them."""
    out = {}
    for r in rows:
        for kind, rel in (r.files or {}).items():
            if kind not in export.TIER2_PATHS or not rel:
                continue
            src = _find_source(sd, rel)
            if src is None:
                continue
            vh = None if kind in ORIG_KINDS else r.vhash
            out[_kind_dest(kind, r.day, vh)] = (src, kind, r.day, r.vhash)
    for r in rows:                                           # documents under report/ that no row names (yet)
        for kind in export.TIER2_PATHS:
            vh = None if kind in ORIG_KINDS else r.vhash
            dest = _kind_dest(kind, r.day, vh)
            if dest in out:
                continue
            src = os.path.join(sd.report_dir, *dest.split('/'))
            if os.path.isfile(src):
                out[dest] = (src, kind, r.day, r.vhash)
    return out


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def place_tier2(collected, out_dir):
    """Copy the collected documents next to the page (only when they are elsewhere or newer); -> {dest: bytes}."""
    sizes = {}
    for dest, (src, _kind, _day, _vh) in sorted(collected.items()):
        target = os.path.join(out_dir, *dest.split('/'))
        if not _same_file(src, target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.isfile(target) or os.path.getmtime(src) > os.path.getmtime(target) or \
                    os.path.getsize(src) != os.path.getsize(target):
                shutil.copy2(src, target)
        sizes[dest] = os.path.getsize(target)
    return sizes


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def window_hours(doc):
    """Hours a series document covers: its `window` when given, else the span of `t` plus one loop interval."""
    if not doc:
        return None
    w = doc.get('window')
    if isinstance(w, (list, tuple)) and len(w) == 2 and all(w):
        try:
            a, b = (datetime.datetime.fromisoformat(str(x).replace('Z', '+00:00')) for x in w)
            return max(0.0, (b - a).total_seconds() / 3600.0)
        except ValueError:
            pass
    t = doc.get('t') or []
    if len(t) >= 2:
        return (t[-1] - t[0] + (t[-1] - t[0]) / (len(t) - 1)) / 3600.0
    return None


# ----------------------------------------------------------------------------- selection for --single-file

def review_order(rounds, status):
    """Variant vhashes (full or 12-char) in review order: round.front, else round ranks, else status.top."""
    rnd = rounds[-1] if rounds else None
    if rnd is not None:
        if rnd.front:
            return list(rnd.front)
        ranked = sorted((c for c in rnd.variants if c.verdict != 'base'),
                        key=lambda c: (c.rank is None, c.rank if c.rank is not None else 0))
        if ranked:
            return [c.vhash for c in ranked]
    return [t.vhash for t in (status.top if status else [])]


def select_variants(spec, rows, rounds, status):
    """--variants: 'all', 'topN', or a comma list of variant labels / vhash prefixes -> set of full vhashes (base
    always included)."""
    every = {r.vhash for r in rows}
    chosen = {contracts.BASE_VHASH}
    spec = (spec or 'top3').strip()
    if spec == 'all':
        return every | chosen
    m = re.fullmatch(r'top(\d+)', spec)
    if m:
        n = int(m.group(1))
        for vh in review_order(rounds, status)[:n]:
            chosen |= {v for v in every if v.startswith(vh)}
        return chosen
    for item in (x.strip() for x in spec.split(',') if x.strip()):
        hit = {r.vhash for r in rows if r.variant == item or r.vhash.startswith(item)}
        if not hit:
            raise ReportError(f'--variants {item}: no trial row with that variant label or vhash')
        chosen |= hit
    return chosen


def select_days(spec, rows):
    every = sorted({r.day for r in rows})
    if not spec or spec == 'all':
        return every
    days = [d.strip() for d in spec.split(',') if d.strip()]
    unknown = [d for d in days if d not in every]
    if unknown:
        raise ReportError(f'--days {",".join(unknown)}: no trial row on that day (known: {", ".join(every) or "none"})')
    return days


def inline_set(collected, days, variants, pred_variants):
    """The page-relative paths --single-file inlines."""
    out = []
    for dest, (_src, kind, day, vh) in sorted(collected.items()):
        if day not in days:
            continue
        if kind == 'orig':
            out.append(dest)
        elif kind == 'series' and vh in variants:
            out.append(dest)
        elif kind in ('pred', 'pred_text') and vh in pred_variants:
            out.append(dest)
        elif kind in ('pred_orig', 'pred_orig_text') and pred_variants:
            out.append(dest)
    return out


# ----------------------------------------------------------------------------- page size budget
#
# A single-file page has to fit the 16 MB artifact limit (ARTIFACT_MAX_BYTES; warning at SINGLE_FILE_WARN_BYTES).
# The budget is PAGE_BUDGET_BYTES (8 MB) for a study of ~1000-1100 trial rows over 13 days with --variants top3,
# measured on studies/oct_tierA (967 rows, 97 variants, 13 days of 1-min loops):
#     before   8.7 MB  = tier-1 3.9 MB (trial rows 2.9: every row's full metrics, stamps and times; rounds 0.1)
#                      + tier-2 4.7 MB (62 documents of ~1260 loops x 30 arrays) + CSS/JS 0.2 MB
#     after    2.4 MB  = tier-1 1.3 MB (parameters 0.54, trial rows 0.47, scenarios 0.14, drill-down 0.1)
#                      + tier-2 0.9 MB + CSS/JS 0.2 MB; --variants all (1260 documents) is 13.1 MB
# What keeps it there:
#   - slim_trials: a trial row carries only what page.js reads (PAGE_TRIAL_KEYS: the files lookup, the trials per
#     day, the base row of a day), its metrics only on base / control rows (the day cards read the base metrics; the
#     Parameters view has every variant's metrics in parameters.variants[*].per_day). tier1.json (orchestrator) keeps
#     the full rows.
#   - slim_rounds: rounds[*] without `variants` (the page reads dead_parameters; the per-variant round data is in
#     parameters.variants).
#   - pack_inline: the inlined tier-2 copies drop the arrays no view reads (INLINE_DROP_FIELDS), and an array of a
#     base or variant document that equals the same day's orig or base document becomes {"$ref": path} (or
#     {"$ref": path, "$diff": [i, v, i, v, ...]} when it differs in few loops). page.js expands both when it reads
#     the inlined copy; the files next to a served page stay complete. The tier-2 numbers are already rounded by
#     export to what the views show (insulin 0.01 U, BG 0.1, factors 0.01), so nothing is rounded here again.
#   - prediction documents are inlined only with --pred (the drill-down fetches them lazily otherwise).
PAGE_BUDGET_BYTES = 8 * 1024 * 1024
PAGE_TRIAL_KEYS = ('trial', 'day', 'vhash', 'variant', 'status', 'control', 'phase', 'round', 'files')
INLINE_DROP_FIELDS = ('autosens', 'f_acce', 'f_bg', 'f_pp', 'f_drift', 'f_dura', 'isf', 'isf_prof', 'smb_off',
                      'act', 'dura_min', 'dura_avg', 'low_ahead', 'high_ahead')
INLINE_PACK_KINDS = ('series', 'orig')
INLINE_REF_MIN = 50                                        # arrays shorter than this stay as they are


def slim_trials(rows):
    """The trial rows as the page needs them (see the budget above)."""
    out = []
    for r in rows or []:
        s = {k: r[k] for k in PAGE_TRIAL_KEYS if k in r}
        if r.get('control') or r.get('vhash') == contracts.BASE_VHASH:
            s['metrics'] = r.get('metrics')
        out.append(s)
    return out


def slim_rounds(rounds):
    return [{k: v for k, v in (r or {}).items() if k != 'variants'} for r in rounds or []]


def _same(a, b):
    return type(a) is type(b) and a == b


def _packed_array(arr, ref, ref_path):
    """{"$ref": ref_path[, "$diff": [...]]} when that is shorter than `arr`, else None."""
    if not isinstance(ref, list) or len(ref) != len(arr):
        return None
    diff = []
    for i, (x, y) in enumerate(zip(arr, ref)):
        if not _same(x, y):
            diff += [i, x]
    if not diff:
        return {'$ref': ref_path}
    enc = {'$ref': ref_path, '$diff': diff}
    return enc if len(json.dumps(enc, separators=(',', ':'))) < len(json.dumps(arr, separators=(',', ':'))) else None


def pack_inline(docs):
    """{page path: tier-2 document} -> the same documents as --single-file inlines them (see the budget above).
    References only point at an orig document or the base series of the same day, and the base series only at the
    orig one, so expanding never runs in a circle; unpack_inline() is the inverse (minus the dropped fields)."""
    by_day = {}
    for path, doc in docs.items():
        if isinstance(doc, dict) and doc.get('kind') == 'orig':
            by_day.setdefault(doc.get('day'), {})['orig'] = path
        elif isinstance(doc, dict) and doc.get('kind') == 'series' and doc.get('vhash') == contracts.BASE_VHASH:
            by_day.setdefault(doc.get('day'), {})['base'] = path
    out = {}
    for path, doc in docs.items():
        if not isinstance(doc, dict) or doc.get('kind') not in INLINE_PACK_KINDS:
            out[path] = doc
            continue
        refs = by_day.get(doc.get('day'), {})
        cands = [] if doc['kind'] == 'orig' else [refs.get('orig')]
        if doc['kind'] == 'series' and refs.get('base') not in (None, path):
            cands.append(refs['base'])
        cands = [c for c in cands if c]
        new = {}
        for k, v in doc.items():
            if k in INLINE_DROP_FIELDS:
                continue
            if isinstance(v, list) and len(v) >= INLINE_REF_MIN:
                best = None
                for c in cands:
                    enc = _packed_array(v, docs[c].get(k), c)
                    if enc is not None and (best is None or len(enc.get('$diff', ())) < len(best.get('$diff', ()))):
                        best = enc
                v = best if best is not None else v
            new[k] = v
        out[path] = new
    return out


def unpack_inline(packed):
    """The inverse of pack_inline (what page.js does in HC.loadTier2): {path: packed doc} -> {path: doc}."""
    done = {}

    def expand(path, depth=0):
        if path in done:
            return done[path]
        if depth > 4 or path not in packed:
            raise ReportError(f'{path}: an inlined reference that cannot be expanded')
        doc = dict(packed[path])
        for k, v in list(doc.items()):
            if isinstance(v, dict) and isinstance(v.get('$ref'), str):
                arr = list(expand(v['$ref'], depth + 1)[k])
                d = v.get('$diff') or []
                for i in range(0, len(d), 2):
                    arr[d[i]] = d[i + 1]
                doc[k] = arr
        done[path] = doc
        return doc

    return {p: expand(p) for p in packed}


# ----------------------------------------------------------------------------- page data (tier 1 + overview)

def _r(v, dp=3):
    return None if v is None else round(float(v), dp)


def _fmt(v, dp=2):
    return '-' if v is None else f'{v:.{dp}f}'


def _signed(v, dp=2):
    if v is None:
        return '-'
    return ('+' if v > 0 else '-' if v < 0 else '±') + f'{abs(v):.{dp}f}'


def _plural(n, word, plural=None):
    return f'{n} {word if n == 1 else (plural or word + "s")}'


def overview_numbers(study, rows, rounds, hours_by_day, days_meta=None):
    """The numbers of the Study overview (H-402), computed here so the page only formats them:
    {'summary': {...}, 'days': {day: {...}}, 'banner': str, 'ties_text': str|None}. Every sentence passes the lint.
    `days` carries every day with a base row (the day cards); the summary and the banner (worst day, SMB bias,
    floors, tie thresholds) cover the scored days only: search + hold-out, never a quarantined day (DESIGN.md 5.4
    phase 0: listed, not scored), the same day set as status.json's `floor` (study.build_status). The quarantined
    days stay listed in summary['quarantined'] with their reasons. The review order's floors (round.json `floors`,
    the Parameters view) cover the scored search days only; the banner says so."""
    days_meta = days_meta or {}
    roles = {d.day: d.role for d in study.days}
    base = {}
    for r in rows:
        if r.status == 'done' and r.metrics and (r.control == 'base' or r.vhash == contracts.BASE_VHASH):
            base[r.day] = r
    out_days = {}
    for d in [x.day for x in study.days]:
        b = base.get(d)
        if b is None:
            out_days[d] = {'has_base': False}
            continue
        m = b.metrics
        f = hm.floors_from_base(m)
        hours = hours_by_day.get(d)
        out_days[d] = {
            'has_base': True, 'loops': m.get('loops'), 'hours': _r(hours, 2),
            'agreement_pct': _r(m.get('agreement_pct')), 'smb_exact_pct': _r(m.get('smb_exact_pct')),
            'smb_within_pct': _r(m.get('smb_within_pct')), 'insreq_within_pct': _r(m.get('insreq_within_pct')),
            'isf_within_pct': _r(m.get('isf_within_pct')),
            'smb_bias_U': _r((m.get('smb_emul_U') or 0) - (m.get('smb_orig_U') or 0), 3),
            'floor_rise_U': _r(f['floor_rise_U']), 'floor_rest_U': _r(f['floor_rest_U']),
            'floor_total_U': _r(f['floor_total_U']), 'floor_smb_U': _r(f['floor_smb_U']),
            'floor_loops': int(f['floor_loops'] or 0),
            'tie_rise_U': _r(hm.TIE_FACTOR * hm.effective_floor(f['floor_rise_U'])),
            'tie_rest_U': _r(hm.TIE_FACTOR * hm.effective_floor(f['floor_rest_U'])),
            'freeze_sha': b.freeze_sha or '',
        }
    all_base = [d for d, o in out_days.items() if o.get('has_base')]
    with_base = [d for d in all_base if roles.get(d) in ('search', 'holdout')]     # the scored days
    n_search = sum(1 for d in with_base if roles.get(d) == 'search')
    done = [r for r in rows if r.status == 'done']
    quarantined = [{'day': d.day, 'reason': d.quarantine_reason or 'no reason recorded'}
                   for d in study.days if d.role == 'quarantined']
    summary = {'days': len(study.days), 'days_with_base': len(all_base), 'scored_days_with_base': len(with_base),
               'search_days_with_base': n_search, 'quarantined': quarantined,
               'loops_with_base': sum(out_days[d]['loops'] or 0 for d in with_base),
               'trials_frozen': sum(1 for r in done if r.freeze_sha), 'trials_live': sum(1 for r in done if not r.freeze_sha),
               'frozen_days': len(days_meta), 'frozen_final_days': sum(1 for m in days_meta.values() if m.get('final'))}
    banner, ties_text = None, None
    if with_base:
        worst = min(with_base, key=lambda d: out_days[d]['agreement_pct'] if out_days[d]['agreement_pct'] is not None else 101)
        hours = [out_days[d]['hours'] for d in with_base if out_days[d]['hours']]
        bias_day = [out_days[d]['smb_bias_U'] * 24.0 / out_days[d]['hours'] for d in with_base if out_days[d]['hours']]
        summary.update({
            'agreement_min_pct': out_days[worst]['agreement_pct'], 'agreement_min_day': worst,
            'smb_exact_min_pct': min((out_days[d]['smb_exact_pct'] for d in with_base
                                      if out_days[d]['smb_exact_pct'] is not None), default=None),
            'smb_bias_U': _r(sum(out_days[d]['smb_bias_U'] for d in with_base)),
            'smb_bias_U_per_day': _r(sum(bias_day) / len(bias_day)) if bias_day else None,
            'hours_min': min(hours) if hours else None, 'hours_total': _r(sum(hours), 2) if hours else None,
            'floor_rise_max_U': max(out_days[d]['floor_rise_U'] for d in with_base),
            'floor_rest_max_U': max(out_days[d]['floor_rest_U'] for d in with_base),
            'floor_loops_max': max(out_days[d]['floor_loops'] for d in with_base),
            'tie_rise_U': max(out_days[d]['tie_rise_U'] for d in with_base),
            'tie_rest_U': max(out_days[d]['tie_rest_U'] for d in with_base),
        })
        o = out_days[worst]
        span = (f'over {_fmt(o["hours"], 1)} h' if o['hours'] and o['hours'] < 23.5 else 'over the day')
        changed = o['loops'] - round(o['loops'] * (o['agreement_pct'] or 0) / 100.0) if o['loops'] else 0
        where = ('on ' + worst) if len(with_base) == 1 else f'on its worst day ({worst})'
        scope = (f'These numbers cover the {_plural(len(with_base), "scored day")} (search and hold-out, the day set '
                 f'of the status line); the review order\'s floors and x floor multiples cover the '
                 f'{_plural(n_search, "scored search day")} only.')
        if quarantined:
            scope += (f' {_plural(len(quarantined), "quarantined day")} ('
                      + ', '.join(q['day'] for q in quarantined) + ') listed, not scored.')
        banner = (f'The base replay (the phone\'s own settings, replayed) matches the phone on all four decisions in '
                  f'{_fmt(o["agreement_pct"], 1)} % of {o["loops"]} loops {where} and disagrees in {changed}; it '
                  f'reproduces the SMB exactly in {_fmt(o["smb_exact_pct"], 1)} %, and its SMB total is '
                  f'{_signed(o["smb_bias_U"], 2)} U {span} against the phone. A difference between two variants '
                  f'below twice the floor is a tie: {_fmt(summary["tie_rise_U"], 2)} U in rising loops, '
                  f'{_fmt(summary["tie_rest_U"], 2)} U in the rest.')
        if (summary['agreement_min_pct'] or 100) < 96.0:
            banner += (f' This floor is wider than the "about 4 %" the general statement above quotes; '
                       f'read every difference on this page against the numbers here.')
        banner += ' ' + scope
    elif all_base:
        banner = ('No scored day (search or hold-out) has a base replay yet: the floor is unknown; quarantined days '
                  'are listed, not scored.')
    rnd = rounds[-1] if rounds else None
    if rnd is not None:
        others = [c for c in rnd.variants if c.verdict != 'base']
        ties = [c for c in others if c.verdict == 'tie']
        dead = list(rnd.dead_parameters)
        parts = []
        if others and len(ties) == len(others):
            parts.append(f'All {_plural(len(others), "variant")} of round {rnd.round} are indistinguishable from base '
                         f'within the noise floor on every day.')
        elif ties:
            parts.append(f'{len(ties)} of {_plural(len(others), "variant")} in round {rnd.round} are indistinguishable '
                         f'from base within the noise floor.')
        if dead:
            parts.append(f'{_plural(len(dead), "parameter")} not testable on these days: '
                         + ', '.join(d.key for d in dead) + '.')
        ties_text = ' '.join(parts) or None
    return {'summary': summary, 'days': out_days, 'banner': banner, 'ties_text': ties_text}


# ----------------------------------------------------------------------------- parameter space (H-403)

# (metric key, label, unit, floor key, sign means insulin direction): the delta columns that carry a floor band
PARAMETER_COMPONENTS = (
    ('rise_vs_base_U', 'insulin in rising loops vs base', 'U', 'floor_rise_U', True),
    ('rest_vs_base_U', 'insulin in the rest vs base', 'U', 'floor_rest_U', True),
    ('delta_vs_base_U', 'total insulin vs base', 'U', 'floor_total_U', True),
    ('smb_vs_base_U', 'SMB insulin vs base', 'U', 'floor_smb_U', True),
    ('tbr_vs_base_U', 'temp basal insulin vs base', 'U', 'floor_tbr_U', True),
    ('changed_vs_base', 'loops changed vs base', 'loops', 'floor_loops', False),
)
# further leaderboard columns (no floor band): the feasibility readings of DESIGN.md 5.2 / 5.3
PARAMETER_EXTRAS = (
    ('smb_more_vs_base', 'loops with a larger SMB than base', 'loops'),
    ('smb_less_vs_base', 'loops with a smaller SMB than base', 'loops'),
    ('lowside_flags', 'low-side flags', 'loops'),
    ('prehypo_extra_U', 'extra insulin in the 2 h before a recorded low', 'U'),
    ('env_3h_max_U', 'largest 3 h sum vs base', 'U'),
    ('env_day_pct_tdd', 'day sum vs base, % of the phone\'s TDD', '%'),
    ('zero_temp_vs_base', 'zero temps vs base', 'loops'),
    ('cap_binding_pct', 'factor on a cap', '%'),
    ('max_single_smb_U', 'largest single SMB', 'U'),
)


def _param_value(params, key, phone):
    return params[key] if key in (params or {}) else phone


def parameter_space(study, rows, rounds):
    """The data of the Parameters view (H-403), computed here so the page only draws it:
    {question, days[], floors{day: {floor_*, tie{component: U or loops}}}, components[], extras[], params[],
     base{vhash, variant, values{param: phone}}, variants[{vhash, variant, values, per_day{day: {metric: v}},
     mean{metric: v}, verdict, why, rank, position, feasible, reasons{day: [str]}, floor_multiple (the review order
     key: metrics.review_order_key), floor_multiples{component: x}, review_score (the record only), review_component,
     tie_with_base, flags, validation}], order[vhash], rule_lines[], warnings[], validation}.
    `validation` per variant: _validation_of() (None unless the validate phase replayed it); the top-level
    `validation` is round.json's study-level block (metrics.Validation.summary(): {top, validated, search_days,
    holdout_days[], folds, tau, recommended_for_review, sign_share, fold_top, fold_share, holdout_top}) or None.
    `floors` are per scored search day, as round.json's; status.json's floor is the worst of search + hold-out.
    Days = the study's search days (hold-out and quarantined days stay out of the review order); the ranking is
    metrics.rank_variants on the latest done row per variant and day, the verdicts come from the latest round when it
    names the variant."""
    space = study.space or {}
    roles = {d.day: d.role for d in study.days}
    days = [d for d, r in roles.items() if r == 'search'] or [d for d, r in roles.items() if r != 'quarantined']
    days = sorted(days)
    comps = [k for k, *_ in PARAMETER_COMPONENTS]
    extras = [k for k, *_ in PARAMETER_EXTRAS]
    params = []
    for key, sp in space.items():
        params.append({'key': key, 'type': sp.type, 'phone': sp.phone, 'low': sp.low, 'high': sp.high, 'step': sp.step,
                       'choices': list(sp.choices) if sp.choices is not None else None})
    done = [r for r in rows if r.status == 'done' and r.metrics and r.day in days and r.control != 'sentinel']
    is_base = lambda r: r.control == 'base' or r.vhash == contracts.BASE_VHASH     # noqa: E731
    base_rows = {r.day: r for r in done if is_base(r)}
    base_label = next((r.variant for r in base_rows.values()), hm.BASE_VARIANT)
    floors = {}
    for d, b in sorted(base_rows.items()):
        f = hm.floors_from_base(b.metrics)
        f = {k: _r(v, 4) if isinstance(v, (int, float)) else v for k, v in f.items()}
        f['tie'] = {k: _r(hm.TIE_FACTOR * hm.effective_floor(f.get(fk), fk), 4) for k, _l, _u, fk, _s in PARAMETER_COMPONENTS}
        floors[d] = f
    by_vh = {}
    for r in done:
        if is_base(r):
            continue
        v = by_vh.setdefault(r.vhash, {'vhash': r.vhash, 'variant': r.variant, 'params': dict(r.params or {}), 'per_day': {}})
        v['per_day'][r.day] = {k: _r(r.metrics.get(k), 4) for k in comps + extras if r.metrics.get(k) is not None}
    warnings = []
    ranking = None
    try:
        objective = study.objective.to_dict() if hasattr(study.objective, 'to_dict') else dict(study.objective or {})
        rank_rows = [{'variant': base_label if is_base(r) else r.variant, 'day': r.day, 'metrics': r.metrics,
                      'params': r.params, 'status': r.status} for r in done]
        ranking = hm.rank_variants(rank_rows, study.question, objective, days, base_label)
        warnings += list(ranking.warnings)
    except Exception as e:                                   # the page still shows the numbers without the ranking
        warnings.append(f'the review order could not be computed here ({type(e).__name__}); rows follow the round file')
    rnd = rounds[-1] if rounds else None
    rv = {c.vhash: c for c in (rnd.variants if rnd is not None else [])}
    variants = []
    for vh, v in by_vh.items():
        s = ranking.variants.get(v['variant']) if ranking is not None else None
        c = rv.get(vh)
        mean = {}
        for k in comps + extras:
            vals = [pd[k] for pd in v['per_day'].values() if pd.get(k) is not None]
            if vals:
                mean[k] = _r(sum(vals) / len(vals), 4)
        reasons = dict(s.infeasible_reasons) if s is not None else {}
        feasible = s.feasible if s is not None else (c.feasible if c is not None else None)
        if s is not None and not s.feasible:                 # feasibility is recomputed here; a stale round never hides it
            verdict, rank = 'infeasible', None
            why = '; '.join(f'{d}: {", ".join(x)}' for d, x in sorted(reasons.items()))
        elif c is not None:
            verdict, why, rank = c.verdict, c.why, c.rank
        elif s is not None:
            verdict = 'infeasible' if not s.feasible else ('tie' if s.tie_with_base else 'candidate')
            why = ('; '.join(f'{d}: {", ".join(x)}' for d, x in sorted(reasons.items())) if not s.feasible else
                   'indistinguishable from base within the noise floor on every day' if s.tie_with_base else
                   f'{s.review_component} {s.mean.get(s.review_component, 0.0):+.2f} U vs base, '
                   f'{_floor_multiple_of(s):.1f} x floor')
            rank = s.rank
        else:
            verdict, why, rank = 'pending', 'no base replay on these days yet', None
        variants.append({
            'vhash': vh, 'variant': v['variant'],
            'values': {p['key']: _param_value(v['params'], p['key'], p['phone']) for p in params},
            'per_day': v['per_day'], 'mean': mean, 'days': sorted(v['per_day']),
            'verdict': verdict, 'why': why, 'rank': rank, 'feasible': feasible, 'reasons': reasons,
            'floor_multiple': _r(_floor_multiple_of(s), 3) if s is not None else None,      # the review order key
            'floor_multiples': ({k: _r(x, 3) for k, x in (s.floor_multiples or {}).items()} if s is not None else {}),
            'review_score': _r(s.review_score, 3) if s is not None else None,                # the record only
            'review_component': s.review_component if s is not None else None,
            'tie_with_base': s.tie_with_base if s is not None else (verdict == 'tie'),
            'flags': list(s.flags) if s is not None else list(c.flags if c is not None else []),
            'validation': _validation_of(c),
        })
    # order: the ranking's order, then the infeasible ones, then whatever has no ranking (round rank, then label)
    pos = {}
    if ranking is not None:
        for i, name in enumerate(list(ranking.order) + list(ranking.infeasible)):
            pos[name] = i
    variants.sort(key=lambda x: (pos.get(x['variant'], 10 ** 6), x['rank'] if x['rank'] is not None else 10 ** 6,
                                 x['variant']))
    for i, v in enumerate(variants, 1):
        v['position'] = i
    rule = (ranking.rule_text if ranking is not None else '\n'.join(rnd.notes if rnd is not None else [])) or ''
    if ranking is not None and rnd is not None and getattr(rnd, 'validation', None):
        rule = hm.ranking_rule_text(study.question, ranking.objective, ranking.floors, validate=True)
    base_any = next(iter(base_rows.values()), None)
    return {
        'question': study.question, 'days': days, 'floors': floors, 'tie_factor': hm.TIE_FACTOR,
        'components': [{'key': k, 'label': lab, 'unit': u, 'floor_key': fk, 'insulin': s}
                       for k, lab, u, fk, s in PARAMETER_COMPONENTS],
        'extras': [{'key': k, 'label': lab, 'unit': u} for k, lab, u in PARAMETER_EXTRAS],
        'params': params,
        'base': {'vhash': base_any.vhash if base_any else contracts.BASE_VHASH, 'variant': base_label,
                 'values': {p['key']: p['phone'] for p in params}, 'days': sorted(base_rows)},
        'variants': variants, 'order': [v['vhash'] for v in variants],
        'rule_lines': [x for x in rule.split('\n') if x.strip()], 'warnings': warnings,
        'validation': dict(rnd.validation) if rnd is not None and getattr(rnd, 'validation', None) else None,
    }


def _floor_multiple_of(s):
    """The 'x floor' multiple of a VariantSummary's review component: metrics.floor_multiple() (the mean over days
    of |effect| / that day's floor, the review order key of metrics.review_order_key), 0 when it is missing (never
    the review score, H-715) - as metrics.leaderboard_rows and the cards print it."""
    fm = (getattr(s, 'floor_multiples', None) or {}).get(s.review_component)
    return 0.0 if fm is None else fm


def _validation_of(candidate):
    """The leaderboard's validation block of one variant (H-308), from round.json's Candidate.validation:
    the metrics.VALIDATION_KEYS {search_rank, holdout_rank, holdout_total, component, direction, holds,
    holdout_days, flip_days, missing_days, lodo_min, lodo_max, lodo_folds, lodo_unranked, fold_top, fold_share,
    holdout_top, inside_floor_days, tie_with_base, feasible_all_days, infeasible_days, recommended_for_review,
    reasons[]} plus `line` (metrics.validation_line) and `caveat` (metrics.validation_caveat, recommended for
    review only, else None); None for a variant the validate phase did not replay (or before it ran)."""
    v = getattr(candidate, 'validation', None) if candidate is not None else None
    if not v:
        return None
    out = dict(v)
    out['line'] = hm.validation_line(v)
    out['caveat'] = hm.validation_caveat(v) if v.get('recommended_for_review') else None
    return out


# ----------------------------------------------------------------------------- scenario comparison (H-404)

SCENARIO_SLOTS = 3                                         # categorical slots 1-3; the picker refuses a fourth
IOB_TOLERANCE_FACTOR = 1.3                                 # DESIGN.md 6: the emulated iobTH and its 130 % tolerance
CAP_KEYS = ('autoISF_max', 'autoISF_min')


def _phone_value(study, settings, key):
    """The phone's value of profile <key>: the study space's `phone` when the key is searched, else the autoisf
    section of ns_settings.json, else None."""
    sp = (study.space or {}).get('profile ' + key)
    if sp is not None and sp.phone is not None:
        return sp.phone
    for section in ('autoisf', 'profile'):
        v = ((settings or {}).get(section) or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def _short_label(params, phone_values):
    """'autoISF_max 1.5, dura_ISF_weight 0.5' from the parameters that differ from the phone ('' for none)."""
    parts = []
    for k, v in sorted((params or {}).items()):
        if phone_values.get(k) is not None and phone_values.get(k) == v:
            continue
        name = re.sub(r'^(profile|new_parameter|state)\s+', '', k)
        parts.append(f'{name} {v}')
    return ', '.join(parts)


def scenario_index(study, rows, collected, days_meta, settings, ps=None):
    """The data of the Scenarios view (H-404), the per-loop numbers stay in tier 2:
    {days[{day, role, tags, confounders, orig (page path or None)}], entities[{vhash, variant, label, base, params,
     position, verdict, caps{autoISF_max, autoISF_min}, series{day: page path}}], phone{caps}, defaults[{vhash, day}],
     slots, smb_tol_U, iob_tolerance, units{t, insulin, tbr}}.
    An entity of the page is a (variant, day) pair; `series` names the days a variant has a series document for."""
    ps = ps or {}
    space = study.space or {}
    phone_values = {k: sp.phone for k, sp in space.items()}
    phone_caps = {k: _phone_value(study, settings, k) for k in CAP_KEYS}
    paths = {}
    for dest, (_src, kind, day, vh) in collected.items():
        if kind == 'series':
            paths.setdefault(vh, {})[day] = dest
        elif kind == 'orig':
            paths.setdefault(None, {})[day] = dest
    pos = {v['vhash']: v for v in ps.get('variants') or []}
    by_vh = {}
    for r in rows:
        if r.status != 'done' or r.control == 'sentinel':
            continue
        is_base = r.control == 'base' or r.vhash == contracts.BASE_VHASH
        e = by_vh.setdefault(r.vhash, {'vhash': r.vhash, 'variant': r.variant, 'base': is_base,
                                       'params': dict(r.params or {})})
        e['base'] = e['base'] or is_base
    entities = []
    for vh, e in by_vh.items():
        p = pos.get(vh) or {}
        caps = {}
        for k in CAP_KEYS:
            v = (e['params'] or {}).get('profile ' + k)
            caps[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else phone_caps[k]
        label = 'base (the phone\'s settings, replayed)' if e['base'] else (_short_label(e['params'], phone_values)
                                                                            or e['variant'] + ' (the phone\'s values)')
        entities.append({'vhash': vh, 'variant': e['variant'], 'label': label, 'base': e['base'], 'params': e['params'],
                         'position': p.get('position'), 'verdict': 'base' if e['base'] else p.get('verdict'),
                         'caps': caps, 'series': dict(sorted(paths.get(vh, {}).items()))})
    entities.sort(key=lambda x: (not x['base'], x['position'] if x['position'] is not None else 10 ** 6, x['variant']))
    days = []
    for d in study.days:
        meta = (days_meta or {}).get(d.day) or {}
        days.append({'day': d.day, 'role': d.role, 'tags': list(d.tags or []),
                     'confounders': list(meta.get('confounders') or []), 'orig': (paths.get(None) or {}).get(d.day)})
    with_orig = [d['day'] for d in days if d['orig']]
    search = [d['day'] for d in days if d['orig'] and d['role'] == 'search']
    first_day = (search or with_orig or [None])[0]
    defaults = []
    if first_day:
        order = [vh for vh in (ps.get('order') or []) if first_day in (paths.get(vh) or {})]
        order += [e['vhash'] for e in entities if not e['base'] and e['vhash'] not in order and first_day in e['series']]
        base = [e['vhash'] for e in entities if e['base'] and first_day in e['series']]
        for vh in order[:1] + base[:1]:
            defaults.append({'vhash': vh, 'day': first_day})
        if not defaults and order:
            defaults.append({'vhash': order[0], 'day': first_day})
    return {'days': days, 'entities': entities, 'phone': {'caps': phone_caps}, 'defaults': defaults,
            'slots': SCENARIO_SLOTS, 'smb_tol_U': hm.TOL_U, 'iob_tolerance': IOB_TOLERANCE_FACTOR,
            'units': {'t': export.UNITS['t'], 'insulin': export.UNITS['insulin'], 'tbr': export.UNITS['tbr']}}


GUARD_MMOL = (3.8, 5.4)                                    # DESIGN.md 6: the guard hairlines of the prediction fans
FACTOR_TOL = 0.005                                         # factors are shown with 2 dp: a visible difference
TBR_TOL = export.CHANGE_TOL                                # U/h, the same 0.05 as `changed` in the series


def drilldown_index(study, collected):
    """The data of the Loop drill-down (H-405); the per-loop numbers stay in tier 2:
    {pred{day: {vhash: page path}}, pred_text{day: {vhash: path}}, pred_orig{day: path}, pred_orig_text{day: path},
     orig{day: path}, series{day: {vhash: path}}, base_vhash, guards_mgdl[2], guards_mmol[2], low_mgdl, low_follow_s,
     tol{insulin, tbr, factor}, match_s}.
    Prediction documents exist for finalists only, so the page asks this index before it fetches one: a missing
    entry is 'not written for this variant', not a load failure."""
    out = {'pred': {}, 'pred_text': {}, 'pred_orig': {}, 'pred_orig_text': {}, 'orig': {}, 'series': {}}
    for dest, (_src, kind, day, vh) in sorted(collected.items()):
        if kind in ('pred', 'pred_text', 'series'):
            out[kind].setdefault(day, {})[vh] = dest
        elif kind in ('pred_orig', 'pred_orig_text', 'orig'):
            out[kind][day] = dest
    out.update({'base_vhash': contracts.BASE_VHASH,
                'guards_mmol': list(GUARD_MMOL),
                'guards_mgdl': [round(g * hm.MMOL_TO_MGDL, 1) for g in GUARD_MMOL],
                'low_mgdl': round(export.LOW_MGDL, 1), 'low_follow_s': export.LOW_AHEAD_S,
                'tol': {'insulin': hm.TOL_U, 'tbr': TBR_TOL, 'factor': FACTOR_TOL},
                'match_s': 90,                               # two documents' loops are the same loop within 90 s
                'days': [d.day for d in study.days]})
    return out


NO_GLUCOSE_RESPONSE = contracts.NO_GLUCOSE_RESPONSE      # shared with the cards ('this page' read as 'this card')


def unlock_caveat(study):
    """The weak-evidence banner of a study that varies keys locked by default (Study.unlocked: tier C profile
    scales, tier S caps): contracts.WEAK_EVIDENCE via Study.unlock_caveat() plus NO_GLUCOSE_RESPONSE; None
    otherwise. The page shows it next to the honesty paragraph."""
    fn = getattr(study, 'unlock_caveat', None)
    text = fn() if callable(fn) else None
    if not text:
        return None
    text = text[0].upper() + text[1:]
    return text + ' ' + NO_GLUCOSE_RESPONSE


def validation_lines(v):
    """The Parameters view's 'Hold-out validation' panel from round.json's study-level validation block
    (metrics.Validation.summary(): {top, validated, search_days, holdout_days[], folds, tau, recommended_for_review,
    sign_share, fold_top, fold_share, holdout_top}); [] without a validate round. The rule sentence is
    metrics.recommend_rule_text() over the block's numbers (defaults for a key an older round lacks), the same
    builder as the rule lines, so the panel cannot state another rule. Kendall tau is quoted as a caveat with its
    day count, never as a statistic."""
    if not v:
        return []
    hold = list(v.get('holdout_days') or [])
    n_search = int(v.get('search_days') or 0)
    n_days = n_search + len(hold)
    validated = int(v.get('validated') or 0)
    rec = int(v.get('recommended_for_review') or 0)
    tau = v.get('tau')
    tau_text = (f'Kendall tau {tau:+.2f} over {_plural(n_days, "day")}' if isinstance(tau, (int, float))
                else 'Kendall tau not computed')
    out = [f'Hold-out validation: {validated} validated, {rec} recommended for review, {tau_text}.']
    out.append(f'The top {v.get("top", validated)} of the search review order were replayed on '
               f'{_plural(len(hold), "hold-out day")}' + (f' ({", ".join(hold)})' if hold else '') +
               f'; {_plural(int(v.get("folds") or 0), "leave-one-day-out fold")} over the {n_search} search and '
               f'{len(hold)} hold-out days.')
    if isinstance(tau, (int, float)):
        out.append(f'Kendall tau compares the search order with the hold-out order of the validated variants; with '
                   f'{_plural(n_days, "day")} it is a caveat on how stable the order is, not a statistic.')
    out.append('Recommended for review means: not a tie with base, ' + hm.recommend_rule_text(v, validate=True)
               + '; a reading for review, not a finding.')
    return out


def build_page_data(sd, study, rows, status, rounds, days_meta, settings, collected, generated, single_file=False,
                    inlined=(), artifact=False):
    """The tier-1 document: export.build_tier1 plus `overview`, `assumed`, `parameters`, `scenarios`, `drilldown` and `page`; the trial rows' `files` are
    rewritten to the page-relative paths of `collected`."""
    hours = {}
    for dest, (src, kind, day, vh) in collected.items():
        if kind == 'series' and vh == contracts.BASE_VHASH and day not in hours:
            hours[day] = window_hours(_read_json(src))
    data = export.build_tier1(study, rows, status=status, rounds=rounds, days=days_meta, generated=generated)
    by_src = {}
    for dest, (_src, kind, day, vh) in collected.items():
        by_src[(kind, day, None if kind in ORIG_KINDS else vh)] = dest
    for r in data['trials']:
        files = {}
        for kind in export.TIER2_PATHS:
            key = (kind, r['day'], None if kind in ORIG_KINDS else r['vhash'])
            if key in by_src:
                files[kind] = by_src[key]
        r['files'] = files
    data['trials'] = slim_trials(data['trials'])
    data['rounds'] = slim_rounds(data.get('rounds'))
    data['overview'] = overview_numbers(study, rows, rounds, hours, days_meta)
    data['assumed'] = assumed_keys(settings)
    data['parameters'] = parameter_space(study, rows, rounds)
    data['parameters']['validation_lines'] = validation_lines(data['parameters'].get('validation'))
    data['scenarios'] = scenario_index(study, rows, collected, days_meta, settings, data['parameters'])
    data['drilldown'] = drilldown_index(study, collected)
    data['page'] = {'generated': data['generated'], 'single_file': bool(single_file), 'inlined': list(inlined),
                    'artifact': bool(artifact), 'source': source_label(study.source), 'host': '',
                    'units': glucose_units(settings), 'study_dir': os.path.basename(sd.path),
                    'caveat': unlock_caveat(study)}
    if isinstance(data.get('study'), dict):                  # the page is shareable: no Nightscout URL or hostname
        data['study']['source'] = source_label(study.source)
        data['study']['host'] = ''
    return scrub_private(data, private_strings(study, settings))


def page_sentences(data):
    """The human-readable sentences this module generated (what the lint runs on)."""
    ov = data.get('overview') or {}
    out = [s for s in (ov.get('banner'), ov.get('ties_text')) if s]
    out += [str(v) for v in (data.get('assumed') or {}).values()]
    if (data.get('page') or {}).get('caveat'):
        out.append(str(data['page']['caveat']))
    ps = data.get('parameters') or {}
    out += list(ps.get('rule_lines') or []) + list(ps.get('warnings') or [])
    out += [str(x) for x in ps.get('validation_lines') or []]
    for v in ps.get('variants') or []:
        if v.get('why'):
            out.append(str(v['why']))
        if (v.get('validation') or {}).get('line'):
            out.append(str(v['validation']['line']))
        for reasons in (v.get('reasons') or {}).values():
            out += [str(x) for x in reasons]
    sc = data.get('scenarios') or {}
    out += [str(e['label']) for e in sc.get('entities') or [] if e.get('label')]
    for d in sc.get('days') or []:
        out += [str(c) for c in d.get('confounders') or []]
    return out


# ----------------------------------------------------------------------------- rendering

def _template(name):
    with open(os.path.join(TEMPLATES_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def _guard_inline(text, what, closing):
    if re.search(r'</' + closing, text, re.IGNORECASE):
        raise ReportError(f'{what} contains "</{closing}", which would end its element early')
    return text


def views_js():
    """Every templates/views/*.js in name order, each in its own <script> (a later view adds a file there)."""
    parts = []
    for fn in sorted(glob.glob(os.path.join(VIEWS_DIR, '*.js'))):
        with open(fn, encoding='utf-8') as fh:
            parts.append('<script>\n/* %s */\n%s\n</script>' % (os.path.basename(fn),
                                                               _guard_inline(fh.read(), fn, 'script')))
    return '\n'.join(parts)


def _human_size(n):
    if n >= 1024 * 1024:
        return f'{n / 1024 / 1024:.1f} MB'
    return f'{n / 1024:.0f} kB'


def strip_skeleton(page_html):
    """The Artifact form: no doctype/html/head/body and no charset/viewport metas (the publish skeleton adds them)."""
    out = re.sub(r'(?is)<!doctype html>\s*', '', page_html, count=1)
    out = re.sub(r'(?i)</?html[^>]*>\s*', '', out)
    out = re.sub(r'(?i)</?head>\s*', '', out)
    out = re.sub(r'(?i)</?body[^>]*>\s*', '', out)
    out = re.sub(r'(?i)<meta charset="[^"]*">\s*', '', out)
    out = re.sub(r'(?i)<meta name="viewport"[^>]*>\s*', '', out)
    return out.lstrip()


def render_html(data, inline_docs=None, size_note='', artifact=False, private=()):
    """Fill templates/page.html; -> the page text. inline_docs: {page-relative path: parsed JSON}. `private`
    (private_strings) is scrubbed from the inlined documents and refused in the result (ReportError)."""
    if private:
        inline_docs = {p: scrub_private(d, private) for p, d in (inline_docs or {}).items()}
    inline_docs = pack_inline(inline_docs or {})
    study = data.get('study') or {}
    page = data.get('page') or {}
    mode = ('single file, ' + _plural(len(inline_docs or {}), 'per-loop document') + ' inlined') if page.get('single_file') \
        else 'per-loop data loaded on demand'
    tier2 = '\n'.join('<script type="application/json" data-tier2="%s">%s</script>' % (path, export.embed_json(doc))
                      for path, doc in sorted((inline_docs or {}).items()))
    values = {
        'TITLE': study.get('name') or 'Hindcast study',
        'STUDY_NAME': study.get('name') or 'Hindcast study',
        'QUESTION': study.get('question') or 'explore',
        'GENERATED': (data.get('generated') or '').replace('T', ' ').replace('Z', ' UTC'),
        'MODE': mode,
        'HONESTY': data.get('honesty') or contracts.HONESTY_STATEMENT,
        'CONTRACT_VERSION': str(data.get('contract_version', contracts.CONTRACT_VERSION)),
        'ECHARTS_VERSION': ECHARTS_VERSION,
        'ECHARTS_URL': ECHARTS_URL,
        'SIZE_NOTE': size_note,
        'CAVEAT': caveat_html(page.get('caveat')),
        'CSS': _guard_inline(_template('page.css'), 'page.css', 'style'),
        'THEME_JS': _guard_inline(_template('theme.js'), 'theme.js', 'script'),
        'PAGE_JS': _guard_inline(_template('page.js'), 'page.js', 'script'),
        'VIEWS_JS': views_js(),
        'DATA_SCRIPTS': export.script_tag('tier1', data) + ('\n' + tier2 if tier2 else ''),
    }
    for k in ('TITLE', 'STUDY_NAME', 'QUESTION', 'GENERATED', 'MODE', 'HONESTY', 'SIZE_NOTE'):
        values[k] = _escape(values[k])
    try:
        text = string.Template(_template('page.html')).substitute(values)
    except (KeyError, ValueError) as e:
        raise ReportError(f'templates/page.html: {e}') from None
    if artifact:
        text = strip_skeleton(text)
    problems = check_external_resources(text)
    if problems:
        raise ReportError('refused: ' + '; '.join(problems))
    if private_problems(text, private):
        raise ReportError('refused: the page would carry the Nightscout address; it is shown as "Nightscout" only')
    return text


def caveat_html(text):
    """The weak-evidence paragraph of the honesty band (unlocked studies), or ''."""
    if not text:
        return ''
    head, sep, rest = text.partition(':')
    if not sep or head.strip().lower() != 'weak evidence':
        head, rest = 'Weak evidence', ' ' + text
    return ('<p class="honesty caveat" id="weak-evidence" role="note"><strong>%s:</strong>%s</p>'
            % (_escape(head), _escape(rest)))


def _escape(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;'))


def render_report(study_dir, out_dir=None, single_file=False, days=None, variants=None, pred=None, artifact=False,
                  settings_path=contracts.NS_SETTINGS_PATH, settings=None, frozen_dir=None, generated=None,
                  studies_dir=st.STUDIES_DIR, log=print):
    """Render studies/<study>/report/index.html (or out_dir/index.html); -> ReportResult. Raises ReportError."""
    sd = study_dir if isinstance(study_dir, st.StudyDir) else resolve_study(study_dir, studies_dir)
    out_dir = os.path.abspath(out_dir or sd.report_dir)
    settings = settings if settings is not None else load_settings(settings_path)
    study, rows, status, rounds = load_bundle(sd)
    collected = collect_tier2(sd, rows)
    days_meta = frozen_meta([d.day for d in study.days], study.host, frozen_dir)
    warnings = []
    inline_paths = []
    if single_file:
        sel_days = select_days(days, rows)
        sel_variants = select_variants(variants, rows, rounds, status)
        sel_pred = select_variants(pred, rows, rounds, status) if pred and pred != 'none' else set()
        inline_paths = inline_set(collected, sel_days, sel_variants, sel_pred)
    data = build_page_data(sd, study, rows, status, rounds, days_meta, settings, collected,
                           generated or contracts.now_iso(), single_file, inline_paths, artifact)
    for s in page_sentences(data):
        try:
            lint.lint_text(s, 'line')
        except lint.LintError as e:
            raise ReportError(f'generated text refused by the lint: {e}') from None
    os.makedirs(out_dir, exist_ok=True)
    sizes = {} if single_file else place_tier2(collected, out_dir)
    inline_docs = {p: _read_json(collected[p][0]) for p in inline_paths}
    inline_docs = {p: d for p, d in inline_docs.items() if d is not None}
    private = private_strings(study, settings)
    text = render_html(data, inline_docs, '', artifact, private)
    tier2_bytes = sum(sizes.values())
    note = f'page {_human_size(len(text.encode("utf-8")))}'
    if sizes:
        note += f', per-loop files {_human_size(tier2_bytes)} in {len(sizes)}'
    text = render_html(data, inline_docs, note, artifact, private)
    size = len(text.encode('utf-8'))
    if size > ARTIFACT_MAX_BYTES:
        warnings.append(f'the page is {_human_size(size)}, over the 16 MB artifact limit: inline fewer days or '
                        f'variants (--days, --variants top1) or leave --pred out')
    elif single_file and size > SINGLE_FILE_WARN_BYTES:
        warnings.append(f'the page is {_human_size(size)}, above the 12 MB warning size (limit 16 MB): consider '
                        f'fewer days or variants')
    path = os.path.join(out_dir, 'index.html')
    tmp = path + '.%d.tmp' % os.getpid()
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    os.replace(tmp, path)
    files = {} if single_file else {dest: os.path.join(out_dir, *dest.split('/')) for dest in sizes}
    res = ReportResult(path=path, bytes=size, out_dir=out_dir, tier2=sizes, inlined=list(inline_docs),
                       warnings=warnings, files=files)
    for w in warnings:
        log('warning: ' + w)
    return res


# ----------------------------------------------------------------------------- serve

class _Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):                      # quiet: one line per request on stderr is noise here
        pass


def make_server(directory, port=DEFAULT_PORT, bind='127.0.0.1'):
    """A ThreadingHTTPServer on `directory` (stdlib only); port 0 picks a free port (server.server_address)."""
    handler = functools.partial(_Handler, directory=os.path.abspath(directory))
    socketserver.TCPServer.allow_reuse_address = True
    return http.server.ThreadingHTTPServer((bind, port), handler)


def serve(directory, port=DEFAULT_PORT, bind='127.0.0.1', log=print):
    server = make_server(directory, port, bind)
    host, p = server.server_address[:2]
    log(f'serving {directory} at http://{host}:{p}/  (Ctrl-C stops)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ----------------------------------------------------------------------------- CLI

def _add_report_options(p):
    p.add_argument('--out', help='output directory (default studies/<study>/report)')
    p.add_argument('--single-file', action='store_true', help='inline the selected per-loop documents (for viewing from disk)')
    p.add_argument('--days', help='--single-file: comma list of UTC days (default all)')
    p.add_argument('--variants', default='top3', help='--single-file: all | topN | comma list of labels or vhash prefixes (base always)')
    p.add_argument('--pred', default=None, help='--single-file: also inline prediction documents: topN | all | labels')
    p.add_argument('--artifact', action='store_true', help='write the page without its html/head/body skeleton (Artifact publish form)')
    p.add_argument('--studies-dir', default=st.STUDIES_DIR)
    p.add_argument('--settings', default=contracts.NS_SETTINGS_PATH)
    p.add_argument('--frozen-dir', default=None, help='frozen/ base for the day records (default software/frozen)')


def register(subparsers):
    p = subparsers.add_parser('report', help='render studies/<study>/report/index.html (H-401)')
    p.add_argument('study', help='study name under studies/ or a study directory')
    _add_report_options(p)
    p.set_defaults(func=run_report)
    s = subparsers.add_parser('serve', help='stdlib http.server on studies/<study>/report/ (renders it first)')
    s.add_argument('study', help='study name under studies/ or a study directory')
    s.add_argument('--port', type=int, default=DEFAULT_PORT)
    s.add_argument('--bind', default='127.0.0.1')
    s.add_argument('--no-render', action='store_true', help='serve what is there, do not render first')
    _add_report_options(s)
    s.set_defaults(func=run_serve)


def _summary_lines(res):
    lines = [f'wrote {res.path} ({_human_size(res.bytes)})']
    if res.tier2:
        lines.append(f'  {len(res.tier2)} per-loop file(s) next to it, {_human_size(sum(res.tier2.values()))}')
    if res.inlined:
        lines.append(f'  {len(res.inlined)} per-loop document(s) inlined')
    return lines


def run_report(args, log=print):
    try:
        res = render_report(args.study, out_dir=args.out, single_file=args.single_file, days=args.days,
                            variants=args.variants, pred=args.pred, artifact=args.artifact,
                            settings_path=args.settings, frozen_dir=args.frozen_dir, studies_dir=args.studies_dir, log=log)
    except (ReportError, st.StudyError, contracts.ContractError) as e:
        log(f'refused: {e}')
        return st.EXIT_REFUSED
    for line in _summary_lines(res):
        log(line)
    if args.artifact and res.files:
        log('  files map for the Artifact publish: ' + json.dumps(res.files))
    log('')
    log(contracts.HONESTY_STATEMENT)
    return st.EXIT_OK


def run_serve(args, log=print):
    try:
        sd = resolve_study(args.study, args.studies_dir)
    except (ReportError, st.StudyError) as e:
        log(f'refused: {e}')
        return st.EXIT_REFUSED
    out = os.path.abspath(args.out or sd.report_dir)
    if not args.no_render or not os.path.isfile(os.path.join(out, 'index.html')):
        try:
            res = render_report(sd, out_dir=out, settings_path=args.settings, frozen_dir=args.frozen_dir, log=log)
        except (ReportError, contracts.ContractError) as e:
            log(f'refused: {e}')
            return st.EXIT_REFUSED
        for line in _summary_lines(res):
            log(line)
    return serve(out, args.port, args.bind, log=log)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='hc', description='Hindcast report / serve')
    sub = parser.add_subparsers(dest='command', metavar='report|serve')
    sub.required = True
    register(sub)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    sys.exit(main())
