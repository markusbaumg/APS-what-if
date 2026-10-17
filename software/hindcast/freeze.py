"""
freeze.py  -  frozen day files, FrozenSource, `hc freeze` and `hc warm` (Hindcast H-101, H-104)

A frozen day is the per-loop input set `ns_source.NightscoutSource.loops()` reconstructs for one UTC day, written
once as `frozen/<host>/<day>.loops.jsonl.gz` plus `<day>.meta.json`, so that every replay of that day (hundreds of
variants) reads a 2 MB file instead of re-running the reconstruction (ns_glucose / ns_iob / ns_meal / ns_profile,
VDF-independent) over the cached Nightscout records. `FrozenSource` is the drop-in for `ns_source.NightscoutSource`
that `emulator_core.scanNightscout()` constructs; `replay.py` (H-102) installs it with
`ns_source.NightscoutSource = FrozenSource` on the real module object.

    from hindcast import freeze
    meta = freeze.freeze_day('2026-09-06')                        # needs the cached day, refuses a non-final one
    src = freeze.FrozenSource('ns:', '2026-09-06T06:00:00Z', '2026-09-06T09:00:00Z', log_msg=print)
    src.load(); loops = list(src.loops())                         # == live NightscoutSource(...).loops()
    freeze.blocked_reason('2026-09-06')                            # None, or why replay.py must mark the trial blocked
    python -m hindcast.freeze freeze 2026-09-06 [--new] [--allow-partial] [--refreeze] [--list] [--online]
    python -m hindcast.freeze freeze --recompute-meta [2026-08-30..2026-10-15]   # character / confounders only
    python -m hindcast.freeze warm   2026-08-30..2026-09-07 [--allow-partial]

File format. `<day>.loops.jsonl.gz` is gzip JSONL, one event per line in the order the live source produced them:
a loop line is the dict `loops()` yielded (`record`, `suggested`, `stamp`, `now_ms`, `utcOffset`, `glucose_status`,
`iob_array`, `currenttemp`, `lastTempAge`, `profile`, `activity_monitor`, `autosens_data`, `meal_data`,
`microBolusAllowed`, `console_error`, `autoISF_version`, and `cgm_bridged: true` only on a loop replayed on a CGM
reading bridged from devicestatus (H-714, ns_glucose; files written before it simply have no such key); see
NIGHTSCOUT_SOURCE.md), a dropped line is
`{"dropped": "<reason>", "ms": <loop_ms of the record>}` for every record the live source skipped (duplicate upload,
loop aborted by AAPS, no CGM within 12 min, no result, and the counted-but-replayed 'SMB fallback loop'; plus
`"cgm_bridged": true` when the skipped record still gave a bridged reading, so FrozenSource can print the live
source's 'CGM readings bridged from devicestatus: N (M loops)' counter: N = bridged loops + such drops). Storing
the drops with their time lets `FrozenSource.loops()` print the same end-of-run `Nightscout loops skipped:` counters
for any sub-window as the live source. The dicts are JSON by construction (the core `json.dumps()` them into its
collectors) and the round trip is dict-equal (verified: `frozen loops() == live loops()` for 2026-09-06 06-09Z and
for the whole day, see tests/test_freeze.py), so the format is JSONL, not pickle. `sha1` in the meta is over the
uncompressed JSONL bytes and is the `freeze_sha` of every trials.jsonl row of that day.

`<day>.meta.json` (contract of DESIGN.md section 4.5, extended): day, host, url, window [start, end) ISO, loops,
sha1, bytes, bytes_gz, seconds, created, final, partial, ns_code_sha (sha1 over the ns_*.py files only - the
freeze depends on nothing else of the core), code_sha (contracts.code_sha, for the record), settings_sha
(contracts.settings_sha), dropped {no_reason, aborted, no_cgm, duplicate, outside, smb_fallback}, bridged {readings,
loops} (H-714: CGM readings of the day bridged from devicestatus and loops replayed on one; absent in metas written
before it, readers use .get), character (days.character), confounders (days.confounders), autoISF_versions,
finality {missing, partial}.

Meta recompute (`hc freeze --recompute-meta [days]`, recompute_meta()). Rewrites only `character`, `confounders`
and `autoISF_versions` of <day>.meta.json (plus `meta_recomputed`, the time) from the cache and the frozen loops,
without re-running the reconstruction: the loops file is not touched and re-hashed first (its sha1 must still be
meta.sha1), so `sha1` - the freeze_sha of every trials.jsonl row - and every other key stay as they are and no study
goes stale. It needs the day's cache files (offline) and refuses a stale day (the character would be computed under
other settings than the frozen loops). Used when days.character / days.confounders change (2026-10-17: the CGM gap
measured on the day's own entries, clipped at midnight, loop gaps as their own badge).

Staleness. A frozen file whose `ns_code_sha` or `settings_sha` no longer matches the working tree is `stale`:
`--list` reports it, `freeze_days()` skips it unless `refreeze=True`, `blocked_reason()` names it so replay.py marks
the trial `blocked` (DESIGN.md: a trial never mixes emulator versions).

Offline rule (H-104). `hc warm` is the only step that may open a socket. Everything else, the freeze included,
runs with `HINDCAST_OFFLINE` unset or '1' = offline: `offline_guard()` patches `ns_client.NightscoutClient` so that
`_get` raises `OfflineError` (a RuntimeError, which `scanNightscout` turns into a normal STOP) and the cache readers
(`_load_day`, `profiles`, `latest_treatment`) serve whatever file exists, final or partial, instead of refetching.
`HINDCAST_OFFLINE=0` or `hc freeze --online` lets the freeze fetch through the normal `ns_client` rules. The warm
walks the days serially through one client (its 1.5 s back-off between retries stays), takes `warm.lock` in the
cache directory so two studies never warm at once, refuses days that cannot be final yet (end of day + 1 h in
the future) unless `--allow-partial`, and performs the exact fetch set `NightscoutSource.load()` needs (treatments
and CGM 8 h back, devicestatus 2 h back and 2 min past the day end, the two `latest/` look-ups, the profile
store), so a freeze right after a warm needs no request and a second warm of final days performs none.

Measured on the author's machine (Python 3.14, Apple silicon), 2026-09-06, 1311 loops: `freeze_day()` 42 s
(a first measurement of the live `loops()` alone took 145 s while another process ran; cProfile: 73 % in
ns_meal.slopes_at - slope calibration and the smoother re-run of the deviation history -, 18 % in ns_iob.iob_at;
`load()` 1.4 s), JSONL 28.0 MB (21 KB per loop), gzip 2.1 MB (compresslevel 6, 0.3 s), FrozenSource over the whole
day 0.5 s, over 06-09Z 0.5 s (the whole file is read). The frozen day equals the whole-day live reconstruction
dict for dict. A live source built for a sub-window (06-09Z) is NOT dict-equal: its `glucose_status` deltas /
`dura_ISF_average` and the `meal_data` deviation slopes differ in the 2nd-3rd decimal because ns_glucose's smoother
and ns_meal's deviation history start at the window start (8 h look-back from there) - a window-start dependence
of the live reconstruction, reported to the fidelity epic; the frozen day is the whole-day reconstruction, so
`hc check` must compare the frozen replay with a whole-day live source filtered to the window, not with a
06-09Z live source.
"""
import argparse
import contextlib
import glob
import gzip
import hashlib
import json
import os
import sys
import time

import ns_client
import ns_source
from ns_client import NightscoutClient, day_bounds, parse_iso, to_iso, utc_day

from hindcast import contracts, days

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN_BASE = os.path.join(SOFTWARE_DIR, 'frozen')          # overridden by HINDCAST_FROZEN_DIR or base_dir arguments
FORMAT = 1                                                  # bump when a line's shape changes
WARM_LOCK_STALE_S = 2 * 60 * 60                             # a warm.lock older than this is taken over
EXIT_OK, EXIT_GUARD, EXIT_REFUSED = 0, 2, 3                 # DESIGN.md section 4.5 exit codes

DROPPED_KEYS = {                                            # live skip reason -> meta counter
    'no result in record': 'no_reason',
    'duplicate upload': 'duplicate',
    'no CGM value within 12 min': 'no_cgm',
    'outside window': 'outside',
    'SMB fallback loop (replayed without SMB)': 'smb_fallback',
}


class FreezeError(RuntimeError):
    """A day cannot be frozen or read as requested; the message says why and what to run."""


class OfflineError(RuntimeError):
    """A Nightscout request was attempted while Hindcast runs offline (HINDCAST_OFFLINE); run `hc warm` first."""


# ----------------------------------------------------------------------------- paths and hashes

def frozen_base(base_dir=None):
    return base_dir or os.environ.get('HINDCAST_FROZEN_DIR') or FROZEN_BASE


def frozen_dir(host, base_dir=None):
    """frozen/<host>/ (created on demand by the writer, not here)."""
    return os.path.join(frozen_base(base_dir), host)


def loops_path(day, host, base_dir=None):
    return os.path.join(frozen_dir(host, base_dir), day + '.loops.jsonl.gz')


def meta_path(day, host, base_dir=None):
    return os.path.join(frozen_dir(host, base_dir), day + '.meta.json')


def ns_code_sha(software_dir=SOFTWARE_DIR):
    """sha1 over the ns_*.py files (name + bytes, sorted): what a frozen day depends on."""
    h = hashlib.sha1()
    for path in sorted(glob.glob(os.path.join(software_dir, 'ns_*.py'))):
        h.update(os.path.basename(path).encode('utf-8') + b'\0')
        with open(path, 'rb') as fh:
            h.update(fh.read())
        h.update(b'\0')
    return h.hexdigest()


def dropped_key(reason):
    """meta counter name of a live skip reason ('loop aborted by AAPS (no bg/predBGs): ...' -> 'aborted')."""
    if reason in DROPPED_KEYS:
        return DROPPED_KEYS[reason]
    if reason.startswith('loop aborted by AAPS'):
        return 'aborted'
    return 'other'


# ----------------------------------------------------------------------------- offline guard

def is_offline():
    """True unless HINDCAST_OFFLINE is '0' (default offline: `hc warm` is the only network step)."""
    return os.environ.get('HINDCAST_OFFLINE', '1') != '0'


def _read_json(fn):
    with open(fn, 'r', encoding='utf-8') as f:
        return json.load(f)


@contextlib.contextmanager
def offline_guard(enabled=None):
    """Inside the block the shared NightscoutClient opens no socket: `_get` raises OfflineError, the cache readers
    serve any existing file (final or partial) and raise OfflineError only when a file is missing. `enabled=None`
    follows is_offline(); False makes the block a no-op."""
    if enabled is None:
        enabled = is_offline()
    if not enabled:
        yield
        return
    original = {name: getattr(NightscoutClient, name) for name in ('_get', '_load_day', 'profiles', 'latest_treatment')}

    def _get(self, path, params):
        raise OfflineError(f'Hindcast runs offline (HINDCAST_OFFLINE); {ns_client.redact(path)} would need the network. '
                           f'Run `hc warm <days>` first')

    def _load_day(self, coll, day):
        fn = self._day_file(coll, day)
        if os.path.exists(fn):
            try:
                return _read_json(fn)
            except (json.JSONDecodeError, OSError) as e:
                raise OfflineError(f'cache file {fn} is unreadable ({e!r}); run `hc warm` to refetch it')
        raise OfflineError(f'cache file {coll}/{day}.json is missing; run `hc warm {day}` (offline mode)')

    def profiles(self, count=50, newer_than=None):
        fn = self._day_file('profile', 'all')
        if os.path.exists(fn):
            return _read_json(fn)
        raise OfflineError('the profile store is not cached; run `hc warm` (offline mode)')

    def latest_treatment(self, event_type, before):
        before_ms = before if isinstance(before, (int, float)) else parse_iso(before)
        fn = self._day_file('latest', days.latest_name(event_type, before_ms))
        if os.path.exists(fn):
            return _read_json(fn)
        raise OfflineError(f'latest "{event_type}" before {to_iso(before_ms)} is not cached; run `hc warm` (offline mode)')

    NightscoutClient._get = _get
    NightscoutClient._load_day = _load_day
    NightscoutClient.profiles = profiles
    NightscoutClient.latest_treatment = latest_treatment
    try:
        yield
    finally:
        for name, fn in original.items():
            setattr(NightscoutClient, name, fn)


# ----------------------------------------------------------------------------- the live source, recording its drops

class _Tracked(list):
    """A list whose iteration remembers the current element (the live loops() iterates self.devicestatus)."""
    current = None

    def __iter__(self):
        for rec in list.__iter__(self):
            self.current = rec
            yield rec


class _RecordingSource(ns_source.NightscoutSource):
    """The live source with every skipped record remembered as a dropped event (reason + loop_ms)."""

    def load(self):
        super().load()
        self.devicestatus = _Tracked(self.devicestatus)
        self.pending = []

    def _skip(self, why):
        super()._skip(why)
        rec = getattr(self.devicestatus, 'current', None)
        ev = {'dropped': why, 'ms': ns_source.loop_ms(rec) if isinstance(rec, dict) else None}
        if why != 'duplicate upload' and ev['ms'] in getattr(self.glucose, 'bridged_ts', ()):
            ev['cgm_bridged'] = True        # a bridged CGM reading came from this record (FrozenSource counts it)
        self.pending.append(ev)


# ----------------------------------------------------------------------------- freezing

def read_meta(day, host, base_dir=None):
    """The meta dict of a frozen day or None."""
    fn = meta_path(day, host, base_dir)
    if not os.path.isfile(fn):
        return None
    try:
        return _read_json(fn)
    except (json.JSONDecodeError, OSError):
        return None


def is_frozen(day, host, base_dir=None):
    return os.path.isfile(loops_path(day, host, base_dir)) and read_meta(day, host, base_dir) is not None


def staleness(meta, settings=None, software_dir=SOFTWARE_DIR, settings_path=contracts.NS_SETTINGS_PATH):
    """Why a frozen day no longer matches the working tree: [] when fresh, else 'ns_code_sha changed' and/or
    'settings_sha changed' (and 'format changed')."""
    out = []
    if not meta:
        return ['no meta']
    if meta.get('format') != FORMAT:
        out.append('format changed')
    if meta.get('ns_code_sha') != ns_code_sha(software_dir):
        out.append('ns_code_sha changed')
    try:
        current = contracts.settings_sha(settings_path, settings)
    except (OSError, ValueError):
        current = None
    if meta.get('settings_sha') != current:
        out.append('settings_sha changed')
    return out


def read_events(path):
    """Generator over the events of a frozen file (loop dicts and dropped dicts, in order)."""
    with gzip.open(path, 'rb') as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read_loops(path):
    """Generator over the loop dicts of a frozen file."""
    for ev in read_events(path):
        if 'dropped' not in ev:
            yield ev


def freeze_day(day, url='ns:', settings=None, allow_partial=False, force=False, log_msg=print, base_dir=None,
               offline=None, software_dir=SOFTWARE_DIR, settings_path=contracts.NS_SETTINGS_PATH):
    """Reconstruct one UTC day with the live source and write <day>.loops.jsonl.gz + <day>.meta.json; returns the meta.

    Refuses (FreezeError) a day that is already frozen unless `force`, a day whose cache is not final unless
    `allow_partial` (then meta.final is False and meta.partial True), and a day whose cache files are missing when
    offline. `offline=None` follows HINDCAST_OFFLINE (default offline)."""
    if not days.is_day(day):
        raise FreezeError(f'"{day}" is not a UTC day (YYYY-MM-DD)')
    settings = settings if settings is not None else days.load_settings(settings_path)
    host = days.host_of(url, settings)
    out_loops, out_meta = loops_path(day, host, base_dir), meta_path(day, host, base_dir)
    if is_frozen(day, host, base_dir) and not force:
        raise FreezeError(f'{day} is already frozen ({out_loops}); use --refreeze to replace it')
    cache_root = days.cache_dir(url, settings)
    fin = days.finality(day, cache_root)
    if offline is None:
        offline = is_offline()
    if fin.missing and offline:
        raise FreezeError(f'{day}: cache incomplete ({fin.why()}); run `hc warm {day}` first (Hindcast runs offline)')
    if not fin.final and not allow_partial:
        raise FreezeError(f'{day} is not final ({fin.why()}); pass --allow-partial to freeze it anyway')

    start_iso, end_iso = days.day_window(day)
    t0 = time.time()
    with offline_guard(offline):
        src = _RecordingSource(url, start_iso, end_iso, settings=settings, log_msg=log_msg, verbose=not offline)
        src.load()
        os.makedirs(os.path.dirname(out_loops), exist_ok=True)
        tmp = out_loops + '.tmp'
        sha = hashlib.sha1()
        n_loops = n_bytes = 0
        loops = []
        counters = {}

        def put(obj):
            nonlocal n_bytes
            line = json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
            sha.update(line)
            n_bytes += len(line)
            gz.write(line)

        with gzip.open(tmp, 'wb', compresslevel=6) as gz:
            for loop in src.loops():
                for ev in src.pending:
                    counters[dropped_key(ev['dropped'])] = counters.get(dropped_key(ev['dropped']), 0) + 1
                    put(ev)
                src.pending.clear()
                put(loop)
                loops.append(loop)
                n_loops += 1
            for ev in src.pending:
                counters[dropped_key(ev['dropped'])] = counters.get(dropped_key(ev['dropped']), 0) + 1
                put(ev)
            src.pending.clear()
        os.replace(tmp, out_loops)

    if n_loops == 0:
        try:
            os.remove(out_loops)
        except OSError:
            pass
        raise FreezeError(f'{day}: no loop found in the cached devicestatus; nothing frozen')

    dropped = {k: counters.get(k, 0) for k in ('no_reason', 'aborted', 'no_cgm', 'duplicate', 'outside', 'smb_fallback')}
    for k, v in counters.items():
        if k not in dropped:
            dropped[k] = v
    day_start, day_end = day_bounds(day)
    gh = getattr(src, 'glucose', None)
    bridged = {'readings': gh.bridged_in(day_start, day_end) if hasattr(gh, 'bridged_in') else 0,
               'loops': sum(1 for l in loops if l.get('cgm_bridged'))}
    meta = {
        'format': FORMAT, 'day': day, 'host': host, 'url': days.resolve_url(url, settings),
        'window': [start_iso, end_iso], 'loops': n_loops, 'sha1': sha.hexdigest(),
        'bytes': n_bytes, 'bytes_gz': os.path.getsize(out_loops), 'seconds': round(time.time() - t0, 1),
        'created': contracts.now_iso(), 'final': bool(fin.final), 'partial': not fin.final,
        'ns_code_sha': ns_code_sha(software_dir), 'code_sha': contracts.code_sha(software_dir),
        'settings_sha': contracts.settings_sha(settings_path, settings),
        'dropped': dropped,
        'bridged': bridged,
        'character': days.character(day, src, loops, cache_root=cache_root),
        'confounders': days.confounders(day, settings, src, loops, cache_root=cache_root),
        'autoISF_versions': sorted({l['autoISF_version'] for l in loops if l.get('autoISF_version')}),
        'finality': {'missing': fin.missing, 'partial': fin.partial},
    }
    contracts.write_json_atomic(out_meta, meta)
    log_msg(f'frozen {day}: {n_loops} loops, {meta["bytes_gz"] / 1e6:.1f} MB gz, {meta["seconds"]} s'
            + ('' if fin.final else ' (PARTIAL: cache not final)'))
    return meta


CHARACTER_KEYS = ('character', 'confounders', 'autoISF_versions')


def _loops_sha1(path):
    h = hashlib.sha1()
    with gzip.open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


_CharacterSource = ns_source.NightscoutSource              # load() only: treatments, iob, profiles (tests patch it)
_SLIM_KEYS = ('now_ms', 'utcOffset', 'console_error', 'autoISF_version', 'cgm_bridged')


def recompute_meta(day, url='ns:', settings=None, base_dir=None, log_msg=print, software_dir=SOFTWARE_DIR,
                   settings_path=contracts.NS_SETTINGS_PATH, dry=False):
    """Recompute character / confounders / autoISF_versions of a frozen day's meta from the cache and the frozen
    loops (no reconstruction, loops file untouched); returns (old_meta, new_meta). Writes the meta unless `dry`.
    FreezeError when the day is not frozen, stale, its loops file no longer hashes to meta.sha1, or a cache file is
    missing."""
    if not days.is_day(day):
        raise FreezeError(f'"{day}" is not a UTC day (YYYY-MM-DD)')
    settings = settings if settings is not None else days.load_settings(settings_path)
    host = days.host_of(url, settings)
    old = read_meta(day, host, base_dir)
    lp = loops_path(day, host, base_dir)
    if old is None or not os.path.isfile(lp):
        raise FreezeError(f'{day} is not frozen; nothing to recompute')
    stale = staleness(old, settings, software_dir, settings_path)
    if stale:
        raise FreezeError(f'{day} frozen file is stale ({", ".join(stale)}); refreeze it instead (--refreeze)')
    if _loops_sha1(lp) != old.get('sha1'):
        raise FreezeError(f'{day}: the loops file no longer hashes to meta.sha1; refreeze it (--refreeze)')
    cache_root = days.cache_dir(url, settings)
    loops, skipped = [], {}
    for ev in read_events(lp):
        if 'dropped' in ev:
            skipped[ev['dropped']] = skipped.get(ev['dropped'], 0) + 1
        else:
            loops.append({k: ev.get(k) for k in _SLIM_KEYS})
    if len(loops) != old.get('loops'):
        raise FreezeError(f'{day}: the loops file holds {len(loops)} loops, the meta says {old.get("loops")}; refreeze it')
    start_iso, end_iso = days.day_window(day)
    with offline_guard(True):
        src = _CharacterSource(url, start_iso, end_iso, settings=settings, log_msg=lambda *a: None, verbose=False)
        try:
            src.load()
        except OfflineError as e:
            raise FreezeError(f'{day}: {e}') from None
    src.skipped = skipped
    new = dict(old)
    new['character'] = days.character(day, src, loops, cache_root=cache_root)
    new['confounders'] = days.confounders(day, settings, src, loops, cache_root=cache_root)
    new['autoISF_versions'] = sorted({l['autoISF_version'] for l in loops if l.get('autoISF_version')})
    new['meta_recomputed'] = contracts.now_iso()
    if not dry:
        contracts.write_json_atomic(meta_path(day, host, base_dir), new)
    return old, new


def character_changes(old, new):
    """[(key, old, new)] of the character fields that differ between two metas (keys of either side)."""
    a, b = (old or {}).get('character') or {}, (new or {}).get('character') or {}
    return [(k, a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def recompute_metas(day_specs=None, url='ns:', settings=None, base_dir=None, log_msg=print, dry=False,
                    software_dir=SOFTWARE_DIR, settings_path=contracts.NS_SETTINGS_PATH):
    """recompute_meta over the days (every frozen day when None); [(day, outcome, detail, old, new)] with outcome
    'recomputed', 'refused' or 'failed'."""
    settings = settings if settings is not None else days.load_settings(settings_path)
    host = days.host_of(url, settings)
    if day_specs:
        want = days.parse_days(day_specs)
    else:
        d = frozen_dir(host, base_dir)
        want = sorted(n[:-len('.meta.json')] for n in (os.listdir(d) if os.path.isdir(d) else [])
                      if n.endswith('.meta.json') and days.is_day(n[:-len('.meta.json')]))
    out = []
    for day in want:
        try:
            old, new = recompute_meta(day, url, settings, base_dir=base_dir, log_msg=log_msg, software_dir=software_dir,
                                      settings_path=settings_path, dry=dry)
        except FreezeError as e:
            out.append((day, 'refused', str(e), None, None))
            continue
        except (OSError, RuntimeError, ValueError) as e:
            out.append((day, 'failed', repr(e), None, None))
            continue
        ch = character_changes(old, new)
        detail = ', '.join(f'{k} {a} -> {b}' for k, a, b in ch) or 'character unchanged'
        if (old.get('confounders') or []) != new['confounders']:
            detail += '; badges changed'
        out.append((day, 'recomputed', detail, old, new))
    return out


def freeze_days(day_specs, url='ns:', settings=None, new=False, allow_partial=False, refreeze=False, log_msg=print,
                base_dir=None, offline=None, software_dir=SOFTWARE_DIR, settings_path=contracts.NS_SETTINGS_PATH):
    """Freeze several days; returns [(day, outcome, detail)] with outcome in 'frozen', 'kept', 'skipped', 'refused',
    'failed'. `new` freezes only days without a frozen file (and keeps fresh ones silently: what `run` calls hourly);
    `refreeze` replaces stale (or, with explicit days, existing) frozen files. Nothing raises; exit codes come from
    outcomes() below."""
    settings = settings if settings is not None else days.load_settings(settings_path)
    host = days.host_of(url, settings)
    results = []
    for day in days.parse_days(day_specs):
        meta = read_meta(day, host, base_dir)
        if meta is not None and os.path.isfile(loops_path(day, host, base_dir)):
            stale = staleness(meta, settings, software_dir, settings_path)
            if stale and not refreeze:
                results.append((day, 'refused', 'stale: ' + ', '.join(stale) + '; pass --refreeze'))
                continue
            if not stale and (new or not refreeze):
                results.append((day, 'kept', 'already frozen' + ('' if meta.get('final') else ' (partial)')))
                continue
        try:
            m = freeze_day(day, url, settings, allow_partial=allow_partial, force=True, log_msg=log_msg,
                           base_dir=base_dir, offline=offline, software_dir=software_dir, settings_path=settings_path)
            results.append((day, 'frozen', f'{m["loops"]} loops' + ('' if m['final'] else ', partial')))
        except FreezeError as e:
            results.append((day, 'refused', str(e)))
        except OfflineError as e:
            results.append((day, 'refused', str(e)))
        except (OSError, RuntimeError, ValueError) as e:
            results.append((day, 'failed', repr(e)))
    return results


def outcomes_exit_code(results):
    """0 when every day is frozen or kept, 3 when one was refused (not final / stale / offline), 2 on a failure."""
    kinds = {r[1] for r in results}
    if 'failed' in kinds:
        return EXIT_GUARD
    if 'refused' in kinds:
        return EXIT_REFUSED
    return EXIT_OK


def blocked_reason(day, url='ns:', settings=None, base_dir=None, software_dir=SOFTWARE_DIR,
                   settings_path=contracts.NS_SETTINGS_PATH, require_final=True):
    """None when replay.py may run a trial on `day`, else the reason for `status: blocked`: not frozen, frozen from
    a non-final cache (unless require_final=False), or stale against the working tree."""
    settings = settings if settings is not None else days.load_settings(settings_path)
    host = days.host_of(url, settings)
    meta = read_meta(day, host, base_dir)
    if meta is None or not os.path.isfile(loops_path(day, host, base_dir)):
        return f'{day} is not frozen; run `hc freeze {day}`'
    if require_final and not meta.get('final'):
        return f'{day} was frozen from a non-final cache (partial); warm and refreeze it once the day is final'
    stale = staleness(meta, settings, software_dir, settings_path)
    if stale:
        return f'{day} frozen file is stale ({", ".join(stale)}); run `hc freeze {day} --refreeze`'
    return None


# ----------------------------------------------------------------------------- listing

def list_days(day_specs=None, url='ns:', settings=None, base_dir=None, software_dir=SOFTWARE_DIR,
              settings_path=contracts.NS_SETTINGS_PATH, verify=False):
    """[{day, cached, final, frozen, stale: [..], loops, partial, summary, sha_ok}] for the given days, or for every
    day that is cached or frozen when `day_specs` is None. `verify` re-hashes the frozen file (sha_ok)."""
    settings = settings if settings is not None else days.load_settings(settings_path)
    host = days.host_of(url, settings)
    cache_root = days.cache_dir(url, settings)
    if day_specs:
        want = days.parse_days(day_specs)
    else:
        frozen = []
        d = frozen_dir(host, base_dir)
        if os.path.isdir(d):
            frozen = [n[:-len('.meta.json')] for n in os.listdir(d) if n.endswith('.meta.json')]
        want = sorted(set(days.cached_days(cache_root)) | {x for x in frozen if days.is_day(x)})
    rows = []
    for day in want:
        fin = days.finality(day, cache_root)
        meta = read_meta(day, host, base_dir)
        frozen_ok = meta is not None and os.path.isfile(loops_path(day, host, base_dir))
        row = {'day': day, 'cached': fin.cached, 'final': fin.final, 'finality': fin.why(), 'frozen': frozen_ok,
               'stale': staleness(meta, settings, software_dir, settings_path) if frozen_ok else [],
               'loops': meta.get('loops') if frozen_ok else None, 'partial': bool(meta.get('partial')) if frozen_ok else None,
               'summary': days.summarize(meta) if frozen_ok else '', 'sha_ok': None}
        if verify and frozen_ok:
            h = hashlib.sha1()
            with gzip.open(loops_path(day, host, base_dir), 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h.update(chunk)
            row['sha_ok'] = h.hexdigest() == meta.get('sha1')
        rows.append(row)
    return rows


def format_listing(rows):
    lines = [f'{"day":<10} {"cache":<16} {"frozen":<22} summary']
    for r in rows:
        cache = 'final' if r['final'] else ('partial' if r['cached'] else 'missing')
        if r['frozen']:
            fro = f'{r["loops"]} loops'
            if r['partial']:
                fro += ' partial'
            if r['stale']:
                fro = 'STALE ' + fro
            if r['sha_ok'] is False:
                fro = 'CORRUPT ' + fro
        else:
            fro = '-'
        lines.append(f'{r["day"]:<10} {cache:<16} {fro:<22} {r["summary"]}')
    return '\n'.join(lines)


# ----------------------------------------------------------------------------- FrozenSource

class FrozenSource:
    """Drop-in for ns_source.NightscoutSource that reads frozen day files instead of Nightscout.

    Same constructor `(url, start_iso, end_iso, settings=None, log_msg=print, verbose=True)` plus `base_dir`
    (frozen/ root, default HINDCAST_FROZEN_DIR or software/frozen); same attributes scanNightscout touches:
    `load()` (checks that every UTC day of the window is frozen, raises FreezeError - a RuntimeError, which
    scanNightscout reports as a source failure and ends with STOP), `loops()` (yields the stored dicts whose
    `now_ms` (deliverAt) lies in [start, end), in order, and prints the same 'Nightscout loops skipped:' counters
    the live source prints for that window), `skipped`, `url`, `settings`, `start_ms`, `end_ms`. Open bounds
    ('2000..'/'2099..') resolve like the live source (now - 24 h .. now). `metas` holds the meta dict per day."""

    def __init__(self, url, start_iso, end_iso, settings=None, log_msg=print, verbose=True, base_dir=None):
        self.settings = settings or days.load_settings()
        self.url = days.resolve_url(url, self.settings)
        self.host = days.host_of(url, self.settings)
        self.base_dir = base_dir
        self.log_msg = log_msg
        self.verbose = verbose
        self.start_ms = parse_iso(start_iso) if start_iso[:4] != '2000' else None
        self.end_ms = parse_iso(end_iso) if end_iso[:4] != '2099' else None
        self._resolved = None
        self._loaded = False
        self.skipped = {}
        self.metas = {}
        self.days = []

    def _window(self):
        if self._resolved is None:
            if self.start_ms is None or self.end_ms is None:
                self._resolved = ns_source.resolve_window('2000' if self.start_ms is None else to_iso(self.start_ms),
                                                         '2099' if self.end_ms is None else to_iso(self.end_ms))
            else:
                self._resolved = (self.start_ms, self.end_ms)
        return self._resolved

    def load(self):
        start, end = self._window()
        self.days = days.days_of_window(start, end)
        missing = [d for d in self.days if not is_frozen(d, self.host, self.base_dir)]
        if missing:
            raise FreezeError(f'not frozen: {", ".join(missing)} (frozen/{self.host}); run `hc freeze {missing[0]}'
                              + (f'..{missing[-1]}' if len(missing) > 1 else '') + '`')
        self.metas = {d: read_meta(d, self.host, self.base_dir) for d in self.days}
        self._loaded = True
        n = sum(m.get('loops', 0) for m in self.metas.values())
        self.log_msg(f'Frozen window {to_iso(start)} .. {to_iso(end)} from {frozen_dir(self.host, self.base_dir)} '
                     f'({len(self.days)} day file{"s" if len(self.days) != 1 else ""}, {n} loops stored)')

    def _skip(self, why):
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def loops(self):
        if not self._loaded:
            self.load()
        start, end = self._window()
        bridged_loops = bridged_readings = 0
        for day in self.days:
            for ev in read_events(loops_path(day, self.host, self.base_dir)):
                if 'dropped' in ev:
                    ms = ev.get('ms')
                    if ms is not None and start <= ms < end:
                        self._skip(ev['dropped'])
                        bridged_readings += bool(ev.get('cgm_bridged'))
                    continue
                ms = ev.get('now_ms')
                if ms is None or not (start <= ms < end):
                    continue
                if ev.get('cgm_bridged'):
                    bridged_loops += 1
                yield ev
        if self.skipped:
            self.log_msg('\nNightscout loops skipped: ' + ', '.join(f'{v} x {k}' for k, v in self.skipped.items()))
        if bridged_loops or bridged_readings:
            self.log_msg(ns_source.bridged_line(bridged_readings + bridged_loops, bridged_loops))


_LIVE_SOURCE = ns_source.NightscoutSource


def install_frozen_source(module=ns_source):
    """Make `module.NightscoutSource` the FrozenSource (what replay.py does before parameters_known); returns the
    live class so the caller can restore it."""
    live = module.NightscoutSource
    module.NightscoutSource = FrozenSource
    return live


def uninstall_frozen_source(module=ns_source, live=None):
    module.NightscoutSource = live or _LIVE_SOURCE


# ----------------------------------------------------------------------------- warm

class WarmLock:
    """`warm.lock` in the cache directory: one request stream per Nightscout host at a time."""

    def __init__(self, cache_root):
        self.path = os.path.join(cache_root, 'warm.lock')
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f'{os.getpid()} {contracts.now_iso()}\n'.encode('utf-8'))
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    continue
                if age > WARM_LOCK_STALE_S:
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                try:
                    holder = open(self.path, encoding='utf-8').read().strip()
                except OSError:
                    holder = '?'
                raise FreezeError(f'another warm is running ({self.path}: {holder}); one request stream per host')
        raise FreezeError(f'could not take {self.path}')

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.remove(self.path)
        except OSError:
            pass


def count_requests(client):
    """Wrap client._get so client.requests counts the HTTP requests made (tests and the warm report)."""
    original = client._get
    client.requests = 0

    def counting(path, params):
        client.requests += 1
        return original(path, params)

    client._get = counting
    return client


def warm_day(client, day, log_msg=print):
    """The fetch set NightscoutSource.load() needs for `day`, through the client's cache rules; returns the number
    of requests made (0 when every file was served from the cache)."""
    before = getattr(client, 'requests', None)
    if before is None:
        count_requests(client)
        before = 0
    start, end = day_bounds(day)
    client.devicestatus(start - days.DS_LOOKBACK_MS, end + days.UPLOAD_LAG_MS)
    client.treatments(start - days.LOOKBACK_MS, end)
    client.entries(start - days.LOOKBACK_MS, end)
    client.profiles(newer_than=end)
    for et in ('Profile Switch', 'Temporary Target'):
        client.latest_treatment(et, start - days.LOOKBACK_MS)
    made = client.requests - before
    log_msg(f'warm {day}: {made} request{"s" if made != 1 else ""}')
    return made


def warm_days(day_specs, url='ns:', settings=None, allow_partial=False, log_msg=print, client=None, now_ms=None,
              settings_path=contracts.NS_SETTINGS_PATH):
    """Serially fill the cache for the days; returns [(day, outcome, detail)] with outcome 'warmed' (detail = request
    count), 'refused' (cannot be final yet and not allow_partial) or 'failed' (network / data error after the client's
    retries; the walk stops there). Takes warm.lock. Sets HINDCAST_OFFLINE=1 for the rest of the process afterwards
    unless the variable was set explicitly, so a following freeze in the same process runs offline."""
    settings = settings if settings is not None else days.load_settings(settings_path)
    client = client or days.client_for(url, settings, verbose=True)
    count_requests(client)
    results = []
    explicit = 'HINDCAST_OFFLINE' in os.environ
    with WarmLock(client.cache_dir):
        for day in days.parse_days(day_specs, now_ms):
            if not days.can_be_final(day, now_ms) and not allow_partial:
                results.append((day, 'refused', f'{day} cannot be final before {to_iso(day_bounds(day)[1] + days.SETTLE_MS)[:16]}Z;'
                                                ' pass --allow-partial'))
                continue
            try:
                n = warm_day(client, day, log_msg)
                results.append((day, 'warmed', f'{n} requests' + ('' if days.can_be_final(day, now_ms) else ' (partial)')))
            except (OSError, RuntimeError, ValueError) as e:
                results.append((day, 'failed', ns_client.redact(repr(e))))
                break
    if not explicit:
        os.environ['HINDCAST_OFFLINE'] = '1'
    return results


# ----------------------------------------------------------------------------- CLI

def register(subparsers):
    """Add the `freeze` and `warm` subcommands to an argparse subparsers object (cli.py calls this); each parser's
    default `func` is run_freeze / run_warm."""
    p = subparsers.add_parser('freeze', help='freeze the reconstructed loop inputs of cached UTC days',
                              description='One frozen file per UTC day under frozen/<host>/. Offline unless --online '
                                          '(run `hc warm` first). Refuses days whose cache is not final.')
    p.add_argument('days', nargs='*', help='YYYY-MM-DD, from..to, yesterday, or a comma list (none with --list: every day)')
    p.add_argument('--new', action='store_true', help='freeze only the days not yet frozen (what `run` calls hourly)')
    p.add_argument('--allow-partial', action='store_true', help='freeze a day whose cache is not final (meta.final false)')
    p.add_argument('--refreeze', action='store_true', help='replace frozen files that are stale (or exist, with explicit days)')
    p.add_argument('--list', action='store_true', help='list cache / frozen state per day instead of freezing')
    p.add_argument('--verify', action='store_true', help='with --list: re-hash the frozen files')
    p.add_argument('--online', action='store_true', help='let the freeze fetch missing cache files (HINDCAST_OFFLINE=0)')
    p.add_argument('--recompute-meta', action='store_true',
                   help='rewrite only character / confounders of frozen metas from the cache (no replay; loops file and '
                        'sha1 unchanged); days default to every frozen day')
    p.add_argument('--dry', action='store_true', help='with --recompute-meta: print the changes, write nothing')
    p.add_argument('--url', default='ns:', help='Nightscout url (default ns: = ns_settings.json)')
    p.add_argument('--frozen-dir', default=None, help='frozen/ root (default HINDCAST_FROZEN_DIR or software/frozen)')
    p.set_defaults(func=run_freeze)

    w = subparsers.add_parser('warm', help='fill the Nightscout day cache serially (the only network step)',
                              description='Walks the days through ns_client with its cache rules and back-off; one '
                                          'request stream, never two studies at once (warm.lock). Refuses days that '
                                          'cannot be final yet unless --allow-partial.')
    w.add_argument('days', nargs='+', help='YYYY-MM-DD, from..to, yesterday, or a comma list')
    w.add_argument('--allow-partial', action='store_true', help='also fetch days that are still running / settling')
    w.add_argument('--url', default='ns:', help='Nightscout url (default ns: = ns_settings.json)')
    w.set_defaults(func=run_warm)
    return p, w


def run_freeze(args, settings=None, settings_path=contracts.NS_SETTINGS_PATH, log_msg=print):
    """`hc freeze`: returns the exit code (0 ok, 2 failure, 3 refused). cli.py passes `args` only; the keyword
    arguments let a test point at another ns_settings.json."""
    base_dir = getattr(args, 'frozen_dir', None)
    if getattr(args, 'online', False):
        os.environ['HINDCAST_OFFLINE'] = '0'
    try:
        if getattr(args, 'list', False):
            rows = list_days(args.days or None, url=args.url, settings=settings, base_dir=base_dir,
                             settings_path=settings_path, verify=getattr(args, 'verify', False))
            print(format_listing(rows))
            return EXIT_OK
        if getattr(args, 'recompute_meta', False):
            res = recompute_metas(args.days or None, url=args.url, settings=settings, base_dir=base_dir, log_msg=log_msg,
                                  dry=getattr(args, 'dry', False), settings_path=settings_path)
            for day, outcome, detail, _, _ in res:
                print(f'{day}: {outcome} - {detail}')
            return outcomes_exit_code([(r[0], 'frozen' if r[1] == 'recomputed' else r[1], r[2]) for r in res])
        if not args.days:
            print('hc freeze: give the days to freeze (YYYY-MM-DD, from..to) or --list', file=sys.stderr)
            return EXIT_REFUSED
        results = freeze_days(args.days, url=args.url, settings=settings, new=args.new, allow_partial=args.allow_partial,
                              refreeze=args.refreeze, log_msg=log_msg, base_dir=base_dir, settings_path=settings_path)
    except (FreezeError, ValueError) as e:
        print(f'hc freeze: {e}', file=sys.stderr)
        return EXIT_REFUSED
    for day, outcome, detail in results:
        print(f'{day}: {outcome} - {detail}')
    return outcomes_exit_code(results)


def run_warm(args, settings=None, settings_path=contracts.NS_SETTINGS_PATH, log_msg=print):
    """`hc warm`: returns the exit code (0 ok, 2 failure, 3 refused). cli.py passes `args` only."""
    try:
        results = warm_days(args.days, url=args.url, settings=settings, allow_partial=args.allow_partial, log_msg=log_msg,
                            settings_path=settings_path)
    except (FreezeError, ValueError) as e:
        print(f'hc warm: {e}', file=sys.stderr)
        return EXIT_REFUSED
    for day, outcome, detail in results:
        print(f'{day}: {outcome} - {detail}')
    kinds = {r[1] for r in results}
    if 'failed' in kinds:
        return EXIT_GUARD
    if 'refused' in kinds:
        return EXIT_REFUSED
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hindcast.freeze', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    register(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
