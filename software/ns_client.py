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
A day file is final once the day is over; the current day is refreshed when older
than `refresh_minutes`.

Typical use
    ns = NightscoutClient('https://ns.vivikusi.ch')
    ds = ns.devicestatus('2026-09-06T00:00:00Z', '2026-09-07T00:00:00Z')   # ascending list
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

NS_TIME_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'


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
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f'Nightscout request failed after retries: {url}\n{last_err}')

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

    def _load_day(self, coll, day):
        fn = self._day_file(coll, day)
        start_ms, end_ms = day_bounds(day)
        now_ms = int(time.time() * 1000)
        if os.path.exists(fn):
            age_min = (now_ms - os.path.getmtime(fn) * 1000) / 60000
            day_over = end_ms < now_ms - 60 * 60 * 1000       # give uploads an hour to settle
            if day_over or age_min < self.refresh_minutes:
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

    def devicestatus(self, start, end):
        return self.fetch('devicestatus', start, end)

    def profiles(self, count=50):
        """all profile store documents, newest first (as Nightscout returns them); cached for refresh_minutes"""
        fn = self._day_file('profile', 'all')
        now_ms = int(time.time() * 1000)
        if os.path.exists(fn) and (now_ms - os.path.getmtime(fn) * 1000) / 60000 < self.refresh_minutes:
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


# ----------------------------------------------------------------------------- CLI: prefetch / inspect
if __name__ == '__main__':
    if len(sys.argv) < 4:
        print('usage: python ns_client.py <nightscout url> <start ISO> <end ISO> [collection ...]\n'
              '       downloads the window into the cache and prints record counts')
        sys.exit(1)
    url, start, end = sys.argv[1:4]
    colls = sys.argv[4:] or ['entries', 'treatments', 'devicestatus']
    ns = NightscoutClient(url)
    for c in colls:
        recs = ns.fetch(c, start, end)
        print(f'{c:>13}: {len(recs)} records', end='')
        if recs:
            key = ns.COLLECTIONS[c][1]
            print(f'   {to_iso(_record_ms(recs[0], key))} .. {to_iso(_record_ms(recs[-1], key))}')
        else:
            print()
    prof = ns.profiles()
    print(f'{"profile":>13}: {len(prof)} documents, newest default = {prof[0].get("defaultProfile") if prof else None}')
