"""Cubase fade records, built from a real save.

An audio event carries, right after its clip, an i64 and then two 8-byte
slots: one for a fade-in object and one for a fade-out object. A slot is
eight zero bytes when the event has no such fade, and the object itself
when it has (checked against Cubase's own saves: happy.cpr, Improv in Dm).
The body here is the byte-exact, self-contained instance a real project
carries (it declares its own interpolator classes); between fades only the
curve points and the length differ, and the length is stored twice - as the
last point's x and again after the interpolator - in samples of the file.

The interpolator is an MLinearInterpolator: Cubase draws straight lines
between its points. Its own saves hold 2 points for a straight fade, 3 for
the default fade-out, 6 and more for a drawn curve - so any curve can be
handed to Cubase as enough points along it (fades.points), which is how a
REAPER fade shape arrives editable rather than printed into the audio.
"""
import base64
import struct

# MFadeOut body, 146 bytes: MLinearInterpolator with 2 points, then the
# fade's own fields, all self-declared
_BODY = base64.b64decode(
    b'//////////4AAAAOTUludGVycG9sYXRvcgAAAP//////////AAAAFE1MaW5lYXJJbnRlcnBvbGF0b3IAAAAAAAAAAAAARAAAAAIAAAAAAAAAAD/wAAAAAAAAQJSsABuP4AAAAAAAAAAAAAAAAAAAAAAAQJSsABuP4AAAAAAAAAAAAD/wAAAAAAAAP+AAAAAAAAA=')

# byte offsets inside the body: the interpolator's size field and point
# count, its points, and the fade length repeated in the tail after them
_ISIZE = 62             # i64: bytes of the interpolator object's data
_COUNT = 70             # u32: how many (x, y) f64 pairs follow
_PTS = 74               # the points, 16 bytes each
_TAIL = 106             # what follows the (two) points in the template
_LEN_IN_TAIL = 8        # the length sits 8 bytes into the tail

_CHAINS = {
    'in': (('MFade', 3), ('MFadeIn', 0)),
    'out': (('MFade', 3), ('MFadeOut', 0)),
}


def _header(kind, size):
    """A declaring object header for the fade class chain."""
    out = bytearray()
    chain = _CHAINS[kind]
    for i, (name, ver) in enumerate(chain):
        out += struct.pack('>q', -1 if i == len(chain) - 1 else -2)
        raw = name.encode('ascii') + b'\x00'
        out += struct.pack('>I', len(raw)) + raw
        out += struct.pack('>H', ver)
    out += struct.pack('>q', size)
    return bytes(out)


def blob(kind, length_samples, points=None):
    """One complete fade object: fade-in or fade-out of this many samples.

    `points` are (x, gain) pairs with x in 0..1 along the fade; None gives
    the straight fade (0->1 in, 1->0 out)."""
    length = float(length_samples)
    if not points:
        points = [(0.0, 0.0), (1.0, 1.0)] if kind == 'in' else [(0.0, 1.0), (1.0, 0.0)]
    pts = bytearray()
    for x, y in points:
        pts += struct.pack('>d', float(x) * length) + struct.pack('>d', float(y))
    tail = bytearray(_BODY[_TAIL:])
    struct.pack_into('>d', tail, _LEN_IN_TAIL, length)
    body = bytearray(_BODY[:_COUNT]) + struct.pack('>I', len(points)) + pts + tail
    # the interpolator object's own size: count, points and its part of the
    # tail (everything up to the fade's last field)
    inner = 4 + len(pts) + (len(_BODY) - _TAIL - 8)
    struct.pack_into('>q', body, _ISIZE, inner)
    return _header(kind, len(body)) + bytes(body)
