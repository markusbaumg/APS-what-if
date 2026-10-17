"""Tests for hindcast.days: day specs, finality from cache mtimes, character and confounders (offline, synthetic)."""
import json
import os
import time

import pytest

from hindcast import days
from ns_client import day_bounds

DAY = '2026-09-06'
START, END = day_bounds(DAY)
H = 60 * 60 * 1000
MIN = 60 * 1000


# ----------------------------------------------------------------------------- day specs

def test_parse_days_forms():
    assert days.parse_days('2026-09-06') == ['2026-09-06']
    assert days.parse_days('2026-08-30..2026-09-01') == ['2026-08-30', '2026-08-31', '2026-09-01']
    assert days.parse_days('2026-09-02,2026-09-01,2026-09-02') == ['2026-09-01', '2026-09-02']
    assert days.parse_days(['2026-09-03', '2026-09-01..2026-09-02']) == ['2026-09-01', '2026-09-02', '2026-09-03']
    now = START + 12 * H                                  # noon on 2026-09-06 UTC
    assert days.parse_days('yesterday', now_ms=now) == ['2026-09-05']
    assert days.parse_days('yesterday..today', now_ms=now) == ['2026-09-05', '2026-09-06']


@pytest.mark.parametrize('bad', ['', '2026-13-01', '2026-09-06..2026-09-05', 'monday', '2026-9-6', '2020-01-01..2021-12-31'])
def test_parse_days_refuses(bad):
    with pytest.raises(ValueError):
        days.parse_days(bad)


def test_day_window_and_days_of_window():
    s, e = days.day_window(DAY)
    assert (s, e) == ('2026-09-06T00:00:00.000Z', '2026-09-07T00:00:00.000Z')
    assert days.days_of_window(START, END) == [DAY]
    assert days.days_of_window(START - 1, END + 1) == ['2026-09-05', DAY, '2026-09-07']
    assert days.days_of_window(START, START) == []


def test_host_and_url_resolution():
    settings = {'nightscout': {'url': 'https://ns.example.org:8443/', 'cache_dir': None}}
    assert days.resolve_url('ns:', settings) == 'https://ns.example.org:8443/'
    assert days.resolve_url('https://other.test', settings) == 'https://other.test'
    assert days.host_of('ns:', settings) == 'ns.example.org_8443'
    assert days.host_of('other.test', settings) == 'other.test'
    client = days.client_for('ns:', settings)
    assert client.cache_dir.endswith(os.path.join('ns_cache', 'ns.example.org_8443'))
    with pytest.raises(ValueError):
        days.host_of('ns:', {'nightscout': {}})


# ----------------------------------------------------------------------------- finality

def _touch(path, mtime_ms, payload='[]'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(payload)
    os.utime(path, (mtime_ms / 1000.0, mtime_ms / 1000.0))


def make_cache(root, day, mtime_ms, skip=()):
    """Write every file a replay of `day` consults, all with the given mtime; `skip` names 'coll/name' to leave out."""
    for cf in days.consulted_files(day):
        if f'{cf.coll}/{cf.name}' in skip:
            continue
        _touch(cf.path(root), mtime_ms, 'null' if cf.coll == 'latest' else '[]')


def test_consulted_files_cover_lookbacks():
    files = {(cf.coll, cf.name): cf.cutoff_ms for cf in days.consulted_files(DAY)}
    assert ('entries', '2026-09-05') in files and ('entries', DAY) in files       # 8 h look-back reaches the day before
    assert ('devicestatus', '2026-09-07') in files                                # 2 min upload lag reaches the day after
    assert ('devicestatus', '2026-09-07') in files and files[('devicestatus', '2026-09-07')] == END   # D's end, not D+1's
    assert files[('entries', '2026-09-05')] == START                              # the previous day's own end
    assert files[('profile', 'all')] == END
    assert ('latest', 'Profile_Switch_2026-09-05T16-00-00') in files
    assert files[('latest', 'Profile_Switch_2026-09-05T16-00-00')] == START - days.LOOKBACK_MS


def test_finality_final_partial_missing(tmp_path):
    root = str(tmp_path / 'host')
    make_cache(root, DAY, END + 2 * H)                                            # written two hours after the day: final
    f = days.finality(DAY, root)
    assert f.cached and f.final and f.complete and f.why() == ''

    make_cache(root, DAY, END + 30 * MIN)                                        # inside the settle hour: not final
    f = days.finality(DAY, root)
    assert f.cached and not f.final and f.complete
    assert f'devicestatus/{DAY}' in f.partial and 'devicestatus/2026-09-05' not in f.partial   # D-1 was final long ago
    assert 'not yet final' in f.why()

    make_cache(root, DAY, END + 2 * H)
    os.remove(os.path.join(root, 'devicestatus', '2026-09-07.json'))
    f = days.finality(DAY, root)
    assert f.cached and not f.final and not f.complete and f.missing == ['devicestatus/2026-09-07']
    assert 'missing devicestatus/2026-09-07' in f.why()

    assert not days.finality('2026-01-01', root).cached


def test_can_be_final():
    assert not days.can_be_final(DAY, now_ms=END + 59 * MIN)
    assert days.can_be_final(DAY, now_ms=END + 61 * MIN)


def test_cached_days_lists_devicestatus_files(tmp_path):
    root = str(tmp_path / 'host')
    _touch(os.path.join(root, 'devicestatus', '2026-09-02.json'), END)
    _touch(os.path.join(root, 'devicestatus', '2026-09-01.json'), END)
    _touch(os.path.join(root, 'devicestatus', 'junk.json'), END)
    assert days.cached_days(root) == ['2026-09-01', '2026-09-02']
    assert days.cached_days(str(tmp_path / 'nothing')) == []


# ----------------------------------------------------------------------------- gaps, character, confounders

class FakeProfiles:
    def __init__(self, tts=(), basal=0.5):
        self.tts = tts
        self._basal = basal

    def basal_at(self, ms):
        return self._basal

    def temp_target_at(self, ms):
        for a, b in self.tts:
            if a <= ms < b:
                return {'start': a, 'end': b}
        return None


class FakeIob:
    def __init__(self, temps):
        self.temps = temps

    def temp_rate(self, temp, ms):
        return temp['rate']


class FakeSource:
    def __init__(self, entries, treatments, temps=(), tts=(), skipped=None):
        self.entries = entries
        self.treatments = treatments
        self.iob = FakeIob(list(temps))
        self.profiles = FakeProfiles(tts)
        self.skipped = skipped or {}


def make_loop(ms, console=(), version='3.2.0', offset=2.0):
    return {'now_ms': ms, 'utcOffset': offset, 'console_error': list(console), 'autoISF_version': version}


def test_gaps_and_cgm_gaps():
    stamps = [START + i * 5 * MIN for i in range(12)]                             # 00:00 .. 00:55
    stamps += [START + 2 * H + i * 5 * MIN for i in range(276)]                   # 02:00 .. 24:00 minus a bit
    g = days.gaps(stamps, START, END, 15)
    assert len(g) == 1 and g[0][2] == 65 and g[0][0] == START + 55 * MIN
    assert days.gaps([], START, END, 15) == [(START, END, 1440)]
    entries = [{'date': s, 'sgv': 120.0} for s in stamps] + [{'date': START + H, 'sgv': 'bad'}, {'sgv': 100}]
    assert days.cgm_gaps(entries, START, END) == g


def test_character_record():
    entries = [{'date': START + i * 5 * MIN, 'sgv': v} for i, v in enumerate([60, 100, 200, 100, 100, 100, 100, 100, 100, 100])]
    treatments = [
        {'eventType': 'Correction Bolus', 'insulin': 0.5, 'type': 'SMB', 'created_at': '2026-09-06T06:00:00.000Z'},
        {'eventType': 'Meal Bolus', 'insulin': 4, 'type': 'NORMAL', 'created_at': '2026-09-06T12:00:00.000Z'},
        {'eventType': 'Correction Bolus', 'insulin': 1.0, 'created_at': '2026-09-05T23:59:00.000Z'},      # day before
        {'eventType': 'Correction Bolus', 'insulin': 1.0, 'created_at': '2026-09-06T13:00:00.000Z', 'isValid': False},
        {'eventType': 'Carb Correction', 'carbs': 30, 'created_at': '2026-09-06T12:00:00.000Z'},
        {'eventType': 'Temp Basal', 'absolute': 0, 'duration': 30, 'created_at': '2026-09-06T01:00:00.000Z', 'insulin': None},
        {'eventType': 'Bolus Wizard', 'insulin': 0.1, 'type': 'PRIMING', 'created_at': '2026-09-06T14:00:00.000Z'},
    ]
    temps = [{'start': START + H, 'end': START + H + 30 * MIN, 'rate': 0.0, 'abs': True}]        # 30 min zero temp
    tts = [(START + 10 * H, START + 11 * H)]
    src = FakeSource(entries, treatments, temps, tts, skipped={'duplicate upload': 10})
    loops = []
    for i in range(90):                                                                            # 06:00 .. 07:29
        console = ['SMB disabled; current target 5.5 has odd decimal'] if i < 45 else ['SMB enabled; current target 5.6 has even decimal']
        if 30 <= i < 42:
            console.append('Activity monitor: detected activity')
        loops.append(make_loop(START + 6 * H + i * MIN, console))
    ch = days.character(DAY, src, loops)
    assert ch['loops'] == 90
    assert ch['bolus_U'] == 4.5 and ch['smb_U'] == 0.5 and ch['carbs_g'] == 30.0
    assert ch['basal_U'] == pytest.approx(0.5 * 23.5, abs=0.01)                                    # 23.5 h at 0.5 U/h
    assert ch['tdd_U'] == pytest.approx(4.5 + 11.75, abs=0.01)
    assert ch['cgm_values'] == 10 and ch['bg_mean'] == 106.0 and ch['pct_gt180'] == 10.0 and ch['pct_lt70'] == 10.0
    assert ch['cgm_gap_min'] == 1395 and ch['cgm_gaps'] == 1                                     # 00:45 .. 24:00
    assert ch['tt_minutes'] == 60
    assert ch['tt_parity_smb_off_pct'] == 50.0
    assert ch['activity_min'] == 12
    assert ch['loop_gap_min'] == 991                                                               # 07:29 .. 24:00
    assert ch['duplicate_pct'] == 10.0
    assert ch['autoISF_versions'] == ['3.2.0']


def test_character_without_source_details():
    ch = days.character(DAY, object(), [])
    assert ch['loops'] == 0 and ch['tdd_U'] is None and ch['basal_U'] is None and ch['tt_minutes'] is None
    assert ch['bg_mean'] is None and ch['tt_parity_smb_off_pct'] is None and ch['duplicate_pct'] is None
    assert ch['cgm_gap_min'] == 1440


def test_confounders():
    settings = {'profile_history': {'changes': [
        {'when': '2026-09-06T20:23:00+02:00', 'parameter': 'autoISF_max', 'from': 1.3, 'to': 1.5},
        {'when': '2026-09-07T22:03:00+02:00', 'parameter': 'smb_max_range_extension', 'from': 1.15, 'to': 1.8},   # next day
        {'when': 'garbage', 'parameter': 'x', 'from': 0, 'to': 1},
        {'when': '2026-09-06T07:02:00+02:00', 'parameter': 'profile ISF (mmol/U)', 'from': 4.0, 'to': 3.7},
    ]}}
    entries = [{'date': START + i * 5 * MIN, 'sgv': 100} for i in range(288) if not (160 <= i < 175)]   # 13:15 .. 14:35 UTC
    treatments = [
        {'eventType': 'Profile Switch', 'percentage': 120, 'duration': 60, 'created_at': '2026-09-06T10:30:00.000Z'},
        {'eventType': 'Profile Switch', 'percentage': 100, 'timeshift': 0, 'created_at': '2026-09-06T11:30:00.000Z'},
        {'eventType': 'Temporary Target', 'duration': 180, 'created_at': '2026-09-06T08:00:00.000Z'},
        {'eventType': 'Temporary Target', 'duration': 30, 'created_at': '2026-09-06T09:00:00.000Z'},
    ]
    src = FakeSource(entries, treatments)
    loops = [make_loop(START + 6 * H + i * MIN, version='3.2.0' if i < 100 else '3.2.1') for i in range(200)]
    out = days.confounders(DAY, settings, src, loops)
    assert 'profile_history: autoISF_max 1.3->1.5 at 20h23 local' in out
    assert not any('smb_max_range_extension' in s for s in out)
    assert 'profile_history: profile ISF 4.0->3.7 at 07h02 local' in out          # unit dropped (lint: no glucose units)
    assert 'cgm gap 80 min 15h15-16h35 local' in out                                   # local = UTC + 2 h
    assert 'phone not looping for 360 min 02h00-08h00 local' in out                    # loops 06:00 .. 09:19 UTC
    assert 'phone not looping for 881 min 11h19-02h00 local' in out
    assert 'autoISF version 3.2.0->3.2.1 at 09h40 local (smoother may differ)' in out
    assert 'profile switch 120 % at 12h30 local (60 min)' in out
    assert not any('100 %' in s for s in out)
    assert 'temp target 180 min from 10h00 local' in out
    assert not any('30 min from' in s for s in out)
    for s in out:                                                                 # privacy: no glucose value in a badge
        assert 'mg' not in s and 'mmol' not in s
    from hindcast import lint
    for s in out:                                                                 # badges reach the page and issues
        lint.lint_text(s, kind='line')
        lint.lint_text(s, kind='issue')


def test_history_changes_in_and_summarize():
    settings = {'profile_history': {'changes': [{'when': '2026-09-06T00:00:00Z', 'parameter': 'a', 'from': 1, 'to': 2}]}}
    assert len(days.history_changes_in(settings, START, END)) == 1
    assert days.history_changes_in(settings, START + 1, END) == []
    assert days.history_changes_in({}, START, END) == []
    meta = {'character': {'loops': 1311, 'tdd_U': 30.5, 'carbs_g': 120.0, 'cgm_gap_min': 71}, 'confounders': ['x']}
    assert days.summarize(meta) == '1311 loops, TDD 30.5 U, 120.0 g, cgm gap 71 min, 1 badge'
    assert days.summarize({}) == '? loops'


def test_loop_intervals_capped():
    loops = [{'now_ms': START}, {'now_ms': START + MIN}, {'now_ms': START + 20 * MIN}]
    assert days.loop_intervals_min(loops) == [1.0, 5.0, 5.0]


# ----------------------------------------------------------------------------- CGM gaps from the cache files

def _write_entries(root, day, stamps):
    path = days.entries_file(root, day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump([{'date': ms, 'sgv': 110, 'type': 'sgv'} for ms in stamps]
                  + [{'date': stamps[0] + 1, 'type': 'cal'}] if stamps else [], fh)


def _every_5(a, b):
    return list(range(a, b, 5 * MIN))


def test_cgm_gap_spanning_midnight_is_clipped_at_midnight(tmp_path):
    """Previous day's last sgv at 18:10Z, the day's first at 01:52Z: the day accounts only for its own part
    (00:00-01:52 = 112 min), whether the previous day is cached or not; the previous day gets its 18:10-24:00 part."""
    root = str(tmp_path / 'host')
    prev_start, _ = day_bounds('2026-09-05')
    _write_entries(root, '2026-09-05', _every_5(prev_start, prev_start + 18 * H + 15 * MIN))      # .. 18:10
    _write_entries(root, DAY, _every_5(START + H + 52 * MIN, END))                                # 01:52 ..
    cd = days.cgm_day(DAY, cache_root=root)
    assert cd['cgm_gap_min'] == 112 and cd['cgm_gaps'] == 1 and cd['note'] is None                # 00:00 .. 01:52
    assert cd['gaps'][0][0] == START
    loops = [make_loop(ms, offset=0.0) for ms in _every_5(START + 2 * H, END)]
    ch = days.character(DAY, object(), loops, cache_root=root)
    assert ch['cgm_gap_min'] == 112 and ch['cgm_values'] == len(_every_5(START + H + 52 * MIN, END))
    assert ch['cgm_note'] is None and ch['loop_gap_min'] == 120
    out = days.confounders(DAY, {}, object(), loops, cache_root=root)
    assert 'cgm gap 112 min 00h00-01h52 local' in out
    assert not any('previous day' in b for b in out)
    assert 'phone not looping for 120 min 00h00-02h00 local' in out
    assert days.quarantine_reasons(DAY, meta={'character': ch}) == []                             # 112 <= 120

    prev = days.cgm_day('2026-09-05', cache_root=root)                                            # 18:10 .. 24:00
    assert prev['cgm_gap_min'] == 350 and prev['gaps'][-1][1] == START

    os.remove(days.entries_file(root, '2026-09-05'))                                              # previous day not cached
    assert days.cgm_day(DAY, cache_root=root) == cd


def test_cgm_day_ignores_the_source_look_back():
    """Without cache_root the source's entries carry the 8 h look-back: entries before midnight never shorten
    or lengthen the day's first gap."""
    look_back = [{'date': t, 'sgv': 120} for t in _every_5(START - 8 * H, START)]
    day = [{'date': t, 'sgv': 120} for t in _every_5(START + 30 * MIN, END)]
    cd = days.cgm_day(DAY, src=FakeSource(look_back + day, []))
    assert cd['gaps'] == [(START, START + 30 * MIN, 30)] and cd['cgm_gap_min'] == 30
    assert len(cd['cgm']) == len(day)


def test_gap_at_the_day_end():
    """No entry in the last hour: a gap from the last entry to 24:00Z, the next day is not consulted."""
    stamps = _every_5(START, END - H + 5 * MIN)                                                   # last at 23:00
    assert days.gaps(stamps, START, END, 15) == [(END - H, END, 60)]
    assert days.gaps(stamps + [END + MIN], START, END, 15) == [(END - H, END, 60)]               # outside: ignored


def test_loop_gap_alone_never_quarantines(tmp_path):
    """CGM every 5 min, the phone not looping for 10 h: cgm_gap_min 0 (no gap >= 15 min), loop_gap_min 600, a badge
    but no quarantine reason."""
    root = str(tmp_path / 'host')
    prev_start, _ = day_bounds('2026-09-05')
    _write_entries(root, '2026-09-05', _every_5(prev_start, START))
    _write_entries(root, DAY, _every_5(START, END))
    loops = [make_loop(ms) for ms in _every_5(START, START + 6 * H) + _every_5(START + 16 * H, END)]
    ch = days.character(DAY, object(), loops, cache_root=root)
    assert ch['cgm_gap_min'] == 0 and ch['cgm_gaps'] == 0 and ch['cgm_values'] == 288
    assert ch['loop_gap_min'] == 605                                                              # 05:55 .. 16:00
    out = days.confounders(DAY, {}, object(), loops, cache_root=root)
    assert out == ['phone not looping for 605 min 07h55-18h00 local']
    from hindcast import lint
    for badge in out:
        lint.lint_text(badge, kind='line')
        lint.lint_text(badge, kind='issue')
    assert days.quarantine_reasons(DAY, meta={'character': ch, 'confounders': out}) == []


def test_day_without_an_entries_file(tmp_path):
    root = str(tmp_path / 'host')
    ch = days.character(DAY, object(), [make_loop(START)], cache_root=root)
    assert ch['cgm_gap_min'] is None and ch['cgm_gaps'] is None and ch['cgm_values'] is None
    assert ch['cgm_note'] == days.NO_CGM_FILE and ch['bg_mean'] is None
    assert days.NO_CGM_FILE in days.confounders(DAY, {}, object(), [make_loop(START)], cache_root=root)
    assert days.quarantine_reasons(DAY, meta={'character': ch}) == []
