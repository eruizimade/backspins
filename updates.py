"""Is there a newer Backspins? Asked of GitHub, never told by anyone else.

The packaged app knows its own version (version.py, written at release time).
This asks GitHub's public API which release is the latest — a single
anonymous GET that sends nothing about you or your library — and remembers
the answer, so the app can say "v0.1.5 is out" and link to the download.

Checked a minute after start and every few hours while the app runs, unless
turned off in Settings (`check_updates`); the Settings screen can also ask
right now. Run from source ("dev") it still reports the latest release, but
never claims there is an update: a source folder is updated with git.
"""

import json
import os
import re
import threading
import time
import urllib.request

import settings
from version import VERSION

REPO = 'eruizimade/backspins'
API = 'https://api.github.com/repos/%s/releases/latest' % REPO
PAGE = 'https://github.com/%s/releases/latest' % REPO
EVERY = 6 * 3600
CACHE = os.path.join(settings.APP_DIR, 'update.json')
_LOCK = threading.Lock()


def _parse(v):
    """'v0.1.4' → (0, 1, 4); anything else → None."""
    m = re.match(r'^v?(\d+)\.(\d+)\.(\d+)$', str(v or '').strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _read():
    try:
        with open(CACHE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def status():
    """What the app shows: this version, the latest known, and whether to update."""
    st = _read()
    mine, latest = _parse(VERSION), _parse(st.get('latest'))
    return {'version': VERSION, 'latest': st.get('latest'), 'checked': st.get('checked'),
            'error': st.get('error'), 'url': st.get('url') or PAGE,
            'auto': bool(settings.load().get('check_updates', True)),
            'source': mine is None,
            'available': bool(mine and latest and latest > mine)}


def check():
    """Ask GitHub now. Returns status(); a failure is remembered, not raised."""
    with _LOCK:
        st = _read()
        try:
            req = urllib.request.Request(API, headers={'Accept': 'application/vnd.github+json',
                                                       'User-Agent': 'Backspins/' + VERSION})
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read().decode('utf-8'))
            tag = j.get('tag_name') or ''
            if not _parse(tag):
                raise ValueError('unexpected answer from GitHub')
            url = j.get('html_url') or PAGE
            # ⚠ Only ever this repository's own pages: this link is opened in
            # your browser, and an answer must not be able to send you elsewhere.
            if not url.startswith('https://github.com/%s/' % REPO):
                url = PAGE
            st = {'latest': tag, 'url': url, 'published': j.get('published_at'),
                  'checked': time.strftime('%Y-%m-%dT%H:%M:%S'), 'error': None}
        except Exception as e:
            st.update(checked=time.strftime('%Y-%m-%dT%H:%M:%S'), error=str(e)[:160])
        try:
            os.makedirs(settings.APP_DIR, exist_ok=True)
            with open(CACHE, 'w', encoding='utf-8') as fh:
                json.dump(st, fh)
        except OSError:
            pass
    return status()


def _loop():
    time.sleep(60)                      # not while the app is still opening
    while True:
        try:
            if settings.load().get('check_updates', True):
                last = _read().get('checked') or ''
                age = time.time() - time.mktime(time.strptime(last, '%Y-%m-%dT%H:%M:%S')) if last else EVERY
                if age >= EVERY:
                    check()
        except Exception:
            pass
        time.sleep(1800)


def start():
    threading.Thread(target=_loop, daemon=True).start()
