"""The songs of a Spotify or SoundCloud playlist, from their public pages.

No account anywhere, the same pages a browser gets:

  · Spotify: the playlist's EMBED page (open.spotify.com/embed/playlist/ID)
    carries the track list as JSON. ⚠ It stops at 100 songs — `capped` says
    when a playlist reached that, so the screen can say the rest was not seen.
  · SoundCloud: the playlist page carries its first few songs in full and only
    the ids of the rest; those are asked for the way its own web player asks,
    with the public client id its scripts ship (cached next to the settings,
    looked up again when it stops working). A private playlist needs its
    share link (the one with /s-xxxx at the end).

Nothing is ever written to either service.
"""

import json
import os
import re
import urllib.parse
import urllib.request

import settings

UA = {'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
                     '(KHTML, like Gecko) Version/17.6 Safari/605.1.15'),
      'Accept-Language': 'en'}
SC_CLIENT = os.path.join(settings.APP_DIR, 'soundcloud-client.json')


class PlaylistError(RuntimeError):
    pass


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode('utf-8', 'replace')


def _split_dash(title, artist):
    m = re.match(r'^(.+?)\s+[-–—]\s+(.+)$', title or '')
    return (m.group(1).strip(), m.group(2).strip()) if m else ((artist or '').strip(), (title or '').strip())


def read(text):
    """→ {url, source, name, capped, tracks: [{title, artist, url}]}"""
    m = re.search(r'https?://\S+', str(text or ''))
    if not m:
        raise PlaylistError('That is not a link.')
    url = m.group(0).rstrip(').,;]')
    host = urllib.parse.urlparse(url).hostname or ''
    # Short links (spotify.link, on.soundcloud.com) lead to the real page.
    if re.search(r'spotify\.link|app\.link|on\.soundcloud\.com', host):
        final, html = _get(url)
        m = re.search(r'https://(?:open\.spotify\.com/(?:intl-[a-z]+/)?playlist/[A-Za-z0-9]+'
                      r'|(?:m\.)?soundcloud\.com/[^\s"\'?#]+/sets/[^\s"\'?#]+)', final + ' ' + html[:200000])
        if m:
            url = m.group(0).replace('://m.', '://')
            host = urllib.parse.urlparse(url).hostname or ''
    if 'spotify' in host:
        return _spotify(url)
    if 'soundcloud' in host:
        return _soundcloud(url)
    raise PlaylistError('Only Spotify and SoundCloud playlists can be checked.')


def _spotify(url):
    m = re.search(r'/playlist/([A-Za-z0-9]+)', url)
    if not m:
        raise PlaylistError('That is not a Spotify playlist link.')
    pid = m.group(1)
    try:
        _, html = _get('https://open.spotify.com/embed/playlist/' + pid)
    except Exception as e:
        raise PlaylistError('Spotify did not open it (%s). Is the playlist public?' % str(e)[:60])
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    try:
        ent = json.loads(m.group(1))['props']['pageProps']['state']['data']['entity']
        rows = ent['trackList']
    except Exception:
        raise PlaylistError('Spotify did not list its songs. Is the playlist public?')
    tracks = []
    for t in rows:
        uri = t.get('uri') or ''
        if not uri.startswith('spotify:track:'):
            continue
        tracks.append({'key': 'sp:' + uri.split(':')[2], 'title': t.get('title') or '',
                       'artist': (t.get('subtitle') or '').replace(' ', ' '),
                       'url': 'https://open.spotify.com/track/' + uri.split(':')[2]})
    return {'url': 'https://open.spotify.com/playlist/' + pid, 'source': 'Spotify',
            'name': ent.get('name') or ent.get('title') or 'Spotify playlist',
            'capped': len(rows) >= 100, 'tracks': tracks}


def _sc_client_id(fresh=False):
    if not fresh:
        try:
            with open(SC_CLIENT) as fh:
                cid = json.load(fh).get('id')
            if cid:
                return cid
        except Exception:
            pass
    _, html = _get('https://soundcloud.com/')
    scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
    for s in reversed(scripts):
        try:
            _, js = _get(s)
        except Exception:
            continue
        m = re.search(r'client_id[:=]"([A-Za-z0-9]{32})"', js)
        if m:
            os.makedirs(settings.APP_DIR, exist_ok=True)
            with open(SC_CLIENT, 'w') as fh:
                json.dump({'id': m.group(1)}, fh)
            return m.group(1)
    raise PlaylistError('SoundCloud could not be read right now.')


def _sc_track(t):
    who = (t.get('user') or {}).get('username') or ''
    artist, title = _split_dash(t.get('title') or '', who)
    return {'key': 'sc:%s' % t.get('id'), 'title': title, 'artist': artist,
            'url': t.get('permalink_url') or ''}


def _soundcloud(url):
    url = url.split('#')[0]
    try:
        _, html = _get(url)
    except Exception as e:
        raise PlaylistError('SoundCloud did not open it (%s). A private playlist needs its share link.' % str(e)[:60])
    m = re.search(r'window\.__sc_hydration = (\[.*?\]);</script>', html, re.S)
    pl = None
    if m:
        for x in json.loads(m.group(1)):
            if x.get('hydratable') == 'playlist':
                pl = x.get('data')
    if not pl or not isinstance(pl.get('tracks'), list):
        raise PlaylistError('That is not a SoundCloud playlist.')
    full = {t['id']: t for t in pl['tracks'] if t.get('title') and t.get('permalink_url')}
    missing = [t['id'] for t in pl['tracks'] if t['id'] not in full]
    for fresh in (False, True):
        if not missing:
            break
        cid = _sc_client_id(fresh)
        try:
            for i in range(0, len(missing), 50):
                q = urllib.parse.urlencode({'ids': ','.join(str(x) for x in missing[i:i + 50]),
                                            'client_id': cid})
                _, body = _get('https://api-v2.soundcloud.com/tracks?' + q)
                for t in json.loads(body):
                    full[t['id']] = t
            missing = [x for x in missing if x not in full]
        except Exception:
            continue             # a stale client id: look it up again
    tracks = [_sc_track(full[t['id']]) for t in pl['tracks'] if t['id'] in full]
    return {'url': pl.get('permalink_url') or url, 'source': 'SoundCloud',
            'name': pl.get('title') or 'SoundCloud playlist', 'capped': False,
            'tracks': tracks, 'unread': len(missing)}
