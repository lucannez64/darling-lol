#!/usr/bin/env python3
"""Partially recover a live Darling runtime with a stale client tree.

This is opt-in through RCLOL_RECOVER_STALE_CLIENT=1. It only acts when all
of these are true:

- a darlingserver is running;
- no League game process is running;
- no managed Riot/League client window is visible;
- one or more Riot/League client, CEF or crash-handler processes at least 60
  seconds old are still present for this prefix.

It terminates only those client processes with SIGTERM/SIGKILL. The
darlingserver, launchd/mldr services, sockets and .init.pid stay intact so the
next launcher run can reuse the runtime. It never patches files or the game.
"""

import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

GAME_NAME = 'LeagueofLegends'
WINDOW_CLASSES = ('leagueclientux', 'riot client', 'riotclientservices')
MIN_AGE_SECONDS = 60
CLIENT_MARKERS = (
    '/Applications/League of Legends.app',
    '/Applications/Riot Client.app',
    '/Users/Shared/Riot Games/Riot Client.app',
    'RiotClientServices',
    'Riot Client',
    'LeagueClient',
    'LeagueClientUx',
    'LeagueCrashHandler',
)
RUNTIME_NAMES = ('darlingserver', 'darling')
SOCK_VARIABLE = b'__mldr_sockpath'
SOCK_NAME = '.darlingserver.sock'


def target_prefix():
    value = os.environ.get('DPREFIX', str(Path.home() / '.darling'))
    prefix = Path(value).expanduser()
    return prefix if prefix.is_absolute() else None


def command_names(proc):
    try:
        raw = (proc / 'cmdline').read_bytes()
    except OSError:
        return []
    return [
        os.path.basename(part.decode(errors='replace'))
        for part in raw.split(b'\0')
        if part
    ]


def command_text(proc):
    try:
        raw = (proc / 'cmdline').read_bytes()
    except OSError:
        return ''
    return b' '.join(part for part in raw.split(b'\0') if part).decode(
        errors='replace')


def process_state(proc):
    try:
        return (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def process_start_ticks(proc):
    """Field 22 of stat: start time in clock ticks, even for a zombie."""
    try:
        return int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def server_running(proc_root=Path('/proc')):
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False
    for proc in entries:
        if not proc.name.isdigit():
            continue
        try:
            if (proc / 'comm').read_text().strip() == 'darlingserver':
                return True
        except OSError:
            pass
    return False


def game_running(proc_root=Path('/proc')):
    for proc in proc_root.iterdir() if proc_root.exists() else ():
        if not proc.name.isdigit():
            continue
        names = command_names(proc)
        if GAME_NAME in names:
            return True
    return False


def visible_client_window():
    """Return True when a managed client window is present.

    A missing xprop/property or display is treated as 'cannot prove there is no
    window' so we do not kill a possibly working client. xprop exits zero and
    prints "not found" for a property the window manager did not set.
    """
    try:
        listing = subprocess.check_output(
            ['xprop', '-root', '_NET_CLIENT_LIST'], text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return True
    lowered = listing.lower()
    if '_net_client_list' not in lowered or 'not found' in lowered:
        return True
    for raw in re.findall(r'0x[0-9a-fA-F]+', listing):
        try:
            classes = subprocess.check_output(
                ['xprop', '-id', raw, 'WM_CLASS'], text=True, timeout=5
            ).lower()
        except (OSError, subprocess.SubprocessError):
            return True
        if 'not found' in classes or '=' not in classes:
            return True
        if any(name in classes for name in WINDOW_CLASSES):
            return True
    return False


def client_identity(prefix, pid, proc_root=Path('/proc')):
    """Start ticks of a live stale client for prefix, or None when not eligible."""
    proc = proc_root / str(pid)
    if process_state(proc) in ('Z', None):
        return None
    owner = (prefix / SOCK_NAME).parent.resolve()
    try:
        if proc.stat().st_uid != os.getuid():
            return None
        names = command_names(proc)
        if GAME_NAME in names or any(name in RUNTIME_NAMES for name in names):
            return None
        text = command_text(proc)
        if not any(marker in text for marker in CLIENT_MARKERS):
            return None
        environment = dict(
            item.split(b'=', 1)
            for item in (proc / 'environ').read_bytes().split(b'\0')
            if b'=' in item
        )
        socket = environment.get(SOCK_VARIABLE)
        if socket is None:
            return None
        if Path(os.fsdecode(socket)).parent.resolve() != owner:
            return None
        uptime = float((proc_root / 'uptime').read_text().split()[0])
        ticks = os.sysconf('SC_CLK_TCK')
        if not math.isfinite(uptime) or uptime < 0 or ticks <= 0:
            return None
        # Field 22 follows a comm field that may contain spaces/parentheses.
        start_ticks = process_start_ticks(proc)
        if start_ticks is None or start_ticks < 0 or uptime - start_ticks / ticks < MIN_AGE_SECONDS:
            return None
        return start_ticks
    except (OSError, ValueError, IndexError):
        return None


def client_pids(prefix, proc_root=Path('/proc')):
    """Client/CEF PIDs belonging to prefix, excluding Darling and the game."""
    found = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for proc in entries:
        if proc.name.isdigit() and client_identity(prefix, proc.name, proc_root) is not None:
            found.append(int(proc.name))
    return sorted(found)


def parent_pid(proc):
    """PPID from /proc/<pid>/stat, coping with a comm field with spaces."""
    try:
        return int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def termination_order(identities, proc_root=Path('/proc')):
    """Deepest processes first, so a live parent can reap its own children."""
    depth = {}

    def measure(pid, seen):
        if pid in depth:
            return depth[pid]
        if pid in seen:
            return 0
        seen.add(pid)
        parent = parent_pid(proc_root / str(pid))
        value = 0 if parent in (None, pid) or parent not in identities else measure(parent, seen) + 1
        depth[pid] = value
        return value

    for pid in identities:
        measure(pid, set())
    return sorted(identities, key=lambda pid: (-depth[pid], pid))


def _signal_tree(prefix, identities, order, signum, kill, proc_root):
    for pid in order:
        if client_identity(prefix, pid, proc_root) != identities[pid]:
            continue
        try:
            kill(pid, signum)
        except OSError:
            pass


def wait_for_exit(prefix, identities, proc_root=Path('/proc'), sleep=time.sleep,
                  attempts=5):
    """Wait briefly for the tree to leave the process table; return leftovers.

    A PID that was reused by an unrelated process is not a leftover. Only the
    original process, including while it is an unreaped zombie, counts.
    """
    leftovers = []
    for attempt in range(attempts):
        leftovers = [
            pid for pid, start_ticks in identities.items()
            if process_state(proc_root / str(pid)) is not None
            and process_start_ticks(proc_root / str(pid)) == start_ticks
        ]
        if not leftovers:
            break
        if attempt != attempts - 1:
            sleep(1)
    return leftovers


def recover(prefix, proc_root=Path('/proc'), window_check=visible_client_window,
            kill=os.kill, sleep=time.sleep, out=sys.stdout):
    if not server_running(proc_root):
        print('recover-stale-client: no darlingserver; normal startup', file=out, flush=True)
        return 0
    if game_running(proc_root):
        print('recover-stale-client: game running; no action', file=out, flush=True)
        return 0
    if window_check():
        print('recover-stale-client: client window present; no action', file=out, flush=True)
        return 0
    identities = {}
    for pid in client_pids(prefix, proc_root):
        start_ticks = client_identity(prefix, pid, proc_root)
        if start_ticks is not None:
            identities[pid] = start_ticks
    if not identities:
        print('recover-stale-client: no stale client processes; no action', file=out, flush=True)
        return 0
    if window_check():
        print('recover-stale-client: client window appeared; no action', file=out, flush=True)
        return 0
    print('recover-stale-client: stale client tree without a window; '
          'terminating %d process(es)' % len(identities), file=out, flush=True)
    order = termination_order(identities, proc_root)
    _signal_tree(prefix, identities, order, signal.SIGTERM, kill, proc_root)
    sleep(4)
    _signal_tree(prefix, identities, order, signal.SIGKILL, kill, proc_root)
    leftovers = wait_for_exit(prefix, identities, proc_root, sleep)
    if leftovers:
        details = [
            'PID %d state=%s parent=%s' % (
                pid, process_state(proc_root / str(pid)) or 'unknown',
                parent_pid(proc_root / str(pid)))
            for pid in leftovers
        ]
        print('recover-stale-client: %d process(es) still present after SIGKILL: %s' %
              (len(leftovers), '; '.join(details)), file=out, flush=True)
    return 0


def main():
    if os.environ.get('RCLOL_RECOVER_STALE_CLIENT') != '1':
        print('recover-stale-client: disabled; set RCLOL_RECOVER_STALE_CLIENT=1',
              flush=True)
        return 0
    prefix = target_prefix()
    if prefix is None:
        print('recover-stale-client: unusable DPREFIX; no action',
              file=sys.stderr, flush=True)
        return 1
    return recover(prefix)


if __name__ == '__main__':
    sys.exit(main())
