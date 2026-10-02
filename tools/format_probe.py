"""Read a plug-in's default saved state from its VST2 and its VST3 build,
with no host: the VST3 module's IComponent::getState into a memory
IBStream, the VST2 DLL's effGetChunk (or its parameters when it keeps no
chunk). Comparing the two is how plugin_formats' recipes were found and
are checked (development only; conversion never loads a plug-in).

    python tools/format_probe.py vst3 <file.vst3> <class-uid>   -> json
    python tools/format_probe.py vst2 <file.dll>                -> json
    python tools/format_probe.py pairs [out.json]   every product this
        PC's REAPER scan lists in both formats, each probed in a subprocess
"""
import ctypes
import json
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))

P = ctypes.c_void_p
FN = ctypes.WINFUNCTYPE
_keep = []


# ------------------------------------------------------------- VST3
def memory_stream():
    """An IBStream over a growing byte buffer: (object pointer, buffer)."""
    from cubaserea import vst3params as v
    buf = bytearray()
    pos = [0]
    IID_IBSTREAM = v.hex_to_tuid('C3BF6EA2309947529B6BF9901EE33E9B')

    def qi(self, iid, obj):
        want = ctypes.string_at(iid, 16)
        if want in (v.IID_FUNKNOWN, IID_IBSTREAM):
            ctypes.cast(obj, ctypes.POINTER(P))[0] = self
            return 0
        ctypes.cast(obj, ctypes.POINTER(P))[0] = None
        return v.E_NOINTERFACE

    def addref(self):
        return 1

    def release(self):
        return 1

    def read(self, b, n, got):
        chunk = bytes(buf[pos[0]:pos[0] + n])
        ctypes.memmove(b, chunk, len(chunk))
        pos[0] += len(chunk)
        if got:
            ctypes.cast(got, ctypes.POINTER(ctypes.c_int32))[0] = len(chunk)
        return 0

    def write(self, b, n, done):
        data = ctypes.string_at(b, n)
        end = pos[0] + n
        if end > len(buf):
            buf.extend(b'\0' * (end - len(buf)))
        buf[pos[0]:end] = data
        pos[0] = end
        if done:
            ctypes.cast(done, ctypes.POINTER(ctypes.c_int32))[0] = n
        return 0

    def seek(self, p, mode, res):
        pos[0] = p if mode == 0 else (pos[0] + p if mode == 1 else len(buf) + p)
        if res:
            ctypes.cast(res, ctypes.POINTER(ctypes.c_int64))[0] = pos[0]
        return 0

    def tell(self, res):
        ctypes.cast(res, ctypes.POINTER(ctypes.c_int64))[0] = pos[0]
        return 0

    T = ctypes.c_int32
    fns = [FN(T, P, P, P)(qi), FN(ctypes.c_uint32, P)(addref), FN(ctypes.c_uint32, P)(release),
           FN(T, P, P, ctypes.c_int32, P)(read), FN(T, P, P, ctypes.c_int32, P)(write),
           FN(T, P, ctypes.c_int64, ctypes.c_int32, P)(seek), FN(T, P, P)(tell)]
    vt = (P * len(fns))(*[ctypes.cast(f, P) for f in fns])
    obj = (P * 1)(ctypes.cast(vt, P))
    _keep.extend([fns, vt, obj])
    return ctypes.cast(obj, P), buf


def vst3_state(path, uid):
    from cubaserea import vst3params as v
    dll = ctypes.WinDLL(v.module_binary(path))
    init = getattr(dll, 'InitDll', None)
    if init is not None:
        init.restype = ctypes.c_bool
        init()
    gpf = dll.GetPluginFactory
    gpf.restype = P
    factory = gpf()
    cid = v.hex_to_tuid(uid)
    comp = P()
    create = v._vt(factory, 6, v.TRESULT, P, P, P)
    if create(factory, ctypes.c_char_p(cid), ctypes.c_char_p(v.IID_ICOMPONENT), ctypes.byref(comp)) != 0:
        raise RuntimeError('no component %s' % uid)
    ctx = v.host_context()
    v._vt(comp, 3, v.TRESULT, P)(comp, ctx)
    # IComponent: 13 setState, 14 getState (after IPluginBase's 3/4 and
    # 5 getControllerClassId, 6 setIoMode, 7 getBusCount, 8 getBusInfo,
    # 9 getRoutingInfo, 10 activateBus, 11 setActive, 12 setState? - the
    # SDK order: ... 11 setActive, 12 setState, 13 getState)
    st, buf = memory_stream()
    r = v._vt(comp, 13, v.TRESULT, P)(comp, st)
    out = {'uid': uid, 'state': bytes(buf).hex(), 'result': r}
    # the controller's own state, when it is a class of its own
    ccid = ctypes.create_string_buffer(16)
    if v._vt(comp, 5, v.TRESULT, P)(comp, ccid) == 0 and ccid.raw.strip(b'\0'):
        ctrl = P()
        if create(factory, ctypes.c_char_p(ccid.raw), ctypes.c_char_p(v.IID_IEDITCONTROLLER),
                  ctypes.byref(ctrl)) == 0 and ctrl:
            v._vt(ctrl, 3, v.TRESULT, P)(ctrl, ctx)
            st2, buf2 = memory_stream()
            v._vt(ctrl, 7, v.TRESULT, P)(ctrl, st2)          # IEditController::getState
            out['controller'] = bytes(buf2).hex()
    return out


# ------------------------------------------------------------- VST2
class AEffect(ctypes.Structure):
    _fields_ = [('magic', ctypes.c_int32), ('dispatcher', P), ('process', P),
                ('setParameter', P), ('getParameter', P), ('numPrograms', ctypes.c_int32),
                ('numParams', ctypes.c_int32), ('numInputs', ctypes.c_int32),
                ('numOutputs', ctypes.c_int32), ('flags', ctypes.c_int32), ('resvd1', P),
                ('resvd2', P), ('initialDelay', ctypes.c_int32), ('realQualities', ctypes.c_int32),
                ('offQualities', ctypes.c_int32), ('ioRatio', ctypes.c_float), ('object', P),
                ('user', P), ('uniqueID', ctypes.c_int32), ('version', ctypes.c_int32),
                ('processReplacing', P), ('processDoubleReplacing', P), ('future', ctypes.c_char * 56)]


MASTER = ctypes.CFUNCTYPE(ctypes.c_ssize_t, P, ctypes.c_int32, ctypes.c_int32, ctypes.c_ssize_t, P,
                          ctypes.c_float)
DISPATCH = ctypes.CFUNCTYPE(ctypes.c_ssize_t, P, ctypes.c_int32, ctypes.c_int32, ctypes.c_ssize_t, P,
                            ctypes.c_float)
GETPARAM = ctypes.CFUNCTYPE(ctypes.c_float, P, ctypes.c_int32)


def _master(effect, opcode, index, value, ptr, opt):
    if opcode == 1:            # audioMasterVersion
        return 2400
    if opcode == 32 and ptr:   # audioMasterGetVendorString
        ctypes.memmove(ptr, b'cubaserea\0', 10)
        return 1
    if opcode == 33 and ptr:   # audioMasterGetProductString
        ctypes.memmove(ptr, b'probe\0', 6)
        return 1
    if opcode == 16:           # sample rate
        return 48000
    if opcode == 17:           # block size
        return 512
    return 0


_MASTER = MASTER(_master)


def vst2_state(path):
    dll = ctypes.CDLL(path)
    main = getattr(dll, 'VSTPluginMain', None) or getattr(dll, 'main', None)
    main.restype = ctypes.POINTER(AEffect)
    main.argtypes = [MASTER]
    eff = main(_MASTER)
    if not eff:
        raise RuntimeError('no AEffect')
    e = eff.contents
    disp = DISPATCH(e.dispatcher)
    disp(eff, 0, 0, 0, None, 0.0)                      # effOpen
    disp(eff, 10, 0, 0, None, 48000.0)                 # effSetSampleRate
    disp(eff, 11, 0, 512, None, 0.0)                   # effSetBlockSize
    out = {'id': struct.pack('>i', e.uniqueID).decode('latin1'), 'id_int': e.uniqueID,
           'flags': e.flags, 'numParams': e.numParams, 'numPrograms': e.numPrograms,
           'version': e.version}
    gp = GETPARAM(e.getParameter)
    out['params'] = [gp(eff, i) for i in range(min(e.numParams, 4096))]
    names = []
    for i in range(min(e.numParams, 4096)):
        b = ctypes.create_string_buffer(256)
        disp(eff, 8, i, 0, b, 0.0)                     # effGetParamName
        names.append(b.value.decode('latin1', 'replace'))
    out['param_names'] = names
    if e.flags & 32:                                   # effFlagsProgramChunks
        for kind, idx in (('bank', 0), ('program', 1)):
            pp = P()
            n = disp(eff, 23, idx, 0, ctypes.byref(pp), 0.0)
            if n > 0 and pp:
                out[kind + '_chunk'] = ctypes.string_at(pp, n).hex()
    return out


# ------------------------------------------------------------- pairs
def pairs():
    from cubaserea.plugins import PluginIndex
    ix = PluginIndex()
    out = []
    for k, es in ix.by_product.items():
        v2 = [e for e in es if not e['vst3']]
        v3 = [e for e in es if e['vst3']]
        if v2 and v3:
            out.append((k, v2[0], v3[0]))
    return out


DIRS2 = [r'C:\Program Files\VSTPlugins', r'C:\Program Files\Steinberg\VSTPlugins',
         r'C:\Program Files\Common Files\VST2', r'C:\Program Files\Common Files\Steinberg\VST2',
         r'C:\Program Files\Vstplugins', r'C:\VSTPlugins', r'C:\Program Files\VST']
DIRS3 = [r'C:\Program Files\Common Files\VST3']


def find_file(name, dirs):
    """A plug-in file by the name REAPER's scan keeps (awkward characters
    as '_'), in these folders and below."""
    import re
    pat = re.compile('^' + re.escape(name).replace('_', '.') + '$', re.I)
    for d in dirs:
        for root, dirs_, files in os.walk(d):
            for f in files + dirs_:
                if f == name or pat.match(f):
                    return os.path.join(root, f)
    return None


def _windows():
    """{hwnd: (pid, title)} of every visible top-level window."""
    import ctypes.wintypes as W
    u = ctypes.windll.user32
    out = {}
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)

    def cb(h, l):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            pid = W.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            out[h] = (pid.value, b.value)
        return True
    u.EnumWindows(PROC(cb), 0)
    return out


def _shot(name):
    gui = os.path.join(SHOTS, 'gui.ps1')
    if os.path.exists(gui):
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        ". '%s'; $script:OX=0; Shot '%s-a' | Out-Null; $script:OX=1920; Shot '%s-b' | Out-Null"
                        % (gui, name, name)], capture_output=True, timeout=30)


SHOTS = os.environ.get('PROBE_SHOTS', HERE)


def watched(cmd, label, timeout=90):
    """Run one probe; any window that appears meanwhile (a plug-in's
    licence or path message) is photographed, closed by WM_CLOSE - never
    clicked - and the probe stopped, reported as 'dialog: <title>'."""
    import time
    before = set(_windows())
    import tempfile
    fo = tempfile.TemporaryFile(mode='w+')
    fe = tempfile.TemporaryFile(mode='w+')
    # files, not pipes: a plug-in that logs a lot fills a pipe and stops
    pr = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, text=True)
    t0 = time.time()
    while pr.poll() is None:
        time.sleep(0.5)
        # only the probe's own windows: anything else on screen is the user's
        new = {h: v for h, v in _windows().items() if h not in before and v[0] == pr.pid}
        if new:
            title = '; '.join(v[1] for v in new.values())
            _shot('dialog-' + label.replace('/', '_').replace(' ', '_'))
            for h in new:
                ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)
            pr.kill()
            time.sleep(1)
            for h, v in _windows().items():
                if h in new or (h not in before and v[1] in [x[1] for x in new.values()]):
                    # the same message left behind by the killed process
                    ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)
            return {'error': 'dialog: ' + title}
        if time.time() - t0 > timeout:
            pr.kill()
            fo.seek(0)
            out = fo.read()
            if out.strip().startswith('{'):
                return json.loads(out)          # done, stuck only in its teardown
            return {'error': 'timeout'}
    fo.seek(0)
    fe.seek(0)
    out = fo.read()
    return json.loads(out) if out.strip().startswith('{') else {'error': (fe.read() or out)[-300:]}


def run_pairs(out_path):
    res = {}
    if os.path.exists(out_path):
        res = json.load(open(out_path))
    for k, a, b in sorted(pairs(), key=lambda t: (bool(t[1]['inst']), t[0])):
        if k in res or 'pianoteq' in k or 'addictivedrums' in k:
            continue
        p2 = find_file(a['file'], DIRS2)
        p3 = find_file(b['file'], DIRS3)
        rec = {'vst2_file': p2, 'vst3_file': p3, 'uid': b['uid'], 'num': a['num'], 'disp': a['disp']}
        for kind, args in (('vst2', [p2]), ('vst3', [p3, b['uid']])):
            if not args[0]:
                rec[kind] = {'error': 'file not found'}
                continue
            rec[kind] = watched([sys.executable, __file__, kind] + args, k + '-' + kind)
        res[k] = rec
        json.dump(res, open(out_path, 'w'), indent=0)
        print(k, 'vst2' if 'error' not in rec['vst2'] else rec['vst2']['error'][:40],
              'vst3' if 'error' not in rec['vst3'] else rec['vst3']['error'][:40], flush=True)


def pumped(fn, *a):
    """fn(*a) on a worker thread while this thread dispatches window
    messages - some plug-ins wait on their own message-only windows."""
    import threading
    import ctypes.wintypes as W
    box = {}

    def work():
        try:
            box['r'] = fn(*a)
        except Exception as e:
            box['e'] = e
    th = threading.Thread(target=work, daemon=True)
    th.start()
    u = ctypes.windll.user32
    msg = W.MSG()
    while th.is_alive():
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        th.join(0.01)
    if 'e' in box:
        raise box['e']
    return box['r']


if __name__ == '__main__':
    if sys.argv[1] == 'vst3':
        print(json.dumps(pumped(vst3_state, sys.argv[2], sys.argv[3])), flush=True)
        os._exit(0)                 # a plug-in's teardown may hang or crash
    elif sys.argv[1] == 'vst2':
        print(json.dumps(pumped(vst2_state, sys.argv[2])), flush=True)
        os._exit(0)
    elif sys.argv[1] == 'pairs':
        run_pairs(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, 'format_probe.json'))


def _connect(v, comp, ctrl):
    """Join the component and its controller (IConnectionPoint), as a host
    does: JUCE-built plug-ins fill their parameter list only then."""
    if not ctrl or ctrl.value == comp.value or os.environ.get('PROBE_NOCONNECT'):
        return
    cp1, cp2 = P(), P()
    v._vt(comp, 0, v.TRESULT, P, P)(comp, ctypes.c_char_p(v.IID_ICONNECTIONPOINT), ctypes.byref(cp1))
    v._vt(ctrl, 0, v.TRESULT, P, P)(ctrl, ctypes.c_char_p(v.IID_ICONNECTIONPOINT), ctypes.byref(cp2))
    if cp1 and cp2:
        v._vt(cp1, 3, v.TRESULT, P)(cp1, cp2)
        v._vt(cp2, 3, v.TRESULT, P)(cp2, cp1)


def vst3_curves(path, uid, grid=33):
    """The controller's normalised -> plain curve for each parameter, at
    `grid` points, and its ids/titles, plus the default state's normalised
    values once handed to the controller (setComponentState)."""
    from cubaserea import vst3params as v
    dll = ctypes.WinDLL(v.module_binary(path))
    init = getattr(dll, 'InitDll', None)
    if init is not None:
        init.restype = ctypes.c_bool
        init()
    gpf = dll.GetPluginFactory
    gpf.restype = P
    factory = gpf()
    create = v._vt(factory, 6, v.TRESULT, P, P, P)
    comp = P()
    create(factory, ctypes.c_char_p(v.hex_to_tuid(uid)), ctypes.c_char_p(v.IID_ICOMPONENT), ctypes.byref(comp))
    ctx = v.host_context()
    v._vt(comp, 3, v.TRESULT, P)(comp, ctx)
    ctrl = P()
    q = v._vt(comp, 0, v.TRESULT, P, P)
    if q(comp, ctypes.c_char_p(v.IID_IEDITCONTROLLER), ctypes.byref(ctrl)) != 0 or not ctrl:
        ccid = ctypes.create_string_buffer(16)
        v._vt(comp, 5, v.TRESULT, P)(comp, ccid)
        ctrl = P()
        create(factory, ctypes.c_char_p(ccid.raw), ctypes.c_char_p(v.IID_IEDITCONTROLLER), ctypes.byref(ctrl))
        v._vt(ctrl, 3, v.TRESULT, P)(ctrl, ctx)
    _connect(v, comp, ctrl)
    st, buf = memory_stream()
    v._vt(comp, 13, v.TRESULT, P)(comp, st)
    st2, buf2 = memory_stream()
    buf2.extend(buf)
    v._vt(ctrl, 5, v.TRESULT, P)(ctrl, st2)                  # setComponentState
    n = v._vt(ctrl, 8, ctypes.c_int32)(ctrl)
    getinfo = v._vt(ctrl, 9, v.TRESULT, ctypes.c_int32, P)
    n2p = v._vt(ctrl, 12, ctypes.c_double, ctypes.c_uint32, ctypes.c_double)
    getn = v._vt(ctrl, 14, ctypes.c_double, ctypes.c_uint32)
    out = []
    for k in range(n):
        pi = v.ParameterInfo()
        if getinfo(ctrl, k, ctypes.byref(pi)) != 0:
            continue
        pid = int(pi.id)
        out.append({'id': pid, 'title': v._s128(pi.title), 'steps': int(pi.stepCount), 'flags': int(pi.flags),
                    'norm': getn(ctrl, pid),
                    'curve': [n2p(ctrl, pid, i / (grid - 1)) for i in range(grid)]})
    return {'uid': uid, 'state': bytes(buf).hex(), 'params': out}


if __name__ == '__main__' and sys.argv[1] == 'curves':
    print(json.dumps(pumped(vst3_curves, sys.argv[2], sys.argv[3])), flush=True)
    os._exit(0)


def vst3_norms(path, uid, state_file):
    """Hand the component this state (setState), then the controller
    (setComponentState): the normalised value of every parameter."""
    from cubaserea import vst3params as v
    state = open(state_file, 'rb').read()
    dll = ctypes.WinDLL(v.module_binary(path))
    init = getattr(dll, 'InitDll', None)
    if init is not None:
        init.restype = ctypes.c_bool
        init()
    gpf = dll.GetPluginFactory
    gpf.restype = P
    factory = gpf()
    create = v._vt(factory, 6, v.TRESULT, P, P, P)
    comp = P()
    create(factory, ctypes.c_char_p(v.hex_to_tuid(uid)), ctypes.c_char_p(v.IID_ICOMPONENT), ctypes.byref(comp))
    ctx = v.host_context()
    v._vt(comp, 3, v.TRESULT, P)(comp, ctx)
    st, buf = memory_stream()
    buf.extend(state)
    r1 = v._vt(comp, 12, v.TRESULT, P)(comp, st)              # setState
    ctrl = P()
    q = v._vt(comp, 0, v.TRESULT, P, P)
    if q(comp, ctypes.c_char_p(v.IID_IEDITCONTROLLER), ctypes.byref(ctrl)) != 0 or not ctrl:
        ccid = ctypes.create_string_buffer(16)
        v._vt(comp, 5, v.TRESULT, P)(comp, ccid)
        ctrl = P()
        create(factory, ctypes.c_char_p(ccid.raw), ctypes.c_char_p(v.IID_IEDITCONTROLLER), ctypes.byref(ctrl))
        v._vt(ctrl, 3, v.TRESULT, P)(ctrl, ctx)
    _connect(v, comp, ctrl)
    # what the component now holds, back out (getState), to the controller
    st2, buf2 = memory_stream()
    v._vt(comp, 13, v.TRESULT, P)(comp, st2)
    st3, buf3 = memory_stream()
    buf3.extend(buf2)
    r2 = v._vt(ctrl, 5, v.TRESULT, P)(ctrl, st3)
    n = v._vt(ctrl, 8, ctypes.c_int32)(ctrl)
    getinfo = v._vt(ctrl, 9, v.TRESULT, ctypes.c_int32, P)
    getn = v._vt(ctrl, 14, ctypes.c_double, ctypes.c_uint32)
    out = []
    for k in range(n):
        pi = v.ParameterInfo()
        if getinfo(ctrl, k, ctypes.byref(pi)) == 0:
            out.append(getn(ctrl, int(pi.id)))
    return {'setState': r1, 'setComponentState': r2, 'norms': out, 'state_back': bytes(buf2).hex()}


if __name__ == '__main__' and sys.argv[1] == 'norms':
    print(json.dumps(pumped(vst3_norms, sys.argv[2], sys.argv[3], sys.argv[4])), flush=True)
    os._exit(0)


def vst2_set(path, state_file):
    """effSetChunk(bank) with this state, then every parameter's value."""
    data = open(state_file, 'rb').read()
    dll = ctypes.CDLL(path)
    main = getattr(dll, 'VSTPluginMain', None) or getattr(dll, 'main', None)
    main.restype = ctypes.POINTER(AEffect)
    main.argtypes = [MASTER]
    eff = main(_MASTER)
    e = eff.contents
    disp = DISPATCH(e.dispatcher)
    disp(eff, 0, 0, 0, None, 0.0)
    buf = ctypes.create_string_buffer(data, len(data))
    r = disp(eff, 24, 0, len(data), buf, 0.0)             # effSetChunk
    gp = GETPARAM(e.getParameter)
    return {'setChunk': r, 'params': [gp(eff, i) for i in range(e.numParams)]}


if __name__ == '__main__' and sys.argv[1] == 'vst2set':
    print(json.dumps(pumped(vst2_set, sys.argv[2], sys.argv[3])), flush=True)
    os._exit(0)


SETPARAM = ctypes.CFUNCTYPE(None, P, ctypes.c_int32, ctypes.c_float)


def vst2_mod(path, edits_json, state_file=''):
    """Load the VST2 (with this chunk when given), set some parameters
    ([[index, value], ...]), then report every parameter and the chunk."""
    edits = json.loads(edits_json)
    dll = ctypes.CDLL(path)
    main = getattr(dll, 'VSTPluginMain', None) or getattr(dll, 'main', None)
    main.restype = ctypes.POINTER(AEffect)
    main.argtypes = [MASTER]
    eff = main(_MASTER)
    e = eff.contents
    disp = DISPATCH(e.dispatcher)
    disp(eff, 0, 0, 0, None, 0.0)
    if state_file:
        data = open(state_file, 'rb').read()
        buf = ctypes.create_string_buffer(data, len(data))
        disp(eff, 24, 0, len(data), buf, 0.0)
    sp = SETPARAM(e.setParameter)
    for i, v in edits:
        sp(eff, int(i), float(v))
    gp = GETPARAM(e.getParameter)
    out = {'params': [gp(eff, i) for i in range(e.numParams)]}
    pp = P()
    n = disp(eff, 23, 0, 0, ctypes.byref(pp), 0.0)
    if n > 0 and pp:
        out['chunk'] = ctypes.string_at(pp, n).hex()
    return out


if __name__ == '__main__' and sys.argv[1] == 'vst2mod':
    print(json.dumps(pumped(vst2_mod, sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else '')),
          flush=True)
    os._exit(0)
