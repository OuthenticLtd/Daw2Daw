"""Re-host the VST2 plug-ins whose settings REAPER saved as a parameter dump.

REAPER saves a VST2's state as the plug-in's own chunk when the plug-in
offers one. When it does not - or when REAPER was set to save the plug-in
by its parameters - the <VST block holds REAPER's own parameter dump
instead: the magic DEADBEEF DEADF00D followed by every parameter as a
normalised float. Nothing outside REAPER can read that, so the Cubase
project used to get such a plug-in on its defaults. That is not a small
difference: the master's Pro-L 2 was lifting the mix by 16 dB in REAPER
and by nothing in Cubase, and the whole Cubase master sat 15 dB down.

REAPER can read it, and REAPER is on this machine. So each such plug-in is
loaded again in a REAPER of its own (a new instance, a new project) from a
copy of its block, the VST3 build of the same plug-in is added beside it,
every parameter is copied across by name (by index where a name is missing
and both builds have the same number of parameters), the VST2 is removed
and the project saved. The saved project holds the VST3 with its real
state, in the form the reader already understands, and that replaces the
dump. Results are cached by the block's contents, so converting the same
project again needs no REAPER.

Copying parameters is what carries the settings, so this relies on the VST3
build exposing the same parameters as the VST2 - true of vendors who build
both from one code base (FabFilter, u-he, Native Instruments...). The
export comparison is the check. CPR_NO_REHOST=1 turns it off; the cache
lives in %LOCALAPPDATA%\\cubaserea.
"""
import hashlib
import json
import os
import re
import subprocess
import time

from . import media

TIMEOUT = float(os.environ.get('CPR_REHOST_TIMEOUT', '300'))


def cache_dir():
    root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    d = os.path.join(root, 'cubaserea')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _cache_load():
    try:
        with open(os.path.join(cache_dir(), 'rehost_cache.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_save(c):
    try:
        with open(os.path.join(cache_dir(), 'rehost_cache.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(c, f)
    except OSError:
        pass


def block_text(fx):
    """The plug-in's <VST ...> block (with its BYPASS/PRESETNAME lines) as
    REAPER wrote it."""
    from .print_tracks import _emit
    out = []
    for e in (fx.raw_group or []):
        out += _emit(e, '')
    return '\n'.join(out)


def ident_of(fx):
    """'VST: FabFilter Pro-L 2 (FabFilter)' -> ('FabFilter Pro-L 2', 'FabFilter')"""
    for e in (fx.raw_group or []):
        if not isinstance(e, str) and getattr(e, 'name', '') in ('VST', 'VST3'):
            ident = e.args[0] if e.args else ''
            name = ident.split(': ', 1)[-1]
            vendor = ''
            m = re.search(r'\(([^()]*)\)\s*$', name)
            if m:
                vendor = m.group(1).strip()
                name = name[:m.start()].strip()
            return name, vendor
    return fx.name, ''


def vst3_for(fx, index):
    """The VST3 build of this VST2, from REAPER's plug-in cache, or None."""
    name, vendor = ident_of(fx)
    cands = [name]
    if vendor and name.lower().startswith(vendor.lower()):
        cands.append(name[len(vendor):].strip(' -:'))
    if vendor and name.lower().endswith(vendor.lower()):
        cands.append(name[:-len(vendor)].strip(' -:'))
    for c in cands:
        e = index.match_product(c, vendor or None, want_instrument=fx.is_instrument)
        if e and e.get('vst3'):
            return e
    return None


def wants_rehost(fx):
    """A parameter dump always; any other VST2 too, unless CPR_KEEP_VST2 is
    set. Cubase 15 is not known to load VST2 at all, and a VST2's chunk
    cannot be handed to the VST3 build of the same plug-in, so the VST3 with
    the parameters copied across is the build that plays."""
    if not fx.raw_group or getattr(fx, 'native', False):
        return False
    if getattr(fx, 'param_dump', False):
        return True
    return bool(fx.is_vst2 or (fx.uid and fx.uid.startswith('56535446'))) \
        and not os.environ.get('CPR_KEEP_VST2')


def candidates(p):
    """(where, fx) for every plug-in to re-host."""
    out = []
    for t in p.tracks:
        chain = ([t.instrument] if getattr(t, 'instrument', None) is not None else []) + list(t.fx)
        for fx in chain:
            if wants_rehost(fx):
                out.append((t.name, fx))
    if p.master is not None:
        for fx in p.master.fx:
            if wants_rehost(fx):
                out.append(('the master', fx))
    return out


LUA = r'''
local LOG = "%(log)s"
local function log(s)
  local f = io.open(LOG, "a")
  if f then f:write(s, "\n"); f:close() end
end
local function copy_params(tr, src, dst)
  local n2 = reaper.TrackFX_GetNumParams(tr, src)
  local n3 = reaper.TrackFX_GetNumParams(tr, dst)
  local byname = {}
  for k = 0, n3 - 1 do
    local _, pn = reaper.TrackFX_GetParamName(tr, dst, k, "")
    byname[pn] = byname[pn] or {}
    table.insert(byname[pn], k)
  end
  local used, copied, missed = {}, 0, 0
  for k = 0, n2 - 1 do
    local _, pn = reaper.TrackFX_GetParamName(tr, src, k, "")
    local v = reaper.TrackFX_GetParamNormalized(tr, src, k)
    local target = nil
    local lst = byname[pn]
    if lst then
      for _, c in ipairs(lst) do
        if not used[c] then target = c; used[c] = true; break end
      end
    end
    if target == nil and n2 == n3 and not used[k] then target = k; used[k] = true end
    if target ~= nil then
      reaper.TrackFX_SetParamNormalized(tr, dst, target, v)
      copied = copied + 1
    else
      missed = missed + 1
      log(string.format("    no VST3 parameter for '%%s'", pn))
    end
  end
  return n2, n3, copied, missed
end
local function one(i, chunk, target)
  reaper.InsertTrackAtIndex(i, false)
  local tr = reaper.GetTrack(0, i)
  reaper.SetTrackStateChunk(tr, chunk, false)
  local n = reaper.TrackFX_GetCount(tr)
  log(string.format("%%d: plug-ins loaded from the block: %%d", i, n))
  if n < 1 then return end
  local dst = reaper.TrackFX_AddByName(tr, target, false, -1)
  log(string.format("%%d: '%%s' -> slot %%d", i, target, dst))
  if dst < 0 then return end
  local n2, n3, copied, missed = copy_params(tr, 0, dst)
  log(string.format("%%d: %%d parameters -> %%d, copied %%d, missed %%d", i, n2, n3, copied, missed))
  reaper.TrackFX_Delete(tr, 0)
end
local ok, err = pcall(function()
%(calls)s
end)
if not ok then log("ERROR " .. tostring(err)) end
reaper.Main_SaveProjectEx(0, "%(out)s", 0)
log("saved")
'''


def _lua_path(p):
    return p.replace('\\', '/')


def run_reaper(jobs, work_dir, log):
    """jobs: [(block_text, 'VST3: Name (Vendor)')] -> [fx or None] read back
    from the project REAPER saved, one per job."""
    from . import rpp_read
    reaper = None if os.environ.get('CPR_NO_REAPER') else media.find_reaper()
    if not reaper:
        log.append('reaper.exe was not found, so %d VST2 plug-in(s) saved as '
                   'REAPER parameter dumps arrive on their defaults' % len(jobs))
        return [None] * len(jobs)
    os.makedirs(work_dir, exist_ok=True)
    stamp = '%d' % int(time.time())
    script = os.path.join(work_dir, 'rehost_%s.lua' % stamp)
    out_rpp = os.path.join(work_dir, 'rehost_%s.rpp' % stamp)
    log_txt = os.path.join(work_dir, 'rehost_%s.log' % stamp)
    calls = []
    for i, (blk, target) in enumerate(jobs):
        chunk = '<TRACK\nNAME "rehost %d"\n<FXCHAIN\nSHOW 0\nLASTSEL 0\nDOCKED 0\n%s\n>\n>' % (i, blk)
        assert ']==]' not in chunk
        calls.append('  one(%d, [==[\n%s]==], "%s")' % (i, chunk, target.replace('"', '\\"')))
    text = LUA % {'log': _lua_path(log_txt), 'out': _lua_path(out_rpp),
                  'calls': '\n'.join(calls)}
    with open(script, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    t0 = time.time()
    # REAPER is watched rather than asked to quit: a plug-in that touches
    # its parameters after the save dirties the project again, and Quit
    # then waits on a save-changes box nobody answers. When the script has
    # logged 'saved' the project is on disk and this instance (only this
    # one - it is ours) is stopped.
    try:
        proc = subprocess.Popen([reaper, '-newinst', '-nosplash', '-new', script])
    except OSError as e:
        log.append('REAPER could not be started to re-host the VST2 plug-ins: %s' % e)
        proc = None
    if proc is not None:
        saved = False
        while time.time() - t0 < TIMEOUT:
            if proc.poll() is not None:
                break
            try:
                if 'saved' in open(log_txt, encoding='utf-8', errors='replace').read().splitlines()[-1:]:
                    saved = True
            except OSError:
                pass
            if saved:
                time.sleep(1.5)
                break
            time.sleep(0.5)
        if proc.poll() is None:
            if not saved:
                log.append('REAPER did not finish re-hosting the VST2 plug-ins '
                           'within %.0f s' % TIMEOUT)
            try:
                proc.terminate()
                proc.wait(timeout=20)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    notes = ''
    try:
        notes = open(log_txt, encoding='utf-8', errors='replace').read()
    except OSError:
        pass
    results = [None] * len(jobs)
    if os.path.isfile(out_rpp):
        try:
            root = rpp_read.parse(out_rpp)
            tracks = root.blocks[0].children('TRACK') if root.blocks else []
            for tb in tracks:
                m = re.match(r'rehost (\d+)', (tb.get('NAME') or [''])[0].strip('"'))
                chain = tb.child('FXCHAIN')
                if not m or chain is None:
                    continue
                fxs = rpp_read.read_fx(chain, [], None)
                i = int(m.group(1))
                if 0 <= i < len(jobs) and fxs and fxs[0].component:
                    results[i] = fxs[0]
        except Exception as e:
            log.append('the project REAPER saved after re-hosting could not '
                       'be read: %s' % e)
    if not os.environ.get('CPR_KEEP_PRINT'):
        for pth in (script, out_rpp, log_txt):
            try:
                os.remove(pth)
            except OSError:
                pass
    if 'ERROR' in notes or any(r is None for r in results):
        log.append('re-hosting notes from REAPER (%.0f s): %s'
                   % (time.time() - t0, ' | '.join(l for l in notes.splitlines() if l.strip())[:600]))
    return results


def apply(p, log, index):
    """Replace every parameter-dump VST2 in `p` by its VST3 build with the
    same settings. Returns how many were replaced."""
    if os.environ.get('CPR_NO_REHOST'):
        return 0
    cands = candidates(p)
    if not cands:
        return 0
    n_dump = sum(1 for _w, fx in cands if getattr(fx, 'param_dump', False))
    cache = _cache_load()
    jobs, plan, unmatched = [], [], []
    for where, fx in cands:
        e = vst3_for(fx, index)
        if e is None:
            unmatched.append('%s on %s' % (fx.name, where))
            continue
        blk = block_text(fx)
        key = hashlib.sha1((blk + '|' + e['uid']).encode('utf-8')).hexdigest()
        plan.append((where, fx, e, key, blk))
    if unmatched:
        log.append('%d VST2 plug-in(s) are saved as REAPER parameter dumps and '
                   'have no VST3 build here to carry the settings to, so they '
                   'arrive on their defaults: %s' % (len(unmatched), ', '.join(unmatched)))
    todo = [(i, pl) for i, pl in enumerate(plan) if pl[3] not in cache]
    # only REAPER can read its own parameter dumps back: without it (the
    # default - media.reaper_wanted) the cache of earlier runs still applies
    # and whatever it lacks arrives on its defaults
    if todo and media.reaper_wanted():
        work = os.path.join(cache_dir(), 'work')
        jobs = [(pl[4], '%s: %s' % ('VST3i' if pl[2]['inst'] else 'VST3', pl[2]['disp']))
                for _i, pl in todo]
        got = run_reaper(jobs, work, log)
        for (i, pl), fx3 in zip(todo, got):
            if fx3 is not None:
                cache[pl[3]] = {'uid': fx3.uid, 'name': fx3.name,
                                'component': fx3.component.hex(),
                                'controller': (fx3.controller or b'').hex(),
                                'n_in': fx3.n_in, 'n_out': fx3.n_out,
                                'block': block_text(fx3)}
        _cache_save(cache)
    done, failed = [], []
    for where, fx, e, key, blk in plan:
        c = cache.get(key)
        if not c:
            failed.append('%s on %s' % (fx.name, where))
            continue
        fx.uid = c['uid']
        fx.name = c['name']
        fx.component = bytes.fromhex(c['component'])
        fx.controller = bytes.fromhex(c['controller'])
        fx.n_in, fx.n_out = c.get('n_in', fx.n_in), c.get('n_out', fx.n_out)
        fx.vst2_id = None
        fx.param_dump = False
        fx.mapped_from = fx.mapped_from or 'VST2'
        # the VST3 block stands in for the VST2's wherever the lines are
        # written out again (a print project, a round trip)
        try:
            from . import rpp_read
            tmp = os.path.join(cache_dir(), 'block.rpp')
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write('<REAPER_PROJECT 0.1 "7.80/win64" 0\n<TRACK\n<FXCHAIN\n%s\n>\n>\n>\n' % c['block'])
            root = rpp_read.parse(tmp)
            chain = root.blocks[0].children('TRACK')[0].child('FXCHAIN')
            fx.raw_group = chain.raw
        except Exception:
            pass
        done.append('%s on %s' % (fx.name, where))
    if done:
        log.append('%d VST2 plug-in(s) were re-hosted: each was loaded again in a '
                   'REAPER of its own, its parameters copied onto the VST3 build '
                   'of the same plug-in, and that VST3 with its settings is what '
                   'Cubase gets (%d of them had been saved as REAPER\'s own '
                   'parameter dump, which nothing else reads): %s'
                   % (len(done), n_dump, ', '.join(done)))
    if failed:
        log.append('%d VST2 plug-in(s) arrive on their defaults: %s. REAPER saved '
                   'them as a bare list of parameter values, not as the plug-in\'s '
                   'own settings, and Cubase 15 loads only the VST3 build, whose '
                   'settings are stored in the plug-in\'s own format - which cannot '
                   'be written from a list of values without the plug-in. To keep '
                   'them: in REAPER, replace each with its VST3 build (or load it, '
                   'Save preset, and load that preset into the VST3), save, and '
                   'convert again'
                   % (len(failed), ', '.join(failed)))
    return len(done)
