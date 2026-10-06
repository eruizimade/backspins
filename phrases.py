"""Where a track really starts, according to rekordbox's phrase analysis.

rekordbox already knows where the intro ends, where the body of the track
comes in and where the outro begins. That is used here for one concrete thing:
when you hit play on a track, it starts **where the music comes in**, not at
the top of the intro.

Two analysis files are involved:
  · `ANLZ0000.EXT` → PSSI section: the phrases, each with its BEAT number.
  · `ANLZ0000.DAT` → PQTZ section: the beat grid, which gives each beat a time.

The phrase's beat number indexes the grid (beat 32 = 32nd entry), and that is
where the milliseconds come from.
"""

import logging
import os
import threading

import settings

# rekordbox has three phrase models. In "high" (the dance-music one) there is
# no "verse" as such: the body of the track is the Up/Down/Chorus phrases. In
# the mid and low models, verses are types 2 and up. Either way the useful
# rule is the same: the first phrase that is not the intro.
INTRO_KINDS = {1}
OUTRO_KINDS_BY_MOOD = {1: {6}, 2: {10}, 3: {10}}

_CACHE = {}
_LOCK = threading.Lock()


def anlz_dir():
    """The analysis folder, derived from wherever the library database is.

    Not hard-coded: someone whose rekordbox lives elsewhere (or on an external
    drive) still gets working phrase data, because the `share` folder always
    sits next to `master.db`.
    """
    return os.path.join(os.path.dirname(settings.find_library()), 'share')


def resolve(analysis_path):
    """From "/PIONEER/USBANLZ/…/ANLZ0000.DAT" to the real path on disk."""
    if not analysis_path:
        return None
    p = os.path.join(anlz_dir(), analysis_path.lstrip('/'))
    return p if os.path.exists(p) else None


def _beat_times(dat_path):
    """Time (ms) of every beat, by index."""
    from pyrekordbox.anlz import AnlzFile
    f = AnlzFile.parse_file(dat_path)
    for tag in f.tags:
        if 'PQTZ' in str(tag.type):
            return [e['time'] for e in (tag.content.get('entries') or [])]
    return []


def _phrases(ext_path):
    from pyrekordbox.anlz import AnlzFile
    f = AnlzFile.parse_file(ext_path)
    for tag in f.tags:
        if 'PSSI' in str(tag.type):
            c = tag.content
            return c.get('mood'), (c.get('entries') or [])
    return None, []


def body_start_seconds(analysis_path):
    """Second at which the body comes in. None if there is no phrase analysis."""
    if not analysis_path:
        return None
    with _LOCK:
        if analysis_path in _CACHE:
            return _CACHE[analysis_path]

    value = None
    try:
        logging.disable(logging.WARNING)
        dat = resolve(analysis_path)
        ext = resolve(os.path.splitext(analysis_path)[0] + '.EXT')
        if dat and ext:
            mood, entries = _phrases(ext)
            if entries:
                outro = OUTRO_KINDS_BY_MOOD.get(mood, {6, 10})
                times = _beat_times(dat)
                for e in entries:
                    kind, beat = e.get('kind'), e.get('beat') or 0
                    if kind in INTRO_KINDS or kind in outro:
                        continue
                    if 0 < beat <= len(times):
                        value = round(times[beat - 1] / 1000.0, 2)
                    break
    except Exception:
        value = None
    finally:
        logging.disable(logging.NOTSET)

    with _LOCK:
        _CACHE[analysis_path] = value
    return value
