"""Cubase attribute containers, on top of arch.Arch.

Two different containers appear in a .cpr:

1. 4CC blocks (track-event "Additional Attributes"):
       u32 count, then (key[4] little-endian 4CC, u16 type, value)*

2. Named trees (device / mixer / plug-in state):
       entry     := string name, u16 type, value
       value     := 1 -> i64 | 3 -> f32 | 4 -> f64 | 8 -> string
                    2 -> container (u16 elemkind + per-kind payload)
                    0x14 -> embedded object
                    other >=0x10 -> nested ARCH object (or an object reference)
       elemkind  := 2 int64[] | 3 double[] | 4 string[] | 8 string[]
                    5 array of anonymous groups (u32 count + entries)
                    6 named entries
                    1/7 raw byte blob of n bytes  (plug-in state chunks)
                    0x14 one embedded object (the u32 after it is a NAME LENGTH)
                    0x15 inline object: i32 runtime id + n entries
                    0xc9 heterogeneous array: n * (u16 type, value)
       object    := u32 namelen, name, i64 runtime id, u32 count, entries
"""


class Node(dict):
    """An attribute group. Duplicate keys are kept in .multi (ordered)."""
    cname = ''
    oid = 0

    def __init__(self):
        dict.__init__(self)
        self.multi = []
        # name -> (offset of the value in the payload, its type code), so a
        # fixed-width value can be written straight back into the .cpr
        self.offs = {}

    def at(self, name, off, t):
        self.offs.setdefault(name, (off, t))

    def add(self, name, val):
        self.multi.append((name, val))
        if name in self:
            cur = self[name]
            if isinstance(cur, ListVal):
                cur.append(val)
            else:
                self[name] = ListVal([cur, val])
        else:
            self[name] = val

    def all(self, name):
        v = self.get(name)
        if v is None:
            return []
        return list(v) if isinstance(v, ListVal) else [v]


class ListVal(list):
    spans = ()      # (start, end) of each group, when known


class Attrs:
    def __init__(self, arch):
        self.A = arch

    # ---------------- 4CC blocks ----------------
    def fourcc(self, o, limit, where=None):
        """A block of four-character-key attributes.

        `where`, when given, is filled in with the block's shape - where the
        count sits, where the block ends, and where each value is - so an
        attribute can be written back, or one the track does not have yet
        can be added on the end."""
        A = self.A
        start = o
        n, o = A.u32(o)
        if n > 4096:
            raise ValueError('4CC count %d' % n)
        out = {}
        for _ in range(n):
            key = A.d[o:o + 4][::-1].decode('latin1', 'replace')
            o += 4
            t, o = A.u16(o)
            if where is not None:
                where.setdefault('keys', {})[key] = (o, t)
            if t == 1:
                v, o = A.i64(o)
            elif t == 4:
                v, o = A.f64(o)
            elif t == 8 and not A.looks_like_obj(o, limit):
                v, o = A.string(o)
            elif A.looks_like_obj(o, limit):
                # an object - also where a string is wrapped in one (the ARA
                # block on an older project's event: 'IUEX' type 8, then a
                # class reference, a size and the string)
                ob = A.read_obj(o, limit)
                v, o = ob, ob.de
            else:
                A.obj_refs.add(o)
                ref, o = A.ptr(o)
                v = A.deref(ref)
            out[key] = v
        if where is not None:
            where['count_off'] = start
            where['count'] = n
            where['end'] = o
        return out, o

    # ---------------- named trees ----------------
    def entries(self, o, n, end):
        A = self.A
        g = Node()
        for _ in range(n):
            name, o = A.string(o)
            t, o = A.u16(o)
            g.at(name, o, t)
            v, o = self.value(t, o, end)
            g.add(name, v)
        return g, o

    def value(self, t, o, end):
        A = self.A
        if t == 1:
            return A.i64(o)
        if t == 3:
            return A.f32(o)
        if t == 4:
            return A.f64(o)
        if t == 8:
            return A.string(o)
        if t == 0x14:
            return self.obj(o, end)
        if t == 2:
            kind, o = A.u16(o)
            if kind == 0x14:
                return self.obj(o, end)
            n, o = A.u32(o)
            return self.container(kind, n, o, end)
        if t >= 0x10:
            if A.looks_like_obj(o, end):
                ob = A.read_obj(o, end)
                return ob, ob.de
            A.obj_refs.add(o)
            ref, o = A.ptr(o)
            return A.deref(ref), o
        raise ValueError('attr type %d at %#x' % (t, o))

    def obj(self, o, end):
        A = self.A
        ln, o = A.u32(o)
        raw = A.d[o:o + ln]
        o += ln
        oid, o = A.i64(o)      # an id, 8 bytes in both containers
        n, o = A.u32(o)
        g, o = self.entries(o, n, end)
        g.cname = raw.split(b'\x00', 1)[0].decode('utf-8', 'replace')
        g.oid = oid
        return g, o

    def container(self, kind, n, o, end):
        A = self.A
        if kind == 6:
            return self.entries(o, n, end)
        if kind == 5:
            out = ListVal()
            # remember each group's byte range: an insert slot is one of
            # these, and adding a plug-in means writing another
            out.spans = []
            for _ in range(n):
                start = o
                c, o = A.u32(o)
                g, o = self.entries(o, c, end)
                out.append(g)
                out.spans.append((start, o))
            return out, o
        if kind == 0x15:
            oid, o = A.i32(o)
            g, o = self.entries(o, n, end)
            g.oid = oid
            return g, o
        if kind == 0xc9:
            out = ListVal()
            for _ in range(n):
                t, o = A.u16(o)
                v, o = self.value(t, o, end)
                out.append(v)
            return out, o
        if kind in (2, 3, 4, 8):
            rd = {2: A.i64, 3: A.f64, 4: A.string, 8: A.string}[kind]
            out = ListVal()
            for _ in range(n):
                v, o = rd(o)
                out.append(v)
            return out, o
        if kind in (1, 7):
            return A.d[o:o + n], o + n
        raise ValueError('container kind %d n=%d at %#x' % (kind, n, o))

    def group(self, o, end, tolerant=True):
        """u32 count + that many entries. Returns (Node, end, error-or-None)."""
        A = self.A
        n, o = A.u32(o)
        g = Node()
        for i in range(n):
            st = o
            try:
                name, o = A.string(o)
                t, o = A.u16(o)
                # where the value sits and what it is, so a fixed-width one
                # can be written back and an entry added after another
                g.at(name, o, t)
                v, o = self.value(t, o, end)
            except Exception as e:
                if not tolerant:
                    raise
                return g, o, '%s (entry %d/%d @%#x)' % (e, i, n, st)
            g.add(name, v)
        return g, o, None

    def device_tree(self, dev):
        """M*Track device object -> (device name, tree, error-or-None)"""
        A = self.A
        o = dev.ds
        _, o = A.u16(o)
        name, o = A.string(o)
        _, o = A.i32(o)
        _, o = A.i32(o)
        tree, _o, err = self.group(o, dev.de)
        return name, tree, err
