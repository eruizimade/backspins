"""Tagging: what to suggest, and how to write it without breaking anything.

Two halves.

**Suggestions.** Never applied on your behalf — offered. They come from your own
decisions: among the tracks most like this one, which tags do they carry? With
446 tracks tagged "Acid", 73% of them are also "Dance" and 68% "Cool". That is
not a guess about music, it is a summary of what you have already decided,
hundreds of times, about tracks like this one.

**Writing.** MyTags live in `master.db`, which cannot be written while rekordbox
is open — and tagging is exactly the thing you do *while* browsing your library.
So nothing is written directly: every change becomes an entry in a QUEUE that is
applied later, when rekordbox is closed.

⚠ The queue holds OPERATIONS, not states. "Add tag X to track 123" can be
applied tomorrow and still means the same thing; "the tags of track 123 are
A, B, C" would silently wipe whatever you did in rekordbox in between. Only
operations survive a delay, which is the whole point of a queue.
"""

import datetime
import json
import os
import threading
import uuid

import settings
import setbuilder as sb

QUEUE_FILE = os.path.join(settings.APP_DIR, 'queue.json')


def _baseline_matches(op_kind, current, before):
    """Is the field still what the client saw when it queued the change?

    ⚠ Compared through the SAME normalisation the client's value went through,
    never by raw equality. The baseline the browser sends is DERIVED: colours
    arrive trimmed (this library really has one called ' BEATGRID', with a
    leading space typed in rekordbox) and genres are split on '/' and rejoined,
    so a stored 'Rap / Hip Hop' comes back as 'Rap/Hip Hop'. Raw equality made
    those changes fail their own baseline check and skip — silently, which is
    the worst way to lose an edit.
    """
    if op_kind == 'set_color':
        return (current or '').strip().casefold() == str(before or '').strip().casefold()
    if op_kind == 'set_genre':
        def norm(v):
            return '/'.join(part.strip() for part in str(v or '').split('/')
                            if part.strip())
        return norm(current) == norm(before)
    if op_kind == 'set_rating':
        return str(current or 0) == str(before or 0)
    if op_kind == 'set_artist':
        # Artist names collect the same stray spacing as colours do.
        return (current or '').strip() == str(before or '').strip()
    return (current or '') == (before or '')


class _Skip(Exception):
    """A queued op whose baseline no longer matches: skip, do not clobber."""
_QUEUE_LOCK = threading.Lock()
# ⚠ Serialises WRITES to master.db. Saving is automatic now, so two quick
# edits arrive as two concurrent requests — without this they would open
# the database on top of each other and one would die on the file lock.
_APPLY_LOCK = threading.Lock()

# What a queued operation may do. Deliberately small: metadata only, nothing
# that touches a file on disk. (set_artwork writes rekordbox's OWN artwork
# files, in its share folder, the way rekordbox does — never the audio file.)
OPS = ('add_tag', 'remove_tag', 'set_genre', 'set_comment', 'set_color',
       'set_rating', 'set_title', 'set_artist',
       'playlist_add', 'playlist_remove', 'set_artwork', 'playlist_create')

# Playlists created through the queue: their key ('new:<key>', which is what
# the playlist_add ops that follow point at) → the ID rekordbox gave them.
# Written only AFTER the commit, so a key never points at a playlist that
# was rolled back.
CREATED_FILE = os.path.join(settings.APP_DIR, 'created-playlists.json')


def created_playlists():
    try:
        with open(CREATED_FILE, encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _save_created(d):
    os.makedirs(settings.APP_DIR, exist_ok=True)
    tmp = CREATED_FILE + '.part'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(d, fh)
    os.replace(tmp, CREATED_FILE)


def resolve_playlist(pid):
    """'new:<key>' → the real ID once it exists; anything else as it is."""
    pid = str(pid or '')
    if pid.startswith('new:'):
        return created_playlists().get(pid[4:]) or pid
    return pid


def _write_rb_artwork(src):
    """A cover the way rekordbox stores its own: three JPEGs (500, 240 and
    80 px) in share/PIONEER/Artwork/<3>/<rest of a uuid>/, and the path the
    track's ImagePath points at. Resized with sips (it ships with macOS)."""
    import subprocess
    u = str(uuid.uuid4())
    rel_dir = '/PIONEER/Artwork/%s/%s' % (u[:3], u[3:])
    share = os.path.join(os.path.dirname(settings.find_library()), 'share')
    out = share + rel_dir
    os.makedirs(out, exist_ok=True)
    for name, px in (('artwork.jpg', 500), ('artwork_m.jpg', 240), ('artwork_s.jpg', 80)):
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '88',
                        '-z', str(px), str(px), src, '--out', os.path.join(out, name)],
                       check=True, capture_output=True, timeout=30)
    return rel_dir + '/artwork.jpg'


# ─────────────────────────────────────────────────────────── suggestions

def _similar(track, tracks, limit=60):
    """The tracks most like this one, for the purpose of borrowing tags.

    Deliberately blunt: same genre or same family, and a tempo within 8%.
    A cleverer score would be harder to explain, and the suggestion has to be
    defensible — you are about to accept it with one keypress.
    """
    fam = {sb.genre_family(g)[0] for g in track['genres']} - {sb.OTHER_FAMILY[0]}
    genres = set(track['genres'])
    out = []
    for t in tracks:
        if t['id'] == track['id'] or not t['tags']:
            continue
        same_genre = bool(genres & set(t['genres']))
        same_fam = bool(fam & {sb.genre_family(g)[0] for g in t['genres']})
        if not (same_genre or same_fam):
            continue
        d = sb.bpm_distance(track['bpm'], t['bpm'])
        if d is not None and d > 0.08:
            continue
        # Same genre beats same family; closer tempo breaks the tie.
        out.append(((0 if same_genre else 1, d if d is not None else 1), t))
    out.sort(key=lambda r: r[0])
    return [t for _, t in out[:limit]]


def suggest(track, tracks, banks, floor=0.45, top=8):
    """Tags carried by similar tracks, with how often. Never applied.

    `floor` is the share of similar tracks that must carry a tag before it is
    worth showing. Below about 40% it stops being a pattern and starts being
    noise, and a suggestion you have to think about is worse than none.
    """
    pool = _similar(track, tracks)
    if len(pool) < 6:
        return {'from': len(pool), 'tags': []}

    have = set(track['tags'])
    # ⚠ Housekeeping groups are excluded. Without this the top suggestion was
    # "TO BE CHECKED" at 68% — perfectly true, and useless: it says something
    # about your workflow, not about the music.
    ignored = {b.lower() for b in (settings.load().get('ignored_banks') or [])}
    cat = {}
    for b in banks:
        if (b.get('name') or '').lower() in ignored:
            continue
        for t in b['tags']:
            cat[t['name']] = b['name']

    # ⚠ The denominator is the similar tracks you have actually TAGGED, not
    # every similar track. A track nobody has got to yet is not evidence
    # against a tag — it is no evidence at all, and counting it as a "no"
    # dragged every share down in proportion to how much of the library was
    # still untagged. With ~450 undecided tracks here, real patterns sat just
    # under the floor and nothing was ever suggested.
    decided = [t for t in pool
               if any(name in cat for name in (t.get('tags') or []))]
    if len(decided) < 6:
        return {'from': len(pool), 'decided': len(decided), 'tags': []}

    counts = {}
    for t in decided:
        for name in set(t['tags']):
            counts[name] = counts.get(name, 0) + 1

    rows = []
    for name, n in counts.items():
        if name in have or name not in cat:
            continue
        share = n / len(decided)
        if share < floor:
            continue
        rows.append({'name': name, 'bank': cat.get(name, ''),
                     'share': round(share, 2), 'count': n})
    rows.sort(key=lambda r: -r['share'])
    return {'from': len(pool), 'decided': len(decided), 'tags': rows[:top]}


def genre_catalogue(tracks, limit=None):
    """Every genre you use, with counts — what the field autocompletes against.

    ⚠ This is the guard rail the genre field never had. Correcting spellings
    after the fact is losing ground; offering what you already use, at the
    moment of typing, is where a vocabulary stops drifting.
    """
    counts = {}
    for t in tracks:
        for g in t['genres']:
            counts[g] = counts.get(g, 0) + 1
    fixes = sb.genre_fixes()
    rows = [{'name': g, 'count': c, 'fix_to': fixes.get(g) or ''}
            for g, c in counts.items()]
    rows.sort(key=lambda r: (-r['count'], r['name'].lower()))
    return rows[:limit] if limit else rows


# ───────────────────────────────────────────────────────────────── queue

def _read_queue():
    try:
        with open(QUEUE_FILE, encoding='utf-8') as fh:
            return json.load(fh) or []
    except Exception:
        return []


def _write_queue(items):
    """Write the queue so a power cut can never leave it half-written.

    ⚠ The rename is atomic, but that alone is not enough: without an fsync the
    data may still be in the page cache when the rename hits the disk, and a
    hard crash in that window can bring the file back EMPTY. The queue can hold
    an evening's worth of decisions, so it gets the same durability the
    converter gives an audio file before it bins the original: flush the
    contents, then flush the directory that now points at them.
    """
    os.makedirs(settings.APP_DIR, exist_ok=True)
    tmp = QUEUE_FILE + '.part'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(items, fh, indent=1, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, QUEUE_FILE)
    try:
        fd = os.open(settings.APP_DIR, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass                      # best-effort: the contents are already safe


def enqueue(ops):
    """Add operations to the queue. Returns how many are now waiting.

    ⚠ An `add_tag` cancels a pending `remove_tag` for the same pair (and the
    other way round) instead of stacking: pressing a key twice is a change of
    mind, not two things to apply.
    """
    now = datetime.datetime.now().isoformat(timespec='seconds')
    with _QUEUE_LOCK:
        items = _read_queue()
        for op in ops or []:
            kind, track = op.get('op'), str(op.get('track') or '')
            if kind not in OPS or not track:
                continue
            opposite = {'add_tag': 'remove_tag', 'remove_tag': 'add_tag',
                        'playlist_add': 'playlist_remove',
                        'playlist_remove': 'playlist_add'}.get(kind)
            # A tag is told apart by its name; a playlist by its id.
            what = 'playlist' if kind.startswith('playlist_') else 'tag'
            paired = ('add_tag', 'remove_tag', 'playlist_add', 'playlist_remove')

            def _same_field(i):
                return (i['track'] == track and (
                    (opposite and i['op'] == opposite and i.get(what) == op.get(what))
                    # A second value for the same single-value field replaces it.
                    or (kind not in paired and i['op'] == kind)
                    or (kind in paired and i['op'] == kind
                        and i.get(what) == op.get(what))))

            # ⚠ CARRY THE BASELINE FORWARD. Only the last op on a single-value
            # field survives, so it must keep the baseline of the FIRST one —
            # that is the value the database still holds. Taking the newcomer's
            # `before` (which describes the edit it is replacing, never the DB)
            # made the compare-and-set fail and BOTH edits vanish silently:
            # edit a comment twice before a sync and you lost the lot.
            replaced = [i for i in items if _same_field(i)]
            items = [i for i in items if not _same_field(i)]
            entry = {'op': kind, 'track': track, 'at': now,
                     'id': uuid.uuid4().hex}
            for k in ('tag', 'value', 'before', 'playlist'):
                if k in op:
                    entry[k] = op[k]
            oldest = next((i for i in replaced
                           if i['op'] == kind and 'before' in i), None)
            if oldest is not None:
                entry['before'] = oldest['before']
            items.append(entry)
        _write_queue(items)
        return len(items)


def pending():
    """What is waiting, and since when — for the counter and the list markers."""
    items = _read_queue()
    tracks = sorted({i['track'] for i in items})
    oldest = min((i.get('at', '') for i in items), default='')
    # ⚠ track_ids so the UI can restore the "queued" markers after a reload —
    # otherwise the durable queue and the list silently disagree.
    return {'count': len(items), 'tracks': len(tracks),
            'track_ids': tracks, 'oldest': oldest}


def held_up():
    """What is waiting for rekordbox to close, in one line for the screens.

    ⚠ This exists because of an evening lost in silence. Seventy-seven changes
    sat in the queue while rekordbox was open — colours, ratings, energy, tags
    — and NOTHING said so: the Browse tab showed the effective values, the menu
    bar said "saved", and the only way to learn they were not in rekordbox was
    to open rekordbox and not find them. A queue that fills up quietly is
    indistinguishable from a bug. So every screen shows this while it is
    non-empty: how much, how many tracks, and since when.
    """
    p = pending()
    if not p['count']:
        return {'count': 0, 'tracks': 0, 'oldest': '', 'rbOpen': _rb_open_cached()}
    return {'count': p['count'], 'tracks': p['tracks'], 'oldest': p['oldest'],
            'rbOpen': _rb_open_cached()}


_RB_OPEN = {'at': 0.0, 'val': None}


def _rb_open_cached(max_age=3.0):
    """rekordbox_running(), remembered for a few seconds.

    The screens poll every second; asking the process list each time is a
    subprocess per poll for an answer that changes once an hour.
    """
    import time
    now = time.time()
    if _RB_OPEN['val'] is None or now - _RB_OPEN['at'] > max_age:
        try:
            import rekordbox_merge as rm
            _RB_OPEN['val'] = bool(rm.rekordbox_running())
        except Exception:
            _RB_OPEN['val'] = True
        _RB_OPEN['at'] = now
    return _RB_OPEN['val']


def clear():
    with _QUEUE_LOCK:
        _write_queue([])


# ───────────────────────────────────────────── what is open in the Tagger
# The menu bar popover is a REMOTE for the Tagger, not a second tagger: it acts
# on whatever track the Tagger has open (which is also the one playing, since
# opening a track starts it). The browser reports that here; the popover reads
# it. In memory on purpose — it describes this session, not your library, and
# a stale "now playing" surviving a restart would be a lie.
_NOW_LOCK = threading.Lock()
_NOW = {'track': None, 'at': None}


def set_now(track_id):
    with _NOW_LOCK:
        _NOW['track'] = str(track_id) if track_id else None
        _NOW['at'] = datetime.datetime.now().isoformat(timespec='seconds')
    return dict(_NOW)


def get_now():
    with _NOW_LOCK:
        return dict(_NOW)


# The menu bar can ask to move through the queue or jump somewhere in the
# track, but it knows neither the queue nor the audio — the browser owns both.
# So these are REQUESTS parked here, which the Tagger picks up on its pulse.
# ⚠ A counter, not a flag: the browser acts when the number MOVES, so a request
# is never executed twice and never silently lost between two polls.
_CMD = {'seq': 0, 'kind': '', 'dir': 0, 'frac': 0.0}
# ⚠ Woken the instant an order arrives. Orders used to ride the 700 ms pulse,
# so dragging the playhead in the menu bar took up to a beat to land — long
# enough to feel broken, and long enough to miss if the beat fell badly.
_CMD_EVENT = threading.Event()

# Where the browser's player currently is. Reported by the browser, read by the
# popover to draw its playhead. ⚠ `at` travels with it: the popover polls once
# a second and advances the head itself in between, which would drift without
# knowing how old the reading is.
_PLAY = {'pos': 0.0, 'dur': 0.0, 'playing': False, 'at': 0.0, 'vol': 1.0}


def request_nav(direction):
    with _NOW_LOCK:
        _CMD['seq'] += 1
        _CMD['kind'] = 'nav'
        _CMD['dir'] = 1 if int(direction or 0) > 0 else -1
        out = dict(_CMD)
    _CMD_EVENT.set()
    return out


def request_seek(frac):
    with _NOW_LOCK:
        _CMD['seq'] += 1
        _CMD['kind'] = 'seek'
        _CMD['frac'] = max(0.0, min(1.0, float(frac or 0)))
        out = dict(_CMD)
    _CMD_EVENT.set()
    return out


def request_play():
    """Ask the browser to start or stop.

    ⚠ The popover has NO audio of its own — the track is playing in the
    browser tab, so its play button is a remote control like ‹ and ›, not a
    second player. It toggles rather than saying play/pause because only the
    browser knows for certain which of the two it currently is.
    """
    with _NOW_LOCK:
        _CMD['seq'] += 1
        _CMD['kind'] = 'play'
        out = dict(_CMD)
    _CMD_EVENT.set()
    return out


def request_volume(vol):
    """Ask the player to change its volume (0..1). Same remote as play/seek:
    the popover has no audio, the app window does."""
    try:
        v = max(0.0, min(1.0, float(vol)))
    except (TypeError, ValueError):
        v = 1.0
    with _NOW_LOCK:
        _CMD['seq'] += 1
        _CMD['kind'] = 'volume'
        _CMD['vol'] = v
        _PLAY['vol'] = v             # so the popover's next look agrees at once
        out = dict(_CMD)
    _CMD_EVENT.set()
    return out


def request_load(track):
    """Ask the browser to load and play one particular track.

    The triage queue decides what comes next, not the list the browser happens
    to have on screen — so the order carries the track itself rather than a
    direction to move in.
    """
    with _NOW_LOCK:
        _CMD['seq'] += 1
        _CMD['kind'] = 'load'
        _CMD['track'] = track
        out = dict(_CMD)
    _CMD_EVENT.set()
    return out


# ───────────────────────────────────────────────────────────────── triage

def pending_colors():
    """{track id: colour} for every colour change still waiting in the queue.

    ⚠ ONE read of the queue for the whole library. The triage count is asked
    for every second, and the per-track helpers read the file once each.
    """
    out = {}
    for i in _read_queue():
        if i.get('op') == 'set_color':
            out[str(i.get('track'))] = i.get('value') or ''
    return out


def _triage_key(t):
    return (t.get('added') or '', str(t.get('id')))


def triage_queue(tracks, color):
    """Every track still waiting to be triaged, newest first.

    "Waiting" means coloured `color` (the import colour, ADD) once the queue
    has landed — so a track you have just marked done leaves it at once,
    even while rekordbox is open and the change is only queued.
    """
    want = (color or '').strip().upper()
    if not want:
        return []
    pend = pending_colors()
    rows = [t for t in tracks
            if (pend.get(str(t['id']), t.get('color')) or '').strip().upper() == want]
    rows.sort(key=_triage_key, reverse=True)
    return rows


def triage_next(tracks, color, from_id):
    """The track after `from_id` in the triage order, and how many are left.

    ⚠ Works whether or not `from_id` is still waiting — and the usual case is
    that it has JUST stopped waiting, because you marked it done a moment ago.
    Its place is found from its date and id, never by looking it up in the
    list: that lookup is exactly what failed in Browse, and why › did nothing
    once the track you were on had left the filter.
    """
    q = [t for t in triage_queue(tracks, color) if str(t['id']) != str(from_id)]
    if not q:
        return None, 0
    cur = next((t for t in tracks if str(t['id']) == str(from_id)), None)
    if cur is not None:
        key = _triage_key(cur)
        for t in q:
            if _triage_key(t) < key:
                return t, len(q)
    return q[0], len(q)          # past the oldest, or nothing open: the newest


def wait_for_cmd(since, timeout=20.0):
    """Block until an order newer than `since` exists, or the timeout.

    The player lives in the browser and the controls in the menu bar, so an
    order has to cross between them. Held open like this it arrives the moment
    it is given, instead of waiting for the next beat of a poll.
    """
    import time
    end = time.time() + timeout
    while True:
        with _NOW_LOCK:
            cur = dict(_CMD)
        if int(cur.get('seq') or 0) != int(since or 0):
            return cur
        left = end - time.time()
        if left <= 0:
            return cur
        _CMD_EVENT.wait(min(left, 1.0))
        _CMD_EVENT.clear()


def get_nav():
    with _NOW_LOCK:
        return dict(_CMD)


def set_playback(pos, dur, playing, vol=None):
    import time
    with _NOW_LOCK:
        _PLAY.update({'pos': float(pos or 0), 'dur': float(dur or 0),
                      'playing': bool(playing), 'at': time.time()})
        if vol is not None:
            try:
                _PLAY['vol'] = max(0.0, min(1.0, float(vol)))
            except (TypeError, ValueError):
                pass
        return dict(_CMD)          # the browser reads its orders in the reply


def get_playback():
    with _NOW_LOCK:
        return dict(_PLAY)


def queued_tag_changes(track_id):
    """{tag: True to add, False to remove} still waiting for this track."""
    out = {}
    for i in _read_queue():
        if str(i.get('track')) != str(track_id):
            continue
        if i.get('op') == 'add_tag':
            out[i.get('tag')] = True
        elif i.get('op') == 'remove_tag':
            out[i.get('tag')] = False
    return out


def effective_scalars(track_id, base):
    """The single-value fields as they WILL be: library value plus the queue.

    `base` is what rekordbox holds right now; the return is what the user
    should see. ⚠ Both are needed by the client: it must SHOW the effective
    value but send `before` as the LIBRARY one, because enqueue() replaces a
    previous op on the same field — so only the last one survives, and its
    baseline has to be what the database will actually still hold at apply
    time. Sending the queued value as `before` would make it skip itself.
    """
    out = dict(base or {})
    # ⚠ Title and artist too: with rekordbox open a rename waits in the queue,
    # and without them here every screen went back to showing the old name.
    field = {'set_rating': 'rating', 'set_comment': 'comment',
             'set_genre': 'genre', 'set_color': 'color',
             'set_title': 'title', 'set_artist': 'artist'}
    for i in _read_queue():
        if str(i.get('track')) != str(track_id):
            continue
        key = field.get(i.get('op'))
        if key:
            out[key] = i.get('value')
    return out


def effective_tags(track_id, base_tags):
    """The tags this track WILL have: what rekordbox holds, plus the queue.

    ⚠ Both surfaces must render this, not the raw library tags. The popover and
    the browser panel write to the same queue, so showing the library alone
    would make a tag you just ticked in the menu bar look like it never landed.
    """
    eff = {t: True for t in (base_tags or []) if t}
    for tag, on in queued_tag_changes(track_id).items():
        if not tag:
            continue
        if on:
            eff[tag] = True
        else:
            eff.pop(tag, None)
    return sorted(eff)


def apply_now_if_possible():
    """Write the queue to rekordbox right away, if rekordbox is closed.

    ⚠ The queue exists for ONE reason: master.db cannot be written while
    rekordbox has it open. When it does not, there is nothing to wait for — so
    an edit simply lands, and you never think about a queue at all. With
    rekordbox open it stays queued, which is the only safe answer.
    """
    import rekordbox_merge as rm
    try:
        if rm.rekordbox_running():
            return {'applied': 0, 'deferred': True, 'applied_ops': [],
                    'why': 'rekordbox is open — saved to apply on Sync'}
        with _APPLY_LOCK:
            return dict(apply_queue(dry_run=False, backup_max_age=600),
                        deferred=False)
    except Exception as e:
        return {'applied': 0, 'deferred': True, 'applied_ops': [],
                'why': str(e)[:160]}


def apply_queue(dry_run=True, backup_max_age=None):
    """Write the queue into rekordbox. Only with rekordbox CLOSED.

    Each operation is applied on its own and failures are reported per item:
    one track whose row has since disappeared must not strand the other forty.
    """
    import uuid
    import rekordbox_merge as rm
    from pyrekordbox.db6 import tables

    items = _read_queue()
    if not items:
        return {'applied': 0, 'failed': [], 'dry_run': dry_run, 'backup': None}
    if not dry_run and rm.rekordbox_running():
        raise RuntimeError('Close rekordbox to apply the queued changes.')

    backup = None if dry_run else rm.backup_db(max_age=backup_max_age)
    db = rm._open_db()
    done, failed, applied_ids = 0, [], set()
    # ⚠ The ops that actually LANDED (not the skipped ones): the server patches
    # its in-memory library from this list, so it must describe exactly what
    # the database now holds — nothing more.
    applied_ops = []
    skipped = []
    now = datetime.datetime.now()

    def current_scalar(content, op_kind):
        # ⚠ For compare-and-set: what the field holds in the DB RIGHT NOW.
        if op_kind == 'set_comment':
            return (content.Commnt or '')
        if op_kind == 'set_rating':
            return str(content.Rating or 0)
        if op_kind == 'set_genre':
            g = db.query(tables.DjmdGenre).filter_by(ID=str(content.GenreID)).first() \
                if content.GenreID else None
            return (getattr(g, 'Name', '') or '') if g else ''
        if op_kind == 'set_color':
            col = db.query(tables.DjmdColor).filter_by(ID=str(content.ColorID)).first() \
                if content.ColorID else None
            return (getattr(col, 'Commnt', '') or '') if col else ''
        if op_kind == 'set_title':
            return (content.Title or '')
        if op_kind == 'set_artwork':
            return (content.ImagePath or '')
        if op_kind == 'set_artist':
            a = db.query(tables.DjmdArtist).filter_by(ID=str(content.ArtistID)).first() \
                if content.ArtistID else None
            return (getattr(a, 'Name', '') or '') if a else ''
        return None
    try:
        # Tag names -> ids, so the queue can store names and survive anything.
        tagid = {}
        for row in db.query(tables.DjmdMyTag).filter_by(rb_local_deleted=0):
            tagid[(row.Name or '').strip()] = str(row.ID)

        created = created_playlists()
        made_now = {}
        for it in items:
            try:
                if it['op'] == 'playlist_create':
                    # ⚠ Before get_content: this op is about a playlist, and
                    # its `track` is only the queue's key ('pl:<key>').
                    key = str(it.get('playlist') or '')[4:]
                    have = created.get(key)
                    if have and db.get_playlist(ID=have) is not None:
                        pass                                    # already made
                    elif not dry_run:
                        pl = db.create_playlist((it.get('value') or 'Playlist').strip() or 'Playlist')
                        db.flush()
                        made_now[key] = str(pl.ID)
                    done += 1
                    applied_ids.add(it.get('id'))
                    if not dry_run:
                        applied_ops.append(dict(it))
                    continue
                c = db.get_content(ID=str(it['track']))
                if c is None:
                    raise RuntimeError('the track is no longer in the library')
                op = it['op']
                if op == 'add_tag':
                    tid = tagid.get(it.get('tag'))
                    if not tid:
                        raise RuntimeError('no MyTag called %r' % it.get('tag'))
                    already = db.query(tables.DjmdSongMyTag).filter_by(
                        ContentID=str(it['track']), MyTagID=tid,
                        rb_local_deleted=0).first()
                    if not already and not dry_run:
                        db.add(tables.DjmdSongMyTag.create(
                            ID=str(uuid.uuid4()), MyTagID=tid,
                            ContentID=str(it['track']), TrackNo=0,
                            UUID=str(uuid.uuid4()), created_at=now, updated_at=now))
                elif op == 'remove_tag':
                    tid = tagid.get(it.get('tag'))
                    for row in db.query(tables.DjmdSongMyTag).filter_by(
                            ContentID=str(it['track']), MyTagID=tid,
                            rb_local_deleted=0):
                        if not dry_run:
                            row.rb_local_deleted = 1
                elif op in ('playlist_add', 'playlist_remove'):
                    # ⚠ At the END of the playlist, numbered after the last
                    # live row, and after a removal the rest renumbered 1..N:
                    # rekordbox orders a playlist by TrackNo and nothing else.
                    pid = str(it.get('playlist') or '')
                    if pid.startswith('new:'):
                        pid = made_now.get(pid[4:]) or created.get(pid[4:]) or ''
                        if not pid:
                            if dry_run:
                                raise _Skip('its playlist is created in the same sync')
                            raise RuntimeError('its playlist has not been created yet')
                    plist = db.get_playlist(ID=pid) if pid else None
                    if plist is None or plist.rb_local_deleted:
                        raise RuntimeError('that playlist is no longer in rekordbox')
                    if plist.Attribute != 0:
                        raise RuntimeError('only ordinary playlists take tracks by hand')
                    rows = [r for r in db.query(tables.DjmdSongPlaylist).filter_by(
                        PlaylistID=pid, rb_local_deleted=0)]
                    mine = [r for r in rows if str(r.ContentID) == str(it['track'])]
                    if op == 'playlist_add' and not mine and not dry_run:
                        last = max([r.TrackNo or 0 for r in rows] or [0])
                        db.add(tables.DjmdSongPlaylist.create(
                            ID=str(uuid.uuid4()), PlaylistID=pid,
                            ContentID=str(it['track']), TrackNo=last + 1,
                            UUID=str(uuid.uuid4()), created_at=now, updated_at=now))
                    elif op == 'playlist_remove' and mine and not dry_run:
                        for r in mine:
                            r.rb_local_deleted = 1
                        keep = sorted((r for r in rows if r not in mine),
                                      key=lambda r: r.TrackNo or 0)
                        for n, r in enumerate(keep, 1):
                            if r.TrackNo != n:
                                r.TrackNo = n
                                r.updated_at = now
                elif op == 'set_artwork':
                    # ⚠ Only where there is none: a cover set in rekordbox
                    # since (by hand, or by a re-import) is never replaced.
                    if (c.ImagePath or '').strip():
                        raise _Skip('it already has artwork in rekordbox')
                    src = it.get('value') or ''
                    if not os.path.isfile(src):
                        raise RuntimeError('the downloaded cover is no longer there')
                    if not dry_run:
                        c.ImagePath = _write_rb_artwork(src)
                elif op == 'set_comment':
                    if 'before' in it and not _baseline_matches(
                            op, current_scalar(c, op), it['before']):
                        raise _Skip('changed in rekordbox since you queued it')
                    if not dry_run:
                        c.Commnt = it.get('value') or ''
                elif op == 'set_rating':
                    if 'before' in it and not _baseline_matches(
                            op, current_scalar(c, op), it['before']):
                        raise _Skip('changed in rekordbox since you queued it')
                    if not dry_run:
                        c.Rating = int(it.get('value') or 0)
                elif op == 'set_genre':
                    # ⚠ Genres are their own ENTITY table: the ID must be a
                    # rekordbox numeric id (generate_unused_id), NOT a uuid4 —
                    # uuid4 is only for junction rows. And the lookup must be on
                    # LIVE rows, or it could reuse a soft-deleted genre and leave
                    # the track's genre blank.
                    name = (it.get('value') or '').strip()
                    if 'before' in it and not _baseline_matches(
                            op, current_scalar(c, op), it['before']):
                        raise _Skip('changed in rekordbox since you queued it')
                    # ⚠ An EMPTY value means "clear it", and that is a write.
                    # Gating the whole branch on `name` made clearing a genre
                    # count as applied, drop out of the queue and repaint as
                    # empty — while the database kept the old genre for ever.
                    if not dry_run and not name:
                        c.GenreID = None
                    elif not dry_run:
                        g = db.query(tables.DjmdGenre).filter_by(
                            Name=name, rb_local_deleted=0).first()
                        if g is None:
                            g = tables.DjmdGenre.create(
                                ID=str(db.generate_unused_id(tables.DjmdGenre)),
                                Name=name, UUID=str(uuid.uuid4()),
                                created_at=now, updated_at=now)
                            db.add(g)
                            db.flush()
                        c.GenreID = str(g.ID)
                elif op == 'set_title':
                    if 'before' in it and not _baseline_matches(
                            op, current_scalar(c, op), it['before']):
                        raise _Skip('changed in rekordbox since you queued it')
                    if not dry_run:
                        c.Title = (it.get('value') or '').strip()
                elif op == 'set_artist':
                    # ⚠ Artists are their OWN table, like genres: look the name
                    # up among LIVE rows and only create one when it is new —
                    # pyrekordbox's add_artist raises on a name already there.
                    name = (it.get('value') or '').strip()
                    if 'before' in it and not _baseline_matches(
                            op, current_scalar(c, op), it['before']):
                        raise _Skip('changed in rekordbox since you queued it')
                    if not dry_run:
                        if not name:
                            c.ArtistID = None
                        else:
                            a = db.query(tables.DjmdArtist).filter_by(
                                Name=name, rb_local_deleted=0).first()
                            if a is None:
                                a = tables.DjmdArtist.create(
                                    ID=str(db.generate_unused_id(tables.DjmdArtist)),
                                    Name=name, UUID=str(uuid.uuid4()),
                                    created_at=now, updated_at=now)
                                db.add(a)
                                db.flush()
                            c.ArtistID = str(a.ID)
                elif op == 'set_color':
                    name = (it.get('value') or '').strip()
                    if 'before' in it and not _baseline_matches(
                            op, current_scalar(c, op), it['before']):
                        raise _Skip('changed in rekordbox since you queued it')
                    # ⚠ "—" in the picker means NO colour. Without this the
                    # lookup for '' found nothing, raised, and the op sat in
                    # the queue failing for ever on every later edit.
                    if not name:
                        if not dry_run:
                            c.ColorID = None
                        col = True          # nothing to look up
                    else:
                        col = db.query(tables.DjmdColor).filter_by(Commnt=name).first()
                        if col is None:
                            # ⚠ Colour names are typed by hand in rekordbox, so
                            # they pick up stray spaces and odd casing that are
                            # invisible in its UI — this library really does have
                            # a colour called ' BEATGRID'. An exact match would
                            # fail on a name the user can SEE is right, so fall
                            # back to comparing them trimmed and case-blind.
                            want = name.strip().casefold()
                            for row in db.query(tables.DjmdColor):
                                if (row.Commnt or '').strip().casefold() == want:
                                    col = row
                                    break
                        if col is None:
                            raise RuntimeError('no colour called %r' % name)
                        if not dry_run and name:
                            c.ColorID = str(col.ID)
                done += 1
                applied_ids.add(it.get('id'))
                if not dry_run:
                    applied_ops.append(dict(it))
            except _Skip as sk:
                # A concurrent rekordbox edit: drop the op (do not clobber) and
                # report it, but do NOT keep it queued forever.
                applied_ids.add(it.get('id'))
                skipped.append({'track': it.get('track'), 'op': it.get('op'),
                                'why': str(sk)})
            except Exception as e:
                # ⚠ A failing op is retried, but NOT for ever. Auto-save reruns
                # the whole queue on every edit, so one poison entry (a track
                # deleted in rekordbox, a MyTag that no longer exists) used to
                # fail silently on every single save until the end of time.
                # After three goes it is given up on and reported.
                tries = int(it.get('tries') or 0) + 1
                it['tries'] = tries
                gave_up = tries >= 3
                if gave_up and not dry_run:
                    applied_ids.add(it.get('id'))     # stop retrying it
                failed.append({'track': it.get('track'), 'op': it.get('op'),
                               'why': str(e)[:120], 'tries': tries,
                               'gave_up': gave_up})

        if not dry_run:
            db.commit()
            if made_now:
                created.update(made_now)
                _save_created(created)
        # Persist the attempt counters of the ops that stay queued.
        if not dry_run and failed:
            with _QUEUE_LOCK:
                cur = _read_queue()
                bumped = {i['id']: i.get('tries') for i in items if i.get('tries')}
                for i in cur:
                    if i.get('id') in bumped:
                        i['tries'] = bumped[i['id']]
                _write_queue(cur)
    finally:
        try:
            db.close()
        except Exception:
            pass

    if not dry_run:
        with _QUEUE_LOCK:
            current = _read_queue()
            remaining = [i for i in current if i.get('id') not in applied_ids]
            _write_queue(remaining)
        still = len(remaining)
    else:
        still = len(items)
    return {'applied': done, 'failed': failed, 'skipped': skipped,
            'applied_ops': applied_ops,
            'dry_run': dry_run, 'backup': backup, 'still_queued': still}
