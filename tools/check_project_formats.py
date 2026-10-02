"""A real project's plug-ins through plugin_formats, checked against the
plug-ins: each one with both builds installed here is turned into its
other build (VST3 -> VST2, VST2 -> VST3), and both builds load their state
- the original into its own build, the converted one into the other - and
report their parameters, which must agree.

    python tools/check_project_formats.py <project> [max]
"""
import copy
import io
import json
import os
import sys
import tempfile
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))
sys.path.insert(0, HERE)
from cubaserea import plugin_formats as F
from format_probe import watched, find_file, DIRS2, DIRS3

PROBE = os.path.join(HERE, 'format_probe.py')


def tmp(b):
    fd, p = tempfile.mkstemp(suffix='.bin')
    os.write(fd, b)
    os.close(fd)
    return p


def files_for(e):
    v2 = find_file(e['vst2']['name'] + '.dll', DIRS2 + [r'C:\Program Files\Steinberg\VstPlugins\FabFilter'])
    v3 = find_file(e['vst3']['name'] + '.vst3', DIRS3) or \
        find_file('FabFilter %s.vst3' % e['vst3']['name'], DIRS3)
    return v2, v3


def vst2_params(f2, fx):
    st = F.reaper_vst2_state(fx)
    if fx.param_dump or st[:8] == F.PARAM_DUMP:
        import struct
        body = st[8:]
        return list(struct.unpack('<%df' % (len(body) // 4), body))
    r = watched([sys.executable, PROBE, 'vst2mod', f2, '[]', tmp(st)], 'v2', timeout=120)
    return r.get('params')


def main():
    import convert
    path = sys.argv[1]
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    log = []
    with contextlib.redirect_stdout(io.StringIO()):
        p = convert.load(path, log, None, None)
    seen = 0
    class _M:
        name = 'master'
    for t, fx in [(t, f) for t in p.tracks for f in ([t.instrument] if t.instrument else []) + list(t.fx)] +             [(_M, f) for f in (p.master.fx if p.master is not None else [])]:
        if True:
            if getattr(fx, 'native', False) or not fx.uid:
                continue
            e = F.entry_for(fx)
            if not e:
                continue
            f2, f3 = files_for(e)
            if not (f2 and f3):
                continue
            if seen >= cap:
                return
            seen += 1
            g = copy.deepcopy(fx)
            if F.is_vst2(fx):
                ok = F.to_vst3(g)
                a = vst2_params(f2, fx)
                b = watched([sys.executable, PROBE, 'norms', f3, e['vst3']['uid'], tmp(g.component)], 'n',
                            timeout=120).get('norms') if ok else None
                way = 'VST2 -> VST3'
            else:
                ok = F.to_vst2(g)
                a = watched([sys.executable, PROBE, 'norms', f3, e['vst3']['uid'], tmp(fx.component)], 'n',
                            timeout=120).get('norms')
                b = vst2_params(f2, g) if ok else None
                way = 'VST3 -> VST2'
            if not ok or a is None or b is None:
                print('%-14s %-16s %s: not checked (%s)' % (t.name[:14], fx.name[:16], way,
                                                          'refused' if not ok else 'no read'))
                continue
            n = min(len(a), len(b), e['vst2'].get('params', len(b)))
            if e['recipe'] == 'params':
                n = min(n, len(e['curves']))
            off = [(i, round(a[i], 4), round(b[i], 4)) for i in range(n) if abs(a[i] - b[i]) > 2e-3]
            moved = sum(1 for i in range(n) if abs(a[i] - b[i]) <= 2e-3)
            print('%-14s %-16s %s (%s): %d of %d parameters the same%s' % (
                t.name[:14], fx.name[:16], way, e['recipe'], moved, n, '' if not off else '  OFF %s' % off[:4]))


if __name__ == '__main__':
    main()
