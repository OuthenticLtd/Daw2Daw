"""Read a Cubase project (.cpr) into the intermediate model.

Notes on the units Cubase uses, all established by cross-checking against the
project itself:
  * track/marker/MIDI positions are ticks at 480 per quarter note
  * audio event length and start offset are SAMPLES, not ticks
  * a channel fader stores both a raw position and its dB value
    ('Value' / 'AnchorValue'), so levels are read straight off in dB
  * event flag bit 0x0002 means muted
"""
import math
import os
import re
import struct

from . import arch
from . import envelope
from . import progress
from .cubase_attrs import Attrs, Node, ListVal
from .model import Project, Track, Item, Marker, Fx, Send

PPQ = 480.0
MUTED = 0x0002
TRACK_EVENT_PREFIX = 26
# Which channel parameter an automation lane follows. Volume is 2, proven by
# the donor. Pan is not yet pinned down: a lane can be pointed at any number
# and reads back as that number. Pan is 7 within the device "Panner"
# (seen in the user's own projects with pan automation, 2026-09-28).
PAN_PARAM = 7
DEVICE_CLASSES = ('MTrack', 'MAudioTrack', 'MMidiTrack', 'MInstrumentTrack',
                  'MVideoTrack')

_DEF_RE = re.compile(rb'\xff{7}[\xfe\xff]\x00\x00\x00(.)', re.S)
# the older container writes a class definition's tag in 4 bytes
_DEF_RE4 = re.compile(rb'\xff{3}[\xfe\xff]\x00\x00\x00(.)', re.S)
_CLS_RE = re.compile(rb'[A-Za-z_][A-Za-z0-9_:]*\Z')


def variaudio_env(segs, soffs, playrate, length):
    """VariAudio's moved notes as a take pitch envelope: (seconds into the
    item, semitones), a step at each note's edges (two points at the same
    time), 0 between notes. segs are (start, end, semitones) in seconds of
    the file; the item plays the file from soffs at playrate."""
    end_item = soffs + length * playrate
    steps = []                                  # (item time, value from there)
    for t0, t1, st in sorted(segs):
        if t1 <= soffs or t0 >= end_item:
            continue
        steps.append(((max(t0, soffs) - soffs) / playrate, st))
        steps.append(((min(t1, end_item) - soffs) / playrate, 0.0))
    if not steps:
        return []
    # a note ending where the next begins: the later step wins
    merged = []
    for t, v in steps:
        if merged and abs(merged[-1][0] - t) < 1e-9:
            merged[-1] = (t, v)
        else:
            merged.append((t, v))
    pts, cur = [(0.0, 0.0)], 0.0
    for t, v in merged:
        if abs(v - cur) < 1e-9:
            continue
        if t <= 0:
            pts = [(0.0, v)]
        else:
            pts += [(t, cur), (t, v)]
        cur = v
    return pts


def scan_class_names(buf, w=8):
    names = set()
    for m in (_DEF_RE if w == 8 else _DEF_RE4).finditer(buf):
        n = m.group(1)[0]
        if not 3 <= n <= 64:
            continue
        raw = buf[m.end():m.end() + n]
        if len(raw) == n and raw.endswith(b'\x00') and _CLS_RE.match(raw[:-1]):
            names.add(raw[:-1].decode('latin1'))
    return names


# Cubase's fader taper, measured off projects Cubase itself saved: the
# position it stores (Value / 32768) against the level that position stands
# for (AnchorValue, in dB). 0 dB sits at 0.789 and the fader tops out at
# +6.02 dB, and the curve is nothing like linear in gain.
#
# It has to be built in because a donor cannot supply it. A project Cubase
# saved carries one pair per track and so calibrates itself, but a donor
# carries exactly one - the 0 dB position - and one point is not a curve.
# Extrapolating from it treated the fader as linear in gain and wrote -25 dB
# as position 0.042, which on the real curve is silence: every attenuated
# track arrived far too quiet, the quieter the worse. That was the balance.
# Position 0 is -inf and is deliberately left out; below the lowest measured
# point the curve follows gain = (x/x1)**p with p fitted from the two lowest
# points, which comes out at very nearly 2.
FADER_TAPER = (
    (0.167508, -27.3037),
    (0.170462, -27.0000),
    (0.177660, -26.2816),
    (0.209029, -23.4568),
    (0.235018, -21.4211),
    (0.249028, -20.4152),
    (0.271159, -18.9361),
    (0.299483, -17.2102),
    (0.309026, -16.6653),
    (0.340091, -15.0013),
    (0.369024, -13.5829),
    (0.389896, -12.6271),
    (0.410126, -11.7484),
    (0.430241, -10.9167),
    (0.431763, -10.8553),
    (0.472981, -9.2714),
    (0.477143, -9.1192),
    (0.499985, -8.3068),
    (0.504117, -8.1639),
    (0.512370, -7.8818),
    (0.520426, -7.6107),
    (0.526380, -7.4131),
    (0.531760, -7.2365),
    (0.561966, -6.2767),
    (0.577749, -5.7302),
    (0.586759, -5.3909),
    (0.617759, -4.3155),
    (0.618766, -4.2827),
    (0.629068, -3.9541),
    (0.642182, -3.5531),
    (0.644498, -3.4841),
    (0.649067, -3.3497),
    (0.669066, -2.7846),
    (0.669704, -2.7671),
    (0.674053, -2.6492),
    (0.690639, -2.2136),
    (0.695410, -2.0922),
    (0.699081, -2.0000),
    (0.703153, -1.8988),
    (0.709065, -1.7540),
    (0.729064, -1.2812),
    (0.746399, -0.8912),
    (0.769063, -0.4064),
    (0.778870, -0.2047),
    (0.789062, 0.0000),
    (0.876070, 3.0000),
    (0.933982, 4.5429),
    (0.999969, 6.0206),
)


def taper_gain(norm):
    """A fader position as a gain, by Cubase's own curve (see fader.py).

    For checking a project's two fader halves against each other: a file
    whose dB is right and whose position is wrong plays at the wrong level."""
    from . import fader
    return fader.norm_to_gain(norm)


class CprReader:
    def __init__(self, path):
        self.path = path
        # The whole archive is read and indexed in one go, before any track
        # is known, so say what is happening first rather than sit silent.
        try:
            size = ' (%s)' % progress.fmt_bytes(os.path.getsize(path))
        except OSError:
            size = ''
        progress.stage('opening %s%s' % (os.path.basename(path), size))
        _d, parts = arch.open_cpr(path)
        self.A = None
        for names, a in parts:
            if names and names[-1] == 'PArrangement':
                self.A = a
        if self.A is None:
            raise ValueError('%s: no PArrangement chunk' % path)
        self.at = Attrs(self.A)
        self.tempo_pts = [(0.0, 0.0, 0.5)]     # (ticks, seconds, sec/quarter)
        self.tempo_area = None
        self.tsig = (4, 4)
        self.palette = []
        self.fader = []
        self.clip_cache = {}
        self._cnames = None
        self.log = []

    # ---------------------------------------------------------- helpers
    def warn(self, msg):
        self.log.append(msg)

    def class_names(self):
        if self._cnames is None:
            self._cnames = scan_class_names(self.A.d, self.A.W)
        return self._cnames

    # Each node class lays its header out slightly differently: a spare u16
    # sits before the child count on some of them and not on others.
    PAD_BEFORE_COUNT = ('MTrackList', 'MAutomationNode')

    def node_header(self, o, limit, extra_u16=False, cls=None):
        A = self.A
        if cls is not None:
            extra_u16 = cls in self.PAD_BEFORE_COUNT
        name, o = A.string(o)
        dt, o = A.i32(o)
        dom = {'type': dt}
        if dt == 1:
            dom['period'], o = A.f64(o)
            if cls is None or extra_u16:
                _, o = A.u16(o)
        else:
            for key in ('tempo', 'sig'):
                if A.looks_like_obj(o, limit):
                    ob = A.read_obj(o, limit)
                    dom[key] = ob
                    o = ob.de
                    self._global_track(ob)
                else:
                    dom[key], o = A.ptr(o)
            if extra_u16:
                _, o = A.u16(o)
        # remember where the child count sits: adding or removing a track
        # means writing a new one, and the caller has no other way to find it
        self.count_off = o
        cnt, o = A.u32(o)
        return name, dom, cnt, o

    def _global_track(self, ob):
        if ob.cls == 'MTempoTrackEvent' and len(self.tempo_pts) < 2:
            self.read_tempo(ob)
        elif ob.cls == 'MSignatureTrackEvent':
            self.read_tsig(ob)

    def read_tempo(self, ob):
        A = self.A
        o = ob.ds
        count_off = o
        n, o = A.u32(o)
        first = o
        pts = []
        for _ in range(n):
            spq, o = A.f32(o)
            sec, o = A.f64(o)
            tick, o = A.f64(o)
            _, o = A.u16(o)
            pts.append((tick, sec, spq))
        # where the map itself sits, so a project's own tempo can replace it.
        # The record may carry a tail after the points, so the end is where
        # the points actually stop rather than where the object does.
        self.tempo_area = {'count_off': A.base + count_off,
                           'first': A.base + first, 'end': A.base + o}
        if pts:
            self.tempo_pts = pts

    def read_tsig(self, ob):
        A = self.A
        o = ob.ds
        n, o = A.u32(o)
        if n:
            _, o = A.i32(o)
            num, o = A.u16(o)
            den, o = A.u16(o)
            if 1 <= num <= 64 and den in (1, 2, 4, 8, 16, 32, 64):
                self.tsig = (num, den)

    def apply_processes(self, it, procs, info, rate, track):
        """A clip's offline processes on the converted item. Reverse is
        REAPER's own reversed take (a SECTION in MODE 3 over the original
        file); the event plays a reversed stretch of the clip when its range
        lies inside the reversed range. Anything else is reported."""
        frames = info.get('frames')
        rev = [p for p in procs if p[0] == 'Reverse']
        other = sorted({p[0] for p in procs if p[0] != 'Reverse'})
        edits = self.edits_copy(info, it)
        native = (rev and not other and frames and len({(a, n) for _x, a, n in rev}) == 1
                  and not getattr(it, 'stretch_markers', None)
                  and self.local_file(it.file) is not None)
        if native:
            (a, n), = {(a, n) for _x, a, n in rev}
            o = it.soffs * rate
            L = it.length * (it.playrate or 1.0) * rate
            native = len(rev) % 2 == 0 or not (o < a - 0.5 or o + L > a + n + 0.5)
        if not native and edits:
            # What Cubase plays is its own render of the processed clip, the
            # same length as the clip, in the project's Edits folder: the
            # event plays that, at the same offset. Rebuilding the process
            # was not possible here - only part of the clip reversed, an
            # effect REAPER has no setting for, or (SuperThunderCrown) the
            # original file not even on this computer.
            it.file = edits
            return
        if other:
            self.warn('%r: an event at %.2f s carries Cubase offline processing '
                      '(%s), which does not cross: the converted project plays '
                      'the file without it' % (track.name, it.pos, ', '.join(other)))
            return
        if not rev or not frames:
            return
        ranges = {(a, n) for _nm, a, n in rev}
        o = it.soffs * rate
        L = it.length * (it.playrate or 1.0) * rate
        if len(ranges) != 1 or getattr(it, 'stretch_markers', None):
            self.warn('%r: an event at %.2f s was reversed in parts in Cubase; '
                      'it plays forwards in the converted project' % (track.name, it.pos))
            return
        (a, n), = ranges
        if len(rev) % 2 == 0:
            return              # reversed back again
        if o < a - 0.5 or o + L > a + n + 0.5:
            self.warn('%r: an event at %.2f s plays past the part of its clip '
                      'that was reversed; it plays forwards in the converted '
                      'project' % (track.name, it.pos))
            return
        # clip sample c inside the range is file sample a + n - (c - a), and
        # REAPER's reversed take at offset x plays the file from N - x back
        it.soffs = (frames - n - 2 * a + o) / rate
        it.section = {'startpos': 0.0, 'length': frames / rate, 'mode': 3,
                      'overlap': 0.01, 'reverse': True}

    def local_file(self, path):
        """The file at `path`, or one of that name in the project's folder
        (where a moved project keeps it), or None."""
        if not path:
            return None
        if os.path.isfile(path):
            return path
        name = re.split(r'[\\/]', path)[-1].lower()
        idx = getattr(self, '_local_idx', None)
        if idx is None:
            idx = {}
            root = os.path.dirname(os.path.abspath(self.path))
            for dp, _dn, fns in os.walk(root):
                for f in fns:
                    idx.setdefault(f.lower(), os.path.join(dp, f))
            self._local_idx = idx
        return idx.get(name)

    def edits_copy(self, info, it):
        """Cubase's rendered copy of a processed clip (Edits/...), when it
        is on this computer and as long as the clip, or None."""
        name = info.get('edits_file')
        if not name:
            return None
        # the name carries the clip's process id, so it is this clip's copy
        return self.local_file(name)

    def read_processes(self, ds, de):
        """The offline processes applied to a clip (Audio > Process), as
        [(name, first sample, samples)] in the order applied, or None.

        Cubase keeps the clip's history as PAudioProcessCommand records,
        each with the range it was applied to (big-endian i64 start and
        length at +8 and +16) and its name in a length-prefixed string
        ("Reverse"), and plays a copy it rendered into the project's Edits
        folder. Measured on a save after Audio > Process > Reverse on an
        event playing the first 288000 of a 384000-sample file
        (2026-10-01): start 0, length 288000 - the event's range, not the
        file."""
        A = self.A
        out = []
        for ob in A.objs.values():
            if ob.cls != 'PAudioProcessCommand' or not (ds <= ob.ds < de):
                continue
            try:
                a, n = struct.unpack_from('>qq', A.d, ob.ds + 8)
                if not (0 <= a < 1 << 40 and 0 < n < 1 << 40):
                    # the other layout Cubase writes (SuperThunderCrown's
                    # partly reversed clip): 0 at +8, then start and length
                    # as two big-endian i32 at +16 - read as one i64 the
                    # pair made a start of -387 million seconds, and the
                    # event played nothing
                    a, n = struct.unpack_from('>ii', A.d, ob.ds + 16)
                    if not (0 <= a and 0 < n):
                        continue
            except struct.error:
                continue
            blob = bytes(A.d[ob.ds:ob.de])
            m = re.search(rb'([\x02-\x40])\x00\x00\x00([A-Z][A-Za-z ]{1,40})\x00\xef\xbb\xbf', blob)
            name = m.group(2).decode('latin1') if m else '?'
            out.append((ob.ds, name, a, n))
        if not out:
            return None
        return [(name, a, n) for _o, name, a, n in sorted(out)]

    def warp_markers(self, pts, rate, start, length, offset, pos):
        """A musical-mode event's warp as REAPER stretch markers, or None
        when one constant rate says it all.

        The clip's warp scale maps file samples to clip ticks (u32 n, then n
        f64 pairs); the event plays clip ticks offset..offset+length at
        project ticks start..start+length, so each tab puts a file second
        at an item second through the project's tempo map. Markers go at
        every tab inside the event, at its two ends, and at every tempo
        change under it (between two markers REAPER runs the source in a
        straight line)."""
        if not pts or len(pts) < 2:
            return None
        pts = sorted(pts, key=lambda p: p[1])
        (x0, c0), (x1, c1) = pts[0], pts[-1]
        if c1 <= c0 or x1 <= x0:
            return None
        straight = all(abs(x0 + (x1 - x0) * (c - c0) / (c1 - c0) - x) < 0.5
                       for x, c in pts[1:-1])
        changes = [t for t, _s, _q in self.tempo_pts[1:]
                   if start + 1e-6 < t < start + length - 1e-6]
        if straight and not changes:
            return None

        def sample_at(c):
            for (xa, ca), (xb, cb) in zip(pts, pts[1:]):
                if c <= cb or (xb, cb) == pts[-1]:
                    if cb <= ca:
                        return xa
                    return xa + (xb - xa) * (c - ca) / (cb - ca)
            return x1

        ticks = {offset, offset + length}
        ticks.update(c for _x, c in pts if offset < c < offset + length)
        ticks.update(t - start + offset for t in changes)
        out = []
        for c in sorted(ticks):
            tau = self.t2s(start + (c - offset)) - pos
            src = sample_at(c) / rate
            if out and (tau <= out[-1][0] + 1e-9 or src <= out[-1][1] + 1e-12):
                continue
            out.append((max(0.0, tau), src))
        return out if len(out) >= 2 else None

    def t2s(self, ticks):
        base_t, base_s, spq = self.tempo_pts[0]
        for t, s, sq in self.tempo_pts:
            if ticks + 1e-9 >= t:
                base_t, base_s, spq = t, s, sq
            else:
                break
        rate = PPQ / spq if spq > 0 else 960.0
        return base_s + (ticks - base_t) / rate

    def note_fader(self, raw, db):
        if raw is not None and db is not None:
            self.fader.append((raw / 32768.0, db))

    def fader_table(self):
        """The position/dB calibration to convert this project's levels by.

        The measured taper is the base and the project's own pairs refine
        it, so a project Cubase saved still calibrates itself exactly while
        a donor - which offers a single point - no longer has a whole curve
        guessed from that one point."""
        tbl = dict(FADER_TAPER)
        for x, d in self.fader:
            if x > 0.0:
                tbl[round(x, 6)] = d
        return sorted(tbl.items())

    @staticmethod
    def _low_exponent(tbl):
        """p in gain = (x / x1) ** p, from the two quietest points."""
        import math
        (x1, d1), (x2, d2) = tbl[0], tbl[1]
        g1, g2 = 10 ** (d1 / 20.0), 10 ** (d2 / 20.0)
        if x1 <= 0 or x2 <= x1 or g1 <= 0 or g2 == g1:
            return 2.0, x1, g1
        p = math.log(g2 / g1) / math.log(x2 / x1)
        return (p if p > 0 else 2.0), x1, g1

    def gain_to_norm(self, gain):
        """A linear gain as the fader position Cubase stores for it.

        Cubase's exact curve (fader.py, fitted 2026-09-28 to every pair
        above within 0.0006 dB). The table interpolation this replaced left
        a -6.02 dB send 0.03 dB off in the export comparison; the curve
        leaves nothing."""
        from . import fader
        return fader.gain_to_norm(gain)

    def norm_to_gain(self, norm):
        """A fader position as the linear gain it stands for."""
        from . import fader
        if norm <= 0:
            return 0.0
        # a fader position outside 0..1 is not a level, and extrapolating one
        # overflows rather than saying so
        return min(fader.norm_to_gain(norm), 10 ** (24.0 / 20.0))

    # ---------------------------------------------------------- structure
    def node_of(self, tr):
        return self.A.read_obj(tr.ds + TRACK_EVENT_PREFIX, tr.de)

    def track_tail(self, tr):
        node = self.node_of(tr)
        at, o = self.at.fourcc(node.de, tr.de)
        rest = []
        while o < tr.de:
            try:
                ob = self.A.read_obj(o, tr.de)
            except Exception:
                break
            if ob.de <= o:
                break
            rest.append(ob)
            o = ob.de
        return node, at, rest

    def device_of(self, tr):
        try:
            _n, _a, rest = self.track_tail(tr)
        except Exception:
            return None
        for ob in rest:
            if ob.cls in DEVICE_CLASSES:
                return ob
        return None

    def find_palette(self, lo, hi):
        A = self.A
        i = A.d.find(b'oCvE', lo, hi)
        while i != -1:
            try:
                ob = A.read_obj(i + 6, hi)
                if ob.cls == 'UColorSet':
                    o = ob.ds
                    _name, o = A.string(o)
                    n, o = A.u32(o)
                    out = []
                    for _ in range(n):
                        _cn, o = A.string(o)
                        _a, r, g, b = A.d[o:o + 4]
                        o += 4
                        out.append((r, g, b))
                    return out
            except Exception:
                pass
            i = A.d.find(b'oCvE', i + 1, hi)
        return []

    # ---------------------------------------------------------- entry point
    def read(self, keep_buses=False):
        """`keep_buses` leaves Cubase's I/O channels in the track list: the
        builder reads its donor that way, since the Input/Output folder is
        where the emptied template records are tucked away."""
        self.keep_buses = keep_buses
        A = self.A
        root = A.read_obj(0)
        mroot = A.read_obj(root.ds, root.de)
        tl = A.read_obj(mroot.ds + TRACK_EVENT_PREFIX, mroot.de)
        _n, _d, cnt, o = self.node_header(tl.ds, tl.de, cls=tl.cls)
        self._list_count_off = self.count_off
        # the list every top-level track hangs off: where new ones are added
        self._top_list = {'sf': A.base + tl.ds - 8,
                          'count_off': A.base + self.count_off}
        tops = []
        self._top_refs = []
        for _ in range(cnt):
            ob, o = self.obj_or_ref(o, tl.de)
            if ob.cls == '?':
                # stored inside a track not read yet: resolved in its turn
                self._top_refs.append(ob.hdr)
                tops.append(ob.hdr)
                continue
            tops.append(ob)
        self.palette = self.find_palette(tl.de, mroot.de)

        p = Project()
        p.src = getattr(self, '_top_list', {})
        p.name = os.path.splitext(os.path.basename(self.path))[0]
        p.tsig = self.tsig
        p.pan_law_of = 'cubase'         # Stereo Balance Panner values (panlaw.py)
        # Melodyne's own document (ara.py): only read when an event carries
        # Melodyne edits, since finding it means scanning the whole file
        p.ara_docs = []
        self.proj = p
        # The tempo and signature live in whichever track record declares
        # them first, and every later list refers to them. With the marker
        # track moved to the top, markers are met before the tempo is known,
        # so every header is looked at once before anything is read.
        def peek(tr):
            try:
                node = self.node_of(tr)
                _n, _d, cnt, o = self.node_header(node.ds, node.de, cls=node.cls)
                if tr.cls == 'MFolderTrack':
                    for _ in range(cnt):
                        ob = A.read_obj(o, node.de)
                        peek(ob)
                        o = ob.de
            except Exception:
                pass
        for tr in tops:
            if not isinstance(tr, int):
                peek(tr)
        bar = progress.Progress(len(tops), 'reading Cubase project', 'tracks')
        for tr in tops:
            if isinstance(tr, int):
                # a reference to a track stored inside an earlier one (Big
                # Win 4's MIDI track inside its chord track): read by now
                ob = A.objs.get(tr)
                if ob is None or not ob.cls.endswith(('TrackEvent', 'Track')):
                    self.warn('a track list entry refers to an object that '
                              'is not a track (%s) - skipped'
                              % (ob.cls if ob else 'unread'))
                    bar.step()
                    continue
                tr = ob
            self.walk(tr, 0, None)
            bar.step(note='%d tracks' % len(p.tracks))
        bar.done(note='%d tracks' % len(p.tracks))
        p.tempo = self.tempo_curve()
        p.tsig = self.tsig
        self.read_arrange_setup(p)
        self.resolve_sends(p)
        self.take_master(p)
        self.read_output_channel(p)
        # where the project lives: media the file names by an old path is
        # looked for under here (Cubase's own Audio folder among others)
        p.srcdir = os.path.dirname(os.path.abspath(self.path))
        self.shift_to_bar_one(p)
        return p

    def shift_to_bar_one(self, p):
        """Move every time so that Cubase's bar 1 is REAPER's 0:00.

        Positions in the file count from the project's Start (see
        read_arrange_setup); with a pre-roll they all sit that much after
        bar 1. REAPER's timeline begins at 0, so the pre-roll is taken off
        events, markers, automation and the tempo map. An event that
        began inside the pre-roll is cut at 0."""
        pre = -float(p.start or 0.0)
        # where the earliest event begins, in seconds from bar 1 (negative
        # inside the pre-roll): Cubase's own export of the project starts
        # there when its locators are set to the selection, and the identity
        # test lines the two renders up with it
        starts = [it.pos for t in p.tracks for it in t.items]
        p.earliest_event = (min(starts) - max(pre, 0.0)) if starts else 0.0
        if pre <= 1e-9:
            return
        keep = not os.environ.get('CPR_NO_PROJOFFS') and (
            os.environ.get('CPR_ON_PROJOFFS') or not getattr(p, 'ara_docs', None))
        if keep:
            # (left out with Melodyne in the project, as rpp_read does)
            # REAPER has the same thing: a project offset (PROJOFFS), its
            # timeline starting before bar 1. So nothing moves and nothing
            # in the pre-roll is lost; rpp_write writes the offset
            self.warn('the project starts %.3f s before bar 1 (Project Setup > '
                      'Start); REAPER gets the same project offset, so bar 1 '
                      'sits where Cubase shows it and nothing moves' % pre)
            return
        cut = gone = 0
        for t in p.tracks:
            keep = []
            for it in t.items:
                it.pos -= pre
                if it.pos < 0:
                    lost = -it.pos
                    if it.length - lost <= 1e-6:
                        # wholly inside the pre-roll: nothing of it is after
                        # bar 1, where REAPER's timeline begins (Olympus
                        # Glory's background loop ended 5 s before bar 1
                        # and came out with a negative length)
                        gone += 1
                        continue
                    if it.kind == 'audio':
                        it.soffs += lost * (it.playrate or 1.0)
                    it.length -= lost
                    it.pos = 0.0
                    cut += 1
                keep.append(it)
            t.items = keep
            # a point in the pre-roll is replaced by the value at the new
            # zero, so a ramp that starts there keeps its level
            t.volenv = envelope.clip_start(t.volenv or [], pre)
            t.panenv = envelope.clip_start(t.panenv or [], pre)
            for fx in ([t.instrument] if getattr(t, 'instrument', None) else []) + list(t.fx):
                fx.envelopes = [(k, envelope.clip_start(pts, pre))
                                for k, pts in (fx.envelopes or [])]
        if p.master is not None:
            p.master.volenv = envelope.clip_start(p.master.volenv or [], pre)
        for m in p.markers:
            m.start = max(0.0, m.start - pre)
            if m.end is not None:
                m.end = max(0.0, m.end - pre)
        tempo = []
        for s, bpm in (p.tempo or []):
            s2 = s - pre
            if s2 <= 0:
                tempo = [(0.0, bpm)]
            else:
                tempo.append((s2, bpm))
        p.tempo = tempo or [(0.0, 120.0)]
        self.warn('the project starts %.3f s before bar 1 (Project Setup > Start); '
                  'everything was moved so that bar 1 is 0:00 in REAPER, as Cubase '
                  'shows it%s%s' % (pre, ', and %d event(s) that began in that pre-roll '
                  'were cut at 0' % cut if cut else '',
                  '; %d event(s) lying wholly in the pre-roll, before bar 1, '
                  'have no place on REAPER\'s timeline and were left out '
                  '(a round trip back to Cubase keeps them)' % gone if gone else ''))

    # PArrangeSetup holds the project's own settings. Its layout is fixed and
    # unnamed, but the sample rate is a big-endian f32 a constant 48 bytes
    # into the record, which is the only value there that reads as an audio
    # rate at all - checked against all three reference projects.
    SETUP_RATE_AT = 48
    SETUP_PANLAW_AT = 76
    RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000,
             88200, 96000, 176400, 192000, 384000)

    def arrange_setup(self):
        """(data start, data end) of PArrangeSetup, or None.

        Found from the class declaration rather than by walking the tree: a
        project has exactly one, and the reader does not otherwise visit it."""
        d = self.A.d
        name = b'PArrangeSetup\x00'
        i = d.find(struct.pack('>I', len(name)) + name)
        if i < 0:
            return None
        o = i + 4 + len(name) + 2          # past the name and its u16 version
        size = struct.unpack_from('>q', d, o)[0]
        o += 8
        if size < 0 or o + size > len(d):
            return None
        return (o, o + size)

    def read_arrange_setup(self, p):
        span = self.arrange_setup()
        if span is None:
            return
        ds, de = span
        o = ds + self.SETUP_RATE_AT
        if o + 4 > de:
            return
        # Project Setup > Start, a double at the head of the record. A
        # negative start is a pre-roll: the timeline (and every tick and
        # sample position in the file) begins that long before bar 1,
        # which Cubase shows at 0:00. This project started 4 s early and
        # everything arrived in REAPER 2 bars late.
        try:
            start = struct.unpack_from('>d', self.A.d, ds)[0]
            if -3600.0 < start < 3600.0:
                p.start = start
        except struct.error:
            pass
        rate = struct.unpack_from('>f', self.A.d, o)[0]
        if rate in self.RATES:
            p.samplerate = int(rate)
            p.src = dict(p.src or {})
            p.src['samplerate_off'] = self.A.base + o
        # Project Setup > Stereo Pan Law: a big-endian int further into
        # the same record (Equal Power 6, 0 dB 4 - measured by changing
        # the setting in Cubase and diffing the saves)
        pl = ds + self.SETUP_PANLAW_AT
        if pl + 4 <= de:
            code = struct.unpack_from('>i', self.A.d, pl)[0]
            if 0 <= code <= 8:
                p.panlaw_code = code
                p.src = dict(p.src or {})
                p.src['panlaw_off'] = self.A.base + pl

    def read_output_channel(self, p):
        """The output bus's fader and inserts, from the Devices chunk.

        Cubase's Stereo Out track record holds nothing but a name. The
        channel itself - fader, inserts, sends - is VST Mixer / Output
        Channels in the Devices chunk, which is why the master used to read
        back as a bare track at 0 dB whatever the project held, and why
        nothing this tool wrote there could be checked. The same entries as
        any track channel, read the same way."""
        if p.master is None:
            return False
        try:
            raw = open(self.path, 'rb').read()
            cs = arch.chunks(raw)
        except Exception:
            return False
        dev = None
        for i, (cid, off, size) in enumerate(cs):
            if cid != 'ROOT':
                continue
            a = arch.Arch(raw[off:off + size])
            names, o = [], 0
            while o < size:
                nm, o = a.string(o)
                names.append(nm)
            if names and names[0] == 'Devices' and i + 1 < len(cs):
                _c2, off2, size2 = cs[i + 1]
                dev = arch.Arch(raw[off2:off2 + size2], name='Devices',
                                base=off2)
                break
        if dev is None:
            return False
        try:
            ob = dev.read_obj(0)
            n, o2 = dev.u32(ob.ds)
            g, _end = Attrs(dev).entries(o2, n, ob.de)
            vm = g.get('VST Mixer')
            oc = vm.get('Output Channels') if hasattr(vm, 'get') else None
            if isinstance(oc, ListVal):
                oc = oc[0] if oc else None
        except Exception:
            return False
        if oc is None:
            return False
        m = p.master
        vol = oc.get('Volume')
        if isinstance(vol, Node) and isinstance(vol.get('AnchorValue'), float):
            m.vol = 10 ** (vol['AnchorValue'] / 20.0)
            if isinstance(vol.get('Value'), (int, float)):
                m.vol_pos = float(vol['Value']) / 32768.0
        ins = oc.get('InsertFolder')
        if isinstance(ins, Node) and ins.get('Slot') is not None:
            slots = ins.get('Slot')
            seq = slots if isinstance(slots, ListVal) else [slots]
            fx = []
            for sl in seq:
                if isinstance(sl, Node) and isinstance(sl.get('Plugin'), Node):
                    fx.append(self.read_plugin(sl['Plugin']))
            m.fx = fx
        m.origin['devices_channel'] = True
        return True

    def take_master(self, p):
        """Lift Cubase's output bus out of the track list onto p.master.

        Everything in a Cubase project sums into an output bus, so its fader,
        its effects and its automation act on the whole mix. REAPER's counter-
        part is the master track. Left as an ordinary track the bus would sum
        nothing at all - every other track already goes straight to the master
        - and its automation would sit there doing nothing while looking as
        though it had converted.

        It is found by routing rather than by name, which is localised: the
        project has exactly one bus that channels feed and that no track owns,
        and exactly one track that neither feeds a bus nor owns one. Anything
        less clear-cut is left alone rather than guessed at."""
        if getattr(self, 'keep_buses', False):
            return
        # a project this converter built keeps the donor's emptied template
        # record, folded away inside the I/O folder; it is nothing. It goes
        # through drop_tracks: sends were resolved to track indices already,
        # and filtering the list directly moved every later track up by one
        # while the sends kept pointing at the old positions - a send to the
        # 'Aux' channel read back as a send to the track after it.
        gone = [i for i, t in enumerate(p.tracks)
                if t.name == '(unused template)']
        if gone:
            self.drop_tracks(p, gone)
        owned = {t.bus_id for t in p.tracks if t.bus_id is not None}
        sinks = {t.out_bus_id for t in p.tracks if t.out_bus_id is not None}
        sinks -= owned
        cands = [i for i, t in enumerate(p.tracks)
                 if not t.is_folder and not t.items and t.kind == 'other'
                 and t.bus_id is None and t.out_bus_id is None
                 and not getattr(t, 'empty_folder', False)]
        if len(sinks) != 1 or len(cands) != 1:
            # Routing did not single it out - a project with FX and group
            # channels has several tracks that own or feed no bus. Cubase's
            # own I/O folder still gives it away: the first folder in the
            # project, ahead of every track that plays anything, holding
            # nothing but channel records (no events, no instrument).
            if self.take_master_by_folder(p):
                return
            if cands:
                self.warn('could not tell which track is the output bus; '
                          'left as an ordinary track, so its fader and '
                          'automation will not act on the mix')
            return
        i = cands[0]
        m = p.tracks[i]
        p.master = m
        gone = [i]
        # the folder it sat in exists only to hold the I/O channels
        j = i - 1
        while j >= 0 and not p.tracks[j].is_folder:
            j -= 1
        if j >= 0 and p.tracks[j].depth < m.depth:
            after = p.tracks[i + 1] if i + 1 < len(p.tracks) else None
            if after is None or after.depth <= p.tracks[j].depth:
                gone.append(j)
        # the I/O folder it was dragged out of, left empty ahead of
        # everything else, is Cubase's plumbing too
        if p.tracks and getattr(p.tracks[0], 'empty_folder', False) and 0 != i:
            gone.append(0)
        self.drop_tracks(p, gone)
        # the inserts are read later, from the Devices chunk
        # (read_output_channel), so they are not counted here
        self.warn('%r is the Cubase output bus: its level and %d automation '
                  'point(s) went to the REAPER master track'
                  % (m.name, len(m.volenv)))

    def drop_tracks(self, p, gone):
        """Take tracks out of the list and keep every send pointing at the
        track it pointed at; a send into a dropped track goes with it."""
        gone = set(gone)
        moved = {}
        out = []
        for i, t in enumerate(p.tracks):
            if i in gone:
                continue
            moved[i] = len(out)
            out.append(t)
        for t in out:
            t.sends = [s for s in t.sends
                       if s.dest is None or s.dest in moved]
            for s in t.sends:
                if s.dest is not None:
                    s.dest = moved[s.dest]
        p.tracks = out

    def tempo_curve(self):
        out = []
        for t, _s, spq in self.tempo_pts:
            if spq > 0:
                out.append((self.t2s(t), 60.0 / spq))
        res = []
        for pos, bpm in out:
            if res and abs(res[-1][1] - bpm) < 1e-6:
                continue
            res.append((pos, bpm))
        return res or [(0.0, 120.0)]

    def take_master_by_folder(self, p):
        """The output bus as the one channel in Cubase's I/O folder that
        shapes the mix; the folder and its other channels (input buses)
        are Cubase's plumbing and go. True when it was found."""
        first = next((i for i, t in enumerate(p.tracks) if t.is_folder), None)
        if first is None:
            return False
        if any(t.items or t.instrument is not None for t in p.tracks[:first]):
            return False
        kids = p.folder_children(first)
        if not kids:
            return False
        for k in kids:
            t = p.tracks[k]
            if t.is_folder or t.items or t.instrument is not None \
                    or t.kind != 'other' or t.bus_id is not None:
                return False
        shaped = [k for k in kids
                  if p.tracks[k].fx or p.tracks[k].volenv
                  or abs(p.tracks[k].vol - 1.0) > 1e-6
                  or abs(p.tracks[k].pan) > 1e-6]
        i = shaped[0] if len(shaped) == 1 else kids[-1]
        p.master = p.tracks[i]
        if len(kids) > 1 and len(shaped) != 1:
            self.warn('the I/O folder holds %d channels; %r was taken for '
                      'the output bus' % (len(kids), p.master.name))
        m = p.master
        self.drop_tracks(p, [first] + kids)
        # the inserts are read later, from the Devices chunk
        # (read_output_channel), so they are not counted here
        self.warn('%r is the Cubase output bus: its level and %d automation '
                  'point(s) went to the REAPER master track'
                  % (m.name, len(m.volenv)))
        return True

    def resolve_sends(self, p):
        bus = {}
        for i, t in enumerate(p.tracks):
            if t.bus_id is not None:
                bus[t.bus_id] = i
        for t in p.tracks:
            keep = []
            for s in t.sends:
                s.dest = bus.get(s.dest_bus)
                if s.dest is None:
                    self.warn('send from %r to unknown bus %s dropped'
                              % (t.name, s.dest_bus))
                else:
                    keep.append(s)
            t.sends = keep

    # ---------------------------------------------------------- tracks
    def new_track(self, name, depth, tr):
        """A Track that remembers where it came from in the file.

        Adding, removing or renaming a track means editing the object it was
        read out of and the child count of the list holding it, and nothing
        else can find those afterwards."""
        # file offsets throughout, so a caller has one convention to follow
        b = self.A.base
        def f(v):
            return None if v is None else b + v
        t = Track(name, depth)
        t.src = {'obj': tr.hdr, 'sf': tr.ds - 8,
                 'name_off': f(getattr(self, '_name_off', None)),
                 'own_count_off': f(getattr(self, '_count_off', None)),
                 'count_off': f(getattr(self, '_list_count_off', None))}
        return t

    def walk(self, tr, depth, parent):
        cls = tr.cls
        try:
            node = self.node_of(tr)
            name, _dom, cnt, o = self.node_header(node.ds, node.de, cls=node.cls)
            self._name_off = node.ds
            self._count_off = self.count_off
        except Exception as e:
            self.warn('%s: %s' % (cls, e))
            return
        if cls == 'MMarkerTrackEvent':
            self.read_markers(node, cnt, o)
            return
        if cls == 'MAutomationTrackEvent':
            return
        if cls == 'MVideoTrackEvent':
            t = self.new_track(name, depth, tr)
            t.kind = 'video'
            self.read_track_attrs(tr, t)
            self.proj.tracks.append(t)
            for _ in range(cnt):
                try:
                    ev = self.A.read_obj(o, node.de)
                except Exception as e:
                    self.warn('%r: %s' % (name, e))
                    break
                try:
                    self.read_video_event(t, ev)
                except Exception as e:
                    self.warn('%r/%s: %s' % (name, ev.cls, e))
                o = ev.de
            return

        if cls == 'MFolderTrack':
            kids = []
            for _ in range(cnt):
                try:
                    ob = self.A.read_obj(o, node.de)
                except Exception as e:
                    self.warn('folder %r: %s' % (name, e))
                    break
                kids.append(ob)
                o = ob.de
            t = self.new_track(name, depth, tr)
            t.is_folder = True
            t.kind = 'other'
            self.read_track_attrs(tr, t)
            self.proj.tracks.append(t)
            before = len(self.proj.tracks)
            outer = getattr(self, '_list_count_off', None)
            # keep this in payload offsets: new_track adds the chunk base
            own = t.src.get('own_count_off')
            self._list_count_off = None if own is None else own - self.A.base
            for k in kids:
                self.walk(k, depth + 1, t)
            self._list_count_off = outer
            if len(self.proj.tracks) == before:
                t.is_folder = False
                # an emptied folder - Cubase's I/O folder once its Stereo
                # Out has been dragged out of it - is not a channel
                t.empty_folder = True
            return

        # A Cubase group channel sits inside its folder under the same name.
        # A REAPER folder already sums its children, so the group's mixer
        # belongs on the folder; as a separate track its effects would get
        # no audio at all.
        if (cls == 'MDeviceTrackEvent' and parent is not None
                and parent.is_folder and parent.name == name
                and cnt == 0 and not parent.fx):
            self.read_mixer(tr, parent)
            self.read_automation(tr, parent)
            self.apply_read_switch(parent)
            return

        t = self.new_track(name, depth, tr)
        t.kind = ('audio' if cls == 'MAudioTrackEvent' else
                  'midi' if cls in ('MMidiTrackEvent', 'MInstrumentTrackEvent')
                  else 'other')
        self.read_track_attrs(tr, t)
        self.read_mixer(tr, t)
        self.read_automation(tr, t)
        if cls == 'MInstrumentTrackEvent':
            self.read_instrument(tr, t)
        self.apply_read_switch(t)
        self.proj.tracks.append(t)
        self.read_events(t, node, cnt, o, lane=t.active_lane)
        self.read_other_versions(t)

    def read_versions(self, tv):
        """MTrackVariationCollection -> the track's version names.

        Cubase gives every track one version, 'v1', unless the user makes
        more. REAPER's counterpart is a fixed item lane per version."""
        A = self.A
        o = tv.ds
        _a, o = A.i32(o)
        _b, o = A.u16(o)
        n, o = A.u32(o)
        names, nodes, active = [], [], 0
        self._variation_offs = []
        for k in range(n):
            ob = A.read_obj(o, tv.de)
            nm, q = A.string(ob.ds)
            names.append(nm)
            # a version is its name, then either its event list (an object)
            # or eight zero bytes, then its number - so the number of a
            # version without a list sits right after those eight bytes
            self._variation_offs.append({'ds': A.base + ob.ds,
                                         'name_off': A.base + ob.ds,
                                         'after_name': A.base + q,
                                         'end': A.base + ob.de})
            inner = None
            while q < ob.de:
                if A.looks_like_obj(q, ob.de):
                    o2 = A.read_obj(q, ob.de)
                    if o2.de > q and o2.cls == 'MListNode':
                        inner = o2
                        break
                    if o2.de > q:
                        q = o2.de
                        continue
                q += 1
            if inner is None:
                active = k          # the live version keeps its events on the track
            else:
                nodes.append((k, inner))
            o = ob.de
        return names, nodes, active

    def obj_or_ref(self, o, limit):
        """(object, where the next entry starts) for a list entry that is
        either an object or a reference to one written earlier: Cubase 12's
        Big Win 4 lists a MIDI track stored inside its chord track by
        reference. A reference is the object's size-field offset, positive;
        an object starts with a negative class tag."""
        A = self.A
        v = A.ptr(o)[0]
        if v >= 0:
            A.obj_refs.add(o) if A.W == 8 else A.obj_refs32.add(o)
            ob = A.objs.get(v)
            if ob is None:
                ob = A.deref(v)
            return ob, o + A.W
        ob = A.read_obj(o, limit)
        return ob, ob.de

    def read_events(self, t, node, cnt, o, lane=0):
        """Read `cnt` events out of a track node onto lane `lane`."""
        first = len(t.items)
        seen = set()
        for _ in range(cnt):
            try:
                ev, o2 = self.obj_or_ref(o, node.de)
            except Exception as e:
                self.warn('%r: %s' % (t.name, e))
                break
            if ev.ds in seen or ev.cls == '?':
                o = o2
                continue            # the same event named twice
            seen.add(ev.ds)
            try:
                if ev.cls == 'MAudioEvent':
                    self.read_audio_event(t, ev)
                elif ev.cls == 'MMidiPartEvent':
                    self.read_midi_part(t, ev)
            except Exception as e:
                self.warn('%r/%s: %s' % (t.name, ev.cls, e))
            o = o2
        for it in t.items[first:]:
            it.lane = lane

    def read_other_versions(self, t):
        """Cubase keeps each inactive track version's events in its own list
        inside the version collection; the active version's list is empty
        because those events sit on the track itself. Each version becomes a
        REAPER fixed lane."""
        for lane, ob in getattr(t, '_variation_nodes', []):
            try:
                _n, _d, cnt, o = self.node_header(ob.ds, ob.de, cls=ob.cls)
            except Exception as e:
                self.warn('%r version %d: %s' % (t.name, lane, e))
                continue
            self.read_events(t, ob, cnt, o, lane=lane)

    def read_track_attrs(self, tr, t):
        where = {}
        try:
            node = self.node_of(tr)
            at, _ = self.at.fourcc(node.de, tr.de, where)
        except Exception:
            return
        if where.get('count_off') is not None:
            b = self.A.base
            t.origin['attrs'] = {
                'count_off': b + where['count_off'],
                'count': where['count'],
                'end': b + where['end'],
                'keys': dict((k, (b + o, ty))
                             for k, (o, ty) in where.get('keys', {}).items()),
            }
        tv = at.get('TVCi')
        if tv is not None and hasattr(tv, 'cls'):
            try:
                t.versions, nodes, active = self.read_versions(tv)
                b = self.A.base
                # where the collection keeps its count, and where the first
                # version sits: how the builder gives a copied track more
                # versions than the donor track had
                t.origin['tvc'] = {'sf': b + tv.ds - 8, 'ds': b + tv.ds,
                                   'count_off': b + tv.ds + 6,
                                   'count': len(t.versions),
                                   'versions': list(self._variation_offs)}
            except Exception:
                t.versions, nodes, active = [], [], 0
            if len(t.versions) > 1:
                t.lane_names = list(t.versions)
                t.active_lane = active
                t._variation_nodes = nodes
        farb = at.get('Farb')
        if isinstance(farb, int) and farb > 0 and self.palette:
            t.color = self.palette[(farb - 1) % len(self.palette)]
        if isinstance(at.get('Solo'), int) and at['Solo']:
            t.solo = 1
        # 'tion' is Cubase's Disable Track: the channel is gone from the
        # mixer, its plug-ins unloaded, it plays nothing and Export Audio
        # Mixdown does not even list it (found by matching the attribute
        # against what Cubase offered to export). 'Thid' alone is a
        # hidden track, which still plays.
        if isinstance(at.get('tion'), int) and at['tion']:
            t.disabled = True

    def read_mixer(self, tr, t):
        dev = self.device_of(tr)
        if dev is None:
            return
        try:
            _dn, tree, _err = self.at.device_tree(dev)
        except Exception as e:
            self.warn('%r mixer: %s' % (t.name, e))
            return
        if tree is None:
            return
        b = self.A.base
        # The channel's mute. Cubase keeps it as a 'SoloFlag' entry (an
        # i64, bit 0) in the mixer channel's attribute group, and writes
        # the entry only while the channel is muted - found by muting a
        # group in Cubase, saving, and diffing the two files. Where the
        # group keeps its count and where Cubase puts the entry (right
        # after RuntimeID) are kept so the builder can add one.
        sf = tree.get('SoloFlag')
        if isinstance(sf, int) and (sf & 1):
            t.mute = 1
        if 'SoloFlag' in tree.offs:
            off, ty = tree.offs['SoloFlag']
            t.origin['solo_flag'] = (b + off, ty)
        try:
            o = dev.ds
            _, o = self.A.u16(o)
            _, o = self.A.string(o)
            _, o = self.A.i32(o)
            _, o = self.A.i32(o)
            if 'RuntimeID' in tree.offs and tree.offs['RuntimeID'][1] == 1:
                t.origin['chan_group'] = (b + o, b + tree.offs['RuntimeID'][0] + 8)
        except Exception:
            pass
        # A mono channel: its own bus arrangement is mono (Type [0]; a
        # stereo channel's is [1, 2]) and its panner is the mono panner
        # (PannerType 4; the Stereo Balance Panner is 2). That panner
        # follows a sine/cosine law (panlaw.cubase_mono_gains): full level
        # on the near side hard-panned, the pan law's gain on both sides
        # at the centre. InputBusArrangementType is 0 on stereo channels
        # too, so it says nothing here - Gradila's Guitar Fills, a stereo
        # channel with mono files, read as mono by it and came out 3 dB
        # off hard-panned. A stereo channel playing a mono file puts the
        # file on both sides at the law's gain before the inserts
        # (rpp_write, item gain) and then balance-pans.
        from . import chan_eq
        t.chan_eq = chan_eq.read(tree)
        # the EQ plug-in in the channel strip, which is what plays: its
        # bands override the mirror, and where its records sit is kept for
        # the builder
        sf = tree.get('StripFolder')
        if isinstance(sf, Node) and hasattr(sf, 'all'):
            from . import builtins
            for slot in sf.all('Slot'):
                if not isinstance(slot, Node) or slot.get('Plugin isA') != 'VstCtrlEQ':
                    continue
                pl = slot.get('Plugin')
                comp = pl.get('audioComponent') if isinstance(pl, Node) else None
                if not isinstance(comp, (bytes, bytearray)) or 'audioComponent' not in pl.offs:
                    continue
                recs = builtins._records(comp)
                if slot.get('State') == 1:
                    t.chan_eq = chan_eq.read_state(recs)
                else:
                    t.chan_eq = []
                start = b + pl.offs['audioComponent'][0] + 6
                t.origin['chan_eq_state'] = (start, {k: o for k, (o, _v) in recs.items()})
                if 'State' in slot.offs:
                    t.origin['chan_eq_slot'] = (b + slot.offs['State'][0], slot.offs['State'][1])
                break
        # where the EQ's fields sit, so a builder can set them
        eqn = tree.get('EQ')
        if eqn is not None and hasattr(eqn, 'offs'):
            where = {'bands': []}
            if 'Bypass' in eqn.offs:
                where['Bypass'] = (b + eqn.offs['Bypass'][0], eqn.offs['Bypass'][1])
            for band in (eqn.all('Band') if hasattr(eqn, 'all') else []):
                if hasattr(band, 'offs'):
                    where['bands'].append({k: (b + o, ty) for k, (o, ty) in band.offs.items()})
            t.origin['chan_eq'] = where
        if t.kind == 'audio':
            mono = None
            own = tree.get('OwnInputBus')
            if isinstance(own, Node):
                ia = own.get('Input Arrangement')
                ty = ia.get('Type') if isinstance(ia, Node) else None
                if ty is not None:
                    mono = (ty == 0 or ty == [0] or list(ty) == [0])
            pan_node = tree.get('Panner')
            if mono is None and isinstance(pan_node, Node):
                pt = pan_node.get('PannerType')
                if isinstance(pt, Node) and isinstance(pt.get('Value'), int):
                    mono = pt['Value'] == 4
            t.mono = bool(mono)
        # the channel's Input Gain (pre-fader): a position out of 32768 like
        # the fader's, 16383.5 at 0 dB. Kept with where it sits, so a level
        # past the fader's +6.02 dB can be put there; how a position maps to
        # dB is measured on a Cubase export (cubase_input_gain_db)
        ig = tree.get('InputGain')
        if isinstance(ig, Node) and isinstance(ig.get('Value'), (int, float)):
            t.input_gain_pos = float(ig['Value'])
            if 'Value' in ig.offs:
                off, ty = ig.offs['Value']
                t.origin['input_gain'] = (b + off, ty)
        vol = tree.get('Volume')
        if isinstance(vol, Node):
            self.note_fader(vol.get('Value'), vol.get('AnchorValue'))
            if isinstance(vol.get('AnchorValue'), float):
                t.vol = 10 ** (vol['AnchorValue'] / 20.0)
            # the fader's other half: the position Cubase actually plays by.
            # Kept so the two can be checked against each other - a file
            # whose dB is right and whose position is wrong plays at the
            # wrong level and reads back as if it were correct.
            if isinstance(vol.get('Value'), (int, float)):
                t.vol_pos = float(vol['Value']) / 32768.0
                # when the two disagree, Cubase plays the position: a file
                # an older converter wrote (Cherry Link Inflate (Cubase):
                # '-4.6 dB' at a position that is -9.6 dB) is read by it
                pg = self.norm_to_gain(t.vol_pos)
                if pg > 0 and t.vol > 0 and \
                        abs(20 * math.log10(pg) - 20 * math.log10(t.vol)) > 0.05:
                    self.warn('%r: the fader says %+.2f dB but sits at %+.2f dB - '
                              'Cubase plays the position, so that is what was read'
                              % (t.name, 20 * math.log10(t.vol), 20 * math.log10(pg)))
                    t.vol = pg
            # both halves of the fader are fixed-width, so a level can be
            # written back into the .cpr without the file changing shape
            for key, slot in (('AnchorValue', 'vol_db'), ('Value', 'vol_raw')):
                if key in vol.offs:
                    off, ty = vol.offs[key]
                    t.origin[slot] = (b + off, ty)
        pan = tree.get('Panner')
        if isinstance(pan, Node):
            ac = pan.get('audioComponent')
            if isinstance(ac, bytes) and len(ac) >= 4:
                v = struct.unpack_from('<f', ac, 0)[0]
                if 0.0 <= v <= 1.0:
                    t.pan = round((v - 0.5) * 2.0, 6)
                if 'audioComponent' in pan.offs:
                    # the value is u16 kind, u32 length, then the bytes, and
                    # the first of those is the position
                    off, ty = pan.offs['audioComponent']
                    t.origin['pan'] = (b + off + 6, 'f32le')
        # The Inspector's track Delay: an f64 in seconds right after the
        # channel's device object (after a u32), followed by a class
        # reference (high bit set). Found by setting 60, 0 and -25 ms on one
        # track in Cubase and diffing the saves (2026-09-29); Gradila's
        # Strings tracks sit at -60 ms and played 60 ms early.
        try:
            q = dev.de + 4
            if (q + 12 <= len(self.A.d)
                    and self.A.d[q + 8] & 0x80):
                dl = struct.unpack_from('>d', self.A.d, q)[0]
                if -10.0 < dl < 10.0:
                    t.delay = dl
                    t.origin['delay'] = (b + q, 'f64')
        except (struct.error, IndexError):
            pass
        own = tree.get('OwnInputBus')
        if isinstance(own, Node) and isinstance(own.get('Bus UID'), int):
            t.bus_id = own['Bus UID']
            # where the channel's own id and its destination sit, so a copy
            # can be given an id of its own and be played into
            if 'Bus UID' in own.offs:
                off, ty = own.offs['Bus UID']
                t.origin['bus_id'] = (b + off, ty)
        obv = tree.get('OutputBusValue')
        if isinstance(obv, Node) and isinstance(obv.get('Value'), int):
            t.out_bus_id = obv['Value']
            if 'Value' in obv.offs:
                off, ty = obv.offs['Value']
                t.origin['out_bus'] = (b + off, ty)
        ins = tree.get('InsertFolder')
        if isinstance(ins, Node):
            for slots in [ins.get('Slot')] if ins.get('Slot') is not None else []:
                seq = slots if isinstance(slots, ListVal) else [slots]
                spans = getattr(slots, 'spans', ())
                # the whole slot strip, so extra plug-ins can be written into
                # it: a populated slot is the pattern, an empty one the filler
                if spans:
                    t.origin['slots'] = [(b + a, b + z) for a, z in spans]
                for i, sl in enumerate(seq):
                    if isinstance(sl, Node) and isinstance(sl.get('Plugin'), Node):
                        fx = self.read_plugin(sl['Plugin'])
                        if i < len(spans):
                            fx.origin['slot'] = (b + spans[i][0], b + spans[i][1])
                        t.fx.append(fx)
        sf = tree.get('SendFolder')
        if isinstance(sf, Node):
            group = sf.get('Slot')
            spans = getattr(group, 'spans', ())
            if spans:
                # the whole send strip, so a send can be written by copying
                # a slot that has one over a slot that has none
                t.origin['send_spans'] = [(b + a, b + z) for a, z in spans]
            for slots in [group] if group is not None else []:
                for sl in (slots if isinstance(slots, ListVal) else [slots]):
                    if not isinstance(sl, Node):
                        continue
                    # where a send's switch, destination and level live, so
                    # one can be turned on and pointed somewhere. Every
                    # channel carries its slots whether they are used or not.
                    where = {}
                    if 'On' in sl.offs:
                        where['on'] = sl.offs['On']
                    ov = sl.get('Output')
                    if isinstance(ov, Node) and 'Value' in ov.offs:
                        where['out'] = ov.offs['Value']
                    vn = sl.get('Volume')
                    if isinstance(vn, Node):
                        for key, slot in (('AnchorValue', 'vol_db'),
                                          ('Value', 'vol_raw')):
                            if key in vn.offs:
                                off, ty = vn.offs[key]
                                where[slot] = (b + off, ty)
                    if 'on' in where:
                        off, ty = where['on']
                        where['on'] = (b + off, ty)
                    if 'out' in where:
                        off, ty = where['out']
                        where['out'] = (b + off, ty)
                    t.origin.setdefault('send_slots', []).append(where)
                    if not sl.get('On'):
                        continue
                    s = Send()
                    v = sl.get('Volume')
                    if isinstance(v, Node):
                        self.note_fader(v.get('Value'), v.get('AnchorValue'))
                        if isinstance(v.get('AnchorValue'), float):
                            s.vol = 10 ** (v['AnchorValue'] / 20.0)
                    out = sl.get('Output')
                    if isinstance(out, Node) and isinstance(out.get('Value'), int):
                        s.dest_bus = out['Value']
                    pn = sl.get('Panner')
                    if isinstance(pn, Node):
                        ac = pn.get('audioComponent')
                        if isinstance(ac, bytes) and len(ac) >= 4:
                            pv = struct.unpack_from('<f', ac, 0)[0]
                            if 0.0 <= pv <= 1.0:
                                s.pan = round((pv - 0.5) * 2.0, 6)
                    if s.dest_bus is not None:
                        t.sends.append(s)

    # u32 length (11) + 'Synth Slot' + NUL, as it appears in the device
    SYNTH_SLOT = struct.pack('>I', 11) + b'Synth Slot' + bytes(1)

    def read_instrument(self, tr, t):
        """An instrument track keeps its VSTi in a 'Synth Slot'.  That entry
        sits at the very end of the device tree, past members this reader does
        not model, so find it by name and parse only what follows."""
        dev = self.device_of(tr)
        if dev is None:
            return
        A = self.A
        i = A.d.find(self.SYNTH_SLOT, dev.ds, dev.de)
        if i < 0:
            return
        o = i + len(self.SYNTH_SLOT)
        try:
            typ, o = A.u16(o)
            if typ != 2:
                return
            kind, o = A.u16(o)
            n, o = A.u32(o)
            if kind != 6 or n > 4096:
                return
        except Exception:
            return
        for _ in range(n):
            try:
                name, o = A.string(o)
                tt, o = A.u16(o)
                v, o = self.at.value(tt, o, dev.de)
            except Exception:
                return
            if name == 'Plugin' and isinstance(v, Node):
                fx = self.read_plugin(v)
                fx.n_in, fx.n_out = 0, 2        # an instrument takes no audio in
                t.instrument = fx
                return

    def read_plugin(self, pl):
        fx = Fx()
        # A plug-in that was not loaded when the project was saved - its
        # instrument deactivated (Agata's FM8, Kontakt 7, CS-80, Hive), or
        # missing on that machine - keeps everything, state included, under
        # 'Unload Attributes' with Active 0. Read there it is the same
        # plug-in, switched off; read at the top it was a nameless '?'.
        un = pl.get('Unload Attributes')
        if isinstance(un, Node) and pl.get('Plugin UID') is None:
            pl = un
        uid = pl.get('Plugin UID')
        fx.uid = ((uid.get('GUID') if isinstance(uid, Node) else '') or '').upper()
        fx.name = pl.get('Plugin Name') or '?'
        comp = pl.get('audioComponent')
        ctrl = pl.get('editController')
        # remember where the two state blobs sit, so a different patch can be
        # written back over them
        for key in ('audioComponent', 'editController', 'Plugin Name',
                    'Bay Program', 'IDString', 'Original Plugin Name'):
            if key in getattr(pl, 'offs', {}):
                off, ty = pl.offs[key]
                fx.origin[key] = (self.A.base + off, ty)
        # the bus layout, so a slot can be reshaped for another plug-in: the
        # count is an i64, the arrangement a list of groups with known spans
        for key in ('Audio Input Count', 'Audio Output Count', 'Active'):
            if key in getattr(pl, 'offs', {}):
                off, ty = pl.offs[key]
                fx.origin[key] = (self.A.base + off, ty)
        for key in ('Audio Input Arrangement', 'Audio Output Arrangement'):
            v = pl.get(key)
            spans = getattr(v, 'spans', None)
            if spans and key in getattr(pl, 'offs', {}):
                off, ty = pl.offs[key]
                fx.origin[key] = {'count_off': self.A.base + off + 2,
                                  'spans': [(self.A.base + a, self.A.base + b)
                                            for a, b in spans]}
        # the plug-in's identity, so a slot can be pointed at a different one
        if isinstance(uid, Node) and 'GUID' in getattr(uid, 'offs', {}):
            off, ty = uid.offs['GUID']
            fx.origin['GUID'] = (self.A.base + off, ty)
        fx.component = comp if isinstance(comp, bytes) else b''
        fx.controller = ctrl if isinstance(ctrl, bytes) else b''
        fx.bypass = 0 if pl.get('Active', 1) else 1
        fx.preset = pl.get('Bay Program') or ''
        from .plugins import vst2_id_from_uid
        v2 = vst2_id_from_uid(fx.uid)
        if v2:
            fx.vst2_id = v2[1]
        return fx

    # ---------------------------------------------------------- automation
    # A lane's target sits in the MAutomationTrack that follows its event
    # list: either a plug-in parameter, named "Inserts\Slot\<uid>-<index>",
    # or a channel parameter identified by a small numeric id.
    CHANNEL_PARAM = {2: 'volume', PAN_PARAM: 'pan'}
    # "Inserts\Slot\<uid>-<index>"; a newer Cubase also numbers the slot,
    # "Inserts\Slot 3\<uid>-<index>" (seen 2026-09-28)
    # an instrument's own parameter lane is "Slot\<uid>-<n>" without the
    # "Inserts\" (drawn by hand on a Hive track, 2026-09-29)
    FX_PARAM_RE = re.compile(rb'(?:Inserts\\)?Slot(?: \d+)?\\([0-9A-Fa-f]{32})-([0-9]+)')

    def auto_target(self, ln, sub):
        self._param_off = None
        self._lane_track = None     # where the lane's MAutomationTrack starts
        blob = self.A.d[ln.de:sub.de]
        m = self.FX_PARAM_RE.search(blob)
        if m:
            return ('fx', m.group(1).decode('latin1').upper(), int(m.group(2)))
        pid = None
        o = ln.de
        while o < sub.de - 16:
            # a lane whose parameter record repeats an earlier lane's is
            # written as a plain reference to that record (Cubase's own pan
            # lane pointed at the one the converter had written); the
            # record it names is read instead
            tag = struct.unpack_from('>q', self.A.d, o)[0]
            if 0 < tag < len(self.A.d) and tag in self.A.objs \
                    and self.A.objs[tag].cls == 'MAutomationTrack':
                ob = self.A.objs[tag]
                self._lane_track = ob.ds
                pid = struct.unpack_from('>H', self.A.d, ob.ds + 4)[0]
                dev = None
                if ob.de - ob.ds >= 10:
                    n = struct.unpack_from('>I', self.A.d, ob.ds + 6)[0]
                    if 0 < n <= 64 and ob.ds + 10 + n <= ob.de:
                        dev = self.A.d[ob.ds + 10:ob.ds + 10 + n]
                        dev = dev.rstrip(b'\0').decode('latin1')
                return ('channel', pid, dev)
            if self.A.looks_like_obj(o, sub.de):
                try:
                    ob = self.A.read_obj(o, sub.de)
                except Exception:
                    break
                if ob.cls == 'MAutomationTrack' and ob.de - ob.ds >= 6:
                    self._lane_track = ob.ds
                    pid = struct.unpack_from('>H', self.A.d, ob.ds + 4)[0]
                    # the record: u16 open flag, u32 parameter id, then the
                    # name of the device the id counts within (u32 length
                    # with the NUL, empty for the channel's own volume,
                    # "Panner" for pan) - cpr_build.clone_channel_lane
                    # writes the whole of it for a new lane
                    self._param_off = self.A.base + ob.ds + 4
                    dev = None
                    if ob.de - ob.ds >= 10:
                        n = struct.unpack_from('>I', self.A.d, ob.ds + 6)[0]
                        if 0 < n <= 64 and ob.ds + 10 + n <= ob.de:
                            dev = self.A.d[ob.ds + 10:ob.ds + 10 + n]
                            dev = dev.rstrip(b'\0').decode('latin1')
                    return ('channel', pid, dev)
                o = ob.de if ob.de > o else o + 1
            else:
                o += 1
        return ('channel', pid, None)

    def apply_read_switch(self, t):
        """A track whose automation Read is off plays none of its lanes:
        Cubase keeps them, draws them, and plays the fader and the plug-in
        settings instead. The switch is the first u16 of each lane's
        MAutomationTrack (1 = Read on; cpr_build.set_automation_read).
        Gradila's Hive Pulse 8 had a volume lane peaking at -20 dB with
        Read off and played at its fader: converted as if the lane played,
        it came out 33 dB too quiet in REAPER. Such lanes move to the idle
        fields (Track.volenv_idle, .panenv_idle, Fx.envelopes_idle), which
        the writers keep as lanes that do not play (REAPER ACT 0, Cubase
        Read off) and nothing else takes for what plays."""
        # Read is the first u16 of each lane's MAutomationTrack (a Cubase save
        # with Read switched on and off, 2026-10-01); the last u16 of the
        # MAutomationNode only says whether the lanes are shown. A lane with
        # no points plays nothing either way, so the lanes that count are
        # the ones with points: the track plays its automation when one of
        # them has Read on.
        lanes = [ln for ln in (t.origin.get('auto_lanes') or []) if ln.get('points')]
        if not lanes:
            return
        word = any(ln.get('read', 1) for ln in lanes)
        t.auto_read = bool(word)
        if word:
            return
        fxs = ([t.instrument] if t.instrument is not None else []) + list(t.fx)
        if not (t.volenv or t.panenv or any(f.envelopes for f in fxs)):
            return
        t.volenv_idle, t.volenv = t.volenv, []
        t.panenv_idle, t.panenv = t.panenv, []
        for f in fxs:
            f.envelopes_idle, f.envelopes = list(f.envelopes or []), []
        self.warn('%r: automation Read is off in Cubase, so its lanes do not '
                  'play; they are kept as lanes that do not play' % t.name)

    def read_automation(self, tr, t):
        dev = self.device_of(tr)
        if dev is None:
            return
        A = self.A
        try:
            an = A.read_obj(dev.de + 12, tr.de)
        except Exception:
            return
        if an.cls != 'MAutomationNode':
            return
        try:
            _n, _d, cnt, o = self.node_header(an.ds, an.de, cls=an.cls)
        except Exception:
            return
        # the list the lanes hang off: a track gets a second lane by copying
        # the first into here and counting one more
        t.origin['auto_node'] = {'count_off': A.base + self.count_off,
                                 'count': cnt, 'first': A.base + o,
                                 'end': A.base + an.de}
        for _ in range(cnt):
            try:
                sub = A.read_obj(o, an.de)
                o = sub.de
                ln = A.read_obj(sub.ds + TRACK_EVENT_PREFIX, sub.de)
                _n2, _d2, pcnt, po = self.node_header(ln.ds, ln.de, cls=ln.cls)
            except Exception:
                return
            # where this lane's points live, so they can be rewritten: each
            # is an object holding an f64 time and an f32 value
            lane = {'count_off': A.base + self.count_off, 'points': [],
                    'at': A.base + sub.ds, 'end': A.base + sub.de}
            pts = []
            for _ in range(pcnt):
                try:
                    ev = A.read_obj(po, ln.de)
                except Exception:
                    break
                pts.append((struct.unpack_from('>d', A.d, ev.ds)[0],
                            struct.unpack_from('>f', A.d, ev.ds + 8)[0]))
                lane['points'].append(A.base + ev.ds)
                po = ev.de
            kind, a, b = self.auto_target(ln, sub)
            if self._lane_track is not None:
                # the lane's Read switch (apply_read_switch)
                lane['read'] = struct.unpack_from('>H', A.d, self._lane_track)[0]
            lane['kind'], lane['target'], lane['param'] = kind, a, b
            lane['param_off'] = self._param_off
            t.origin.setdefault('auto_lanes', []).append(lane)
            if not pts:
                continue
            if kind == 'fx':
                for fx in ([t.instrument] if t.instrument else []) + list(t.fx):
                    if (fx.uid or '').upper() == a:
                        # The number in the lane's name is only a per-plug-in
                        # lane counter (Cubase renumbers it on save). The
                        # parameter is named by the word that closes the
                        # lane record: 0x1069 + the plug-in's parameter
                        # index - which is REAPER's index too (drawn by hand
                        # in Cubase 15 on Pro-Q 4 and ValhallaDelay,
                        # 2026-09-29). Older projects without that word fall
                        # back to the number.
                        from . import vst3params
                        tag = struct.unpack_from('>H', A.d, sub.de - 8)[0]
                        idx = tag - vst3params.TAG_BASE
                        table = vst3params.table_for(a, fx.name)
                        if idx < 0 or (table is not None and idx >= len(table)):
                            idx = vst3params.cubase_to_reaper_index(
                                a, int(b), fx.name, None)
                        fx.envelopes.append((idx, [(self.t2s(p), v) for p, v in pts]))
                        break
                else:
                    self.warn('automation on %r targets a plug-in that is not '
                              'in the chain (%s parameter %d), skipped'
                              % (t.name, a, b))
                continue
            if a == 2 or a is None:
                # Cubase draws the lane straight in fader position; the
                # model keeps gain-linear ramps (envelope.py)
                t.volenv = envelope.volume_from_cubase(
                    [(self.t2s(p), self.norm_to_gain(v)) for p, v in pts])
            elif a == PAN_PARAM and b in (None, 'Panner'):
                # a lane keeps pan as 0..1; REAPER counts it -1..+1
                t.panenv = [(self.t2s(p), v * 2.0 - 1.0) for p, v in pts]
            else:
                self.warn('automation lane on %r for channel parameter %s%s is '
                          'not one this converter maps (%d points), skipped'
                          % (t.name, a, ' of %s' % b if b else '', len(pts)))

    # ---------------------------------------------------------- markers
    def read_markers(self, node, cnt, o):
        A = self.A
        b = A.base
        self.marker_list = {'count_off': b + self.count_off,
                            'first': b + o, 'end': b + node.de}
        for _ in range(cnt):
            ev = A.read_obj(o, node.de)
            p = ev.ds
            name_off = p
            name, p = A.string(p)
            # the number Cubase shows for it; point markers and cycle
            # markers each count from 1
            id_off = p
            mid, p = A.i32(p)
            _, p = A.u16(p)
            start_off = p
            start, p = A.f64(p)
            end = None
            len_off = None
            if ev.cls == 'MRangeMarkerEvent':
                len_off = p
                length, p = A.f64(p)
                end = self.t2s(start + length)
            m = Marker(name, self.t2s(start), end)
            m.rid = mid
            # where this marker sits, so one can be rewritten or copied
            m.src = {'cls': ev.cls, 'ds': b + ev.ds, 'de': b + ev.de,
                     'name_off': b + name_off, 'start_off': b + start_off,
                     'id_off': b + id_off,
                     'len_off': (b + len_off) if len_off is not None else None}
            self.proj.markers.append(m)
            o = ev.de

    # ---------------------------------------------------------- events
    def origin(self, ev, rate):
        """Where this event's fixed-width fields live in the .cpr itself.

        An event begins with u16 flags then three f64s - start, length and
        offset - at known positions, so those can be written straight back
        into the original file without moving a byte. Everything around them
        stays exactly as Cubase wrote it, which is what makes the result a
        project Cubase will open."""
        b = self.A.base
        return {'flags': b + ev.ds, 'start': b + ev.ds + 2,
                'length': b + ev.ds + 10, 'offset': b + ev.ds + 18,
                'rate': rate, 'clip_bpm': None}

    def ev_prefix(self, ev):
        A = self.A
        flags, o = A.u16(ev.ds)
        start, o = A.f64(o)
        length, o = A.f64(o)
        offset, o = A.f64(o)
        return flags, start, length, offset, o

    def read_audio_event(self, track, ev):
        A = self.A
        flags, start, length, offset, o = self.ev_prefix(ev)
        if A.looks_like_obj(o, ev.de):
            clip = A.read_obj(o, ev.de)
            o = clip.de
            info = self.read_clip(clip.ds, clip.de)
            self.clip_cache[clip.ds - A.W] = info
        else:
            ref, o = A.ptr(o)
            info = self.clip_cache.get(ref)
            if info is None:
                size, ds = A.ptr(ref)
                info = self.read_clip(ds, ds + size)
                self.clip_cache[ref] = info

        rate = info.get('rate') or 48000.0
        it = Item()
        it.kind = 'audio'
        it.pos = self.t2s(start)
        it.file = info.get('path')
        it.mute = 1 if (flags & MUTED) else 0
        # an ARA extension (Melodyne, SpectraLayers) edited this event: its
        # record carries the modification's id (the audio source's GUID in
        # Melodyne's document). Melodyne's document goes across to REAPER
        # (ara.py); other extensions' edits do not
        blob = bytes(self.A.d[ev.ds:ev.de])
        # The tag 'IMXA' starts the record on every such event; only the
        # first event spells out the class name AXtModificationId, later
        # ones refer to it (a project Cubase saved with two Melodyne events,
        # 2026-10-01). After the GUID comes the modification's number:
        # Melodyne's modification is GUID.<n>.
        if b'IMXA\x00' in blob or b'AXtModificationId' in blob:
            low = blob.lower()
            ext = ('Melodyne' if b'melodyne' in low or b'celemony' in low
                   else 'SpectraLayers' if b'spectralayers' in low
                   else 'an ARA extension')
            m = re.search(rb'IMXA\x00.{0,60}?\x00\x00\x00%'
                          rb'([0-9A-Fa-f-]{36})\x00(....)', blob, re.S)
            if ext == 'Melodyne' and m:
                it.ara_id = m.group(1).decode()
                it.ara_mod = '%s.%d' % (it.ara_id, struct.unpack('>I', m.group(2))[0])
                if not self.proj.ara_docs:
                    from . import ara
                    with open(self.path, 'rb') as f:
                        self.proj.ara_docs = ara.documents(f.read())
            if (ext != 'Melodyne' or not getattr(it, 'ara_id', None)
                    or not self.proj.ara_docs) and not it.mute:
                self.warn('%r: an event at %.2f s carries %s edits, which do not cross: '
                          'the converted project plays the file as recorded. Render the '
                          'event in place in Cubase first to keep them'
                          % (track.name, it.pos, ext))
        it.origin = self.origin(ev, rate)
        it.origin['clip_bpm'] = info.get('clip_bpm')
        it.origin['file_type'] = info.get('ftype')
        it.origin['file_name_off'] = info.get('name_off')
        it.origin['file_dir_off'] = info.get('dir_off')
        it.origin['clip_name_off'] = info.get('clip_name_off')
        it.origin['file_info_off'] = info.get('file_info_off')
        it.origin['paths'] = info.get('paths')
        it.origin['uid_offs'] = info.get('uid_offs')
        it.origin['frame_offs'] = info.get('frame_offs')
        it.origin['channels'] = info.get('channels')
        for k in ('warp_off', 'warp_end', 'warp_n', 'grid_off', 'tape_off', 'frames'):
            it.origin[k] = info.get(k)
        it.channels = info.get('channels')

        # A plain event measures its length and start offset in samples of the
        # file. A clip in Cubase's musical mode is stretched to follow the
        # tempo instead, and then both are in ticks - the same units as the
        # start - so the event keeps its musical length as the tempo moves.
        # Read as samples they come out absurdly short: a 14-second part of a
        # 53-second file turned into a third of a second.
        clip_bpm = info.get('clip_bpm')
        if info.get('warped') and clip_bpm:
            it.warped = True
            it.length = self.t2s(start + length) - it.pos
            src = length / PPQ * (60.0 / clip_bpm)       # seconds of the file
            it.soffs = offset / PPQ * (60.0 / clip_bpm)
            it.playrate = (src / it.length) if it.length > 0 else 1.0
            # elastique Pro - Tape (tapeStyleMode 1): the pitch follows the
            # speed, REAPER's Preserve Pitch off. Its place was found and
            # the value never read, so a tape stretch came back pitch-kept
            to = info.get('tape_off')
            if to is not None:
                try:
                    it.preserve_pitch = struct.unpack_from('>q', A.d, to - A.base)[0] == 0
                except struct.error:
                    pass
            sm = self.warp_markers(info.get('warp_pts'), rate, start, length,
                                   offset, it.pos)
            if sm:
                # warp tabs, or a tempo change under the event: REAPER's
                # stretch markers, one per tab (and per tempo change), at
                # rate 1 - the source positions count from the file's start
                # and override SOFFS (measured: smprobe, 2026-10-01)
                it.stretch_markers = sm
                it.playrate = 1.0
                it.soffs = sm[0][1]
        else:
            it.length = length / rate
            it.soffs = offset / rate

        procs = info.get('processes')
        if procs:
            self.apply_processes(it, procs, info, rate, track)

        # After the clip: a u32 count of extra records - each a 4-byte tag,
        # a u16 version and an f64; 'FtiP' is the event's pitch shift as a
        # frequency ratio (the Info Line's Transpose and Fine-tune: Gradila's
        # Kaval Perc events carry 1.02046 = +35 cents, and rendered at the
        # right level but uncorrelated against an unshifted REAPER item) -
        # then a u32 serial that says which of two overlapping events is in
        # front (the higher one; model.flatten_lanes), then the fade slots.
        # A record's u16 says what follows: 4 an f64; 0x82 a reference; 0x22 an object - Cubase
        # 14's events carry their 'mediaId' and 'UniqueId' attributes this
        # way (Sunset Treasures: FtiP, then two objects). Read as three
        # f64s they threw the reader off and every one of the project's 587
        # events came out named "mediaId" with a gain from stray bytes.
        n_extra, q = A.u32(o)
        if 0 <= n_extra <= 8 and q + 14 * n_extra + 4 <= ev.de:
            it.origin['ev_attrs'] = (A.base + o, n_extra)
            ratio = None
            ratio_off = None
            stretch = None
            ok = True
            for _ in range(n_extra):
                tag = A.d[q:q + 4]
                q += 4
                kind, q = A.u16(q)
                if kind == 4:
                    val, q = A.f64(q)
                    if tag == b'FrtS' and 0.01 <= val <= 100.0:
                        # 'StrF' backwards: the event's time stretch (the
                        # Sizing Applies Time Stretch tool), the factor its
                        # file is played longer by, pitch kept - a 1.5 s
                        # whoosh sized to 1.82 s carries 1.2128. Ignored, the
                        # event played its file at its own speed and fell
                        # silent early (SuperThunderCrown, 14 events).
                        stretch = val
                    elif tag == b'FtiP' and 0.25 <= val <= 4.0:
                        ratio, ratio_off = val, q - 8
                elif kind == 1:
                    val, q = A.i64(q)
                    if tag == b'braF' and self.palette:
                        # 'Farb' backwards: the event's own colour, the
                        # palette's index (Colorize Selected Events, the
                        # tenth swatch saved as 9); no 'Farb', the track's
                        it.color = self.palette[int(val) % len(self.palette)]
                        it.origin['farb_off'] = A.base + q - 8
                elif kind == 0x82:
                    q += 8      # an i64 reference to an object written before
                                # ('vPFC' on events copied from another one)
                elif A.looks_like_obj(q, ev.de):
                    q = A.read_obj(q, ev.de).de
                else:
                    ok = False
                    break
            if not ok:
                self.warn('%s: an event record of an unknown kind; its '
                          'pitch and order may be missing' % track.name)
            serial, q = A.u32(q)
            it.zorder = serial
            it.origin['zorder_off'] = A.base + q - 4
            if ratio is not None and abs(ratio - 1.0) > 1e-9:
                it.pitch = 12.0 * math.log2(ratio)
                it.origin['pitch_off'] = A.base + ratio_off
            if stretch is not None and abs(stretch - 1.0) > 1e-9 \
                    and not getattr(it, 'warped', False):
                it.playrate = (it.playrate or 1.0) / stretch
                # the event's start offset is counted in the stretched time
                # too: Christmas Transition (0.644, offset 0.63 s) came out
                # 0.22 s early until it was taken back to the file's seconds
                it.soffs = (it.soffs or 0.0) / stretch
                it.preserve_pitch = True
                it.origin['stretch'] = stretch
            o = q - 8          # the fade slots follow at o + 8 (read_fades)

        self._fade_points = {}
        fin, fout, o2 = self.read_fades(o, ev.de, rate)
        if getattr(it, 'warped', False):
            # a musical-mode event counts its fades in ticks (measured on
            # the Music 2 export, 2026-09-30): read_fades divided ticks by
            # the rate; back to ticks, then seconds under the tempo map
            t0, t1 = start, start + length        # the event's own ticks
            end = it.pos + it.length
            fin = self.t2s(t0 + fin * rate) - it.pos if fin else fin
            fout = end - self.t2s(t1 - fout * rate) if fout else fout
        it.fadein, it.fadeout = fin, fout
        # the curve Cubase draws, as (x 0..1, gain) points, for the REAPER
        # writer to fit a shape to (rpp_write.fade_line)
        it.fade_points = dict(self._fade_points)
        # After the clip come an i64 and then two 8-byte slots, one for a
        # fade-in object and one for a fade-out: eight zero bytes when the
        # event has no such fade, the object itself when it has. An event
        # without fades is the one a fade can be written into, by filling
        # its slot.
        if A.d[o + 8:o + 24] == bytes(16):
            it.origin['fade_slots'] = (A.base + o + 8, A.base + o + 16)
        name, gain, name_off = self.read_event_tail(o2, ev.de)
        it.name = name or info.get('name') or track.name
        it.gain = gain
        self.read_event_curve(ev, it, rate)
        if info.get('vari'):
            it.pitchenv = variaudio_env(info['vari'], it.soffs,
                                        it.playrate or 1.0, it.length)
            if it.pitchenv:
                self._vari_items = getattr(self, '_vari_items', 0) + 1
        if info.get('vari_odd') and not info.get('vari_odd_told'):
            info['vari_odd_told'] = True
            self.warn('%s: %d VariAudio note(s) carry an edit besides pitch '
                      '(timing, straighten or formant) that is not carried'
                      % (info.get('name') or track.name, info['vari_odd']))
        if name_off is not None:
            it.origin['event_name_off'] = A.base + name_off
            # the event's gain is an f32 four bytes past the end of its name
            n_str, _ = A.u32(name_off)
            g_off = name_off + 4 + n_str + 4
            if g_off + 4 <= ev.de:
                it.origin['gain_off'] = A.base + g_off
        track.items.append(it)

    def read_event_curve(self, ev, it, rate):
        """The volume curve drawn along the top of an audio event
        (VolumeCurveDataNode): u32, u32, f64 seconds per x unit (1/48000 -
        x counts samples of the file), u32 point count, then one
        VolumeCurveEvent each - f64 x, f64 y (linear gain), u16. Found by
        drawing two points with the Draw tool on a test event and reading
        the save (2026-09-29). Cubase plays a straight line between points
        in its own display scale (envelope.cubase_event_curve), which is
        how Gradila's Serum Rhodes rose to +24 dB where the converted item
        played at 0 dB. Becomes Item.volenv: (seconds into the item, gain),
        dense enough for REAPER's straight lines."""
        A = self.A
        node = None
        for ob in self.descendants(ev.ds, ev.de, ('VolumeCurveDataNode',), 2):
            node = ob
            break
        if node is None or node.de - node.ds < 20:
            return
        try:
            unit = struct.unpack_from('>d', A.d, node.ds + 8)[0]
            cnt = struct.unpack_from('>I', A.d, node.ds + 16)[0]
        except struct.error:
            return
        if not cnt or not (0 < unit < 1.0):
            return
        pts = []
        recs = []
        o = node.ds + 20
        for _ in range(cnt):
            try:
                p = A.read_obj(o, node.de)
            except Exception:
                break
            x, y = struct.unpack_from('>dd', A.d, p.ds)
            pts.append((x * unit, max(0.0, y)))
            recs.append((A.base + p.hdr, A.base + p.ds))
            o = p.de
        # where the curve's records are, for writing one (cpr_build.
        # write_curve): the node, its count, and each point's header and data
        it.origin['curve_node'] = A.base + node.ds
        it.origin['curve_end'] = A.base + node.de
        it.origin['curve_unit'] = unit
        it.origin['curve_pts'] = recs
        if not pts or all(abs(y - 1.0) < 1e-9 for _, y in pts):
            return
        rate_play = it.playrate or 1.0
        # x is a time in the file; the item starts soffs into it
        ipts = [((t - it.soffs) / rate_play, y) for t, y in pts]
        it.volenv = envelope.cubase_event_curve(ipts, 0.0, max(it.length, 1e-6))
        it.origin['event_curve'] = pts
        self._event_curves = getattr(self, '_event_curves', 0) + 1

    def read_fades(self, o, end, rate):
        """Fade objects follow the clip after a small fixed gap; each holds an
        interpolator whose last point's x is the fade length in samples."""
        A = self.A
        fin = fout = 0.0
        for skip in (8, 16):
            q = o + skip
            if q >= end or not A.looks_like_obj(q, end):
                continue
            while q < end and A.looks_like_obj(q, end):
                ob = A.read_obj(q, end)
                if ob.de <= q:
                    break
                if ob.cls in ('MFadeIn', 'MFadeOut'):
                    ln = self.fade_length(ob) / rate
                    if ob.cls == 'MFadeIn':
                        fin = ln
                    else:
                        fout = ln
                    pts = self.fade_points(ob)
                    if pts and hasattr(self, '_fade_points'):
                        self._fade_points['in' if ob.cls == 'MFadeIn' else 'out'] = pts
                q = ob.de
            return fin, fout, q
        return fin, fout, o

    def fade_length(self, ob):
        A = self.A
        i = A.d.find(b'\x00\x00\x00', ob.ds, ob.de)
        # the interpolator is the object nested inside the fade
        q = ob.ds
        while q < ob.de and not A.looks_like_obj(q, ob.de):
            q += 1
        if q >= ob.de:
            return 0.0
        inner = A.read_obj(q, ob.de)
        o = inner.ds
        n, o = A.u32(o)
        if not 1 <= n <= 64:
            return 0.0
        last = 0.0
        for _ in range(n):
            x, o = A.f64(o)
            _y, o = A.f64(o)
            last = max(last, x)
        return last

    def fade_points(self, ob):
        """The fade's interpolator points as (x 0..1, gain), or []."""
        A = self.A
        q = ob.ds
        while q < ob.de and not A.looks_like_obj(q, ob.de):
            q += 1
        if q >= ob.de:
            return []
        inner = A.read_obj(q, ob.de)
        o = inner.ds
        n, o = A.u32(o)
        if not 1 <= n <= 256:
            return []
        pts = []
        for _ in range(n):
            x, o = A.f64(o)
            y, o = A.f64(o)
            pts.append((x, y))
        length = max(x for x, _y in pts) or 1.0
        return [(x / length, y) for x, y in pts]

    def read_event_tail(self, o, end):
        """The tail carries the event's display name followed by its gain.

        Returns the name, the gain, and where the name sits, so a copy of the
        event can be renamed."""
        A = self.A
        best = None
        best_off = None
        gain = 1.0
        p = o
        while p < end - 4:
            n, r = A.u32(p)
            # a block of tagged attributes (a u32 count, then 4-letter tags
            # each with a u16 kind): the ARA extension's record on an event
            # (IMXA, IUEX, IUSX, OPXA, UMXA, VtXA, aNSX, dISX) sits ahead of
            # the event's name and holds strings of its own - read whole
            if 1 <= n <= 64 and r + 6 <= end                     and all(65 <= c <= 122 for c in A.d[r:r + 4]):
                try:
                    _blk, q = self.at.fourcc(p, end)
                    if q > r:
                        p = q
                        continue
                except Exception:
                    pass
            if 2 <= n <= 300 and r + n <= end:
                raw = A.d[r:r + n]
                body = raw.split(b'\x00', 1)[0]
                ok = body and all(32 <= ch < 127 or ch > 160 for ch in body)
                # exactly the text and its terminator: an interned-string
                # reference (00 00 02 62) followed by a count (00 00 03 6f)
                # reads as a 3-byte string "o" otherwise, and the "gain" four
                # bytes after it is whatever happens to be there
                exact = raw == body + b'\x00' or raw == body + b'\x00\xef\xbb\xbf'
                # a string that is the whole payload of a small object (its
                # size field just before it is the string's length + 4) is a
                # value inside an attribute block, not the event's name: an
                # event an ARA extension touched carries 'SpectraLayers' and
                # 'com.Steinberg.SpectraLayers.arafactory' ahead of it, and
                # Agata's Cello events were named after SpectraLayers with a
                # gain of 0 read from the bytes that followed
                if ok and exact and p >= A.W:
                    before = A.ptr(p - A.W)[0]
                    if before == n + 4:
                        p = r + n
                        continue
                if ok and exact:
                    try:
                        s = body.decode('utf-8')
                    except Exception:
                        s = None
                    if s and s not in self.class_names():
                        best = s
                        best_off = p
                        if r + n + 8 <= end:
                            g = struct.unpack_from('>f', A.d, r + n + 4)[0]
                            if 0.0 < g <= 64.0:
                                gain = g
                        # the tail is: name, four zero bytes, gain, then
                        # references and counts. Scanning on found a
                        # "string" in those (00 00 00 02 62 00 reads as
                        # "b") and took a count for the gain: 53 events of
                        # one project came out silent.
                        break
                    p = r + n
                    continue
            p += 1
        return best, gain, best_off

    def read_video_event(self, track, ev):
        """A video track's domain is linear time, so start, length and offset
        are all plain seconds."""
        A = self.A
        flags, start, length, offset, o = self.ev_prefix(ev)
        if A.looks_like_obj(o, ev.de):
            clip = A.read_obj(o, ev.de)
            o = clip.de
            info = self.read_clip(clip.ds, clip.de)
            self.clip_cache[clip.ds - A.W] = info
        else:
            ref, o = A.ptr(o)
            info = self.clip_cache.get(ref)
            if info is None:
                size, ds = A.ptr(ref)
                info = self.read_clip(ds, ds + size)
                self.clip_cache[ref] = info
        it = Item()
        it.kind = 'video'
        it.pos = start
        it.length = length
        it.soffs = offset
        it.name = info.get('name') or track.name
        it.file = info.get('path')
        it.mute = 1 if (flags & MUTED) else 0
        it.origin = self.origin(ev, 1.0)
        it.origin['clip_name_off'] = info.get('clip_name_off')
        it.origin['paths'] = info.get('paths')
        it.origin['file_name_off'] = info.get('name_off')
        it.origin['file_dir_off'] = info.get('dir_off')
        track.items.append(it)
        # a video named on another drive is looked for by name later
        # (media.relink), which reports it only when it is nowhere to be
        # found - warning here said "not found" for Agata's video, which
        # sat in the project folder and was collected

    ID_RE = re.compile(rb'^[0-9A-F]{32}$')

    def read_clip(self, ds, de):
        A = self.A
        info = {}
        # The audio file's own id. Every event copied from this clip would
        # otherwise claim to be the same recording as the original, whatever
        # file it names, and Cubase builds one waveform for the lot.
        o = ds
        while o < de - 36:
            n = struct.unpack_from('>I', A.d, o)[0]
            if n in (33, 36) and o + 4 + n <= de:
                body = A.d[o + 4:o + 36]
                if self.ID_RE.match(body):
                    info.setdefault('uid_offs', []).append(A.base + o)
                    o += 4 + n
                    continue
            o += 1
        try:
            info['name'], _ = A.string(ds)
            info['clip_name_off'] = A.base + ds
        except Exception:
            pass
        want = ('FNPath', 'AudioFile', 'VideoFile', 'PGridDefinition',
                'PAudioWarpScale')
        for ob, shared in self.clip_objects(ds, de, want):
            if ob.cls == 'PAudioWarpScale':
                # present only on a clip Cubase is stretching to the tempo:
                # u32 n, then n (file sample, clip tick) f64 pairs
                info['warped'] = True
                try:
                    n_w = struct.unpack_from('>I', A.d, ob.ds)[0]
                    if 0 < n_w <= 100000 and ob.ds + 4 + 16 * n_w <= ob.de:
                        info['warp_pts'] = [struct.unpack_from('>dd', A.d, ob.ds + 4 + 16 * k)
                                            for k in range(n_w)]
                except struct.error:
                    pass
                if not shared:
                    info['warp_off'] = A.base + ob.ds
                    info['warp_end'] = A.base + ob.de
                    try:
                        info['warp_n'] = struct.unpack_from('>I', A.d, ob.ds)[0]
                    except Exception:
                        pass
            elif ob.cls == 'PGridDefinition' and 'clip_bpm' not in info:
                # u32 num, u32 den, u32 bars, f32 tempo the clip was cut at
                # (twice: +12 and +16)
                try:
                    bpm, _ = A.f32(ob.ds + 12)
                    if 20.0 <= bpm <= 400.0:
                        info['clip_bpm'] = bpm
                    if not shared:
                        info['grid_off'] = A.base + ob.ds
                except Exception:
                    pass
            elif ob.cls == 'FNPath':
                try:
                    o = ob.ds
                    name_off = o
                    name, o = A.string(o)
                    # the file's type: WAVE, AIFF, AIFC - a clip names the
                    # format it reads the file as
                    info.setdefault('ftype', bytes(A.d[o:o + 4]).decode('latin-1'))
                    o += 4
                    for _ in range(3):
                        _s, o = A.string(o)
                    _, o = A.i32(o)
                    _, o = A.u16(o)
                    dir_off = o
                    d, o = A.string(o)
                    # A clip names its file more than once - the path it
                    # plays and the path it came from - and all of them have
                    # to agree, or Cubase looks for a file that is not there.
                    # A record another clip owns (a copied clip refers back
                    # to the original's) is read but never listed for
                    # rewriting: changing it would change the other clip too.
                    if not shared:
                        info.setdefault('paths', []).append(
                            (A.base + name_off, A.base + dir_off))
                    if 'path' not in info:
                        info['path'] = os.path.join(d, name) if d else name
                        if not shared:
                            info['name_off'] = A.base + name_off
                            info['dir_off'] = A.base + dir_off
                except Exception:
                    pass
            elif ob.cls == 'AudioFile' and 'rate' not in info:
                try:
                    o = ob.ds
                    at = o
                    frames, o = A.i64(o)
                    bits, o = A.u16(o)
                    _, o = A.u16(o)
                    ch, o = A.u16(o)
                    rate, o = A.f32(o)
                    if 8000 <= rate <= 384000:
                        info.update(frames=frames, bits=bits, channels=ch,
                                    rate=rate)
                        # what the file is: length, width and rate. A clip
                        # pointed at a different file has to say so, or
                        # Cubase reads the wrong number of samples and the
                        # event shows an image construction error
                        if not shared:
                            info['file_info_off'] = A.base + at
                except Exception:
                    pass
            # no early exit: the warp marker and the grid sit past the file
            # objects in the clip, and missing them costs the event its length
        procs = self.read_processes(ds, de)
        if procs is not None:
            info['processes'] = procs
            # the copy Cubase rendered of the processed clip, which is what
            # it plays: Edits/<file>-<process>-<32 hex>.wav
            # A length-prefixed string; found by its tail, then read from the
            # u32 length that ends where it starts (a pattern for the whole
            # name lost its first letter to the length byte before it)
            blob = bytes(self.A.d[ds:de])
            for m in re.finditer(rb'-[A-Za-z ]{2,40}-[0-9A-F]{32}\.wav\x00', blob):
                end = m.end()                   # past the NUL
                # nearest first: further back, zero bytes before the length
                # also add up and gave the name a run of NULs in front
                for k in range(m.start() - 4, max(-1, m.start() - 264), -1):
                    # the length counts the NUL and, after it, a UTF-8 BOM
                    if struct.unpack_from('>I', blob, k)[0] in (end - (k + 4), end + 3 - (k + 4)):
                        name = blob[k + 4:end - 1]
                        if b'\x00' not in name:
                            info['edits_file'] = name.decode('utf-8', 'replace')
                        break
        segs, odd = self.read_variaudio(ds, de)
        if segs:
            info['vari'] = segs
        if odd:
            info['vari_odd'] = odd
        # The clip states how long the file is more than once - the file
        # record, and the stream it is cut from - and a clip pointed at
        # another file has to say the new length everywhere, or Cubase reads
        # past the end and cannot draw the waveform.
        # A clip on an elastique algorithm keeps its ElastiquePreset
        # attributes inline: name, NUL, u16 1, then the value as 8 bytes.
        # tapeStyleMode 1 is 'elastique Pro - Tape': the pitch follows the
        # speed, as a REAPER item does with Preserve Pitch off
        if info.get('warped'):
            blob = bytes(A.d[ds:de])
            k = blob.find(b'tapeStyleMode\x00')
            if k >= 0:
                info['tape_off'] = A.base + ds + k + len(b'tapeStyleMode\x00') + 2
        frames = info.get('frames')
        if frames:
            offs = []
            o = ds
            while o < de - 8:
                if struct.unpack_from('>q', A.d, o)[0] == frames:
                    offs.append(A.base + o)
                    o += 8
                else:
                    o += 1
            info['frame_offs'] = offs
        return info

    def read_variaudio(self, ds, de):
        """VariAudio's notes on a clip: LiquidAudio::NoteSegmentEvent
        objects inside the PAudioClip, one per detected note, back to back.
        Each is a u16, f64 start and f64 length in samples of the file, 16
        bytes, a u32 count of tagged records (4-byte tag, u16 version, f64:
        'qFQU' a frequency, 'tSQI' the Pitch Quantize amount), then f64 the
        pitch the note plays at and f64 the pitch it was sung at (Hz), then
        16 bytes. An untouched note has the two equal; Gradila's vocal,
        quantised to the semitone, has 226.71 Hz played at 233.08 (A#3).
        Returns ([(start s, end s, semitones)] for the moved notes, count of
        notes with bytes set where every analysed project has zeros - an
        edit this reader does not know, reported rather than guessed)."""
        A = self.A
        rate = None
        for ob in self.descendants(ds, de, ('AudioFile',), 5):
            try:
                r, _ = A.f32(ob.ds + 14)
                if 8000 <= r <= 384000:
                    rate = r
            except Exception:
                pass
            break
        segs, odd = [], 0
        for ob in self.descendants(ds, de, ('LiquidAudio::NoteSegmentEvent',), 3):
            body = A.d[ob.ds:ob.de]
            try:
                st, ln = struct.unpack_from('>dd', body, 2)
                n = struct.unpack_from('>I', body, 34)[0]
                if not 1 <= n <= 16:
                    continue
                q, recs = 38, {}
                for _ in range(n):
                    recs[body[q:q + 4]] = struct.unpack_from('>d', body, q + 6)[0]
                    q += 14
                played, sung = struct.unpack_from('>dd', body, q)
                tail = body[q + 16:]
            except struct.error:
                continue
            # 'hSoF' at 1.0 is on untouched notes too; anything else in it,
            # the 20 bytes after the length or the tail is an edit not read
            if body[18:34].strip(b'\0') or tail.strip(b'\0') \
                    or abs(recs.get(b'hSoF', 1.0) - 1.0) > 1e-9:
                odd += 1
            if not rate or not (20.0 <= played <= 20000.0) \
                    or not (20.0 <= sung <= 20000.0):
                continue
            semis = 12.0 * math.log2(played / sung)
            if abs(semis) > 1e-4:
                segs.append((st / rate, (st + ln) / rate, semis))
        return segs, odd

    def clip_objects(self, ds, de, want):
        """The file records of a clip: those written inside it (shared=False)
        and, when it was copied from another clip, the ones it refers back
        to (shared=True). Cubase writes a copied clip's FNPath and AudioFile
        as 8-byte offsets of the original's records, which sit before the
        copy in the stream and have been parsed already, so a value that
        keys an object of the wanted class is such a reference. The
        referenced records are yielded after the inline ones, so an inline
        record (the clip's own file) always wins."""
        A = self.A
        found = set()
        classes = set()
        for ob in self.descendants(ds, de, want, 5):
            found.add(ob.ds)
            classes.add(ob.cls)
            yield ob, False
        if 'FNPath' in classes:
            return      # the clip names its own file: nothing to look up
        refs = []
        o = ds
        while o < de - 8:
            v = struct.unpack_from('>q', A.d, o)[0]
            if 0 < v < de:
                ob = A.objs.get(v)
                if ob is not None and ob.cls in want and ob.ds not in found:
                    found.add(ob.ds)
                    refs.append(ob)
                    o += 8
                    continue
            o += 1
        for ob in refs:
            yield ob, True

    def descendants(self, ds, de, want, maxdepth, depth=0):
        A = self.A
        o = ds
        while o < de:
            if A.looks_like_obj(o, de):
                ob = A.read_obj(o, de)
                if ob.cls in want:
                    yield ob
                if depth < maxdepth:
                    for x in self.descendants(ob.ds, ob.de, want, maxdepth, depth + 1):
                        yield x
                o = ob.de if ob.de > o else o + 1
            else:
                o += 1

    def read_midi_part(self, track, ev):
        A = self.A
        flags, start, length, offset, o = self.ev_prefix(ev)
        ev_origin = self.origin(ev, PPQ)
        part = A.read_obj(o, ev.de)
        ev_origin['part_name_off'] = A.base + part.ds
        name, _dom, cnt, p = self.node_header(part.ds, part.de, cls=part.cls)
        nreg = A.d[p]
        p += 1
        for _ in range(nreg):
            ob = A.read_obj(p, part.de)
            p = ob.de
        notes, ccs = [], []
        # where the event list starts and how long each record is: enough to
        # rewrite a part by copying one of its own notes and editing it
        # 'records' are the note records, 'cc_records' the controller
        # ones: a builder writing either needs one of that kind to copy,
        # and the two are not the same shape - only a note carries a length
        ev_area = {'count_off': A.base + self.count_off, 'first': A.base + p,
                   'end': A.base + part.de, 'records': [], 'cc_records': []}
        for i in range(cnt):
            rec_start = p
            kind = A.d[p]
            p += 1
            pos, p = A.f64(p)
            ch, d1, d2 = A.d[p], A.d[p + 1], A.d[p + 2]
            p += 3
            _, p = A.u16(p)
            try:
                _at, p = self.at.fourcc(p, part.de)
            except Exception as e:
                self.warn('%r: MIDI part %r stopped after %d of %d events (%s)'
                          % (track.name, name, i, cnt, e))
                break
            if kind == 0x90:
                # only a note carries a length and a note-off velocity:
                # the length, then a 17-byte tail whose last byte is the
                # off velocity (the 4CC block's OffV double mirrors it)
                len_off = p
                nlen, p = A.f64(p)
                offv = A.d[p + 16] if p + 17 <= part.de else 64
                p += 17
                if d2 == 0 and isinstance(_at.get('HRDT'), float) and _at['HRDT'] > 0:
                    # velocity byte 0 with a fine velocity above 0 plays as
                    # velocity 1 in Cubase; 0 with 0 is silent (measured:
                    # a Pianoteq note at -61 dB and nothing). Versailles'
                    # piano has both
                    d2 = 1
                notes.append((pos, nlen, ch, d1, d2, offv))
                ev_area['records'].append(
                    {'start': A.base + rec_start, 'end': A.base + p,
                     'pos_off': A.base + rec_start + 1,
                     'data_off': A.base + rec_start + 9,
                     'len_off': A.base + len_off})
            elif kind in (0xA0, 0xB0, 0xC0, 0xD0, 0xE0):
                ccs.append((pos, kind | (ch & 0x0f), d1, d2))
                ev_area['cc_records'].append(
                    {'start': A.base + rec_start, 'end': A.base + p})
            else:
                self.warn('%r: unknown MIDI event type %#04x in %r, rest of the '
                          'part skipped' % (track.name, kind, name))
                break
        # The event's offset is where in the part's own timeline the window
        # opens (ticks, like the notes): a part whose front was trimmed or
        # slid, or a copy cut from a longer one, keeps its notes where they
        # were and says how far in it starts playing. Gradila's Hive parts
        # (offset 17040) put their chords 17.75 s late, at the very end of
        # each part, until the notes were read relative to the window.
        if offset:
            notes = [(n[0] - offset,) + tuple(n[1:]) for n in notes]
            ccs = [(c[0] - offset,) + tuple(c[1:]) for c in ccs]
            ev_origin['midi_offset'] = offset
            # a controller set before the window (sustain down, a bend held)
            # is still in force when it opens: its last value moves to 0
            early, late = {}, []
            for c in ccs:
                if c[0] < 0:
                    key = (c[1], c[2] if (c[1] & 0xF0) in (0xA0, 0xB0) else None)
                    early[key] = (0.0,) + tuple(c[1:])
                else:
                    late.append(c)
            ccs = sorted(early.values(), key=lambda c: c[1]) + late
        # Notes past the window's end do not play either, and one that
        # starts inside it stops at the part's end, in Cubase as in REAPER
        if length > 0:
            after = [n for n in notes if n[0] >= length]
            if after:
                notes = [n for n in notes if n[0] < length]
                ccs = [c for c in ccs if c[0] < length]
            notes = [(n[0], min(n[1], length - n[0])) + tuple(n[2:])
                     if n[0] + n[1] > length else n for n in notes]
        # A part trimmed at its front keeps the clip's earlier notes at
        # negative ticks. Cubase never plays a note whose start lies outside
        # the part, and REAPER cannot hold a negative position at all: the
        # squashed heap it made of them was audible.
        before = [n for n in notes if n[0] < 0]
        if before:
            notes = [n for n in notes if n[0] >= 0]
            ccs = [c for c in ccs if c[0] >= 0]
            self.warn('%r: MIDI part %r is trimmed at its start; %d note(s) '
                      'before the part, which Cubase does not play, were left '
                      'out' % (track.name, name, len(before)))
        it = Item()
        it.kind = 'midi'
        it.pos = self.t2s(start)
        it.length = self.t2s(start + length) - it.pos
        it.name = name or track.name
        it.mute = 1 if (flags & MUTED) else 0
        it.origin = ev_origin
        it.notes = notes
        it.ccs = ccs
        it.ticks = length
        it.ppq = PPQ
        it.events = ev_area
        # after the part: the event's attribute block, as on an audio event
        # ('Farb' its own colour, a palette index)
        try:
            where = {}
            at, _q = self.at.fourcc(part.de, ev.de, where)
            ev_origin['ev_attrs'] = (A.base + part.de, where.get('count', 0))
            farb = at.get('Farb')
            if isinstance(farb, int) and self.palette:
                it.color = self.palette[farb % len(self.palette)]
                ev_origin['farb_off'] = A.base + where['keys']['Farb'][0]
        except Exception:
            pass
        track.items.append(it)


def read(path):
    r = CprReader(path)
    p = r.read()
    p.log = r.log
    return p
