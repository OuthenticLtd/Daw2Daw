"""Track-to-track output routing for a host that only routes to groups.

Cubase sends a channel's output to a group channel and nothing else, and a
folder that sums is how a group channel is built (cpr_build). Live's 'Audio
To' another track (and a REAPER track sent to another with its parent send
off) is read as the track's out_bus_id; for Cubase each such track moves
inside its target, made a folder, so it plays through the target's effects
and fader as it did:

- a target with nothing of its own to play (a bus: no clips, no
  instrument) becomes the folder - its effects and level the group's;
- a target with clips or an instrument stays a track inside a new folder,
  '<name> (bus)', which takes its effects, level and pan, the target and
  its sources both summing there.

Tracks move, so every send's destination is renumbered."""
from .model import Track, output_routes


def _subtree(tracks, i):
    j = i + 1
    while j < len(tracks) and tracks[j].depth > tracks[i].depth:
        j += 1
    return list(range(i, j))


def routes_as_folders(proj, log):
    tr = proj.tracks
    routes = output_routes(proj)
    if not routes:
        return 0
    into = {}
    for i, g in routes:
        if g not in _subtree(tr, i):       # never into its own child
            into.setdefault(g, []).append(i)
    moving = set()
    for srcs in into.values():
        for s in srcs:
            moving.update(_subtree(tr, s))
    order = []                             # old indices, or a new Track
    placed = set()

    def place(i, depth):
        """Track i and everything inside it at `depth`, then whatever
        outputs to it, inside it."""
        if i in placed:
            return
        sub = _subtree(tr, i)
        shift = depth - tr[i].depth
        t = tr[i]
        inner = depth + 1
        if i in into:
            if not t.is_folder and (t.items or t.instrument is not None):
                bus = Track(t.name + ' (bus)', depth)
                bus.is_folder = True
                bus.fx, t.fx = t.fx, []
                bus.vol, t.vol = t.vol, 1.0
                bus.pan, t.pan = t.pan, 0.0
                bus.volenv, t.volenv = t.volenv, []
                bus.panenv, t.panenv = t.panenv, []
                bus.mute, t.mute = t.mute, 0
                bus.color = t.color
                order.append(bus)
                shift += 1
                inner = depth + 1
                log.append('%r takes other tracks\' output and plays clips of its own: in '
                           'Cubase a group channel %r holds it and them, with its effects '
                           'and level' % (t.name, bus.name))
            else:
                t.is_folder = True
        for k in sub:
            if k in placed:
                continue
            if k != i and k in into:
                place(k, tr[k].depth + shift)
                continue
            tr[k].depth += shift
            placed.add(k)
            order.append(k)
        for s in into.get(i, []):
            place(s, inner)
        if i in into:
            log.append('%s output to %r: in Cubase inside it, its group channel'
                       % (', '.join(repr(tr[s].name) for s in into[i]), tr[i].name))

    for i in range(len(tr)):
        if i in moving or i in placed:
            continue
        place(i, tr[i].depth)
    for i in range(len(tr)):           # anything a cycle left out
        if i not in placed:
            placed.add(i)
            order.append(i)
    new_index = {}
    new_tracks = []
    for pos, x in enumerate(order):
        if isinstance(x, Track):
            new_tracks.append(x)
        else:
            new_index[x] = pos
            new_tracks.append(tr[x])
    for t in new_tracks:
        for s in getattr(t, 'sends', []):
            if s.dest is not None and s.dest in new_index:
                s.dest = new_index[s.dest]
        if t.out_bus_id is not None and any(t is tr[i] for i, _ in routes):
            t.out_bus_id = None              # it sums into its folder now
    proj.tracks = new_tracks
    return len(into)
