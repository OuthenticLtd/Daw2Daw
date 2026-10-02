"""A third-party plug-in's VST2 build and its VST3 build, either way round,
settings carried - no plug-in loaded, no host.

Every DAW keeps a plug-in as one format: REAPER and Live as whichever was
loaded, Cubase mostly as the VST3. When the other build is the one that
loads (Live would not load Pro-C 2's VST3 on this PC, its VST2 loads; a
machine with only the VST3 of a plug-in a project used as VST2), the
project should name that build and hand it a state it reads. Which build
pairs with which, and how its state is written, is the catalog
(plugin_catalog.json, built by tools/build_plugin_catalog.py from a
machine's plug-ins with tools/format_probe.py) - one entry per product:

  vst2: {id: four characters, name}    vst3: {uid: 32 hex, name}
  recipe:
    'same'    the two builds save the same bytes (JUCE's 'VC2!' block,
              u-he's patch text, Native Instruments' own) - measured on
              each build's default state
    'vstw'    the VST3 is Steinberg's VST2 wrapper: its state is 'VstW',
              a 12-byte header and the VST2 bank (fxb) - a chunk ('FBCh')
              or the parameters ('FxBk')
    'params'  the VST2 keeps only its parameters, the VST3 a block of its
              own (FabFilter's 'FabF': the parameters' plain values): each
              parameter's plain value at 0..1 is in the entry ('curves'),
              and the two builds agree on the normalised values (Pro-C 2:
              all 46, read from both)

A plug-in the catalog does not know still crosses when its ids follow the
two conventions most of them use: Steinberg's wrapper and its imitators
name the VST3 'VST' + the VST2 id + the first nine characters of the name
in lower case (iZotope, KORG, Native Instruments, Serum, Spitfire), with
the state handed across as it is ('same' - the wrapper's own reads it).
"""
import json
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
PARAM_DUMP = b'\xef\xbe\xad\xde\x0d\xf0\xad\xde'
_CAT = None


def catalog():
    global _CAT
    if _CAT is None:
        try:
            with open(os.path.join(HERE, 'plugin_catalog.json'), encoding='utf-8') as f:
                _CAT = json.load(f)
        except Exception:
            _CAT = {'entries': []}
        _CAT['by_uid'] = {e['vst3']['uid'].upper(): e for e in _CAT['entries'] if e.get('vst3')}
        _CAT['by_id'] = {e['vst2']['id']: e for e in _CAT['entries'] if e.get('vst2')}
    return _CAT


# ------------------------------------------------------------------ ids
def vst2_pseudo_uid(four, name):
    """The 16-byte id Cubase, REAPER and Live build for a VST2: 'VST', its
    four-character id, its name's first nine characters in lower case."""
    nm = (name or '').lower().encode('latin1', 'replace')[:9].ljust(9, b'\0')
    return (b'VST' + four.encode('latin1') + nm).hex().upper()


def four_of(fx):
    """The VST2 four-character id of a plug-in, from whichever id it has."""
    if getattr(fx, 'vst2_id', None) is not None:
        return struct.pack('>i', int(fx.vst2_id) if int(fx.vst2_id) < 2 ** 31
                           else int(fx.vst2_id) - 2 ** 32).decode('latin1')
    try:
        raw = bytes.fromhex(fx.uid or '')
    except ValueError:
        return None
    if len(raw) == 16 and raw[:3] == b'VST':
        return raw[3:7].decode('latin1')
    return None


def is_vst2(fx):
    fmt = getattr(fx, 'format', '') or ''
    if fmt:
        return fmt == 'VST'
    return bool(getattr(fx, 'vst2_id', None) is not None or getattr(fx, 'param_dump', False))


def entry_for(fx):
    """The catalog entry of this plug-in, either build, or None."""
    c = catalog()
    uid = (fx.uid or '').upper()
    if uid in c['by_uid']:
        return c['by_uid'][uid]
    four = four_of(fx)
    if four and four in c['by_id']:
        return c['by_id'][four]
    return None


# ------------------------------------------------------------- the fxb
def _fxb_head(magic, four, version, count, size_after):
    return (b'CcnK' + struct.pack('>I', size_after) + magic + struct.pack('>I', 2 if magic == b'FBCh' else 1)
            + four.encode('latin1') + struct.pack('>II', version, count) + b'\0' * 128)


def fxb_chunk(four, chunk, version=1):
    """A VST2 bank file holding the plug-in's chunk."""
    body = struct.pack('>I', len(chunk)) + chunk
    head = _fxb_head(b'FBCh', four, version, 1, 0)
    size = len(head) - 8 + len(body)
    return head[:4] + struct.pack('>I', size) + head[8:] + body


def fxb_params(four, params, version=1, name=''):
    """A VST2 bank file of one program: the parameters (normalised)."""
    prog_body = (b'FxCk' + struct.pack('>I', 1) + four.encode('latin1')
                 + struct.pack('>II', version, len(params))
                 + name.encode('latin1', 'replace')[:27].ljust(28, b'\0')
                 + b''.join(struct.pack('>f', float(v)) for v in params))
    prog = b'CcnK' + struct.pack('>I', len(prog_body)) + prog_body
    head = _fxb_head(b'FxBk', four, version, 1, 0)
    head = head[:12] + struct.pack('>I', 2) + head[16:]      # as Cubase writes it
    return head[:4] + struct.pack('>I', len(head) - 8 + len(prog)) + head[8:] + prog


def parse_fxb(b):
    """A VST2 bank/program file -> {'four', 'version', 'chunk'|'params'}."""
    if len(b) < 28 or b[:4] != b'CcnK':
        return None
    magic, four = b[8:12], b[16:20].decode('latin1')
    version = struct.unpack_from('>I', b, 20)[0]
    if magic in (b'FBCh', b'FPCh'):
        o = 28 + 128 if magic == b'FBCh' else 28 + 28
        n = struct.unpack_from('>I', b, o)[0]
        return {'four': four, 'version': version, 'chunk': b[o + 4:o + 4 + n]}
    if magic == b'FxBk':
        return parse_fxb(b[28 + 128:]) and dict(parse_fxb(b[28 + 128:]), four=four)
    if magic == b'FxCk':
        n = struct.unpack_from('>I', b, 24)[0]
        vals = struct.unpack_from('>%df' % n, b, 56)
        return {'four': four, 'version': version, 'params': list(vals),
                'name': b[28:56].split(b'\0')[0].decode('latin1')}
    return None


def vstw(fxb):
    """Steinberg's VST2 wrapper's VST3 state around a bank file."""
    return b'VstW' + struct.pack('>III', 8, 1, 0) + fxb


def unvstw(state):
    if state[:4] == b'VstW' and len(state) > 16:
        return parse_fxb(state[16:])
    return None


# ------------------------------------------------------- VST2 states
def vst2_parts(fx):
    """What a VST2 keeps: ('chunk', bytes) or ('params', [floats])."""
    raw = getattr(fx, 'raw_state', None) or b''
    if getattr(fx, 'param_dump', False) or raw[:8] == PARAM_DUMP:
        body = raw[8:]
        return 'params', list(struct.unpack('<%df' % (len(body) // 4), body[:len(body) // 4 * 4]))
    chunk = raw or fx.component or b''
    if chunk[:4] == b'VstW':
        chunk = chunk[16:]
    if chunk[:4] == b'CcnK':
        p = parse_fxb(chunk)
        if p:
            return ('chunk', p['chunk']) if 'chunk' in p else ('params', p['params'])
    return 'chunk', chunk


def set_vst2(fx, four, name, kind, data, inst):
    fx.format = 'VST'
    fx.uid = vst2_pseudo_uid(four, name)
    fx.vst2_id = struct.unpack('>i', four.encode('latin1'))[0]
    fx.name = name
    fx.controller = b''
    if kind == 'params':
        fx.param_dump = True
        fx.raw_state = PARAM_DUMP + b''.join(struct.pack('<f', float(v)) for v in data)
        fx.component = b''
    else:
        fx.param_dump = False
        fx.raw_state = data
        fx.component = data


def set_vst3(fx, uid, name, component, controller=b''):
    fx.format = 'VST3'
    fx.param_dump = False
    fx.uid = uid.upper()
    fx.name = name
    fx.vst2_id = None
    fx.component = component
    fx.controller = controller
    fx.raw_state = None


# -------------------------------------------- 'params' (FabFilter's)
def _plain(curve, n):
    """Plain value at normalised n on a curve sampled at even steps (or
    {lo, hi, steps}: a switch or a list, its values evenly spaced)."""
    if isinstance(curve, dict):
        st = max(1, curve['steps'])
        return curve['lo'] + (curve['hi'] - curve['lo']) * round(max(0.0, min(1.0, n)) * st) / st
    k = len(curve) - 1
    x = max(0.0, min(1.0, n)) * k
    i = min(k - 1, int(x))
    return curve[i] + (curve[i + 1] - curve[i]) * (x - i)


def _norm(curve, v):
    """The inverse: normalised value of plain v (the curve is monotonic)."""
    if isinstance(curve, dict):
        span = curve['hi'] - curve['lo']
        st = max(1, curve['steps'])
        return round((v - curve['lo']) / span * st) / st if span else 0.0
    k = len(curve) - 1
    up = curve[-1] >= curve[0]
    if (v <= curve[0]) == up:
        return 0.0
    if (v >= curve[-1]) == up:
        return 1.0
    for i in range(k):
        a, b = curve[i], curve[i + 1]
        if (a <= v <= b) or (b <= v <= a):
            return (i + ((v - a) / (b - a) if b != a else 0.0)) / k
    return 0.0


def fabf_values(state):
    """FabFilter's VST3 block: (head, values, tail). 'FabF', u32 2, the
    preset name (u32 length, the text: 'Default Setting' as saved in a
    project, empty in a fresh one), u32, u32 n, n f32 plain values."""
    if state[:4] != b'FabF' or len(state) < 20:
        return None
    ln = struct.unpack_from('<I', state, 8)[0]
    off = 12 + ln + 4
    if off + 4 > len(state):
        return None
    n = struct.unpack_from('<I', state, off)[0]
    if off + 4 + 4 * n > len(state):
        return None
    return state[:off + 4], list(struct.unpack_from('<%df' % n, state, off + 4)), state[off + 4 + 4 * n:]


def params_to_vst3(e, params):
    curves = e['curves']
    tpl = bytes.fromhex(e['vst3']['default'])
    head, vals, tail = fabf_values(tpl)
    for i in range(min(len(vals), len(params), len(curves))):
        vals[i] = _plain(curves[i], params[i])
    return head + b''.join(struct.pack('<f', v) for v in vals) + tail


def vst3_to_params(e, state):
    got = fabf_values(state)
    if not got:
        return None
    curves = e['curves']
    n2 = e['vst2'].get('params', len(curves))
    vals = got[1]
    return [_norm(curves[i], vals[i]) if i < len(vals) and i < len(curves) else 0.0 for i in range(n2)]


# ------------------------------------- 'ffbs' (FabFilter's newer ones)
# Both builds write 'FFBS', u32 1, u32 n, n f32 values, then tagged
# sections of their own (the VST2 a preset name...). Each build reads only
# its own sections (the VST2 ignored the VST3's block outright; the VST3
# took the VST2's in part), so the values go into the other build's own
# block, its default state from the catalog.
def ffbs_swap(state, template):
    if state[:4] != b'FFBS' or template[:4] != b'FFBS':
        return None
    n = struct.unpack_from('<I', state, 8)[0]
    m = struct.unpack_from('<I', template, 8)[0]
    k = min(n, m)
    return template[:12] + state[12:12 + 4 * k] + template[12 + 4 * k:]


def ffbs_from_params(e, params, template):
    """A VST2 kept as its parameter list (REAPER's dump of it) into the
    'FFBS' block: each value made plain on its curve."""
    curves = e.get('curves')
    if not curves or template[:4] != b'FFBS':
        return None
    n = struct.unpack_from('<I', template, 8)[0]
    vals = list(struct.unpack_from('<%df' % n, template, 12))
    for i in range(min(n, len(params), len(curves))):
        vals[i] = _plain(curves[i], params[i])
    return template[:12] + b''.join(struct.pack('<f', v) for v in vals) + template[12 + 4 * n:]


# ------------------------------------------------------------- crossing
def to_vst3(fx, log=None):
    """Turn a VST2 into its VST3 build in place. True when it crossed."""
    if not is_vst2(fx):
        return False
    e = entry_for(fx)
    kind, data = vst2_parts(fx)
    four = four_of(fx)
    name = fx.name
    if e is None:
        if not four or kind != 'chunk' or not data:
            return False
        uid = vst2_pseudo_uid(four, fx.name)     # Steinberg's convention
        set_vst3(fx, uid, name, data)
        if log is not None:
            log.append('%r: VST2 -> VST3 by its id (not in the catalog; its state handed over '
                       'as it is)' % name)
        return True
    r = e['recipe']
    v3 = e['vst3']
    if r == 'same':
        if kind != 'chunk':
            return False
        set_vst3(fx, v3['uid'], v3['name'], data, data if e.get('controller_same') else b'')
    elif r == 'vstw':
        fxb = fxb_chunk(four or e['vst2']['id'], data, e['vst2'].get('version', 1)) if kind == 'chunk' \
            else fxb_params(four or e['vst2']['id'], data, e['vst2'].get('version', 1))
        set_vst3(fx, v3['uid'], v3['name'], vstw(fxb))
    elif r == 'params':
        if kind != 'params':
            return False
        set_vst3(fx, v3['uid'], v3['name'], params_to_vst3(e, data))
    elif r == 'ffbs':
        tpl = bytes.fromhex(v3['default'])
        st = ffbs_swap(data, tpl) if kind == 'chunk' else ffbs_from_params(e, data, tpl)
        if st is None:
            return False
        set_vst3(fx, v3['uid'], v3['name'], st)
    elif r == 'prefixed':
        # iZotope: the VST2 chunk is u32 length, the VST3 state, the preset name
        if kind != 'chunk' or len(data) < 4:
            return False
        n = struct.unpack_from('<I', data, 0)[0]
        set_vst3(fx, v3['uid'], v3['name'], data[4:4 + n])
    else:
        return False
    if log is not None:
        log.append('%r: VST2 -> VST3 (%s), settings carried' % (name, r))
    return True


def to_vst2(fx, log=None):
    """Turn a VST3 into its VST2 build in place. True when it crossed."""
    if is_vst2(fx) or not fx.uid:
        return False
    e = entry_for(fx)
    state = fx.component or b''
    if e is None:
        raw = bytes.fromhex(fx.uid) if len(fx.uid or '') == 32 else b''
        if raw[:3] != b'VST' or not state:
            return False
        four = raw[3:7].decode('latin1')
        got = unvstw(state)
        if got:
            kind, data = ('chunk', got['chunk']) if 'chunk' in got else ('params', got['params'])
        else:
            kind, data = 'chunk', state
        set_vst2(fx, four, fx.name, kind, data, fx.is_instrument)
        if log is not None:
            log.append('%r: VST3 -> VST2 by its id (not in the catalog)' % fx.name)
        return True
    r = e['recipe']
    v2 = e['vst2']
    if r == 'same':
        set_vst2(fx, v2['id'], v2['name'], 'chunk', state, fx.is_instrument)
    elif r == 'vstw':
        got = unvstw(state)
        if not got:
            return False
        kind, data = ('chunk', got['chunk']) if 'chunk' in got else ('params', got['params'])
        set_vst2(fx, v2['id'], v2['name'], kind, data, fx.is_instrument)
    elif r == 'params':
        vals = vst3_to_params(e, state)
        if vals is None:
            return False
        set_vst2(fx, v2['id'], v2['name'], 'params', vals, fx.is_instrument)
    elif r == 'ffbs':
        st = ffbs_swap(state, bytes.fromhex(v2['default']))
        if st is None:
            return False
        set_vst2(fx, v2['id'], v2['name'], 'chunk', st, fx.is_instrument)
    elif r == 'prefixed':
        tail = bytes.fromhex(e.get('tail', '')) or (struct.pack('<I', 7) + b'Default')
        set_vst2(fx, v2['id'], v2['name'], 'chunk', struct.pack('<I', len(state)) + state + tail,
                 fx.is_instrument)
    else:
        return False
    if log is not None:
        log.append('%r: VST3 -> VST2 (%s), settings carried' % (fx.name, r))
    return True


def reaper_vst2_state(fx):
    """A VST2's state as REAPER keeps it: the chunk itself, or REAPER's
    parameter dump."""
    kind, data = vst2_parts(fx)
    if kind == 'params':
        return PARAM_DUMP + b''.join(struct.pack('<f', float(v)) for v in data)
    return data


def for_cubase(project, log):
    """Cubase keeps a VST2 the way Steinberg's wrapper does: under the
    'VST' + id + name class id, its state 'VstW' and the bank file (the
    chunk, or the parameters) - read off projects Cubase saved
    (ValhallaRoom: FBCh, a VST2 of parameters: FxBk). A VST2 REAPER or
    Live kept as its chunk or parameter list is written so."""
    n = 0
    for fx in chains(project):
        if getattr(fx, 'native', False) or (getattr(fx, 'format', '') or '') != 'VST':
            continue
        four = four_of(fx)
        if not four:
            continue
        kind, data = vst2_parts(fx)
        if kind == 'chunk' and not data:
            continue
        fxb = fxb_chunk(four, data) if kind == 'chunk' else fxb_params(four, data)
        fx.uid = vst2_pseudo_uid(four, fx.name)
        fx.component = vstw(fxb)
        fx.controller = b''
        fx.param_dump = False
        n += 1
    if n:
        log.append('%d VST2 plug-in(s) written as Cubase keeps a VST2 (its id, the bank in '
                   "Steinberg's wrapper)" % n)
    return n


def chains(project):
    """Every plug-in of the project: instruments, inserts, the master's."""
    for t in getattr(project, 'tracks', []):
        if getattr(t, 'instrument', None) is not None:
            yield t.instrument
        for fx in list(getattr(t, 'fx', [])):
            yield fx
    m = getattr(project, 'master', None)
    if m is not None:
        for fx in list(getattr(m, 'fx', [])):
            yield fx


def apply(project, mode, log):
    """mode 'vst3' / 'vst2': every third-party plug-in in the project that
    has the other build is turned into this one (the catalog's, or one
    whose ids follow the wrapper convention); 'source' leaves them."""
    if mode not in ('vst2', 'vst3'):
        return 0
    n = 0
    for fx in chains(project):
        if getattr(fx, 'native', False) or not getattr(fx, 'uid', None):
            continue
        try:
            done = to_vst3(fx, log) if mode == 'vst3' else to_vst2(fx, log)
        except Exception as ex:          # a state not as the recipe expects
            log.append('%r: kept as it was (%s)' % (fx.name, ex))
            done = False
        n += bool(done)
    return n


# ------------------------------------------------- what this Live loads
def live_plugins():
    """(VST3 class ids, VST2 ids) Live's plug-in database on this machine
    lists as loaded - a plug-in whose scan failed is not among them (Pro-C
    2's VST3 on the development PC: scanstate 3, no entry). None where no
    Live is installed (the browser)."""
    import glob
    import sqlite3
    import shutil
    import tempfile
    d = os.path.expandvars(r'%LOCALAPPDATA%\Ableton\Live Database')
    dbs = sorted(glob.glob(os.path.join(d, 'Live-files-*.db')))
    if not dbs:
        return None
    tmp = tempfile.mkdtemp()
    try:
        for ext in ('', '-wal', '-shm'):
            if os.path.exists(dbs[-1] + ext):
                shutil.copy(dbs[-1] + ext, os.path.join(tmp, 'l.db' + ext))
        c = sqlite3.connect(os.path.join(tmp, 'l.db'))
        rows = c.execute('select dev_identifier from plugins where enabled = 1').fetchall()
        c.close()
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    v3, v2 = set(), set()
    for (ident,) in rows:
        ident = ident or ''
        if ident.startswith('device:vst3:'):
            v3.add(ident.split(':')[-1].split('?')[0].replace('-', '').upper())
        elif ident.startswith('device:vst:'):
            try:
                v2.add(int(ident.split(':')[-1].split('?')[0]) & 0xFFFFFFFF)
            except ValueError:
                pass
    return v3, v2


def fit_to_live(project, log):
    """A plug-in Live on this machine cannot load in the build the project
    names, but can in the other: turned into that build, settings
    carried."""
    got = live_plugins()
    if not got:
        return 0
    v3, v2 = got
    n = 0
    for fx in chains(project):
        if getattr(fx, 'native', False) or not fx.uid:
            continue
        e = entry_for(fx)
        if not e:
            continue
        num = struct.unpack('>I', e['vst2']['id'].encode('latin1'))[0]
        if is_vst2(fx):
            if num not in v2 and e['vst3']['uid'].upper() in v3 and to_vst3(fx):
                n += 1
                log.append('%r: Live here has only its VST3 build - that one, settings carried' % fx.name)
        else:
            if fx.uid.upper() not in v3 and num in v2 and to_vst2(fx):
                n += 1
                log.append('%r: Live here has only its VST2 build - that one, settings carried' % fx.name)
    return n
