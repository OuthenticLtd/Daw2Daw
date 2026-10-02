"""Reader for Steinberg Cubase .cpr (RIF2/NUND) archive object streams.

Container:  'RIF2' + u32(0) + u32 totalsize + 'NUND' then repeated
            chunk = id[4] + u64 size + payload.
            Chunks come in pairs: ROOT (names) / ARCH (object stream).

Object header inside an ARCH payload:
            tag:i64
              -2 -> class definition record follows, more records follow
              -1 -> class definition record follows, chain ends
              other negative / non-negative -> reference: (tag & 0x7fff..) is the
                 byte offset *within this ARCH payload* of the defining record's tag
            class def record = u32 len + len bytes (NUL terminated) + u16 version
            after the chain: i64 dataSize, then dataSize bytes of class-specific data.

Strings:    u32 len + len bytes; content is NUL terminated and may be followed by
            a UTF-8 BOM (ef bb bf) marking a "wide" string.
"""
import struct

HI = 0x8000000000000000
MASK = 0x7FFFFFFFFFFFFFFF


def width(d):
    """Bytes in a pointer-sized field: 8 for RIF2 (Cubase 13 on), 4 for the
    older RIFF container (Cubase 12 and before - 108 of the user's 179
    projects, 2026-09-29). Chunk sizes, object tags, object sizes and
    references are that wide; everything else is laid out the same."""
    if d[:4] == b'RIFF' and d[8:12] == b'NUND':
        return 4
    assert d[:4] == b'RIF2', 'not a Cubase project (no RIF2/RIFF header)'
    return 8


def chunks(d):
    w = width(d)
    o = 16 if w == 8 else 12
    out = []
    while o + 4 + w <= len(d):
        cid = d[o:o + 4].decode('latin1')
        size = struct.unpack_from('>Q' if w == 8 else '>I', d, o + 4)[0]
        out.append((cid, o + 4 + w, size))
        o += 4 + w + size
    return out


class Obj:
    __slots__ = ('chain', 'ver', 'ds', 'de', 'hdr', 'end')

    def __init__(self, chain, ver, hdr, ds, de):
        self.chain = chain      # tuple of class names, base..derived
        self.ver = ver
        self.hdr = hdr          # offset of header start
        self.ds = ds            # data start
        self.de = de            # data end (exclusive)

    @property
    def cls(self):
        return self.chain[-1]

    def __repr__(self):
        return f"<{self.cls} v{self.ver} {self.de - self.ds}B @{self.hdr:#x}>"


class Arch:
    def __init__(self, payload, name='', base=0, w=8):
        self.d = payload
        self.W = w        # pointer width: 8 (RIF2) or 4 (older RIFF)
        self._pf = '>q' if w == 8 else '>i'
        self.HI = 1 << (8 * w - 1)
        self.MASK = self.HI - 1
        self.name = name
        self.base = base  # where this payload starts in the .cpr, so an
                          # offset inside it can be turned back into a file
                          # position and the original edited in place
        self.defs = {}    # offset of def tag -> (chain tuple, version)
        self.objs = {}    # offset of an object's size field -> Obj
        # Where references actually live. Rebuilding the stream moves
        # objects, and every stored offset then has to be rewritten - but
        # only the ones that really are offsets. Searching for values that
        # look like offsets corrupts any data that matches by coincidence,
        # which inside a megabyte of plug-in state is a certainty.
        self.obj_refs = set()     # positions holding an object offset
        self.cls_refs = set()     # positions holding a class reference
        self.obj_refs32 = set()   # positions holding a 4-byte object offset:
                                  # the drum maps, the pool tree and several
                                  # settings records keep them this size

    # ---- primitives -------------------------------------------------
    def u8(self, o):  return self.d[o], o + 1
    def u16(self, o): return struct.unpack_from('>H', self.d, o)[0], o + 2
    def i16(self, o): return struct.unpack_from('>h', self.d, o)[0], o + 2
    def u32(self, o): return struct.unpack_from('>I', self.d, o)[0], o + 4
    def i32(self, o): return struct.unpack_from('>i', self.d, o)[0], o + 4
    def i64(self, o): return struct.unpack_from('>q', self.d, o)[0], o + 8
    def f32(self, o): return struct.unpack_from('>f', self.d, o)[0], o + 4
    def f64(self, o): return struct.unpack_from('>d', self.d, o)[0], o + 8
    def ptr(self, o):
        """A pointer-sized field (tag, object size, reference): 8 bytes in
        RIF2, 4 in the older container."""
        return struct.unpack_from(self._pf, self.d, o)[0], o + self.W

    def string(self, o):
        n, o = self.u32(o)
        b = self.d[o:o + n]
        o += n
        if b.endswith(b'\xef\xbb\xbf'):
            b = b[:-3]
        b = b.split(b'\x00', 1)[0]
        return b.decode('utf-8', 'replace'), o

    # ---- class defs -------------------------------------------------
    def read_def(self, o):
        """Read a class-def record at o (positioned after its tag)."""
        n, o = self.u32(o)
        raw = self.d[o:o + n]; o += n
        name = raw.split(b'\x00', 1)[0].decode('latin1')
        ver, o = self.u16(o)
        return name, ver, o

    def resolve(self, off):
        """Class chain for the def record whose tag lives at payload offset off."""
        if off in self.defs:
            return self.defs[off]
        tag, o = self.ptr(off)
        if tag not in (-1, -2):
            raise ValueError(f'no class def at {off:#x} (tag={tag:#x})')
        name, ver, _ = self.read_def(o)
        res = ((name,), ver)
        self.defs[off] = res
        return res

    # ---- objects ----------------------------------------------------
    def read_obj(self, o, limit=None):
        hdr = o
        chain = []
        W = self.W
        while True:
            tag, o = self.ptr(o)
            if tag in (-1, -2):
                start = o - W
                name, ver, o = self.read_def(o)
                chain.append(name)
                self.defs[start] = (tuple(chain), ver)
                if tag == -1:
                    break
            elif tag < 0:
                # class reference: high bit + offset of the defining record
                self.cls_refs.add(o - W)
                c, ver = self.resolve(tag & self.MASK)
                chain = list(c)
                break
            else:
                raise ValueError('object ref %#x in header position at %#x'
                                 % (tag, hdr))
        size, o = self.ptr(o)
        if size < 0:
            raise ValueError(f'bad size {size} at {hdr:#x}')
        if limit is not None and o + size > limit:
            raise ValueError(f'size overflow at {hdr:#x}: {o + size} > {limit}')
        if o + size > len(self.d):
            raise ValueError(f'size past eof at {hdr:#x}')
        ob = Obj(tuple(chain), ver, hdr, o, o + size)
        self.objs[o - W] = ob
        return ob

    def looks_like_obj(self, o, limit):
        if o + 2 * self.W > limit:
            return False
        try:
            ob = self.read_obj(o, limit)
        except Exception:
            return False
        return True


def _deref(self, off):
    """Resolve an object reference: `off` is the offset of that object's
    size field (references always point backwards to an object already
    written).  Returns the previously parsed Obj when we have it, otherwise
    an Obj of unknown class covering the right byte range."""
    prev = self.objs.get(off)
    if prev is not None:
        return prev
    size, ds = self.ptr(off)
    if size < 0 or ds + size > len(self.d):
        raise ValueError('bad object ref %#x' % off)
    return Obj(('?',), 0, off, ds, ds + size)


Arch.deref = _deref


def open_cpr(path):
    d = open(path, 'rb').read()
    out = []
    w = width(d)
    cs = chunks(d)
    i = 0
    while i < len(cs):
        cid, off, size = cs[i]
        if cid == 'ROOT':
            a = Arch(d[off:off + size], w=w)
            names = []
            o = 0
            while o < size:
                s, o = a.string(o)
                names.append(s)
            cid2, off2, size2 = cs[i + 1]
            out.append((names, Arch(d[off2:off2 + size2],
                                    name=names[0] if names else '',
                                    base=off2, w=w)))
            i += 2
        else:
            i += 1
    return d, out
