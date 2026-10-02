"""Make a converted project self-contained.

Copies every piece of media the project actually references next to the
project file and repoints the items at the copies, so the whole folder can be
zipped and opened on another machine.

Only referenced media is copied: a Cubase project folder usually holds far
more than the current version uses - older takes, backups, video, bounces -
and none of that is needed to open the project.
"""
import os
import shutil
import zipfile

from . import progress


AUDIO_DIR = 'Audio'
VIDEO_DIR = 'Video'
VIDEO_EXT = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.webm', '.wmv',
             '.mpg', '.mpeg', '.m2v')


def _folder_for(path):
    return VIDEO_DIR if os.path.splitext(path)[1].lower() in VIDEO_EXT else AUDIO_DIR


def _unique_name(dest_dir, name, taken):
    """Two folders can hold different files with the same name."""
    base, ext = os.path.splitext(name)
    cand = name
    n = 1
    while cand.lower() in taken:
        cand = '%s-%d%s' % (base, n, ext)
        n += 1
    taken.add(cand.lower())
    return cand


def collect(proj, out_path, log=None, subdir=AUDIO_DIR):
    """Copy referenced media beside `out_path`; rewrite item paths.

    Returns a stats dict. Re-running is cheap: a destination file that is
    already there with the same size is left alone."""
    log = log if log is not None else []
    out_dir = os.path.dirname(os.path.abspath(out_path))
    stats = {'copied': 0, 'reused': 0, 'missing': 0, 'bytes': 0, 'files': 0,
             'video': 0}

    mapping = {}        # source path (lowercased) -> destination path
    taken = {}          # folder -> names already used there

    todo = [it for t in proj.tracks for it in t.items
            if it.kind in ('audio', 'video') and it.file]
    bar = progress.Progress(len(todo), 'collecting media', 'files')

    for t in proj.tracks:
        for it in t.items:
            if it.kind not in ('audio', 'video') or not it.file:
                continue
            subdir = _folder_for(it.file)
            media_dir = os.path.join(out_dir, subdir)
            if subdir not in taken:
                taken[subdir] = ({f.lower() for f in os.listdir(media_dir)}
                                 if os.path.isdir(media_dir) else set())
            src = it.file
            key = os.path.abspath(src).lower()
            if key in mapping:
                it.file = mapping[key]
                bar.step()
                continue
            if not os.path.exists(src):
                stats['missing'] += 1
                log.append('media not found, left as-is: %s' % src)
                bar.step()
                continue
            os.makedirs(media_dir, exist_ok=True)
            name = _unique_name(media_dir, os.path.basename(src), taken[subdir])
            dst = os.path.join(media_dir, name)
            try:
                if (os.path.exists(dst)
                        and os.path.getsize(dst) == os.path.getsize(src)):
                    stats['reused'] += 1
                else:
                    shutil.copy2(src, dst)
                    stats['copied'] += 1
            except Exception as e:
                stats['missing'] += 1
                log.append('could not copy %s (%s)' % (src, e))
                bar.step()
                continue
            stats['bytes'] += os.path.getsize(dst)
            stats['files'] += 1
            if subdir == VIDEO_DIR:
                stats['video'] += 1
            mapping[key] = dst
            it.file = dst
            bar.step(nbytes=os.path.getsize(dst), note=name[:28])
    bar.done()
    return stats


def plugin_manifest(proj, index=None):
    """The plug-ins a collaborator needs, one line each."""
    seen = {}
    for t in proj.tracks:
        for fx in ([t.instrument] if t.instrument else []) + list(t.fx):
            key = (fx.uid or fx.name).upper()
            if key not in seen:
                seen[key] = [fx, 0, t.instrument is fx]
            seen[key][1] += 1
    rows = []
    for fx, n, is_inst in seen.values():
        entry = index.lookup(fx)[0] if index is not None else None
        # Cubase shows the instance name ("Keys"), not the product - use the
        # real plug-in name whenever we can resolve it
        label = entry['disp'] if entry else fx.name
        rows.append((n, label, 'instrument' if is_inst else 'effect',
                     fx.uid, fx.name, entry is not None))
    rows.sort(key=lambda r: (r[2], -r[0], r[1].lower()))

    lines = ['Plug-ins this project needs', '=' * 27, '']
    for kind in ('instrument', 'effect'):
        group = [r for r in rows if r[2] == kind]
        if not group:
            continue
        lines.append('%ss' % kind.capitalize())
        for n, label, _k, _uid, cname, known in group:
            note = ('' if cname == label.split(' (')[0]
                    else '  (called "%s" in the project)' % cname)
            flag = '' if known else '  ** NOT INSTALLED ON THIS MACHINE **'
            lines.append('  %-46s x%-3d%s%s' % (label, n, note, flag))
        lines.append('')
    lines.append('Plug-in IDs, in case a name is ambiguous:')
    for _n, label, _k, uid, _c, _known in rows:
        lines.append('  %-46s %s' % (label, uid or '-'))
    lines.append('')
    lines.append('Every plug-in carries its saved settings inside the project')
    lines.append('file, so each one opens with the right patch as long as the')
    lines.append('plug-in itself is installed.')
    return '\n'.join(lines) + '\n'


def zip_folder(folder, zip_path, log=None):
    """Zip a folder's contents, storing paths relative to the folder."""
    log = log if log is not None else []
    folder = os.path.abspath(folder)
    total = 0
    entries = []
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            full = os.path.join(root, f)
            if os.path.abspath(full) != os.path.abspath(zip_path):
                entries.append(full)
    bar = progress.Progress(len(entries), 'zipping', 'files')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED,
                         allowZip64=True) as z:
        for full in entries:
            z.write(full, os.path.relpath(full, folder))
            n = os.path.getsize(full)
            total += n
            bar.step(nbytes=n, note=os.path.basename(full)[:28])
    bar.done()
    return {'entries': total, 'size': os.path.getsize(zip_path)}
