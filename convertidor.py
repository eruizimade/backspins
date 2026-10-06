#!/usr/bin/env python3
"""Backspins — a local server with a web UI (convertidor.html).

The Convert tab scans a folder (recursively) for music files, renames them to
"Title - Artist" ONLY when both tags exist, converts FLAC to AIFF keeping tags
and artwork, and copies everything else into the output folder. THE ORIGINALS
ARE NEVER TOUCHED.

The other tabs read your rekordbox library — always from a copy — to find
duplicates, rate quality, check player compatibility and build sets.
"""

import importlib.util
import os
import subprocess
import sys

# Packaged (PyInstaller): the pages and modules are unpacked in sys._MEIPASS,
# and there is no source folder to build a .venv in — everything is inside.
FROZEN = bool(getattr(sys, 'frozen', False))
TOOL_DIR = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))


# mutagen is essential (tags and durations). The rest are only needed to read
# and WRITE the rekordbox library: without them the app still runs the Convert
# tab, minus the library features.
# ⚠ pyrekordbox was undeclared and had to be installed by hand — on a fresh
# machine every write (merge, save playlist, FLAC→AIFF, apply the tag queue)
# died with ImportError. It is pinned because its table schema tracks the
# rekordbox version.
REQUIRED_MODULES = {'mutagen': 'mutagen'}
OPTIONAL_MODULES = {'sqlcipher3': 'sqlcipher3-wheels',
                    'pyrekordbox': 'pyrekordbox==0.4.4',
                    # Reading the audio itself for Teach (musicdna.py). It came
                    # along with pyrekordbox until now; said out loud, so a
                    # fresh install does not depend on that.
                    'numpy': 'numpy'}


def ensure_deps():
    """Make sure dependencies exist: if not, build a local .venv and re-run in it."""
    if FROZEN:
        return
    missing_req = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    missing_opt = [m for m in OPTIONAL_MODULES if importlib.util.find_spec(m) is None]
    if not missing_req and not missing_opt:
        return

    venv_dir = os.path.join(TOOL_DIR, '.venv')
    # ⚠ Windows puts the venv interpreter in Scripts\python.exe, not bin/python3.
    vpy = (os.path.join(venv_dir, 'Scripts', 'python.exe') if os.name == 'nt'
           else os.path.join(venv_dir, 'bin', 'python3'))
    if os.path.abspath(sys.executable) == os.path.abspath(vpy):
        # Already inside the venv: if the essentials are here, carry on.
        if missing_req:
            sys.exit("Could not install %s into the .venv.\nTry:  rm -rf '%s'"
                     % (', '.join(missing_req), venv_dir))
        return

    if not os.path.exists(vpy):
        print('First run: setting up the environment…')
        import venv as venv_mod
        venv_mod.EnvBuilder(with_pip=True).create(venv_dir)
    pip = [vpy, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check']
    for mod, pkg in REQUIRED_MODULES.items():
        if subprocess.run([vpy, '-c', 'import ' + mod], capture_output=True).returncode:
            subprocess.check_call(pip + [pkg])
    for mod, pkg in OPTIONAL_MODULES.items():
        if subprocess.run([vpy, '-c', 'import ' + mod], capture_output=True).returncode:
            try:
                subprocess.check_call(pip + [pkg])
            except Exception:
                print('WARNING: could not install %s; the rekordbox library '
                      'tabs will not be available.' % pkg)
    os.execv(vpy, [vpy] + sys.argv)


ensure_deps()

import argparse
import hashlib
import json
import re
import shutil
import signal
import tempfile
import threading
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import platform

import mutagen
import settings as prefs
from mutagen import id3 as id3f
from mutagen.aiff import AIFF
from mutagen.flac import FLAC

DEFAULT_OUTPUT = os.path.expanduser(os.path.join('~', 'Music', 'Backspins'))
AUDIO_EXTS = {
    '.flac', '.mp3', '.m4a', '.mp4', '.aac',
    '.aiff', '.aif', '.aifc', '.wav',
    '.ogg', '.oga', '.opus', '.wma',
}
# Originals that have been dealt with go here, inside the scanned folder.
PROCESSED_DIR = 'processed'

LOCK = threading.Lock()
STATE = {
    'phase': 'idle',   # idle | scanning | scanned | processing | done | error
    'error': None,
    'source': '',
    'output': DEFAULT_OUTPUT,
    'converter': None,  # 'ffmpeg' | 'afconvert' | None
    'scan': None,
    'prog': None,
    'stop': False,
    # rekordbox library analysis (independent of the conversion flow)
    'rb': {'phase': 'idle', 'error': None, 'done': 0, 'total': 0, 'stats': None},
    # FLAC → AIFF inside the rekordbox library
    'fl': {'phase': 'idle', 'error': None, 'done': 0, 'total': 0,
           'ok': 0, 'fail': 0, 'plan': None, 'stop': False},
}
# Duplicate groups you have looked at and decided are NOT duplicates. Kept on
# disk: an "ignore" that comes back after every analysis is not an ignore.
DUPE_IGNORE = os.path.join(prefs.APP_DIR, 'dupes-ignored.json')


def dupes_ignored():
    try:
        with open(DUPE_IGNORE, encoding='utf-8') as fh:
            return set(json.load(fh) or [])
    except Exception:
        return set()


_DUPE_LOCK = threading.Lock()


def dupes_ignore(key, on=True):
    # ⚠ Read-modify-write under a lock, and a temp name of our own: two
    # overlapping clicks shared one .part file and lost one of the decisions
    # (or read a half-written file back).
    with _DUPE_LOCK:
        return _dupes_ignore_locked(key, on)


def _dupes_ignore_locked(key, on):
    cur = dupes_ignored()
    cur.add(key) if on else cur.discard(key)
    os.makedirs(prefs.APP_DIR, exist_ok=True)
    tmp = '%s.%d.part' % (DUPE_IGNORE, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(sorted(cur), fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, DUPE_IGNORE)
    return cur


# Songs you looked for and could not find anywhere. Kept so Follow does not
# search them again every time they come round, and so they can be tried
# again later from one list.
UNFINDABLE = os.path.join(prefs.APP_DIR, 'unfindable.json')
_UNF_LOCK = threading.Lock()


def unfindable_key(artist, title, query=''):
    import re as _re
    fold = lambda s: _re.sub(r'\s+', ' ', str(s or '').lower()).strip()
    return (fold(artist) + '\t' + fold(title)) if (title or artist) else fold(query)


def unfindable_list():
    try:
        with open(UNFINDABLE, encoding='utf-8') as fh:
            return json.load(fh) or []
    except Exception:
        return []


def unfindable_set(item, on=True):
    with _UNF_LOCK:
        cur = [x for x in unfindable_list() if x.get('key') != item['key']]
        if on:
            cur.insert(0, item)
        os.makedirs(prefs.APP_DIR, exist_ok=True)
        tmp = '%s.%d.part' % (UNFINDABLE, os.getpid())
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(cur, fh, ensure_ascii=False)
        os.replace(tmp, UNFINDABLE)
        return cur


TG_LOG = []
TG_STATE = {'phase': 'idle', 'error': None, 'done': 0, 'total': 0,
            'ok': 0, 'fail': 0, 'stop': False, 'dest': ''}
FL_LOG = []
FL_LOGFILE = os.path.join(prefs.DATA_DIR, 'flac-a-aiff.log')
ITEMS = []
RB_GROUPS = []
RB_QUALITY = {}
RB_LIBRARY = {}
RB_COMPAT = {}
# ⚠ Bumped under LOCK by every write that changes the library. A rebuild reads
# it before it starts and refuses to install its snapshot if it moved: without
# this, a rebuild that began before a write could install the PRE-write picture
# after the patch, quietly undoing it — the kind of race that makes a fixed bug
# "come back on its own".
LIB_GEN = {'n': 0}

#: The learned transition model, rebuilt whenever the library changes.
AFFINITY = {'gen': -1, 'model': None}


def bump_library_gen():
    LIB_GEN['n'] += 1          # callers already hold LOCK


# ---------------------------------------------------------------- utilitats

def detect_converter():
    """Which converter this machine has. ffmpeg first, everywhere.

    `afconvert` ships with macOS and is a usable fallback there; on any other
    platform it does not exist, so it is not even looked for.
    """
    if shutil.which('ffmpeg'):
        return 'ffmpeg'
    if platform.system() == 'Darwin' and shutil.which('afconvert'):
        return 'afconvert'
    return None


def sanitize_part(s):
    """Clean one part of a filename (title or artist)."""
    s = unicodedata.normalize('NFC', str(s))
    s = s.replace('/', '-').replace(':', '-').replace('\\', '-')
    s = re.sub(r'[\x00-\x1f\x7f]', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s[:150]


def _join_vals(v, multi=False):
    if v is None:
        return None
    if not isinstance(v, list):
        v = [v]
    vals = []
    for x in v:
        if hasattr(x, 'text'):  # ID3 frame (avoids its \x00 separator)
            vals.extend(str(t).strip() for t in x.text)
        else:
            vals.append(str(x).strip())
    vals = [x for x in vals if x]
    if not vals:
        return None
    return ', '.join(vals) if multi else vals[0]


def read_tags(path):
    """Returns (title, artist) or (None, None). Never raises."""
    title = artist = None
    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        easy = None
    if easy is not None and easy.tags:
        try:
            title = _join_vals(easy.tags.get('title'))
            artist = _join_vals(easy.tags.get('artist'), multi=True)
        except Exception:
            pass
    if title and artist:
        return title, artist
    # Formats the easy interface misses: ID3 inside AIFF/WAV, WMA (ASF)…
    try:
        raw = mutagen.File(path)
    except Exception:
        raw = None
    if raw is not None and raw.tags is not None:
        tg = raw.tags
        def rawget(keys, multi=False):
            for k in keys:
                try:
                    v = tg.get(k)
                except Exception:
                    v = None
                s = _join_vals(v, multi=multi)
                if s:
                    return s
            return None
        title = title or rawget(('TIT2', 'Title', '\xa9nam', 'title'))
        artist = artist or rawget(('TPE1', 'Author', '\xa9ART', 'artist'), multi=True)
    return title, artist


def build_new_name(item):
    """Final name: "Title - Artist.ext" if both tags exist, else the original."""
    ext = '.aiff' if item['ext'] == '.flac' else item['ext']
    if item['renamed']:
        base = f"{sanitize_part(item['title'])} - {sanitize_part(item['artist'])}"
        return base[:200] + ext
    return item['stem'] + ext


def nkey(name):
    return unicodedata.normalize('NFC', name).lower()


def suffixed(name, n):
    stem, ext = os.path.splitext(name)
    return f'{stem} ({n}){ext}'


# -------------------------------------------------------------- conversion

def convert_flac_to_aiff(src, dst, converter):
    bits = 16
    try:
        bits = FLAC(src).info.bits_per_sample or 16
    except Exception:
        pass
    fmt = 'BEI16' if bits <= 16 else ('BEI24' if bits <= 24 else 'BEI32')
    if converter == 'ffmpeg':
        codec = {'BEI16': 'pcm_s16be', 'BEI24': 'pcm_s24be', 'BEI32': 'pcm_s32be'}[fmt]
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
               '-map', '0:a:0', '-map_metadata', '-1', '-c:a', codec, '-f', 'aiff', dst]
    else:
        cmd = ['afconvert', '-f', 'AIFF', '-d', fmt, src, dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or 'unknown error').strip()
        raise RuntimeError(msg[:400])


def write_aiff_tags(dst, src_flac):
    """Copy the tags (and artwork) from the FLAC to the AIFF as ID3."""
    f = FLAC(src_flac)
    tags = f.tags
    a = AIFF(dst)
    if a.tags is None:
        a.add_tags()
    t = a.tags

    def vc(key):
        if tags is None:
            return None
        v = tags.get(key)
        return [str(x) for x in v] if v else None

    for key, Frame in (('title', id3f.TIT2), ('artist', id3f.TPE1),
                       ('album', id3f.TALB), ('albumartist', id3f.TPE2),
                       ('genre', id3f.TCON), ('composer', id3f.TCOM)):
        v = vc(key)
        if v:
            t.add(Frame(encoding=3, text=v))
    d = vc('date') or vc('year')
    if d:
        t.add(id3f.TDRC(encoding=3, text=d[0]))
    tn = vc('tracknumber')
    tt = vc('tracktotal') or vc('totaltracks')
    if tn:
        txt = tn[0] + (f'/{tt[0]}' if tt and '/' not in tn[0] else '')
        t.add(id3f.TRCK(encoding=3, text=txt))
    dn = vc('discnumber')
    if dn:
        t.add(id3f.TPOS(encoding=3, text=dn[0]))
    for pic in list(getattr(f, 'pictures', []))[:2]:
        t.add(id3f.APIC(encoding=3, mime=pic.mime or 'image/jpeg',
                        type=pic.type or 3, desc=pic.desc or '', data=pic.data))
    a.save()


# ---------------------------------------------------------------- workers

def scan_worker(source):
    try:
        paths = []
        for root, dirs, files in os.walk(source):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            # What has already been dealt with is not dealt with again.
            if os.path.abspath(root) == os.path.abspath(source):
                dirs[:] = [d for d in dirs if d != PROCESSED_DIR]
            for fn in files:
                if fn.startswith('.'):
                    continue
                if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                    paths.append(os.path.join(root, fn))
        paths.sort(key=lambda p: os.path.relpath(p, source).lower())
        with LOCK:
            STATE['scan']['found'] = len(paths)

        # The library, indexed once for the whole scan. If rekordbox is not
        # there this is simply None and the scan behaves as it always did.
        lib_index, rb_mod = None, None
        try:
            import rekordbox as rb_mod
            lib_index = rb_mod.library_index()
        except Exception:
            lib_index, rb_mod = None, None

        items = []
        for i, p in enumerate(paths):
            name = os.path.basename(p)
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            title, artist = read_tags(p)
            renamed = bool(title and artist and sanitize_part(title) and sanitize_part(artist))
            item = {
                'path': p,
                'rel': os.path.relpath(p, source),
                'stem': stem,
                'ext': ext,
                'size': size,
                'title': title,
                'artist': artist,
                'kind': 'convert' if ext == '.flac' else 'copy',
                'renamed': renamed,
                'res': None,
                'final': None,
                'note': None,
            }
            item['new_name'] = build_new_name(item)
            # ⚠ Do you already own this? Asked HERE, before importing, which is
            # the only moment it costs nothing to act on it.
            item['have'] = []
            if lib_index is not None and title:
                try:
                    import mutagen
                    info = getattr(mutagen.File(p), 'info', None)
                    secs = getattr(info, 'length', None)
                except Exception:
                    secs = None
                try:
                    item['have'] = rb_mod.already_in_library(title, artist,
                                                             lib_index, secs)
                except Exception:
                    item['have'] = []
            items.append(item)
            with LOCK:
                STATE['scan']['tagged'] = i + 1

        with LOCK:
            ITEMS[:] = items
            STATE['scan']['with_tags'] = sum(1 for it in items if it['renamed'])
            STATE['scan']['flac'] = sum(1 for it in items if it['kind'] == 'convert')
            STATE['scan']['no_tags'] = sum(1 for it in items if not it['renamed'])
            STATE['phase'] = 'scanned'
    except Exception as e:
        with LOCK:
            STATE['phase'] = 'error'
            STATE['error'] = f'Error while scanning: {e}'


def move_to_processed(item, source):
    """Put the original in <source>/processed/, keeping the folder layout."""
    dest_dir = os.path.join(source, PROCESSED_DIR, os.path.dirname(item['rel']))
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(item['path'])
    n = 1
    while True:
        cand = name if n == 1 else suffixed(name, n)
        target = os.path.join(dest_dir, cand)
        if not os.path.exists(target):
            break
        n += 1
    shutil.move(item['path'], target)


def process_worker(output, converter, source, move_processed,
                   import_rb=False, import_color=None):
    try:
        os.makedirs(output, exist_ok=True)
    except Exception as e:
        with LOCK:
            STATE['phase'] = 'error'
            STATE['error'] = f'Cannot create the output folder: {e}'
        return

    out_real = os.path.realpath(output)
    used = set()
    with LOCK:
        items = list(ITEMS)

    for item in items:
        with LOCK:
            if STATE['stop']:
                STATE['prog']['stopped'] = True
                break
            STATE['prog']['current'] = item['rel']
        try:
            _process_one(item, output, out_real, used, converter)
        except subprocess.TimeoutExpired:
            item['res'] = 'error'
            item['note'] = 'timed out while converting'
            with LOCK:
                STATE['prog']['errors'].append({'file': item['rel'], 'msg': 'timed out'})
        except Exception as e:
            item['res'] = 'error'
            item['note'] = str(e)[:300]
            with LOCK:
                STATE['prog']['errors'].append({'file': item['rel'], 'msg': str(e)[:300]})

        # An original is only moved if it really was dealt with: if it
        # failed it stays put, and what already lived in the output is left.
        done_ok = item['res'] == 'ok' or (item['res'] == 'skip'
                                          and item['note'] == 'already there')
        # ⚠ AND its counterpart must actually be in the output. Without this a
        # file could move to processed/ while nothing reached the destination —
        # a skip that misjudged "already there", or an odd write. If the output
        # file is not there, the original stays put and is flagged, never
        # quietly relocated as if it were done.
        final_ok = bool(item.get('final')) and os.path.exists(
            os.path.join(output, item['final']))
        if done_ok and not final_ok:
            done_ok = False
            item['res'] = 'error'
            item['note'] = 'was marked done but is not in the output — left in place'
            with LOCK:
                STATE['prog']['errors'].append(
                    {'file': item['rel'], 'msg': 'not found in the output; original left in place'})
        if move_processed and done_ok:
            try:
                move_to_processed(item, source)
                item['moved'] = True
                with LOCK:
                    STATE['prog']['moved'] += 1
            except Exception as e:
                with LOCK:
                    STATE['prog']['warnings'].append(
                        {'file': item['rel'],
                         'msg': 'done, but could not move it to processed: %s' % str(e)[:180]})
        with LOCK:
            STATE['prog']['done'] += 1

    # ── into rekordbox, stamped "not looked at yet" ──
    # ⚠ Runs at the END and never fails the conversion: the files are already
    # converted and safe in the output folder, so rekordbox being open means
    # "import it later", not "this went wrong".
    imported = None
    if import_rb:
        try:
            import rekordbox_import as ri
            import rekordbox_merge as rm
            with LOCK:
                produced = [os.path.join(output, it['final'])
                            for it in ITEMS if it.get('final')]
            if not produced:
                imported = {'added': 0, 'skipped': 0, 'note': 'nothing to import'}
            elif rm.rekordbox_running():
                imported = {'added': 0, 'skipped': 0,
                            'note': 'rekordbox is open — import them later'}
            else:
                res = ri.import_files(produced, color=import_color)
                imported = {'added': res['added'], 'skipped': res['skipped'],
                            'failed': len(res['failed']), 'color': res['color']}
                if res['added']:
                    with LOCK:
                        RB_ALL.clear()
                        RB_LIBRARY.clear()
                        RB_COMPAT.clear()
                        _forget_library_files()
                        bump_library_gen()
        except Exception as e:
            imported = {'added': 0, 'skipped': 0, 'note': str(e)[:200]}

    with LOCK:
        STATE['prog']['current'] = ''
        STATE['prog']['imported'] = imported
        STATE['phase'] = 'done'


def _process_one(item, output, out_real, used, converter):
    # If the source file already lives INSIDE the output folder, leave it.
    if os.path.realpath(item['path']).startswith(out_real + os.sep):
        item['res'] = 'skip'
        item['note'] = 'already in the output folder'
        with LOCK:
            STATE['prog']['skipped'] += 1
        return

    name = item['new_name']
    n = 1
    while True:
        cand = name if n == 1 else suffixed(name, n)
        key = nkey(cand)
        cpath = os.path.join(output, cand)
        exists = os.path.exists(cpath)
        if not exists and key not in used:
            break
        if exists and key not in used:
            # Left over from an earlier run: do not do it twice.
            same_copy = (item['kind'] == 'copy'
                         and os.path.getsize(cpath) == item['size'])
            if item['kind'] == 'convert' or same_copy:
                item['res'] = 'skip'
                item['final'] = cand
                item['note'] = 'already there'
                with LOCK:
                    STATE['prog']['skipped'] += 1
                return
        n += 1

    used.add(key)
    dest = cpath

    if item['kind'] == 'convert':
        tmp = dest + '.part'
        try:
            convert_flac_to_aiff(item['path'], tmp, converter)
            try:
                write_aiff_tags(tmp, item['path'])
            except Exception as e:
                with LOCK:
                    STATE['prog']['warnings'].append(
                        {'file': item['rel'], 'msg': f'AIFF created but untagged: {str(e)[:200]}'})
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        with LOCK:
            STATE['prog']['converted'] += 1
    else:
        shutil.copy2(item['path'], dest)
        with LOCK:
            STATE['prog']['copied'] += 1

    item['res'] = 'ok'
    item['final'] = cand
    if item['renamed']:
        with LOCK:
            STATE['prog']['renamed'] += 1


# ------------------------------------------------------------- previewing

# Browsers play MP3/FLAC/WAV/M4A natively, but nothing outside Safari
# touches AIFF: those are transcoded to MP3 once and then cached.
# ⚠⚠ AIFF BELONGS HERE. It was missing, and three quarters of the library
# is AIFF since the FLAC conversion — so almost every track you played was
# fully transcoded to MP3 by ffmpeg BEFORE a single byte was served: measured
# at 2.3 s of pegged CPU for a 47 MB file, on top of which the cache lives in
# the system temp dir that macOS purges, so the bill came due again and again.
# That was the "everything is slow" and most of the "the next track does not
# play": the element sat waiting on the server, and a background Safari tab
# gives up rather than wait. WebKit plays AIFF natively through Core Audio.
# A browser that does NOT (Chrome, Firefox) falls back on its own: the media
# `error` handler re-requests the same URL with &mp3=1, which forces the
# transcode below.
NATIVE_AUDIO = {
    '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.wav': 'audio/wav',
    '.aiff': 'audio/aiff', '.aif': 'audio/aiff',
    '.m4a': 'audio/mp4', '.mp4': 'audio/mp4', '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg', '.opus': 'audio/ogg',
}
PREVIEW_DIR = os.path.join(tempfile.gettempdir(), 'rekordbox-toolkit-preview')
# ⚠ ONE LOCK PER FILE, not one global lock. Transcoding a track holds its
# lock for the whole ffmpeg run; with a single global lock, clicking a second
# track while the first was still converting made the second request WAIT for
# it — and the browser's audio element gives up before that queue clears,
# which is the "could not play" you get on the first play of a track.
# ⚠ Bounded, not a plain dict: one lock per file forever would leak memory
# over a long session of previewing thousands of tracks. LRU-evict the oldest.
import collections as _collections
PREVIEW_LOCKS = _collections.OrderedDict()
PREVIEW_LOCKS_GUARD = threading.Lock()
PREVIEW_LOCKS_CAP = 512


def _preview_lock(key):
    with PREVIEW_LOCKS_GUARD:
        lock = PREVIEW_LOCKS.get(key)
        if lock is None:
            lock = PREVIEW_LOCKS[key] = threading.Lock()
            while len(PREVIEW_LOCKS) > PREVIEW_LOCKS_CAP:
                PREVIEW_LOCKS.popitem(last=False)   # drop the oldest
        else:
            PREVIEW_LOCKS.move_to_end(key)
        return lock


def browser_can_play(path, ext):
    """An .m4a can be AAC (browsers play it) or ALAC (they do not).

    This has to be checked properly: serve an ALAC as-is and the player
    goes silent with an "unsupported format" error that looks like something else.
    """
    if ext not in ('.m4a', '.mp4'):
        return True
    try:
        import mutagen
        info = getattr(mutagen.File(path), 'info', None)
        return not str(getattr(info, 'codec', '') or '').startswith('alac')
    except Exception:
        return False   # when in doubt, transcode


def preview_source(path, converter, force=False):
    """Returns (file to serve, mime type). Transcodes only when it has to.

    `force` is the escape hatch for a browser that cannot play the format we
    believe is native — it asks for the transcode explicitly rather than
    leaving us to guess the browser from a user agent.
    """
    ext = os.path.splitext(path)[1].lower()
    if not force and ext in NATIVE_AUDIO and browser_can_play(path, ext):
        return path, NATIVE_AUDIO[ext]

    st = os.stat(path)
    key = hashlib.sha1(('%s|%s|%s' % (path, st.st_mtime, st.st_size)).encode()).hexdigest()
    out = os.path.join(PREVIEW_DIR, key + '.mp3')
    with _preview_lock(key):
        if not os.path.exists(out):
            os.makedirs(PREVIEW_DIR, exist_ok=True)
            tmp = '%s.%d.part' % (out, os.getpid() ^ threading.get_ident())
            if converter == 'ffmpeg':
                # The -f mp3 is required: the temp file ends in .part and ffmpeg
                # cannot infer the format from the name.
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', path,
                       '-vn', '-c:a', 'libmp3lame', '-b:a', '192k', '-f', 'mp3', tmp]
            else:
                tmp = '%s.%d.part.m4a' % (out, os.getpid() ^ threading.get_ident())
                out = out[:-4] + '.m4a'
                cmd = ['afconvert', '-f', 'm4af', '-d', 'aac', path, tmp]
            subprocess.run(cmd, capture_output=True, timeout=300, check=True)
            os.replace(tmp, out)
    return out, ('audio/mp4' if out.endswith('.m4a') else 'audio/mpeg')


# ⚠⚠ AIFF for a browser that cannot play it (Chrome, which is where the app
# opens): served as a WAV, built on the fly. It is the SAME audio — AIFF and WAV
# are both plain PCM, AIFF big-endian and WAV little-endian — so all it takes
# is a 44-byte WAV header and each sample's bytes turned round as they go out.
# The MP3 it replaces was a full ffmpeg transcode BEFORE the first byte: 1.2 s
# for a 27 MB track, 2.3 s for a 47 MB one, on every track you moved to, after
# the browser had first fetched the AIFF and given up on it. This starts at
# once, loses nothing, and seeks, because a byte of the WAV maps straight onto
# a byte of the file.
_WAV_LAYOUT = {}
_SIGN8 = bytes((i + 128) & 255 for i in range(256))


def _ext80(b):
    """An AIFF sample rate: 80-bit IEEE extended, big-endian."""
    exp = ((b[0] & 0x7f) << 8) | b[1]
    mant = int.from_bytes(b[2:10], 'big')
    if not exp and not mant:
        return 0.0
    return (-1 if b[0] & 0x80 else 1) * mant * 2.0 ** (exp - 16383 - 63)


def aiff_wav_layout(path):
    """How to read `path` as a WAV: its header, and where the samples are.

    None when the file is not an AIFF we can pass through (a compressed AIFF-C,
    say) — the caller then falls back on transcoding.
    """
    import struct
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    if key in _WAV_LAYOUT:
        return _WAV_LAYOUT[key]
    lay = None
    try:
        with open(path, 'rb') as fh:
            head = fh.read(12)
            if (len(head) == 12 and head[:4] == b'FORM'
                    and head[8:12] in (b'AIFF', b'AIFC')):
                aifc = head[8:12] == b'AIFC'
                comm = ssnd = None
                pos = 12
                while pos + 8 <= st.st_size and not (comm and ssnd):
                    fh.seek(pos)
                    ck = fh.read(8)
                    if len(ck) < 8:
                        break
                    cid, size = ck[:4], struct.unpack('>I', ck[4:])[0]
                    if cid == b'COMM':
                        comm = fh.read(min(size, 64))
                    elif cid == b'SSND':
                        off = struct.unpack('>I', fh.read(4))[0]
                        ssnd = (pos + 16 + off, max(0, size - 8 - off))
                    pos += 8 + size + (size & 1)
                if comm and ssnd and len(comm) >= 18:
                    ch, frames, bits = struct.unpack('>hIh', comm[:8])
                    rate = int(round(_ext80(comm[8:18])))
                    comp = comm[18:22] if (aifc and len(comm) >= 22) else b'NONE'
                    floaty = comp in (b'fl32', b'FL32', b'fl64', b'FL64')
                    width = (8 if comp in (b'fl64', b'FL64') else 4) if floaty \
                        else (bits + 7) // 8
                    swap = comp in (b'NONE', b'twos', b'fl32', b'FL32',
                                    b'fl64', b'FL64')
                    if (comp in (b'NONE', b'twos', b'sowt') or floaty) \
                            and ch > 0 and rate > 0 and width in (1, 2, 3, 4, 8):
                        block = ch * width
                        length = min(frames * block, ssnd[1])
                        length -= length % block
                        fmt = 3 if floaty else 1
                        header = (b'RIFF' + struct.pack('<I', 36 + length) + b'WAVE'
                                  + b'fmt ' + struct.pack('<IHHIIHH', 16, fmt, ch, rate,
                                                          rate * block, block, width * 8)
                                  + b'data' + struct.pack('<I', length))
                        lay = {'header': header, 'start': ssnd[0], 'length': length,
                               'width': width, 'swap': swap and width > 1,
                               'sign8': width == 1}
    except (OSError, struct.error):
        lay = None
    if len(_WAV_LAYOUT) > 256:
        _WAV_LAYOUT.clear()
    _WAV_LAYOUT[key] = lay
    return lay


def _to_little_endian(buf, width):
    """Turn each `width`-byte sample round. `buf` holds whole samples."""
    import array
    if width == 3:
        out = bytearray(len(buf))
        out[0::3] = buf[2::3]
        out[1::3] = buf[1::3]
        out[2::3] = buf[0::3]
        return bytes(out)
    code = {2: 'H', 4: 'I', 8: 'Q'}[width]
    arr = array.array(code)
    if arr.itemsize != width:            # never on a Mac, but be honest
        return b''.join(buf[i:i + width][::-1] for i in range(0, len(buf), width))
    arr.frombytes(buf)
    arr.byteswap()
    return arr.tobytes()


PEAK_BUCKETS = 900


def audio_peaks(path):
    """Waveform profile (SoundCloud style): peaks normalised to 0..1.

    Decoded to 8 kHz mono PCM, which is plenty for drawing bars, and cached:
    the second time is instant.
    """
    st = os.stat(path)
    key = hashlib.sha1(('peaks2|%s|%s|%s' % (path, st.st_mtime, st.st_size)).encode()).hexdigest()
    cache = os.path.join(PREVIEW_DIR, key + '.json')
    if os.path.exists(cache):
        try:
            with open(cache, 'r') as fh:
                return json.load(fh)
        except Exception:
            pass
    if not shutil.which('ffmpeg'):
        return None

    raw = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', '8000',
         '-f', 's16le', '-'],
        capture_output=True, timeout=300).stdout
    import array
    samples = array.array('h')
    samples.frombytes(raw[:len(raw) - (len(raw) % 2)])
    if not samples:
        return None

    step = max(len(samples) // PEAK_BUCKETS, 1)
    peaks = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        if not chunk:
            break
        # Mean amplitude, NOT the peak: on mastered music the peak clips and
        # the drawing comes out a solid block, with no breakdowns or drops.
        peaks.append(sum(map(abs, chunk)) / len(chunk))
    top = max(peaks) or 1
    data = {'peaks': [round(p / top, 3) for p in peaks],
            'duration': round(len(samples) / 8000.0, 2)}
    try:
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        with open(cache, 'w') as fh:
            json.dump(data, fh)
    except Exception:
        pass
    return data


# ------------------------------------------------------- duplicats rekordbox

def converted_sibling(target):
    """The right path for a track converted under the page's feet.

    In-place conversion changes the extension (.flac -> .aiff), and a tab
    opened BEFORE that keeps the old path: hitting play gave a 404 and a
    "could not play" message, with the track sitting perfectly well on
    disk.

    Only the EXACT sibling is accepted — same folder, same name, the
    extension the only difference — which is what the conversion produces
    and what was verified sample for sample. No guessing by title.
    """
    if not target or os.path.isfile(target):
        return target
    base, ext = os.path.splitext(target)
    if ext.lower() not in ('.flac', '.m4a', '.mp4', '.wav'):
        return target
    alt = base + '.aiff'
    return alt if os.path.isfile(alt) else target


RB_ALL = {}


def library_payload():
    """The set-builder library, built once and reused.

    ⚠ Shared by /api/library and the tagger endpoints on purpose: three copies
    of "read the library and build it" would drift apart the first time the
    shape changed.
    """
    with LOCK:
        cached = dict(RB_LIBRARY) if RB_LIBRARY else None
    if cached is not None:
        return cached
    import rekordbox as rb
    import setbuilder as sb
    with LOCK:
        gen = LIB_GEN['n']
    tracks, _ = rb.load_tracks()
    built = sb.build_library(tracks, playlists=rb.load_playlists(),
                             history=rb.load_history())
    with LOCK:
        if LIB_GEN['n'] != gen:
            return built       # someone wrote while we read: do not install
        RB_LIBRARY.clear()
        RB_LIBRARY.update(built)
    return built


_SLOTS = {'at': 0.0, 'val': None}


def label_slots(max_age=60.0):
    """Which of rekordbox's eight colour slots each label sits in, cached.

    The database keeps no colour VALUE for a label (ColorCode is empty), only
    the slot, and the screens paint rekordbox's own swatch for that slot. It
    changes when you rename a label in rekordbox, so a minute is plenty — and
    the popover asks every second, which is no reason to open the database
    every second.
    """
    import time
    now = time.time()
    if _SLOTS['val'] is not None and now - _SLOTS['at'] < max_age:
        return _SLOTS['val']
    # ⚠ Stale is fine, waiting is not: the popover asks every second and on
    # every open, and opening the database costs a quarter of a second. After
    # the first time, a stale answer goes out at once and the refresh happens
    # on the side.
    if _SLOTS['val'] is not None:
        if not _SLOTS.get('busy'):
            _SLOTS['busy'] = True
            def _refresh():
                try:
                    _read_label_slots(now)
                finally:
                    _SLOTS['busy'] = False
            threading.Thread(target=_refresh, daemon=True).start()
        return _SLOTS['val']
    return _read_label_slots(now)


def _read_label_slots(now):
    slots = {}
    try:
        import rekordbox as rbx
        with rbx.open_library() as cur2:
            if cur2 is not None:
                cur2.execute('SELECT ID, Commnt FROM djmdColor')
                for cid, nm in cur2.fetchall():
                    if nm:
                        slots[nm.strip()] = int(cid)
    except Exception:
        if _SLOTS['val'] is not None:
            return _SLOTS['val']
    _SLOTS['val'], _SLOTS['at'] = slots, now
    return slots


def order_banks(banks):
    """The MyTag groups with their tags in YOUR order (settings: tag_order).

    Tags you have not placed — new ones, say — keep rekordbox's order after
    the ones you have. The order is applied here, on the way out, so every
    screen that shows the banks shows them the same way.
    """
    order = prefs.load().get('tag_order') or {}
    out = []
    for b in banks or []:
        want = order.get(b.get('name')) or []
        if not want:
            out.append(b)
            continue
        pos = {n: i for i, n in enumerate(want)}
        tags = sorted(b.get('tags') or [], key=lambda t: pos.get(t.get('name'), len(pos)))
        out.append(dict(b, tags=tags))
    return out


def browse_row(t):
    """One track as every tagging screen sees it — and as the phone does.

    ⚠ Scalars get the queue overlay too, not just tags. With rekordbox open the
    queue holds the edit, and showing the raw library value made your own
    change vanish from the row a moment after you typed it. And `base` goes
    WITH it: the LIBRARY values, for compare-and-set baselines — an edit must
    be checked against what the database holds, never against what the queue
    is about to make it.

    Shared by /api/browse and the phone sync on purpose: the phone works from a
    copy of these rows, and two versions of "a row" would drift apart the
    first time a field was added to one of them.
    """
    import tagger
    tid = str(t['id'])
    base = {'rating': t.get('rating') or 0,
            'comment': t.get('comment') or '',
            'genre': '/'.join(t.get('genres') or []),
            'color': (t.get('color') or '').strip(),
            'title': t.get('title') or '',
            'artist': t.get('artist') or ''}
    eff = tagger.effective_scalars(tid, base)
    return {
        'id': tid, 'title': eff['title'], 'artist': eff['artist'],
        'genres': (eff['genre'].split('/') if eff['genre'] else []),
        'bpm': t.get('bpm'),
        'key': t.get('key'), 'seconds': t.get('seconds'),
        'rating': eff['rating'], 'color': eff['color'],
        'tags': tagger.effective_tags(tid, t.get('tags')),
        'added': t.get('added'), 'imported': t.get('imported') or '', 'path': t.get('path'),
        'kind': t.get('kind'),
        'kbps': t.get('kbps'), 'comment': eff['comment'],
        'base': base,
    }


def all_library():
    """The library WITHOUT the "ready" filter, built once and reused.

    ⚠ Every tagger endpoint must read this one, never `library_payload()`.
    That one hides anything not marked ready — which is precisely the material
    the Tagger exists to work through (450 of ~2,400 tracks here). Reading the
    filtered library from a tagger route does not look like a bug: it looks
    like a track that does not exist.
    """
    with LOCK:
        cached = dict(RB_ALL) if RB_ALL else None
        gen = LIB_GEN['n']
    if cached is not None:
        return cached
    import rekordbox as rb
    import setbuilder as sb
    tracks, _ = rb.load_tracks()
    built = sb.build_library(tracks, ready_color='',
                             playlists=rb.load_playlists())
    # Built unfiltered, but the client still has to know which colour means
    # "already decided", so it does not offer the vetted pile as a queue.
    _cfg = prefs.load()
    built['ready_color'] = (_cfg.get('ready_color') or '')
    # What finishing a track stamps. Separate from "ready" because tagging can
    # leave the beatgrid outstanding, which only rekordbox can do.
    built['done_color'] = (_cfg.get('done_color') or _cfg.get('ready_color') or '')
    with LOCK:
        if LIB_GEN['n'] != gen:
            return built       # stale snapshot: serve it, never install it
        RB_ALL.clear()
        RB_ALL.update(built)
    return built


# ── reading the audio (musicdna.py), in a process of its own ──────────────
# ⚠ A separate PROCESS, not a thread: the analysis is numpy across every core
# for minutes, and inside the server it would hold the interpreter and make
# every click wait. It reports one line per track; this only keeps count.
DNA = {'phase': 'idle', 'done': 0, 'total': 0, 'errors': 0, 'proc': None,
       'started': 0.0, 'error': None}
_DNA_AGREE = {'key': None, 'value': None}


def dna_status(tracks=None):
    """How much of the library has been read, and how far to trust the key."""
    import musicdna as md
    data = md.load()
    out = {k: DNA[k] for k in ('phase', 'done', 'total', 'errors', 'error')}
    if DNA['phase'] == 'running' and DNA['done']:
        rate = DNA['done'] / max(1.0, time.time() - DNA['started'])
        out['eta'] = int((DNA['total'] - DNA['done']) / max(rate, 0.01))
    if tracks is None:
        return out
    ok = pending = 0
    agree = keyed = 0
    for t in tracks:
        p = t.get('path')
        if not p:
            continue
        e = data.get(str(t['id']))
        if md.fresh(e, p, t.get('key')):
            if e.get('error'):
                continue
            ok += 1
            lk = md.camelot_to_key(t.get('key'))
            if lk and e.get('heard'):
                keyed += 1
                agree += (lk == (e['heard']['tonic'], e['heard']['mode']))
        else:
            pending += 1
    out.update({'analysed': ok, 'pending': pending, 'library': len(tracks),
                'key_agree': round(agree / float(keyed), 3) if keyed else None,
                'key_checked': keyed})
    return out


def dna_start():
    import musicdna as md, energy_calc as ec
    with LOCK:
        if DNA['phase'] == 'running':
            return {'ok': True, 'already': True}
        DNA.update(phase='planning', done=0, total=0, errors=0, error=None)
    try:
        tracks = (all_library() or {}).get('tracks') or []
        jobs = md.plan(tracks, ec.anlz_paths())
    except Exception as e:
        DNA.update(phase='error', error=str(e)[:200])
        return {'error': str(e)[:200]}
    if not jobs:
        DNA.update(phase='done')
        return {'ok': True, 'total': 0}
    fd, job_file = tempfile.mkstemp(prefix='dna-', suffix='.json', dir=prefs.APP_DIR)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(jobs, fh)
    env = dict(os.environ, VECLIB_MAXIMUM_THREADS='1', OMP_NUM_THREADS='1',
               OPENBLAS_NUM_THREADS='1')
    proc = subprocess.Popen(helper_command('musicdna') + ['--batch', job_file],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            cwd=TOOL_DIR, env=env, text=True,
                            # Its own group, so stopping it takes the workers too.
                            start_new_session=True)
    DNA.update(phase='running', total=len(jobs), proc=proc, started=time.time())

    def follow():
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if 'done' in msg and not msg.get('finished'):
                    DNA['done'] = msg['done']
                    if msg.get('error'):
                        DNA['errors'] += 1
            proc.wait()
        finally:
            try:
                os.remove(job_file)
            except OSError:
                pass
            if DNA['phase'] == 'running':
                DNA['phase'] = 'done' if proc.returncode == 0 else 'stopped'
            DNA['proc'] = None

    threading.Thread(target=follow, daemon=True).start()
    return {'ok': True, 'total': len(jobs)}


def dna_stop():
    proc = DNA.get('proc')
    if proc and proc.poll() is None:
        DNA['phase'] = 'stopped'
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
    return {'ok': True}


def _refresh_derived(t, cache):
    """Recompute the fields track_view derives, after a patch.

    ⚠ `banks` and `search` are derived, not stored: the Sets tab reads banks and
    every search box reads the folded index. Patching only the raw fields left
    both describing the track as it was — a renamed track stayed findable only
    under its old name, and a tag added here never reached the Sets columns.
    """
    import setbuilder as sb
    by_tag = {}
    for b in (cache.get('banks') or []):
        for tag in (b.get('tags') or []):
            by_tag[tag['name']] = b['name']
    banks = {}
    for name in (t.get('tags') or []):
        banks.setdefault(by_tag.get(name, ''), []).append(name)
    banks.pop('', None)
    t['banks'] = banks
    t['search'] = sb.fold_search(t)


def patch_library_caches(ops):
    """Mirror freshly APPLIED ops onto the in-memory library, in place.

    ⚠ This is the other half of auto-saving. Every screen reads the cached
    library (RB_ALL / RB_LIBRARY); the write itself goes to master.db. Without
    this, the next poll showed the PRE-edit values — stars vanished a second
    after you set them, a genre reverted the moment you touched the energy —
    and, worse, the stale copy fed wrong `before` baselines to later edits,
    which made them skip as "changed elsewhere".

    A full rebuild here is not an option: it re-copies a 40 MB database per
    edit. The ops describe exactly what the database now holds, so the caches
    are patched to match — including the DERIVED fields (energy and note come
    out of the comment, the genre list out of the genre string).
    """
    import setbuilder as sb
    with LOCK:
        caches = [c for c in (RB_ALL, RB_LIBRARY) if c and c.get('tracks')]
        for op in ops or []:
            kind, tid = op.get('op'), str(op.get('track') or '')
            val = op.get('value')
            for cache in caches:
                for t in cache['tracks']:
                    if str(t.get('id')) != tid:
                        continue
                    if kind == 'add_tag':
                        tag = op.get('tag')
                        if tag and tag not in (t.get('tags') or []):
                            t['tags'] = (t.get('tags') or []) + [tag]
                    elif kind == 'remove_tag':
                        t['tags'] = [x for x in (t.get('tags') or [])
                                     if x != op.get('tag')]
                    elif kind == 'set_rating':
                        t['rating'] = int(val or 0)
                    elif kind == 'set_comment':
                        t['comment'] = val or ''
                        energy, note = sb.split_comment(t['comment'])
                        t['energy'], t['note'] = energy, note
                    elif kind == 'set_genre':
                        t['genres'] = sb.split_genres(val or '')
                    elif kind == 'set_color':
                        t['color'] = (val or '').strip()
                    elif kind == 'set_title':
                        t['title'] = (val or '').strip()
                    elif kind == 'set_artist':
                        t['artist'] = (val or '').strip()
                    _refresh_derived(t, cache)
                    break
        # ⚠ A colour change can move a track IN or OUT of the ready-filtered
        # library, and a patch can only touch rows already present. Membership
        # cannot be patched, so that cache is rebuilt instead.
        if any(o.get('op') == 'set_color' for o in (ops or [])):
            RB_LIBRARY.clear()
        # The playlists travel inside both caches: a track added to one from
        # the phone must show up in them, so they are read again.
        if any(str(o.get('op') or '').startswith('playlist_') for o in (ops or [])):
            RB_LIBRARY.clear()
            RB_ALL.clear()
        bump_library_gen()


def rekordbox_worker():
    try:
        import rekordbox as rb
        tracks, stats = rb.load_tracks()
        with LOCK:
            STATE['rb']['stats'] = stats

        def progress(done, total):
            with LOCK:
                STATE['rb']['done'] = done
                STATE['rb']['total'] = total

        groups, quality = rb.analyze(tracks, progress=progress)
        with LOCK:
            RB_GROUPS[:] = groups
            RB_QUALITY.clear()
            RB_QUALITY.update(quality)
            STATE['rb']['phase'] = 'done'
    except Exception as e:
        with LOCK:
            STATE['rb']['phase'] = 'error'
            STATE['rb']['error'] = str(e)[:400]


# ----------------------------------------------------- FLAC → AIFF in situ

def fl_log(level, text):
    """Every line goes to the screen AND to a file, in case you want it later."""
    import datetime
    line = {'t': datetime.datetime.now().strftime('%H:%M:%S'),
            'level': level, 'text': text}
    with LOCK:
        FL_LOG.append(line)
    try:
        with open(FL_LOGFILE, 'a') as fh:
            fh.write('%s  %-5s  %s\n' % (line['t'], level, text))
    except OSError:
        pass




def tg_log(level, text):
    with LOCK:
        TG_LOG.append({'level': level, 'text': text})




def flac_worker(delete_originals, limit=None):
    import flac_to_aiff_inplace as fl
    try:
        todo, skip = fl.plan()
        if limit:
            todo = todo[:limit]
        problema = fl.space_check(todo, delete_originals)
        if problema:
            raise RuntimeError(problema)
        with LOCK:
            STATE['fl']['total'] = len(todo)
            STATE['fl']['done'] = 0
        fl_log('info', 'Starting %d tracks (%d skipped).' % (len(todo), len(skip)))

        def log(level, text):
            fl_log(level, text)
            if level in ('ok', 'error'):
                with LOCK:
                    STATE['fl']['done'] += 1
                    STATE['fl']['ok' if level == 'ok' else 'fail'] += 1

        def should_stop():
            with LOCK:
                return STATE['fl']['stop']

        fl.convert_batch(todo, delete_originals, log=log, should_stop=should_stop)
        with LOCK:
            STATE['fl']['phase'] = 'done'
            RB_COMPAT.clear()      # the library changed formats
            RB_LIBRARY.clear()
            RB_ALL.clear()         # Browse rows carry paths, now renamed
            # ⚠ Every converted track has a NEW path; the allowlist that gates
            # playback still holds the .flac ones, so previewing a converted
            # track 404'd until the server restarted.
            _forget_library_files()
            bump_library_gen()
    except Exception as e:
        fl_log('error', str(e)[:300])
        with LOCK:
            STATE['fl']['phase'] = 'error'
            STATE['fl']['error'] = str(e)[:300]


# ---------------------------------------------------------------- servidor

def snapshot(with_items=False):
    with LOCK:
        s = {k: STATE[k] for k in ('phase', 'error', 'source', 'output', 'converter')}
        s['default_output'] = DEFAULT_OUTPUT
        s['rb'] = dict(STATE['rb'])
        s['fl'] = dict(STATE['fl'])
        s['scan'] = dict(STATE['scan']) if STATE['scan'] else None
        if STATE['prog']:
            p = dict(STATE['prog'])
            p['errors'] = list(p['errors'])[-80:]
            p['warnings'] = list(p['warnings'])[-80:]
            s['prog'] = p
        else:
            s['prog'] = None
        if with_items:
            s['items'] = [
                {'rel': it['rel'], 'new_name': it['new_name'], 'kind': it['kind'],
                 'renamed': it['renamed'], 'res': it['res'], 'final': it['final'],
                 'note': it['note'],
                 # ⚠ Without this the whole "you already own this" check never
                 # reached the screen: it was computed and then dropped here.
                 'have': it.get('have') or []}
                for it in ITEMS[:800]
            ]
            s['items_total'] = len(ITEMS)
    return s


def clean_path(p):
    p = (p or '').strip().strip('"').strip("'").strip()
    return os.path.expanduser(p) if p else ''


_LIB_FILES = None
_LIB_FILES_GUARD = threading.Lock()


def _forget_library_files():
    """Drop the cached allowlist of playable paths.

    ⚠ Must be called wherever library paths change (conversion renames, import,
    delete, merge). The allowlist gates /api/audio, /api/peaks and /api/reveal,
    so a stale one refuses to serve a track whose file the toolkit itself just
    renamed — playback simply 404s until a restart.
    """
    global _LIB_FILES
    _LIB_FILES = None


def library_file_set():
    """realpaths of every file the rekordbox library references. Cached.

    ⚠ Used to confine the paths a client may ask us to serve/reveal. It is the
    library's OWN files, so tracks on external drives are naturally included and
    playback never breaks — but /etc/passwd and friends are not in it.
    """
    global _LIB_FILES
    with _LIB_FILES_GUARD:
        if _LIB_FILES is not None:
            return _LIB_FILES
    files = set()
    try:
        import rekordbox as rb
        tracks, _ = rb.load_tracks()
        for t in tracks:
            if t.get('path'):
                files.add(os.path.realpath(t['path']))
    except Exception:
        pass
    with _LIB_FILES_GUARD:
        _LIB_FILES = files
    return files


def path_allowed(target):
    """Whether the client is allowed to touch this path.

    Allowed: any file the library references, or anything under the output,
    source or preview folders. Everything else is refused — a page in the
    browser must not be able to read arbitrary files off disk.
    """
    if not target:
        return False
    rp = os.path.realpath(target)
    if rp in library_file_set():
        return True
    roots = [STATE.get('output'), STATE.get('source'), DEFAULT_OUTPUT, PREVIEW_DIR]
    for root in roots:
        if not root:
            continue
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def open_in_file_manager(target, select=False):
    """Show a file or folder in the OS file manager. Never raises for a
    missing binary — that was a dead 'reveal' button on Windows/Linux."""
    osname = platform.system()
    try:
        if osname == 'Darwin':
            subprocess.Popen(['open', '-R', target] if select else ['open', target])
        elif osname == 'Windows':
            if select:
                subprocess.Popen(['explorer', '/select,', os.path.normpath(target)])
            else:
                subprocess.Popen(['explorer', os.path.normpath(target)])
        else:
            subprocess.Popen(['xdg-open', target if not select else os.path.dirname(target)])
        return True
    except (FileNotFoundError, OSError):
        return False


def _detect_folder_dialog():
    """Whether a native folder picker is available on this machine."""
    if platform.system() == 'Darwin':
        return True
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


HAS_FOLDER_DIALOG = _detect_folder_dialog()


def choose_folder_dialog():
    """Open a native 'choose folder' dialog. Returns the path, or '' if
    cancelled / unavailable. ⚠ Was macOS-only (osascript) and swallowed only
    TimeoutExpired, so on Windows/Linux the button was dead."""
    if platform.system() == 'Darwin':
        try:
            r = subprocess.run(
                ['osascript', '-e', 'POSIX path of (choose folder with prompt '
                 '"Choose the music folder to scan")'],
                capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().rstrip('/')
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return ''
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        chosen = filedialog.askdirectory()
        root.destroy()
        return chosen or ''
    except Exception:
        return ''


class Handler(BaseHTTPRequestHandler):
    server_version = 'Backspins/1.0'

    def log_message(self, *args):
        pass

    def _trusted(self):
        """Only this app's own pages may talk to the server.

        ⚠ Listening on 127.0.0.1 keeps the network out, NOT the browser: any
        web page open in any tab can send a request to 127.0.0.1:8765, and
        "simple" cross-site POSTs (text/plain bodies) are sent without asking.
        Without this check, a page could delete tracks, rewrite settings or
        start a merge while the toolkit is running. Three checks:
          · Host must be the loopback name we serve — stops DNS rebinding,
            where evil.example resolves to 127.0.0.1 and becomes same-origin.
          · Origin, when the browser sends one, must be ours.
          · Sec-Fetch-Site, when sent, must be same-origin or none (typed into
            the address bar) — covers the GETs an <img> or a link can trigger,
            which carry no Origin.
        Clients that are not browsers (the menu bar, curl) send none of the
        last two and pass on the Host check alone.
        """
        port = self.server.server_address[1]
        ours = {'127.0.0.1:%d' % port, 'localhost:%d' % port, '[::1]:%d' % port}
        ok = (self.headers.get('Host') or '').lower() in ours
        origin = (self.headers.get('Origin') or '').lower()
        if ok and origin:
            ok = origin.startswith('http://') and origin[7:] in ours
        site = self.headers.get('Sec-Fetch-Site')
        if ok and site is not None:
            ok = site in ('same-origin', 'none')
        if not ok:
            self.send_error(403, 'Forbidden')
        return ok

    def _serve_wav(self, path, lay):
        """An AIFF served as a WAV, byte ranges included (see aiff_wav_layout)."""
        header, width = lay['header'], lay['width']
        total = len(header) + lay['length']
        start, end = 0, total - 1
        rng = self.headers.get('Range') or ''
        partial = False
        m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = min(int(m.group(1)), total - 1)
                if m.group(2):
                    end = min(int(m.group(2)), total - 1)
            else:
                start = max(total - int(m.group(2)), 0)
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if partial:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, total))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            pos = start
            if pos < len(header):
                piece = header[pos:end + 1]
                self.wfile.write(piece)
                pos += len(piece)
            with open(path, 'rb') as fh:
                while pos <= end:
                    d0 = pos - len(header)                 # offset in the samples
                    a0 = d0 - d0 % width                    # whole samples only
                    a1 = min(lay['length'], a0 + 1048576)
                    fh.seek(lay['start'] + a0)
                    raw = fh.read(a1 - a0)
                    if not raw:
                        break
                    raw = raw[:len(raw) - len(raw) % width]
                    if lay['swap']:
                        raw = _to_little_endian(raw, width)
                    elif lay['sign8']:
                        raw = raw.translate(_SIGN8)
                    piece = raw[d0 - a0:(end + 1 - len(header)) - a0]
                    if not piece:
                        break
                    self.wfile.write(piece)
                    pos += len(piece)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_range(self, path, mime):
        """Serve a file with Range support, so the playhead can be dragged."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get('Range') or ''
        partial = False
        m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = min(int(m.group(1)), size - 1)
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # suffix range: the last N bytes
                start = max(size - int(m.group(2)), 0)
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if partial:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            with open(path, 'rb') as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser switched track or moved the playhead

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, mime):
        """Serve a file from the tool's own folder, never from the cache."""
        try:
            with open(path, 'rb') as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, 'missing %s' % os.path.basename(path))
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._trusted():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ('/', '/index.html', '/sets', '/sets2', '/find', '/browse',
                    '/library', '/inbox'):
            html_path = os.path.join(TOOL_DIR, 'convertidor.html')
            try:
                with open(html_path, 'rb') as fh:
                    body = fh.read()
            except OSError:
                self.send_error(500, 'Falta convertidor.html')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/state':
            self._json(snapshot(with_items='items=1' in (parsed.query or '')))
        elif path == '/api/environment':
            # Everything the first-run screen needs in one call: what the tool
            # found on this machine, and whether rekordbox is holding the
            # library open (which blocks every write, not just conversions).
            env = prefs.environment()
            try:
                import rekordbox_merge as rm
                env['rekordbox_open'] = rm.rekordbox_running()
                env['backups'] = rm.BACKUP_DIR
            except Exception:
                env['rekordbox_open'] = False
                env['backups'] = ''
            env['settings_file'] = prefs.SETTINGS_FILE
            self._json(env)
        elif path == '/api/backups':
            import library_backup
            self._json(library_backup.status())
        elif path == '/api/settings':
            self._json(prefs.load())
        elif path == '/api/compat':
            try:
                with LOCK:
                    cached = dict(RB_COMPAT) if RB_COMPAT else None
                if cached is None:
                    import rekordbox as rb
                    import compat
                    tracks, _ = rb.load_tracks()
                    cached = compat.analyse(tracks)
                    with LOCK:
                        RB_COMPAT.clear()
                        RB_COMPAT.update(cached)
                self._json(cached)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)

        elif path == '/api/flac/log':
            since = int((parse_qs(parsed.query).get('since') or ['0'])[0])
            with LOCK:
                self._json({'lines': FL_LOG[since:], 'total': len(FL_LOG)})
        elif path == '/api/rekordbox/groups':
            with LOCK:
                groups = list(RB_GROUPS)
                quality = dict(RB_QUALITY)
            # The ones you have already ruled out travel WITH the groups, so
            # the browser never has to ask twice.
            self._json({'groups': groups, 'quality': quality,
                        'ignored': sorted(dupes_ignored())})
        elif path == '/api/storage':
            # What the app keeps on disk, and where (storage.py).
            import storage
            try:
                out = storage.report()
                q = parse_qs(parsed.query)
                if q.get('budget'):
                    import library_backup as lb
                    n, b = lb.budget_preview(int(q['budget'][0] or 0))
                    out['preview'] = {'copies': n, 'bytes': b}
                self._json(out)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)

        elif path == '/api/access':
            # Can the app reach the library and the music files (access.py).
            import access
            try:
                lib = all_library() or {}
                self._json(access.check(lib.get('tracks') or []))
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)

        elif path == '/api/vocabulary':
            # The starter vocabulary, and how much of it this library lacks.
            import vocabulary
            try:
                self._json(vocabulary.plan())
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)

        elif path == '/api/tagger/suggest':
            # ⚠ Server-side on purpose: the co-occurrence rule lives in one
            # place. Re-implementing it in the browser to save a local
            # round-trip would leave two versions to keep honest.
            import tagger, setbuilder as sb
            q = parse_qs(parsed.query)
            tid = (q.get('id') or [''])[0]
            try:
                # ⚠ Unfiltered: with library_payload() every track in the ADD and
                # CHECK queues came back 404, so the suggestions — the whole
                # point of the Tagger — never appeared on the tracks you were
                # actually tagging. The wider corpus is also better evidence.
                lib = all_library()
                track = next((t for t in lib['tracks'] if str(t['id']) == tid), None)
                if track is None:
                    self._json({'error': 'unknown track'}, 404)
                else:
                    self._json(tagger.suggest(track, lib['tracks'], lib['banks']))
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/tagger/tracks':
            # ⚠ all_library(), never library_payload(): the WHOLE library,
            # unfiltered. And it must be the SAME builder the other tagger
            # routes use — this handler used to build its own copy, so a field
            # added there (done_color) never reached the Tagger and the finish
            # button went on offering the old colour.
            try:
                self._json(all_library())
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)

        elif path == '/api/tagger/genres':
            import tagger
            try:
                # ⚠ all_library, never the ready-filtered one: the genres you
                # need to autocomplete are exactly the ones on the un-vetted
                # material the Tagger works through.
                self._json({'genres': tagger.genre_catalogue(all_library()['tracks'])})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)


        elif path == '/api/find/unfindable':
            self._json({'items': unfindable_list()})


        elif path == '/api/find/links':
            # The streaming playlists linked to a rekordbox one, with the
            # rekordbox playlist's current name.
            import pairing, tagger, rekordbox as _rb
            try:
                names = {}
                try:
                    names = {str(n['id']): n.get('name') or '' for n in _rb.load_playlist_tree() or []}
                except Exception:
                    pass
                out = []
                for l in pairing.links():
                    pid = tagger.resolve_playlist(l.get('rbPlaylist'))
                    out.append({'id': l['id'], 'url': l['url'], 'source': l.get('source'), 'name': l.get('name'),
                                'rbName': names.get(pid) or '', 'pending': pid.startswith('new:'),
                                'gone': not pid.startswith('new:') and pid not in names and bool(names),
                                'added': len(l.get('added') or []), 'lastSync': l.get('lastSync'),
                                'created': l.get('created')})
                self._json({'items': out})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/rb/playlists':
            # Ordinary playlists (not folders, not smart lists), with their
            # folder path — to link a streaming playlist to one you already have.
            import rekordbox as _rb
            try:
                tree = _rb.load_playlist_tree() or []
                by = {str(n['id']): n for n in tree}
                def path_of(n):
                    parts, p = [], by.get(str(n.get('parent') or ''))
                    while p:
                        parts.insert(0, p.get('name') or '')
                        p = by.get(str(p.get('parent') or ''))
                    return ' / '.join(parts)
                self._json({'items': [{'id': str(n['id']), 'name': n.get('name') or '', 'folder': path_of(n),
                                       'count': len(n.get('tracks') or [])}
                                      for n in tree if n.get('kind') == 'list']})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/artwork':
            import artwork
            try:
                # ?state=1: just the progress — the full view reads a copy of
                # the database, too heavy to ask for every second and a half.
                if (parse_qs(parsed.query).get('state') or [''])[0]:
                    self._json({'state': dict(artwork.STATE)})
                    return
                self._json(artwork.view())
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)


        elif path == '/api/tagger/queue':
            import tagger
            self._json(tagger.pending())

        elif path == '/api/tagger/now':
            # Everything the menu bar popover needs in ONE call: which track the
            # Tagger has open, its tag vocabulary, and the tags it will end up
            # with (library + queue). One call because the popover asks again
            # every second while it is open.
            import tagger
            try:
                now = tagger.get_now()
                tid = now.get('track')
                # ⚠ Even with nothing open, the popover must be able to say
                # "N changes waiting for rekordbox to close" — that is when
                # you would otherwise assume everything is done.
                held = tagger.held_up()
                if not tid:
                    self._json({'track': None, 'banks': [], 'tags': [], 'held': held})
                    return
                with LOCK:
                    cached = dict(RB_ALL) if RB_ALL else None
                if cached is None:
                    self._json({'track': None, 'banks': [], 'tags': [],
                                'warming': True, 'held': held})
                    return
                track = next((t for t in cached.get('tracks', [])
                              if str(t['id']) == str(tid)), None)
                if track is None:
                    self._json({'track': None, 'banks': [], 'tags': [], 'held': held})
                    return
                # ⚠ TWO copies of every single-value field. `values` is what
                # to SHOW (library + queue); `base` is what rekordbox holds and
                # is what the client must send back as `before`. See
                # effective_scalars() for why sending the effective value as a
                # baseline would make the change skip itself.
                base = {'rating': track.get('rating') or 0,
                        'comment': track.get('comment') or '',
                        'genre': '/'.join(track.get('genres') or []),
                        'color': (track.get('color') or '').strip()}
                cfg = prefs.load()
                shown = {k: track.get(k) for k in
                         ('id', 'title', 'artist', 'bpm', 'key',
                          'seconds', 'color', 'genres', 'path')}
                # A rename waiting in the queue is the name to show.
                names = tagger.effective_scalars(tid, {
                    'title': track.get('title') or '', 'artist': track.get('artist') or ''})
                shown['title'], shown['artist'] = names['title'], names['artist']
                self._json({
                    'held': held,
                    'track': shown,
                    'banks': order_banks(cached.get('banks', [])),
                    'tags': tagger.effective_tags(tid, track.get('tags')),
                    'base': base,
                    'values': tagger.effective_scalars(tid, base),
                    'genres': [g['name'] for g in
                               tagger.genre_catalogue(cached['tracks'], limit=400)],
                    'energyInComment': bool(cfg.get('energy_in_comment', True)),
                    'doneColor': (cfg.get('done_color')
                                  or cfg.get('ready_color') or '').strip(),
                    'colorIds': label_slots(),
                    'queued': tagger.pending().get('count', 0),
                    # ⚠ Which banks to leave out is a SETTING, not a constant:
                    # hard-coding it here would make the popover disagree with
                    # the Tagger panel the moment you changed it.
                    'ignoredBanks': prefs.load().get('ignored_banks') or [],
                    'playback': tagger.get_playback(),
                    # How many are still waiting to be triaged, and which colour
                    # means "waiting" — so the popover can offer "done, next"
                    # and say how far there is to go.
                    'triage': {
                        'color': (cfg.get('import_color') or '').strip(),
                        'left': len(tagger.triage_queue(
                            cached.get('tracks', []), cfg.get('import_color'))),
                    },
                    # ⚠ The library's revision. Both surfaces watch it so an
                    # edit made in one shows up in the other without waiting
                    # for a window focus — they were only ever converging when
                    # you happened to click back into the browser.
                    'gen': LIB_GEN['n'],
                    'at': now.get('at'),
                })
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/tagger/nav':
            import tagger
            self._json(tagger.get_nav())

        elif path == '/api/tagger/cmd-wait':
            # Long poll: held open until the menu bar gives an order. ⚠ Cheap
            # because it is one idle thread on localhost, and it is what makes
            # a drag on the popover's waveform land immediately.
            import tagger
            q = parse_qs(parsed.query)
            since = int((q.get('since') or ['0'])[0] or 0)
            self._json(tagger.wait_for_cmd(since))

        elif path == '/api/lib-gen':
            # Deliberately tiny: polled every second or so by whichever screen
            # is open, purely to learn whether anything changed — and, riding
            # along, whether anything is HELD UP waiting for rekordbox to close.
            # Both screens already poll this; making the queue visible cost no
            # extra request.
            import tagger
            held = tagger.held_up()
            with LOCK:
                gen = LIB_GEN['n']
            self._json({'gen': gen, 'held': held,
                        'stale': code_stamp() > BOOT['stamp'],
                        'page': page_stamp()})

        elif path == '/api/browse':
            # Free-text browse over the WHOLE library (all_library, never the
            # filtered one — "have I got this?" must see everything).
            import library_search as lsr, tagger
            q = parse_qs(parsed.query)
            term = (q.get('q') or [''])[0].strip()
            want_color = (q.get('color') or [''])[0].strip()
            # How many rows. The table wants a screenful; the Inbox wants the
            # whole pile, because it walks through it one track at a time.
            try:
                limit = max(1, min(5000, int((q.get('limit') or ['120'])[0])))
            except ValueError:
                limit = 120
            try:
                lib = all_library()
                tracks = lib['tracks']
                # ⚠ Counted over the WHOLE library before any filtering, so the
                # numbers on the chips do not move as you type or narrow down.
                counts = {}
                for t in tracks:
                    c = (t.get('color') or '').strip()
                    if c:
                        counts[c] = counts.get(c, 0) + 1
                if want_color:
                    tracks = [t for t in tracks
                              if (t.get('color') or '').strip().casefold()
                              == want_color.casefold()]
                rows = (lsr.search(tracks, term, limit=min(limit, 500)) if term else
                        [{'track': t, 'score': 1.0, 'exact': True}
                         for t in sorted(tracks, key=lambda x: (x.get('added') or '',
                                                                 x.get('imported') or ''),
                                         reverse=True)[:limit]])
                out = []
                for r in rows:
                    row = browse_row(r['track'])
                    row['score'] = r['score']
                    out.append(row)
                # The colour SLOT each label sits in, so the browser can
                # paint rekordbox's own swatch: the database stores no colour
                # value (ColorCode is empty), only which of the eight it is.
                slots = label_slots()
                # Everything the tagging panel needs travels WITH the rows:
                # the vocabulary, which groups to hide, and the colour that
                # means "done". One call, so opening a row is instant.
                cfg = prefs.load()
                self._json({'total': len(lib['tracks']), 'shown': len(tracks),
                            'rows': out, 'query': term,
                            'colorIds': slots, 'colorCounts': counts,
                            'banks': order_banks(lib.get('banks', [])),
                            'ignoredBanks': cfg.get('ignored_banks') or [],
                            'energyInComment': bool(cfg.get('energy_in_comment', True)),
                            'doneColor': (cfg.get('done_color')
                                          or cfg.get('ready_color') or '').strip(),
                            'readyColor': (cfg.get('ready_color') or '').strip(),
                            'importColor': (cfg.get('import_color') or 'NEW').strip()})
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)


        elif path == '/i18n.js':
            self._send_file(os.path.join(TOOL_DIR, 'i18n.js'),
                            'text/javascript; charset=utf-8')
        elif path == '/menubar':
            self._send_file(os.path.join(TOOL_DIR, 'menubar.html'),
                            'text/html; charset=utf-8')
        elif path == '/api/quick':
            # The track that is playing, as something to tag or file: the tags
            # it will have (library + queue), every MyTag in your order, and
            # every ordinary playlist with whether it is already in it. Writing
            # goes through /api/tagger/enqueue like every other edit.
            import tagger, rekordbox as rb
            try:
                tid = str((parse_qs(parsed.query).get('id') or [''])[0])
                lib = all_library() or {}
                track = next((t for t in lib.get('tracks') or []
                              if str(t['id']) == tid), None)
                if track is None:
                    self._json({'error': 'this file is not in your rekordbox library'}, 404)
                    return
                tree = rb.load_playlist_tree()
                inlist = {n['id'] for n in tree if tid in (n.get('tracks') or [])}
                # ⚠ The queue too: with rekordbox open, a track you just added
                # is not in the database yet, and "add" must not be offered
                # twice.
                for it in tagger._read_queue():
                    if str(it.get('track')) != tid:
                        continue
                    if it.get('op') == 'playlist_add':
                        inlist.add(str(it.get('playlist')))
                    elif it.get('op') == 'playlist_remove':
                        inlist.discard(str(it.get('playlist')))
                byid = {n['id']: n for n in tree}

                def where(n):
                    names, p, seen = [], n.get('parent'), set()
                    while p and p in byid and p not in seen:
                        seen.add(p)
                        names.append(byid[p]['name'])
                        p = byid[p].get('parent')
                    return ' / '.join(reversed(names))
                lists = [{'id': n['id'], 'name': n['name'], 'folder': where(n),
                          'n': len(n.get('tracks') or []), 'has': n['id'] in inlist}
                         for n in tree if n.get('kind') == 'list']
                self._json({'track': {'id': tid, 'title': track.get('title'),
                                      'artist': track.get('artist')},
                            'banks': order_banks(lib.get('banks') or []),
                            'tags': tagger.effective_tags(tid, track.get('tags')),
                            'lists': lists})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)
        elif path == '/api/dna/status':
            with LOCK:
                cached = list(RB_ALL.get('tracks') or []) if RB_ALL else None
            self._json(dna_status(cached))
        elif path == '/api/library':
            try:
                self._json(library_payload())
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)
        elif path == '/api/peaks':
            target = (parse_qs(parsed.query).get('path') or [''])[0]
            if target and not path_allowed(target):
                self.send_error(404)
                return
            if (not target or not os.path.isfile(target)
                    or os.path.splitext(target)[1].lower() not in AUDIO_EXTS):
                self.send_error(404)
                return
            # ⚠ The ffmpeg decode below is only for tracks rekordbox has not
            # analysed. It used to run first, on every track, to draw a shape
            # that the three bands then replaced — 0.3 s on each new track for
            # nothing, since every track here has its bands.
            data = {'peaks': [], 'duration': 0}
            # Where the body of the track comes in, from the phrase analysis:
            # that is where playback starts, instead of at the top of the intro.
            try:
                import rekordbox as rb
                import phrases
                anlz = rb.anlz_for_path(target)
                data['start'] = phrases.body_start_seconds(anlz)
            except Exception:
                anlz, data['start'] = None, None
            # The THREE BANDS rekordbox has already worked out (low, mid,
            # high). We do not compute them: they are its own, so the drawing
            # is EXACTLY what you see on a CDJ. If a track was never analysed
            # for CDJ-3000 they are absent and the client draws the plain one.
            try:
                import anlz_wave
                import phrases as _ph
                real = _ph.resolve(anlz) if anlz else None
                bands = anlz_wave.read_3band(real) if real else None
                if bands:
                    top = max(max(bands['low']), max(bands['mid']),
                              max(bands['high'])) or 1
                    # Raw integers plus the ceiling: half the weight of sending
                    # decimals, and whoever draws is the one who normalises.
                    data['bands'] = {'low': bands['low'], 'mid': bands['mid'],
                                     'high': bands['high'], 'max': top}
            except Exception:
                pass
            if not data.get('bands'):
                try:
                    data.update(audio_peaks(target) or {})
                except Exception:
                    pass
            self._json(data)
        elif path == '/api/mixclip':
            # A short piece of a track, rendered to WAV, already stretched to
            # the tempo it has to play at.
            #
            # ⚠ This is what makes a real sync possible instead of a rough
            # one. Two things the <audio> path cannot do:
            #   · `playbackRate` moves the PITCH — there is no time-stretch in
            #     a media element, so beat-matching detuned the record. ffmpeg
            #     `atempo` stretches time and leaves the pitch alone.
            #   · a media element cannot be told to start at an exact instant;
            #     the browser starts it when it gets round to it. A decoded
            #     buffer can be scheduled to the sample, which is the whole
            #     difference between "close" and "locked".
            import subprocess, tempfile as _tf
            try:
                q = parse_qs(parsed.query)
                src = converted_sibling((q.get('path') or [''])[0])
                if not src or not path_allowed(src) or not os.path.isfile(src):
                    self.send_error(404)
                    return
                start = max(0.0, float((q.get('from') or ['0'])[0]))
                secs = max(1.0, min(240.0, float((q.get('secs') or ['90'])[0])))
                rate = float((q.get('rate') or ['1'])[0])
                rate = max(0.5, min(2.0, rate))
                conv = STATE.get('converter') or detect_converter()
                if not conv:
                    self.send_error(500, 'No converter found')
                    return
                af = []
                if abs(rate - 1.0) > 0.0005:
                    af = ['-af', 'atempo=%.6f' % rate]
                # ⚠ -ss BEFORE -i seeks by keyframe and can land tens of
                # milliseconds out, which is exactly the error this endpoint
                # exists to remove. Accurate seek costs a little decode time
                # and is the only version worth having here.
                cmd = ([conv, '-hide_banner', '-loglevel', 'error',
                        '-i', src, '-ss', '%.6f' % start, '-t', '%.6f' % secs,
                        '-map', '0:a:0'] + af +
                       ['-ac', '2', '-ar', '44100', '-c:a', 'pcm_s16le',
                        '-f', 'wav', 'pipe:1'])
                out = subprocess.run(cmd, capture_output=True, timeout=180)
                if out.returncode or not out.stdout:
                    self.send_error(500, 'Could not render the clip')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(len(out.stdout)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(out.stdout)
            except Exception:
                try:
                    self.send_error(500, 'Could not render the clip')
                except Exception:
                    pass

        elif path == '/api/audio':
            target = converted_sibling((parse_qs(parsed.query).get('path') or [''])[0])
            if target and not path_allowed(target):
                self.send_error(404)
                return
            if (not target or not os.path.isfile(target)
                    or os.path.splitext(target)[1].lower() not in AUDIO_EXTS):
                self.send_error(404)
                return
            q_audio = parse_qs(parsed.query)
            force_mp3 = (q_audio.get('mp3') or [''])[0] == '1'
            # A browser that cannot play AIFF asks for the WAV view of it.
            if (not force_mp3 and (q_audio.get('wav') or [''])[0] == '1'
                    and os.path.splitext(target)[1].lower() in ('.aiff', '.aif')):
                lay = aiff_wav_layout(target)
                if lay:
                    self._serve_wav(target, lay)
                    return
            try:
                src, mime = preview_source(target, STATE['converter'], force=force_mp3)
            except Exception:
                self.send_error(500, 'Could not prepare the audio')
                return
            self._serve_range(src, mime)
        elif path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._trusted():
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            data = {}

        if path == '/api/settings':
            try:
                saved = prefs.save(data)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)
                return
            # ⚠ The library view depends on these (the "ready" colour filters
            # it, and the energy convention changes every comment), so the
            # cached analysis has to go or the screen would keep showing the
            # old settings' results.
            with LOCK:
                RB_LIBRARY.clear()
                RB_COMPAT.clear()
                # ⚠ RB_ALL bakes ready_color/done_color in at build time, so a
                # settings change leaves it lying about which colour means done.
                RB_ALL.clear()
                bump_library_gen()
            self._json({'ok': True, 'settings': saved})
        elif path == '/api/choose-folder':
            chosen = choose_folder_dialog()
            if chosen:
                self._json({'path': chosen})
            else:
                # Not just cancelled: on a machine with no native dialog the
                # client falls back to typing the path in.
                self._json({'cancelled': True, 'no_dialog': not HAS_FOLDER_DIALOG})

        elif path == '/api/scan':
            source = clean_path(data.get('source'))
            if not source or not os.path.isdir(source):
                self._json({'error': 'That source folder does not exist.'}, 400)
                return
            with LOCK:
                if STATE['phase'] in ('scanning', 'processing'):
                    self._json({'error': 'There is already a job running.'}, 409)
                    return
                STATE['phase'] = 'scanning'
                STATE['error'] = None
                STATE['source'] = source
                STATE['scan'] = {'found': 0, 'tagged': 0, 'with_tags': 0, 'flac': 0, 'no_tags': 0}
                STATE['prog'] = None
                STATE['stop'] = False
                ITEMS[:] = []
            threading.Thread(target=scan_worker, args=(source,), daemon=True).start()
            self._json({'ok': True})

        elif path == '/api/process':
            output = clean_path(data.get('output')) or DEFAULT_OUTPUT
            with LOCK:
                if STATE['phase'] != 'scanned':
                    self._json({'error': 'Scan a folder first.'}, 409)
                    return
                converter = STATE['converter']
                if not converter:
                    self._json({'error': 'No converter found (install ffmpeg).'}, 500)
                    return
                STATE['phase'] = 'processing'
                STATE['output'] = output
                STATE['stop'] = False
                source = STATE['source']
                move_processed = bool(data.get('moveProcessed', True))
                STATE['prog'] = {'total': len(ITEMS), 'done': 0, 'converted': 0,
                                 'copied': 0, 'renamed': 0, 'skipped': 0, 'moved': 0,
                                 'moving': move_processed, 'stopped': False,
                                 'errors': [], 'warnings': [], 'current': ''}
            threading.Thread(target=process_worker,
                             args=(output, converter, source, move_processed,
                                   bool(data.get('importToRekordbox')),
                                   data.get('importColor') or None),
                             daemon=True).start()
            self._json({'ok': True})

        elif path == '/api/rekordbox/scan':
            with LOCK:
                if STATE['rb']['phase'] == 'scanning':
                    self._json({'error': 'An analysis is already running.'}, 409)
                    return
                STATE['rb'] = {'phase': 'scanning', 'error': None, 'done': 0,
                               'total': 0, 'stats': None}
                RB_GROUPS[:] = []
            threading.Thread(target=rekordbox_worker, daemon=True).start()
            self._json({'ok': True})

        elif path == '/api/merge':
            try:
                import rekordbox_merge as rm
                keeper = data.get('keeper')
                others = data.get('others') or []
                if not keeper or not others:
                    self._json({'error': 'The copies to merge are missing.'}, 400)
                    return
                dry = bool(data.get('dryRun', True))
                done = rm.apply_merge(keeper, others, dry_run=dry,
                                      delete_others=bool(data.get('deleteOthers')))
                if not dry:
                    # The analysis held in memory is stale now: tracks were
                    # retired and tags were added. ⚠ RB_ALL too — Browse reads
                    # it, and a merged-away copy must stop appearing there.
                    with LOCK:
                        RB_ALL.clear()
                        RB_LIBRARY.clear()
                        RB_GROUPS[:] = []
                        RB_QUALITY.clear()
                        RB_COMPAT.clear()
                        _forget_library_files()
                        bump_library_gen()
                self._json({'ok': True, 'done': done})
            except Exception as e:
                self._json({'error': str(e)[:400]}, 400)

        elif path == '/api/playlist':
            try:
                import rekordbox_merge as rm
                name = (data.get('name') or '').strip()
                ids = data.get('ids') or []
                pid = data.get('playlistId')
                if not ids or (not name and not pid):
                    self._json({'error': 'The name or the tracks are missing.'}, 400)
                    return
                res = rm.create_set_playlist(name, ids, playlist_id=pid)
                with LOCK:
                    RB_LIBRARY.clear()   # the playlist list is out of date now
                    RB_ALL.clear()       # it carries the playlists too
                    bump_library_gen()
                self._json({'ok': True, **res})
            except Exception as e:
                self._json({'error': str(e)[:400]}, 400)

        elif path == '/api/flac/plan':
            try:
                import flac_to_aiff_inplace as fl
                import rekordbox_merge as rm
                todo, skip = fl.plan()
                bits = {}
                for it in todo:
                    bits[str(it['bits'])] = bits.get(str(it['bits']), 0) + 1
                mida = sum(it['size'] for it in todo)
                free = shutil.disk_usage(os.path.expanduser('~')).free
                with LOCK:
                    STATE['fl']['plan'] = {
                        'total': len(todo), 'skipped': len(skip), 'bits': bits,
                        'flacBytes': mida, 'freeBytes': free,
                        'needKeep': int(mida * 1.55), 'needDelete': int(mida * 0.55),
                        'skips': [{'name': os.path.basename(t['path']), 'why': w}
                                  for t, w in skip[:20]],
                        'rekordboxOpen': rm.rekordbox_running()}
                    self._json(STATE['fl']['plan'])
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)


        elif path == '/api/sets/start':
            # The empty screen: which kinds of night this library knows, and
            # what opens one. ⚠ All of it from setbuilder — the JS must not
            # grow its own copy, which is how neighbours() ended up dead while
            # a divergent candidates() ran in the browser.
            import setbuilder as sb
            try:
                lib = all_library() or {}
                tracks = lib.get('tracks') or []
                # ⚠ The bank names come from YOUR rekordbox MyTag groups and
                # the library already resolved them. Falling back to the
                # hard-coded defaults gave an empty screen and HTTP 200 to
                # anybody whose groups are named differently — the exact bug
                # setbuilder.py:445 already warns about.
                mood = lib.get('moodBank') or 'MOOD'
                timing = lib.get('timingBank') or 'TIMING'
                kind = lib.get('typeBank') or 'TYPE OF SET'
                style = (data.get('style') or '').strip() or None
                out = {'styles': sb.styles(tracks, type_bank=kind)}
                if style:
                    out['style'] = style
                    out['openers'] = [
                        {'track': r['track'], 'stage': r['stage'],
                         'tagged': r['rank'] == 0}
                        for r in sb.openers(tracks, style=style,
                                            type_bank=kind, timing_bank=timing,
                                            limit=int(data.get('limit') or 12))]
                self._json(out)
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/affinity':
            # The learned transition model, handed to the browser whole.
            import affinity as af
            try:
                lib = all_library() or {}
                tracks = lib.get('tracks') or []
                model = affinity_model(tracks)
                self._json(af.compact(model))
            except Exception as e:
                self._json({'flow': {}, 'party': {}, 'pairs': 0, 'why': str(e)[:200]})

        elif path == '/api/sets/suggest':
            # What belongs in the set as it stands — without picking a track
            # first. The model is learned from real sessions; see affinity.py.
            import affinity as af
            try:
                lib = all_library() or {}
                tracks = lib.get('tracks') or []
                by_id = {str(t['id']): t for t in tracks}
                set_rows = [by_id[str(i)] for i in (data.get('setIds') or [])
                            if str(i) in by_id]
                model = affinity_model(tracks)
                rows = af.suggest(set_rows, tracks, model,
                                  limit=int(data.get('limit') or 40))
                self._json({'suggestions': rows,
                            'learnedFrom': model.get('pairs', 0)})
            except Exception as e:
                self._json({'suggestions': [], 'why': str(e)[:200]})

        elif path == '/api/sets/fit':
            # How well each candidate belongs to the set as it stands. The set
            # is sent as ids; the library is already here.
            import setbuilder as sb
            try:
                lib = all_library() or {}
                tracks = lib.get('tracks') or []
                by_id = {str(t['id']): t for t in tracks}
                set_rows = [by_id[str(i)] for i in (data.get('setIds') or [])
                            if str(i) in by_id]
                mood = lib.get('moodBank') or 'MOOD'
                feel = sb.set_feel(set_rows, mood_bank=mood)
                fits = {}
                # ⚠ ABSENT and EMPTY mean different things. No `ids` key at
                # all = score the whole library (you want candidates ranked).
                # An empty list = score nothing, just tell me the feel — which
                # is what the header asks for on every repaint. Treating the
                # two the same scored 2,400 tracks to render one line of text.
                want = data.get('ids')
                pool = (tracks if want is None
                        else [by_id[str(i)] for i in want if str(i) in by_id])
                for t in pool:
                    score, why = sb.feel_fit(t, feel, mood_bank=mood)
                    fits[str(t['id'])] = {'fit': score, 'why': why}
                self._json({'feel': {
                    'n': feel['n'], 'energy': feel['energy'], 'bpm': feel['bpm'],
                    'moods': sorted(feel['moods'].items(), key=lambda kv: -kv[1])[:6],
                    'families': sorted(feel['families'].items(), key=lambda kv: -kv[1])[:4],
                }, 'fits': fits})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/drive':
            # The library's own energy scale, worked out from the waveform
            # rekordbox already computed. Cheap enough (about two seconds for
            # 2,400 tracks) that recomputing beats going stale.
            import energy_calc as ec
            try:
                out, weights = ec.load()
                if data.get('recompute') or not out:
                    tracks = (all_library() or {}).get('tracks') or []
                    out, weights = ec.compute(tracks)
                # ⚠ The tag weights travel too: they are the explanation. A
                # number nobody can inspect is a number nobody should trust,
                # and these were learned from the library, not written by hand.
                ranked = sorted(weights.items(), key=lambda kv: -kv[1])
                self._json({'ok': True, 'drive': out, 'n': len(out),
                            'tags': ranked})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/teach':
            # Teach a property by example. One endpoint, because every call is
            # the same shape: here is what I did, give me the next question,
            # how good the model now is, and the library ranked by it.
            # `action`: list | open | answer | undo | forget | mark | settings
            # | delete. See teacher.py for what the model is and how it is
            # judged; musicdna.py for what it hears.
            import teacher as tc, musicdna as md, energy_calc as ec
            try:
                action = data.get('action') or 'open'
                lib = all_library() or {}
                tracks = lib.get('tracks') or []
                dna = md.load()
                if action == 'list':
                    out = tc.overview(tracks, dna)
                    out['dna'] = dna_status(tracks)
                    self._json(out)
                    return
                name = (data.get('name') or '').strip()
                if not name:
                    self._json({'error': 'name the thing you are teaching'}, 400)
                    return
                if action == 'delete':
                    with tc._LOCK:
                        allx = tc.load()
                        allx.pop(name, None)
                        tc.save(allx)
                    self._json({'ok': True})
                    return
                kind = data.get('kind') if data.get('kind') in ('label', 'order') else None

                def edit(les):
                    if kind and not (les['yes'] or les['no'] or les['pairs']):
                        les['kind'] = kind
                    if action == 'answer' and data.get('answer'):
                        tc.answer(les, data['answer'])
                    elif action == 'undo':
                        tc.undo(les)
                    elif action == 'forget' and data.get('id') is not None:
                        tc.forget(les, data['id'])
                    elif action == 'mark':
                        for i in data.get('ids') or []:
                            tc.answer(les, {'id': i, 'yes': bool(data.get('yes'))})
                    elif action == 'settings':
                        if isinstance(data.get('groups'), list):
                            les['groups'] = [g for g in data['groups']
                                             if g in tc.GROUP_LABEL]
                        if 'use_existing' in data:
                            les['use_existing'] = bool(data['use_existing'])
                les = tc.change(name, edit, kind)
                drive, _ = ec.load()
                # ⚠ The tags are part of the fingerprint: a tag written from
                # this screen patches the cached library in place without a
                # new generation, and the model must see it at once.
                fp = sum(len(v) for t in tracks for v in (t.get('banks') or {}).values())
                # With rekordbox open a tag waits in the queue; on this screen
                # it already counts, or "tag them" would offer it again.
                import tagger
                queued = {}
                for it in tagger._read_queue():
                    if it.get('tag') == name and it.get('op') in ('add_tag', 'remove_tag'):
                        queued[str(it.get('track'))] = it['op'] == 'add_tag'
                self._json(tc.state(les, tracks, dna, drive, (LIB_GEN['n'], fp),
                                    with_groups=bool(data.get('groups_report')),
                                    queued={t for t, on in queued.items() if on}))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/dna/start':
            # Read the audio of every track not read yet. Its own process, so
            # the UI never waits on it; see musicdna.py.
            try:
                self._json(dna_start())
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/dna/stop':
            self._json(dna_stop())

        elif path == '/api/dna/track':
            import musicdna as md, teacher as tc
            tid = str(data.get('id') or '')
            self._json({'id': tid, 'dna': tc.dna_full(md.load().get(tid))})

        elif path == '/api/phrases':
            # Where the sections of a track are, in seconds. Lets you jump
            # from breakdown to drop while you are tasting a join, instead of
            # scrubbing for it.
            import mixplan as mp, energy_calc as ec
            try:
                tid = str(data.get('id') or '')
                dat = ec.anlz_paths().get(tid)
                if not dat:
                    self._json({'ok': True, 'phrases': [], 'why': 'never analysed'})
                    return
                grid = mp.beat_seconds(dat)
                # ⚠ Names, not numbers: kind 5 means nothing on a screen, and
                # rekordbox's high-mood model is the one dance music uses.
                names = {1: 'intro', 2: 'up', 3: 'down', 4: 'bridge',
                         5: 'chorus', 6: 'outro', 7: 'verse', 8: 'verse',
                         9: 'verse', 10: 'outro'}
                out = []
                for kind, beat in mp.phrase_beats(dat):
                    i = max(0, min(len(grid) - 1, (beat or 1) - 1))
                    if grid:
                        out.append({'kind': names.get(kind, str(kind)),
                                    'at': round(grid[i], 2)})
                self._json({'ok': True, 'phrases': out,
                            'beats': len(grid),
                            'first': round(grid[0], 3) if grid else None})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/mixplan':
            # Where a set's joins should happen, from the beat grid and the
            # phrases rekordbox already worked out. Planning only: see
            # mixplan.py for why playing it is deliberately somewhere else.
            import mixplan as mp, energy_calc as ec
            try:
                ids = [str(i) for i in (data.get('ids') or [])]
                bars = int(data.get('bars') or mp.BARS_DEFAULT)
                lib = all_library() or {}
                by = {str(t['id']): t for t in (lib.get('tracks') or [])}
                paths = ec.anlz_paths()

                def pack(tid):
                    t = by.get(tid) or {}
                    return {'bpm': t.get('bpm'), 'key': t.get('key'),
                            'seconds': t.get('seconds'), 'anlz': paths.get(tid)}

                out = []
                for i in range(len(ids) - 1):
                    p2 = mp.plan(pack(ids[i]), pack(ids[i + 1]), bars)
                    p2['from'] = ids[i]
                    p2['to'] = ids[i + 1]
                    out.append(p2)
                self._json({'ok': True, 'joins': out})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/teach/apply':
            # Writing the tag goes through the tagger's own queue: same
            # baselines, same rekordbox-is-open guard, same audit trail.
            # ⚠ Ticking a track to tag it IS answering yes, so the lesson
            # learns from it too — the list you correct is a training screen,
            # not just an output.
            import tagger, teacher as tc
            try:
                name = (data.get('name') or '').strip()
                ids = [str(i) for i in (data.get('ids') or [])]
                if not name or not ids:
                    self._json({'error': 'nothing to apply'}, 400)
                    return

                def confirm(les):
                    for i in ids:
                        tc.answer(les, {'id': i, 'yes': True})
                tc.change(name, confirm)
                ops = [{'op': 'add_tag', 'track': i, 'tag': name} for i in ids]
                n = tagger.enqueue(ops)
                res = tagger.apply_now_if_possible()
                if res.get('applied'):
                    patch_library_caches(res.get('applied_ops') or [])
                else:
                    with LOCK:
                        bump_library_gen()
                self._json({'ok': True, 'queued': n,
                            'saved': res.get('applied', 0),
                            'deferred': bool(res.get('deferred'))})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)


        elif path == '/api/artwork/find':
            import artwork
            if data.get('stop'):
                artwork.STATE['stop'] = True
                self._json({'ok': True})
            else:
                self._json({'started': artwork.start_find()})

        elif path == '/api/artwork/apply':
            import artwork
            try:
                res = artwork.apply(data.get('picks') or {})
                self._json(res)
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/find/playlist':
            # A Spotify / SoundCloud playlist, song by song against your
            # library — the same matcher and the same bands as the Find
            # screen's own answer (90% = yours, 70% = maybe).
            import playlist_read as plr, library_search as lsr, pairing
            try:
                pl = plr.read(data.get('url'))
                tracks = (all_library() or {}).get('tracks') or []
                by_id = {str(t.get('id')): t for t in tracks}
                prs = pairing.pairs()
                unf = {x.get('key') for x in unfindable_list()}
                # ⚠ Matching 100 songs against the whole library took 9 s.
                # A match always shares a real word with the song, so each one
                # is only compared with the tracks that share one (index built
                # once); the matcher itself is the same.
                import re as _re
                common = {'the', 'and', 'feat', 'featuring', 'mix', 'remix', 'edit', 'original',
                          'extended', 'radio', 'version', 'club', 'dub', 'vip', 'with'}
                def words(*xs):
                    return {w for w in _re.findall(r'[a-z0-9]+', lsr.fold(' '.join(x or '' for x in xs)))
                            if len(w) >= 3 and w not in common}
                index = {}
                for tr in tracks:
                    for w in words(tr.get('title'), tr.get('artist')):
                        index.setdefault(w, []).append(tr)
                out = []
                for t in pl['tracks']:
                    pr = prs.get(t.get('key') or '')
                    mine = by_id.get(pr['track']) if pr else None
                    seen, cand = set(), []
                    for w in words(t['title'], t['artist']):
                        for tr in index.get(w, ()):
                            if id(tr) not in seen:
                                seen.add(id(tr)); cand.append(tr)
                    hits = lsr.best_matches(t['title'], t['artist'], cand, limit=1, floor=0.55)
                    h = hits[0] if hits else None
                    sc = h['score'] if h else 0
                    if h and sc >= 0.9:
                        verdict = 'have' if h['same_version'] else 'other'
                    elif h and sc >= 0.7:
                        verdict = 'maybe'
                    else:
                        verdict = 'missing'
                    row = dict(t, verdict=verdict,
                               unfindable=unfindable_key(t['artist'], t['title']) in unf)
                    if h:
                        # ⚠ Below 70% it is NOT a yes — but it is shown, as
                        # "closest", so you can judge it by eye.
                        row['match'] = {'id': str(h['track']['id']), 'title': h['track'].get('title'),
                                        'artist': h['track'].get('artist'), 'color': h['track'].get('color'),
                                        'score': round(sc, 2), 'theirVersion': h['their_version']}
                    if mine:
                        # A pair you made wins over any guess: it exists to
                        # correct it. The guess travels too (`auto`), so the
                        # screen can undo the pair without asking again.
                        row = dict(t, verdict='have', paired=True, unfindable=False,
                                   auto={'verdict': row['verdict'], 'match': row.get('match'),
                                         'unfindable': row['unfindable']},
                                   match={'id': str(mine['id']), 'title': mine.get('title'),
                                          'artist': mine.get('artist'), 'color': mine.get('color'),
                                          'score': 1.0, 'theirVersion': ''})
                    out.append(row)
                pl['tracks'] = out
                lk = pairing.link_for(pl['url'])
                if lk:
                    import tagger
                    pid = tagger.resolve_playlist(lk.get('rbPlaylist'))
                    name = ''
                    try:
                        import rekordbox as _rb
                        for n in _rb.load_playlist_tree() or []:
                            if str(n.get('id')) == pid:
                                name = n.get('name') or ''
                    except Exception:
                        pass
                    pl['link'] = {'id': lk['id'], 'rbName': name or lk.get('name'),
                                  'pending': pid.startswith('new:'), 'added': lk.get('added') or [],
                                  'lastSync': lk.get('lastSync')}
                self._json(pl)
            except plr.PlaylistError as e:
                self._json({'error': str(e)})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/find/pair':
            import pairing
            key = str(data.get('key') or '')
            if not key:
                self._json({'error': 'no song'}, 400)
                return
            pairing.set_pair(key, data.get('track') or '', data.get('title') or '',
                             data.get('artist') or '', on=data.get('on') is not False)
            self._json({'ok': True})

        elif path == '/api/find/playlist/sync':
            # Make (or update) the rekordbox playlist this streaming playlist
            # feeds: only songs you have — paired, or surely matched — and only
            # the ones it has never put in. Nothing is ever taken out.
            import playlist_read as plr, pairing
            try:
                url = str(data.get('url') or '')
                checked = data.get('tracks') or []      # what the screen showed: key → track id
                resolved = {str(x.get('key')): str(x.get('track')) for x in checked
                            if x.get('key') and x.get('track')}
                pl = plr.read(url)
                link = pairing.link_for(pl['url'])
                res = pairing.sync(pl, resolved, link=link,
                                   create_name=(data.get('name') or '').strip() or None,
                                   existing_playlist=data.get('existing') or None)
                self._json({'ok': True, 'added': res['added'], 'deferred': res['deferred'],
                            'why': res['why'], 'linkId': res['link']['id']})
            except plr.PlaylistError as e:
                self._json({'error': str(e)})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)

        elif path == '/api/find/playlist/unlink':
            import pairing
            pairing.unlink(str(data.get('id') or ''))
            self._json({'ok': True})

        elif path == '/api/find/unfindable':
            import time as _t
            artist = str(data.get('artist') or '').strip()
            title = str(data.get('title') or '').strip()
            query = str(data.get('query') or '').strip()
            key = str(data.get('key') or '') or unfindable_key(artist, title, query)
            if not key.strip():
                self._json({'error': 'nothing to mark'}, 400)
                return
            items = unfindable_set({'key': key, 'artist': artist, 'title': title,
                                    'query': query, 'app': str(data.get('app') or ''),
                                    'at': _t.strftime('%Y-%m-%d %H:%M')},
                                   on=data.get('on') is not False)
            self._json({'ok': True, 'items': items})


        elif path == '/api/tags/scan':
            # What the shops wrote into the library. Reads only.
            import tag_clean as tcl, rekordbox as rbx
            try:
                tracks, _ = rbx.load_tracks()
                files = [t['path'] for t in tracks
                         if t.get('path') and os.path.exists(t['path'])]
                found = tcl.scan(files, workers=8)
                self._json({'looked': len(files), 'found': [
                    {'path': f['path'], 'name': os.path.basename(f['path']),
                     'brand': [str(x) for x in f['brand']],
                     'album': [str(x) for x in f['album']],
                     'cover': f['cover'], 'kind': f['kind']}
                    for f in found]})
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)

        elif path == '/api/tags/clean':
            # ⚠ Tags only: the audio frames are never rewritten (see
            # tag_clean.py for why that had to stop being ffmpeg's job).
            import tag_clean as tcl
            try:
                paths = data.get('paths') or []
                drop_album = bool(data.get('dropAlbum'))
                drop_cover = data.get('dropCover') is not False
                done, failed = [], []
                for p2 in paths:
                    p2 = clean_path(p2)
                    if not p2 or not os.path.isfile(p2):
                        failed.append({'path': p2, 'why': 'no hi és'})
                        continue
                    try:
                        r = tcl.clean(p2, drop_album=drop_album,
                                      drop_cover=drop_cover)
                        if r['changed']:
                            done.append({'name': os.path.basename(p2),
                                         'removed': r['removed'],
                                         'cover': r['cover']})
                    except Exception as e:
                        failed.append({'path': p2, 'why': str(e)[:120]})
                self._json({'cleaned': done, 'failed': failed})
            except Exception as e:
                self._json({'error': str(e)[:300]}, 500)


        elif path == '/api/storage/clear':
            import storage
            try:
                self._json(storage.clear(str(data.get('key') or '')))
            except Exception as e:
                self._json({'error': str(e)[:300]}, 400)

        elif path == '/api/storage/budget':
            # The size limit for library backups. The screen has already shown
            # what it removes; this applies it straight away.
            import library_backup as lb
            mb = max(0, int(data.get('mb') or 0))
            prefs.save({'backup_budget_mb': mb})
            removed = lb.prune() if mb else []
            self._json({'ok': True, 'mb': mb, 'removed': len(removed)})

        elif path == '/api/storage/reveal':
            # ⚠ Only the app's own two folders, by name — never a path from the page.
            import storage
            where = {'app': prefs.APP_DIR, 'backups': prefs.BACKUP_DIR,
                     'previews': storage.preview_dir()}.get(str(data.get('where') or ''))
            if where and os.path.isdir(where):
                open_in_file_manager(where)
            self._json({'ok': bool(where)})

        elif path == '/api/access/open':
            import access
            self._json({'ok': access.open_pane(str(data.get('pane') or ''))})

        elif path == '/api/vocabulary/apply':
            # Adds the starter MyTag groups and colour labels (vocabulary.py).
            # Only adds; refuses while rekordbox is open, backs up first.
            import vocabulary
            try:
                res = vocabulary.apply(with_colours=data.get('colours', True) is not False)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 400)
                return
            with LOCK:
                RB_ALL.clear()
                RB_LIBRARY.clear()
                bump_library_gen()
            _SLOTS['val'] = None            # the colour labels may have new names
            self._json(res)

        elif path == '/api/dupes/ignore':
            key = str(data.get('key') or '').strip()
            if not key:
                self._json({'error': 'no group given'}, 400)
                return
            cur = dupes_ignore(key, bool(data.get('on', True)))
            self._json({'ok': True, 'ignored': len(cur)})

        elif path == '/api/browse/delete':
            # ⚠ Removes the rekordbox row AND bins the file. Both recoverable:
            # the row is soft-deleted the way rekordbox does it, the file goes
            # to the Trash. Never call this without the user having said so.
            import library_edit as le
            try:
                res = le.delete_tracks(data.get('ids') or [],
                                       remove_files=bool(data.get('removeFiles', True)),
                                       dry_run=bool(data.get('dryRun')))
                with LOCK:
                    RB_ALL.clear()        # the list on screen is now stale
                    RB_LIBRARY.clear()
                    # ⚠ Deleting changes formats, duplicate groups AND the set
                    # of paths playback is allowed to serve.
                    RB_COMPAT.clear(); RB_GROUPS[:] = []; RB_QUALITY.clear()
                    _forget_library_files()
                    bump_library_gen()
                self._json(res)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 400)

        elif path == '/api/rekordbox/import':
            # Put what the converter just produced into rekordbox, stamped with
            # the "needs a decision" colour so it lands in the Tagger's queue.
            import rekordbox_import as ri
            import rekordbox_merge as rm
            if rm.rekordbox_running():
                self._json({'error': 'Close rekordbox to import into it.',
                            'needsClose': True}, 400)
                return
            with LOCK:
                out = STATE['output'] or DEFAULT_OUTPUT
                produced = [os.path.join(out, it['final'])
                            for it in ITEMS if it.get('final')]
            if not produced:
                self._json({'added': 0, 'skipped': 0, 'failed': [],
                            'note': 'Nothing converted to import.'})
                return
            try:
                res = ri.import_files(
                    produced, color=(data.get('color') or None),
                    log=lambda lvl, txt: tg_log(lvl, txt))
                if res.get('added'):
                    # ⚠ New rows exist that the cached library has never seen:
                    # without this, an imported track was invisible in Browse
                    # until the server restarted.
                    with LOCK:
                        RB_ALL.clear()
                        RB_LIBRARY.clear()
                        RB_COMPAT.clear()
                        _forget_library_files()
                        bump_library_gen()
                self._json(res)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 400)


        elif path == '/api/flac/start':
            # ⚠ CLAIM the job in the same lock as the check. The rekordbox
            # process scan between them takes ~50-300 ms, and two clicks inside
            # that window both passed and started two in-place converters over
            # the same files.
            with LOCK:
                if STATE['fl']['phase'] == 'running':
                    self._json({'error': 'A conversion is already running.'}, 409)
                    return
                STATE['fl'] = {'phase': 'running', 'error': None, 'done': 0,
                               'total': 0, 'ok': 0, 'fail': 0,
                               'plan': STATE['fl'].get('plan'), 'stop': False}
                FL_LOG[:] = []
            import rekordbox_merge as rm
            if rm.rekordbox_running():
                with LOCK:
                    STATE['fl']['phase'] = 'idle'      # hand the claim back
                self._json({'error': 'Close rekordbox before converting.'}, 400)
                return
            lim = data.get('limit')
            threading.Thread(target=flac_worker,
                             args=(bool(data.get('deleteOriginals')),
                                   int(lim) if lim else None),
                             daemon=True).start()
            self._json({'ok': True})

        elif path == '/api/flac/stop':
            with LOCK:
                STATE['fl']['stop'] = True
            self._json({'ok': True})

        elif path == '/api/rekordbox/status':
            try:
                import rekordbox_merge as rm
                self._json({'running': rm.rekordbox_running()})
            except Exception:
                self._json({'running': False})

        elif path == '/api/reveal':
            target = clean_path(data.get('path'))
            if not path_allowed(target):
                self._json({'error': 'That path is not in your library.'}, 400)
            elif os.path.exists(target):
                open_in_file_manager(target, select=True)
                self._json({'ok': True})
            else:
                self._json({'error': 'That file is gone.'}, 400)

        elif path == '/api/backups/now':
            # A few seconds of work: answered at once, the screen polls.
            import library_backup
            threading.Thread(target=library_backup.run, kwargs={'force': True},
                             daemon=True).start()
            self._json({'ok': True})

        elif path == '/api/backups/reveal':
            # ⚠ Only names that are backups of ours: a bare path from the page
            # would make this a "show me any file" endpoint.
            import library_backup
            target = library_backup.path_of(data.get('name'))
            open_in_file_manager(target or library_backup.BACKUP_DIR, select=bool(target))
            self._json({'ok': True})


        elif path == '/api/tagger/enqueue':
            # ⚠ Nothing is written to rekordbox here. Tagging happens while you
            # browse, which is precisely when rekordbox may be open, so every
            # change waits in the queue until it can be applied safely.
            import tagger
            try:
                n = tagger.enqueue(data.get('ops') or [])
                # ⚠ Write it through NOW if rekordbox is closed. The queue was
                # never the point — it exists only because master.db cannot be
                # written while rekordbox holds it. With it shut there is
                # nothing to wait for, so an edit simply lands and you never
                # have to think about syncing at all.
                res = tagger.apply_now_if_possible()
                if res.get('applied'):
                    patch_library_caches(res.get('applied_ops') or [])
                else:
                    # ⚠ The edit is QUEUED, not written — but every screen shows
                    # effective values (library + queue), so the picture HAS
                    # changed and the other surface must refetch. Bumping only
                    # on a real write meant that with rekordbox open the two
                    # screens quietly disagreed until you clicked into one.
                    with LOCK:
                        bump_library_gen()
                # ⚠ `skipped` and `failed` travel to the client. They used to be
                # computed and thrown away, so an edit the server had DISCARDED
                # was reported as saved — the worst possible answer.
                self._json({'ok': True, 'queued': n,
                            'saved': res.get('applied', 0),
                            'deferred': bool(res.get('deferred')),
                            'skipped': res.get('skipped') or [],
                            'failed': res.get('failed') or [],
                            'why': res.get('why', '')})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 400)

        elif path == '/api/tagger/apply':
            # ⚠ The manual Sync must do EXACTLY what the auto-save path does:
            # take the write lock and patch the caches. Skipping either brought
            # back both auto-save bugs through this door — stale rows on every
            # screen, and poisoned `before` baselines that silently dropped the
            # next edit.
            import tagger
            dry = bool(data.get('dry_run', True))
            try:
                if dry:
                    self._json(tagger.apply_queue(dry_run=True))
                else:
                    with tagger._APPLY_LOCK:
                        res = tagger.apply_queue(dry_run=False)
                    if res.get('applied'):
                        patch_library_caches(res.get('applied_ops') or [])
                    self._json(res)
            except Exception as e:
                self._json({'error': str(e)[:200]}, 400)

        elif path == '/api/tagger/nav':
            # The menu bar asking the Tagger to move through the queue.
            import tagger
            self._json(tagger.request_nav(data.get('dir')))

        elif path == '/api/tagger/seek':
            # The menu bar asking the Tagger to jump inside the track.
            import tagger
            self._json(tagger.request_seek(data.get('frac')))

        elif path == '/api/tagger/play':
            # The menu bar asking the Tagger to start or stop.
            import tagger
            self._json(tagger.request_play())

        elif path == '/api/restart':
            # "Restart" in the rail, after an update. Answer first, then go.
            self._json({'ok': True})
            threading.Timer(0.3, _restart_self).start()

        elif path == '/api/tag-order':
            # One group's tags in the order you dragged them into. ⚠ Saved on
            # its own, NOT through /api/settings: that one throws the library
            # caches away (a 40 MB re-read) for settings that change what the
            # library looks like, and an order changes nothing but the screen.
            bank = str(data.get('bank') or '').strip()
            names = [str(n) for n in (data.get('order') or []) if str(n).strip()]
            if not bank:
                self._json({'error': 'no group given'}, 400)
                return
            order = dict(prefs.load().get('tag_order') or {})
            if names:
                order[bank] = names
            else:
                order.pop(bank, None)
            prefs.save({'tag_order': order})
            with LOCK:
                LIB_GEN['n'] += 1           # the other screen redraws its banks
            self._json({'ok': True, 'tag_order': order})

        elif path == '/api/tagger/volume':
            # The menu bar's volume slider, for the player in the app window.
            import tagger
            self._json(tagger.request_volume(data.get('vol')))

        elif path == '/api/tagger/triage-next':
            # "Done, next": the next track still waiting to be triaged, after
            # the one you were on — whatever Browse happens to have filtered.
            import tagger
            with LOCK:
                cached = dict(RB_ALL) if RB_ALL else None
            if not cached:
                self._json({'track': None, 'left': 0, 'warming': True})
                return
            color = prefs.load().get('import_color')
            frm = data.get('from') or tagger.get_now().get('track')
            nxt, left = tagger.triage_next(cached.get('tracks', []), color, frm)
            row = None
            if nxt:
                row = {k: nxt.get(k) for k in
                       ('id', 'title', 'artist', 'genres', 'bpm', 'key', 'seconds',
                        'rating', 'color', 'tags', 'added', 'path', 'kind',
                        'kbps', 'comment')}
                row['id'] = str(row['id'])
                # ⚠ BOTH: point the popover at it straight away, and ask the
                # browser to play it. With only the second, the popover sat on
                # the finished track until the player reported back — and
                # with no player open, forever.
                tagger.set_now(row['id'])
                tagger.request_load(row)
            self._json({'track': row, 'left': left})

        elif path == '/api/tagger/pulse':
            # The browser's heartbeat: it says where the playhead is and gets
            # back any pending order in the SAME round trip — one request for
            # both directions rather than two polls chasing each other.
            import tagger
            self._json(tagger.set_playback(data.get('pos'), data.get('dur'),
                                           data.get('playing'), data.get('vol')))

        elif path == '/api/tagger/now':
            # The browser telling us which track it has open (and playing), so
            # the menu bar popover can act on the same one.
            import tagger
            print('[now] report rebut: track=%r' % (data.get('track'),), flush=True)
            self._json(tagger.set_now(data.get('track')))

        elif path == '/api/stop':
            with LOCK:
                STATE['stop'] = True
            self._json({'ok': True})

        elif path == '/api/open-output':
            target = clean_path(data.get('output')) or STATE['output'] or DEFAULT_OUTPUT
            if os.path.isdir(target):
                open_in_file_manager(target)
                self._json({'ok': True})
            else:
                self._json({'error': 'That folder does not exist.'}, 400)

        else:
            self.send_error(404)




def queue_flusher():
    """Drain the deferred queue as soon as rekordbox is closed.

    ⚠ Edits made while rekordbox is open are queued, and until now the only
    thing that ever drained them was making ANOTHER edit with it shut — the
    Sync button lives in a view that is never shown. Work could sit there for
    days without a hint. This watches quietly: nothing to press, nothing to
    remember.
    """
    import time
    while True:
        time.sleep(20)
        try:
            import tagger
            if not tagger.pending().get('count'):
                continue
            res = tagger.apply_now_if_possible()
            if res.get('applied'):
                patch_library_caches(res.get('applied_ops') or [])
        except Exception:
            pass          # a flush that fails just waits for the next round


def warm_imports():
    """Import pyrekordbox once, here, in the main thread.

    ⚠ This is not an optimisation. Every module that talks to rekordbox
    imports pyrekordbox lazily, inside the function that needs it. That is
    fine single-threaded, but this is a THREADING server: two requests
    arriving together (the merge dry run and the queue counter, say) start
    the same import in two threads at once, each ends up waiting on the
    other's module lock, and Python aborts the request with

        deadlock detected by _ModuleLock('pyrekordbox.utils')

    Importing it once before the server accepts anything means every later
    lazy import is already satisfied and instant.
    """
    try:
        import pyrekordbox                      # noqa: F401
        import pyrekordbox.utils                # noqa: F401
        import pyrekordbox.db6.tables           # noqa: F401
        import pyrekordbox.anlz                 # noqa: F401
        import sqlcipher3                       # noqa: F401
    except Exception:
        # A machine without rekordbox still runs the Convert tab, so a
        # failure here is not fatal — it only means no warm-up.
        pass


# ⚠ Chrome, not "whatever the system calls the default". This tab is not a
# viewer, it IS the player — the audio element, the waveform and the playhead
# all live in it — so pinning it to one browser means one set of behaviours to
# reason about instead of two. Falls back to the default if Chrome is missing.
BROWSER_APP = 'Google Chrome'


# The Mac app (app/ in this folder, installed by app/build.sh). It finds this
# server by itself, so opening it is all it takes.
APP_NAMES = ('Backspins', 'Backspin', 'Rekordbox Toolkit')   # the others: earlier names


def open_in_browser(url):
    """Open the toolkit: the Mac app if it is installed, else Chrome, else
    whatever browser is the default."""
    if platform.system() == 'Darwin':
        for cmd in [['open', '-a', n] for n in APP_NAMES] + [['open', '-a', BROWSER_APP, url]]:
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=10)
                return
            except Exception:
                pass                  # not installed, or moved — try the next
    webbrowser.open(url)


def helper_command(name):
    """How to run one of the helper programs (musicdna, menubar) as its own
    process. From source it is that script; packaged there are no scripts,
    so the app runs itself with a switch that desktop.py turns into it."""
    if FROZEN:
        return [sys.executable, '--' + name]
    return [sys.executable, os.path.join(TOOL_DIR, name + '.py')]


def start_menubar(port):
    """Bring up the macOS menu bar icon next to the server. Best-effort.

    ⚠ Started by the SERVER, not by the launcher, because only the server knows
    which port it ended up on: it walks forward when 8765 is busy, and a menu
    bar that assumed the number ended up talking to a different instance than
    the one on screen — showing "Nothing open" while a track was open and
    playing. It refuses to start twice on its own, so this is safe to call
    whenever. Skipped off macOS, or without PyObjC.
    """
    if platform.system() != 'Darwin':
        return
    if not FROZEN and not os.path.exists(os.path.join(TOOL_DIR, 'menubar.py')):
        return
    if importlib.util.find_spec('objc') is None:
        return
    try:
        _MENUBAR['proc'] = subprocess.Popen(helper_command('menubar'),
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ⚠ The menu bar icon goes when the server goes. It is a remote for THIS
# server; left behind it sits in the menu bar saying "Nothing open" with no
# window anywhere — which is how it looked like the toolkit was still running
# after the terminal was closed. Quitting the app sends SIGTERM, closing the
# terminal SIGHUP, and both now take the icon down on the way out.
_MENUBAR = {'proc': None}


def _stop_menubar():
    p = _MENUBAR.get('proc')
    if p is not None and p.poll() is None:
        try:
            p.terminate()
        except OSError:
            pass


def _stop_on_signal(signum, frame):
    """Stop NOW. ⚠ Not SystemExit: that lets Python wind down politely and wait
    for every thread a library left running, and it took the server more than
    ten seconds to go — long enough for the app to be reopened on top of a
    server that no longer answered but still held the port. Nothing is lost by
    going at once: the queue file is replaced atomically and every database
    write is a SQLite transaction, which is all or nothing."""
    _stop_menubar()
    os._exit(0)


# ── an update, without anyone having to remember to restart ─────────────────
# The pages are read from disk on every request, but the server's own code is
# what it was when it started. After an update the screens can ask for
# something the running server does not know yet (a 404), and the change
# looks saved and is not. So the server notices its code has moved on, the
# screens say so, and one click restarts it in place.
def code_stamp():
    """When the server's code last changed: its .py files and the popover."""
    newest = 0.0
    try:
        names = os.listdir(TOOL_DIR)
    except OSError:
        return 0.0
    for name in names:
        if name.endswith('.py') or name == 'menubar.html':
            try:
                newest = max(newest, os.stat(os.path.join(TOOL_DIR, name)).st_mtime)
            except OSError:
                pass
    return newest


BOOT = {'stamp': 0.0}


def page_stamp():
    try:
        return os.stat(os.path.join(TOOL_DIR, 'convertidor.html')).st_mtime
    except OSError:
        return 0.0


def _restart_self():
    """Become a fresh copy of this server, on the same port and the same PID.

    ⚠ The same PID matters: the Mac app keeps the server it started by process
    and stops it on Quit, and it still finds it after this. The menu bar icon
    goes first; the new server starts a new one, running the new code.
    """
    _stop_menubar()
    args = [a for a in sys.argv[1:]]
    if '--no-browser' not in args:
        args.append('--no-browser')     # it is already open somewhere
    if FROZEN:
        # The packaged app runs the server as `Backspins --server …`; desktop.py
        # takes the switch out of argv before main() reads it.
        os.execv(sys.executable, [sys.executable, '--server'] + args)
    vpy = os.path.join(TOOL_DIR, '.venv', 'bin', 'python3')
    py = vpy if os.path.exists(vpy) else sys.executable
    os.execv(py, [py, os.path.abspath(__file__)] + args)


def affinity_model(tracks=None):
    """The learned transition model (affinity.py), rebuilt when the library changes.

    Shared by the Sets tab and the phone: one model, never two copies of it.
    """
    import affinity as af
    with LOCK:
        gen = LIB_GEN['n']
        model = AFFINITY.get('model') if AFFINITY.get('gen') == gen else None
    if model is None:
        import rekordbox as rb
        if tracks is None:
            tracks = (all_library() or {}).get('tracks') or []
        model = af.learn(tracks, (rb.load_history() or {}).get('follows'))
        with LOCK:
            AFFINITY['gen'], AFFINITY['model'] = gen, model
    return model




def _playlist_tree():
    try:
        import rekordbox as rb
        return rb.load_playlist_tree()
    except Exception:
        return []


def save_set_playlist(name, ids, playlist_id=None):
    """What POST /api/playlist does, for a set made on the phone."""
    import rekordbox_merge as rm
    # ⚠ A backup at most every ten minutes from here: sets from the phone come
    # in bursts, and a full copy of master.db per set could fill the disk.
    res = rm.create_set_playlist(name, ids, playlist_id=playlist_id, backup_max_age=600)
    with LOCK:
        RB_LIBRARY.clear()
        RB_ALL.clear()
        bump_library_gen()
    return res




def main():
    ap = argparse.ArgumentParser(description='Backspins (local server)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    # The desktop app (desktop.py) learns the port this way: the server walks
    # forward from --port when it is busy, so only the server knows.
    ap.add_argument('--port-file', default='')
    args = ap.parse_args()

    # A first run has no app folder yet, and several things (the audio
    # analysis' job file among them) write into it before anything else
    # would have made it.
    os.makedirs(prefs.APP_DIR, exist_ok=True)
    prefs.tighten()
    BOOT['stamp'] = code_stamp()
    STATE['converter'] = detect_converter()
    warm_imports()
    threading.Thread(target=queue_flusher, daemon=True).start()
    # Read the label colours now, so the first screen does not wait on it.
    threading.Thread(target=label_slots, daemon=True).start()
    # A copy of the library every hour it changes, kept by age, with the text
    # versions (rekordbox XML + every table as JSON) that outlive the key.
    import library_backup
    library_backup.start()

    httpd = None
    port = args.port
    for p in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(('127.0.0.1', p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit('No free port available.')
    httpd.daemon_threads = True

    url = f'http://127.0.0.1:{port}'
    if args.port_file:
        with open(args.port_file, 'w') as fh:
            fh.write(str(port))
    print('Backspins')
    print(f'  UI:        {url}')
    print(f'  Converter: {STATE["converter"] or "NONE (install ffmpeg)"}')
    print(f'  Output:    {DEFAULT_OUTPUT}')
    print('  Stop with Ctrl+C (or by closing this window).')
    if not args.no_browser:
        threading.Timer(0.4, open_in_browser, [url]).start()
    # Give the server a moment to answer before the icon goes looking for it.
    threading.Timer(1.0, start_menubar, [port]).start()
    # ⚠ Windows has no SIGHUP: naming it there killed the server at startup.
    for sig in (signal.SIGTERM, getattr(signal, 'SIGHUP', None)):
        if sig is not None:
            signal.signal(sig, _stop_on_signal)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        _stop_menubar()
        dna_stop()


if __name__ == '__main__':
    main()
