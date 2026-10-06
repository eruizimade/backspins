"""Merge the work from two copies of a track INSIDE the rekordbox database.

Philosophy: **it only adds**. It copies over to the copy you keep the tags and
playlists it is missing, the better rating and a merged comment. On its own it
deletes nothing — removing the spare copy is a separate, explicit opt-in.

Safeguards before anything is touched:
  1. rekordbox must be CLOSED (if it writes at the same time, changes are lost
     or worse).
  2. `master.db` is backed up before the first write.
  3. There is a dry run that tells you what it would do without doing it.

The USNs (the numbers rekordbox uses to decide what changed) are handled by
pyrekordbox, which is why writes go through its session and not through raw
SQL.
"""

import datetime
import os
import time
import platform
import shutil
import subprocess
import sys
import uuid

import settings
from rekordbox import merge_comment

BACKUP_DIR = settings.BACKUP_DIR


def rekordbox_running():
    """rekordbox open means nothing gets written.

    This uses pyrekordbox's own helper — the same one that stops it committing
    — so the hint in the interface and the real guard always agree.
    """
    # ⚠ Fails CLOSED: if we cannot tell (pyrekordbox missing, helper errors),
    # assume it IS running and refuse to write. The whole point of the guard is
    # to never write while rekordbox might be open, so "unsure" must block.
    try:
        from pyrekordbox.utils import get_rekordbox_pid
    except Exception:
        return True
    try:
        return bool(get_rekordbox_pid())
    except Exception:
        return True


def trash_file(path):
    """Move to the recycle bin, NOT a real delete: a mistake must be undoable.

    ⚠ Never `os.remove` here. Everything this tool deletes is a music file
    somebody spent money and time on, and "recoverable" is the whole reason
    the merge is safe to offer at all.
    """
    if platform.system() == 'Darwin':
        script = ('tell application "Finder" to delete POSIX file "%s"'
                  % path.replace('\\', '\\\\').replace('"', '\\"'))
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or 'could not move to the Bin').strip()[:200])
        return
    try:
        from send2trash import send2trash
    except ImportError:
        raise RuntimeError(
            'Deleting to the recycle bin on %s needs the send2trash package: '
            '%s -m pip install send2trash' % (platform.system(), sys.executable))
    send2trash(path)


def prune_backups():
    """Thin out old backups by age — the tiers live in library_backup.

    ⚠ This used to keep "the last 20", and one afternoon of edits made 20
    copies in four hours: every backup older than that was gone.
    """
    try:
        import library_backup
        library_backup.prune(BACKUP_DIR)
    except Exception:
        pass


def backup_db(db_path=None, max_age=None):
    """Date-stamped backup of the library. Returns its path.

    ⚠ `max_age` (seconds) reuses a recent backup instead of making another.
    Writing is now automatic — every tag you tick reaches rekordbox on its own
    — and a fresh 40 MB copy per click would fill the disk and tell you nothing
    new. A backup from a few minutes ago restores you to the same place.
    """
    src = db_path or settings.find_library()
    if max_age:
        try:
            recent = sorted(
                (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                 if f.startswith('master-') and f.endswith('.db')
                 and '.part' not in f),
                key=os.path.getmtime, reverse=True)
            if recent and (time.time() - os.path.getmtime(recent[0])) < max_age:
                return recent[0]
        except OSError:
            pass
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # ⚠ Room first. A copy that fills the disk half-way leaves no backup AND
    # a disk rekordbox itself can no longer save to. Refusing here also stops
    # the write that asked for the backup — nothing is written without one.
    need = sum(os.path.getsize(src + s) for s in ('', '-wal', '-shm') if os.path.exists(src + s))
    free = shutil.disk_usage(BACKUP_DIR).free
    if free < need + 512 * 1024 * 1024:
        raise RuntimeError('Not enough free disk space for a backup of the library '
                           '(%d MB needed, %d MB free, keeping 512 MB spare). Nothing was written.'
                           % (need // 1048576, free // 1048576))
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = os.path.join(BACKUP_DIR, 'master-%s.db' % stamp)
    # ⚠ Copy to a private temp name and rename into place. Copied straight to
    # the final name, a 40 MB copy in progress is already listed with a fresh
    # timestamp — so a concurrent backup_db(max_age=…) hands back a TRUNCATED
    # file as "your backup", which is worse than having none.
    tmp = '%s.%d.part' % (dst, os.getpid())
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    for suf in ('-wal', '-shm'):
        if os.path.exists(src + suf):
            t2 = '%s%s.%d.part' % (dst, suf, os.getpid())
            shutil.copy2(src + suf, t2)
            os.replace(t2, dst + suf)
    prune_backups()
    return dst


def _open_db(db_path=None):
    from pyrekordbox import Rekordbox6Database
    return Rekordbox6Database(path=db_path or settings.find_library(),
                              key=settings.library_key())


def create_set_playlist(name, content_ids, db_path=None, playlist_id=None):
    """Save a set to rekordbox: as a NEW list, or over an existing one.

    The order is fixed by writing `TrackNo` by hand (1..N) instead of using
    `add_to_playlist`, which always appends and also counts deleted rows: when
    overwriting, the order would have come out shifted.
    """
    from pyrekordbox.db6 import tables

    if rekordbox_running():
        raise RuntimeError('Close rekordbox to save a playlist into it.')
    # ⚠ Overwriting with an empty list would wipe the playlist.
    if not content_ids:
        raise RuntimeError('There are no tracks to save.')
    backup = backup_db(db_path)

    db = _open_db(db_path)
    try:
        # ⚠ Resolve the tracks FIRST, before touching any existing rows. And
        # dedupe: a repeated id must not create two rows with different TrackNo.
        seen, valid = set(), []
        for cid in content_ids:
            sid = str(cid)
            if sid in seen or db.get_content(ID=sid) is None:
                continue
            seen.add(sid)
            valid.append(sid)
        # ⚠ Overwriting with nothing that resolves would SILENTLY EMPTY the
        # playlist (a stale snapshot after a re-import fails every lookup).
        # Refuse before deleting anything.
        if playlist_id and not valid:
            raise RuntimeError('None of those tracks are in the library any more '
                               '— refusing to empty the playlist. Reload and retry.')

        if playlist_id:
            plist = db.get_playlist(ID=str(playlist_id))
            if plist is None:
                raise RuntimeError('That playlist no longer exists.')
            if plist.Attribute != 0:
                raise RuntimeError('Only ordinary playlists can be overwritten.')
            # Old rows are marked deleted the way rekordbox itself does it.
            for row in db.query(tables.DjmdSongPlaylist).filter_by(
                    PlaylistID=str(plist.ID)):
                if not row.rb_local_deleted:
                    row.rb_local_deleted = 1
        else:
            plist = db.create_playlist(name)

        now = datetime.datetime.now()
        added = 0
        for sid in valid:
            added += 1
            db.add(tables.DjmdSongPlaylist.create(
                ID=str(uuid.uuid4()), PlaylistID=str(plist.ID),
                ContentID=sid, TrackNo=added, UUID=str(uuid.uuid4()),
                created_at=now, updated_at=now))
        db.commit()
        return {'name': plist.Name, 'added': added, 'backup': backup,
                'overwritten': bool(playlist_id), 'id': str(plist.ID)}
    finally:
        try:
            db.close()
        except Exception:
            pass


def remove_content(db, content, tables):
    """Retire a track in rekordbox, exactly the way rekordbox does it.

    Taken from the retirements already present in a real library: the
    `djmdContent` row ends up with `rb_local_deleted=1` and
    `rb_data_status=258`, and its MyTag and playlist rows are marked deleted
    too — otherwise ghost entries stay behind in the lists.
    """
    cid = str(content.ID)
    content.rb_local_deleted = 1
    content.rb_data_status = 258
    for model in (tables.DjmdSongMyTag, tables.DjmdSongPlaylist):
        for row in db.query(model).filter_by(ContentID=cid):
            row.rb_local_deleted = 1


def file_identity(path):
    """What really identifies a file: device + inode, not the text of the path.

    ⚠ macOS disks (APFS by default) are case-INSENSITIVE: "X.aiff" and "x.aiff"
    are THE SAME file, but as text they look like two. Comparing paths, a merge
    treated them as different copies and sent one to the bin… taking with it
    the file belonging to the copy you were keeping, which was left pointing at
    nothing. With the inode that cannot happen (and hard links are covered too).
    """
    try:
        st = os.stat(path)
        return ('fs', st.st_dev, st.st_ino)
    except OSError:
        return ('path', os.path.normcase(os.path.abspath(path or '')))


def _place_after(db, tables, playlist_id, keeper_id, other_id, track_no):
    """Move the keeper to where the retired copy sat, and renumber cleanly.

    ⚠ Renumbers the WHOLE list from 1 rather than patching the neighbours.
    rekordbox tolerates gaps and repeats in TrackNo, so a partial fix leaves a
    list that looks right today and reorders itself the next time anything
    touches it. Cheap: these lists are tens of rows, not thousands.
    """
    rows = [r for r in db.query(tables.DjmdSongPlaylist)
            .filter_by(PlaylistID=str(playlist_id), rb_local_deleted=0)]
    if not rows:
        return
    keeper_row = None
    rest = []
    for r in rows:
        if str(r.ContentID) == keeper_id and keeper_row is None:
            keeper_row = r
        else:
            rest.append(r)
    if keeper_row is None:
        return
    rest.sort(key=lambda r: (r.TrackNo or 0))

    # Slot the keeper immediately before the copy it replaces, so that when
    # that copy goes the keeper is left exactly in its place.
    out, placed = [], False
    for r in rest:
        if not placed and str(r.ContentID) == other_id:
            out.append(keeper_row)
            placed = True
        out.append(r)
    if not placed:
        # The retired copy is not here (already gone): fall back to the number
        # it used to hold.
        idx = max(0, min(len(out), (track_no or len(out) + 1) - 1))
        out.insert(idx, keeper_row)

    for i, r in enumerate(out):
        r.TrackNo = i + 1


def apply_merge(keeper_id, other_ids, db_path=None, dry_run=True,
                delete_others=False):
    """Pour the work of `other_ids` into `keeper_id`. Returns what it did.

    With `delete_others`, once everything has been poured over, the spare
    copies are retired in rekordbox and their files go to the RECYCLE BIN
    (recoverable).
    """
    from pyrekordbox.db6 import tables

    if not dry_run and rekordbox_running():
        raise RuntimeError(
            'Close rekordbox before merging: if it writes at the same time we '
            'do, the changes can be lost.')

    backup = None
    if not dry_run:
        backup = backup_db(db_path)

    db = _open_db(db_path)
    try:
        keeper = db.get_content(ID=str(keeper_id))
        if keeper is None:
            raise RuntimeError('The copy you want to keep was not found.')

        # ⚠ Essential safety net: if by mistake the copy you are keeping also
        # appeared in the list of spares, we would retire it along with all its
        # tags. Duplicates in the list are dropped here too.
        clean, seen_ids = [], set()
        for oid in other_ids:
            sid = str(oid)
            if sid == str(keeper_id) or sid in seen_ids:
                continue
            seen_ids.add(sid)
            clean.append(sid)
        other_ids = clean
        if not other_ids:
            raise RuntimeError('There is no spare copy to merge.')

        done = {'tags': [], 'playlists': [], 'rating': None, 'comment': None,
                'backup': backup, 'dry_run': dry_run, 'removed': [], 'trashed': [],
                'note': None}

        have_tags = {str(r.MyTagID) for r in
                     db.query(tables.DjmdSongMyTag).filter_by(
                         ContentID=str(keeper_id))}
        have_lists = {str(r.PlaylistID) for r in
                      db.query(tables.DjmdSongPlaylist).filter_by(
                          ContentID=str(keeper_id))}
        now = datetime.datetime.now()
        keeper_ident = file_identity(keeper.FolderPath or '')
        # ⚠ If the copy you are KEEPING has no file on disk, do not trash the
        # spares' files: the merged, well-tagged row would end up pointing at
        # nothing while the audio went to the bin. Rows with an absent file are
        # normal here (that is the whole premise of repair_orphans).
        keeper_has_file = bool(keeper.FolderPath) and os.path.exists(keeper.FolderPath)
        if delete_others and not keeper_has_file:
            delete_others = False
            done_keeper_note = ('the copy you are keeping has no file on disk, so '
                                'nothing was deleted — fix its path first')
        else:
            done_keeper_note = None
        done['note'] = done_keeper_note
        paths_to_trash = []
        trash_idents = set()
        other_comments = []

        for other_id in other_ids:
            other = db.get_content(ID=str(other_id))
            if other is None:
                continue
            other_path = other.FolderPath or ''
            # Safety net: two rows can point at the SAME file. If they did,
            # deleting it would take the copy you are keeping with it.
            oid = file_identity(other_path) if other_path else None
            if oid and oid != keeper_ident and oid not in trash_idents:
                trash_idents.add(oid)
                paths_to_trash.append(other_path)

            # --- tags it is missing
            # ⚠ Only LIVE ones: a real library carries thousands of retired
            # MyTag rows, and copying those would resurrect tags that were
            # deliberately removed at some point.
            for row in db.query(tables.DjmdSongMyTag).filter_by(
                    ContentID=str(other_id), rb_local_deleted=0):
                tag_id = str(row.MyTagID)
                if tag_id in have_tags:
                    continue
                have_tags.add(tag_id)
                tag = db.get_my_tag(ID=tag_id)
                done['tags'].append(getattr(tag, 'Name', tag_id))
                if dry_run:
                    continue
                db.add(tables.DjmdSongMyTag.create(
                    ID=str(uuid.uuid4()), MyTagID=tag_id,
                    ContentID=str(keeper_id), TrackNo=0,
                    UUID=str(uuid.uuid4()), created_at=now, updated_at=now))

            # --- playlists the other one is in and ours is not (live rows
            # only: a membership that was removed should not come back)
            for row in db.query(tables.DjmdSongPlaylist).filter_by(
                    ContentID=str(other_id), rb_local_deleted=0):
                pid = str(row.PlaylistID)
                if pid in have_lists:
                    continue
                plist = db.get_playlist(ID=pid)
                # Attribute: 0 = ordinary list. Smart lists and folders are out.
                if plist is None or plist.Attribute != 0:
                    continue
                have_lists.add(pid)
                done['playlists'].append(plist.Name)
                if not dry_run:
                    # ⚠ AT THE POSITION IT HELD, not at the end. A playlist is
                    # an ORDER — a set, a crate in running order — and a track
                    # that used to be fourth turning up fortieth has lost the
                    # only information the list carried about it. This used to
                    # append, on the grounds that renumbering was not worth the
                    # risk for a duplicate; that was wrong, because the copy
                    # being retired is precisely the one that was placed there
                    # on purpose.
                    db.add_to_playlist(plist, keeper)
                    _place_after(db, tables, pid, str(keeper_id),
                                 str(other_id), row.TrackNo)

            # ⚠ THE COLOUR LABEL IS NEVER COPIED, and that is deliberate.
            # The copy you keep is usually the better FILE — the extended
            # lossless you just found — and the one you are retiring is the
            # old MP3 or radio edit that has been through your hands: gridded,
            # cued, marked "OK". Copying that OK across would claim the new
            # file has been vetted when its beat grid has not even been
            # checked. Everything else transfers; the colour stays as it is,
            # so the track lands in the vetting queue where it belongs.

            # --- rating
            if (other.Rating or 0) > (keeper.Rating or 0):
                done['rating'] = other.Rating
                if not dry_run:
                    keeper.Rating = other.Rating
            # The comment is merged at the end, once every copy has been seen:
            # deciding it one at a time would make the result depend on order.
            other_comments.append(other.Commnt or '')

        merged = merge_comment(keeper.Commnt or '', other_comments)
        if merged:
            done['comment'] = merged
            if not dry_run:
                keeper.Commnt = merged

        # Retiring comes AFTER everything has been poured over: if something
        # fails before this point, you still have both copies and have lost
        # nothing.
        if delete_others:
            for other_id in other_ids:
                other = db.get_content(ID=str(other_id))
                if other is None:
                    continue
                done['removed'].append(os.path.basename(other.FolderPath or ''))
                if not dry_run:
                    remove_content(db, other, tables)

        if not dry_run:
            db.commit()

        # The files themselves, only once the database has saved cleanly.
        if delete_others and not dry_run:
            for path in list(paths_to_trash):
                try:
                    if os.path.exists(path):
                        trash_file(path)
                        done['trashed'].append(os.path.basename(path))
                except Exception as e:
                    done.setdefault('trash_errors', []).append(
                        '%s: %s' % (os.path.basename(path), e))
        return done
    finally:
        try:
            db.close()
        except Exception:
            pass
