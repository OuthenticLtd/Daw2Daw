"""Take a .cpr's object stream apart, change its shape, and put it back.

The in-place writer (cpr_write) can only change fields that keep their size,
because moving a byte invalidates every offset stored after it. This module
lifts that restriction: it parses the whole object stream into a tree, lets
callers delete, duplicate and splice records, then re-emits everything with
recomputed sizes and rewrites every stored offset to match.

The format makes that possible because references are countable. An object
reference is the plain byte offset of that object's size field; a class
reference is the high bit plus the byte offset of the record defining the
class. A six-megabyte project holds roughly four hundred of the first and
sixteen hundred of the second, and both can be found by value once the set
of legitimate targets is known.

Verified on a real project: parsing and re-emitting it unchanged gives back
a byte-identical payload, and renaming a track to a name twenty-five bytes
longer moves a thousand objects, rewrites two hundred references, and still
reads back with every track, marker, part and note intact.
"""
import bisect
import struct

from . import arch

HI = arch.HI
MASK = arch.MASK


class Node(object):
    """One object in the stream, plus whatever the caller wants changed."""

    __slots__ = ('hdr', 'ds', 'de', 'cls', 'kids', 'sf', 'new_sf',
                 'deleted', 'splices', 'extra', 'clone', 'applied', '_done',
                 'placed', 'origin_node', 'ref_class',
                 'force_class', 'new_hdr', 'declares')

    def __init__(self, hdr, ds, de, cls):
        self.hdr = hdr          # first byte of the object header
        self.ds = ds            # first byte of its data
        self.de = de            # one past its last data byte
        self.cls = cls
        self.kids = []
        self.sf = ds - 8        # its size field: what references point at
        self.new_sf = None
        self.new_hdr = None
        self.declares = False
        self.deleted = False
        self.splices = []       # (start, end, bytes) inside this object's data
        self.extra = []         # (before_child_or_None, Node) to insert
        self.clone = False      # a copy: emits the same bytes somewhere else
        self.placed = None      # where a copy of it was written out
        self.origin_node = None # the record a copy was taken from
        self.ref_class = False  # point at the original's class declaration
        self.force_class = None # declare it as this class instead

    def __repr__(self):
        return '<%s %#x..%#x %s>' % (self.cls, self.hdr, self.de,
                                     'deleted' if self.deleted else '')

    def splice(self, start, end, raw):
        """Replace the bytes [start, end) of the payload with `raw`.

        The range has to lie in this object's own bytes without straddling a
        nested object. Use replace() when it does.

        Writing the same range twice keeps the later write. Appending both
        used to emit both payloads back to back - one record where a string
        was renamed and then renamed again carried two strings, and Cubase
        read everything after the first as garbage."""
        for i, (s0, e0, _r) in enumerate(self.splices):
            if s0 == start and e0 == end:
                self.splices[i] = (start, end, raw)
                return
        self.splices.append((start, end, raw))

    def replace(self, start, end, raw):
        """Replace [start, end) even though objects are nested inside it.

        A MIDI part's note list is bytes and objects interleaved, so putting
        new notes in means the objects belonging to the old ones go too.
        They are dropped and the payload takes their place."""
        self.splices.append((start, end, raw))
        for k in list(self.kids):
            if k.hdr >= start and k.de <= end:
                k.deleted = True


def parse(A):
    """The whole payload as a containment tree."""
    index = {}
    root = parse_at(A, 0, len(A.d), index)
    return root, index


def parse_at(A, start, limit, index):
    """The object at `start` and everything nested in it, into `index`."""
    def walk(o, limit):
        try:
            ob = A.read_obj(o, limit)
        except Exception:
            return None
        node = Node(ob.hdr, ob.ds, ob.de, ob.cls)
        index[node.sf] = node
        q = ob.ds
        while q < ob.de:
            if A.looks_like_obj(q, ob.de):
                c = walk(q, ob.de)
                if c is not None and c.de > q:
                    node.kids.append(c)
                    q = c.de
                    continue
            q += 1
        return node

    return walk(start, limit)


def copy_subtree(node):
    """A duplicate of `node` that emits the same bytes at a new position.

    The copy reads from the same source range, so it is the same record in
    every respect except where it lands. References inside it that point at
    the original's parts are redirected afterwards, once both are placed."""
    dup = Node(node.hdr, node.ds, node.de, node.cls)
    dup.clone = True
    dup.origin_node = node
    dup.splices = list(node.splices)
    dup.kids = [copy_subtree(k) for k in node.kids]
    return dup


def emit(A, root, refs=None):
    """Write the tree out.

    A copy is made self-contained: any class its header merely refers to is
    written out in full instead, so the copy survives the record that first
    declared the class being deleted.

    Returns (payload, old size-field -> new, clone placements). A clone
    shares its source offsets with the original, so it cannot share the one
    move map; each gets its own record of where its parts ended up."""
    out = bytearray()
    moves = {}
    clones = []
    clsrefs = []                # (where a copy's class pointer goes, source)
    # A class is declared in the header of the first object of that class
    # and referred to from then on. Delete that object and every later
    # reference dangles - which is what kept the donor's unused instrument
    # track in every converted project: other tracks' lanes and parts refer
    # to classes first declared inside it. So a header that refers to a
    # class whose declaration is about to go declares the class itself
    # instead (the first such header; later ones refer to it), and
    # fix_refs() sends every other reference to that declaration there.
    dead_defs = set()
    for n in walk_nodes(root):
        if n.deleted:
            for m in walk_nodes(n):
                for off in A.defs:
                    if m.hdr <= off < m.sf:
                        dead_defs.add(off)
    relocated = {}              # (chain, ver) -> new position of the record
    reloc_by_off = {}           # dead def offset -> new position
    # every declaring header, so a relocated declaration can copy the exact
    # records - each with its own version - rather than guess them
    decl_hdr = {}               # def record offset -> header start
    for n in walk_nodes(root):
        tag = struct.unpack_from('>q', A.d, n.hdr)[0]
        if tag in (-1, -2):
            recs = chain_records(A, n.hdr)
            if recs:
                for _name, _ver, rel in recs:
                    decl_hdr.setdefault(n.hdr + rel, n.hdr)

    def exact_declaration(off):
        """The bytes declaring the chain up to the record at `off`, copied
        record by record from the header that declared it."""
        hdr = decl_hdr.get(off)
        if hdr is None:
            return None
        recs = chain_records(A, hdr)
        if not recs:
            return None
        out_b = bytearray()
        for i, (name, ver, rel) in enumerate(recs):
            last = (hdr + rel == off)
            out_b += struct.pack('>q', -1 if last else -2)
            raw = name.encode('latin1') + bytes(1)
            out_b += struct.pack('>I', len(raw)) + raw + struct.pack('>H', ver)
            if last:
                return bytes(out_b)
        return None

    def last_record_pos(buf, start):
        """Position of the -1 record in a declaring header at `start`."""
        o = start
        while True:
            tag = struct.unpack_from('>q', buf, o)[0]
            n = struct.unpack_from('>I', buf, o + 8)[0]
            if tag == -1:
                return o
            o = o + 8 + 4 + n + 2

    def relocate_header(node):
        """A header pointing at a dead declaration: declare instead, or
        point at the relocated declaration. Returns True when handled."""
        tag = struct.unpack_from('>q', A.d, node.hdr)[0]
        if tag in (-1, -2) or tag >= 0:
            return False
        off = tag & MASK
        if off not in dead_defs:
            return False
        ent = A.defs.get(off)
        if not ent:
            return False
        key = (tuple(ent[0]), ent[1])
        if key in relocated:
            out.extend(struct.pack('>Q', HI | relocated[key]))
            reloc_by_off.setdefault(off, relocated[key])
            return True
        hdr = exact_declaration(off) or own_header(A, node)
        if hdr is None:
            return False
        pos = len(out)
        out.extend(hdr)
        relocated[key] = last_record_pos(out, pos)
        reloc_by_off[off] = relocated[key]
        return True
    # Reference positions, followed through the copy so each one's new home
    # is known exactly rather than inferred from how far its object moved.
    src = A.d
    marks = sorted(refs) if refs else []
    posmap = {}
    ranges = []                 # (old start, old end, new start): every run
                                # of source bytes and where it landed, so any
                                # interior offset can be translated - the
                                # format interns attribute-name strings and
                                # refers to them by absolute position

    def copy(a, b, ctx=None):
        """Copy src[a:b] and record where any reference in it landed.

        A clone re-copies bytes the original already used, so one source
        position lands in two places. The two cannot share a map: a clone
        keeps its own list, and its references are resolved against its own
        copies first."""
        base = len(out)
        out.extend(src[a:b])
        if b > a:
            if ctx is None:
                ranges.append((a, b, base))
            else:
                ctx.setdefault('ranges', []).append((a, b, base))
        if not marks:
            return
        i = bisect.bisect_left(marks, a)
        while i < len(marks) and marks[i] < b:
            if ctx is None:
                posmap[marks[i]] = base + (marks[i] - a)
            else:
                ctx['positions'].append((marks[i], base + (marks[i] - a)))
            i += 1

    def opaque(node, start, end, ctx=None):
        """Bytes of `node` in [start, end), with this node's splices applied."""
        cuts = sorted((s, e, r) for s, e, r in node.splices
                      if s >= start and e <= end)
        if cuts:
            done = getattr(node, '_done', None)
            if done is None:
                done = node._done = []
            done.extend(cuts)
        q = start
        for s, e, r in cuts:
            copy(q, s, ctx)
            out.extend(r)
            q = e
        copy(q, end, ctx)

    def put(node, inside_clone=None):
        if node.deleted:
            return
        hdr_pos = len(out)
        if node.clone or inside_clone is not None:
            src = node.origin_node
            # A class is declared once and pointed at from then on. A copy
            # that declares the same class again is a second class as far as
            # Cubase is concerned - and an audio clip declared twice is one
            # it will not draw a waveform for. Only where the original has
            # already gone out, so a reader meets the declaration first.
            if relocate_header(node):
                pass
            elif (node.ref_class and src is not None and not src.deleted
                    and not node.force_class and src.new_sf is not None
                    and struct.unpack_from('>q', A.d, src.hdr)[0] in (-1, -2)):
                clsrefs.append((len(out), src))
                out.extend(bytes(8))
            else:
                hdr = own_header(A, node)
                if hdr is None:
                    copy(node.hdr, node.sf, inside_clone)
                else:
                    out.extend(hdr)     # declares its classes rather than
                                        # pointing at someone else's
        elif relocate_header(node):
            pass
        else:
            copy(node.hdr, node.sf, inside_clone)   # class chain or class ref
        # A header either declares classes or points at a declaration; a
        # copy whose header became an 8-byte pointer declares nothing, and
        # def_map() must not take a definition's new place from it.
        node.new_hdr = hdr_pos
        t0 = struct.unpack_from('>q', out, hdr_pos)[0] \
            if len(out) >= hdr_pos + 8 else 0
        node.declares = t0 in (-1, -2)
        size_pos = len(out)
        out.extend(b'\0' * 8)
        data_start = len(out)
        node.new_sf = size_pos
        if inside_clone is None:
            moves[node.sf] = size_pos
        else:
            inside_clone['map'][node.sf] = size_pos
        # Every copy gets its own map, nested ones included. Five copies of
        # one event all share the original's offsets, so a single map has
        # each copy's internal references resolve to the last one written -
        # which is how five regions collapsed into one.
        mine = inside_clone
        if node.clone:
            # a copy inside a copy resolves against its own parts first, then
            # its parent's, and only then the originals - otherwise a nested
            # copy points back at a track that is about to be deleted
            mine = {'start': len(out), 'end': None, 'positions': [],
                    'map': {node.sf: size_pos}, 'parent': inside_clone}
            clones.append(mine)
            # so a caller can find where a copy of a record ended up
            node.placed = mine
        q = node.ds
        # a replacement that covers whole children supplies their bytes too,
        # so those children are not emitted separately
        covered = [(s, e) for s, e, _r in node.splices
                   if any(k.hdr >= s and k.de <= e for k in node.kids)]
        # an extra keyed by an offset goes out at exactly that place in the
        # node's own bytes: a point into an automation lane that has none,
        # right after its count and before the fields that follow it
        at_extras = sorted(((w, x) for w, x in node.extra if isinstance(w, int)),
                           key=lambda e: e[0])

        def span(q0, end):
            for off, extra in at_extras:
                if q0 <= off <= end:
                    opaque(node, q0, off, mine)
                    put(extra, mine if extra.clone or mine else None)
                    q0 = off
            opaque(node, q0, end, mine)

        for k in node.kids:
            cov = any(k.hdr >= s and k.de <= e for s, e in covered)
            # the bytes in front of a child come first, then anything to be
            # inserted before that child, then the child. Inserting before
            # the first child used to go out ahead of the list's own header
            # fields, which put a track where the list's name should be.
            if not cov:
                span(q, k.hdr)
            for where, extra in node.extra:
                if where is k:
                    put(extra, mine if extra.clone or mine else None)
            if cov:
                continue
            put(k, mine)
            q = k.de
        for where, extra in node.extra:
            if where is None:
                put(extra, mine if extra.clone or mine else None)
        span(q, node.de)
        # 'end' puts a record after everything this node holds, which is
        # where a list that is empty keeps its first entry: its own fields
        # come first, and an insert before them would be read as one of them
        for where, extra in node.extra:
            if where == 'end':
                put(extra, mine if extra.clone or mine else None)
        struct.pack_into('>q', out, size_pos, len(out) - data_start)
        if mine is not None and mine is not inside_clone:
            mine['end'] = len(out)
            # A copy resolves its references against its own parts, then its
            # parent's - but a parent also points down at its children: a
            # track's automation node names the lane record two levels below
            # it. Hand a finished copy's placements up, so an ancestor's
            # reference into it lands on the copy and not on the original.
            if inside_clone is not None:
                for k2, v2 in mine['map'].items():
                    inside_clone['map'].setdefault(k2, v2)

    put(root)
    missed = []
    for node in walk_nodes(root):
        if node.deleted:
            continue
        for sp in node.splices:
            if sp not in getattr(node, '_done', ()):
                missed.append((node.cls, sp[0], sp[1]))
    ranges.sort()
    if missed:
        # which nodes never reached the output, and why
        emitted = set()
        for node in walk_nodes(root):
            if node.new_sf is not None:
                emitted.add(id(node))
        detail = []
        for node in walk_nodes(root):
            for sp in node.splices:
                if sp in getattr(node, '_done', ()):
                    continue
                over = [k for k in node.kids if k.hdr < sp[1] and k.de > sp[0]]
                detail.append(
                    '%s %#x..%#x  clone=%s deleted=%s emitted=%s '
                    'node=%#x..%#x overlapping-children=%d'
                    % (node.cls, sp[0], sp[1], node.clone, node.deleted,
                       id(node) in emitted, node.ds, node.de, len(over)))
                if len(detail) >= 5:
                    break
            if len(detail) >= 5:
                break
        raise ValueError('splice never written:\n  ' + '\n  '.join(detail))
    return bytes(out), moves, clones, posmap, clsrefs, ranges, reloc_by_off


def chain_records(A, hdr):
    """[(name, ver, offset of the record relative to hdr), ...] declared by
    a declaring header, or None for a header that only refers to a class."""
    out = []
    o = hdr
    while True:
        tag, o2 = A.i64(o)
        if tag not in (-1, -2):
            return None
        name, ver, o3 = A.read_def(o2)
        out.append((name, ver, o - hdr))
        o = o3
        if tag == -1:
            return out


def make_translate(ranges):
    """old payload offset -> new, over the runs emit() actually copied."""
    starts = [r[0] for r in ranges]

    def translate(off):
        i = bisect.bisect_right(starts, off) - 1
        if i < 0:
            return None
        a, b, base = ranges[i]
        if a <= off < b:
            return base + (off - a)
        return None
    return translate


def own_header(A, node):
    """A header that declares this object's classes outright.

    Returns None when the original already declares them, in which case its
    bytes are copied unchanged."""
    tag = struct.unpack_from('>q', A.d, node.hdr)[0]
    if tag in (-1, -2) and not node.force_class:
        return None                 # already a declaration
    if tag in (-1, -2):
        ent = A.defs.get(node.hdr)
    else:
        ent = A.defs.get(tag & MASK)
    if not ent:
        return None
    chain, ver = ent
    if node.force_class:
        # the same record, declared as a different class
        chain = tuple(list(chain[:-1]) + [node.force_class])
    out = bytearray()
    for i, name in enumerate(chain):
        last = (i == len(chain) - 1)
        out += struct.pack('>q', -1 if last else -2)
        raw = name.encode('latin1') + bytes([0])
        out += struct.pack('>I', len(raw)) + raw
        out += struct.pack('>H', ver)
    return bytes(out)


def find_string_again(src, out, v, before):
    """`v` named an interned string (u32 length, text) in the source that did
    not survive the rebuild. The same string, if it occurs anywhere in the
    output before `before`, serves as well - Cubase interns by content."""
    if src is None or v < 4 or v + 4 > len(src):
        return None
    n = struct.unpack_from('>I', src, v - 4)[0]
    if not 1 <= n <= 300 or v + n > len(src):
        return None
    needle = src[v - 4:v + n]
    hit = bytes(out[:before]).find(needle)
    if hit < 0:
        hit = bytes(out).find(needle)
    return hit + 4 if hit >= 0 else None


def fix_refs(buf, moves, defmap, positions, narrow=(), translate=None,
             src=None):
    """Rewrite the stored offsets that moved, at the positions they occupy.

    An earlier version searched the whole payload for values that matched a
    moved offset. That corrupts any data which happens to equal one: inside
    a twelve-kilobyte synth patch some bytes always do, and the attribute
    stream after them then reads as garbage. So references are rewritten
    only where parsing actually found one, and `positions` maps each old
    position to its new one."""
    out = bytearray(buf)
    n_obj = n_cls = 0
    for old_pos, new_pos in positions.items():
        if new_pos is None:
            continue
        if old_pos in narrow:
            # a 4-byte reference to an arbitrary interior position - an
            # interned name string, usually - found by diffing real saves
            if new_pos + 4 > len(out) or translate is None:
                continue
            v = struct.unpack_from('>I', out, new_pos)[0]
            new = translate(v)
            if new is None:
                new = find_string_again(src, out, v, new_pos)
            if new is not None and new != v and new <= 0xffffffff:
                struct.pack_into('>I', out, new_pos, new)
                n_obj += 1
            continue
        if new_pos + 8 > len(out):
            continue
        v = struct.unpack_from('>q', out, new_pos)[0]
        if v > 0:
            new = moves.get(v)
            if new is not None and new != v:
                struct.pack_into('>q', out, new_pos, new)
                n_obj += 1
        elif v < 0:
            off = v & MASK
            new = defmap.get(off)
            if new is not None and new != off:
                struct.pack_into('>q', out, new_pos, (v & ~MASK) | new)
                n_cls += 1
    return bytes(out), n_obj, n_cls




def string_prefix_at32(d, i):
    """The 4-byte version of string_prefix_at: the window [i, i+4) is the
    tail of a u32 string length plus the string's first letters."""
    for k in (1, 2, 3):
        q = i - k
        if q < 0 or q + 4 > len(d):
            continue
        n = struct.unpack_from('>I', d, q)[0]
        if not 1 <= n <= 300 or q + 4 + n > len(d):
            continue
        body = d[q + 4:q + 4 + n]
        if body.endswith(BOM):
            body = body[:-3]
        if not body.endswith(b'\x00'):
            continue
        text = body[:-1]
        if len(text) >= 3 and all(32 <= c < 127 or c >= 128 for c in text):
            return True
    return False


def string_prefix_at(d, i):
    """Is the 8-byte window at `i` really the tail of a string's length
    field plus the first letters of the string?

    A stored offset is a small number in eight bytes, so its low bytes are
    the only ones set - and a u32 length of 10 followed by "Low Bongo" reads
    the same way: 00 00 00 00 00 00 0a 4c. Three of those in the drum maps
    pinned the donor's instrument track in place for weeks."""
    for k in (1, 2, 3):
        q = i + 8 - k - 4               # where the length field would sit
        if q < 0 or q + 4 > len(d):
            continue
        n = struct.unpack_from('>I', d, q)[0]
        if not 1 <= n <= 300 or q + 4 + n > len(d):
            continue
        body = d[q + 4:q + 4 + n]
        if body.endswith(BOM):
            body = body[:-3]
        if not body.endswith(b'\x00'):
            continue
        text = body[:-1]
        if len(text) >= 3 and all(32 <= c < 127 or c >= 128 for c in text):
            return True
    return False


def def_map(A, moves, root):
    """Where each class-definition record ends up.

    A definition lives inside the header of the object that first declares
    it, so it moves with that object."""
    out = {}
    # The record that really declares a class is the original. A copy shares
    # its source offsets, so letting a copy win would send references to the
    # copy's header - which, when it only points at the original, declares
    # nothing at all.
    for copies in (True, False):
        for node in walk_nodes(root):
            if node.deleted or node.new_sf is None or bool(node.clone) != copies:
                continue
            if not getattr(node, 'declares', True):
                continue
            src_tag = struct.unpack_from('>q', A.d, node.hdr)[0]
            if src_tag not in (-1, -2):
                continue        # the source header declared nothing here
            new_hdr = getattr(node, 'new_hdr', None)
            delta = (new_hdr - node.hdr) if new_hdr is not None \
                else node.new_sf - node.sf
            for off in A.defs:
                if node.hdr <= off < node.sf:
                    # the earliest declaration in the file is the one Cubase
                    # itself would point at
                    if copies and off in out and out[off] < off + delta:
                        continue
                    out[off] = off + delta
    return out


ROW = 10        # u64 object offset + u16 id


def object_table(A, index):
    """Find the registry of objects Cubase keeps at the end of the root.

    It is a flat run of (offset, id) rows preceded by a count, and it is what
    makes a track hard to remove: the track's own records can go, but a row
    here still points at where they used to be. The ids are stable
    identifiers rather than positions, so rows can be dropped without
    renumbering the ones that stay."""
    buf = A.d
    targets = set(index)

    def is_row(o):
        if o < 0 or o + ROW > len(buf):
            return False
        return struct.unpack_from('>q', buf, o)[0] in targets

    # find any row, then grow the run in both directions
    seed = None
    for node in index.values():
        pass
    for o in range(len(buf) - ROW, max(0, len(buf) - 4 * 1024 * 1024), -1):
        if is_row(o) and is_row(o + ROW) and is_row(o + 2 * ROW):
            seed = o
            break
    if seed is None:
        return None
    start = seed
    while is_row(start - ROW):
        start -= ROW
    end = seed
    while is_row(end + ROW):
        end += ROW
    end += ROW
    n = (end - start) // ROW
    # The count sits just in front of the rows, and how wide it is has to be
    # read rather than assumed: a u16 count preceded by the tail of a double
    # reads as the same number when taken as a u32 two bytes earlier, and a
    # splice written at the wrong place straddles the record in front and is
    # silently dropped.
    count_off = count_size = None
    for back, size, fmt in ((4, 2, '>H'), (6, 4, '>I'), (10, 4, '>I')):
        o = start - back
        if o >= 0 and struct.unpack_from(fmt, buf, o)[0] == n:
            count_off, count_size = o, size
            break
    if count_off is None:
        return None
    return {'start': start, 'end': end, 'rows': n, 'count_off': count_off,
            'count_size': count_size}


def drop_table_rows(A, table, root, gone):
    """Remove the registry rows pointing at anything in `gone`."""
    if not table or not gone:
        return 0
    buf = A.d
    holder = find_cover(root, table['start'], table['end'])
    removed = 0
    for i in range(table['rows']):
        o = table['start'] + i * ROW
        if struct.unpack_from('>q', buf, o)[0] in gone:
            holder.splice(o, o + ROW, b'')
            removed += 1
    if removed:
        left = table['rows'] - removed
        size = table.get('count_size', 4)
        owner = find_cover(root, table['count_off'],
                           table['count_off'] + size)
        owner.splice(table['count_off'], table['count_off'] + size,
                     struct.pack('>H' if size == 2 else '>I', left))
    return removed


def registry_rows(A, table):
    """The (object offset, id) pairs the registry holds, in file order."""
    out = []
    for i in range(table['rows']):
        o = table['start'] + i * ROW
        out.append((struct.unpack_from('>q', A.d, o)[0],
                    struct.unpack_from('>H', A.d, o + 8)[0]))
    return out


def free_registry_ids(used, n):
    """`n` ids no row uses yet.

    The ids are stable identifiers rather than positions, so any unused
    number will do; 0xffff is left alone because the bytes after the rows
    use it as a terminator."""
    out = []
    i = 0
    while len(out) < n:
        if i not in used:
            out.append(i)
            used.add(i)
        i += 1
        if i >= 0xffff:
            raise ValueError('the object registry is out of ids')
    return out


def plan_registry(A, root, table, rows):
    """Lay out a new registry holding exactly `rows`.

    `rows` is the final ordered list of (node, id). A copied track needs a
    row of its own: Cubase connects a track through this registry, and one
    that has no row here is the "could not be connected" it offers to remove
    - which is why a copied track's automation lane opened with no parameter
    chosen however faithfully the lane itself was reproduced.

    Where each object ends up is not known until the payload has been laid
    out, so a placeholder of the right length goes in now and rebuild()
    fills in the offsets afterwards."""
    holder = find_cover(root, table['start'], table['end'])
    holder.splice(table['start'], table['end'], b'\0' * (ROW * len(rows)))
    size = table.get('count_size', 4)
    owner = find_cover(root, table['count_off'], table['count_off'] + size)
    owner.splice(table['count_off'], table['count_off'] + size,
                 struct.pack('>H' if size == 2 else '>I', len(rows)))
    # the rows sit at the tail of the holder's own bytes, after every object
    # nested in it, so the distance back from its end is what survives the
    # rebuild - the distance forward from its start does not.
    return {'holder': holder, 'rows': list(rows),
            'tail': holder.de - table['end']}


def write_registry(buf, registry, moves):
    """Fill in the registry placeholder once everything has been placed.

    Positions come from each node's own `new_sf`, never from `moves`. A copy
    keeps the source offsets of the record it was taken from, so emitting it
    overwrites `moves[sf]` with the copy's position and the original and its
    copies all resolve to the same place - which registered one object twice
    and left the other with no row at all."""
    h = registry['holder'].new_sf
    if h is None:
        return 0
    rows = registry['rows']
    end = h + 8 + struct.unpack_from('>q', bytes(buf), h)[0] - registry['tail']
    start = end - ROW * len(rows)
    if start < 0 or end > len(buf):
        raise ValueError('the object registry does not fit where it was put')
    seen = set()
    written = 0
    for node, rid in rows:
        off = node.new_sf
        if off is None or off in seen:
            # never emitted, or already registered under another row
            continue
        seen.add(off)
        struct.pack_into('>q', buf, start + written * ROW, off)
        struct.pack_into('>H', buf, start + written * ROW + 8, rid)
        written += 1
    if written != len(rows):
        raise ValueError('the object registry was laid out for %d rows but '
                         '%d could be placed' % (len(rows), written))
    return written


def find_cover(root, a, b):
    """The innermost object whose own bytes cover the whole of [a, b).

    A field can straddle a child's last byte - the object registry's count
    does, sitting two bytes before the end of the record in front of it - and
    a splice recorded on a node that only holds part of the range is never
    written out. Walking down only while the child still holds all of it
    lands on the node that does."""
    node = root
    while True:
        for k in node.kids:
            if k.hdr <= a and b <= k.de:
                node = k
                break
        else:
            return node


def find_node(root, off):
    """The innermost object whose own bytes cover `off`.

    A splice has to be registered on the node that actually emits those
    bytes: a track's name sits inside a child of the track object, and a
    splice recorded on the parent would never be written out."""
    node = root
    while True:
        for k in node.kids:
            if k.hdr <= off < k.de:
                node = k
                break
        else:
            return node


def subtree_targets(node, A):
    """Every reference target that would disappear with `node`.

    That is the size field of the node and of everything inside it, plus any
    class definition first declared in one of those headers."""
    objs, defs = set(), set()
    for n in walk_nodes(node):
        objs.add(n.sf)
        for off in A.defs:
            if n.hdr <= off < n.sf:
                defs.add(off)
    return objs, defs


def dangling_after(A, nodes, ignore=None):
    """References left pointing at nothing if every node in `nodes` goes.

    A record can be removed only when the rest of the project has stopped
    pointing at it; Cubase follows those references while it loads and calls
    the file invalid when one leads nowhere. Two records that only refer to
    each other come out together safely, which is why the whole set is
    weighed at once rather than one at a time."""
    objs, defs = set(), set()
    ranges = []
    for n in nodes:
        o, d = subtree_targets(n, A)
        objs |= o
        defs |= d
        ranges.append((n.hdr, n.de))
    if not objs and not defs:
        return 0
    ranges.sort()
    ign_lo, ign_hi = ignore if ignore else (-1, -1)
    buf = A.d
    hits = 0
    i = 0
    end = len(buf) - 8
    while i <= end:
        for lo, hi in ranges:
            if lo <= i < hi:
                i = hi
                break
        else:
            if ign_lo <= i < ign_hi:
                i = ign_hi
                continue
            v = struct.unpack_from('>q', buf, i)[0]
            if v > 0 and v in objs and not string_prefix_at(buf, i):
                hits += 1
            elif v < 0 and (v & MASK) in defs:
                hits += 1
            i += 1
    return hits


def deletable(A, nodes, ignore=None, classes_ok=True):
    """The largest set of these that can go without leaving a dangling
    reference behind.

    Two records that only point at each other come out together safely, so
    the set is weighed as a whole; whatever the rest of the project still
    reads is put back and the rest weighed again."""
    cur = list(nodes)
    while cur:
        targets = {}
        ranges = []
        for n in cur:
            objs, defs = subtree_targets(n, A)
            for v in objs:
                targets[v] = n
            for v in defs:
                targets[-v] = n
            ranges.append((n.hdr, n.de))
        ranges.sort()
        ign_lo, ign_hi = ignore if ignore else (-1, -1)
        buf = A.d
        blame = {}
        i = 0
        end = len(buf) - 8
        while i <= end:
            for lo, hi in ranges:
                if lo <= i < hi:
                    i = hi
                    break
            else:
                if ign_lo <= i < ign_hi:
                    i = ign_hi
                    continue
                v = struct.unpack_from('>q', buf, i)[0]
                owner = None
                if v > 0:
                    owner = targets.get(v)
                    if owner is not None and string_prefix_at(buf, i):
                        owner = None
                elif v < 0 and not classes_ok:
                    owner = targets.get(-(v & MASK))
                if owner is not None:
                    blame[id(owner)] = blame.get(id(owner), 0) + 1
                i += 1
        if not blame:
            return cur
        worst = max(cur, key=lambda n: blame.get(id(n), 0))
        cur.remove(worst)
    return cur


def referenced_from_outside(A, node, ignore=None):
    """Is anything outside `node` pointing into it?

    Removing a record that something still refers to leaves a reference into
    empty space, and Cubase follows those. This is what makes a deletion
    unsafe, so it is checked rather than hoped for. `ignore` skips a byte
    range - the object registry, whose rows are dropped alongside the track
    rather than left dangling."""
    objs, defs = subtree_targets(node, A)
    if not objs and not defs:
        return 0
    lo, hi = node.hdr, node.de
    ign_lo, ign_hi = ignore if ignore else (-1, -1)
    buf = A.d
    hits = 0
    i = 0
    end = len(buf) - 8
    while i <= end:
        if lo <= i < hi:            # references from inside go with it
            i = hi
            continue
        if ign_lo <= i < ign_hi:
            i = ign_hi
            continue
        v = struct.unpack_from('>q', buf, i)[0]
        if v > 0:
            if v in objs:
                hits += 1
        elif v < 0 and (v & MASK) in defs:
            hits += 1
        i += 1
    return hits


def walk_nodes_all(index):
    """Every node in the tree, from the index that parse() built."""
    return list(index.values())


def walk_nodes(node):
    yield node
    for k in node.kids:
        for x in walk_nodes(k):
            yield x
    for _where, extra in node.extra:
        for x in walk_nodes(extra):
            yield x


def repack(raw, A, payload):
    """Put a rebuilt payload back into the .cpr container."""
    # RIF2 (Cubase 12 on) has 64-bit chunk sizes after a 16-byte header;
    # the older RIFF form has 32-bit ones after 12 bytes, and its total
    # size sits at 4 instead of 8
    w = arch.width(raw)
    out = bytearray()
    out += raw[:16 if w == 8 else 12]
    grew = 0
    for cid, off, size in arch.chunks(raw):
        if off == A.base:
            body = payload
            # measured against the chunk as it is in the file, not against
            # A.d, which may have had records appended to it since
            grew = len(payload) - size
        else:
            body = raw[off:off + size]
        out += cid.encode('latin1')
        out += struct.pack('>Q' if w == 8 else '>I', len(body))
        out += body
    at = 8 if w == 8 else 4
    total = struct.unpack_from('>I', out, at)[0]
    struct.pack_into('>I', out, at, total + grew)
    return bytes(out)


def fix_clone_refs(buf, clones, moves, defmap, narrow=(), translate=None,
                    src=None):
    """Point a copy's references at its own parts, or at the shared originals.

    A duplicated track holds references among its own records, and others to
    things it shares with the rest of the project. The first kind has to be
    redirected to the copy's versions; the second still points where it
    always did, allowing for whatever moved. Both are resolved at the exact
    positions parsing found, never by searching the bytes for values."""
    out = bytearray(buf)
    n = 0
    for c in clones:
        def resolve(val, ctx=c):
            while ctx is not None:
                hit = ctx['map'].get(val)
                if hit is not None:
                    return hit
                ctx = ctx.get('parent')
            return moves.get(val)
        own_tr = None
        for _old, pos in c.get('positions', ()):
            if _old in narrow:
                if pos + 4 > len(out):
                    continue
                if own_tr is None:
                    chain = []
                    cc = c
                    while cc is not None:
                        chain.extend(cc.get('ranges', ()))
                        cc = cc.get('parent')
                    chain.sort()
                    own_tr = make_translate(chain)
                v = struct.unpack_from('>I', out, pos)[0]
                # the copy's own bytes first, then wherever the shared
                # original ended up
                new = own_tr(v)
                if new is None and translate is not None:
                    new = translate(v)
                if new is None:
                    new = find_string_again(src, out, v, pos)
                if new is not None and new != v and new <= 0xffffffff:
                    struct.pack_into('>I', out, pos, new)
                    n += 1
                continue
            if pos + 8 > len(out):
                continue
            v = struct.unpack_from('>q', out, pos)[0]
            if v > 0:
                new = resolve(v)
                if new is not None and new != v:
                    struct.pack_into('>q', out, pos, new)
                    n += 1
            elif v < 0:
                off = v & MASK
                tgt = defmap.get(off)
                if tgt is not None and tgt != off:
                    struct.pack_into('>q', out, pos, (v & ~MASK) | tgt)
                    n += 1
    return bytes(out), n


def placed_at(node, moves):
    """Where a node's size field ended up, copy or original."""
    rec = getattr(node, 'placed', None)
    if rec is not None and node.sf in rec['map']:
        return rec['map'][node.sf]
    return moves.get(node.sf)


def rebuild(raw, A, root, links=(), registry=None):
    """Emit the tree, fix every reference, and return a whole .cpr.

    `links` are (holder, target, offset, width) for a field that names an
    object by its position as a plain number - the pool says where each
    clip is that way, and an event that shares a clip points at it the same
    way - and which can only be filled in once everything is laid out.

    `registry` comes from plan_registry() and is filled in the same way."""
    refs = A.obj_refs | A.cls_refs | A.obj_refs32
    payload, moves, clones, posmap, clsrefs, ranges, reloc = emit(A, root, refs)
    translate = make_translate(ranges)
    defmap = def_map(A, moves, root)
    # declarations that moved into another header (see emit)
    for off, pos in reloc.items():
        defmap.setdefault(off, pos)
    if clsrefs:
        buf = bytearray(payload)
        for at, src in clsrefs:
            decl = [o for o in A.defs if src.hdr <= o < src.sf]
            new_pos = None
            if decl and getattr(src, 'declares', False) \
                    and getattr(src, 'new_hdr', None) is not None:
                new_pos = src.new_hdr + (max(decl) - src.hdr)
            elif decl:
                new_pos = defmap.get(max(decl))
            if new_pos is not None:
                struct.pack_into('>Q', buf, at, HI | new_pos)
        payload = bytes(buf)
    fixed, n_obj, n_cls = fix_refs(payload, moves, defmap, posmap,
                                   A.obj_refs32, translate, A.d)
    fixed, n_clone = fix_clone_refs(fixed, clones, moves, defmap,
                                    A.obj_refs32, translate, A.d)
    if links:
        buf = bytearray(fixed)
        for holder, target, at, width in links:
            h = placed_at(holder, moves)
            t = placed_at(target, moves)
            if h is None or t is None:
                continue
            struct.pack_into('>q' if width == 8 else '>I', buf,
                             h + 8 + at, t)
        fixed = bytes(buf)
    n_rows = 0
    if registry:
        buf = bytearray(fixed)
        n_rows = write_registry(buf, registry, moves)
        fixed = bytes(buf)
    return repack(raw, A, fixed), {'objects': n_obj, 'classes': n_cls,
                                   'clone_refs': n_clone,
                                   'clones': len(clones),
                                   'registry_rows': n_rows,
                                   'bytes': len(fixed)}


# ------------------------------------------------------------------ strings

BOM = b'\xef\xbb\xbf'


def encode_string(text):
    """Cubase's string form: u32 length, bytes, NUL, and a BOM when wide."""
    b = text.encode('utf-8')
    try:
        text.encode('ascii')
        return struct.pack('>I', len(b) + 1) + b + b'\x00'
    except UnicodeEncodeError:
        return struct.pack('>I', len(b) + 4) + b + b'\x00' + BOM


def string_at(src, off):
    """(text, length in bytes) of the string stored at `off`."""
    n = struct.unpack_from('>I', src, off)[0]
    body = src[off + 4:off + 4 + n]
    if body.endswith(BOM):
        body = body[:-3]
    return body.split(b'\x00', 1)[0].decode('utf-8', 'replace'), 4 + n


def rename(node, src, off, text):
    """Replace the string at `off` (inside `node`) with `text`."""
    old, n = string_at(src, off)
    if old == text:
        return False
    keep_wide = src[off + 4:off + 4 + struct.unpack_from('>I', src, off)[0]] \
        .endswith(BOM)
    b = text.encode('utf-8')
    if keep_wide:
        raw = struct.pack('>I', len(b) + 4) + b + b'\x00' + BOM
    else:
        raw = encode_string(text)
    node.splice(off, off + n, raw)
    return True
