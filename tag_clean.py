#!/usr/bin/env python3
"""Find, and take out, what the shops wrote into your files.

Every pool stamps its downloads, and no two do it the same way — five show up
in this library alone (musicmasterdjpool, whoplaymusic, ftpdjemilio,
electronicfresh, djsoundtop) writing into album, publisher, comment, the URL
frames, the grouping, and a per-account id.

⚠⚠ This edits TAGS, never the audio, and that distinction is the whole
design. The first version of this module rebuilt each file with ffmpeg —
strip all metadata, write back the good bits — and validation killed it: on
thirteen test files it lost `artist`, `genre`, `tkey`, `energylevel` and Mixed
In Key's cue points (ffmpeg cannot write every tag name into every container,
so anything it could not map simply vanished), and on three MP3s `-c:a copy`
did not even come out bit-identical. mutagen rewrites only the tag block; the
audio frames are never touched, so "did the audio survive" stops being a
question that needs asking.

⚠ Findings come in three KINDS and only the first is cleaned without asking:

  · **brand** — a URL, a shop's name in a field that should describe the
    music, an account id. No argument for keeping these.
  · **cover** — the shop's logo, and ONLY on a file whose tags prove it is
    theirs. Plenty of downloads carry real art from elsewhere; treating "has
    a cover" as evidence destroyed a 1080×1080 sleeve the first time.
  · **album** — "Beatport Top 100 Downloads December 2025". Not the album,
    but it does say where the record came from, and that is the owner's call.

⚠ What is KEPT is as deliberate as what goes: Mixed In Key's cues, energy and
key, Serato's GEOB markers, BPM, ISRC. None of that belongs to a shop.
"""

import os
import re

AUDIO_EXTS = ('.mp3', '.flac', '.wav', '.aif', '.aiff', '.m4a', '.aac', '.ogg')

_URL = re.compile(r'\b(?:https?://|www\.)|\b[a-z0-9-]+\.(?:com|net|org|io|co)\b', re.I)

_SHOP = re.compile(
    r'music\s*master|musicmaster|whoplay|ftpdjemilio|electronicfresh|'
    r'djsoundtop|beatport\s+top\s+\d+|dj\s*pool|djpool|promo\s*only|'
    r'bpm\s*supreme|digital\s*dj\s*pool|club\s*killers|crate\s*connect|'
    r'samplefocus|ytbmp3|downloaded\s+from',
    re.I)

#: ID3 frames that exist only to carry a shop's address. Always junk when set.
_URL_FRAMES = ('WOAF', 'WOAS', 'WORS', 'WPUB', 'WCOM', 'WCOP', 'WOAR')

#: Frames whose CONTENT decides: a shop's name here is wrong, a real value is
#: not. ⚠ TIT1 is the grouping — shops love it, but so do people.
_MAYBE = ('TENC', 'TPUB', 'TIT1', 'TCOP', 'TSSE', 'TCOM', 'TOWN', 'TSRC')

#: Never removed, whatever the rule says. The analysis other software wrote
#: is not branding, and this is the list that makes the tool safe to run on a
#: curated library.
_SACRED = ('TBPM', 'TKEY', 'TIT2', 'TPE1', 'TPE2', 'TCON', 'TDRC', 'TYER',
           'TRCK', 'TPOS', 'APIC')


def _is_junk_text(v):
    v = str(v or '')
    return bool(_URL.search(v) or _SHOP.search(v))


def inspect(path):
    """What this one file carries that does not belong to the music."""
    import mutagen
    out = {'path': path, 'brand': [], 'album': [], 'cover': False,
           'any': False, 'kind': ''}
    try:
        m = mutagen.File(path)
    except Exception:
        return out
    if m is None or not getattr(m, 'tags', None):
        return out
    out['kind'] = type(m).__name__
    tags = m.tags

    # ── MP4 keeps its own vocabulary
    if out['kind'] == 'MP4':
        for k in list(tags.keys()):
            ks = str(k)
            if ks == 'covr':
                continue                       # decided below
            if ks in ('\xa9cmt', 'cprt') and _is_junk_text(tags[k]):
                out['brand'].append(ks)
            elif ks == '\xa9alb' and _is_junk_text(tags[k]):
                out['album'].append(ks)
            elif ks.startswith('----') and _is_junk_text(tags[k]):
                # ⚠ but never Mixed In Key's or Serato's freeform atoms.
                if 'mixedinkey' not in ks.lower() and 'serato' not in ks.lower():
                    out['brand'].append(ks)
        if 'covr' in tags and out['brand']:
            out['cover'] = True
        out['any'] = bool(out['brand'] or out['album'] or out['cover'])
        return out

    # ── everything else speaks ID3 or Vorbis
    for k in list(tags.keys()):
        ks = str(k)
        base = ks.split(':')[0].upper()
        if base in _SACRED and base != 'APIC':
            continue
        try:
            val = str(tags[k])
        except Exception:
            val = ''

        if base in _URL_FRAMES or ks.upper().startswith('WXXX'):
            if val.strip():
                out['brand'].append(ks)
        elif base == 'TALB' or ks.lower() == 'album':
            if _is_junk_text(val):
                out['album'].append(ks)
        elif base == 'COMM' or ks.lower() == 'comment':
            if _is_junk_text(val):
                out['brand'].append(ks)
        elif base in _MAYBE and _is_junk_text(val):
            out['brand'].append(ks)
        elif ks.upper().startswith('TXXX'):
            name = ks.split(':', 1)[-1].lower()
            if 'serato' in name or 'energy' in name or 'key' in name:
                continue                       # analysis, not branding
            if _is_junk_text(val) or 'pool-user' in name or name.endswith('-user'):
                out['brand'].append(ks)
        elif ks.lower() in ('www', 'publisher', 'mmpool-user') and val.strip():
            out['brand'].append(ks)
        elif ks.lower() in ('comment', 'encoded_by', 'copyright') and _is_junk_text(val):
            out['brand'].append(ks)

    if out['brand']:
        for k in list(tags.keys()):
            if str(k).upper().startswith('APIC'):
                out['cover'] = True
                break
    out['any'] = bool(out['brand'] or out['album'] or out['cover'])
    return out


def scan(paths, workers=8, on_progress=None):
    """Look at a whole library. Returns only the files with something in them."""
    from concurrent.futures import ThreadPoolExecutor
    found, done = [], [0]

    def one(p):
        try:
            r = inspect(p)
        except Exception:
            r = None
        done[0] += 1
        if on_progress and done[0] % 100 == 0:
            on_progress(done[0], len(paths))
        return r if (r and r['any']) else None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, paths):
            if r:
                found.append(r)
    return found


def clean(path, drop_album=False, drop_cover=True):
    """Take it out, in place. The audio is never rewritten."""
    import mutagen
    f = inspect(path)
    drops = list(f['brand']) + (list(f['album']) if drop_album else [])
    want_cover = f['cover'] and drop_cover
    if not drops and not want_cover:
        return {'path': path, 'changed': False, 'removed': [], 'cover': False}

    m = mutagen.File(path)
    if m is None or not getattr(m, 'tags', None):
        return {'path': path, 'changed': False, 'removed': [], 'cover': False}

    for k in drops:
        try:
            del m.tags[k]
        except Exception:
            pass
    if want_cover:
        if f['kind'] == 'MP4':
            try:
                del m.tags['covr']
            except Exception:
                pass
        else:
            for k in [x for x in list(m.tags.keys())
                      if str(x).upper().startswith('APIC')]:
                try:
                    del m.tags[k]
                except Exception:
                    pass
    m.save()
    return {'path': path, 'changed': True, 'removed': sorted(drops),
            'cover': want_cover}
