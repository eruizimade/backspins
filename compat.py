"""Which Pioneer players your library actually works on.

Every figure comes from looking at each track: format, bit depth and sample
rate. Nothing is estimated.

⚠ On confidence: that the newer players read FLAC and the older ones do not is
well established. What is NOT certain is whether the entry-level decks
(CDJ-350/850) accept 24-bit files, so that tier is reported as a RANGE and
flagged as unconfirmed rather than given a precise-looking number that might
be wrong.
"""

import os

# Per tier: which formats it reads, and up to what depth.
DEVICES = [
    {
        'id': 'nxs2',
        'name': 'CDJ-3000 · CDJ-2000NXS2 · XDJ-XZ · XDJ-RX2/RX3 · XDJ-1000MK2',
        'short': 'Current generation',
        'formats': {'.mp3', '.m4a', '.mp4', '.wav', '.aiff', '.aif', '.flac'},
        'alac': True,
        'max_bits': 24,
        'max_rate': 48000,
        'sure': True,
    },
    {
        'id': 'nxs',
        'name': 'CDJ-2000NXS · CDJ-2000 · CDJ-900(NXS) · XDJ-1000 · XDJ-700',
        'short': 'NXS generation',
        'formats': {'.mp3', '.m4a', '.mp4', '.wav', '.aiff', '.aif'},
        'alac': False,
        'max_bits': 24,
        'max_rate': 48000,
        'sure': True,
    },
    {
        'id': 'entry',
        'name': 'CDJ-850 · CDJ-350 · XDJ-RX (1st generation)',
        'short': 'Entry-level and older',
        'formats': {'.mp3', '.m4a', '.mp4', '.wav', '.aiff', '.aif'},
        'alac': False,
        'max_bits': 24,
        'max_rate': 48000,
        'sure': False,          # the 24-bit question, stated honestly
        'doubt_bits': 16,
    },
]


def widely_readable(ext, alac=False):
    """Readable even by the oldest player in the table.

    Used to break ties between duplicates: a FLAC and an AIFF at 44.1 kHz /
    16-bit sound EXACTLY the same, but only the newest gear reads the FLAC.
    At equal sound quality, then, the format is not a matter of taste.
    """
    dev = DEVICES[-1]
    if ext not in dev['formats']:
        return False
    if ext in ('.m4a', '.mp4') and alac and not dev['alac']:
        return False
    return True


def is_alac(path):
    """An .m4a may be AAC (everything reads it) or ALAC (only the newest)."""
    try:
        import mutagen
        info = getattr(mutagen.File(path), 'info', None)
        return str(getattr(info, 'codec', '') or '').startswith('alac')
    except Exception:
        return False


def why_not(track, dev, bits_limit=None):
    """Why this player would NOT read this track. None if it would."""
    ext = track['ext']
    if ext not in dev['formats']:
        return '%s format' % ext.lstrip('.').upper()
    if ext in ('.m4a', '.mp4') and track.get('alac') and not dev['alac']:
        return 'ALAC'
    bits = track.get('bits') or 0
    limit = bits_limit or dev['max_bits']
    if bits and bits > limit:
        return '%d-bit' % bits
    if (track.get('rate') or 0) > dev['max_rate']:
        return '%d kHz' % round((track['rate'] or 0) / 1000)
    return None


def analyse(tracks, min_seconds=60):
    """The whole compatibility picture."""
    songs = []
    for t in tracks:
        if (t['rb_length'] or 0) < min_seconds:
            continue
        songs.append({
            'id': t['id'], 'title': t['title'], 'artist': t['artist'],
            'path': t['path'], 'ext': t['ext'],
            'bits': t['rb_depth'], 'rate': t['rb_rate'],
            'alac': is_alac(t['path']) if t['ext'] in ('.m4a', '.mp4') else False,
        })
    total = len(songs) or 1

    formats = {}
    for s in songs:
        formats[s['ext']] = formats.get(s['ext'], 0) + 1

    levels = []
    for dev in DEVICES:
        bad = []
        for s in songs:
            r = why_not(s, dev)
            if r:
                bad.append({'title': s['title'], 'artist': s['artist'],
                            'path': s['path'], 'why': r,
                            'name': os.path.basename(s['path'])})
        entry = {
            'id': dev['id'], 'name': dev['name'], 'short': dev['short'],
            'sure': dev['sure'],
            'ok': total - len(bad),
            'pct': round((total - len(bad)) / total * 100, 1),
            'bad': len(bad),
            'reasons': _count(bad),
        }
        if not dev['sure']:
            # The pessimistic case: if that gear did not read 24-bit either.
            worse = [s for s in songs if why_not(s, dev, dev['doubt_bits'])]
            entry['pct_low'] = round((total - len(worse)) / total * 100, 1)
            entry['doubt'] = ('It is not confirmed that these players accept '
                              '24-bit files. If they do not, the figure drops '
                              'to the bottom of the range.')
        levels.append(entry)

    # The problems, grouped: what would need fixing and how many tracks.
    issues = {}
    for s in songs:
        r = why_not(s, DEVICES[0])       # what even the best player refuses
        key = 'FLAC' if s['ext'] == '.flac' else r
        if not key:
            continue
        issues.setdefault(key, []).append({
            'name': os.path.basename(s['path']), 'path': s['path'],
            'title': s['title'], 'artist': s['artist']})

    return {'total': total, 'formats': formats, 'levels': levels,
            'issues': sorted(({'why': k, 'count': len(v), 'files': v[:60]}
                              for k, v in issues.items()),
                             key=lambda x: -x['count'])}


def _count(bad):
    c = {}
    for b in bad:
        c[b['why']] = c.get(b['why'], 0) + 1
    return sorted(({'why': k, 'count': v} for k, v in c.items()),
                  key=lambda x: -x['count'])
