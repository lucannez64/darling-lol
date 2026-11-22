#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Relay the guest's Discord IPC connections to the host Discord socket.

The Darling guest runs in its own PID namespace, so a client's Discord
SET_ACTIVITY command carries a guest PID. Discord looks that PID up on the
host, finds no such process (or an unrelated one) and shows no activity.

This relay listens on its own discord-ipc-N sockets, which the launcher links
into the guest, and forwards every frame to the matching host socket
unchanged, except that the ``pid`` argument of SET_ACTIVITY is replaced with
the relay's own PID. That is the process Discord sees on the other end of its
socket: a host process it can find, which lives exactly as long as the
session. The guest process's host PID is not used: Discord does not list
Darling's mldr processes (#286), so an activity tied to one never shows.
Nothing else is parsed or stored, and nothing is logged unless --debug is
given. With --debug the relay prints one line per frame to stderr: the
direction, opcode, command and event names, Discord's error code and message,
and SET_ACTIVITY's pid. It never prints the activity itself, the handshake's
client ID or any other payload. The relay exits when the process given with
--watch-pid (the session supervisor) exits.
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

HEADER = struct.Struct('<II')
OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
MAX_FRAME = 1 << 20
SLOTS = range(10)
DEBUG = False


def debug(line):
    if DEBUG:
        try:
            print(f'discord-rpc: {line}', file=sys.stderr, flush=True)
        except OSError:
            pass


def describe(source, opcode, payload):
    """One debug line for a frame: names, error code/message and pid only."""
    parts = [source, f'op={opcode}', f'len={len(payload)}']
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return ' '.join(parts + ['not-json'])
    if not isinstance(message, dict):
        return ' '.join(parts + ['not-object'])
    if opcode == OP_HANDSHAKE:
        parts.append(f'handshake v={message.get("v")!r}')
    for key in ('cmd', 'evt'):
        if isinstance(message.get(key), str):
            parts.append(f'{key}={message[key]}')
    args = message.get('args')
    if message.get('cmd') == 'SET_ACTIVITY' and isinstance(args, dict):
        parts.append(f'pid={args.get("pid")!r}')
        parts.append('activity=' + ('null' if args.get('activity') is None else 'set'))
    error = message.get('data') if message.get('evt') == 'ERROR' else None
    if opcode == OP_CLOSE:
        error = message
    if isinstance(error, dict):
        parts.append(f'code={error.get("code")!r}')
        parts.append(f'message={str(error.get("message"))[:200]!r}')
    return ' '.join(parts)


def rewrite_frame(opcode, payload, pid):
    """Return the payload with SET_ACTIVITY's pid replaced; else unchanged."""
    if opcode != OP_FRAME or pid <= 0:
        return payload
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return payload
    if not isinstance(message, dict) or message.get('cmd') != 'SET_ACTIVITY':
        return payload
    args = message.get('args')
    if not isinstance(args, dict) or 'pid' not in args or args['pid'] == pid:
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


def pump_frames(source, target, label, pid=0):
    """Forward whole frames; rewrite SET_ACTIVITY's pid when pid is set."""
    while True:
        header = recv_exact(source, HEADER.size)
        if header is None:
            debug(f'{label} closed the connection')
            return
        opcode, length = HEADER.unpack(header)
        if length > MAX_FRAME:
            debug(f'{label} sent an oversized frame ({length} bytes); closing')
            return
        payload = recv_exact(source, length)
        if payload is None:
            debug(f'{label} closed the connection mid-frame')
            return
        debug(describe(label, opcode, payload))
        if pid:
            payload = rewrite_frame(opcode, payload, pid)
        target.sendall(HEADER.pack(opcode, len(payload)) + payload)


def pump_guest_to_discord(guest, discord, pid):
    pump_frames(guest, discord, 'client', pid)


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
    # Discord's own end of this connection is the relay, so its PID is the one
    # that matches the socket and that Discord can look up on the host.
    pid = os.getpid()
    discord = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        discord.connect(host_path)
    except OSError as error:
        debug(f'cannot connect to {os.path.basename(host_path)}: {error}')
        close_both(guest, discord)
        return
    debug(f'client connected; forwarding to {os.path.basename(host_path)} as pid {pid}')

    def run(target, *args):
        try:
            target(*args)
        except OSError:
            pass
        finally:
            close_both(guest, discord)

    if DEBUG:
        replies = (pump_frames, discord, guest, 'discord')
    else:
        replies = (pump_raw, discord, guest)
    threading.Thread(target=run, args=replies, daemon=True).start()
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
    parser.add_argument('--debug', action='store_true',
                        help='print frame names and Discord errors to stderr (no activity content)')
    args = parser.parse_args(argv)
    global DEBUG
    DEBUG = args.debug

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
        # Release the launcher's pipes. Stdout must close so the launcher's
        # command substitution returns; with --debug, stderr stays on the
        # launch log.
        for fd in (0, 1) if args.debug else (0, 1, 2):
            os.dup2(devnull, fd)
        os.close(devnull)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    debug(f'relay pid {os.getpid()} serving slots {" ".join(str(n) for n in sorted(listeners))}')
    serve(listeners, host_paths, args.watch_pid, args.relay_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
