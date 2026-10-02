"""Where a REAPER plug-in keeps each parameter in its saved data block.

    python tools/reaper_probe.py "VST: ReaXcomp (Cockos)" out.json

Runs tools/reaper_probe.lua in a REAPER of its own (headless, a new
project), which sets every parameter in turn and saves the track's state;
the data blocks are compared here. Writes, per parameter: its name, its
displayed value at 0 / .25 / .5 / .75 / 1, and the byte offsets (and f32 /
f64 reading) that changed - the layout stock.py writes into, and the units
behind it.
"""
import base64
import json
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'src'))
from cubaserea import media   # noqa: E402


def data_block(chunk):
    """The plug-in's own data (blob 1) from a track chunk's <VST block."""
    lines = chunk.split('\\n')
    i = next(k for k, l in enumerate(lines) if l.strip().startswith('<VST'))
    blob, blobs = [], []
    for l in lines[i + 1:]:
        s = l.strip()
        if s.startswith('>'):
            break
        blob.append(s)
        if len(s) < 128:
            blobs.append(base64.b64decode(''.join(blob)))
            blob = []
    return blobs[1] if len(blobs) > 1 else b''


def main():
    name, out = sys.argv[1], os.path.abspath(sys.argv[2])
    raw = out + '.txt'
    with open(os.path.join(HERE, 'probe_in.txt'), 'w') as f:
        f.write(name + '\n' + raw + '\n')
    if os.path.exists(raw):
        os.remove(raw)
    p = subprocess.Popen([media.find_reaper_exe(), '-nosplash', '-new',
                          os.path.join(HERE, 'reaper_probe.lua')])
    for _ in range(240):
        time.sleep(1)
        if os.path.exists(raw) and open(raw, encoding='utf-8', errors='replace').read().endswith('END\n'):
            break
    p.kill()
    base, params, chunks = None, [], {}
    for l in open(raw, encoding='utf-8', errors='replace'):
        t = l.rstrip('\n').split('\t')
        if t[0] == 'BASE':
            base = data_block(t[1])
        elif t[0] == 'P':
            params.append(t[1:])
        elif t[0] == 'C':
            chunks[int(t[1])] = data_block(t[2])
    res = []
    for pr in params:
        i = int(pr[0])
        d = chunks.get(i, b'')
        diff = [k for k in range(min(len(d), len(base))) if d[k] != base[k]]
        lo = (min(diff) // 4) * 4 if diff else None
        entry = {'index': i, 'name': pr[1], 'default': float(pr[2]), 'display': pr[3:],
                 'changed': [diff[0], diff[-1]] if diff else None, 'len': len(d)}
        if lo is not None:
            entry['f32_at'] = lo
            entry['f32_base'] = struct.unpack_from('<f', base, lo)[0]
            entry['f32_at_0.3'] = struct.unpack_from('<f', d, lo)[0]
        res.append(entry)
    json.dump({'fx': name, 'base': base.hex(), 'params': res}, open(out, 'w'), indent=1)
    for e in res:
        print(e['index'], e['name'], e['changed'], e.get('f32_base'), e.get('f32_at_0.3'), e['display'][:5])


if __name__ == '__main__':
    main()
