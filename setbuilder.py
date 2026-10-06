"""Set-building logic: the Camelot wheel, compatibility and directions.

Everything here is a pure function over the tracks `rekordbox.load_tracks`
already read — no database, no network.

The idea: your MyTag vocabulary already describes how a set moves — a TIMING
group is the arc (Openers → Warmup → Filler → Peak Hour → Closing → Outro) and
a MOOD group is the direction — so there is no need to invent a new axis.
"""

import os
import re
import unicodedata

import settings

# The Camelot wheel. Minor keys are "A", major keys are "B".
CAMELOT_MINOR = {
    'AB': '1A', 'G#': '1A', 'EB': '2A', 'D#': '2A', 'BB': '3A', 'A#': '3A',
    'F': '4A', 'C': '5A', 'G': '6A', 'D': '7A', 'A': '8A', 'E': '9A',
    'B': '10A', 'F#': '11A', 'GB': '11A', 'DB': '12A', 'C#': '12A',
}
CAMELOT_MAJOR = {
    'B': '1B', 'F#': '2B', 'GB': '2B', 'DB': '3B', 'C#': '3B', 'AB': '4B',
    'G#': '4B', 'EB': '5B', 'D#': '5B', 'BB': '6B', 'A#': '6B', 'F': '7B',
    'C': '8B', 'G': '9B', 'D': '10B', 'A': '11B', 'E': '12B',
}


def normalize_key(raw):
    """Turn any notation into Camelot ("6A"). Returns "" if it cannot.

    A real library mixes '6A', 'Fm', 'E min', '08A', '12m' and '7M' happily.
    """
    s = str(raw or '').strip().upper().replace(' ', '')
    if not s or s == 'ALL':
        return ''

    # Already Camelot, or a variant of it: 6A, 08A, 12m, 7M, 9b
    m = re.match(r'^0?(\d{1,2})([ABM])?$', s)
    if m:
        num = int(m.group(1))
        letter = (m.group(2) or 'A')
        if not 1 <= num <= 12:
            return ''
        # A capital 'M' means major (7M = 7B); anything else is A/B as written.
        if letter == 'M':
            letter = 'B' if raw.strip().endswith('M') else 'A'
        return '%d%s' % (num, letter)

    # Musical notation: note plus quality (Fm, E MIN, Gbmaj, D)
    m = re.match(r'^([A-G])([#B]?)(.*)$', s)
    if not m:
        return ''
    note = m.group(1) + ('#' if m.group(2) == '#' else ('B' if m.group(2) == 'B' else ''))
    rest = m.group(3)
    if rest.startswith('MIN') or rest == 'M':
        table = CAMELOT_MINOR
    elif rest.startswith('MAJ') or rest == '':
        table = CAMELOT_MAJOR
    else:
        return ''
    return table.get(note, '')


def camelot_parts(key):
    m = re.match(r'^(\d{1,2})([AB])$', str(key or ''))
    return (int(m.group(1)), m.group(2)) if m else (None, None)


#: ⚠ compatible_keys lives at the BOTTOM of this file now, next to the move
#: table it reads. The old four-key version that used to sit here was replaced
#: — do not put a second one back: a private copy of the key rule is exactly
#: how neighbours() ended up dead while a different rule ran in the browser.


def bpm_distance(a, b):
    """Relative tempo difference, allowing for half-time and double-time."""
    if not a or not b:
        return None
    best = abs(a - b) / a
    for factor in (2.0, 0.5):
        best = min(best, abs(a - b * factor) / a)
    return best


# Genre families. ORDER MATTERS: the first rule that matches wins. "Afro House"
# belongs in House (it is a house record), not in Latin & Afro, which is why
# the house rule comes first. Rules match on a contained word, so they absorb
# the spelling variants and typos any real catalogue accumulates.
# ⚠ The colours are for a WHITE background and must pass contrast as TEXT
# (family chips are drawn with coloured text and a tinted border, not filled).
GENRE_FAMILIES = [
    ('Trance',          '#5c2091', ('trance', 'psyrance', 'goa')),
    ('Indie & house',   '#0f5323', ('indie', 'indietronica')),
    ('Techno',          '#12468c', ('techno', 'minimal')),
    ('House',           '#0f5323', ('house',)),
    ('Urban',           '#6b3800', ('hip hop', 'hip-hop', 'rap', 'trap', 'r&b',
                                    'drill', 'jersey')),
    ('Latin & afro',    '#931d17', ('reggaeton', 'reggeaton', 'reggaetek', 'dembow',
                                    'dancehall', 'reggae', 'afro', 'african',
                                    'baile', 'flamenco', 'gipsy', 'balkan', 'latin')),
    ('Disco & funk',    '#88103f', ('disco', 'funk', 'groove', 'swing', 'jazz')),
    ('Bass & breaks',   '#004f48', ('bass', 'garage', 'dubstep', 'drum', 'hardstyle')),
    ('Pop & other',     '#4a4e52', ('pop', 'rock', 'punk', 'country', 'folk',
                                    'classical', 'ost', 'movie', 'game', 'ballad')),
]
OTHER_FAMILY = ('No family', '#4a4e52')


def genre_family(name):
    """(family, colour) of a genre."""
    low = fold(name)
    for fam, color, words in GENRE_FAMILIES:
        for w in words:
            if w in low:
                return fam, color
    return OTHER_FAMILY


# Spellings worth repairing anywhere. Kept deliberately short: these are the
# ones that are wrong in every library, not judgements about yours.
#
# ⚠ There is no clever typo detection here, and that is on purpose. An earlier
# version compared edit distances and proposed "Pop → Rap" and "Folk → Rock".
# With a catalogue of a hundred-odd names, a decision beats a similarity score
# every time — so your own corrections go in Settings and are merged on top.
DEFAULT_GENRE_FIXES = {
    'Reggeaton': 'Reggaeton',
    'Pogressive Trance': 'Progressive Trance',
    'Psyrance': 'Psytrance',
    'Hip-Hop': 'Hip Hop',
    'Afro Beat': 'Afrobeat',
    'Psy Techno': 'Psytechno',
    'Drum n Bass': 'Drum and Bass',
    'Nu disco': 'Nu Disco',
    'indietronica': 'Indietronica',
}

# Genres that LOOK like typos of one another but are not the same thing. This
# list exists so they do not get flagged again next time.
GENRE_KEEP_APART = {
    'Progressive Psytrance': 'not Progressive Trance: it is progressive psytrance',
    'Melodic Psytrance': 'not Melodic Trance',
    'Reggaetek': 'reggaeton meets techno, a genre of its own',
    'Trip Hop': 'nothing to do with Hip Hop',
    'Progressive Deep House': 'not Progressive House',
    'UK Bassline': 'the UK sound, not generic Bassline',
    'Euro House': 'not Afro House (they only look alike written down)',
    'Folk': 'Folk is Folk. If it says Folk, it is Folk — not Rock',
    'Pop': 'Pop. Nothing to do with Rap',
    'Trap': 'a genre of its own, not a misspelling of Rap',
}

# Not genres at all, but usage markers: better off out of the genre field.
GENRE_NOT_A_GENRE = ('need genre', 'Samples', 'Sampleable', 'DJ TOOL', 'Memes',
                     'Movies', 'Games', 'OST')


def genre_fixes():
    """The universal corrections plus whatever you added in Settings."""
    out = dict(DEFAULT_GENRE_FIXES)
    out.update(settings.load().get('genre_fixes') or {})
    return out


def genre_fix_report(cloud):
    """What would be corrected and how many tracks it touches. Read-only."""
    counts = dict((g['name'], g['count']) for g in cloud)
    rows = []
    for wrong, right in genre_fixes().items():
        if wrong in counts and wrong != right:
            rows.append({'from': wrong, 'to': right, 'count': counts[wrong],
                         'merges_into': counts.get(right, 0)})
    rows.sort(key=lambda r: -r['count'])
    return rows


def edit_distance(a, b, cap=3):
    """Edit distance WITH transpositions (Damerau).

    A transposition costs 1, which is what this needs: "Reggeaton" is
    "Reggaeton" with two letters swapped, not two independent mistakes.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
        if min(d[i]) > cap:
            return cap + 1
    return d[la][lb]


def genre_issues(cloud):
    """Genres that are the SAME thing written differently.

    Two criteria, both conservative:
      · `same`: identical once case, accents and punctuation are removed
        (Nu disco / Nu Disco, Hip-Hop / Hip Hop, Afro Beat / Afrobeat).
      · `typo`: same number of words, at least five letters, and a difference
        that is small IN PROPORTION to the name (≤20%).

    ⚠ All three conditions are there because of real false positives: without
    the word count, "Progressive Psytrance" looked like a typo of "Progressive
    Trance"; with absolute distance and short names, "Pop → Rap" and "Folk →
    Rock" appeared; and without the proportion, "Euro House → Afro House" and
    "Reggaetek → Reggaeton", which are different genres.
    """
    def norm(s):
        return re.sub(r'[^a-z0-9]', '', fold(s))

    def words(s):
        return len([w for w in re.split(r'[^a-z0-9]+', fold(s)) if w])

    items = sorted(cloud, key=lambda g: (-g['count'], g['name']))
    groups, seen = [], set()
    for i, a in enumerate(items):
        if a['name'] in seen:
            continue
        variants = []
        for b in items[i + 1:]:
            if b['name'] in seen:
                continue
            na, nb = norm(a['name']), norm(b['name'])
            if not na or not nb:
                continue
            kind = None
            if na == nb:
                kind = 'same'
            elif words(a['name']) == words(b['name']) and edit_distance(na, nb) <= 2:
                kind = 'typo'
            if kind:
                variants.append(dict(b, kind=kind))
                seen.add(b['name'])
        if variants:
            seen.add(a['name'])
            groups.append({'keep': a, 'variants': variants,
                           'affected': sum(v['count'] for v in variants)})
    groups.sort(key=lambda g: -g['affected'])
    return groups


def split_genres(raw):
    """One genre tag can hold several: "House/Hip Hop" → two genres."""
    out = []
    for part in re.split(r'\s*[/,;|]\s*', str(raw or '')):
        part = part.strip()
        if part:
            out.append(part)
    return out


def fold(s):
    return ''.join(c for c in unicodedata.normalize('NFD', str(s or '').lower())
                   if unicodedata.category(c) != 'Mn')


ENERGY_RE = re.compile(r'^\s*(\d{1,2})\s*[-–—.:]?\s*(.*)$', re.S)


def split_comment(raw):
    """Many DJs start the comment with an ENERGY level: "6 - dark roller".

    When that convention is switched off in Settings the comment is returned
    untouched, so a library that uses the field for something else does not
    get its first word eaten.
    """
    s = str(raw or '').strip()
    if not s or not settings.load().get('energy_in_comment', True):
        return None, s
    m = ENERGY_RE.match(s)
    if not m:
        return None, s
    energy = int(m.group(1))
    if not 1 <= energy <= 10:
        return None, s
    return energy, m.group(2).strip()


def track_view(t):
    """A track, with what the set builder needs from it."""
    banks = {}
    for mt in t['mytags']:
        banks.setdefault(mt['cat'], []).append(mt['name'])
    energy, note = split_comment(t['comment'])
    return {
        'id': t['id'],
        'title': t['title'],
        'artist': t['artist'],
        'path': t['path'],
        'bpm': t['bpm'],
        'key': normalize_key(t['key']),
        'rawkey': t['key'],
        'genres': split_genres(t['genre']),
        'banks': banks,
        'tags': [mt['name'] for mt in t['mytags']],
        'seconds': t['rb_length'],
        'rating': t['rating'],
        'plays': t['plays'],
        # Energy comes out of the comment and is treated as its own metric;
        # `note` keeps the actual comment, without the number.
        'energy': energy,
        'note': note,
        'comment': t['comment'],   # raw, so energy edits preserve the rest verbatim
        'added': t['added'],
        'imported': t.get('imported') or '',
        # ⚠ The colour label travels with the track: the Tagger needs it
        # to build the vetting queues, and it is the only field that says
        # whether a track has been through your hands yet.
        'color': t['color'],
        # ⚠ rekordbox stores 0 for formats it does not rate (FLAC); the
        # converter fills it in for the AIFFs it makes. 0 means "unknown", not
        # "silent", so the browser shows a dash rather than a number.
        'kbps': t.get('rb_bitrate') or 0,
        # Search has to hit ANY field: artist, title, genre, comment, tags,
        # key and tempo.
        'search': fold(' '.join([
            t['artist'], t['title'], t['genre'], t['comment'] or '', t['key'],
            str(t['bpm'] or ''), os.path.basename(t['path']),
            ' '.join(mt['name'] for mt in t['mytags']),
        ])),
    }


def fold_search(view):
    """The search index, rebuilt from a track VIEW row.

    ⚠ Same recipe as track_view's 'search', but taking the VIEW (genres and
    tags already split into lists) so an edit applied in memory can refresh it.
    Without a way to recompute it, a renamed track stayed findable only under
    its old name.
    """
    return fold(' '.join([
        view.get('artist') or '', view.get('title') or '',
        ' '.join(view.get('genres') or []), view.get('comment') or '',
        view.get('key') or '', str(view.get('bpm') or ''),
        os.path.basename(view.get('path') or ''),
        ' '.join(view.get('tags') or []),
    ]))


NO_COLOR = '(no colour)'


def build_library(tracks, min_seconds=60, playlists=None, ready_color=None,
                  history=None):
    """Prepare the library and its vocabulary (groups, genres) for the UI.

    `ready_color` is the rekordbox colour label meaning "this one is ready to
    play". ⚠ It defaults to whatever you set in Settings, and to nothing at
    all if you set none — filtering by a colour a new user has never applied
    would show them an empty library and look broken.
    """
    if ready_color is None:
        ready_color = (settings.load().get('ready_color') or '').strip()

    long_enough = [t for t in tracks if (t['rb_length'] or 0) >= min_seconds]
    skipped = {}
    if ready_color:
        keep = []
        for t in long_enough:
            col = (t['color'] or '').strip()
            if col == ready_color:
                keep.append(t)
            else:
                skipped[col or NO_COLOR] = skipped.get(col or NO_COLOR, 0) + 1
        long_enough = keep
    songs = [track_view(t) for t in long_enough]

    # When you last played it, from the real session history.
    hist = history or {}
    lastmap = hist.get('last') or {}
    for sg in songs:
        sg['lastPlayed'] = lastmap.get(sg['id']) or None

    bank_order, bank_counts = {}, {}
    for t in tracks:
        for mt in t['mytags']:
            bank_order.setdefault(mt['cat'], mt['catseq'])
    # Every group and tag rekordbox has, used or not, so a new tag can be
    # chosen before any track carries it.
    try:
        import rekordbox as _rb
        for cat, seq, names in _rb.load_mytag_groups():
            bank_order.setdefault(cat, seq)
            for n in names:
                bank_counts.setdefault(cat, {}).setdefault(n, 0)
    except Exception:
        pass                    # the tags on the tracks still give the rest
    for s in songs:
        for cat, names in s['banks'].items():
            for n in names:
                bank_counts.setdefault(cat, {}).setdefault(n, 0)
                bank_counts[cat][n] += 1

    banks = []
    for cat in sorted(bank_counts, key=lambda c: bank_order.get(c, 99)):
        banks.append({
            'name': cat,
            'seq': bank_order.get(cat, 99),
            # Alphabetical: with a couple of dozen moods, finding one by size
            # is impossible.
            'tags': sorted(({'name': n, 'count': c}
                            for n, c in bank_counts[cat].items()),
                           key=lambda x: x['name'].lower()),
        })

    genres = {}
    for s in songs:
        for g in s['genres']:
            genres[g] = genres.get(g, 0) + 1
    cloud = []
    for g, c in genres.items():
        fam, color = genre_family(g)
        cloud.append({'name': g, 'count': c, 'family': fam, 'color': color})
    cloud.sort(key=lambda x: -x['count'])

    fam_order = [f[0] for f in GENRE_FAMILIES] + [OTHER_FAMILY[0]]
    fam_counts = {}
    for s in songs:
        fams = set(genre_family(g)[0] for g in s['genres'])
        for f in fams:
            fam_counts[f] = fam_counts.get(f, 0) + 1
    fam_colors = dict([(x[0], x[1]) for x in GENRE_FAMILIES] + [OTHER_FAMILY])
    families = [{'name': f,
                 'color': fam_colors.get(f, OTHER_FAMILY[1]),
                 'count': fam_counts.get(f, 0)}
                for f in fam_order if fam_counts.get(f)]

    # Only the track-to-track chains that are actually in the library.
    ids = set(sg['id'] for sg in songs)
    follows = {}
    for a, nexts in (hist.get('follows') or {}).items():
        if a not in ids:
            continue
        keep = dict((b, n) for b, n in nexts.items() if b in ids)
        if keep:
            follows[a] = keep

    # Which MyTag group is the direction axis and which is the set arc, plus
    # the arc in ORDER. ⚠ All three come from the library, not from constants:
    # they used to be the author's own group names, so anybody else's library
    # produced a set builder with no directions and no arc.
    guess = settings.guess_banks(banks)
    arc = []
    if guess['timing']:
        present = [t['name'] for b in banks if b['name'] == guess['timing']
                   for t in b['tags']]
        arc = order_arc(present)
        # The arc group reads best in running order, not alphabetically.
        for b in banks:
            if b['name'] == guess['timing']:
                rank = {n: i for i, n in enumerate(arc)}
                b['tags'].sort(key=lambda t: rank.get(t['name'], len(rank)))

    return {'tracks': songs, 'banks': banks, 'genres': cloud, 'follows': follows,
            'families': families, 'playlists': playlists or [],
            'ready_color': ready_color,
            'moodBank': guess['mood'], 'timingBank': guess['timing'],
            'typeBank': guess.get('type') or '',
            # ⚠ The whole key table, computed here and only here. The browser
            # looks moves up in it; it must never grow its own copy.
            'keyMatrix': key_matrix(4),
            'timingArc': arc,
            'ignoredBanks': settings.load().get('ignored_banks') or [],
            'skipped': sorted(({'color': c, 'count': n} for c, n in skipped.items()),
                              key=lambda x: -x['count'])}


# The arc of a set, as STAGES rather than names. Whatever a library calls its
# timing tags — "Openers" or "Intro", "Peak Hour" or "Prime time" — a tag is
# placed by the words in it, so the set builder knows the running order of
# a vocabulary it has never seen.
# ⚠ This used to be one person's six tag names, literally. Any other library
# got no openers and an arc in alphabetical order.
ARC_STAGES = (
    ('open',   ('open', 'intro', 'start', 'first')),
    ('warm',   ('warm',)),
    ('middle', ('middle', 'filler', 'main', 'build', 'groove')),
    ('peak',   ('peak', 'prime', 'climax', 'banger')),
    ('close',  ('clos', 'last')),
    # ⚠ Not "after" (a kind of NIGHT in most vocabularies) nor a bare "end",
    # which is inside "legend" and "weekend".
    ('outro',  ('outro', 'ending')),
)


def arc_stage_of(name):
    """The stage (0 = opening … 5 = outro) a timing tag's NAME places it
    in, or None when nothing in it says."""
    low = (name or '').lower()
    for i, (_, words) in enumerate(ARC_STAGES):
        if any(w in low for w in words):
            return i
    return None


def order_arc(names):
    """Timing tags in the order a night runs; any the words do not place
    keep their own order after the ones they do."""
    placed = [(arc_stage_of(n), i, n) for i, n in enumerate(names)]
    known = sorted((s, i, n) for s, i, n in placed if s is not None)
    return [n for _, _, n in known] + [n for s, _, n in placed if s is None]

BPM_TIGHT = 0.03
BPM_LOOSE = 0.06


def neighbours(ref, pool, bpm_tol=BPM_LOOSE, key_strict=True, exclude=()):
    """Tracks that mix with `ref`: neighbouring key and a close tempo."""
    if not ref:
        return []
    ok_keys = compatible_keys(ref['key'])
    skip = set(exclude) | {ref['id']}
    out = []
    for t in pool:
        if t['id'] in skip:
            continue
        if key_strict and ok_keys and t['key'] and t['key'] not in ok_keys:
            continue
        d = bpm_distance(ref['bpm'], t['bpm'])
        if d is not None and d > bpm_tol:
            continue
        out.append((d if d is not None else 1.0, t))
    out.sort(key=lambda r: r[0])
    return [t for _, t in out]


def directions(ref, candidates, bank='MOOD', top=8):
    """Where you can go next: each tag in the group, and how many tracks it holds.

    Tags the current track ALREADY has are marked as "staying"; the rest are
    the turn. That shows at a glance whether you can go darker, faster or
    more open.
    """
    have = set(ref['banks'].get(bank, [])) if ref else set()
    counts = {}
    for t in candidates:
        for name in t['banks'].get(bank, []):
            counts[name] = counts.get(name, 0) + 1
    rows = [{'name': n, 'count': c, 'same': n in have} for n, c in counts.items()]
    rows.sort(key=lambda r: (r['same'], -r['count']))
    return rows[:top]


# ══ the feel of a set, and how to start one ════════════════════════════════
#
# Two tracks matching on key and tempo only says they can be mixed. It says
# nothing about whether the second one belongs in what you are building — and
# that is the thing you actually decide with your ears. So the set carries a
# CENTRE OF GRAVITY (what it is made of so far) and a candidate is measured
# against it, not only against the track before it.
#
# ⚠ Measured on the real library before choosing what to lean on: bpm, key and
# length are on every track, energy on 97%, genres and MyTags on 99.8%. So all
# of those are fair game.
#
# ⚠⚠ But the ARC is not derivable. The TIMING tags average 6.25 energy for
# "Openers" and 6.53 for "Peak Hour" — a third of a point apart on a nine
# point scale, and four BPM between the slowest and fastest label. Deriving
# "this is an opener" from energy or tempo would be inventing a signal that is
# not in the data. Where the arc really lives is in the tags themselves, put
# there by the person who knows. So the arc is READ, never computed.

#: How much each ingredient counts when asking "does this belong here".
#: Mood and family carry the most because they are what "feel" means; tempo
#: and energy are already handled by the mixing filters, so here they only
#: break ties.
FEEL_WEIGHTS = {'moods': 0.40, 'family': 0.25, 'energy': 0.20, 'bpm': 0.15}


def _energy_of(track):
    raw = track.get('energy')
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _families(track):
    # ⚠ genre_family returns (name, colour). Only the NAME identifies a
    # family — keeping the pair meant the colour travelled into the feel and
    # out to the interface, where it reads as ('Trance', '#5c2091').
    return {genre_family(g)[0] for g in (track.get('genres') or []) if g}


def set_feel(tracks, mood_bank='MOOD'):
    """What a set is made of so far — its centre of gravity.

    Shares, not counts: a set of six and a set of sixty have to be comparable,
    because the same candidate gets measured against both.
    """
    tracks = [t for t in (tracks or []) if t]
    n = len(tracks)
    feel = {'n': n, 'moods': {}, 'families': {}, 'energy': None, 'bpm': None}
    if not n:
        return feel

    moods, fams = {}, {}
    for t in tracks:
        for tag in (t.get('banks') or {}).get(mood_bank, []) or []:
            moods[tag] = moods.get(tag, 0) + 1
        for f in _families(t):
            fams[f] = fams.get(f, 0) + 1
    feel['moods'] = {k: v / float(n) for k, v in moods.items()}
    feel['families'] = {k: v / float(n) for k, v in fams.items()}

    es = [e for e in (_energy_of(t) for t in tracks) if e is not None]
    if es:
        feel['energy'] = sum(es) / len(es)
    bs = [t['bpm'] for t in tracks if t.get('bpm')]
    if bs:
        feel['bpm'] = sum(bs) / len(bs)
    return feel


def feel_fit(track, feel, mood_bank='MOOD'):
    """0..1 — how much this track belongs to that set, and why.

    Returns (score, reasons). An empty set fits everything: there is nothing
    to belong to yet, and pretending otherwise would rank the first pick.
    """
    if not feel or not feel.get('n'):
        return 1.0, []

    reasons = []
    parts = {}

    # Mood: how much of the set's mood does this track carry? Shares are
    # summed rather than counted, so a track sharing the one tag that is on
    # every track of the set scores higher than one sharing three rare ones.
    # ⚠ A track with NO mood tags is not a bad match, it is an unmeasured one.
    # Scoring the absent signal as zero cost it 40% of the total before
    # anything was compared: 492 of 2,416 tracks here have no MOOD tag, and
    # the best of them sat at position 806 instead of 5. Same rule as energy
    # and bpm below, which gate on BOTH sides — this was the odd one out.
    tags = set((track.get('banks') or {}).get(mood_bank, []) or [])
    if feel['moods'] and tags:
        got = sum(share for tag, share in feel['moods'].items() if tag in tags)
        total = sum(feel['moods'].values()) or 1.0
        parts['moods'] = min(1.0, got / total)
        hits = [t for t in feel['moods'] if t in tags]
        if hits:
            hits.sort(key=lambda t: -feel['moods'][t])
            reasons.append(', '.join(hits[:2]))

    fams = _families(track)
    if feel['families'] and fams:
        got = sum(share for f, share in feel['families'].items() if f in fams)
        total = sum(feel['families'].values()) or 1.0
        parts['family'] = min(1.0, got / total)

    # Energy and tempo: closeness, forgiving by a point and by 6%.
    e = _energy_of(track)
    if e is not None and feel['energy'] is not None:
        parts['energy'] = max(0.0, 1.0 - abs(e - feel['energy']) / 3.0)
    if track.get('bpm') and feel['bpm']:
        drift = abs(track['bpm'] - feel['bpm']) / float(feel['bpm'])
        parts['bpm'] = max(0.0, 1.0 - drift / 0.12)

    # ⚠ Re-weight over the parts we could actually measure. Scoring a missing
    # field as zero would push every untagged track to the bottom, which is
    # not what "we do not know" means.
    live = {k: w for k, w in FEEL_WEIGHTS.items() if k in parts}
    if not live:
        return 0.5, reasons
    total_w = sum(live.values())
    score = sum(parts[k] * w for k, w in live.items()) / total_w
    return round(score, 3), reasons


def arc_stage(track, timing_bank='TIMING'):
    """Where in the night this track says it belongs, if it says at all:
    (stage, tag) for its earliest timing tag, or (None, '').
    ⚠ A middle tag ("Filler" — the commonest timing tag in one real library)
    must count as a stage: reported as "no tag at all", openers() ranked it
    ABOVE everything tagged as a warm-up."""
    best = (None, '')
    for tag in (track.get('banks') or {}).get(timing_bank, []) or []:
        s = arc_stage_of(tag)
        if s is not None and (best[0] is None or s < best[0]):
            best = (s, tag)
    return best


def openers(tracks, style=None, type_bank='TYPE OF SET', timing_bank='TIMING',
            limit=12):
    """Tracks to start a set with, for a given style.

    `style` is a TYPE OF SET tag — the vocabulary this library actually uses
    for what kind of night it is: Rave, Sunset, After, Pre-Party, Family,
    Pop Party, Sunrise, Nowhere.

    ⚠ Ranked by the Openers TAG first and only then by anything measured. The
    tags are the whole signal here: energy does not separate an opener from a
    peak-hour track in this library (6.25 against 6.53), so sorting by energy
    would just shuffle them.
    """
    out = []
    for t in tracks or []:
        if style:
            kinds = (t.get('banks') or {}).get(type_bank, []) or []
            if style not in kinds:
                continue
        idx, name = arc_stage(t, timing_bank)
        tagged_opener = (idx == 0)
        # Without the tag a track can still open, but it goes below the ones
        # you have actually marked.
        rank = 0 if tagged_opener else (1 if idx is None else 2)
        out.append({'track': t, 'rank': rank, 'stage': name,
                    'rating': t.get('rating') or 0,
                    'energy': _energy_of(t)})
    out.sort(key=lambda r: (r['rank'], -r['rating'],
                            r['energy'] if r['energy'] is not None else 9))
    return out[:limit]


def styles(tracks, type_bank='TYPE OF SET'):
    """The kinds of night this library knows about, commonest first."""
    counts = {}
    for t in tracks or []:
        for tag in (t.get('banks') or {}).get(type_bank, []) or []:
            counts[tag] = counts.get(tag, 0) + 1
    return [{'name': k, 'count': v}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
# ══ key compatibility: what can follow what ════════════════════════════════
#
# The Camelot wheel has TWO axes and mixing them up is why the DJ literature
# contradicts itself:
#
#   · Δ, the number of steps around the wheel, governs the HARMONIC MATERIAL
#     and nothing else. Scale notes in common: 7, 6, 5, 4, 3, 2, 2 for
#     |Δ| = 0…6. The letter does not enter into it — 10A and 10B are the same
#     seven notes.
#   · The letter change governs the TONIC and the mode. How far the tonic
#     moves depends on which side you start from: with σ = +1 from an A and
#     σ = −1 from a B, a crossing shifts the tonic by 7Δ + 3σ semitones. That
#     is why 8A→7B and 8A→9B share the same 6 notes and sound nothing alike.
#
# So a move is judged on both: Δ says how much material is shared, the shared
# TRIAD notes say whether the new tonic is already inside the old chord.
#
# ⚠ Every number in the table below is CHECKED at import by _verify(), not
# copied from a source. The harmonic-mixing literature repeats each other and
# a wrong rule here puts a clashing track in front of you.

#: 8B is C major and 8A is A minor; one step round the wheel is a fifth.
_C_MAJOR_AT = 8

#: (how far round, does the letter flip, level, what it is called)
#: `dn` is a function of σ, which is +1 from an A and −1 from a B.
KEY_MOVES = (
    ('same',      lambda s: 0,     False, 1, 'same key'),
    ('relative',  lambda s: 0,     True,  1, 'relative major/minor'),
    ('fifth',     lambda s: 1,     False, 1, 'a fifth up (dominant)'),
    ('fourth',    lambda s: -1,    False, 1, 'a fourth up (subdominant)'),
    ('mediant',   lambda s: -s,    True,  1, 'mediant'),

    ('tone_up',   lambda s: 2,     False, 2, 'a tone up (energy boost)'),
    ('tone_down', lambda s: -2,    False, 2, 'a tone down'),
    ('diag',      lambda s: s,     True,  2, 'the other diagonal'),
    ('fourth_mode', lambda s: 2*s, True,  2, 'a fourth up, mode changed'),
    ('semitone_soft', lambda s: -2*s, True, 2, 'a semitone, the gentle way'),

    ('third_up',  lambda s: -3,    False, 3, 'a minor third up'),
    ('third_down', lambda s: 3,    False, 3, 'a minor third down'),
    ('parallel',  lambda s: 3*s,   True,  3, 'parallel (same tonic, other mode)'),
    # ⚠ (n-3σ, other letter) is deliberately absent at every level: it is the
    # one destination three steps out whose tonic lands a TRITONE away.
)

#: Major scale as semitone offsets, and the triad inside it.
_SCALE = (0, 2, 4, 5, 7, 9, 11)
_TRIAD = (0, 4, 7)


def _parts(key):
    import re
    m = re.match(r'^(\d{1,2})([AB])$', str(key or '').strip().upper())
    if not m:
        return None, None
    n = int(m.group(1))
    return (n, m.group(2)) if 1 <= n <= 12 else (None, None)


def _tonic(n, letter):
    """Semitone of the tonic, 0 = C."""
    major_root = (7 * (n - _C_MAJOR_AT)) % 12
    return major_root if letter == 'B' else (major_root - 3) % 12


def _notes(n, letter):
    """The seven notes of that key, as semitones from C."""
    root = (7 * (n - _C_MAJOR_AT)) % 12          # the major parent either way
    return {(root + s) % 12 for s in _SCALE}


def _triad(n, letter):
    t = _tonic(n, letter)
    third = 4 if letter == 'B' else 3            # major or minor third
    return {t, (t + third) % 12, (t + 7) % 12}


def _wheel(n, d):
    return ((n - 1 + d) % 12) + 1


def key_move(src, dst):
    """How you get from one key to the other, or None if it is not a move."""
    n, letter = _parts(src)
    dn_, dl = _parts(dst)
    if n is None or dn_ is None:
        return None
    sigma = 1 if letter == 'A' else -1
    for move_id, dn, flip, level, label in KEY_MOVES:
        want_n = _wheel(n, dn(sigma))
        want_l = ('B' if letter == 'A' else 'A') if flip else letter
        if want_n == dn_ and want_l == dl:
            return {'id': move_id, 'level': level, 'label': label,
                    'semitones': (_tonic(dn_, dl) - _tonic(n, letter)) % 12,
                    'notes': len(_notes(n, letter) & _notes(dn_, dl)),
                    'triad': len(_triad(n, letter) & _triad(dn_, dl))}
    return None


def compatible_keys(key, level=1):
    """Every key you can go to at that level of openness.

    ⚠ The default is 1 — the four-plus-one classic set — so callers written
    before levels existed keep the behaviour they were written against.
    """
    n, letter = _parts(key)
    if n is None:
        return set()
    if level >= 4:
        return {'%d%s' % (i, l) for i in range(1, 13) for l in ('A', 'B')}
    sigma = 1 if letter == 'A' else -1
    out = set()
    for move_id, dn, flip, lvl, label in KEY_MOVES:
        if lvl > level:
            continue
        want_l = ('B' if letter == 'A' else 'A') if flip else letter
        out.add('%d%s' % (_wheel(n, dn(sigma)), want_l))
    return out


def key_matrix(level=4):
    """Every source key to every destination it reaches, with the move.

    Handed to the browser whole so the interface never computes key rules of
    its own — the split between setbuilder and a private copy in the page is
    exactly how neighbours() ended up dead while a different rule ran up
    there, and with four levels that would be worse.
    """
    keys = ['%d%s' % (i, l) for i in range(1, 13) for l in ('A', 'B')]
    out = {}
    for src in keys:
        row = {}
        for dst in keys:
            mv = key_move(src, dst)
            if mv and mv['level'] <= level:
                row[dst] = mv
        out[src] = row
    return out


def _verify():
    """Check the table against the intervals, at import. Cheap and total."""
    keys = ['%d%s' % (i, l) for i in range(1, 13) for l in ('A', 'B')]
    for src in keys:
        for lvl, want in ((1, 5), (2, 10), (3, 13)):
            got = compatible_keys(src, lvl)
            assert len(got) == want, (src, lvl, len(got), sorted(got))
        # the notes shared must follow |Δ| alone, never the letter
        n, l = _parts(src)
        for d in range(-6, 7):
            a = len(_notes(n, l) & _notes(_wheel(n, d), l))
            b = len(_notes(n, l) & _notes(_wheel(n, d), 'B' if l == 'A' else 'A'))
            assert a == b, (src, d, a, b)
        # no move may land a tritone from the tonic
        for dst, mv in key_matrix(3)[src].items():
            assert mv['semitones'] != 6, (src, dst, mv)


_verify()
