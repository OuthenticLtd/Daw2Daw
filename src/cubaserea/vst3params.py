"""The parameter table of a VST3 plug-in, read from the plug-in itself.

REAPER keeps a plug-in parameter envelope by the parameter's position in
the plug-in's list (`<PARMENV 2 ...>`), Cubase by the parameter's VST3 id
(`Inserts\\Slot\\<uid>-2`). The two agree only for plug-ins that number
their parameters 0..n in list order (Valhalla), not in general (Pro-Q 4's
Output Level is id 2 and sits second in the list). The table that maps
one onto the other lives in the plug-in: this module loads the plug-in
module through the VST3 COM-style ABI (ctypes, Windows x64), creates its
edit controller and reads IEditController::getParameterInfo for every
index. No host is involved - the plug-in binary has to be installed, which
it has to be for either DAW to play it anyway.

A plug-in may misbehave when initialised without a host context, so the
enumeration runs in a subprocess, and the result is cached per class id in
templates/vst3-params.json.

    python -m cubaserea.vst3params "<file>.vst3" [class-id-hex]
"""
import ctypes
import glob
import json
import os
import struct
import subprocess
import sys

VST3_DIRS = [r'C:\Program Files\Common Files\VST3',
             os.path.expandvars(r'%LOCALAPPDATA%\Programs\Common\VST3')]
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'templates', 'vst3-params.json')


# ---------------------------------------------------------------- ids
def tuid_to_hex(raw):
    """A TUID as Windows lays it out -> the 32-hex form Cubase and the
    VST3 SDK print (FUID::toString: l1 l2 l3 l4)."""
    b = bytes(raw)
    l1 = struct.unpack('<I', b[0:4])[0]
    l2 = (b[4] << 16) | (b[5] << 24) | b[6] | (b[7] << 8)
    l3 = (b[8] << 24) | (b[9] << 16) | (b[10] << 8) | b[11]
    l4 = (b[12] << 24) | (b[13] << 16) | (b[14] << 8) | b[15]
    return '%08X%08X%08X%08X' % (l1, l2, l3, l4)


def hex_to_tuid(h):
    l1, l2, l3, l4 = (int(h[i:i + 8], 16) for i in (0, 8, 16, 24))
    b = bytearray(16)
    b[0:4] = struct.pack('<I', l1)
    b[4] = (l2 >> 16) & 0xff
    b[5] = (l2 >> 24) & 0xff
    b[6] = l2 & 0xff
    b[7] = (l2 >> 8) & 0xff
    b[8:12] = struct.pack('>I', l3)
    b[12:16] = struct.pack('>I', l4)
    return bytes(b)


IID_IPLUGINFACTORY = hex_to_tuid('7A4D811C52114A1FAED9D2EE0B43BF9F')
IID_ICOMPONENT = hex_to_tuid('E831FF31F2D54301928EBBEE25697802')
IID_IEDITCONTROLLER = hex_to_tuid('DCD7BBE37742448DA874AACC979C759E')
IID_ICONNECTIONPOINT = hex_to_tuid('70A4156F6E6E4026989148BFAA60D8D1')

# ---------------------------------------------------------- vtables
# every VST3 interface is a COM-style vtable of __stdcall (x64: the one
# calling convention) methods; only the slots used here are declared
P = ctypes.c_void_p
I32 = ctypes.c_int32
TRESULT = ctypes.c_int32
FN = ctypes.WINFUNCTYPE if os.name == 'nt' else ctypes.CFUNCTYPE

# FUnknown: 0 queryInterface, 1 addRef, 2 release
# IPluginFactory: 3 getFactoryInfo, 4 countClasses, 5 getClassInfo,
#                 6 createInstance
# IPluginBase (after FUnknown): 3 initialize, 4 terminate
# IComponent: 5 getControllerClassId, ...
# IEditController: 5 setComponentState, 6 setState, 7 getState,
#                  8 getParameterCount, 9 getParameterInfo, ...


def _vt(obj, slot, restype, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(P))[0]
    fn = ctypes.cast(vtable, ctypes.POINTER(P))[slot]
    return FN(restype, P, *argtypes)(fn)


class PClassInfo(ctypes.Structure):
    _fields_ = [('cid', ctypes.c_ubyte * 16), ('cardinality', I32),
                ('category', ctypes.c_char * 32), ('name', ctypes.c_char * 64)]


class ParameterInfo(ctypes.Structure):
    _fields_ = [('id', ctypes.c_uint32), ('title', ctypes.c_uint16 * 128),
                ('shortTitle', ctypes.c_uint16 * 128), ('units', ctypes.c_uint16 * 128),
                ('stepCount', I32), ('defaultNormalizedValue', ctypes.c_double),
                ('unitId', I32), ('flags', I32)]


def _s128(arr):
    out = []
    for c in arr:
        if c == 0:
            break
        out.append(chr(c))
    return ''.join(out)


# ------------------------------------------------------- host context
# A minimal IHostApplication for plug-ins that want one at initialize()
# (JUCE-built plug-ins report no parameters without it): it names itself
# and declines to create anything.
IID_FUNKNOWN = hex_to_tuid('0000000000000000C0000000' '00000046')
IID_IHOSTAPPLICATION = hex_to_tuid('58E79DE188E44A168CE7A48E3E1D5DF3')
E_NOINTERFACE = -2147467262
_keep = []


def host_context():
    def qi(self, iid, obj):
        want = ctypes.string_at(iid, 16)
        if want in (IID_FUNKNOWN, IID_IHOSTAPPLICATION):
            ctypes.cast(obj, ctypes.POINTER(P))[0] = self
            return 0
        ctypes.cast(obj, ctypes.POINTER(P))[0] = None
        return E_NOINTERFACE

    def addref(self):
        return 1

    def release(self):
        return 1

    def getname(self, name):
        buf = ctypes.cast(name, ctypes.POINTER(ctypes.c_uint16))
        text = 'cubaserea'
        for i, ch in enumerate(text):
            buf[i] = ord(ch)
        buf[len(text)] = 0
        return 0

    def createinstance(self, cid, iid, obj):
        ctypes.cast(obj, ctypes.POINTER(P))[0] = None
        return -1                                  # kNotImplemented

    fns = [FN(TRESULT, P, P, P)(qi), FN(ctypes.c_uint32, P)(addref),
           FN(ctypes.c_uint32, P)(release), FN(TRESULT, P, P)(getname),
           FN(TRESULT, P, P, P, P)(createinstance)]
    vtable = (P * len(fns))(*[ctypes.cast(f, P) for f in fns])
    obj = (P * 1)(ctypes.cast(vtable, P))
    _keep.extend([fns, vtable, obj])
    return ctypes.cast(obj, P)


def module_binary(path):
    """The DLL inside a .vst3 bundle folder, or the file itself."""
    if os.path.isdir(path):
        cands = glob.glob(os.path.join(path, 'Contents', 'x86_64-win', '*.vst3'))
        if cands:
            return cands[0]
    return path


def enumerate_module(path):
    """[(class id hex, name, category, [(param id, title), ...])] for
    every audio-module class in the plug-in file."""
    dll = ctypes.WinDLL(module_binary(path))
    init = getattr(dll, 'InitDll', None)
    if init is not None:
        init.restype = ctypes.c_bool
        init()
    gpf = dll.GetPluginFactory
    gpf.restype = P
    factory = gpf()
    if not factory:
        raise RuntimeError('no plug-in factory in %s' % path)
    count = _vt(factory, 4, I32)(factory)
    classes = []
    for i in range(count):
        info = PClassInfo()
        _vt(factory, 5, TRESULT, I32, P)(factory, i, ctypes.byref(info))
        classes.append((bytes(info.cid), info.category.decode('latin1'),
                        info.name.decode('latin1')))
    out = []
    create = _vt(factory, 6, TRESULT, P, P, P)
    for cid, cat, name in classes:
        if cat != 'Audio Module Class':
            continue
        comp = P()
        r = create(factory, ctypes.c_char_p(cid), ctypes.c_char_p(IID_ICOMPONENT),
                   ctypes.byref(comp))
        if r != 0 or not comp:
            continue
        ctx = host_context()
        _vt(comp, 3, TRESULT, P)(comp, ctx)              # initialize(host)
        ctrl = P()
        # the controller is the component itself (single-component plug-in)
        # or a class of its own that the component names
        q = _vt(comp, 0, TRESULT, P, P)
        if q(comp, ctypes.c_char_p(IID_IEDITCONTROLLER), ctypes.byref(ctrl)) != 0 or not ctrl:
            ccid = ctypes.create_string_buffer(16)
            if _vt(comp, 5, TRESULT, P)(comp, ccid) == 0:
                ctrl = P()
                if create(factory, ctypes.c_char_p(ccid.raw),
                          ctypes.c_char_p(IID_IEDITCONTROLLER), ctypes.byref(ctrl)) == 0 and ctrl:
                    _vt(ctrl, 3, TRESULT, P)(ctrl, ctx)   # initialize(host)
        params = []
        if ctrl and ctrl.value != comp.value:
            # a JUCE-built plug-in fills its controller's parameter list
            # only once the component is connected to it (IConnectionPoint)
            cp1, cp2 = P(), P()
            q(comp, ctypes.c_char_p(IID_ICONNECTIONPOINT), ctypes.byref(cp1))
            _vt(ctrl, 0, TRESULT, P, P)(ctrl, ctypes.c_char_p(IID_ICONNECTIONPOINT),
                                        ctypes.byref(cp2))
            if cp1 and cp2:
                _vt(cp1, 3, TRESULT, P)(cp1, cp2)
                _vt(cp2, 3, TRESULT, P)(cp2, cp1)
        if ctrl:
            n = _vt(ctrl, 8, I32)(ctrl)
            getinfo = _vt(ctrl, 9, TRESULT, I32, P)
            for k in range(n):
                pi = ParameterInfo()
                if getinfo(ctrl, k, ctypes.byref(pi)) == 0:
                    params.append((int(pi.id), _s128(pi.title), int(pi.flags), int(pi.unitId), int(pi.stepCount), _s128(pi.units)))
            _vt(ctrl, 4, TRESULT)(ctrl)                   # terminate
        _vt(comp, 4, TRESULT)(comp)                        # terminate
        out.append((tuid_to_hex(cid), name, cat, params))
    return out


# ------------------------------------------------------------- cache
def load_cache():
    try:
        with open(CACHE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w', encoding='utf-8') as f:
        json.dump(c, f, indent=1)


# plug-ins that must not be loaded outside a real host: Pianoteq's copy
# protection raised a system "Fatal error [1322] corrupted ..." box when its
# module was loaded from here (2026-09-28) and hung; it is printed anyway
NEVER_LOAD = ('pianoteq',)


def find_module(uid=None, name=None):
    """The .vst3 file for a class id or a name, from the usual folders."""
    uid = (uid or '').upper()
    for d in VST3_DIRS:
        for f in sorted(glob.glob(os.path.join(d, '*.vst3'))):
            base = os.path.splitext(os.path.basename(f))[0].lower()
            if any(k in base for k in NEVER_LOAD):
                continue
            if name and (name.lower() == base or name.lower() in base
                         or base in name.lower()):
                return f
    return None


def params_for(uid, name=None, log=None):
    """[(id, title), ...] in the plug-in's list order for the class `uid`
    (32 hex), or None. Read once from the plug-in in a subprocess, then
    from the cache."""
    uid = (uid or '').upper()
    cache = load_cache()
    if uid in cache:
        return [(p[0], p[1]) for p in cache[uid]['params']]
    if any(k in (name or '').lower() for k in NEVER_LOAD):
        return None
    # No plug-in is loaded to convert a project (the user, 2026-09-30: "I
    # don't want you to do processing. It's just conversion"): only the
    # table shipped in templates/vst3-params.json is used. CPR_READ_PARAMS=1
    # reads a missing plug-in's table from its binary, for building that
    # table on a development machine.
    if not os.environ.get('CPR_READ_PARAMS'):
        if log is not None:
            log.append('%s is not in the shipped parameter table; its automation '
                       'lanes assume the parameter ids equal REAPER\'s indices '
                       '(true of most plug-ins, not of all)' % (name or uid))
        return None
    mod = find_module(uid, name)
    if mod is None:
        if log is not None:
            log.append('no VST3 file found for %s (%s); parameter ids assumed '
                       'to equal REAPER\'s indices' % (name or '?', uid))
        return None
    try:
        r = subprocess.run([sys.executable, '-m', 'cubaserea.vst3params', mod],
                           capture_output=True, text=True, timeout=120,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        data = json.loads(r.stdout or '[]')
    except Exception as e:
        if log is not None:
            log.append('%s: its parameters could not be read from %s (%s)'
                       % (name or uid, mod, e))
        return None
    for cls in data:
        cache[cls['uid']] = cls
    save_cache(cache)
    if uid in cache:
        return [(p[0], p[1]) for p in cache[uid]['params']]
    if log is not None:
        log.append('%s: class %s is not in %s' % (name or '?', uid, mod))
    return None


KIS_BYPASS = 0x10000
CAN_AUTOMATE = 0x1
TAG_BASE = 0x1069


def cubase_order(params):
    """The positions Cubase numbers a plug-in's automatable parameters
    by, as REAPER indices in that order: the bypass parameter first, then
    the root unit's automatable parameters in the plug-in's order, then
    each further unit's (ascending unit id). Read off Cubase 15 numbering
    Pro-Q 4's Output Level (index 556, id 556, second automatable
    parameter of the root unit) as 2 - hypothesis confirmed only for the
    root unit so far."""
    idx = list(range(len(params)))
    bypass = [i for i in idx if (params[i][2] & KIS_BYPASS)]
    rest = [i for i in idx if i not in bypass and (params[i][2] & CAN_AUTOMATE)]
    units = sorted(set(params[i][3] if len(params[i]) > 3 else 0 for i in rest),
                   key=lambda u: (u != 0, u))
    order = list(bypass)
    for u in units:
        order.extend(i for i in rest if (params[i][3] if len(params[i]) > 3 else 0) == u)
    return order


def table_for(uid, name=None, log=None):
    """The plug-in's full parameter rows [id, title, flags, unitId] in
    REAPER's (the plug-in's) order, or None."""
    uid = (uid or '').upper()
    cache = load_cache()
    if uid not in cache:
        params_for(uid, name, log)
        cache = load_cache()
    if uid not in cache:
        return None
    return [list(p) + [0] * (6 - len(p)) for p in cache[uid]['params']]


def reaper_index_to_cubase(uid, index, name=None, log=None):
    """(Cubase's lane number, lane tag) for REAPER's parameter index, or
    (index, TAG_BASE + index) when the plug-in cannot be read."""
    t = table_for(uid, name, log)
    if t is None:
        return index, TAG_BASE + index
    order = cubase_order(t)
    if index in order:
        return order.index(index), TAG_BASE + index
    if log is not None:
        log.append('%s: parameter %d is not one Cubase automates; its lane '
                   'may not bind' % (name or uid, index))
    return index, TAG_BASE + index


def cubase_to_reaper_index(uid, number, name=None, log=None):
    """REAPER's parameter index for Cubase's lane number, or the number
    itself when the plug-in cannot be read."""
    t = table_for(uid, name, log)
    if t is None:
        return number
    order = cubase_order(t)
    if 0 <= number < len(order):
        return order[number]
    return number


def index_to_id(uid, index, name=None, log=None):
    t = params_for(uid, name, log)
    if t is None or index >= len(t):
        return index
    return t[index][0]


def id_to_index(uid, pid, name=None, log=None):
    t = params_for(uid, name, log)
    if t is None:
        return pid
    for i, (k, _title) in enumerate(t):
        if k == pid:
            return i
    return pid



KIS_LIST = 0x8


def type_word(uid, index, name=None, log=None):
    """The word after a lane's tag, as Cubase 15 wrote it (2026-09-29):
    5 on a bypass parameter; 4 on a parameter that sits in a unit other
    than the root or carries the kIsList flag (Pro-Q 4's Band 1 Frequency
    and Output Level, Hive's Output); 0 otherwise (ValhallaDelay's Mix)."""
    t = table_for(uid, name, log)
    if t is None or index >= len(t):
        return 0
    row = t[index]
    flags, unit = row[2], row[3]
    if flags & KIS_BYPASS:
        return 5
    if (flags & KIS_LIST) or unit != 0:
        return 4
    return 0


if __name__ == '__main__':
    path = sys.argv[1]
    res = enumerate_module(path)
    print(json.dumps([{'uid': u, 'name': n, 'category': c, 'file': path,
                       'params': [list(row) for row in ps]}
                      for u, n, c, ps in res]))
