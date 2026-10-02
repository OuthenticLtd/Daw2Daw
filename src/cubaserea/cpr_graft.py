"""Bring a record from another Cubase project into the one being built.

The builder (cpr_build) makes every track by copying a record out of one
donor project, so it can only produce the kinds of record the donor holds.
The donor has no video track, and every track in it has a single version -
so a REAPER project with a video track, or with fixed lanes that ought to
become track versions, had those left out.

This module fills that gap in two ways, both ending in the same place: new
bytes appended to the donor's object stream, parsed into nodes, and handed
to the builder to place and edit like any copy.

* transplant(): take a subtree out of another .cpr - a video track, say -
  and rewrite it so it is valid inside the donor. Every class the record
  uses is either pointed at the donor's own declaration of that class, when
  the donor has one with the same name and version, or declared inline;
  every reference from one part of the record to another is redirected to
  where those parts now sit; a reference to anything outside the record is
  reported, since it would dangle.

* variation(): synthesise a track version. Cubase keeps a track's versions
  in an MTrackVariationCollection inside the track: each version is a name,
  an event list (absent on the version that is active, whose events sit on
  the track itself) and a number. Checked against real saves ("Balkan Dad",
  4 versions; "Bell Link", 5 tracks with versions): the layout is the same
  on audio, instrument and MIDI tracks.
"""
import struct

from . import arch
from . import cpr_read
from . import cpr_tree

HI = arch.HI
MASK = arch.MASK


# ------------------------------------------------------------ class chains
def chain_records(A, hdr):
    """[(name, version), ...] declared by the header starting at `hdr`.

    Only for a header that declares its classes (tag -2/-2/.../-1)."""
    out = []
    o = hdr
    while True:
        tag, o = A.i64(o)
        if tag not in (-1, -2):
            return None
        name, ver, o = A.read_def(o)
        out.append((name, ver))
        if tag == -1:
            return out


def chains_of(A, root):
    """Offset of every class record -> the records up to and including it.

    A header declares an object's classes base first, derived last, and a
    later object refers to whichever record names its own class - the
    stream's first MTrackList header declares MListNode, MDataNode and
    MTrackList, and every plain MListNode after it points at the first of
    those three records. So each record is a possible target, and the class
    chain of an object pointing at it is the records up to that one."""
    out = {}
    for n in cpr_tree.walk_nodes(root):
        tag = struct.unpack_from('>q', A.d, n.hdr)[0]
        if tag not in (-1, -2):
            continue
        recs = chain_records(A, n.hdr)
        if not recs:
            continue
        o = n.hdr
        for i in range(len(recs)):
            out.setdefault(o, recs[:i + 1])
            _t, o2 = A.i64(o)
            _name, _ver, o = A.read_def(o2)
    return out


def records_for(A, node, chains):
    """The class records of any node's header, declared or referenced."""
    tag = struct.unpack_from('>q', A.d, node.hdr)[0]
    if tag in (-1, -2):
        return chain_records(A, node.hdr)
    if tag < 0:
        return chains.get(tag & MASK)
    return None


def def_for(A, recs):
    """Where the donor declares exactly this chain: the offset of the record
    naming its last class, which is what a class reference points at. None
    if the donor never declares it."""
    chain = tuple(name for name, _v in recs)
    ver = recs[-1][1]
    best = None
    for off, (c, v) in A.defs.items():
        if tuple(c) == chain and v == ver:
            if best is None or off < best:
                best = off          # the first declaration, as Cubase does
    return best


def declare(recs):
    """Header bytes declaring the chain outright."""
    out = bytearray()
    for i, (name, ver) in enumerate(recs):
        out += struct.pack('>q', -1 if i == len(recs) - 1 else -2)
        raw = name.encode('latin1') + b'\x00'
        out += struct.pack('>I', len(raw)) + raw
        out += struct.pack('>H', ver)
    return bytes(out)


def header_for(A, recs):
    """A reference to the donor's declaration when it has one, else a
    declaration. Returns (bytes, declared?)."""
    off = def_for(A, recs)
    if off is not None:
        return struct.pack('>Q', HI | off), False
    return declare(recs), True


def encode_string(text):
    return cpr_tree.encode_string(text)


# ------------------------------------------------------------ the source
class Source:
    """Another Cubase project, opened so records can be taken out of it."""

    def __init__(self, path):
        self.path = path
        self.reader = cpr_read.CprReader(path)
        self.proj = self.reader.read(keep_buses=True)
        self.A = self.reader.A
        self.root, self.index = cpr_tree.parse(self.A)
        self.chains = chains_of(self.A, self.root)

    def track_node(self, track):
        return self.index.get(track.src.get('sf'))

    def video_track(self):
        for t in self.proj.tracks:
            if t.kind == 'video' and t.items:
                n = self.track_node(t)
                if n is not None:
                    return t, n
        return None, None


def transplant(A, src, node, log=None):
    """`node` (from `src`, a Source) as bytes valid inside A's stream.

    Returns (bytes, old sf -> offset of the size field inside the bytes),
    or (None, reason) when the record refers to something outside itself
    that cannot come along."""
    B = src.A
    d = B.d
    out = bytearray()
    ranges = []                   # (old start, old end, new start)
    sfmap = {}

    def copy(a, b):
        if b > a:
            ranges.append((a, b, len(out)))
            out.extend(d[a:b])

    def emit(n):
        recs = records_for(B, n, src.chains)
        if not recs:
            raise ValueError('cannot resolve the class of %r' % (n,))
        hdr, _decl = header_for(A, recs)
        out.extend(hdr)
        size_pos = len(out)
        out.extend(bytes(8))
        ds_new = len(out)
        sfmap[n.sf] = size_pos
        q = n.ds
        for k in n.kids:
            copy(q, k.hdr)
            emit(k)
            q = k.de
        copy(q, n.de)
        struct.pack_into('>q', out, size_pos, len(out) - ds_new)

    emit(node)

    # references inside the record: values that name one of its own parts
    inside = set(sfmap)
    everything = set(src.index)
    external = []
    ranges.sort()

    def newpos(old):
        for a, b, base in ranges:
            if a <= old < b - 7:
                return base + (old - a)
        return None

    for a, b, base in ranges:
        i = a
        while i <= b - 8:
            v = struct.unpack_from('>q', d, i)[0]
            if v > 0 and v in inside:
                struct.pack_into('>q', out, base + (i - a), sfmap[v])
                i += 8
                continue
            if v > 0 and v in everything:
                tgt = src.index[v]
                external.append('%s at %#x -> %s' % (node.cls, i, tgt.cls))
                i += 8
                continue
            i += 1
    if external:
        if log is not None:
            log.append('%s in %s points outside itself (%s) and cannot be '
                       'brought across whole'
                       % (node.cls, src.path, '; '.join(external[:3])))
        return None, external
    return bytes(out), sfmap


def append(A, index, raw):
    """Add `raw` to the end of A's stream and parse it into nodes.

    Returns the node the bytes begin with. Class declarations inside are
    registered as the stream is parsed, so a reference to them can be
    rewritten when the stream is rebuilt; the nodes go into `index` so the
    reference scan knows their size fields are not references."""
    base = len(A.d)
    A.d = A.d + raw
    node = cpr_tree.parse_at(A, base, len(A.d), index)
    if node is None:
        raise ValueError('the appended record did not parse back')
    return node


# ------------------------------------------------------------ versions
def variation_bytes(A, coll_node, list_node, chains, name, track_name,
                    n_events, vid):
    """One MTrackVariation holding an empty event list, as bytes.

    `coll_node` is the donor track's MTrackVariationCollection and
    `list_node` the track's own event list: the version's list copies that
    list's domain fields (the tempo and signature it follows), so it is the
    same kind of list the track itself keeps. The events are added as nodes
    afterwards; the count written here has to match how many."""
    var_proto = coll_node.kids[0] if coll_node.kids else None
    if var_proto is None:
        raise ValueError('the donor track has no version record to copy')
    var_recs = records_for(A, var_proto, chains)
    list_recs = records_for(A, list_node, chains)
    if not var_recs or not list_recs:
        raise ValueError('cannot resolve the version classes in the donor')
    # the list node: name, then the domain the track's own list carries,
    # then the count
    _nm, name_end = A.string(list_node.ds)
    count_off = _list_count_off(A, list_node)
    domain = A.d[name_end:count_off]
    list_data = encode_string(track_name) + domain + struct.pack('>I', n_events)
    list_hdr, _ = header_for(A, list_recs)
    list_bytes = list_hdr + struct.pack('>q', len(list_data)) + list_data
    var_data = encode_string(name) + list_bytes + struct.pack('>I', vid)
    var_hdr, _ = header_for(A, var_recs)
    return var_hdr + struct.pack('>q', len(var_data)) + var_data


def _list_count_off(A, list_node):
    """Where a track's event list keeps its child count."""
    r = cpr_read.CprReader.__new__(cpr_read.CprReader)
    r.A = A
    r.node_header(list_node.ds, list_node.de, cls=list_node.cls)
    return r.count_off
