"""Write the intermediate model as a Cubase Track Archive (.xml).

Cubase reads this with File > Import > Track Archive. It is Cubase's own
object model in text form, so it carries folder structure, track names and
colours, channel levels and pan, and complete insert chains including each
plug-in's saved state.

Schema note
-----------
Every element written for tracks, the mixer and the insert chains was taken
from a Track Archive that Cubase itself exported, so those names are exact.
The audio-event members marked EVENT_SCHEMA below were reconstructed from the
binary project layout rather than from an export; Cubase's importer ignores
member names it does not recognise, so an inexact name means the events do
not appear, not that the file is rejected. Export one Track Archive from
Cubase containing an audio event and a MIDI part to pin them down.
"""
from .model import playing_items
import math
import xml.sax.saxutils as sx

from . import fader
from . import progress

# Cubase's default event-colour palette, used to map an RGB back to an index.
PALETTE = [
    (0xE5, 0x36, 0x36), (0xE5, 0x76, 0x36), (0xE5, 0xBA, 0x3B), (0xD5, 0xE8, 0x4C),
    (0x8D, 0xE5, 0x36), (0x51, 0xD8, 0x3C), (0x35, 0xDD, 0x5F), (0x33, 0xD6, 0x97),
    (0x30, 0xCC, 0xCC), (0x40, 0xAA, 0xE8), (0x5D, 0x80, 0xEA), (0x79, 0x6A, 0xED),
    (0xA0, 0x56, 0xEA), (0xCF, 0x44, 0xE5), (0xE5, 0x36, 0xB9), (0xE5, 0x36, 0x79),
]

EVENT_SCHEMA = {
    'events_list': 'Events',
    'clip_obj': 'Audio Clip',
    'offset': 'Offset',
}

TEMPO_TRACK_ID = 1082871680
SIG_TRACK_ID = 847702144


def colour_index(rgb):
    if not rgb:
        return 0
    best, bi = None, 0
    for i, c in enumerate(PALETTE):
        d = sum((a - b) ** 2 for a, b in zip(rgb, c))
        if best is None or d < best:
            best, bi = d, i + 1
    return bi


def g2db(gain):
    return 20.0 * math.log10(gain) if gain > 1e-9 else -200.0


class Writer:
    def __init__(self, proj, ppq=480.0, with_events=True, log=None):
        self.p = proj
        self.ppq = ppq
        self.with_events = with_events
        self.log = log if log is not None else []
        self.out = []
        self._id = 0x40000000
        self._domain_written = False

    def nid(self):
        self._id += 16
        return self._id

    def w(self, depth, text):
        self.out.append('   ' * depth + text)

    def esc(self, s):
        return sx.quoteattr(s or '')

    def s(self, d, name, val, wide=True):
        self.w(d, '<string name=%s value=%s%s/>'
               % (self.esc(name), self.esc(val), ' wide="true"' if wide else ''))

    def i(self, d, name, val):
        self.w(d, '<int name=%s value="%d"/>' % (self.esc(name), int(val)))

    def f(self, d, name, val):
        v = float(val)
        self.w(d, '<float name=%s value="%s"/>'
               % (self.esc(name), ('%d' % v) if v == int(v) else repr(round(v, 6))))

    def binhex(self, d, name, data):
        self.w(d, '<bin name=%s>' % self.esc(name))
        h = data.hex().upper()
        for k in range(0, len(h), 64):
            self.w(d + 1, h[k:k + 64])
        self.w(d, '</bin>')

    # ---------------------------------------------------------------
    def build(self):
        self.w(0, '<?xml version="1.0" encoding="utf-8"?>')
        self.w(0, '<tracklist2>')
        self.w(1, '<list name="track" type="obj">')
        i = 0
        tracks = self.p.tracks
        while i < len(tracks):
            t = tracks[i]
            if t.is_folder:
                kids = self.p.folder_children(i)
                self.folder(2, t, [tracks[k] for k in kids if tracks[k].depth == t.depth + 1],
                            tracks, kids)
                i = (kids[-1] + 1) if kids else i + 1
            elif t.kind == 'video':
                self.log.append('video track %r left out of the Track Archive; '
                                'import the video in Cubase with Project > Add '
                                'Track > Video' % t.name)
                i += 1
            else:
                self.audio_track(2, t)
                i += 1
        self.master_track(2)
        self.w(1, '</list>')
        self.w(0, '</tracklist2>')
        return '\n'.join(self.out) + '\n'

    def master_track(self, d):
        """REAPER's master, written as a group channel.

        Cubase's real output bus lives in VST Connections, and a Track
        Archive can only bring in tracks - it cannot create or alter an
        output bus. Dropping the master would silently lose whatever was on
        it, so it comes in as a group channel carrying the same level and the
        same inserts; route the project into it, or copy its inserts onto
        Stereo Out. A .cpr carries the output bus itself."""
        m = getattr(self.p, 'master', None)
        if m is None or not (m.fx or m.volenv or m.vol != 1.0 or m.pan != 0.0):
            return
        self.audio_track(d, m, group=True)
        self.log.append(
            'the REAPER master became a group channel named %r (level, %d '
            'insert(s)): a Track Archive cannot set Cubase\'s output bus, so '
            'route the project into that group or copy its inserts onto '
            'Stereo Out' % (m.name, len(m.fx)))
        if m.volenv:
            self.log.append(
                '%d master automation point(s) could not be written: a Track '
                'Archive has no verified schema for a volume lane, so redraw '
                'them on Stereo Out or convert to .cpr instead'
                % len(m.volenv))

    def folder(self, d, t, direct, tracks, kid_idx):
        self.w(d, '<obj class="MFolderTrack" ID="%d">' % self.nid())
        self.i(d + 1, 'Flags', 1)
        self.f(d + 1, 'Start', 0)
        self.f(d + 1, 'Length', self.length_sec())
        self.w(d + 1, '<obj class="MTrackList" name="Node" ID="%d">' % self.nid())
        self.s(d + 2, 'Name', t.name)
        self.w(d + 2, '<member name="Domain">')
        self.i(d + 3, 'Type', 1)
        self.f(d + 3, 'Period', 1)
        self.w(d + 2, '</member>')
        self.w(d + 2, '<list name="Tracks" type="obj">')
        # the folder's own channel comes back as a group channel of the same name
        if t.fx or abs(t.vol - 1.0) > 1e-6 or abs(t.pan) > 1e-6:
            self.audio_track(d + 3, t, group=True)
        j = 0
        while j < len(kid_idx):
            k = kid_idx[j]
            ct = tracks[k]
            if ct.depth != t.depth + 1:
                j += 1
                continue
            if ct.kind == 'video':
                self.log.append('video track %r left out of the Track Archive; '
                                'import the video in Cubase with Project > Add '
                                'Track > Video' % ct.name)
                j += 1
                continue
            if ct.is_folder:
                sub = [x for x in kid_idx if x > k and tracks[x].depth > ct.depth]
                sub = [x for x in sub if all(tracks[y].depth > ct.depth
                                             for y in range(k + 1, x + 1))]
                self.folder(d + 3, ct,
                            [tracks[x] for x in sub if tracks[x].depth == ct.depth + 1],
                            tracks, sub)
                j += 1 + len(sub)
            else:
                self.audio_track(d + 3, ct)
                j += 1
        self.w(d + 2, '</list>')
        self.w(d + 1, '</obj>')
        self.w(d, '</obj>')

    def length_sec(self):
        end = 0.0
        for t in self.p.tracks:
            for it in playing_items(t):
                end = max(end, it.pos + it.length)
        return max(600.0, round(end + 30.0))

    # What Cubase itself writes for each kind of track, read back out of a
    # real project rather than guessed: the event class, the device class
    # inside it, and the channel Type. Writing an instrument track as an
    # audio track - which is what this did - gives an archive that carries
    # the instrument's settings but has nowhere to put them, so the import
    # brings in nothing playable.
    TRACK_KIND = {
        'audio':      ('MAudioTrackEvent', 'MAudioTrack', 1),
        'instrument': ('MInstrumentTrackEvent', 'MInstrumentTrack', 4),
        'group':      ('MDeviceTrackEvent', 'MTrack', 2),
    }

    def kind_of(self, t, group=False):
        if group or t.is_folder:
            return 'group'
        if t.instrument is not None or any(i.kind == 'midi' for i in t.items):
            return 'instrument'
        if any(i.kind == 'audio' for i in t.items):
            return 'audio'
        return 'group'

    def audio_track(self, d, t, group=False):
        kind = self.kind_of(t, group)
        cls, devcls, devtype = self.TRACK_KIND[kind]
        self.w(d, '<obj class="%s" ID="%d">' % (cls, self.nid()))
        self.i(d + 1, 'Flags', 1)
        self.f(d + 1, 'Start', 0)
        self.f(d + 1, 'Length', self.length_sec() * self.ppq * 2)
        self.w(d + 1, '<obj class="MListNode" name="Node" ID="%d">' % self.nid())
        self.s(d + 2, 'Name', t.name)
        self.domain(d + 2)
        if self.with_events and not group and t.items:
            self.events(d + 2, t)
        self.w(d + 1, '</obj>')
        self.w(d + 1, '<member name="Additional Attributes">')
        self.i(d + 2, 'Farb', colour_index(t.color))
        self.w(d + 1, '</member>')
        self.device(d + 1, t, devcls, devtype)
        self.w(d, '</obj>')

    def events(self, d, t):
        audio = [i for i in playing_items(t) if i.kind == 'audio' and i.file]
        if not audio:
            return
        # A stretched item has no plain-mode equivalent: an event's length is
        # in samples of the file, so either the span on the timeline or the
        # amount of audio in it has to give. The span is kept, which leaves
        # the arrangement intact, and the stretch is reported rather than
        # applied silently. Musical mode in Cubase restores it.
        warped = [i for i in audio if getattr(i, 'warped', False)]
        if warped:
            self.log.append('%d event(s) on %r are time-stretched in REAPER '
                            '(rate %s); they keep their place on the timeline '
                            'but the stretch is not carried - switch the clip '
                            'to Musical Mode in Cubase'
                            % (len(warped), t.name,
                               ', '.join('%.4g' % i.playrate for i in warped[:4])))
        self.w(d, '<list name="%s" type="obj">' % EVENT_SCHEMA['events_list'])
        for it in audio:
            self.w(d + 1, '<obj class="MAudioEvent" ID="%d">' % self.nid())
            self.f(d + 2, 'Start', it.pos * self.ppq * 2)
            self.f(d + 2, 'Length', it.length * self.p.samplerate)
            self.f(d + 2, EVENT_SCHEMA['offset'], it.soffs * self.p.samplerate)
            self.w(d + 2, '<obj class="PAudioClip" name="%s" ID="%d">'
                   % (EVENT_SCHEMA['clip_obj'], self.nid()))
            self.s(d + 3, 'Name', it.name)
            self.w(d + 3, '<obj class="FNPath" name="Path" ID="%d">' % self.nid())
            self.s(d + 4, 'Name', it.file.replace('/', '\\').rsplit('\\', 1)[-1])
            self.s(d + 4, 'Directory',
                   it.file.replace('/', '\\').rsplit('\\', 1)[0] + '\\')
            self.w(d + 3, '</obj>')
            self.w(d + 2, '</obj>')
            self.w(d + 1, '</obj>')
        self.w(d, '</list>')

    def device(self, d, t, devcls='MTrack', devtype=2):
        self.w(d, '<obj class="%s" name="Track Device" ID="%d">'
               % (devcls, self.nid()))
        self.i(d + 1, 'Connection Type', 1)
        self.w(d + 1, '<string name="Device Name" value="VST Multitrack"/>')
        self.i(d + 1, 'Channel ID', 8 if devcls == 'MInstrumentTrack' else 4)
        self.w(d + 1, '<member name="DeviceAttributes">')
        self.w(d + 2, '<member name="Name">')
        self.s(d + 3, 'String', t.name)
        self.w(d + 2, '</member>')
        self.i(d + 2, 'Type', devtype)
        db = g2db(t.vol)
        self.w(d + 2, '<member name="Volume">')
        self.f(d + 3, 'Value', fader.db_to_raw(db))
        self.f(d + 3, 'AnchorValue', db)
        self.w(d + 2, '</member>')
        self.panner(d + 2, t.pan)
        self.inserts(d + 2, t)
        if t.instrument is not None:
            # member order follows Cubase's own export
            self.w(d + 2, '<member name="Synth Slot">')
            self.w(d + 3, '<string name="Plugin isA" value="VstCtrlInternalEffect"/>')
            self.plugin(d + 3, t.instrument, instrument=True)
            self.i(d + 3, 'WasEnableBeforeFreeze', 0)
            self.i(d + 3, 'State', 1)
            self.i(d + 3, 'SlotType', -1)
            self.w(d + 2, '</member>')
        self.w(d + 1, '</member>')
        if devcls == 'MInstrumentTrack':
            # without these the track exists but takes no notes
            self.i(d + 1, 'MidiChannel', -1)
            self.i(d + 1, 'Input Type', 3)
            self.s(d + 1, 'Input Device ID', 'All MIDI Inputs', wide=False)
            self.i(d + 1, 'Input Channel ID', 0)
            self.s(d + 1, 'Input Port Name', 'All MIDI Inputs', wide=False)
            self.i(d + 1, 'Solo Flags', 0)
            self.i(d + 1, 'Midi Channel', -1)
            self.i(d + 1, 'Midi Group', 0)
            self.i(d + 1, 'Midi Input Channel Filter', -1)
            self.i(d + 1, 'Midi Input Group Filter', -1)
        self.w(d, '</obj>')

    def panner(self, d, pan):
        import struct
        v = max(0.0, min(1.0, pan / 2.0 + 0.5))
        blob = struct.pack('<ffII', v, 0.5, 4, 2) + b'\x00' * 4
        self.w(d, '<member name="Panner">')
        self.s(d + 1, 'Plugin Name', 'Standard Panner')
        self.i(d + 1, 'PannerType', 2)
        self.binhex(d + 1, 'audioComponent', blob)
        self.i(d + 1, 'Active', 1)
        self.w(d, '</member>')

    def domain(self, d):
        """Every track's Domain points at the tempo and signature tracks.

        Cubase writes both out in full the first time they are mentioned and
        refers to them by ID after that. Referring to an ID that is never
        defined leaves the file with dangling pointers, which is what this
        did - six references, no definitions - and the import fails."""
        self.w(d, '<member name="Domain">')
        self.i(d + 1, 'Type', 0)
        if not self._domain_written:
            self._domain_written = True
            self.w(d + 1, '<obj class="MTempoTrackEvent" name="Tempo Track" '
                          'ID="%d">' % TEMPO_TRACK_ID)
            self.w(d + 2, '<list name="TempoEvent" type="obj">')
            for pos, bpm in (self.p.tempo or [(0.0, 120.0)]):
                self.w(d + 3, '<obj class="MTempoEvent" ID="%d">' % self.nid())
                self.f(d + 4, 'BPM', bpm)
                self.f(d + 4, 'PPQ', pos * self.ppq)
                self.w(d + 3, '</obj>')
            self.w(d + 2, '</list>')
            self.f(d + 2, 'RehearsalTempo', (self.p.tempo or [(0, 120.0)])[0][1])
            self.w(d + 1, '</obj>')
            num, den = self.p.tsig
            self.w(d + 1, '<obj class="MSignatureTrackEvent" '
                          'name="Signature Track" ID="%d">' % SIG_TRACK_ID)
            self.w(d + 2, '<list name="SignatureEvent" type="obj">')
            self.w(d + 3, '<obj class="MTimeSignatureEvent" ID="%d">' % self.nid())
            self.i(d + 4, 'Flags', 8)
            self.f(d + 4, 'Start', 0)
            self.f(d + 4, 'Length', 1)
            self.i(d + 4, 'Bar', 0)
            self.i(d + 4, 'Numerator', num)
            self.i(d + 4, 'Denominator', den)
            self.i(d + 4, 'TimeSignatureDisplayFlags', 0)
            self.i(d + 4, 'Position', 0)
            self.w(d + 3, '</obj>')
            self.w(d + 2, '</list>')
            self.w(d + 1, '</obj>')
        else:
            self.w(d + 1, '<obj name="Tempo Track" ID="%d"/>' % TEMPO_TRACK_ID)
            self.w(d + 1, '<obj name="Signature Track" ID="%d"/>' % SIG_TRACK_ID)
        self.w(d, '</member>')

    def arrangement(self, d, name):
        """A stereo speaker arrangement, written the way Cubase writes it."""
        self.w(d, '<list name="%s" type="list">' % name)
        self.w(d + 1, '<item>')
        self.w(d + 2, '<list name="Type" type="int">')
        self.w(d + 3, '<item value="1"/>')
        self.w(d + 3, '<item value="2"/>')
        self.w(d + 2, '</list>')
        self.w(d + 1, '</item>')
        self.w(d, '</list>')

    def plugin(self, d, fx, instrument=False):
        """One plug-in, matching what Cubase exports.

        The pin counts, the speaker arrangement and IDString are not
        decoration: without them Cubase has no way to wire the plug-in up,
        and an instrument arrives with its state but nothing connected. All
        of these were read out of a Track Archive Cubase itself wrote."""
        self.w(d, '<member name="Plugin">')
        self.w(d + 1, '<member name="Plugin UID">')
        self.s(d + 2, 'GUID', fx.uid)
        self.w(d + 1, '</member>')
        self.s(d + 1, 'Plugin Name', fx.name)
        if instrument:
            self.i(d + 1, 'Audio Input Count', 0)
            self.i(d + 1, 'Audio Output Count', 1)
            self.arrangement(d + 1, 'Audio Output Arrangement')
            self.i(d + 1, 'Event Input Count', 1)
            self.w(d + 1, '<list name="Event Input Name" type="string">')
            self.w(d + 2, '<item value="Midi In"/>')
            self.w(d + 1, '</list>')
            self.w(d + 1, '<list name="Event Input Channel Count" type="int">')
            self.w(d + 2, '<item value="16"/>')
            self.w(d + 1, '</list>')
            self.i(d + 1, 'Event Output Count', 0)
        else:
            self.i(d + 1, 'Audio Input Count', 1)
            self.arrangement(d + 1, 'Audio Input Arrangement')
            self.i(d + 1, 'Audio Output Count', 1)
            self.arrangement(d + 1, 'Audio Output Arrangement')
            self.i(d + 1, 'Event Input Count', 0)
            self.i(d + 1, 'Event Output Count', 0)
        if fx.component:
            self.binhex(d + 1, 'audioComponent', fx.component)
        if fx.controller:
            self.binhex(d + 1, 'editController', fx.controller)
        self.i(d + 1, 'Version', 1)
        self.i(d + 1, 'Editor Size Count', 0)
        self.i(d + 1, 'Active', 0 if fx.bypass else 1)
        self.s(d + 1, 'IDString', '%s-0' % (fx.uid or ''), wide=False)
        self.s(d + 1, 'Bay Program', fx.preset)
        self.w(d, '</member>')

    def inserts(self, d, t):
        if not t.fx:
            return
        self.w(d, '<member name="InsertFolder">')
        self.i(d + 1, 'Bypass', 0)
        self.w(d + 1, '<list name="Slot" type="list">')
        for n in range(16):
            self.w(d + 2, '<item>')
            if n < len(t.fx):
                fx = t.fx[n]
                self.i(d + 3, 'State', 1)
                self.i(d + 3, 'SlotType', -1)
                self.w(d + 3, '<string name="Plugin isA" value="VstCtrlInternalEffect"/>')
                self.plugin(d + 3, fx, instrument=False)
            else:
                self.i(d + 3, 'State', 0)
                self.i(d + 3, 'SlotType', -1)
            self.w(d + 2, '</item>')
        self.w(d + 1, '</list>')
        self.i(d + 1, 'SummingMode', 1)
        self.w(d, '</member>')


def write(proj, path, with_events=True, log=None):
    log = log if log is not None else []
    progress.stage('building Cubase Track Archive')
    w = Writer(proj, with_events=with_events, log=log)
    text = w.build()
    with open(path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(text)
    n_fx = sum(len(t.fx) for t in proj.tracks)
    n_state = sum(1 for t in proj.tracks for fx in t.fx if fx.component)
    return {'tracks': len(proj.tracks), 'fx': n_fx, 'fx_state': n_state}
