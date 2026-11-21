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
OP_FRAME = 1
MAX_FRAME = 1 << 20
SLOTS = range(10)


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
    serve(listeners, host_paths, args.watch_pid, args.relay_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
