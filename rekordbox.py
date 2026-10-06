"""Reading a rekordbox 6/7 library, and spotting likely duplicates.

The database (`master.db`) is encrypted with SQLCipher. Every read here happens
on a temporary COPY: rekordbox's own file is never touched and never locked.

Quality is NOT taken from rekordbox: its BitRate field is 0 for every FLAC (and
for well over half the tracks in a typical library). Files that end up in a
duplicate group are measured for real, with mutagen.
"""

import contextlib
import os
import re
import shutil
import tempfile
import unicodedata

import settings

LOSSLESS_EXTS = {'.flac', '.aiff', '.aif', '.aifc', '.wav', '.alac'}

# Bracketed fragments that describe a VERSION. These cannot be ignored: a remix
# is a different song. Only "original mix" and friends are noise.
VERSION_HINT = re.compile(
    r'\b(remix|mix|edit|version|versión|dub|bootleg|mashup|rework|refix|vip|'
    r'instrumental|acapella|acappella|live|extended|radio|club|remaster\w*|'
    r'intro|outro|transition|short|full|clean|dirty)\b', re.I)
NOISE_VERSION = re.compile(
    r'^(original(\s+(mix|version))?|official(\s+\w+)?|hq|hd|master(ed)?|'
    r'free\s+download|out\s+now|full\s+track)$', re.I)
# ⚠ NO 'con'/'with' here: applied to the whole title with a greedy capture,
# 'Bailando Con Lola' and 'Bailando Con Marta' both collapsed to 'bailando'
# and got grouped as duplicates — inviting you to delete a different song.
# Only the unambiguous markers; bracketed '(with X)' is handled by the
# bracket pass already.
FEAT = re.compile(r'\b(feat|ft|featuring)\.?\s+[^()\[\]-]+', re.I)

NO_NAME = '(untitled)'


def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


def norm_text(s):
    """Comparable text: no accents, lower case, no punctuation."""
    s = strip_accents(str(s or '')).lower()
    s = s.replace('&', ' and ').replace('+', ' and ')
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def split_title(title):
    """Split a title into (base, version). The version decides the grouping."""
    raw = str(title or '')
    versions = []

    def take(m):
        inner = m.group(1).strip()
        if VERSION_HINT.search(inner) and not NOISE_VERSION.match(inner.strip()):
            versions.append(norm_text(inner))
        return ' '
    base = re.sub(r'[\(\[]([^\)\]]*)[\)\]]', take, raw)
    base = FEAT.sub(' ', base)
    return norm_text(base), ' '.join(sorted(v for v in versions if v))


def primary_artist(artist):
    """First artist listed: "A, B" and "A & B" must both match "A"."""
    # ⚠ '&'/'+' must be treated as a collaborator separator too, or the
    # contract in the docstring is a lie: "A & B" kept the whole string and
    # missed the duplicate against "A, B".
    raw = str(artist or '').replace('&', ' and ').replace('+', ' and ')
    a = re.split(r'\s*[,;/]\s*|\s+(?:and|vs|x)\s+', raw, maxsplit=1)[0]
    return norm_text(a)


# --------------------------------------------------------------- reading

def find_db(path=None):
    p = path or settings.find_library()
    return p if os.path.exists(p) else None


@contextlib.contextmanager
def open_library(db_path=None):
    """A cursor on a throwaway copy of the library. Yields None if there is none.

    ⚠ The copy includes the `-wal` and `-shm` files. Without them the most
    recent edits — the ones you just made in rekordbox — are missing, and the
    tool would quietly report a stale library.
    """
    import sqlcipher3

    src = find_db(db_path)
    if not src:
        yield None
        return
    tmpdir = tempfile.mkdtemp(prefix='rbread-')
    con = None
    try:
        copy = os.path.join(tmpdir, 'master.db')
        shutil.copy2(src, copy)
        for suf in ('-wal', '-shm'):
            if os.path.exists(src + suf):
                shutil.copy2(src + suf, copy + suf)
        con = sqlcipher3.connect(copy)
        cur = con.cursor()
        cur.execute("PRAGMA key='%s'" % settings.library_key())
        # ⚠ If the key does not decrypt (a newer rekordbox rotated it), try to
        # recover the real one from the app before giving up — otherwise a
        # friend on a recent version is locked out of every library tab.
        try:
            cur.execute('SELECT count(*) FROM sqlite_master')
        except Exception:
            recovered = settings.recover_key()
            if recovered and recovered != settings.library_key():
                cur.execute("PRAGMA key='%s'" % recovered)
        yield cur
    finally:
        if con is not None:
            with contextlib.suppress(Exception):
                con.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_history(db_path=None):
    """What you have actually played, from rekordbox's own history.

    Two things come out of it that exist nowhere else:
      · `last`   {contentId: 'YYYY-MM-DD'} — when you last played it. This is
        not `DJPlayCount`, which counts any listen at all.
      · `follows` {A: {B: times}} — which tracks you have REALLY mixed after
        another one in a real session.
    """
    try:
        with open_library(db_path) as cur:
            if cur is None:
                return {'last': {}, 'follows': {}}
            cur.execute('''
                SELECT h.ID, h.DateCreated, s.ContentID, s.TrackNo
                  FROM djmdSongHistory s
                  JOIN djmdHistory h ON h.ID = s.HistoryID
                 WHERE s.rb_local_deleted = 0 AND h.rb_local_deleted = 0
                 ORDER BY h.DateCreated, s.TrackNo
            ''')
            rows = cur.fetchall()
    except Exception:
        return {'last': {}, 'follows': {}}

    last, sessions = {}, {}
    for hid, date, cid, trackno in rows:
        day = str(date or '')[:10]
        if day and (cid not in last or day > last[cid]):
            last[cid] = day
        sessions.setdefault(hid, []).append((trackno or 0, cid))

    follows = {}
    for items in sessions.values():
        items.sort()
        for i in range(len(items) - 1):
            a, b = items[i][1], items[i + 1][1]
            if a == b:
                continue
            follows.setdefault(a, {})
            follows[a][b] = follows[a].get(b, 0) + 1

    # ⚠ And the years before rekordbox. This library came through djay, and
    # rekordbox's history therefore starts empty — `follows` had never once
    # been filled, so the set builder's "you have mixed these two before"
    # signal was dead code. djay kept 147 sessions between 2021 and 2023;
    # those are the same evidence, in the same shape, and they are merged
    # rather than replacing anything: a chain played in both eras counts
    # twice, which is exactly what it deserves.
    try:
        import date_recovery
        with open_library(db_path) as cur2:
            if cur2 is not None:
                cur2.execute('SELECT ID, Title FROM djmdContent '
                             'WHERE rb_local_deleted = 0')
                titles = cur2.fetchall()
        for a, nexts in date_recovery.follows_by_content_id(titles).items():
            follows.setdefault(a, {})
            for b, n in nexts.items():
                follows[a][b] = follows[a].get(b, 0) + n
    except Exception:
        pass                            # history is a bonus, never a blocker

    return {'last': last, 'follows': follows}


_ANLZ_MAP = {}


def anlz_for_path(path, db_path=None):
    """Path of a file's analysis (ANLZ) data. Read once, then cached."""
    global _ANLZ_MAP
    if not _ANLZ_MAP:
        try:
            with open_library(db_path) as cur:
                if cur is None:
                    return None
                cur.execute("SELECT FolderPath, AnalysisDataPath FROM djmdContent "
                            "WHERE rb_local_deleted = 0 AND AnalysisDataPath <> ''")
                _ANLZ_MAP = {p: a for p, a in cur.fetchall() if p}
        except Exception:
            _ANLZ_MAP = {'': ''}   # so we do not retry on every single call
    return _ANLZ_MAP.get(path)


_TREE = {'stamp': None, 'val': None}


def load_playlist_tree(db_path=None):
    """Your playlists as rekordbox shows them: folders, your own order, and
    the smart lists with what they hold right now.

    [{id, name, parent, seq, kind: 'folder' | 'list' | 'smart', tracks}] —
    `parent` is '' at the top; `tracks` are content ids in playlist order (for
    a smart list, the order rekordbox's query gives).

    ⚠ `_FolderTracks` is left out: rekordbox makes one inside every folder it
    imports from disk and never shows it — here it only cluttered the list.
    ⚠ Read on a COPY (like open_library), and remembered until the database
    changes: this runs on every sync, and copying 40 MB each time is not an
    option.
    """
    src = find_db(db_path)
    if not src:
        return []
    stamp = tuple(os.path.getmtime(src + suf) if os.path.exists(src + suf) else 0
                  for suf in ('', '-wal'))
    if _TREE['stamp'] == stamp and _TREE['val'] is not None:
        return _TREE['val']
    tmpdir = tempfile.mkdtemp(prefix='rbtree-')
    try:
        copy = os.path.join(tmpdir, 'master.db')
        shutil.copy2(src, copy)
        for suf in ('-wal', '-shm'):
            if os.path.exists(src + suf):
                shutil.copy2(src + suf, copy + suf)
        from pyrekordbox import Rekordbox6Database
        import logging
        logging.disable(logging.WARNING)
        try:
            db = Rekordbox6Database(path=copy, key=settings.library_key())
        finally:
            logging.disable(logging.NOTSET)
        out = []
        try:
            for pl in db.get_playlist():
                if pl.rb_local_deleted:
                    continue
                name = pl.Name or NO_NAME
                if name.startswith('_FolderTracks'):
                    continue
                attr = pl.Attribute or 0
                kind = {1: 'folder', 4: 'smart'}.get(attr, 'list')
                node = {'id': str(pl.ID), 'name': name,
                        'parent': '' if str(pl.ParentID) in ('root', '', 'None') else str(pl.ParentID),
                        'seq': pl.Seq or 0, 'kind': kind}
                if kind != 'folder':
                    try:
                        if kind == 'list':
                            rows = sorted((s for s in pl.Songs if not s.rb_local_deleted),
                                          key=lambda s: s.TrackNo or 0)
                            node['tracks'] = [str(s.ContentID) for s in rows]
                        else:
                            node['tracks'] = [str(c.ID) for c in db.get_playlist_contents(pl)]
                    except Exception:
                        node['tracks'] = []
                out.append(node)
        finally:
            db.close()
        _TREE['stamp'], _TREE['val'] = stamp, out
        return out
    except Exception:
        return _TREE['val'] or []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_playlists(db_path=None):
    """Ordinary playlists, with their tracks in order.

    Smart playlists (Attribute=4) and folders (1) are left out: they are not
    lists you built track by track.
    """
    with open_library(db_path) as cur:
        if cur is None:
            return []
        cur.execute('''
            SELECT p.ID, p.Name, sp.ContentID, sp.TrackNo
              FROM djmdPlaylist p
              JOIN djmdSongPlaylist sp ON sp.PlaylistID = p.ID
             WHERE p.rb_local_deleted = 0 AND sp.rb_local_deleted = 0
               AND p.Attribute = 0
             ORDER BY p.Name, sp.TrackNo
        ''')
        rows = cur.fetchall()

    lists = {}
    for pid, name, content_id, trackno in rows:
        entry = lists.setdefault(pid, {'id': pid, 'name': name or NO_NAME,
                                       'tracks': []})
        entry['tracks'].append(content_id)
    out = list(lists.values())
    for entry in out:
        entry['count'] = len(entry['tracks'])
    out.sort(key=lambda p: p['name'].lower())
    return out


def load_tracks(db_path=None):
    """Read the tracks out of rekordbox. Returns (tracks, stats)."""
    try:
        import sqlcipher3       # noqa: F401  (imported for the error message)
    except ImportError:
        raise RuntimeError(
            'The sqlcipher3 package is needed to read the rekordbox database. '
            'Install it with:  .venv/bin/python3 -m pip install sqlcipher3-wheels')

    if not find_db(db_path):
        raise RuntimeError(
            'No rekordbox database found at %s. If yours lives somewhere else, '
            'set its location in Settings.' % settings.find_library())

    with open_library(db_path) as cur:
        try:
            cur.execute('SELECT count(*) FROM sqlite_master')
        except Exception:
            raise RuntimeError(
                'Could not decrypt the rekordbox database. A newer version may '
                'have changed the key; you can set your own in Settings.')

        cur.execute('''
            SELECT c.ID, c.Title, a.Name, c.FolderPath, c.Length, c.BitRate,
                   c.BitDepth, c.SampleRate, c.FileSize, c.Rating, c.DJPlayCount,
                   COALESCE(c.StockDate, c.created_at), g.Name, k.ScaleName,
                   c.BPM, c.Commnt, col.Commnt, c.created_at
              FROM djmdContent c
              LEFT JOIN djmdArtist a ON a.ID = c.ArtistID
              LEFT JOIN djmdGenre  g ON g.ID = c.GenreID
              LEFT JOIN djmdKey    k ON k.ID = c.KeyID
              LEFT JOIN djmdColor col ON col.ID = c.ColorID
             WHERE c.rb_local_deleted = 0
        ''')
        rows = cur.fetchall()

        # MyTags: the curation work. They are stored in two levels
        # (category -> tag); what matters here is the tag plus its group.
        cur.execute('SELECT ID, Name, ParentID, Seq FROM djmdMyTag '
                    'WHERE rb_local_deleted = 0')
        tagrows = cur.fetchall()
        tagname = {t[0]: (t[1] or '') for t in tagrows}
        tagparent = {t[0]: t[2] for t in tagrows}
        tagseq = {t[0]: (t[3] or 0) for t in tagrows}
        mytags = {}
        cur.execute('SELECT ContentID, MyTagID FROM djmdSongMyTag '
                    'WHERE rb_local_deleted = 0')
        for content_id, tag_id in cur.fetchall():
            name = tagname.get(tag_id)
            if not name:
                continue
            parent = tagparent.get(tag_id)
            mytags.setdefault(content_id, []).append({
                'id': tag_id,
                'name': name,
                'cat': tagname.get(parent) or '',
                # Groups keep rekordbox's own order, not alphabetical, so the
                # list reads the way it does in the app.
                'catseq': tagseq.get(parent, 99),
            })

        # Ordinary playlists. Smart ones (Attribute=4) stay out: you did not
        # put the track there, and a merge must not touch them.
        playlists = {}
        cur.execute('''
            SELECT sp.ContentID, sp.PlaylistID, p.Name, sp.TrackNo
              FROM djmdSongPlaylist sp
              JOIN djmdPlaylist p ON p.ID = sp.PlaylistID
             WHERE sp.rb_local_deleted = 0 AND p.rb_local_deleted = 0
               AND p.Attribute = 0
        ''')
        for content_id, pid, pname, trackno in cur.fetchall():
            playlists.setdefault(content_id, []).append(
                {'id': pid, 'name': pname or NO_NAME, 'trackno': trackno or 0})

        # Cue points (memory and hot cues): the other half of the work.
        cues = {}
        cur.execute('SELECT ContentID, count(*) FROM djmdCue '
                    'WHERE rb_local_deleted = 0 GROUP BY ContentID')
        for content_id, n in cur.fetchall():
            cues[content_id] = n

    tracks, streaming = [], 0
    for (cid, title, artist, path, length, br, depth, sr, size,
         rating, plays, created, genre, key, bpm, comment, color, imported) in rows:
        path = path or ''
        # Streaming tracks (soundcloud:tracks:…) have no file behind them.
        # ⚠ Tested with isabs and NOT with a leading "/": on Windows every
        # real path starts with a drive letter, so the naive check threw the
        # entire library away and the tool reported an empty database.
        if not os.path.isabs(path):
            streaming += 1
            continue
        title = (title or '').strip() or os.path.splitext(os.path.basename(path))[0]
        tracks.append({
            'id': cid,
            'title': title,
            'artist': (artist or '').strip(),
            'path': path,
            'ext': os.path.splitext(path)[1].lower(),
            'rb_length': int(length) if length else 0,
            'rb_bitrate': int(br) if br else 0,
            'rb_depth': int(depth) if depth else 0,
            'rb_rate': int(sr) if sr else 0,
            'rb_size': int(size) if size else 0,
            'rating': int(rating) if rating else 0,
            'plays': int(plays) if plays else 0,
            # ⚠ StockDate first, created_at only as a fallback. created_at is
            # when the ROW was written, so a library that came through an
            # import says the same afternoon for everything; StockDate is
            # where the recovered dates live (see date_recovery.py).
            'added': str(created or '')[:10],
            # ⚠ The date above has no time, and one download is a dozen tracks
            # on the same day: without the moment each one came in, a day's
            # tracks came out in any order. The import time breaks the tie.
            'imported': str(imported or '')[:19],
            'genre': (genre or '').strip(),
            'key': (key or '').strip(),
            # rekordbox stores BPM multiplied by 100 (12800 = 128.00).
            'bpm': round(bpm / 100.0, 1) if bpm and bpm > 1000 else (bpm or 0),
            'comment': (comment or '').strip(),
            'color': (color or '').strip(),
            # By group (rekordbox's order) and alphabetical within each group.
            'mytags': sorted(mytags.get(cid, []),
                             key=lambda t: (t['catseq'], t['name'].lower())),
            'cues': cues.get(cid, 0),
            'playlists': sorted(playlists.get(cid, []),
                                key=lambda p: p['name'].lower()),
        })
    stats = {'total': len(rows), 'local': len(tracks), 'streaming': streaming}
    return tracks, stats


# ------------------------------------------------------- measuring quality

def probe_file(path):
    """The file's REAL properties (via mutagen). Never raises."""
    out = {'exists': os.path.exists(path), 'size': 0, 'duration': None,
           'bitrate': None, 'rate': None, 'depth': None, 'channels': None,
           'lossless': None, 'codec': None}
    if not out['exists']:
        return out
    try:
        out['size'] = os.path.getsize(path)
    except OSError:
        pass
    try:
        import mutagen
        f = mutagen.File(path)
        info = getattr(f, 'info', None)
    except Exception:
        info = None
    if info is None:
        return out
    for src, dst in (('length', 'duration'), ('bitrate', 'bitrate'),
                     ('sample_rate', 'rate'), ('bits_per_sample', 'depth'),
                     ('channels', 'channels')):
        v = getattr(info, src, None)
        if v:
            out[dst] = v
    ext = os.path.splitext(path)[1].lower()
    codec = getattr(info, 'codec', '') or ''
    if ext in ('.m4a', '.mp4'):
        out['lossless'] = codec.startswith('alac')
        out['codec'] = 'ALAC' if out['lossless'] else 'AAC'
    else:
        out['lossless'] = ext in LOSSLESS_EXTS
        out['codec'] = ext.lstrip('.').upper()
    # FLAC files do not report a bitrate: derive it from size and duration.
    if not out['bitrate'] and out['duration'] and out['size']:
        out['bitrate'] = int(out['size'] * 8 / out['duration'])
    return out


def work_score(t):
    """How much DJ work this copy carries (MyTags, playlists, cues…)."""
    return (len(t.get('mytags') or []) * 2 + (t.get('cues') or 0) * 2
            + len(t.get('playlists') or []) * 3
            + (2 if t.get('comment') else 0) + (1 if t.get('rating') else 0))


def merge_comment(keeper_comment, other_comments):
    """The merged comment, or None if there is nothing to add.

    Many DJs write the comment as "energy - description" ("6 - rock groove").
    Merging it properly matters: the copy you keep is usually the one in the
    BETTER format, not the better described one — the same track had the AIFF
    saying "6" and the FLAC saying "6 - rock groove", and because the AIFF's
    comment was not empty, the description went to the bin with the file.

    Rules, all of them additive:
      · the energy is the keeper's (or the first one found);
      · descriptions are joined with " · ", never repeated;
      · if another copy's energy disagrees with ours, it is noted inside the
        description rather than one of them being silently dropped.
    """
    from setbuilder import split_comment

    energy, desc = split_comment(keeper_comment)
    parts = [desc] if desc else []
    seen = {_norm(desc)} if desc else set()

    for c in other_comments:
        e, d = split_comment(c)
        if energy is None and e is not None:
            energy = e
        elif e is not None and energy is not None and e != energy:
            d = ('%s (energy %d on the other copy)' % (d, e)) if d else \
                'energy %d on the other copy' % e
        if d and _norm(d) not in seen:
            seen.add(_norm(d))
            parts.append(d)

    if not parts and energy is None:
        return None
    text = ' · '.join(parts)
    merged = ('%d - %s' % (energy, text)) if energy is not None and text else (
        str(energy) if energy is not None else text)
    return merged if merged != (keeper_comment or '').strip() else None


def _norm(s):
    """So the same description written differently is not added twice."""
    return ' '.join(str(s or '').lower().split())


def merge_plan(keeper, others):
    """What the copy you keep would gain if the others were poured into it.

    Additive only: tags and playlists it is missing, plus the best rating. It
    never removes anything and never deletes a copy.
    """
    have_tags = {t['id'] for t in keeper['mytags']}
    have_lists = {p['id'] for p in keeper['playlists']}
    tags, lists, seen_t, seen_l = [], [], set(), set()
    cues_left = 0

    for o in others:
        for t in o['mytags']:
            if t['id'] not in have_tags and t['id'] not in seen_t:
                seen_t.add(t['id'])
                tags.append(t)
        for p in o['playlists']:
            if p['id'] not in have_lists and p['id'] not in seen_l:
                seen_l.add(p['id'])
                lists.append(dict(p, from_title=o['title']))
        cues_left += o['cues']

    best_rating = max([o['rating'] for o in others] + [0])
    comment = merge_comment(keeper['comment'],
                            [o['comment'] for o in others]) or ''

    return {
        'keeper_id': keeper['id'],
        'other_ids': [o['id'] for o in others],
        'tags': sorted(tags, key=lambda t: (t['catseq'], t['name'].lower())),
        'playlists': sorted(lists, key=lambda p: p['name'].lower()),
        'rating': best_rating if best_rating > keeper['rating'] else 0,
        'comment': comment,
        # Cues live in the analysis files; copying those is another matter.
        'cues_not_copied': cues_left if not keeper['cues'] else 0,
        'anything': bool(tags or lists or comment
                         or (best_rating > keeper['rating'])),
    }


def describe_work(t):
    bits = []
    n = len(t.get('mytags') or [])
    if n:
        bits.append('1 MyTag' if n == 1 else '%d MyTags' % n)
    if t.get('cues'):
        bits.append('%d cues' % t['cues'])
    if t.get('comment'):
        bits.append('a comment')
    if t.get('rating'):
        bits.append('a rating')
    return ', '.join(bits)


def audio_rank(p):
    """REAL sound quality: lossless first, then more bits / higher bitrate.

    File size is deliberately not part of it: an AIFF and a FLAC at 44.1 kHz /
    16-bit sound exactly the same, even though the AIFF takes twice the space.
    """
    if p.get('lossless'):
        return (1, (p.get('depth') or 16) * (p.get('rate') or 44100))
    return (0, p.get('bitrate') or 0)


def quality_rank(p):
    """Display order: quality, and on a tie the larger file first."""
    return audio_rank(p) + (p.get('size') or 0,)


def fmt_quality(p):
    """Short readable label: "FLAC 44.1 kHz / 24-bit", "MP3 320 kbps"."""
    if not p.get('exists'):
        return 'file not found'
    codec = p.get('codec') or '?'
    if p.get('lossless'):
        bits = []
        if p.get('rate'):
            bits.append(('%.1f' % (p['rate'] / 1000)).replace('.0', '') + ' kHz')
        if p.get('depth'):
            bits.append('%d-bit' % p['depth'])
        return codec + (' ' + ' / '.join(bits) if bits else '')
    kbps = round((p.get('bitrate') or 0) / 1000)
    return '%s %d kbps' % (codec, kbps) if kbps else codec


def fmt_duration(secs):
    if not secs:
        return '—'
    s = int(round(secs))
    return '%d:%02d' % (s // 60, s % 60)


# --------------------------------------------------------------- duplicates

# Below this difference, two copies are the SAME recording.
SAME_DURATION_TOLERANCE = 3.0
# A short sample or effect carrying a whole song's tags is not a version:
# it is a mis-tagged file.
SHORT_CLIP = 30.0
FULL_TRACK = 60.0


def analyze(tracks, progress=None):
    """Measure the WHOLE library once, and get duplicates and quality from it."""
    total = len(tracks)
    for i, t in enumerate(tracks):
        t['probe'] = probe_file(t['path'])
        if progress:
            progress(i + 1, total)
    return find_duplicates(tracks), quality_report(tracks)


# Quality bands for lossy files. Lossless ones stay out: they have no
# comparable bitrate, and they are not the problem.
QUALITY_BUCKETS = [
    ('le128', 128, 'Up to 128 kbps',
     'Poor: on a big system you hear it (short cymbals, soft bass).'),
    ('le256', 256, '129 to 256 kbps',
     'Passable. Fine on headphones, can show on a club system.'),
    ('le320', 320, '257 to 320 kbps',
     'Good: this is the MP3 ceiling. Only a lossless file beats it.'),
]


def quality_report(tracks):
    """Sort the lossy files into bitrate bands.

    Anything under a minute (samples, effects, drops) is left OUT of every
    count: those are not songs, and a 64 kbps sample is not a fault.
    """
    buckets = {k: [] for k, _, _, _ in QUALITY_BUCKETS}
    lossless = unknown = clips = 0

    for t in tracks:
        p = t.get('probe') or {}
        if (p.get('duration') or t['rb_length'] or 0) < FULL_TRACK:
            clips += 1
            continue
        if p.get('lossless'):
            lossless += 1
            continue
        kbps = round((p.get('bitrate') or 0) / 1000)
        if not kbps:
            unknown += 1
            continue
        for key, top, _, _ in QUALITY_BUCKETS:
            if kbps <= top:
                buckets[key].append((kbps, t))
                break

    out = []
    for key, _, label, hint in QUALITY_BUCKETS:
        rows = sorted(buckets[key], key=lambda r: r[0])
        out.append({
            'key': key, 'label': label, 'hint': hint, 'count': len(rows),
            'files': [{
                'path': t['path'],
                'name': os.path.basename(t['path']),
                'folder': os.path.dirname(t['path']).replace(
                    os.path.expanduser('~'), '~'),
                'artist': t['artist'],
                'title': t['title'],
                'kbps': kbps,
                'quality': fmt_quality(t['probe']),
                'duration': fmt_duration(t['probe'].get('duration') or t['rb_length']),
                'size': t['probe'].get('size') or 0,
                'rating': t['rating'],
                'plays': t['plays'],
                'mytags': len(t['mytags']),
            } for kbps, t in rows],
        })
    return {'buckets': out, 'lossless': lossless, 'unknown': unknown,
            'clips': clips, 'total': len(tracks) - clips}


def _readable_everywhere(c):
    """Whether even the oldest player would read this copy."""
    import compat
    # ⚠ The probe stores no "alac" field: it says so in the codec (ALAC vs
    # AAC), and the difference matters because an AAC .m4a plays everywhere
    # and an ALAC one does not.
    return compat.widely_readable(os.path.splitext(c['path'])[1].lower(),
                                  alac=(c['probe'].get('codec') == 'ALAC'))


def library_index(tracks=None):
    """{(artist, base, version): [tracks]} — the library, keyed for matching.

    The SAME key the duplicate finder uses, on purpose: a file that would end
    up grouped as a duplicate after importing should be flagged as one before.
    """
    if tracks is None:
        tracks, _ = load_tracks()
    idx = {}
    for t in tracks:
        base, version = split_title(t['title'])
        if not base:
            continue
        idx.setdefault((primary_artist(t['artist']), base, version), []).append(t)
    return idx


def already_in_library(title, artist, idx, seconds=None):
    """Copies of this track you already have. Empty if it is new.

    ⚠ Checked BEFORE importing, which is the only moment it is cheap to act
    on. Finding it later, in the Duplicates tab, means the file is already in
    the library with its own analysis and its own half of your tagging.
    """
    base, version = split_title(title or '')
    if not base:
        return []
    hits = idx.get((primary_artist(artist or ''), base, version)) or []
    out = []
    for t in hits:
        probe = probe_file(t['path'])
        dur = probe.get('duration') or t['rb_length'] or 0
        gap = abs(dur - seconds) if (seconds and dur) else None
        out.append({
            'title': t['title'], 'artist': t['artist'],
            'name': os.path.basename(t['path']),
            'quality': fmt_quality(probe),
            'duration': fmt_duration(dur),
            'added': t['added'],
            'rating': t['rating'],
            'mytags': len(t['mytags']),
            # A different length is the signal that it is another edit, not
            # the same file arriving twice.
            'same_length': (gap is not None and gap <= SAME_DURATION_TOLERANCE),
            'gap': (fmt_duration(gap) if gap and gap > SAME_DURATION_TOLERANCE else ''),
        })
    return out


def find_duplicates(tracks):
    """Group likely duplicates. The files must already have been measured."""
    groups = {}
    for t in tracks:
        base, version = split_title(t['title'])
        if not base:
            continue
        key = (primary_artist(t['artist']), base, version)
        groups.setdefault(key, []).append(t)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    out = []
    for (artist, base, version), copies in dupes.items():
        for c in copies:
            c['quality'] = fmt_quality(c['probe'])
            c['duration'] = c['probe'].get('duration') or c['rb_length'] or 0
        copies.sort(key=lambda c: quality_rank(c['probe']), reverse=True)

        durs = [c['duration'] for c in copies if c['duration']]
        spread = (max(durs) - min(durs)) if len(durs) > 1 else 0.0
        same_len = spread <= SAME_DURATION_TOLERANCE

        best, rest = copies[0], copies[1:]
        same_quality = all(audio_rank(c['probe']) == audio_rank(best['probe'])
                           for c in rest)
        identical = same_len and all(
            c['probe'].get('size') == best['probe'].get('size') for c in rest)

        # The work already done (MyTags, cues, comment, rating) often matters
        # more than the format: deleting the curated copy loses it for good.
        curated = max(copies, key=work_score)
        has_work = work_score(curated) > 0
        if same_quality and has_work:
            best = curated   # at equal sound, the curated copy wins

        # …but at equal sound the FORMAT also decides: a FLAC and an AIFF at
        # 44.1/16 sound the same, and no CDJ older than the 3000 reads FLAC.
        # ⚠ Only swap if it costs no CUES: tags, playlists, rating and comment
        # all transfer in a merge, but cues live in the analysis files and do
        # not.
        swapped_for_format = False
        if same_quality and not _readable_everywhere(best) and not best['cues']:
            alt = next((c for c in copies
                        if c is not best and _readable_everywhere(c)), None)
            if alt:
                best, swapped_for_format = alt, True

        mistagged = (any(d < SHORT_CLIP for d in durs)
                     and any(d > FULL_TRACK for d in durs))

        if mistagged:
            verdict = 'mistagged'
            why = ('One of these files is only %s long: it is not a version, '
                   'the TAGS ARE WRONG (a sample or effect carries this song\'s '
                   'title and artist). Fix its tags.' % fmt_duration(min(durs)))
        elif identical:
            verdict, why = 'identical', 'Identical copies (same size and length).'
        elif same_len:
            verdict = 'duplicate'
            if swapped_for_format:
                why = ('Same recording and same sound quality, but one of the '
                       'files only plays on the newest gear: keep the one that '
                       'plays everywhere. The other one\'s work (%s) comes along '
                       'with the merge.' % (describe_work(curated) or 'none'))
            elif same_quality and has_work:
                why = ('Same recording and same sound quality: keep the one that '
                       'already carries your work (%s).' % describe_work(curated))
            elif same_quality:
                why = ('Same recording and the SAME sound quality: keep whichever '
                       'you like (the only difference is disk space).')
            else:
                why = 'Same recording at different quality: keep the better one.'
        else:
            verdict = 'review'
            why = ('The lengths do not match (%s apart): these are probably '
                   'different VERSIONS, not duplicates. Do not delete either '
                   'without listening.' % fmt_duration(spread))

        # Each copy usually carries DIFFERENT MyTags: before deleting one it
        # helps to know which tags only exist there.
        best_tags = {t['name'] for t in best['mytags']}
        lost = set()
        for c in copies:
            c['lost'] = ([] if c is best
                         else sorted({t['name'] for t in c['mytags']} - best_tags))
            lost.update(c['lost'])

        # The warning that actually matters: the best-sounding copy is not the
        # one you have worked on.
        work_note = ''
        if (has_work and curated is not best
                and work_score(curated) > work_score(best)):
            # ⚠ Say what to DO, not only what is at stake. The old wording
            # described the loss and stopped there, next to a Merge button
            # that prevents exactly that loss — so it read as a dead end and
            # the remedy went unnoticed.
            work_note = (
                ('The marked copy plays on any gear; your work is on the other '
                 'one (%s). Merge copies it across — tags, comment, rating and '
                 'its place in every playlist. Cues are the one thing that '
                 'cannot travel.'
                 if swapped_for_format else
                 'The marked copy sounds better; your work is on the other one '
                 '(%s). Merge copies it across — tags, comment, rating and its '
                 'place in every playlist. Cues are the one thing that cannot '
                 'travel.') % describe_work(curated))

        out.append({
            'artist': copies[0]['artist'] or artist,
            'title': copies[0]['title'],
            'version': version,
            'verdict': verdict,
            'why': why,
            'work_note': work_note,
            'lost_tags': sorted(lost),
            'merge': merge_plan(best, [c for c in copies if c is not best]),
            'spread': round(spread, 1),
            'tied': same_quality,
            'copies': [{
                'id': c['id'],
                'path': c['path'],
                'name': os.path.basename(c['path']),
                'folder': os.path.dirname(c['path']).replace(
                    os.path.expanduser('~'), '~'),
                'quality': c['quality'],
                'duration': fmt_duration(c['duration']),
                'seconds': round(c['duration'] or 0, 1),
                'size': c['probe'].get('size') or 0,
                'lossless': bool(c['probe'].get('lossless')),
                'exists': c['probe'].get('exists', False),
                'rating': c['rating'],
                'plays': c['plays'],
                'added': c['added'],
                'genre': c['genre'],
                'bpm': c['bpm'],
                'key': c['key'],
                'comment': c['comment'],
                'color': c['color'],
                'cues': c['cues'],
                'mytags': c['mytags'],
                'playlists': c['playlists'],
                'lost': c['lost'],
                # We recommend the copy that wins on sound or, on a tie, the
                # curated one.
                'best': c is best and not (same_quality and not has_work),
            } for c in copies],
        })

    order = {'identical': 0, 'duplicate': 1, 'review': 2, 'mistagged': 3}
    out.sort(key=lambda g: (order.get(g['verdict'], 9), g['artist'].lower(),
                            g['title'].lower()))
    return out
