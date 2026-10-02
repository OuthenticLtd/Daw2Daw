"""Ask an installed VST3 plug-in what its parameters mean.

    python tools/vst3_values.py "<file>.vst3" <class name> [param title filter] [--state FILE]

For each parameter whose title contains the filter: its id, title, step
count, and the plug-in's own text for every step (a list parameter: the
names behind the numbers a saved state holds) or for a few points of a
continuous one, with the plain value behind each (normalizedParamToPlain).
--state loads a component state first (setComponentState) and prints each
parameter's current value instead - what a stored setting means.

Used to build the converter's built-in effect tables from the plug-in
itself rather than by guessing (Cubase's Frequency band types, ...). It
uses cubaserea.vst3params' COM plumbing; nothing here ships to the browser.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from cubaserea import vst3params as V   # noqa: E402

P, I32, TRESULT = V.P, V.I32, V.TRESULT


class BStream(ctypes.Structure):
    pass


def _string128(buf):
    return V._s128(buf)


def mem_stream(data):
    """A minimal IBStream over bytes (read/seek/tell) for setComponentState."""
    pos = [0]
    READ = ctypes.WINFUNCTYPE(TRESULT, P, P, I32, ctypes.POINTER(I32))
    WRITE = ctypes.WINFUNCTYPE(TRESULT, P, P, I32, ctypes.POINTER(I32))
    SEEK = ctypes.WINFUNCTYPE(TRESULT, P, ctypes.c_int64, I32, ctypes.POINTER(ctypes.c_int64))
    TELL = ctypes.WINFUNCTYPE(TRESULT, P, ctypes.POINTER(ctypes.c_int64))
    QI = ctypes.WINFUNCTYPE(TRESULT, P, P, ctypes.POINTER(P))
    AR = ctypes.WINFUNCTYPE(ctypes.c_uint32, P)

    def qi(this, iid, obj):
        obj[0] = this
        return 0

    def addref(this):
        return 1

    def read(this, buf, n, got):
        k = max(0, min(n, len(data) - pos[0]))
        ctypes.memmove(buf, data[pos[0]:pos[0] + k], k)
        pos[0] += k
        if got:
            got[0] = k
        return 0

    def write(this, buf, n, got):
        return 1

    def seek(this, off, mode, res):
        base = {0: 0, 1: pos[0], 2: len(data)}[mode]
        pos[0] = max(0, min(len(data), base + off))
        if res:
            res[0] = pos[0]
        return 0

    def tell(this, res):
        res[0] = pos[0]
        return 0
    fns = [QI(qi), AR(addref), AR(addref), READ(read), WRITE(write), SEEK(seek), TELL(tell)]
    vt = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    obj = (ctypes.c_void_p * 1)(ctypes.cast(vt, ctypes.c_void_p))
    mem_stream.keep = getattr(mem_stream, 'keep', []) + [fns, vt, obj]
    return ctypes.cast(obj, P)


def main():
    path, want = sys.argv[1], sys.argv[2]
    flt = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith('--') else ''
    state = None
    if '--state' in sys.argv:
        state = open(sys.argv[sys.argv.index('--state') + 1], 'rb').read()
    dll = ctypes.WinDLL(V.module_binary(path))
    init = getattr(dll, 'InitDll', None)
    if init is not None:
        init.restype = ctypes.c_bool
        init()
    gpf = dll.GetPluginFactory
    gpf.restype = P
    factory = gpf()
    count = V._vt(factory, 4, I32)(factory)
    create = V._vt(factory, 6, TRESULT, P, P, P)
    for i in range(count):
        info = V.PClassInfo()
        V._vt(factory, 5, TRESULT, I32, P)(factory, i, ctypes.byref(info))
        name = info.name.decode('latin1')
        if info.category.decode('latin1') != 'Audio Module Class' or name != want:
            continue
        cid = bytes(info.cid)
        comp = P()
        create(factory, ctypes.c_char_p(cid), ctypes.c_char_p(V.IID_ICOMPONENT), ctypes.byref(comp))
        ctx = V.host_context()
        V._vt(comp, 3, TRESULT, P)(comp, ctx)
        ctrl = P()
        q = V._vt(comp, 0, TRESULT, P, P)
        if q(comp, ctypes.c_char_p(V.IID_IEDITCONTROLLER), ctypes.byref(ctrl)) != 0 or not ctrl:
            ccid = ctypes.create_string_buffer(16)
            V._vt(comp, 5, TRESULT, P)(comp, ccid)
            ctrl = P()
            create(factory, ctypes.c_char_p(ccid.raw), ctypes.c_char_p(V.IID_IEDITCONTROLLER),
                   ctypes.byref(ctrl))
            V._vt(ctrl, 3, TRESULT, P)(ctrl, ctx)
        if state is not None:
            V._vt(ctrl, 5, TRESULT, P)(ctrl, mem_stream(state))      # setComponentState
        n = V._vt(ctrl, 8, I32)(ctrl)
        getinfo = V._vt(ctrl, 9, TRESULT, I32, P)
        tostr = V._vt(ctrl, 10, TRESULT, ctypes.c_uint32, ctypes.c_double, P)
        toplain = V._vt(ctrl, 12, ctypes.c_double, ctypes.c_uint32, ctypes.c_double)
        getnorm = V._vt(ctrl, 14, ctypes.c_double, ctypes.c_uint32)
        buf = (ctypes.c_uint16 * 128)()
        for k in range(n):
            pi = V.ParameterInfo()
            if getinfo(ctrl, k, ctypes.byref(pi)) != 0:
                continue
            title = V._s128(pi.title)
            if flt and flt.lower() not in title.lower():
                continue

            def text(v):
                tostr(ctrl, pi.id, v, buf)
                return _string128(buf)
            if state is not None:
                v = getnorm(ctrl, pi.id)
                print('%d\t%s\t%.6f\t%s\t%g' % (pi.id, title, v, text(v), toplain(ctrl, pi.id, v)))
                continue
            steps = int(pi.stepCount)
            if 0 < steps <= 64:
                vals = ['%d=%s' % (s, text(s / steps)) for s in range(steps + 1)]
            else:
                vals = ['%.2f=%s (%g)' % (x, text(x), toplain(ctrl, pi.id, x)) for x in (0, .25, .5, .75, 1)]
            print('%d\t%s\tsteps %d\t%s' % (pi.id, title, steps, ' | '.join(vals)))
        return
    print('no class named %r' % want)


if __name__ == '__main__':
    main()
