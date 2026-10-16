"""
days.py  -  Hindcast day inventory: UTC day specs, cache finality, day character, confounder badges and the day
            quarantine (H-101, H-104, H-204)

Days are the unit of everything in Hindcast (DESIGN.md section 5.4); this module answers the day-level questions
the freeze, the warm-up and the quarantine ask, from the Nightscout day cache, `ns_settings.json`, the frozen meta
and the base trial's metrics alone. It opens no socket: the only object it builds from `ns_client` is the client's
path layout.

    from hindcast import days
    days.parse_days('2026-08-30..2026-09-01')          -> ['2026-08-30', '2026-08-31', '2026-09-01']
    f = days.finality('2026-09-06', days.cache_dir())  -> Finality(cached=True, final=True, missing=[], partial=[])
    days.character(day, src, loops)                    -> the day character record of <day>.meta.json
    days.confounders(day, settings, src, loops)        -> ['profile_history: autoISF_max 1.3->1.5 at 20:23 local', ...]

Finality (the rule `ns_client.NightscoutClient._load_day` applies, extended to the files one replay day consults).
A cached day file is final once it was WRITTEN more than `SETTLE_MS` (one hour) after the UTC day it holds was
over. Replaying day D reads more than D's own files: treatments and CGM entries from `LOOKBACK_MS` (8 h) before
the day (file D-1), devicestatus from 2 h before (D-1) to 2 min after the day (the head of file D+1, for the upload
lag of the last loops), the two `latest/` look-ups for the running Profile Switch / Temporary Target, and the
profile store. For each file the records D needs are complete once the file was written an hour after
min(end of the file's own day, end of D): that is the cutoff `finality()` checks per file. `can_be_final(day)`
says whether a fetch made now would produce final files (now > end of D + 1 h): `hc warm` refuses other days
unless `--allow-partial`.

Character record (numbers of the day, written into <day>.meta.json, shown as the day strip on the page; mg/dl):
    loops, tdd_U (= bolus_U + basal_U), bolus_U, smb_U, basal_U, carbs_g, cgm_values, bg_mean, pct_gt180, pct_lt70,
    tt_minutes, tt_parity_smb_off_pct, cgm_gap_min, cgm_gaps (count >= GAP_MIN), loop_gap_min, activity_min,
    duplicate_pct, autoISF_versions
`tdd_U` is insulin the pump delivered as recorded in Nightscout (boluses + temp basals + scheduled basal where no
temp ran), not the phone's own TDD statistic. The character describes the day; it is never a score and never
inferred into chips - day chips (`workday`, `sport`) are typed by the user in study.json (DESIGN.md section 4.5).

Confounders (strings for the badges; local time of the phone from the records' utcOffset):
    settings changes from ns_settings.json profile_history inside the day, CGM gaps >= GAP_MIN minutes, loop gaps
    (no loop run) >= LOOP_GAP_MIN minutes, autoISF version changes inside the day (H-712: the smoother changed with
    3.2.1), Profile Switches with a percentage or time shift, Temporary Targets longer than LONG_TT_MIN.
They never quote glucose values (the strings may travel into an issue body; DESIGN.md section 9 privacy).

Quarantine (H-204, DESIGN.md section 5.4 phase 0). A day is quarantined - listed with its reasons on the page and in
status.json, excluded from scoring, never hidden - when any of these holds:
    1. the base replay's `agreement_pct` is below `agreement_pct_min` (94 %: the study would measure the emulator,
       not the settings),
    2. the longest CGM gap of the day exceeds `cgm_gap_max_min` (120 min; from the frozen meta's character record,
       or from raw entries when given),
    3. ns_settings.json `profile_history` records a change INSIDE the day of a key the study searches (the phone's
       own value of that key is not one value on that day),
    4. the loop counts of the day's trials differ across variants (state leaked between in-process replays, or a
       failed run; DESIGN.md section 5.1 makes this a hard stop as well).
Thresholds come from study.json (`objective.quarantine`, contracts.Objective) with QUARANTINE_THRESHOLDS as the
defaults. `quarantine_reasons()` judges one day from the pieces, `quarantine_from_trials()` collects the pieces for a
study from its trial rows and frozen metas, `apply_quarantine()` sets the StudyDay roles, `quarantine_listing()`
renders the list. Every reason string passes hindcast.lint for the kinds 'line' and 'issue' (no glucose values, no
loop counts, no insulin amounts; tests/test_quarantine.py checks it).
"""
import datetime
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field

from ns_client import NightscoutClient, day_bounds, parse_iso, to_iso, utc_day
import ns_source

SETTLE_MS = NightscoutClient.SETTLE_MS                 # one hour: late uploads after a day / window end
LOOKBACK_MS = ns_source.LOOKBACK_MS                    # treatments and CGM before the first loop (8 h)
DS_LOOKBACK_MS = ns_source.DS_LOOKBACK_MS              # devicestatus before the first loop (2 h)
UPLOAD_LAG_MS = ns_source.UPLOAD_LAG_MS                # devicestatus after the window end (2 min)
DAY_MS = 24 * 60 * 60 * 1000
MIN_MS = 60 * 1000

GAP_MIN = 15                                           # a CGM gap this long or longer is a confounder
LOOP_GAP_MIN = 30                                      # no loop run for this long: phone off, pump offline
LONG_TT_MIN = 120                                      # a Temporary Target longer than this is a confounder
LOW_MGDL = 70
HIGH_MGDL = 180
LOOP_DT_CAP_MS = 5 * MIN_MS                            # a loop counts for at most 5 min of the day

_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_re_smb_parity_off = re.compile(r'^SMB disabled; current target ')
_re_autoisf_version = re.compile(r'^start AutoISF\s+([0-9.]+)')


# ----------------------------------------------------------------------------- day specs

def is_day(text):
    """True for a 'YYYY-MM-DD' string that is a real date."""
    if not isinstance(text, str) or not _DAY_RE.match(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def day_add(day, n):
    """'YYYY-MM-DD' shifted by n days."""
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=n)).isoformat()


def today_utc(now_ms=None):
    import time
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    return utc_day(ms)


def parse_days(spec, now_ms=None):
    """A day spec -> ascending list of UTC days, duplicates removed.

    Accepted: 'YYYY-MM-DD', 'YYYY-MM-DD..YYYY-MM-DD' (inclusive), 'yesterday', 'today', a comma-separated list of
    those, or a list/tuple of such specs. ValueError names the offending token."""
    if isinstance(spec, (list, tuple)):
        out = []
        for s in spec:
            out += parse_days(s, now_ms)
        return sorted(set(out))
    text = str(spec or '').strip()
    if not text:
        raise ValueError('empty day spec')
    if ',' in text:
        return parse_days([t for t in text.split(',') if t.strip()], now_ms)
    aliases = {'today': today_utc(now_ms), 'yesterday': day_add(today_utc(now_ms), -1)}
    if '..' in text:
        a, b = (t.strip() for t in text.split('..', 1))
        a, b = aliases.get(a, a), aliases.get(b, b)
        if not (is_day(a) and is_day(b)):
            raise ValueError(f'day range "{text}": expected YYYY-MM-DD..YYYY-MM-DD')
        if b < a:
            raise ValueError(f'day range "{text}": end before start')
        if (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days > 400:
            raise ValueError(f'day range "{text}": more than 400 days')
        out, d = [], a
        while d <= b:
            out.append(d)
            d = day_add(d, 1)
        return out
    text = aliases.get(text, text)
    if not is_day(text):
        raise ValueError(f'"{text}" is not a UTC day (YYYY-MM-DD)')
    return [text]


def day_window(day):
    """'YYYY-MM-DD' -> (start_iso, end_iso) of the UTC day in the emulator's label form ('...T00:00:00.000Z')."""
    start, end = day_bounds(day)
    return to_iso(start), to_iso(end)


def days_of_window(start_ms, end_ms):
    """The UTC days a half-open [start_ms, end_ms) window touches, ascending."""
    if end_ms <= start_ms:
        return []
    out, d, last = [], utc_day(start_ms), utc_day(end_ms - 1)
    while True:
        out.append(d)
        if d >= last:
            return out
        d = day_add(d, 1)


# ----------------------------------------------------------------------------- settings, host, cache paths

def load_settings(path=None):
    """ns_settings.json as a dict (ns_source.load_settings)."""
    return ns_source.load_settings(path)


def resolve_url(url='ns:', settings=None):
    """'ns:' / '' -> nightscout.url of the settings; anything else is returned as given."""
    if url in ('ns', 'ns:', '', None) or str(url).startswith('ns:'):
        settings = settings if settings is not None else load_settings()
        url = (settings.get('nightscout') or {}).get('url') or ''
    return url


def client_for(url='ns:', settings=None, verbose=False):
    """A NightscoutClient for `url` with the token / api secret / cache dir of ns_settings.json when the url is the
    settings' own (the rule of ns_client._client_from_settings). No request is made."""
    settings = settings if settings is not None else load_settings()
    cfg = settings.get('nightscout') or {}
    url = resolve_url(url, settings)
    if not url:
        raise ValueError('no Nightscout url: pass one or set nightscout.url in ns_settings.json')
    own = url.rstrip('/') == str(cfg.get('url') or '').rstrip('/')
    return NightscoutClient(url, cache_dir=cfg.get('cache_dir') if own else None,
                            token=cfg.get('token') if own else None,
                            api_secret_sha1=cfg.get('api_secret_sha1') if own else None, verbose=verbose)


def host_of(url='ns:', settings=None):
    """The host directory name ns_client uses under the cache ('ns.vivikusi.ch'; ':' of a port becomes '_')."""
    url = resolve_url(url, settings)
    if not url:
        raise ValueError('no Nightscout url: pass one or set nightscout.url in ns_settings.json')
    base = url.rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    return urllib.parse.urlparse(base).netloc.replace(':', '_')


def cache_dir(url='ns:', settings=None):
    """<cache base>/<host>: the directory holding entries/, treatments/, devicestatus/, latest/, profile/."""
    return client_for(url, settings).cache_dir


def cached_days(cache_root):
    """Ascending UTC days with a devicestatus day file under <cache_dir>/devicestatus/."""
    d = os.path.join(cache_root, 'devicestatus')
    if not os.path.isdir(d):
        return []
    return sorted(n[:-5] for n in os.listdir(d) if n.endswith('.json') and is_day(n[:-5]))


# ----------------------------------------------------------------------------- finality

@dataclass
class ConsultedFile:
    """One cache file a replay of a day reads, with the instant after which its records for that day are complete."""
    coll: str
    name: str                                          # file name without .json ('2026-09-06', 'all', 'Profile_Switch_...')
    cutoff_ms: int                                     # final once mtime > cutoff_ms + SETTLE_MS

    def path(self, cache_root):
        return os.path.join(cache_root, self.coll, self.name + '.json')


def latest_name(event_type, before_ms):
    """File name (without .json) ns_client.latest_treatment caches its answer under."""
    slug = re.sub(r'[^A-Za-z0-9]+', '_', event_type).strip('_')
    return f'{slug}_{to_iso(before_ms)[:19].replace(":", "-")}'


def consulted_files(day):
    """Every cache file `ns_source.NightscoutSource.load()` reads for a replay of the whole UTC `day`."""
    start, end = day_bounds(day)
    out = []
    for coll, lo, hi in (('entries', start - LOOKBACK_MS, end), ('treatments', start - LOOKBACK_MS, end),
                         ('devicestatus', start - DS_LOOKBACK_MS, end + UPLOAD_LAG_MS)):
        for file_day in days_of_window(lo, hi):
            file_end = day_bounds(file_day)[1]
            out.append(ConsultedFile(coll, file_day, min(file_end, end)))
    before = start - LOOKBACK_MS
    for et in ('Profile Switch', 'Temporary Target'):
        out.append(ConsultedFile('latest', latest_name(et, before), before))
    out.append(ConsultedFile('profile', 'all', end))
    return out


def file_final(path, cutoff_ms):
    """True when the file exists and was written more than SETTLE_MS after cutoff_ms."""
    try:
        return os.path.getmtime(path) * 1000 > cutoff_ms + SETTLE_MS
    except OSError:
        return False


def can_be_final(day, now_ms=None):
    """True when a fetch made now yields final files for `day` (now > end of day + 1 h)."""
    import time
    now = int(time.time() * 1000) if now_ms is None else now_ms
    return now > day_bounds(day)[1] + SETTLE_MS


@dataclass
class Finality:
    day: str
    cached: bool                                       # the day's own three day files exist
    final: bool                                        # every consulted file is final for this day
    missing: list = field(default_factory=list)        # 'coll/name' of consulted files that do not exist
    partial: list = field(default_factory=list)        # 'coll/name' of files that exist but are not yet final

    @property
    def complete(self):
        """Every consulted file exists (final or not): an offline freeze can run."""
        return not self.missing

    def why(self):
        """One line for a refusal message, '' when final."""
        if self.final:
            return ''
        parts = []
        if self.missing:
            parts.append('missing ' + ', '.join(self.missing))
        if self.partial:
            parts.append('not yet final ' + ', '.join(self.partial))
        return '; '.join(parts)


def finality(day, cache_root):
    """Finality of `day` from the mtimes of the cache files its replay consults (no request, no clock needed)."""
    missing, partial = [], []
    own = {('entries', day), ('treatments', day), ('devicestatus', day)}
    cached = all(os.path.isfile(os.path.join(cache_root, c, n + '.json')) for c, n in own)
    for cf in consulted_files(day):
        p = cf.path(cache_root)
        if not os.path.isfile(p):
            missing.append(f'{cf.coll}/{cf.name}')
        elif not file_final(p, cf.cutoff_ms):
            partial.append(f'{cf.coll}/{cf.name}')
    return Finality(day, cached, not missing and not partial, missing, partial)


# ----------------------------------------------------------------------------- local time helpers

def utc_offset_hours(loops):
    """The phone's UTC offset in hours from the first loop that carries one (0.0 when none)."""
    for loop in loops or []:
        off = loop.get('utcOffset')
        if isinstance(off, (int, float)):
            return float(off)
    return 0.0


def local_hhmm(ms, offset_h):
    dt = datetime.datetime.fromtimestamp(ms / 1000.0 + offset_h * 3600, tz=datetime.timezone.utc)
    return dt.strftime('%H:%M')


# ----------------------------------------------------------------------------- gaps

def _entry_ms(rec):
    v = rec.get('date')
    if isinstance(v, (int, float)):
        return int(v)
    if rec.get('dateString'):
        try:
            return parse_iso(rec['dateString'])
        except ValueError:
            return None
    return None


def entries_in(entries, start_ms, end_ms):
    """(ms, sgv) of the CGM entries with a numeric sgv inside [start, end), ascending."""
    out = []
    for rec in entries or []:
        if not isinstance(rec, dict):
            continue
        ms = _entry_ms(rec)
        sgv = rec.get('sgv')
        if ms is None or not isinstance(sgv, (int, float)) or not (start_ms <= ms < end_ms):
            continue
        out.append((ms, float(sgv)))
    out.sort()
    return out


def gaps(stamps_ms, start_ms, end_ms, min_gap_min):
    """[(gap_start_ms, gap_end_ms, minutes)] of the pauses >= min_gap_min between consecutive stamps inside
    [start, end), the day edges included (no stamp in the first hour = a gap from start)."""
    pts = sorted(s for s in stamps_ms if start_ms <= s < end_ms)
    if not pts:
        return [(start_ms, end_ms, round((end_ms - start_ms) / MIN_MS))]
    out = []
    prev = start_ms
    for s in pts + [end_ms]:
        minutes = (s - prev) / MIN_MS
        if minutes >= min_gap_min:
            out.append((prev, s, round(minutes)))
        prev = s
    return out


def cgm_gaps(entries, start_ms, end_ms, min_gap_min=GAP_MIN):
    """CGM gaps of the window from the raw entries."""
    return gaps([ms for ms, _ in entries_in(entries, start_ms, end_ms)], start_ms, end_ms, min_gap_min)


# ----------------------------------------------------------------------------- character

def _rate_minutes(src, start_ms, end_ms):
    """basal units delivered over the window: temp rate where a temp ran, the profile basal elsewhere (1-min grid)."""
    iob = getattr(src, 'iob', None)
    profiles = getattr(src, 'profiles', None)
    if iob is None or profiles is None:
        return None
    temps = sorted(getattr(iob, 'temps', []) or [], key=lambda t: t['start'])
    units = 0.0
    i = 0
    ms = start_ms
    while ms < end_ms:
        while i < len(temps) and temps[i]['end'] <= ms:
            i += 1
        rate = None
        if i < len(temps) and temps[i]['start'] <= ms < temps[i]['end']:
            try:
                rate = float(iob.temp_rate(temps[i], ms))
            except Exception:
                rate = None
        if rate is None:
            try:
                rate = float(profiles.basal_at(ms))
            except Exception:
                rate = 0.0
        units += rate / 60.0
        ms += MIN_MS
    return units


def _tt_minutes(src, start_ms, end_ms):
    """minutes of the window a Temporary Target was running (ProfileBuilder.temp_target_at on a 1-min grid)."""
    profiles = getattr(src, 'profiles', None)
    if profiles is None or not hasattr(profiles, 'temp_target_at'):
        return None
    minutes = 0
    ms = start_ms
    while ms < end_ms:
        try:
            if profiles.temp_target_at(ms):
                minutes += 1
        except Exception:
            pass
        ms += MIN_MS
    return minutes


def _treatment_ms(t):
    if isinstance(t.get('created_at'), str):
        try:
            return parse_iso(t['created_at'])
        except ValueError:
            return None
    if isinstance(t.get('date'), (int, float)):
        return int(t['date'])
    if isinstance(t.get('mills'), (int, float)):
        return int(t['mills'])
    return None


def loop_intervals_min(loops):
    """minutes each loop stands for: the distance to the next loop, capped at LOOP_DT_CAP_MS."""
    stamps = [loop.get('now_ms') for loop in loops]
    out = []
    for i, ms in enumerate(stamps):
        nxt = stamps[i + 1] if i + 1 < len(stamps) else None
        dt = LOOP_DT_CAP_MS if nxt is None or ms is None else min(max(nxt - ms, 0), LOOP_DT_CAP_MS)
        out.append(dt / MIN_MS)
    return out


def activity_state(console_error):
    """'activity' | 'partial_activity' | other state names of ns_profile, None when no monitor line is present."""
    for line in console_error or []:
        s = str(line)
        if 'Activity monitor' not in s and 'activity' not in s.lower():
            continue
        if 'detected activity' in s:
            return 'activity'
        if 'partial activity' in s:
            return 'partial_activity'
        if 'partial inactivity' in s:
            return 'partial_inactivity'
        if 'detected inactivity' in s:
            return 'inactivity'
        if 'disabled' in s:
            return 'disabled'
        if s.startswith('Activity monitor'):
            return 'other'
    return None


def character(day, src, loops):
    """The day character record (see the module docstring) from the loaded source and its yielded loops.

    `src` is the ns_source.NightscoutSource after load() (entries, treatments, iob, profiles are read); `loops` the
    list its loops() yielded for the day. Values are rounded for the meta file; None where the source lacks the
    data (a FrozenSource, say)."""
    start, end = day_bounds(day)
    rec = {'loops': len(loops)}
    treatments = getattr(src, 'treatments', None) or []
    entries = getattr(src, 'entries', None) or []

    bolus = smb = 0.0
    carbs = 0.0
    for t in treatments:
        if not isinstance(t, dict) or t.get('isValid') is False:
            continue
        ms = _treatment_ms(t)
        if ms is None or not (start <= ms < end):
            continue
        ins = t.get('insulin')
        et = str(t.get('eventType') or '')
        if isinstance(ins, (int, float)) and ins > 0 and et != 'Temp Basal' and str(t.get('type', '')).upper() != 'PRIMING':
            bolus += float(ins)
            if str(t.get('type', '')).upper() == 'SMB' or et == 'SMB':
                smb += float(ins)
        c = t.get('carbs')
        if isinstance(c, (int, float)) and c > 0:
            carbs += float(c)
    basal = _rate_minutes(src, start, end)
    rec['bolus_U'] = round(bolus, 2)
    rec['smb_U'] = round(smb, 2)
    rec['basal_U'] = None if basal is None else round(basal, 2)
    rec['tdd_U'] = None if basal is None else round(bolus + basal, 2)
    rec['carbs_g'] = round(carbs, 1)

    cgm = entries_in(entries, start, end)
    rec['cgm_values'] = len(cgm)
    if cgm:
        vals = [v for _, v in cgm]
        rec['bg_mean'] = round(sum(vals) / len(vals), 1)
        rec['pct_gt180'] = round(100.0 * sum(1 for v in vals if v > HIGH_MGDL) / len(vals), 1)
        rec['pct_lt70'] = round(100.0 * sum(1 for v in vals if v < LOW_MGDL) / len(vals), 1)
    else:
        rec['bg_mean'] = rec['pct_gt180'] = rec['pct_lt70'] = None
    g = gaps([ms for ms, _ in cgm], start, end, GAP_MIN)
    rec['cgm_gap_min'] = max([m for _, _, m in g], default=0)
    rec['cgm_gaps'] = len(g)

    rec['tt_minutes'] = _tt_minutes(src, start, end)
    n = len(loops)
    parity_off = sum(1 for loop in loops if any(_re_smb_parity_off.match(str(l)) for l in loop.get('console_error') or []))
    rec['tt_parity_smb_off_pct'] = round(100.0 * parity_off / n, 1) if n else None

    lg = gaps([loop['now_ms'] for loop in loops if loop.get('now_ms') is not None], start, end, LOOP_GAP_MIN)
    rec['loop_gap_min'] = max([m for _, _, m in lg], default=0)

    act = 0.0
    for loop, dt in zip(loops, loop_intervals_min(loops)):
        if activity_state(loop.get('console_error')) in ('activity', 'partial_activity'):
            act += dt
    rec['activity_min'] = round(act)

    dropped = getattr(src, 'skipped', None) or {}
    dup = dropped.get('duplicate upload', 0)
    rec['duplicate_pct'] = round(100.0 * dup / (n + dup), 1) if (n + dup) else None
    rec['autoISF_versions'] = sorted({loop['autoISF_version'] for loop in loops if loop.get('autoISF_version')})
    return rec


# ----------------------------------------------------------------------------- confounders

def history_changes_in(settings, start_ms, end_ms):
    """[(when_ms, parameter, from, to)] of profile_history.changes with `when` inside [start, end)."""
    out = []
    for ch in ((settings or {}).get('profile_history') or {}).get('changes', []) or []:
        try:
            when = parse_iso(str(ch['when']))
        except (KeyError, ValueError, TypeError):
            continue
        if start_ms <= when < end_ms:
            out.append((when, str(ch.get('parameter')), ch.get('from'), ch.get('to')))
    out.sort(key=lambda x: x[0])
    return out


def _fmt(v):
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v == int(v) and abs(v) < 1e6:
        return str(v)
    return str(v)


def confounders(day, settings, src, loops, min_gap_min=GAP_MIN):
    """Badge strings for the day (module docstring); the empty list when nothing stands out."""
    start, end = day_bounds(day)
    off = utc_offset_hours(loops)
    out = []
    for when, key, old, new in history_changes_in(settings, start, end):
        out.append(f'profile_history: {key} {_fmt(old)}->{_fmt(new)} at {local_hhmm(when, off)} local')
    entries = getattr(src, 'entries', None) or []
    for a, b, minutes in cgm_gaps(entries, start, end, min_gap_min):
        out.append(f'cgm gap {minutes} min {local_hhmm(a, off)}-{local_hhmm(b, off)}')
    for a, b, minutes in gaps([l['now_ms'] for l in loops if l.get('now_ms') is not None], start, end, LOOP_GAP_MIN):
        out.append(f'loop gap {minutes} min {local_hhmm(a, off)}-{local_hhmm(b, off)}')
    last_v = None
    for loop in loops:
        v = loop.get('autoISF_version')
        if v and last_v and v != last_v:
            out.append(f'autoISF version {last_v}->{v} at {local_hhmm(loop["now_ms"], off)} local (smoother may differ)')
        if v:
            last_v = v
    for t in getattr(src, 'treatments', None) or []:
        if not isinstance(t, dict) or t.get('isValid') is False:
            continue
        ms = _treatment_ms(t)
        if ms is None or not (start <= ms < end):
            continue
        et = t.get('eventType')
        if et == 'Profile Switch':
            pct = t.get('percentage', 100)
            shift = t.get('timeshift', 0) or 0
            dur = t.get('duration') or 0
            if (isinstance(pct, (int, float)) and pct != 100) or shift:
                out.append(f'profile switch {int(pct)} %{f" shift {shift} h" if shift else ""} at {local_hhmm(ms, off)} local'
                           f'{f" ({int(dur)} min)" if dur else ""}')
        elif et == 'Temporary Target':
            dur = t.get('duration') or 0
            if isinstance(dur, (int, float)) and dur >= LONG_TT_MIN:
                out.append(f'temp target {int(dur)} min from {local_hhmm(ms, off)} local')
    return out


# ----------------------------------------------------------------------------- misc

def summarize(meta):
    """One line for `hc freeze --list`: loops, TDD, carbs, gaps, badges."""
    ch = (meta or {}).get('character') or {}
    bits = [f"{ch.get('loops', '?')} loops"]
    if ch.get('tdd_U') is not None:
        bits.append(f"TDD {ch['tdd_U']} U")
    if ch.get('carbs_g') is not None:
        bits.append(f"{ch['carbs_g']} g")
    if ch.get('cgm_gap_min'):
        bits.append(f"cgm gap {ch['cgm_gap_min']} min")
    conf = (meta or {}).get('confounders') or []
    if conf:
        bits.append(f'{len(conf)} badge{"s" if len(conf) != 1 else ""}')
    return ', '.join(bits)


# ----------------------------------------------------------------------------- quarantine (H-204)

QUARANTINE_THRESHOLDS = {'agreement_pct_min': 94.0, 'cgm_gap_max_min': 120.0}
QUARANTINE_ROLE = 'quarantined'


def quarantine_thresholds(study=None, overrides=None):
    """QUARANTINE_THRESHOLDS updated from study.objective.quarantine (study.json) and `overrides`; unknown keys and
    non-numbers are ignored."""
    out = dict(QUARANTINE_THRESHOLDS)
    sources = []
    obj = getattr(study, 'objective', None) if study is not None else None
    if obj is not None:
        q = getattr(obj, 'quarantine', None)
        if q is None and isinstance(obj, dict):
            q = obj.get('quarantine')
        sources.append(q)
    elif isinstance(study, dict):
        sources.append(((study.get('objective') or {}).get('quarantine')))
    sources.append(overrides)
    for src in sources:
        for k, v in (src or {}).items():
            if k in out and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
    return out


def searched_items(searched_keys):
    """{'autoISF_max', ...}: the <item> part of '<array> <item>' keys (a bare item passes as itself)."""
    out = set()
    for name in searched_keys or []:
        parts = str(name).split()
        if parts:
            out.add(parts[-1])
    return out


def history_hits(settings, day, searched_keys):
    """profile_history changes inside `day` whose parameter is a searched key: [(when_ms, parameter, from, to)]. The
    parameter text of a history entry is matched on its last word ('autoISF_max'; 'profile ISF (mmol/U)' never
    matches a searchable key)."""
    items = searched_items(searched_keys)
    if not items:
        return []
    start, end = day_bounds(day)
    out = []
    for when, key, old, new in history_changes_in(settings, start, end):
        words = str(key).replace('(', ' ').replace(')', ' ').split()
        if key in items or (words and words[-1] in items) or (words and words[0] in items):
            out.append((when, key, old, new))
    return out


def _fmt_minutes(v):
    return str(int(round(v))) if isinstance(v, (int, float)) else str(v)


def quarantine_reasons(day, settings=None, searched_keys=(), base_metrics=None, meta=None, entries=None,
                       loop_counts=None, thresholds=None):
    """The reasons `day` is quarantined ([] = it takes part in scoring). Pieces, each optional:
    `base_metrics` the base trial's metrics dict (agreement_pct), `meta` the frozen meta (character.cgm_gap_min) or
    `entries` raw CGM entries (the gap is measured here), `settings` + `searched_keys` for the profile_history rule,
    `loop_counts` {variant: loops} or a list of loop counts of the day's trials. Strings are lint-clean for the kinds
    'line' and 'issue'."""
    thr = quarantine_thresholds(None, thresholds)
    out = []
    if isinstance(base_metrics, dict):
        pct = base_metrics.get('agreement_pct')
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and pct < thr['agreement_pct_min']:
            out.append(f'base agreement {pct:.1f} % (threshold {thr["agreement_pct_min"]:g} %): the replay would '
                       f'measure the emulator, not the settings')
    gap = None
    if entries is not None:
        start, end = day_bounds(day)
        gap = max([m for _, _, m in cgm_gaps(entries, start, end)], default=0)
    elif isinstance(meta, dict):
        gap = ((meta.get('character') or {}).get('cgm_gap_min'))
    if isinstance(gap, (int, float)) and not isinstance(gap, bool) and gap > thr['cgm_gap_max_min']:
        out.append(f'CGM gap {_fmt_minutes(gap)} min (threshold {thr["cgm_gap_max_min"]:g} min)')
    for when, key, old, new in history_hits(settings, day, searched_keys):
        out.append(f'profile_history: {key} changed {_fmt(old)}->{_fmt(new)} inside the day and {key} is a searched key')
    counts = loop_counts.values() if isinstance(loop_counts, dict) else (loop_counts or [])
    counts = {int(c) for c in counts if isinstance(c, (int, float)) and not isinstance(c, bool)}
    if len(counts) > 1:
        out.append('loop counts differ across the variants of this day (state leaked between replays, or a failed run)')
    return out


def _is_base_row(row):
    status = getattr(row, 'status', None)
    control = getattr(row, 'control', None)
    params = getattr(row, 'params', None)
    return status == 'done' and (control == 'base' or not params)


def quarantine_from_trials(study, rows, metas=None, settings=None, thresholds=None, days_=None):
    """{day: [reasons]} for the study's days from its trial rows (contracts.TrialRow: base metrics, loop counts per
    variant; only the latest row per key, as trials.jsonl is append-only), the frozen metas {day: meta} and
    ns_settings.json; days without reasons map to []. `days_` limits the days judged (default: every day of the
    study)."""
    thr = quarantine_thresholds(study, thresholds)
    searched = list(getattr(study, 'space', {}) or {})
    want = list(days_) if days_ is not None else [d.day for d in getattr(study, 'days', [])]
    from hindcast.study import StudyDir                     # lazy: study imports days lazily too
    base_by_day, counts_by_day = {}, {}
    # trials.jsonl is append-only: only the latest row per key counts (a done row superseded by a stale / failed
    # re-evaluation row no longer contributes its metrics)
    for r in StudyDir.latest_by_key(rows or []).values():
        if getattr(r, 'status', None) != 'done':
            continue
        day = r.day
        if _is_base_row(r):
            base_by_day[day] = r.metrics
        loops = (r.metrics or {}).get('loops')
        if isinstance(loops, (int, float)):
            counts_by_day.setdefault(day, {})[r.vhash] = int(loops)
    out = {}
    for day in want:
        out[day] = quarantine_reasons(day, settings=settings, searched_keys=searched, base_metrics=base_by_day.get(day),
                                      meta=(metas or {}).get(day), loop_counts=counts_by_day.get(day), thresholds=thr)
    return out


def _previous_role(reason_text):
    """The role a quarantined day had before ('was <role>: ...' prefix of quarantine_reason), default 'search'."""
    text = str(reason_text or '')
    if text.startswith('was ') and ':' in text:
        prev = text[4:].split(':', 1)[0].strip()
        if prev in ('search', 'holdout'):
            return prev
    return 'search'


def apply_quarantine(study, reasons_by_day, restore=False):
    """Set role 'quarantined' on the study's days that have reasons, with quarantine_reason = 'was <role>: <reasons>'
    (the previous role is kept in that prefix); returns [(day, reasons)] of the days whose role changed. A day that is
    already quarantined gets its reasons refreshed. `restore=True` gives a quarantined day that has no reasons any more
    its previous role back (reasons [] in the returned list)."""
    changed = []
    for d in getattr(study, 'days', []):
        reasons = list((reasons_by_day or {}).get(d.day) or [])
        if reasons:
            prev = _previous_role(d.quarantine_reason) if d.role == QUARANTINE_ROLE else d.role
            d.quarantine_reason = f'was {prev}: ' + '; '.join(reasons)
            if d.role != QUARANTINE_ROLE:
                d.role = QUARANTINE_ROLE
                changed.append((d.day, reasons))
        elif restore and d.role == QUARANTINE_ROLE:
            d.role = _previous_role(d.quarantine_reason)
            d.quarantine_reason = None
            changed.append((d.day, []))
    return changed


def scoring_days(study, reasons_by_day=None, role='search'):
    """The days of `role` that are not quarantined (by their study role or by `reasons_by_day`)."""
    out = []
    for d in getattr(study, 'days', []):
        if d.role != role:
            continue
        if reasons_by_day and reasons_by_day.get(d.day):
            continue
        out.append(d.day)
    return out


def quarantine_listing(reasons_by_day, thresholds=None):
    """Text lines: the quarantined days with their reasons (listed, not hidden), then the thresholds used."""
    thr = quarantine_thresholds(None, thresholds)
    q = {d: r for d, r in sorted((reasons_by_day or {}).items()) if r}
    if not q:
        lines = ['quarantined days: none']
    else:
        lines = [f'quarantined days ({len(q)}; listed, excluded from scoring):']
        for d, reasons in q.items():
            for i, r in enumerate(reasons):
                lines.append(f'  {d if i == 0 else " " * len(d)}  {r}')
    lines.append(f'  thresholds: base agreement >= {thr["agreement_pct_min"]:g} %, CGM gap <= {thr["cgm_gap_max_min"]:g} min, '
                 f'no profile_history change of a searched key inside the day, equal loop counts across variants')
    return lines


if __name__ == '__main__':
    import sys
    spec = sys.argv[1] if len(sys.argv) > 1 else 'yesterday'
    root = cache_dir()
    for d in parse_days(spec):
        f = finality(d, root)
        print(d, 'cached' if f.cached else 'not cached', 'final' if f.final else f.why())
