#!/usr/bin/env python3
"""Supervise one guest session so a launcher exit does not orphan it.

The launcher runs this script instead of exec'ing the guest client directly. It
forwards SIGINT/SIGTERM/SIGHUP to the child, and after the child exits it asks
darlingserver to shut down and stops any process still inside the guest PID
namespace. That covers the two ways a session leaks: the Riot Client stays
resident after its window closes, and a killed loader leaves launchd/mldr and
the client tree behind.

The guest namespace is captured while the session runs, from a process that
carries this prefix's `__mldr_sockpath`; without that evidence cleanup never
touches a process. If the namespace cannot be read, only processes that carry
the socket are stopped. Cleanup only runs when this launcher started the
runtime: a second launch that attaches to an existing server leaves it alone
when its own loader exits.

Optional idle stop: with RCLOL_STOP_AFTER_CLIENT_SECONDS=N, the supervisor
stops the session once a managed League/Riot window has been absent for N
seconds and no game process is running. It is off by default and never stops
when the window state cannot be read.

A failed client exit is reported with its status, and, when --guest-log names
the client's log directory, with the last available line of its newest log file, labelled as possibly historical. That
turns a bare "runtime and guest processes are gone" into the reason the guest
died, such as a dyld "Library not loaded" abort.

League Exit: LeagueClientUx 16.22 loses its link to LeagueClient early in a
session, so its Exit button and window close only log "Failed to send quit
message because there was no connection" and the client stays open. When a
new line like that appears, the supervisor asks LeagueClient to quit over its
local API and then stops the League client processes that are left. It never
touches a running game or the Riot Client. RCLOL_FINISH_LEAGUE_EXIT=0 turns
this off.

League splash: the same early link drop can also lose the UX's "loaded" report,
and LeagueClient then keeps its 512x216 splash on screen. When the LeagueClient
log shows UX_PROCESS_BOOTSTRAP_COMPLETE without "Ux reports successful load"
3 seconds later, the supervisor delivers that report over the local API
(PUT /riotclient/ux-load-complete), and LeagueClient hides the splash itself.
RCLOL_FINISH_LEAGUE_LOAD=0 turns this off.

Toolbox: when the project is installed with `--toolbox`, the launcher runs
inside a Fedora Toolbox container that `toolbox run` leaves running after the
command exits. The supervisor therefore stops its own host container as the
last cleanup step, once the guest session is gone. Sessions that share the
container register with each other, so only the last one stops it. A container
started from an interactive `toolbox enter` shell is left alone, because the
shell that runs the launcher would end with it. RCLOL_STOP_TOOLBOX=0 turns this
off.

Set RCLOL_CLEAN_ON_EXIT=0 to keep the previous behaviour: the runtime is left
alone when the launcher exits.

Usage (from run-riot-client.sh):

    python3 session-supervisor.py --prefix ~/.darling --darling /path/darling \
        -- python3 exec-guest.py env ... darling ...
"""
import argparse
import fcntl
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SOCK_VARIABLE = b'__mldr_sockpath'
SOCK_NAME = '.darlingserver.sock'
GAME_NAME = 'LeagueofLegends'
CLIENT_CLASSES = ('leagueclientux', 'riot client')
POLL_SECONDS = 2.0
TERM_GRACE = 5.0
KILL_GRACE = 5.0
# Toolbox leaves the first marker inside its containers; Podman writes the
# engine metadata, including the container ID that names its systemd scope, to
# the second one. A shell that started the launcher means `toolbox enter`: the
# container belongs to that interactive session, not to this launch.
TOOLBOX_MARKER = Path('/run/.toolboxenv')
CONTAINER_ENV = Path('/run/.containerenv')
INTERACTIVE_SHELLS = ('bash', 'dash', 'fish', 'ksh', 'sh', 'zsh')
# Sessions that share one Toolbox container register below a per-container
# directory and hold a lock for their lifetime, so only the last one stops it.
SESSION_PREFIX = 'session-'
REGISTRY_LOCK = 'registry.lock'
STOP_REQUESTED = 'stop-requested'
STOP_QUEUED = 'stop-queued'
TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def log(message, out=sys.stdout):
    """Best-effort output; a closed terminal must not abort cleanup."""
    try:
        print('session-supervisor: %s' % message, file=out, flush=True)
    except (OSError, ValueError):
        pass


def process_info(proc):
    """Return (state, ppid, start_ticks), or None when stat is unreadable."""
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if len(fields) < 20:
            return None
        return fields[0], int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def command_names(proc):
    try:
        raw = (proc / 'cmdline').read_bytes()
    except OSError:
        return []
    return [os.path.basename(part.decode(errors='replace'))
            for part in raw.split(b'\0') if part]


def command_line(proc):
    """Raw argv as text, without basenaming, for interpreter/path checks."""
    try:
        raw = (proc / 'cmdline').read_bytes()
    except OSError:
        return []
    return [os.fsdecode(part) for part in raw.split(b'\0') if part]


def environment(proc):
    try:
        return dict(
            item.split(b'=', 1)
            for item in (proc / 'environ').read_bytes().split(b'\0')
            if b'=' in item
        )
    except OSError:
        return {}


def socket_prefix(proc):
    """The prefix named by a process's loader socket, or None."""
    raw = environment(proc).get(SOCK_VARIABLE)
    if raw is None:
        return None
    try:
        path = Path(os.fsdecode(raw))
        return path.parent.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def owned_by_prefix(proc, prefix):
    if process_info(proc) is None:
        return False
    try:
        if proc.stat().st_uid != os.getuid():
            return False
    except OSError:
        return False
    return socket_prefix(proc) == prefix


def prefix_namespace(prefix, proc_root=Path('/proc')):
    """The guest PID namespace, proved by a process carrying this socket."""
    for proc in proc_root.iterdir() if proc_root.exists() else ():
        if not proc.name.isdigit():
            continue
        if owned_by_prefix(proc, prefix):
            try:
                return os.readlink(proc / 'ns/pid')
            except OSError:
                return None
    return None


def namespace_members(namespace, proc_root=Path('/proc'), own_namespace=None):
    """Processes inside namespace, excluding this host process.

    A namespace equal to the supervisor's own is refused: a mistaken capture
    must never turn session cleanup into a host-wide kill.
    """
    if not namespace or namespace == own_namespace:
        return []
    found = []
    for proc in proc_root.iterdir() if proc_root.exists() else ():
        if not proc.name.isdigit():
            continue
        try:
            if os.readlink(proc / 'ns/pid') != namespace:
                continue
            if proc.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        found.append((int(proc.name), proc))
    return found


def prefix_only(prefix, proc_root=Path('/proc')):
    """Fallback: processes that positively carry this prefix's socket."""
    found = []
    for proc in proc_root.iterdir() if proc_root.exists() else ():
        if proc.name.isdigit() and owned_by_prefix(proc, prefix):
            found.append((int(proc.name), proc))
    return found


def deepest_first(members, proc_root=Path('/proc')):
    """Children before parents, so live parents can reap their own tree."""
    parents = {}
    for pid, proc in members:
        info = process_info(proc)
        parents[pid] = info[1] if info else None
    known = {pid for pid, _proc in members}

    def depth(pid, seen):
        parent = parents.get(pid)
        if parent not in known or parent == pid or parent in seen:
            return 0
        return depth(parent, seen | {pid}) + 1

    return sorted((pid for pid, _proc in members),
                  key=lambda pid: (-depth(pid, set()), pid))


def wrapper(proc, name):
    """Return (state, comm, ppid) for a /proc entry, or None."""
    try:
        state = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        comm = (proc / 'comm').read_text().strip()
        parent = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        return state, comm, parent
    except (OSError, ValueError, IndexError):
        return None


def process_identity(proc):
    """Identity captured before shutdown, not just a reusable PID."""
    info = process_info(proc)
    if info is None:
        return None
    try:
        return info[2], os.readlink(proc / 'ns/pid'), proc.stat().st_uid
    except OSError:
        return None


def signal_process(pid, proc, identity, signum, kill=os.kill):
    """Pin the process before the final identity check to close the PID reuse race."""
    if identity is None or process_identity(proc) != identity:
        return
    if kill is not os.kill:
        # Injected signal transport for offline process fixtures.
        kill(pid, signum)
        return
    try:
        descriptor = os.pidfd_open(pid)
    except (AttributeError, OSError):
        # Never fall back to signalling an unpinned PID.
        return
    try:
        if process_identity(proc) == identity:
            signal.pidfd_send_signal(descriptor, signum)
    except (AttributeError, OSError):
        pass
    finally:
        os.close(descriptor)


def stop_processes(members, kill=os.kill, sleep=time.sleep, proc_root=Path('/proc'),
                   out=sys.stdout, term_grace=TERM_GRACE, kill_grace=KILL_GRACE,
                   clock=time.monotonic, identities=None):
    """SIGTERM then SIGKILL a set of processes; return what is still present."""
    if identities is None:
        identities = {pid: process_identity(proc) for pid, proc in members}

    def present(pid, proc):
        return (identities.get(pid) is not None
                and process_identity(proc) == identities[pid])

    def alive(pid, proc):
        info = process_info(proc)
        return present(pid, proc) and info is not None and info[0] != 'Z'

    processes = dict(members)
    remaining = [(pid, proc) for pid, proc in members if alive(pid, proc)]
    for pid in deepest_first(remaining, proc_root):
        try:
            signal_process(pid, processes[pid], identities[pid], signal.SIGTERM, kill)
        except OSError:
            pass
    deadline = clock() + term_grace
    while remaining and clock() < deadline:
        sleep(POLL_SECONDS)
        remaining = [(pid, proc) for pid, proc in remaining if alive(pid, proc)]
    # Re-read once before escalating: a zero grace period must not SIGKILL a
    # process that already exited on SIGTERM.
    remaining = [(pid, proc) for pid, proc in remaining if alive(pid, proc)]
    for pid in deepest_first(remaining, proc_root):
        try:
            signal_process(pid, processes[pid], identities[pid], signal.SIGKILL, kill)
        except OSError:
            pass
    deadline = clock() + kill_grace
    while remaining and clock() < deadline:
        sleep(POLL_SECONDS)
        remaining = [(pid, proc) for pid, proc in remaining
                     if present(pid, proc)]
    # Re-read once even when a grace period is zero, so the caller never gets a
    # stale "still running" answer for a process that SIGKILL already removed.
    return [(pid, proc) for pid, proc in remaining
            if present(pid, proc)]


def cleanup(prefix, darling, namespace, proc_root=Path('/proc'), run=subprocess.run,
            kill=os.kill, sleep=time.sleep, out=sys.stdout, own_namespace=None,
            term_grace=TERM_GRACE, kill_grace=KILL_GRACE, clock=time.monotonic,
            note=''):
    """Shut down the runtime, then stop anything left in the guest namespace."""
    if namespace is None:
        namespace = prefix_namespace(prefix, proc_root)
    own_namespace = own_namespace or os.readlink('/proc/self/ns/pid')
    members = namespace_members(namespace, proc_root, own_namespace)
    identities = {pid: process_identity(proc) for pid, proc in members}
    if members and darling:
        try:
            run([darling, 'shutdown'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=25)
        except (OSError, subprocess.SubprocessError):
            pass
        deadline = clock() + term_grace
        while members and clock() < deadline:
            sleep(POLL_SECONDS)
            members = [(pid, proc) for pid, proc in members
                       if identities.get(pid) is not None
                       and process_identity(proc) == identities[pid]]
    if not members:
        members = prefix_only(prefix, proc_root)
        identities = {pid: process_identity(proc) for pid, proc in members}
    if not members:
        log('runtime and guest processes are gone%s' % note, out)
        return []
    log('stopping %d leftover guest process(es)' % len(members), out)
    remaining = stop_processes(members, kill=kill, sleep=sleep,
                               proc_root=proc_root, out=out, term_grace=term_grace,
                               kill_grace=kill_grace, clock=clock, identities=identities)
    if remaining:
        details = []
        for pid, proc in remaining:
            info = wrapper(proc, proc_root)
            details.append('%d (%s, %s)' % (pid, info[1] if info else '?',
                                            info[0] if info else '?'))
        log('still present: %s' % '; '.join(details), out)
    else:
        log('guest session stopped', out)
    return remaining


def container_metadata(marker=TOOLBOX_MARKER, containerenv=CONTAINER_ENV):
    """(name, id) of the Toolbox container hosting this process, or None.

    Only a Toolbox container qualifies: `/run/.containerenv` exists in every
    Podman container, but a native install inside an ordinary container must
    not stop it. The ID is validated as hexadecimal because it becomes part of
    a systemd unit name.
    """
    try:
        if not marker.exists():
            return None
        lines = containerenv.read_text(errors='replace').splitlines()
    except OSError:
        return None
    values = {}
    for line in lines:
        key, separator, value = line.partition('=')
        if separator:
            values[key.strip()] = value.strip().strip('"')
    container_id = values.get('id', '').lower()
    if not container_id or any(char not in '0123456789abcdef' for char in container_id):
        return None
    return values.get('name') or container_id, container_id


def started_by_shell(proc_root=Path('/proc'), pid=None):
    """True when a shell launched this process, as `toolbox enter` does.

    `toolbox run` starts the launcher as the container's exec command, so the
    supervisor is the direct child of the exec session. A shell parent means a
    human owns the container: stopping it would close that shell too.
    """
    info = process_info(proc_root / str(os.getpid() if pid is None else pid))
    if info is None or info[1] <= 1:
        return False
    names = command_names(proc_root / str(info[1]))
    if not names:
        return False
    # A login shell is exposed as `-bash` in argv[0]; the leading hyphen marks
    # the login mode and is not part of the program name.
    return os.path.basename(names[0]).lstrip('-') in INTERACTIVE_SHELLS


def ignore_termination_signals():
    """Ignore termination signals; return the handlers to restore.

    A queued `systemctl --user stop` reaches every process in the container's
    systemd scope, this supervisor included. Once the stop is queued, ignoring
    those signals lets the supervisor exit normally, so its child's exit status
    reaches the launcher instead of a signal status.
    """
    previous = {}
    for signum in TERMINATION_SIGNALS:
        try:
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    return previous


def restore_termination_signals(previous):
    """Put back the handlers saved by ignore_termination_signals()."""
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def stop_container(container, run=subprocess.run, out=sys.stdout, timeout=10):
    """Stop this session's Toolbox container as the last cleanup step.

    `podman stop` is unavailable inside the Toolbox image, and the container
    lives in a host systemd user scope named after its ID. `--no-block` queues
    the stop and returns: the scope contains this process, so the supervisor
    ends with the container. Termination signals are ignored from the moment
    the stop is queued, because the queued stop also signals this process;
    otherwise it would die before returning the child's exit status. A stop
    that was not queued restores the previous handlers.
    """
    name, container_id = container
    unit = 'libpod-%s.scope' % container_id
    previous = ignore_termination_signals()
    try:
        result = run(['systemctl', '--user', '--no-block', 'stop', unit],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        restore_termination_signals(previous)
        log('could not stop Toolbox container %s (%s); stop it with '
            "'podman stop %s'" % (name, error, name), out)
        return False
    status = getattr(result, 'returncode', 0)
    if status != 0:
        restore_termination_signals(previous)
        log('could not stop Toolbox container %s (systemctl exited with %d); '
            "stop it with 'podman stop %s'" % (name, status, name), out)
        return False
    log('stopping Toolbox container %s' % name, out)
    return True


def session_registry(container_id, base=None):
    """Directory where the sessions sharing one Toolbox container register."""
    root = Path('/run/user') / str(os.getuid()) if base is None else Path(base)
    return root / 'darling-lol-toolbox' / container_id


def in_container_scope(proc, unit):
    try:
        return any(line.startswith('0::') and unit in Path(line[3:]).parts
                   for line in (proc / 'cgroup').read_text().splitlines())
    except OSError:
        return False


def toolbox_state(container_id, proc_root):
    """Return the live Toolbx init identity and whether its scope is idle."""
    unit = 'libpod-%s.scope' % container_id
    if not in_container_scope(proc_root / 'self', unit):
        return None, False
    instance = None
    idle = True
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        if not in_container_scope(proc, unit):
            continue
        info = process_info(proc)
        if info is not None and info[0] in ('Z', 'X'):
            continue
        args = command_line(proc)
        if (info is not None and args and Path(args[0]).name == 'toolbox'
                and 'init-container' in args[1:]):
            # Both the mount namespace inode and PID can be reused at restart.
            instance = '%s:%d' % (proc.name, info[2])
            continue
        # Other supervisors serialize through the registry. A finisher can
        # still be returning its status after it removes its registration.
        if (len(args) > 1 and Path(args[0]).name.startswith('python')
                and Path(args[1]).name == 'session-supervisor.py'):
            continue
        idle = False
    return instance, idle


class ContainerStopping(Exception):
    """A previous supervisor already queued a stop for this instance."""


class ContainerSessions:
    """Registration of the sessions that share one Toolbox container.

    `toolbox run` leaves the container running, but several launches can use
    the same container. Each supervisor registers before starting its child
    and holds an advisory lock on its own file for the session's lifetime, so
    the session that ends last is the one that stops the container. The final
    stop decision and unregistering use the same lock. A finished runtime
    owner can authorize the last attached session to stop the container.
    The Toolbx init PID and start time identify each running instance.
    """

    def __init__(self, container_id, base=None, proc_root=Path('/proc')):
        self.container_id = container_id
        self.directory = session_registry(container_id, base)
        self.proc_root = proc_root
        self.path = None
        self.handle = None

    def register(self):
        """Register this session; False when the registry is unusable."""
        handle = None
        try:
            self.directory.parent.mkdir(mode=0o700, exist_ok=True)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            with open(self.directory / REGISTRY_LOCK, 'a+') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                queued = self.directory / STOP_QUEUED
                if queued.exists():
                    instance, _idle = toolbox_state(self.container_id, self.proc_root)
                    if instance is None:
                        return False
                    if queued.read_text() == instance:
                        raise ContainerStopping
                    queued.unlink()
                pending = self.directory / STOP_REQUESTED
                if pending.exists() and not self.active_sessions():
                    pending.unlink()
                handle = tempfile.NamedTemporaryFile(
                    prefix=SESSION_PREFIX, dir=str(self.directory), delete=False)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            if handle is not None:
                handle.close()
            return False
        self.handle, self.path = handle, Path(handle.name)
        return True

    def finish(self, container, *, request_stop, stoppable,
               run=subprocess.run, out=sys.stdout):
        """Stop `container` only when no other session is still using it."""
        if self.path is None:
            return False
        try:
            with open(self.directory / REGISTRY_LOCK, 'a+') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self.release()
                pending = self.directory / STOP_REQUESTED
                if request_stop:
                    pending.touch()
                if self.active_sessions():
                    return False
                if not pending.exists():
                    return False
                pending.unlink()
                instance, idle = toolbox_state(self.container_id, self.proc_root)
                if not stoppable or instance is None or not idle:
                    log('leaving Toolbox container %s running' % container[0], out)
                    return False
                queued = self.directory / STOP_QUEUED
                queued.write_text(instance)
                if stop_container(container, run=run, out=out):
                    return True
                queued.unlink()
                return False
        except OSError as error:
            log('could not read the Toolbox session registry (%s); leaving the '
                "container running; stop it with 'podman stop %s'"
                % (error, container[0]), out)
            return False
        finally:
            self.release()

    def active_sessions(self):
        """Registered sessions, other than this one, that still hold a lock."""
        active = []
        for entry in sorted(self.directory.iterdir()):
            if entry == self.path or not entry.name.startswith(SESSION_PREFIX):
                continue
            try:
                fd = os.open(entry, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    active.append(entry.name)
                else:
                    try:
                        entry.unlink()  # the session that owned it is gone
                    except OSError:
                        pass
            finally:
                os.close(fd)
        return active

    def release(self):
        """Forget this session; leftovers are removed by the next check."""
        handle, self.handle = self.handle, None
        path, self.path = self.path, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def newest_guest_log(directory, pattern='*.log'):
    """Newest log file below directory, or None when there is none."""
    if not directory:
        return None
    try:
        candidates = [path for path in Path(directory).rglob(pattern)
                      if path.is_file()]
        return max(candidates, key=lambda path: path.stat().st_mtime)
    except (OSError, ValueError):
        return None


def last_log_line(path, chunk=65536):
    """Last non-blank line of a log file, or None when unreadable."""
    if path is None:
        return None
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - chunk))
            lines = handle.read().decode(errors='replace').splitlines()
    except (OSError, ValueError):
        return None
    for line in reversed(lines):
        line = line.strip()
        if line:
            return line[:300]
    return None


def runtime_alive(prefix, proc_root=Path('/proc')):
    """True when this prefix's darlingserver marker names a live server.

    Used to tell "we started the runtime" from "we attached to one that another
    launcher already owns". Cleanup must never end the second kind.
    """
    try:
        pid = int((prefix / '.init.pid').read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    proc = proc_root / str(pid)
    info = process_info(proc)
    if info is None or info[0] == 'Z':
        return False
    argv = command_line(proc)
    if not argv or os.path.basename(argv[0]) != 'darlingserver':
        return False
    try:
        return (proc.stat().st_uid == os.getuid() and len(argv) > 1
                and Path(argv[1]).resolve() == prefix)
    except OSError:
        return False


def managed_client_window(run=subprocess.run):
    """True/False for a managed client window; None when it cannot be read."""
    try:
        listing = run(['xprop', '-root', '_NET_CLIENT_LIST'], text=True,
                      capture_output=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lowered = listing.lower()
    if '_net_client_list' not in lowered or 'not found' in lowered:
        return None
    for raw in set(re.findall(r'0x[0-9a-fA-F]+', listing)):
        try:
            classes = run(['xprop', '-id', raw, 'WM_CLASS'], text=True,
                          capture_output=True, timeout=5).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return None
        if 'not found' in classes or '=' not in classes:
            return None
        if any(name in classes for name in CLIENT_CLASSES):
            return True
    return False


def game_running(prefix, proc_root=Path('/proc')):
    """True/False for a game of this prefix; None when /proc cannot prove it."""
    if not proc_root.exists():
        return None
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        if GAME_NAME not in command_names(proc):
            continue
        if socket_prefix(proc) == prefix:
            return True
    return False


def idle_decision(seen_client, idle_since, now, windows, game, seconds):
    """Return (seen_client, idle_since, stop_now) for one poll."""
    if seconds <= 0 or windows is None or game is None:
        return seen_client, None, False
    if game or windows:
        return seen_client or game or windows, None, False
    if not seen_client:
        return False, None, False
    if idle_since is None:
        return True, now, False
    return True, idle_since, now - idle_since >= seconds


LEAGUE_LOL = 'Applications/League of Legends.app/Contents/LoL'
LEAGUE_APP_PATH = '/League of Legends.app/'
LEAGUE_UX_PATH = '/LoL/League of Legends.app/'
QUIT_MARKER = b'Failed to send quit message because there was no connection'
LEAGUE_QUIT_GRACE = 8.0


class LeagueLogTail:
    """New complete lines of the League client logs matching one pattern.

    Lines already in a log when the supervisor starts are history and are
    never returned.
    """

    pattern = '*.log'

    def __init__(self, prefix):
        self.directory = prefix / LEAGUE_LOL / 'Logs/LeagueClient Logs'
        self.offsets = {path: size for path, size in self._logs()}

    def _logs(self):
        try:
            for path in self.directory.glob(self.pattern):
                yield path, path.stat().st_size
        except OSError:
            return

    def new_lines(self):
        """Complete lines appended since the last call, in file order."""
        lines = []
        for path, size in list(self._logs()):
            offset = self.offsets.get(path, 0)
            if size < offset:
                offset = 0
            if size == offset:
                continue
            try:
                with open(path, 'rb') as handle:
                    handle.seek(offset)
                    data = handle.read(size - offset)
            except OSError:
                continue
            complete = data.rfind(b'\n') + 1
            self.offsets[path] = offset + complete
            lines.extend(data[:complete].splitlines())
        return lines


class LeagueExitWatch(LeagueLogTail):
    """Spot a League client Exit that the client itself cannot deliver.

    LeagueClientUx 16.22 drops its link to LeagueClient (close code 1003,
    "Unsupported Data Type") early in the session. From then on Exit and the
    window close button only log QUIT_MARKER and the client stays open.
    """

    pattern = '*_LeagueClientUx.log'

    def poll(self):
        """True when a complete new log line carries QUIT_MARKER."""
        return any(QUIT_MARKER in line for line in self.new_lines())


BOOTSTRAP_MARKER = b'UX_PROCESS_BOOTSTRAP_COMPLETE'
LOADED_MARKER = b'Ux reports successful load'
LEAGUE_LOAD_GRACE = 3.0


class LeagueLoadWatch(LeagueLogTail):
    """Spot a League client UX load report that never reached LeagueClient.

    LeagueClient hides its 512x216 splash only when the UX reports a
    successful load, which normally follows UX_PROCESS_BOOTSTRAP_COMPLETE
    within a second. When the UX drops its link (close code 1003) before
    sending that report, the splash stays on screen for the whole session.
    """

    pattern = '*_LeagueClient.log'

    def __init__(self, prefix, clock=time.monotonic, grace=LEAGUE_LOAD_GRACE):
        super().__init__(prefix)
        self.clock = clock
        self.grace = grace
        self.waiting_since = None

    def poll(self):
        """True once when a bootstrapped UX has not reported its load in time."""
        for line in self.new_lines():
            if BOOTSTRAP_MARKER in line:
                self.waiting_since = self.clock()
            elif LOADED_MARKER in line:
                self.waiting_since = None
        if self.waiting_since is None or self.clock() - self.waiting_since < self.grace:
            return False
        self.waiting_since = None
        return True


def league_client_processes(prefix, namespace, proc_root=Path('/proc'), own_namespace=None):
    """League client processes of this session, never the game itself."""
    return [(pid, proc) for pid, proc in session_members(prefix, namespace, proc_root, own_namespace)
            if any(LEAGUE_APP_PATH in part for part in command_line(proc))
            and GAME_NAME not in command_names(proc)]


def session_members(prefix, namespace, proc_root=Path('/proc'), own_namespace=None):
    """Guest processes of this session; never the supervisor's own namespace."""
    if own_namespace is None:
        try:
            own_namespace = os.readlink('/proc/self/ns/pid')
        except OSError:
            return []
    if namespace:
        return namespace_members(namespace, proc_root, own_namespace)
    return prefix_only(prefix, proc_root)


def league_game_present(prefix, namespace, proc_root=Path('/proc'), own_namespace=None):
    """True unless /proc proves no game runs; unknown counts as running."""
    if any(GAME_NAME in command_names(proc)
           for _pid, proc in session_members(prefix, namespace, proc_root, own_namespace)):
        return True
    return game_running(prefix, proc_root) is not False


def request_league_quit(prefix, urlopen=None):
    """Ask LeagueClient to quit over its local API; False when that fails."""
    return league_api(prefix, 'POST', '/process-control/v1/process/quit', urlopen)


def report_league_load(prefix, out=sys.stdout, urlopen=None):
    """Deliver the UX load report LeagueClient never received."""
    sent = league_api(prefix, 'PUT', '/riotclient/ux-load-complete', urlopen)
    log('League UX load report was lost; %s' % (
        'delivered it over the local API' if sent else 'local API request failed'), out)
    return sent


def league_api(prefix, method, path, urlopen=None):
    """One request to LeagueClient's local API; False when that fails."""
    import base64
    import ssl
    import urllib.request
    try:
        fields = (prefix / LEAGUE_LOL / 'lockfile').read_text().strip().split(':')
        port, password, scheme = int(fields[2]), fields[3], fields[4]
    except (OSError, ValueError, IndexError):
        return False
    token = base64.b64encode(('riot:%s' % password).encode()).decode()
    # The client serves its own self-signed certificate on loopback.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        request = urllib.request.Request(
            '%s://127.0.0.1:%d%s' % (scheme, port, path),
            data=b'', method=method, headers={'Authorization': 'Basic ' + token})
        (urlopen or urllib.request.urlopen)(request, timeout=3, context=context).close()
        return True
    except Exception:  # noqa: BLE001 - any failure falls through to stopping
        return False


def finish_league_exit(prefix, namespace, proc_root=Path('/proc'), kill=os.kill,
                       sleep=time.sleep, out=sys.stdout, own_namespace=None,
                       urlopen=None, clock=time.monotonic, grace=LEAGUE_QUIT_GRACE):
    """Complete a League Exit: API quit first, then stop what is left."""
    if league_game_present(prefix, namespace, proc_root, own_namespace):
        log('League Exit failed inside the client, but a game is running; leaving it', out)
        return []
    members = league_client_processes(prefix, namespace, proc_root, own_namespace)
    if not members:
        return []
    identities = {pid: process_identity(proc) for pid, proc in members}
    asked = request_league_quit(prefix, urlopen)
    log('League Exit could not reach LeagueClient; %s' % (
        'asked it to quit over its local API' if asked else 'stopping the League client'), out)

    def alive():
        return [(pid, proc) for pid, proc in members
                if identities[pid] is not None and process_identity(proc) == identities[pid]
                and (process_info(proc) or ('Z',))[0] != 'Z']

    if asked:
        # LeagueClient now waits for a UX that cannot hear it; left alone, the
        # UX shows "You've lost connection" until the grace period ends. Stop
        # the UX tree so LeagueClient can finish its own shutdown.
        ux = [(pid, proc) for pid, proc in alive()
              if any(LEAGUE_UX_PATH in part for part in command_line(proc))]
        if ux:
            stop_processes(ux, kill=kill, sleep=sleep, proc_root=proc_root, out=out,
                           clock=clock, identities=identities)
    deadline = clock() + (grace if asked else 0)
    while alive() and clock() < deadline:
        sleep(POLL_SECONDS)
    remaining = alive()
    if remaining:
        remaining = stop_processes(remaining, kill=kill, sleep=sleep, proc_root=proc_root,
                                   out=out, clock=clock, identities=identities)
    log('League client closed' if not remaining else
        'League client processes still present: %s' % ' '.join(str(pid) for pid, _ in remaining), out)
    return remaining


def run_supervised(command, prefix, darling, stop_after=0, clean=True,
                   proc_root=Path('/proc'), run=subprocess.run, kill=os.kill,
                   sleep=time.sleep, out=sys.stdout, poll=POLL_SECONDS,
                   own_namespace=None, runtime_at_start=None, guest_log=None,
                   league_exit=None, league_load=None, container=None,
                   stoppable=True, session_base=None):
    """Run command, forward termination signals, then clean the session.

    Cleanup only runs when this launcher started the runtime. A second launch
    while a session is alive attaches to that server, and must not shut it down
    when its own loader exits. `container` names the Toolbox container this
    session shares, and `stoppable` says whether the container may be stopped
    when this is the last session using it; both stay unused for a native
    session.
    """
    if not clean:
        os.execvp(command[0], command)
    owns_runtime = (runtime_at_start == 'stopped' if runtime_at_start is not None
                    else not runtime_alive(prefix, proc_root))
    sessions = None
    if container is not None:
        sessions = ContainerSessions(container[1], base=session_base, proc_root=proc_root)
        try:
            registered = sessions.register()
        except ContainerStopping:
            log('Toolbox container %s is stopping; retry the launch' % container[0], out)
            return 1
        if not registered:
            log('could not register this session for Toolbox container %s; '
                'it will be left running' % container[0], out)
    namespace = None
    try:
        child = subprocess.Popen(command, start_new_session=True)
    except OSError:
        if sessions is not None:
            sessions.release()
        raise
    def signal_child(signum):
        try:
            if os.getpgid(child.pid) != child.pid:
                return
            if os.getsid(child.pid) != child.pid:
                return
            os.killpg(child.pid, signum)
        except OSError:
            pass
    stop = []
    previous = {}

    def forward(signum, _frame):
        stop.append(signum)
        signal_child(signal.SIGTERM)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.signal(signum, forward)
    seen_client = False
    idle_since = None
    if league_exit is None:
        league_exit = os.environ.get('RCLOL_FINISH_LEAGUE_EXIT', '1') != '0'
    exit_watch = LeagueExitWatch(prefix) if league_exit else None
    if league_load is None:
        league_load = os.environ.get('RCLOL_FINISH_LEAGUE_LOAD', '1') != '0'
    load_watch = LeagueLoadWatch(prefix) if league_load else None
    try:
        while child.poll() is None and not stop:
            # Learn the guest namespace while the session runs: after a crash
            # the processes that still carry the socket may be the only proof.
            if namespace is None:
                namespace = prefix_namespace(prefix, proc_root)
            if exit_watch is not None and exit_watch.poll():
                finish_league_exit(prefix, namespace, proc_root=proc_root, kill=kill,
                                   sleep=sleep, out=out, own_namespace=own_namespace)
            if load_watch is not None and load_watch.poll():
                report_league_load(prefix, out=out)
            if stop_after > 0:
                now = time.monotonic()
                seen_client, idle_since, stop_now = idle_decision(
                    seen_client, idle_since, now, managed_client_window(run),
                    league_game_present(prefix, namespace, proc_root, own_namespace),
                    stop_after)
                if stop_now:
                    log('client window has been gone for %ds; stopping the session'
                        % stop_after, out)

                    shutdown_requested = False

                    if owns_runtime and darling:
                        try:
                            result = run(
                                [darling, 'shutdown'],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=25
                            )
                            shutdown_requested = getattr(result, 'returncode', 0) == 0
                        except (OSError, subprocess.SubprocessError):
                            pass

                    if shutdown_requested:
                        log('Darling shutdown requested successfully', out)
                    else:
                        log('Darling shutdown failed; signalling launcher', out)
                        signal_child(signal.SIGTERM)

                    break
            sleep(poll)
        deadline = time.monotonic() + TERM_GRACE
        while child.poll() is None and time.monotonic() < deadline:
            sleep(poll)
        if child.poll() is None:
            signal_child(signal.SIGKILL)
        code = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    status = code if code >= 0 else 128 - code
    if status != 0:
        # A crash used to look like a clean shutdown: say that the client
        # failed, and quote its newest log line so the cause is visible.
        log('client exited with status %d' % status, out)
        line = last_log_line(newest_guest_log(guest_log))
        if line:
            log('last available guest log line (possibly from an earlier session): %s' % line, out)
    if not owns_runtime:
        log('another session owns this runtime; leaving it running', out)
        remaining = []
    else:
        namespace = namespace or prefix_namespace(prefix, proc_root)
        note = '' if status == 0 else ' (client exited with status %d)' % status
        remaining = cleanup(prefix, darling, namespace, proc_root=proc_root, run=run,
                            kill=kill, sleep=sleep, out=out,
                            own_namespace=own_namespace, note=note)
    if stop:
        log('interrupted by signal %d' % stop[0], out)
    if sessions is not None and sessions.path is not None:
        # A different finisher can stop the scope as soon as we unregister.
        # This process has finished its work and is about to return the status.
        ignore_termination_signals()
        sessions.finish(container, request_stop=owns_runtime and stoppable,
                        stoppable=stoppable, run=run, out=out)
    # A surviving guest process is a failed cleanup, not a clean exit.
    return (status or 1) if remaining else status


def env_seconds(name, out=sys.stderr):
    """Seconds from an optional environment variable; 0 when unset or invalid."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        log('ignoring invalid %s=%r' % (name, raw), out)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--probe-runtime', action='store_true',
                        help='print runtime state before launcher startup helpers')
    parser.add_argument('--runtime-at-start', choices=('running', 'stopped'),
                        help='runtime state captured before launcher startup helpers')
    parser.add_argument('--darling', help='loader used for a graceful shutdown')
    parser.add_argument('--guest-log', type=Path,
                        help='guest log directory reported when the client fails')
    parser.add_argument('--stop-after', type=int,
                        default=env_seconds('RCLOL_STOP_AFTER_CLIENT_SECONDS'),
                        help='stop after the client window has been gone this many seconds')
    parser.add_argument('--no-clean', action='store_true',
                        help='leave the runtime alone after the child exits')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    prefix = args.prefix.expanduser().resolve()
    if args.probe_runtime:
        print('running' if runtime_alive(prefix) else 'stopped')
        return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required after --')
    clean = not args.no_clean and os.environ.get('RCLOL_CLEAN_ON_EXIT', '1') != '0'
    # The registry keeps every session that shares the container visible, even
    # when this one must not stop it (an interactive shell, or opt-out).
    container = container_metadata()
    stoppable = (container is not None
                 and os.environ.get('RCLOL_STOP_TOOLBOX', '1') != '0'
                 and not started_by_shell())
    return run_supervised(command, prefix, args.darling, stop_after=max(0, args.stop_after),
                          clean=clean, runtime_at_start=args.runtime_at_start,
                          guest_log=args.guest_log, container=container,
                          stoppable=stoppable)


if __name__ == '__main__':
    sys.exit(main())
