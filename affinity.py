#!/usr/bin/env python3
"""What you actually play after what — learned from your own sets.

The set builder could always answer "what fits after THIS record". This
answers the question you really ask, which is the other way round: given the
set as it stands, what belongs in it next? Nothing has to be selected.

Everything here is learned from real sessions — 3,246 transitions recovered
from rekordbox's history and djay's, spanning 2021 to now. Four decisions
shape it, and each one came from measuring:

⚠ **Tags, not genres.** The genre field is free text and has fragmented:
"Dembow" and "Dembow/Reggaeton", "Hip Hop/Rap" and "Hip Hop/Rap/Pop" are the
same music spelled twice, and the strongest "bridges" a genre model finds are
just those spelling pairs. The MyTag vocabulary is curated, covers 79% of the
library and 82% of all transitions, and means what it says.

⚠ **Lift, not counts.** The most repeated single move is always staying put
(Indie Dance → Indie Dance, 111 times). Ranking by frequency recommends more
of the same, for ever. What matters is how much more often a move happens than
chance would give it — that finds the moves that are CHARACTERISTIC rather
than merely common. Measured: 84% of real transitions cross genre, so staying
in a lane was never what this DJ did anyway.

⚠ **Rare pairs are shrunk, not trusted.** Raw lift on three occurrences comes
out at ×180 and is noise. Every lift is damped by how much evidence stands
behind it, so a move seen twenty times outranks a fluke.

⚠ **A suggestion that cannot say why is useless.** Every candidate carries the
reasons it surfaced, in words, so you can disagree with the machine.
"""

import collections
import math

#: Evidence needed before a lift is taken at face value. Below it the score is
#: pulled towards "no opinion" in proportion to how thin the evidence is.
CONFIDENCE_AT = 8.0

#: Banks that describe the music itself. MISC holds workflow marks ("OK",
#: "Sampleable") that say nothing about what to play next.
FLOW_BANKS = ('MOOD', 'TIMING', 'FAMILIES')

#: The bank that names the kind of night.
TYPE_BANK = 'TYPE OF SET'

#: How much of the set to read as "where we are now". The whole set sets the
#: mood; the last few records decide what can physically come next.
RECENT = 3

#: Beyond this many BPM from the last record, nothing is a continuation, no
#: matter how well the tags agree. ⚠ Without it the model happily put a
#: 90 BPM rap track after 143 BPM psytrance because both are tagged "Party":
#: the tags were right and the suggestion was absurd.
BPM_REACH = 14.0

#: At most this many from one artist, and from one kind of move. ⚠ The first
#: version returned six rows that were the same idea ("after Party you go to
#: Sexy") and three of them were the same artist. A list of near-duplicates is
#: one suggestion wearing six coats.
PER_ARTIST = 2
PER_REASON = 3


def _tags(track, banks=None):
    """(bank, tag) pairs, from either shape a track arrives in.

    ⚠ Two shapes exist and they are not interchangeable: rekordbox.load_tracks
    gives `mytags` as [{cat, name, ...}], while setbuilder.track_view — which
    is what the server actually serves — folds them into `banks`
    {cat: [name, ...]}. Reading only the first returned nothing at all through
    the API while working perfectly in a direct test, which is the most
    confusing way for this to fail.
    """
    out = []
    raw = track.get('mytags')
    if raw:
        for mt in raw:
            cat = mt.get('cat')
            if not banks or cat in banks:
                out.append((cat, mt.get('name')))
        return out
    for cat, names in (track.get('banks') or {}).items():
        if banks and cat not in banks:
            continue
        for name in names:
            out.append((cat, name))
    return out


def _damp(count):
    """Trust in a lift, 0..1, from the evidence behind it."""
    return count / (count + CONFIDENCE_AT)


def learn(tracks, follows):
    """Build the model from the library and the real transition history.

    `follows` is {trackId: {trackId: times}} — rekordbox's shape.
    """
    by_id = {str(t['id']): t for t in tracks}

    pair = collections.Counter()      # (tagA, tagB) -> times
    src = collections.Counter()       # tagA -> times it led
    dst = collections.Counter()       # tagB -> times it followed
    total = 0

    for a, nexts in (follows or {}).items():
        ta = by_id.get(str(a))
        if not ta:
            continue
        atags = _tags(ta, FLOW_BANKS)
        if not atags:
            continue
        for b, n in nexts.items():
            tb = by_id.get(str(b))
            if not tb:
                continue
            btags = _tags(tb, FLOW_BANKS)
            if not btags:
                continue
            # Every tag on the left paired with every tag on the right. A
            # record is several things at once and so is the move.
            for x in atags:
                for y in btags:
                    pair[(x, y)] += n
                    src[x] += n
                    dst[y] += n
                    total += n

    flow = {}
    for (x, y), n in pair.items():
        expected = src[x] * dst[y] / total if total else 0
        if expected <= 0:
            continue
        lift = math.log(n / expected)          # log: symmetric around "as expected"
        flow[(x, y)] = lift * _damp(n)

    # Which tags belong to which kind of night, same lift idea.
    type_tag = collections.Counter()
    type_n = collections.Counter()
    tag_n = collections.Counter()
    grand = 0
    for t in tracks:
        types = [n for c, n in _tags(t, (TYPE_BANK,))]
        flows = _tags(t, FLOW_BANKS)
        for ty in types:
            type_n[ty] += 1
            for f in flows:
                type_tag[(ty, f)] += 1
        for f in flows:
            tag_n[f] += 1
        grand += 1

    party = {}
    for (ty, f), n in type_tag.items():
        expected = type_n[ty] * tag_n[f] / grand if grand else 0
        if expected > 0:
            party[(ty, f)] = math.log(n / expected) * _damp(n)

    return {'flow': flow, 'party': party,
            'pairs': int(total), 'tags': len(src)}


def compact(model):
    """The model in a shape small enough to hand to the browser.

    ⚠ Worth doing precisely because it IS small — 34 tags, so at most a
    thousand or so pairs. Sending it once means the candidate list can be
    rescored on every keystroke without a round trip, and the same numbers
    drive both "match against this record" and "match against the set".
    Two scorers computing the same thing in different places is how they
    drift apart.
    """
    flow = dict(('%s|%s\t%s|%s' % (a[0], a[1], b[0], b[1]), round(v, 4))
                for (a, b), v in (model.get('flow') or {}).items()
                if abs(v) > 0.02)
    party = dict(('%s\t%s|%s' % (ty, f[0], f[1]), round(v, 4))
                 for (ty, f), v in (model.get('party') or {}).items()
                 if abs(v) > 0.02)
    return {'flow': flow, 'party': party, 'pairs': model.get('pairs', 0)}


def _set_types(set_tracks):
    """The kind of night this set is turning out to be, with weights."""
    c = collections.Counter()
    for t in set_tracks:
        for _cat, name in _tags(t, (TYPE_BANK,)):
            c[name] += 1
    if not c:
        return {}
    top = max(c.values())
    return {k: v / top for k, v in c.items()}


def suggest(set_tracks, pool, model, limit=40, exclude_ids=()):
    """Rank the pool by how well each record continues THIS set.

    Returns [{track, score, why: [...]}], best first. `why` is the point: a
    suggestion you cannot argue with is one you cannot trust.
    """
    if not model or not model.get('flow'):
        return []

    flow, party = model['flow'], model['party']
    recent = set_tracks[-RECENT:] if set_tracks else []
    recent_tags = []
    for i, t in enumerate(recent):
        # The last record counts most.
        weight = (i + 1) / len(recent)
        for tag in _tags(t, FLOW_BANKS):
            recent_tags.append((tag, weight))

    types = _set_types(set_tracks)
    # What the set already leans on, so we can reward a change of air.
    seen_tags = collections.Counter()
    for t in set_tracks:
        for tag in _tags(t, FLOW_BANKS):
            seen_tags[tag] += 1

    skip = set(str(i) for i in exclude_ids) | set(str(t['id']) for t in set_tracks)

    # Where the set physically is right now.
    last = set_tracks[-1] if set_tracks else None
    last_bpm = (last or {}).get('bpm') or 0
    last_key = (last or {}).get('key') or ''

    out = []
    for cand in pool:
        if str(cand['id']) in skip:
            continue
        ctags = _tags(cand, FLOW_BANKS)
        if not ctags:
            continue

        # 1. Does this move look like a move you make?
        best, flow_score = None, 0.0
        for tag, weight in recent_tags:
            for ct in ctags:
                v = flow.get((tag, ct))
                if v is None:
                    continue
                v *= weight
                flow_score += v
                if best is None or v > best[0]:
                    best = (v, tag, ct)
        flow_score = flow_score / max(1, len(ctags))

        # 2. Does it belong to this kind of night?
        party_score = 0.0
        for ty, w in types.items():
            for ct in ctags:
                party_score += w * party.get((ty, ct), 0.0)
        party_score /= max(1, len(ctags))

        # 3. ⚠ Reward what the set has NOT had yet. Without this the model
        # converges on one colour: everything that fits keeps fitting, and a
        # set is not a genre, it is a journey.
        fresh = sum(1 for ct in ctags if not seen_tags.get(ct)) / len(ctags)

        # 4. Can it actually follow? Tags say what belongs; tempo and key say
        # what is playable, and no amount of agreement about mood survives a
        # 50 BPM gap.
        tempo = 1.0
        if last_bpm and cand.get('bpm'):
            gap = abs(float(cand['bpm']) - float(last_bpm))
            # Half and double time are the same tempo to a dancer.
            gap = min(gap, abs(float(cand['bpm']) * 2 - float(last_bpm)),
                      abs(float(cand['bpm']) / 2 - float(last_bpm)))
            if gap > BPM_REACH:
                continue
            tempo = 1.0 - (gap / BPM_REACH) * 0.5

        keyed = 0.0
        if last_key and cand.get('key'):
            try:
                import setbuilder as _sb
                move = _sb.key_move(last_key, cand['key'])
                if move:
                    keyed = 0.45 / max(1, move.get('level', 4))
            except Exception:
                pass

        score = (flow_score + 0.6 * party_score + 0.5 * fresh + keyed) * tempo
        why = []
        if best and best[0] > 0.05:
            why.append('després de «%s» sols anar a «%s»' % (best[1][1], best[2][1]))
        if party_score > 0.05 and types:
            night = max(types, key=lambda k: types[k])
            why.append('encaixa amb un set de «%s»' % night)
        if fresh > 0.5:
            new = [ct[1] for ct in ctags if not seen_tags.get(ct)]
            why.append('aporta %s, que encara no hi és' % ', '.join(new[:2]))
        if keyed > 0.2:
            why.append('la tonalitat lliga amb «%s»' % last_key)
        out.append({'id': str(cand['id']), 'score': round(score, 4), 'why': why,
                    '_artist': (cand.get('artist') or '').lower(),
                    '_reason': best[1:] if best else None})

    out.sort(key=lambda r: -r['score'])

    # Spread the answer out: the point of a list is to offer real choices.
    picked, artists, reasons = [], collections.Counter(), collections.Counter()
    for r in out:
        if artists[r['_artist']] >= PER_ARTIST:
            continue
        if r['_reason'] and reasons[r['_reason']] >= PER_REASON:
            continue
        artists[r['_artist']] += 1
        if r['_reason']:
            reasons[r['_reason']] += 1
        picked.append({k: v for k, v in r.items() if not k.startswith('_')})
        if len(picked) >= limit:
            break
    return picked
