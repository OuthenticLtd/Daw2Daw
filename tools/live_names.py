"""Name Live's exported stems the way render_reference.py names REAPER's.

    python tools/live_names.py live_export_dir out_dir prefix [--sum "A=B+C" ...]

Live writes '<prefix> <track>.wav' per track, '<prefix> A-<return>.wav' for
returns and '<prefix>.wav' for the master; compare_renders.py wants
'<track>.wav' and 'master.wav'. --sum adds stems together (a REAPER track
the conversion spread over several Live tracks).
"""
import os
import re
import shutil
import sys


def main():
    src, out, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    sums = [a for a in sys.argv[4:] if a != '--sum']
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.lower().endswith('.wav'):
            os.remove(os.path.join(out, f))
    for f in os.listdir(src):
        if not f.lower().endswith('.wav'):
            continue
        n = f[:-4]
        if n == prefix:
            n = 'master'
        else:
            n = re.sub(r'^%s ' % re.escape(prefix), '', n)
            n = re.sub(r'^[A-L]-', '', n)
        shutil.copyfile(os.path.join(src, f), os.path.join(out, n + '.wav'))
    # repeated names: Live writes X, X-1, X-2 ..., REAPER X-001, X-002 ...
    names = {f[:-4] for f in os.listdir(out) if f.lower().endswith('.wav')}
    for n in sorted(names):
        m = re.match(r'^(.*)-(\d+)$', n)
        if m and m.group(1) in names and not re.search(r'-\d{3}$', n):
            base = m.group(1)
            os.replace(os.path.join(out, n + '.wav'),
                       os.path.join(out, '%s-%03d.wav' % (base, int(m.group(2)) + 1)))
    for n in sorted(names):
        if os.path.exists(os.path.join(out, n + '-002.wav')) and os.path.exists(os.path.join(out, n + '.wav')):
            os.replace(os.path.join(out, n + '.wav'), os.path.join(out, n + '-001.wav'))
    if sums:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import numpy as np
        import struct
        from live_calibrate import read
        for s in sums:
            dst, parts = s.split('=', 1)
            xs = [read(os.path.join(out, p + '.wav')) for p in parts.split('+')]
            n = max(len(x) for x in xs)
            tot = np.zeros((n, xs[0].shape[1]))
            for x in xs:
                tot[:len(x)] += x
            data = tot.astype('<f4').tobytes()
            with open(os.path.join(out, dst + '.wav'), 'wb') as fh:
                ch = tot.shape[1]
                fh.write(b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE')
                fh.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 3, ch, 48000, 48000 * 4 * ch, 4 * ch, 32))
                fh.write(b'data' + struct.pack('<I', len(data)) + data)
            for p in parts.split('+'):
                if p != dst:
                    os.remove(os.path.join(out, p + '.wav'))
    print(len(os.listdir(out)), 'stems')


if __name__ == '__main__':
    main()
