"""The three-band waveform rekordbox has already worked out for you.

We do not compute it: rekordbox analyses every track on import and stores the
result next to it. Reading it back is instant and — more to the point — draws
exactly what you see on a CDJ, which is what makes a track recognisable at a
glance.

Where each version lives:

    .2EX   PWV6  three-band overview, 1,200 columns   (the one we use)
           PWV7  three-band detail, 150 columns/second
    .EXT   PWV4  colour overview (rekordbox 6), 7,200 columns
           PWV3  monochrome detail
    .DAT   PWAV  monochrome overview, 400 columns

Layout of the three-band sections: after the header come **3 bytes per
column**, one per band — low, mid, high. ⚠ `PWV6` has a 20-byte header and
`PWV7` a 24-byte one, so it must be read from the field and never assumed.
"""

import os
import struct

BANDS = ('low', 'mid', 'high')


def sections(data):
    """{name: (offset, header size, total size)} for the whole analysis file."""
    out = {}
    try:
        i = struct.unpack('>I', data[4:8])[0]
    except Exception:
        return out
    while i + 12 <= len(data):
        name = data[i:i + 4]
        try:
            hlen, tlen = struct.unpack('>II', data[i + 4:i + 12])
        except Exception:
            break
        if tlen <= 0 or i + tlen > len(data):
            break
        out[name.decode('ascii', 'replace')] = (i, hlen, tlen)
        i += tlen
    return out


def read_3band(anlz_dat_path):
    """The three bands of a track, or None if it was never analysed for CDJ-3000.

    Takes the path of the `.DAT`; the three-band data lives in the `.2EX`
    beside it.
    """
    p = os.path.splitext(anlz_dat_path)[0] + '.2EX'
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'rb') as fh:
            data = fh.read()
    except OSError:
        return None

    sec = sections(data)
    if 'PWV6' not in sec:
        return None
    off, hlen, tlen = sec['PWV6']
    # ⚠ Bytes-per-column and column count are DECLARED in the header; we do
    # not assume them (PWV7's header is 24 bytes and PWV6's is 20).
    try:
        per_col, cols = struct.unpack('>II', data[off + 12:off + 20])
    except Exception:
        return None
    if per_col != 3 or cols <= 0:
        return None
    body = data[off + hlen:off + tlen]
    cols = min(cols, len(body) // 3)
    if cols <= 0:
        return None

    low, mid, high = [], [], []
    for k in range(cols):
        b = body[k * 3:k * 3 + 3]
        low.append(b[0]); mid.append(b[1]); high.append(b[2])
    return {'low': low, 'mid': mid, 'high': high, 'cols': cols}


def resample(bands, n):
    """The three bands at `n` columns, taking the PEAK of each slice.

    The peak and not the average: flattening would erase exactly the hits that
    let you recognise a track's structure by looking at it.
    """
    if not bands or n <= 0:
        return None
    src = bands['cols']
    out = {}
    for b in BANDS:
        v = bands[b]
        col = []
        for i in range(n):
            a = i * src // n
            z = max((i + 1) * src // n, a + 1)
            col.append(max(v[a:z]) if z <= src else 0)
        out[b] = col
    return out
