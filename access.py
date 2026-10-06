"""Can Backspins actually reach what it needs? Checked, not assumed.

Two things are needed, and both can be refused by the system rather than by
anything wrong in the library:

  · rekordbox's DATABASE — read (always from a copy) and, for edits, written
    while rekordbox is closed. It lives in rekordbox's own folder, which needs
    no special permission on macOS or Windows; it fails when the library is
    on a drive that is not connected, or when the key changed.
  · your MUSIC FILES — read for waveforms, playback, analysis and quality.
    macOS asks before an app may read Desktop, Documents, Downloads, external
    drives and network volumes, and an answer of "Don't allow" (or no answer
    yet) makes every such file look unreadable. That is the common one.

So every track's file is opened (one byte each, a couple of seconds for
thousands) and failures are sorted into "blocked" (permission) and "missing"
(moved, renamed, drive not connected), grouped by where they live, with what
to do about each.

⚠ Nothing is ever asked for silently. The system dialogs appear only when the
app first touches a protected place, and this screen says which places those
are and how to change the answer later.
"""

import os
import platform
import sys

import settings

MAC = platform.system() == 'Darwin'
WIN = platform.system() == 'Windows'

#: macOS System Settings panes, opened by the "Open settings" buttons.
PANES = {
    'files': 'x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders',
    'full': 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles',
    'automation': 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation',
}

_PROTECTED = ('Desktop', 'Documents', 'Downloads')


def who():
    """The name the system asks about — the one to look for in Settings."""
    if settings.FROZEN:
        return 'Backspins'
    return 'Terminal (or the app you started Backspins from) and Python'


def place_of(path):
    """(label, pane) for where a file lives, for grouping and for the fix."""
    home = os.path.expanduser('~')
    p = os.path.abspath(path)
    if MAC and p.startswith('/Volumes/'):
        name = p.split('/')[2]
        return 'Drive "%s"' % name, 'files'
    if p.startswith(home + os.sep):
        top = p[len(home) + 1:].split(os.sep, 1)[0]
        if MAC and top in _PROTECTED:
            return '~/' + top, 'files'
        if MAC and top == 'Library':
            return '~/Library', 'full'
        return '~/' + top, 'full' if MAC else None
    if WIN and len(p) > 2 and p[1] == ':':
        return 'Drive %s' % p[:2].upper(), None
    return os.path.dirname(p), 'full' if MAC else None


def _try_read(path):
    try:
        with open(path, 'rb') as fh:
            fh.read(1)
        return 'ok'
    except PermissionError:
        return 'blocked'
    except FileNotFoundError:
        return 'missing'
    except OSError as e:
        return 'blocked' if getattr(e, 'errno', 0) in (1, 13) else 'missing'


def check(tracks=None):
    """Everything the Access card shows."""
    out = {'platform': platform.system(), 'who': who(), 'warnings': []}

    # Where the app itself is running from. A packaged Mac app opened straight
    # out of Downloads runs from a random read-only copy ("App Translocation"),
    # and permissions granted to that copy do not stick.
    exe = sys.executable or ''
    if MAC and '/AppTranslocation/' in exe:
        out['warnings'].append({
            'kind': 'translocated',
            'text': 'Backspins is running from a temporary copy macOS made of the download. '
                    'Move it to Applications and open it from there, or permissions you give '
                    'it will be asked for again every time.'})

    # The library.
    lib = {'path': settings.find_library() or '', 'state': 'ok', 'detail': ''}
    if not lib['path'] or not os.path.exists(lib['path']):
        lib.update(state='missing', detail='No rekordbox library found. If it lives on an external '
                   'drive, connect it; otherwise set its location in the settings file.')
    else:
        r = _try_read(lib['path'])
        if r != 'ok':
            label, pane = place_of(lib['path'])
            lib.update(state=r, pane=pane,
                       detail='The library is there but cannot be read (%s).' % label)
        else:
            try:
                import rekordbox as rb
                with rb.open_library() as cur:
                    n = cur.execute('SELECT count(*) FROM djmdContent WHERE rb_local_deleted = 0').fetchone()[0]
                lib['tracks'] = n
            except Exception as e:
                lib.update(state='unreadable',
                           detail='The file opens but cannot be decrypted: %s. A newer rekordbox '
                                  'may have changed its key (setting library_key).' % str(e)[:160])
            folder = os.path.dirname(lib['path'])
            lib['writable'] = os.access(lib['path'], os.W_OK) and os.access(folder, os.W_OK)
            if not lib['writable'] and lib['state'] == 'ok':
                lib['detail'] = ('Read-only: Backspins can show your library but cannot save edits '
                                 'to it. Check the permissions of %s.' % folder)
    try:
        import rekordbox_merge as rm
        lib['rekordboxOpen'] = bool(rm.rekordbox_running())
    except Exception:
        lib['rekordboxOpen'] = None
    out['library'] = lib

    # The music files.
    groups, counts = {}, {'ok': 0, 'blocked': 0, 'missing': 0}
    for t in tracks or []:
        path = t.get('path') or ''
        if not path:
            continue
        r = _try_read(path)
        counts[r] += 1
        if r == 'ok':
            continue
        label, pane = place_of(path)
        g = groups.setdefault((r, label), {'state': r, 'place': label, 'pane': pane,
                                           'count': 0, 'example': path})
        g['count'] += 1
    out['files'] = counts
    out['problems'] = sorted(groups.values(), key=lambda g: (g['state'] != 'blocked', -g['count']))
    out['panes'] = PANES if MAC else {}
    return out


def open_pane(name):
    """Open a System Settings privacy pane (macOS only)."""
    url = PANES.get(name)
    if not MAC or not url:
        return False
    import subprocess
    subprocess.Popen(['open', url])
    return True
