#!/usr/bin/env python3
"""Finding a track in your library when you do not know its exact name.

Two questions, one engine:

  · **"Do I already have this?"** — you are listening to something elsewhere
    and want to know whether it is already in the library. Names never match
    exactly: Spotify writes "Catz 'n Dogz", the file says "Catz N Dogz", and a
    store adds "(Extended Mix)" that a promo does not have.

  · **Browsing** — free text over everything, ranked by how well it fits.

⚠ Nothing here decides anything. It returns a score and lets you look: a "you
already have this" that is confidently wrong is worse than one that hesitates,
because it stops you buying a track you do not own.
"""

import difflib
import re
import unicodedata

# Version markers. Kept OUT of the base title so that "Skyfall" and "Skyfall
# (Extended Mix)" recognise each other, but still compared separately — an
# extended mix and a radio edit ARE different files and you may want both.
VERSION_WORDS = (
    'original mix', 'extended mix', 'radio edit', 'club mix', 'dub mix',
    'instrumental', 'acapella', 'a cappella', 'remix', 'rework', 'edit',
    'bootleg', 'mashup', 'vip', 'remaster', 'remastered', 'live',
    'extended', 'radio', 'mix', 'version',
)

_FEAT = re.compile(r'\b(feat|ft|featuring|with)\.?\s+.*$', re.I)
_BRACKETS = re.compile(r'[\(\[\{][^\)\]\}]*[\)\]\}]')
_NONWORD = re.compile(r'[^a-z0-9]+')


def fold(s):
    """Lowercase, unaccented, punctuation-free. The comparison alphabet."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return _NONWORD.sub(' ', s.lower()).strip()


def base_title(title):
    """The song, without the version. "Skyfall (Extended Mix)" -> "skyfall"."""
    t = _BRACKETS.sub(' ', str(title or ''))
    t = _FEAT.sub(' ', t)
    t = fold(t)
    # Trailing version words survive the bracket strip when nobody used any.
    words = t.split()
    while words and ' '.join(words[-2:]) in VERSION_WORDS:
        words = words[:-2]
    while words and words[-1] in VERSION_WORDS:
        words = words[:-1]
    return ' '.join(words) or t


def version_of(title):
    """Just the version marker, if the title carries one."""
    inside = ' '.join(_BRACKETS.findall(str(title or '')))
    inside = fold(inside)
    hits = [w for w in VERSION_WORDS if w in inside]
    return max(hits, key=len) if hits else ''


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _token_containment(a, b):
    """How much of the SHORTER side appears in the longer one.

    Right for ARTISTS, where extra names are normal: "Catz N Dogz" against
    "Catz 'n Dogz, Nala" reads badly letter-by-letter, yet every word of the
    first is in the second and it is plainly the same act.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _token_jaccard(a, b):
    """Shared words over ALL the words. Right for TITLES.

    ⚠ Containment must NOT be used here. It divides by the shorter side, so a
    one-word title scores 100% inside any title containing that word: playing
    "Back To Basics" matched a library track called "Back" at 77% and claimed
    you already owned it. A title is the whole title.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


#: How alike two titles must be before an artist match is allowed to speak.
#: Calibrated on the real failure ("rockstar" vs "postman" = 0.53) and the
#: real near-misses that must survive ("freak"/"freaks" = 0.91).
TITLE_FLOOR = 0.62

#: The same bar with no artist on one side, where the title stands alone.
TITLE_FLOOR_BLIND = 0.85


def similarity(title_a, artist_a, title_b, artist_b):
    """0..1 — how much these two look like the same recording."""
    bt_a, bt_b = base_title(title_a), base_title(title_b)
    ar_a, ar_b = fold(artist_a), fold(artist_b)

    t = max(_ratio(bt_a, bt_b), _token_jaccard(bt_a, bt_b))
    a = (max(_ratio(ar_a, ar_b), _token_containment(ar_a, ar_b))
         if (ar_a and ar_b) else 0.0)

    # The title carries the weight: an artist is often written three ways, but
    # if the titles do not match you are simply looking at another song.
    score = 0.72 * t + 0.28 * a if (ar_a and ar_b) else t

    # ⚠ Tolerating VARIATION is not the same as tolerating CONTRADICTION.
    # With those weights an identical title and a completely unrelated artist
    # still scored 0.72 — above every floor in the app — so "Move Your Body"
    # by Öwnboss came back as "already yours: Cara Elizabeth — Move Your
    # Body". Titles repeat constantly across dance music; that is the one case
    # where the artist has to be able to say no.
    # ⚠ The test is WORDS, not characters. Two short names always share some
    # letters — "biscits" against "doja cat" scores 0.27 on characters, enough
    # to slip through a character gate — while sharing a word is exactly what
    # the same artist written differently does: "Sia" inside "David Guetta ft
    # Sia" is 1.00, "Alan Walker" inside "Alan Walker & Ava Max" is 1.00, and
    # two different people are 0.00. The character ratio stays as the way out
    # for a misspelling that shares no whole word.
    if ar_a and ar_b and _token_containment(ar_a, ar_b) == 0 \
            and _ratio(ar_a, ar_b) < 0.6:
        score = min(score, 0.45)

    # ⚠ And the same disease on the TITLE side, which the artist gate cannot
    # catch because the artist is right: "HIGHLITE — Rockstar" was reported as
    # your "Highlite — Postman (Extended Mix)" at 0.66, because character
    # similarity on two short words is noise ("rockstar" against "postman"
    # scores 0.53) and a perfect artist match then carried it over the floor.
    # An artist agreeing cannot make two different songs the same song, so the
    # title has to clear a bar of its own.
    # ⚠ Neither measure works alone here, which is why this is a floor on
    # their max and not a word test: "freak"/"freaks" share no whole word and
    # are the same record (0.91 on characters), while "alone"/"alone extended"
    # scores only 0.53 on characters and is also the same record (the word
    # test carries it). Below TITLE_FLOOR both agree there is nothing there.
    # ⚠ And higher still when there is no artist to corroborate it. "You
    # already own this" is a strong claim, and with one side missing an artist
    # the title is the only evidence there is: "Move It (Extended Mix)"
    # against "I Love It" scores 0.75 on characters, which is plenty to fool a
    # 0.62 bar and tell you not to bother downloading a record you do not have.
    # ⚠ …with one way through it that characters cannot express: one title
    # whose words are ALL inside the other. "Born Slippy" against "Born Slippy
    # .NUXX" is 0.70 on characters and would be thrown out, and it is plainly
    # the same record; "Move It" against "I Love It" shares only "it", which
    # is half its words, and is plainly not. Full containment is the line.
    blind_ok = (not (ar_a and ar_b)) and _token_containment(bt_a, bt_b) >= 0.99
    bar = TITLE_FLOOR if (ar_a and ar_b) else TITLE_FLOOR_BLIND
    if t < bar and not blind_ok:
        score = min(score, 0.45)

    # Exactly the same base title AND artist: lift it clear of the near-misses.
    if bt_a and bt_a == bt_b and ar_a and ar_a == ar_b:
        score = max(score, 0.99)
    return round(min(1.0, score), 3)


def best_matches(title, artist, tracks, limit=5, floor=0.5):
    """The library tracks most like this one, best first.

    `tracks` are the view rows (id, title, artist, …). Each result carries the
    score and whether the VERSION differs, which is the usual reason you own
    something and still want the other one.
    """
    want_ver = version_of(title)
    out = []
    for t in tracks:
        s = similarity(title, artist, t.get('title'), t.get('artist'))
        if s < floor:
            continue
        have_ver = version_of(t.get('title'))
        out.append({'track': t, 'score': s,
                    'same_version': want_ver == have_ver,
                    'their_version': have_ver, 'your_version': want_ver})
    out.sort(key=lambda r: (-r['score'], not r['same_version']))
    return out[:limit]


def search(tracks, query, limit=60):
    """Free-text browse: rank the library against whatever you typed.

    Substring hits come first — when you type half a title you mean that
    title — and fuzzy ones fill in behind, so a typo still finds it.
    """
    q = fold(query)
    if not q:
        return []
    qt = [w for w in q.split() if w]
    rows = []
    for t in tracks:
        hay = fold('%s %s %s' % (t.get('title') or '', t.get('artist') or '',
                                 ' '.join(t.get('genres') or [])))
        if not hay:
            continue
        exact = all(w in hay for w in qt)
        # ⚠ Score against the title AND the artist separately: you search for
        # either, and comparing only against the title meant a misspelt artist
        # ("berrin" for Berin) found nothing at all.
        s = max(similarity(query, '', t.get('title'), ''),
                similarity(query, '', t.get('artist'), ''))
        # ⚠ A fuzzy hit must SHARE A WORD with what you typed, or be a near
        # spelling of it. Letter-level similarity alone is meaningless on short
        # strings — "catz dogz" scored 56% against "Calm Down" and filled the
        # results with things that have nothing to do with the query. A typo
        # ("skyfal") still gets through on the ratio.
        shares = any(len(w) >= 3 and w in hay for w in qt)
        close = s >= 0.8
        if not exact and not (shares and s >= 0.5) and not close:
            continue
        rows.append({'track': t, 'score': round(1.0 if exact else s, 3),
                     'exact': exact})
    rows.sort(key=lambda r: (not r['exact'], -r['score'],
                             (r['track'].get('title') or '').lower()))
    return rows[:limit]
