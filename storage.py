"""Everything Backspins keeps on disk: what it is, where, how big, and whether
it can go.

The screen this feeds exists because nobody should have to discover, months
later, that a tool quietly filled their disk. Library backups are the big one
— a full copy of rekordbox's database each time, 40-odd MB for a few thousand
tracks — and they are kept by age, so the total depends on the library and
on how long the app has been running. All of that is shown, in bytes, with
the folder it lives in.

Three kinds of thing, treated differently:

  · BACKUPS — copies of your library. Never cleared from here in bulk; the
    size limit (settings `backup_budget_mb`) decides how many old ones stay.
  · CACHES — work the app can redo: audio analysis, energy, artwork it found,
    playback previews, logs. Clearing one costs time, never data.
  · YOUR WORK — the tagging queue, lessons, pairings, settings. Listed so you
    know it is there; never offered for deletion.
"""

import os
import shutil
import tempfile

import settings

MB = 1024 * 1024


def _size(path):
    """Bytes in a file or a folder (recursively). 0 when it is not there."""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _app(name):
    return os.path.join(settings.APP_DIR, name)


def preview_dir():
    return os.path.join(tempfile.gettempdir(), 'rekordbox-toolkit-preview')


def _items():
    """(key, group, label, what it is, [paths], clearable)."""
    import library_backup as lb
    bdir = lb.BACKUP_DIR
    try:
        names = os.listdir(bdir)
    except OSError:
        names = []
    db = [os.path.join(bdir, n) for n in names if n.startswith('master-')]
    text = [os.path.join(bdir, n) for n in names if n.startswith(('library-', 'tables-'))]
    other = [os.path.join(bdir, n) for n in names
             if not n.startswith(('master-', 'library-', 'tables-', 'snapshots', '.'))]
    logs = [_app(n) for n in _listdir(settings.APP_DIR) if n.endswith('.log')]
    if os.path.exists(os.path.join(settings.DATA_DIR, 'flac-a-aiff.log')):
        logs.append(os.path.join(settings.DATA_DIR, 'flac-a-aiff.log'))
    return [
        ('backups-db', 'backups', 'Library backups',
         'Full copies of rekordbox\'s database, kept by age (and by the size limit below).',
         db, False),
        ('backups-text', 'backups', 'Readable backups',
         'Your library as rekordbox XML and as JSON: small, and readable without rekordbox.',
         text, False),
        ('backups-other', 'backups', 'Other files in the backups folder',
         'Things that were put there by hand. Backspins never touches them.',
         other, False),
        ('dna', 'cache', 'Audio analysis',
         'What Teach heard in each track. Clearing it means analysing again (about ten minutes for 2,500 tracks).',
         [_app('dna.json')], True),
        ('energy', 'cache', 'Energy',
         'The energy of each track, worked out from rekordbox\'s waveforms. Rebuilt in seconds.',
         [_app('drive.json')], True),
        ('artwork', 'cache', 'Artwork found',
         'Covers found for tracks without one, waiting for you to apply them.',
         [_app('artwork-staging'), _app('artwork-found.json')], True),
        ('previews', 'cache', 'Playback previews',
         'Temporary copies of tracks the player could not play directly.',
         [preview_dir()], True),
        ('logs', 'cache', 'Logs', 'What the app did, for when something goes wrong.', logs, True),
        ('work', 'work', 'Your work',
         'The tagging queue, Teach lessons, pairings, linked playlists and settings. Never deleted from here.',
         [_app(n) for n in ('queue.json', 'lessons.json', 'pairs.json', 'linked-playlists.json',
                            'created-playlists.json', 'settings.json', 'dupes-ignored.json',
                            'unfindable.json')], False),
    ]


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def report():
    """The Storage screen, in one call."""
    rows, total = [], 0
    for key, group, label, what, paths, clearable in _items():
        size = sum(_size(p) for p in paths)
        total += size
        if not size and group != 'backups':
            continue
        rows.append({'key': key, 'group': group, 'label': label, 'what': what,
                     'bytes': size, 'clearable': clearable,
                     'where': os.path.dirname(paths[0]) if paths else ''})
    import library_backup as lb
    db_size = 0
    try:
        db_size = os.path.getsize(settings.find_library() or '')
    except OSError:
        pass
    try:
        du = shutil.disk_usage(lb.BACKUP_DIR if os.path.isdir(lb.BACKUP_DIR) else settings.DATA_DIR)
        free, disk = du.free, du.total
    except OSError:
        free = disk = None
    return {'rows': rows, 'total': total, 'free': free, 'disk': disk,
            'appDir': settings.APP_DIR, 'backupDir': lb.BACKUP_DIR,
            'librarySize': db_size,
            'budgetMb': int(settings.load().get('backup_budget_mb') or 0),
            # Worst case with no limit: the age tiers keep at most this many.
            'maxCopies': lb.MAX_DB_COPIES}


def clear(key):
    """Empty one cache. Refuses anything that is not a cache."""
    for k, group, _, _, paths, clearable in _items():
        if k != key:
            continue
        if not clearable:
            raise ValueError('That is not something to clear from here.')
        freed = 0
        for p in paths:
            freed += _size(p)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        return {'freed': freed}
    raise ValueError('Unknown item.')
