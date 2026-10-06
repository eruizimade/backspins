"""Rewrite the file path inside rekordbox's analysis files, byte by byte.

Why this exists: `pyrekordbox.update_content_path()` parses the whole analysis
file, and a fair number of them (25 in the library this was built against)
contain sections its parser rejects — the read blows up and the track cannot
be repointed at all.

Here only the **PPTH** tag is touched. It holds the path in UTF-16BE:

    'PPTH' | header size (4B) | tag size (4B) | path size (4B) | path…

⚠ The edit is only accepted when the new path takes EXACTLY as many bytes as
the old one. Going from `.flac` to `.aiff` always does (same letter count), so
no size or offset anywhere else has to move and the rest of the file stays
byte-identical. If a length ever did change, the function refuses instead of
corrupting the file.
"""

import os
import struct


def read_ppth(data):
    """(offset of the path, its length, the text) of the PPTH tag, or None."""
    i = data.find(b'PPTH')
    if i < 0:
        return None
    try:
        slen = struct.unpack('>I', data[i + 12:i + 16])[0]
        raw = data[i + 16:i + 16 + slen]
        return i + 16, slen, raw.decode('utf-16-be').rstrip('\x00')
    except Exception:
        return None


def patch_file(path, old_ext, new_ext):
    """Swap ONLY the extension inside whatever path the file already stores.

    ⚠ The analysis file does NOT hold an absolute path but a short one,
    relative to the volume. Writing the full path in doubled its length
    (60 → 132 bytes) and the guard below refused the edit, as it should.
    Changing just the tail means the length never moves.
    """
    with open(path, 'rb') as fh:
        data = fh.read()
    found = read_ppth(data)
    if not found:
        return 'no PPTH tag'
    off, slen, old = found

    if not old.lower().endswith(old_ext.lower()):
        return 'does not end in %s (%s)' % (old_ext, old[-12:])
    new = old[:-len(old_ext)] + new_ext
    raw = (new + '\x00').encode('utf-16-be')
    if len(raw) != slen:
        return 'length would change (%d → %d): leaving it alone' % (slen, len(raw))

    out = data[:off] + raw + data[off + slen:]
    if len(out) != len(data):
        return 'file size would change: leaving it alone'
    tmp = path + '.part'
    with open(tmp, 'wb') as fh:
        fh.write(out)
    os.replace(tmp, path)
    return None


def patch_pair(dat_path, old_ext, new_ext):
    """Both analysis files (.DAT and .EXT) of the same track."""
    errors = []
    for p in (dat_path, os.path.splitext(dat_path)[0] + '.EXT'):
        if not os.path.exists(p):
            continue
        err = patch_file(p, old_ext, new_ext)
        if err:
            errors.append('%s: %s' % (os.path.basename(p), err))
    return errors
