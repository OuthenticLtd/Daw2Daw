"""Compare a REAPER project with the Cubase project built from it.

The conversion is only finished when the two hold the same music, so this
reads both back and says where they differ: which tracks arrived, what is on
them, where each event sits, what plug-in each track carries, and where the
markers are.

    python verify.py project.rpp project.cpr

It reads the .cpr the same way the converter writes it, so it proves what
went into the file rather than what Cubase makes of it - that a project
opens at all is a separate question, and the converter's own open test
answers it. Everything here is a fact about the file: a clean report and an
opening project together mean the conversion is right.
"""
import math
import os
import re
import sys

# the embeddable Python uses a ._pth file, which replaces the
# default path and does not add the script's own folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cubaserea import cpr_read, rpp_read
from cubaserea.model import (comp_items, other_lanes, expand_for_cubase,
                             window_midi, mute_inside_folders)

TIME = 0.002        # seconds; two events at the same place agree this closely
GAIN = 0.01


def base(path):
    # REAPER writes backslashes whatever machine reads the file
    return (path or '').replace('\\', '/').rsplit('/', 1)[-1].lower()


def same_media(x, y):
    """The Cubase event plays what the REAPER item plays: the same file, or
    the WAV the conversion made from it - the same name with .wav, which is
    what an MP3 or a WAV at another sample rate becomes, or the same name
    with the change in brackets, which is what a stretched or pitch-shifted
    item or one with a curve drawn inside it is rendered to."""
    a, b = base(x), base(y)
    if a == b:
        return True
    sa, sb = os.path.splitext(a)[0], os.path.splitext(b)[0]
    if ' [' in sb:
        sb = os.path.splitext(sb.split(' [', 1)[0])[0]
    # a reversed take: the reversed copy in Edits, named as Cubase names
    # its own (media.reverse_sections) - '<name>-reverse-<32 hex>'
    m = re.match(r'(.*)-reverse-[0-9a-f]{32}$', sb)
    if m:
        sb = m.group(1)
    if b.endswith('.wav') and sa == sb:
        return True
    # a converted copy renamed so as not to land on another file of the
    # project ('noise.mp3' beside 'noise.wav' -> 'noise-02.wav', media.prepare)
    m = re.match(r'(.*)-\d{2}$', sb)
    return bool(m) and b.endswith('.wav') and a != b and sa == m.group(1)


class Report:
    def __init__(self):
        self.rows = []          # (severity, area, text)

    def bad(self, area, text):
        self.rows.append(('differs', area, text))

    def note(self, area, text):
        self.rows.append(('note', area, text))

    @property
    def differences(self):
        return sum(1 for s, _a, _t in self.rows if s == 'differs')

    def show(self, out=sys.stdout):
        if not self.rows:
            out.write('the two projects match\n')
            return
        width = max(len(a) for _s, a, _t in self.rows)
        for sev, area, text in self.rows:
            mark = 'x' if sev == 'differs' else '-'
            out.write('%s %-*s  %s\n' % (mark, width, area, text))
        out.write('\n%d difference(s), %d note(s)\n'
                  % (self.differences, len(self.rows) - self.differences))


def playable(project):
    """The tracks that carry music, in order.

    A folder holds no events of its own, and the Cubase side keeps a couple
    of tracks of its own - the output bus, and whatever the marker track
    lives in - which have no counterpart in REAPER."""
    # an item with no take holds nothing to play, so a track of nothing but
    # those (a chord chart) is not a track with something on it
    # nor is a track whose only content is a bypassed generator (Three Pots
    # Cherry's 'Audio 01': a Test Generator, switched off, and no events)
    # nor, for the pairing, one whose only content is an effect the other
    # program cannot hold (an FX return with Cubase's REVerence): that is
    # reported as the left-out effect it is (compare_left_out_only)
    return [t for t in project.tracks if not t.is_folder and (
        any(i.kind != 'empty' for i in t.items) or t.instrument
        or any(not getattr(f, 'bypass', 0) and not left_out(f) for f in t.fx))
        and t.name != '(unused template)']


def compare_left_out_only(src, dst, rep):
    """Tracks that carry nothing but effects the other program cannot load:
    the conversion loses what they play, and says so."""
    names = set(t.name for t in dst.tracks)
    for t in src.tracks:
        if t.is_folder or any(i.kind != 'empty' for i in t.items) or t.instrument:
            continue
        lost = [f for f in t.fx if not getattr(f, 'bypass', 0) and left_out(f)]
        if lost and not [f for f in t.fx if not getattr(f, 'bypass', 0) and not left_out(f)]:
            rep.bad(t.name, 'carries only %s, which the other program has no '
                            'equivalent for yet: what it plays is missing%s'
                    % (', '.join(f.name for f in lost),
                       '' if t.name in names else ' (and the track itself)'))


TEMPLATE = 'TEMPLATE '


def pair_tracks(src, dst, rep):
    """Line the two track lists up by name, in order."""
    a, b = playable(src), playable(dst)
    # Cubase keeps one video track per project: the builder puts the events
    # of every further REAPER video track on the first one, so the REAPER
    # side is read the same way here
    vids = [t for t in a if t.kind == 'video']
    if len(vids) > 1:
        first, rest = vids[0], vids[1:]
        for t in rest:
            first.items = first.items + [i for i in t.items if i.kind == 'video']
            a.remove(t)
        rep.note('tracks', 'Cubase has one video track per project, so the '
                           'events of %s were compared on %r'
                 % (', '.join(repr(t.name) for t in rest), first.name))
    # The records every track was copied from stay in the project: Cubase
    # refuses a file whose donor tracks have been written into or taken out,
    # so they arrive named for what they are and are deleted by hand.
    kept = [t for t in b if t.name.startswith(TEMPLATE)]
    b = [t for t in b if not t.name.startswith(TEMPLATE)]
    if kept:
        rep.note('tracks', '%d template track(s) came along and can be '
                           'deleted in Cubase: %s'
                 % (len(kept), ', '.join(t.name for t in kept)))
    if len(a) != len(b):
        rep.bad('tracks', 'REAPER has %d track(s) with something on them, '
                          'Cubase has %d' % (len(a), len(b)))
    used = set()
    pairs = []
    for t in a:
        match = None
        for i, u in enumerate(b):
            if i not in used and u.name == t.name:
                match = i
                break
        if match is None:
            rep.bad('tracks', '%r is missing from the Cubase project'
                    % t.name)
            continue
        used.add(match)
        pairs.append((t, b[match]))
    for i, u in enumerate(b):
        if i not in used:
            rep.bad('tracks', '%r is in the Cubase project but not in REAPER'
                    % u.name)
    # order
    order_a = [t.name for t, _ in pairs]
    order_b = [u.name for u in b if u.name in order_a]
    if order_a != order_b:
        rep.bad('order', 'the tracks are in a different order: REAPER has %s, '
                         'Cubase has %s'
                % (', '.join(order_a[:6]) + ('...' if len(order_a) > 6 else ''),
                   ', '.join(order_b[:6]) + ('...' if len(order_b) > 6 else '')))
    return pairs


def compare_versions(t, u, rep):
    """REAPER's other lanes against Cubase's other track versions."""
    a = other_lanes(t)
    b = other_lanes(u)
    if len(a) != len(b):
        rep.bad(t.name, '%d lane(s) besides the one that plays in REAPER, '
                        '%d track version(s) besides the active one in Cubase'
                % (len(a), len(b)))
        return
    for (_ka, na, ia), (_kb, nb, ib) in zip(a, b):
        if na != nb:
            rep.bad(t.name, 'lane %r arrived as version %r' % (na, nb))
        if len(ia) != len(ib):
            rep.bad(t.name, 'lane %r has %d item(s) in REAPER, version %r '
                            'has %d in Cubase' % (na, len(ia), nb, len(ib)))
        else:
            for n, (x, y) in enumerate(zip(sorted(ia, key=lambda i: i.pos),
                                           sorted(ib, key=lambda i: i.pos))):
                if abs(x.pos - y.pos) > TIME or abs(x.length - y.length) > TIME:
                    rep.bad(t.name, 'version %r item %d sits at %.3fs for '
                                    '%.3fs in REAPER, %.3fs for %.3fs in Cubase'
                            % (na, n + 1, x.pos, x.length, y.pos, y.length))


# a thirty-second note: tight enough to catch a doubled or halved tick rate
# and a quantise that moved, loose enough for the rounding a tick is
BEAT = 0.125


# the project tempo, for turning a part's length in seconds into beats,
# and whether the converted side is a Cubase project at all - a REAPER item
# is allowed notes outside itself, a Cubase part is not
dst_tempo = 0.0
dst_tempo_map = []
to_cubase = True
both_cubase = False       # two Cubase projects: neither side is REAPER
cubase_proj = None          # the Cubase side, for its pan law


def compare_items(t, u, rep, src=None, dst=None):
    # The lane REAPER plays is what sits on the Cubase track - every take of
    # it, since Cubase stacks them as lanes; the other lanes are compared as
    # track versions.
    # takes stack in the order the builder writes them: the one that plays
    # last; Cubase reads them back in list order, so a stable sort by position
    # lines the two up
    # an item with no take holds nothing to play and is not written to
    # Cubase at all (rpp_read: kind 'empty'), so it is not a difference
    a = sorted(sorted((i for i in comp_items(t) if i.kind != 'empty'),
                      key=lambda i: (i.take_sel, i.take_no)),
               key=lambda i: (round(i.pos, 3), i.kind))
    b = sorted((i for i in comp_items(u) if i.kind != 'empty'),
               key=lambda i: (round(i.pos, 3), i.kind))
    if len(a) != len(b):
        rep.bad(t.name, '%d item(s) in REAPER, %d in Cubase'
                % (len(a), len(b)))
    for n, (x, y) in enumerate(zip(a, b)):
        where = '%s item %d' % (t.name, n + 1)
        if x.kind != y.kind:
            rep.bad(where, 'is %s in REAPER and %s in Cubase'
                    % (x.kind, y.kind))
            continue
        if abs(x.pos - y.pos) > TIME:
            rep.bad(where, 'starts at %.3fs in REAPER, %.3fs in Cubase'
                    % (x.pos, y.pos))
        if abs(x.length - y.length) > TIME:
            rep.bad(where, 'is %.3fs long in REAPER, %.3fs in Cubase'
                    % (x.length, y.length))
        if bool(x.mute) != bool(y.mute) and not (
                t.mute and y.mute and not x.mute):
            # a muted REAPER track arrives with its events muted instead,
            # so an event muted only in Cubase on such a track is expected
            rep.bad(where, 'is %s in REAPER and %s in Cubase'
                    % ('muted' if x.mute else 'not muted',
                       'muted' if y.mute else 'not muted'))
        if x.kind == 'video':
            if base(x.file) != base(y.file):
                rep.bad(where, 'plays %r in REAPER and %r in Cubase'
                        % (base(x.file), base(y.file)))
            if abs(x.soffs - y.soffs) > TIME:
                rep.bad(where, 'starts %.3fs into the video in REAPER, '
                               '%.3fs in Cubase' % (x.soffs, y.soffs))
        elif x.kind == 'audio':
            # a mono file on a stereo Cubase channel plays at the pan law's
            # gain (rpp_write.mono_item_gain), which the REAPER item carries
            # (t/src is the first project given, u/dst the second; the
            # Cubase side is whichever was the .cpr)
            if to_cubase:
                cub_it, cub_tr, cub_pr, rea_it = y, u, dst, x
            else:
                cub_it, cub_tr, cub_pr, rea_it = x, t, src, y
            mg = 1.0
            if cub_pr is not None and not both_cubase:
                from cubaserea.rpp_write import mono_item_gain
                mg = mono_item_gain(cub_it, cub_tr, cub_pr)
            px = float(getattr(rea_it, 'pitch', 0.0) or 0.0)
            py = float(getattr(cub_it, 'pitch', 0.0) or 0.0)
            # a printed item carries its pitch in the file it now plays
            printed = (base(rea_it.file) != base(cub_it.file)
                       and ' [' in base(cub_it.file))
            if abs(px - py) > 0.005 and not printed:
                rep.bad(where, 'is pitched %+.2f semitones in REAPER and %+.2f '
                               'in Cubase' % (px, py))
            ea = getattr(rea_it, 'pitchenv', None) or []
            eb = getattr(cub_it, 'pitchenv', None) or []
            if ea or eb:
                # a pitch curve (Cubase: VariAudio's moved notes) as it
                # plays: compared just inside every step, where each holds
                from cubaserea import envelope as _env
                def at(e, tt):
                    return _env.interp(e, tt) if e else 0.0
                worst = (0.0, 0.0)
                for tt in sorted(set(p for p, _ in ea + eb)):
                    for s in (tt - 1e-4, tt + 1e-4):
                        if 0 <= s <= x.length:
                            d = abs(at(ea, s) - at(eb, s))
                            if d > worst[0]:
                                worst = (d, s)
                if worst[0] > 0.01:
                    rep.bad(where, 'its pitch curve differs by %.2f semitones '
                                   'at %.3f s between REAPER and Cubase' % worst)
            if printed and not getattr(cub_it, 'volenv', None):
                pass        # the curve is in the printed file (a looped item)
            elif getattr(rea_it, 'volenv', None) or getattr(cub_it, 'volenv', None):
                # an item volume curve: judged as the gain the item plays
                # over time (take volume x envelope), since the writer may
                # move the envelope's peak onto the take volume
                from cubaserea import envelope as _env
                ts = sorted(set([0.0, max(x.length, 1e-6)]
                                + [p for p, _ in (rea_it.volenv or [])]
                                + [p for p, _ in (cub_it.volenv or [])]))
                worst = 0.0
                for tt in ts:
                    if tt < 0 or tt > x.length + 1e-6:
                        continue
                    gr = (1.0 if rea_it.gain is None else rea_it.gain) * (_env.interp(rea_it.volenv, tt)
                                                 if rea_it.volenv else 1.0)
                    gc = (1.0 if cub_it.gain is None else cub_it.gain) * mg * (_env.interp(cub_it.volenv, tt)
                                                      if cub_it.volenv else 1.0)
                    if gr > 1e-3 or gc > 1e-3:
                        worst = max(worst, abs(20 * math.log10(max(gr, 1e-6))
                                               - 20 * math.log10(max(gc, 1e-6))))
                if worst > 0.05:
                    rep.bad(where, 'its volume curve plays up to %.2f dB '
                                   'differently in REAPER and Cubase' % worst)
            elif abs((1.0 if rea_it.gain is None else rea_it.gain) - (1.0 if cub_it.gain is None else cub_it.gain) * mg) > GAIN:
                rep.bad(where, 'clip gain is %.3f in REAPER and %.3f in Cubase'
                               '%s' % (rea_it.gain, cub_it.gain,
                                       '' if mg == 1.0 else
                                       ' (a mono file, played at %.3f)' % mg))
            rendered = base(x.file) != base(y.file) and ' [' in base(y.file)
            if not same_media(x.file, y.file):
                rep.bad(where, 'plays %r in REAPER and %r in Cubase'
                        % (base(x.file), base(y.file)))
            elif y.file and not os.path.exists(y.file) and not (
                    # Cubase finds a file by name in its project folder when
                    # the stored path is from another machine
                    dst is not None and getattr(dst, 'srcdir', None)
                    and __import__('cubaserea.media', fromlist=['locate'])
                    .locate(y.file, [dst.srcdir])):
                if x.file and not os.path.exists(x.file) and not (
                        src is not None and getattr(src, 'srcdir', None)
                        and __import__('cubaserea.media', fromlist=['locate'])
                        .locate(x.file, [src.srcdir])):
                    # missing in the source project as well: nothing the
                    # conversion lost (Cherry Link's stem from another PC)
                    rep.note(where, '%s is missing in the source project too'
                             % os.path.basename(x.file))
                else:
                    rep.bad(where, 'points at %s, which is not there' % y.file)
            elif to_cubase and y.file and not y.file.lower().endswith('.wav') and                     (getattr(y, 'origin', None) or {}).get('file_type') in (None, 'WAVE'):
                # a clip reads its file as the type it names: an AIFF behind
                # a clip copied from a WAV one is not read (Lumiere's AIFC
                # clips name AIFC and play - in REAPER any type plays)
                rep.bad(where, 'plays %s, which Cubase will report as '
                               'missing - only WAV comes across'
                        % os.path.basename(y.file))
            if not rendered and abs(x.soffs - y.soffs) > TIME:
                rep.bad(where, 'starts %.3fs into the file in REAPER, '
                               '%.3fs in Cubase' % (x.soffs, y.soffs))
            if abs(x.fadein - y.fadein) > TIME or abs(x.fadeout - y.fadeout) > TIME:
                from cubaserea.media import curved_fade
                if rendered and curved_fade(x) and y.fadein < TIME and y.fadeout < TIME:
                    # a fade with a shape Cubase cannot draw is printed into
                    # the audio, and the event then rightly has none
                    rep.note(where, 'fades of %.3fs/%.3fs with a curved shape '
                                    'are printed into the audio, so the Cubase '
                                    'event carries none' % (x.fadein, x.fadeout))
                else:
                    rep.bad(where, 'fades are %.3fs/%.3fs in REAPER and %.3fs/%.3fs '
                                   'in Cubase' % (x.fadein, x.fadeout,
                                                  y.fadein, y.fadeout))
        elif x.kind == 'midi':
            # An independent check, not a comparison: whatever the two sides
            # agree on, a note past the end of the part it sits in is wrong
            # on its face. Both sides are windowed by the same code, so a
            # fault in that code would agree with itself and slip through a
            # comparison; this catches it. A REAPER item is a window onto
            # its source and its notes were once copied over whole, which
            # put a thirty-two beat take inside a sixteen beat part.
            qy = y.ppq or 480.0
            # beats the part spans under the tempo map where it sits - one
            # tempo for the whole song made Sunset Treasures' parts in its
            # 140 BPM section look 27.43 beats long instead of 32
            if dst_tempo_map:
                from cubaserea.model import _qn_upto
                part_beats = (_qn_upto(dst_tempo_map, y.pos + y.length)
                              - _qn_upto(dst_tempo_map, y.pos))
            else:
                part_beats = y.length * 2.0 if not dst_tempo else (
                    y.length * dst_tempo / 60.0)
            over = [n for n in y.notes if n[0] / qy > part_beats + 0.05]
            ynotes = list(y.notes)
            if to_cubase and over and part_beats > 0:
                if both_cubase and len(y.notes) == len(x.notes):
                    # two Cubase parts keeping the same unplayed notes
                    rep.note(where, '%d note(s) past the end of the part are '
                                    'kept, unplayed, as in the original'
                             % len(over))
                elif part_beats > 0 and len(y.notes) - len(over) == len(x.notes):
                    # the notes REAPER plays are all there; the rest start
                    # after the part ends, which Cubase does not play either.
                    # A Cubase part keeps such notes (Sunset Treasures' Bass
                    # has one at beat 31.5 of a 31.2-beat part) and they are
                    # carried as they were, unplayed on both sides
                    rep.note(where, '%d note(s) past the end of the part are '
                                    'kept, unplayed, as in the original'
                             % len(over))
                    ynotes = [n for n in y.notes if n not in over]
                else:
                    rep.bad(where, '%d of its %d note(s) start past the end of '
                                   'the part in Cubase - the part is %.2f beat(s) '
                                   'long and the last note starts at %.2f'
                            % (len(over), len(y.notes), part_beats,
                               max(n[0] for n in y.notes) / qy))
            if len(x.notes) != len(ynotes):
                rep.bad(where, '%d note(s) in REAPER, %d in Cubase'
                        % (len(x.notes), len(y.notes)))
            else:
                # A note's position and length are ticks, and the two
                # programs count them differently - REAPER at whatever the
                # item says, Cubase always at 480 to the quarter - so they
                # are compared in beats. Timing was once left out of this
                # comparison, which let a whole project arrive at half
                # speed and still be called a match.
                qx = x.ppq or 480.0
                qy = y.ppq or 480.0
                wrong = late = long_ = 0
                released = 0
                # notes are paired by where they start, their channel and
                # pitch - not by raw tuples: a Cubase length is fractional
                # ticks and a REAPER one whole ticks, so two notes starting
                # together could sort the other way round after rounding and
                # be reported as a changed pitch (23 Feb Idea's piano)
                # a note that runs past the part's end is cut there by both
                # programs, so it is compared as it plays
                endx = part_beats if part_beats > 0 else None
                first_long = None
                first_wrong = None
                def pair_notes(xs, ys):
                    # same channel and pitch, in time order: no rounding of
                    # positions, which put notes a tick apart either way
                    gx, gy = {}, {}
                    for n in xs:
                        gx.setdefault((n[2], n[3]), []).append(n)
                    for n in ys:
                        gy.setdefault((n[2], n[3]), []).append(n)
                    pairs, lone_x, lone_y = [], [], []
                    for key in set(gx) | set(gy):
                        a_ = sorted(gx.get(key, []))
                        b_ = sorted(gy.get(key, []))
                        pairs += list(zip(a_, b_))
                        lone_x += a_[len(b_):]
                        lone_y += b_[len(a_):]
                    # what has no partner of its pitch is paired by time,
                    # and then counts as a changed pitch
                    pairs += list(zip(sorted(lone_x), sorted(lone_y)))
                    return pairs
                for (p1, l1, c1, k1, v1, *r1), (p2, l2, c2, k2, v2, *r2) in                         pair_notes(x.notes, ynotes):
                    if k1 != k2 or v1 != v2 or c1 != c2:
                        wrong += 1
                        if first_wrong is None:
                            first_wrong = (p1 / qx, k1, v1, c1, p2 / qy, k2, v2, c2)
                    if r1 and r2 and int(r1[0]) != int(r2[0]):
                        released += 1
                    if abs(p1 / qx - p2 / qy) > BEAT:
                        late += 1
                    e1, e2 = (p1 + l1) / qx, (p2 + l2) / qy
                    if endx is not None:
                        e1, e2 = min(e1, endx), min(e2, endx)
                    if abs((e1 - p1 / qx) - (e2 - p2 / qy)) > BEAT:
                        long_ += 1
                        if first_long is None:
                            first_long = (p1 / qx, l1 / qx, l2 / qy)
                if wrong or released or long_:
                    # Notes of one pitch that overlap are paired differently
                    # by the two programs (a note-on and note-off stream has
                    # no say in which off ends which note); what the
                    # instrument receives is the stream itself. Compared as
                    # that - each pitch's note-ons (time, velocity) and
                    # note-offs (time, release velocity) - they may agree
                    # (Big Win 4's horns: a zero-length note inside a longer
                    # one of the same pitch).
                    def stream(notes, q):
                        ons, offs = {}, {}
                        for n in notes:
                            k = (n[2], n[3])
                            ons.setdefault(k, []).append((n[0] / q, n[4]))
                            rv = n[5] if len(n) > 5 else None
                            offs.setdefault(k, []).append(((n[0] + max(n[1], 0)) / q, rv))
                        return ({k: sorted(v) for k, v in ons.items()},
                                {k: sorted(v, key=lambda e: e[0]) for k, v in offs.items()})

                    def same_events(a, b):
                        # the same times within the tolerance, the same
                        # values (velocities) as a multiset per pitch
                        for k in set(a) | set(b):
                            ea, eb = a.get(k, []), b.get(k, [])
                            if len(ea) != len(eb):
                                return False
                            if any(abs(u[0] - v[0]) > BEAT for u, v in zip(ea, eb)):
                                return False
                            if sorted(str(u[1]) for u in ea) != sorted(str(v[1]) for v in eb):
                                return False
                        return True
                    sx, sy = stream(x.notes, qx), stream(ynotes, qy)
                    same_on = same_events(sx[0], sy[0])
                    same_off = same_events(sx[1], sy[1])
                    if same_on and same_off:
                        rep.note(where, 'overlapping notes of one pitch are paired '
                                        'differently; the notes played are the same')
                        wrong = released = long_ = 0
                if wrong:
                    rep.bad(where, '%d note(s) came out with a different '
                                   'pitch, velocity or channel (first: beat '
                                   '%.3f pitch %d vel %d ch %d in REAPER, beat '
                                   '%.3f pitch %d vel %d ch %d in Cubase)'
                            % ((wrong,) + tuple(first_wrong)))
                if released:
                    rep.bad(where, '%d note(s) came out with a different '
                                   'note-off (release) velocity' % released)
                if late:
                    rep.bad(where, '%d note(s) sit at a different beat in '
                                   'Cubase (first: %.3f in REAPER, %.3f in '
                                   'Cubase)'
                            % (late, sorted(x.notes)[0][0] / qx,
                               sorted(y.notes)[0][0] / qy))
                if long_:
                    rep.bad(where, '%d note(s) are a different length in '
                                   'Cubase (first, at beat %.3f: %.3f beat(s) '
                                   'in REAPER, %.3f in Cubase)'
                            % ((long_,) + first_long))
                # the controller events themselves, in beats. An All Notes
                # Off (CC 123, 0) at the part's very end is REAPER's end of
                # source marker and plays nothing (the wire order test's
                # parts carry one from an earlier REAPER import)
                def cc_list(it, q, beats):
                    out = []
                    for pos, st, d1, d2 in (it.ccs or ()):
                        b = pos / q
                        if (st & 0xF0) == 0xB0 and d1 == 123 and d2 == 0 and \
                                beats and b >= beats - BEAT:
                            continue
                        # outside the part nothing is sent, by either program
                        # (before the start not even by a hair: Cubase does
                        # not send it, measured, and the writer leaves it out)
                        if b < -1e-4 or (beats and b >= beats - BEAT / 2):
                            continue
                        out.append((b, st, d1, d2))
                    # in time order, and in the order they are sent where
                    # two share a moment (CC 1 = 23 then 0 at one beat
                    # leaves 0): a stable sort on the time, rounded
                    return sorted(out, key=lambda e: round(e[0], 3))
                cx = cc_list(x, qx, part_beats)
                cy = cc_list(y, qy, part_beats)
                # matched in order with the beat tolerance; an event the
                # rounding of a tick put just past the part's end on one
                # side only is not a difference
                def unmatched(p_, q_):
                    out, j = [], 0
                    used = [False] * len(q_)
                    for e in p_:
                        k = next((i for i in range(len(q_)) if not used[i]
                                  and q_[i][1:] == e[1:] and abs(q_[i][0] - e[0]) <= BEAT), None)
                        if k is None:
                            if not (part_beats and e[0] >= part_beats - 2 * BEAT):
                                out.append(e)
                        else:
                            used[k] = True
                    return out
                ux, uy = unmatched(cx, cy), unmatched(cy, cx)
                if ux or uy:
                    first = (ux or uy)[0]
                    rep.bad(where, '%d controller event(s) in REAPER and %d in Cubase have '
                                   'no partner (first: beat %.3f %02x %d %d in %s)'
                            % ((len(ux), len(uy)) + first + ('REAPER' if ux else 'Cubase',)))


def _plugin_key(name):
    """A plug-in name reduced to what identifies it: REAPER's cache says
    'FabFilter Pro-L 2' or 'ValhallaRoom_x64' where Cubase says 'Pro-L 2'
    and 'ValhallaRoom'."""
    import re as _re
    s = (name or '').strip().lower()
    s = _re.sub(r'[\s_\-]*(x64|x86|64|win64|vst3?|au)\s*$', '', s)
    return _re.sub(r'[^a-z0-9]+', '', s)


def same_plugin_name(a, b):
    ka, kb = _plugin_key(a), _plugin_key(b)
    if not ka or not kb:
        return ka == kb
    return ka == kb or ka.endswith(kb) or kb.endswith(ka)


_PLUGIN_INDEX = []


def plugin_index():
    """The scanned plug-in list, read once, or None if it cannot be read."""
    if not _PLUGIN_INDEX:
        try:
            from cubaserea import plugins
            _PLUGIN_INDEX.append(plugins.PluginIndex())
        except Exception:
            _PLUGIN_INDEX.append(None)
    return _PLUGIN_INDEX[0]


def reaper_has(fx):
    """Is this plug-in installed in REAPER on this machine?"""
    idx = plugin_index()
    if idx is None:
        return True
    return idx.lookup(fx)[0] is not None


def successor(a, b):
    """One plug-in declares it stands in for the other (VST3 plug-in
    compatibility, as REAPER's scan lists it): Cubase and the converter
    both open the old one's settings in the new one."""
    idx = plugin_index()
    if idx is None or not a or not b:
        return False
    a, b = a.upper(), b.upper()
    return idx.compat.get(a) == b or idx.compat.get(b) == a


def left_out(fx):
    """A plug-in the other host cannot load and that has no stand-in there
    (builtins.py): left out on the way over, so not compared."""
    from cubaserea import stock
    if not getattr(fx, 'native', False) and stock.from_cubase(fx) is not None:
        return False            # REAPER's own plug-in stands in for it
    return getattr(fx, 'native', False) or not reaper_has(fx)


def compare_param_lanes(track_name, f, g, rep):
    """Plug-in parameter automation, as the curves play (both hosts hold
    a parameter lane as the plug-in's own 0..1 and draw straight lines)."""
    from cubaserea import envelope
    a = dict((int(k), pts) for k, pts in (f.envelopes or []) if pts)
    b = dict((int(k), pts) for k, pts in (g.envelopes or []) if pts)
    for k in sorted(set(a) | set(b)):
        if k not in a or k not in b:
            rep.bad(track_name, '%s parameter %d is automated on %s side only'
                    % (f.name, k, 'the REAPER' if (k in a) == to_cubase else 'the Cubase'))
            continue
        ts = sorted(set(round(s, 6) for s, _ in a[k]) | set(round(s, 6) for s, _ in b[k]))
        ts = sorted(set(ts + [0.5 * (x + y) for x, y in zip(ts, ts[1:])]))
        worst = max((abs(envelope.interp(a[k], s) - envelope.interp(b[k], s)), s)
                    for s in ts)
        if worst[0] > 0.002:
            rep.bad(track_name, '%s parameter %d plays %.4f in REAPER and %.4f '
                                'in Cubase at %.3f s'
                    % (f.name, k, envelope.interp(a[k], worst[1]),
                       envelope.interp(b[k], worst[1]), worst[1]))


def compare_plugins(t, u, rep):
    def names(track):
        return [f.name for f in track.fx]

    def same_fx(f, g):
        # the class id is the identity; names differ by host ("Blue Cat's
        # Gain 3" in Cubase, "Blue Cat's Gain 3 (Stereo)" in REAPER's list)
        if f.uid and g.uid:
            return f.uid.upper() == g.uid.upper() or successor(f.uid, g.uid)
        return same_plugin_name(f.name, g.name)

    def same_chain(a, b):
        return len(a) == len(b) and all(same_fx(f, g) for f, g in zip(a, b))
    # a Cubase channel EQ crosses as a ReaEQ on the end of the REAPER chain;
    # it is not an insert on the Cubase side, so it is taken off before the
    # chains are compared
    rea_t = t if to_cubase else u
    cub_t = u if to_cubase else t
    # the channel EQ itself: the same curve on both sides (REAPER carries it
    # as a ReaEQ fitted to Cubase's curve, read back into Cubase bands of
    # possibly other types - the curve is what is heard), within 0.5 dB
    ea = sorted(getattr(t, 'chan_eq', None) or [])
    eb = sorted(getattr(u, 'chan_eq', None) or [])
    if ea or eb:
        from cubaserea import chan_eq as _ce
        ca = _ce._total(_ce.cubase_band_db, ea, 48000.0) if ea else [0.0] * len(_ce._GRID)
        cb = _ce._total(_ce.cubase_band_db, eb, 48000.0) if eb else [0.0] * len(_ce._GRID)
        worst = max(abs(x - y) for x, y in zip(ca, cb))
        if worst > 0.5:
            rep.bad(t.name, 'the channel EQ curves differ by up to %.2f dB (%s in REAPER, %s in Cubase)'
                    % (worst, ea or 'off', eb or 'off'))
        elif ea != eb:
            rep.note(t.name, 'the channel EQ has other bands on the two sides; the curves agree within %.2f dB' % worst)
    if getattr(cub_t, 'chan_eq', None) and rea_t.fx and rea_t.fx[-1].name == 'ReaEQ':
        rea_t = type(rea_t)() if False else rea_t
        rea_fx = rea_t.fx[:-1]
        rep.note(t.name, 'its Cubase channel EQ is a ReaEQ at the end of the REAPER chain')
    else:
        rea_fx = rea_t.fx
    tfx, ufx = (rea_fx, u.fx) if to_cubase else (t.fx, rea_fx)
    if same_chain(tfx, ufx):
        for f, g in zip(tfx, ufx):
            if bool(f.bypass) != bool(g.bypass):
                rep.bad(t.name, '%s is %s in the source and %s in the conversion'
                        % (f.name, 'bypassed' if f.bypass else 'on',
                           'bypassed' if g.bypass else 'on'))
    if not same_chain(tfx, ufx):
        # a plug-in REAPER does not have (Cubase's own, or not installed
        # here) is left out on the way to REAPER, and the log said so
        absent = [f for f in tfx + ufx if not reaper_has(f)]
        ta = [f for f in tfx if f not in absent]
        ua = [f for f in ufx if f not in absent]
        if absent and same_chain(ta, ua):
            rep.note(t.name, 'insert(s) REAPER cannot load were left out: %s'
                     % ', '.join(f.name for f in absent))
        else:
            rep.bad(t.name, 'insert effects are %s in REAPER and %s in Cubase'
                    % (names(t) or 'none', names(u) or 'none'))
    else:
        for f, g in zip(t.fx, u.fx):
            if f.component and not g.component:
                rep.bad(t.name, '%s arrived without its saved settings'
                        % f.name)
            compare_param_lanes(t.name, f, g, rep)
    ti = t.instrument.name if t.instrument else None
    ui = u.instrument.name if u.instrument else None
    # Cubase's 'Plugin Name' is the name the instrument shows on its track,
    # which the user may have changed ('Serum Rhodes' for a Serum 2): the
    # class id is the identity, the name only when an id is missing
    same_inst = (ti is None and ui is None) or (
        t.instrument is not None and u.instrument is not None and (
            (t.instrument.uid and u.instrument.uid
             and t.instrument.uid.upper() == u.instrument.uid.upper())
            or same_plugin_name(ti, ui)
            or successor(t.instrument.uid, u.instrument.uid)))
    if same_inst and t.instrument is not None and u.instrument is not None \
            and t.instrument.component and not u.instrument.component:
        rep.bad(t.name, 'the instrument %s arrived without its saved settings'
                % ti)
    if not same_inst:
        rep.bad(t.name, 'the instrument is %s in REAPER and %s in Cubase'
                % (ti or 'none', ui or 'none'))
    elif t.instrument and t.instrument.component and not u.instrument.component:
        rep.bad(t.name, '%s arrived without its saved patch' % ti)


def muted_by_events(track):
    """A track silent because every event on it is muted."""
    live = [i for i in track.items if i.kind != 'empty']
    return bool(live) and all(i.mute for i in live)


def compare_fader_halves(u, rep):
    """A Cubase track's two fader fields have to agree with each other.

    Cubase stores a level twice: as dB (AnchorValue) and as the fader
    position it plays by (Value). This tool reads the dB, so a wrong
    position was invisible - and the position was written off a curve
    extrapolated from a single donor calibration point, which put -25 dB at
    a position the real fader reads as near silence. Every attenuated track
    arrived far too quiet and every check still passed."""
    pos = getattr(u, 'vol_pos', None)
    if pos is None:
        return
    want = cpr_read.taper_gain(pos)

    def db(v):
        # anything under -144 dB is silence on either reading (HSMG's
        # Hive at -200 dB sits at position 0)
        return -144.0 if v <= 10 ** -7.2 else 20.0 * math.log10(v)

    if abs(db(want) - db(u.vol or 0.0)) > 0.6 and pos >= 0.9999:
        # Cubase's fader stops at +6.02 dB and REAPER's does not. A track
        # set louder than that arrives at the top of Cubase's fader and
        # there is no position left for the rest: a limit, not a mistake.
        rep.note(u.name, 'is %.1f dB in REAPER, and the fader in Cubase '
                         'stops at +6.0 dB - it arrives %.1f dB quieter. '
                         'Make the rest up with clip gain or an insert'
                 % (db(u.vol or 0.0), db(u.vol or 0.0) - db(want)))
        return
    if abs(db(want) - db(u.vol or 0.0)) > 0.6:
        rep.bad(u.name, 'the fader says %.1f dB but its position %.4f is '
                        '%.1f dB on Cubase\'s own curve - Cubase plays the '
                        'position, not the number'
                % (db(u.vol or 0.0), pos, db(want)))


def channel_gains(track, proj, vol=None, pan=None, pan_balance=False):
    """(L, R) linear gains the track's fader and panner apply together,
    by the panner of the host the project came from (panlaw.py). `vol`
    and `pan` override the static values (a point on an automation
    curve)."""
    from cubaserea import panlaw
    vol = track.vol if vol is None else vol
    pan = track.pan if pan is None else pan
    if pan_balance:
        # a REAPER take pan moved onto the track (model.split_item_pan): a
        # plain linear balance in any pan mode, Cubase's stereo panner's law
        gl, gr = panlaw.cubase_gains(pan)
    elif getattr(proj, 'pan_law_of', None) == 'cubase':
        if getattr(track, 'mono', False):
            # a mono channel's own panner: sine/cosine, the law at centre
            gl, gr = panlaw.cubase_mono_gains(
                pan, getattr(proj, 'panlaw_code', None) or 6)
        else:
            gl, gr = panlaw.cubase_gains(pan)
    else:
        law = proj.panlaw if getattr(proj, 'panlaw', None) is not None else 1.0
        mode = track.panmode if getattr(track, 'panmode', None) is not None \
            else getattr(proj, 'panmode', 3)
        gl, gr = panlaw.reaper_gains(pan, law, mode)
    # Cubase's Volume effect (or its REAPER stand-in) in the chain is plain
    # gain, measured: a level past Cubase's fader is carried on one
    from cubaserea import builtins
    for fx in getattr(track, 'fx', None) or []:
        if (fx.uid or '').upper() == builtins.VOLUME_UID and not fx.bypass:
            g, g0, g1, byp = builtins.volume_params(fx.component)
            if not byp:
                gl *= g * g0
                gr *= g * g1
    return gl * vol, gr * vol


def mix_times(*tracks):
    """Where to sample two tracks' mix curves: every automation point on
    either side and the middle of every gap between them."""
    ts = set()
    for t in tracks:
        for env in (t.volenv or [], t.panenv or []):
            for s, _ in env:
                ts.add(round(s, 6))
    ts = sorted(ts)
    mids = [0.5 * (a + b) for a, b in zip(ts, ts[1:])]
    return sorted(set(ts + mids)) or [0.0]


def gains_at(track, proj, when):
    """(L, R) gains the track plays at `when`: an automation lane replaces
    the fader / the pan control (the model keeps envelopes as what plays,
    rpp_read._fold_trim), so the curve's value stands in for it."""
    from cubaserea import envelope
    vol = envelope.interp(track.volenv, when) if track.volenv else None
    pan = envelope.interp(track.panenv, when) if track.panenv else None
    return channel_gains(track, proj, vol, pan,
                         pan_balance=pan is not None
                         and getattr(track, 'panenv_law', None) == 'balance')


def compare_mix(t, u, rep, src=None, dst=None):
    if t.kind == 'video' or u.kind == 'video':
        return          # a video track has no channel in Cubase
    for fld in ('volenv_idle', 'panenv_idle'):
        if bool(getattr(t, fld, None)) != bool(getattr(u, fld, None)):
            rep.bad(t.name, 'a lane that does not play (%s) is on one side only'
                    % fld.replace('env_idle', ''))
    da, db_ = (float(getattr(x, 'delay', 0.0) or 0.0) for x in (t, u))
    if abs(da - db_) > 1e-5:
        rep.bad(t.name, 'track delay is %+.2f ms in the source and %+.2f ms in '
                        'the conversion' % (da * 1000, db_ * 1000))
    # Fader and pan are judged together, as the gain each channel ends up
    # with: the two panners follow different curves (panlaw.py), so the
    # converter writes a different pan and a different fader on purpose,
    # and comparing the raw numbers would call a correct conversion wrong.
    def db(v):
        # anything below -60 dB counts as silence: a hard pan's far channel
        # comes out as cos(pi/2), a few e-17, on one side and a clean 0 on
        # the other, and on a fade to nothing the two hosts' straight lines
        # (gain vs fader position) part ways only down there
        return -60.0 if v <= 10 ** (-60.0 / 20.0) else 20.0 * math.log10(v)
    automated = bool(t.volenv or t.panenv or u.volenv or u.panenv)
    # a mono Cubase channel's panner is judged by its own law inside
    # channel_gains; the pan law's gain on a mono FILE on a stereo channel
    # sits on the items (compare_items), so nothing is applied here
    tm = um = 1.0
    if not automated or src is None or dst is None:
        tl, tr = channel_gains(t, src) if src is not None else (t.vol, t.vol)
        ul, ur = channel_gains(u, dst) if dst is not None else (u.vol, u.vol)
        tl, tr, ul, ur = tl * tm, tr * tm, ul * um, ur * um
        if abs(db(tl) - db(ul)) > 0.05 or abs(db(tr) - db(ur)) > 0.05:
            rep.bad(t.name, 'plays at L %+.2f / R %+.2f dB in the source and '
                            'L %+.2f / R %+.2f dB in the conversion (fader %.3f '
                            'pan %+.2f -> fader %.3f pan %+.2f)'
                    % (db(tl), db(tr), db(ul), db(ur), t.vol, t.pan, u.vol, u.pan))
    else:
        # automated: the two hosts hold different point lists on purpose
        # (envelope.py adds points so straight lines in fader position
        # match straight lines in gain, and folds the pan law's level into
        # the volume lane), so the curves are compared as what they play
        worst = (0.0, None)
        for when in mix_times(t, u):
            tl, tr = gains_at(t, src, when)
            ul, ur = gains_at(u, dst, when)
            tl, tr, ul, ur = tl * tm, tr * tm, ul * um, ur * um
            dev = max(abs(db(tl) - db(ul)), abs(db(tr) - db(ur)))
            if dev > worst[0]:
                worst = (dev, when, db(tl), db(tr), db(ul), db(ur))
        if worst[0] > 0.05:
            _dev, when, a, b, c, d = worst
            rep.bad(t.name, 'the automated mix plays at L %+.2f / R %+.2f dB '
                            'in the source and L %+.2f / R %+.2f dB in the '
                            'conversion at %.3f s' % (a, b, c, d, when))
    cub_t = u if to_cubase else t
    rea_t = t if to_cubase else u
    if getattr(cub_t, 'disabled', False) and rea_t.mute:
        rep.note(t.name, 'is disabled in Cubase and arrives muted in REAPER with its plug-ins offline')
    elif bool(t.mute) != bool(u.mute):
        if t.mute and muted_by_events(u):
            # the builder mutes every event on a muted track, Cubase having
            # no track-mute field this can write; the track plays the same
            rep.note(t.name, 'is muted in REAPER and arrives with all of '
                             'its events muted, which plays the same')
        else:
            rep.bad(t.name, 'is %s in REAPER and %s in Cubase'
                    % ('muted' if t.mute else 'not muted',
                       'muted' if u.mute else 'not muted'))
    if len(t.sends) != len(u.sends):
        rep.bad(t.name, '%d send(s) in REAPER, %d in Cubase'
                % (len(t.sends), len(u.sends)))
    elif t.sends and src is not None and dst is not None:
        def dests(track, proj):
            return sorted(proj.tracks[s.dest].name for s in track.sends
                          if s.dest is not None and 0 <= s.dest < len(proj.tracks))
        da, db = dests(t, src), dests(u, dst)
        if da != db:
            rep.bad(t.name, 'sends go to %s in REAPER and to %s in Cubase'
                    % (', '.join(da) or 'nowhere', ', '.join(db) or 'nowhere'))
    if bool(t.volenv) != bool(u.volenv) and not (t.panenv or u.panenv):
        rep.bad(t.name, 'has volume automation on %s side only'
                % ('the REAPER' if t.volenv else 'the Cubase'))
    if bool(t.panenv) != bool(u.panenv):
        rep.bad(t.name, 'has pan automation on %s side only'
                % ('the REAPER' if t.panenv else 'the Cubase'))


def compare_markers(src, dst, rep):
    # A region and a point marker can share a position - a region opening
    # where a marker already sits - and sorting on position alone left that
    # tie to be broken differently on each side, which read back as the two
    # having swapped their names and their ends. Cubase keeps regions and
    # markers in lists of their own, so the order between them carries no
    # meaning; the tie is broken the same way on both sides instead.
    def order(m):
        return (m.start, 0 if m.end is not None else 1, m.name or '')

    a = sorted(src.markers, key=order)
    b = sorted(dst.markers, key=order)
    if len(a) != len(b):
        rep.bad('markers', '%d in REAPER, %d in Cubase' % (len(a), len(b)))
    for n, (x, y) in enumerate(zip(a, b)):
        if (x.name or '') != (y.name or ''):
            rep.bad('markers', 'marker %d is %r in REAPER and %r in Cubase'
                    % (n + 1, x.name, y.name))
        if abs(x.start - y.start) > TIME:
            rep.bad('markers', '%r is at %.3fs in REAPER and %.3fs in Cubase'
                    % (x.name or n + 1, x.start, y.start))
        xe = x.end if x.end is not None else x.start
        ye = y.end if y.end is not None else y.start
        if abs(xe - ye) > TIME:
            rep.bad('markers', '%r ends at %.3fs in REAPER and %.3fs in '
                               'Cubase' % (x.name or n + 1, xe, ye))


def compare_folders(src, dst, rep):
    want = [(t.name, t.depth) for t in src.tracks if t.is_folder]
    # the donor's own 'Input/Output' folder is not the project's - unless the
    # project has a folder of that name itself (Sunset Treasures does)
    own_io = any(n == 'Input/Output' for n, _ in want)
    got = [(t.name, t.depth) for t in dst.tracks
           if t.is_folder and not t.name.startswith(TEMPLATE)
           and (own_io or t.name != 'Input/Output')]
    if len(want) != len(got):
        rep.bad('folders', '%d folder(s) in REAPER (%s), %d in Cubase (%s)'
                % (len(want), ', '.join(n for n, _ in want) or 'none',
                   len(got), ', '.join(n for n, _ in got) or 'none'))
    # A REAPER folder sums what is inside it, so it can carry inserts and
    # sends on that sum - a drum bus with a compressor and a limiter, and a
    # send to a reverb. Folders sit outside the track comparison, so none of
    # that was ever checked: a whole drum bus lost its processing in silence.
    # folders of one name (Three Pots Cherry has two 'Drums') pair in order
    byname = {}
    for t in dst.tracks:
        if t.is_folder:
            byname.setdefault(t.name, []).append(t)
    for t in src.tracks:
        if not t.is_folder:
            continue
        u = byname[t.name].pop(0) if byname.get(t.name) else None
        if u is None:
            if t.fx or t.sends:
                rep.bad(t.name, 'is a folder carrying %d insert(s) and %d '
                                'send(s) in REAPER, and no folder of that '
                                'name is in Cubase'
                        % (len(t.fx), len(t.sends)))
            continue
        # the same allowance compare_plugins makes: a plug-in one host
        # cannot load is left out on the way over, and the log says so
        absent = [f for f in list(t.fx) + list(u.fx)
                  if left_out(f)]
        an = [f.name for f in t.fx if f not in absent]
        bn = [f.name for f in u.fx if f not in absent]
        # by class id where both sides have one (names differ by host)
        ak = sorted((f.uid or f.name).upper() for f in t.fx if f not in absent)
        bk = sorted((g.uid or g.name).upper() for g in u.fx if g not in absent)
        if ak != bk:
            rep.bad(t.name, 'the folder carries %s in REAPER and %s in '
                            'Cubase' % (', '.join(an) or 'no inserts',
                                        ', '.join(bn) or 'no inserts'))
        elif absent:
            rep.note(t.name, 'insert(s) on the folder that the other program '
                             'cannot load were left out: %s'
                     % ', '.join(f.name for f in absent))
        if len(t.sends) != len(u.sends):
            rep.bad(t.name, 'the folder has %d send(s) in REAPER and %d in '
                            'Cubase' % (len(t.sends), len(u.sends)))


def compare(src, dst, rep):
    global cubase_proj
    cubase_proj = dst if to_cubase else src
    global dst_tempo, dst_tempo_map
    dst_tempo = dst.tempo[0][1] if dst.tempo else 0.0
    dst_tempo_map = sorted(dst.tempo or [])
    if abs(src.tempo[0][1] - dst.tempo[0][1]) > 0.01:
        rep.bad('tempo', '%.3f in REAPER, %.3f in Cubase'
                % (src.tempo[0][1], dst.tempo[0][1]))
    compare_left_out_only(src, dst, rep)
    if src.samplerate != dst.samplerate:
        # not a note: a project at the wrong rate plays every audio event at
        # the wrong speed, and stores its lengths against the wrong clock
        rep.bad('sample rate', '%d Hz in REAPER, %d Hz in Cubase'
                % (src.samplerate, dst.samplerate))
    for t, u in pair_tracks(src, dst, rep):
        compare_items(t, u, rep, src, dst)
        compare_versions(t, u, rep)
        compare_plugins(t, u, rep)
        compare_mix(t, u, rep, src, dst)
        compare_fader_halves(u, rep)
    compare_folders(src, dst, rep)
    compare_master(src, dst, rep)
    compare_markers(src, dst, rep)
    return rep


def compare_master(src, dst, rep):
    """REAPER's master against Cubase's output bus: fader and inserts.

    Everything sums into it, so nothing matters more to how the mix sounds,
    and until the reader learnt where Cubase keeps it (the Devices chunk,
    not the track list) it could not be compared at all."""
    a, b = src.master, dst.master
    if a is None and b is None:
        return
    if a is None or b is None:
        if (a and (a.fx or abs(a.vol - 1.0) > GAIN)) or \
                (b and (b.fx or abs(b.vol - 1.0) > GAIN)):
            rep.bad('master', 'REAPER %s a master, Cubase %s'
                    % ('has' if a else 'has no', 'has' if b else 'has none'))
        return

    def db(v):
        # below -60 dB a fade to nothing is silence on both sides
        return -60.0 if not v or v <= 10 ** (-60.0 / 20.0) else 20.0 * math.log10(v)

    if a.volenv or b.volenv:
        from cubaserea import envelope
        worst = (0.0, None, 0, 0)
        for when in mix_times(a, b):
            ga = envelope.interp(a.volenv, when) if a.volenv else a.vol
            gb = envelope.interp(b.volenv, when) if b.volenv else b.vol
            dev = abs(db(ga) - db(gb))
            if dev > worst[0]:
                worst = (dev, when, db(ga), db(gb))
        if worst[0] > 0.1:
            rep.bad('master', 'the master volume curve is %+.2f dB in REAPER '
                              'and %+.2f dB in Cubase at %.3f s'
                    % (worst[2], worst[3], worst[1]))
    elif abs(db(a.vol) - db(b.vol)) > 0.1:
        rep.bad('master', 'the master fader is %+.2f dB in REAPER and %+.2f '
                          'dB in Cubase' % (db(a.vol), db(b.vol)))
    absent = [f for f in list(a.fx) + list(b.fx)
              if left_out(f)]
    an = [f.name for f in a.fx if f not in absent]
    bn = [f.name for f in b.fx if f not in absent]
    ai = [f for f in a.fx if f not in absent]
    bi = [f for f in b.fx if f not in absent]
    if len(an) != len(bn) or any(
            not ((x.uid and y.uid and x.uid.upper() == y.uid.upper())
                 or same_plugin_name(x.name, y.name)) for x, y in zip(ai, bi)):
        rep.bad('master', 'inserts are %s in REAPER and %s in Cubase'
                % (an or 'none', bn or 'none'))
    else:
        for f, g in zip([f for f in a.fx if f not in absent],
                        [f for f in b.fx if f not in absent]):
            if f.component and not g.component:
                rep.bad('master', '%s arrived without its saved settings'
                        % f.name)
            elif not f.component and not g.component and f.name:
                rep.note('master', '%s has no saved settings on either side '
                                   '(REAPER stored it as a parameter dump)%s'
                         % (f.name, (' - preset %r' % f.preset)
                            if getattr(f, 'preset', '') else ''))
    if absent:
        rep.note('master', 'insert(s) the other program cannot load were '
                           'left out: %s' % ', '.join(f.name for f in absent))


def load(path, other=None, source=True):
    """Read a project of either kind, the way the converter saw it when it
    made `other` from it (`source`), or as it is (`source` False).

    A REAPER source is read with the tracks the builder printed through
    REAPER (their WAVs sit beside `other`) as printed; a Cubase source has
    its overlapping events cut down to what Cubase plays, as the REAPER
    writer does. The converted side is never reinterpreted."""
    if path.lower().endswith(('.cpr', '.bak')):
        p = cpr_read.read(path)
        came_back = (not source and other and other.lower().endswith('.rpp')
                     and os.path.exists(other + '.cubase-origin'))
        if (source and other and other.lower().endswith('.rpp')) or came_back:
            # the REAPER writer cuts overlapping Cubase events down to what
            # Cubase plays; compare against the same. A Cubase project
            # written back from a REAPER project that came from Cubase keeps
            # its original overlapping events, so it is cut the same way.
            from cubaserea.model import flatten_project_lanes
            flatten_project_lanes(p)
        # a channel whose output is a group channel arrives in REAPER as a
        # send to that group (model.output_routes): count it as one here
        from cubaserea.model import output_routes, Send
        for i, g in output_routes(p):
            p.tracks[i].sends.append(Send(dest=g, vol=1.0, pan=0.0, mode=0))
        return p
    printer = None
    if source and other and other.lower().endswith(('.cpr', '.bak')):
        from cubaserea import print_tracks
        audio_dir = os.path.join(os.path.dirname(os.path.abspath(other)),
                                 'Audio')
        printer = print_tracks.printer(audio_dir, dry=True)
    # the same list the conversion used, so a CLAP that crossed over as its
    # VST build is not taken here for a track that must have been printed
    p = rpp_read.read(path, printer=printer, index=plugin_index())
    # both ways: against the Cubase build, and against the REAPER project
    # that came back from it (its loops arrive as the same passes)
    if source and not os.environ.get('CPR_NO_LOOP_EXPAND'):
        # a looped item past its file's end goes to Cubase as one event per
        # pass (model.expand_loops): the REAPER side is cut the same way, so
        # the events are compared pass by pass
        from cubaserea import media
        from cubaserea.model import expand_loops

        audio_dir = (os.path.join(os.path.dirname(os.path.abspath(other)), 'Audio')
                     if other else None)

        def _dur(f):
            f = media.resolve(f, p.srcdir)
            if not os.path.isfile(f):
                return None
            d = media.wav_duration(f)
            if d is None and audio_dir:
                # an MP3 is measured as the conversion decoded it: the WAV
                # beside the Cubase project (cpr_build decodes looped
                # non-WAV items before cutting them at the file's end)
                w = os.path.join(audio_dir, os.path.splitext(os.path.basename(f))[0] + '.wav')
                if os.path.isfile(w):
                    d = media.wav_duration(w)
            if d is None:
                # or decoded here the same way (against a REAPER project,
                # there is no Cubase Audio folder to measure from)
                d = media.decoded_duration(f, gapless.get(os.path.normcase(f), False),
                                           p.samplerate or 48000.0)
            return d
        gapless = {}
        for t_ in p.tracks:
            for i_ in t_.items:
                if i_.file:
                    gapless[os.path.normcase(media.resolve(i_.file, p.srcdir))] = \
                        '1' in [str(a) for a in (getattr(i_, 'file_args', None) or [])]
        expand_loops(p, None, _dur)
    return p


def run(first, second, out=sys.stdout):
    """Compare two projects, whichever way round they are given."""
    src, dst = load(first, second, True), load(second, first, False)
    # the builder splits a mixed MIDI+audio track in two; compare like with like
    global to_cubase, both_cubase
    to_cubase = second.lower().endswith(('.cpr', '.bak'))
    both_cubase = to_cubase and first.lower().endswith(('.cpr', '.bak'))
    if first.lower().endswith('.rpp'):
        # only going to Cubase: a REAPER item is a window onto its source and
        # the builder cuts each part down to it, so the source side is read
        # the same way. A REAPER project this tool wrote from a .cpr is not
        # windowed - its items already are their sources.
        if to_cubase:
            window_midi(src)
            if os.path.exists(first + '.cubase-origin'):
                # a project that came from Cubase goes back into its
                # original, whose parts stay whole with the window as their
                # offset: the Cubase side is cut to its windows the same way
                # (Salonica's kaval part, split around a part lying on it)
                window_midi(dst)
            # the builder puts a video item's audio on a track of its own
            from cubaserea.model import split_video_audio
            split_video_audio(src, second, None)
            # a muted folder is a muted group in Cubase and its tracks play
            # on into it, as in REAPER - nothing to carry down any more
            # (the builder used to mute the tracks inside as well)
            # a muted folder silences what is inside it, and the builder
            # carries that down to the tracks; read the source the same way
            pass
        expand_for_cubase(src)
    if first.lower().endswith(('.cpr', '.bak')) and second.lower().endswith('.rpp'):
        # going to REAPER, a video's split-off audio track is folded back
        # into the video item (model.merge_video_audio): read Cubase so too
        from cubaserea.model import merge_video_audio
        merge_video_audio(src, None)
    if second.lower().endswith('.rpp'):
        expand_for_cubase(dst)
    rep = compare(src, dst, Report())
    print(os.path.basename(first), file=out)
    print('  vs ' + os.path.basename(second), file=out)
    print(file=out)
    rep.show(out)
    return rep


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: verify.py project-one project-two   '
                 '(a .rpp and a .cpr, in either order)')
    rep = run(sys.argv[1], sys.argv[2])
    sys.exit(1 if rep.differences else 0)


if __name__ == '__main__':
    main()
