#!/usr/bin/env python3
"""Opt-in: hide the League of Legends splash window when it appears.

The League client can create a borderless, override-redirect 512x216 window
that shows the League of Legends splash overlay. It is not managed by the window
manager and does not appear in _NET_CLIENT_LIST, so it can stay on screen
without a taskbar entry. It may appear at any time during a session.

This helper polls the X11 tree every five seconds and unmaps only windows that
match all of:

- WM_CLASS is LeagueClient/LeagueClient
- the window is 512x216
- the window is override-redirect (not managed by the window manager)

It is window management only. It does not patch client binaries, CEF,
anti-cheat or DRM, and it never touches the game window. It is off by default,
fails closed, and is intended for X11/Xwayland desktops. Hiding is an unmap,
not a destroy: the client keeps running.

Diagnose one observation with:

    python3 tools/hide-league-splash-window.py --once

Enable it for a launcher run with:

    RCLOL_HIDE_LEAGUE_SPLASH=1 ./scripts/run.sh
"""

import argparse
import ctypes
import os
import signal
import sys
import time
from pathlib import Path

WIDTH = 512
HEIGHT = 216
POLL_SECONDS = 5.0
IDLE_SECONDS = 120
SOCK_VARIABLE = b'__mldr_sockpath'
SOCK_NAME = '.darlingserver.sock'
CLIENT_NAMES = ('RiotClientServices', 'LeagueClient', 'LeagueClientUx')
WINDOW_NAME = b'leagueclient'


def target_prefix():
    """The prefix this launcher owns, or None when it cannot be determined."""
    value = os.environ.get('DPREFIX', str(Path.home() / '.darling'))
    prefix = Path(value).expanduser()
    return prefix if prefix.is_absolute() else None


def is_splash(attributes, res_name, res_class):
    """True only for the fixed-size, override-redirect League splash."""
    name = (res_name or b'').lower()
    cls = (res_class or b'').lower()
    return (
        attributes.override_redirect == 1
        and attributes.map_state == 2  # IsViewable
        and attributes.width == WIDTH
        and attributes.height == HEIGHT
        and name == WINDOW_NAME
        and cls == WINDOW_NAME
    )


def find_splash_windows(x11):
    """Return visible splash window IDs, including override-redirect windows."""
    found = []
    for window in x11.top_level_windows():
        attributes = x11.attributes(window)
        if attributes is None:
            continue
        if not is_splash(attributes, *x11.class_hint(window)):
            continue
        found.append(window)
    return found


def process_state(proc):
    try:
        return (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def command_names(proc):
    try:
        raw = (proc / 'cmdline').read_bytes()
    except OSError:
        return []
    return [os.path.basename(part.decode(errors='replace'))
            for part in raw.split(b'\0') if part]


def client_running(prefix, proc_root=Path('/proc')):
    """True/False for this prefix's client; None when /proc is unreliable.

    Guest processes do not carry DPREFIX. They are attributed through the
    loader socket in __mldr_sockpath, the same way the in-game helper does it.
    """
    owner = (prefix / SOCK_NAME).parent.resolve()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for proc in entries:
        if not proc.name.isdigit() or process_state(proc) in ('Z', None):
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            if not any(name in CLIENT_NAMES for name in command_names(proc)):
                continue
            environment = dict(
                item.split(b'=', 1)
                for item in (proc / 'environ').read_bytes().split(b'\0')
                if b'=' in item
            )
            socket = environment.get(SOCK_VARIABLE)
            if socket is None:
                continue
            if Path(os.fsdecode(socket)).parent.resolve() == owner:
                return True
        except FileNotFoundError:
            if proc.exists():
                return None
            continue
        except (OSError, ValueError):
            return None
    return False


class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ('x', ctypes.c_int),
        ('y', ctypes.c_int),
        ('width', ctypes.c_int),
        ('height', ctypes.c_int),
        ('border_width', ctypes.c_int),
        ('depth', ctypes.c_int),
        ('visual', ctypes.c_void_p),
        ('root', ctypes.c_ulong),
        ('class_', ctypes.c_int),
        ('bit_gravity', ctypes.c_int),
        ('win_gravity', ctypes.c_int),
        ('backing_store', ctypes.c_int),
        ('backing_planes', ctypes.c_ulong),
        ('backing_pixel', ctypes.c_ulong),
        ('save_under', ctypes.c_int),
        ('colormap', ctypes.c_ulong),
        ('map_installed', ctypes.c_int),
        ('map_state', ctypes.c_int),
        ('all_event_masks', ctypes.c_long),
        ('your_event_mask', ctypes.c_long),
        ('do_not_propagate_mask', ctypes.c_long),
        ('override_redirect', ctypes.c_int),
        ('screen', ctypes.c_void_p),
    ]


class XClassHint(ctypes.Structure):
    _fields_ = [
        ('res_name', ctypes.c_void_p),
        ('res_class', ctypes.c_void_p),
    ]


class X11:
    def __init__(self):
        self.lib = ctypes.CDLL('libX11.so.6')
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.lib.XQueryTree.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.lib.XQueryTree.restype = ctypes.c_int
        self.lib.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes),
        ]
        self.lib.XGetWindowAttributes.restype = ctypes.c_int
        self.lib.XGetClassHint.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XClassHint),
        ]
        self.lib.XGetClassHint.restype = ctypes.c_int
        self.lib.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.XFree.argtypes = [ctypes.c_void_p]
        self.lib.XCloseDisplay.argtypes = [ctypes.c_void_p]

        error_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._error_handler = error_type(lambda _display, _event: 0)
        self.lib.XSetErrorHandler.argtypes = [error_type]
        self.lib.XSetErrorHandler.restype = ctypes.c_void_p
        self.lib.XSetErrorHandler(self._error_handler)

        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('cannot open DISPLAY')
        self.root = self.lib.XDefaultRootWindow(self.display)

    def top_level_windows(self):
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        if not self.lib.XQueryTree(self.display, self.root, ctypes.byref(root),
                                   ctypes.byref(parent), ctypes.byref(children),
                                   ctypes.byref(count)):
            return []
        try:
            return [children[index] for index in range(count.value)]
        finally:
            if children:
                self.lib.XFree(children)

    def attributes(self, window):
        attributes = XWindowAttributes()
        if not self.lib.XGetWindowAttributes(self.display, window,
                                             ctypes.byref(attributes)):
            return None
        return attributes

    def class_hint(self, window):
        hint = XClassHint()
        if not self.lib.XGetClassHint(self.display, window, ctypes.byref(hint)):
            return (b'', b'')
        try:
            if not hint.res_name or not hint.res_class:
                return (b'', b'')
            name = ctypes.cast(hint.res_name, ctypes.c_char_p).value or b''
            cls = ctypes.cast(hint.res_class, ctypes.c_char_p).value or b''
            return (name, cls)
        finally:
            if hint.res_name:
                self.lib.XFree(hint.res_name)
            if hint.res_class:
                self.lib.XFree(hint.res_class)

    def unmap(self, window):
        self.lib.XUnmapWindow(self.display, window)
        self.lib.XSync(self.display, 0)

    def close(self):
        self.lib.XCloseDisplay(self.display)


class Watcher:
    """Hide matching splash windows until the client session ends."""

    def __init__(self, prefix, x11, window_source=find_splash_windows,
                 client_source=client_running, sleep=time.sleep, out=sys.stdout,
                 clock=time.monotonic):
        self.prefix = prefix
        self.x11 = x11
        self.window_source = window_source
        self.client_source = client_source
        self.sleep = sleep
        self.out = out
        self.clock = clock
        self.hidden = set()

    def _log(self, message):
        try:
            print(message, file=self.out, flush=True)
        except Exception:
            # A closed launcher pipe must not stop the watcher.
            pass

    def _hide(self):
        for window in self.window_source(self.x11):
            try:
                self.x11.unmap(window)
            except Exception as error:
                self._log(f'hide-league-splash-window: cannot hide 0x{window:x}: {error}')
                continue
            if window not in self.hidden:
                self.hidden.add(window)
                self._log(f'hide-league-splash-window: hidden 0x{window:x}')

    def run(self, max_steps=None):
        idle_since = None
        steps = 0
        while True:
            state = self.client_source(self.prefix)
            now = self.clock()
            if state is not None:
                self._hide()
            if state is False:
                if idle_since is None:
                    idle_since = now
                elif now - idle_since >= IDLE_SECONDS:
                    self._log('hide-league-splash-window: no client session; leaving')
                    return 0
            else:
                # None means unreadable /proc; keep watching instead of exiting.
                idle_since = None
            steps += 1
            if max_steps is not None and steps >= max_steps:
                return 0
            self.sleep(POLL_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--detach', action='store_true',
                        help='leave the launcher session so a terminal hang-up cannot end it')
    parser.add_argument('--once', action='store_true',
                        help='one window observation and exit (no window changes)')
    args = parser.parse_args(argv)

    if not args.once and os.environ.get('RCLOL_HIDE_LEAGUE_SPLASH') != '1':
        print('hide-league-splash-window: disabled; set RCLOL_HIDE_LEAGUE_SPLASH=1',
              flush=True)
        return 0
    prefix = target_prefix()
    if prefix is None:
        print('hide-league-splash-window: unusable DPREFIX: no action',
              file=sys.stderr, flush=True)
        return 1
    try:
        x11 = X11()
    except Exception as error:
        print(f'hide-league-splash-window: cannot open X11: {error}',
              file=sys.stderr, flush=True)
        return 1
    if args.once:
        windows = find_splash_windows(x11)
        x11.close()
        print('hide-league-splash-window: %d splash window(s)%s' % (
            len(windows), ''.join(f' 0x{window:x}' for window in windows)), flush=True)
        return 0
    if args.detach:
        try:
            os.setsid()
        except OSError:
            pass

    watcher = Watcher(prefix, x11)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    print('hide-league-splash-window: watching for the League splash window; '
          'opt out with RCLOL_HIDE_LEAGUE_SPLASH=0', flush=True)
    try:
        watcher.run()
    except KeyboardInterrupt:
        pass
    finally:
        x11.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
