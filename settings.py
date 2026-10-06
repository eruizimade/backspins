"""What the toolkit knows about *your* setup, and how it finds it.

Everything in here used to be a constant somewhere in the code, tuned to one
person's library. That is fine until somebody else runs it: their rekordbox
lives elsewhere, they mark "ready to play" with a different colour, and their
genre spellings are wrong in their own particular way.

The settings file is plain JSON at `~/.config/rekordbox-toolkit/settings.json`.
Nothing in it is secret, and deleting it just puts every default back.

⚠ Auto-detection is never silently overridden: a value you set by hand always
wins, and an empty value means "figure it out for me". So a library on an
external drive keeps working after an update, and a wrong guess is one edit
away from being fixed.
"""

import json
import os
import shutil
import sys

#: Running as a packaged app (PyInstaller) rather than from the source folder.
FROZEN = bool(getattr(sys, 'frozen', False))


def _default_app_dir():
    # ⚠ The folder kept its old name for whoever already has one: the tagging
    # queue in it is work waiting to be written, and a rename must never
    # strand it.
    # Oldest name first, then 0.1.x's "Backspin", then today's.
    home = os.path.expanduser('~')
    appdata = os.environ.get('APPDATA') if os.name == 'nt' else None
    for legacy in (os.path.join(home, '.config', 'rekordbox-toolkit'),
                   os.path.join(appdata, 'Backspin') if appdata else
                   os.path.join(home, '.config', 'backspin')):
        if os.path.isdir(legacy):
            return legacy
    if appdata:
        return os.path.join(appdata, 'Backspins')
    return os.path.join(home, '.config', 'backspins')


# ⚠ Redirectable on purpose. Everything personal lives here — your settings
# and, more importantly, the tagging QUEUE — and all instances of the app share
# it. Pointing a second copy somewhere else is the only way to try things
# without risking work that is waiting to be synced.
APP_DIR = (os.environ.get('BACKSPINS_HOME') or os.environ.get('BACKSPIN_HOME')
           or os.environ.get('REKORDBOX_TOOLKIT_HOME')
           or _default_app_dir())

# What the app writes beside itself — library backups, the conversion log.
# Run from source that is the source folder, as it always was; a packaged app
# lives somewhere read-only (Program Files, a signed .app), so it uses APP_DIR.
DATA_DIR = APP_DIR if FROZEN else os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')

# Tools the app installs for you (ffmpeg_get.py), FIRST on the PATH so every
# "ffmpeg" the app runs finds them. ⚠ Plus Homebrew's folders: an app opened
# from the Finder gets a bare PATH and would not see a brew-installed ffmpeg.
TOOLS_DIR = os.path.join(APP_DIR, 'bin')
_extra = [TOOLS_DIR] + (['/opt/homebrew/bin', '/usr/local/bin'] if sys.platform == 'darwin' else [])
_path = os.environ.get('PATH', '').split(os.pathsep)
os.environ['PATH'] = os.pathsep.join([TOOLS_DIR] + [p for p in _path if p and p != TOOLS_DIR]
                                     + [p for p in _extra[1:] if p not in _path])
SETTINGS_FILE = os.path.join(APP_DIR, 'settings.json')

# The published SQLCipher passphrase for rekordbox 6.6.5+. It is used as a
# TEXT passphrase, not as a hex key. Newer builds may rotate it; if they do,
# `pyrekordbox`'s key extraction can read it back out of the app itself and
# the result goes in `library_key` below.
DEFAULT_KEY = '402fd482c38817c35ffa8ffb8c7d93143b749e7d315df7a81732a1ff43608497'

# Where rekordbox keeps its database, per platform. First hit wins.
LIBRARY_CANDIDATES = [
    '~/Library/Pioneer/rekordbox/master.db',            # macOS
    '~/Library/Pioneer/rekordbox6/master.db',
    '~/AppData/Roaming/Pioneer/rekordbox/master.db',    # Windows
    '~/AppData/Roaming/Pioneer/rekordbox6/master.db',
]

DEFAULTS = {
    # Empty means "detect it". A path here is used as-is.
    'library_db': '',
    'library_key': '',

    # Folders the Convert tab starts from.
    'source_folder': '',
    'output_folder': '',
    'move_processed': True,

    # ── conventions, which are personal by nature ──────────────────────────
    # rekordbox colour label that means "this track is ready to play". Empty
    # means every track counts, which is the right default for a new user:
    # filtering by a colour nobody has set would show an empty library.
    'ready_color': '',
    # The colour stamped when you finish tagging a track. It is NOT always the
    # same as "ready": tagging can leave one job outstanding that you do in
    # rekordbox (the beatgrid), so finishing here means "tagged, now beatgrid
    # it" and only rekordbox marks it ready. Empty = use ready_color.
    'done_color': '',
    # Colour stamped on tracks imported into rekordbox from here: they have not
    # been looked at yet, so they go where your first queue is. NEW is the
    # starter vocabulary's name for it (vocabulary.py).
    'import_color': 'NEW',
    # Many DJs start the comment with the energy level ("6 - dark roller").
    # When off, the comment is left alone and no energy column appears.
    'energy_in_comment': True,
    # Genre spellings to repair, e.g. {"reggeaton": "Reggaeton"}. Yours, not
    # ours: guessing typos algorithmically turned "Folk" into "Rock".
    'genre_fixes': {},

    # ── your MyTag vocabulary ──────────────────────────────────────────────
    # rekordbox lets you name the MyTag groups whatever you like, and the set
    # builder leans on two of them: one is the DIRECTION you can move in, the
    # other is the ARC of a set. Empty means "work it out from my library".
    # ⚠ Without this, a library whose groups are called anything other than
    # MOOD and TIMING gets a set builder with no directions and no arc — it
    # looks broken rather than unconfigured.
    'mood_bank': '',
    'timing_bank': '',
    'type_bank': '',
    # Groups that are not a set context at all (housekeeping tags).
    'ignored_banks': ['MISC'],
    # Your own order for the tags inside each group, set by dragging them in
    # the Inbox: {"MOOD": ["Dark", "Acid", …], …}. Tags not listed (new ones)
    # keep rekordbox's order after the ones you placed.
    'tag_order': {},
    # How much disk the library backups may take, in MB (0 = no limit: only
    # the age tiers in library_backup.py decide). The oldest copies go first,
    # and the two newest always stay.
    'backup_budget_mb': 2048,
    # Look for a newer Backspins on GitHub now and then (updates.py). Only the
    # public release number is read; nothing about you is sent.
    'check_updates': True,
}


def guess_banks(banks, manual=True):
    """Which MyTag group is the mood axis, which is the set arc, which is the kind of night.

    `banks` is what `rekordbox.load_tracks` produces: [{name, tags:[…]}, …].
    Anything you set by hand wins; this only fills the blanks.

    The guess is by CONTENT, not by name: the arc group is the one holding
    words like "warmup" or "peak", and the mood group is simply the biggest
    one left. A friend calling them "Vibe" and "Moment" gets a working set
    builder without touching a config file.
    """
    cur = load() if manual else dict(DEFAULTS, ignored_banks=load().get('ignored_banks') or [])
    mood = (cur.get('mood_bank') or '').strip()
    timing = (cur.get('timing_bank') or '').strip()
    kind = (cur.get('type_bank') or '').strip()
    ignored = {b.lower() for b in (cur.get('ignored_banks') or [])}

    # The arc group is the one whose tags read as stages of a night — the same
    # words the set builder orders them by, so the two cannot disagree.
    from setbuilder import arc_stage_of
    usable = [b for b in (banks or []) if (b.get('name') or '').lower() not in ignored]

    if not timing:
        best, hits = '', 0
        for b in usable:
            k = sum(1 for t in (b.get('tags') or []) if arc_stage_of(t['name']) is not None)
            if k > hits:
                best, hits = b['name'], k
        timing = best if hits >= 2 else ''

    if not mood:
        rest = [b for b in usable if b['name'] != timing]
        rest.sort(key=lambda b: -len(b.get('tags') or []))
        mood = rest[0]['name'] if rest else ''

    # The kind of night — "Rave", "Sunset", "After". Guessed the same way as
    # the rest: by content first (these words), and otherwise the biggest
    # group that is neither the arc nor the mood axis.
    if not kind:
        KIND_WORDS = ('rave', 'after', 'sunset', 'sunrise', 'party',
                      'family', 'club', 'festival', 'lounge', 'wedding')
        best, hits = '', 0
        for b in usable:
            if b['name'] in (timing, mood):
                continue
            names = ' '.join(t['name'].lower() for t in (b.get('tags') or []))
            k = sum(1 for w in KIND_WORDS if w in names)
            if k > hits:
                best, hits = b['name'], k
        if hits >= 2:
            kind = best
        else:
            rest = [b for b in usable if b['name'] not in (timing, mood)]
            rest.sort(key=lambda b: -len(b.get('tags') or []))
            kind = rest[0]['name'] if rest else ''

    return {'mood': mood, 'timing': timing, 'type': kind}


def _read():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def load():
    """Settings with every missing key filled in from the defaults."""
    out = dict(DEFAULTS)
    stored = _read()
    for k in DEFAULTS:
        if k in stored:
            out[k] = stored[k]
    return out


def save(values):
    """Write the given keys, keeping the rest. Returns the full settings."""
    current = _read()
    for k, v in (values or {}).items():
        if k in DEFAULTS:
            current[k] = v
    os.makedirs(APP_DIR, mode=0o700, exist_ok=True)
    tmp = SETTINGS_FILE + '.part'
    # ⚠ Yours alone (0600): it can hold the key this Mac uses for the cloud,
    # and other accounts on the same computer have no business reading it.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)          # never a half-written file
    return load()


def tighten():
    """The app folder and its settings readable by you alone — for folders
    made before this was the rule. Best-effort."""
    for path, mode in ((APP_DIR, 0o700), (SETTINGS_FILE, 0o600)):
        try:
            if os.path.exists(path) and os.name != 'nt':
                os.chmod(path, mode)
        except OSError:
            pass


def find_library():
    """The rekordbox database: yours if you set one, else the first we find."""
    chosen = (load().get('library_db') or '').strip()
    if chosen:
        return os.path.expanduser(chosen)
    for c in LIBRARY_CANDIDATES:
        p = os.path.expanduser(c)
        if os.path.exists(p):
            return p
    return os.path.expanduser(LIBRARY_CANDIDATES[0])


def library_key():
    return (load().get('library_key') or '').strip() or DEFAULT_KEY


_KEY_TRIED = False


def recover_key():
    """If the published key fails, ask pyrekordbox to read the real one.

    ⚠ Newer rekordbox builds rotate the SQLCipher key, so a friend on a recent
    version is locked out of every library tab with the hard-coded DEFAULT_KEY.
    pyrekordbox can extract the key from the app itself; we cache the result in
    settings so it is a one-time cost. Returns the key, or '' if it could not.
    """
    global _KEY_TRIED
    stored = (load().get('library_key') or '').strip()
    if stored:
        return stored
    if _KEY_TRIED:
        return ''
    _KEY_TRIED = True
    try:
        from pyrekordbox.config import get_config
        key = get_config('rekordbox6', 'dp')     # the decrypted DB password
        if key:
            save({'library_key': key})
            return key
    except Exception:
        pass
    return 


def environment():
    """What the tool needs, and whether this machine has it.

    Shown on first run instead of letting a missing ffmpeg surface later as a
    conversion that mysteriously fails on every single file.
    """
    db = find_library()
    ff = shutil.which('ffmpeg')
    fp = shutil.which('ffprobe')
    return {
        'library': {
            'path': db,
            'found': os.path.exists(db),
            'configured': bool((load().get('library_db') or '').strip()),
            'why': 'Every library feature reads this file (a copy of it, '
                   'never the original).',
        },
        'ffmpeg': {
            'path': ff or '',
            'found': bool(ff),
            'why': 'Converts audio and verifies each conversion sample for '
                   'sample. Without it, only the library views work.',
            'install': 'brew install ffmpeg',
        },
        'ffprobe': {
            'path': fp or '',
            'found': bool(fp),
            'why': 'Reads durations and formats.',
            'install': 'brew install ffmpeg',
        },
        'ready': os.path.exists(db) and bool(ff) and bool(fp),
    }
