"""Melodyne (ARA) edits between Cubase and REAPER.

Both hosts keep Melodyne's work as one document Melodyne itself writes: a
zlib-packed "GN" object archive ('GNBKVA' header). Cubase stores it in the
.cpr (an FMemoryStream), REAPER in the .rpp (<ARA ...> base64). The document
is the same format in both - the same analysis, notes and edits; measured on
Armenian Tales, 2026-10-01: one Cubase document played through REAPER's
Melodyne moved the same notes by the same cents - with two host-specific
parts:

* the persistent IDs. Melodyne finds its audio source and its audio
  modification in the document by the IDs the host hands it. Cubase names
  the source by a GUID and the modification by that GUID + '.1'; REAPER
  names both by one string, the one in <ARASRC> for the take (by default
  the media file's path, but any string works). So converting renames them.
* the musical context (Cubase's chord track, tempo map): Melodyne rebuilds
  it from the host on load, so it is left as it is.

The document's own layout, needed to rename a string without breaking it:

    u32 key count, keys (u32 length + name)
    u32 class count, classes (u32 length + name, u32, u32 field count,
        fields: u32 key, u32 1, type char, u32 byte size)
    u32 object count, one class index per object (0xffffffff: no record)
    u32 record count, one object index per record
    records: u32 size + payload (the class's fields in schema order, then
        two zero words; lists, strings and data carry variable payloads)

A string record holds u32 n, u32 n, n bytes (NUL-terminated); its record's
size field is the only other thing that changes with its length (measured
by letting REAPER save one document from two folders of different length).
"""
import bisect
import os
import re
import struct
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
TAKEFX_TEMPLATE = os.path.join(os.path.dirname(HERE), 'templates',
                               'melodyne_takefx.txt')
# REAPER's chunk name for Melodyne's document and ARA's "first modification"
CHUNK = 'com.celemony.ara.chunk.13'
FIRST_MOD = '{00000000-0000-0000-0000-000000000001}'
_NAME = re.compile(rb'[\x03-\x4f]\x00\x00\x00((?:MU|GN|MD|MI|MC|MA)[A-Za-z0-9]+)')


class Doc(object):
    """A parsed, uncompressed Melodyne document."""

    def __init__(self, raw):
        self.raw = raw
        n = struct.unpack_from('<I', raw, 0)[0]
        p = 4
        self.keys = []
        for _i in range(n):
            L = struct.unpack_from('<I', raw, p)[0]
            self.keys.append(raw[p + 4:p + 4 + L].decode('latin1'))
            p += 4 + L
        C = struct.unpack_from('<I', raw, p)[0]
        names = [(m.start(1), m.group(1).decode())
                 for m in _NAME.finditer(raw, p + 4, p + 4 + 400000)]
        if len(names) < C:
            raise ValueError('Melodyne document: %d of %d class names found'
                             % (len(names), C))
        self.classes = [nm for _o, nm in names[:C]]
        self.class_at = [o for o, _nm in names[:C]]
        q = names[C - 1][0] + len(names[C - 1][1])
        while True:
            N = struct.unpack_from('<I', raw, q)[0]
            if 100 < N < 10000000 and all(
                    x < C or x == 0xffffffff
                    for x in struct.unpack_from('<64I', raw, q + 4)):
                break
            q += 1
            if q > len(raw) - 300:
                raise ValueError('Melodyne document: no object table')
        self.table_at = q
        N = struct.unpack_from('<I', raw, q)[0]
        self.obj_class = list(struct.unpack_from('<%dI' % N, raw, q + 4))
        q += 4 + 4 * N
        M = struct.unpack_from('<I', raw, q)[0]
        self.rec_obj = list(struct.unpack_from('<%dI' % M, raw, q + 4))
        q += 4 + 4 * M
        self.recs = []
        for _i in range(M):
            L = struct.unpack_from('<I', raw, q)[0]
            self.recs.append((q, raw[q + 4:q + 4 + L]))
            q += 4 + L
        if q != len(raw):
            raise ValueError('Melodyne document: records end at %d of %d'
                             % (q, len(raw)))
        self.obj_rec = {o: r for r, o in enumerate(self.rec_obj)}
        self._schema = None

    def cls(self, r):
        c = self.obj_class[self.rec_obj[r]]
        return self.classes[c] if c < len(self.classes) else None

    def schema(self):
        if self._schema is None:
            out = []
            for i, nm in enumerate(self.classes):
                a = self.class_at[i] - 4
                b = (self.class_at[i + 1] - 4 if i + 1 < len(self.classes)
                     else self.table_at)
                e = self.raw[a:b]
                q = 4 + struct.unpack_from('<I', e, 0)[0]
                _sup, nf = struct.unpack_from('<II', e, q)
                q += 8
                fl = []
                for _k in range(nf):
                    key = struct.unpack_from('<I', e, q)[0]
                    fl.append((self.keys[key], chr(e[q + 8]),
                               struct.unpack_from('<I', e, q + 9)[0]))
                    q += 13
                out.append(fl)
            self._schema = out
        return self._schema

    def fields(self, r):
        """{field: (type, raw bytes)} of fixed-field record r."""
        rec = self.recs[r][1]
        q = 4
        out = {}
        for f, t, sz in self.schema()[self.obj_class[self.rec_obj[r]]]:
            out[f] = (t, rec[q:q + sz])
            q += sz
        return out

    def ref(self, r, field):
        """The object index a reference field of record r points at."""
        t, b = self.fields(r)[field]
        o = struct.unpack_from('<I', b, 0)[0]
        return None if o == 0xffffffff else o

    def string(self, obj):
        """The text of a GNString object."""
        rec = self.recs[self.obj_rec[obj]][1]
        for q in range(len(rec) - 8):
            a, b = struct.unpack_from('<II', rec, q)
            if a == b and 0 < a <= len(rec) - q - 8 and rec[q + 8 + a - 1] == 0:
                return rec[q + 8:q + 8 + a - 1].decode('utf-8', 'replace')
        return None

    def of_class(self, name):
        return [r for r in range(len(self.recs)) if self.cls(r) == name]

    def ids(self):
        """(source persistent IDs, [(modification ID, its source's ID)])."""
        src = [self.string(self.ref(r, 'persistentID'))
               for r in self.of_class('MUAraAudioSource')]
        mods = [self.string(self.ref(r, 'persistentID'))
                for r in self.of_class('MUAraAudioModification')]
        pairs = []
        for m in mods:
            # Cubase: the modification is its source's ID + '.<n>'
            owner = next((s for s in src if m == s or m.startswith(s + '.')),
                         None)
            pairs.append((m, owner))
        return src, pairs


def rename(raw, old, new):
    """The document with every string value `old` replaced by `new`."""
    ob = old.encode('utf-8') + b'\x00'
    nb = new.encode('utf-8') + b'\x00'
    pat = struct.pack('<II', len(ob), len(ob)) + ob
    rep = struct.pack('<II', len(nb), len(nb)) + nb
    hits = []
    i = raw.find(pat)
    while i >= 0:
        hits.append(i)
        i = raw.find(pat, i + 1)
    if not hits:
        return raw, 0
    starts = [p for p, _r in Doc(raw).recs]
    out = bytearray(raw)
    shift = 0
    for i in hits:
        r = starts[bisect.bisect_right(starts, i) - 1]
        size = struct.unpack_from('<I', raw, r)[0]
        struct.pack_into('<I', out, r + shift, size + len(nb) - len(ob))
        out[i + shift:i + shift + len(pat)] = rep
        shift += len(rep) - len(pat)
    return bytes(out), len(hits)


HEADER = b'GNBKVAi\x00\x06\x00\x00\x00\x01\x00\x00\x00'


def unpack(blob):
    """(16-byte header, uncompressed document) of a 'GNBKVA' blob."""
    if blob[:6] != b'GNBKVA':
        raise ValueError('not a Melodyne document')
    n = struct.unpack_from('<I', blob, 16)[0]
    return blob[:16], zlib.decompress(blob[20:20 + n])


def pack(raw, header=HEADER):
    z = zlib.compress(raw, 6)
    return header + struct.pack('<I', len(z)) + z


def documents(data):
    """Every Melodyne document (uncompressed) in a file's bytes.

    A .cpr also carries small 'GNBKVA' blocks that are Melodyne's plug-in
    view state; the document is the one with the ARA root object."""
    out = []
    i = data.find(b'GNBKVA')
    while i >= 0:
        n = struct.unpack_from('<I', data, i + 16)[0]
        try:
            raw = zlib.decompress(data[i + 20:i + 20 + n])
        except zlib.error:
            raw = b''
        if b'MUAraDocumentRoot' in raw[:20000] and b'audioModifications' in raw[:20000]:
            out.append(raw)
        i = data.find(b'GNBKVA', i + 6)
    return out


def takefx(indent):
    """REAPER's Melodyne take-FX block, with a fresh FXID."""
    with open(TAKEFX_TEMPLATE, encoding='utf-8') as f:
        lines = f.read().rstrip('\n').split('\n')
    out = []
    for l in lines:
        if l.strip() == 'WAK 0 0':
            out.append(indent + '  FXID {%s}' % str(uuid.uuid4()).upper())
        out.append(indent + l)
    return out


def b64_lines(blob, indent):
    import base64
    s = base64.b64encode(blob).decode()
    return [indent + s[k:k + 128] for k in range(0, len(s), 128)]


def cubase_to_reaper(docs, log=None, wanted=None):
    """Cubase's Melodyne documents as one REAPER document.

    Returns (document blob, {Cubase source ID: REAPER ARASRC string}), or
    (None, {}) when there is nothing usable. REAPER holds one Melodyne
    document per project; a Cubase project holds one as well."""
    if not docs:
        return None, {}
    if len(docs) > 1 and log is not None:
        log.append('%d Melodyne documents in the project; the first is '
                   'carried over' % len(docs))
    raw = docs[0]
    src, mods = Doc(raw).ids()
    names = {}
    seen = set()
    wanted = wanted or {}
    # the modification the events play first (an event names its own:
    # AXtModificationId's GUID.<n>), then any other
    mods.sort(key=lambda mo: (0 if wanted.get(mo[1]) == mo[0] else 1))
    for m, owner in mods:
        if owner is None:
            continue
        if owner in seen:
            # REAPER keeps one modification per source (ARA_POOLED_EDITS)
            if log is not None:
                log.append('Melodyne: source %s has more than one edited '
                           'version; REAPER keeps one, the first' % owner)
            continue
        seen.add(owner)
        raw, _n = rename(raw, m, owner)
        names[owner] = owner
    Doc(raw)                    # still whole
    return pack(raw), names


def set_string(raw, obj, new):
    """The document with GNString object `obj` set to `new` (its record's
    size follows the new length)."""
    D = Doc(raw)
    at, rec = D.recs[D.obj_rec[obj]]
    for q in range(len(rec) - 8):
        a, b = struct.unpack_from('<II', rec, q)
        if a == b and 0 < a <= len(rec) - q - 8 and rec[q + 8 + a - 1] == 0:
            nb = new.encode('utf-8') + b'\x00'
            body = rec[:q] + struct.pack('<II', len(nb), len(nb)) + nb + rec[q + 8 + a:]
            return raw[:at] + struct.pack('<I', len(body)) + body + raw[at + 4 + len(rec):]
    raise ValueError('object %d holds no string' % obj)


def set_field(raw, cls, field, value):
    """The document with a fixed-size field of every `cls` record set."""
    D = Doc(raw)
    out = bytearray(raw)
    for r in D.of_class(cls):
        q = 4
        for f, _t, sz in D.schema()[D.obj_class[D.rec_obj[r]]]:
            if f == field:
                if len(value) != sz:
                    raise ValueError('%s.%s is %d bytes' % (cls, field, sz))
                at = D.recs[r][0] + 4 + q
                out[at:at + sz] = value
            q += sz
    return bytes(out)


NULL_REF = b'\xff\xff\xff\xff\x00\x00\x00\x00'


def cubase_guid(name):
    """A stable Cubase-style GUID for a REAPER Melodyne ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'melodyne:' + name)).upper()


def reaper_to_cubase(docs, log=None):
    """REAPER's Melodyne document for Cubase.

    Returns (document blob, {REAPER ID: Cubase source GUID}). REAPER names a
    source and its modification by one string; Cubase's source is a GUID
    and its modification that GUID + '.1'. REAPER's musical context also
    points at a REAPER region sequence (regionSequenceData), which Cubase
    has none of: left in, it crashed Cubase on load (2026-10-01), so it is
    cleared - Melodyne takes the musical context from the host anyway."""
    if not docs:
        return None, {}
    raw = docs[0]
    D = Doc(raw)
    names = {}
    edits = []
    for r in D.of_class('MUAraAudioSource'):
        o = D.ref(r, 'persistentID')
        s = D.string(o)
        names[s] = cubase_guid(s)
        edits.append((o, names[s]))
    for r in D.of_class('MUAraAudioModification'):
        o = D.ref(r, 'persistentID')
        s = D.string(o)
        if s in names:
            edits.append((o, names[s] + '.1'))
        elif log is not None:
            log.append('Melodyne: a modification (%s) with no source of its '
                       'own was left as it is' % s)
    for o, new in edits:
        raw = set_string(raw, o, new)
    if os.environ.get('CPR_ARA_SYNC'):
        raw = set_field(raw, 'MUAraAudioSource', 'timelineIsSynchronizedWithHost', b'\x01')
    if os.environ.get('CPR_ARA_ORDER0'):
        raw = set_field(raw, 'MUAraMusicalContextPersistentData', 'orderIndex',
                        struct.pack('<i', 0))
    if 'regionSequenceData' in [f for fl in Doc(raw).schema() for f, _t, _s in fl]:
        raw = set_field(raw, 'MUAraMusicalContextPersistentData',
                        'regionSequenceData', NULL_REF)
    Doc(raw)
    return pack(raw), names
