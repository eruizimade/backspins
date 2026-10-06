"""Pairs (a Spotify / SoundCloud song ↔ a track in your library) and the
streaming playlists linked to a rekordbox playlist.

PAIRS (pairs.json) are the answer you gave once and should never have to give
again: «this Spotify song IS that track of mine». A pair always wins over the
automatic match — it exists precisely to correct it.

LINKS (linked-playlists.json): a streaming playlist and the rekordbox
playlist it feeds. Updating a link only ever ADDS, and only songs it has
never put in before (`added`):

  · a track in the rekordbox playlist that is not on Spotify (not available
    there, or added by hand) is never touched;
  · a track you took OUT of the rekordbox playlist does not come back;
  · a Spotify song you do not have yet is simply left for the next update —
    once you download it (or pair it) it goes in.

Everything that writes rekordbox goes through the queue (tagger): it lands
when rekordbox is closed, like every other edit.
"""

import json
import os
import threading
import time
import uuid

import settings

PAIRS_FILE = os.path.join(settings.APP_DIR, 'pairs.json')
LINKS_FILE = os.path.join(settings.APP_DIR, 'linked-playlists.json')
_LOCK = threading.Lock()


def _read(path, empty):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh) or empty
    except Exception:
        return empty


def _write(path, data):
    os.makedirs(settings.APP_DIR, exist_ok=True)
    tmp = '%s.%d.part' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


# ── pairs ─────────────────────────────────────────────────────────────────
def pairs():
    return _read(PAIRS_FILE, {})


def set_pair(key, track_id, title='', artist='', on=True):
    with _LOCK:
        cur = pairs()
        if on:
            cur[key] = {'track': str(track_id), 'title': title, 'artist': artist,
                        'at': time.strftime('%Y-%m-%d %H:%M')}
        else:
            cur.pop(key, None)
        _write(PAIRS_FILE, cur)
        return cur


# ── links ─────────────────────────────────────────────────────────────────
def links():
    return _read(LINKS_FILE, [])


def link_for(url):
    return next((l for l in links() if l.get('url') == url), None)


def _save_link(link):
    with _LOCK:
        cur = [l for l in links() if l.get('id') != link['id']]
        cur.append(link)
        _write(LINKS_FILE, cur)


def unlink(link_id):
    with _LOCK:
        _write(LINKS_FILE, [l for l in links() if l.get('id') != link_id])


def sync(pl, resolved, link=None, create_name=None, existing_playlist=None):
    """Queue what this playlist adds to its rekordbox playlist.

    pl:        the playlist as playlist_read returns it (keys in its order).
    resolved:  {song key: library track id} — paired or surely matched.
    Returns the link and what was queued.
    """
    import tagger
    ops = []
    if link is None:
        link = {'id': uuid.uuid4().hex, 'url': pl['url'], 'source': pl['source'],
                'name': pl['name'], 'added': [], 'created': time.strftime('%Y-%m-%d %H:%M')}
        if existing_playlist:
            link['rbPlaylist'] = str(existing_playlist)
        else:
            key = uuid.uuid4().hex[:12]
            link['rbPlaylist'] = 'new:' + key
            ops.append({'op': 'playlist_create', 'track': 'pl:' + key,
                        'playlist': 'new:' + key, 'value': create_name or pl['name']})
    done = set(link.get('added') or [])
    new_keys = []
    for t in pl['tracks']:
        k = t.get('key')
        tid = resolved.get(k)
        if not k or not tid or k in done:
            continue
        ops.append({'op': 'playlist_add', 'track': str(tid), 'playlist': link['rbPlaylist']})
        new_keys.append(k)
        done.add(k)
    if ops:
        tagger.enqueue(ops)
    link['added'] = list(done)
    link['name'] = pl['name']
    link['lastSync'] = time.strftime('%Y-%m-%d %H:%M')
    _save_link(link)
    res = tagger.apply_now_if_possible() if ops else {'applied': 0, 'deferred': False}
    return {'link': link, 'added': len(new_keys), 'deferred': res.get('deferred', False),
            'why': res.get('why', '')}
