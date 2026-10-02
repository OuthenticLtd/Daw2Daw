#!/usr/bin/env python3
"""Convert projects between Cubase, REAPER and Ableton Live.

    convert.py song.cpr song.rpp        Cubase project -> REAPER project
    convert.py song.rpp song.xml        REAPER project -> Cubase Track Archive
    convert.py song.cpr song.xml        rewrite a Cubase project as an archive
    convert.py song.rpp out.rpp         normalise a REAPER project
    convert.py song.rpp song.als        REAPER project -> Live 11 Set
    convert.py song.cpr song.als        Cubase project -> Live 11 Set

The output format is taken from the extension; with only an input given, a
REAPER project is written next to it.

Import a .xml into Cubase with File > Import > Track Archive.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cubaserea import (cpr_read, cpr_write, cpr_build, rpp_read, rpp_write, media,
                       als_write,
                       cubase_xml_write, midi_write, plugins, collect,
                       progress)


# Opening a project instantiates every plug-in in it before the project
# appears. The cost is nowhere near evenly spread: a synth writes a patch of
# a few kilobytes and opens instantly - 21 instances of Hive came to under a
# gigabyte between them - while a sampler writes megabytes and every one of
# those megabytes is a reference to a library it then pulls off disk. So a
# sampler is recognised by the size of its saved settings rather than by
# name, which would be a list that goes stale.
SAMPLER_STATE = 1024 * 1024

# What one loaded sampler instance costs in memory is a property of the
# library, not of anything in the project file, so it cannot be read out of
# the .cpr. This is a deliberately cautious figure for a produced patch; the
# decision it feeds is scaled to the machine actually doing the work rather
# than to any one project, and --sampler-cost overrides it when a particular
# set of libraries is known to be lighter or heavier.
SAMPLER_COST = 1.5 * 1024 ** 3

# Leave this much of physical memory for the host, its media cache and the
# rest of the machine before samplers are allowed to fill the remainder.
HEADROOM = 0.35


def total_ram():
    """Physical memory on this machine, or None if it cannot be read."""
    if sys.platform == 'emscripten' or os.environ.get('CPR_OTHER_MACHINE'):
        # In the browser this is the page's own memory, a few hundred MB,
        # not the computer the project will open on: two of
        # SuperThunderCrown's Kontakts came out offline on the website and
        # online on the desktop. A server converting for someone else
        # (CPR_OTHER_MACHINE) cannot know that computer either.
        return None
    try:
        if os.name == 'nt':
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong),
                            ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong),
                            ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong),
                            ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong),
                            ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullTotalPhys)
        else:
            return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except Exception:
        pass
    return None


def instrument_state(p):
    return sum(len(t.instrument.component or b'')
               for t in p.tracks if t.instrument is not None)


def samplers(p):
    """The instruments heavy enough to drag a library in behind them."""
    return [t for t in p.tracks if t.instrument is not None
            and len(t.instrument.component or b'') > SAMPLER_STATE]


def sampler_budget(cost=None, ram=None):
    """How many samplers this machine can be expected to open at once."""
    ram = ram or total_ram()
    if not ram:
        return None                      # unknown machine: do not second-guess
    return max(1, int((ram * (1.0 - HEADROOM)) / (cost or SAMPLER_COST)))


def heavy_load(p, cost=None):
    """True when opening every sampler at once would not fit in memory."""
    budget = sampler_budget(cost)
    return budget is not None and len(samplers(p)) > budget


def load(path, log, out=None, index=None):
    ext = os.path.splitext(path)[1].lower()
    if ext == '.cpr' or ext == '.bak':
        p = cpr_read.read(path)
        log.extend(getattr(p, 'log', []))
        return p
    if ext == '.als':
        from cubaserea import als_read
        progress.stage('reading Live Set %s' % os.path.basename(path))
        p = als_read.read(path, log)
        return p
    if ext == '.rpp':
        printer = None
        if out and out.lower().endswith('.cpr'):
            # tracks only REAPER can play are printed into the Cubase
            # project's Audio folder before anything else happens
            from cubaserea import print_tracks
            audio_dir = os.path.join(os.path.dirname(os.path.abspath(out)),
                                     'Audio')
            printer = print_tracks.printer(audio_dir)
        # the plug-in list has to be in hand before the tracks are read: it
        # is what says whether a CLAP can be loaded as the VST build of the
        # same plug-in, and so whether its track is printed at all
        progress.stage('reading REAPER project %s' % os.path.basename(path))
        return rpp_read.read(path, log, printer=printer, index=index)
    raise SystemExit('cannot read %s (expected .cpr, .bak, .rpp or .als)' % path)


def summarise(p):
    folders = sum(1 for t in p.tracks if t.is_folder)
    audio = sum(1 for t in p.tracks for i in t.items if i.kind == 'audio')
    midi = sum(1 for t in p.tracks for i in t.items if i.kind == 'midi')
    notes = sum(len(i.notes) for t in p.tracks for i in t.items)
    fx = sum(len(t.fx) for t in p.tracks)
    state = sum(1 for t in p.tracks for f in t.fx if f.component)
    inst = sum(1 for t in p.tracks if t.instrument)
    inst_state = sum(1 for t in p.tracks if t.instrument and t.instrument.component)
    sends = sum(len(t.sends) for t in p.tracks)
    env = sum(len(t.volenv) for t in p.tracks)
    penv = sum(len(pts) for t in p.tracks
               for f in ([t.instrument] if t.instrument else []) + list(t.fx)
               for _i, pts in f.envelopes)
    vids = sum(1 for t in p.tracks for i in t.items if i.kind == 'video')
    fades = sum(1 for t in p.tracks for i in t.items if i.fadein or i.fadeout)
    return ('tracks %d (%d folders) | markers %d | audio items %d | '
            'midi items %d (%d notes)\n'
            'instruments %d (%d with saved settings) | '
            'effects %d (%d with saved settings)\n'
            'sends %d | item fades %d | video items %d\n'
            'automation: %d channel points, %d plug-in parameter points'
            % (len(p.tracks) - folders, folders, len(p.markers), audio, midi,
               notes, inst, inst_state, fx, state, sends, fades, vids,
               env, penv))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('output', nargs='?')
    ap.add_argument('--media-root', default=None,
                    help='write media paths relative to this folder')
    ap.add_argument('--reaper-ini', default=None,
                    help="REAPER's scanned plug-in cache "
                         "(default %%APPDATA%%\\REAPER\\reaper-vstplugins64.ini)")
    ap.add_argument('--vst-dir', action='append', default=None,
                    help='extra plug-in folder to search (repeatable)')
    ap.add_argument('--no-fx', action='store_true',
                    help='leave effect chains out')
    ap.add_argument('--no-instruments', action='store_true',
                    help='leave VST instruments out (much smaller output: a '
                         'sampler can carry megabytes of state per instance)')
    ap.add_argument('--instruments-offline', action='store_true',
                    help='add instruments offline (shown red in REAPER until '
                         'you bring them online). They still carry their patch, '
                         'and peak memory on a sampler-heavy project drops '
                         'sharply - but they are not playable until enabled. '
                         'Samplers are held back on their own when opening '
                         'them all at once would not fit in this machine\'s '
                         'memory (about %d of them here)'
                         % (sampler_budget() or 0))
    ap.add_argument('--instruments-online', action='store_true',
                    help='load every instrument straight away even on a heavy '
                         'project. REAPER then has to open all of them before '
                         'the project appears, which on a sampler-heavy '
                         'project can take many minutes or fail outright')
    ap.add_argument('--no-video', action='store_true',
                    help='leave the video track out. REAPER decodes video on '
                         'load, which on a project with long clips can add '
                         'minutes to the opening time')
    ap.add_argument('--no-events', action='store_true',
                    help='Track Archive only: leave audio events out')
    ap.add_argument('--collect', action='store_true',
                    help='copy every referenced media file next to the output '
                         'and point the project at the copies, so the folder '
                         'stands on its own')
    ap.add_argument('--zip', action='store_true',
                    help='zip the output folder ready to hand over (implies '
                         '--collect)')
    ap.add_argument('--sampler-cost', type=float, default=None, metavar='GB',
                    help='what one loaded sampler instance is expected to '
                         'cost in memory (default %.1f GB). That figure is a '
                         'property of the library, not of anything in the '
                         'project file, so it cannot be read out - raise it '
                         'if your libraries are heavy and the project will '
                         'not open, lower it if they are light'
                         % (SAMPLER_COST / 1024.0 ** 3))
    ap.add_argument('--template', default=None,
                    help='the .cpr to update when writing a .cpr. A Cubase '
                         'project can only be written by editing one, so '
                         'writing back to Cubase needs the project the work '
                         'started from')
    ap.add_argument('--donor', default=None,
                    help='a Cubase project to build a new .cpr out of. It '
                         'supplies one record of each kind - an audio event, '
                         'an instrument track, a MIDI part, a marker - and '
                         'every track written is a copy of the matching one')
    ap.add_argument('--plugin-format', choices=('source', 'vst3', 'vst2'), default='source',
                    help="third-party plug-ins with both builds: keep each as it was saved "
                         "(source), or turn it into its VST3 or its VST2 build, settings carried "
                         "(plugin_formats.py) - for a machine where only the other build loads")
    ap.add_argument('-q', '--quiet', action='store_true')
    a = ap.parse_args()

    # A console window is rarely UTF-8. Track and plug-in names carry
    # whatever the project was written with, and one character the console
    # cannot represent would otherwise end the run with a traceback part way
    # through - which from the outside looks exactly like a crash.
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors='replace')
        except Exception:
            pass

    out = a.output
    if not out:
        # A converted project goes in a folder of its own, beside the one it
        # came from. That keeps the source folder tidy, and Cubase expects a
        # project to own its folder - it puts Audio and Images next to it.
        stem = os.path.splitext(os.path.basename(a.input))[0]
        here = os.path.dirname(os.path.abspath(a.input))
        if a.input.lower().endswith(('.cpr', '.bak', '.als')):
            out = os.path.join(here, stem + ' (REAPER)', stem + '.rpp')
        else:
            out = os.path.join(here, stem + ' (Cubase)', stem + '.cpr')
    folder = os.path.dirname(os.path.abspath(out))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)

    progress.enable(not a.quiet)
    if not a.quiet:
        print('%s' % a.input)
    bar = progress.overall(plan(a, out))
    try:
        convert(a, out, bar)
    except BaseException:
        if bar is not None:
            bar.finish(ok=False)
        raise


# The stages of a conversion in the order they come, each with its share of
# the one progress bar: (words in the stage line or bar label, weight). The
# weights follow where the time goes - reading and writing a project takes
# seconds, media (converting, collecting, zipping) can take minutes.
def plan(a, out):
    gather = a.collect or a.zip
    to_cubase = out.lower().endswith(('.cpr', '.xml'))
    if out.lower().endswith('.als'):
        return [(('plug-in list',), 3), (('reading',), 30),
                (('collecting media',), 30 if gather else 0),
                (('converting audio',), 15),
                (('writing live set',), 30),
                (('zipping',), 20 if a.zip else 0)]
    if to_cubase:
        return [(('plug-in list',), 3), (('reading reaper project',), 7),
                (('collecting media',), 30 if gather else 0),
                (('opening', 'reading cubase project'), 8),
                (('converting audio', 'checking'), 25),
                (('building', 'writing cubase project', 'track archive'), 30),
                (('marker', 'removing', 'putting'), 10),
                (('zipping',), 20 if a.zip else 0)]
    return [(('plug-in list',), 3), (('opening', 'reading cubase project'), 35),
            (('collecting media',), 30 if gather else 0),
            (('converting audio',), 15),
            (('writing reaper project',), 35), (('saving',), 4),
            (('zipping',), 20 if a.zip else 0)]


def convert(a, out, bar):
    progress.stage('reading the REAPER plug-in list')
    idx_for_manifest = plugins.PluginIndex(a.reaper_ini, a.vst_dir)
    log = []
    # what the project is written as decides which plug-in builds it can
    # hold (rehost: a Cubase project needs every VST2 as its VST3)
    os.environ['CPR_TARGET_EXT'] = os.path.splitext(out)[1].lower()
    p = load(a.input, log, out, idx_for_manifest)
    fmt = getattr(a, 'plugin_format', 'source')
    if os.path.splitext(out)[1].lower() == '.cpr':
        if fmt == 'vst2':
            log.append('Cubase 15 loads no VST2 plug-ins: each plug-in is written as its VST3 build')
        fmt = 'vst3'
    if fmt != 'source':
        from cubaserea import plugin_formats
        n = plugin_formats.apply(p, fmt, log)
        if n:
            log.append('%d plug-in(s) turned into their %s build' % (n, fmt.upper()))
    if a.no_fx:
        for t in p.tracks:
            t.fx = []
            t.instrument = None
    if a.no_instruments:
        for t in p.tracks:
            t.instrument = None
    if a.no_video:
        p.tracks = [t for t in p.tracks if t.kind != 'video']
    cost = (a.sampler_cost * 1024.0 ** 3) if a.sampler_cost else None
    auto_offline = heavy_load(p, cost) and not (a.instruments_online
                                          or a.instruments_offline)
    if a.instruments_offline:
        held_back = [t for t in p.tracks if t.instrument is not None]
    elif auto_offline:
        held_back = samplers(p)
    else:
        held_back = []
    for t in held_back:
        t.instrument.offline = 1

    gather = a.collect or a.zip
    cstats = None
    if gather:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
        # the files where they really are first (a Cubase project names the
        # drive it was recorded on), then the copies beside the new project
        media.relink(p, out, log)
        cstats = collect.collect(p, out, log)

    ext = os.path.splitext(out)[1].lower()
    if ext == '.rpp':
        idx = idx_for_manifest
        root = a.media_root or os.path.dirname(os.path.abspath(out))
        if a.input.lower().endswith(('.cpr', '.bak')):
            # A Cubase project remembers where its audio was when it was
            # recorded - another drive, another machine - and Cubase itself
            # falls back to the project's own Audio folder. So does this.
            media.relink(p, out, log)
            from cubaserea.model import merge_video_audio
            merge_video_audio(p, log)
        stats = rpp_write.write(p, out, media_root=root, plugin_index=idx, log=log)
        if a.input.lower().endswith(('.cpr', '.bak')):
            # so the trip back to Cubase finds its own project without asking
            cpr_write.note_origin(out, a.input)
        extra = ('plug-ins written %d (%d with their settings) | sends %d'
                 % (stats['fx'], stats['fx_state'], stats['sends']))
        n_off = sum(1 for t in p.tracks if t.instrument is not None
                    and t.instrument.offline)
        if n_off and auto_offline:
            n_on = sum(1 for t in p.tracks if t.instrument is not None) - n_off
            names = sorted({t.instrument.name for t in held_back})
            extra += ('\n%d instruments load normally and are playable at once.'
                      '\n%d samplers (%s) are added OFFLINE, shown red: this '
                      'machine has %s of memory, which at about %.1f GB per '
                      'loaded instance is room for roughly %d of them at a '
                      'time. They keep their patch - select the tracks you '
                      'want to hear and run the "cubaserea bring samplers '
                      'online" action, or bring one online from its FX window. '
                      '--instruments-online loads them all up front, and '
                      '--sampler-cost sets the per-instance figure if your '
                      'libraries are lighter or heavier than that'
                      % (n_on, n_off, ', '.join(names)[:60],
                         progress.fmt_bytes(total_ram() or 0),
                         (cost or SAMPLER_COST) / 1024.0 ** 3,
                         sampler_budget(cost) or 0))
        elif n_off:
            extra += ('\n%d instruments added offline so the project opens at '
                      'once - bring one online from its FX window when you '
                      'want it' % n_off)
    elif ext == '.cpr':
        # A REAPER project with no Cubase original is built from the donor;
        # one that came from Cubase updates its own project in place.
        tmpl, how = cpr_write.find_template(a.input, a.template)
        if tmpl is None:
            from cubaserea import plugin_formats, routing
            routing.routes_as_folders(p, log)
            plugin_formats.for_cubase(p, log)
            donor = a.donor or cpr_build.pick_donor(p)
            stats = cpr_build.write(p, out, donor=donor, log=log)
            extra = ('Cubase project built from the donor: %d audio track(s), '
                     '%d instrument track(s), %d event(s), %d part(s) with '
                     '%d note(s), %d plug-in(s)'
                     % (stats['audio_tracks'], stats['instrument_tracks'],
                        stats['events'], stats['parts'], stats['notes'],
                        stats['plugins']))
            if stats['skipped']:
                extra += ('\n%d item(s) past the first on their track were '
                          'not written' % stats['skipped'])
            log.append('built from %s' % os.path.basename(donor))
            tmpl = None
        # Two passes. The first reshapes the project - renames, and takes out
        # tracks that are gone - which moves records around. The second sets
        # the fields that keep their size, reading the reshaped file so its
        # offsets are the ones that survive.
        if tmpl is not None:
            stats = cpr_write.write(p, out, tmpl, log=log)
            extra = ('Cubase project written by updating %s in place (%s): '
                     '%d field(s) changed - %d part(s) moved, %d resized, '
                     '%d muted/unmuted, %d level(s)'
                     % (os.path.basename(tmpl), how,
                        stats['fields'], stats['moved'], stats['resized'],
                        stats['muted'], stats['levels']))
        if tmpl is not None and stats.get('skipped'):
            extra += ('\n%d change(s) could not be written in place and are '
                      'listed below - a .cpr can only be edited, not rebuilt, '
                      'so anything that changes the size of a record has to be '
                      'redone in Cubase' % stats['skipped'])
    elif ext == '.als':
        progress.stage('writing Live Set %s' % os.path.basename(out))
        stats = als_write.write(p, out, log=log)
        extra = ('Live 11 Set: %d track(s), %d group(s), %d audio clip(s) '
                 '(Warp off), %d MIDI clip(s) with %d note(s)'
                 % (stats['tracks'], stats['groups'], stats['audio_clips'],
                    stats['midi_clips'], stats['notes']))
    elif ext == '.xml':
        stats = cubase_xml_write.write(p, out, with_events=not a.no_events, log=log)
        extra = 'Cubase Track Archive: import with File > Import > Track Archive'
        mid = os.path.splitext(out)[0] + '.mid'
        n = midi_write.write(p, mid)
        if n:
            extra += ('\n%d MIDI parts written to %s '
                      '(File > Import > MIDI File)' % (n, os.path.basename(mid)))
    else:
        raise SystemExit('cannot write %s (expected .rpp, .cpr, .xml or .als)' % out)

    out_dir = os.path.dirname(os.path.abspath(out)) or '.'
    manifest = None
    if gather:
        manifest = os.path.join(out_dir, 'PLUGINS-NEEDED.txt')
        with open(manifest, 'w', encoding='utf-8') as f:
            f.write(collect.plugin_manifest(p, idx_for_manifest))
    zstats = None
    if a.zip:
        zpath = os.path.splitext(out)[0] + '.zip'
        zstats = collect.zip_folder(out_dir, zpath, log)
    if bar is not None:
        bar.finish()

    if not a.quiet:
        print('')
        print('  -> %s' % out)
        print(summarise(p))
        print(extra)
        if cstats:
            print('collected %d media files (%d copied, %d already there, '
                  '%d video, %.1f GB)%s'
                  % (cstats['files'], cstats['copied'], cstats['reused'],
                     cstats.get('video', 0), cstats['bytes'] / 1e9,
                     ', %d missing' % cstats['missing'] if cstats['missing'] else ''))
            if manifest:
                print('wrote %s' % os.path.basename(manifest))
        if zstats:
            print('zipped -> %s (%.1f GB)'
                  % (os.path.basename(os.path.splitext(out)[0] + '.zip'),
                     zstats['size'] / 1e9))
        if log:
            seen, shown = set(), 0
            print('notes (%d):' % len(log))
            for m in log:
                if m in seen:
                    continue
                seen.add(m)
                shown += 1
                if shown > 25:
                    print('    ... %d more' % (len(log) - 25))
                    break
                print('    ' + m)


if __name__ == '__main__':
    main()
