#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Relay the guest's Discord IPC connections to the host Discord socket.

The Darling guest runs in its own PID namespace, so a client's Discord
SET_ACTIVITY command carries a guest PID. Discord looks that PID up on the
host, finds no such process (or an unrelated one) and shows no activity.

This relay listens on its own discord-ipc-N sockets, which the launcher links
into the guest, and forwards every frame to the matching host socket
unchanged, except that the ``pid`` argument of SET_ACTIVITY is replaced with
the host PID of the connecting process (SO_PEERCRED). Nothing else is parsed,
stored or logged. The relay exits when the process given with --watch-pid
(the session supervisor) exits.

The Riot Client connects but never sends SET_ACTIVITY: its server-side
configuration keeps the Discord integration off ("Activity Gateway Discord is
disabled"), and Linux Discord cannot detect the game by process name because
a Darling process is the loader, not a Windows or macOS executable. So while
the League game runs, the relay sets the activity Discord's own game detection
shows: its own connection with Discord's League of Legends application, the
relay's PID and the game's start time, nothing else. It closes that
connection, which clears the activity, when the game exits or when the guest
client sends an activity of its own. --no-game-activity turns this off.
"""

import argparse
import json
import os
import selectors
import signal
import socket
import struct
import sys
import threading
import time

HEADER = struct.Struct('<II')
OP_HANDSHAKE = 0
OP_FRAME = 1
MAX_FRAME = 1 << 20
SLOTS = range(10)
# Discord's detectable-games entry for League of Legends (the application its
# own process detection reports on Windows and macOS).
GAME_APPLICATION_ID = '1402418696126992445'
# Basename of the game's argv[0] in the guest
# (.../LeagueofLegends.app/Contents/MacOS/LeagueofLegends), as the other
# launcher helpers match it.
GAME_NAME = 'LeagueofLegends'
GAME_POLL_SECONDS = 5.0
REPLY_TIMEOUT = 10.0
# Set once the guest client sends a non-empty activity: the client's own
# status then wins and the relay stops setting one.
client_activity = threading.Event()


def peer_pid(conn):
    """Host PID of the process on the other end of a Unix socket, or 0."""
    try:
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    except OSError:
        return 0
    pid, _uid, _gid = struct.unpack('3i', data)
    return pid if pid > 0 else 0


def rewrite_frame(opcode, payload, pid):
    """Return the payload with SET_ACTIVITY's pid replaced; else unchanged."""
    if opcode != OP_FRAME:
        return payload
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return payload
    if not isinstance(message, dict) or message.get('cmd') != 'SET_ACTIVITY':
        return payload
    args = message.get('args')
    if isinstance(args, dict) and args.get('activity'):
        client_activity.set()
    if pid <= 0 or not isinstance(args, dict) or 'pid' not in args or args['pid'] == pid:
        return payload
    args['pid'] = pid
    return json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode()


def recv_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return bytes(data)


def pump_guest_to_discord(guest, discord, pid):
    while True:
        header = recv_exact(guest, HEADER.size)
        if header is None:
            return
        opcode, length = HEADER.unpack(header)
        if length > MAX_FRAME:
            return
        payload = recv_exact(guest, length)
        if payload is None:
            return
        payload = rewrite_frame(opcode, payload, pid)
        discord.sendall(HEADER.pack(opcode, len(payload)) + payload)


def pump_raw(source, target):
    while True:
        data = source.recv(65536)
        if not data:
            return
        target.sendall(data)


def close_both(*socks):
    for sock in socks:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def serve_connection(guest, host_path):
    pid = peer_pid(guest)
    discord = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        discord.connect(host_path)
    except OSError:
        close_both(guest, discord)
        return

    def run(target, *args):
        try:
            target(*args)
        except OSError:
            pass
        finally:
            close_both(guest, discord)

    threading.Thread(target=run, args=(pump_raw, discord, guest), daemon=True).start()
    run(pump_guest_to_discord, guest, discord, pid)


def host_sockets(host_dir):
    return {n: os.path.join(host_dir, f'discord-ipc-{n}') for n in SLOTS
            if os.path.exists(os.path.join(host_dir, f'discord-ipc-{n}'))}


def bind_relays(relay_dir, slots):
    """Bind one socket per slot in this session's own directory.

    Each session gets its own directory (the launcher passes one per
    supervisor PID) and never reuses another session's relay, which exits
    with that session. A leftover socket is from a dead session whose PID was
    reused, so it is replaced.
    """
    parent = os.path.dirname(relay_dir.rstrip('/'))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    os.makedirs(relay_dir, mode=0o700, exist_ok=True)
    os.chmod(relay_dir, 0o700)
    listeners = {}
    for n in slots:
        path = os.path.join(relay_dir, f'discord-ipc-{n}')
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(8)
        listener.setblocking(False)
        listeners[n] = (listener, path)
    return listeners


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def boot_time(proc_root):
    try:
        with open(os.path.join(proc_root, 'stat'), encoding='ascii') as stat:
            for line in stat:
                if line.startswith('btime '):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def find_game(proc_root='/proc'):
    """(pid, start in Unix seconds or None) of this user's League game, or None."""
    uid = os.getuid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        proc = os.path.join(proc_root, entry)
        try:
            if os.stat(proc).st_uid != uid:
                continue
            with open(os.path.join(proc, 'cmdline'), 'rb') as cmdline:
                argv0 = cmdline.read(4096).split(b'\0', 1)[0]
        except OSError:
            continue
        if os.path.basename(os.fsdecode(argv0)) != GAME_NAME:
            continue
        start = None
        btime = boot_time(proc_root)
        try:
            with open(os.path.join(proc, 'stat'), encoding='ascii', errors='replace') as stat:
                ticks = int(stat.read().rsplit(')', 1)[1].split()[19])
            if btime is not None:
                start = btime + ticks // os.sysconf('SC_CLK_TCK')
        except (OSError, ValueError, IndexError):
            pass
        return int(entry), start
    return None


def send_frame(conn, opcode, message):
    payload = json.dumps(message, separators=(',', ':')).encode()
    conn.sendall(HEADER.pack(opcode, len(payload)) + payload)


def read_frame(conn):
    header = recv_exact(conn, HEADER.size)
    if header is None:
        return None
    opcode, length = HEADER.unpack(header)
    if length > MAX_FRAME:
        return None
    payload = recv_exact(conn, length)
    if payload is None:
        return None
    try:
        return opcode, json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return opcode, None


def wait_for(conn, accept):
    """Read frames until accept(opcode, message) is true; False on close or error."""
    while True:
        reply = read_frame(conn)
        if reply is None:
            return False
        opcode, message = reply
        if opcode != OP_FRAME:
            return False
        if accept(message):
            return isinstance(message, dict) and message.get('evt') != 'ERROR'


def open_game_activity(host_path, start, nonce):
    """Connect to Discord and set the League activity; the socket, or None."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(REPLY_TIMEOUT)
    try:
        conn.connect(host_path)
        send_frame(conn, OP_HANDSHAKE, {'v': 1, 'client_id': GAME_APPLICATION_ID})
        if not wait_for(conn, lambda m: isinstance(m, dict) and m.get('evt') in ('READY', 'ERROR')):
            raise OSError('handshake refused')
        activity = {'type': 0, 'timestamps': {'start': start or int(time.time())}}
        send_frame(conn, OP_FRAME, {'cmd': 'SET_ACTIVITY', 'nonce': nonce,
                                    'args': {'pid': os.getpid(), 'activity': activity}})
        if not wait_for(conn, lambda m: isinstance(m, dict) and m.get('nonce') == nonce):
            raise OSError('activity refused')
    except (OSError, ValueError):
        close_both(conn)
        return None
    conn.setblocking(False)
    return conn


def discord_closed(conn):
    """Drain Discord's frames; True once Discord closed the connection."""
    try:
        while True:
            if not conn.recv(65536):
                return True
    except BlockingIOError:
        return False
    except OSError:
        return True


def game_activity(host_path, proc_root='/proc', poll=GAME_POLL_SECONDS, stop=None):
    """Hold the League activity open while the game runs (see the module doc)."""
    stop = stop or threading.Event()
    conn = None
    tried_pid = None
    counter = 0
    while not stop.is_set():
        game = None if client_activity.is_set() else find_game(proc_root)
        if conn is not None and (game is None or game[0] != tried_pid or discord_closed(conn)):
            close_both(conn)
            conn = None
        # One attempt per game process: a refused application is not retried
        # every poll.
        if game is not None and conn is None and game[0] != tried_pid:
            tried_pid = game[0]
            counter += 1
            conn = open_game_activity(host_path, game[1], f'darling-lol-{counter}')
        if game is None:
            tried_pid = None
        stop.wait(poll)
    if conn is not None:
        close_both(conn)


def serve(listeners, host_paths, watch_pid, relay_dir, poll=1.0):
    selector = selectors.DefaultSelector()
    for n, (listener, _path) in listeners.items():
        selector.register(listener, selectors.EVENT_READ, n)
    try:
        while watch_pid <= 0 or process_alive(watch_pid):
            for key, _events in selector.select(timeout=poll):
                try:
                    guest, _ = key.fileobj.accept()
                except OSError:
                    continue
                guest.setblocking(True)
                threading.Thread(target=serve_connection,
                                 args=(guest, host_paths[key.data]), daemon=True).start()
    finally:
        for listener, path in listeners.values():
            listener.close()
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(relay_dir)
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host-dir', required=True, help='directory holding Discord\'s discord-ipc-N')
    parser.add_argument('--relay-dir', required=True, help='directory for the relay sockets')
    parser.add_argument('--watch-pid', type=int, default=0, help='exit when this process exits')
    parser.add_argument('--foreground', action='store_true', help='do not detach after binding')
    parser.add_argument('--no-game-activity', action='store_true',
                        help='do not set the League activity while the game runs')
    parser.add_argument('--proc-root', default='/proc', help=argparse.SUPPRESS)
    parser.add_argument('--game-poll', type=float, default=GAME_POLL_SECONDS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    host_paths = host_sockets(args.host_dir)
    if not host_paths:
        print('discord-rpc: no Discord IPC socket found; relay not started', file=sys.stderr)
        return 1
    try:
        listeners = bind_relays(args.relay_dir, host_paths)
    except OSError as error:
        print(f'discord-rpc: cannot create the relay sockets: {error}', file=sys.stderr)
        return 1
    # Print the relayed slots, then detach: the launcher links only those to
    # the relay directory.
    print(' '.join(str(n) for n in sorted(listeners)), flush=True)
    if not args.foreground:
        if os.fork():
            os._exit(0)
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        # Release the launcher's pipes: nothing is logged after binding.
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.close(devnull)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if not args.no_game_activity:
        threading.Thread(target=game_activity,
                         args=(host_paths[min(host_paths)], args.proc_root, args.game_poll),
                         daemon=True).start()
    serve(listeners, host_paths, args.watch_pid, args.relay_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
