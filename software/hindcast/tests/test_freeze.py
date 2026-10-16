"""
Tests for hindcast.freeze: the frozen file writer and meta, FrozenSource as a drop-in for ns_source.NightscoutSource,
staleness / --new / --refreeze / blocked_reason semantics, the offline guard, warm (zero requests the second time)
and the CLI registration. Everything runs offline against a synthetic cache and a fake live source; the acceptance
test against the real Nightscout cache (frozen loops() == live loops() for 2026-09-06) needs the cache and
--run-slow (the live reconstruction of a day takes 40-150 s).
"""
import argparse
import gzip
import json
import os
import socket
import time

import pytest

import ns_client
import ns_source
from ns_client import NightscoutClient, day_bounds, to_iso

from hindcast import contracts, days, freeze
from hindcast.tests import conftest

DAY = '2026-09-06'
START, END = day_bounds(DAY)
H = 60 * 60 * 1000
MIN = 60 * 1000
SETTINGS = {'nightscout': {'url': 'https://example.test', 'token': 'secret', 'api_secret_sha1': None, 'cache_dir': None},
            'safety': {'max_iob': 7.0},
            'profile_history': {'changes': [{'when': '2026-09-06T20:23:00+02:00', 'parameter': 'autoISF_max', 'from': 1.3, 'to': 1.5}]}}
HOST = 'example.test'


# ----------------------------------------------------------------------------- fixtures

def _touch(path, mtime_ms, payload='[]'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(payload)
    os.utime(path, (mtime_ms / 1000.0, mtime_ms / 1000.0))


def make_cache(root, day, mtime_ms, skip=()):
    for cf in days.consulted_files(day):
        if f'{cf.coll}/{cf.name}' in skip:
            continue
        _touch(cf.path(root), mtime_ms, 'null' if cf.coll == 'latest' else '[]')


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Synthetic settings (cache under tmp), frozen base under tmp, offline, a fake live source installed."""
    settings = json.loads(json.dumps(SETTINGS))
    settings['nightscout']['cache_dir'] = str(tmp_path / 'cache')
    cache_root = os.path.join(str(tmp_path / 'cache'), HOST)
    make_cache(cache_root, DAY, END + 2 * H)
    base = str(tmp_path / 'frozen')
    monkeypatch.setenv('HINDCAST_FROZEN_DIR', base)
    monkeypatch.delenv('HINDCAST_OFFLINE', raising=False)
    monkeypatch.setattr(freeze, '_RecordingSource', FakeLiveSource)
    settings_path = str(tmp_path / 'ns_settings.json')
    with open(settings_path, 'w', encoding='utf-8') as fh:
        json.dump(settings, fh)
    return argparse.Namespace(settings=settings, settings_path=settings_path, cache_root=cache_root, base=base, tmp=tmp_path)


def fake_loop(i, ms):
    return {'record': {'_id': f'id{i}', 'openaps': {'suggested': {'deliverAt': to_iso(ms), 'reason': 'r', 'bg': 120, 'predBGs': {}}}},
            'suggested': {'deliverAt': to_iso(ms), 'reason': 'r', 'bg': 120, 'predBGs': {}, 'variable_sens': 91.8},
            'stamp': to_iso(ms), 'now_ms': ms, 'utcOffset': 2.0,
            'glucose_status': {'glucose': 120.5, 'delta': -0.5, 'short_avgdelta': 0.0, 'long_avgdelta': 0.1, 'noise': 0.0},
            'iob_array': [{'iob': 1.0, 'activity': 0.01, 'lastBolusTime': ms - H}], 'currenttemp': {'rate': 0.35, 'duration': 27},
            'lastTempAge': 3, 'profile': {'autoISF_max': 1.3, 'enable_autoISF': True, 'sens': 91.8}, 'activity_monitor': {'recentSteps5Minutes': 50},
            'autosens_data': {'ratio': 1.0}, 'meal_data': {'carbs': 0.0, 'mealCOB': 0.0}, 'microBolusAllowed': i % 7 != 3,
            'console_error': ['start AutoISF 3.2.0'] + (['SMB disabled (!microBolusAllowed)'] if i % 7 == 3 else []),
            'autoISF_version': '3.2.0'}


class FakeLiveSource:
    """Stands in for freeze._RecordingSource: 100 loops of the window's UTC day one minute apart from 06:00, a
    duplicate every 10th record, one aborted and one no-CGM record, and an 'SMB fallback' skip counted beside its
    loop (only the loops inside [start, end) are yielded, like the live source)."""
    N = 100

    def __init__(self, url, start_iso, end_iso, settings=None, log_msg=print, verbose=True):
        self.url, self.settings, self.log_msg = url, settings, log_msg
        self.start_ms, self.end_ms = ns_client.parse_iso(start_iso), ns_client.parse_iso(end_iso)
        self.skipped = {}
        self.pending = []
        self.entries = [{'date': START + i * 5 * MIN, 'sgv': 100.0 + i % 50} for i in range(288)]
        self.treatments = [{'eventType': 'Correction Bolus', 'insulin': 0.3, 'type': 'SMB', 'created_at': to_iso(START + 6 * H)}]
        self.iob = None
        self.profiles = None
        self.loaded = False
        FakeLiveSource.instances.append(self)

    instances = []

    def load(self):
        self.loaded = True

    def _skip(self, why, ms):
        self.skipped[why] = self.skipped.get(why, 0) + 1
        self.pending.append({'dropped': why, 'ms': ms})

    def loops(self):
        assert self.loaded
        day_start = day_bounds(ns_client.utc_day(self.start_ms))[0]
        for i in range(self.N):
            ms = day_start + 6 * H + i * MIN
            if not (self.start_ms <= ms < self.end_ms):
                continue
            if i % 10 == 9:
                self._skip('duplicate upload', ms)
            if i == 50:
                self._skip('loop aborted by AAPS (no bg/predBGs): CGM data is unchanged', ms)
                continue
            if i == 60:
                self._skip('no CGM value within 12 min', ms)
                continue
            if i % 7 == 3:
                self._skip('SMB fallback loop (replayed without SMB)', ms)
            yield fake_loop(i, ms)
        if self.skipped:
            self.log_msg('\nNightscout loops skipped: ' + ', '.join(f'{v} x {k}' for k, v in self.skipped.items()))


def freeze_fake(env, day=DAY, **kw):
    return freeze.freeze_day(day, settings=env.settings, settings_path=env.settings_path, log_msg=lambda *a: None, **kw)


# ----------------------------------------------------------------------------- writer and meta

def test_freeze_day_writes_files_and_meta(env):
    meta = freeze_fake(env)
    lp, mp = freeze.loops_path(DAY, HOST), freeze.meta_path(DAY, HOST)
    assert os.path.isfile(lp) and os.path.isfile(mp) and lp.startswith(env.base)
    assert meta == json.load(open(mp, encoding='utf-8'))
    assert meta['day'] == DAY and meta['host'] == HOST and meta['url'] == 'https://example.test'
    assert meta['loops'] == 98 and meta['final'] is True and meta['partial'] is False and meta['format'] == freeze.FORMAT
    assert meta['dropped'] == {'no_reason': 0, 'aborted': 1, 'no_cgm': 1, 'duplicate': 10, 'outside': 0, 'smb_fallback': 14}
    assert meta['ns_code_sha'] == freeze.ns_code_sha() and len(meta['ns_code_sha']) == 40
    assert meta['code_sha'] == contracts.code_sha()
    assert meta['settings_sha'] == contracts.settings_sha(settings=env.settings)
    assert meta['window'] == ['2026-09-06T00:00:00.000Z', '2026-09-07T00:00:00.000Z']
    assert meta['character']['loops'] == 98 and meta['character']['smb_U'] == 0.3 and meta['character']['cgm_values'] == 288
    assert meta['character']['tdd_U'] is None                                 # the fake source has no iob/profiles
    assert 'profile_history: autoISF_max 1.3->1.5 at 20:23 local' in meta['confounders']
    assert meta['autoISF_versions'] == ['3.2.0']
    assert 'secret' not in open(mp, encoding='utf-8').read()                  # the token never reaches the meta
    # sha1 is over the uncompressed JSONL and the event order is loop order with drops in place
    with gzip.open(lp, 'rb') as f:
        raw = f.read()
    import hashlib
    assert hashlib.sha1(raw).hexdigest() == meta['sha1'] and len(raw) == meta['bytes']
    assert meta['bytes_gz'] == os.path.getsize(lp)
    events = list(freeze.read_events(lp))
    assert len(events) == 98 + 26
    assert events[0]['now_ms'] == START + 6 * H
    drops = [e for e in events if 'dropped' in e]
    assert all(set(e) == {'dropped', 'ms'} for e in drops)
    i_fallback = next(i for i, e in enumerate(events) if e.get('dropped', '').startswith('SMB fallback'))
    assert events[i_fallback + 1]['now_ms'] == events[i_fallback]['ms']       # the counted loop follows its skip line
    assert [l['now_ms'] for l in freeze.read_loops(lp)] == [e['now_ms'] for e in events if 'dropped' not in e]
    assert not os.path.exists(lp + '.tmp')


def test_freeze_day_refuses_existing_partial_missing(env):
    freeze_fake(env)
    with pytest.raises(freeze.FreezeError, match='already frozen'):
        freeze_fake(env)
    meta = freeze_fake(env, force=True)
    assert meta['loops'] == 98

    make_cache(env.cache_root, DAY, END + 10 * MIN)                               # written inside the settle hour
    with pytest.raises(freeze.FreezeError, match='not final'):
        freeze_fake(env, force=True)
    meta = freeze_fake(env, force=True, allow_partial=True)
    assert meta['final'] is False and meta['partial'] is True and meta['finality']['partial']

    os.remove(os.path.join(env.cache_root, 'devicestatus', '2026-09-07.json'))
    with pytest.raises(freeze.FreezeError, match='cache incomplete'):            # offline: cannot fetch the missing file
        freeze_fake(env, force=True, allow_partial=True)
    with pytest.raises(freeze.FreezeError):
        freeze_fake(env, day='2026-99-99')


def test_freeze_day_without_loops_leaves_nothing(env, monkeypatch):
    monkeypatch.setattr(FakeLiveSource, 'N', 0)
    with pytest.raises(freeze.FreezeError, match='no loop'):
        freeze_fake(env)
    assert not os.path.exists(freeze.loops_path(DAY, HOST))


# ----------------------------------------------------------------------------- staleness, --new, --refreeze, blocked

def test_staleness_and_blocked_reason(env):
    assert freeze.blocked_reason(DAY, settings=env.settings, settings_path=env.settings_path).startswith(f'{DAY} is not frozen')
    meta = freeze_fake(env)
    assert freeze.staleness(meta, env.settings, settings_path=env.settings_path) == []
    assert freeze.blocked_reason(DAY, settings=env.settings, settings_path=env.settings_path) is None

    changed = json.loads(json.dumps(env.settings))
    changed['safety']['max_iob'] = 8.0
    assert freeze.staleness(meta, changed, settings_path=env.settings_path) == ['settings_sha changed']
    assert 'stale' in freeze.blocked_reason(DAY, settings=changed, settings_path=env.settings_path)
    assert freeze.staleness(meta, env.settings, software_dir=str(env.tmp), settings_path=env.settings_path) == ['ns_code_sha changed']
    assert freeze.staleness(dict(meta, format=99), env.settings, settings_path=env.settings_path) == ['format changed']
    assert freeze.staleness(None) == ['no meta']

    make_cache(env.cache_root, DAY, END + 10 * MIN)
    freeze_fake(env, force=True, allow_partial=True)
    assert 'partial' in freeze.blocked_reason(DAY, settings=env.settings, settings_path=env.settings_path)
    assert freeze.blocked_reason(DAY, settings=env.settings, settings_path=env.settings_path, require_final=False) is None


def test_freeze_days_new_and_refreeze(env):
    kw = dict(settings=env.settings, settings_path=env.settings_path, log_msg=lambda *a: None)
    make_cache(env.cache_root, '2026-09-05', END + 2 * H)
    res = freeze.freeze_days('2026-09-05..2026-09-07', **kw)
    assert [(d, o) for d, o, _ in res] == [('2026-09-05', 'frozen'), (DAY, 'frozen'), ('2026-09-07', 'refused')]
    assert 'not final' in res[2][2] or 'incomplete' in res[2][2]
    assert freeze.outcomes_exit_code(res) == freeze.EXIT_REFUSED

    n = len(FakeLiveSource.instances)
    res = freeze.freeze_days(DAY, new=True, **kw)                                  # --new keeps a fresh frozen day
    assert res == [(DAY, 'kept', 'already frozen')] and len(FakeLiveSource.instances) == n
    res = freeze.freeze_days(DAY, **kw)                                            # so does a plain call
    assert res[0][1] == 'kept' and len(FakeLiveSource.instances) == n
    res = freeze.freeze_days(DAY, refreeze=True, **kw)                             # --refreeze replaces it
    assert res[0][1] == 'frozen' and len(FakeLiveSource.instances) == n + 1
    assert freeze.outcomes_exit_code(res) == freeze.EXIT_OK

    changed = json.loads(json.dumps(env.settings))
    changed['safety']['max_iob'] = 8.0
    kw2 = dict(kw, settings=changed)
    res = freeze.freeze_days(DAY, new=True, **kw2)
    assert res[0][1] == 'refused' and 'stale: settings_sha changed' in res[0][2]
    res = freeze.freeze_days(DAY, new=True, refreeze=True, **kw2)
    assert res[0][1] == 'frozen'
    assert freeze.staleness(freeze.read_meta(DAY, HOST), changed, settings_path=env.settings_path) == []


def test_list_days_and_format(env):
    freeze_fake(env)
    rows = freeze.list_days('2026-09-05..2026-09-06', settings=env.settings, settings_path=env.settings_path, verify=True)
    assert [r['day'] for r in rows] == ['2026-09-05', DAY]
    # 2026-09-05's own three files exist (the look-back of 2026-09-06 wrote them) but its own look-back files do not
    assert rows[0]['cached'] is True and rows[0]['final'] is False and rows[0]['frozen'] is False and rows[0]['summary'] == ''
    assert rows[1]['final'] and rows[1]['frozen'] and rows[1]['loops'] == 98 and rows[1]['sha_ok'] is True and rows[1]['stale'] == []
    text = freeze.format_listing(rows)
    assert 'partial' in text.splitlines()[1] and '98 loops' in text.splitlines()[2] and '98 loops' in rows[1]['summary']
    rows = freeze.list_days('2026-09-04', settings=env.settings, settings_path=env.settings_path)
    assert rows[0]['cached'] is False and 'missing' in freeze.format_listing(rows)
    rows = freeze.list_days(None, settings=env.settings, settings_path=env.settings_path)   # every cached or frozen day
    assert {r['day'] for r in rows} >= {'2026-09-05', DAY, '2026-09-07'}
    changed = json.loads(json.dumps(env.settings))
    changed['safety']['max_iob'] = 8.0
    rows = freeze.list_days(DAY, settings=changed, settings_path=env.settings_path)
    assert rows[0]['stale'] == ['settings_sha changed'] and 'STALE' in freeze.format_listing(rows)


# ----------------------------------------------------------------------------- FrozenSource

def test_frozen_source_is_a_drop_in(env):
    freeze_fake(env)
    s_iso, e_iso = to_iso(START + 6 * H + 10 * MIN), to_iso(START + 6 * H + 40 * MIN)
    live = FakeLiveSource('ns:', s_iso, e_iso, settings=env.settings, log_msg=lambda *a: None)
    live.load()
    L = list(live.loops())
    msgs = []
    fro = freeze.FrozenSource('ns:', s_iso, e_iso, settings=env.settings, log_msg=msgs.append)
    assert fro.url == 'https://example.test' and fro.start_ms == START + 6 * H + 10 * MIN and fro.skipped == {}
    fro.load()
    F = list(fro.loops())
    assert F == L and len(F) == 30                                            # i = 10..39 (the dropped records lie outside)
    assert fro.skipped == live.skipped
    assert msgs[0].startswith('Frozen window ') and msgs[-1].startswith('\nNightscout loops skipped: ')
    assert msgs[-1] == '\nNightscout loops skipped: ' + ', '.join(f'{v} x {k}' for k, v in live.skipped.items())
    assert fro.days == [DAY] and fro.metas[DAY]['loops'] == 98

    # loops() without load() loads; a window with no loop yields nothing and prints no counters
    fro2 = freeze.FrozenSource('ns:', to_iso(START), to_iso(START + H), settings=env.settings, log_msg=msgs.append)
    assert list(fro2.loops()) == [] and fro2.skipped == {}

    # the whole day: every stored loop, every drop counted
    fro3 = freeze.FrozenSource('ns:', *days.day_window(DAY), settings=env.settings, log_msg=lambda *a: None)
    F3 = list(fro3.loops())
    assert len(F3) == 98 and fro3.skipped == {'duplicate upload': 10, 'loop aborted by AAPS (no bg/predBGs): CGM data is unchanged': 1,
                                               'no CGM value within 12 min': 1, 'SMB fallback loop (replayed without SMB)': 14}


def test_frozen_source_missing_day_fails_in_load_as_runtime_error(env):
    freeze_fake(env)
    fro = freeze.FrozenSource('ns:', to_iso(START - H), to_iso(START + H), settings=env.settings, log_msg=lambda *a: None)
    with pytest.raises(freeze.FreezeError, match='not frozen: 2026-09-05') as e:
        fro.load()
    assert isinstance(e.value, RuntimeError)                                     # scanNightscout catches RuntimeError -> STOP


def test_frozen_source_open_bounds_resolve_like_live(env, monkeypatch):
    freeze_fake(env)
    now = START + 20 * H
    monkeypatch.setattr(time, 'time', lambda: now / 1000.0)
    fro = freeze.FrozenSource('ns:', '2000', '2099', settings=env.settings, log_msg=lambda *a: None)
    assert fro._window() == ns_source.resolve_window('2000', '2099', now_ms=now) == (now - 24 * H, now)
    assert fro._window()[0] < START, 'the open window reaches into the day before, which is not frozen'
    with pytest.raises(freeze.FreezeError):
        fro.load()


def test_frozen_source_opens_no_socket(env, monkeypatch):
    freeze_fake(env)

    def boom(*a, **k):
        raise AssertionError('socket opened during a frozen replay')
    monkeypatch.setattr(socket, 'socket', boom)
    monkeypatch.setattr(ns_client.urllib.request, 'urlopen', boom)
    fro = freeze.FrozenSource('ns:', *days.day_window(DAY), settings=env.settings, log_msg=lambda *a: None)
    assert len(list(fro.loops())) == 98


def test_install_frozen_source_on_the_real_module():
    live = freeze.install_frozen_source()
    try:
        assert ns_source.NightscoutSource is freeze.FrozenSource and live is freeze._LIVE_SOURCE
        assert ns_source.resolve_window('2026-09-06T00:00:00Z', '2026-09-07T00:00:00Z') == (START, END)
    finally:
        freeze.uninstall_frozen_source()
    assert ns_source.NightscoutSource is live


def test_core_attribute_names_pinned():
    """scanNightscout() constructs ns_source.NightscoutSource(fn, start, end, log_msg=...), calls load() and iterates
    loops(); FrozenSource must keep exactly these (DESIGN.md section 4.4)."""
    import inspect
    import emulator_core
    src = inspect.getsource(emulator_core.scanNightscout)
    assert 'ns_source.NightscoutSource(fn, t_startLabel, t_stoppLabel, log_msg=log_msg)' in src
    assert 'src.load()' in src and 'src.loops()' in inspect.getsource(emulator_core.scanNightscoutLoops)
    for name in ('load', 'loops', 'skipped', 'url', 'settings', 'start_ms', 'end_ms'):
        assert hasattr(freeze.FrozenSource, name) or name in freeze.FrozenSource.__init__.__code__.co_names
    live_sig = inspect.signature(ns_source.NightscoutSource.__init__)
    frozen_sig = inspect.signature(freeze.FrozenSource.__init__)
    assert list(frozen_sig.parameters)[:len(live_sig.parameters)] == list(live_sig.parameters)


# ----------------------------------------------------------------------------- offline guard

def test_offline_guard_serves_cache_and_refuses_network(env, monkeypatch):
    client = days.client_for('ns:', env.settings)
    calls = []
    monkeypatch.setattr(NightscoutClient, '_get', lambda self, path, params: calls.append(path) or [])
    monkeypatch.setattr(ns_client.time, 'sleep', lambda s: None)
    # a stale partial file: ns_client would refetch it, the guard serves it
    fn = os.path.join(env.cache_root, 'entries', '2026-09-07.json')
    _touch(fn, END + 10 * MIN, '[{"date": %d, "sgv": 100}]' % (END + 5 * MIN))
    with freeze.offline_guard(True):
        assert client.entries(END, END + H) == [{'date': END + 5 * MIN, 'sgv': 100}]
        assert client.profiles(newer_than=END) == []
        assert client.latest_treatment('Profile Switch', START - days.LOOKBACK_MS) is None
        with pytest.raises(freeze.OfflineError, match='missing'):
            client.entries(START - 3 * 24 * H, START - 2 * 24 * H)
        with pytest.raises(freeze.OfflineError):
            client.status()
        with pytest.raises(freeze.OfflineError):
            client.latest_treatment('Profile Switch', START)
    assert calls == []
    # restored afterwards: the patched class _get is called again
    client.status()
    assert calls == ['/api/v1/status.json']
    with freeze.offline_guard(False):
        client.status()
    assert len(calls) == 2


def test_is_offline_default(monkeypatch):
    monkeypatch.delenv('HINDCAST_OFFLINE', raising=False)
    assert freeze.is_offline()
    monkeypatch.setenv('HINDCAST_OFFLINE', '0')
    assert not freeze.is_offline()
    monkeypatch.setenv('HINDCAST_OFFLINE', '1')
    assert freeze.is_offline()


# ----------------------------------------------------------------------------- warm

def test_warm_second_run_makes_no_request(env, monkeypatch):
    requests = []

    def fake_get(self, path, params):
        requests.append((path, dict(params)))
        if path.startswith('/api/v1/profile'):
            return [{'defaultProfile': 'p', 'store': {}}]
        return []
    monkeypatch.setattr(NightscoutClient, '_get', fake_get)
    monkeypatch.setattr(ns_client.time, 'sleep', lambda s: None)
    day = '2020-01-10'                                                            # long over: files written now are final
    settings = env.settings
    import shutil
    shutil.rmtree(env.cache_root)
    msgs = []
    res = freeze.warm_days(day, settings=settings, log_msg=msgs.append, settings_path=env.settings_path)
    assert len(res) == 1 and res[0][1] == 'warmed'
    n_first = len(requests)
    assert n_first >= 7                                                            # 3 collections x 2-3 day files, profile, 2 latest
    assert all('token=' not in p for p, _ in requests)                            # the token is added inside the real _get only
    assert days.finality(day, env.cache_root).final
    assert os.environ.get('HINDCAST_OFFLINE') == '1'
    assert not os.path.exists(os.path.join(env.cache_root, 'warm.lock'))

    monkeypatch.setattr(ns_client.urllib.request, 'urlopen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))
    monkeypatch.setattr(NightscoutClient, '_get', lambda self, path, params: (_ for _ in ()).throw(AssertionError('request made')))
    res = freeze.warm_days(day, settings=settings, log_msg=msgs.append, settings_path=env.settings_path)
    assert res == [(day, 'warmed', '0 requests')]
    assert msgs[-1] == f'warm {day}: 0 requests'


def test_warm_refuses_days_that_cannot_be_final(env, monkeypatch):
    monkeypatch.setattr(NightscoutClient, '_get', lambda self, path, params: [])
    now = END + 30 * MIN
    res = freeze.warm_days('2026-09-05..2026-09-06', settings=env.settings, now_ms=now, settings_path=env.settings_path)
    assert [o for _, o, _ in res] == ['warmed', 'refused'] and 'allow-partial' in res[1][2]
    res = freeze.warm_days(DAY, settings=env.settings, now_ms=now, allow_partial=True, settings_path=env.settings_path)
    assert res[0][1] == 'warmed' and '(partial)' in res[0][2]


def test_warm_lock_single_stream(env, monkeypatch):
    monkeypatch.setattr(NightscoutClient, '_get', lambda self, path, params: [])
    os.makedirs(env.cache_root, exist_ok=True)
    lock = os.path.join(env.cache_root, 'warm.lock')
    with open(lock, 'w') as fh:
        fh.write('4242 now\n')
    with pytest.raises(freeze.FreezeError, match='another warm is running'):
        freeze.warm_days('2020-01-10', settings=env.settings, settings_path=env.settings_path)
    os.utime(lock, (time.time() - 3 * 3600, time.time() - 3 * 3600))                 # stale lock is taken over
    res = freeze.warm_days('2020-01-10', settings=env.settings, settings_path=env.settings_path)
    assert res[0][1] == 'warmed' and not os.path.exists(lock)


def test_warm_failure_stops_the_walk(env, monkeypatch):
    def failing(self, path, params):
        raise RuntimeError('Nightscout request failed after retries: https://example.test/x?token=***')
    monkeypatch.setattr(NightscoutClient, '_get', failing)
    res = freeze.warm_days('2020-01-10..2020-01-12', settings=env.settings, settings_path=env.settings_path)
    assert len(res) == 1 and res[0][1] == 'failed' and 'secret' not in res[0][2]


# ----------------------------------------------------------------------------- CLI

def test_register_and_run(env, monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    freeze.register(sub)
    kw = dict(settings=env.settings, settings_path=env.settings_path, log_msg=lambda *a: None)

    args = parser.parse_args(['freeze', DAY])
    assert args.func is freeze.run_freeze and args.url == 'ns:' and not args.new
    assert freeze.run_freeze(args, **kw) == freeze.EXIT_OK
    assert f'{DAY}: frozen - 98 loops' in capsys.readouterr().out
    assert freeze.run_freeze(parser.parse_args(['freeze', DAY, '--new']), **kw) == freeze.EXIT_OK
    assert 'kept' in capsys.readouterr().out
    assert freeze.run_freeze(parser.parse_args(['freeze', '2026-09-07']), **kw) == freeze.EXIT_REFUSED
    assert freeze.run_freeze(parser.parse_args(['freeze']), **kw) == freeze.EXIT_REFUSED
    assert 'give the days' in capsys.readouterr().err
    assert freeze.run_freeze(parser.parse_args(['freeze', 'not-a-day']), **kw) == freeze.EXIT_REFUSED
    assert freeze.run_freeze(parser.parse_args(['freeze', '--list', DAY, '--verify']), **kw) == freeze.EXIT_OK
    out = capsys.readouterr().out
    assert '98 loops' in out and 'final' in out
    assert os.environ.get('HINDCAST_OFFLINE') is None
    assert freeze.run_freeze(parser.parse_args(['freeze', '--list', DAY, '--online']), **kw) == freeze.EXIT_OK
    assert os.environ.get('HINDCAST_OFFLINE') == '0'

    wargs = parser.parse_args(['warm', '2020-01-10', '--allow-partial'])
    assert wargs.func is freeze.run_warm and wargs.allow_partial
    monkeypatch.setattr(NightscoutClient, '_get', lambda self, path, params: [])
    monkeypatch.setattr(ns_client.time, 'sleep', lambda s: None)
    assert freeze.run_warm(wargs, **kw) == freeze.EXIT_OK
    assert 'warmed' in capsys.readouterr().out
    assert freeze.run_warm(parser.parse_args(['warm', 'today']), **kw) == freeze.EXIT_REFUSED
    with pytest.raises(SystemExit):
        freeze.main([])


# ----------------------------------------------------------------------------- the real cache (acceptance of H-101)

@pytest.mark.slow
@pytest.mark.needs_cache('2026-09-05', '2026-09-06', '2026-09-07')
def test_frozen_equals_live_on_the_real_cache(tmp_path, monkeypatch):
    """Freeze 2026-09-06 from the local cache (no network: the offline guard is on), then the frozen loops of the
    06-09Z window must equal, dict for dict, the loops the live source reconstructs over the whole day, and the
    end-of-run counters must match the live 06-09Z source. A live 06-09Z source is NOT dict-equal to a frozen
    day: ns_source's smoother and deviation history start at the window start, so a window-start dependence of
    the live reconstruction exists (reported to the fidelity epic); the frozen day is the whole-day reconstruction."""
    monkeypatch.setenv('HINDCAST_FROZEN_DIR', str(tmp_path / 'frozen'))
    monkeypatch.delenv('HINDCAST_OFFLINE', raising=False)
    fin = days.finality(DAY, days.cache_dir())
    if not fin.complete:
        pytest.skip('cache incomplete for 2026-09-06: ' + fin.why())
    t0 = time.time()
    meta = freeze.freeze_day(DAY, allow_partial=not fin.final, log_msg=lambda *a: None)
    seconds = time.time() - t0
    assert meta['loops'] > 1000 and meta['bytes_gz'] < 6 * 1024 * 1024
    s_iso, e_iso = conftest.CHECK_WINDOW
    s_ms, e_ms = ns_client.parse_iso(s_iso), ns_client.parse_iso(e_iso)

    fro = freeze.FrozenSource('ns:', s_iso, e_iso, log_msg=lambda *a: None)
    F = list(fro.loops())
    assert len(F) == 180

    with freeze.offline_guard(True):
        day_live = ns_source.NightscoutSource('ns:', *days.day_window(DAY), log_msg=lambda *a: None, verbose=False)
        D = [l for l in day_live.loops() if s_ms <= l['now_ms'] < e_ms]
        win_live = ns_source.NightscoutSource('ns:', s_iso, e_iso, log_msg=lambda *a: None, verbose=False)
        W = list(win_live.loops())
    assert F == D
    assert fro.skipped == win_live.skipped
    assert [l['stamp'] for l in F] == [l['stamp'] for l in W]
    print(f'\nfreeze {DAY}: {meta["loops"]} loops, {meta["bytes_gz"] / 1e6:.2f} MB gz, {seconds:.1f} s; '
          f'frozen == whole-day live: True; keys differing from the 06-09Z live source: '
          f'{sorted({k for a, b in zip(F, W) for k in a if a[k] != b[k]})}')
