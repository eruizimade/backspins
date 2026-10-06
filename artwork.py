"""Covers for the tracks that have none in rekordbox.

Three steps, and only the last one writes anything:

  1. `missing()`  — the tracks whose ImagePath is empty (read on a copy).
  2. `find()`     — for each, the covers Deezer and Apple Music have for the
     same artist and title, scored with the library's own matcher. Deezer
     first (a generous rate limit); Apple Music only when Deezer found
     nothing sure, because it allows ~20 searches a minute. Results are kept
     in artwork-found.json, so a second run only asks about what is new.
  3. `apply()`    — the covers you ticked are downloaded to a staging folder
     and queued as `set_artwork`: they land in rekordbox (its own three JPEGs,
     tagger._write_rb_artwork) when rekordbox is closed, and never on a track
     that got a cover in the meantime. The audio files are not touched.

No account anywhere: both are public search APIs.
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import library_search as lsr
import rekordbox as rb
import settings

FOUND_FILE = os.path.join(settings.APP_DIR, 'artwork-found.json')
STAGING = os.path.join(settings.APP_DIR, 'artwork-staging')
UA = {'User-Agent': 'rekordbox-toolkit/1.0 (personal library covers)'}
SURE = 0.85           # at or above: ticked for you
FLOOR = 0.55          # below: not a candidate at all

_LOCK = threading.Lock()
STATE = {'phase': 'idle', 'done': 0, 'total': 0, 'error': None, 'stop': False}


def _read_found():
    try:
        with open(FOUND_FILE, encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _write_found(d):
    os.makedirs(settings.APP_DIR, exist_ok=True)
    tmp = FOUND_FILE + '.part'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.replace(tmp, FOUND_FILE)


def _queued_ids():
    import tagger
    return {str(i.get('track')) for i in tagger._read_queue() if i.get('op') == 'set_artwork'}


def missing():
    """[{id, title, artist}] — live tracks with no artwork in rekordbox."""
    with rb.open_library() as cur:
        if cur is None:
            return []
        rows = cur.execute(
            "SELECT c.ID, c.Title, a.Name FROM djmdContent c "
            "LEFT JOIN djmdArtist a ON a.ID = c.ArtistID "
            "WHERE c.rb_local_deleted = 0 AND (c.ImagePath IS NULL OR c.ImagePath = '')").fetchall()
    return [{'id': str(r[0]), 'title': r[1] or '', 'artist': r[2] or ''} for r in rows]


def _get_json(url, timeout=12):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _query(t):
    title = lsr.base_title(t['title']) or t['title']
    artist = re.split(r'\s*(?:,|&| feat\.? | ft\.? | x | vs\.? )\s*', t['artist'] or '', flags=re.I)[0]
    return (artist + ' ' + title).strip()


def _deezer(t):
    j = _get_json('https://api.deezer.com/search?' + urllib.parse.urlencode({'q': _query(t), 'limit': 8}))
    out = []
    for r in j.get('data') or []:
        alb = r.get('album') or {}
        if not alb.get('cover_xl'):
            continue
        out.append({'source': 'Deezer', 'title': r.get('title') or '',
                    'artist': (r.get('artist') or {}).get('name') or '',
                    'album': alb.get('title') or '', 'url': alb['cover_xl'],
                    'thumb': alb.get('cover_medium') or alb['cover_xl']})
    return out


_ITUNES_LAST = [0.0]


def _itunes(t):
    # ⚠ ~20 searches a minute, or it answers 403 for a while: one every 3.2 s.
    wait = _ITUNES_LAST[0] + 3.2 - time.time()
    if wait > 0:
        time.sleep(wait)
    _ITUNES_LAST[0] = time.time()
    j = _get_json('https://itunes.apple.com/search?' + urllib.parse.urlencode(
        {'term': _query(t), 'media': 'music', 'entity': 'song', 'limit': 8}))
    out = []
    for r in j.get('results') or []:
        a = r.get('artworkUrl100') or ''
        if not a:
            continue
        out.append({'source': 'Apple Music', 'title': r.get('trackName') or '',
                    'artist': r.get('artistName') or '', 'album': r.get('collectionName') or '',
                    'url': a.replace('100x100bb', '1000x1000bb'),
                    'thumb': a.replace('100x100bb', '300x300bb')})
    return out


# A compilation's cover is the compiler's, not the song's: ranked after a
# real release with the same score, and never ticked for you.
_COMPILATION = re.compile(
    r'\b(vol(ume)?\.?\s*\d+|various|compilation|best of|hits|the collection|anthems|essentials|'
    r'selected|presents|sampler|playlist|top \d+|\d{2,4}\s*$|^\d{2,3}\b|mixed by|dj mix)\b', re.I)


def _score(t, cands):
    seen, out = set(), []
    for c in cands:
        if c['url'] in seen:
            continue
        seen.add(c['url'])
        c['score'] = round(lsr.similarity(t['title'], t['artist'], c['title'], c['artist']), 2)
        # An edit or a remix has no cover of its own: the original's is the
        # best there is, and the screen says that is what it is.
        c['sameVersion'] = lsr.version_of(t['title']) == lsr.version_of(c['title'])
        c['compilation'] = bool(_COMPILATION.search(c.get('album') or '')) and \
            lsr.fold(c.get('album') or '') != lsr.fold(c.get('title') or '')
        if c['score'] >= FLOOR:
            out.append(c)
    out.sort(key=lambda c: (-c['score'], c['compilation'], not c['sameVersion']))
    return out[:4]


def find_one(t):
    cands = []
    try:
        cands = _score(t, _deezer(t))
    except Exception:
        pass
    if not cands or cands[0]['score'] < SURE:
        try:
            cands = _score(t, cands + _itunes(t))
        except Exception:
            pass
    return cands


def start_find(tracks=None):
    """Look up covers in the background. Returns at once."""
    with _LOCK:
        if STATE['phase'] == 'finding':
            return False
        STATE.update(phase='finding', done=0, total=0, error=None, stop=False)
    threading.Thread(target=_find_worker, args=(tracks,), daemon=True).start()
    return True


def _find_worker(tracks):
    try:
        todo = tracks if tracks is not None else missing()
        found = _read_found()
        todo = [t for t in todo if t['id'] not in found]
        STATE['total'] = len(todo)
        for i, t in enumerate(todo):
            if STATE['stop']:
                break
            found[t['id']] = {'title': t['title'], 'artist': t['artist'],
                              'cands': find_one(t), 'at': time.strftime('%Y-%m-%d')}
            STATE['done'] = i + 1
            if (i + 1) % 10 == 0:
                _write_found(found)
            time.sleep(0.15)          # Deezer allows 50 in 5 s; stay well under
        _write_found(found)
        STATE['phase'] = 'done'
    except Exception as e:
        STATE.update(phase='error', error=str(e)[:200])


def view():
    """What the screen shows: every track still without a cover."""
    miss = missing()
    found = _read_found()
    queued = _queued_ids()
    items = []
    for t in miss:
        f = found.get(t['id'])
        items.append({'id': t['id'], 'title': t['title'], 'artist': t['artist'],
                      'looked': f is not None, 'cands': (f or {}).get('cands') or [],
                      'queued': t['id'] in queued})
    return {'items': items, 'state': dict(STATE), 'sure': SURE}


def apply(picks):
    """picks: {track_id: cover_url}. Downloads and queues each; applies if it can."""
    import tagger
    os.makedirs(STAGING, exist_ok=True)
    ops, failed = [], []
    for tid, url in (picks or {}).items():
        tid = re.sub(r'[^0-9]', '', str(tid))
        if not tid or not re.match(r'^https://', str(url or '')):
            continue
        dst = os.path.join(STAGING, tid + '.jpg')
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                data = r.read()
            if len(data) < 2000:
                raise RuntimeError('not an image')
            with open(dst, 'wb') as fh:
                fh.write(data)
            ops.append({'op': 'set_artwork', 'track': tid, 'value': dst})
        except Exception as e:
            failed.append({'id': tid, 'why': str(e)[:100]})
    if ops:
        tagger.enqueue(ops)
    res = tagger.apply_now_if_possible() if ops else {'applied': 0, 'deferred': False}
    return {'queued': len(ops), 'failed': failed, 'applied': res.get('applied', 0),
            'deferred': res.get('deferred', False), 'why': res.get('why', '')}
