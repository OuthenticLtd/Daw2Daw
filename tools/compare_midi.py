"""Compare the MIDI notes of a converted Live Set with its source project.

    python tools/compare_midi.py source.rpp|source.cpr converted.als

Every note on both sides as (track, start, length, pitch, velocity), times
in quarter notes from the start of the song: the source through the
converter's own reader (the parts windowed the way the host plays them),
the Live Set straight from its XML (clip start + note time, notes outside
the clip's window left out the way Live leaves them out). Prints what is
missing, extra or different per track; exit status 1 on any difference.
"""
import gzip
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

TOL = 1e-4          # quarter notes


def source_notes(path):
    from cubaserea import progress, model
    from cubaserea.model import _qn_upto
    progress.enable(False)
    os.environ.setdefault('CPR_NO_REAPER', '1')
    if path.lower().endswith('.rpp'):
        from cubaserea import rpp_read
        p = rpp_read.read(path, [])
    else:
        from cubaserea import cpr_read
        p = cpr_read.read(path)
    model.window_midi(p, [])
    tempo = sorted(p.tempo or [(0.0, 120.0)])
    out = {}
    for t in p.tracks:
        for it in model.playing_items(t):
            if it.kind != 'midi' or it.mute:
                continue
            q0 = _qn_upto(tempo, it.pos)
            ppq = it.ppq or 480.0
            for n in it.notes:
                pos, ln, _ch, pitch, vel = n[:5]
                out.setdefault(t.name, []).append(
                    (round(q0 + pos / ppq, 4), round(max(ln, 1.0) / ppq, 4), int(pitch), int(vel)))
    return out


def live_notes(path):
    root = ET.fromstring(gzip.open(path).read())
    out = {}
    for tr in root.find('LiveSet/Tracks'):
        if tr.tag != 'MidiTrack':
            continue
        name = tr.find('Name/EffectiveName').get('Value')
        evs = tr.find('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events')
        for c in evs:
            if c.find('Disabled').get('Value') == 'true':
                continue
            start = float(c.find('CurrentStart').get('Value'))
            end = float(c.find('CurrentEnd').get('Value'))
            ls = float(c.find('Loop/LoopStart').get('Value'))
            for kt in c.find('Notes/KeyTracks'):
                pitch = int(kt.find('MidiKey').get('Value'))
                for n in kt.find('Notes'):
                    if n.get('IsEnabled') == 'false':
                        continue
                    t = float(n.get('Time')) - ls
                    if t < -TOL or start + t >= end - TOL:
                        continue
                    out.setdefault(name, []).append(
                        (round(start + t, 4), round(float(n.get('Duration')), 4),
                         pitch, int(float(n.get('Velocity')))))
    return out


def main():
    src, als = sys.argv[1], sys.argv[2]
    a, b = source_notes(src), live_notes(als)
    bad = 0
    for name in sorted(set(a) | set(b)):
        na, nb = sorted(a.get(name, [])), sorted(b.get(name, []))
        if na == nb:
            print('%-24s %4d notes  identical' % (name[:24], len(na)))
            continue
        bad += 1
        ra, rb = list(na), list(nb)
        for n in na:
            if n in rb:
                rb.remove(n)
                ra.remove(n)
        print('%-24s %d vs %d notes: %d only in the source, %d only in Live'
              % (name[:24], len(na), len(nb), len(ra), len(rb)))
        for n in ra[:5]:
            print('    source: at %.4f len %.4f pitch %d vel %d' % n)
        for n in rb[:5]:
            print('    live:   at %.4f len %.4f pitch %d vel %d' % n)
    print('%d track(s) differ' % bad)
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
