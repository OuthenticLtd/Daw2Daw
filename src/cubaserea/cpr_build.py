"""Build a Cubase project from a REAPER one.

A .cpr is not a format anything but Cubase writes, and inventing one from
nothing means inventing record layouts that have not been worked out. So a
project is built the way it can be built safely: from a donor .cpr that
Cubase wrote, holding one of each kind of record. Every track in the REAPER
project becomes a copy of the matching kind, and only its contents are
rewritten - name, plug-in, file reference, notes. Nothing is invented; the
bytes around what changes are still exactly what Cubase produced.

What that buys, in one pass:

  * an audio track per REAPER audio track, its events pointed at the right
    files and placed at the right times
  * an instrument track per REAPER instrument track, carrying whatever VST
    the REAPER project used, with its saved patch
  * MIDI parts with their notes
  * track names and levels

The donor ships in templates/donor.cpr. Any real Cubase project works in its
place provided it has at least one audio event and one instrument track with
a MIDI part on it.
"""
import math
import uuid
import os
import re
import struct

from . import arch
from . import cpr_graft
from . import cpr_read
from . import cpr_tree
from . import envelope
from . import fadetpl
from . import media
from . import progress
from .model import (window_midi, mute_inside_folders,
                    playing_items, comp_items, other_lanes, muted_lane_items,
                    alternate_takes, expand_for_cubase, Fx)

def legacy(name):
    """Is this 2026-08-31 change switched off?

    CPR_LEGACY=1 turns all of them off at once, so a project can be built
    the way the tool built them before, and whatever Cubase objects to can
    be narrowed down one change at a time. The two that change the file's
    structure rather than its contents - the object registry and removing
    the donor prototypes - are off unless asked for: a project built with
    them has not yet been seen to open in Cubase, and one built without
    them has.

    CPR_ON_<NAME>=1 switches one on; CPR_NO_<NAME>=1 switches one off."""
    if os.environ.get('CPR_LEGACY') or os.environ.get('CPR_NO_' + name):
        return True
    if name == 'REGISTRY':
        # still opt-in: not yet seen to open
        return not os.environ.get('CPR_ON_' + name)
    # CONSUME and DELPROTO once came back "Invalid project file" together
    # and were opt-in; drop.py has built with both since, and those files
    # open. Left off, every project carried the donor's TEMPLATE
    # INSTRUMENT - Pianoteq with 262 notes, audible in the mix - and a
    # TEMPLATE AUDIO pointing at a KICK.wav on a D: drive no customer has.
    return False


HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.abspath(os.path.join(HERE, os.pardir, 'templates'))
DONOR = os.path.join(TEMPLATES, 'donor.cpr')
DONOR_AUDIO_FIRST = os.path.join(TEMPLATES, 'donor-audio-first.cpr')
# donor.cpr made again by Cubase with Melodyne applied to a copy of the
# template audio event (2026-10-01): TEMPLATE AUDIO holds a plain event, a
# Melodyne event and the musical-mode one, and the project a Melodyne
# document. Used for projects whose REAPER takes have Melodyne (ara.py).
DONOR_MELODYNE = os.path.join(TEMPLATES, 'donor-melodyne.cpr')
# A Cubase project with a video track on it. The donor has none, and a video
# track is not something a copy of another track can become, so it is
# brought in from here (see cpr_graft).
VIDEO_DONOR = os.path.join(TEMPLATES, 'video-donor.cpr')
# Cubase ships no instrument of its own - HALion Sonic and the others are
# separate installs - but its built-in effects are in every copy. A record
# whose instrument must not travel (the donor's Pianoteq) gets one of those
# in the slot: nothing to find, nothing to load, nothing missing.
STOCK_PLUGIN = ('946051208E29496E804F64A825C8A047', 'StudioEQ')


def stock_instrument():
    fx = Fx()
    fx.uid, fx.name = STOCK_PLUGIN
    return fx


def is_instrument(track):
    return track.instrument is not None or any(i.kind == 'midi'
                                               for i in track.items)


def track_kind(track, have_midi_proto=True):
    """Which of Cubase's three track kinds this REAPER track becomes.

    Cubase separates an instrument track, which owns a VST, from a plain
    MIDI track, which does not. A REAPER track with parts but no instrument
    is the second; built from the first it arrives carrying the donor's own
    synth."""
    if track.instrument is not None:
        return 'inst'
    if any(i.kind == 'midi' for i in track.items):
        return 'midi' if have_midi_proto else 'inst'
    return 'audio'


def stacked(items):
    """Events in the order Cubase should hold them: by position, and where an
    item has several takes, the take that plays last - Cubase plays the
    topmost of overlapping events, and the last in the list is on top."""
    return sorted(items, key=lambda i: (i.pos, 1 if i.take_sel else 0,
                                        i.take_no))


def anchors(want, audio_first):
    """Which project tracks take the donor's own two records.

    Those two cannot move: everything else is a copy placed around them, so
    the one that goes first has to be a track that comes first. This returns
    (audio position, instrument position), or None when the donor's order
    cannot produce the project's."""
    inst = [n for n, t in enumerate(want) if is_instrument(t)]
    audio = [n for n, t in enumerate(want) if not is_instrument(t)]
    if not inst or not audio:
        return (audio[0] if audio else None, inst[0] if inst else None)
    if audio_first:
        for a in audio:
            after = [i for i in inst if i > a]
            if after:
                return (a, after[0])
    else:
        for i in inst:
            after = [a for a in audio if a > i]
            if after:
                return (after[0], i)
    return None


def pick_donor(project):
    """The donor whose two records are in an order this project can use.

    With the donor's records consumed only where a track's position agrees
    (the default since 2026-09-30), every order is reproducible from
    donor.cpr: a kind whose record comes too early is simply copied. So
    donor.cpr is used always - it is also the only donor with a musical-mode
    prototype and track colours. donor-audio-first.cpr is kept for
    CPR_NO_CONSUME builds, whose order the two fixed records decide.
    (A resaved donor-audio-first.cpr with a warped event came out invalid
    for every project, 2026-09-30; ZITRO built from donor.cpr opens.)"""
    if (getattr(project, 'ara_from', None) == 'reaper'
            and getattr(project, 'ara_docs', None)
            and os.path.exists(DONOR_MELODYNE)
            and any(getattr(i, 'ara_id', None)
                    for t in project.tracks for i in t.items)):
        return DONOR_MELODYNE
    if not legacy('CONSUME'):
        return DONOR
    want = [t for t in project.tracks if not t.is_folder]
    for path, audio_first in ((DONOR, False), (DONOR_AUDIO_FIRST, True)):
        if os.path.exists(path) and anchors(want, audio_first) is not None:
            return path
    return DONOR

NOTE_KIND = 0x90

# The controller records this is prepared to write. Only 0xB0 (a control
# change - mod wheel, expression, sustain) appears in any project Cubase
# saved on this machine, across thousands of records, so it is the only one
# whose byte layout is known rather than assumed. Aftertouch (0xA0, 0xD0)
# and program change (0xC0) are carried no further than the note saying so.
#
# Pitch bend is the one worth settling. A control change stores its two
# data bytes exactly as MIDI sends them - d1 the controller number, d2 the
# value - so a bend most likely stores its low half in d1 and its high half
# in d2, which is what MIDI sends. "Most likely" is not good enough to write
# by default: a bend with its halves the wrong way round does not fail
# quietly, it bends a part off key. So it is off unless asked for, and it
# can be asked for either way round to find out which is right by ear:
#
#   CPR_WRITE_BENDS=1      d1 = low half, d2 = high half (MIDI's own order)
#   CPR_WRITE_BENDS=swap   d1 = high half, d2 = low half
#
# Whichever plays back in tune is the answer, and then it becomes what this
# writes without being asked.
BEND_KIND = 0xE0


def bend_mode():
    """How a pitch bend's value is written.

    'hrdt' (the default): d1/d2 in MIDI wire order and the value, normalised
    the way Cubase writes it, in the record's HRDT attribute - which is the
    value Cubase plays by (measured by importing a MIDI file with known
    bends into Cubase and reading the save; see write_notes). 'wire' writes
    d1/d2 only, leaving HRDT at the template's 0.0 (full down); 'swap' the
    halves the other way round; 'off' writes no bends. CPR_WRITE_BENDS
    selects; the default is the measured one."""
    v = (os.environ.get('CPR_WRITE_BENDS') or 'hrdt').strip().lower()
    return {'1': 'wire', '': 'hrdt'}.get(v, v)


def cc_kinds_written():
    return (0xB0,) if bend_mode() == 'off' else (0xB0, BEND_KIND)

CC_KIND_NAMES = {0xA0: 'polyphonic aftertouch', 0xB0: 'control change',
                 0xC0: 'program change', 0xD0: 'channel aftertouch',
                 0xE0: 'pitch bend'}

# Which channel parameter a lane follows. Volume is 2 with no device name;
# pan is 7 inside the device "Panner" (found 2026-09-28 in the user's own
# projects with pan automation: the MAutomationTrack record holds
# u16 1, u32 id, u32 name length, the name, u32 0). Ids 0-9 without the
# name bound to nothing.
PAN_PARAM = 7
PAN_DEVICE = b'Panner'
PAN_CLOSE = (0x1069, 0x0006)      # the words that close a pan lane's record
# a plug-in parameter lane, as Cubase 15 wrote one by hand on a converted
# track (2026-09-28): id 7, the name "Inserts\Slot\<uid>-<id>" for the
# first insert slot and "Inserts\Slot N\<uid>-<id>" for slot N >= 2, and
# closing words whose first differs per lane (0x1295 on Pro-Q 4's Output
# Level, 0x106a / 0x1069 / 0x108f / 0x1099 in the user's projects) - a
# tag Cubase assigns; 0x106a is the most common. <id> is the VST3
# parameter id, which REAPER's PARMENV index equals only for plug-ins that
# number their parameters 0..n in order. Old projects used id 0x00010007.
FX_PARAM = 7
FX_CLOSE = (0x106a, 0x0004)       # the tag is replaced per lane (vst3params)


def pan_to_norm(pan):
    """REAPER pan (-1 left .. +1 right) as the 0..1 a lane stores."""
    return (float(pan) + 1.0) / 2.0


def norm_to_pan(v):
    return float(v) * 2.0 - 1.0


# Where the folder written into a project should say a file is. Converting
# in a browser (web/webshim.py) the files sit under a folder of the
# browser's own (CPR_WEB_ROOT, '/work'); written as is, Cubase on Windows
# read '/work/Song/Song (Cubase)/' as no folder at all and asked for the
# project folder on opening. Under that root the folder is written the way a
# Windows Cubase writes one - a project that has been moved since, whose
# files Cubase then finds in its own folder.
WEB_DRIVE = 'C:\\DAW Converter\\'


def host_dir(d):
    """`d` (a folder) as the string a project records it with, ending in a
    separator; '' for no folder."""
    if not d:
        return ''
    root = os.environ.get('CPR_WEB_ROOT')
    if root:
        root = root.rstrip('/')
        if d == root or d.startswith(root + '/'):
            rest = d[len(root):].strip('/')
            return WEB_DRIVE + (rest.replace('/', '\\') + '\\' if rest else '')
    return d + os.sep


def file_uid(path):
    """A stable 32-character id for a file, the shape Cubase writes them."""
    import hashlib
    return hashlib.md5(os.path.normcase(os.path.abspath(path))
                       .encode('utf-8')).hexdigest().upper()


def pack_typed(ty, value):
    """A fixed-width number in the form the file stores it."""
    if ty == 'f32le':
        return struct.pack('<f', float(value))
    if ty == 3:
        return struct.pack('>f', float(value))
    if ty == 4:
        return struct.pack('>d', float(value))
    if ty == 1:
        return struct.pack('>q', int(value))
    return None


def wav_info(path):
    """(frames, bits, channels, sample rate) from a WAV header, or None."""
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
            if head[:4] != b'RIFF' or head[8:12] != b'WAVE':
                return None
            fmt = None
            data = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
                if cid == b'fmt ':
                    body = f.read(size)
                    ch = struct.unpack_from('<H', body, 2)[0]
                    rate = struct.unpack_from('<I', body, 4)[0]
                    bits = struct.unpack_from('<H', body, 14)[0]
                    fmt = (ch, rate, bits)
                elif cid == b'data':
                    data = size
                    f.seek(size + (size & 1), 1)
                else:
                    f.seek(size + (size & 1), 1)
                if fmt and data is not None:
                    break
            if not fmt or data is None:
                return None
            ch, rate, bits = fmt
            width = max(1, (bits + 7) // 8) * max(1, ch)
            return (data // width, bits, ch, float(rate))
    except OSError:
        return None


def node_at(root, ds):
    """The node in this subtree whose data starts at `ds`.

    A copy keeps the offsets of what it was copied from, so the same address
    identifies the matching record inside either one."""
    for n in cpr_tree.walk_nodes(root):
        if n.ds == ds:
            return n
    return None


def find_in(node, off):
    """The innermost node of this subtree whose own bytes cover `off`."""
    cur = node
    while True:
        for k in cur.kids:
            if k.hdr <= off < k.de:
                cur = k
                break
        else:
            return cur


def under_deleted(node, off):
    """True when `off` lies in an object of this subtree marked deleted."""
    cur = node
    while True:
        if cur.deleted:
            return True
        nxt = next((k for k in cur.kids if k.hdr <= off < k.de), None)
        if nxt is None:
            return False
        cur = nxt


class Builder:
    def __init__(self, donor, log):
        self.log = log
        self.donor_path = donor
        self.reader = cpr_read.CprReader(donor)
        self.donor = self.reader.read(keep_buses=True)
        self.A = self.reader.A
        self.base = self.A.base
        self.raw = open(donor, 'rb').read()
        self.root, self.index = cpr_tree.parse(self.A)
        self.table = cpr_tree.object_table(self.A, self.index)
        self.record_clip_refs()
        self._point = None
        self._lanes = {}        # track copy -> automation lanes added to it
        self._next_bus = None
        self._clip_no = 0
        self._pool = None
        self._send_tpl = None
        self._not_wav = set()
        self._pool_added = 0
        self._clips = {}
        self._shared = 0
        self.tempo = TempoMap([(0.0, 120.0)])
        self._chains = None
        self._var_count = {}
        self._video_src = None
        self.extra_registered = []  # grafted records that need a registry row
        self._instances = {}        # plug-in class id -> instances so far
        self._cc_done = 0           # controller events written into parts
        self._cc_nodonor = 0        # ones with no donor record to copy
        self._colour_blocked = 0    # colours the donor could not carry
        self._cc_unsupported = {}   # controller kind -> events left out
        self._stateless = []        # (where, plug-in, preset) written empty

    def devices_arch(self):
        """The Devices chunk of the donor, as an Arch, or None.

        A .cpr is seven chunks and this tool reads and rewrites exactly one
        of them, Arrangement1. Devices is the second largest and is copied
        through untouched - and Cubase keeps the whole VST Mixer in it,
        including the output channel that every track sums into."""
        raw = self.raw
        cs = arch.chunks(raw)
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
                return arch.Arch(raw[off2:off2 + size2], name='Devices',
                                 base=off2)
        return None

    def output_channel(self, dev=None):
        """(Devices Arch, the output channel's attribute Node), or (None, None).

        The track record for Cubase's Stereo Out holds nothing but the name
        'VST Mixer\\Channels\\OutputChannel' - 43 bytes, no mixer at all -
        which is why reading it as a track gives no fader and no inserts.
        The channel itself is VST Mixer / Output Channels in Devices, and it
        carries the same Volume and InsertFolder as any other channel."""
        from .cubase_attrs import Attrs, ListVal
        dev = dev if dev is not None else self.devices_arch()
        if dev is None:
            return None, None
        try:
            ob = dev.read_obj(0)
            n, o2 = dev.u32(ob.ds)
            g, _end = Attrs(dev).entries(o2, n, ob.de)
            vm = g.get('VST Mixer')
            oc = vm.get('Output Channels') if hasattr(vm, 'get') else None
            if isinstance(oc, ListVal):
                oc = oc[0] if oc else None
            return (dev, oc) if oc is not None else (dev, None)
        except Exception:
            return dev, None

    def write_master_mix(self, master):
        """The output bus's fader: REAPER's master level, on Cubase's mix.

        Everything sums into this channel, so its fader is the level of the
        whole mix - and it has never crossed, because it is not on a track.
        A project mixed 14 dB down on its master arrived 14 dB loud, with
        nothing said. Both halves of the fader are fixed-width doubles, so
        they are written where they sit and the chunk keeps its shape and
        its length; no record moves and nothing has to be repacked."""
        import math
        if master is None:
            return False
        dev, oc = self.output_channel()
        if oc is None:
            return False
        vol = oc.get('Volume')
        if not hasattr(vol, 'offs'):
            return False
        want_db = -144.0 if master.vol <= 0 else 20.0 * math.log10(master.vol)
        want_db = max(-144.0, want_db)
        pos = self.reader.gain_to_norm(master.vol) * 32768.0
        raw = bytearray(self.raw)
        done = 0
        for key, value in (('AnchorValue', want_db), ('Value', pos)):
            off_ty = vol.offs.get(key)
            if not off_ty:
                continue
            off, ty = off_ty
            if ty != 4:                 # f64; anything else is not this field
                continue
            a = dev.base + off
            struct.pack_into('>d', raw, a, float(value))
            done += 1
        if done:
            self.raw = bytes(raw)
        return done == 2

    def write_master_inserts(self, fxlist):
        """Put REAPER's master effects into Cubase's output-bus insert strip.

        The output channel's sixteen slots sit in the Devices chunk, empty:
        seventy-one bytes apiece holding a RuntimeID, a State and a
        SlotType. A slot with a plug-in in it is the same shape with more
        entries - the attribute format counts entries rather than measuring
        bytes, so a filled slot simply is longer and nothing inside needs a
        length corrected. The filled slot is taken from the donor's own
        arrangement, where a track already carries one, and the plug-in's
        identity and saved state are written into the copy exactly as they
        are for a track insert; the empty slot's RuntimeID is kept, since
        that is the only thing telling the sixteen apart.

        Returns how many were written."""
        from .cubase_attrs import Attrs, ListVal
        if not fxlist:
            return 0
        usable = [f for f in fxlist
                  if f.uid and not getattr(f, 'native', False)]
        if not usable:
            return 0
        dev, oc = self.output_channel()
        if oc is None:
            return 0
        ins = oc.get('InsertFolder')
        slots = ins.get('Slot') if hasattr(ins, 'get') else None
        spans = list(getattr(slots, 'spans', ()) or ())
        if not spans:
            return 0
        # a slot the donor already filled, to copy the shape from
        proto_fx = None
        for src in self.donor.tracks:
            if src.fx and 'slot' in (src.fx[0].origin or {}):
                proto_fx = src.fx[0]
                break
        if proto_fx is None:
            return 0
        s0, e0 = proto_fx.origin['slot']
        full = self.A.d[s0 - self.base:e0 - self.base]
        org = proto_fx.origin
        cuts = []
        done = 0
        for k, fx in enumerate(usable[:len(spans)]):
            a, b = spans[k]
            if 'Plugin' in slots[k]:
                continue                    # already occupied; leave it be
            if not fx.component:
                self._stateless.append(('the master', fx.name,
                                        getattr(fx, 'preset', '')))
            edits = []
            if 'GUID' in org:
                edits.append(self.encode_string(full, org['GUID'][0] - s0,
                                                fx.uid))
            if 'Plugin Name' in org and fx.name:
                edits.append(self.encode_string(full,
                                                org['Plugin Name'][0] - s0,
                                                fx.name))
            if 'IDString' in org:
                edits.append(self.encode_string(full, org['IDString'][0] - s0,
                                                self.instance_id(fx)))
            if 'audioComponent' in org:
                edits.append(self.encode_blob(full,
                                              org['audioComponent'][0] - s0,
                                              fx.component or b''))
            if 'editController' in org:
                edits.append(self.encode_blob(full,
                                              org['editController'][0] - s0,
                                              fx.controller or b''))
            payload = bytearray(self.patch_bytes(full, edits))
            # keep the slot's own RuntimeID: it is what distinguishes the
            # sixteen from each other inside the Devices chunk
            rid = slots[k].offs.get('RuntimeID')
            src_rid = None
            try:
                tmp = arch.Arch(bytes(payload))
                n2, o2 = tmp.u32(0)
                g2, _ = Attrs(tmp).entries(o2, n2, len(payload))
                src_rid = g2.offs.get('RuntimeID')
            except Exception:
                src_rid = None
            if rid and src_rid and rid[1] == 1 and src_rid[1] == 1:
                keep = struct.unpack_from('>q', dev.d, rid[0])[0]
                struct.pack_into('>q', payload, src_rid[0], keep)
            cuts.append((a, b, bytes(payload)))
            done += 1
        if not cuts:
            return 0
        cuts.sort()
        return done if self.apply_device_cuts(dev, cuts) else 0

    def strip_synth_rack(self):
        """Empty the donor's instrument rack in the Devices chunk.

        Rack instruments do not live on tracks: the plug-in and its state
        sit in the Devices chunk, which is copied through verbatim. The
        donor carries HALion Sonic there, so every converted project loaded
        HALion on open even though nothing in it used the instrument. The
        rack is an attribute list (VST Mixer / Synth Rack / Slot); its
        slots are spliced out and the count set to zero, which is what a
        project that never had a rack instrument holds."""
        raw = self.raw
        cs = arch.chunks(raw)
        dev = None
        for i, (cid, off, size) in enumerate(cs):
            if cid != 'ROOT':
                continue
            a = arch.Arch(raw[off:off + size])
            names = []
            o = 0
            while o < size:
                nm, o = a.string(o)
                names.append(nm)
            if names and names[0] == 'Devices' and i + 1 < len(cs):
                _c2, off2, size2 = cs[i + 1]
                dev = arch.Arch(raw[off2:off2 + size2], name='Devices',
                                base=off2)
                break
        if dev is None:
            return 0
        from .cubase_attrs import Attrs
        try:
            ob = dev.read_obj(0)
            n, o2 = dev.u32(ob.ds)
            g, _end = Attrs(dev).entries(o2, n, ob.de)
            vm = g.get('VST Mixer')
            rack = vm.get('Synth Rack') if hasattr(vm, 'get') else None
            slots = rack.get('Slot') if hasattr(rack, 'get') else None
            spans = getattr(slots, 'spans', None)
        except Exception:
            return 0
        if not spans:
            return 0
        # Every real project keeps a fixed 64-slot rack, so the occupied
        # slots are not cut out: each is replaced by a copy of an empty
        # slot carrying the occupied one's RuntimeID, which is the only
        # field that distinguishes the empties from one another. Done as
        # linear byte surgery: the tree machinery's object detection sees
        # embedded records straddling slot boundaries and refuses.
        d = dev.d
        empty_i = next((k for k, sl in enumerate(slots)
                        if 'Plugin' not in sl), None)
        occupied = [k for k, sl in enumerate(slots) if 'Plugin' in sl]
        if empty_i is None or not occupied:
            return 0
        ea, eb = slots.spans[empty_i]
        template = d[ea:eb]
        rid_off, rid_ty = slots[empty_i].offs.get('RuntimeID', (None, None))
        cuts = []
        for k in occupied:
            a, b = slots.spans[k]
            repl = bytearray(template)
            r_off, r_ty = slots[k].offs.get('RuntimeID', (None, None))
            if rid_off is not None and r_off is not None and rid_ty == 1:
                rid = struct.unpack_from('>q', d, r_off)[0]
                struct.pack_into('>q', repl, rid_off - ea, rid)
            cuts.append((a, b, bytes(repl)))

        # The MIDI port a rack instrument exposed dies with the instrument,
        # but its descriptor stays listed, and a listed port that no longer
        # exists is a Missing Ports prompt on every open. Drop descriptors
        # naming an emptied instrument, and say how many remain.
        names = []
        for k in occupied:
            plug = slots[k].get('Plugin')
            nm = plug.get('Plugin Name') if hasattr(plug, 'get') else None
            if isinstance(nm, str) and nm:
                names.append(nm.encode('utf-8'))
        pd = g.get('PortDescriptors')
        pspans = getattr(pd, 'spans', None)
        if pspans and names:
            keep = len(pspans)
            for (pa, pb) in pspans:
                if any(nm in d[pa:pb] for nm in names):
                    cuts.append((pa, pb, b''))
                    keep -= 1
            if keep != len(pspans):
                pc = pspans[0][0] - 4
                cuts.append((pc, pc + 4, struct.pack('>I', keep)))
        cuts.sort()
        return (self.apply_device_cuts(dev, cuts) and len(occupied)) or 0

    def apply_device_cuts(self, dev, cuts):
        """Apply (start, end, replacement) edits to the Devices chunk.

        Devices is not modelled by cpr_tree the way the arrangement is, so
        the records inside it are moved by hand: every 64-bit reference to
        an object and every 32-bit reference to an interned string is found
        by scanning, the payload is rebuilt around the cuts, and each of
        those references and each enclosing object's size is moved to where
        its target ended up. It works in both directions - a replacement
        may be longer than what it replaces, which is what putting a
        plug-in into an empty insert slot needs."""
        d = dev.d
        raw = self.raw
        if not cuts:
            return False
        root, idx = cpr_tree.parse(dev)
        objs = set(idx)
        refs64 = set()
        refs32 = set()
        i2 = 0
        end = len(d) - 8
        while i2 <= end:
            if i2 in objs:
                i2 += 8
                continue
            v = struct.unpack_from('>q', d, i2)[0]
            if v > 0 and v in objs and v != i2:
                refs64.add(i2)
                i2 += 8
                continue
            i2 += 1
        i2 = 0
        while i2 <= len(d) - 4:
            v = struct.unpack_from('>I', d, i2)[0]
            if (256 <= v < len(d) - 8 and v != i2
                    and self._interned_string_at_in(dev, v)):
                refs32.add(i2)
                i2 += 4
                continue
            i2 += 1

        pieces = []
        q = 0
        table = []      # (old start of unchanged run, old end, new start)
        newlen = 0
        for a, b, repl in cuts:
            pieces.append(d[q:a])
            table.append((q, a, newlen))
            newlen += a - q
            pieces.append(repl)
            newlen += len(repl)
            q = b
        pieces.append(d[q:])
        table.append((q, len(d), newlen))
        payload = bytearray(b''.join(pieces))

        def newpos(pos):
            for a, b, nb in table:
                if a <= pos < b:
                    return nb + (pos - a)
            return None

        for pos in refs64:
            np = newpos(pos)
            if np is None or np + 8 > len(payload):
                continue
            v = struct.unpack_from('>q', payload, np)[0]
            nv = newpos(v)
            if nv is not None and nv != v:
                struct.pack_into('>q', payload, np, nv)
        for pos in refs32:
            np = newpos(pos)
            if np is None or np + 4 > len(payload):
                continue
            v = struct.unpack_from('>I', payload, np)[0]
            nv = newpos(v)
            if nv is not None and nv != v:
                struct.pack_into('>I', payload, np, nv)
        for n2 in idx.values():
            np = newpos(n2.sf)
            if np is None or np + 8 > len(payload):
                continue
            shrink = 0
            for a, b, repl in cuts:
                if n2.ds <= a and n2.de >= b:
                    shrink += (b - a) - len(repl)
            if shrink:
                size = struct.unpack_from('>q', payload, np)[0]
                struct.pack_into('>q', payload, np, size - shrink)
        self.raw = cpr_tree.repack(raw, dev, bytes(payload))
        return True

    def _interned_string_at_in(self, A, v):
        d = A.d
        if v < 4:
            return False
        n = struct.unpack_from('>I', d, v - 4)[0]
        if not 0 < n <= 64 or v + n > len(d):
            return False
        seen_nul = False
        for b in d[v:v + n]:
            if seen_nul and b not in (0, 0xef, 0xbb, 0xbf):
                return False
            if b == 0:
                seen_nul = True
            elif not seen_nul and not 32 <= b < 127:
                return False
        return seen_nul

    # ---------------------------------------------------------- prototypes
    def set_project_path(self, out):
        """Tell the project where it now lives.

        The pool records the project file's own name and folder. Left as the
        donor's, Cubase opens the converted project asking to confirm a
        working directory that belongs to another project."""
        A = self.A
        for n in cpr_tree.walk_nodes(self.root):
            if n.cls != 'PPool':
                continue
            for k in n.kids:
                if k.cls != 'FNPath':
                    continue
                try:
                    o = k.ds
                    name_off = o
                    _name, o = A.string(o)
                    o += 4
                    for _ in range(3):
                        _s, o = A.string(o)
                    _, o = A.i32(o)
                    _, o = A.u16(o)
                    dir_off = o
                except Exception:
                    return False
                d, nm = os.path.split(os.path.abspath(out))
                self.set_string(self.root, self.base + name_off, nm)
                new_dir = host_dir(d)
                # The donor's folder is spelt out in more places than the
                # pool's own path record: the project-folder setting Cubase
                # asks about on opening ("Set Project Folder"), and the
                # pool's Audio and Media folder records. Every string that
                # starts with the donor's folder - or with its parent, which
                # is what the setting holds - is re-based onto the new one.
                old_dir, _ = A.string(dir_off)
                stripped = old_dir.rstrip('\\/')
                # the donor was saved on Windows: split on its separators,
                # not this system's (posix dirname keeps "C:\a\b" whole,
                # which left the setting unchanged on Linux and in the browser)
                cut = max(stripped.rfind('\\'), stripped.rfind('/'))
                parent = stripped[:cut] if cut > 0 else ''
                parent = parent + old_dir[len(stripped)] if parent else ''
                done = set()
                # not inside an audio clip: those name the clip's own file
                # and are rewritten, or removed, with the event they belong to
                clips = [(c.hdr, c.de) for c in self.index.values()
                         if c.cls == 'PAudioClip']
                for prefix in (old_dir, parent):
                    if not prefix:
                        continue
                    pb = prefix.encode('utf-8')
                    i = A.d.find(pb)
                    while i >= 4:
                        try:
                            txt, _e = A.string(i - 4)
                            n = struct.unpack_from('>I', A.d, i - 4)[0]
                        except Exception:
                            txt, n = '', 0
                        if (0 < n < 2048 and txt.startswith(prefix)
                                and (i - 4) not in done
                                and not any(a <= i < b for a, b in clips)):
                            if txt == parent and prefix == parent:
                                # The project-folder setting: Cubase keeps
                                # it as the folder's parent followed, in the
                                # string just before, by the folder's own
                                # name (parent "D:/Projects/", name "Song").
                                # Left as the donor's name, Cubase looks for
                                # a folder that is not there and asks for one.
                                self.set_string(self.root, self.base + i - 4,
                                                host_dir(os.path.dirname(d)))
                                # the name sits a few fixed fields before
                                # the parent: the nearest short, plain string
                                # (no separators) in the 64 bytes before it
                                sep = os.sep.encode() + b'/'
                                name_at = None
                                for q in range(max(0, i - 8 - 64), i - 8):
                                    n2 = struct.unpack_from('>I', A.d, q)[0]
                                    if not 2 <= n2 <= 64 or q + 4 + n2 > i - 4:
                                        continue
                                    body = A.d[q + 4:q + 4 + n2]
                                    if body.endswith(cpr_tree.BOM):
                                        body = body[:-3]
                                    if not body.endswith(bytes(1)):
                                        continue
                                    text = body[:-1]
                                    if text and all(32 <= c < 127 for c in text)                                             and not any(c in sep for c in text):
                                        name_at = q
                                if name_at is not None:
                                    self.set_string(self.root,
                                                    self.base + name_at,
                                                    os.path.basename(d))
                            else:
                                self.set_string(self.root, self.base + i - 4,
                                                new_dir + txt[len(prefix):])
                            done.add(i - 4)
                        i = A.d.find(pb, i + len(pb))
                return True
        return False

    def record_all_refs(self):
        """Find every stored offset in the payload, not just the parsed ones.

        The rebuild only rewrites references at positions parsing identified,
        and parsing only visits what the reader understands. The instrument
        prototype alone is pointed at by dozens of positions the reader never
        walks - and the moment anything before such a target moves, every
        unparsed reference to it dangles and Cubase calls the file invalid.
        That is why appending at the tail always worked and any early
        deletion or in-place resize did not.

        So every 8-byte value that names a real object's size field or a real
        class definition is treated as a reference. The risk of a value that
        only looks like one is accepted deliberately: a size field cannot be
        hit (their positions are excluded), overlapping matches keep the
        first, and a payload position is far likelier to be the reference it
        appears to be than data that happens to equal a live offset."""
        d = self.A.d
        objs = set(self.index)
        defs = set(self.A.defs)
        known = self.A.obj_refs | self.A.cls_refs
        end = len(d) - 8
        i = 0
        added = 0
        while i <= end:
            if i in objs:           # a size field: emit recomputes these
                i += 8
                continue
            v = struct.unpack_from('>q', d, i)[0]
            if v > 0 and v in objs and v != i:
                # An attribute's i64 value is not a reference, whatever it
                # happens to equal: a RuntimeID of 0x562 was taken for a
                # pointer at an object at 0x562 and "moved", and Cubase,
                # which connects a track's parts by RuntimeID, then showed
                # every instrument track as "No VST Instrument".
                if self._in_attr_value(i, 8):
                    i += 1
                    continue
                if i not in known:
                    self.A.obj_refs.add(i)
                    added += 1
                i += 8
                continue
            if v < -2 and (v & cpr_tree.MASK) in defs:
                if i not in known:
                    self.A.cls_refs.add(i)
                    added += 1
                i += 8
                continue
            i += 1

        # The 4-byte references. Diffing consecutive Cubase auto-saves of
        # real projects - files where Cubase itself shifted bytes and
        # updated every offset it maintains - shows these classes keep
        # object offsets as u32: the drum maps alone hold over a hundred.
        # The scan is scoped to those classes so a count or an id that
        # happens to equal a live offset elsewhere is never touched.
        # Only classes whose u32 fields are name references and which hold
        # no sample counts or positions: an audio clip's length in samples
        # passes any looks-like-an-offset test and rewriting one corrupts
        # the event, which is exactly what happened when the pool classes
        # were included here. The pool's own u32 references (each entry's
        # clip position) are added precisely, from the entry layout, below.
        NARROW = ('PDrumMapPool', 'PDrumMap', 'PDrumMapEntry',
                  'DynamicsParam', 'SNADynamicDefinition',
                  'PSNAParamSelection', 'PSNAParamSelectionEntry',
                  'MAutoFadeSetting', 'MLinearInterpolator', 'UColorSet',
                  'VisibilityConfigurations')
        spans = []
        for n in self.index.values():
            if n.cls in NARROW:
                spans.append((n.hdr, n.de))
        spans.sort()
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        wide = self.A.obj_refs | self.A.cls_refs
        for a, b in merged:
            i = a
            while i <= b - 4:
                if i in objs:
                    i += 8
                    continue
                # never split a known 8-byte reference
                if any((i - k) in wide for k in range(1, 8)) or i in wide:
                    i += 1
                    continue
                v = struct.unpack_from('>I', d, i)[0]
                # These point at interior positions - interned name
                # strings, mostly. A plausible-position test alone is not
                # enough: a window read one byte into a string's length
                # field passes it, and rewriting that shredded the drum
                # map. So the TARGET has to be recognisable too: a real
                # object, or an interned string - a sane length prefix
                # followed by name-shaped bytes.
                if (256 <= v < len(d) - 8 and v != i
                        and (v in objs or self._interned_string_at(v))):
                    if i not in self.A.obj_refs32:
                        self.A.obj_refs32.add(i)
                        added += 1
                    i += 4
                    continue
                i += 1

        # every pool entry names its clip by a u32 position at a fixed
        # place in the row - the same field pool_links maintains for the
        # entries the build edits; the untouched ones have to move too
        for n in cpr_tree.walk_nodes(self.root):
            if n.cls != 'GTreeEntry' or n.kids:
                continue
            a = n.ds + self.POOL_CLIP_AT
            if a + 4 > len(d):
                continue
            v = struct.unpack_from('>I', d, a)[0]
            if v and v in objs and a not in self.A.obj_refs32:
                self.A.obj_refs32.add(a)
                added += 1
        return added

    # a typed attribute: u32 name length (2..64), the name and its NUL, then
    # a u16 type - 1 for i64, 3 for f32, 4 for f64 - and the value itself
    _ATTR_RE = re.compile(rb'\x00\x00\x00([\x02-\x40])([\x20-\x7e]{1,63}\x00)\x00([\x01\x03\x04])', re.S)

    def _attr_value_ranges(self):
        """Every byte range holding a typed attribute's numeric value."""
        if getattr(self, '_attr_ranges', None) is None:
            rs = []
            for m in self._ATTR_RE.finditer(self.A.d):
                if len(m.group(2)) != m.group(1)[0]:
                    continue
                w = 4 if m.group(3) == b'\x03' else 8
                rs.append((m.end(), m.end() + w))
            rs.sort()
            self._attr_ranges = rs
            self._attr_starts = [a for a, _b in rs]
        return self._attr_ranges

    def _in_attr_value(self, i, width=8):
        """Does the window [i, i+width) overlap an attribute's value?

        An attribute's value is not a reference whatever it happens to
        equal - and neither is a window that straddles one. A RuntimeID of
        0x562 read one byte late gave 0x56200, which was an object's
        offset; the "reference" was moved on rebuild, the RuntimeID with
        it, and Cubase - which connects a track's parts by RuntimeID -
        showed every instrument track as "No VST Instrument"."""
        import bisect
        rs = self._attr_value_ranges()
        j = bisect.bisect_right(self._attr_starts, i + width - 1)
        for a, b in rs[max(0, j - 3):j]:
            if a < i + width and i < b:
                return True
        return False

    def _interned_string_at(self, v):
        """Does payload offset v hold the body of a length-prefixed name?"""
        d = self.A.d
        if v < 4:
            return False
        n = struct.unpack_from('>I', d, v - 4)[0]
        if not 0 < n <= 64 or v + n > len(d):
            return False
        seen_nul = False
        for b in d[v:v + n]:
            if seen_nul and b not in (0, 0xef, 0xbb, 0xbf):
                return False
            if b == 0:
                seen_nul = True
            elif not seen_nul and not 32 <= b < 127:
                return False
        return seen_nul

    def record_clip_refs(self):
        """Find the references a clip keeps to its own parts.

        A clip points at its file record and its path records with plain
        object references that sit inside data the attribute reader passes
        over, so nothing recorded them and nothing moved them. A copy of the
        clip then still points at the original's file - it describes one
        recording and names another - and Cubase will not build a waveform
        for it. They are found by looking for the positions themselves,
        inside that one clip and only where the value is a record of its."""
        d = self.A.d
        found = 0
        for n in cpr_tree.walk_nodes(self.root):
            if n.cls != 'PAudioClip':
                continue
            targets = set(k.sf for k in cpr_tree.walk_nodes(n))
            o = n.ds
            while o < n.de - 8:
                if struct.unpack_from('>q', d, o)[0] in targets:
                    self.A.obj_refs.add(o)
                    found += 1
                    o += 8
                else:
                    o += 1
        return found

    def prototypes(self):
        """One donor track of each kind, and the list that holds them.

        A third kind matters as much as the other two: Cubase separates an
        instrument track, which owns a VST, from a plain MIDI track, which
        does not. A REAPER track with MIDI parts and no instrument is the
        second, and copying the instrument prototype for it handed every
        such track the donor's own synth."""
        audio = inst = midi = None
        for t in self.donor.tracks:
            if t.is_folder:
                continue
            if audio is None and any(i.kind == 'audio' for i in t.items):
                audio = t
            if inst is None and t.instrument is not None:
                inst = t
            if (midi is None and t.instrument is None
                    and any(i.kind == 'midi' for i in t.items)):
                midi = t
        self.proto_midi = midi
        if audio is None or inst is None:
            raise SystemExit(
                'the donor project needs one audio track with an event on it '
                'and one instrument track: %s has %s' %
                (os.path.basename(self.reader.path),
                 'no audio event' if audio is None else 'no instrument track'))
        # new tracks hang off the top-level list, which survives when the
        # donor's own tracks are removed
        top = self.donor.src.get('sf')
        holder = self.index.get(top - self.base) if top else None
        if holder is None:
            node = self.index[inst.src['sf']]
            for n in self.index.values():
                if node in n.kids:
                    holder = n
                    break
        return audio, inst, holder

    def patch_metadata_rate(self, rate):
        """Write the sample rate into the Metadata chunk too.

        The arrangement's PArrangeSetup is what Cubase plays by, but the
        file repeats the rate in a Metadata chunk that is copied through
        verbatim, and the two disagreeing is the kind of thing a loader is
        entitled to reject. The entry is AudioSampleRate -> Float, an f64."""
        raw = self.raw
        i = raw.find(b'AudioSampleRate')
        if i < 0:
            return False
        j = raw.find(b'\x00\x00\x00\x06Float\x00', i, i + 200)
        if j < 0:
            return False
        o = j + 4 + 6            # past the length and the name
        ty = struct.unpack_from('>H', raw, o)[0]
        if ty != 4:
            return False
        o += 2
        self.raw = raw[:o] + struct.pack('>d', rate) + raw[o + 8:]
        return True

    # ---------------------------------------------------------- edits
    def sub(self, root, off, payload, span):
        find_in(root, off - self.base).splice(off - self.base,
                                              off - self.base + span, payload)

    def sub_over(self, root, off, payload, span):
        """Replace a run that may hold whole records of its own.

        An insert strip carries the plug-ins' states inside it, so writing a
        new strip takes those records out along with the bytes around them."""
        a = off - self.base
        find_in(root, a).replace(a, a + span, payload)

    def set_string(self, root, off, text):
        a = off - self.base
        n = struct.unpack_from('>I', self.A.d, a)[0]
        wide = self.A.d[a + 4:a + 4 + n].endswith(b'\xef\xbb\xbf')
        b = text.encode('utf-8')
        if wide:
            payload = struct.pack('>I', len(b) + 4) + b + b'\x00' + b'\xef\xbb\xbf'
        else:
            payload = struct.pack('>I', len(b) + 1) + b + b'\x00'
        find_in(root, a).splice(a, a + 4 + n, payload)

    def set_blob(self, root, off, data):
        a = off - self.base
        kind = struct.unpack_from('>H', self.A.d, a)[0]
        n = struct.unpack_from('>I', self.A.d, a + 2)[0]
        payload = struct.pack('>H', kind) + struct.pack('>I', len(data)) + data
        find_in(root, a).splice(a, a + 6 + n, payload)

    def set_f64(self, root, off, value):
        a = off - self.base
        find_in(root, a).splice(a, a + 8, struct.pack('>d', float(value)))

    def set_raw(self, root, off, raw):
        a = off - self.base
        find_in(root, a).splice(a, a + len(raw), raw)

    # ---------------------------------------------------------- content
    def write_plugin(self, root, proto_fx, fx):
        """Point a slot at whatever plug-in the REAPER project used."""
        org = proto_fx.origin
        if 'GUID' in org and fx.uid:
            self.set_string(root, org['GUID'][0], fx.uid)
        if 'Plugin Name' in org and fx.name:
            self.set_string(root, org['Plugin Name'][0], fx.name)
        # Cubase finds the plug-in through IDString - the class id plus an
        # instance number - not through the GUID alone: a slot whose IDString
        # still named the donor's plug-in came up as "No VST Instrument"
        if 'IDString' in org and fx.uid:
            self.set_string(root, org['IDString'][0], self.instance_id(fx))
        if fx.uid and fx.uid != (proto_fx.uid or '').upper():
            self.normalise_buses(root, org)
        empty = bool(os.environ.get('CPR_EMPTY_STATE'))   # for bisecting
        # (the stock stand-in a donor instrument slot gets has no settings
        # to lose: it is the builder's, not the user's)
        if not fx.component and not empty and \
                (fx.uid or '').upper() != STOCK_PLUGIN[0]:
            self._stateless.append((getattr(fx, '_where', ''), fx.name,
                                    getattr(fx, 'preset', '')))
        if 'audioComponent' in org:
            self.set_blob(root, org['audioComponent'][0],
                          b'' if empty else (fx.component or b''))
        if 'editController' in org:
            self.set_blob(root, org['editController'][0],
                          b'' if empty else (fx.controller or b''))
        # a bypassed REAPER plug-in: Cubase's Active flag off, so the slot
        # passes its input through (the builder used to leave every slot on,
        # and a bypassed limiter played in Cubase)
        if 'Active' in org:
            off, ty = org['Active']
            self.set_typed(root, off, ty, 0 if fx.bypass else 1)
        elif fx.bypass:
            self.log.append('%r: bypassed in REAPER, but the donor slot has no '
                            'Active flag to switch off' % fx.name)

    def normalise_buses(self, root, org):
        """One stereo input and one stereo output on a plug-in slot.

        The slot describes the plug-in's bus layout, and it is the donor's
        plug-in's layout - eight outputs for Pianoteq. Handed to a plug-in
        with a different layout Cubase would not load it ("No VST
        Instrument"). A real save of a synth shows one stereo bus each way,
        which is what every instrument has as its main pair; Cubase takes
        the rest from the plug-in itself."""
        out = org.get('Audio Output Arrangement')
        if not out or not out.get('spans'):
            return False
        s0, e0 = out['spans'][0]
        stereo = self.A.d[s0 - self.base:e0 - self.base]
        for key, arr in (('Audio Input Count', 'Audio Input Arrangement'),
                         ('Audio Output Count', 'Audio Output Arrangement')):
            lst = org.get(arr)
            if not lst or not lst.get('spans'):
                continue
            a = lst['spans'][0][0] - self.base
            z = lst['spans'][-1][1] - self.base
            find_in(root, a).splice(a, z, stereo)
            co = lst['count_off'] - self.base
            find_in(root, co).splice(co, co + 4, struct.pack('>I', 1))
            if key in org:
                off, ty = org[key]
                self.set_typed(root, off, ty, 1)
        return True

    def instance_id(self, fx):
        """'<class id>-<n>', unique per plug-in instance in the project."""
        n = self._instances.get(fx.uid, 0) + 1
        self._instances[fx.uid] = n
        return '%s-%d' % (fx.uid, n)

    def patch_bytes(self, buf, edits):
        """Apply (offset, old length, new bytes) to a byte string."""
        out = bytearray()
        q = 0
        for off, old_len, new in sorted(edits):
            out += buf[q:off]
            out += new
            q = off + old_len
        out += buf[q:]
        return bytes(out)

    def encode_string(self, buf, off, text):
        n = struct.unpack_from('>I', buf, off)[0]
        wide = buf[off + 4:off + 4 + n].endswith(cpr_tree.BOM)
        b = text.encode('utf-8')
        if wide:
            return (off, 4 + n,
                    struct.pack('>I', len(b) + 4) + b + bytes([0]) + cpr_tree.BOM)
        return (off, 4 + n, struct.pack('>I', len(b) + 1) + b + bytes([0]))

    def encode_blob(self, buf, off, data):
        kind = struct.unpack_from('>H', buf, off)[0]
        n = struct.unpack_from('>I', buf, off + 2)[0]
        return (off, 6 + n,
                struct.pack('>H', kind) + struct.pack('>I', len(data)) + data)

    def slot_edits(self, proto_track, fxlist):
        """One replacement per insert slot, in order.

        A slot is a run of bytes rather than a record of its own, so a
        plug-in is put in by writing over the slot with a copy of one the
        donor filled and the plug-in's own identity and state patched into
        it. The slots are written one at a time: the strip as a whole runs
        past the last of them into a record that has to stay."""
        slots = proto_track.origin.get('slots')
        if not slots or not fxlist:
            return []
        proto_fx = None
        for src in [proto_track] + list(self.donor.tracks):
            if src.fx and 'slot' in src.fx[0].origin:
                proto_fx = src.fx[0]
                break
        if proto_fx is None:
            return []
        s0, e0 = proto_fx.origin['slot']
        full = self.A.d[s0 - self.base:e0 - self.base]
        org = proto_fx.origin
        out = []
        for i, fx in enumerate(fxlist[:len(slots)]):
            if not fx.component:
                self._stateless.append((getattr(fx, '_where', ''), fx.name,
                                        getattr(fx, 'preset', '')))
            edits = []
            if 'GUID' in org and fx.uid:
                edits.append(self.encode_string(full, org['GUID'][0] - s0,
                                                fx.uid))
            if 'Plugin Name' in org and fx.name:
                edits.append(self.encode_string(full,
                                                org['Plugin Name'][0] - s0,
                                                fx.name))
            if 'IDString' in org and fx.uid:
                edits.append(self.encode_string(full, org['IDString'][0] - s0,
                                                self.instance_id(fx)))
            if 'audioComponent' in org:
                edits.append(self.encode_blob(full,
                                              org['audioComponent'][0] - s0,
                                              fx.component or b''))
            if 'editController' in org:
                edits.append(self.encode_blob(full,
                                              org['editController'][0] - s0,
                                              fx.controller or b''))
            # the slot's Active flag (i64): off for a plug-in bypassed in
            # REAPER, so Cubase passes the audio through it as REAPER does
            if 'Active' in org and org['Active'][1] == 1:
                edits.append((org['Active'][0] - s0, 8,
                              struct.pack('>q', 0 if fx.bypass else 1)))
            out.append((slots[i], self.patch_bytes(full, edits)))
        return out

    def write_slots(self, root, proto_track, fxlist):
        """Put the effects into the strip, and say how many went in."""
        done = 0
        bad = [f for f in fxlist
               if not f.uid or getattr(f, 'native', False)]
        if bad:
            self.log.append('%d plug-in(s) have no identity Cubase could '
                            'load and were left out of the inserts: %s'
                            % (len(bad), ', '.join(f.name for f in bad)))
            fxlist = [f for f in fxlist if f not in bad]
        for (a, z), payload in self.slot_edits(proto_track, fxlist):
            node = find_in(root, a - self.base)
            if any(k.hdr < z - self.base and k.de > z - self.base
                   for k in node.kids):
                # this slot runs into a record of its own; leave it alone
                break
            node.replace(a - self.base, z - self.base, payload)
            done += 1
        return done

    def set_typed(self, root, off, ty, value):
        """Write a fixed-width number back where the reader found it."""
        a = off - self.base
        if ty == 'f32le':
            payload = struct.pack('<f', float(value))
        elif ty == 'f64le':
            payload = struct.pack('<d', float(value))
        elif ty == 3:
            payload = struct.pack('>f', float(value))
        elif ty == 4:
            payload = struct.pack('>d', float(value))
        elif ty == 1:
            payload = struct.pack('>q', int(value))
        else:
            return False
        find_in(root, a).splice(a, a + len(payload), payload)
        return True

    def set_attr(self, root, proto_track, key, value):
        """Set one of a track's four-character attributes.

        If the track already carries it, the value is written where it sits.
        If it does not, the attribute is added on the end of the block and
        the block's count goes up by one - which is how a colour is given to
        a track the donor never coloured."""
        at = proto_track.origin.get('attrs')
        if not at:
            return False
        here = at['keys'].get(key)
        if here is None:
            # Adding an attribute the track does not carry means growing the
            # block, and on an audio track that lands where the record ends
            # rather than where the block does - the track then reads back as
            # nothing at all. So an attribute is only ever written where the
            # donor already keeps one, which is why the template tracks carry
            # a colour of their own.
            return False
        off, ty = here
        return bool(self.set_typed(root, off, ty, value))

    def can_colour(self, proto_track):
        """Whether a track copied from this record can be given a colour.

        A colour is the 4CC attribute 'Farb', and set_attr can only write an
        attribute the donor's record already carries - so a donor whose
        tracks were never coloured in Cubase cannot pass one on. That is a
        property of the donor file, not of the project, and it is worth
        saying out loud: it used to drop every colour in silence."""
        at = proto_track.origin.get('attrs') if proto_track is not None else None
        return bool(at and 'Farb' in at.get('keys', {}))

    def write_color(self, root, proto_track, rgb):
        """The nearest colour in the project's palette.

        Cubase keeps a palette and a track holds an index into it, so a
        REAPER colour arrives as the closest one Cubase has."""
        pal = self.reader.palette
        if not rgb or not pal:
            self._colour_blocked += 1
            return False
        if not self.can_colour(proto_track):
            self._colour_blocked += 1
            return False
        r, g, b = rgb
        best = min(range(len(pal)),
                   key=lambda i: (pal[i][0] - r) ** 2 + (pal[i][1] - g) ** 2
                   + (pal[i][2] - b) ** 2)
        ok = self.set_attr(root, proto_track, 'Farb', best + 1)
        if not ok:
            self._colour_blocked += 1
        return ok

    def cubase_mix(self, track):
        """(fader gain, pan) as Cubase has to hold them to play what the
        REAPER track played.

        REAPER's balance panner boosts the pair towards the centre (up to
        3 dB at the 0 dB law) and Cubase's does not (panlaw.py), so a pan
        value copied across sat 1.8 dB off at half pan in the export
        comparison. The pan that gives Cubase the same L/R ratio is
        written, and the level the louder channel had goes onto the
        fader; the two renders then null."""
        from . import panlaw
        proj = getattr(self, 'project', None)
        if getattr(proj, 'pan_law_of', None) != 'reaper':
            return track.vol, track.pan
        law = proj.panlaw if proj.panlaw is not None else 1.0
        mode = track.panmode if track.panmode is not None else proj.panmode
        c_pan, gain = panlaw.reaper_to_cubase(track.pan, law, mode)
        return track.vol * gain, c_pan

    def write_mix(self, root, proto_track, track):
        """Fader, pan and mute, in the places the donor keeps them."""
        import math
        org = proto_track.origin
        done = 0
        vol, pan = self.cubase_mix(track)
        if vol > 2.0 + 1e-9:
            # Cubase's fader stops at +6.02 dB; the rest is on a Volume
            # insert (write(), fader_overflow)
            if not any(getattr(f, 'fader_overflow', False) for f in track.fx):
                self.log.append('%r: its level would be %+.2f dB and Cubase\'s '
                                'fader stops at +6.02 dB; it arrives %.2f dB '
                                'quieter' % (track.name, 20 * math.log10(vol),
                                             20 * math.log10(vol / 2.0)))
            vol = 2.0
        if 'vol_db' in org:
            off, ty = org['vol_db']
            db = -144.0 if vol <= 0 else 20.0 * math.log10(vol)
            done += self.set_typed(root, off, ty, max(-144.0, db))
        if 'vol_raw' in org:
            # the fader position is kept scaled by 32768, the same way the
            # reader takes it apart
            off, ty = org['vol_raw']
            done += self.set_typed(root, off, ty,
                                   self.reader.gain_to_norm(vol) * 32768.0)
        # the channel EQ: (band index, type, gain dB, Hz, Q as stored) for
        # each band that is on; the others are switched off
        eqw = org.get('chan_eq')
        want = getattr(track, 'chan_eq', None)
        eqs = org.get('chan_eq_state')
        if eqs and want is not None:
            # the EQ plug-in's own records: what Cubase plays
            from . import chan_eq as _ce
            start, recs = eqs
            on = dict((int(i), (ty, g, f, q)) for i, ty, g, f, q in want)
            for i, keys in enumerate(_ce.BAND_KEYS):
                k_on, k_ty, k_g, k_f, k_q = keys
                vals = [(k_on, 1.0 if i in on else 0.0)]
                if i in on:
                    ty, g, f, q = on[i]
                    vals += [(k_ty, float(ty)), (k_g, float(g)), (k_f, float(f)), (k_q, float(q))]
                for k, v in vals:
                    if k in recs:
                        done += self.set_typed(root, start + recs[k], 'f64le', v)
            if want and 'bypass' in recs:
                done += self.set_typed(root, start + recs['bypass'], 'f64le', 0.0)
            if want and org.get('chan_eq_slot'):
                so, sty = org['chan_eq_slot']
                done += self.set_typed(root, so, sty, 1)
        if eqw and want is not None and eqw.get('bands'):
            on = dict((int(i), (ty, g, f, q)) for i, ty, g, f, q in want)
            for i, fields in enumerate(eqw['bands']):
                if 'Enable' in fields:
                    done += self.set_typed(root, fields['Enable'][0], fields['Enable'][1],
                                           1 if i in on else 0)
                if i in on:
                    ty, g, f, q = on[i]
                    # (the mirror's Q is on another scale; left as it is)
                    for k, v in (('Type', int(ty)), ('Gain', float(g)),
                                 ('Freq', float(f))):
                        if k in fields:
                            done += self.set_typed(root, fields[k][0], fields[k][1], v)
            if want and 'Bypass' in eqw:
                done += self.set_typed(root, eqw['Bypass'][0], eqw['Bypass'][1], 0)
        elif want and not eqw:
            self.log.append('%r: its channel EQ was not written - the donor '
                            'channel has no EQ fields' % track.name)
        ig = getattr(track, 'input_gain_pos', None)
        if ig is not None and 'input_gain' in org:
            # the channel's Input Gain, as the position Cubase keeps
            # (16383.5 = 0 dB)
            off, ty = org['input_gain']
            done += self.set_typed(root, off, ty, max(0.0, min(32767.0, float(ig))))
        if 'delay' in org:
            off, _ty = org['delay']
            a = off - self.base
            dl = max(-10.0, min(10.0, float(getattr(track, 'delay', 0.0) or 0.0)))
            find_in(root, a).splice(a, a + 8, struct.pack('>d', dl))
            done += 1
        elif abs(getattr(track, 'delay', 0.0) or 0.0) > 1e-9:
            self.log.append('%r: its track delay (%+.1f ms) was not written - '
                            'the donor channel has no delay field'
                            % (track.name, track.delay * 1000))
        if 'pan' in org:
            off, ty = org['pan']
            done += self.set_typed(root, off, ty,
                                   max(0.0, min(1.0, pan / 2.0 + 0.5)))
        if track.mute:
            done += self.write_mute(root, proto_track)
        return done

    def write_mute(self, root, proto_track):
        """Mute the channel itself.

        Cubase keeps a channel's mute as a 'SoloFlag' entry - an i64 whose
        bit 0 is the mute - in the mixer channel's attribute group, and
        writes the entry only while the channel is muted (found by muting
        a group in Cubase, saving, and diffing the files). Written into
        the entry when the donor channel has one; otherwise put in right
        after RuntimeID, where Cubase writes it, and the group's count
        raised by one. A muted folder becomes a muted group this way:
        its tracks keep playing into it, exactly as REAPER's do into a
        muted folder, and only the sum is silent."""
        org = proto_track.origin
        if 'solo_flag' in org:
            off, ty = org['solo_flag']
            return 1 if self.set_typed(root, off, ty, 1) else 0
        if 'chan_group' not in org:
            return 0
        count_off, at = org['chan_group']
        a, z = count_off - self.base, at - self.base
        n = struct.unpack_from('>I', self.A.d, a)[0]
        find_in(root, a).splice(a, a + 4, struct.pack('>I', n + 1))
        entry = (struct.pack('>I', 9) + b'SoloFlag\x00'
                 + struct.pack('>H', 1) + struct.pack('>q', 1))
        find_in(root, z).splice(z, z, entry)
        self._muted_channels = getattr(self, '_muted_channels', 0) + 1
        return 1

    def write_tempo(self, tempo):
        """Replace the donor's tempo map with the project's own."""
        area = getattr(self.reader, 'tempo_area', None)
        if not area:
            return 0
        body, n = tempo.records()
        a, z = area['first'] - self.base, area['end'] - self.base
        find_in(self.root, a).replace(a, z, body)
        co = area['count_off'] - self.base
        find_in(self.root, co).splice(co, co + 4, struct.pack('>I', n))
        return n

    def name_offsets(self, node, text):
        """Every place inside `node` that spells `text` as a whole string.

        A summing folder writes its name in several records - the folder,
        the group channel it holds, the mixer's copy of each - and renaming
        one of them leaves the others reading the donor's name."""
        if not text:
            return []
        want = text.encode('utf-8')
        out = []
        d = self.A.d
        i = node.hdr
        while True:
            i = d.find(want, i, node.de)
            if i < 0:
                break
            for pad in (1, 4):
                if i >= 4:
                    n = struct.unpack_from('>I', d, i - 4)[0]
                    if n == len(want) + pad:
                        out.append(self.base + i - 4)
                        break
            i += len(want)
        return out

    # A pool entry: u32 spare, u32 where the clip is, u16 kind, u32 how many
    # entries it holds, its name, then its place in the list.
    POOL_CLIP_AT = 4
    POOL_COUNT_AT = 10
    POOL_NAME_AT = 14

    def pool(self):
        """The pool's audio folder and the entry to copy for each clip.

        Cubase lists every clip in the pool, and a clip that is not listed is
        one it will not build a waveform for - the event shows an image
        construction error instead. A media entry is told apart from a plain
        folder by the clip position it carries."""
        if self._pool is None:
            self._pool = (None, None)
            here = set(self.index)
            for n in cpr_tree.walk_nodes(self.root):
                if n.cls != 'GTreeEntry' or n.kids:
                    continue
                try:
                    at = struct.unpack_from('>I', self.A.d,
                                            n.ds + self.POOL_CLIP_AT)[0]
                except Exception:
                    continue
                if at and at in here:
                    for p in cpr_tree.walk_nodes(self.root):
                        if n in p.kids:
                            self._pool = (p, n)
                            return self._pool
        return self._pool

    def add_to_pool(self, name, clip_node):
        """List one clip in the pool, and say where its position goes."""
        folder, proto = self.pool()
        if folder is None or clip_node is None:
            return None
        entry = cpr_tree.copy_subtree(proto)
        folder.extra.append((None, entry))
        self.set_string(entry, self.base + proto.ds + self.POOL_NAME_AT, name)
        self._pool_added += 1
        return (entry, clip_node, self.POOL_CLIP_AT, 4)

    def prune_pool(self, pool_links):
        """Unlist donor clips that nothing plays any more.

        The pool names every clip, and Cubase resolves each named clip's
        file on open - so a donor clip whose events are all gone made every
        converted project ask for the donor's own KICK.wav. An entry is
        kept if its clip is the target of a rewritten event pointer, or if
        some live record outside the rewritten events still points at it;
        otherwise the entry is dropped from the pool listing. The clip
        records themselves stay - unlisted, they are just bytes."""
        folder, proto = self.pool()
        if folder is None:
            return []
        d = self.A.d
        linked = set(id(t) for _h, t, _o, _w in pool_links)
        dead_spans = [(n.hdr, n.de) for n in cpr_tree.walk_nodes(self.root)
                      if n.deleted]
        dead_spans += [(h.hdr, h.de) for h, _t, _o, _w in pool_links
                       if h is not None]

        def live_ref(clip_sf, entry):
            i = 0
            end = len(d) - 8
            while i <= end:
                if struct.unpack_from('>q', d, i)[0] == clip_sf:
                    if not (entry.hdr <= i < entry.de) and not any(
                            a <= i < b for a, b in dead_spans):
                        return True
                    i += 8
                    continue
                i += 1
            return False

        pruned = []
        for k in list(folder.kids):
            if k.kids or k.deleted:
                continue
            try:
                at = struct.unpack_from('>I', d,
                                        k.ds + self.POOL_CLIP_AT)[0]
            except Exception:
                continue
            clip = self.index.get(at)
            if clip is None:
                continue
            if id(clip) in linked:
                continue
            if live_ref(at, k):
                continue
            k.deleted = True
            name = ''
            try:
                name, _ = self.A.string(k.ds + self.POOL_NAME_AT)
            except Exception:
                pass
            pruned.append(name or '?')
        return pruned

    def close_pool(self):
        """Say how many entries the audio folder ended up with."""
        folder, proto = self.pool()
        if folder is None:
            return
        kept = sum(1 for k in folder.kids if not k.deleted)
        n = kept + self._pool_added
        if n == len(folder.kids) and not self._pool_added:
            return
        a = folder.ds + self.POOL_COUNT_AT
        find_in(folder, a).splice(a, a + 4, struct.pack('>I', n))

    def folder_group_name(self, folder_node):
        """What the group channel inside a summing folder is called.

        Cubase names the folder and the channel it holds separately, so a
        folder taking a project's name has to hand it to both."""
        try:
            inner = folder_node.kids[0]
            dev = inner.kids[0]
            node = self.A.read_obj(dev.ds + cpr_read.TRACK_EVENT_PREFIX,
                                   dev.de)
            name, _ = self.A.string(node.ds)
            return name
        except Exception:
            return None

    def folder_group(self, folder_node):
        """The group channel a summing folder keeps inside itself."""
        if folder_node is None:
            return None
        lo, hi = folder_node.hdr, folder_node.de
        for t in self.donor.tracks:
            n = self.index.get(t.src.get('sf'))
            if (n is not None and n is not folder_node
                    and lo <= n.hdr and n.de <= hi and t.bus_id is not None):
                return t
        return None

    def free_bus_id(self):
        """An id no channel in the donor is using."""
        if self._next_bus is None:
            used = [t.bus_id for t in self.donor.tracks
                    if t.bus_id is not None]
            used += [t.out_bus_id for t in self.donor.tracks
                     if t.out_bus_id is not None]
            self._next_bus = (max(used) if used else 64) + 1
        self._next_bus += 1
        return self._next_bus - 1

    def group_prototype(self):
        """A plain group channel in the donor, and its record.

        A REAPER track that other tracks send to is an aux: in Cubase that
        is a channel of its own, so those tracks are built from this rather
        than as audio tracks, which cannot be sent to."""
        best = None
        for t in self.donor.tracks:
            n = self.index.get(t.src.get('sf'))
            if (n is None or n.cls != 'MDeviceTrackEvent'
                    or t.bus_id is None or not t.origin.get('bus_id')):
                continue
            # one without effects of its own, or every aux gets them too
            rank = (len(t.fx), sum(1 for _ in cpr_tree.walk_nodes(n)))
            if best is None or rank < best[0]:
                best = (rank, t, n)
        return (best[1], best[2]) if best else (None, None)

    def send_template(self):
        """A send slot the donor filled in, to copy over empty ones."""
        if self._send_tpl is None:
            self._send_tpl = False
            for t in self.donor.tracks:
                slots = t.origin.get('send_slots') or []
                spans = t.origin.get('send_spans') or []
                for i, sl in enumerate(slots):
                    if 'on' in sl and i < len(spans):
                        self._send_tpl = (sl, spans[i])
                        return self._send_tpl
        return self._send_tpl or None

    def write_sends(self, root, proto_track, sends, bus_of):
        """Turn on one send per destination, pointing at its channel."""
        tpl = self.send_template()
        spans = proto_track.origin.get('send_spans')
        if not tpl or not spans or not sends:
            return 0
        fields, (s0, e0) = tpl
        full = self.A.d[s0 - self.base:e0 - self.base]
        done = 0
        for i, snd in enumerate(sends[:len(spans)]):
            bus = bus_of.get(snd.dest)
            if bus is None:
                continue
            import math
            db = -144.0 if snd.vol <= 0 else 20.0 * math.log10(snd.vol)
            # a muted REAPER send is a switched-off Cubase send
            values = {'on': 0 if getattr(snd, 'mute', False) else 1,
                      'out': bus, 'vol_db': max(-144.0, db),
                      'vol_raw': self.reader.gain_to_norm(snd.vol) * 32768.0}
            edits = []
            for key, val in values.items():
                if key not in fields:
                    continue
                off, ty = fields[key]
                raw = pack_typed(ty, val)
                if raw is not None:
                    edits.append((off - s0, len(raw), raw))
            payload = self.patch_bytes(full, edits)
            a, z = spans[i][0] - self.base, spans[i][1] - self.base
            node = find_in(root, a)
            if any(k.hdr < z and k.de > z for k in node.kids):
                break
            node.replace(a, z, payload)
            done += 1
        return done

    def clear_sends(self, root, proto_track, keep):
        """Switch off the donor's own sends past the first `keep` slots.

        A copy of the donor's audio track brings its send strip along, and
        the donor's TEMPLATE AUDIO has a send switched on to its FX
        channel - which is removed from the project. Every converted track
        then carried a live send to a bus that was not there ("send to
        unknown bus 180" on reading the file back). The slots the project
        does not use are switched off."""
        slots = proto_track.origin.get('send_slots') or []
        n = 0
        if os.environ.get('CPR_NO_CLEARSENDS'):
            return 0
        for i, sl in enumerate(slots):
            if i < keep or 'on' not in sl:
                continue
            off, ty = sl['on']
            if self.set_typed(root, off, ty, 0):
                n += 1
        return n

    def folder_prototype(self):
        """A folder track in the donor with nothing in it, and its record.

        A folder that already holds something would bring that along; the
        empty one is the pattern for a folder of the project's own."""
        best = None
        for t in self.donor.tracks:
            n = self.index.get(t.src.get('sf'))
            if n is None or n.cls != 'MFolderTrack':
                continue
            size = sum(1 for _ in cpr_tree.walk_nodes(n))
            # A REAPER folder sums what is inside it, and the Cubase folder
            # that does the same is one with a group channel. A folder
            # without one is only a way of tidying the track list.
            rank = (0 if t.bus_id is not None else 1, size)
            if best is None or rank < best[0]:
                best = (rank, t, n)
        return (best[1], best[2]) if best else (None, None)

    def channel_lane(self, proto_track):
        for ln in proto_track.origin.get('auto_lanes', []):
            if ln.get('kind') == 'channel':
                return ln
        return None

    def pan_param(self):
        """The parameter number to point a pan lane at.

        Overridable while working out what Cubase calls each number."""
        probe = os.environ.get('CPR_PAN_PROBE')
        if probe:
            ids = [int(x) for x in probe.replace(',', ' ').split()]
            n = self._lanes.get('probe', 0)
            self._lanes['probe'] = n + 1
            return ids[n % len(ids)]
        return int(os.environ.get('CPR_PAN_PARAM', PAN_PARAM))

    def clone_channel_lane(self, root, proto_track, param, name=None,
                           close=None):
        """A second automation lane on a copied track, following `param`.

        The donor was recorded with a volume lane and nothing else. A lane
        says which parameter it follows in two bytes and in nothing else, so
        every other channel parameter is that same record with those two
        bytes changed - which is how pan gets a lane without one ever
        having been drawn in Cubase.

        Returns a lane the planner can fill, or None if this track has no
        lane to copy."""
        lane = self.channel_lane(proto_track)
        node = proto_track.origin.get('auto_node')
        if lane is None or node is None or not lane.get('param_off'):
            return None
        src = node_at(root, lane['at'] - self.base)
        if src is None:
            return None
        holder = None
        for n in cpr_tree.walk_nodes(root):
            if src in n.kids:
                holder = n
                break
        if holder is None:
            return None
        dup = cpr_tree.copy_subtree(src)
        # after the lanes already there and before the node's own tail, so
        # the list Cubase walks stays in one piece
        holder.extra.append((None, dup))
        added = self._lanes.get(id(root), 0) + 1
        self._lanes[id(root)] = added
        co = node['count_off'] - self.base
        find_in(root, co).splice(co, co + 4,
                                 struct.pack('>I', node['count'] + added))
        po = lane['param_off'] - self.base
        rec = None
        for n in cpr_tree.walk_nodes(dup):
            if n.cls == 'MAutomationTrack' and n.ds <= po < n.de:
                rec = n
                break
        if name is None:
            name = PAN_DEVICE if param == PAN_PARAM else b''
        if close is None and param == PAN_PARAM:
            close = PAN_CLOSE
        if rec is not None and rec.de - rec.ds >= 14:
            # the whole parameter record: open flag, id, the device name
            # the id counts within, and the trailing word (emit recomputes
            # the record's size)
            tail = self.A.d[rec.ds + 10:rec.de]
            body = (struct.pack('>HII', 1, param, len(name) + 1 if name else 0)
                    + (name + b'\0' if name else b'') + tail)
            rec.splice(rec.ds, rec.de, body)
        else:
            find_in(dup, po).splice(po, po + 2, struct.pack('>H', param))
        if close and lane.get('end'):
            # the two words that close the lane record name the parameter
            # too: 0x0401/0x0004 on a volume lane, 0x1069/0x0006 on every pan
            # lane Cubase wrote itself (with the record alone Cubase opened a
            # second, empty pan lane beside the copy and ignored it)
            z = lane['end'] - self.base
            d = self.A.d
            if (struct.unpack_from('>H', d, z - 8)[0] == 0x0401
                    and z - 8 >= dup.hdr):
                own = find_in(dup, z - 8)
                own.splice(z - 8, z - 6, struct.pack('>H', close[0]))
                own.splice(z - 2, z, struct.pack('>H', close[1]))
            else:
                self.log.append('a new lane\'s closing words were not '
                                'where the donor keeps them; the lane may '
                                'not bind')
        out = dict(lane)
        out['node'] = dup
        return out

    def point_proto(self, ref=False):
        """Any automation point in the donor, to copy for new ones.

        `ref`: one whose header points at the class declaration rather than
        declaring it. Copies of a declaring point each declared the class
        again - fifteen declarations of MParamEvent on the output bus's lane
        hung Cubase on opening; copies of a pointing one all point at the
        same declaration (relocated to the first of them when the original
        declaration is gone, by cpr_tree.emit)."""
        if not ref:
            if self._point is None:
                for t in self.donor.tracks:
                    for ln in t.origin.get('auto_lanes', []):
                        if ln['points']:
                            self._point = node_at(self.root,
                                                  ln['points'][0] - self.base)
                            if self._point is not None:
                                return self._point
                self._point = False
            return self._point or None
        if getattr(self, '_point_ref', None) is None:
            self._point_ref = False
            for t in self.donor.tracks:
                for ln in t.origin.get('auto_lanes', []):
                    for off in ln['points']:
                        n = node_at(self.root, off - self.base)
                        if n is None:
                            continue
                        tag = struct.unpack_from('>q', self.A.d, n.hdr)[0]
                        if tag not in (-1, -2) and tag < 0:
                            self._point_ref = n
                            return n
        return self._point_ref or None

    def plan_volenv(self, root, proto_track, env):
        """Reserve a record per automation point, before any is written.

        `root` is the track this is going into - the donor's own record or a
        copy of it - so the points are looked up inside that subtree rather
        than in the donor, which a copy only resembles."""
        lane = self.channel_lane(proto_track)
        if lane is None:
            return None
        return self.plan_env(root, lane, env)

    def plan_env(self, root, lane, env, allow_empty=False):
        """The same, for a lane already chosen - a copy of one, say.

        `allow_empty`: fill a lane the donor left without points from the
        point prototype (the output bus's volume lane, for the master
        envelope) - the point count of the lane is at lane['count_off'] and
        the points hang off the node that holds it."""
        own = []
        for off in lane['points']:
            n = node_at(root, off - self.base)
            if n is not None:
                own.append(n)
        if not own and env and not allow_empty:
            # A lane the donor never wrote a point into is a stub: it has no
            # record to copy and no room laid out for one, and guessing that
            # layout produces a file that reads back as nonsense.
            self.log.append('the donor has no automation on one of its '
                            'tracks, so none was written to a track like it')
            return None
        holder = None
        if own:
            for n in cpr_tree.walk_nodes(root):
                if own[0] in n.kids:
                    holder = n
                    break
        if holder is None:
            holder = find_in(root, lane['count_off'] - self.base)
        # a lane with points of its own takes the copies after them; one
        # without gets them right behind its count (an offset key, which
        # cpr_tree.emit places exactly there), copied from a point that
        # refers to the class declaration instead of making one
        proto = self.point_proto(ref=not own) or self.point_proto()
        slots = []
        where = None if own else (lane['count_off'] - self.base + 4)
        for i in range(len(env)):
            if i < len(own):
                slots.append(own[i])
            elif proto is not None:
                cp = cpr_tree.copy_subtree(proto)
                holder.extra.append((where, cp))
                slots.append(cp)
        for n in own[len(slots):]:
            n.deleted = True
        return {'lane': lane, 'slots': slots, 'root': root}

    def write_volenv(self, plan, env, tonorm=None):
        """Times and levels into the records reserved for them.

        A lane holds each value as 0..1, so what that stands for is the
        lane's business: gain for volume, position for pan."""
        if not plan:
            return 0
        if tonorm is None:
            tonorm = self.reader.gain_to_norm
        for node, (when, value) in zip(plan['slots'], env):
            a = node.ds
            find_in(node, a).splice(a, a + 8,
                                    struct.pack('>d', self.tempo.ticks(when)))
            find_in(node, a + 8).splice(
                a + 8, a + 12,
                struct.pack('>f', max(0.0, min(1.0, tonorm(value)))))
        co = plan['lane']['count_off'] - self.base
        find_in(plan['root'], co).splice(co, co + 4,
                                         struct.pack('>I', len(plan['slots'])))
        return len(plan['slots'])


    # ---------------------------------------------------------- grafts
    def chain_map(self):
        if self._chains is None:
            self._chains = cpr_graft.chains_of(self.A, self.root)
        return self._chains

    def new_variation(self, dup, src_t, name, track_name, n_events):
        """Another version on the copied track `dup`, with room for
        `n_events` events.

        Returns the version's event list node - the events go into it as
        `extra` at its end - or None when the track keeps no versions."""
        tvc = src_t.origin.get('tvc')
        if not tvc:
            return None
        coll = node_at(dup, tvc['ds'] - self.base)
        lst = node_at(dup, src_t.src['name_off'] - self.base)
        if coll is None or lst is None:
            return None
        n = self._var_count.get(id(dup), tvc['count']) + 1
        try:
            raw = cpr_graft.variation_bytes(self.A, coll, lst, self.chain_map(),
                                            name, track_name, n_events, n)
        except ValueError as e:
            self.log.append('%r: no track version written (%s)'
                            % (track_name, e))
            return None
        var = cpr_graft.append(self.A, self.index, raw)
        coll.extra.append((None, var))
        self._var_count[id(dup)] = n
        co = tvc['count_off'] - self.base
        find_in(dup, co).splice(co, co + 4, struct.pack('>I', n))
        return var.kids[0]

    def rename_active_version(self, dup, src_t, name):
        """Name the version whose events sit on the track, and number it 1."""
        tvc = src_t.origin.get('tvc')
        if not tvc or not tvc.get('versions'):
            return False
        v = tvc['versions'][0]
        self.set_string(dup, v['name_off'], name or 'v1')
        a = v['after_name'] - self.base + 8
        if a + 4 <= v['end'] - self.base:
            find_in(dup, a).splice(a, a + 4, struct.pack('>I', 1))
        return True

    def write_event_flags(self, root, proto_item, item):
        """The event's flag word: muted or not."""
        off = (proto_item.origin or {}).get('flags')
        if not off:
            return False
        a = off - self.base
        flags = struct.unpack_from('>H', self.A.d, a)[0]
        if item.mute:
            flags |= cpr_read.MUTED
        else:
            flags &= ~cpr_read.MUTED
        find_in(root, a).splice(a, a + 2, struct.pack('>H', flags))
        return True

    def write_zorder(self, root, proto_item, item, serial):
        """The event's front-to-back serial (u32 after the clip; cpr_read
        reads it as Item.zorder). Cubase plays the overlapping event with
        the highest, so the events of a track are numbered in the order
        stacked() puts them - the take that plays last."""
        off = (proto_item.origin or {}).get('zorder_off')
        if not off:
            return False
        a = off - self.base
        find_in(root, a).splice(a, a + 4, struct.pack('>I', int(serial)))
        return True

    def write_pitch(self, root, proto_item, item):
        """A REAPER item's pitch shift (semitones) as the event's 'FtiP'
        record: the u32 count before the serial becomes 1 and the record -
        tag, u16 version 4, f64 frequency ratio - is inserted after it
        (cpr_read reads the same back). The donor's event has no such
        record, so the count is 0 there."""
        semis = float(getattr(item, 'pitch', 0.0) or 0.0)
        off = (proto_item.origin or {}).get('zorder_off')
        if not off or abs(semis) < 1e-6:
            return False
        a = off - self.base - 4
        if struct.unpack_from('>I', self.A.d, a)[0] != 0:
            return False        # the donor's event already carries records
        rec = (struct.pack('>I', 1) + b'FtiP' + struct.pack('>H', 4)
               + struct.pack('>d', 2.0 ** (semis / 12.0)))
        find_in(root, a).splice(a, a + 4, rec)
        return True

    def write_ara(self, root, proto_item, guid, event_guid):
        """A Melodyne event's IDs (the prototype's, DONOR_MELODYNE).

        The event names its audio source in Melodyne's document by a GUID
        and a version number (AXtModificationId: the modification is
        GUID.<n>), keeps a GUID of its own (IUEX) and the source's again in
        its AXtProjectAudioEvent (UMXA); all are fixed-width, so they are
        spliced in place. IUSX is Melodyne's constant and stays."""
        a0 = proto_item.origin['flags'] - self.base
        d = bytes(self.A.d[a0:a0 + 8000])
        done = 0
        for tag, val in ((b'IMXA', guid), (b'IUEX', event_guid), (b'UMXA', guid)):
            i = d.find(tag)
            if i < 0:
                continue
            m = re.compile(rb'\x00\x00\x00%([0-9A-F]{8}-[0-9A-F-]{27})\x00').search(d, i)
            if not m:
                continue
            at = a0 + m.start(1)
            find_in(root, at).splice(at, at + 36, val.encode('ascii'))
            if tag == b'IMXA':
                # the version number right after: this is version 1
                n_at = a0 + m.end(0)
                find_in(root, n_at).splice(n_at, n_at + 4, struct.pack('>I', 1))
            done += 1
        return done == 3

    def write_gain(self, root, proto_item, item):
        """The clip gain (REAPER's item volume): an f32 after the event's name."""
        off = (proto_item.origin or {}).get('gain_off')
        if not off:
            return False
        a = off - self.base
        g = item.gain if item.gain is not None else 1.0
        g = max(0.0, min(64.0, float(g)))
        find_in(root, a).splice(a, a + 4, struct.pack('>f', g))
        return True

    def graft_video(self):
        """A video track brought in from the video donor, or None."""
        if self._video_src is False:
            return None
        if not os.path.exists(VIDEO_DONOR):
            self.log.append('no video donor at %s, so the video track was '
                            'left out' % VIDEO_DONOR)
            self._video_src = False
            return None
        try:
            src = cpr_graft.Source(VIDEO_DONOR)
            vt, vnode = src.video_track()
            if vnode is None:
                raise ValueError('it has no video track with an event on it')
            raw, _info = cpr_graft.transplant(self.A, src, vnode, self.log)
            if raw is None:
                raise ValueError('its video track points outside itself')
            node = cpr_graft.append(self.A, self.index, raw)
        except Exception as e:
            self.log.append('the video track could not be brought in from '
                            '%s: %s' % (os.path.basename(VIDEO_DONOR), e))
            self._video_src = False
            return None
        self._video_src = (src, vt)
        return node

    def video_layout(self, vnode):
        """Where the fields of the grafted video track sit."""
        A = self.A
        lst = next((k for k in vnode.kids if k.cls == 'MListNode'), None)
        if lst is None:
            return None
        ev = next((k for k in lst.kids if k.cls == 'MVideoEvent'), None)
        if ev is None:
            return None
        clip = next((k for k in ev.kids if k.cls == 'PVideoClip'), None)
        kids = clip.kids if clip is not None else []
        fn = next((k for k in kids if k.cls == 'FNPath'), None)
        vf = next((k for k in kids if k.cls == 'VideoFile'), None)
        out = {'list': lst, 'event': ev, 'clip': clip, 'fnpath': fn,
               'videofile': vf}
        # the list: its name, a domain type (1, linear), a period, its count
        _nm, o = A.string(lst.ds)
        out['list_count_off'] = o + 4 + 8
        if fn is not None:
            # FNPath: name, a four-character type, the extension twice, a
            # description, an i32 and a u16, then the folder
            o = fn.ds
            out['fn_name_off'] = o
            _n, o = A.string(o)
            out['fn_4cc_off'] = o
            o += 4
            offs = []
            for _ in range(3):
                offs.append(o)
                _s, o = A.string(o)
            out['fn_ext_offs'] = offs
            _, o = A.i32(o)
            _, o = A.u16(o)
            out['fn_dir_off'] = o
        return out

    # what Cubase writes into FNPath for the two video containers seen in
    # real saves; anything else gets the MPEG-4 shape with its own extension
    VIDEO_KINDS = {'mov': (b'MooV', 'QuickTime Movie'),
                   'mp4': (bytes(4), 'MPEG 4 Video File'),
                   'm4v': (bytes(4), 'MPEG 4 Video File')}

    def write_video_track(self, vnode, track, items, links):
        """Name the grafted video track and put the project's video events
        on it. Returns how many were written."""
        lay = self.video_layout(vnode)
        if lay is None:
            self.log.append('%r: the grafted video track has an unexpected '
                            'shape; its events were not written' % track.name)
            return 0
        A = self.A
        base = self.base
        src_name = self._video_src[1].name if self._video_src else 'Video'
        offs = self.name_offsets(vnode, src_name) or [base + lay['list'].ds]
        for off in offs:
            self.set_string(vnode, off, track.name or 'Video')
        ev0 = lay['event']
        slots = [ev0]
        for _ in items[1:]:
            cp = cpr_tree.copy_subtree(ev0)
            for n in cpr_tree.walk_nodes(cp):
                n.ref_class = True      # point at the graft's declarations
            lay['list'].extra.append((None, cp))
            slots.append(cp)
        clips = {}
        n = 0
        for slot, it in zip(slots, items):
            path = media.resolve(it.file, '') if it.file else ''
            key = os.path.normcase(path)
            owner = clips.get(key)
            clip = (node_at(slot, lay['clip'].ds)
                    if lay['clip'] is not None else None)
            if owner is not None and clip is not None:
                # the same file again: point at the first event's clip
                slot.replace(clip.hdr, clip.de, bytes(8))
                links.append((slot, owner, self.EVENT_CLIP_AT, 8))
            elif clip is not None:
                clips[key] = clip
                d, nm = os.path.split(path)
                stem, ext = os.path.splitext(nm)
                ext = ext.lstrip('.').lower()
                self.set_string(slot, base + clip.ds, stem)
                if lay.get('fn_name_off') is not None:
                    self.set_string(slot, base + lay['fn_name_off'], nm)
                    fourcc, kind = self.VIDEO_KINDS.get(
                        ext, (bytes(4), '%s Video File' % ext.upper()))
                    a = lay['fn_4cc_off']
                    find_in(slot, a).splice(a, a + 4, fourcc)
                    e1, e2, e3 = lay['fn_ext_offs']
                    self.set_string(slot, base + e1, ext)
                    self.set_string(slot, base + e2, ext)
                    self.set_string(slot, base + e3, kind)
                    self.set_string(slot, base + lay['fn_dir_off'], host_dir(d))
                if lay['videofile'] is not None:
                    dur, fps = media.probe(path)
                    if dur and fps:
                        a = lay['videofile'].ds
                        find_in(slot, a).splice(
                            a, a + 8, struct.pack('>q', int(round(dur * fps))))
            # a video track counts in seconds, not ticks
            self.set_f64(slot, base + ev0.ds + 2, it.pos)
            self.set_f64(slot, base + ev0.ds + 10, it.length)
            self.set_f64(slot, base + ev0.ds + 18, it.soffs)
            flags = struct.unpack_from('>H', A.d, ev0.ds)[0]
            if it.mute:
                flags |= cpr_read.MUTED
            else:
                flags &= ~cpr_read.MUTED
            find_in(slot, ev0.ds).splice(ev0.ds, ev0.ds + 2,
                                         struct.pack('>H', flags))
            n += 1
        co = lay['list_count_off']
        find_in(lay['list'], co).splice(co, co + 4, struct.pack('>I', n))
        return n

    def drop_automation(self, dup, src_t):
        """Take the donor's automation lanes off a copied track.

        The donor was recorded with a volume lane open on every track, so
        every converted track arrived with an expanded lane that REAPER never
        had. A track without channel automation gets none."""
        node = src_t.origin.get('auto_node')
        lanes = src_t.origin.get('auto_lanes') or []
        if not node or not lanes:
            return 0
        co = node['count_off'] - self.base
        holder = find_in(dup, co)
        if holder.cls != 'MAutomationNode':
            return 0            # a layout the reader did not take apart
        # The lane records stay where they are - removing them made Cubase
        # drop the track, something still points at them - but the node's
        # count says how many lanes the track has, and with it at zero none
        # is made, so none is shown.
        n = sum(1 for ln in lanes if node_at(dup, ln['at'] - self.base) is not None)
        if n:
            holder.splice(co, co + 4, struct.pack('>I', 0))
        return n

    def fold_automation(self, dup, src_t):
        """Hide the track's automation lanes.

        Whether a track shows its lanes is the last u16 of its
        MAutomationNode: 1 shown, 0 hidden (a Cubase save before and after
        Hide Automation, 2026-10-01). The donor was saved with its lanes
        shown, so every converted track arrived with an open Volume lane
        REAPER never had. Hiding changes nothing that plays."""
        return self.show_automation(dup, src_t, False)

    def show_automation(self, root, src_t, show=True):
        """The last u16 of a track's MAutomationNode: 1 = lanes shown."""
        node = src_t.origin.get('auto_node')
        if not node:
            return False
        co = node['count_off'] - self.base
        holder = find_in(root, co)
        if holder is None or holder.cls != 'MAutomationNode' or holder.de - 2 < holder.ds:
            return False
        holder.splice(holder.de - 2, holder.de, struct.pack('>H', 1 if show else 0))
        return True

    def set_automation_read(self, root, src_t, on=True):
        """Cubase's automation Read: the first u16 of each lane's
        MAutomationTrack, 1 = the lane plays (a Cubase save with the track's
        R switched on, 2026-10-01; zeroed, a volume lane sat silent in the
        identity test of 2026-09-28). Returns how many lanes were set."""
        n = 0
        for ln in src_t.origin.get('auto_lanes') or []:
            k = node_at(root, ln['at'] - self.base)
            if k is None:
                continue
            for kid in k.kids:
                if kid.cls == 'MAutomationTrack' and kid.de - kid.ds >= 2:
                    kid.splice(kid.ds, kid.ds + 2, struct.pack('>H', 1 if on else 0))
                    n += 1
        return n

    def write_fades(self, root, proto_item, item):
        """REAPER item fades as Cubase MFadeIn/MFadeOut objects.

        An event keeps an 8-byte slot for each of the two fades right after
        its clip, zero when there is no fade. The donor's event has none, so
        a fade is written by putting a real save's fade record - with its
        curve endpoints and length patched - into the slot. The containing
        sizes take care of themselves: emit() recomputes every object's
        size over its splices."""
        slots = (proto_item.origin or {}).get('fade_slots')
        if not slots:
            return 0
        # the same rate write_audio counts the event's samples in
        info = wav_info(item.file) if item.file else None
        rate = float(info[3] if info
                     else (proto_item.origin.get('rate') or 48000.0))
        n = 0
        from . import fades
        from .rpp_read import fade_shape
        lines = getattr(item, 'fade_lines', None) or {}
        for slot, kind, secs, key in ((slots[0], 'in', item.fadein, 'FADEIN'),
                                      (slots[1], 'out', item.fadeout, 'FADEOUT')):
            if not secs or secs <= 0:
                continue
            # REAPER's fade shape, traced as points for Cubase's linear
            # interpolator (fades.py); a straight fade stays two points
            pts = None
            toks = lines.get(key)
            if toks:
                try:
                    shape, curve = fade_shape(toks)
                except ValueError:
                    shape, curve = 0, 0.0
                if not fades.is_linear(shape, curve) \
                        and not os.environ.get('CPR_NO_FADEPOINTS'):
                    pts = fades.points(shape, curve, fade_out=(kind == 'out'))
                    self._curved_fades = getattr(self, '_curved_fades', 0) + 1
            # A musical-mode event counts its fades in ticks, as it does its
            # length and offset (write_stretch); written in samples, Fills'
            # 0.196 s fade-in became ten seconds and the item played 11 dB
            # down, and every 1 ms fade on a sliced hi-hat 50 ms
            if getattr(item, 'native_stretch', False) \
                    and (proto_item.origin or {}).get('warp_off'):
                if kind == 'in':
                    length = (self.tempo.ticks(item.pos + secs)
                              - self.tempo.ticks(item.pos))
                else:
                    end = item.pos + item.length
                    length = self.tempo.ticks(end) - self.tempo.ticks(end - secs)
            else:
                length = secs * rate
            a = slot - self.base
            find_in(root, a).splice(a, a + 8,
                                    fadetpl.blob(kind, length, pts))
            n += 1
        return n

    def write_part_name(self, root, proto_item, name):
        off = (proto_item.origin or {}).get('part_name_off')
        if off and name:
            self.set_string(root, off, name)

    def _attr_offset(self, rec, name, ty=4):
        """Offset of a 4CC attribute's value inside an event record, or
        None - HRDT/OffV in a note, HRDT in a controller. The block is
        parsed rather than assumed to be at a fixed place."""
        from .cubase_attrs import Attrs
        try:
            tmp = arch.Arch(bytes(rec))
            where = {}
            Attrs(tmp).fourcc(14, len(rec), where)
            keys = where.get('keys', {})
            k = keys.get(name) or keys.get(name[::-1])
            if k and k[1] == ty:
                return k[0]
        except Exception:
            return None
        return None

    def _hrdt_offset(self, rec):
        """Offset of the HRDT double inside a controller record, or None.

        The record is: kind, f64 position, channel, d1, d2, u16, then a 4CC
        block - which the donor's CC 123 carries as XFLG (i64) and HRDT
        (f64). The block is parsed rather than assumed to be at a fixed
        place."""
        from .cubase_attrs import Attrs
        try:
            tmp = arch.Arch(bytes(rec))
            where = {}
            Attrs(tmp).fourcc(14, len(rec), where)
            k = where.get('keys', {}).get('HRDT')
            if k and k[1] == 4:
                return k[0]
        except Exception:
            return None
        return None

    def write_notes(self, root, proto_item, notes, ppq=None, ccs=()):
        """Replace a part's events, built from ones of its own.

        A note's position and length are ticks, and the two programs do not
        count them the same: Cubase is always 480 to the quarter, REAPER
        writes whatever the item says (`HASDATA 1 960 QN` - 960 is what
        REAPER uses by default). Handing REAPER's numbers over unscaled put
        every note at twice its beat and twice its length, so a part played
        at half speed and ran off the end of itself. `ppq` is the item's own
        resolution; every event is rescaled to Cubase's.

        `ccs` are the controllers - mod wheel, pitch bend, aftertouch, the
        movement a patch is played with. They are records of the same list
        as the notes, one byte shorter of a length and its note-off tail, so
        they are copied from a controller record rather than a note one; the
        donor keeps a CC 123 at the end of its part, which is that record.
        Notes and controllers go back interleaved in position order, as
        Cubase writes them."""
        e = proto_item.events
        if not e or not e['records']:
            return 0
        r0 = e['records'][0]
        tmpl = self.A.d[r0['start'] - self.base:r0['end'] - self.base]
        scale = cpr_read.PPQ / float(ppq or cpr_read.PPQ)
        len_rel = r0['len_off'] - r0['start']
        # A note's velocities live three times over: the 7-bit byte after
        # the pitch, a double HRDT = (v - 1) / 126 in the 4CC block, and
        # for the note-off the last byte of the 17-byte tail after the
        # length plus its double OffV. Cubase sends the tail byte as the
        # note-off velocity; the donor's template had 0 there, so every
        # release sample in Kontakt and Pianoteq played from velocity 0
        # where REAPER sent 64 or the played value - a bass that nulled to
        # -15 dB while its drums, one-shots with no release, nulled to
        # -66. Formula and byte measured on projects Cubase saved.
        hr_off = self._attr_offset(tmpl, 'HRDT')
        ov_off = self._attr_offset(tmpl, 'OffV')
        off_byte = len_rel + 8 + 16 if len_rel + 8 + 17 <= len(tmpl) else None
        out = []
        for pos, nlen, ch, pitch, vel, *rest in notes:
            b = bytearray(tmpl)
            at = float(pos) * scale
            struct.pack_into('>d', b, 1, at)
            b[9] = int(ch) & 0x0f
            b[10] = int(pitch) & 0x7f
            # 0 stays 0: a note that does not sound (a muted note in REAPER),
            # which Cubase plays as silence when the fine velocity is 0 too
            v = max(0, min(127, int(vel)))
            b[11] = v
            struct.pack_into('>d', b, len_rel, float(max(nlen * scale, 1.0)))
            offv = max(0, min(127, int(rest[0]))) if rest else 64
            if hr_off is not None:
                struct.pack_into('>d', b, hr_off, max(v - 1, 0) / 126.0)
            if ov_off is not None:
                struct.pack_into('>d', b, ov_off, (offv - 1) / 126.0 if offv else 0.0)
            if off_byte is not None:
                b[off_byte] = offv
            out.append((at, 0, bytes(b)))
        cc_recs = e.get('cc_records') or []
        ctmpl = (self.A.d[cc_recs[0]['start'] - self.base:
                          cc_recs[0]['end'] - self.base] if cc_recs else None)
        n_cc = 0
        for pos, st, d1, d2 in (ccs or ()):
            kind = int(st) & 0xf0
            if kind not in cc_kinds_written():
                # Every controller record in every project Cubase saved here
                # is a 0xB0, so that is the only shape there is any evidence
                # for. A pitch bend written on the same pattern is a guess
                # about which of the two data bytes Cubase reads as the high
                # half, and a wrong guess does not go unheard: it bent a
                # flute part off key from its first bend onwards. Counted and
                # reported instead of invented.
                self._cc_unsupported[kind] = (
                    self._cc_unsupported.get(kind, 0) + 1)
                continue
            if ctmpl is None:
                self._cc_nodonor += 1
                continue
            lo, hi = int(d1) & 0x7f, int(d2) & 0x7f
            if kind == BEND_KIND and bend_mode() == 'swap':
                lo, hi = hi, lo
            b = bytearray(ctmpl)
            at = float(pos) * scale
            b[0] = kind
            struct.pack_into('>d', b, 1, at)
            b[9] = int(st) & 0x0f
            b[10] = lo
            b[11] = hi
            # The record's HRDT double is the value Cubase plays by, and it
            # is normalised - measured by importing a MIDI file with known
            # values into Cubase and reading the save: a pitch bend is
            # v/16384 below centre and 0.5 + (v - 8192)/16382 above it
            # (0 -> 0.0, 8192 -> 0.5, 12288 -> 0.750031, 16383 -> 1.0); a
            # CC or aftertouch is (v - 1)/126 clamped at 0, like a note's
            # velocity. Written as the raw 14-bit value, a centre bend read
            # as 8192.0 and was clamped to 1.0 - full bend up - which is
            # why a bent brass part played sharp and its samples ran out
            # early in Cubase; and a CC record left at the template's 0.0
            # sent every mod-wheel and sustain event as 0.
            hr = self._hrdt_offset(ctmpl)
            if hr is not None:
                if kind == BEND_KIND:
                    v14 = ((int(d2) & 0x7f) << 7) | (int(d1) & 0x7f)
                    if bend_mode() == 'swap':
                        v14 = ((int(d1) & 0x7f) << 7) | (int(d2) & 0x7f)
                    norm = (v14 / 16384.0 if v14 <= 8192
                            else 0.5 + (v14 - 8192) / 16382.0)
                elif kind in (0xD0, 0xC0):
                    norm = max(0.0, ((int(d1) & 0x7f) - 1) / 126.0)
                else:
                    norm = max(0.0, ((int(d2) & 0x7f) - 1) / 126.0)
                struct.pack_into('>d', b, hr, float(norm))
            out.append((at, 1, bytes(b)))
            n_cc += 1
        # a stable sort by position, notes before controllers at the same
        # tick, so a patch is set up before the note that plays through it
        out.sort(key=lambda x: (x[0], x[1]))
        self._cc_done += n_cc
        a = e['first'] - self.base
        z = e['end'] - self.base
        find_in(root, a).replace(a, z, b''.join(x[2] for x in out))
        co = e['count_off'] - self.base
        find_in(root, co).splice(co, co + 4, struct.pack('>I', len(out)))
        return len(notes)

    # An event holds its clip 26 bytes in - after the flags and the three
    # times - either as the record itself or as the position of one it
    # shares with other events.
    EVENT_CLIP_AT = 26

    def write_audio(self, root, proto_item, item, reader, srcdir='',
                    links=None):
        """Point an event at a file and put it where REAPER had it.

        Cubase keeps one clip per audio file and has every event that plays
        it point at that one. A copy of the event brings a copy of the clip
        with it, and a file described twice is a file Cubase will not build
        a waveform for, so the first event to use a file keeps the clip and
        the rest are pointed at it.

        REAPER writes its media paths relative to the .rpp; the .cpr can sit
        in a different folder, so they are made absolute first or Cubase
        looks for the audio beside the wrong project."""
        org = proto_item.origin
        file_rate = None
        path = media.resolve(item.file, srcdir) if item.file else None
        info = wav_info(path) if path else None
        if info:
            file_rate = info[3]
        elif path:
            # The clip is a copy of one Cubase made for a WAV, and it
            # describes the file it plays down to its length in samples.
            # Anything else - an MP3, an OGG - cannot be described that way,
            # and Cubase reports the file as missing.
            self._not_wav.add(path)

        clip_ds = (org.get('clip_name_off') or 0) - self.base
        clip = node_at(root, clip_ds) if org.get('clip_name_off') else None
        share = None
        if path and clip is not None:
            key = os.path.normcase(path)
            if getattr(item, 'native_stretch', False) and org.get('warp_off'):
                # musical mode, its tempo and its algorithm belong to the
                # clip: every stretch gets a clip of its own (Cubase's own
                # Import > New Version makes the same: one file, two clips)
                key = (key, 'warp',
                       round(self.tempo.bpm_at(item.pos) / (item.playrate or 1.0), 6),
                       bool(item.preserve_pitch),
                       # stretch markers live in the clip's warp scale:
                       # every marked item gets a clip of its own
                       tuple(item.stretch_markers or ()), round(item.playrate or 1.0, 9))
            owner = self._clips.get(key)
            if owner is not None and owner is not clip:
                share = owner
            else:
                self._clips[key] = clip
                # the copy points at the class the donor already declares
                # rather than declaring it again
                for n in cpr_tree.walk_nodes(clip):
                    n.ref_class = True

        if share is not None:
            # this file already has a clip: hand the event its position
            # instead of a second copy of the record
            root.replace(clip.hdr, clip.de, bytes(8))
            if links is not None:
                links.append((root, share, self.EVENT_CLIP_AT, 8))
            self._shared += 1
        elif path:
            d, n = os.path.split(path)
            for name_off, dir_off in (org.get('paths')
                                      or [(org.get('file_name_off'),
                                           org.get('file_dir_off'))]):
                if name_off:
                    self.set_string(root, name_off, n)
                if dir_off:
                    self.set_string(root, dir_off, host_dir(d))
            # one id per file, and no two files sharing one
            for off in (org.get('uid_offs') or ()):
                self.set_string(root, off, file_uid(path))
            if org.get('clip_name_off'):
                self.set_string(root, org['clip_name_off'],
                                os.path.splitext(n)[0])
            if info and org.get('file_info_off'):
                frames, bits, ch, rate = info
                for off in (org.get('frame_offs') or ()):
                    # the file record itself is rewritten whole, just below
                    if off == org.get('file_info_off'):
                        continue
                    b = off - self.base
                    find_in(root, b).splice(b, b + 8,
                                            struct.pack('>q', frames))
                a = org['file_info_off'] - self.base
                # The two bytes between the depth and the channel count are
                # the block align - how many bytes one frame takes - and
                # they were being copied from the donor's own clip rather
                # than worked out for the file being described. Read off
                # projects Cubase saved: 24-bit mono 0x0003, 24-bit stereo
                # 0x0006, 32-bit stereo 0x0008, which is (bits / 8) x
                # channels every time. A wrong one describes a file whose
                # frames are a different size than they are.
                align = max(1, (bits + 7) // 8) * max(1, ch)
                find_in(root, a).splice(
                    a, a + 18,
                    struct.pack('>q', frames) + struct.pack('>H', bits)
                    + struct.pack('>H', align) + struct.pack('>H', ch)
                    + struct.pack('>f', rate))
            if links is not None:
                entry = self.add_to_pool(os.path.splitext(n)[0], clip)
                if entry:
                    links.append(entry)

        # the event's own name, whether or not it owns its clip
        if path and org.get('event_name_off'):
            self.set_string(root, org['event_name_off'],
                            item.name
                            or os.path.splitext(os.path.basename(path))[0])
        # an event's length and start offset are counted in samples of the
        # file it plays, so they follow that file's rate rather than the
        # donor's
        rate = file_rate or org.get('rate') or 48000.0
        pos, offset = item.pos, item.soffs * rate
        sr = getattr(self, 'project_rate', None)
        if sr and file_rate and abs(file_rate - sr) < 1.0 \
                and not os.environ.get('CPR_NO_SAMPLE_SNAP'):
            # Whole samples. An item that starts between two samples plays
            # its file from the nearest one in REAPER - the file offset is
            # rounded - and Cubase truncates the same fraction, so every
            # item whose fraction was over one half came out one sample
            # late (ZITRO's Snare: half its hits nulled at -75 dB, the rest
            # at -5). The start goes to REAPER's sample and the offset to
            # the file sample REAPER plays there, each a thousandth of a
            # sample over the whole number so that neither rounding nor
            # truncation can move it.
            p = item.pos * sr
            d = int(math.floor(item.soffs * sr - p + 0.5))
            pi = int(math.floor(p + 0.5))
            # a Melodyne event is placed where it says: Melodyne renders
            # from the region's own start, fraction and all (env test)
            nudge = (float(os.environ.get('CPR_ARA_NUDGE', '0.001'))
                     if getattr(item, 'ara_id', None) else 0.001)
            pos = (pi + nudge) / sr
            offset = pi + d + nudge
            if offset < 0:
                pos, offset = item.pos, item.soffs * rate
        if getattr(item, 'native_stretch', False) and org.get('warp_off') \
                and org.get('grid_off') and info:
            self.write_stretch(root, org, item, info, owns_clip=share is None)
            return
        self.set_f64(root, org['start'], self.tempo.ticks(pos))
        self.set_f64(root, org['length'], item.length * rate)
        self.set_f64(root, org['offset'], offset)

    def write_curve(self, root, proto_item, item):
        """The item's take volume envelope as the event's own volume curve.

        The donor's two TEMPLATE AUDIO events carry curves Cubase drew (23
        and 20 points, 2026-09-30); a point is rewritten where it lies - x
        in samples of the file, y as linear gain - the ones not needed are
        cut off the end, and the count set. The first point, which holds
        the record's class declaration, always stays: an event with no
        envelope keeps one point at unity, which plays as no curve.
        Returns True when the event got the item's envelope."""
        org = proto_item.origin or {}
        recs = org.get('curve_pts')
        if not recs:
            return False
        unit = org.get('curve_unit') or (1.0 / 48000.0)
        r = item.playrate or 1.0
        env = item.volenv if getattr(item, 'native_curve', False) else None
        if env:
            knots = envelope.cubase_points_for(env, 0.0, max(item.length, 1e-6),
                                               len(recs))
        else:
            knots = [(0.0, 1.0)]
        n = max(1, min(len(recs), len(knots)))
        for (t, g), (_hdr, ds) in zip(knots[:n], recs[:n]):
            x = (max(0.0, item.soffs) + t * r) / unit
            self.set_raw(root, ds, struct.pack('>dd', float(x), float(max(0.0, g))))
        if n < len(recs):
            a = recs[n][0] - self.base
            z = org['curve_end'] - self.base
            node = find_in(root, org['curve_node'] - self.base)
            node.splice(a, z, b'')
        self.set_raw(root, org['curve_node'] + 16, struct.pack('>I', n))
        return bool(env)

    @staticmethod
    def marker_map(item, dur):
        """REAPER's stretch markers as a map source second -> item second.

        SM pairs are (position, source position), the position in take time
        - item seconds times the item's playrate - and between markers the
        source runs in a straight line; outside them at the item's own rate
        (measured on a burst probe, 2026-09-30: markers 1 1 + 2 1.5 put the
        source's 1.3 s burst 1.61 s into the item). Returns [(source s, item
        s)] at 0, at every marker inside the file and at `dur`, or None when
        the markers do not run forward."""
        r = item.playrate or 1.0
        mk = sorted(item.stretch_markers)
        # two markers a hair apart in take time make a jump in the source
        # (Music 2's song: 0 -> 3.9288 s and 1 us -> 3.9740 s); REAPER plays
        # on from the second, and Cubase, which cannot hold a jump, merged
        # the two and ran the whole first stretch 0.2 % off (37 ms behind
        # 10 s in). The pair becomes its second marker.
        keep = []
        for a in mk:
            if keep and a[0] - keep[-1][0] < 1e-3:
                keep[-1] = a
            else:
                keep.append(a)
        mk = keep
        if any(b[0] <= a[0] or b[1] <= a[1] for a, b in zip(mk, mk[1:])):
            return None

        def tau_of(s):
            if s <= mk[0][1]:
                return mk[0][0] - (mk[0][1] - s)
            if s >= mk[-1][1]:
                return mk[-1][0] + (s - mk[-1][1])
            for (ta, sa), (tb, sb) in zip(mk, mk[1:]):
                if sa <= s <= sb:
                    return ta + (tb - ta) * (s - sa) / (sb - sa)
            return mk[-1][0]
        pts = [0.0] + [s for _t, s in mk if 0.0 < s < dur] + [dur]
        return [(s, tau_of(s) / r) for s in pts]

    def _write_markers(self, root, org, item, info, owns_clip, t0, t1):
        """A stretch-marked item as a musical-mode event whose clip's warp
        scale carries one point per marker - Cubase's warp tabs. The clip
        runs at the project tempo, so its ticks are the project's and each
        point puts that source sample at the beat REAPER plays it at."""
        frames, _bits, _ch, frate = info
        dur = frames / float(frate)
        pm = self.marker_map(item, dur)
        bpm = self.tempo.bpm_at(item.pos)
        per_s = bpm / 60.0 * cpr_read.PPQ            # ticks per item second
        t_start = pm[0][1]                            # item time of file start
        self.set_f64(root, org['start'], t0)
        self.set_f64(root, org['length'], t1 - t0)
        self.set_f64(root, org['offset'], max(0.0, -t_start * per_s))
        if not owns_clip:
            return
        g = org['grid_off']
        beats = (pm[-1][1] - t_start) * bpm / 60.0
        self.set_raw(root, g + 8, struct.pack('>I', max(1, int(math.ceil(beats)))))
        self.set_raw(root, g + 12, struct.pack('>ff', bpm, bpm))
        n = len(pm)
        body = struct.pack('>I', n)
        for s, t in pm:
            body += struct.pack('>dd', s * frate, (t - t_start) * per_s)
        body += struct.pack('>4d', 1.0, 1.0, 1.0, 1.0) + bytes(8 * n)
        a = org['warp_off'] - self.base
        z = org['warp_end'] - self.base
        find_in(root, a).splice(a, z, body)
        if org.get('tape_off'):
            self.set_raw(root, org['tape_off'],
                         struct.pack('>q', 0 if item.preserve_pitch else 1))

    def write_stretch(self, root, org, item, info, owns_clip=True):
        """A stretched REAPER item as a Cubase event in musical mode, played
        by Cubase's own elastique rather than printed through ffmpeg.

        Musical mode plays the clip's beats at the project's tempo, so a clip
        whose tempo is T / r plays r times as fast at tempo T - REAPER's
        PLAYRATE r. The event's length and start offset are then counted in
        beats (480 ticks each) on both clocks at once; the clip's warp scale
        maps its first and last file sample to clip ticks; its grid holds the
        clip tempo. Preserve Pitch off is elastique Pro - Tape (tapeStyleMode
        1): the pitch follows the speed. The prototype is a clip Cubase 15
        itself put in musical mode on elastique Pro - Time (templates/
        donor.cpr's second TEMPLATE AUDIO event)."""
        frames, _bits, _ch, frate = info
        t0 = self.tempo.ticks(item.pos)
        t1 = self.tempo.ticks(item.pos + item.length)
        r = item.playrate or 1.0
        if getattr(item, 'stretch_markers', None):
            return self._write_markers(root, org, item, info, owns_clip, t0, t1)
        bpm = self.tempo.bpm_at(item.pos) / r
        per_s = bpm / 60.0 * cpr_read.PPQ            # clip ticks per file second
        self.set_f64(root, org['start'], t0)
        self.set_f64(root, org['length'], t1 - t0)
        self.set_f64(root, org['offset'], max(0.0, item.soffs) * per_s)
        if not owns_clip:
            return      # the clip is another event's, written with it
        g = org['grid_off']
        beats = frames / frate * bpm / 60.0
        self.set_raw(root, g + 8, struct.pack('>I', max(1, int(math.ceil(beats)))))
        self.set_raw(root, g + 12, struct.pack('>ff', bpm, bpm))
        w = org['warp_off']
        # u32 n (2), (0, 0), (last sample, its clip tick)
        self.set_raw(root, w + 4 + 16, struct.pack('>dd', float(frames),
                                                   frames / frate * per_s))
        if org.get('tape_off'):
            self.set_raw(root, org['tape_off'],
                         struct.pack('>q', 0 if item.preserve_pitch else 1))


class TempoMap:
    """The tempo the built project runs at, and how it counts ticks.

    Positions in a .cpr are ticks, so seconds only land where REAPER had
    them once the .cpr keeps REAPER's tempo. The map is built from the
    REAPER project rather than from the donor, and written into the file
    alongside the events that were placed with it."""

    def __init__(self, points):
        pts = sorted(points or [(0.0, 120.0)])
        if pts[0][0] > 0:
            pts.insert(0, (0.0, pts[0][1]))
        self.pts = []           # (seconds, bpm, ticks at that second)
        ticks = 0.0
        prev_s, prev_b = pts[0][0], pts[0][1]
        for sec, bpm in pts:
            if sec > prev_s:
                ticks += (sec - prev_s) * (prev_b / 60.0) * cpr_read.PPQ
            self.pts.append((sec, bpm, ticks))
            prev_s, prev_b = sec, bpm

    def ticks(self, seconds):
        sec0, bpm0, t0 = self.pts[0]
        for sec, bpm, t in self.pts:
            if seconds + 1e-9 >= sec:
                sec0, bpm0, t0 = sec, bpm, t
            else:
                break
        return t0 + (seconds - sec0) * (bpm0 / 60.0) * cpr_read.PPQ

    def bpm_at(self, seconds):
        bpm0 = self.pts[0][1]
        for sec, bpm, _t in self.pts:
            if seconds + 1e-9 >= sec:
                bpm0 = bpm
            else:
                break
        return bpm0

    def constant_over(self, a, b):
        """No tempo change strictly inside (a, b)."""
        return not any(a + 1e-6 < sec < b - 1e-6 for sec, _b, _t in self.pts)

    def records(self):
        """The map in the shape Cubase stores it in."""
        out = b''
        for sec, bpm, t in self.pts:
            out += (struct.pack('>f', 60.0 / bpm) + struct.pack('>d', sec)
                    + struct.pack('>d', t) + struct.pack('>H', 0))
        return out, len(self.pts)


def s2t(reader, seconds):
    """Seconds to Cubase ticks, following the donor's tempo map."""
    pts = reader.tempo_pts
    base_t, base_s, spq = pts[0]
    for t, s, sq in pts:
        if seconds + 1e-9 >= s:
            base_t, base_s, spq = t, s, sq
        else:
            break
    rate = cpr_read.PPQ / spq if spq > 0 else 960.0
    return base_t + (seconds - base_s) * rate


# Cubase's Project Setup > Stereo Pan Law codes. 0 dB and Equal Power were
# measured (the setting changed in Cubase, the saves diffed); the three
# attenuating laws sit before 0 dB in Cubase's menu (-6, -4.5, -3 dB) and
# are assumed to count up to it.
PAN_LAW_CODES = {0.0: (4, True), -3.0: (3, False), -4.5: (2, False),
                 -6.0: (1, False)}


def pan_law_code(panlaw):
    """REAPER's PANLAW (gain at centre) -> (Cubase code, measured?) or
    (None, False) when there is nothing to write."""
    if panlaw is None or panlaw <= 0:
        return None, False
    db = 20 * math.log10(panlaw)
    best = min(PAN_LAW_CODES, key=lambda k: abs(k - db))
    if abs(best - db) > 0.8:
        return None, False
    return PAN_LAW_CODES[best]


def write_master_volenv(B, new, log):
    """REAPER's master volume envelope onto the donor's Stereo Out lane.

    The output bus lives in the donor's Input/Output folder as a track of
    its own with one automation lane (Volume) and no points. The lane is
    filled the way a track's is (plan_env with allow_empty, points copied
    from the prototype, the count rewritten), on the original record, since
    the output bus is never copied. Returns the points written."""
    m = new.master
    if m is None or len(m.volenv or []) < 2:
        return 0
    so = None
    for t in B.donor.tracks:
        if t.name == 'Stereo Out' and t.kind == 'other':
            so = t
            break
    if so is None:
        log.append('the donor has no Stereo Out track record to put the master '
                   'volume envelope on')
        return 0
    lanes = [ln for ln in (so.origin.get('auto_lanes') or [])
             if ln.get('kind') in (None, 'volume', 'fader', 'channel')]
    if not lanes:
        log.append('the donor\'s Stereo Out has no automation lane to fill')
        return 0
    node = B.index.get(so.src.get('sf')) if so.src else None
    if node is None:
        node = B.root
    try:
        plan = B.plan_env(node, lanes[0], m.volenv, allow_empty=True)
        if not plan or not plan['slots']:
            return 0
        n = B.write_volenv(plan, m.volenv)
        # the donor's output bus has automation Read off (its lane never
        # had a point); the lane only plays with it on
        B.set_automation_read(node, so, True)
        # and the lane shown, so the rides are in view
        B.show_automation(node, so, True)
        return n
    except Exception as e:
        log.append('the master volume envelope could not be written: %s' % e)
        return 0


def mute_tracks_by_events(new, log):
    """With CPR_MUTE_EVENTS set, a muted REAPER track also arrives with
    all of its events muted.

    A muted track's channel is muted the way Cubase does it (SoloFlag,
    see Builder.write_mute) - the M button lit, the events untouched, as
    in REAPER. Muting the events was the way before the channel's mute
    was found, and is kept behind CPR_MUTE_EVENTS for a project where a
    channel's mute would not take. A muted folder's tracks are no longer
    muted along with it: they play into the muted group exactly as they
    play into REAPER's muted folder, so a stem of one has the same audio
    in both hosts and only the folder's sum is silent."""
    if not os.environ.get('CPR_MUTE_EVENTS'):
        return 0
    inside = set()
    n_tracks = n_events = 0
    names = []
    for t in new.tracks:
        if not t.mute or t.is_folder:
            continue
        live = [i for i in t.items if i.kind != 'empty' and not i.mute]
        if not live:
            continue
        for i in live:
            i.mute = 1
        n_tracks += 1
        n_events += len(live)
        names.append(t.name)
    if n_tracks:
        log.append('%d muted REAPER track(s) arrive with all %d of their '
                   'event(s) muted as well as their channel (CPR_MUTE_EVENTS): '
                   '%s. Unmute the parts in Cubase to hear them'
                   % (n_tracks, n_events, ', '.join(names)))
    return n_tracks


def idle_lanes_for_cubase(new, log):
    """Lanes that do not play (REAPER envelopes with ACT 0, or a Cubase
    track whose automation Read was off) become the track's lanes with
    Cubase's automation Read switched off for the track, so Cubase keeps
    and draws them and plays the fader. Read is one switch per track in
    Cubase and ACT one per envelope in REAPER: a track with both kinds
    keeps the ones that play, and the others are named in the summary."""
    for t in new.tracks:
        fxs = ([t.instrument] if t.instrument is not None else []) + list(t.fx)
        idle = bool(getattr(t, 'volenv_idle', None) or getattr(t, 'panenv_idle', None)
                    or any(getattr(f, 'envelopes_idle', None) for f in fxs))
        if not idle:
            continue
        active = bool(t.volenv or t.panenv or any(f.envelopes for f in fxs))
        if active:
            log.append('%r: its envelopes that are switched off were left out - '
                       'Cubase has one automation Read switch per track, and '
                       'the ones that play are on' % t.name)
            continue
        t.volenv = list(t.volenv_idle or [])
        t.panenv = list(t.panenv_idle or [])
        for f in fxs:
            f.envelopes = list(getattr(f, 'envelopes_idle', None) or [])
        t._read_off = True


def curves_for_cubase(new):
    """Extra points on every envelope so Cubase's straight lines (in fader
    position, in pan position) play the model's (envelope.py)."""
    reaper_pans = getattr(new, 'pan_law_of', None) == 'reaper'
    law = new.panlaw if new.panlaw is not None else 1.0
    for t in new.tracks:
        if t.panenv and reaper_pans and getattr(t, 'panenv_law', None) != 'balance':
            from . import panlaw
            mode = t.panmode if t.panmode is not None else new.panmode
            # REAPER's panner boosts towards the sides (up to 3 dB at the
            # 0 dB law) and Cubase's does not: the level rides on the
            # volume lane, since the fader cannot follow the pan
            t.volenv = envelope.volume_with_pan_gain(
                t.volenv, t.vol, t.panenv,
                lambda p: panlaw.reaper_to_cubase(p, law, mode)[1])
            t.panenv = envelope.pan_curve(
                t.panenv,
                lambda p: panlaw.reaper_to_cubase(p, law, mode)[0],
                lambda c: panlaw.cubase_to_reaper(c, law, mode)[0])
        if t.volenv:
            t.volenv = envelope.volume_for_cubase(t.volenv)
    m = getattr(new, 'master', None)
    if m is not None and m.volenv:
        m.volenv = envelope.volume_for_cubase(m.volenv)


def write(new, path, donor=None, log=None):
    """Build a .cpr matching `new` and save it."""
    log = log if log is not None else []
    donor = donor or os.path.abspath(DONOR)
    if not os.path.exists(donor):
        raise SystemExit('no donor project to build from: %s' % donor)

    # what each part actually holds comes first: the splits below copy
    # parts onto the tracks they make, so they have to be right by then
    window_midi(new, log)
    from .model import split_video_audio
    split_video_audio(new, path, log)
    expand_for_cubase(new, log)
    mute_tracks_by_events(new, log)
    idle_lanes_for_cubase(new, log)
    curves_for_cubase(new)
    B = Builder(donor, log)
    B.project = new                 # for the pan mapping (cubase_mix)
    # A level past Cubase's fader (+6.02 dB) goes on Volume inserts at the
    # end of the chain - Cubase's own gain effect, its gain curve measured
    # (builtins.VOLUME_CURVE), +6.02 dB each at most: pre-fader, after the
    # other inserts, where REAPER's fader gain sits too. Banatul Dance's
    # pianos (+10.42 dB) arrived 4.4 dB quiet with the fader simply stopped
    # at +6.
    from .model import Fx as _Fx
    from . import builtins as _bi
    for t in new.tracks:
        v, _p = B.cubase_mix(t)
        if v > 2.0 + 1e-9:
            extra = v / 2.0
            for part in _bi.volume_split(extra):
                vfx = _Fx()
                vfx.name = 'Volume'
                vfx.uid = _bi.VOLUME_UID
                vfx.component = _bi.volume_state(part)
                vfx.fader_overflow = True
                t.fx = list(t.fx) + [vfx]
            log.append('%r: its level (%+.2f dB) is past Cubase\'s fader '
                       '(+6.02 dB); the fader is at +6.02 and a Volume insert '
                       'at the end of the chain adds the other %+.2f dB'
                       % (t.name, 20 * math.log10(v), 20 * math.log10(extra)))
    B.tempo = TempoMap(new.tempo)
    B.write_tempo(B.tempo)
    # REAPER's project offset is Cubase's Project Setup > Start, a double at
    # the head of PArrangeSetup (cpr_read.read_arrange_setup): the timeline
    # begins that long before bar 1 and every position still counts from
    # the timeline's start, so nothing else moves
    if abs(getattr(new, 'start', 0.0) or 0.0) > 1e-9:
        span = B.reader.arrange_setup()
        if span is not None:
            B.set_f64(B.root, B.base + span[0], float(new.start))
            log.append('the project starts %.3f s before bar 1 (REAPER\'s project '
                       'offset): Cubase\'s Project Setup > Start is set the same'
                       % -float(new.start))
    B.set_project_path(path)
    proto_audio, proto_inst, holder = B.prototypes()
    a_node = B.index[proto_audio.src['sf']]
    i_node = B.index[proto_inst.src['sf']]
    # a plain MIDI track, for REAPER tracks with parts but no instrument
    proto_midi = None if legacy('MIDIPROTO') else B.proto_midi
    m_node = B.index[proto_midi.src['sf']] if proto_midi is not None else None
    if proto_midi is None:
        log.append('the donor has no plain MIDI track, so a REAPER track '
                   'with parts but no instrument is copied from the '
                   'instrument track and arrives carrying the donor\'s synth')
    _aud = [i for i in proto_audio.items if i.kind == 'audio']
    # the donor's audio track holds a plain event and, next to it, one whose
    # clip Cubase put in musical mode on elastique: the prototype for every
    # stretched item (write_stretch)
    proto_warp_item = next((i for i in _aud if getattr(i, 'warped', False)
                            and (i.origin or {}).get('warp_off')
                            and (i.origin or {}).get('grid_off')), None)
    # the Melodyne donor's event with Melodyne on it (DONOR_MELODYNE)
    proto_ara_item = next((i for i in _aud if getattr(i, 'ara_id', None)
                           and i is not proto_warp_item), None)
    proto_audio_item = next((i for i in _aud if i is not proto_warp_item
                             and i is not proto_ara_item), _aud[0])
    ara_names = {}
    if proto_ara_item is not None and getattr(new, 'ara_from', None) == 'reaper':
        from . import ara as _ara
        try:
            B.ara_blob, ara_names = _ara.reaper_to_cubase(new.ara_docs, log)
        except Exception as e:
            log.append('the Melodyne edits could not be carried over (%s): '
                       'those takes play as recorded' % e)
    if os.environ.get('CPR_NO_NATIVE_STRETCH'):
        proto_warp_item = None
    midi_items = [i for i in proto_inst.items if i.kind == 'midi']
    proto_midi_item = midi_items[0] if midi_items else None
    # the same, taken from the plain MIDI prototype
    mp_items = ([i for i in proto_midi.items if i.kind == 'midi']
                if proto_midi is not None else [])
    mp_item = mp_items[0] if mp_items else None

    def part_proto(kind):
        """(the prototype track, its MIDI parts, the one to write into)."""
        if kind == 'midi':
            return proto_midi, mp_items, mp_item
        return proto_inst, midi_items, proto_midi_item

    for t_ in new.tracks:
        for f_ in ([t_.instrument] if t_.instrument else []) + list(t_.fx):
            f_._where = t_.name
    want = [t for t in new.tracks if not t.is_folder]

    # What the events will point at has to be something Cubase can play:
    # WAV, at the project's rate. Anything else is converted first, into the
    # project's Audio folder, the way Cubase's own importer would.
    # every item on every lane and take goes in: the other lanes become
    # track versions and the other takes events stacked under the comp
    if not os.environ.get('CPR_NO_LOOP_EXPAND'):
        from .model import expand_loops
        # a looped item on an MP3 (or any non-WAV) is cut at its file's end
        # as it decodes - REAPER wraps the decoded audio - so those files are
        # decoded first, the way prepare() would anyway, and measured as WAV
        _rate = new.samplerate or B.donor.samplerate or 48000.0
        _nonwav = [i for t in want for i in t.items
                   if i.kind == 'audio' and i.loop and i.file
                   and not i.section and not i.stretch_markers and not i.takefx
                   and os.path.isfile(media.resolve(i.file, new.srcdir))
                   and media.wav_duration(media.resolve(i.file, new.srcdir)) is None]
        if _nonwav:
            media.prepare(new, path, _rate, wav_info, log, items=_nonwav,
                          skip_printable=False)

        def _dur(f):
            f = media.resolve(f, new.srcdir)
            return media.wav_duration(f) if os.path.isfile(f) else None
        expand_loops(new, log, _dur)
    play = [i for t in want for i in t.items]
    n_media = sum(1 for i in play if i.kind in ('audio', 'video') and i.file)
    if n_media:
        progress.stage('checking %d media file(s)' % n_media)
    rate = new.samplerate or B.donor.samplerate or 48000.0
    B.project_rate = rate
    chans = None
    for _it in proto_audio.items:
        if _it.kind == 'audio' and (_it.origin or {}).get('channels'):
            chans = int(_it.origin['channels'])
            break
    media.relink(new, path, log, items=play)
    media.reverse_sections(new, path, log, items=play)
    # Stretched items Cubase can play natively: a stretch and nothing else
    # that needs a print, under one tempo (musical mode follows the tempo
    # map; a REAPER item on time base does not)
    # A transposed item goes the same way at rate 1: Cubase's Transpose on a
    # plain clip runs its Standard algorithm, which put the stretch probe's
    # bursts 10 ms (-3 st) to 57 ms (+12 st) late; on the elastique clip the
    # +2 st item landed within 0.4 ms of REAPER (2026-09-30)
    # A take volume envelope is the event's own volume curve when the donor
    # event has one to rewrite (write_curve); the item is then printed only
    # for whatever else it carries
    if (proto_audio_item.origin or {}).get('curve_pts')             and not os.environ.get('CPR_NO_NATIVE_CURVE'):
        n_curve = 0
        for i in play:
            if i.kind == 'audio' and i.volenv and (
                    len(i.volenv) > 1 or abs(i.volenv[0][1] - 1.0) > 1e-4):
                i.native_curve = True
                n_curve += 1
        if n_curve:
            log.append('%d item volume envelope(s) arrive as the Cubase '
                       'event\'s own volume curve, fitted to Cubase\'s curve '
                       'scale within 0.05 dB (up to 20 points each) - nothing '
                       'printed for them' % n_curve)
    n_native = 0
    if proto_warp_item is not None:
        for i in play:
            if i.kind != 'audio':
                continue
            if abs((i.playrate or 1.0) - 1.0) <= 1e-6 and not i.pitch                     and not i.stretch_markers:
                continue
            if i.stretch_markers:
                f_ = media.resolve(i.file, new.srcdir) if i.file else None
                d_ = media.wav_duration(f_) if f_ and os.path.isfile(f_) else None
                if d_ is None and f_ and os.path.isfile(f_):
                    # an MP3: its length as prepare() decodes it, which is
                    # REAPER's timeline (media.mp3_decode_args)
                    d_ = media.decoded_duration(f_, '1' in [str(a) for a in (
                        getattr(i, 'file_args', None) or [])])
                if not d_ or B.marker_map(i, d_) is None:
                    continue
            # the clip's warp scale is counted in the file's samples: a file
            # that is not on this machine has none to count, and its event
            # stays plain (Cherry Link's stems, named on another computer,
            # came out 50 times too long as musical events)
            if not i.file or not os.path.isfile(media.resolve(i.file, new.srcdir)):
                continue
            i.native_stretch = True
            if media.needs_render(i) or not B.tempo.constant_over(
                    i.pos, i.pos + i.length):
                i.native_stretch = False
            else:
                n_native += 1
    if n_native:
        log.append('%d stretched or transposed item(s) arrive as Cubase '
                   'events in musical mode on elastique Pro (Tape where REAPER '
                   'had Preserve Pitch off): Cubase does the stretch and the '
                   'transpose, nothing is printed'
                   % n_native)
    if os.environ.get('CPR_KEEP_CHANNELS'):
        # write_audio describes each file's own channel count and block
        # align in the clip record; the copy to the donor's count is then
        # not needed (and it changes what Melodyne analysed, ara.py)
        chans = None
    media.prepare(new, path, rate, wav_info, log, items=play, channels=chans)
    media.render(new, path, rate, wav_info, log, items=play, channels=chans)
    # whatever could not be printed still needs a file Cubase can play
    left = [i for i in play if i.kind == 'audio' and media.needs_render(i)]
    if left:
        media.prepare(new, path, rate, wav_info, log, items=left,
                      channels=chans, skip_printable=False)

    progress.stage('building %d track(s) from %s'
                   % (len(want), os.path.basename(donor)))
    bar = progress.Progress(len(want), 'writing Cubase project', 'tracks')

    stats = {'audio_tracks': 0, 'instrument_tracks': 0, 'events': 0,
             'parts': 0, 'notes': 0, 'plugins': 0, 'skipped': 0,
             'markers': 0}

    # The first track of each kind is written into the donor's own record
    # rather than beside it. A donor track that has been reused is not a
    # leftover to remove afterwards, and removing one is only safe when
    # nothing else in the project still points at it.
    # Which project tracks take the donor's own two records, and where
    # every copy goes so the list comes out in the project's order: a copy
    # can be placed in front of either of them, so two fixed records are
    # enough to reproduce any order that keeps them the right way round.
    # Every track is a copy. Writing into the donor's own record instead -
    # which would save a copy and put a track where the donor had it -
    # produces a file Cubase calls invalid, so the donor's records are read
    # from and never written to.
    # A REAPER folder becomes a Cubase folder track, and Cubase keeps a
    # folder's tracks inside it rather than beside it, so each one is put
    # into the list its folder carries.
    folder_track, folder_node = B.folder_prototype()
    group_track, group_node = B.group_prototype()
    # a REAPER track that others send to is an aux; in Cubase it has to be a
    # channel of its own, since nothing can be sent to an audio track
    aux = set(s.dest for t in new.tracks for s in t.sends
              if s.dest is not None)
    bus_of = {}
    # a folder that sums carries its group channel as a track inside it;
    # that channel takes the folder's name too
    folder_group = B.folder_group(folder_node)
    folder_names = []
    if folder_node is not None:
        for nm in {folder_track.name, B.folder_group_name(folder_node)}:
            folder_names += B.name_offsets(folder_node, nm)
    allocs = []
    folders = []
    auxes = []
    videos = []
    pool_links = []
    order = dict((id(t), n) for n, t in enumerate(new.tracks))
    # Which project track is written into the donor's own record rather than
    # into a copy of it. A prototype that becomes a real track is not a
    # TEMPLATE left over at the end, and it cannot be deleted instead: the
    # audio one is pointed at by 2 records elsewhere in the project and the
    # instrument one by 69, so removing them leaves references into nothing.
    # Off by default until a converted project has been opened in Cubase: an
    # earlier attempt at this produced a file Cubase called invalid, which
    # was probably the object-registry bug since fixed, but probably is not
    # good enough to make it the behaviour everyone gets.
    reuse = {}
    consumed = set()
    if not legacy('CONSUME'):
        # The donor's own records cannot move: everything else is a copy
        # placed around them. So a prototype may only be given to a project
        # track whose position agrees with where that record already sits -
        # walk the kinds in the order the donor holds them and take, for
        # each, the next project track of that kind that comes after the one
        # before it. A kind with no such track keeps being copied.
        pos = {}
        for kind_, node in (('audio', a_node), ('inst', i_node),
                           ('midi', m_node)):
            if node is not None and node in holder.kids:
                pos[kind_] = holder.kids.index(node)
        plain = [t for t in new.tracks if not t.is_folder and t.depth == 0
                 and t.kind != 'video' and order.get(id(t)) not in aux]
        cursor = -1
        for kind_ in sorted(pos, key=lambda k: pos[k]):
            for n_, t in enumerate(plain):
                if n_ <= cursor:
                    continue
                if track_kind(t, m_node is not None) == kind_:
                    reuse[kind_] = id(t)
                    cursor = n_
                    break

    # The donor's records cannot move, so every track that is not written
    # into one has to be threaded around them: a copy is inserted in front
    # of the first consumed record that comes later in the project's order,
    # and only the copies after the last one are appended at the end.
    proto_of = {'audio': a_node, 'inst': i_node, 'midi': m_node}
    anchor_seq = sorted((order[tid], proto_of[k]) for k, tid in reuse.items())

    def anchored(t, where):
        idx = order.get(id(t), -1)
        for ci, node in anchor_seq:
            if ci > idx:
                return node
        return where

    stack = [(-1, holder, None, None)]
    for t in new.tracks:
        while len(stack) > 1 and t.depth <= stack[-1][0]:
            stack.pop()
        _d, into, where, into_bus = stack[-1]
        if t.is_folder:
            if folder_node is None:
                log.append('%r is a folder and the donor has none to copy, '
                           'so its tracks arrive alongside it' % t.name)
                continue
            f = cpr_tree.copy_subtree(folder_node)
            into.extra.append((anchored(t, where) if into is holder
                               else where, f))
            inner = node_at(f, folder_node.kids[0].ds) if folder_node.kids                 else None
            # each folder's group channel needs an id of its own, or two
            # folders would be the same destination
            # Each folder's group channel gets an id of its own, or two
            # folders are the same destination and everything sums into one.
            bus = (B.free_bus_id() if folder_track.origin.get('bus_id')
                   else folder_track.bus_id)
            folders.append((t, f, inner, bus, into_bus))
            if bus is not None:
                bus_of[order.get(id(t))] = bus
            if inner is not None:
                stack.append((t.depth, inner, 'end', bus))
            continue
        if t.kind == 'video':
            # Cubase keeps one video track per project; the events of any
            # further REAPER video track go on the same one
            vitems = [i for i in comp_items(t) if i.kind == 'video' and i.file]
            if videos:
                log.append('%r: Cubase has one video track per project, so '
                           'its %d video event(s) went onto %r'
                           % (t.name, len(vitems), videos[0][0].name))
                videos[0][2].extend(vitems)
                continue
            vnode = (B.graft_video() if vitems and not legacy('VIDEO')
                     else None)
            if vnode is None:
                if vitems:
                    log.append('%d video event(s) were left out - add the '
                               'video in Cubase with Project > Add Track > '
                               'Video' % len(vitems))
                continue
            into.extra.append((anchored(t, where) if into is holder
                               else where, vnode))
            videos.append((t, vnode, vitems))
            continue
        idx = order.get(id(t))
        if idx in aux and group_node is not None:
            # an aux: a channel of its own that others can be sent to
            dup = cpr_tree.copy_subtree(group_node)
            into.extra.append((anchored(t, where) if into is holder
                               else where, dup))
            bus = B.free_bus_id()
            bus_of[idx] = bus
            # An aux inside a folder still plays into that folder, exactly
            # as it does in REAPER: a send-return track sitting in an FX
            # folder feeds the folder, and the folder feeds the mix. This
            # path used to leave the output alone, so the channel went
            # straight to the master and the folder summed nothing - which
            # is a folder that looks like a folder and is not one.
            auxes.append((t, dup, bus, into_bus))
            continue
        kind = track_kind(t, m_node is not None)
        is_inst = kind != 'audio'
        proto_node = {'inst': i_node, 'midi': m_node, 'audio': a_node}[kind]
        own = reuse.get(kind) == id(t)
        if own:
            # the donor's own record, kept where it already sits in the list
            dup = proto_node
        else:
            dup = cpr_tree.copy_subtree(proto_node)
            into.extra.append((anchored(t, where) if into is holder
                               else where, dup))
        src_t = {'inst': proto_inst, 'midi': proto_midi,
                 'audio': proto_audio}[kind]
        # Every channel needs an id of its own. Left as the donor's, a copy
        # is a second channel claiming one id, and Cubase binds only the
        # first: the rest open with their automation lanes attached to
        # nothing - blank, and silent. The record the copies were taken from
        # keeps the id it already has, and is written into only after every
        # copy has been made - a copy carries its source's pending edits.
        if src_t.origin.get('bus_id') and not own:
            off, ty = src_t.origin['bus_id']
            B.set_typed(dup, off, ty, B.free_bus_id())
        plan = B.plan_volenv(dup, src_t, t.volenv)
        # pan needs a lane of its own, made by copying the volume lane the
        # donor was recorded with and pointing the copy at pan instead
        pplan = None
        if t.panenv:
            lane = B.clone_channel_lane(dup, src_t, B.pan_param())
            if lane is not None:
                pplan = B.plan_env(lane['node'], lane, t.panenv)
        # a plug-in parameter lane the same way, named after the insert
        # and the parameter it follows (values are the plug-in's own 0..1)
        fplans = []
        # inserts are "Inserts\Slot[ N]\<uid>-n"; the instrument's own
        # parameters "Slot\<uid>-n" (drawn by hand on a Hive track,
        # 2026-09-29)
        chain = [(('Slot' if k == 0 else 'Slot %d' % (k + 1)), fx)
                 for k, fx in enumerate(t.fx)]
        if t.instrument is not None:
            chain.append((None, t.instrument))
        for where, fx in chain:
            for pidx, pts in (fx.envelopes or []):
                if not pts or not fx.uid or fx.native:
                    continue
                # The parameter a lane follows is named by the word that
                # closes its record: 0x1069 + the plug-in's parameter index,
                # which is REAPER's index as well (Cubase 15, drawn by hand
                # on Pro-Q 4 and ValhallaDelay, 2026-09-29). The number in
                # the lane's name is only a counter of that plug-in's lanes
                # in the project, which Cubase renumbers on save. The word
                # after the tag is a type code: 5 on a bypass parameter, 4
                # on FabFilter's (flag kIsList set), 0 otherwise.
                from . import vst3params
                key = fx.uid.upper()
                B._fx_lanes = getattr(B, '_fx_lanes', {})
                B._fx_lanes[key] = B._fx_lanes.get(key, 0) + 1
                number = B._fx_lanes[key]
                tag = vst3params.TAG_BASE + int(pidx)
                kind_word = vst3params.type_word(fx.uid, int(pidx), fx.name)
                if os.environ.get('CPR_FX_TAG'):
                    tag = int(os.environ['CPR_FX_TAG'], 16)
                if os.environ.get('CPR_FX_TYPE'):
                    kind_word = int(os.environ['CPR_FX_TYPE'])
                if where is None:
                    nm = ('Slot\\%s-%d' % (fx.uid.upper(), number)).encode('ascii')
                else:
                    nm = ('Inserts\\%s\\%s-%d'
                          % (where, fx.uid.upper(), number)).encode('ascii')
                lane = B.clone_channel_lane(dup, src_t, FX_PARAM, name=nm,
                                            close=(tag, kind_word))
                if lane is None:
                    continue
                fp = B.plan_env(lane['node'], lane, pts)
                if fp:
                    fplans.append((fp, pts, fx.name, pidx))
        if own:
            consumed.add(id(dup))
        allocs.append((t, kind, dup, plan, pplan, into_bus, fplans))
    reused = set()

    for t, f, inner, bus, into_bus in folders:
        for off in (folder_names or [folder_track.src['name_off']]):
            B.set_string(f, off, t.name)
        # a folder inside a folder: its group channel plays into the outer
        # folder's, as a REAPER folder plays into its parent. Left on the
        # donor's Stereo Out, SuperThunderCrown's CROWN STOP skipped CROWN
        # STOP 01 and the outer folder came out 11 dB down.
        if into_bus is not None and folder_track.origin.get('out_bus'):
            o2, t2 = folder_track.origin['out_bus']
            B.set_typed(f, o2, t2, into_bus)
            stats['nested folders'] = stats.get('nested folders', 0) + 1
        oc = folder_track.src.get('own_count_off')
        if oc is not None and inner is not None:
            # the folder already holds its own group channel, and the
            # project's tracks were added after it
            n = len(inner.kids) + len(inner.extra)
            a = oc - B.base
            find_in(f, a).splice(a, a + 4, struct.pack('>I', n))

        if bus is not None and folder_track.origin.get('bus_id'):
            off, ty = folder_track.origin['bus_id']
            B.set_typed(f, off, ty, bus)
        B.write_mix(f, folder_track, t)
        if t.color and B.write_color(f, folder_track, t.color):
            stats['colours'] = stats.get('colours', 0) + 1

        # A REAPER folder sums what is inside it and can carry inserts and
        # sends of its own on that sum - a drum bus with a compressor and a
        # limiter on it, and a send to a reverb. The writing loop below
        # skips folders (`want` leaves them out), and nothing here used to
        # put those back, so a folder arrived as an empty container: the
        # whole drum bus lost its processing and its reverb, silently.
        if t.fx:
            n = B.write_slots(f, folder_track, t.fx)
            stats['plugins'] += n
            if n < len(t.fx):
                log.append('%r is a summing folder carrying %d insert(s) and '
                           '%d of them were written. %s\'s folder record '
                           'has %d insert slot(s) to write into - a folder '
                           'that is only a way of tidying the track list has '
                           'none, and its inserts cannot cross. Put them on '
                           'the folder in Cubase by hand: %s'
                           % (t.name, len(t.fx), n,
                              os.path.basename(B.donor_path),
                              len(folder_track.origin.get('slots') or []),
                              ', '.join(f_.name for f_ in t.fx)))
        n_s = 0
        if t.sends:
            n_s = B.write_sends(f, folder_track, t.sends, bus_of)
            stats['sends'] = stats.get('sends', 0) + n_s
            if n_s < len(t.sends):
                log.append('%r is a summing folder with %d send(s) and %d of '
                           'them were written - the rest go somewhere Cubase '
                           'cannot be sent to, or the donor\'s folder record '
                           'has no send slot to write into'
                           % (t.name, len(t.sends), n_s))
        B.clear_sends(f, folder_track, n_s)
        stats['folders'] = stats.get('folders', 0) + 1

    # Every copy is taken before a word is written into any of them: a copy
    # carries whatever edits are pending on its source, so cloning the donor
    # track after the first project track had been written into it would give
    # every later track the first one's name and music.
    # the aux channels: name, level and whatever effects they carry
    aux_names = (B.name_offsets(group_node, group_track.name)
                 if group_node is not None else [])
    # An audio or instrument track spells its name in more than one record
    # too - the track, its channel, the mixer's copy - and writing only the
    # first left every converted track still reading the donor's name in the
    # rest. Folders and aux channels already did this; these did not.
    def track_name_offsets(node, t):
        offs = B.name_offsets(node, t.name)
        parts = set((i.origin or {}).get('part_name_off') for i in t.items)
        return [o for o in offs if o not in parts]

    audio_names = ([] if legacy('NAMES') else
                   track_name_offsets(a_node, proto_audio)
                   if a_node is not None else [])
    inst_names = ([] if legacy('NAMES') else
                  track_name_offsets(i_node, proto_inst)
                  if i_node is not None else [])
    midi_names = (track_name_offsets(m_node, proto_midi)
                  if m_node is not None else [])
    def routed_output(t):
        """(bus, send) when a REAPER track feeds only a group: its parent
        send off and one unity, post-fader, centred send to an aux. In
        Cubase that is the channel's output, not a send - written as a send,
        the track also kept playing through its folder and was heard twice
        (SuperThunderCrown back to Cubase: the master 3 dB hot, folders up
        to 7 dB)."""
        if getattr(t, 'main_send', True):
            return None
        for s in t.sends:
            bus = bus_of.get(s.dest)
            if (bus is not None and s.mode == 0 and abs(s.vol - 1.0) < 1e-4
                    and abs(s.pan) < 1e-4):
                return bus, s
        return None

    for t, dup, bus, into_bus in auxes:
        r = routed_output(t)
        if r:
            into_bus = r[0]
            t.sends = [s for s in t.sends if s is not r[1]]
            stats['outputs to groups'] = stats.get('outputs to groups', 0) + 1
        for off in (aux_names or [group_track.src['name_off']]):
            B.set_string(dup, off, t.name)
        off, ty = group_track.origin['bus_id']
        B.set_typed(dup, off, ty, bus)
        if into_bus is not None and group_track.origin.get('out_bus'):
            o2, t2 = group_track.origin['out_bus']
            B.set_typed(dup, o2, t2, into_bus)
        B.write_mix(dup, group_track, t)
        if t.color and B.write_color(dup, group_track, t.color):
            stats['colours'] = stats.get('colours', 0) + 1
        if t.fx:
            stats['plugins'] += B.write_slots(dup, group_track, t.fx)
        B.clear_sends(dup, group_track, 0)
        stats['aux'] = stats.get('aux', 0) + 1

    for t, kind, dup, plan, pplan, into_bus, fplans in allocs:
        is_inst = kind != 'audio'
        bar.step(note=t.name[:24])

        src_t = {'inst': proto_inst, 'midi': proto_midi,
                 'audio': proto_audio}[kind]
        for off in (({'inst': inst_names, 'midi': midi_names,
                      'audio': audio_names}[kind])
                    or [src_t.src['name_off']]):
            B.set_string(dup, off, t.name)
        B.write_mix(dup, src_t, t)
        if t.color and B.write_color(dup, src_t, t.color):
            stats['colours'] = stats.get('colours', 0) + 1
        r = routed_output(t)
        if r:
            into_bus = r[0]
            t.sends = [s for s in t.sends if s is not r[1]]
            stats['outputs to groups'] = stats.get('outputs to groups', 0) + 1
        n_s = 0
        if t.sends:
            n_s = B.write_sends(dup, src_t, t.sends, bus_of)
            stats['sends'] = stats.get('sends', 0) + n_s
            if n_s < len(t.sends):
                log.append('%r: %d of %d send(s) written - the rest go '
                           'somewhere Cubase cannot be sent to'
                           % (t.name, n_s, len(t.sends)))
        B.clear_sends(dup, src_t, n_s)
        # a track inside a folder plays into that folder's group channel,
        # the way a REAPER track plays into the folder that holds it
        if into_bus is not None and src_t.origin.get('out_bus'):
            off, ty = src_t.origin['out_bus']
            B.set_typed(dup, off, ty, into_bus)
        # Deleting the donor's lanes makes Cubase drop the track on opening
        # (something in the channel still points at them), so this stays
        # off unless asked for with CPR_ON_DROPLANES=1.
        # off: zeroing the lane count made Cubase drop the whole track, and
        # deleting the lane records did the same. CPR_ON_DROPLANES=1 to retry.
        if not legacy('FOLDLANES'):
            # a track with no automation shows no lane, as in REAPER; one
            # with automation shows its lanes
            fx_auto = any(f.envelopes for f in
                          ([t.instrument] if t.instrument is not None else []) + list(t.fx))
            if not (t.volenv or t.panenv or fx_auto):
                B.fold_automation(dup, src_t)
        if (not t.volenv and not t.panenv
                and os.environ.get('CPR_ON_DROPLANES')):
            if B.drop_automation(dup, src_t):
                plan = None         # the lane its point count lived in is gone
        stats['automation'] = stats.get('automation', 0) + \
            B.write_volenv(plan, t.volenv)
        # a pan lane holds the pan that gives Cubase the same L/R ratio
        # (panlaw.py); the level that goes with it rides on the volume lane
        penv = t.panenv
        if penv and getattr(new, 'pan_law_of', None) == 'reaper' \
                and getattr(t, 'panenv_law', None) != 'balance':
            # the level that goes with each pan already rides on the
            # volume lane (curves_for_cubase); the lane gets the position
            from . import panlaw
            law = new.panlaw if new.panlaw is not None else 1.0
            mode = t.panmode if t.panmode is not None else new.panmode
            penv = [(s, panlaw.reaper_to_cubase(v, law, mode)[0])
                    for s, v in t.panenv]
        stats['pan automation'] = stats.get('pan automation', 0) + \
            B.write_volenv(pplan, penv, tonorm=pan_to_norm)
        for fp, pts, fxname, pidx in fplans:
            n = B.write_volenv(fp, pts, tonorm=lambda v: float(v))
            stats['plug-in automation'] = stats.get('plug-in automation', 0) + n
        if getattr(t, '_read_off', False):
            # the lanes are there to keep, not to play (idle_lanes_for_cubase)
            if B.set_automation_read(dup, src_t, False):
                stats['lanes with Read off'] = stats.get('lanes with Read off', 0) + 1
        elif t.volenv or t.panenv:
            # lanes that play: Read on and in view, whatever the donor had
            B.set_automation_read(dup, src_t, True)
            B.show_automation(dup, src_t, True)

        # the insert strip: one slot per effect, the rest left empty
        if t.fx:
            n = B.write_slots(dup, src_t, t.fx)
            stats['plugins'] += n
            if n < len(t.fx):
                log.append('%r: %d of %d insert effect(s) written'
                           % (t.name, n, len(t.fx)))

        # The events of the lane REAPER plays go on the track itself, every
        # take of a multi-take item among them with the one that plays on
        # top. Every other lane becomes a track version: a list of its own
        # inside the track, holding copies of the same event record.
        if is_inst:
            stats['instrument_tracks'] += 1
            pr_t, pr_items, pr_item = part_proto(kind)
            if (kind == 'inst' and t.instrument is not None
                    and proto_inst.instrument is not None):
                inst = t.instrument
                if not inst.uid or getattr(inst, 'native', False):
                    log.append('%r: its instrument %r has no identity Cubase '
                               'could load; the track got HALion Sonic instead'
                               % (t.name, inst.name))
                    inst = stock_instrument()
                B.write_plugin(dup, proto_inst.instrument, inst)
                stats['plugins'] += 1
            # an instrument or MIDI track has nowhere to put an audio
            # event; split_mixed moves them off, so anything left here is a
            # loss and says so rather than going quietly
            stranded = [i for i in comp_items(t) if i.kind == 'audio']
            if stranded:
                log.append('%r is an instrument track and %d audio event(s) '
                           'on it could not be written - an instrument track '
                           'has nowhere to hold one. This should not happen; '
                           'the audio belongs on a track of its own'
                           % (t.name, len(stranded)))
                stats['skipped'] += len(stranded)
            parts = stacked([i for i in comp_items(t) if i.kind == 'midi'])
            versions = [(nm, [i for i in items if i.kind == 'midi'])
                        for _k, nm, items in other_lanes(t)]
            if legacy('VERSIONS'):
                versions = []
            # the donor track brings its own parts along; keep one to write
            # into and drop the rest, or the copy plays the donor's music
            spare = [i for i in pr_items if i is not pr_item]
            for sp in spare:
                if sp.origin:
                    find_in(dup, sp.origin['flags'] - B.base).deleted = True
            # the surviving donor part, matched by where its data starts
            part_node = part_holder = None
            if pr_item is not None and pr_item.origin:
                want_ds = pr_item.origin['flags'] - B.base
                for n_ in cpr_tree.walk_nodes(dup):
                    for k in n_.kids:
                        if k.ds == want_ds:
                            part_node, part_holder = k, n_
                            break
                    if part_node is not None:
                        break
            after = None
            if part_holder is not None and part_node in part_holder.kids:
                i = part_holder.kids.index(part_node)
                if i + 1 < len(part_holder.kids):
                    after = part_holder.kids[i + 1]
            # copies first, contents second - a copy carries whatever
            # edits are already pending on the node it came from
            slots = []
            if parts and part_node is not None:
                slots = [part_node]
                for _ in parts[1:]:
                    cp = cpr_tree.copy_subtree(part_node)
                    part_holder.extra.append((after, cp))
                    slots.append(cp)
            elif parts:
                stats['skipped'] += len(parts)
            vslots = []
            for vname, vitems in versions:
                lst = (B.new_variation(dup, pr_t, vname, t.name, len(vitems))
                       if part_node is not None else None)
                if lst is None:
                    stats['skipped'] += len(vitems)
                    continue
                stats['versions'] = stats.get('versions', 0) + 1
                for it in vitems:
                    cp = cpr_tree.copy_subtree(part_node)
                    lst.extra.append(('end', cp))
                    vslots.append((cp, it))
            if versions and part_node is not None:
                B.rename_active_version(dup, pr_t, t.lane_names[t.active_lane])
            if not parts and part_node is not None:
                # an instrument track with nothing on it: the donor's own
                # part would otherwise play on it
                part_node.deleted = True
            for slot, pt in list(zip(slots, parts)) + vslots:
                B.write_part_name(slot, pr_item, pt.name or t.name)
                n = B.write_notes(slot, pr_item, pt.notes, pt.ppq,
                                  pt.ccs)
                B.set_f64(slot, pr_item.origin['start'],
                          B.tempo.ticks(pt.pos))
                B.set_f64(slot, pr_item.origin['length'],
                          B.tempo.ticks(pt.pos + pt.length)
                          - B.tempo.ticks(pt.pos))
                B.write_event_flags(slot, pr_item, pt)
                stats['parts'] += 1
                stats['notes'] += n
            oc = pr_t.src.get('own_count_off')
            if oc is not None:
                a = oc - B.base
                find_in(dup, a).splice(a, a + 4,
                                       struct.pack('>I', len(slots)))
        else:
            stats['audio_tracks'] += 1
            events = stacked([i for i in comp_items(t) if i.kind == 'audio'])
            versions = [(nm, [i for i in items if i.kind == 'audio'])
                        for _k, nm, items in other_lanes(t)]
            if legacy('VERSIONS'):
                versions = []
            # the donor track carries one event; copy it for the rest so a
            # track with five regions arrives with five. The event object
            # itself is matched by where its data starts.
            want_ds = proto_audio_item.origin['flags'] - B.base
            want_ws = (proto_warp_item.origin['flags'] - B.base
                       if proto_warp_item is not None else None)
            want_as = (proto_ara_item.origin['flags'] - B.base
                       if proto_ara_item is not None else None)
            ev_node = holder_node = warp_node = ara_node = None
            for n in cpr_tree.walk_nodes(dup):
                for k in n.kids:
                    if k.ds == want_ds and ev_node is None:
                        ev_node, holder_node = k, n
                    elif want_ws is not None and k.ds == want_ws:
                        warp_node = k
                    elif want_as is not None and k.ds == want_as:
                        ara_node = k
                if ev_node is not None and (want_ws is None
                                            or warp_node is not None)                         and (want_as is None or ara_node is not None):
                    break
            if ara_node is not None and (holder_node is None
                                         or ara_node not in holder_node.kids):
                ara_node = None
            if ev_node is None:
                log.append('%r: could not find the event to copy' % t.name)
            if warp_node is not None and (holder_node is None
                                          or warp_node not in holder_node.kids):
                warp_node = None
            # events are read one after another, so a copy has to sit
            # next to the original rather than after everything else
            def after_of(node):
                if holder_node is not None and node in holder_node.kids:
                    i = holder_node.kids.index(node)
                    if i + 1 < len(holder_node.kids):
                        return holder_node.kids[i + 1]
                return None
            after = after_of(ev_node)
            # Copy first, write second: copy_subtree carries the source
            # node's pending edits with it, so cloning after writing gave
            # every copy the first event's changes on top of its own.
            # A stretched item copies the musical-mode prototype instead.
            native = [i for i in events if getattr(i, 'native_stretch', False)]
            if native and warp_node is None:
                for i in native:
                    i.native_stretch = False
                native = []
            # a take with Melodyne copies the Melodyne prototype (its own
            # GUIDs are written by write_ara); REAPER's stretch on such a
            # take is left to Melodyne's timing
            mel = [i for i in events if ara_node is not None
                   and ara_names.get(getattr(i, 'ara_id', None) or '')]
            for i in mel:
                if i in native:
                    native.remove(i)
                    i.native_stretch = False
            plain = [i for i in events if i not in native and i not in mel]
            slots = []
            if events and ev_node is not None:
                order = plain + native + mel
                for kind_events, node in ((plain, ev_node), (native, warp_node),
                                          (mel, ara_node)):
                    if not kind_events:
                        continue
                    slots.append(node)
                    for _ in kind_events[1:]:
                        copy_ev = cpr_tree.copy_subtree(node)
                        holder_node.extra.append((after_of(node), copy_ev))
                        slots.append(copy_ev)
                events = order
                if not plain:
                    ev_node.deleted = True
            elif events:
                stats['skipped'] += len(events)
            if warp_node is not None and not native:
                warp_node.deleted = True
            if ara_node is not None and not mel:
                ara_node.deleted = True
            stats['stretched events'] = (stats.get('stretched events', 0)
                                         + len(native))
            vslots = []
            for vname, vitems in versions:
                lst = (B.new_variation(dup, proto_audio, vname, t.name,
                                       len(vitems))
                       if ev_node is not None else None)
                if lst is None:
                    stats['skipped'] += len(vitems)
                    continue
                stats['versions'] = stats.get('versions', 0) + 1
                for it in vitems:
                    src_node = ev_node
                    if getattr(it, 'native_stretch', False):
                        if warp_node is not None:
                            src_node = warp_node
                        else:
                            it.native_stretch = False
                    cp = cpr_tree.copy_subtree(src_node)
                    lst.extra.append(('end', cp))
                    vslots.append((cp, it))
            if versions and ev_node is not None:
                B.rename_active_version(dup, proto_audio,
                                        t.lane_names[t.active_lane])
            if not events and ev_node is not None:
                # an audio track with nothing on it: the donor's own event
                # would otherwise be playing on it
                ev_node.deleted = True
            for serial, (slot, item) in enumerate(list(zip(slots, events))
                                                  + vslots, 1):
                pr = (proto_warp_item if getattr(item, 'native_stretch', False)
                      else proto_ara_item if item in mel
                      else proto_audio_item)
                B.write_audio(slot, pr, item, B.reader,
                              new.srcdir, pool_links)
                B.write_event_flags(slot, pr, item)
                B.write_gain(slot, pr, item)
                if pr is proto_ara_item and proto_ara_item is not None:
                    if B.write_ara(slot, pr, ara_names[item.ara_id],
                                   str(uuid.uuid4()).upper()):
                        stats['Melodyne events'] = stats.get('Melodyne events', 0) + 1
                B.write_zorder(slot, pr, item, serial)
                if B.write_pitch(slot, pr, item):
                    stats['pitched events'] = stats.get('pitched events', 0) + 1
                if B.write_curve(slot, pr, item):
                    stats['volume curves'] = stats.get('volume curves', 0) + 1
                stats['events'] += 1
                stats['fades'] = (stats.get('fades', 0)
                                  + B.write_fades(slot, pr, item))
            oc = proto_audio.src.get('own_count_off')
            if oc is not None:
                a = oc - B.base
                find_in(dup, a).splice(a, a + 4,
                                       struct.pack('>I', len(slots)))
    bar.done()

    for t, vnode, vitems in videos:
        n_v = B.write_video_track(vnode, t, vitems, pool_links)
        stats['video_events'] = stats.get('video_events', 0) + n_v
        if n_v:
            B.extra_registered.append(vnode)

    # ---- markers -------------------------------------------------------
    # The donor carries one marker of each kind. A project with forty of them
    # gets forty copies, the same way a track with five regions does: the
    # record Cubase wrote is cloned and only its name and times rewritten.
    donor_marks = B.donor.markers
    mlist = getattr(B.reader, 'marker_list', None)
    if donor_marks and mlist:
        proto_pt = next((m for m in donor_marks
                         if m.src.get('len_off') is None), None)
        proto_rg = next((m for m in donor_marks
                         if m.src.get('len_off') is not None), None)
        nodes = {}
        mholder = None
        for m in donor_marks:
            nodes[id(m)] = cpr_tree.find_node(B.root, m.src['ds'] - B.base)
        for n in cpr_tree.walk_nodes(B.root):
            if any(nodes[id(m)] in n.kids for m in donor_marks):
                mholder = n
                break
        used = set()
        written = 0
        if new.markers:
            progress.stage('writing %d marker(s)' % len(new.markers))
        # Every copy is made before anything is written into one: a copy
        # carries the pending edits of what it was copied from, so cloning
        # after writing gives each marker the previous one's name and time.
        plan = []
        for mk in new.markers:
            want_range = mk.end is not None and mk.end > mk.start
            proto = (proto_rg or proto_pt) if want_range else (proto_pt
                                                               or proto_rg)
            if proto is None:
                stats['skipped'] += 1
                continue
            base_node = nodes[id(proto)]
            if id(proto) not in used:
                node = base_node
                used.add(id(proto))
            elif mholder is None:
                stats['skipped'] += 1
                continue
            else:
                node = cpr_tree.copy_subtree(base_node)
                mholder.extra.append((None, node))
            plan.append((mk, proto, node, want_range))
        # Cubase shows a number on every marker, counting point markers and
        # cycle markers separately from 1. A copy of the donor's marker
        # carries the donor's number, so a project with five regions arrived
        # with five regions all called [1]. REAPER's own number is kept
        # where it has one, so region 4 stays region 4.
        counts = {}
        used_ids = {}
        for mk, proto, node, want_range in plan:
            src = proto.src
            B.set_string(node, src['name_off'], mk.name or '')
            B.set_f64(node, src['start_off'], B.tempo.ticks(mk.start))
            if src.get('id_off') is not None:
                kind = 'range' if src.get('len_off') is not None else 'point'
                have = used_ids.setdefault(kind, set())
                mid = mk.rid if (mk.rid and mk.rid not in have) else None
                if mid is None:
                    mid = counts.get(kind, 0) + 1
                    while mid in have:
                        mid += 1
                counts[kind] = max(counts.get(kind, 0), mid)
                have.add(mid)
                a = src['id_off'] - B.base
                find_in(node, a).splice(a, a + 4, struct.pack('>i', mid))
            if src.get('len_off') is not None:
                end_t = mk.end if want_range else mk.start
                B.set_f64(node, src['len_off'],
                          max(0.0, B.tempo.ticks(end_t)
                              - B.tempo.ticks(mk.start)))
            elif want_range:
                log.append('%r is a range; the donor has no cycle marker to '
                           'copy, so it arrives as a point'
                           % (mk.name or 'unnamed'))
            written += 1
        for m in donor_marks:
            if id(m) not in used:
                nodes[id(m)].deleted = True
        co = mlist['count_off'] - B.base
        cpr_tree.find_node(B.root, co).splice(co, co + 4,
                                              struct.pack('>I', written))
        stats['markers'] = written

    # ---- the donor's leftovers ----------------------------------------
    # What is left of the donor is whatever track was neither reused nor
    # needed: its spare MIDI tracks, its group, its instrument rack. Those
    # come out, but only together and only once the project has stopped
    # pointing at them - Cubase follows a reference while it loads and calls
    # the file invalid when one leads nowhere, so a set that still has a
    # reader stays instead of producing a project that will not open.
    progress.stage('removing the donor tracks that are no longer used')
    TRACKISH = ('MAudioTrackEvent', 'MInstrumentTrackEvent', 'MMidiTrackEvent',
                'MDeviceTrackEvent', 'MFolderTrack')
    # Cubase puts the markers, tempo and signature on tracks of their own,
    # and a donor may well keep them inside a folder. A folder holding one of
    # those stays, or the markers written into it go out with it.
    GLOBALISH = ('MMarkerTrackEvent', 'MTempoTrackEvent',
                 'MSignatureTrackEvent', 'MArrangerTrackEvent',
                 'MChordTrackEvent', 'MTransposeTrackEvent',
                 'MVideoTrackEvent', 'MRulerTrackEvent')
    # The records every copy was taken from stay. A copy carries its own
    # class declarations, but not the things a project keeps once and shares
    # - a group channel's bus among them - and removing the original leaves
    # the copies pointing at a bus that is no longer there.
    keep_nodes = set(reused) | set(consumed)
    # The records every copy was taken from used to be kept unconditionally,
    # which left a TEMPLATE track in every converted project. They are
    # offered up for removal instead: deletable() still has the last word, so
    # a prototype the rest of the project still points at stays regardless.
    # Set CPR_KEEP_PROTOTYPES=1 to get the old behaviour back.
    if os.environ.get('CPR_KEEP_PROTOTYPES') or legacy('DELPROTO'):
        for n in (a_node, i_node, folder_node, group_node):
            if n is not None:
                keep_nodes.add(id(n))
    if B.donor.master is not None and B.donor.master.src.get('sf'):
        m = B.index.get(B.donor.master.src['sf'])
        if m is not None:
            keep_nodes.add(id(m))

    spare = []
    for k in holder.kids:
        if k.deleted or k.clone or k.cls not in TRACKISH:
            continue
        if any(n.cls in GLOBALISH or id(n) in keep_nodes
               for n in cpr_tree.walk_nodes(k)):
            continue
        spare.append(k)

    ignore = (B.table['start'], B.table['end']) if B.table else None
    _cand = list(spare)
    spare = cpr_tree.deletable(B.A, spare, ignore, classes_ok=False)
    if os.environ.get('CPR_DEBUG_SPARE'):
        for k in _cand:
            if k not in spare:
                t_ = next((t for t in B.donor.tracks if t.src.get('sf') == k.sf), None)
                refs = cpr_tree.referenced_from_outside(B.A, k, ignore)
                log.append('DEBUG kept %s %r: referenced from outside: %r'
                           % (k.cls, t_.name if t_ else '?', refs))

    doomed = set()
    for k in spare:
        k.deleted = True
        objs, _defs = cpr_tree.subtree_targets(k, B.A)
        doomed |= objs

    # A prototype the project still points at stays, and it stays under
    # the donor's name for it. donor.cpr calls its own TEMPLATE AUDIO and
    # TEMPLATE INSTRUMENT; donor-audio-first.cpr's were 'Audio 01' and
    # 'Pianoteq 9 01', which read in Cubase as two tracks of the song
    # (ZITRO Variations came in with both). Every copy has been made by
    # now, so the name is the prototype's own to change - unless the
    # prototype became one of the song's tracks itself (consumed), as
    # ZITRO's Bass did: renamed, it came out as TEMPLATE INSTRUMENT.
    gone = set(id(k) for k in spare)
    for node, proto, label in ((a_node, proto_audio, 'TEMPLATE AUDIO'),
                               (i_node, proto_inst, 'TEMPLATE INSTRUMENT')):
        if node is None or proto is None or id(node) in gone or node.deleted:
            continue
        if id(node) in consumed or id(node) in reused:
            continue
        if (proto.name or '').startswith('TEMPLATE'):
            continue
        for off in track_name_offsets(node, proto):
            B.set_string(node, off, label)

    # The object registry at the tail of the root: a row of (offset, id) per
    # track, automation lanes included. It is how Cubase connects a track,
    # and a copy that has no row here is one it cannot connect - it says so
    # on opening and the lane comes up with no parameter chosen. Rows were
    # being dropped for the tracks that went and never added for the copies
    # that arrived, so every converted track was unconnectable.
    # A consumed donor track keeps its automation lanes, and the exposed
    # lanes are exactly the registry-listed ones: the donor registers 2 of
    # its 7 automation records and shows 2. A track whose REAPER original
    # has no channel automation should not arrive with an open Volume lane,
    # so its lane rows are dropped along with the deleted tracks'.
    for t, kind, dup, plan, pplan, into_bus, fplans in allocs:
        if id(dup) not in consumed or t.volenv:
            continue
        for n in cpr_tree.walk_nodes(dup):
            if n.cls == 'MAutomationTrackEvent':
                doomed.add(n.sf)

    registry = None
    if B.table and legacy('REGISTRY'):
        # the old behaviour, for bisecting: drop the rows of the
        # tracks that went and add none for the copies that arrived
        if doomed:
            cpr_tree.drop_table_rows(B.A, B.table, B.root, doomed)
    elif B.table:
        rows = cpr_tree.registry_rows(B.A, B.table)
        used = set(i for _off, i in rows)
        keep = []
        for off, rid in rows:
            n = B.index.get(off)
            if n is None or n.deleted or off in doomed:
                continue
            keep.append((n, rid))
        # A copy is registered exactly when the record it was taken from is.
        # The donor keeps automation objects that carry no row of their own,
        # so mirroring it is safer than registering everything that looks
        # like a track.
        registered = set(off for off, _ in rows)
        fresh = [n for n in cpr_tree.walk_nodes(B.root)
                 if n.clone and not n.deleted and n.origin_node is not None
                 and n.origin_node.sf in registered]
        # a record brought in from another project has no original here to
        # mirror, and a track without a row is one Cubase cannot connect
        fresh += [n for n in B.extra_registered if not n.deleted]
        ids = cpr_tree.free_registry_ids(used, len(fresh))
        keep.extend(zip(fresh, ids))
        registry = cpr_tree.plan_registry(B.A, B.root, B.table, keep)
        stats['registry rows'] = len(keep)
        stats['registry rows added'] = len(fresh)

    # The records that could not be removed - other records still refer
    # into them - are emptied of their events, renamed, and moved into
    # Cubase's own Input/Output folder, which opens collapsed: out of sight,
    # playing nothing, and every reference to them still valid.
    # Cubase's own Input/Output folder is the first record in the list and
    # holds the output bus; it is never a leftover
    io_node = next((k for k in holder.kids
                    if k.cls == 'MFolderTrack' and not k.deleted), None)
    leftovers = [k for k in holder.kids
                 if not k.deleted and not k.clone and k.cls in TRACKISH
                 and k is not io_node and id(k) not in keep_nodes
                 and not any(n.cls in GLOBALISH for n in cpr_tree.walk_nodes(k))]
    io_track = next((t for t in B.donor.tracks
                     if io_node is not None and t.src.get('sf') == io_node.sf), None)
    io_inner = io_node.kids[0] if io_node is not None and io_node.kids else None
    tucked = 0
    if io_inner is not None and io_track is not None and leftovers \
            and not legacy('TUCK'):
        for k in leftovers:
            src_t = next((t for t in B.donor.tracks if t.src.get('sf') == k.sf), None)
            if src_t is None:
                continue
            mode = os.environ.get('CPR_TUCK_MODE', 'move')
            # no events: the donor's own part or clip must not play
            for it in ([] if os.environ.get('CPR_TUCK_KEEPPARTS') else src_t.items):
                if it.origin and it.origin.get('flags'):
                    ev = find_in(k, it.origin['flags'] - B.base)
                    if ev is not k:
                        ev.deleted = True
                        objs_, _d = cpr_tree.subtree_targets(ev, B.A)
                        doomed |= objs_
            oc = src_t.src.get('own_count_off')
            if oc is not None:
                a = oc - B.base
                find_in(k, a).splice(a, a + 4, struct.pack('>I', 0))
            for off in (B.name_offsets(k, src_t.name) or [src_t.src['name_off']]):
                # a part named like its track (DONOR_MELODYNE's, resaved by
                # Cubase) goes with its deleted event: nothing to rename
                if not under_deleted(k, off - B.base):
                    B.set_string(k, off, '(unused template)')
            B.fold_automation(k, src_t)
            if src_t.instrument is not None:
                B.write_plugin(k, src_t.instrument, stock_instrument())
            # The record plays nothing, but its channel is still in the
            # mix, and the stand-in in its instrument slot is an effect
            # (Cubase ships no instrument) that Cubase then runs as one: in
            # the identity test its export came out as garbage - millions
            # of samples at 1e26 and NaN - on two runs out of six, straight
            # into Stereo Out. Muted and pulled all the way down, so
            # whatever the slot produces goes nowhere.
            B.write_mute(k, src_t)
            for key, val in (('vol_db', -144.0), ('vol_raw', 0.0)):
                if key in src_t.origin:
                    off, ty = src_t.origin[key]
                    B.set_typed(k, off, ty, val)
            if mode == 'move':
                holder.kids.remove(k)
                holder.splice(k.hdr, k.de, b'')
                io_inner.extra.append(('end', k))
            tucked += 1
        oc = io_track.src.get('own_count_off')
        if oc is not None and tucked and os.environ.get('CPR_TUCK_MODE', 'move') == 'move':
            a = oc - B.base
            n_io = len(io_inner.kids) + len(io_inner.extra)
            find_in(io_node, a).splice(a, a + 4, struct.pack('>I', n_io))
    left = len(leftovers) - tucked
    if left:
        log.append('%d track(s) named TEMPLATE came along: they are the '
                   'records every track was copied from, and Cubase will not '
                   'open a project that has been written into them or had '
                   'them taken out. Delete them in Cubase once it is open.'
                   % left)

    # ---- the marker track goes first ---------------------------------
    # Cubase keeps its Input/Output folder as the first record and calls a
    # file that puts anything ahead of it invalid; the marker track goes
    # right after it, at the top of the visible track list. The record is
    # moved, not copied: taken out of the list's children, its bytes cut
    # from where they were, and put back in front of whatever now follows
    # the folder (or, with nothing left there, ahead of every copy) - the
    # same object, so every reference to it and its registry row still hold.
    if not legacy('MARKERSFIRST'):
        live = [k for k in holder.kids if not k.deleted]
        first = next((k for k in live if k.cls == 'MFolderTrack'), None)
        for k in live:
            if k.cls == 'MMarkerTrackEvent' and k is not first:
                after_io = [x for x in live if x is not first and x is not k]
                anchor = after_io[0] if after_io else None
                if anchor is not None and holder.kids.index(anchor) > holder.kids.index(k):
                    break               # already ahead of everything
                holder.kids.remove(k)
                holder.splice(k.hdr, k.de, b'')
                holder.extra.insert(0, (anchor, k))
                break

    co = B.donor.src.get('count_off') or proto_inst.src.get('count_off')
    if co is not None:
        kept = sum(1 for k in holder.kids if not k.deleted)
        cpr_tree.find_node(B.root, co - B.base).splice(
            co - B.base, co - B.base + 4,
            struct.pack('>I', kept + len(holder.extra)))

    # Automation used to reach the file intact and still open unassigned,
    # because a copied track had no row in the object registry and so could
    # not be connected. The rows are written now. Whether Cubase is satisfied
    # by that has not been confirmed on a real project yet, so the note stays
    # until it has.
    # The project's own sample rate. Left as the donor's, a 44.1k project
    # opens as 48k, and every audio event's length and offset - which a .cpr
    # stores in samples - is then read against the wrong rate.
    if (new.samplerate and new.samplerate != B.donor.samplerate
            and not legacy('SAMPLERATE')):
        off = (B.donor.src or {}).get('samplerate_off')
        if off:
            a = off - B.base
            find_in(B.root, a).splice(a, a + 4,
                                      struct.pack('>f', float(new.samplerate)))
            B.patch_metadata_rate(float(new.samplerate))
            stats['sample rate'] = new.samplerate
        else:
            log.append('the project is %d Hz and the donor %d Hz, and the '
                       'setting could not be found to change it - set it in '
                       'Cubase under Project > Project Setup'
                       % (new.samplerate, B.donor.samplerate))
    # The pan law. REAPER's PANLAW is the gain at centre (1.0 is 0 dB,
    # 0.707 is -3 dB); Cubase's is Project Setup > Stereo Pan Law, a code
    # in the same record as the sample rate. A panned track sits at a
    # different level under a different law, so REAPER's is written.
    code, sure = pan_law_code(getattr(new, 'panlaw', None))
    off = (B.donor.src or {}).get('panlaw_off')
    if code is not None and off:
        if code != getattr(B.donor, 'panlaw_code', None):
            a = off - B.base
            find_in(B.root, a).splice(a, a + 4, struct.pack('>i', code))
            stats['pan law'] = code
        if not sure:
            log.append('the pan law was written as Cubase code %d for REAPER\'s '
                       '%.3f (%.1f dB at centre); only 0 dB and Equal Power '
                       'have been confirmed in Cubase, check Project Setup > '
                       'Stereo Pan Law' % (code, new.panlaw,
                                            20 * math.log10(max(new.panlaw, 1e-6))))
    elif code is not None:
        log.append('the pan law could not be written: set Cubase\'s Project '
                   'Setup > Stereo Pan Law to match REAPER\'s Project '
                   'Settings > Pan law')

    n_video = stats.get('video_events', 0)
    if n_video:
        log.append('%d video event(s) were written on a video track brought '
                   'in from %s' % (n_video, os.path.basename(VIDEO_DONOR)))
    n_tpan = sum(1 for t in want for i in comp_items(t)
                 if i.kind == 'audio' and i.panenv
                 and (len(i.panenv) > 1 or abs(i.panenv[0][1]) > 1e-6))
    if n_tpan:
        log.append('%d item(s) still carry a pan curve drawn inside the item '
                   'that could not be rendered into the audio; Cubase has no '
                   'per-event pan, so they arrive centred' % n_tpan)

    if getattr(B, '_curved_fades', 0):
        log.append('%d fade(s) with a REAPER shape other than a straight line '
                   'were written as Cubase fade curves (32 points along '
                   'REAPER\'s curve on Cubase\'s linear fade), so they stay '
                   'editable and play the same' % B._curved_fades)
    n_ver = stats.get('versions', 0)
    n_muted = sum(muted_lane_items(t) for t in new.tracks)
    if n_ver:
        log.append('%d REAPER lane(s) became Cubase track versions holding '
                   '%d event(s); the lane REAPER plays is the active version, '
                   'and the others are in the track\'s version list'
                   % (n_ver, n_muted))
    elif n_muted:
        log.append('%d item(s) sit on REAPER lanes that do not play and were '
                   'left out' % n_muted)
    n_takes = sum(alternate_takes(t) for t in new.tracks)
    if n_takes:
        log.append('%d alternate take(s) were written as events stacked '
                   'under the take that plays - Cubase shows them as lanes '
                   '(Show Lanes on the track) and plays the one on top, as '
                   'REAPER did' % n_takes)

    n_auto = stats.get('automation', 0) + stats.get('pan automation', 0)
    n_fx = stats.get('plug-in automation', 0)
    if n_auto or n_fx:
        # volume, pan and master lanes play in Cubase and null against
        # REAPER's render (TB3, 2026-09-28); plug-in lanes bind through the
        # parameter table read from the plug-in (vst3params)
        log.append('%d automation point(s) on volume/pan lanes%s were written; '
                   'curves are re-pointed so Cubase\'s straight lines in fader '
                   'position play REAPER\'s straight lines in gain'
                   % (n_auto, (' and %d on plug-in parameter lanes' % n_fx)
                      if n_fx else ''))

    # The output bus. Cubase keeps its channel outside the track records
    # and this tool has never found it - not in a donor and not in any
    # project Cubase saved here, where the master reads back with no mixer
    # and no insert strip at all. So a REAPER master chain does not cross,
    # and it used to do that in silence: a multiband compressor and a
    # limiter over the whole mix went missing with nothing said, which is
    # the loudest thing that can quietly go wrong with a conversion.

    # Pan law. A track panned away from centre is louder or quieter
    # depending on the law the host applies, and the two hosts have their
    # own: REAPER's is PANLAW in the project, Cubase's is in Project Setup.
    # Neither is written, so a panned track can sit at a different level
    # even with the same pan value - which is worth saying when any track
    # is actually panned.
    panned = [t for t in new.tracks if abs(t.pan) > 0.001 and not t.is_folder]
    if panned and getattr(new, 'pan_law_of', None) == 'reaper':
        from . import panlaw as _pl
        law = new.panlaw if new.panlaw is not None else 1.0
        shown = []
        for t in panned[:6]:
            mode = t.panmode if t.panmode is not None else new.panmode
            cp, g = _pl.reaper_to_cubase(t.pan, law, mode)
            shown.append('%s %+.2f -> %+.2f (%+.2f dB on the fader)'
                         % (t.name, t.pan, cp, 20 * math.log10(max(g, 1e-9))))
        log.append('%d panned track(s): REAPER\'s balance panner boosts the '
                   'pair towards the centre and Cubase\'s does not, so each '
                   'arrives with the Cubase pan that keeps its L/R ratio and '
                   'the difference on its fader - the two then play the same '
                   'level on both channels: %s%s'
                   % (len(panned), '; '.join(shown),
                      ' ...' if len(panned) > 6 else ''))
    if stats.get('pan lanes uncompensated'):
        log.append('%d track(s) carry pan automation: the lane holds the '
                   'Cubase pan with the right L/R ratio at every point, but '
                   'the level compensation a static pan gets on the fader '
                   'cannot follow a curve, so those tracks can sit up to 3 dB '
                   'off along the lane (see panlaw.py)'
                   % stats['pan lanes uncompensated'])
    if panned and 'pan law' not in stats and pan_law_code(getattr(new, 'panlaw', None))[0] is None \
            and getattr(new, 'pan_law_of', None) != 'reaper':
        log.append('%d track(s) are panned away from centre (%s). What a pan '
                   'costs in level is set by the host\'s pan law, and this '
                   'writes neither side\'s: if those tracks sit at a '
                   'different level than in REAPER, match Cubase\'s Project '
                   'Setup > Stereo Pan Law to REAPER\'s Project Settings > '
                   'Pan law'
                   % (len(panned),
                      ', '.join('%s %+.2f' % (t.name, t.pan)
                                for t in panned[:6])))

    # A plug-in that arrives with no saved settings comes up on its
    # defaults, and nothing in the mix sounds right until it is put back.
    # REAPER can store a VST2's state as its own parameter dump (a
    # DEADBEEF/DEADF00D header and a list of normalised floats) rather than
    # the plug-in's chunk, and no other host can read that; the preset it
    # was on, at least, is in the project.
    if B._stateless:
        seen = []
        for where, name, preset in B._stateless:
            key = (where, name, preset)
            if key not in seen:
                seen.append(key)
        log.append('%d plug-in(s) arrive WITHOUT their settings, on their '
                   'defaults - REAPER saved their state in its own parameter '
                   'format, which nothing else can read. Put them back by '
                   'hand: %s'
                   % (len(seen),
                      '; '.join('%s%s%s' % (name,
                                             (' on ' + where) if where else '',
                                             (' - REAPER preset %r' % preset)
                                             if preset else ' - no preset name')
                                for where, name, preset in seen[:8])))

    n_col_want = sum(1 for t in new.tracks if t.color)
    if n_col_want and B._colour_blocked:
        log.append('%d of %d track colour(s) could not be written and the '
                   'tracks arrive in Cubase\'s default grey. A colour is the '
                   'attribute \'Farb\', and only a record the donor already '
                   'keeps one on can pass it on - %s was never given track '
                   'colours in Cubase, and this project needs that donor '
                   'because all of its audio tracks come before all of its '
                   'instrument tracks. To fix it once and for all: open that '
                   'donor in Cubase, give every track any colour, save it '
                   'back. Colour every track in Cubase for now'
                   % (B._colour_blocked, n_col_want,
                      os.path.basename(getattr(B, 'donor_path', '') or 'the donor')))

    if B._cc_done:
        log.append('%d control change event(s) - mod wheel, expression, '
                   'sustain - were written into the parts alongside the '
                   'notes, so a patch is played the way REAPER played it'
                   % B._cc_done)
    if B._cc_nodonor:
        log.append('%d controller event(s) could not be written: the donor '
                   'project has no controller event in its MIDI part to copy '
                   'the record from' % B._cc_nodonor)
    n_bends = sum(1 for t in new.tracks for it in t.items
                  if it.kind == 'midi'
                  for c in (it.ccs or ()) if (c[1] & 0xf0) == BEND_KIND)
    if n_bends and bend_mode() != 'off':
        log.append('%d pitch bend(s) were written (%s): the two data bytes in '
                   'MIDI order and the 14-bit value in the record\'s HRDT '
                   'field. Confirm by ear or by the export comparison; '
                   'CPR_WRITE_BENDS=wire|swap|off changes it'
                   % (n_bends, bend_mode()))
    if B._cc_unsupported:
        total = sum(B._cc_unsupported.values())
        what = ', '.join(
            '%d %s' % (n, CC_KIND_NAMES.get(k, hex(k)))
            for k, n in sorted(B._cc_unsupported.items(), key=lambda kv: -kv[1]))
        log.append('%d MIDI controller event(s) were left out because how '
                   'Cubase lays them out is not known - no project Cubase '
                   'saved here contains one to copy, and a guessed layout '
                   'bends a part off key rather than failing quietly: %s. '
                   'The notes are all there; draw these back in Cubase\'s '
                   'controller lane, or keep those tracks as audio'
                   % (total, what))

    progress.stage('putting the project together')
    if B._not_wav:
        names = sorted(os.path.basename(f) for f in B._not_wav)
        log.append('%d file(s) are not WAV and will not play in Cubase - '
                   'convert them and re-run: %s'
                   % (len(names), ', '.join(names[:4])
                      + ('...' if len(names) > 4 else '')))
    dropped_clips = B.prune_pool(pool_links)
    if dropped_clips:
        log.append('%d donor clip(s) nothing plays any more were unlisted '
                   'from the pool (%s) - a listed clip is a file Cubase '
                   'asks for on open'
                   % (len(dropped_clips), ', '.join(dropped_clips[:3])))
    B.close_pool()
    n_rack = 0 if os.environ.get('CPR_NO_RACKSTRIP') else B.strip_synth_rack()
    if n_rack:
        log.append('%d rack instrument(s) the donor kept in its VST rack '
                   'were emptied - they are what made Cubase load HALion on '
                   'opening a project that never used it' % n_rack)
    # The output bus, written last: it lives in the Devices chunk and
    # strip_synth_rack repacks that chunk, so its offsets are only settled
    # once that has finished moving things about. Inserts before the fader,
    # since writing one changes the chunk's length and the fader's offsets
    # are read afresh either way.
    if new.master is not None and new.master.fx:
        want = [f for f in new.master.fx if not getattr(f, 'native', False)]
        gone = [f for f in new.master.fx if getattr(f, 'native', False)]
        n_mi = 0
        if want and not os.environ.get('CPR_NO_MASTER_FX'):
            try:
                n_mi = B.write_master_inserts(want)
            except Exception as e:
                log.append('the REAPER master inserts could not be written '
                           '(%s); add them to Stereo Out by hand: %s'
                           % (str(e)[:80], ', '.join(f.name for f in want)))
        if n_mi:
            stats['plugins'] += n_mi
            with_state = [f for f in want[:n_mi] if f.component]
            without = [f for f in want[:n_mi] if not f.component]
            log.append('%d master insert(s) were written onto the Cubase '
                       'output bus%s: %s. They live in the Devices chunk with '
                       'the output channel, not on any track, which is why '
                       'they never used to cross%s'
                       % (n_mi,
                          ', settings and all' if not without else
                          (', %d with their settings' % len(with_state)
                           if with_state else ''),
                          ', '.join(f.name for f in want[:n_mi]),
                          ('. %d arrive on their defaults - REAPER kept '
                           'their state as its own parameter dump, which no '
                           'other host reads: %s'
                           % (len(without),
                              ', '.join('%s%s' % (f.name,
                                                  (' (REAPER preset %r)' % f.preset)
                                                  if getattr(f, 'preset', '')
                                                  else '')
                                        for f in without)))
                          if without else ''))
        if want and n_mi < len(want):
            log.append('%d master insert(s) are still NOT in the Cubase '
                       'project: %s - add them to Stereo Out by hand'
                       % (len(want) - n_mi,
                          ', '.join(f.name for f in want[n_mi:])))
        if gone:
            log.append('%d REAPER-only master plug-in(s) have no Cubase '
                       'counterpart and were left out: %s'
                       % (len(gone), ', '.join(f.name for f in gone)))

    if new.master is not None and abs(new.master.vol - 1.0) > 1e-6:
        if B.write_master_mix(new.master):
            import math as _m
            stats['master fader'] = 20.0 * _m.log10(max(1e-9, new.master.vol))
            log.append('the REAPER master fader (%+.2f dB) was written onto '
                       'the Cubase output bus, which is where the whole mix '
                       'sums - it lives in the Devices chunk rather than on '
                       'a track, which is why it never used to cross'
                       % stats['master fader'])
        else:
            log.append('the REAPER master fader is %+.2f dB and could not be '
                       'written onto the output bus - set it in Cubase'
                       % (20.0 * __import__('math').log10(max(1e-9, new.master.vol))))

    if new.master is not None and len(getattr(new.master, 'volenv', []) or []) > 1:
        # Found by the identity test (Cherry Link, 2026-09-28): REAPER's
        # master render followed its master volume envelope and came out
        # 3.8 dB quieter than Cubase's Stereo Out, whose bus had no lane
        # written for it. The donor's Stereo Out carries a volume lane with
        # no points; it is filled from the point prototype.
        n_m = 0
        if not os.environ.get('CPR_NO_MASTER_LANE'):
            n_m = write_master_volenv(B, new, log)
        if n_m:
            stats['master volume envelope'] = n_m
            log.append('the REAPER master volume envelope (%d points) was written '
                       'onto the Cubase output bus\'s volume lane' % n_m)
        else:
            stats['master volume envelope dropped'] = len(new.master.volenv)
            log.append('the REAPER master has a volume envelope with %d point(s) '
                       'and it could not be written onto the Cubase output bus, '
                       'so the mix arrives WITHOUT it - the master rides are '
                       'lost. Print the master in REAPER, or route everything '
                       'through a group and automate that instead'
                       % len(new.master.volenv))

    if proto_ara_item is not None:
        # the project's Melodyne document: REAPER's, converted, or - when no
        # take used Melodyne after all - none
        doc = next((n for n in cpr_tree.walk_nodes(B.root)
                    if n.cls == 'FMemoryStream'
                    and bytes(B.A.d[n.ds + 4:n.ds + 10]) == b'GNBKVA'), None)
        if doc is not None:
            if getattr(B, 'ara_blob', None) and stats.get('Melodyne events'):
                doc.splice(doc.ds, doc.de,
                           struct.pack('>I', len(B.ara_blob)) + B.ara_blob)
                log.append("%d event(s) with Melodyne edits: Cubase's Melodyne "
                           "opens REAPER's document with them (Melodyne has to "
                           "be installed to hear them)" % stats['Melodyne events'])
            else:
                doc.deleted = True
    stats['refs found by scan'] = B.record_all_refs()
    out, counts = cpr_tree.rebuild(B.raw, B.A, B.root, pool_links, registry)
    with open(path, 'wb') as f:
        f.write(out)
    stats.update(counts)
    stats['removed'] = len(spare)
    return stats
