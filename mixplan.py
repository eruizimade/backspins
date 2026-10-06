"""Where two tracks should be joined, and by how much to bend the tempo.

Everything needed is already on disk, computed by rekordbox when it analysed
the library: a BEAT GRID for 99% of tracks (PQTZ, in milliseconds) and a
PHRASE map for 92% (PSSI — where the intro ends, where the choruses are,
where the outro starts). Add the tempo and the key, which are on everything,
and a transition is arithmetic rather than opinion.

What this works out, for a pair:

  · the OUT point in the outgoing track — the start of a phrase near the end,
    so the blend begins where the music says a section begins and not at an
    arbitrary "thirty seconds before the end";
  · the IN point in the incoming one — after its intro, unless its intro is
    long enough to be worth playing under the outgoing track;
  · the tempo ratio, and whether it is small enough to be honest;
  · how many bars the two can overlap without either of them changing section
    underneath the other.

⚠ It plans, it does not play. Two very different things can act on this plan —
hearing it in the browser, or rendering the set to a file — and they have
opposite compromises about pitch. Keeping the arithmetic in one place means
they can never disagree about where the mix is.

⚠ The plan is only as good as rekordbox's grid. A track whose beat grid was
never corrected will be planned confidently and wrongly; that is a property
of the grid, not of this file, and it is why `confidence` says out loud when
a track has no phrases or a suspicious grid.
"""

import os

#: A blend shorter than this is a cut, longer than that is a mess.
BARS_MIN, BARS_MAX = 8, 64
BARS_DEFAULT = 32

#: Past this the pitch shift of a naive playback-rate change is audible, and
#: the harmonic work of matching keys is undone by it.
TEMPO_COMFORTABLE = 0.03
TEMPO_LIMIT = 0.08


def beat_seconds(anlz_dat):
    """The beat grid in SECONDS. ⚠ rekordbox stores it in milliseconds."""
    import phrases as ph
    try:
        return [b / 1000.0 for b in (ph._beat_times(anlz_dat) or [])]
    except Exception:
        return []


def phrase_beats(anlz_dat):
    """[(kind, beat)] from the .EXT beside the .DAT, oldest first."""
    import phrases as ph
    ext = os.path.splitext(anlz_dat)[0] + '.EXT'
    if not os.path.exists(ext):
        return []
    try:
        _mood, entries = ph._phrases(ext)
    except Exception:
        return []
    return [(e.get('kind'), e.get('beat') or 0) for e in (entries or [])]


def _phrase_starts(anlz_dat, grid):
    """Where each phrase begins, in seconds."""
    out = []
    for kind, beat in phrase_beats(anlz_dat):
        i = max(0, min(len(grid) - 1, beat - 1))
        if grid:
            out.append((kind, grid[i]))
    return out


def out_point(grid, phrases, bars, bpm):
    """Where the outgoing track should start handing over.

    The last phrase boundary that still leaves room for the whole blend. With
    no phrases, fall back to a whole number of bars from the end — still on
    the grid, just without the music's opinion.
    """
    if not grid:
        return None, 'no grid'
    span = bars * 4 * 60.0 / (bpm or 125.0)
    latest = grid[-1] - span
    if latest <= 0:
        return max(0.0, grid[-1] - span), 'track too short for that many bars'
    marks = [t for _k, t in phrases if t <= latest]
    if marks:
        return marks[-1], 'on a phrase'
    beats_from_end = int(span / (60.0 / (bpm or 125.0)))
    i = max(0, len(grid) - beats_from_end - 1)
    return grid[i], 'on the grid, no phrase there'


def in_point(grid, phrases):
    """Where the incoming track should start.

    ⚠ Its first beat, not the end of its intro. An intro is written to be
    played under the previous record — skipping it throws away the part of
    the track that was built for exactly this moment. The exception is an
    intro so long it would outlast the blend, and that is the caller's
    decision to make with `intro_bars`.
    """
    if not grid:
        return 0.0, 0
    body = [t for k, t in phrases if k not in (1,)]     # 1 = intro
    intro_secs = (body[0] - grid[0]) if body else 0.0
    return grid[0], intro_secs


def plan(a, b, bars=BARS_DEFAULT):
    """A transition from `a` to `b`. Both are dicts:

        {bpm, key, seconds, anlz}   anlz = path of the .DAT

    Returns what to do, in seconds, plus how much to trust it.
    """
    bars = max(BARS_MIN, min(BARS_MAX, int(bars or BARS_DEFAULT)))
    ga, gb = beat_seconds(a.get('anlz') or ''), beat_seconds(b.get('anlz') or '')
    pa = _phrase_starts(a.get('anlz') or '', ga)
    pb = _phrase_starts(b.get('anlz') or '', gb)

    bpm_a = float(a.get('bpm') or 0) or 125.0
    bpm_b = float(b.get('bpm') or 0) or bpm_a
    ratio = bpm_a / bpm_b if bpm_b else 1.0        # what B must be bent BY
    drift = abs(ratio - 1.0)

    out_at, why_out = out_point(ga, pa, bars, bpm_a)
    in_at, intro_secs = in_point(gb, pb)
    blend = bars * 4 * 60.0 / bpm_a

    notes = []
    if not ga:
        notes.append('the outgoing track has no beat grid')
    if not gb:
        notes.append('the incoming track has no beat grid')
    if not pa:
        notes.append('no phrases in the outgoing track')
    if drift > TEMPO_LIMIT:
        notes.append('%.1f%% of tempo is too far to bend' % (drift * 100))
    elif drift > TEMPO_COMFORTABLE:
        notes.append('%.1f%% of tempo — audible if the pitch is not locked'
                     % (drift * 100))
    if intro_secs > blend:
        notes.append('its intro (%ds) outlasts the blend' % int(intro_secs))

    ok = bool(ga and gb) and drift <= TEMPO_LIMIT
    return {
        'outAt': round(out_at, 2) if out_at is not None else None,
        'inAt': round(in_at, 2),
        'bars': bars,
        'blendSeconds': round(blend, 2),
        'tempoRatio': round(ratio, 5),
        'tempoPercent': round((ratio - 1.0) * 100, 2),
        'introSeconds': round(intro_secs, 1),
        'why': why_out,
        'notes': notes,
        'usable': ok,
    }
