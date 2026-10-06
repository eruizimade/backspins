"""Teach the library a property by example, instead of guessing at it.

Two things wear the same machinery here:

  · LABEL — "is this one Acid?" You invent a tag today and it sits on the four
    tracks you put it on. Answer a few dozen yes/nos (or start from the tracks
    that already carry it) and the model ranks the rest of the library by how
    likely each one is to belong, for you to correct and tag.

  · ORDER — "which of these two is more intense?" Asked forty times, it fits
    a scale to YOUR ear rather than to whatever a waveform happens to
    correlate with.

WHAT IT LEARNS FROM. The first version saw tempo, one waveform statistic,
the rating and your own tags — and measured its own ceiling: twenty answers
were as good as a hundred and sixty, because nothing in those numbers could
hear the difference it was being asked about. Now each track also brings what
musicdna.py reads from the audio: its key and mode, the colour of its scale
degree by degree, how its harmony moves, its timbre, its groove and how it
breathes. Every one of those is a named feature, grouped so the screen can
say which KIND of information is doing the work.

THE MODEL is still a logistic regression — on purpose. With tens of answers a
deep model would memorise them; a linear one with a well-chosen penalty
generalises, and every weight it learns has a name you can read. What changed
is how it is trained and judged:

  · every feature standardised against the whole library, so weights compare;
  · fitted exactly (Newton's method), not by a fixed number of guesses;
  · the penalty CHOSEN by cross-validation instead of hard-coded;
  · judged on answers it was not trained on — the AUC on screen is what it
    scores on tracks it has not seen, never on the ones it memorised;
  · questions picked where an answer teaches most (the tracks it is least
    sure of), with an occasional "it thinks yes — check it" so the positives
    of a rare tag are found rather than waited for.

MEASURED on this library (October 2026), before trusting any of it:

  · fully supervised, six tags you already use (Acid, Sunset, Sexy, After,
    Family, Uptempo), 4-fold: your own tags + genres + tempo separate them at
    AUC 0.84-0.89; the audio alone at 0.72-0.77 (harmony alone reaches 0.73
    for Acid); both together are no better than your tags alone.
  · taught from scratch (3 examples, then 80 answers picked by the model):
    tags 0.72, audio 0.60, everything 0.71.

So where a track is already well tagged, your own vocabulary is the strongest
witness and the audio adds little. The audio is what is left for a track
with no tags yet, and what can learn a distinction your tags do not already
draw. "What it listens to" on the screen shows this per lesson, so it is seen
case by case rather than promised.

⚠ A model that cannot tell you how often it is wrong is a model asking to be
believed, and this one is asked to guess about music. With fewer than ten of
either answer it says it is early rather than letting a lucky number look like
knowledge.
"""

import json
import math
import os
import random
import threading
import time

import numpy as np

import settings

STORE = os.path.join(settings.APP_DIR, 'lessons.json')
_LOCK = threading.Lock()

#: Penalties tried by cross-validation, weakest to strongest.
LAMBDAS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0)
#: How much one of the "assumed no" tracks counts against a real answer.
WEAK = 0.15

GROUPS = [
    ('tempo', 'Tempo & drive'),
    ('harmony', 'Key, scale & chords'),
    ('timbre', 'Sound & timbre'),
    ('rhythm', 'Rhythm & groove'),
    ('dynamics', 'Dynamics & shape'),
    ('tags', 'Your MyTags'),
    ('genres', 'Genres'),
]
GROUP_LABEL = dict(GROUPS)

DEGREES = ['1', '♭2', '2', '♭3', '3', '4', '♯4', '5', '♭6', '6', '♭7', '7']
BANDS = ['Sub-bass', 'Bass', 'Low mids', 'Mids', 'High mids', 'Presence', 'Air']
TEMPOS = (100, 110, 118, 123, 127, 132, 140, 150, 170)


# ── features ───────────────────────────────────────────────────────────────

def _audio_features():
    """(name, group, label, getter(dna)) for everything read from the audio."""
    f = []

    def add(name, group, label, get):
        f.append((name, group, label, get))

    # Harmony: the key's mode, the scale's colour degree by degree (counted
    # from the tonic, so an A minor and an F minor track compare), the bass,
    # and how the chords move.
    add('minor', 'harmony', 'Minor key', lambda d: 1.0 if d['key']['mode'] == 'minor' else 0.0)
    add('clarity', 'harmony', 'Clear sense of key', lambda d: d['heard']['clarity'])
    for i in range(12):
        add('deg%d' % i, 'harmony', 'Notes on the %s' % DEGREES[i],
            lambda d, i=i: d['rel'][i])
    for i in range(12):
        add('bdeg%d' % i, 'harmony', 'Bass on the %s' % DEGREES[i],
            lambda d, i=i: d['brel'][i])
    for sc in ('Phrygian', 'Dorian', 'Harmonic minor', 'Mixolydian', 'Lydian'):
        add('scale_' + sc, 'harmony', '%s colour' % sc,
            lambda d, sc=sc: 1.0 if d['scale']['name'] == sc else 0.0)
    add('moves', 'harmony', 'Has a chord progression', lambda d: 1.0 if d['prog'].get('moves') else 0.0)
    add('chords', 'harmony', 'Many different chords', lambda d: math.log1p(d['chords']['distinct']))
    add('changes', 'harmony', 'Chords change often', lambda d: math.log1p(d['chords']['per_min']))
    add('minorch', 'harmony', 'Mostly minor chords', lambda d: d['chords']['minor'])
    add('tonicch', 'harmony', 'Sits on the home chord', lambda d: d['chords']['tonic'])
    add('outside', 'harmony', 'Chords from outside the key', lambda d: d['chords']['outside'])
    add('motion', 'harmony', 'Harmony keeps moving', lambda d: d['chords']['motion'])

    # Timbre. The first two MFCCs have names (overall tilt, body); the rest
    # are finer detail and are labelled as such rather than invented.
    add('centroid', 'timbre', 'Bright', lambda d: math.log(max(50.0, d['timbre']['centroid'])))
    add('centroid_sd', 'timbre', 'Brightness changes a lot', lambda d: d['timbre']['centroid_sd'])
    add('rolloff', 'timbre', 'Lots of top end', lambda d: math.log(max(50.0, d['timbre']['rolloff'])))
    add('flat', 'timbre', 'Noisy rather than tonal', lambda d: d['timbre']['flat'])
    for i, b in enumerate(BANDS):
        add('band%d' % i, 'timbre', '%s weight' % b, lambda d, i=i: d['timbre']['bands'][i])
    names = {1: 'Dark tilt (timbre 1)', 2: 'Hollow mids (timbre 2)'}
    for i in range(1, 13):
        add('mfcc%d' % i, 'timbre', names.get(i, 'Timbre detail %d' % i),
            lambda d, i=i: d['timbre']['mfcc'][i])
    for i in range(1, 6):
        add('mfccsd%d' % i, 'timbre', 'Timbre varies (%d)' % i,
            lambda d, i=i: d['timbre']['mfcc_sd'][i])

    add('onsets', 'rhythm', 'Busy — many hits', lambda d: d['rhythm']['onsets'])
    add('perc', 'rhythm', 'Percussive', lambda d: d['rhythm']['perc'])
    add('pulse', 'rhythm', 'Steady, clear pulse', lambda d: d['rhythm']['pulse'])
    add('onbeat', 'rhythm', 'Hits on the beat', lambda d: d['rhythm']['on'])
    add('off8', 'rhythm', 'Off-beat (the "and")', lambda d: d['rhythm']['off8'])
    add('off16', 'rhythm', 'Sixteenths & swing', lambda d: d['rhythm']['off16'])
    add('punch', 'rhythm', 'Punchy contrast', lambda d: d['rhythm']['flux_sd'])

    add('loud', 'dynamics', 'Loud master', lambda d: d['dyn']['loud'])
    add('range', 'dynamics', 'Breathes (dynamic range)', lambda d: d['dyn']['range'])
    add('breaks', 'dynamics', 'Breakdowns', lambda d: min(4, d['dyn']['breaks']))
    add('intro', 'dynamics', 'Long quiet intro',
        lambda d: next((i for i, v in enumerate(d['dyn']['contour']) if v > 0.75), 32) / 32.0)
    return f


AUDIO = _audio_features()


class Space:
    """The whole library as a standardised matrix, plus what each column is.

    Built once per (library, analysis, lesson) and reused: every answer only
    refits the model, it never rebuilds this.
    """

    def __init__(self, tracks, dna, drive, exclude_tag=None):
        self.ids = [str(t['id']) for t in tracks]
        self.index = {tid: i for i, tid in enumerate(self.ids)}
        cols, groups, labels = [], [], []

        def col(name, group, label, values):
            cols.append(values)
            groups.append(group)
            labels.append((name, label))

        # Tempo: the number itself, and soft bands around the tempos dance
        # music clusters at — a linear weight on bpm alone cannot say
        # "around 124 and nowhere else".
        bpm = [float(t.get('bpm') or 0) or 124.0 for t in tracks]
        col('bpm', 'tempo', 'Faster', bpm)
        for c in TEMPOS:
            col('t%d' % c, 'tempo', 'Around %d bpm' % c,
                [math.exp(-((b - c) / 5.0) ** 2) for b in bpm])
        col('drive', 'tempo', 'Drive (how hard it goes)',
            [float(drive.get(tid) or 5) for tid in self.ids])
        col('rating', 'tempo', 'Rated higher', [float(t.get('rating') or 0) for t in tracks])

        # Audio. A track not analysed yet gets the library average (0 once
        # standardised) — "unknown", not "zero", which would be a claim.
        have = [dna.get(tid) for tid in self.ids]
        have = [d if (d and not d.get('error') and 'rel' in d) else None for d in have]
        self.analysed = sum(1 for d in have if d)
        for name, group, label, get in AUDIO:
            vals = []
            for d in have:
                try:
                    vals.append(float(get(d)) if d else float('nan'))
                except Exception:
                    vals.append(float('nan'))
            col(name, group, label, vals)

        # Your own vocabulary. ⚠ The tag being taught is left out, or the
        # model would learn the answer from the question.
        tagcount, gencount = {}, {}
        for t in tracks:
            for names in (t.get('banks') or {}).values():
                for n in names:
                    tagcount[n] = tagcount.get(n, 0) + 1
            for g in (t.get('genres') or []):
                gencount[g] = gencount.get(g, 0) + 1
        tags = sorted(n for n, c in tagcount.items() if c >= 5 and n != exclude_tag)
        # ⚠ Genres are a long tail (140 of them, most on a handful of tracks);
        # only the common ones, or the model learns one track by its genre.
        gens = sorted(g for g, c in gencount.items() if c >= 25)
        mine = []
        for t in tracks:
            s = set()
            for names in (t.get('banks') or {}).values():
                s.update(names)
            mine.append(s)
        for n in tags:
            col('tag:' + n, 'tags', 'Tag: ' + n, [1.0 if n in s else 0.0 for s in mine])
        for g in gens:
            col('genre:' + g, 'genres', 'Genre: ' + g,
                [1.0 if g in (t.get('genres') or []) else 0.0 for t in tracks])

        X = np.array(cols, dtype=np.float64).T if cols else np.zeros((len(tracks), 0))
        # Yes/no columns (a tag, a genre, minor or not) read differently from
        # amounts when a track is explained: "not minor", but "less bright".
        self.binary = [bool(np.all(np.isin(c[~np.isnan(c)], (0.0, 1.0))))
                       for c in X.T] if X.size else []
        mean = np.nanmean(X, axis=0) if X.size else np.zeros(0)
        mean = np.where(np.isnan(mean), 0.0, mean)
        X = np.where(np.isnan(X), mean, X)
        sd = X.std(axis=0)
        sd[sd < 1e-9] = 1.0
        self.X = (X - mean) / sd
        self.groups = groups
        self.labels = labels
        self.tagged = {}
        if exclude_tag:
            for tid, s in zip(self.ids, mine):
                if exclude_tag in s:
                    self.tagged[tid] = True

    def columns(self, groups):
        return [j for j, g in enumerate(self.groups) if g in groups]


_SPACE = {'key': None, 'space': None}


def space_for(tracks, dna, drive, tag, gen):
    key = (gen, len(tracks), id(dna), len(dna), tag)
    with _LOCK:
        if _SPACE['key'] == key:
            return _SPACE['space']
    sp = Space(tracks, dna, drive, tag)
    with _LOCK:
        _SPACE.update(key=key, space=sp)
    return sp


# ── the model ──────────────────────────────────────────────────────────────

def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit(X, y, w=None, lam=3.0, iters=40):
    """L2 logistic regression, fitted exactly by Newton's method.

    Returns (bias, weights). The bias is never penalised: it only says how
    common the thing is, and shrinking it biases every prediction towards no.
    """
    n, d = X.shape
    w = np.ones(n) if w is None else np.asarray(w, dtype=np.float64)
    Xb = np.hstack([np.ones((n, 1)), X])
    beta = np.zeros(d + 1)
    reg = np.full(d + 1, lam)
    reg[0] = 0.0
    for _ in range(iters):
        p = _sigmoid(Xb @ beta)
        g = Xb.T @ (w * (p - y)) + reg * beta
        s = w * p * (1 - p)
        H = (Xb * s[:, None]).T @ Xb + np.diag(reg + 1e-8)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        beta -= step
        if np.abs(step).max() < 1e-6:
            break
    return beta[0], beta[1:]


def predict(model, X):
    b, w = model
    return _sigmoid(X @ w + b)


def auc(y, p):
    """Probability that a random yes scores above a random no."""
    y = np.asarray(y)
    p = np.asarray(p)
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind='mergesort')
    ranks = np.empty(len(order))
    allv = np.concatenate([pos, neg])[order]
    # Average ranks for ties.
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    r = ranks[:len(pos)].sum()
    return float((r - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def _folds(y, k, seed=0):
    """Stratified folds: every fold gets its share of yes and of no."""
    rng = random.Random(seed)
    out = [[] for _ in range(k)]
    for cls in (0, 1):
        idx = [i for i, v in enumerate(y) if v == cls]
        rng.shuffle(idx)
        for n, i in enumerate(idx):
            out[n % k].append(i)
    return out


def crossval(X, y, w, real, lams=LAMBDAS, k=5):
    """Out-of-fold predictions for each penalty -> (best lam, auc, acc, oof).

    `real` marks the rows the model is JUDGED on — normally the genuine
    answers: the "assumed no" rows help training, but scoring the model on
    guesses would grade it against itself.
    """
    yr = y[real]
    k = int(min(k, (yr == 1).sum(), (yr == 0).sum()))
    if k < 2:
        return None
    folds = _folds(list(y), k)
    best = None
    for lam in lams:
        oof = np.full(len(y), np.nan)
        for f in folds:
            test = np.zeros(len(y), dtype=bool)
            test[f] = True
            m = fit(X[~test], y[~test], w[~test], lam)
            oof[test] = predict(m, X[test])
        mask = real & ~np.isnan(oof)
        a = auc(y[mask], oof[mask])
        if a is None:
            continue
        if best is None or a > best[1] + 1e-9:
            acc = float(((oof[mask] >= 0.5) == (y[mask] == 1)).mean())
            best = (lam, a, acc, oof)
    return best


# ── assembling the training set for a lesson ───────────────────────────────

def training_rows(lesson, space):
    """(row indices, labels, weights, is-real) for a label lesson."""
    idx = space.index
    yes = [i for i in lesson.get('yes') or [] if i in idx]
    no = [i for i in lesson.get('no') or [] if i in idx]
    noset = set(no)
    pos = list(dict.fromkeys(yes))
    existing = []
    if lesson.get('use_existing', True):
        existing = [t for t in space.tagged if t not in noset and t not in set(pos)]
    rows, ys, real = [], [], []
    for t in pos + existing:
        rows.append(idx[t]); ys.append(1); real.append(True)
    for t in dict.fromkeys(no):
        rows.append(idx[t]); ys.append(0); real.append(True)
    # "Assumed no": with positives but few real noes the model has nothing to
    # push against, so a sample of everything else stands in — weakly, and
    # always the same sample, so the ranking does not jump between requests.
    n_pos = len(pos) + len(existing)
    weak = []
    if n_pos:
        taken = set(pos) | set(existing) | noset
        pool = [t for t in space.ids if t not in taken]
        rng = random.Random(lesson.get('name') or 'x')
        weak = rng.sample(pool, min(len(pool), max(60, 3 * n_pos), 600))
        for t in weak:
            rows.append(idx[t]); ys.append(0); real.append(False)
    y = np.array(ys, dtype=np.float64)
    r = np.array(real, dtype=bool)
    w = np.where(r, 1.0, WEAK)
    # Balance the two sides, so a rare tag is not learned as "always no".
    sp, sn = w[y == 1].sum(), w[y == 0].sum()
    if sp and sn:
        w = np.where(y == 1, w * (sp + sn) / (2 * sp), w * (sp + sn) / (2 * sn))
    return (np.array(rows, dtype=int), y, w, r,
            {'yes': len(pos), 'no': len(no), 'existing': len(existing), 'weak': len(weak)})


def train_label(lesson, space, groups=None, lams=LAMBDAS):
    """Fit a label lesson. -> dict with the model and how good it is."""
    groups = groups or lesson.get('groups') or [g for g, _ in GROUPS]
    cols = space.columns(groups)
    rows, y, w, real, counts = training_rows(lesson, space)
    out = {'counts': counts, 'cols': cols, 'groups': groups}
    if not len(cols) or not (y == 1).any() or not (y == 0).any():
        return out
    X = space.X[rows][:, cols]
    # ⚠ Judged on real answers. A lesson started from a tag you already use
    # has hundreds of real yeses but no real noes yet; then — and only then —
    # it is judged against the untagged sample instead, and says so: some of
    # those may belong, so the score is a floor, not a promise.
    proxy = int(((y == 0) & real).sum()) < 10
    judge = (real | (y == 0)) if proxy else real
    cv = crossval(X, y, w, judge, lams)
    lam = cv[0] if cv else 10.0
    model = fit(X, y, w, lam)
    out.update(model=model, lam=lam)
    if cv:
        out.update(auc=round(cv[1], 3), acc=round(cv[2], 3), proxy=proxy)
        # The rate a model that always said the majority answer would score.
        yr = y[judge]
        out['base'] = round(float(max(yr.mean(), 1 - yr.mean())), 3)
    return out


def train_order(lesson, space, groups=None, lams=LAMBDAS):
    """A comparison is a logistic problem on the DIFFERENCE of two tracks:
    "the winner scores higher" is "the difference is positive". Each answer is
    used both ways round, so nothing is learned from which side it was shown."""
    groups = groups or lesson.get('groups') or [g for g, _ in GROUPS]
    cols = space.columns(groups)
    idx = space.index
    pairs = [(a, b) for a, b in lesson.get('pairs') or [] if a in idx and b in idx]
    out = {'counts': {'pairs': len(pairs)}, 'cols': cols, 'groups': groups}
    if len(pairs) < 3 or not cols:
        return out
    Xc = space.X[:, cols]
    D = np.array([Xc[idx[a]] - Xc[idx[b]] for a, b in pairs])
    X = np.vstack([D, -D])
    y = np.concatenate([np.ones(len(D)), np.zeros(len(D))])
    # Cross-validated by PAIR, so the mirror of a test pair never trains.
    # ⚠ Not scored below eight: three right out of four is a coin having a
    # good day, and a number on screen gets believed.
    k = min(5, len(pairs))
    best = None
    if len(pairs) >= 8:
        order = list(range(len(pairs)))
        random.Random(0).shuffle(order)
        for lam in lams:
            right = 0
            for f in range(k):
                test = set(order[f::k])
                tr = [i for i in range(len(pairs)) if i not in test]
                Xt = np.vstack([D[tr], -D[tr]])
                yt = np.concatenate([np.ones(len(tr)), np.zeros(len(tr))])
                b, wv = fit(Xt, yt, None, lam)
                right += sum(1 for i in test if D[i] @ wv > 0)
            acc = right / float(len(pairs))
            if best is None or acc > best[1] + 1e-9:
                best = (lam, acc)
    lam = best[0] if best else 10.0
    b, wv = fit(X, y, None, lam)
    out.update(model=(0.0, wv), lam=lam)
    if best:
        out.update(acc=round(best[1], 3), base=0.5)
    return out


def verdict(rep, kind):
    """Plain words for how far to trust it."""
    if kind == 'order':
        a = rep.get('acc')
        n = rep['counts'].get('pairs', 0)
        if a is None:
            return 'too-few', 'A few comparisons more and it starts to rank.'
        if n < 15:
            return 'early', 'Early days: %d comparisons.' % n
        if a >= 0.8:
            return 'strong', 'Predicts your choice %d times in 10.' % round(a * 10)
        if a >= 0.65:
            return 'useful', 'Predicts your choice %d times in 10 — keep going.' % round(a * 10)
        return 'weak', 'Barely better than a coin. The difference may be hard to hear in these features.'
    a = rep.get('auc')
    c = rep['counts']
    if a is None:
        if not (c.get('yes') or c.get('existing')):
            return 'too-few', 'It needs at least one yes. Add a few examples you know.'
        return 'too-few', 'It needs a couple of real yes and no answers before it can be checked.'
    if rep.get('proxy'):
        floor = (' Judged against untagged tracks, some of which may belong — '
                 'answer a few noes for a fair score.')
        if a >= 0.8:
            return 'useful', 'It tells your tagged tracks from the rest well.' + floor
        return 'rough', 'It partly tells your tagged tracks from the rest.' + floor
    few = min(c.get('yes', 0) + c.get('existing', 0), c.get('no', 0))
    if few < 10:
        return 'early', ('Early days: with %d %s the score moves a lot from one answer '
                         'to the next.' % (few, 'no' if c.get('no', 0) == few else 'yes'))
    if a >= 0.9:
        return 'strong', 'Strong: the top of the ranking can be trusted.'
    if a >= 0.8:
        return 'useful', 'Useful: check the top of the list, it is mostly right.'
    if a >= 0.7:
        return 'rough', 'Rough: it has the idea. More answers will sharpen it.'
    return 'weak', 'Guessing. Either it needs more answers or this is hard to hear in these features.'


def explain(rep, space, top=8):
    """The weights, by name. Every number here is in "per typical spread of
    this feature across your library", so they compare with each other."""
    if 'model' not in rep:
        return []
    _b, w = rep['model']
    cols = rep['cols']
    order = np.argsort(-np.abs(w))
    out = []
    for j in order[:top * 3]:
        if abs(w[j]) < 0.02:
            break
        name, label = space.labels[cols[j]]
        out.append({'f': name, 'label': label, 'w': round(float(w[j]), 3),
                    'group': space.groups[cols[j]]})
        if len(out) >= top * 2:
            break
    return out


def why(rep, space, tid, top=4):
    """What pushed ONE track's score up or down: weight × how unusual it is."""
    if 'model' not in rep or tid not in space.index:
        return []
    _b, w = rep['model']
    x = space.X[space.index[tid]][rep['cols']]
    c = w * x
    order = np.argsort(-np.abs(c))
    out = []
    for j in order[:top]:
        if abs(c[j]) < 0.05:
            break
        col = rep['cols'][j]
        label = space.labels[col][1]
        # ⚠ Said the way round it acts: a major track pushed up by a negative
        # weight on "Minor key" is pushed up by NOT being minor, and the
        # label has to say that or it reads as the opposite.
        if x[j] < 0:
            label = ('Not ' + label[0].lower() + label[1:] if space.binary[col]
                     else 'Less ' + label[0].lower() + label[1:])
        out.append({'label': label, 'c': round(float(c[j]), 2)})
    return out


def group_report(lesson, space, kind):
    """How well each KIND of information does on its own — which is the
    honest answer to "what is it listening to?"."""
    out = []
    lam = lesson.get('lam') or 10.0
    for g, label in GROUPS:
        if kind == 'order':
            rep = train_order(lesson, space, [g], lams=(lam,))
            score = rep.get('acc')
        else:
            rep = train_label(lesson, space, [g], lams=(lam,))
            score = rep.get('auc')
        out.append({'group': g, 'label': label, 'score': score,
                    'n': len(space.columns([g]))})
    return out


# ── which question to ask next ─────────────────────────────────────────────

def next_question(lesson, space, rep, kind, rng=None):
    """-> (kind of question, [track ids], why, model's belief)."""
    rng = rng or random.Random()
    seen = set(lesson.get('yes') or []) | set(lesson.get('no') or [])
    for a, b in lesson.get('pairs') or []:
        seen.add(a); seen.add(b)
    skipped = set(lesson.get('skipped') or [])
    pool = [t for t in space.ids if t not in seen and t not in skipped]
    if kind == 'order':
        if len(pool) < 2:
            return None
        if 'model' not in rep:
            a, b = rng.sample(pool, 2)
            return ('pair', [a, b], 'explore', None)
        sample = rng.sample(pool, min(200, len(pool)))
        X = space.X[[space.index[t] for t in sample]][:, rep['cols']]
        s = X @ rep['model'][1]
        o = np.argsort(s)
        gaps = np.abs(np.diff(s[o]))
        i = int(np.argmin(gaps)) if len(gaps) else 0
        return ('pair', [sample[o[i]], sample[o[i + 1]]], 'close', None)

    pool = [t for t in pool if t not in space.tagged or not lesson.get('use_existing', True)]
    if not pool:
        return None
    if 'model' not in rep:
        return ('one', [rng.choice(pool)], 'explore', None)
    X = space.X[[space.index[t] for t in pool]][:, rep['cols']]
    p = predict(rep['model'], X)
    n_answers = len(lesson.get('yes') or []) + len(lesson.get('no') or [])
    # One question in five checks the model's favourite unconfirmed track:
    # for a rare tag that is how the yeses are found, and it shows you what
    # the model believes rather than only where it is lost.
    if n_answers % 5 == 4:
        j = int(np.argmax(p))
        return ('one', [pool[j]], 'likely', round(float(p[j]), 3))
    # Otherwise the least certain — among the ten closest to 50%, one at
    # random, so two lessons never ask the very same sequence.
    o = np.argsort(np.abs(p - 0.5))[:10]
    j = int(rng.choice(list(o)))
    return ('one', [pool[j]], 'unsure', round(float(p[j]), 3))


# ── what has been taught, kept between sessions ────────────────────────────

def load():
    try:
        with open(STORE, encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save(data):
    tmp = STORE + '.part'
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STORE)


def blank(name, kind='label'):
    return {'name': name, 'kind': kind, 'yes': [], 'no': [], 'pairs': [],
            'log': [], 'skipped': [], 'history': [], 'use_existing': True,
            'groups': [g for g, _ in GROUPS], 'created': int(time.time())}


def lesson(name):
    cur = load().get(name)
    if not cur:
        return blank(name)
    base = blank(name, cur.get('kind') or 'label')
    base.update(cur)
    base['name'] = name
    return base


def change(name, fn, kind=None):
    """Read-modify-write one lesson under the lock. `fn(lesson)` edits it."""
    with _LOCK:
        data = load()
        cur = data.get(name)
        les = blank(name, kind or 'label')
        if cur:
            les.update(cur)
        les['name'] = name
        fn(les)
        les['updated'] = int(time.time())
        data[name] = les
        save(data)
        return les


def answer(les, a):
    """Record one answer in place (an answer about a track replaces any
    earlier one about the same track — you are allowed to change your mind)."""
    if a.get('winner') and a.get('loser'):
        les['pairs'].append([str(a['winner']), str(a['loser'])])
        les['log'].append({'k': 'pair', 'w': str(a['winner']), 'l': str(a['loser'])})
        return
    tid = str(a.get('id'))
    if a.get('skip'):
        if tid not in les['skipped']:
            les['skipped'].append(tid)
        les['skipped'] = les['skipped'][-200:]
        return
    side = 'yes' if a.get('yes') else 'no'
    other = 'no' if side == 'yes' else 'yes'
    les[other] = [t for t in les[other] if t != tid]
    if tid not in les[side]:
        les[side].append(tid)
    les['log'].append({'k': side, 'id': tid})


def undo(les):
    """Take back the last answer."""
    while les['log']:
        last = les['log'].pop()
        if last['k'] == 'pair':
            for i in range(len(les['pairs']) - 1, -1, -1):
                if les['pairs'][i] == [last['w'], last['l']]:
                    del les['pairs'][i]
                    return last
        elif last['id'] in les.get(last['k'], []):
            les[last['k']] = [t for t in les[last['k']] if t != last['id']]
            return last
    return None


def forget(les, tid):
    tid = str(tid)
    les['yes'] = [t for t in les['yes'] if t != tid]
    les['no'] = [t for t in les['no'] if t != tid]
    les['pairs'] = [p for p in les['pairs'] if tid not in p]
    les['log'] = [e for e in les['log'] if e.get('id') != tid
                  and tid not in (e.get('w'), e.get('l'))]


# ── what the screen gets ───────────────────────────────────────────────────

def _usable(d):
    return d if (d and not d.get('error') and 'rel' in d) else None


def dna_brief(d):
    """One line's worth of what the audio says: key, scale, progression."""
    d = _usable(d)
    if not d:
        return None
    loop = (d.get('prog') or {}).get('loop') or []
    return {'key': d['key']['name'], 'camelot': d['key']['camelot'],
            'from': d['key'].get('from'),
            'scale': d['scale']['name'], 'colour': d['scale'].get('colour') or '',
            'moves': bool((d.get('prog') or {}).get('moves')),
            'prog': [[c['num'], c['chord'], c['bars']] for c in loop]}


def dna_full(d):
    """Everything worth drawing about one track."""
    d = _usable(d)
    if not d:
        return None
    out = dna_brief(d)
    out.update({
        'heard': d['heard'], 'tonic': d['key']['tonic'], 'mode': d['key']['mode'],
        'scale_notes': d['scale'].get('notes') or [], 'mark': d['scale'].get('mark'),
        'notes': d['notes'], 'bass': d['bass'],
        'coverage': (d.get('prog') or {}).get('coverage'),
        'chords': {k: d['chords'][k] for k in ('distinct', 'per_min', 'minor',
                                                 'tonic', 'outside')},
        'timeline': (d['chords'].get('timeline') or [])[:80],
        'bands': d['timbre']['bands'], 'centroid': d['timbre']['centroid'],
        'rhythm': d['rhythm'], 'contour': d['dyn']['contour'],
        'loud': d['dyn']['loud'], 'range': d['dyn']['range'],
        'breaks': d['dyn']['breaks'], 'dur': d.get('dur'),
    })
    return out


def brief(t, dna, drive):
    tid = str(t.get('id'))
    return {'id': tid, 'title': t.get('title'), 'artist': t.get('artist'),
            'bpm': t.get('bpm'), 'key': t.get('key'), 'energy': t.get('energy'),
            'rating': t.get('rating'), 'drive': drive.get(tid),
            'path': t.get('path'), 'seconds': t.get('seconds'),
            'genres': (t.get('genres') or [])[:2],
            'dna': dna_brief(dna.get(tid))}


def state(les, tracks, dna, drive, gen, with_groups=False, rng=None, queued=None):
    """The whole Teach screen for one lesson, in one answer.

    `queued` = tracks with this tag waiting in the tagger's queue (rekordbox
    was open): they count as tagged on screen, because they will be.
    """
    name, kind = les['name'], les.get('kind') or 'label'
    sp = space_for(tracks, dna, drive, name, gen)
    tagged_now = set(sp.tagged) | set(queued or ())
    rep = train_order(les, sp) if kind == 'order' else train_label(les, sp)
    by = {str(t['id']): t for t in tracks}
    counts = rep['counts']
    score = rep.get('acc') if kind == 'order' else rep.get('auc')
    n_answers = (counts.get('pairs', 0) if kind == 'order'
                 else counts.get('yes', 0) + counts.get('no', 0))

    # Keep the learning curve: one point each time the number of answers
    # changes, so you can SEE whether more answers are still helping.
    hist = list(les.get('history') or [])
    if score is not None and (not hist or hist[-1]['n'] != n_answers
                              or hist[-1]['s'] != score):
        point = {'n': n_answers, 's': score, 'lam': rep.get('lam')}

        def keep(l):
            h = [x for x in (l.get('history') or []) if x['n'] < n_answers]
            h.append(point)
            l['history'] = h[-200:]
            l['lam'] = rep.get('lam')
        les = change(name, keep, kind)
        hist = les['history']

    word, sentence = verdict(rep, kind)
    out = {'name': name, 'kind': kind, 'counts': counts,
           'use_existing': les.get('use_existing', True),
           'groups': [{'key': g, 'label': l, 'on': g in rep['groups'],
                       'n': len(sp.columns([g]))} for g, l in GROUPS],
           'trained': 'model' in rep,
           'report': {'score': score, 'base': rep.get('base'), 'lam': rep.get('lam'),
                      'proxy': bool(rep.get('proxy')),
                      'verdict': word, 'sentence': sentence, 'history': hist,
                      'weights': explain(rep, sp)},
           'analysed': sp.analysed, 'library': len(sp.ids),
           'tagged': len(sp.tagged)}
    if with_groups and 'model' in rep:
        out['report']['by_group'] = group_report(dict(les, lam=rep.get('lam')), sp, kind)

    # The yeses that do not carry the tag yet: one click writes them all.
    if kind == 'label':
        out['waiting'] = [t for t in (les.get('yes') or [])
                          if t in by and t not in tagged_now]

    # What you answered, newest first, so a wrong click can be found and taken back.
    def small(tid):
        t = by.get(tid) or {}
        return {'id': tid, 'title': t.get('title') or '?', 'artist': t.get('artist'),
                'path': t.get('path'), 'seconds': t.get('seconds')}
    if kind == 'order':
        out['answers'] = {'pairs': [[small(a), small(b)] for a, b in
                                    reversed((les.get('pairs') or [])[-150:])]}
    else:
        out['answers'] = {k: [small(t) for t in reversed((les.get(k) or [])[-300:])
                              if t in by] for k in ('yes', 'no')}

    # The next question.
    q = next_question(les, sp, rep, kind, rng)
    if q:
        qk, ids, reason, p = q
        out['ask'] = {'type': qk, 'why': reason, 'p': p,
                      'tracks': [dict(brief(by[i], dna, drive),
                                      full=dna_full(dna.get(i)),
                                      because=why(rep, sp, i) if kind == 'label' else [])
                                 for i in ids if i in by]}

    # The library, ranked by what the model now believes.
    if 'model' in rep:
        Xc = sp.X[:, rep['cols']]
        yes, no = set(les.get('yes') or []), set(les.get('no') or [])
        if kind == 'order':
            s = Xc @ rep['model'][1]
            pct = np.argsort(np.argsort(s)) / max(1.0, len(s) - 1.0)
            order = np.argsort(-s)
            out['ranked'] = [dict(brief(by[sp.ids[i]], dna, drive),
                                  p=round(float(pct[i]), 3))
                             for i in order[:300] if sp.ids[i] in by]
        else:
            p = predict(rep['model'], Xc)
            order = np.argsort(-p)
            rows, shown = [], set()

            def row(i):
                tid = sp.ids[i]
                shown.add(tid)
                return dict(brief(by[tid], dna, drive), p=round(float(p[i]), 3),
                            mark='yes' if tid in yes else 'no' if tid in no else None,
                            tagged=tid in tagged_now)
            for i in order:
                if sp.ids[i] not in by:
                    continue
                rows.append(row(i))
                if len(rows) >= 300:
                    break
            # ⚠ Every yes you gave that is not tagged yet is in the list,
            # however low the model ranks it: answering yes IS tagging, and a
            # yes that fell off the bottom of the list could never be written.
            for tid in yes:
                if tid in by and tid not in shown and tid not in tagged_now:
                    rows.append(row(sp.index[tid]))
            out['ranked'] = rows
            free = np.array([sp.ids[i] not in yes and sp.ids[i] not in no
                             and sp.ids[i] not in tagged_now for i in range(len(sp.ids))])
            pf = p[free]
            out['dist'] = {'hist': np.histogram(pf, bins=20, range=(0, 1))[0].tolist(),
                           'over': {str(c): int((pf >= c).sum()) for c in (0.5, 0.7, 0.9)}}
    return out


def overview(tracks, dna):
    """The lesson list, and the tags you could start one from."""
    data = load()
    counts = {}
    for t in tracks:
        for names in (t.get('banks') or {}).values():
            for n in names:
                counts[n] = counts.get(n, 0) + 1
    lessons = []
    for name, les in data.items():
        hist = les.get('history') or []
        lessons.append({'name': name, 'kind': les.get('kind') or 'label',
                        'yes': len(les.get('yes') or []), 'no': len(les.get('no') or []),
                        'pairs': len(les.get('pairs') or []),
                        'tagged': counts.get(name, 0),
                        'score': hist[-1]['s'] if hist else None,
                        'updated': les.get('updated') or les.get('created') or 0})
    lessons.sort(key=lambda l: -l['updated'])
    return {'lessons': lessons,
            'tags': sorted(([n, c] for n, c in counts.items()), key=lambda x: -x[1])}


