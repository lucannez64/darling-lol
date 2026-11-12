#!/usr/bin/env python3
"""Remove orphaned client-in-game helpers from an earlier launcher run.

The close/hide helpers are long-lived watch processes. A launcher can end
while they remain, and a later run starts another helper, so several copies
watch the same prefix. This opt-in preflight removes only helpers that belong
to this prefix and whose parent is init/systemd: the launcher that started
them has already exited and reparented them. It never signals a helper whose
parent is still alive, the current launcher, darlingserver, the game,
Riot/League/CEF, or a process of another prefix.

It fails closed: an unreadable command line, environment, stat entry or
parent leaves that process alone. Enable it with
RCLOL_CLEAN_STALE_HELPERS=1; the launcher runs it before starting its own
helper.
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

HELPER_NAMES = ('close-league-client-in-game.py',
                'hide-league-client-in-game.py',
                'hide-league-splash-window.py')
REAPER_NAMES = ('init', 'systemd')
TERM_GRACE = 3.0


def target_prefix():
    """The prefix this launcher owns, or None when it cannot be determined."""
    value = os.environ.get('DPREFIX', str(Path.home() / '.darling'))
    prefix = Path(value).expanduser()
    return prefix if prefix.is_absolute() else None


def process_info(proc):
    """Return (state, ppid, start_ticks), or None when stat is unreadable."""
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if len(fields) < 20:
            return None
        return fields[0], int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def parent_is_reaper(ppid, proc_root):
    """True when the parent is PID 1 or a systemd/init user manager."""
    if ppid == 1:
        return True
    try:
        comm = (proc_root / str(ppid) / 'comm').read_text().strip()
    except OSError:
        return False
    return comm in REAPER_NAMES


def helper_identity(proc, prefix, proc_root):
    """Return the start ticks of an orphaned helper of prefix, or None.

    The script name must be an exact argument, and the executable must be a
    Python interpreter. This avoids matching an editor or a log viewer that
    happens to have the helper name in its arguments. DPREFIX is inherited by
    the helper from run-riot-client.sh; a missing or different value belongs
    to another install and is preserved.
    """
    try:
        if proc.stat().st_uid != os.getuid():
            return None
        raw = (proc / 'cmdline').read_bytes()
        argv = [part.decode(errors='replace') for part in raw.split(b'\0') if part]
        if len(argv) < 2 or not os.path.basename(argv[0]).startswith(('python', 'pypy')):
            return None
        if not any(os.path.basename(arg) in HELPER_NAMES for arg in argv[1:]):
            return None
        environment = dict(
            item.split(b'=', 1)
            for item in (proc / 'environ').read_bytes().split(b'\0')
            if b'=' in item
        )
        raw_prefix = environment.get(b'DPREFIX')
        if raw_prefix is None:
            return None
        helper_prefix = Path(os.fsdecode(raw_prefix)).expanduser()
        if not helper_prefix.is_absolute():
            return None
        helper_prefix = helper_prefix.resolve()
        if helper_prefix != prefix:
            return None

        info = process_info(proc)
        if info is None or info[0] == 'Z':
            return None
        if not parent_is_reaper(info[1], proc_root):
            return None
        return info[2]
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, ValueError):
        return None


def clean(prefix, proc_root=Path('/proc'), kill=os.kill, sleep=time.sleep,
          out=sys.stdout, dry_run=False):
    """Terminate orphaned helpers for prefix; return 0 on every normal path."""
    if prefix is None or not prefix.is_absolute():
        print('clean-stale-helpers: unusable DPREFIX: no action', file=out, flush=True)
        return 1
    try:
        prefix = prefix.expanduser().resolve()
    except (OSError, RuntimeError):
        print('clean-stale-helpers: unusable DPREFIX: no action', file=out, flush=True)
        return 1
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        print('clean-stale-helpers: unreadable /proc: no action', file=out, flush=True)
        return 0

    helpers = {}
    for proc in entries:
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        start_ticks = helper_identity(proc, prefix, proc_root)
        if start_ticks is not None:
            helpers[int(proc.name)] = start_ticks
    if not helpers:
        print('clean-stale-helpers: no orphaned helpers; nothing to do',
              file=out, flush=True)
        return 0

    pids = sorted(helpers)
    action = 'would remove' if dry_run else 'removing'
    print('clean-stale-helpers: %s %d orphaned helper(s): %s'
          % (action, len(pids), ','.join(str(pid) for pid in pids)),
          file=out, flush=True)
    if dry_run:
        return 0

    for pid, start_ticks in helpers.items():
        proc = proc_root / str(pid)
        if helper_identity(proc, prefix, proc_root) != start_ticks:
            continue
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            continue
    sleep(TERM_GRACE)
    for pid, start_ticks in helpers.items():
        proc = proc_root / str(pid)
        if helper_identity(proc, prefix, proc_root) != start_ticks:
            continue
        try:
            kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prefix', type=Path,
                        help='prefix whose helpers should be checked')
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would be removed without signalling')
    args = parser.parse_args()
    if os.environ.get('RCLOL_CLEAN_STALE_HELPERS') != '1':
        print('clean-stale-helpers: disabled; set RCLOL_CLEAN_STALE_HELPERS=1',
              flush=True)
        return 0
    prefix = args.prefix or target_prefix()
    if prefix is None or not prefix.is_absolute():
        print('clean-stale-helpers: unusable DPREFIX: no action',
              file=sys.stderr, flush=True)
        return 1
    return clean(prefix, dry_run=args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
