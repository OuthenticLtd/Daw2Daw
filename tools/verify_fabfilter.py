"""Check plugin_formats' FabFilter tables against the plug-ins: random
VST2 parameter values -> the VST3 state the converter writes -> the VST3
itself loads it (setState, setComponentState) and reports its normalised
values, which must be the VST2's.   python tools/verify_fabfilter.py <fabfilter.json>"""
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))
sys.path.insert(0, HERE)
from cubaserea import plugin_formats as F
from format_probe import watched

ents = json.load(open(sys.argv[1]))
rng = random.Random(7)
import struct
V2 = r'C:\Program Files\Steinberg\VstPlugins\FabFilter\FabFilter %s.dll'
probe = os.path.join(HERE, 'format_probe.py')


def tmp(b):
    fd, path = tempfile.mkstemp(suffix='.bin')
    os.write(fd, b)
    os.close(fd)
    return path


def edit(b, picks):
    b = bytearray(b)
    for i, v in picks:
        struct.pack_into('<f', b, 12 + 4 * i, v)
    return bytes(b)


for e in ents:
    if e['recipe'] == 'ffbs':
        f3 = r'C:\Program Files\Common Files\VST3\FabFilter %s.vst3' % e['product']
        s2, s3 = bytes.fromhex(e['vst2']['default']), bytes.fromhex(e['vst3']['default'])
        n = struct.unpack_from('<I', s3, 8)[0]
        vals = struct.unpack_from('<%df' % n, s3, 12)
        picks = [(i, vals[i] * 0.9) for i in rng.sample(range(n), min(60, n)) if abs(vals[i]) > 1e-3]
        # VST2 -> VST3: its own edited block vs the VST2's swapped in
        a = watched([sys.executable, probe, 'norms', f3, e['vst3']['uid'], tmp(F.ffbs_swap(edit(s2, picks), s3))], 'a')
        b = watched([sys.executable, probe, 'norms', f3, e['vst3']['uid'], tmp(edit(s3, picks))], 'b')
        d1 = sum(abs(x - y) > 1e-6 for x, y in zip(a['norms'], b['norms']))
        x = watched([sys.executable, probe, 'vst2set', V2 % e['product'], tmp(F.ffbs_swap(edit(s3, picks), s2))], 'x')
        y = watched([sys.executable, probe, 'vst2set', V2 % e['product'], tmp(edit(s2, picks))], 'y')
        z = watched([sys.executable, probe, 'vst2set', V2 % e['product'], tmp(s2)], 'z')
        d2 = sum(abs(u - w) > 1e-6 for u, w in zip(x['params'], y['params']))
        moved = sum(abs(u - w) > 1e-6 for u, w in zip(y['params'], z['params']))
        print('%-12s ffbs  %d edits | VST2->VST3: %d of %d differ | VST3->VST2: %d of %d differ (the edit moved %d)'
              % (e['product'], len(picks), d1, len(a['norms']), d2, len(x['params']), moved))
        continue
    n2 = e['vst2']['params']
    vals = []
    for c in e['curves'][:n2]:
        if isinstance(c, dict):
            st = max(1, c['steps'])
            vals.append(rng.randint(0, st) / st)
        else:
            vals.append(round(rng.random(), 4))
    state = F.params_to_vst3(e, vals)
    fd, path = tempfile.mkstemp(suffix='.bin')
    os.write(fd, state)
    os.close(fd)
    f3 = r'C:\Program Files\Common Files\VST3\FabFilter %s.vst3' % e['product']
    r = watched([sys.executable, os.path.join(HERE, 'format_probe.py'), 'norms', f3, e['vst3']['uid'], path],
                e['product'] + '-norms', timeout=120)
    if 'error' in r:
        print('%-12s error %s' % (e['product'], r['error'][:80]))
        continue
    nf = F.fabf_values(bytes.fromhex(e['vst3']['default']))
    n2 = min(n2, len(nf[1]))           # what the VST3 block holds
    vals = vals[:n2]
    got = r['norms'][:n2]
    err = [abs(a - b) for a, b in zip(got, vals)]
    bad = [(i, vals[i], got[i]) for i, x in enumerate(err) if x > 2e-3]
    back = F.vst3_to_params(e, bytes.fromhex(r['state_back']))
    err2 = max(abs(a - b) for a, b in zip(back, vals)) if back else None
    print('%-12s %4d params  max err %.5f  off %d %s  | back through FabF max err %s' % (
        e['product'], n2, max(err) if err else 0, len(bad), bad[:3], '%.5f' % err2 if err2 is not None else '-'))
