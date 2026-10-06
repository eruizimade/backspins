"""Folders full of music you already have — and that rekordbox never plays.

They pile up without anyone deciding to keep them: the FLACs a conversion
set aside, a "backup" copied before a reinstall, a downloader's "processed"
folder, a zip unpacked twice. Each one is gigabytes of the same records.

What makes a folder worth showing here, all three at once:

  · rekordbox uses NONE of its files. A folder the library plays from is
    never offered, however many copies it holds — those are a job for the
    Duplicates tab, which can merge the work across first.
  · most of its music is ALREADY in the library, matched one of two ways:
      identical   same size and the same bytes (sampled at start, middle and
                  end) as a library file — a plain copy;
      same record same artist and title (rekordbox's own duplicate rules)
                  and the same length within 3 s — another file of the same
                  recording: the FLAC behind a converted AIFF, an MP3 of a
                  WAV you bought.
  · it is not one of the places themselves (Music, Downloads…): loose
    duplicates there are offered one by one, never the folder.

⚠ Nothing is deleted. Everything goes to the Bin, and only after the folder
is looked at AGAIN, just before: a file the library took up since the scan,
music that matches nothing, or anything that is not music or its clutter
(covers, .asd, playlists, notes) turns "the folder" into "only the
duplicates in it".

⚠ "Not used" means not used by rekordbox. Serato, Engine, Traktor or a DAW
project may still point at these files; the screen says so.
"""

import os
import platform
import subprocess
import sys
import time

import rekordbox as rb

AUDIO_EXTS = ('.flac', '.wav', '.aif', '.aiff', '.mp3', '.m4a', '.aac',
              '.ogg', '.opus', '.alac', '.wma')

#: What a music folder carries besides the music. Binned with it: none of it
#: is anything on its own.
CLUTTER_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.txt',
                '.nfo', '.m3u', '.m3u8', '.pls', '.cue', '.log', '.sfv',
                '.md5', '.asd', '.reapeaks', '.pk', '.url', '.webloc', '.ini',
                '.db', '.lrc', 'icon\r')

#: A folder is offered when this share of its music is already in the library.
MIN_SHARE = 0.6
#: …and when it holds at least this many duplicates.
MIN_DUPES = 2

#: Same recording: rekordbox's own tolerance for "same length".
SAME_LENGTH = rb.SAME_DURATION_TOLERANCE

#: Never walked into. Bundles are one thing to the user, not folders; the
#: rest is the system's or a program's own business.
_SKIP_SUFFIX = ('.app', '.logicx', '.band', '.photoslibrary', '.musiclibrary',
                '.tvlibrary', '.bundle', '.framework', '.pkg', '.zip')
_SKIP_NAMES = {'node_modules', '.git', '.Trash', 'Library', '__pycache__',
               '.venv', 'venv', 'PIONEER', 'Contents', 'Serato Packs'}

_SAMPLE = 64 * 1024


def places(tracks):
    """Where to look: the usual places plus wherever the library lives.

    The top-level folder of every library file (~/Music, a drive's root) is
    where its stray copies tend to end up as well.
    """
    home = os.path.expanduser('~')
    roots = []
    for name in ('Music', 'Downloads', 'Desktop'):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            roots.append(p)
    for t in tracks:
        p = os.path.abspath(t.get('path') or '')
        if p.startswith(home + os.sep):
            top = os.path.join(home, p[len(home) + 1:].split(os.sep, 1)[0])
        elif platform.system() == 'Darwin' and p.startswith('/Volumes/'):
            top = '/'.join(p.split('/')[:3])
        else:
            top = os.path.dirname(p)
        if top not in roots and os.path.isdir(top) and top != home:
            roots.append(top)
    # A root inside another root is already covered.
    roots.sort(key=len)
    out = []
    for r in roots:
        if not any(r == o or r.startswith(o + os.sep) for o in out):
            out.append(r)
    return out


def _ident(st):
    return (st.st_dev, st.st_ino)


def _sample(path, size):
    """Start, middle and end of a file: same size + same samples = a copy."""
    try:
        with open(path, 'rb') as fh:
            parts = [fh.read(_SAMPLE)]
            if size > 3 * _SAMPLE:
                fh.seek(size // 2)
                parts.append(fh.read(_SAMPLE))
                fh.seek(size - _SAMPLE)
                parts.append(fh.read(_SAMPLE))
            return b''.join(parts)
    except OSError:
        return None


def _tags(path):
    """(artist, title, seconds) from the file, the name filling any gap."""
    artist = title = ''
    secs = None
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is not None:
            if f.info and getattr(f.info, 'length', None):
                secs = float(f.info.length)
            tags = f.tags or {}
            try:
                artist = (tags.get('artist') or [''])[0]
                title = (tags.get('title') or [''])[0]
            except Exception:
                pass
    except Exception:
        pass
    if not title:
        stem = os.path.splitext(os.path.basename(path))[0]
        # "01. Artist - Title", "Artist - Title"
        stem = stem.lstrip('0123456789').lstrip(' .-_') or stem
        if ' - ' in stem:
            a, _, title = stem.partition(' - ')
            artist = artist or a
        else:
            title = stem
    return str(artist), str(title), secs


def _stem(path):
    return rb.norm_text(os.path.splitext(os.path.basename(path))[0])


def _key(artist, title):
    base, version = rb.split_title(title)
    return (rb.primary_artist(artist), base, version) if base else None


class _Library:
    """What the library has, indexed the two ways a stray copy is matched."""

    def __init__(self, tracks):
        self.used = set()           # (dev, inode) of every file rekordbox uses
        self.by_size = {}
        self.by_key = {}
        self.by_stem = {}
        for t in tracks:
            p = t.get('path') or ''
            try:
                st = os.stat(p)
            except OSError:
                continue
            self.used.add(_ident(st))
            self.by_size.setdefault(st.st_size, []).append((p, t))
            k = _key(t.get('artist'), t.get('title'))
            if k:
                self.by_key.setdefault(k, []).append(t)
            self.by_stem.setdefault(_stem(p), []).append(t)

    def match(self, path, size):
        """(how, library path) when this file is already in the library."""
        for lp, _t in self.by_size.get(size, ()):
            a = _sample(path, size)
            if a is not None and a == _sample(lp, size):
                return 'identical', lp
        artist, title, secs = _tags(path)
        if secs is None:
            return None, None
        # By tags, then by file name: the tags in the library are often the
        # ones you corrected, and the copy left behind still has the old ones.
        k = _key(artist, title)
        for t in (self.by_key.get(k, []) if k else []) + self.by_stem.get(_stem(path), []):
            if t.get('rb_length') and abs(t['rb_length'] - secs) <= SAME_LENGTH:
                return 'same', t['path']
        return None, None


def _walk(root, skip):
    """Every file under root, as (dir, name, stat). Hidden things skipped."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            name = e.name
            if name.startswith('.'):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if (name in _SKIP_NAMES or name.lower().endswith(_SKIP_SUFFIX)
                            or os.path.realpath(e.path) in skip):
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    yield d, name, e.stat(follow_symlinks=False)
            except OSError:
                continue


def _blank():
    return {'audio': 0, 'used': 0, 'dupes': 0, 'unique': 0, 'other': 0,
            'bytes': 0, 'dupeBytes': 0}


def scan(tracks, skip=(), progress=None, stop=None):
    """Folders of music the library already has and does not use.

    Returns {'folders': [...], 'loose': [...], 'places': [...], ...}. Each
    folder lists its duplicates (with where the library's copy is), the music
    in it that matches nothing, and whether it can go to the Bin whole.
    """
    t0 = time.time()
    lib = _Library(tracks)
    roots = places(tracks)
    skip = {os.path.realpath(s) for s in skip if s}

    # Pass 1: names and sizes only (fast), to know what to open.
    files = []
    for r in roots:
        for d, name, st in _walk(r, skip):
            files.append((r, d, name, st))
            if stop and stop():
                return None
    audio = [f for f in files if f[2].lower().endswith(AUDIO_EXTS)]

    # Pass 2: every music file rekordbox does not use, matched.
    per_dir = {}
    for i, (r, d, name, st) in enumerate(audio):
        if stop and stop():
            return None
        if progress and i % 25 == 0:
            progress(i, len(audio))
        p = os.path.join(d, name)
        if _ident(st) in lib.used:
            how, orig = 'used', None
        else:
            how, orig = lib.match(p, st.st_size)
        per_dir.setdefault(d, []).append((name, st.st_size, how, orig))
    if progress:
        progress(len(audio), len(audio))

    # Totals for every folder, its subfolders included.
    totals = {}
    root_of = {}
    for r, d, name, st in files:
        root_of[d] = r
    for r, d, name, st in files:
        low = name.lower()
        if low.endswith(AUDIO_EXTS) or low.endswith(CLUTTER_EXTS):
            continue
        x = d
        while True:
            totals.setdefault(x, _blank())['other'] += 1
            if x == r or len(x) <= len(r):
                break
            x = os.path.dirname(x)
    for d, rows in per_dir.items():
        r = root_of[d]
        for name, size, how, orig in rows:
            x = d
            while True:
                tt = totals.setdefault(x, _blank())
                tt['audio'] += 1
                tt['bytes'] += size
                if how == 'used':
                    tt['used'] += 1
                elif how:
                    tt['dupes'] += 1
                    tt['dupeBytes'] += size
                else:
                    tt['unique'] += 1
                if x == r or len(x) <= len(r):
                    break
                x = os.path.dirname(x)

    def offered(d):
        t = totals.get(d)
        return (t and not t['used'] and t['dupes'] >= MIN_DUPES
                and t['dupes'] >= MIN_SHARE * t['audio'])

    # The topmost folder that qualifies, never the places themselves.
    folders = []
    for d in sorted(totals, key=len):
        r = root_of.get(d) or next((x for x in roots if d.startswith(x + os.sep)), None)
        if d in roots or not r or not offered(d):
            continue
        if any(d.startswith(f['path'] + os.sep) for f in folders):
            continue
        folders.append(_describe(d, totals[d], per_dir))
    folders.sort(key=lambda f: f['dupeBytes'], reverse=True)

    # Loose copies sitting straight in one of the places.
    loose = []
    for r in roots:
        rows = [x for x in per_dir.get(r, []) if x[2] not in (None, 'used')]
        if rows:
            loose.append({'path': r, 'dupes': len(rows),
                          'dupeBytes': sum(x[1] for x in rows),
                          'files': [_file(r, x) for x in rows[:200]]})
    return {'folders': folders, 'loose': loose, 'places': roots,
            'scanned': len(audio), 'seconds': round(time.time() - t0, 1),
            'at': time.time()}


def _file(d, row):
    name, size, how, orig = row
    return {'name': name, 'size': size, 'how': how, 'path': os.path.join(d, name),
            'orig': orig}


def _describe(d, t, per_dir):
    dupes, unique = [], []
    for x, rows in per_dir.items():
        if x == d or x.startswith(d + os.sep):
            for row in rows:
                (dupes if row[2] not in (None, 'used') else unique).append(_file(x, row))
    identical = sum(1 for f in dupes if f['how'] == 'identical')
    return {'path': d, 'audio': t['audio'], 'dupes': t['dupes'],
            'unique': t['unique'], 'other': t['other'], 'bytes': t['bytes'],
            'dupeBytes': t['dupeBytes'], 'identical': identical,
            # The whole folder only when nothing of yours goes with it.
            'whole': not t['unique'] and not t['other'],
            'example': dupes[0] if dupes else None,
            'uniqueFiles': unique[:30],
            'dupeFiles': [f['path'] for f in dupes]}


# --------------------------------------------------------------- binning

def _trash_many(paths):
    """Several things to the Bin in one go (one Finder call, not hundreds)."""
    if platform.system() != 'Darwin':
        import rekordbox_merge as rm
        for p in paths:
            rm.trash_file(p)
        return
    for i in range(0, len(paths), 150):
        chunk = paths[i:i + 150]
        items = ', '.join('POSIX file "%s"' % p.replace('\\', '\\\\').replace('"', '\\"')
                          for p in chunk)
        r = subprocess.run(['osascript', '-e',
                            'tell application "Finder" to delete {%s}' % items],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or 'could not move to the Bin').strip()[:200])


def bin_folder(folder, result, tracks, whole=True):
    """Bin a folder from the last scan — after looking at it once more.

    `whole` asks for the folder itself; it is honoured only if the folder,
    looked at now, still holds nothing but duplicates and their clutter.
    Otherwise only the files that are still duplicates go.
    """
    entry = next((f for f in (result or {}).get('folders', []) if f['path'] == folder), None)
    loose = next((f for f in (result or {}).get('loose', []) if f['path'] == folder), None)
    if not entry and not loose:
        raise ValueError('That folder is not in the last scan. Scan again.')
    lib = _Library(tracks)
    roots = (result or {}).get('places') or []
    if entry:
        candidates = []
        for d, name, st in _walk(folder, ()):
            low = name.lower()
            if low.endswith(AUDIO_EXTS):
                candidates.append((os.path.join(d, name), st))
            elif not low.endswith(CLUTTER_EXTS):
                whole = False
    else:
        whole = False
        candidates = []
        for f in loose['files']:
            try:
                candidates.append((f['path'], os.stat(f['path'])))
            except OSError:
                pass
    go, kept = [], []
    for p, st in candidates:
        if _ident(st) in lib.used:
            kept.append(p)
            continue
        how, _ = lib.match(p, st.st_size)
        (go if how else kept).append(p)
    if kept:
        whole = False
    if folder in roots or folder == os.path.expanduser('~'):
        whole = False
    if whole:
        _trash_many([folder])
        return {'binned': len(go), 'whole': True, 'kept': 0}
    if go:
        _trash_many(go)
    return {'binned': len(go), 'whole': False, 'kept': len(kept)}


if __name__ == '__main__':
    tracks, _ = rb.load_tracks()

    def say(done, total):
        sys.stderr.write('\r%d / %d' % (done, total))
    res = scan(tracks, progress=say)
    sys.stderr.write('\n')
    gb = lambda b: '%.1f GB' % (b / 1e9)
    print('looked in:', ', '.join(res['places']), '·', res['scanned'], 'music files,',
          res['seconds'], 's')
    for f in res['folders']:
        print('%-70s %4d of %4d already in the library (%d identical) · %s%s'
              % (f['path'][-70:], f['dupes'], f['audio'], f['identical'],
                 gb(f['dupeBytes']), '' if f['whole'] else
                 '  · keeps %d other / %d unmatched' % (f['other'], f['unique'])))
    for f in res['loose']:
        print('loose in %s: %d · %s' % (f['path'], f['dupes'], gb(f['dupeBytes'])))
