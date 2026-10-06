"""A starter vocabulary: MyTag groups and colour labels for a library that has none.

Every screen here leans on a library's own words — the kind of night, the
mood, where in the set a track goes — and works out which group is which from
what is in it (settings.guess_banks). A library with no MyTags yet has none of
that, and the set builder, the tagger and Teach all open onto nothing.

So this offers a vocabulary to start from. It was DISTILLED from one real
library of 2,588 tracks tagged by hand for a year, with fifty tags in four
groups, by looking at what each tag actually added:

  · "Dance" and "Cool" were on half the library each. A tag on everything
    says nothing; gone.
  · "Uptempo", "Downtempo": the BPM already says it. Gone.
  · "Latin", "Arab", "Flamenco", "Brasil", "French": the genre says it.
  · "Scary" was on 107 tracks and every one was also "Dark" — one tag. The
    same for the clusters below: Aggressive → Hard; Bounce, Dirty → Groovy;
    Funny, Party → Happy; Uplifting → Euphoric; Beautiful, Soulful,
    Profound → Emotional; Sad → Melancholic.
  · "Nowhere" (a night it fits none of) is what NO tag means. "Sunrise"
    lived inside Sunset and After.
  · Housekeeping tags ("TO BE CHECKED", "CHECK", "OK") are what colours are
    for, so they are colours here.

Fifty became twenty-five, and the four groups still answer the three questions
a set is built from: what kind of night, what it feels like, when it plays.

⚠ It only ADDS. A group or tag that already exists (same name, any case) is
left exactly as it is, and colour labels are renamed only while they still
have rekordbox's factory names — a label somebody chose is theirs.
"""

import datetime
import uuid

#: (group, what the group is for, [(tag, what it means)]). The order is the
#: order they appear in rekordbox and in the tagger.
STARTER = [
    ('TYPE OF SET', 'The kind of night a track belongs to. Pick a night in Sets and these are its tracks.', [
        ('Rave', 'late, loud and relentless'),
        ('After', 'after-hours: hypnotic, deep, long'),
        ('Sunset', 'open air and golden hour: warm, melodic'),
        ('Bar', 'before the party: background that can grow'),
        ('Party', 'crowd-pleasers people sing along to'),
        ('Family', 'weddings, all ages, daytime'),
    ]),
    ('MOOD', 'What it feels like. The set builder moves between these.', [
        ('Dark', 'menacing, tense, night-time'),
        ('Hard', 'aggressive, distorted, pounding'),
        ('Acid', 'the squelch of a 303'),
        ('Psychedelic', 'trippy, twisting, hypnotic'),
        ('Tribal', 'percussion first: drums, chants, world'),
        ('Groovy', 'bouncy, swung, makes you move'),
        ('Sexy', 'slinky, sensual'),
        ('Happy', 'bright, fun, playful'),
        ('Euphoric', 'uplifting, hands-in-the-air'),
        ('Emotional', 'beautiful, soulful, deep'),
        ('Melancholic', 'bittersweet, sad'),
        ('Chill', 'relaxed, low pressure'),
    ]),
    ('TIMING', 'Where in the set it plays, in running order.', [
        ('Opener', 'starts a set'),
        ('Warm-up', 'builds the room'),
        ('Middle', 'the body of the set'),
        ('Peak', 'the top of the night'),
        ('Closer', 'brings it home'),
    ]),
    ('MISC', 'Notes that are not a set context.', [
        ('Anthem', 'everybody knows it'),
        ('Tool', 'loops, drums, transitions'),
    ]),
]

#: Colour labels for the tagging workflow: (rekordbox slot 1-8, label, meaning).
#: A track comes in NEW, is tagged and becomes GRID (it still needs its
#: beatgrid checked in rekordbox), and READY once rekordbox is done with it.
WORKFLOW_COLOURS = [
    (1, 'NEW', 'just imported, not listened to yet'),
    (4, 'GRID', 'tagged here; check the beatgrid in rekordbox'),
    (5, 'READY', 'ready to play'),
]

#: What rekordbox calls its eight colours out of the box. A label with one of
#: these names (or none) has not been chosen by anyone.
FACTORY_COLOURS = {'', 'pink', 'red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple'}

#: The settings that go with the workflow colours.
WORKFLOW_SETTINGS = {'import_color': 'NEW', 'done_color': 'GRID', 'ready_color': 'READY'}


def _existing(db):
    from pyrekordbox.db6 import tables
    groups, tags = {}, {}
    for row in db.query(tables.DjmdMyTag).filter_by(rb_local_deleted=0):
        if row.Attribute == 1:
            groups[(row.Name or '').strip().lower()] = row
    for row in db.query(tables.DjmdMyTag).filter_by(rb_local_deleted=0):
        if row.Attribute != 1:
            tags.setdefault(str(row.ParentID), set()).add((row.Name or '').strip().lower())
    return groups, tags


def _colours(db):
    from pyrekordbox.db6 import tables
    return {int(c.SortKey or 0): c for c in db.query(tables.DjmdColor).filter_by(rb_local_deleted=0)}


def plan(db=None):
    """What applying the starter would add, without writing anything.

    {'groups': [{name, about, exists, tags: [{name, about, exists}]}],
     'colours': [{slot, name, about, current, rename}], 'adds': n}
    """
    import rekordbox_merge as rm
    own = db is None
    db = db or rm._open_db()
    try:
        groups, tags = _existing(db)
        out, adds = [], 0
        for gname, about, items in STARTER:
            g = groups.get(gname.lower())
            have = tags.get(str(g.ID), set()) if g is not None else set()
            rows = [{'name': n, 'about': a, 'exists': n.lower() in have} for n, a in items]
            adds += (g is None) + sum(1 for r in rows if not r['exists'])
            out.append({'name': gname, 'about': about, 'exists': g is not None, 'tags': rows})
        cols = _colours(db)
        colours = []
        for slot, name, about in WORKFLOW_COLOURS:
            cur = ((cols[slot].Commnt if slot in cols else '') or '').strip()
            rename = cur.lower() in FACTORY_COLOURS and cur != name
            adds += rename
            colours.append({'slot': slot, 'name': name, 'about': about,
                            'current': cur, 'rename': rename})
        return {'groups': out, 'colours': colours, 'adds': adds}
    finally:
        if own:
            db.close()


def apply(with_colours=True):
    """Add the starter vocabulary to rekordbox. Only with rekordbox CLOSED,
    after a backup. Returns what it added."""
    import rekordbox_merge as rm
    import settings
    from pyrekordbox.db6 import tables

    if rm.rekordbox_running():
        raise RuntimeError('Close rekordbox first: it is the only time its library can be written.')
    backup = rm.backup_db()
    db = rm._open_db()
    added_groups, added_tags, renamed = [], [], []
    try:
        now = datetime.datetime.now()
        groups, tags = _existing(db)

        def new_row(name, attribute, parent, seq):
            # ⚠ Created under a placeholder and THEN named, the way pyrekordbox
            # creates playlists: the rename is what gives the row a fresh USN,
            # which is how rekordbox notices a row it did not write itself.
            row = tables.DjmdMyTag.create(
                ID=str(db.generate_unused_id(tables.DjmdMyTag, is_28_bit=True)),
                Seq=seq, Name='New', Attribute=attribute, ParentID=parent,
                UUID=str(uuid.uuid4()), created_at=now, updated_at=now)
            db.add(row)
            db.flush()
            row.Name = name
            return row

        top = max([g.Seq or 0 for g in groups.values()] or [0])
        for gname, _, items in STARTER:
            g = groups.get(gname.lower())
            if g is None:
                top += 1
                g = new_row(gname, 1, 'root', top)
                added_groups.append(gname)
            have = tags.get(str(g.ID), set())
            seq = len(have)
            for name, _ in items:
                if name.lower() in have:
                    continue
                seq += 1
                new_row(name, 0, str(g.ID), seq)
                added_tags.append('%s / %s' % (gname, name))

        if with_colours:
            cols = _colours(db)
            for slot, name, _ in WORKFLOW_COLOURS:
                c = cols.get(slot)
                if c is None:
                    continue
                cur = (c.Commnt or '').strip()
                if cur.lower() in FACTORY_COLOURS and cur != name:
                    c.Commnt = name
                    c.updated_at = now
                    renamed.append(name)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # The colours mean nothing until the settings point at them. Only the
    # ones that are still at their defaults: a choice made in Settings stays.
    if renamed:
        cur = settings.load()
        changes = {k: v for k, v in WORKFLOW_SETTINGS.items()
                   if v in renamed
                   and (cur.get(k) or '').strip() in ('', settings.DEFAULTS.get(k, ''))}
        if changes:
            settings.save(changes)
    return {'groups': added_groups, 'tags': added_tags, 'colours': renamed,
            'backup': backup}
