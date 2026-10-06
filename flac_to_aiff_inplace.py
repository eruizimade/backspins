#!/usr/bin/env python3
"""Convert the FLACs already in rekordbox to AIFF, WITHOUT losing anything.

The trick: no new track is ever created. The file is converted and the SAME
`djmdContent` row is **repointed**, so MyTags, playlists, cues, play count,
date added, rating, colour and history — which hang off the ContentID and not
off the file — stay attached on their own.

Order per track (if a step fails, the FLAC is left untouched and you are told):
  1. Convert with ffmpeg at the SAME bit depth (16 or 24).
  2. Verify the PCM is IDENTICAL sample for sample (MD5 of the decoded audio).
  3. Copy tags and artwork onto the new AIFF.
  4. Put it in place and move the FLAC aside.
  5. Update the rekordbox row (path, size, type, bitrate, depth, rate).

Usage:
    python3 flac_to_aiff_inplace.py                 # dry run, touches nothing
    python3 flac_to_aiff_inplace.py --limit 3 --apply
    python3 flac_to_aiff_inplace.py --apply         # all of them

Requirements: rekordbox CLOSED. `master.db` is backed up before the first
write. FLACs are never deleted unless you ask: they are moved to another folder.
"""

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rekordbox as rb
import rekordbox_merge as rm
import settings

ORIGINALS_DIR = '_flac-replaced'
# ⚠ Earlier versions of this tool named that folder in Catalan. A library
# converted with one of those has tens of gigabytes sitting in it, so lookups
# accept the old names too and only NEW folders use the name above. Renaming
# without this would leave those originals invisible to the repair tool.
LEGACY_ORIGINALS_DIRS = ('_flac-substituits',)

AIFF_FILETYPE = 12          # how rekordbox marks a .aiff


def _suffixed(name, k):
    stem, ext = os.path.splitext(name)
    return '%s (%d)%s' % (stem, k, ext)


def park_original(src, keep_dir):
    """Move an original into keep_dir WITHOUT ever landing on an existing file.

    ⚠ shutil.move falls through to os.rename, which on POSIX REPLACES the
    destination silently. Two originals with the same basename would leave the
    second parking on top of the first — a permanent loss, no recycle bin. The
    sibling move_to_processed already avoids this with a suffix loop; this is
    the same guard, so every place that parks an original is collision-safe.
    """
    os.makedirs(keep_dir, exist_ok=True)
    name = os.path.basename(src)
    k = 1
    while True:
        cand = name if k == 1 else _suffixed(name, k)
        target = os.path.join(keep_dir, cand)
        if not os.path.exists(target):
            break
        k += 1
    shutil.move(src, target)
    return target


def originals_path(audio_path, name):
    """Where this track's original sits, checking legacy folder names too."""
    d = os.path.dirname(audio_path)
    for folder in (ORIGINALS_DIR,) + LEGACY_ORIGINALS_DIRS:
        p = os.path.join(d, folder, name)
        if os.path.exists(p):
            return p
    return os.path.join(d, ORIGINALS_DIR, name)      # where it would go


def pcm_md5(path):
    """MD5 of the DECODED audio. If two files share it, they are identical."""
    r = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0',
         '-c:a', 'pcm_s32le', '-f', 'md5', '-'],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or 'ffmpeg failed').strip()[:200])
    return r.stdout.strip().split('=')[-1]


def convert(src, dst, bits):
    codec = {16: 'pcm_s16be', 24: 'pcm_s24be', 32: 'pcm_s32be'}.get(bits, 'pcm_s16be')
    r = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
         '-map', '0:a:0', '-map_metadata', '-1', '-c:a', codec, '-f', 'aiff', dst],
        capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or 'error').strip()[:300])


def copy_tags(aiff_path, flac_path):
    """Tags and artwork, from the FLAC to the AIFF (as ID3)."""
    import convertidor
    convertidor.write_aiff_tags(aiff_path, flac_path)


def plan():
    """Which FLACs can be converted and which cannot, and why."""
    from mutagen.flac import FLAC

    tracks, _ = rb.load_tracks()
    existing = set()
    for t in tracks:
        if os.path.exists(t['path']):
            existing.add(os.path.normpath(t['path']).lower())

    todo, skip = [], []
    for t in tracks:
        if t['ext'] != '.flac':
            continue
        if not os.path.exists(t['path']):
            skip.append((t, 'the file is gone'))
            continue
        dst = os.path.splitext(t['path'])[0] + '.aiff'
        if os.path.normpath(dst).lower() in existing or os.path.exists(dst):
            # These are the duplicates: resolve them by hand, overwrite nothing.
            skip.append((t, 'an AIFF with that name already exists'))
            continue
        try:
            info = FLAC(t['path']).info
        except Exception as e:
            skip.append((t, 'cannot read it: %s' % e))
            continue
        todo.append({'t': t, 'dst': dst, 'bits': info.bits_per_sample,
                     'rate': info.sample_rate, 'ch': info.channels,
                     'size': os.path.getsize(t['path'])})
    return todo, skip


def space_check(todo, delete_originals):
    """Do not run out of disk halfway. Returns a warning, or None.

    ⚠ Keeping the FLACs needs the WHOLE AIFF (~1.5× the FLAC), not the
    difference between them. Getting this wrong is how a conversion dies at
    track 800 of 1,200.
    """
    if not todo:
        return None
    need = sum(it['size'] for it in todo) * (1.55 if not delete_originals else 0.55)
    free = shutil.disk_usage(os.path.dirname(todo[0]['t']['path'])).free
    if need > free * 0.95:
        return ('Not enough space: about %.1f GB is needed and %.1f GB is free. '
                'Tick "delete each verified FLAC" or free some space.'
                % (need / 1e9, free / 1e9))
    return None


def _fsync_path(path):
    """Flush a file and its directory to disk. Best-effort."""
    for target, flags in ((path, os.O_RDONLY),
                          (os.path.dirname(path) or '.', os.O_RDONLY)):
        try:
            fd = os.open(target, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def convert_batch(todo, delete_originals=False, log=None, should_stop=None):
    """The engine. `log(level, text)` reports; `should_stop()` interrupts.

    The command line and the app window both run this same code.
    """
    def say(level, text):
        if log:
            log(level, text)
        else:
            print(text)

    from pyrekordbox import Rekordbox6Database
    backup = rm.backup_db()
    say('info', 'Database backup: %s' % os.path.basename(backup))

    db = Rekordbox6Database(path=settings.find_library(), key=settings.library_key())
    ok = fail = 0
    try:
        for i, it in enumerate(todo, 1):
            if should_stop and should_stop():
                say('info', 'Stopped at your request.')
                break
            # ⚠ rekordbox may have been opened mid-run (this loops for hours).
            # Writing while it is open desyncs USNs; abort cleanly. The current
            # track's FLAC is untouched (database-first ordering), so it is safe.
            if rm.rekordbox_running():
                say('error', 'rekordbox was opened — stopping to protect the '
                    'library. Close it and run again to finish the rest.')
                break
            t, dst = it['t'], it['dst']
            name = os.path.basename(t['path'])
            tmp = dst + '.part'
            try:
                convert(t['path'], tmp, it['bits'])

                # ── the check that justifies the whole exercise ──
                if pcm_md5(t['path']) != pcm_md5(tmp):
                    raise RuntimeError('the audio did NOT come out identical')

                copy_tags(tmp, t['path'])
                os.replace(tmp, dst)

                # ⚠ THE ORDER MATTERS: database first, and the original is only
                # moved aside once it saved cleanly. The other way round (as it
                # was at first) an error here left rekordbox pointing at a FLAC
                # that had already moved — a broken track. On failure the AIFF
                # is undone and the FLAC has not been touched: the track is left
                # exactly as it was.
                try:
                    content = db.get_content(ID=str(t['id']))
                    db.update_content_path(content, dst, save=True, commit=False)
                    content.FileSize = os.path.getsize(dst)
                    content.FileType = AIFF_FILETYPE
                    content.BitDepth = it['bits']
                    content.SampleRate = it['rate']
                    content.BitRate = int(it['rate'] * it['bits'] * it['ch'] / 1000)
                    content.FileNameS = os.path.basename(dst)
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    if os.path.exists(dst):
                        os.remove(dst)
                    raise

                # Now the FLAC is either moved aside, or deleted once VERIFIED
                # identical. Deleting it loses nothing recoverable: the AIFF is
                # the same audio bit for bit and a FLAC can be made from it again.
                if delete_originals:
                    # ⚠ Recoverable delete, and only after the AIFF is durably
                    # on disk: a sub-second power cut must not leave neither
                    # file. fsync the AIFF and its directory, then bin the FLAC.
                    _fsync_path(dst)
                    rm.trash_file(t['path'])
                else:
                    park_original(t['path'],
                                  os.path.join(os.path.dirname(t['path']), ORIGINALS_DIR))

                ok += 1
                say('ok', '%s   ·   %d-bit' % (name, it['bits']))
            except Exception as e:
                fail += 1
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                say('error', '%s   ·   %s' % (name, str(e)[:140]))
    finally:
        try:
            db.close()
        except Exception:
            pass

    say('info', 'Converted: %d   |   failed: %d' % (ok, fail))
    if delete_originals:
        say('info', 'The verified FLACs were deleted: the AIFF is the same audio '
                    'bit for bit and a FLAC can be remade from it any time.')
    else:
        say('info', 'The original FLACs are in "%s". Nothing was deleted.' % ORIGINALS_DIR)
    return {'ok': ok, 'fail': fail, 'backup': backup}


def run(limit=None, apply=False, delete_originals=False):
    if apply and rm.rekordbox_running():
        sys.exit('Close rekordbox before converting.')

    todo, skip = plan()
    if limit:
        todo = todo[:limit]

    print('FLACs to convert: %d   |   skipped: %d' % (len(todo), len(skip)))
    for t, why in skip[:10]:
        print('   SKIPPED  %-52s %s' % (os.path.basename(t['path'])[:52], why))
    bits = {}
    for it in todo:
        bits[it['bits']] = bits.get(it['bits'], 0) + 1
    print('bit depths:', bits)
    if not apply:
        print('\n(dry run: nothing was touched. Add --apply to do it)')
        return

    problem = space_check(todo, delete_originals)
    if problem:
        sys.exit(problem)

    convert_batch(todo, delete_originals,
                  log=lambda lvl, txt: print('  [%s] %s' % (lvl.upper(), txt)))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--delete-originals', action='store_true',
                    help='delete each FLAC once verified identical (saves ~45 GB)')
    a = ap.parse_args()
    run(limit=a.limit, apply=a.apply, delete_originals=a.delete_originals)
