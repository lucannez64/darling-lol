"""Discord IPC relay: guest SET_ACTIVITY PIDs become host PIDs (issue #286)."""
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / 'tools/discord-ipc-relay.py'
LAUNCHER = ROOT / 'tools/run-riot-client.sh'

spec = importlib.util.spec_from_file_location('discord_ipc_relay', RELAY)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def frame(opcode, message):
    payload = json.dumps(message).encode()
    return struct.pack('<II', opcode, len(payload)) + payload


def read_frame(conn):
    opcode, length = struct.unpack('<II', relay.recv_exact(conn, 8))
    return opcode, json.loads(relay.recv_exact(conn, length))


class RewriteTests(unittest.TestCase):
    def test_set_activity_pid_is_replaced(self):
        payload = json.dumps({'cmd': 'SET_ACTIVITY', 'nonce': 'n',
                              'args': {'pid': 42, 'activity': {'state': 'é'}}}).encode()
        message = json.loads(relay.rewrite_frame(1, payload, 9000))
        self.assertEqual(message['args']['pid'], 9000)
        self.assertEqual(message['args']['activity'], {'state': 'é'})

    def test_other_frames_are_untouched(self):
        cases = [
            (0, json.dumps({'v': 1, 'client_id': '1'}).encode()),
            (1, json.dumps({'cmd': 'SUBSCRIBE', 'args': {'pid': 3}}).encode()),
            (1, json.dumps({'cmd': 'SET_ACTIVITY', 'args': {'activity': None}}).encode()),
            (1, b'not json'),
            (1, json.dumps([1, 2]).encode()),
        ]
        for opcode, payload in cases:
            self.assertIs(relay.rewrite_frame(opcode, payload, 9000), payload)

    def test_unknown_peer_keeps_the_pid(self):
        payload = json.dumps({'cmd': 'SET_ACTIVITY', 'args': {'pid': 42}}).encode()
        self.assertIs(relay.rewrite_frame(1, payload, 0), payload)


class RelayProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='discord relay ')
        self.addCleanup(tmp.cleanup)
        self.host_dir = Path(tmp.name) / 'host'
        self.relay_dir = Path(tmp.name) / 'relay'
        self.host_dir.mkdir()
        self.discord = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.discord.close)
        self.discord.bind(str(self.host_dir / 'discord-ipc-0'))
        self.discord.listen(1)

    def start_relay(self, watch_pid):
        return subprocess.run(
            [sys.executable, str(RELAY), '--host-dir', str(self.host_dir),
             '--relay-dir', str(self.relay_dir), '--watch-pid', str(watch_pid)],
            capture_output=True, text=True, timeout=10, check=False)

    def test_relays_frames_and_replaces_the_pid(self):
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        result = self.start_relay(sleeper.pid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '0')
        self.assertEqual(os.stat(self.relay_dir).st_mode & 0o777, 0o700)

        guest = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(guest.close)
        guest.connect(str(self.relay_dir / 'discord-ipc-0'))
        self.discord.settimeout(10)
        server, _ = self.discord.accept()
        self.addCleanup(server.close)
        server.settimeout(10)

        guest.sendall(frame(0, {'v': 1, 'client_id': '123'}))
        self.assertEqual(read_frame(server), (0, {'v': 1, 'client_id': '123'}))
        guest.sendall(frame(1, {'cmd': 'SET_ACTIVITY', 'nonce': 'a',
                                'args': {'pid': 7, 'activity': {'details': 'x'}}}))
        opcode, message = read_frame(server)
        self.assertEqual(opcode, 1)
        self.assertEqual(message['args']['pid'], os.getpid())
        self.assertEqual(message['args']['activity'], {'details': 'x'})

        # Replies travel back unchanged.
        server.sendall(frame(1, {'cmd': 'DISPATCH', 'evt': 'READY'}))
        guest.settimeout(10)
        self.assertEqual(read_frame(guest), (1, {'cmd': 'DISPATCH', 'evt': 'READY'}))

        # The relay leaves with the watched process and removes its socket.
        sleeper.kill()
        sleeper.wait()
        deadline = time.monotonic() + 10
        while (self.relay_dir / 'discord-ipc-0').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse((self.relay_dir / 'discord-ipc-0').exists())

    def test_no_discord_socket_means_no_relay(self):
        self.discord.close()
        (self.host_dir / 'discord-ipc-0').unlink()
        result = self.start_relay(os.getpid())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertFalse((self.relay_dir).exists())


class LauncherTests(unittest.TestCase):
    def test_launcher_links_relayed_slots_and_has_an_opt_out(self):
        text = LAUNCHER.read_text()
        self.assertIn('discord-ipc-relay.py', text)
        self.assertIn('--watch-pid "$$"', text)
        self.assertIn('RCLOL_DISCORD_PID_RELAY', text)
        self.assertIn('target=$relay_dir', text)


if __name__ == '__main__':
    unittest.main()
