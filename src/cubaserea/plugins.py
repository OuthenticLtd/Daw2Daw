"""Plug-in identity, and REAPER's <VST> block encoding.

REAPER stores a plug-in in an RPP as three base64 blobs:

  blob 0  little-endian header
            u32 plug-in numeric id (as listed in REAPER's plug-in cache)
            u32 0xFEED5EEE
            u32 numInputPins,  u64 mask per input pin  (mask = 1 << pin)
            u32 numOutputPins, u64 mask per output pin
            u32 length of blob 1
            u32 1
            u32 0
  blob 1  the plug-in state, as one or more sections:
            u32 length, u32 flag, <length bytes>
          flag 1 = VST3 IComponent state (or a VST2 chunk),
          flag 0 = VST3 IEditController state (length 0 for a VST2).
          Cubase stores exactly those streams as 'audioComponent' and
          'editController', so they move across verbatim.
  blob 2  u8 0, preset name (NUL terminated), u32 0

A base64 line shorter than the wrap width ends a blob.

Cubase identifies a VST2 plug-in with a synthetic 32-hex-digit id built as
ASCII "VST" + the plug-in's four-character VST2 id + its vendor string, so a
VST2 can be recognised and matched to REAPER's numeric id exactly.

A <CLAP> block is a different matter. Cubase loads no CLAP at all, but the
plug-in behind one nearly always ships as a VST3 as well, and REAPER names
the product in the block ("CLAPi: Hive (u-he)", com.u-he.Hive). Matching
that product against the scanned VST list is what lets a CLAP instrument
cross over as the VST3 build of the same synth - see clap_identity and
PluginIndex.match_product - rather than the track being flattened to audio.
"""
import base64
import binascii
import os
import re
import struct

MAGIC = 0xFEED5EEE
WRAP = 128

DEFAULT_VST_DIRS = (r'%COMMONPROGRAMFILES%\VST3',
                    r'%PROGRAMFILES%\Steinberg\VstPlugins',
                    r'%PROGRAMFILES(X86)%\Steinberg\VstPlugins',
                    r'%LOCALAPPDATA%\Programs\Common\VST3',
                    r'%COMMONPROGRAMFILES%\VST2')
DEFAULT_INI = r'%APPDATA%\REAPER\reaper-vstplugins64.ini'


def vst2_id_from_uid(uid):
    """Cubase's VST2 pseudo-uid -> (four character id, int32) or None."""
    try:
        raw = binascii.unhexlify(uid)
    except Exception:
        return None
    if len(raw) != 16 or raw[:3] != b'VST':
        return None
    code = raw[3:7]
    if not all(32 <= c < 127 for c in code):
        return None
    return code.decode('latin1'), struct.unpack('>i', code)[0]


CLAP_TAG = re.compile(r'^CLAP(i?)\s*:\s*', re.I)

# u-he writes its patch as the plug-in's own '.h2p' text, the same bytes in
# every format it is built as, and keeps a second copy of that text as the
# VST3 controller state. Recognising it is what says the controller stream
# can be filled in as well; anything else is given an empty one.
UHE_STATE = re.compile(rb'^#pgm=|\n#AM=')


def norm_product(s):
    """A plug-in's product name, reduced to what two formats agree on."""
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def clap_identity(display, clap_id=''):
    """A <CLAP> block's name and id -> (product, vendor, is_instrument).

    REAPER writes the block as

        <CLAP "CLAPi: Hive (u-he)" com.u-he.Hive ""

    so the display name carries the product and the vendor, the 'i' on the
    tag says it is an instrument, and the reverse-DNS id repeats the vendor.
    The name is the better source - 'com.FabFilter.Pro-Q.4' splits into
    segments no VST list would recognise - so the id is only a fallback."""
    disp = (display or '').strip()
    m = CLAP_TAG.match(disp)
    inst = bool(m and m.group(1))
    while True:                     # "CLAP: CLAP: Pro-Q 4 (FabFilter)"
        m = CLAP_TAG.match(disp)
        if not m:
            break
        disp = disp[m.end():]
    vendor = ''
    m = re.search(r'\(([^()]*)\)\s*$', disp)
    if m:
        vendor = m.group(1).strip()
        disp = disp[:m.start()].strip()
    segs = [x for x in (clap_id or '').split('.') if x]
    if not disp and segs:
        disp = segs[-1]
    if not vendor and len(segs) >= 3:
        vendor = segs[1]
    return disp, vendor, inst


def state_for_vst3(state):
    """(audioComponent, editController) for a state that came from another
    format of the same plug-in.

    A plug-in that serialises itself the same way whatever it is built as
    can be handed its settings straight across: u-he writes the '.h2p'
    preset text and FabFilter an 'FFBS' chunk, in a CLAP exactly as in a
    VST3. The controller stream is a second copy of that text for u-he and
    something else entirely for the rest, so it is only filled in when the
    state is recognised as the text; an empty controller state is what a
    plug-in that keeps none writes anyway, so it is safe to leave."""
    if not state:
        return b'', b''
    if UHE_STATE.search(state[:4096]):
        return state, state
    return state, b''


def wrap_b64(data, width=WRAP):
    s = base64.b64encode(data).decode('ascii')
    lines = [s[i:i + width] for i in range(0, len(s), width)]
    if not lines or len(lines[-1]) == width:
        lines.append('')          # otherwise the blob runs into the next one
    return lines


def unwrap_b64(lines, width=WRAP):
    """Split REAPER's base64 lines back into blobs."""
    blobs = []
    cur = []
    for s in lines:
        cur.append(s)
        if len(s) < width:
            blobs.append(base64.b64decode(''.join(cur) + '=' * (-len(''.join(cur)) % 4)))
            cur = []
    if cur:
        j = ''.join(cur)
        blobs.append(base64.b64decode(j + '=' * (-len(j) % 4)))
    return blobs


def vst_header(num_id, n_in, n_out, state_len):
    b = struct.pack('<II', num_id & 0xFFFFFFFF, MAGIC)
    b += struct.pack('<I', n_in)
    for i in range(n_in):
        b += struct.pack('<Q', 1 << i)
    b += struct.pack('<I', n_out)
    for i in range(n_out):
        b += struct.pack('<Q', 1 << i)
    b += struct.pack('<III', state_len, 1, 0)
    return b


def parse_vst_header(b):
    if len(b) < 16 or struct.unpack_from('<I', b, 4)[0] != MAGIC:
        return None
    num = struct.unpack_from('<I', b, 0)[0]
    o = 8
    n_in, o = struct.unpack_from('<I', b, o)[0], o + 4
    o += 8 * n_in
    n_out, o = struct.unpack_from('<I', b, o)[0], o + 4
    o += 8 * n_out
    return {'num': num, 'n_in': n_in, 'n_out': n_out}


def vst_state(component, controller):
    return (struct.pack('<II', len(component), 1) + component
            + struct.pack('<II', len(controller), 0) + controller)


# REAPER's own parameter dump in place of a VST2's chunk: the two magic
# words, then every parameter as a normalised f32 (see rehost.py)
PARAM_DUMP = b'\xef\xbe\xad\xde\x0d\xf0\xad\xde'


def parse_vst_state(b):
    """-> (component, controller)"""
    comp = ctrl = b''
    o = 0
    while o + 8 <= len(b):
        n, flag = struct.unpack_from('<II', b, o)
        o += 8
        chunk = b[o:o + n]
        o += n
        if flag == 1 and not comp:
            comp = chunk
        elif flag == 0 and not ctrl:
            ctrl = chunk
    return comp, ctrl


def vst_tail(preset_name=''):
    return b'\x00' + preset_name.encode('utf-8') + b'\x00' + b'\x00' * 4


class PluginIndex:
    """REAPER's scanned-plug-in cache."""

    LINE = re.compile(r'^(?P<key>[^=]+)=(?P<stamp>[^,]*),(?P<rest>.*)$')
    COMPAT = re.compile(r'^([0-9A-Fa-f]{32})=([0-9A-Fa-f]{32})\s*-\s*(\S+)')

    def __init__(self, ini_path=None, search_dirs=None):
        ini_path = os.path.expandvars(ini_path or DEFAULT_INI)
        dirs = [os.path.expandvars(d) for d in (search_dirs or DEFAULT_VST_DIRS)]
        self.by_uid = {}
        self.by_num = {}
        self.by_name = {}
        # every build of a product, for matching a plug-in across formats:
        # by_name keeps only the first one seen, which may be the VST2
        self.by_product = {}        # normalised product name -> [entry]
        self.compat = {}            # legacy uid -> vst3 uid
        files = {}
        for root in dirs:
            if os.path.isdir(root):
                for f in os.listdir(root):
                    files[f] = f
        if not os.path.exists(ini_path):
            return
        for line in open(ini_path, encoding='utf-8', errors='replace'):
            line = line.strip()
            cm = self.COMPAT.match(line)
            if cm:
                self.compat[cm.group(1).upper()] = cm.group(2).upper()
                continue
            m = self.LINE.match(line)
            if not m:
                continue
            key, rest = m.group('key'), m.group('rest')
            mm = re.match(r'(\d+)\{([0-9A-Fa-f]{32}),(.*)$', rest)
            if mm:
                num, uid, disp = int(mm.group(1)), mm.group(2).upper(), mm.group(3)
            else:
                m2 = re.match(r'(\d+),(.*)$', rest)
                if not m2:
                    continue
                num, uid, disp = int(m2.group(1)), None, m2.group(2)
            inst = disp.endswith('!!!VSTi')
            disp = disp.replace('!!!VSTi', '')
            e = {'num': num, 'uid': uid, 'disp': disp,
                 'file': self._unescape(key, files), 'inst': inst,
                 'vst3': key.lower().endswith('.vst3')}
            if uid:
                self.by_uid.setdefault(uid, e)
            self.by_num.setdefault(num, e)
            self.by_name.setdefault(disp.split(' (')[0], e)
            self.by_product.setdefault(
                norm_product(disp.split(' (')[0]), []).append(e)

    @staticmethod
    def _unescape(key, files):
        """REAPER replaces awkward characters in the cache key with '_';
        recover the real file name from the plug-in folders."""
        if key in files:
            return key
        pat = re.compile('^' + re.escape(key).replace('_', '.') + '$', re.I)
        for real in files:
            if pat.match(real):
                return real
        return key

    def match_product(self, product, vendor=None, want_instrument=None):
        """The plug-in Cubase can load whose product name is `product`.

        This is how a CLAP crosses over: Hive loaded as a CLAP and Hive
        loaded as a VST3 are the same synth reading the same patch, and only
        the second is one Cubase will load. VST3 wins over VST2, that being
        the build Cubase looks for first, and an instrument is only ever
        matched to an instrument - handing a synth's settings to an effect
        of the same name would load the wrong plug-in outright."""
        cands = list(self.by_product.get(norm_product(product), ()))
        if not cands:
            return None
        if want_instrument is not None:
            same = [e for e in cands
                    if bool(e['inst']) == bool(want_instrument)]
            if not same:
                return None
            cands = same
        if vendor:
            v = norm_product(vendor)
            same = [e for e in cands if v and v in norm_product(e['disp'])]
            if same:
                cands = same
        cands.sort(key=lambda e: (not e['vst3'], e['num']))
        return cands[0]

    def lookup(self, fx):
        """model.Fx -> (entry, state_is_usable).

        The saved state may only be handed to a plug-in we matched exactly:
        a VST2 chunk means nothing to the VST3 build of the same effect."""
        uid = (fx.uid or '').upper()
        e = self.by_uid.get(uid)
        if e is not None:
            return e, True              # exact id match, state is usable
        if not uid and fx.vst2_id is not None:
            e = self.by_num.get(fx.vst2_id)
            if e is not None:
                return e, True          # identified by its VST2 numeric id
        v2 = vst2_id_from_uid(uid)
        if v2:
            e = self.by_num.get(v2[1])
            if e is not None and not e['vst3']:
                return e, True                    # the VST2 binary itself
        # A successor that declares it can stand in for this one (VST3
        # IPluginCompatibility / moduleinfo.json) takes the old one's saved
        # state - that is what the declaration is for, and it is what Cubase
        # does when it opens the project: Sunset Treasures' "Kontakt" (an
        # NI 'hsin' chunk) and Pianoteq 8 (a VstW/CcnK block) open in
        # Kontakt 8 and Pianoteq 9 with their sounds. Handing over nothing
        # left both on their init patch.
        tgt = self.compat.get(uid)
        if tgt and tgt in self.by_uid:
            return self.by_uid[tgt], True
        # Not in this REAPER's list at all: the plug-in Cubase names, as
        # itself, state and all. REAPER finds a VST3 by its class id, so it
        # loads wherever it is installed - converting must not depend on
        # what this machine happens to have (Splice Bridge was dropped).
        # Cubase's own effects are the exception: no other host loads them
        # (cubase_only.py), so they are left out and reported instead
        from .cubase_only import is_cubase_only
        if uid and len(uid) == 32 and not fx.is_vst2 and fx.name \
                and not is_cubase_only(uid):
            return self.synthetic(fx), True
        if (fx.is_vst2 and fx.name and not self.by_uid and uid
                and len(uid) == 32 and vst2_id_from_uid(uid)
                and not is_cubase_only(uid)):
            # No REAPER scan to look in (the browser, a server): Cubase
            # names a VST2 by the class id its VST3 build carries ('VST' +
            # the VST2 id + the name), and that build is the one a REAPER
            # with the plug-in installed finds - on this PC the scan maps
            # ValhallaRoom and Kontakt 8 exactly so. Written as the VST2,
            # REAPER looked for a .dll that is not installed and dropped them.
            return self.synthetic(fx), True
        if fx.is_vst2 and fx.name and not is_cubase_only(uid):
            # a VST2 the same way: REAPER finds it by its numeric id even
            # when the file is called something else (tested with Neoverb
            # under a wrong name, 2026-09-30) - Acho's Addictive Drums 2 and
            # Alexander iPhone's Electric Grand 80 were being dropped
            return {'num': int(fx.vst2_id), 'uid': None, 'disp': fx.name,
                    'file': fx.name + '.dll', 'inst': bool(fx.is_instrument),
                    'vst3': False, 'synthetic': True}, True
        if fx.name:
            e = self.by_name.get(fx.name)
            if e is not None and not uid:
                return e, False
        return None, False

    @staticmethod
    def synthetic(fx):
        """A cache entry for a VST3 this REAPER has not scanned: the class
        id and name Cubase saved, a module named after the plug-in (REAPER
        falls back to the id when the file is called something else) and a
        stable number standing in for the one REAPER's scan would give."""
        import zlib
        uid = fx.uid.upper()
        return {'num': zlib.crc32(uid.encode('ascii')) & 0x7FFFFFFF,
                'uid': uid, 'disp': fx.name, 'file': fx.name + '.vst3',
                'inst': bool(fx.is_instrument), 'vst3': True,
                'synthetic': True}


def vst_block(entry, component, controller, n_in=2, n_out=2, preset='',
              indent='      ', vst2_ident=None):
    """-> the RPP lines for one <VST ...> block (no BYPASS/WAK around it)."""
    state = vst_state(component or b'', controller or b'')
    head = vst_header(entry['num'], n_in, n_out, len(state))
    if entry['vst3']:
        kind = 'VST3i' if entry['inst'] else 'VST3'
    else:
        kind = 'VSTi' if entry['inst'] else 'VST'
    fname = entry['file']
    fq = '"%s"' % fname if (' ' in fname or not fname) else fname
    # REAPER writes a VST3's class id in braces and a VST2's 16-byte
    # identifier in angle brackets; Cubase builds the same VST2 identifier,
    # so it can be handed straight back.
    if entry['uid']:
        uid = '{%s}' % entry['uid']
    elif vst2_ident:
        uid = '<%s>' % vst2_ident.upper()
    else:
        uid = ''
    lines = ['%s<VST "%s: %s" %s 0 "" %d%s ""'
             % (indent, kind, entry['disp'], fq, entry['num'], uid)]
    for blob in (head, state, vst_tail(preset)):
        for ln in wrap_b64(blob):
            lines.append(indent + '  ' + ln)
    lines.append(indent + '>')
    return lines
