#!/usr/bin/env python3
"""Check a rekordbox USB before you take it to a gig.

A CDJ saying "no file" means one thing: the device database points at a file
that is not on the stick. It is not a problem with the music — the track can
be sitting right there under another name — and you find out about it in front
of people, with the record already on the deck.

This looks for exactly that: every audio path the device database references,
checked against what is really on the stick. It reads, and only reads.

⚠ Why the paths are extracted the way they are. `export.pdb` packs its strings
next to each other with nothing in between, so pulling text out of it runs
neighbouring records together and you get
"…Frontal Attack (Exte.aiffWork That Body (Extended .flac" — one path that
looks like two, and a "missing file" that was never missing. A path always
ends at an audio extension, so every candidate is cut at the FIRST one after
its /Contents/. Without that the report cries wolf on a third of the library,
which is worse than not having a report at all.

    python3 usb_check.py                # every rekordbox stick plugged in
    python3 usb_check.py /Volumes/NONAME
"""

import os
import re
import sys

#: What a CDJ will play. The extension is also where a stored path ends.
AUDIO_EXTS = ('.aiff', '.aif', '.flac', '.mp3', '.wav', '.m4a', '.alac', '.aac')

_END = re.compile('|'.join(re.escape(e) for e in AUDIO_EXTS), re.I)

#: Where rekordbox keeps the device database and the music.
_DB_DIR = os.path.join('PIONEER', 'rekordbox')
_PDB = 'export.pdb'


def find_devices(root='/Volumes'):
    """Mounted volumes that carry a rekordbox export."""
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        vol = os.path.join(root, name)
        if os.path.isfile(os.path.join(vol, _DB_DIR, _PDB)):
            out.append(vol)
    return out


def referenced_paths(pdb_path):
    """Every /Contents/... audio path the device database mentions.

    Returns them device-relative ("/Contents/Artist/Album/track.aiff"), unique
    and in a stable order.
    """
    with open(pdb_path, 'rb') as fh:
        blob = fh.read()
    text = blob.decode('utf-8', 'ignore')
    seen, out = set(), []
    for m in re.finditer(r'/Contents/', text):
        rest = text[m.start():m.start() + 512]
        end = _END.search(rest)
        if not end:
            continue
        path = rest[:end.end()]
        # A path with a control character in it is a misread, not a path.
        if any(ord(ch) < 32 for ch in path):
            continue
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def check(device):
    """Look at one stick. Returns a summary dict; changes nothing."""
    pdb = os.path.join(device, _DB_DIR, _PDB)
    refs = referenced_paths(pdb)

    missing, by_ext = [], {}
    for rel in refs:
        ext = os.path.splitext(rel)[1].lower()
        slot = by_ext.setdefault(ext, {'refs': 0, 'missing': 0})
        slot['refs'] += 1
        # The database stores POSIX-ish paths from the device root.
        full = os.path.join(device, rel.lstrip('/').replace('/', os.sep))
        if not os.path.isfile(full):
            slot['missing'] += 1
            missing.append(rel)

    # Music sitting on the stick that no playlist can reach. Not an error —
    # but it is what a stick looks like after tracks were replaced, and it is
    # the other half of the same story.
    on_disk = set()
    contents = os.path.join(device, 'Contents')
    for base, _dirs, files in os.walk(contents):
        for f in files:
            if f.startswith('._'):          # macOS metadata, not music
                continue
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                full = os.path.join(base, f)
                on_disk.add('/' + os.path.relpath(full, device).replace(os.sep, '/'))
    orphans = sorted(on_disk - set(refs))

    return {'device': device,
            'refs': len(refs),
            'missing': missing,
            'byExt': by_ext,
            'files': len(on_disk),
            'orphans': orphans,
            'ok': not missing}


def dates_on_device(device):
    """What span of "date added" the stick's own database carries.

    ⚠ A stick cannot learn that the collection changed: its database is a
    photograph taken at export time, and the dates are collection metadata,
    not file tags — nothing about them travels inside the audio. So a stick
    exported before the dates were recovered will keep showing the old ones
    for ever, and this is how you see that from outside.
    """
    pdb = os.path.join(device, _DB_DIR, _PDB)
    try:
        with open(pdb, 'rb') as fh:
            text = fh.read().decode('utf-8', 'ignore')
    except OSError:
        return None
    found = re.findall(r'20[12]\d-[01]\d-[0-3]\d', text)
    if not found:
        return {'count': 0, 'distinct': 0, 'oldest': None, 'newest': None}
    uniq = sorted(set(found))
    return {'count': len(found), 'distinct': len(uniq),
            'oldest': uniq[0], 'newest': uniq[-1]}


def report(res):
    dev = os.path.basename(res['device'].rstrip('/')) or res['device']
    print('▸ %s' % dev)
    print('   %d fitxers de música · %d referències a la base de dades'
          % (res['files'], res['refs']))
    for ext in sorted(res['byExt']):
        s = res['byExt'][ext]
        mark = '   ' if not s['missing'] else ' ✗ '
        print('  %s%-6s %5d referències   %4d sense fitxer'
              % (mark, ext, s['refs'], s['missing']))
    if res['missing']:
        print('\n   %d TEMES DIRAN «NO FILE»:' % len(res['missing']))
        for rel in res['missing'][:25]:
            print('      %s' % os.path.basename(rel))
        if len(res['missing']) > 25:
            print('      … i %d més' % (len(res['missing']) - 25))
    else:
        print('   cap referència trencada — el llapis està sa')
    if res['orphans']:
        print('\n   %d fitxers que cap playlist reclama (%.1f GB)'
              % (len(res['orphans']), _size(res['device'], res['orphans']) / 1e9))
    d = dates_on_device(res['device'])
    if d and d['distinct']:
        print('\n   dates a la base de dades del llapis: %s → %s (%d diferents)'
              % (d['oldest'], d['newest'], d['distinct']))
        if d['oldest'] >= '2025-01-01':
            print('      ⚠ cap data anterior al 2025: exportat ABANS de recuperar-les.')
            print('        Torna a exportar perquè hi arribin.')


def _size(device, rels):
    total = 0
    for rel in rels:
        try:
            total += os.path.getsize(os.path.join(device, rel.lstrip('/')))
        except OSError:
            pass
    return total


if __name__ == '__main__':
    devices = sys.argv[1:] or find_devices()
    if not devices:
        sys.exit('cap llapis de rekordbox connectat')
    for i, d in enumerate(devices):
        if i:
            print()
        report(check(d))
