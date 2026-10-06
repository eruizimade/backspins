"""A number for how hard a track goes — one that actually spreads out.

Mixed In Key gives every track an energy from 1 to 10, and in this library
1,209 of them are a 6 and 732 are a 7: eighty per cent of the collection in
two values. As a thing to sort by, that is no thing at all.

WHERE THE ANSWER COMES FROM, and it took a wrong turn to find out.

The first version scored the waveform alone: brightness, relentlessness and
tempo, weighted 40/40/20. Measured afterwards against 1,146 tracks the owner
had already labelled hard or soft with his own MyTags, the separations were:

    brightness   -0.03      the 40% went to a signal that separates NOTHING
    relentless    0.49
    tempo         0.80      the best of them got 20%

So it was close to backwards, and the tracks it called hardest did not sound
hard. The lesson is not to tune the weights: it is that the person who owns
the library has already said what everything is, forty-nine tags at a time,
and that knowledge beats anything inferable from a waveform overview.

So now:

  · every TAG earns a weight from the tracks that carry it — its average
    tempo-and-relentlessness, which is the one thing measurable that does
    separate hard from soft. Nobody hand-writes the list; the data ranks it,
    and it comes out in exactly the order a DJ would say out loud (Aggressive,
    Scary, Rave, Uptempo, Peak Hour at the top; Chill, Pop Party, Downtempo at
    the bottom).
  · a track scores by ITS tags, plus its own tempo and relentlessness. A slow
    track tagged Aggressive and Dark now outranks a fast one tagged Chill,
    which was the whole complaint.
  · the raw score becomes a DECILE across your library. Ten per cent in each
    band, by construction — it cannot collapse into the middle the way an
    absolute scale does, because it is not an absolute scale. It says "harder
    than 70% of what you own", which is the only question you ask it.

⚠ It reads the three-band waveform rekordbox has ALREADY computed (the .2EX
beside every analysed track). No audio is decoded: the whole library takes
about two seconds.

⚠ The measured half is deliberately independent of mastering LOUDNESS. Average
waveform height mostly tells you which decade a record was cut in.

⚠ Blind spot: a track that is relentless and fast scores high whether or not
it is music. A click track in this library lands near the top. Tools, loops
and drum jams will do the same, and no amount of tag weighting fixes an
untagged tool.
"""

import json
import os
import statistics

import settings

CACHE = os.path.join(settings.APP_DIR, 'drive.json')

#: The measurable anchor, weighted by how well each ACTUALLY separated the
#: owner's hard-tagged tracks from his soft-tagged ones (0.80 and 0.49).
#: Brightness is absent on purpose: it measured -0.03, which is nothing.
ANCHOR = {'bpm': 0.72, 'dens': 0.28}

#: How much the tags have the last word against the track's own measurements.
#: Tags are a person's judgement about the music; tempo is a fact about the
#: file. Both matter, and the judgement matters more.
TAG_SHARE = 0.55

#: A tag needs this many tracks before its weight means anything.
MIN_TAG = 25


def features(dat_path):
    """The three loudness-independent signals, or None if never analysed."""
    import anlz_wave as aw
    try:
        bands = aw.read_3band(dat_path)
    except Exception:
        return None
    if not bands:
        return None
    lo, mid, hi = bands['low'], bands['mid'], bands['high']
    n = len(lo)
    if not n:
        return None
    tot = [lo[i] + mid[i] + hi[i] for i in range(n)]
    total = sum(tot)
    if total <= 0:
        return None
    ordered = sorted(tot)
    peak = ordered[int(n * 0.95)] or 1
    return {
        # Where the track lives: bright and hissy, or all bottom end.
        'tilt': sum(hi) / total,
        # How much of it is spent at full tilt — relentlessness, not level.
        'dens': sum(1 for v in tot if v > 0.75 * peak) / float(n),
        # Kick weight, kept for the record though it is not scored: it turned
        # out to say more about the genre than about the intensity.
        'punch': sum(lo) / total,
    }


def _z(values):
    if not values:
        return []
    mean = statistics.mean(values)
    sd = statistics.pstdev(values) or 1.0
    return [(v - mean) / sd for v in values]


def tag_weights(rows):
    """What each of YOUR tags is worth, learned from the tracks carrying it.

    No hand-written list: a tag's weight is the average anchor of the tracks
    that wear it. Rare tags are left out — a weight from six tracks is noise
    wearing a number.
    """
    bucket = {}
    for r in rows:
        for tag in r.get('tags') or []:
            bucket.setdefault(tag, []).append(r['anchor'])
    return {tag: statistics.mean(vals) for tag, vals in bucket.items()
            if len(vals) >= MIN_TAG}


def deciles(rows):
    """`rows` = [{id, dens, bpm, tags}] -> ({id: 1..10}, tag weights).

    ⚠ Ranked, not thresholded. A threshold on the raw score would drift with
    whatever you happened to buy this year and would pile up in the middle
    again — the very problem this exists to fix.
    """
    rows = [r for r in rows if r.get('dens') is not None]
    if not rows:
        return {}, {}

    zb = _z([float(r.get('bpm') or 0) for r in rows])
    zd = _z([r['dens'] for r in rows])
    for i, r in enumerate(rows):
        r['anchor'] = ANCHOR['bpm'] * zb[i] + ANCHOR['dens'] * zd[i]

    weights = tag_weights(rows)
    for r in rows:
        mine = [weights[t] for t in (r.get('tags') or []) if t in weights]
        # ⚠ An untagged track cannot be scored on judgement it does not carry,
        # so it falls back to its own measurements rather than to zero — zero
        # would quietly mean "average", which is a claim nobody made.
        r['score'] = (TAG_SHARE * statistics.mean(mine) + (1 - TAG_SHARE) * r['anchor']
                      if mine else r['anchor'])

    rows.sort(key=lambda r: r['score'])
    n = len(rows)
    out = {str(r['id']): min(10, 1 + int(10 * i / n)) for i, r in enumerate(rows)}
    return out, weights


def anlz_paths(db_path=None):
    """Every live track that has been analysed, and where its data sits."""
    import rekordbox_merge as rm
    from pyrekordbox.db6 import tables

    share = os.path.expanduser('~/Library/Pioneer/rekordbox/share')
    db = rm._open_db(db_path)
    out = {}
    try:
        for c in db.query(tables.DjmdContent).filter_by(rb_local_deleted=0):
            p = c.AnalysisDataPath
            if p:
                out[str(c.ID)] = os.path.join(share, p.lstrip('/'))
    finally:
        try:
            db.close()
        except Exception:
            pass
    return out


def compute(tracks, db_path=None, log=None):
    """Work out the decile for every track we can read. Returns {id: 1..10}."""
    paths = anlz_paths(db_path)
    rows = []
    for t in tracks or []:
        tid = str(t.get('id'))
        dat = paths.get(tid)
        if not dat:
            continue
        f = features(dat)
        if not f:
            continue
        f['id'] = tid
        f['bpm'] = t.get('bpm') or 0
        tags = []
        for names in (t.get('banks') or {}).values():
            tags += names
        f['tags'] = tags
        rows.append(f)
        if log and len(rows) % 250 == 0:
            log('info', 'read %d tracks' % len(rows))
    out, weights = deciles(rows)
    save(out, weights)
    return out, weights


def save(mapping, weights=None):
    tmp = CACHE + '.part'
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'drive': mapping, 'tags': weights or {}}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CACHE)


def load():
    """(drive, tag weights). Tolerates the older shape, which had only drive."""
    try:
        with open(CACHE, encoding='utf-8') as fh:
            d = json.load(fh) or {}
    except Exception:
        return {}, {}
    if 'drive' in d:
        return d.get('drive') or {}, d.get('tags') or {}
    return d, {}
