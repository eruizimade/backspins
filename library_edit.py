#!/usr/bin/env python3
"""Removing a track from the library, from the browser.

⚠ "Delete" here means BOTH sides — the row in rekordbox and the file on disk —
because leaving one behind is what produces the two states this toolkit spends
most of its time repairing: a row pointing at nothing, or a file nobody knows
about.

⚠ Neither side is destroyed:
  · The rekordbox row is marked `rb_local_deleted = 1`, which is what rekordbox
    itself does when you remove a track from the collection. Reversible.
  · The file goes to the Trash, never `os.remove`. Reversible until you empty it.

There is no undo button here, and there does not need to be one: the recovery
path is the Trash and a database backup, both of which exist before the first
row is touched.
"""

import os

import rekordbox_merge as rm


def delete_tracks(track_ids, remove_files=True, db_path=None, log=None,
                  dry_run=False):
    """Remove these tracks from rekordbox, and bin their files.

    Returns what happened per track, so the UI can say it plainly.
    """
    from pyrekordbox.db6 import tables

    def say(level, text):
        if log:
            log(level, text)

    ids = [str(i) for i in (track_ids or []) if str(i).strip()]
    if not ids:
        return {'removed': 0, 'trashed': 0, 'failed': [], 'backup': None}

    if not dry_run and rm.rekordbox_running():
        raise RuntimeError('Close rekordbox before deleting from it.')

    backup = None if dry_run else rm.backup_db(db_path)
    db = rm._open_db(db_path)
    removed = trashed = 0
    failed = []
    to_trash = []          # binned only once the commit has gone through
    try:
        for tid in ids:
            try:
                c = db.get_content(ID=tid)
                if c is None:
                    failed.append({'id': tid, 'why': 'not in the library any more'})
                    continue
                path = c.FolderPath
                name = c.Title or os.path.basename(path or '') or tid
                if dry_run:
                    removed += 1
                    say('ok', '%s   ·   would be removed' % name)
                    continue

                # ⚠ Database FIRST, file second — the same order the converter
                # uses. If the row is marked and the file cannot be binned you
                # have a tidy library and a stray file; the other way round you
                # have rekordbox pointing at nothing, which is worse and much
                # harder to notice.
                #
                # ⚠⚠ And the file waits for the COMMIT, not just the flush. A
                # flush is only transactional: if the commit later fails —
                # rekordbox opened mid-run, or an earlier error poisoned the
                # session — every mark rolls back while the files are already
                # in the Trash. That is the one outcome this whole module
                # exists to prevent: live rows pointing at nothing.
                c.rb_local_deleted = 1
                db.flush()
                removed += 1
                if remove_files and path and os.path.exists(path):
                    to_trash.append((name, path))
                else:
                    say('ok', '%s   ·   removed from rekordbox '
                              '(no file on disk)' % name)
            except Exception as e:
                failed.append({'id': tid, 'why': str(e)[:160]})
                say('error', '%s   ·   %s' % (tid, str(e)[:110]))

        if not dry_run and removed:
            db.commit()
        # The rows are durably marked now: the files can follow.
        for name, path in to_trash:
            try:
                rm.trash_file(path)
                trashed += 1
                say('ok', '%s   ·   removed, file in the Trash' % name)
            except Exception as e:
                say('error', '%s   ·   removed from rekordbox, but the file '
                             'could not be binned: %s' % (name, str(e)[:90]))
    except Exception:
        # ⚠ The commit failed: the marks rolled back, so the files must STAY.
        say('error', 'Nothing was deleted — the database would not accept the '
                     'change, so the files were left alone.')
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass

    say('info', 'Removed: %d   |   files binned: %d   |   failed: %d'
        % (removed, trashed, len(failed)))
    return {'removed': removed, 'trashed': trashed, 'failed': failed,
            'backup': backup}
