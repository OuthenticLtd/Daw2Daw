"""A one-line console progress bar.

Long steps - copying half a gigabyte of media, zipping it again - otherwise
leave the window blank for minutes, which looks exactly like a hang. Writes
with a carriage return so it updates in place, and falls back to plain lines
when the output is redirected to a file.
"""
import os
import shutil
import sys
import time

_enabled = True


def enable(on=True):
    global _enabled
    _enabled = on


def _fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return ('%.0f %s' % (n, unit)) if unit in ('B', 'KB') else ('%.1f %s' % (n, unit))
        n /= 1024.0


def _fmt_time(s):
    s = int(s)
    return '%d:%02d' % (s // 60, s % 60) if s >= 60 else '%ds' % s


class Progress:
    """Progress over a known number of steps, or over a number of bytes."""

    def __init__(self, total, label, unit='', stream=None, min_interval=0.1):
        self.total = max(0, total or 0)
        self.label = label
        self.unit = unit
        self.stream = stream or sys.stdout
        self.n = 0
        self.bytes = 0
        self.start = time.time()
        self.last = 0.0
        self.min_interval = min_interval
        # drop.py pipes this output on to its console window, so a pipe can
        # still end on a screen: it says so, and how wide the window is
        self.tty = bool(getattr(self.stream, 'isatty', lambda: False)()
                        or os.environ.get('CPR_PROGRESS_TTY'))
        self.width = 0
        if _enabled:
            try:
                self.width = int(os.environ.get('CPR_COLUMNS') or 0) \
                    or shutil.get_terminal_size((80, 20)).columns
            except Exception:
                self.width = 80
        self._shown = False
        if _enabled and _overall is not None:
            _overall.begin(label, self)

    def _what(self, note=''):
        """What the overall bar says about this step: label, count, note."""
        bits = [self.label]
        if self.total:
            bits.append('%d/%d%s' % (min(self.n, self.total), self.total,
                                     (' ' + self.unit) if self.unit else ''))
        if self.bytes:
            bits.append(_fmt_bytes(self.bytes))
        if note:
            bits.append(note)
        return '  '.join(bits)

    def step(self, n=1, nbytes=0, note=''):
        self.n += n
        self.bytes += nbytes
        now = time.time()
        if not _enabled:
            return
        if _overall is not None:
            # one bar for the whole conversion: this step moves it on
            # within its stage, or only names what is happening now
            _overall.sub(self, (self.n / float(self.total)) if self.total else 0.0,
                         self._what(note))
            return
        done = self.n >= self.total
        if not done and (now - self.last) < self.min_interval:
            return
        self.last = now
        self._draw(note, now)

    def _draw(self, note, now):
        frac = (self.n / float(self.total)) if self.total else 0.0
        frac = min(1.0, max(0.0, frac))
        elapsed = now - self.start
        bits = ['%s' % self.label]
        if self.tty:
            barw = 30
            filled = int(round(barw * frac))
            bits.append('[%s%s]' % ('#' * filled, '-' * (barw - filled)))
        bits.append('%3d%%' % int(frac * 100))
        if self.total:
            bits.append('%d/%d%s' % (self.n, self.total,
                                     (' ' + self.unit) if self.unit else ''))
        if self.bytes:
            bits.append(_fmt_bytes(self.bytes))
            if elapsed > 0.5:
                bits.append('%s/s' % _fmt_bytes(self.bytes / elapsed))
        if frac > 0.02 and elapsed > 2 and frac < 1.0:
            bits.append('~%s left' % _fmt_time(elapsed / frac - elapsed))
        if note:
            bits.append(note)
        line = '  ' + '  '.join(bits)
        if self.tty:
            if self.width and len(line) > self.width - 3:
                line = line[:self.width - 4] + '...'
            self.stream.write('\r' + line.ljust(self.width - 1 if self.width else 0))
            self.stream.flush()
            self._shown = True
        else:
            # redirected: only print occasional milestones, one per line,
            # but always the last one so a log ends on a finished stage
            pct = int(frac * 100)
            if pct >= getattr(self, '_next_pct', 0) or frac >= 1.0:
                self._next_pct = pct + 25
                self.stream.write(line + '\n')
                self.stream.flush()

    def done(self, note=''):
        if not _enabled:
            return
        self.n = self.total
        if _overall is not None:
            _overall.sub(self, 1.0, self._what(note))
            return
        self._draw(note, time.time())
        if self.tty and self._shown:
            self.stream.write('\n')
        self.stream.flush()


def stage(text):
    """Announce a step that has no measurable progress."""
    if not _enabled:
        return
    if _overall is not None:
        _overall.begin(text)
        return
    sys.stdout.write('  %s\n' % text)
    sys.stdout.flush()


# ---- one bar for the whole conversion -----------------------------------

_overall = None


class _Passthrough:
    """stdout/stderr while the overall bar is up: anything else printed
    lands above the bar, which is drawn again under it."""

    def __init__(self, bar, real):
        self._bar, self._real = bar, real

    def write(self, s):
        if not s:
            return 0
        self._bar._clear()
        n = self._real.write(s)
        if s.endswith('\n'):
            self._bar._draw(force=True)
        return n

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


class Overall:
    """A single bar from 0 to 100 % over a whole conversion.

    `plan` is the conversion's stages in order, each (keywords, weight):
    a stage() line or a Progress whose label holds one of a later stage's
    keywords starts that stage, and that Progress's own steps then fill
    the stage's share. Anything else only changes the text beside the bar.
    The bar never goes back."""

    BARW = 30

    def __init__(self, plan, stream=None):
        plan = [(tuple(k.lower() for k in keys), float(w)) for keys, w in plan if w > 0]
        total = sum(w for _, w in plan) or 1.0
        self.keys = [k for k, _ in plan]
        self.share = [w / total for _, w in plan]
        self.start_at = [sum(self.share[:j]) for j in range(len(plan))]
        self.k = -1
        self.owner = None
        self.sub_f = 0.0
        self.value = 0.0
        self.text = ''
        self.t0 = time.time()
        self.last = 0.0
        self.drawn = False
        self.real_out, self.real_err = sys.stdout, sys.stderr
        self.stream = stream or self.real_out
        try:
            self.width = int(os.environ.get('CPR_COLUMNS') or 0) \
                or shutil.get_terminal_size((80, 20)).columns
        except Exception:
            self.width = 80

    def begin(self, label, owner=None):
        low = label.lower()
        for j in range(max(self.k + 1, 0), len(self.keys)):
            if any(key in low for key in self.keys[j]):
                self._advance(j)
                self.owner = owner
                break
        else:
            # the stage already under way (a second bar of the same kind
            # takes it over), or a step inside it
            if owner is not None and self.k >= 0 and \
                    any(key in low for key in self.keys[self.k]):
                self.owner = owner
        self.text = label
        self._draw(force=True)

    def _advance(self, j):
        self.value = max(self.value, self.start_at[j])
        self.k, self.sub_f = j, 0.0

    def sub(self, who, frac, text):
        if who is self.owner and self.k >= 0:
            self.sub_f = max(self.sub_f, min(1.0, max(0.0, frac)))
            self.value = max(self.value, self.start_at[self.k] + self.share[self.k] * self.sub_f)
        self.text = text
        self._draw()

    def _line(self, final=None):
        frac = 1.0 if final else min(0.999, self.value)
        filled = int(round(self.BARW * frac))
        el = time.time() - self.t0
        bits = ['[%s%s]' % ('#' * filled, '-' * (self.BARW - filled)), '%3d%%' % int(frac * 100)]
        if final:
            bits.append(final)
        else:
            bits.append(self.text)
            if frac > 0.05 and el > 3:
                bits.append('~%s left' % _fmt_time(el / frac - el))
        line = '  ' + '  '.join(bits)
        if self.width and len(line) > self.width - 2:
            line = line[:self.width - 5] + '...'
        return line

    def _draw(self, force=False, final=None):
        now = time.time()
        if not force and final is None and now - self.last < 0.1:
            return
        self.last = now
        line = self._line(final)
        self.stream.write('\r' + line.ljust((self.width or 80) - 2))
        self.stream.flush()
        self.drawn = True

    def _clear(self):
        if self.drawn:
            self.stream.write('\r' + ' ' * ((self.width or 80) - 2) + '\r')
            self.drawn = False

    def finish(self, ok=True):
        global _overall
        if ok:
            self._draw(final='done in %s' % _fmt_time(time.time() - self.t0))
        if self.drawn:
            self.stream.write('\n')
            self.drawn = False
        self.stream.flush()
        sys.stdout, sys.stderr = self.real_out, self.real_err
        _overall = None


def overall(plan):
    """Start one bar for the whole conversion when the output is a screen
    (otherwise the stages keep printing a line each, which reads better in a
    log). Returns the bar, or None; finish() it when the work is done."""
    global _overall
    if not _enabled:
        return None
    s = sys.stdout
    if not (getattr(s, 'isatty', lambda: False)() or os.environ.get('CPR_PROGRESS_TTY')):
        return None
    bar = Overall(plan)
    sys.stdout = _Passthrough(bar, bar.real_out)
    sys.stderr = _Passthrough(bar, bar.real_err)
    _overall = bar
    bar._draw(force=True)
    return bar


def fmt_bytes(n):
    """Public form of the size formatter, for stage lines."""
    return _fmt_bytes(n)
