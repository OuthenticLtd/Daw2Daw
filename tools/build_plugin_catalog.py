"""Build src/cubaserea/plugin_catalog.json: which VST2 build pairs with
which VST3 build, and how one's state becomes the other's (see
plugin_formats.py), from this machine's plug-ins.

    python tools/build_plugin_catalog.py <probe.json> [--fabfilter]

probe.json is tools/format_probe.py's 'pairs' output (each build's default
state, read with no host). A pair is given the recipe its two states
show:
  'vstw'    the VST3 state is 'VstW' + the VST2 bank, chunk or parameters
  'same'    the VST2 chunk is the VST3 state, or its start (JUCE adds its
            private data after it), or the two only differ in bytes a
            build writes of itself (both carry the same block)
  'params'  the VST2 keeps only parameters and the VST3 a 'FabF' block -
            FabFilter's - its curves read from the VST3 controller
Anything else is left out (reported), so it is never crossed blind.
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))
sys.path.insert(0, HERE)
OUT = os.path.join(os.path.dirname(HERE), 'src', 'cubaserea', 'plugin_catalog.json')


def four_of(n):
    return struct.pack('>i', n if n < 2 ** 31 else n - 2 ** 32).decode('latin1')


def name_of(disp):
    return disp.split(' (')[0].strip()


def vendor_of(disp):
    import re
    m = re.search(r'\(([^()]*)\)', disp)
    return m.group(1).strip() if m else ''


def classify(rec):
    from cubaserea import plugin_formats as F
    v2, v3 = rec.get('vst2', {}), rec.get('vst3', {})
    if 'error' in v2 or 'error' in v3:
        return None, 'probe failed: %s' % (v2.get('error') or v3.get('error'))
    s3 = bytes.fromhex(v3.get('state', ''))
    ctrl = bytes.fromhex(v3.get('controller', '') or '')
    chunk = bytes.fromhex(v2.get('bank_chunk', '') or v2.get('program_chunk', '') or '')
    if s3[:4] == b'VstW':
        got = F.unvstw(s3)
        # the wrapper's bank holds the VST2's own chunk (or parameters); the
        # two defaults' contents may differ (a factory preset each)
        if got and (('chunk' in got and chunk) or ('params' in got and not chunk)):
            return 'vstw', 'the VST3 is the VST2 wrapped'
        return None, 'VstW, but its bank is not the VST2 chunk'
    if chunk and len(chunk) > 8 and chunk[4:8] == s3[:4] \
            and abs(struct.unpack_from('<I', chunk, 0)[0] - len(s3)) < 64 \
            and struct.unpack_from('<I', chunk, 0)[0] + 4 <= len(chunk):
        return 'prefixed', 'VST2 = u32 length + the VST3 state + its preset name'
    if chunk and chunk[:1] == b'x' and s3[:2] == chunk[:2]:
        import zlib
        try:
            if len(zlib.decompress(chunk)) == len(zlib.decompress(s3)):
                return 'same', 'the same compressed patch'
        except Exception:
            pass
    if chunk and b'hsin' in chunk[:64] and b'hsin' in s3[:64] and chunk[:4] == s3[:4]:
        return 'same', "Native Instruments' hsin container both"
    if chunk:
        if s3 == chunk or s3.startswith(chunk) or (len(chunk) > 16 and chunk[:16] == s3[:16]):
            return 'same', 'the same block (%d / %d bytes)%s' % (
                len(chunk), len(s3), ', controller the same' if ctrl == s3 else '')
        return None, 'different blocks (VST2 %r.. VST3 %r..)' % (chunk[:8], s3[:8])
    if s3[:4] == b'FabF':
        return 'params', 'FabFilter: parameters / FabF'
    return None, 'VST2 keeps parameters only, VST3 %r..' % s3[:8]


def entry(rec, recipe, why):
    v2, v3 = rec['vst2'], rec['vst3']
    s3 = bytes.fromhex(v3.get('state', ''))
    ctrl = bytes.fromhex(v3.get('controller', '') or '')
    return {'product': name_of(rec['disp']), 'vendor': vendor_of(rec['disp']),
            'vst2': {'id': four_of(rec['num']), 'name': name_of(rec['disp']),
                     'version': v2.get('version', 1), 'params': v2.get('numParams', 0)},
            'vst3': {'uid': rec['uid'].upper(), 'name': name_of(rec['disp'])},
            'recipe': recipe, 'why': why,
            'controller_same': bool(ctrl) and ctrl == s3,
            'tail': (lambda c: c[4 + struct.unpack_from('<I', c, 0)[0]:].hex())(
                bytes.fromhex(v2.get('bank_chunk', '') or '')) if recipe == 'prefixed' else ''}


def compact(curve):
    """A sampled curve, as its two ends when it is a straight line."""
    c = [round(x, 6) for x in curve]
    k = len(c) - 1
    span = c[-1] - c[0]
    if all(abs(c[i] - (c[0] + span * i / k)) <= 1e-4 * max(1.0, abs(span)) for i in range(len(c))):
        return [c[0], c[-1]]
    return c


def fabfilter():
    """FabFilter's plug-ins: both builds of each, the VST3's class id and
    curves read from it, the VST2's id from its DLL."""
    import glob
    import subprocess
    from format_probe import watched
    probe = os.path.join(HERE, 'format_probe.py')
    out = []
    v3dir = r'C:\Program Files\Common Files\VST3'
    v2dir = r'C:\Program Files\Steinberg\VstPlugins\FabFilter'
    for f3 in sorted(glob.glob(os.path.join(v3dir, 'FabFilter *.vst3'))):
        base = os.path.splitext(os.path.basename(f3))[0]
        f2 = os.path.join(v2dir, base + '.dll')
        if not os.path.exists(f2):
            continue
        r = subprocess.run([sys.executable, '-m', 'cubaserea.vst3params', f3], capture_output=True, text=True,
                           timeout=120, cwd=os.path.join(os.path.dirname(HERE), 'src'))
        try:
            uid = json.loads(r.stdout)[0]['uid']
        except Exception:
            print(base, 'no class id')
            continue
        c = watched([sys.executable, probe, 'curves', f3, uid], base + '-curves', timeout=120)
        v2 = watched([sys.executable, probe, 'vst2', f2], base + '-vst2', timeout=90)
        if 'error' in c or 'error' in v2:
            print(base, 'failed', c.get('error'), v2.get('error'))
            continue
        n2 = v2['numParams']
        params = c['params'][:n2]
        # the two builds name the same parameters in the same order
        same = sum(1 for a, b in zip(params, v2['param_names'])
                   if a['title'][:6].lower() == b[:6].lower())
        name = base.replace('FabFilter ', '')
        if c['state'][:8] == b'FFBS'.hex():
            out.append({'product': name, 'vendor': 'FabFilter',
                        'vst2': {'id': v2['id'], 'name': base, 'version': v2['version'], 'params': n2,
                                 'default': v2.get('bank_chunk', '')},
                        'vst3': {'uid': uid, 'name': name, 'default': c['state']},
                        'recipe': 'ffbs', 'why': "FabFilter: 'FFBS' both, each build's own sections",
                        # for a VST2 kept as its parameter list
                        'curves': [{'lo': p['curve'][0], 'hi': p['curve'][-1], 'steps': p['steps']}
                                   if p['steps'] > 0 else compact(p['curve']) for p in c['params'][:n2]]})
            print(base, uid, 'ffbs')
            continue
        out.append({'product': name, 'vendor': 'FabFilter',
                    'vst2': {'id': v2['id'], 'name': base, 'version': v2['version'], 'params': n2},
                    'vst3': {'uid': uid, 'name': name, 'default': c['state']},
                    'recipe': 'params',
                    'why': 'FabFilter: VST2 parameters / VST3 FabF; %d of %d names agree' % (same, n2),
                    'curves': [{'lo': p['curve'][0], 'hi': p['curve'][-1], 'steps': p['steps']}
                               if p['steps'] > 0 else compact(p['curve']) for p in params]})
        print(base, uid, n2, 'names agree', same)
    return out


def main():
    probe = json.load(open(sys.argv[1]))
    entries, skipped = [], []
    for k, rec in sorted(probe.items()):
        recipe, why = classify(rec)
        if recipe:
            entries.append(entry(rec, recipe, why))
        else:
            skipped.append((rec.get('disp', k), why))
    if '--fabfilter' in sys.argv:
        entries.extend(fabfilter())
    json.dump({'entries': entries}, open(OUT, 'w', encoding='utf-8'), indent=0)
    print('%d entries -> %s' % (len(entries), OUT))
    for r in entries:
        print('  %-30s %-7s %s' % (r['product'][:30], r['recipe'], r['why'][:70]))
    print('left out:')
    for d, why in skipped:
        print('  %-30s %s' % (d[:30], why[:90]))


if __name__ == '__main__':
    main()
