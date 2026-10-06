#!/usr/bin/env python3
"""Put freshly converted tracks INTO rekordbox, marked for review.

The last gap in the chain: the converter leaves finished files in your library
folder, and until now you still had to bring them into rekordbox by hand. This
writes the row for you and stamps the colour that means "not looked at yet"
(ADD here), so a track downloaded five minutes ago is already sitting in the
Tagger's queue waiting for you.

⚠ THIS CREATES NEW ROWS in master.db — everything else this toolkit does to
your library only edits existing ones. Same rules, applied harder:
  · rekordbox must be CLOSED (checked here, and pyrekordbox checks again on
    commit — it fails closed if it cannot tell).
  · master.db is backed up before the first write.
  · A file already in the library is skipped, never added twice: the guard is
    pyrekordbox's own path check plus our own before it, so a re-run of the
    same folder is harmless.

⚠ What this does NOT do is ANALYSE. The waveform, the beatgrid and the key are
rekordbox's own work, and nothing here can fake them: an imported track arrives
with tags, duration and colour, and no analysis. Select the new ones in
rekordbox and run Analyse — which is the same step that produces the beatgrid
you were going to check anyway.
"""

import os

import rekordbox_merge as rm
import settings

AUDIO_OK = ('.aiff', '.aif', '.flac', '.wav', '.mp3', '.m4a')


def _tags(path):
    """Title, artist, album, genre, duration and stream facts, best-effort."""
    import mutagen

    out = {'title': '', 'artist': '', 'album': '', 'genre': '',
           'length': 0, 'bitrate': 0, 'rate': 0, 'depth': 0}
    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        easy = None
    if easy is not None and easy.tags:
        def first(key):
            v = easy.tags.get(key)
            return str(v[0]).strip() if v else ''
        out['title'] = first('title')
        out['artist'] = first('artist')
        out['album'] = first('album')
        out['genre'] = first('genre')
    try:
        raw = mutagen.File(path)
        info = getattr(raw, 'info', None)
        out['length'] = int(getattr(info, 'length', 0) or 0)
        out['bitrate'] = int((getattr(info, 'bitrate', 0) or 0) / 1000)
        out['rate'] = int(getattr(info, 'sample_rate', 0) or 0)
        out['depth'] = int(getattr(info, 'bits_per_sample', 0) or 0)
        # ID3 inside AIFF/WAV is invisible to the easy interface.
        if raw is not None and raw.tags is not None and not out['title']:
            def rawget(*keys):
                for k in keys:
                    try:
                        v = raw.tags.get(k)
                    except Exception:
                        v = None
                    if v is not None:
                        txt = getattr(v, 'text', None)
                        return str(txt[0] if txt else v).strip()
                return ''
            out['title'] = out['title'] or rawget('TIT2')
            out['artist'] = out['artist'] or rawget('TPE1')
            out['album'] = out['album'] or rawget('TALB')
            out['genre'] = out['genre'] or rawget('TCON')
    except Exception:
        pass
    if not out['title']:
        out['title'] = os.path.splitext(os.path.basename(path))[0]
    return out


def _find_color(db, tables, name):
    """The colour row, tolerant of the stray spaces rekordbox names collect."""
    want = (name or '').strip().casefold()
    if not want:
        return None
    for row in db.query(tables.DjmdColor):
        if (row.Commnt or '').strip().casefold() == want:
            return row
    return None


def _link(db, name, table, adder):
    """The id of an artist / album / genre, creating it only if new.

    ⚠ pyrekordbox's add_* RAISE when the name already exists — they are "create"
    calls, not "get or create". Calling them blind failed five of six imports on
    the first run, on names as ordinary as the genre "Dance". Look first.

    ⚠ And look only among LIVE rows. pyrekordbox's get_artist/get_genre are a
    plain filter on the name with no `rb_local_deleted` condition, so a name you
    once deleted comes back first and the import is linked to a tombstone — the
    track then shows no artist in rekordbox at all.
    """
    name = (name or '').strip()
    if not name:
        return None

    def live():
        try:
            return db.query(table).filter_by(Name=name, rb_local_deleted=0).first()
        except Exception:
            return None

    row = live()
    if row is None:
        try:
            row = adder(name)
        except Exception:
            row = live()      # lost a race: take whatever is there now
    return str(row.ID) if row is not None else None


def find_row(db, tables, path):
    """The library row for this file, live or retired, or None.

    ⚠ Two traps, both of which created duplicates or dead ends:
    · A row this toolkit RETIRED (rb_local_deleted=1) used to count as "already
      in the library", so a track you deleted could never be imported again —
      it was skipped for ever with a cheerful "already there".
    · The library volume is case-insensitive APFS, where "Dj Koze - X.aiff" and
      "DJ Koze - X.aiff" are one file. Comparing the path byte-for-byte let a
      case variant through and added a SECOND row for the same audio.
    """
    want = os.path.normcase(os.path.abspath(str(path)))
    live = dead = None
    for row in db.query(tables.DjmdContent).filter(
            tables.DjmdContent.FolderPath.isnot(None)):
        fp = row.FolderPath or ''
        if os.path.normcase(fp) != want:
            continue
        if int(getattr(row, 'rb_local_deleted', 0) or 0):
            dead = dead or row
        else:
            live = row
            break
    return live, dead


#: The state a track is in the moment rekordbox has added it and not yet looked
#: at it. `add_content` leaves every one of these NULL, which is a state no row
#: rekordbox made is ever in — a native row carries zeros and empty strings.
#: (Read from a row rekordbox itself created, not guessed: see
#: github.com/dylanljones/pyrekordbox/issues/118 for one dumped raw.)
#:
#: KeyID and BPM stay out on purpose: they are answers the analysis gives, and
#: guessing them would put a wrong key on screen instead of no key.
_FRESH_ROW = {
    'Analysed': 0,
    'AnalysisDataPath': '',
    'AnalysisUpdated': '0',
    'TrackInfoUpdated': '0',
    'ServiceID': 0,
    'VideoAssociate': '0',
    'ExtInfo': 'null',
    'ImagePath': '',
    'DJPlayCount': 0,
    'Rating': 0,           # never NULL on a native row (0 of 2,518)
    'Commnt': '',
    'TrackNo': 0,
    'DiscNo': 0,
    'ReleaseYear': 0,
    'SamplerTrackInfo': 0,
    'SamplerPlayOffset': 0,
    'SamplerGain': 0.0,
    'LyricStatus': 0,
    'Reserved1': '',
    # strings a native row leaves EMPTY, never NULL
    'FileNameS': '',
    'Subtitle': '',
    'ReleaseDate': '',
    'ModifiedByRBM': '',
    'DeliveryComment': '',
    'Lyricist': '',
    'ISRC': '',
    # the old KUVO "public" flag; every row rekordbox has made since 2025 has it
    'DeliveryControl': 'on',
}

#: ⚠⚠ ContentLink. This is the field that made a whole evening of imports
#: silent, and it is worth reading before touching anything here.
#:
#: pyrekordbox's add_content fills it with the `rb_local_usn` of the "TRACK"
#: menu item — a guess its own authors flagged as such ("This could just be a
#: coincidence", pyrekordbox PR #121; the same author later gave up on it in
#: rbox: "No clue what content link should be"). It is not a link at all. In
#: the rekordbox 7 binary it is a BIT FIELD the app itself edits with OR
#: (`setHasVideoToContentLink` = bit 12, `setAnalyzedSongStructToContentLink`
#: = bit 16), reads back when you load a deck (`select FolderPath, ContentLink
#: … ___deck___`), and tests before loading: `isLoadable` returns false the
#: moment bits 11 AND 12 are both set. No message. Nothing happens.
#:
#: In this library the TRACK usn happened to be 146059 = 0x23A8B — bits 11 and
#: 12 both on. Every row we created was born unloadable, analysed fine (the
#: analyser does not check), and never played. Of 2,845 rows only ours had
#: those bits.
#:
#: So: never keep what add_content put there. Copy the value rekordbox itself
#: writes today — the commonest ContentLink among the newest rows it made —
#: and REFUSE to import if what we are about to write carries the fatal bits.
#: A refused import is a message; a silent track is a bug you find on stage.
_UNLOADABLE_BITS = 0x1800          # bits 11 + 12: rekordbox's "not loadable"
_LOW_BYTE_OK = (0x0E, 0x00)        # every native value ends in one of these


def native_content_link(db, tables):
    """The ContentLink rekordbox is writing to new tracks in THIS library.

    Taken from the newest rows that are not ours (nothing with the fatal
    bits), by majority. Falls back to 0x0E — the seed rekordbox's own addTrack
    uses (`(CL & 0x26000) | 0x0E`) — only if the library gives us nothing.
    """
    import collections
    rows = (db.query(tables.DjmdContent)
              .filter_by(rb_local_deleted=0)
              .order_by(tables.DjmdContent.created_at.desc())
              .limit(200))
    votes = collections.Counter()
    for r in rows:
        v = r.ContentLink
        if v is None or (v & _UNLOADABLE_BITS) or (v & 0xFF) not in _LOW_BYTE_OK:
            continue
        votes[int(v)] += 1
    if not votes:
        return 0x0E
    return votes.most_common(1)[0][0]


def assert_loadable(content):
    """Stop the import rather than create a track the deck will refuse."""
    v = content.ContentLink
    if v is None or (v & _UNLOADABLE_BITS):
        raise RuntimeError(
            'refusing to import: ContentLink %r would carry the bits rekordbox '
            'treats as "not loadable" (0x1800)' % v)


def _mark_unanalysed(content, path, content_link):
    """Give a row we just created the shape of one rekordbox made itself."""
    # add_content fills FolderPath alone; everything here that repoints a track
    # (fix_stragglers, repair_orphans) writes the pair, so we do too.
    content.OrgFolderPath = path
    for field, value in _FRESH_ROW.items():
        if getattr(content, field, None) is None:
            setattr(content, field, value)
    # ⚠ Overwrite, do not "fill if empty": add_content has already put the
    # toxic value there. See the note above _UNLOADABLE_BITS.
    content.ContentLink = content_link
    assert_loadable(content)


def import_files(paths, color=None, db_path=None, log=None, dry_run=False):
    """Add these files to rekordbox. Returns a summary.

    `color` defaults to the "needs a decision" colour from Settings — the same
    one the Tagger's first queue is built from, so imports land where you will
    actually see them.
    """
    from pyrekordbox.db6 import tables

    def say(level, text):
        if log:
            log(level, text)

    files = [p for p in paths
             if os.path.splitext(p)[1].lower() in AUDIO_OK and os.path.exists(p)]
    if not files:
        return {'added': 0, 'skipped': 0, 'failed': [], 'backup': None}

    if color is None:
        cfg = settings.load()
        color = (cfg.get('import_color') or 'NEW').strip()

    if not dry_run and rm.rekordbox_running():
        raise RuntimeError('Close rekordbox before importing.')

    backup = None if dry_run else rm.backup_db(db_path)
    db = rm._open_db(db_path)
    added = skipped = 0
    failed = []
    try:
        col = _find_color(db, tables, color)
        if color and col is None:
            raise RuntimeError('There is no colour called %r in rekordbox.' % color)
        content_link = native_content_link(db, tables)

        for path in files:
            path = os.path.abspath(path)
            try:
                live, dead = find_row(db, tables, path)
                if live is not None:
                    skipped += 1
                    say('info', '%s   ·   already in the library'
                        % os.path.basename(path))
                    continue
                t = _tags(path)
                if dead is not None and not dry_run:
                    # It was here and you deleted it. Bring the SAME row back
                    # rather than adding a second one: its play count, cues and
                    # playlist memberships are still attached to it.
                    dead.rb_local_deleted = 0
                    dead.Title = t['title']
                    dead.FolderPath = path
                    dead.OrgFolderPath = path
                    # a row we made before this fix may carry the fatal bits
                    if dead.ContentLink is None or (dead.ContentLink & _UNLOADABLE_BITS):
                        dead.ContentLink = content_link
                    if col is not None:
                        dead.ColorID = str(col.ID)
                    db.flush()
                    added += 1
                    say('ok', '%s   ·   %s (brought back)' % (t['title'], color or ''))
                    continue
                if dry_run:
                    added += 1
                    say('ok', '%s   ·   %s — %s'
                        % (os.path.basename(path), t['artist'] or '?', t['title']))
                    continue

                content = db.add_content(
                    path,
                    Title=t['title'],
                    Length=t['length'],
                    BitRate=t['bitrate'],
                    SampleRate=t['rate'],
                    BitDepth=t['depth'] or None,
                )
                _mark_unanalysed(content, path, content_link)
                content.ArtistID = _link(db, t['artist'], tables.DjmdArtist,
                                         db.add_artist) or content.ArtistID
                content.AlbumID = _link(db, t['album'], tables.DjmdAlbum,
                                        db.add_album) or content.AlbumID
                content.GenreID = _link(db, t['genre'], tables.DjmdGenre,
                                        db.add_genre) or content.GenreID
                if col is not None:
                    content.ColorID = str(col.ID)
                db.flush()
                added += 1
                say('ok', '%s   ·   %s' % (t['title'], color or ''))
            except Exception as e:
                failed.append({'path': path, 'why': str(e)[:160]})
                say('error', '%s   ·   %s'
                    % (os.path.basename(path), str(e)[:120]))

        if not dry_run and added:
            db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass

    say('info', 'Imported: %d   |   already there: %d   |   failed: %d'
        % (added, skipped, len(failed)))
    if added and not dry_run:
        say('info', 'Select them in rekordbox and run Analyse — that is what '
                    'builds the beatgrid and the waveform.')
    return {'added': added, 'skipped': skipped, 'failed': failed,
            'backup': backup, 'color': color}
