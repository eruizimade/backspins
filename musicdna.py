"""What is actually IN a track: its notes, its key and scale, its chords.

The Teach screen used to learn from tempo, a waveform statistic and your own
tags — and the honest ceiling it measured was the features: twenty answers
were as good as a hundred and sixty, because nothing in those numbers could
tell a Phrygian acid line from a major-key house piano. This reads the audio
itself and keeps what a musician would describe:

  · NOTES — how much of the track sits on each of the twelve pitch classes,
    for the whole mix and for the bass alone (the bass names the chord in
    dance music far more often than the pads do).
  · KEY and SCALE — the tonic and major/minor from the note profile, then
    which mode fits best once the tonic is known: Aeolian, Dorian, Phrygian,
    harmonic minor, the pentatonics… the colour a DJ hears as "dark" or
    "deep" before knowing why.
  · CHORDS — one per beat from rekordbox's own beat grid, smoothed so a hi-hat
    cannot change the harmony, then folded into the loop the track keeps
    returning to and written as degrees of the key (i – ♭VI – ♭III – ♭VII).
  · TIMBRE, RHYTHM, DYNAMICS — the classic descriptors (MFCCs, spectral
    shape, band balance, how percussive, how syncopated, how much it breathes)
    that a model needs to hear "acid" or "vocal" or "raw".

⚠ numpy only, and ffmpeg (which the app already needs) to decode. No model
downloads, no compiled audio libraries: it installs anywhere the app does and
gives the same answer on every machine.

⚠ Chord and scale reading on a full mix is approximate, and the screen must
say so. Drums smear the spectrum, overtones invent notes nobody played, and a
track built on one droning chord has no progression to find. The key read
from the notes is checked against the one already in your library (Mixed In
Key or rekordbox) and the agreement is shown on the Teach screen: 79% across
the 2,521 keyed tracks here, 81% on tracks the key profiles were not learned
from. Where the library has a key, that one is used to count scale degrees
and chords from; the heard key is kept beside it as a second opinion.

Run as a script it analyses a batch in parallel and writes the cache; the
server drives it that way so the work never competes with the UI for a core.
"""

import json
import math
import os
import subprocess
import sys
import time

import numpy as np

try:
    import settings
    CACHE = os.path.join(settings.APP_DIR, 'dna.json')
except Exception:                                   # run from elsewhere
    CACHE = os.path.expanduser('~/.config/rekordbox-toolkit/dna.json')

#: Bump when the analysis changes: older entries are then re-read.
VERSION = 4

SR = 22050
N_FFT = 8192          # 2.7 Hz per bin: enough to tell semitones apart from ~60 Hz up
HOP = 2048            # 93 ms
N_FFT_FAST = 1024     # the onset pass: 23 ms, where the drums live
HOP_FAST = 512
MAX_SECONDS = 12 * 60

PCS = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
SHARPS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
FLATS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B']
#: Tonic names the way DJ software prints them.
MAJOR_NAMES = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
MINOR_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'G#', 'A', 'Bb', 'B']
#: Keys written with sharps; everything else is spelt with flats.
SHARP_KEYS = {('major', t) for t in (7, 2, 9, 4, 11, 6)} | \
             {('minor', t) for t in (4, 11, 6, 1, 8)}

# ── key profiles ─────────────────────────────────────────────────────────
# ⚠ LEARNED, not taken from a textbook. Measured against 590 tracks of this
# library keyed by Mixed In Key, the published profiles agreed 41-44% of the
# time (Krumhansl-Kessler, Temperley); profiles averaged from the library's
# own keys agree 81% on tracks they were not learned from (four random
# halves: 78, 81, 83, 81). Dance music leans on the tonic, the fifth and the
# minor seventh far more than the classical corpora those were built from.
# Read from the notes between 220 Hz and 1.8 kHz, where chords and hooks live;
# adding the bass or the whole range measured slightly worse.
PROFILE_MAJOR = np.array([1.000, 0.534, 0.836, 0.531, 0.953, 0.649,
                          0.553, 0.984, 0.470, 0.690, 0.580, 0.690])
PROFILE_MINOR = np.array([1.000, 0.496, 0.764, 0.722, 0.636, 0.660,
                          0.460, 0.970, 0.529, 0.532, 0.704, 0.517])
#: 94% of this library is in a minor key; a close call goes that way.
MINOR_PRIOR = 0.10

#: The modes, told apart by the notes where they differ, counted from the
#: tonic. A track is called Phrygian when its flat second is stronger,
#: against its natural second, than in nine of ten minor tracks here — RELATIVE
#: to the library, because overtones make every track "contain" a little of
#: every note and an absolute threshold would name nothing or everything.
#: (mean, spread) of each contrast across the library, by mode.
MODE_CONTRAST = {
    'minor': {'b2': (-0.0336, 0.0259), 'b6': (-0.0003, 0.0198),
              'b7': (0.0234, 0.0242)},
    'major': {'b7': (-0.0130, 0.0313), '#4': (-0.0114, 0.0267)},
}
MODE_Z = 1.25           # ~ the most marked tenth

#: The notes of each scale, counted from the tonic — for drawing it.
SCALES = {
    'Aeolian': (0, 2, 3, 5, 7, 8, 10), 'Dorian': (0, 2, 3, 5, 7, 9, 10),
    'Phrygian': (0, 1, 3, 5, 7, 8, 10), 'Harmonic minor': (0, 2, 3, 5, 7, 8, 11),
    'Ionian': (0, 2, 4, 5, 7, 9, 11), 'Mixolydian': (0, 2, 4, 5, 7, 9, 10),
    'Lydian': (0, 2, 4, 6, 7, 9, 11),
}

#: Roman numerals by semitones above the tonic, against the MAJOR scale — so
#: a minor key reads i – ♭VI – ♭III – ♭VII, the way it is usually written.
NUMERALS = ['I', '♭II', 'II', '♭III', 'III', 'IV', '♭V', 'V', '♭VI', 'VI', '♭VII', 'VII']


# ── reading the file ─────────────────────────────────────────────────────

def decode(path, sr=SR, seconds=MAX_SECONDS):
    """Mono float32 at `sr`, straight from ffmpeg. None if it cannot be read."""
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
           '-i', path, '-map', '0:a:0', '-t', str(seconds),
           '-ac', '1', '-ar', str(sr), '-f', 'f32le', 'pipe:1']
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=240)
    except Exception:
        return None
    if out.returncode or not out.stdout:
        return None
    return np.frombuffer(out.stdout, dtype=np.float32)


def _frames(y, n, hop):
    if y.size < n:
        y = np.pad(y, (0, n - y.size))
    return np.lib.stride_tricks.sliding_window_view(y, n)[::hop]


def _spectrogram(y, n, hop, chunk=384):
    """|STFT|, frames × bins, float32 — in chunks, so a long track never
    holds the complex spectrum of the whole thing in memory at once."""
    fr = _frames(y, n, hop)
    win = np.hanning(n).astype(np.float32)
    out = np.empty((fr.shape[0], n // 2 + 1), dtype=np.float32)
    for i in range(0, fr.shape[0], chunk):
        out[i:i + chunk] = np.abs(np.fft.rfft(fr[i:i + chunk] * win, axis=1))
    return out


# ── filterbanks ──────────────────────────────────────────────────────────

_BANKS = {}


def _chroma_bank(lo, hi, n=N_FFT, sr=SR):
    """bins × 12. Each bin votes for its nearest semitone, more strongly the
    closer it sits to that semitone's centre (leakage between two notes then
    counts for little)."""
    key = ('c', lo, hi, n, sr)
    if key in _BANKS:
        return _BANKS[key]
    f = np.arange(n // 2 + 1) * sr / float(n)
    m = np.zeros((f.size, 12), dtype=np.float32)
    ok = (f >= lo) & (f <= hi)
    midi = 69 + 12 * np.log2(np.where(ok, f, 1.0) / 440.0)
    near = np.round(midi)
    w = np.cos(np.pi * (midi - near)) ** 2
    pc = (near.astype(int) % 12)
    m[ok, pc[ok]] = w[ok]
    _BANKS[key] = m
    return m


def _mel_bank(n_mels, n, sr, fmin=20.0, fmax=None):
    key = ('m', n_mels, n, sr, fmin, fmax)
    if key in _BANKS:
        return _BANKS[key]
    fmax = fmax or sr / 2.0
    mel = lambda hz: 2595.0 * np.log10(1.0 + hz / 700.0)
    hz = lambda m: 700.0 * (10 ** (m / 2595.0) - 1.0)
    pts = hz(np.linspace(mel(fmin), mel(fmax), n_mels + 2))
    f = np.arange(n // 2 + 1) * sr / float(n)
    bank = np.zeros((f.size, n_mels), dtype=np.float32)
    for i in range(n_mels):
        a, b, c = pts[i], pts[i + 1], pts[i + 2]
        up = (f - a) / max(b - a, 1e-9)
        down = (c - f) / max(c - b, 1e-9)
        bank[:, i] = np.maximum(0, np.minimum(up, down))
    # Slaney-style: every band has the same area, so wide ones do not dominate.
    bank *= (2.0 / np.maximum(pts[2:] - pts[:-2], 1e-9))[None, :]
    _BANKS[key] = bank
    return bank


def _mel_centres(n_mels, sr, fmin=20.0):
    mel = lambda hz: 2595.0 * np.log10(1.0 + hz / 700.0)
    pts = 700.0 * (10 ** (np.linspace(mel(fmin), mel(sr / 2.0), n_mels + 2) / 2595.0) - 1.0)
    return pts[1:-1]


def _dct(n_out, n_in):
    key = ('d', n_out, n_in)
    if key not in _BANKS:
        k = np.arange(n_out)[:, None]
        i = np.arange(n_in)[None, :]
        d = np.cos(np.pi / n_in * (i + 0.5) * k) * math.sqrt(2.0 / n_in)
        d[0] /= math.sqrt(2.0)
        _BANKS[key] = d.astype(np.float32)
    return _BANKS[key]


def _peaks_only(mag, width=25):
    """Keep what stands above its neighbourhood in frequency.

    ⚠ This is what makes the notes readable under a kick drum. A drum hit is
    broadband — it lifts every bin at once — while a note is a narrow peak.
    Subtracting a running average across frequency leaves the peaks and drops
    the wash, so the pitch classes describe what was played rather than how
    loud the percussion was. Measured: without it the key agreed with the
    library on 16-22% of tracks; with it, 81%.
    """
    lg = np.log1p(mag * np.float32(50.0))
    pad = width // 2
    p = np.pad(lg, ((0, 0), (pad + 1, pad)), mode='edge')
    c = np.cumsum(p, axis=1, dtype=np.float32)
    avg = (c[:, width:] - c[:, :-width]) / np.float32(width)
    return np.maximum(0.0, lg - avg[:, :lg.shape[1]])


# ── key and scale ────────────────────────────────────────────────────────

def _corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum()) * float((b * b).sum())) or 1.0
    return float((a * b).sum()) / d


def find_key(profile):
    """(tonic 0-11, 'major'|'minor', clarity, margin) from a 12-value profile."""
    best = []
    for t in range(12):
        r = np.roll(profile, -t)
        best.append((_corr(r, PROFILE_MAJOR), t, 'major'))
        best.append((_corr(r, PROFILE_MINOR) + MINOR_PRIOR, t, 'minor'))
    best.sort(reverse=True)
    (c1, t1, m1), (c2, _, _) = best[0], best[1]
    if m1 == 'minor':
        c1 -= MINOR_PRIOR
    return t1, m1, c1, c1 - c2


def find_scale(profile, tonic, mode):
    """The mode, from the notes where the modes differ. -> (name, colour, z)

    `colour` names the note that gives it away (♭2 for Phrygian), `z` how
    marked it is against the rest of the library — the screen shows it, so a
    borderline call looks borderline.
    """
    r = np.roll(profile, -tonic)
    d = {'b2': r[1] - r[2], 'b6': r[8] - r[9], 'b7': r[10] - r[11],
         '#4': r[6] - r[5]}
    z = {k: (d[k] - m) / s for k, (m, s) in MODE_CONTRAST[mode].items()}
    if mode == 'minor':
        flags = [('Phrygian', '♭2', z['b2']),
                 ('Dorian', '6', -z['b6']),
                 ('Harmonic minor', '7', -z['b7'])]
        plain = ('Aeolian', '', 0.0)
    else:
        flags = [('Mixolydian', '♭7', z['b7']),
                 ('Lydian', '♯4', z['#4'])]
        plain = ('Ionian', '', 0.0)
    flags.sort(key=lambda f: -f[2])
    best = flags[0] if flags[0][2] >= MODE_Z else plain
    return best[0], best[1], round(float(best[2] if best is flags[0] else flags[0][2]), 2)


_NOTE = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}


def camelot_to_key(code):
    """A key as the library writes it -> (tonic, 'major'|'minor'), or None.

    Camelot ('8A') is what this library mostly holds; a few hundred tracks
    carry the musical name instead ('Am', 'Dbm', 'Fmaj', 'F#').
    """
    code = str(code or '').strip()
    if not code:
        return None
    # Camelot with M/m for major/minor ('7M', '12m'), as some taggers write it.
    if code[:-1].isdigit() and code[-1] in 'Mm':
        code = code[:-1] + ('B' if code[-1] == 'M' else 'A')
    up = code.upper()
    if up[:-1].isdigit() and up[-1] in 'AB':
        n = int(up[:-1])
        if not 1 <= n <= 12:
            return None
        if up[-1] == 'B':
            return ((7 * (n - 8)) % 12, 'major')
        return ((9 + 7 * (n - 8)) % 12, 'minor')
    if code[0].upper() not in _NOTE:
        return None
    t, rest = _NOTE[code[0].upper()], code[1:]
    if rest[:1] in ('#', '♯'):
        t, rest = t + 1, rest[1:]
    elif rest[:1] in ('b', '♭'):
        t, rest = t - 1, rest[1:]
    rest = rest.strip().lower()
    if rest in ('m', 'min', 'minor'):
        return (t % 12, 'minor')
    if rest in ('', 'maj', 'major'):
        return (t % 12, 'major')
    return None


def key_to_camelot(tonic, mode):
    base = 0 if mode == 'major' else 9
    n = ((tonic - base) * 7) % 12          # 7 is its own inverse mod 12
    n = (n + 8 - 1) % 12 + 1
    return '%d%s' % (n, 'B' if mode == 'major' else 'A')


def key_name(tonic, mode):
    return MINOR_NAMES[tonic] + 'm' if mode == 'minor' else MAJOR_NAMES[tonic]


def speller(tonic, mode):
    return SHARPS if (mode, tonic) in SHARP_KEYS else FLATS


# ── chords ───────────────────────────────────────────────────────────────

def _chord_templates():
    rows, names = [], []
    for root in range(12):
        for q, third in (('', 4), ('m', 3)):
            t = np.zeros(12)
            t[root] = 1.35             # the root carries more weight than the fifth
            t[(root + 7) % 12] = 1.0
            t[(root + third) % 12] = 0.8
            rows.append(t / np.linalg.norm(t))
            names.append((root, q))
    return np.array(rows), names


TEMPLATES, CHORDS = _chord_templates()


def diatonic(tonic, mode):
    """The chords that belong to the key: its triads, plus the major V a
    minor key borrows from harmonic minor."""
    sc = SCALES['Aeolian'] if mode == 'minor' else SCALES['Ionian']
    out = set()
    for i, (root, q) in enumerate(CHORDS):
        third = 3 if q == 'm' else 4
        if all(((root + x - tonic) % 12) in sc for x in (0, third, 7)):
            out.add(i)
    if mode == 'minor':
        out.add(CHORDS.index(((tonic + 7) % 12, '')))
    return out


def chords_from(beat_chroma, beat_bass, tonic=None, mode=None, stay=0.85):
    """One chord per beat, Viterbi-smoothed. Returns [index or -1 for none].

    ⚠ The smoothing is the point: frame by frame a hi-hat or a vocal ad-lib
    changes the "chord" every half second. Real harmony moves rarely, so a
    change has to pay for itself across several beats before it is believed.

    ⚠ The key nudges the choice towards chords that belong to it. Without
    that, a third of the minor tracks here came out with a MAJOR tonic chord:
    the fifth overtone of every bass note is a major third, so the spectrum
    itself argues for major. With the nudge it is 1%, and the chord a track
    sits on most is its tonic 73% of the time — what dance music actually does.
    """
    n = beat_chroma.shape[0]
    if not n:
        return []
    x = beat_chroma / (np.linalg.norm(beat_chroma, axis=1, keepdims=True) + 1e-9)
    b = beat_bass / (beat_bass.sum(axis=1, keepdims=True) + 1e-9)
    sim = x @ TEMPLATES.T                                   # beats × 24
    roots = np.array([r for r, _q in CHORDS])
    sim = sim + 0.3 * b[:, roots]                           # the bass names the root
    if tonic is not None:
        bonus = np.zeros(len(CHORDS))
        bonus[list(diatonic(tonic, mode))] = 0.08
        sim = sim + bonus
    strength = np.linalg.norm(beat_chroma, axis=1)
    quiet = strength < 0.25 * (np.median(strength) + 1e-9)
    # A "no chord" state for drum-only stretches.
    sim = np.concatenate([sim, np.full((n, 1), 0.55)], axis=1)
    sim[quiet, -1] = 2.0
    logp = sim * 9.0
    logp = logp - logp.max(axis=1, keepdims=True)
    logp = logp - np.log(np.exp(logp).sum(axis=1, keepdims=True))
    k = sim.shape[1]
    trans = np.full((k, k), math.log((1 - stay) / (k - 1)))
    np.fill_diagonal(trans, math.log(stay))
    score = logp[0].copy()
    back = np.zeros((n, k), dtype=np.int16)
    for i in range(1, n):
        cand = score[:, None] + trans
        back[i] = cand.argmax(axis=0)
        score = cand.max(axis=0) + logp[i]
    path = [int(score.argmax())]
    for i in range(n - 1, 0, -1):
        path.append(int(back[i, path[-1]]))
    path.reverse()
    return [p if p < len(CHORDS) else -1 for p in path]


def chord_name(i, names=None):
    if i is None or i < 0:
        return 'N'
    r, q = CHORDS[i]
    return (names or PCS)[r] + q


def numeral(i, tonic):
    if i is None or i < 0:
        return '–'
    r, q = CHORDS[i]
    s = NUMERALS[(r - tonic) % 12]
    return s.lower() if q == 'm' else s


def _fold(pattern):
    """Am Am F F C C G G -> [[Am, 2], [F, 2], [C, 2], [G, 2]], wrapping round."""
    runs = []
    for c in pattern:
        if runs and runs[-1][0] == c:
            runs[-1][1] += 1
        else:
            runs.append([c, 1])
    if len(runs) > 1 and runs[0][0] == runs[-1][0]:
        runs[0][1] += runs.pop()[1]
    return runs


def _canon(runs, tonic):
    """The same loop heard from a different bar is the same loop: start it on
    the tonic chord when it has one, otherwise on its longest chord."""
    if not runs:
        return ()
    k = len(runs)
    start = next((i for i, (c, _n) in enumerate(runs) if CHORDS[c][0] == tonic), None)
    if start is None:
        start = max(range(k), key=lambda i: (runs[i][1], -i))
    return tuple(tuple(runs[(start + i) % k]) for i in range(k))


def progression(bars, tonic, mode='minor', window=16):
    """The loop a track keeps coming back to, from one chord per bar.

    Dance music changes harmony by SECTION — the breakdown has the chords, the
    drop often sits on one — so the loop is looked for phrase by phrase: in
    every 16 bars, the 2-, 4- or 8-bar pattern that fits best (each position's
    most common chord), kept if it fits at least three bars in four and has
    any movement in it. The loop found in the most phrases wins.

    ⚠ A track that never moves is honestly reported as one chord. Inventing a
    progression for a techno drone would be the screen lying to you.
    """
    seq = list(bars)
    real = [c for c in seq if c >= 0]
    if len(real) < 4:
        return {'loop': [], 'bars': 0, 'coverage': 0.0, 'moves': False}
    found = {}
    for w0 in range(0, max(1, len(seq) - window + 1), window // 2):
        win = seq[w0:w0 + window]
        if sum(1 for c in win if c >= 0) < window * 0.75:
            continue
        best = None
        for L in (2, 4, 8):
            pat = []
            for k in range(L):
                col = [win[j] for j in range(k, len(win), L) if win[j] >= 0]
                pat.append(max(set(col), key=col.count) if col else -1)
            if -1 in pat or len(set(pat)) < 2:
                continue
            fit = sum(1 for j, c in enumerate(win) if c == pat[j % L]) / float(len(win))
            # ⚠ A longer pattern always fits at least as well — it has more
            # freedom — so it has to fit clearly better to be preferred.
            if fit >= 0.75 and (best is None or fit > best[0] + 0.08):
                best = (fit, L, pat)
        if best:
            key = _canon(_fold(best[2]), tonic)
            f = found.setdefault(key, [0, 0.0, best[1]])
            f[0] += 1
            f[1] += best[0]
    names = speller(tonic, mode)
    total = max(1, (len(seq) - window) // (window // 2) + 1)
    if found:
        key, (hits, fitsum, L) = max(found.items(),
                                     key=lambda kv: (kv[1][0], kv[1][1]))
        # ⚠ Heard in a single phrase it is a passage, not the track's loop:
        # one breakdown is not a progression the track is built on.
        if hits >= 2:
            return {'loop': [{'chord': chord_name(c, names), 'num': numeral(c, tonic),
                              'bars': n} for c, n in key],
                    'bars': sum(n for _c, n in key),
                    'coverage': round(min(1.0, hits / float(total)), 2),
                    'moves': True}
    top = max(set(real), key=real.count)
    return {'loop': [{'chord': chord_name(top, names), 'num': numeral(top, tonic), 'bars': 1}],
            'bars': 1, 'coverage': round(real.count(top) / float(len(real)), 2),
            'moves': False}


# ── the whole analysis ───────────────────────────────────────────────────

def _sync(frames_feat, frame_t, edges):
    """Average frame features between consecutive time edges."""
    idx = np.searchsorted(frame_t, edges)
    out = np.zeros((len(edges) - 1, frames_feat.shape[1]), dtype=np.float32)
    for i in range(len(edges) - 1):
        a, b = idx[i], max(idx[i] + 1, idx[i + 1])
        out[i] = frames_feat[a:b].mean(axis=0) if a < frames_feat.shape[0] else 0
    return out


def analyse(path, beats=None, bpm=None, known_key=None):
    """Everything this module knows about one file, as a plain dict.

    `beats` is rekordbox's beat grid in seconds when there is one; without it
    the beats are laid out from `bpm`, and without that every half second.
    `known_key` is the key already in the library (Camelot): when there is
    one, the scale and the chord degrees are counted from it — it was checked
    by a dedicated tool, and the key read here is kept beside it as a second
    opinion rather than overruling it.
    """
    y = decode(path)
    if y is None:
        return None
    if y.size < SR * 5:
        # Air horns, drops, one-shots: there is no key or groove to read.
        return {'error': 'shorter than 5 seconds — a sample, not a track'}
    dur = y.size / float(SR)
    mag = _spectrogram(y, N_FFT, HOP)
    t = (np.arange(mag.shape[0]) * HOP + N_FFT / 2) / float(SR)
    # ⚠ float32 and in place: a six-minute track is 4,500 × 4,097 values, and
    # twelve workers each copying that in float64 spent their time waiting on
    # memory rather than computing.
    power = np.square(mag)
    rms = np.sqrt(power.sum(axis=1, dtype=np.float64) / N_FFT) + 1e-9
    db = 20 * np.log10(rms)
    live = db > (np.percentile(db, 95) - 30)         # leave silence out of the averages

    # ── notes. Only the bins up to 1.8 kHz (plus the averaging margin) are
    # ever read for pitch, so only those are peak-picked.
    top_bin = int(1800.0 * N_FFT / SR) + 40
    pk = _peaks_only(mag[:, :top_bin])
    mid = pk @ _chroma_bank(220.0, 1760.0)[:top_bin]    # chords and hooks
    bass = pk @ _chroma_bank(40.0, 220.0)[:top_bin]     # what the bass says
    del pk
    lv = live[:, None].astype(np.float32)
    notes = (mid * lv).sum(axis=0)
    notes = notes / (notes.sum() or 1.0)
    bnotes = (bass * lv).sum(axis=0)
    bnotes = bnotes / (bnotes.sum() or 1.0)
    h_tonic, h_mode, clarity, margin = find_key(notes)
    known = camelot_to_key(known_key) if known_key else None
    tonic, mode = known if known else (h_tonic, h_mode)
    scale, colour, mark = find_scale(notes, tonic, mode)
    rel = np.roll(notes, -tonic)
    brel = np.roll(bnotes, -tonic)

    # ── beats, then chords
    if beats and len(beats) > 8:
        edges = np.array([b for b in beats if b < dur] + [dur])
    else:
        step = 60.0 / bpm if bpm else 0.5
        edges = np.arange(0.0, dur, step)
        edges = np.append(edges, dur)
    bc = _sync(mid, t, edges)
    bb = _sync(bass, t, edges)
    per_beat = chords_from(bc, bb, tonic, mode)
    # One chord per bar, by majority of its four beats.
    bars = []
    for i in range(0, len(per_beat) - 3, 4):
        grp = [c for c in per_beat[i:i + 4] if c >= 0]
        bars.append(max(set(grp), key=grp.count) if grp else -1)
    prog = progression(bars, tonic, mode)
    real = [c for c in bars if c >= 0]
    changes = sum(1 for a, b in zip(real, real[1:]) if a != b)
    names = speller(tonic, mode)
    timeline = []
    for i, c in enumerate(bars):
        if timeline and timeline[-1][1] == chord_name(c, names):
            continue
        at = float(edges[min(i * 4, len(edges) - 1)])
        timeline.append([round(at, 1), chord_name(c, names)])
    minor_share = (sum(1 for c in real if CHORDS[c][1] == 'm') / float(len(real))
                   if real else 0.0)
    tonic_share = (sum(1 for c in real if CHORDS[c][0] == tonic) / float(len(real))
                   if real else 0.0)
    dia = diatonic(tonic, mode)
    outside = (sum(1 for c in real if c not in dia) / float(len(real)) if real else 0.0)
    # How much the harmony moves bar to bar, whatever the chord labels say.
    bar_ch = _sync(mid, t, edges[::4]) if len(edges) > 8 else bc
    nrm = bar_ch / (np.linalg.norm(bar_ch, axis=1, keepdims=True) + 1e-9)
    motion = (float(np.mean(1.0 - (nrm[1:] * nrm[:-1]).sum(axis=1)))
              if nrm.shape[0] > 1 else 0.0)

    # ── timbre
    mel = power @ _mel_bank(40, N_FFT, SR)
    lmel = np.log10(mel + 1e-10)
    mfcc = lmel @ _dct(13, 40).T
    f = (np.arange(mag.shape[1]) * SR / float(N_FFT)).astype(np.float32)
    psum = power.sum(axis=1) + 1e-12
    centroid = (power @ f) / psum
    # Rolloff and flatness from the 40 mel bands: the same shape, a hundredth
    # of the work of reading every bin of every frame again.
    mel_hz = _mel_centres(40, SR)
    mcum = np.cumsum(mel, axis=1)
    rolloff = mel_hz[np.minimum((mcum < 0.85 * mcum[:, -1:]).sum(axis=1), 39)]
    flat = np.exp(np.log(mel + 1e-12).mean(axis=1)) / (mel.mean(axis=1) + 1e-12)
    bands = [(20, 60), (60, 250), (250, 500), (500, 2000), (2000, 4000),
             (4000, 6000), (6000, 11025)]
    spec = power[live].sum(axis=0, dtype=np.float64)
    tot = spec.sum() or 1.0
    band_share = [float(spec[(f >= a) & (f < b)].sum() / tot) for a, b in bands]
    del power, mag

    # ── rhythm, from a finer pass
    fm = _spectrogram(y, N_FFT_FAST, HOP_FAST)
    fmel = np.log1p(10.0 * (np.square(fm) @ _mel_bank(40, N_FFT_FAST, SR)))
    del fm
    flux = np.maximum(0.0, np.diff(fmel, axis=0)).sum(axis=1)
    flux = flux / (flux.mean() + 1e-9)
    ft = (np.arange(flux.size) * HOP_FAST + N_FFT_FAST) / float(SR)
    # Percussive versus sustained: median across time keeps what holds still
    # (pads, bass notes), median across frequency keeps what is a click.
    sub = fmel[::2]                                         # 46 ms, plenty here
    sv = np.lib.stride_tricks.sliding_window_view(
        np.pad(sub, ((8, 8), (0, 0)), mode='edge'), 17, axis=0)
    harm = np.median(sv, axis=2)
    pv = np.lib.stride_tricks.sliding_window_view(
        np.pad(sub, ((0, 0), (4, 4)), mode='edge'), 9, axis=1)
    perc = np.median(pv, axis=2)
    perc_share = float(perc.sum() / ((perc.sum() + harm.sum()) or 1.0))
    peaks = ((flux[1:-1] > flux[:-2]) & (flux[1:-1] >= flux[2:]) & (flux[1:-1] > 1.5))
    onset_rate = float(peaks.sum()) / dur
    # Where the onsets fall against the beat: on it, on the "and", or on the
    # sixteenths in between — a number for swing and syncopation.
    on_beat = off8 = off16 = 0.0
    if len(edges) > 8:
        for a, b in zip(edges[:-1], edges[1:]):
            L = b - a
            if L <= 0:
                continue
            i0, i1 = np.searchsorted(ft, [a, b])
            if i1 <= i0:
                continue
            pos = (ft[i0:i1] - a) / L
            fl = flux[i0:i1]
            on_beat += fl[(pos < 0.1) | (pos > 0.9)].sum()
            off8 += fl[np.abs(pos - 0.5) < 0.1].sum()
            off16 += fl[(np.abs(pos - 0.25) < 0.08) | (np.abs(pos - 0.75) < 0.08)].sum()
    hit = on_beat + off8 + off16 or 1.0
    # Pulse clarity: how strongly the onset curve repeats at the beat period.
    pulse = 0.0
    period = (60.0 / bpm) if bpm else None
    if period:
        lag = int(round(period * SR / HOP_FAST))
        fz = flux - flux.mean()
        den = float((fz * fz).sum()) or 1.0
        if 0 < lag < fz.size // 2:
            pulse = float((fz[:-lag] * fz[lag:]).sum()) / den

    # ── dynamics and shape
    short = np.convolve(db, np.ones(11) / 11.0, mode='same')     # ~1 s
    body = np.median(short[live]) if live.any() else np.median(short)
    breaks, run = 0, 0
    for v in short:
        if v < body - 6:
            run += 1
        else:
            if run * HOP / float(SR) >= 8:
                breaks += 1
            run = 0
    contour = []
    for i in range(32):
        a, b = int(i * len(short) / 32), int((i + 1) * len(short) / 32)
        contour.append(float(short[a:max(a + 1, b)].mean()))
    top = max(contour)
    contour = [round(max(0.0, 1 + (v - top) / 24.0), 3) for v in contour]

    r4 = lambda xs: [round(float(x), 4) for x in xs]
    return {
        'v': VERSION,
        'dur': round(dur, 1),
        # The key everything below is counted from, and where it came from.
        'key': {'tonic': tonic, 'mode': mode, 'name': key_name(tonic, mode),
                'camelot': key_to_camelot(tonic, mode),
                'from': 'library' if known else 'heard'},
        # What the notes alone say, kept even when the library had a key.
        'heard': {'tonic': h_tonic, 'mode': h_mode, 'name': key_name(h_tonic, h_mode),
                  'camelot': key_to_camelot(h_tonic, h_mode),
                  'clarity': round(clarity, 3), 'margin': round(margin, 3)},
        'scale': {'name': scale, 'colour': colour, 'mark': mark,
                  'notes': list(SCALES[scale])},
        'notes': r4(notes),
        'bass': r4(bnotes),
        'rel': r4(rel),
        'brel': r4(brel),
        'prog': prog,
        'chords': {'timeline': timeline[:400],
                   'distinct': len(set(real)),
                   'per_min': round(changes / (dur / 60.0), 2),
                   'minor': round(minor_share, 3),
                   'tonic': round(tonic_share, 3),
                   'outside': round(outside, 3),
                   'motion': round(motion, 4)},
        'timbre': {'mfcc': [round(float(x), 3) for x in mfcc[live].mean(axis=0)],
                   'mfcc_sd': [round(float(x), 3) for x in mfcc[live].std(axis=0)],
                   'centroid': round(float(centroid[live].mean()), 1),
                   'centroid_sd': round(float(centroid[live].std()), 1),
                   'rolloff': round(float(rolloff[live].mean()), 1),
                   'flat': round(float(flat[live].mean()), 4),
                   'bands': r4(band_share)},
        'rhythm': {'onsets': round(onset_rate, 2),
                   'perc': round(perc_share, 3),
                   'pulse': round(pulse, 3),
                   'on': round(float(on_beat / hit), 3),
                   'off8': round(float(off8 / hit), 3),
                   'off16': round(float(off16 / hit), 3),
                   'flux_sd': round(float(flux.std()), 3)},
        'dyn': {'loud': round(float(np.mean(db[live])), 2),
                'range': round(float(np.percentile(short, 95) - np.percentile(short, 10)), 2),
                'breaks': breaks,
                'contour': contour},
    }


# ── the cache ────────────────────────────────────────────────────────────

def signature(path):
    try:
        st = os.stat(path)
        return '%d:%d' % (st.st_size, int(st.st_mtime))
    except OSError:
        return None


_MEM = {'mtime': None, 'data': {}}


def load():
    """{track id: analysis}, re-read only when the file has changed."""
    try:
        m = os.path.getmtime(CACHE)
    except OSError:
        return {}
    if _MEM['mtime'] != m:
        try:
            with open(CACHE, encoding='utf-8') as fh:
                _MEM['data'] = json.load(fh) or {}
            _MEM['mtime'] = m
        except Exception:
            return _MEM['data']
    return _MEM['data']


def save(data):
    tmp = CACHE + '.part'
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, separators=(',', ':'))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CACHE)


def fresh(entry, path, key=None):
    """Still true for this file: same analysis, same file, same library key
    (the scale and the chord degrees are counted from that key)."""
    # ⚠ The key compared as a KEY, not as text: the library view writes
    # 'Em' as '9A', and comparing strings re-read sixty tracks for nothing.
    return bool(entry and entry.get('v') == VERSION
                and entry.get('sig') == signature(path)
                and camelot_to_key(entry.get('lk')) == camelot_to_key(key))


def plan(tracks, anlz=None):
    """The tracks that still need reading -> jobs for batch()."""
    data = load()
    jobs = []
    for t in tracks or []:
        tid, p = str(t.get('id')), t.get('path')
        if not p or not os.path.isfile(p):
            continue
        if fresh(data.get(tid), p, t.get('key')):
            continue
        jobs.append({'id': tid, 'path': p, 'bpm': t.get('bpm'),
                     'key': t.get('key') or '', 'anlz': (anlz or {}).get(tid)})
    return jobs


# ── batch: run as a separate process by the server ───────────────────────

def _one(job):
    sig = signature(job['path'])
    beats = None
    if job.get('anlz'):
        try:
            import mixplan
            beats = mixplan.beat_seconds(job['anlz'])
        except Exception:
            beats = None
    try:
        res = analyse(job['path'], beats, job.get('bpm'), job.get('key'))
    except Exception as e:                       # one bad file never stops a batch
        res = {'error': str(e)[:160]}
    if res is None:
        res = {'error': 'could not decode'}
    res.update({'v': VERSION, 'sig': sig, 'lk': job.get('key') or ''})
    return job['id'], res


def batch(jobs, workers=None):
    """Analyse `jobs` ([{id, path, bpm, key, anlz}]) into the cache.

    Prints one JSON line per finished track so the caller can show progress.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    data = dict(load())
    workers = workers or max(1, (os.cpu_count() or 2) - 2)
    done, t0, last = 0, time.time(), time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, j) for j in jobs]
        for fut in as_completed(futs):
            tid, res = fut.result()
            data[str(tid)] = res
            done += 1
            print(json.dumps({'done': done, 'total': len(jobs), 'id': tid,
                              'error': res.get('error')}), flush=True)
            # Saved as it goes: a stopped batch keeps what it already read.
            if time.time() - last > 15:
                save(data)
                last = time.time()
    save(data)
    print(json.dumps({'finished': True, 'done': done,
                      'seconds': round(time.time() - t0, 1)}), flush=True)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) > 1 and argv[1] == '--batch':
        # ⚠ Background work: below the UI in priority, and one maths thread
        # per worker — every worker spawning a full set of BLAS threads made
        # twelve processes fight over the same cores and the batch SLOWER.
        try:
            os.nice(10)
        except Exception:
            pass
        with open(argv[2], encoding='utf-8') as fh:
            jobs = json.load(fh)
        batch(jobs)
    elif len(argv) > 1:
        r = analyse(argv[1])
        print(json.dumps(r, indent=1)[:6000])


if __name__ == '__main__':
    main()
