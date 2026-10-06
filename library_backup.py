"""Backups of the library that you can keep for years, and read without rekordbox.

Two kinds of copy live in `backups/`, side by side:

  · `master-STAMP.db` — the database itself, byte for byte. The only thing that
    puts rekordbox back EXACTLY where it was, and 40 MB each that does not
    compress (it is encrypted). Kept for weeks, not years.
  · `library-STAMP.xml.gz` + `tables-STAMP.json.gz` — the same library as
    text, a few megabytes together. Kept for years.

The text copies are the ones that survive what the database copy does not:

  · **The key.** `master.db` opens with a key reverse-engineered from
    rekordbox. The day AlphaTheta rotates it, every `.db` in this folder is a
    locked box. The text copies are already open.
  · **Other software.** The XML is rekordbox's own exchange format
    (File → Import → rekordbox xml, and what Lexicon, Serato and Traktor
    converters read): tracks, cues, loops, the beat grid, every playlist.
  · **What rekordbox's XML leaves out.** My Tags, the history of what you
    played and your colour names are not in rekordbox's format. Two places
    cover that: the XML carries them as playlist folders (`MyTag`, `History`),
    so any program that reads the XML sees them; and `tables-*.json.gz` is
    every table of the database, row for row, in plain JSON — enough to
    rebuild the library from nothing, or to answer "what did this track's
    tags look like in March".

⚠ Retention is by AGE, not by count. "Keep the last 20" sounded safe until an
afternoon of edits made 20 copies in four hours and quietly deleted every one
older than that — the state from last week was gone, and last week is exactly
what you reach for when something went wrong without you noticing.
"""

import datetime
import gzip
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from xml.sax.saxutils import quoteattr

import settings

BACKUP_DIR = settings.BACKUP_DIR
STATE_FILE = os.path.join(BACKUP_DIR, 'snapshots.json')

HOUR = 3600
DAY = 24 * HOUR

#: (how far back, bucket) — within each window, the newest copy in each
#: bucket survives. `None` as the window means forever; `None` as the bucket
#: means every copy.
DB_TIERS = [
    (6 * HOUR, None),           # this afternoon: everything
    (2 * DAY, 'hour'),          # the last two days: one an hour
    (14 * DAY, 'day'),          # two weeks: one a day
    (8 * 7 * DAY, 'week'),      # two months: one a week
]
#: The most copies of the database the tiers above can keep at once (6 hours
#: of hourly copies, 48 hourly, 14 daily, 8 weekly). Times the size of the
#: library, that is the worst case with no size limit — shown on screen.
MAX_DB_COPIES = 6 + 48 + 14 + 8

TEXT_TIERS = [
    (2 * DAY, None),            # the last two days: everything
    (365 * DAY, 'day'),         # a year: one a day
    (None, 'month'),            # after that: one a month, forever
]

#: How often the automatic copy is taken while the toolkit runs — and only if
#: the library has changed since the last one.
AUTO_EVERY = HOUR
AUTO_CHECK = 15 * 60

_STAMP = re.compile(r'^(master|library|tables)-(\d{8}-\d{6})\.(db|xml\.gz|json\.gz)$')
_LOCK = threading.Lock()
_LAST_ERROR = {'msg': None}


# ── Retention ──────────────────────────────────────────────────────────────

def _bucket(dt, kind):
    if kind == 'hour':
        return dt.strftime('%Y%m%d%H')
    if kind == 'day':
        return dt.strftime('%Y%m%d')
    if kind == 'week':
        y, w, _ = dt.isocalendar()
        return '%d-%02d' % (y, w)
    if kind == 'month':
        return dt.strftime('%Y%m')
    return None


def to_keep(stamps, tiers, now=None):
    """Which of `stamps` (datetimes) survive `tiers`. Pure: easy to reason about.

    A copy survives if it is the newest of its bucket in ANY window it falls
    in, and the newest copy of all always survives — whatever the clock says.
    """
    now = now or datetime.datetime.now()
    keep = set()
    if not stamps:
        return keep
    keep.add(max(stamps))
    for window, kind in tiers:
        best = {}
        for s in stamps:
            age = (now - s).total_seconds()
            if window is not None and age > window:
                continue
            if kind is None:
                keep.add(s)
                continue
            b = _bucket(s, kind)
            if b not in best or s > best[b]:
                best[b] = s
        keep.update(best.values())
    return keep


def _listing(folder=BACKUP_DIR):
    """{kind: {datetime: filename}} for everything in the backup folder."""
    out = {'master': {}, 'library': {}, 'tables': {}}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        m = _STAMP.match(n)
        if not m:
            continue
        try:
            dt = datetime.datetime.strptime(m.group(2), '%Y%m%d-%H%M%S')
        except ValueError:
            continue
        out[m.group(1)][dt] = n
    return out


def prune(folder=BACKUP_DIR, now=None):
    """Delete what the tiers no longer keep. Returns the names removed."""
    removed = []
    lst = _listing(folder)
    plans = [('master', DB_TIERS, ('', '-wal', '-shm')),
             ('library', TEXT_TIERS, ('',)),
             ('tables', TEXT_TIERS, ('',))]
    for kind, tiers, sufs in plans:
        files = lst[kind]
        keep = to_keep(list(files), tiers, now)
        for dt, name in files.items():
            if dt in keep:
                continue
            for suf in sufs:
                try:
                    os.remove(os.path.join(folder, name + suf))
                except OSError:
                    pass
            removed.append(name)
    removed += _enforce_budget(folder)
    return removed


def _enforce_budget(folder=BACKUP_DIR, budget_mb=None, dry=False):
    """Oldest database copies out until they fit the size limit.

    ⚠ The two newest always stay, whatever the limit says: a limit smaller
    than two copies of the library would otherwise leave nothing to restore.
    The readable XML/JSON versions are small and are not counted.
    """
    if budget_mb is None:
        budget_mb = settings.load().get('backup_budget_mb') or 0
    budget = int(budget_mb) * 1024 * 1024
    if budget <= 0:
        return []
    copies = sorted(_listing(folder)['master'].items())           # oldest first
    def size(name):
        total = 0
        for suf in ('', '-wal', '-shm'):
            try:
                total += os.path.getsize(os.path.join(folder, name + suf))
            except OSError:
                pass
        return total
    sizes = {name: size(name) for _, name in copies}
    total = sum(sizes.values())
    removed = []
    while total > budget and len(copies) > 2:
        _, name = copies.pop(0)
        if not dry:
            for suf in ('', '-wal', '-shm'):
                try:
                    os.remove(os.path.join(folder, name + suf))
                except OSError:
                    pass
        total -= sizes[name]
        removed.append((name, sizes[name]) if dry else name)
    return removed


def budget_preview(budget_mb):
    """What a size limit would remove right now: (copies, bytes)."""
    gone = _enforce_budget(BACKUP_DIR, budget_mb, dry=True)
    return len(gone), sum(s for _, s in gone)


def listing(folder=BACKUP_DIR):
    """Every copy, newest first: [{stamp, when, db, xml, tables, bytes}]."""
    lst = _listing(folder)
    stamps = set(lst['master']) | set(lst['library']) | set(lst['tables'])
    out = []
    for dt in sorted(stamps, reverse=True):
        row = {'stamp': dt.strftime('%Y%m%d-%H%M%S'),
               'when': dt.strftime('%Y-%m-%d %H:%M')}
        size = 0
        for kind, key in (('master', 'db'), ('library', 'xml'), ('tables', 'tables')):
            n = lst[kind].get(dt)
            row[key] = n
            if n:
                for suf in (('', '-wal') if kind == 'master' else ('',)):
                    try:
                        size += os.path.getsize(os.path.join(folder, n + suf))
                    except OSError:
                        pass
        row['bytes'] = size
        out.append(row)
    return out


def status(folder=BACKUP_DIR):
    """Everything the Backups screen shows, in one call."""
    rows = listing(folder)
    st = _state()
    db_bytes = text_bytes = 0
    for r in rows:
        for key, sufs in (('db', ('', '-wal', '-shm')), ('xml', ('',)), ('tables', ('',))):
            for suf in sufs:
                if r[key]:
                    try:
                        size = os.path.getsize(os.path.join(folder, r[key] + suf))
                    except OSError:
                        continue
                    if key == 'db':
                        db_bytes += size
                    else:
                        text_bytes += size
    last = rows[0] if rows else None
    last_text = next((r for r in rows if r['xml']), None)
    try:
        changed = _fingerprint() != st.get('fingerprint')
    except Exception:
        changed = False
    return {
        'dir': folder, 'rows': rows, 'busy': _LOCK.locked(),
        'last': last, 'last_text': last_text,
        'last_age': (time.time() - time.mktime(time.strptime(last['stamp'], '%Y%m%d-%H%M%S'))
                     if last else None),
        'changed_since': changed, 'error': _LAST_ERROR.get('msg'),
        'db_bytes': db_bytes, 'text_bytes': text_bytes,
        'every': AUTO_EVERY,
        'tiers': {'db': 'everything from the last 6 hours, then one an hour for '
                        '2 days, one a day for 2 weeks, one a week for 2 months',
                  'text': 'everything from the last 2 days, then one a day for '
                          'a year, one a month after that — forever'},
    }


def path_of(name, folder=BACKUP_DIR):
    """The full path of a backup file, or None if `name` is not one of ours."""
    if not name or not _STAMP.match(name):
        return None
    p = os.path.join(folder, name)
    return p if os.path.exists(p) else None


# ── The text copies ────────────────────────────────────────────────────────

def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


def export_tables(cur, dest):
    """Every table, every row, as JSON. The library without its lock.

    Deleted rows (`rb_local_deleted = 1`) are kept too: they are what rekordbox
    itself still holds, and "it was there and then it was deleted" is exactly
    the kind of question a backup is for.
    """
    tables = [r[0] for r in _rows(
        cur, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    tmp = dest + '.part'
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=6) as fh:
        fh.write('{"format": 1, "made": %s, "tables": {' % json.dumps(
            datetime.datetime.now().isoformat(timespec='seconds')))
        for i, name in enumerate(tables):
            cur.execute('SELECT * FROM "%s"' % name.replace('"', '""'))
            cols = [d[0] for d in cur.description]
            rows = [[v.hex() if isinstance(v, (bytes, bytearray)) else v for v in r]
                    for r in cur.fetchall()]
            fh.write('%s%s: ' % (',' if i else '', json.dumps(name)))
            json.dump({'columns': cols, 'rows': rows}, fh, ensure_ascii=False,
                      separators=(',', ':'))
        fh.write('}}')
    os.replace(tmp, dest)


_KIND = {1: 'MP3 File', 4: 'M4A File', 5: 'FLAC File', 11: 'WAV File', 12: 'AIFF File'}

#: rekordbox's eight track colours, as its own XML export writes them. The
#: NAMES you gave them (djmdColor.Commnt) are not in the format — they are in
#: the tables copy.
_COLOUR = {1: '0xFF007F', 2: '0xFF0000', 3: '0xFFA500', 4: '0xFFFF00',
           5: '0x00FF00', 6: '0x25FDE9', 7: '0x0000FF', 8: '0x660099'}

#: djmdCue.Kind → hot cue slot. ⚠ Kind 4 is skipped: A–C are 1–3 and D–H are
#: 5–9, which is what the counts in a real library look like (hundreds on 1,
#: none on 4). Any other value is written as a memory cue at the same place
#: rather than guessed into a slot — a cue in the wrong letter is worse than
#: one without a letter.
_HOT = {1: 0, 2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}


def _beat_grid(analysis_path):
    """[(seconds, bpm, beat in bar)] where the grid starts or changes.

    Read straight from the PQTZ section of the track's .DAT. A constant grid
    comes out as a single entry; a dynamic one gets a new entry wherever the
    tempo changes or a beat lands more than 3 ms off where the previous tempo
    would have put it — which is how rekordbox itself writes it back.
    """
    import struct
    import anlz_wave
    import phrases
    p = phrases.resolve(analysis_path)
    if not p:
        return []
    try:
        with open(p, 'rb') as fh:
            data = fh.read()
    except OSError:
        return []
    sec = anlz_wave.sections(data).get('PQTZ')
    if not sec:
        return []
    off, hlen, tlen = sec
    try:
        n = struct.unpack('>I', data[off + 20:off + 24])[0]
    except struct.error:
        return []
    out = []
    anchor_t = anchor_bpm = None
    since = 0
    for i in range(n):
        j = off + hlen + 8 * i
        if j + 8 > off + tlen:
            break
        beat, tempo, ms = struct.unpack('>HHI', data[j:j + 8])
        t, bpm = ms / 1000.0, tempo / 100.0
        if anchor_t is not None and bpm == anchor_bpm and bpm > 0:
            since += 1
            if abs(anchor_t + since * 60.0 / bpm - t) <= 0.003:
                continue
        out.append((t, bpm, beat))
        anchor_t, anchor_bpm, since = t, bpm, 0
    return out


def _tree(nodes):
    """{parent: [node, …]} ordered as rekordbox shows them."""
    kids = {}
    for n in nodes:
        kids.setdefault(n['parent'], []).append(n)
    for v in kids.values():
        v.sort(key=lambda n: (n['seq'] or 0, n['name'].lower()))
    return kids


def _write_nodes(fh, kids, parent, known, depth):
    pad = '  ' * depth
    for n in kids.get(parent, []):
        if n['folder']:
            sub = kids.get(n['id'], [])
            fh.write('%s<NODE Type="0" Name=%s Count="%d">\n'
                     % (pad, quoteattr(n['name']), len(sub)))
            _write_nodes(fh, kids, n['id'], known, depth + 1)
            fh.write('%s</NODE>\n' % pad)
        else:
            tracks = [t for t in n['tracks'] if t in known]
            fh.write('%s<NODE Type="1" Name=%s KeyType="0" Entries="%d">\n'
                     % (pad, quoteattr(n['name']), len(tracks)))
            for t in tracks:
                fh.write('%s  <TRACK Key="%s"/>\n' % (pad, t))
            fh.write('%s</NODE>\n' % pad)


def _smart_contents(db_path):
    """{playlist id: [content ids]} for the smart lists, as they stand now.

    rekordbox's XML has no smart lists, so they go in as ordinary playlists
    with what they hold today. Needs pyrekordbox to run the query; without it
    the smart lists come out empty rather than stopping the backup.
    """
    out = {}
    tmpdir = tempfile.mkdtemp(prefix='rbsmart-')
    try:
        copy = os.path.join(tmpdir, 'master.db')
        shutil.copy2(db_path, copy)
        for suf in ('-wal', '-shm'):
            if os.path.exists(db_path + suf):
                shutil.copy2(db_path + suf, copy + suf)
        import logging
        from pyrekordbox import Rekordbox6Database
        logging.disable(logging.WARNING)
        try:
            db = Rekordbox6Database(path=copy, key=settings.library_key())
        finally:
            logging.disable(logging.NOTSET)
        try:
            for pl in db.get_playlist():
                if (pl.Attribute or 0) == 4 and not pl.rb_local_deleted:
                    try:
                        out[str(pl.ID)] = [str(c.ID) for c in db.get_playlist_contents(pl)]
                    except Exception:
                        pass
        finally:
            db.close()
    except Exception:
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


def export_xml(cur, dest, db_path):
    """The library in rekordbox's own XML format, plus My Tags and history."""
    names = {}
    for table in ('djmdArtist', 'djmdAlbum', 'djmdGenre', 'djmdKey', 'djmdLabel'):
        col = 'ScaleName' if table == 'djmdKey' else 'Name'
        names[table] = {str(i): n for i, n in _rows(cur, 'SELECT ID, %s FROM %s' % (col, table))}

    def nm(table, i):
        return names[table].get(str(i)) or '' if i not in (None, '') else ''

    tracks = _rows(cur, '''
        SELECT ID, FolderPath, Title, ArtistID, AlbumID, GenreID, BPM, Length,
               TrackNo, BitRate, Commnt, FileType, Rating, ReleaseYear,
               RemixerID, LabelID, KeyID, StockDate, ColorID, DJPlayCount,
               FileSize, DiscNo, ComposerID, SampleRate, DateCreated, Subtitle,
               AnalysisDataPath
          FROM djmdContent WHERE rb_local_deleted = 0
         ORDER BY CAST(ID AS INTEGER)''')
    known = {str(t[0]) for t in tracks}

    cues = {}
    for cid, kind, ms_in, ms_out, comment in _rows(cur, '''
            SELECT ContentID, Kind, InMsec, OutMsec, Comment FROM djmdCue
             WHERE rb_local_deleted = 0 ORDER BY ContentID, InMsec'''):
        cues.setdefault(str(cid), []).append((kind, ms_in, ms_out, comment))

    playlists = []
    for pid, seq, name, attr, parent in _rows(cur, '''
            SELECT ID, Seq, Name, Attribute, ParentID FROM djmdPlaylist
             WHERE rb_local_deleted = 0'''):
        if (name or '').startswith('_FolderTracks'):
            continue
        playlists.append({'id': str(pid), 'seq': seq, 'name': name or '(no name)',
                          'parent': '' if str(parent) in ('root', '', 'None') else str(parent),
                          'folder': attr == 1, 'smart': attr == 4, 'tracks': []})
    by_id = {p['id']: p for p in playlists}
    for pid, cid in _rows(cur, '''
            SELECT PlaylistID, ContentID FROM djmdSongPlaylist
             WHERE rb_local_deleted = 0 ORDER BY PlaylistID, TrackNo'''):
        if str(pid) in by_id:
            by_id[str(pid)]['tracks'].append(str(cid))
    if any(p['smart'] for p in playlists):
        for pid, ids in _smart_contents(db_path).items():
            if pid in by_id:
                by_id[pid]['tracks'] = ids

    # My Tags: a folder per category, a playlist per tag.
    mytags = []
    for tid, seq, name, attr, parent in _rows(cur, '''
            SELECT ID, Seq, Name, Attribute, ParentID FROM djmdMyTag
             WHERE rb_local_deleted = 0'''):
        mytags.append({'id': 'tag' + str(tid), 'seq': seq, 'name': name or '(no name)',
                       'parent': '' if str(parent) in ('root', '', 'None') else 'tag' + str(parent),
                       'folder': attr == 1, 'tracks': []})
    tag_by = {t['id']: t for t in mytags}
    for tid, cid in _rows(cur, '''
            SELECT MyTagID, ContentID FROM djmdSongMyTag
             WHERE rb_local_deleted = 0 ORDER BY MyTagID, CAST(ContentID AS INTEGER)'''):
        t = tag_by.get('tag' + str(tid))
        if t:
            t['tracks'].append(str(cid))

    # History: years and months as folders, every session as a playlist.
    history = []
    for hid, seq, name, attr, parent, made in _rows(cur, '''
            SELECT ID, Seq, Name, Attribute, ParentID, DateCreated FROM djmdHistory
             WHERE rb_local_deleted = 0'''):
        history.append({'id': 'his' + str(hid), 'seq': seq,
                        'name': (name or '') if attr == 1 else (str(made or '')[:16] or name or ''),
                        'parent': '' if str(parent) in ('root', '', 'None') else 'his' + str(parent),
                        'folder': attr == 1, 'tracks': []})
    his_by = {h['id']: h for h in history}
    for hid, cid in _rows(cur, '''
            SELECT HistoryID, ContentID FROM djmdSongHistory
             WHERE rb_local_deleted = 0 ORDER BY HistoryID, TrackNo'''):
        h = his_by.get('his' + str(hid))
        if h:
            h['tracks'].append(str(cid))

    tmp = dest + '.part'
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=6) as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">\n')
        fh.write('  <PRODUCT Name="rekordbox" Version="6.0.0" Company="AlphaTheta"/>\n')
        fh.write('  <COLLECTION Entries="%d">\n' % len(tracks))
        for (tid, path, title, artist, album, genre, bpm, length, trackno, bitrate,
             comment, ftype, rating, year, remixer, label, key, stock, colour,
             plays, size, disc, composer, rate, created, mix, anlz) in tracks:
            a = [('TrackID', tid), ('Name', title or ''),
                 ('Artist', nm('djmdArtist', artist)),
                 ('Composer', nm('djmdArtist', composer)),
                 ('Album', nm('djmdAlbum', album)), ('Grouping', ''),
                 ('Genre', nm('djmdGenre', genre)), ('Kind', _KIND.get(ftype, '')),
                 ('Size', size or 0), ('TotalTime', length or 0),
                 ('DiscNumber', disc or 0), ('TrackNumber', trackno or 0),
                 ('Year', year or 0), ('AverageBpm', '%.2f' % ((bpm or 0) / 100.0)),
                 ('DateAdded', (stock or str(created or ''))[:10]),
                 ('BitRate', bitrate or 0), ('SampleRate', rate or 0),
                 ('Comments', comment or ''), ('PlayCount', plays or 0),
                 ('Rating', min(5, max(0, int(rating or 0))) * 51),
                 ('Location', 'file://localhost' + urllib.parse.quote(path or '', safe='/')),
                 ('Remixer', nm('djmdArtist', remixer)),
                 ('Tonality', nm('djmdKey', key)), ('Label', nm('djmdLabel', label)),
                 ('Mix', mix or '')]
            try:
                c = _COLOUR.get(int(colour or 0))
            except (TypeError, ValueError):
                c = None
            if c:
                a.append(('Colour', c))
            fh.write('    <TRACK %s>\n' % ' '.join('%s=%s' % (k, quoteattr(str(v))) for k, v in a))
            for t, b, beat in _beat_grid(anlz):
                fh.write('      <TEMPO Inizio="%.3f" Bpm="%.2f" Metro="4/4" Battito="%d"/>\n'
                         % (t, b, beat))
            for kind, ms_in, ms_out, name in cues.get(str(tid), []):
                num = _HOT.get(kind, -1) if kind else -1
                loop = ms_out is not None and ms_out > 0
                end = (' End="%.3f"' % (ms_out / 1000.0)) if loop else ''
                fh.write('      <POSITION_MARK Name=%s Type="%d" Start="%.3f"%s Num="%d"/>\n'
                         % (quoteattr(name or ''), 4 if loop else 0, (ms_in or 0) / 1000.0,
                            end, num))
            fh.write('    </TRACK>\n')
        fh.write('  </COLLECTION>\n  <PLAYLISTS>\n')
        roots = _tree(playlists).get('', [])
        extra = [('MyTag', mytags), ('History', history)]
        fh.write('    <NODE Type="0" Name="ROOT" Count="%d">\n'
                 % (len(roots) + sum(1 for _, n in extra if n)))
        _write_nodes(fh, _tree(playlists), '', known, 3)
        for label, nodes in extra:
            if not nodes:
                continue
            kids = _tree(nodes)
            fh.write('      <NODE Type="0" Name=%s Count="%d">\n'
                     % (quoteattr(label), len(kids.get('', []))))
            _write_nodes(fh, kids, '', known, 4)
            fh.write('      </NODE>\n')
        fh.write('    </NODE>\n  </PLAYLISTS>\n</DJ_PLAYLISTS>\n')
    os.replace(tmp, dest)


def export_text(db_path, stamp):
    """Both text copies of the database at `db_path`, named with `stamp`."""
    import rekordbox as rb
    xml_dest = os.path.join(BACKUP_DIR, 'library-%s.xml.gz' % stamp)
    tab_dest = os.path.join(BACKUP_DIR, 'tables-%s.json.gz' % stamp)
    with rb.open_library(db_path) as cur:
        if cur is None:
            raise RuntimeError('No library at %s' % db_path)
        export_tables(cur, tab_dest)
        export_xml(cur, xml_dest, db_path)
    return xml_dest, tab_dest


# ── The automatic copy ─────────────────────────────────────────────────────

def _fingerprint():
    src = settings.find_library()
    parts = []
    for suf in ('', '-wal'):
        try:
            st = os.stat(src + suf)
            parts.append('%d:%d' % (st.st_size, int(st.st_mtime)))
        except OSError:
            parts.append('-')
    return '|'.join(parts)


def _state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(st):
    try:
        tmp = STATE_FILE + '.part'
        with open(tmp, 'w') as fh:
            json.dump(st, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def snapshot(force=False):
    """A database copy and its two text copies, if the library has changed.

    Returns the stamp made, or None when there was nothing new to keep. The
    text is exported FROM the fresh `.db` copy, not from the live library, so
    the three files describe exactly the same moment.
    """
    import rekordbox_merge as rm
    with _LOCK:
        if not os.path.exists(settings.find_library()):
            return None
        st = _state()
        fp = _fingerprint()
        if not force:
            if fp == st.get('fingerprint'):
                return None
            if time.time() - st.get('at', 0) < AUTO_EVERY:
                return None
        db = rm.backup_db()
        stamp = re.search(r'master-(\d{8}-\d{6})\.db$', db).group(1)
        export_text(db, stamp)
        _save_state({'fingerprint': fp, 'at': time.time(), 'stamp': stamp})
        prune()
        return stamp


def _loop():
    time.sleep(90)                   # let the toolkit finish starting first
    while True:
        run()
        time.sleep(AUTO_CHECK)


def run(force=False):
    """snapshot(), but a failure is remembered for the screen instead of raised.

    ⚠ A backup that fails silently is the worst kind: you find out the day you
    need it. So the last error stays on the Backups screen until one succeeds.
    """
    try:
        made = snapshot(force=force)
        _LAST_ERROR['msg'] = None
        return made
    except Exception as e:           # a failed backup must never stop the app
        _LAST_ERROR['msg'] = str(e)[:300]
        print('backup: %s' % e)
        return None


def start():
    """The automatic copy, in the background, for as long as the toolkit runs."""
    threading.Thread(target=_loop, daemon=True, name='library-backup').start()


if __name__ == '__main__':
    import sys
    if '--prune' in sys.argv:
        print('\n'.join(prune()) or 'nothing to remove')
    else:
        t0 = time.time()
        print('made', snapshot(force=True), 'in %.1fs' % (time.time() - t0))
    for row in listing()[:10]:
        print(row['when'], '%.1f MB' % (row['bytes'] / 1e6),
              'db' if row['db'] else '  ', 'xml' if row['xml'] else '   ',
              'tables' if row['tables'] else '')
