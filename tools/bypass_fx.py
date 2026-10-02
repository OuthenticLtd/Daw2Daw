"""Copy a REAPER project with some plug-ins bypassed.

    python tools/bypass_fx.py in.rpp out.rpp "Pro-C 2" ["Other name" ...]

For an identity test on a machine where the target DAW cannot load a
plug-in the source uses (an install problem there, not the conversion):
the REAPER reference is rendered from this copy, so that plug-in counts on
neither side and the rest is measured. Every <VST/<CLAP block whose display
name contains one of the names gets BYPASS 1 on the line before it.
"""
import sys


def main():
    src, dst, names = sys.argv[1], sys.argv[2], [n.lower() for n in sys.argv[3:]]
    lines = open(src, encoding='utf-8', errors='replace').read().split('\n')
    n = 0
    for i, l in enumerate(lines):
        s = l.strip()
        if (s.startswith('<VST') or s.startswith('<CLAP')) and any(x in s.lower() for x in names):
            for j in range(i - 1, max(0, i - 4), -1):
                if lines[j].strip().startswith('BYPASS'):
                    ind = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
                    parts = lines[j].split()
                    parts[1] = '1'
                    lines[j] = ind + ' '.join(parts)
                    n += 1
                    break
    with open(dst, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))
    print('bypassed %d plug-in(s)' % n)


if __name__ == '__main__':
    main()
