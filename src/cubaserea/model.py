"""The intermediate project model both sides read and write.

Times are in seconds, gains are linear (1.0 = 0 dB), pan is -1..+1.
"""
import os


class Project:
    def __init__(self):
        self.name = ''
        self.samplerate = 48000
        self.tempo = [(0.0, 120.0)]     # (seconds, bpm)
        self.tsig = (4, 4)
        self.markers = []               # Marker
        self.tracks = []                # Track, in display order
        self.master = None              # Cubase output bus -> REAPER master
        self.src = {}                   # where the track list lives in a .cpr
        self.srcdir = ''                # folder the project was read from
        self.header_keep = []           # REAPER header lines a print repeats
        self.panlaw = None              # gain at centre pan: 1.0 = 0 dB, 0.707 = -3 dB
        self.panlaw_code = None         # Cubase's Project Setup code (see PAN_LAW_CODES)
        self.panmode = 3                # REAPER's PANMODE (3 stereo balance, 5 stereo pan, 6 dual)
        # whose panner the tracks' pan values belong to: 'reaper' or 'cubase'
        # (see panlaw.py - the two curves differ, and a writer for the other
        # host maps the value and compensates on the fader); None = the
        # writer's own host, so a model built in code passes straight through
        self.pan_law_of = None
        self.start = 0.0                # Cubase Project Setup > Start, seconds (negative = pre-roll before bar 1)

    def folder_children(self, i):
        """Indices of the tracks nested under tracks[i]."""
        out = []
        d = self.tracks[i].depth
        for j in range(i + 1, len(self.tracks)):
            if self.tracks[j].depth <= d:
                break
            out.append(j)
        return out


class Marker:
    def __init__(self, name='', start=0.0, end=None):
        self.name = name
        self.start = start
        self.end = end              # None for a point marker
        self.rid = None             # REAPER's own number for it
        self.src = {}               # where it lives in a .cpr


class Track:
    def __init__(self, name='', depth=0):
        self.name = name
        self.depth = depth
        self.is_folder = False
        self.kind = 'audio'         # audio | midi | other
        self.color = None           # (r, g, b)
        self.vol = 1.0
        self.pan = 0.0              # the source host's own panner value, -1..+1
        self.panmode = None         # REAPER per-track PANMODE override, else the project's
        self.delay = 0.0            # track delay in seconds, + later / - earlier: Cubase's
                                    # Inspector Delay, REAPER's media playback offset
        self.mute = 0
        self.solo = 0
        self.disabled = False       # Cubase 'Disable Track': plays nothing, plug-ins unloaded
        self.mono = False           # a Cubase mono channel (bus arrangement mono, its own
                                    # panner: panlaw.cubase_mono_gains); a stereo channel
                                    # playing mono files is not one
        self.chan_eq = []           # Cubase channel EQ bands (index, type, dB, Hz, Q), see chan_eq.py
        self.fx = []                # Fx, insert effects in order
        self.instrument = None      # Fx, the VST instrument if any
        self.items = []             # Item
        self.volenv = []            # (seconds, linear gain)
        self.panenv = []            # (seconds, -1..+1)
        self.volenv_idle = []       # lanes kept but not playing: Cubase automation Read
        self.panenv_idle = []       # off / REAPER envelope ACT 0 (same units as above)
        self.auto_read = True       # Cubase's automation Read switch for the track
        self.sends = []             # Send
        self.versions = []          # Cubase track version names
        self.lane_names = []        # one per fixed lane, when versions are used
        self.active_lane = 0
        self.lanes_playing = 1      # how many REAPER lanes were set to play
        self.src = {}               # where this track lives in a .cpr
        self.origin = {}            # byte offsets of this track's mixer
                                    # fields in a .cpr, for writing back
        self.vol_pos = None         # Cubase fader position (0..1), the
                                    # half of the fader it plays by
        self.bus_id = None          # source id, for resolving sends
        self.out_bus_id = None


class Fx:
    def __init__(self):
        self.name = ''
        self.uid = ''               # 32 hex chars, Cubase/VST3 class id
        self.vst2_id = None         # int, when the plug-in is a VST2
        self.component = b''        # VST3 IComponent / VST2 chunk
        self.controller = b''       # VST3 IEditController state
        self.bypass = 0
        self.offline = 0
        self.preset = ''
        self.n_in = 2
        self.n_out = 2
        self.origin = {}          # where this plug-in's state lives in a .cpr
        self.envelopes = []          # (parameter index, [(seconds, 0..1)])
        self.envelopes_idle = []     # the same, kept but not playing (Read off / ACT 0)
        self.native = False         # exists only in REAPER (Rea*, JS...)
        self.chain_pos = 0          # its place in the REAPER chain
        self.raw_group = None       # its lines of the .rpp, for printing
        self.is_instrument = False  # the chain's synth rather than an insert
        self.mapped_from = ''       # the format it was written in, when that
                                    # is not the one it is being loaded as
                                    # ('CLAP' - see rpp_read.clap_as_vst)
        self.clap_id = ''           # a CLAP's reverse-DNS id
        self.param_dump = False     # state is REAPER's parameter dump only

    @property
    def is_vst2(self):
        return self.vst2_id is not None


class Item:
    def __init__(self):
        self.kind = 'audio'         # audio | midi
        self.pos = 0.0
        self.length = 0.0
        self.soffs = 0.0            # start offset into the source file
        self.soffs_qn = None        # the same offset in quarter notes, which
                                    # is what REAPER's second SOFFS field
                                    # holds for a MIDI item - the window onto
                                    # the source starts there
        self.name = ''
        self.color = None           # (r, g, b) of the item/event, else the track's
        self.file = None
        self.channels = None        # the file's channel count when the project says
                                    # (a Cubase clip records it), else None
        self.zorder = None          # Cubase's front-to-back serial: where events
                                    # overlap the highest plays (flatten_lanes)
        self.mute = 0
        self.gain = 1.0
        self.fadein = 0.0           # seconds
        self.fadeout = 0.0
        self.panenv = []            # take pan envelope: (seconds into the
                                    # item, -1..+1); nothing in Cubase
        self.volenv = []            # take volume envelope: (seconds into the
                                    # item, linear gain)
        self.pitch = 0.0            # semitones of pitch shift on the item
        self.pitchenv = []          # take pitch envelope: (seconds, semitones)
        self.loop = False           # REAPER loops the source to fill the item
        self.preserve_pitch = True  # a stretched item keeps its pitch
        # REAPER takes: alternatives for the same stretch of time inside one
        # item, one of them playing. Cubase has no takes as such; the same
        # thing there is events overlapping on one track with the audible one
        # on top, which is what its lanes display.
        self.take_group = None      # items from one multi-take item share this
        self.take_no = 0            # which take of that item this is
        self.take_sel = True        # the take REAPER plays
        self.takefx = None          # REAPER take FX blocks (raw), for printing
        self.stretch_markers = []   # REAPER SM pairs (source s, item s): a warped take
        self.fade_lines = {}        # REAPER's FADEIN/FADEOUT tokens (shape, length, curve...)
        self.takefx_fx = []         # the same chain as Fx objects
        self.notes = []             # (pos_ticks, len_ticks, chan, pitch, vel)
        self.ccs = []               # (pos_ticks, status, d1, d2) - CC, bend, ...
        self.lane = 0               # fixed-lane index
        self.ppq = 480.0            # ticks per quarter for `notes`
        self.ticks = 0.0            # part length in those ticks
        self.origin = None          # byte offsets of this event in a .cpr
        self.events = None          # where a MIDI part's note records live
        self.playrate = 1.0         # source seconds per timeline second
        self.warped = False         # came from a Cubase musical-mode clip
        self.section = None         # REAPER <SOURCE SECTION>: dict(startpos,
                                    # length, mode, overlap, reverse)
        self.file_args = []         # tokens after the path on REAPER's FILE
                                    # line ('1' behind an MP3 = gapless trim)


class Send:
    def __init__(self, dest=None, vol=1.0, pan=0.0, mode=0):
        self.dest = dest            # track index, filled in by the reader
        self.dest_bus = None        # raw destination id before resolution
        self.vol = vol
        self.pan = pan
        self.mode = mode            # 0 post-fader, 1 pre-fx, 3 pre-fader
        self.mute = False           # switched off: sends nothing


def output_routes(proj):
    """(track index, group track index) for every Cubase channel whose
    output is another channel (a group or FX channel) rather than Stereo
    Out, leaving out a track inside the folder it outputs to - a REAPER
    folder sums it already. In REAPER each of these stops feeding its
    parent and sends to the group at unity, post-fader."""
    owner = {}
    for i, t in enumerate(proj.tracks):
        if getattr(t, 'bus_id', None) is not None:
            owner.setdefault(t.bus_id, i)
    out, stack = [], []
    for i, t in enumerate(proj.tracks):
        while stack and proj.tracks[stack[-1]].depth >= t.depth:
            stack.pop()
        g = owner.get(getattr(t, 'out_bus_id', None))
        if g is not None and g != i and g not in stack:
            out.append((i, g))
        if t.is_folder:
            stack.append(i)
    return out


def lane_items(track, lane):
    """Every item on one lane, alternate takes included."""
    if len(track.lane_names) > 1:
        return [i for i in track.items if i.lane == lane]
    return list(track.items)


def comp_items(track):
    """The items on the lane the track plays, alternate takes included.

    This is what a Cubase track holds: the events of the active version,
    with the takes of a multi-take item stacked on top of each other."""
    return lane_items(track, track.active_lane)


def playing_items(track):
    """The items that actually sound: the active lane, selected takes only."""
    return [i for i in comp_items(track) if i.take_sel]


def other_lanes(track):
    """(lane index, name, items) for every lane the track does not play.

    Each becomes a Cubase track version."""
    if len(track.lane_names) <= 1:
        return []
    return [(k, name, lane_items(track, k))
            for k, name in enumerate(track.lane_names)
            if k != track.active_lane]


def muted_lane_items(track):
    """How many items the other lanes hold, for reporting."""
    return sum(len(items) for _k, _n, items in other_lanes(track))


def alternate_takes(track):
    """How many items on the playing lane are takes that do not sound."""
    return sum(1 for i in comp_items(track) if not i.take_sel)


def _qn_upto(tempo, seconds):
    """Quarter notes from the project start to `seconds`."""
    if not tempo:
        return seconds * 2.0
    qn = 0.0
    prev_s, prev_bpm = 0.0, tempo[0][1]
    for s, bpm in tempo:
        if s > seconds:
            break
        qn += (s - prev_s) * prev_bpm / 60.0
        prev_s, prev_bpm = s, bpm
    return qn + (seconds - prev_s) * prev_bpm / 60.0


def mute_inside_folders(project, log=None):
    """Carry a muted folder's mute down to the tracks it holds.

    A muted folder is not a track that plays nothing - it silences
    everything inside it, which is the whole point of muting one. The
    folder itself holds no events to mute, so the only way to say the same
    thing in Cubase is to mute what is inside. Without it a muted REF
    folder played its whole reference mix over the song.

    Returns the track indices it muted."""
    inside = set()
    for i, t in enumerate(project.tracks):
        if t.is_folder and t.mute:
            for k in project.folder_children(i):
                inside.add(k)
    for k in inside:
        project.tracks[k].mute = 1
    if inside and log is not None:
        log.append('%d muted REAPER folder(s) silence everything inside '
                   'them, so the mute was carried down to the %d track(s) '
                   'they hold - a folder has no events of its own to mute'
                   % (sum(1 for t in project.tracks if t.is_folder and t.mute),
                      len(inside)))
    return inside


def window_midi(project, log=None):
    """Cut every MIDI part down to the stretch of its source the item shows.

    A REAPER MIDI item is a window onto its source, not the whole of it: the
    second SOFFS field says how far into the source the window opens, in
    quarter notes, and the item's own length says how much of it is shown.
    Splitting or gluing MIDI leaves items whose source runs far past them -
    four items here windowing one thirty-two beat take, one of them opening
    11.5 beats in - and REAPER plays only what falls inside the window.

    The whole source used to be copied into the Cubase part, so a part in
    exactly the right place held the wrong music: the notes started from the
    beginning of the take instead of from the window, and everything past
    the item's end came along too. Notes are shifted back to the window's
    start, anything outside it is dropped, and a note running past the end
    is cut there, which is where REAPER stops sounding it.

    CPR_NO_MIDI_WINDOW=1 leaves the parts whole, for comparison."""
    import os
    if os.environ.get('CPR_NO_MIDI_WINDOW'):
        return 0
    tempo = sorted(project.tempo or [(0.0, 120.0)])
    n_items = n_dropped = n_shifted = 0
    for t in project.tracks:
        for it in t.items:
            if it.kind != 'midi' or not (it.notes or it.ccs):
                continue
            ppq = it.ppq or 480.0
            if it.soffs_qn is not None:
                start_qn = it.soffs_qn
            else:
                bpm = tempo[0][1]
                for s, b in tempo:
                    if s <= it.pos:
                        bpm = b
                start_qn = it.soffs * bpm / 60.0
            span_qn = (_qn_upto(tempo, it.pos + it.length)
                       - _qn_upto(tempo, it.pos))
            lo = start_qn * ppq
            hi = lo + span_qn * ppq
            if lo <= 1e-6 and hi >= (it.ticks or 0) - 1e-6:
                continue                # the item shows all of its source
            notes, dropped = [], 0
            for pos, ln, ch, pitch, vel, *rest in it.notes:
                if pos < lo - 1e-6 or pos >= hi - 1e-6:
                    dropped += 1
                    continue
                # max(0): a note on the item's first tick comes out a hair
                # below zero (28 - 0.02916666666667 * 960) and a part drops
                # a note that starts before it - ZITRO's Bass lost its first
                notes.append((max(0.0, pos - lo), max(1.0, min(ln, hi - pos)),
                              ch, pitch, vel) + tuple(rest))
            ccs = [(max(0.0, pos - lo), st, d1, d2)
                   for pos, st, d1, d2 in (it.ccs or ())
                   if lo - 1e-6 <= pos < hi - 1e-6]
            n_dropped += dropped
            if lo > 1e-6:
                n_shifted += 1
            it.notes, it.ccs = notes, ccs
            it.ticks = max(0.0, hi - lo)
            it.soffs = 0.0
            it.soffs_qn = 0.0
            n_items += 1
    if n_items and log is not None:
        log.append('%d MIDI part(s) show only a stretch of a longer source, '
                   'the way a split or glued take does: %d note(s) outside '
                   'the item were left out and %d part(s) had their notes '
                   'moved back to where the item opens. REAPER plays only '
                   'what is inside the item, and this is what that is'
                   % (n_items, n_dropped, n_shifted))
    return n_items


def split_mixed(project, log=None):
    """Give every track Cubase would build as an instrument its audio a
    track of its own.

    REAPER lets one track carry MIDI and audio items, and even takes of both
    kinds on one item. Cubase has instrument tracks and audio tracks and
    nothing that is both, so the audio goes on a track of its own named
    after the original, right below it, and nothing is left behind. A take
    that REAPER did not play is muted on the track it lands on, since Cubase
    would otherwise play whichever is on top.

    A track holding an instrument counts even with no MIDI on it at all: the
    builder makes an instrument track of anything with a synth in its chain,
    and an instrument track has nowhere to put an audio event. Splitting
    only on 'MIDI and audio both present' left that case out, and three
    audio events on a synth-carrying track went missing without a word.

    Sends refer to tracks by position, so they are re-pointed."""
    import copy
    out = []
    moved = {}          # old index -> new index
    n_split = 0
    for old_i, t in enumerate(project.tracks):
        moved[old_i] = len(out)
        kinds = set(i.kind for i in t.items)
        # what the builder will make of this track: anything with a synth in
        # its chain, or any MIDI on it, becomes an instrument or MIDI track,
        # and neither can hold an audio event
        as_instrument = t.instrument is not None or 'midi' in kinds
        if t.is_folder or not ('audio' in kinds and as_instrument):
            out.append(t)
            continue
        sel_kind = {}
        for i in t.items:
            if i.take_group is not None and i.take_sel:
                sel_kind[i.take_group] = i.kind
        a = copy.copy(t)
        a.name = '%s (audio)' % t.name
        a.kind = 'audio'
        a.instrument = None
        a.fx = []
        a.sends = []
        a.volenv = []
        a.panenv = []
        a.items = [i for i in t.items if i.kind == 'audio']
        t.items = [i for i in t.items if i.kind != 'audio']
        # only the take REAPER plays stays audible - on either track. Two
        # audio takes of one item used to land unmuted on top of each
        # other and Cubase played the one on top: the older render, not
        # the one REAPER plays, and the two differed at the end
        for i in a.items:
            if i.take_group is not None and not i.take_sel:
                i.mute = 1
        for i in t.items:
            if i.take_group is not None and not i.take_sel:
                i.mute = 1
        out.append(t)
        out.append(a)
        n_split += 1
        if log is not None:
            log.append('%r %s; Cubase has no track that holds both, so '
                       'its %d audio item(s) went on %r right below it, with '
                       'takes REAPER did not play muted'
                       % (t.name,
                          'carries both MIDI and audio' if 'midi' in kinds
                          else 'carries audio and a synth in its chain, which '
                               'makes it an instrument track in Cubase',
                          len(a.items), a.name))
    if n_split:
        project.tracks = out
        _repoint_sends(project, moved)
    return n_split


def _repoint_sends(project, moved):
    for t in project.tracks:
        for s in t.sends:
            if s.dest is not None:
                s.dest = moved.get(s.dest, s.dest)


def last_synth_only(project, log=None):
    """Keep only the last instrument in each REAPER chain. DO NOT USE for
    an ordinary REAPER project: it is wrong.

    This was written on the theory that a synth's outputs overwrite the
    track channels, so only the last synth in a chain would be heard. It
    was then measured (2026-09-18) by rendering one track three ways through
    the converter's own stem path: first synth alone -40.3 dBFS, last synth
    alone -38.1 dBFS, all three together -33.1 dBFS - louder than either
    and than the two summed. REAPER sums stacked synths; every one is heard.
    So one Cubase track per synth, which split_instruments does, is the
    faithful conversion, and this switch silences music that plays.

    Kept only for a project whose owner knows its synths were deliberately
    made inaudible some other way. CPR_LAST_SYNTH_ONLY=1."""
    n = 0
    for t in project.tracks:
        if t.is_folder or t.instrument is None:
            continue
        chain = ([t.instrument] + list(t.fx))
        synths = [f for f in chain if getattr(f, 'is_instrument', False)]
        if len(synths) < 2:
            continue
        keep = max(synths, key=lambda f: getattr(f, 'chain_pos', 0))
        dropped = [f for f in synths if f is not keep]
        t.instrument = keep
        t.fx = [f for f in t.fx if f is not keep and f not in dropped]
        n += len(dropped)
        if log is not None:
            log.append('%r had %d instruments in its chain and only %r, the '
                       'last of them, is kept, because CPR_LAST_SYNTH_ONLY '
                       'is set. NOTE: REAPER sums stacked synths (measured), '
                       'so this silences music that plays in REAPER. %s left '
                       'out'
                       % (t.name, len(synths), keep.name,
                          ', '.join(f.name or 'instrument' for f in dropped)))
    return n


def split_instruments(project, log=None):
    """One Cubase instrument track per instrument on a REAPER track.

    REAPER lets several instruments sit in one track's effect chain, every
    one of them fed the track's MIDI - five Hives layered on one track. A
    Cubase instrument track holds one instrument, so each further one gets
    a track of its own right below, carrying the same MIDI parts and its own
    patch. The insert effects stay on the first track: in REAPER they sat
    after the instruments and processed their sum, which Cubase can only
    approximate; the summary says so.

    Whether REAPER played them all is a separate question - see
    last_synth_only, which runs first when it is asked for and leaves
    nothing here to split."""
    import copy
    import os
    if os.environ.get('CPR_LAST_SYNTH_ONLY'):
        last_synth_only(project, log)
    out = []
    moved = {}
    n_split = 0
    for old_i, t in enumerate(project.tracks):
        extra = [f for f in t.fx if getattr(f, 'is_instrument', False)]
        if t.is_folder or t.instrument is None or not extra:
            moved[old_i] = len(out)
            out.append(t)
            continue
        # REAPER plays the chain in series: each instrument adds its sound
        # to what reaches it, and the effects after the last one process
        # the sum. In Cubase: a folder whose group channel carries those
        # effects, the track's level, pan and sends, and one instrument
        # track per instrument inside it, each with the effects that sat
        # between it and the next instrument, at unity
        chain = list(t.fx)
        last = max(k for k, f in enumerate(chain) if getattr(f, 'is_instrument', False))
        post = chain[last + 1:]
        segs, cur, insts = [], [], [t.instrument]
        for f in chain[:last + 1]:
            if getattr(f, 'is_instrument', False):
                segs.append(cur)
                cur = []
                insts.append(f)
            else:
                cur.append(f)
        segs.append(cur)                 # the last instrument has none before the post chain
        g = copy.copy(t)
        g.is_folder = True
        g.items = []
        g.instrument = None
        g.fx = post
        g.name = t.name
        g.kind = 'other'
        moved[old_i] = len(out)
        out.append(g)
        midi = [i for i in t.items if i.kind == 'midi']
        for k, inst in enumerate(insts):
            a = copy.copy(t)
            a.is_folder = False
            a.depth = t.depth + 1
            a.name = t.name if k == 0 else '%s (%s %d)' % (t.name, inst.name or 'instrument', k + 1)
            a.instrument = inst
            a.fx = segs[k]
            a.sends = []
            a.vol, a.pan = 1.0, 0.0
            a.volenv, a.panenv = [], []
            a.volenv_idle, a.panenv_idle = [], []
            a.mute = 0
            a.bus_id = None
            a.out_bus_id = None
            a.items = list(t.items) if k == 0 else [copy.copy(i) for i in midi]
            out.append(a)
        g.name = t.name + ' (layers)'
        n_split += len(insts) - 1
        if log is not None:
            log.append('%r has %d instruments in its chain; Cubase holds one per track, so each '
                       'got a track inside the group %r, which carries the effects after them '
                       '(%s), the level and the sends - the sum is processed as in REAPER'
                       % (t.name, len(insts), g.name, ', '.join(f.name for f in post) or 'none'))
    if n_split:
        project.tracks = out
        _repoint_sends(project, moved)
    return n_split


def split_folder_items(project, log=None):
    """A REAPER folder track can carry items of its own; a Cubase folder
    cannot. They go on a track of their own as the folder's first child."""
    import copy
    out = []
    moved = {}
    n = 0
    for old_i, t in enumerate(project.tracks):
        moved[old_i] = len(out)
        out.append(t)
        if not t.is_folder or not t.items:
            continue
        a = copy.copy(t)
        a.is_folder = False
        a.depth = t.depth + 1
        a.name = '%s (items)' % t.name
        a.kind = 'midi' if any(i.kind == 'midi' for i in t.items) else 'audio'
        a.fx = []
        a.sends = []
        a.volenv = []
        a.panenv = []
        a.items = list(t.items)
        t.items = []
        out.append(a)
        n += 1
        if log is not None:
            log.append('%r is a folder with %d item(s) on it; a Cubase folder '
                       'holds no events, so they went on %r inside it'
                       % (t.name, len(a.items), a.name))
    if n:
        project.tracks = out
        _repoint_sends(project, moved)
    return n


def split_item_fx(project, log=None):
    """An item with FX of its own becomes a track of its own.

    Cubase has no plug-ins on an event. A REAPER item carrying take FX is
    exactly equivalent to a track holding that one item with the same chain
    as its inserts (followed by the track's own inserts), so that is what it
    becomes: an audio track named after the track and the item, right below
    the original, with the item's FX as insert effects - settings intact,
    nothing printed, no other program needed. With CPR_PRINT_WITH_REAPER=1
    the items are rendered through REAPER instead (see render_reaper)."""
    import copy
    from . import media
    if not os.environ.get('CPR_NO_REAPER') and media.find_reaper():
        return 0            # REAPER prints them instead (media.render)
    out = []
    moved = {}
    n = 0
    for old_i, t in enumerate(project.tracks):
        moved[old_i] = len(out)
        out.append(t)
        if t.is_folder:
            continue
        with_fx = [i for i in t.items if i.kind == 'audio' and i.takefx_fx]
        if not with_fx:
            continue
        t.items = [i for i in t.items if i not in with_fx]
        for k, it in enumerate(with_fx):
            a = copy.copy(t)
            a.name = '%s (%s)' % (t.name, it.name or 'item %d' % (k + 1))
            a.kind = 'audio'
            a.instrument = None
            a.fx = list(it.takefx_fx) + list(t.fx)
            a.sends = list(t.sends)
            a.items = [it]
            it.takefx = None
            it.takefx_fx = []
            out.append(a)
            n += 1
        if log is not None:
            log.append('%r: %d item(s) carry FX of their own, which Cubase has '
                       'no place for on an event; each is now a track below '
                       'the original with that chain as its inserts'
                       % (t.name, len(with_fx)))
            js = sum(1 for i in with_fx for blk in (i.takefx or [])
                     for b in blk.blocks if b.name in ('JS', 'CLAP', 'AU'))
            if js:
                log.append('%r: %d of those item plug-ins are JS/CLAP/AU, '
                           'which Cubase cannot load; they are left out '
                           '(set CPR_PRINT_WITH_REAPER=1 to print such items '
                           'through REAPER instead)' % (t.name, js))
    if n:
        project.tracks = out
        _repoint_sends(project, moved)
    return n


def split_item_pan(project, log=None):
    """An item with a pan curve of its own becomes a track of its own,
    the curve its pan automation.

    Cubase has no pan on an event. A REAPER take pan is a plain linear
    balance - the louder side stays at unity, the other falls by the pan -
    whatever the project's pan mode (rendered, PANMODE 3, 2026-10-01): the
    law of Cubase's stereo balance panner. So the item moves to a track
    below the original - same inserts, sends and fader - whose pan
    automation is the curve, as it is (panenv_law 'balance': no pan law to
    convert, no level to ride), and nothing is printed. A track that pans as well keeps its
    items, which are then printed."""
    import copy
    out = []
    moved = {}
    n = 0
    for old_i, t in enumerate(project.tracks):
        moved[old_i] = len(out)
        out.append(t)
        if t.is_folder or t.panenv or abs(t.pan or 0.0) > 1e-9:
            continue
        with_pan = [i for i in t.items if i.kind == 'audio' and i.panenv
                    and (len(i.panenv) > 1 or abs(i.panenv[0][1]) > 1e-4)]
        if not with_pan:
            continue
        if len(t.items) == 1:
            # the track's only item: the curve goes on the track itself
            it = with_pan[0]
            t.panenv = [(it.pos + max(0.0, s), v) for s, v in sorted(it.panenv)]
            t.panenv_law = 'balance'
            it.panenv = []
            n += 1
            continue
        t.items = [i for i in t.items if i not in with_pan]
        for k, it in enumerate(with_pan):
            a = copy.copy(t)
            a.name = '%s (%s pan)' % (t.name, it.name or 'item %d' % (k + 1))
            a.items = [it]
            a.fx = list(t.fx)
            a.sends = list(t.sends)
            # item seconds -> project seconds; the value holds outside the
            # item, where the track plays nothing
            pts = sorted(it.panenv)
            a.panenv = [(it.pos + max(0.0, s), v) for s, v in pts]
            a.panenv_law = 'balance'
            it.panenv = []
            out.append(a)
            n += 1
        if log is not None:
            log.append('%r: %d item(s) carry a pan curve of their own, which Cubase '
                       'has no place for on an event; each is now a track below the '
                       'original with the curve as its pan automation' % (t.name, len(with_pan)))
    if n:
        project.tracks = out
        _repoint_sends(project, moved)
    return n


def merge_video_audio(project, log=None):
    """The inverse of split_video_audio, for Cubase -> REAPER.

    An audio track split_video_audio made ('<name> (video audio)', one
    event per video event at the same place and length) is folded back
    into the video: in REAPER the video item plays its own sound again, at
    the audio event's gain and fades, and the extra track goes. Cubase
    keeps one video track ('VIDEO'), and the audio track can sit anywhere
    in the list, so the events are matched by place, not by neighbour.
    Returns how many tracks were folded back."""
    vids = [i for t in project.tracks if t.kind == 'video'
            for i in t.items if i.kind == 'video']
    n = 0
    keep = []
    for a in project.tracks:
        if not (a.kind == 'audio' and a.name.endswith(' (video audio)')
                and a.items and vids):
            keep.append(a)
            continue
        pairs = []
        for ai in a.items:
            v = next((i for i in vids if abs(i.pos - ai.pos) < 1e-3
                      and abs(i.length - ai.length) < 1e-3
                      and not getattr(i, 'video_sound', False)), None)
            if v is None or ai.kind != 'audio':
                pairs = None
                break
            pairs.append((v, ai))
        if not pairs:
            keep.append(a)
            continue
        for v, ai in pairs:
            v.gain = (ai.gain if ai.gain is not None else 1.0) * (a.vol or 1.0)
            v.mute = ai.mute or a.mute
            v.fadein, v.fadeout = ai.fadein, ai.fadeout
            v.video_sound = True
        n += 1
    if n:
        project.tracks = keep
        if log is not None:
            log.append('%d video audio track(s) folded back: in REAPER the video '
                       'item plays its own sound again' % n)
    return n


def split_video_audio(project, out_path, log=None):
    """The sound of a video item, on an audio track of its own.

    REAPER plays a video file's audio stream through the video item like
    any other take; Cubase's video track plays no audio at all (its audio
    has to be extracted onto an audio track by hand). So the stream is
    extracted with ffmpeg into the project's Audio folder and placed on an
    audio track right after the video track, at the item's position, gain
    and fades. A video track turned down to nothing, or muted, sends
    nothing. Returns how many audio items were made."""
    import copy
    from . import media
    out = []
    moved = {}
    n = 0
    for old_i, t in enumerate(project.tracks):
        moved[old_i] = len(out)
        out.append(t)
        if t.kind != 'video' or not t.items or t.vol <= 1e-6 or t.mute:
            continue
        # an item whose take is turned down to nothing plays no audio (a
        # REAPER project that came from Cubase has its video silent)
        vitems = [i for i in t.items if i.kind == 'video' and i.file and not i.mute
                  and (i.gain if i.gain is not None else 1.0) > 1e-6]
        if not vitems:
            continue
        made = media.extract_video_audio(project, out_path, vitems, log)
        if not made:
            continue
        a = copy.copy(t)
        a.name = '%s (video audio)' % t.name
        a.kind = 'audio'
        a.is_folder = False
        a.instrument = None
        a.fx = list(t.fx)
        a.sends = list(t.sends)
        a.items = []
        for it in vitems:
            path = made.get(id(it))
            if not path:
                continue
            ai = copy.copy(it)
            ai.kind = 'audio'
            ai.file = path
            ai.take_group = None
            a.items.append(ai)
            n += 1
        if a.items:
            out.append(a)
            if log is not None:
                log.append('%r: REAPER plays the audio of its %d video item(s), '
                           'and a Cubase video track plays none, so that audio '
                           'was extracted to %r right below it'
                           % (t.name, len(a.items), a.name))
    if n:
        project.tracks = out
        _repoint_sends(project, moved)
    return n


def expand_for_cubase(project, log=None):
    """Everything a REAPER track can hold that a Cubase track cannot.

    Call window_midi before this when the project was READ from a .rpp - it
    decides what each part holds, and the splits here copy those parts onto
    the tracks they make. It is not called from in here because this also
    models a REAPER project the tool itself wrote from a .cpr, whose items
    are already exactly their sources; windowing that trimmed notes off
    parts which were right to begin with."""
    return (split_folder_items(project, log) + split_item_fx(project, log)
            + split_item_pan(project, log)
            + split_mixed(project, log) + split_instruments(project, log))


# ------------------------------------------------------------ Cubase lanes
def same_span(a, b, eps=1e-4):
    return abs(a.pos - b.pos) < eps and abs(a.length - b.length) < eps


def _subtract(span, covers, eps=1e-3):
    """`span` minus the union of `covers`, as a list of (start, end)."""
    out = [span]
    for c0, c1 in covers:
        nxt = []
        for a, b in out:
            if c1 <= a + eps or c0 >= b - eps:
                nxt.append((a, b))
                continue
            if c0 > a + eps:
                nxt.append((a, c0))
            if c1 < b - eps:
                nxt.append((c1, b))
        out = nxt
    # a remnant of a covered event is always longer than eps (it is only cut
    # off where a cover starts more than eps in); what this drops is empty
    # spans. It used eps and dropped an event that was 1 ms long to begin
    # with - the wrapped start of a loop that ran 1 ms past its file
    return [(a, b) for a, b in out if b - a > 1e-6]


def expand_loops(project, log=None, duration=None):
    """A looped REAPER item that runs past the end of its source, as the
    back-to-back events Cubase can hold: one per pass through the file.

    REAPER wraps a looped take to the source's start and plays on; a Cubase
    event stops at its clip's end. Cut at every wrap, each pass is an
    ordinary event (or a musical-mode one when the item is stretched), the
    item's fade-in on the first piece and its fade-out on the last - so the
    loop no longer has to be printed through ffmpeg. An item with a curve
    drawn inside it (volume, pan, pitch), stretch markers, a <SOURCE
    SECTION> or take FX is left whole: those still need the print.
    `duration(path)` gives a file's length in seconds, or None."""
    import copy
    n_items = n_pieces = 0
    for t in project.tracks:
        out = []
        for e in t.items:
            dur = None
            if (e.kind == 'audio' and e.loop and e.file and duration
                    and not e.section and not e.stretch_markers
                    and not e.takefx and not e.panenv and not e.pitchenv):
                try:
                    dur = duration(e.file)
                except Exception:
                    dur = None
            r = e.playrate or 1.0
            if not dur or dur <= 1e-3 or e.soffs + e.length * r <= dur + 1e-3:
                out.append(e)
                continue
            end = e.pos + e.length
            t0 = e.pos
            s = e.soffs % dur
            k = 0
            while t0 < end - 1e-6:
                b = min(end, t0 + (dur - s) / r)
                p = copy.copy(e)
                p.pos, p.length, p.soffs = t0, b - t0, s
                p.loop = False
                p.fadein = e.fadein if k == 0 else 0.0
                p.fadeout = e.fadeout if b >= end - 1e-6 else 0.0
                # a volume curve drawn on the item goes with each piece
                # (seconds into the item -> seconds into the piece); a flat
                # one at unity - REAPER's default envelope - is no curve
                if e.volenv:
                    p.volenv = _env_slice(e.volenv, t0 - e.pos, b - e.pos)
                out.append(p)
                t0, s, k = b, 0.0, k + 1
            n_items += 1
            n_pieces += k
        t.items = out
    if n_items and log is not None:
        log.append('%d looped item(s) that run past the end of their file '
                   'arrive as %d back-to-back events, one per pass through '
                   'the file, the way REAPER wraps them - nothing printed'
                   % (n_items, n_pieces))
    return n_items


def _env_slice(pts, a, b):
    """Envelope points [(t, v)] cut to [a, b], re-timed from a; the value
    at each cut is interpolated. A flat unity curve comes back empty."""
    if not pts:
        return pts
    if all(abs(v - 1.0) < 1e-4 for _t, v in pts):
        return []

    def at(x):
        if x <= pts[0][0]:
            return pts[0][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= x <= t1:
                return v0 if t1 <= t0 else v0 + (v1 - v0) * (x - t0) / (t1 - t0)
        return pts[-1][1]
    out = [(0.0, at(a))]
    out += [(t - a, v) for t, v in pts if a < t < b]
    out.append((b - a, at(b)))
    return out


def _piece(e, a, b):
    import copy
    p = copy.copy(e)
    p.pos = a
    p.length = b - a
    p.soffs = e.soffs + (a - e.pos) * (e.playrate or 1.0)
    end = e.pos + e.length
    p.fadein = e.fadein if abs(a - e.pos) < 1e-4 else 0.0
    p.fadeout = e.fadeout if abs(b - end) < 1e-4 else 0.0
    p.take_group = None
    p.take_no = 0
    p.take_sel = True
    return p


def flatten_lanes(track, log=None):
    """What a Cubase track with overlapping events actually plays.

    Cubase draws its events in list order, so where events overlap the
    later one is in front and is the one heard; whatever sticks out from
    under it is heard too. That is how takes recorded on lanes and comped
    together end up in the file: all of them unmuted, the chosen ones in
    front. REAPER plays every unmuted item, so the events are cut down to
    the parts Cubase plays: an event wholly in front stays as it is,
    a partly covered one becomes its audible pieces, a wholly covered one
    goes. Events sharing an exact span stay together as alternatives (they
    become one REAPER item with takes) and the top one of them decides the
    coverage. Video events are left alone; layered video is layered video
    in both. Returns (events cut, events dropped)."""
    items = list(track.items)
    if len(items) < 2:
        return 0, 0
    out = [i for i in items if i.kind == 'video']
    # Cubase keeps its events in position order and decides what is in
    # front by a serial each event carries (Item.zorder, read from the
    # .cpr: the highest is in front - the last recorded take, or whatever
    # 'Move to Front' was used on). Gradila's VOX track played a take
    # recorded later but placed 1.4 s earlier over the one listed after it,
    # which list order got backwards. List order stands in where the
    # serial is unknown (a REAPER project's items).
    order = {id(i): ((getattr(i, 'zorder', None)
                      if getattr(i, 'zorder', None) is not None else -1), n)
             for n, i in enumerate(items)}
    by_lane = {}
    for i in items:
        if i.kind != 'video':
            by_lane.setdefault(getattr(i, 'lane', 0), []).append(i)
    n_cut = n_gone = 0
    active = getattr(track, 'active_lane', 0) or 0
    for lane, evs in by_lane.items():
        if len(by_lane) > 1 and lane != active:
            # a version that does not play is kept as it was: cutting it
            # changed items of a REAPER lane on the round trip (Banatul
            # Dance's Kaval, version '1') and nothing is heard from it
            out.extend(evs)
            continue
        stacks = []
        for e in evs:
            for s in stacks:
                if same_span(s[0], e):
                    s.append(e)
                    break
            else:
                stacks.append([e])
        for s in stacks:
            s.sort(key=lambda e: order[id(e)])      # the top take last
        stacks.sort(key=lambda s: order[id(s[-1])])
        for si, s in enumerate(stacks):
            top = s[-1]
            covers = [(o[-1].pos, o[-1].pos + o[-1].length)
                      for o in stacks[si + 1:]]
            ranges = _subtract((top.pos, top.pos + top.length), covers)
            if not ranges:
                n_gone += len(s)
                continue
            if (len(ranges) == 1 and abs(ranges[0][0] - top.pos) < 1e-4
                    and abs(ranges[0][1] - top.pos - top.length) < 1e-4):
                out.extend(s)
                continue
            n_cut += 1
            n_gone += len(s) - 1
            for a, b in ranges:
                out.append(_piece(top, a, b))
    # a cut piece is a new object with no entry in `order`: it sorts as
    # unserialised, and the default has the entries' own shape (an int
    # against a tuple stopped Banatul Dance's conversion)
    out.sort(key=lambda i: (round(i.pos, 6), order.get(id(i), (-1, -1))))
    track.items = out
    if log is not None and (n_cut or n_gone):
        log.append('%r: %d overlapping event(s) were cut to the parts Cubase '
                   'plays and %d covered take(s) left out - Cubase plays the '
                   'event in front, REAPER would have played them all'
                   % (track.name, n_cut, n_gone))
    return n_cut, n_gone


def flatten_project_lanes(project, log=None):
    n = 0
    for t in project.tracks:
        c, g = flatten_lanes(t, log)
        n += c + g
    return n
