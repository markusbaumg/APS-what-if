"""
ns_client.py  -  minimal Nightscout REST (API v1) reader with a per-day disk cache.

Only the Python standard library is used (urllib), so it also runs on the phone
under QPython.  All timestamps handed in or out are UTC.

Collections
    entries       CGM values          keyed by  date        (unix ms)
    treatments    boluses, temps, TT  keyed by  created_at  (ISO string)
    devicestatus  loop results        keyed by  created_at  (ISO string)
    profile       profile store       (small; fetched whole)

Cache layout      <cache_dir>/<host>/<collection>/<YYYY-MM-DD>.json   (one UTC day per file)
A day file is final once it was WRITTEN after the day was over (plus an hour for late
uploads); a file written while the day was still running is partial and is refetched
when older than `refresh_minutes`.  `python ns_client.py ... --refresh` forces a refetch.
`latest_treatment()` answers (one record) land in <cache_dir>/<host>/latest/ under the same rule.

Typical use
    ns = NightscoutClient('https://ns.vivikusi.ch')
    ds = ns.devicestatus('2026-09-06T00:00:00Z', '2026-09-07T00:00:00Z')   # ascending list
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

NS_TIME_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'

_re_secret = re.compile(r'((?:token|api[-_]secret(?:_sha1)?|secret)=)[^&\s\'"]*', re.IGNORECASE)


def redact(text):
    """hide the token / api secret query values in an URL or error text ('token=abc' -> 'token=***')"""
    return _re_secret.sub(r'\1***', str(text))


# ----------------------------------------------------------------------------- time helpers
def parse_iso(stamp):
    """ISO-8601 string (with Z or offset, optional fraction) -> unix milliseconds (int)."""
    s = stamp.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    # python accepts up to 6 fractional digits
    if '.' in s:
        head, tail = s.split('.', 1)
        # tail = fraction + offset
        frac = ''
        i = 0
        while i < len(tail) and tail[i].isdigit():
            frac += tail[i]
            i += 1
        rest = tail[i:]
        frac = (frac + '000000')[:6]
        s = head + '.' + frac + rest
    if len(s) == 10:                       # date only
        s += 'T00:00:00+00:00'
    elif '+' not in s[10:] and '-' not in s[10:]:
        s += '+00:00'
    dt = datetime.fromisoformat(s)
    return int(dt.timestamp() * 1000)


def to_iso(ms):
    """unix milliseconds -> Nightscout style ISO string  2026-09-07T19:33:23.140Z"""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def utc_day(ms):
    """unix ms -> 'YYYY-MM-DD' of that instant in UTC"""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d')


def day_bounds(day):
    """'YYYY-MM-DD' -> (start_ms, end_ms) of that UTC day, end exclusive"""
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _record_ms(rec, key):
    """timestamp of a record in unix ms, tolerant to the various NS fields"""
    if key == 'date':
        v = rec.get('date')
        if isinstance(v, (int, float)):
            return int(v)
        if 'mills' in rec:
            return int(rec['mills'])
        if 'dateString' in rec:
            return parse_iso(rec['dateString'])
    v = rec.get('created_at')
    if isinstance(v, str):
        return parse_iso(v)
    if 'mills' in rec:
        return int(rec['mills'])
    if isinstance(rec.get('date'), (int, float)):
        return int(rec['date'])
    return 0


# ----------------------------------------------------------------------------- the client
class NightscoutClient:
    COLLECTIONS = {
        #  name          url path                     time key
        'entries':      ('/api/v1/entries/sgv.json', 'date'),
        'treatments':   ('/api/v1/treatments.json',  'created_at'),
        'devicestatus': ('/api/v1/devicestatus.json', 'created_at'),
    }
    PAGE = 1000

    def __init__(self, base_url, cache_dir=None, token=None, api_secret_sha1=None,
                 refresh_minutes=5, verbose=True, timeout=60):
        self.base_url = base_url.rstrip('/')
        if not self.base_url.startswith('http'):
            self.base_url = 'https://' + self.base_url
        host = urllib.parse.urlparse(self.base_url).netloc.replace(':', '_')
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ns_cache')
        self.cache_dir = os.path.join(cache_dir, host)
        self.token = token
        self.api_secret_sha1 = api_secret_sha1
        self.refresh_minutes = refresh_minutes
        self.verbose = verbose
        self.timeout = timeout

    # ------------------------------------------------------------------ low level
    def _get(self, path, params):
        q = dict(params)
        if self.token:
            q['token'] = self.token
        url = self.base_url + path + '?' + urllib.parse.urlencode(q, safe='[]$:')
        req = urllib.request.Request(url, headers={'Accept': 'application/json',
                                                   'User-Agent': 'APS-emulator-ns/1.0'})
        if self.api_secret_sha1:
            req.add_header('api-secret', self.api_secret_sha1)
        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode('utf-8'))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError,
                    http.client.HTTPException,          # IncompleteRead: connection dropped mid-body
                    json.JSONDecodeError,               # truncated / garbled body
                    OSError) as e:                      # ssl.SSLError, socket errors
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        # the token travels in the query string: never let it reach a log line
        raise RuntimeError(redact(f'Nightscout request failed after retries: {url}\n{last_err!r}'))

    def _page_window(self, coll, start_ms, end_ms):
        """download all records of one collection in [start_ms, end_ms) - ascending, de-duplicated"""
        path, key = self.COLLECTIONS[coll]
        out = {}
        hi = end_ms                                             # exclusive upper bound, walks backwards
        while True:
            if key == 'date':
                params = {'count': self.PAGE, 'find[date][$gte]': start_ms, 'find[date][$lt]': hi}
            else:
                params = {'count': self.PAGE, 'find[created_at][$gte]': to_iso(start_ms), 'find[created_at][$lt]': to_iso(hi)}
            batch = self._get(path, params)
            if not isinstance(batch, list) or len(batch) == 0:
                break
            new = 0
            oldest = hi
            for rec in batch:
                ident = rec.get('_id') or rec.get('identifier') or (str(_record_ms(rec, key)) + rec.get('eventType', ''))
                if ident not in out:
                    out[ident] = rec
                    new += 1
                ms = _record_ms(rec, key)
                if ms < oldest:
                    oldest = ms
            if len(batch) < self.PAGE or new == 0:
                break
            # next page: everything strictly older than the oldest we have; records sharing that exact
            # millisecond would be lost, so step back by one ms and let the de-duplication handle overlaps
            hi = oldest + 1
            if hi >= end_ms:                                    # no progress possible
                break
        recs = list(out.values())
        recs.sort(key=lambda r: _record_ms(r, key))
        return recs

    # ------------------------------------------------------------------ cache
    def _day_file(self, coll, day):
        d = os.path.join(self.cache_dir, coll)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, day + '.json')

    SETTLE_MS = 60 * 60 * 1000                          # give late uploads an hour after the day / window end

    def _load_day(self, coll, day):
        fn = self._day_file(coll, day)
        start_ms, end_ms = day_bounds(day)
        now_ms = int(time.time() * 1000)
        if os.path.exists(fn):
            mtime_ms = os.path.getmtime(fn) * 1000
            age_min = (now_ms - mtime_ms) / 60000
            # final only if the file was written after the day was over (a file fetched during the day holds
            # the records up to the fetch time and temp basals that had not ended yet)
            is_final = mtime_ms > end_ms + self.SETTLE_MS
            if is_final or age_min < self.refresh_minutes:
                try:
                    with open(fn, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass                                        # corrupt -> refetch
        if self.verbose:
            print(f'  Nightscout: fetching {coll} for {day} ...', end='', flush=True)
        recs = self._page_window(coll, start_ms, min(end_ms, now_ms + 60000))
        if self.verbose:
            print(f' {len(recs)} records')
        with open(fn, 'w', encoding='utf-8') as f:
            json.dump(recs, f)
        return recs

    def fetch(self, coll, start, end):
        """records of `coll` with timestamp in [start, end); start/end = ISO string or unix ms. Ascending."""
        start_ms = start if isinstance(start, (int, float)) else parse_iso(start)
        end_ms = end if isinstance(end, (int, float)) else parse_iso(end)
        key = self.COLLECTIONS[coll][1]
        out = []
        day = utc_day(start_ms)
        last_day = utc_day(end_ms - 1)
        while True:
            for rec in self._load_day(coll, day):
                ms = _record_ms(rec, key)
                if start_ms <= ms < end_ms:
                    out.append(rec)
            if day >= last_day:
                break
            day = (datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        out.sort(key=lambda r: _record_ms(r, key))
        return out

    # convenience wrappers
    def entries(self, start, end):
        return self.fetch('entries', start, end)

    def treatments(self, start, end):
        return self.fetch('treatments', start, end)

    def latest_treatment(self, event_type, before):
        """the newest treatment of `event_type` created before `before` (ISO string or unix ms), or None.
        One count=1 query; ns_source uses it for the Profile Switch / Temporary Target that may have started long
        before a replay window. Cached under <cache_dir>/latest/: final once written an hour after `before`
        (nothing older can appear any more), a younger file is refetched after refresh_minutes."""
        before_ms = before if isinstance(before, (int, float)) else parse_iso(before)
        slug = re.sub(r'[^A-Za-z0-9]+', '_', event_type).strip('_')
        fn = self._day_file('latest', f'{slug}_{to_iso(before_ms)[:19].replace(":", "-")}')
        now_ms = int(time.time() * 1000)
        if os.path.exists(fn):
            mtime_ms = os.path.getmtime(fn) * 1000
            if mtime_ms > before_ms + self.SETTLE_MS or (now_ms - mtime_ms) / 60000 < self.refresh_minutes:
                try:
                    with open(fn, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass                                        # corrupt -> refetch
        if self.verbose:
            print(f'  Nightscout: fetching last "{event_type}" before {to_iso(before_ms)} ...', end='', flush=True)
        path, _ = self.COLLECTIONS['treatments']
        # a few records, newest first: a treatment deleted in AAPS stays in Nightscout with isValid false and
        # would otherwise hide the one still running underneath it (ProfileBuilder drops isValid false too)
        recs = self._get(path, {'count': 5, 'find[eventType]': event_type, 'find[created_at][$lt]': to_iso(before_ms)})
        rec = None
        if isinstance(recs, list):
            for r in recs:
                if isinstance(r, dict) and r.get('isValid', True) is not False:
                    rec = r
                    break
        if self.verbose:
            print(f' {rec.get("created_at") if rec else "none"}')
        with open(fn, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        return rec

    def devicestatus(self, start, end):
        return self.fetch('devicestatus', start, end)

    def profiles(self, count=50, newer_than=None):
        """all profile store documents, newest first (as Nightscout returns them); cached for refresh_minutes.
        newer_than (unix ms): a cache file written more than an hour after that instant is served regardless of
        its age - the store can only gain documents after the window a replay is interested in."""
        fn = self._day_file('profile', 'all')
        now_ms = int(time.time() * 1000)
        cached_ok = False
        if os.path.exists(fn):
            mtime_ms = os.path.getmtime(fn) * 1000
            cached_ok = (now_ms - mtime_ms) / 60000 < self.refresh_minutes
            if newer_than is not None and mtime_ms > newer_than + self.SETTLE_MS:
                cached_ok = True
        if cached_ok:
            try:
                with open(fn, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        if self.verbose:
            print('  Nightscout: fetching profile store ...', end='', flush=True)
        recs = self._get('/api/v1/profile.json', {'count': count})
        if self.verbose:
            print(f' {len(recs)} documents')
        with open(fn, 'w', encoding='utf-8') as f:
            json.dump(recs, f)
        return recs

    def status(self):
        return self._get('/api/v1/status.json', {})

    def refresh(self, coll, start, end):
        """delete the cached day files of `coll` covering [start, end) so the next fetch downloads them again;
        returns the deleted file names"""
        start_ms = start if isinstance(start, (int, float)) else parse_iso(start)
        end_ms = end if isinstance(end, (int, float)) else parse_iso(end)
        gone = []
        if coll == 'treatments':                                   # the pre-window Profile Switch / TT lookups too
            latest_dir = os.path.dirname(self._day_file('latest', 'x'))
            if os.path.isdir(latest_dir):
                for name in sorted(os.listdir(latest_dir)):
                    fn = os.path.join(latest_dir, name)
                    os.remove(fn)
                    gone.append(fn)
        if coll == 'profile':
            days = ['all']
        else:
            days = []
            day = utc_day(start_ms)
            last_day = utc_day(end_ms - 1)
            while True:
                days.append(day)
                if day >= last_day:
                    break
                day = (datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        for day in days:
            fn = self._day_file(coll, day)
            if os.path.exists(fn):
                os.remove(fn)
                gone.append(fn)
        return gone


# ----------------------------------------------------------------------------- CLI: prefetch / inspect
def _client_from_settings(url):
    """'ns:' (or the url given in ns_settings.json) -> client with token / api secret / cache dir from the settings"""
    cfg = {}
    try:
        import ns_source
        cfg = ns_source.load_settings().get('nightscout', {}) or {}
    except (ImportError, OSError, ValueError):
        pass
    if url in ('ns', 'ns:', '') or url.startswith('ns:'):
        url = cfg.get('url') or ''
        if not url:
            sys.exit('ns_client: "ns:" needs nightscout.url in ns_settings.json')
    use_cfg = url.rstrip('/') == str(cfg.get('url') or '').rstrip('/')
    return NightscoutClient(url, cache_dir=cfg.get('cache_dir') if use_cfg else None,
                            token=cfg.get('token') if use_cfg else None,
                            api_secret_sha1=cfg.get('api_secret_sha1') if use_cfg else None)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = [a for a in sys.argv[1:] if a.startswith('--')]
    unknown = [f for f in flags if f != '--refresh']
    if len(args) < 3 or unknown:
        print('usage: python ns_client.py <nightscout url | ns:> <start ISO> <end ISO> [collection ...] [--refresh]\n'
              '       downloads the window into the cache and prints record counts\n'
              '       ns:        url, token and cache dir from ns_settings.json\n'
              '       --refresh  delete the cached day files of the window (and the profile store) first')
        sys.exit(1)
    url, start, end = args[:3]
    colls = args[3:] or ['entries', 'treatments', 'devicestatus']
    ns = _client_from_settings(url)
    if '--refresh' in flags:
        gone = []
        for c in colls + ['profile']:
            gone += ns.refresh(c, start, end)
        print(f'refresh: {len(gone)} cache file(s) deleted')
    for c in colls:
        recs = ns.fetch(c, start, end)
        print(f'{c:>13}: {len(recs)} records', end='')
        if recs:
            key = ns.COLLECTIONS[c][1]
            print(f'   {to_iso(_record_ms(recs[0], key))} .. {to_iso(_record_ms(recs[-1], key))}')
        else:
            print()
    prof = ns.profiles(newer_than=parse_iso(end))
    print(f'{"profile":>13}: {len(prof)} documents, newest default = {prof[0].get("defaultProfile") if prof else None}')
