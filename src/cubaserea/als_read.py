"""Read an Ableton Live 11 Set (.als) into the model.

The inverse of als_write, with the same measurements behind it:

  time      Live counts the Arrangement in beats (quarter notes); the model
            in seconds, through the song's tempo (its automation, as steps
            where a value jumps and as straight-line ramps between points,
            which is how Live plays them).
  mixer     Live's fader is linear gain, its lane straight in dB
            (envelope.volume_for_live); its panner the angle law REAPER's
            uses with +3 dB at the edges (panlaw.live_gains) - so the pans
            are read as REAPER pans with the level difference on the fader,
            and the project says its pans are REAPER's (pan_law_of).
  clips     unwarped: seconds of the file from LoopStart, fades in seconds;
            warped: the warp markers map the clip's beats onto file seconds
            (rate, offset), Transpose/Detune the pitch, fades in beats.
  devices   VST3: class id from the four big-endian words, ProcessorState
            / ControllerState as the plug-in's own state. VST2: the
            program buffer - a chunk ('FBCh'/'FPCh') or 28-byte name and
            parameter values ('FxBk'/'FxCk', REAPER's parameter dump).
            An Instrument Rack's chains are the instruments of one track,
            as REAPER stacks them. Utility's gain folds into the fader;
            Live's other own devices are named, not carried (no other
            host has them).
  markers   locators; "<name>" / "<name> end" pairs (how als_write writes a
            region) are a region again.
  colours   Live's palette index to its RGB (als_write.PALETTE).
"""
import gzip
import math
import os
import re
import struct
import xml.etree.ElementTree as ET

from .model import Project, Track, Item, Fx, Send, Marker
from .als_write import PALETTE, LIVE_VOL_MIN

START = -63072000.0


def _v(e, path, default=None, conv=float):
    n = e.find(path)
    if n is None:
        return default
    v = n.get('Value')
    if v is None:
        return default
    if conv is bool:
        return v == 'true'
    try:
        return conv(v)
    except (TypeError, ValueError):
        return default


def rgb_of(index):
    try:
        c = PALETTE[int(index) % len(PALETTE)]
    except (TypeError, ValueError):
        return None
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)


# ------------------------------------------------------------- tempo
class TempoMap:
    """Beats <-> seconds on Live's tempo automation: points (beat, bpm),
    straight lines between them, a jump where two share a beat."""

    def __init__(self, bpm0, events):
        pts = sorted((max(0.0, t), v) for t, v in events if t > START / 2)
        self.pts = [(0.0, pts[0][1] if pts and pts[0][0] <= 1e-9 else bpm0)] + pts
        if not self.pts[1:]:
            self.pts = [(0.0, bpm0)]

    def bpm_at(self, b):
        p = self.pts
        if b <= p[0][0]:
            return p[0][1]
        for (b0, v0), (b1, v1) in zip(p, p[1:]):
            if b0 <= b < b1:
                return v0 + (v1 - v0) * (b - b0) / (b1 - b0) if b1 > b0 else v1
        return p[-1][1]

    def seconds(self, beat):
        """Seconds from the song's start to `beat`, integrating 60/bpm."""
        p = self.pts
        s, cur = 0.0, 0.0
        for (b0, v0), (b1, v1) in zip(p, p[1:]):
            if beat <= b0:
                break
            hi = min(beat, b1)
            if hi > b0 and b1 > b0:
                # bpm linear in beats: integral of 60 / (v0 + k x) dx
                k = (v1 - v0) / (b1 - b0)
                va, vb = v0 + k * (max(b0, cur) - b0), v0 + k * (hi - b0)
                if abs(k) < 1e-12:
                    s += 60.0 * (hi - max(b0, cur)) / v0
                else:
                    s += 60.0 / k * math.log(vb / va)
            cur = hi
        if beat > cur:
            s += 60.0 * (beat - cur) / self.bpm_at(beat)
        return s

    def steps(self):
        """The model's tempo: (seconds, bpm) steps; a ramp becomes steps a
        sixteenth apart (the model holds no ramps)."""
        out = []
        p = self.pts
        for (b0, v0), (b1, v1) in zip(p, p[1:] + [(p[-1][0], p[-1][1])]):
            out.append((self.seconds(b0), v0))
            if b1 > b0 and abs(v1 - v0) > 1e-6:
                n = int((b1 - b0) * 4)
                for k in range(1, n):
                    b = b0 + k * 0.25
                    out.append((self.seconds(b), self.bpm_at(b)))
        dedup = []
        for s, v in out:
            if dedup and abs(dedup[-1][0] - s) < 1e-9:
                dedup[-1] = (s, v)
            elif not dedup or abs(dedup[-1][1] - v) > 1e-9:
                dedup.append((s, v))
        return dedup or [(0.0, p[0][1])]


def envelope_events(env):
    return [(float(e.get('Time')), float(e.get('Value')))
            for e in env.find('Automation/Events')
            if e.get('Value') not in (None, 'true', 'false')]


# ------------------------------------------------------------- reader
class Reader:
    def __init__(self, path, log):
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        self.log = log
        self.root = ET.fromstring(gzip.open(path).read())
        self.ls = self.root.find('LiveSet')
        if self.ls is None:
            raise SystemExit('%s is not a Live Set' % path)
        mixer = self.ls.find('MasterTrack/DeviceChain/Mixer')
        bpm0 = _v(mixer, 'Tempo/Manual', 120.0)
        tid = mixer.find('Tempo/AutomationTarget').get('Id')
        evs = []
        for env in self.ls.find('MasterTrack/AutomationEnvelopes/Envelopes'):
            if env.find('EnvelopeTarget/PointeeId').get('Value') == tid:
                evs = envelope_events(env)
        self.tempo = TempoMap(bpm0, evs)
        self.unsupported = {}

    def sec(self, beat):
        return self.tempo.seconds(max(0.0, beat))

    # ---------------------------------------------------------- song
    def project(self):
        p = Project()
        p.name = os.path.splitext(os.path.basename(self.path))[0]
        p.srcdir = self.dir
        p.tempo = self.tempo.steps()
        code = _v(self.ls, 'MasterTrack/DeviceChain/Mixer/TimeSignature/Manual', 201.0, int)
        p.tsig = (code % 99 + 1, 2 ** (code // 99))
        p.samplerate = 48000
        # Live's pans are REAPER's angle with its own level: read as REAPER
        # pans at the 0 dB law, the level difference on the fader (mix)
        p.pan_law_of = 'reaper'
        p.panlaw = 1.0
        p.panmode = 3
        self.read_markers(p)
        self.read_tracks(p)
        self.read_master(p)
        for k, n in sorted(self.unsupported.items()):
            self.log.append('%d %s device(s) are Live\'s own and no other host has '
                            'them; left out' % (n, k))
        return p

    def read_markers(self, p):
        locs = sorted(((_v(l, 'Time', 0.0), _v(l, 'Name', '', str))
                       for l in self.ls.find('Locators/Locators')), key=lambda x: x[0])
        ends = {}
        for t, n in locs:
            if n.endswith(' end'):
                ends.setdefault(n[:-4], []).append(t)
        used = set()
        for t, n in locs:
            if n.endswith(' end') and n[:-4] in [x for _t, x in locs]:
                continue
            m = Marker(n, self.sec(t))
            later = [e for e in ends.get(n, []) if e > t and e not in used]
            if later:
                used.add(later[0])
                m.end = self.sec(later[0])
            p.markers.append(m)

    def mix(self, mixer, t):
        """Fader and pan of a Live mixer, as REAPER's (see the module)."""
        from . import panlaw
        vol = _v(mixer, 'Volume/Manual', 1.0)
        pan = _v(mixer, 'Pan/Manual', 0.0)
        gl, gr = panlaw.live_gains(pan)
        rl, rr = panlaw.reaper_gains(pan, 1.0, 3)
        near_l, near_r = max(gl, gr), max(rl, rr)
        t.vol = vol * (near_l / near_r if near_r > 0 else 1.0)
        t.pan = pan
        t.mute = 0 if _v(mixer, 'Speaker/Manual', True, bool) else 1
        return mixer

    def lanes(self, tr, mixer, t):
        """Volume and pan automation of a track."""
        from . import envelope, panlaw
        envs = tr.find('AutomationEnvelopes/Envelopes')
        if envs is None:
            return
        vid = mixer.find('Volume/AutomationTarget').get('Id')
        pid = mixer.find('Pan/AutomationTarget').get('Id')
        for env in envs:
            target = env.find('EnvelopeTarget/PointeeId').get('Value')
            pts = [(self.sec(b), v) for b, v in envelope_events(env) if b > START / 2]
            if not pts:
                continue
            if target == vid:
                # Live draws its lane straight in dB; the model is straight
                # in gain - points between, where the two part
                db = [(s, envelope._live_db(v)) for s, v in pts]
                dense = envelope.densify(db, lambda d: 10 ** (d / 20.0), envelope._live_db,
                                         envelope.db_err, envelope.DB_TOL)
                t.volenv = [(s, 10 ** (d / 20.0)) for s, d in dense]
            elif target == pid:
                t.panenv = pts
                # the level the Live panner adds over REAPER's rides on
                # the volume lane
                def gain(x):
                    gl, gr = panlaw.live_gains(x)
                    rl, rr = panlaw.reaper_gains(x, 1.0, 3)
                    return max(gl, gr) / max(rl, rr)
                t.volenv = envelope.volume_with_pan_gain(t.volenv, t.vol / gain(t.pan or 0.0),
                                                         pts, gain)

    # -------------------------------------------------------- tracks
    def read_tracks(self, p):
        tracks = list(self.ls.find('Tracks'))
        by_id = {tr.get('Id'): tr for tr in tracks}

        def depth(tr):
            d, g = 0, tr.find('TrackGroupId').get('Value')
            while g not in (None, '-1') and g in by_id:
                d += 1
                g = by_id[g].find('TrackGroupId').get('Value')
            return d
        returns = [tr for tr in tracks if tr.tag == 'ReturnTrack']
        index = {}
        for tr in tracks:
            t = Track(_v(tr, 'Name/EffectiveName', '', str), depth(tr) if tr.tag != 'ReturnTrack' else 0)
            t.is_folder = tr.tag == 'GroupTrack'
            t.kind = 'midi' if tr.tag == 'MidiTrack' else 'audio'
            t.color = rgb_of(_v(tr, 'Color', 69, int))
            dc = tr.find('DeviceChain')
            mixer = self.mix(dc.find('Mixer'), t)
            delay = _v(tr, 'TrackDelay/Value', 0.0)
            if delay and not _v(tr, 'TrackDelay/IsValueSampleBased', False, bool):
                t.delay = delay / 1000.0
            self.lanes(tr, mixer, t)
            self.read_devices(dc.find('DeviceChain/Devices'), t)
            if tr.tag == 'AudioTrack':
                for c in dc.find('MainSequencer/Sample/ArrangerAutomation/Events'):
                    it = self.audio_clip(c)
                    if it is not None:
                        t.items.append(it)
            elif tr.tag == 'MidiTrack':
                for c in dc.find('MainSequencer/ClipTimeable/ArrangerAutomation/Events'):
                    t.items.append(self.midi_clip(c))
            index[tr.get('Id')] = len(p.tracks)
            t.live_sends = [(_v(h, 'Send/Manual', 0.0), _v(h, 'Active', True, bool))
                            for h in dc.find('Mixer/Sends')]
            p.tracks.append(t)
        # sends: slot k of every track feeds the k-th return
        pre = [e.get('Value') == 'true' for e in self.ls.find('SendsPre')]
        ret_idx = [index[r.get('Id')] for r in returns]
        for t in p.tracks:
            for k, (g, on) in enumerate(getattr(t, 'live_sends', [])):
                if k < len(ret_idx) and on and g > LIVE_VOL_MIN * 1.01:
                    t.sends.append(Send(dest=ret_idx[k], vol=g, pan=0.0,
                                        mode=3 if (k < len(pre) and pre[k]) else 0))
        # a track inside a group plays through it; one that outputs
        # elsewhere (a return, nowhere) is said
        for tr, t in zip(tracks, p.tracks):
            t.bus_id = tr.get('Id')
        for tr, t in zip(tracks, p.tracks):
            out = _v(tr, 'DeviceChain/AudioOutputRouting/Target', '', str)
            m = re.match(r'AudioOut/Track\.(\d+)/TrackIn$', out)
            if m and m.group(1) in index:
                # 'Audio To' another track: a Cubase output to a group
                # channel, a REAPER send in place of the parent send
                t.out_bus_id = m.group(1)
            elif out not in ('AudioOut/Master', 'AudioOut/GroupTrack', ''):
                self.log.append('%r outputs to %s, which is not carried; it plays '
                                'into the master' % (t.name, out))

    def read_master(self, p):
        mt = self.ls.find('MasterTrack')
        m = Track('Master')
        m.kind = 'other'
        self.mix(mt.find('DeviceChain/Mixer'), m)
        self.lanes(mt, mt.find('DeviceChain/Mixer'), m)
        self.read_devices(mt.find('DeviceChain/DeviceChain/Devices'), m)
        p.master = m

    # ------------------------------------------------------- devices
    def plugin(self, d):
        f = Fx()
        info3 = d.find('PluginDesc/Vst3PluginInfo')
        info2 = d.find('PluginDesc/VstPluginInfo')
        on = _v(d, 'On/Manual', True, bool)
        f.bypass = 0 if on else 1
        if info3 is not None:
            words = [int(info3.find('Uid/Fields.%d' % k).get('Value')) for k in range(4)]
            f.uid = struct.pack('>4i', *words).hex().upper()
            f.name = _v(info3, 'Name', '', str)
            pre = info3.find('Preset/Vst3Preset')
            f.component = bytes.fromhex((pre.find('ProcessorState').text or '').strip())
            f.controller = bytes.fromhex((pre.find('ControllerState').text or '').strip())
            f.is_instrument = _v(info3, 'DeviceType', 2, int) == 1
            f.format = 'VST3'
            return f
        if info2 is not None:
            f.name = _v(info2, 'PlugName', '', str)
            unique = _v(info2, 'UniqueId', 0, int)
            f.vst2_id = unique
            cc = struct.pack('>i', unique)
            name = (f.name or '').lower().encode('ascii', 'replace')[:9].ljust(9, b'\0')
            f.uid = (b'VST' + cc + name).hex().upper()
            pre = info2.find('Preset/VstPreset')
            buf = bytes.fromhex((pre.find('Buffer').text or '').strip())
            kind = struct.pack('>i', _v(pre, 'Type', 0, int))
            if kind in (b'FxBk', b'FxCk'):
                # a parameter list: REAPER's parameter dump is the same values
                f.param_dump = True
                f.raw_state = bytes.fromhex('EFBEADDE0DF0ADDE') + buf[28:]
                f.component = b''
            else:
                f.raw_state = buf
                f.component = buf
            f.format = 'VST'
            f.is_instrument = _v(info2, 'Category', 1, int) == 2
            return f
        return None

    def read_devices(self, devs, t):
        if devs is None:
            return
        for d in devs:
            if d.tag == 'PluginDevice':
                f = self.plugin(d)
                if f is None:
                    continue
                if f.is_instrument and t.instrument is None:
                    t.instrument = f
                else:
                    t.fx.append(f)
            elif d.tag == 'InstrumentGroupDevice':
                # an Instrument Rack: each chain's instrument plays the
                # track's MIDI, its effects after it; REAPER stacks the
                # instruments in one chain the same way (als_write)
                for br in d.find('Branches'):
                    chain = br.find('DeviceChain/MidiToAudioDeviceChain/Devices')
                    for k, x in enumerate(chain):
                        if x.tag != 'PluginDevice':
                            self.unsupported[x.tag] = self.unsupported.get(x.tag, 0) + 1
                            continue
                        f = self.plugin(x)
                        if f is None:
                            continue
                        if k == 0 and t.instrument is None:
                            t.instrument = f
                        else:
                            f.is_instrument = f.is_instrument or k == 0
                            t.fx.append(f)
            elif d.tag == 'StereoGain':
                # Utility: its gain (and mute) fold into the fader - the
                # only thing als_write puts it there for
                g = _v(d, 'Gain/Manual', 1.0)
                if _v(d, 'On/Manual', True, bool):
                    t.vol *= g
            else:
                self.unsupported[d.tag] = self.unsupported.get(d.tag, 0) + 1

    # --------------------------------------------------------- clips
    def file_of(self, c):
        ref = c.find('SampleRef/FileRef')
        rel = _v(ref, 'RelativePath', '', str)
        path = _v(ref, 'Path', '', str)
        for cand in (os.path.join(self.dir, rel) if rel else None, path):
            if cand and os.path.isfile(cand):
                return os.path.normpath(cand)
        return os.path.normpath(os.path.join(self.dir, rel)) if rel else path

    def audio_clip(self, c):
        it = Item()
        it.kind = 'audio'
        b0, b1 = _v(c, 'CurrentStart', 0.0), _v(c, 'CurrentEnd', 0.0)
        it.pos = self.sec(b0)
        it.length = max(0.0, self.sec(b1) - it.pos)
        it.name = _v(c, 'Name', '', str)
        it.mute = 1 if _v(c, 'Disabled', False, bool) else 0
        it.color = rgb_of(_v(c, 'Color', 69, int))
        it.gain = _v(c, 'SampleVolume', 1.0)
        it.file = self.file_of(c)
        warped = _v(c, 'IsWarped', False, bool)
        ls_ = _v(c, 'Loop/LoopStart', 0.0)
        it.loop = _v(c, 'Loop/LoopOn', False, bool)
        fades = c.find('Fades')
        fin, fout = _v(fades, 'FadeInLength', 0.0), _v(fades, 'FadeOutLength', 0.0)
        if not warped:
            it.soffs = ls_
            it.fadein, it.fadeout = fin, fout
        else:
            marks = sorted((float(m.get('BeatTime')), float(m.get('SecTime')))
                           for m in c.find('WarpMarkers'))
            if len(marks) >= 2:
                def src(b):
                    for (x0, y0), (x1, y1) in zip(marks, marks[1:]):
                        if b <= x1 or (x1, y1) == marks[-1]:
                            return y0 + (y1 - y0) * (b - x0) / (x1 - x0) if x1 > x0 else y0
                    return marks[-1][1]
                it.soffs = src(ls_)
                span_src = src(ls_ + (b1 - b0)) - it.soffs
                it.playrate = span_src / it.length if it.length > 0 else 1.0
                if len(marks) > 2:
                    # more than a straight stretch: REAPER's stretch markers
                    it.stretch_markers = [(y, self.sec(b0 + x - ls_) - it.pos) for x, y in marks]
            wm = _v(c, 'WarpMode', 6, int)
            it.preserve_pitch = wm != 3
            it.pitch = _v(c, 'PitchCoarse', 0.0) + _v(c, 'PitchFine', 0.0) / 100.0
            it.fadein = self.sec(b0 + fin) - it.pos if fin else 0.0
            it.fadeout = it.pos + it.length - self.sec(b1 - fout) if fout else 0.0
        if getattr(it, 'file', '') and os.path.splitext(it.file)[1].lower() in ('.mp4', '.mov', '.m4v'):
            it.kind = 'video'
        return it

    def midi_clip(self, c):
        it = Item()
        it.kind = 'midi'
        b0, b1 = _v(c, 'CurrentStart', 0.0), _v(c, 'CurrentEnd', 0.0)
        ls_ = _v(c, 'Loop/LoopStart', 0.0)
        it.pos = self.sec(b0)
        it.length = max(0.0, self.sec(b1) - it.pos)
        it.name = _v(c, 'Name', '', str)
        it.mute = 1 if _v(c, 'Disabled', False, bool) else 0
        it.color = rgb_of(_v(c, 'Color', 69, int))
        it.ppq = 960.0
        it.ticks = (b1 - b0) * it.ppq
        notes = []
        for kt in c.find('Notes/KeyTracks'):
            pitch = _v(kt, 'MidiKey', 60, int)
            for n in kt.find('Notes'):
                if n.get('IsEnabled') == 'false':
                    continue
                t0 = float(n.get('Time')) - ls_
                if t0 < -1e-9 or t0 >= (b1 - b0) - 1e-9:
                    continue
                ln = min(float(n.get('Duration')), (b1 - b0) - t0)
                notes.append((t0 * it.ppq, ln * it.ppq, 0, pitch,
                              int(round(float(n.get('Velocity')))), 64))
        it.notes = sorted(notes)
        return it


def read(path, log=None):
    log = log if log is not None else []
    r = Reader(path, log)
    p = r.project()
    p.log = log
    return p
