"""Check every catalog recipe (plugin_formats) against the plug-ins
themselves, both ways, with settings that are not the defaults:

  VST2: some parameters moved -> its chunk
     -> plugin_formats.to_vst3 -> the VST3 loads it (setState) -> its
        parameters, matched to the VST2's by name, must show the moves
     -> the VST3's state back out -> plugin_formats.to_vst2 -> the VST2
        loads it (setChunk) -> its parameters must show them again

    python tools/verify_formats.py <probe.json> [product ...]
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))
sys.path.insert(0, HERE)
from cubaserea import plugin_formats as F
from cubaserea.model import Fx
from format_probe import watched

PROBE = os.path.join(HERE, 'format_probe.py')


def tmp(b):
    fd, p = tempfile.mkstemp(suffix='.bin')
    os.write(fd, b)
    os.close(fd)
    return p


def norm_name(s):
    return ''.join(ch for ch in (s or '').lower() if ch.isalnum())


def roundtrip(e, f2, f3, uid, names2):
    """No VST3 parameter list to compare (it needs a fuller host): VST2
    edits -> VST3 state -> the VST3 loads and saves it -> VST2 again; the
    edits survive only if the VST3 read them."""
    os.environ['PROBE_NOCONNECT'] = '1'
    try:
        moved = list(range(0, len(names2), max(1, len(names2) // 6)))[:6]
        edits = [[i, (0.31 if k % 2 else 0.73)] for k, i in enumerate(moved)]
        a = watched([sys.executable, PROBE, 'vst2mod', f2, json.dumps(edits)], e['product'] + '-mod', timeout=120)
        if 'chunk' not in a:
            print('%-24s VST2 edit: %s' % (e['product'], a.get('error', 'no chunk')[:60]))
            return
        ref = watched([sys.executable, PROBE, 'vst2mod', f2, '[]', tmp(bytes.fromhex(a['chunk']))], 'ref', timeout=120)
        fx = Fx()
        fx.format, fx.name = 'VST', e['vst2']['name']
        fx.vst2_id = int.from_bytes(e['vst2']['id'].encode('latin1'), 'big', signed=True)
        fx.uid = F.vst2_pseudo_uid(e['vst2']['id'], fx.name)
        fx.raw_state = fx.component = bytes.fromhex(a['chunk'])
        F.to_vst3(fx)
        b = watched([sys.executable, PROBE, 'norms', f3, uid, tmp(fx.component)], e['product'] + '-n', timeout=120)
        if 'error' in b:
            print('%-24s VST3 load: %s' % (e['product'], b['error'][:60]))
            return
        fx2 = Fx()
        fx2.format, fx2.name, fx2.uid = 'VST3', e['vst3']['name'], uid
        fx2.component = bytes.fromhex(b['state_back'])
        F.to_vst2(fx2)
        c = watched([sys.executable, PROBE, 'vst2mod', f2, '[]', tmp(F.reaper_vst2_state(fx2))], 'back', timeout=120)
        d = [(i, round(ref['params'][i], 3), round(c['params'][i], 3)) for i in moved
             if abs(ref['params'][i] - c['params'][i]) > 2e-3]
        dflt = watched([sys.executable, PROBE, 'vst2mod', f2, '[]'], 'dflt', timeout=120)
        changed = sum(abs(ref['params'][i] - dflt['params'][i]) > 2e-3 for i in moved)
        print('%-24s %-8s %d moved (%d off their default) | VST2->VST3->VST2 %s' % (
            e['product'], e['recipe'], len(moved), changed, 'OK' if not d else 'OFF %s' % d[:3]))
    finally:
        os.environ.pop('PROBE_NOCONNECT', None)


def main():
    probe = json.load(open(sys.argv[1]))
    only = set(a.lower() for a in sys.argv[2:])
    by_uid = {r['uid'].upper(): r for r in probe.values() if r.get('uid')}
    for e in F.catalog()['entries']:
        if e['vendor'] == 'FabFilter' or e['recipe'] not in ('same', 'vstw', 'prefixed'):
            continue
        if only and e['product'].lower() not in only:
            continue
        rec = by_uid.get(e['vst3']['uid'].upper())
        if not rec:
            continue
        f2, f3, uid = rec['vst2_file'], rec['vst3_file'], e['vst3']['uid']
        names2 = rec['vst2'].get('param_names', [])
        n2 = len(names2)
        if not n2:
            print('%-24s no VST2 parameters to check by' % e['product'])
            continue
        cur = watched([sys.executable, PROBE, 'curves', f3, uid], e['product'] + '-titles', timeout=120)
        if 'error' in cur or not cur.get('params'):
            roundtrip(e, f2, f3, uid, names2)
            continue
        titles = [norm_name(p['title']) for p in cur['params']]
        pairs = []
        used = set()
        for i, nm in enumerate(names2):
            k = norm_name(nm)
            if not k:
                continue
            if k in titles and titles.index(k) not in used:
                j = titles.index(k)
            else:
                # VST2 names stop at 8 characters: the VST3 title starting so,
                # when only one does
                cands = [j for j, t in enumerate(titles) if t.startswith(k[:6]) and j not in used]
                if len(cands) != 1:
                    continue
                j = cands[0]
            used.add(j)
            pairs.append((i, j))
        cand = [p for p in pairs if not (cur['params'][p[1]]['flags'] & 0x10000)][:200]
        if not cand:
            print('%-24s no parameter names in common (%d VST2)' % (e['product'], n2))
            continue
        step = max(1, len(cand) // 6)
        moved = cand[::step][:6]
        edits = [[i, (0.31 if k % 2 else 0.73)] for k, (i, _) in enumerate(moved)]
        a = watched([sys.executable, PROBE, 'vst2mod', f2, json.dumps(edits)], e['product'] + '-mod', timeout=120)
        if 'error' in a or 'chunk' not in a:
            print('%-24s VST2 edit: %s' % (e['product'], a.get('error', 'no chunk')[:60]))
            continue
        # what the state holds: the VST2 reloading its own chunk (a switch
        # moved to 0.73 is 1; a solo button is not kept)
        ref = watched([sys.executable, PROBE, 'vst2mod', f2, '[]', tmp(bytes.fromhex(a['chunk']))], 'ref', timeout=120)
        p2 = ref['params'] if 'params' in ref else a['params']
        fx = Fx()
        fx.format, fx.name = 'VST', e['vst2']['name']
        fx.vst2_id = int.from_bytes(e['vst2']['id'].encode('latin1'), 'big', signed=True)
        fx.uid = F.vst2_pseudo_uid(e['vst2']['id'], fx.name)
        fx.raw_state = fx.component = bytes.fromhex(a['chunk'])
        if not F.to_vst3(fx):
            print('%-24s to_vst3 refused' % e['product'])
            continue
        b = watched([sys.executable, PROBE, 'norms', f3, uid, tmp(fx.component)], e['product'] + '-n', timeout=120)
        if 'error' in b:
            print('%-24s VST3 load: %s' % (e['product'], b['error'][:60]))
            continue
        n3 = b['norms']
        d1 = [(i, round(p2[i], 3), round(n3[j], 3)) for i, j in moved if abs(p2[i] - n3[j]) > 2e-3]
        fx2 = Fx()
        fx2.format, fx2.name, fx2.uid = 'VST3', e['vst3']['name'], uid
        fx2.component = bytes.fromhex(b['state_back'])
        if not F.to_vst2(fx2):
            print('%-24s to_vst2 refused' % e['product'])
            continue
        c = watched([sys.executable, PROBE, 'vst2mod', f2, '[]', tmp(F.reaper_vst2_state(fx2))],
                    e['product'] + '-back', timeout=120)
        if 'error' in c:
            print('%-24s VST2 load back: %s' % (e['product'], c['error'][:60]))
            continue
        d2 = [(i, round(p2[i], 3), round(c['params'][i], 3)) for i, _ in moved if abs(p2[i] - c['params'][i]) > 2e-3]
        print('%-24s %-8s %d moved | VST2->VST3 %s | VST3->VST2 %s' % (
            e['product'], e['recipe'], len(moved), 'OK' if not d1 else 'OFF %s' % d1[:3],
            'OK' if not d2 else 'OFF %s' % d2[:3]))


if __name__ == '__main__':
    main()
