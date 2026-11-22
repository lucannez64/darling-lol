"""Discord IPC relay: guest SET_ACTIVITY PIDs become the relay's PID (issue #286)."""
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

    def test_debug_line_names_the_frame_but_never_its_content(self):
        payload = json.dumps({'cmd': 'SET_ACTIVITY', 'nonce': 'n', 'args': {
            'pid': 31, 'activity': {'state': 'secret-state', 'party': {'id': 'secret-party'}}}}).encode()
        line = relay.describe('client', 1, payload)
        self.assertIn('cmd=SET_ACTIVITY', line)
        self.assertIn('pid=31', line)
        self.assertIn('activity=set', line)
        self.assertNotIn('secret', line)
        handshake = relay.describe('client', 0, json.dumps({'v': 1, 'client_id': '987654'}).encode())
        self.assertIn('handshake v=1', handshake)
        self.assertNotIn('987654', handshake)
        error = relay.describe('discord', 1, json.dumps({
            'cmd': 'SET_ACTIVITY', 'evt': 'ERROR', 'data': {'code': 4000, 'message': 'bad pid'}}).encode())
        self.assertIn("evt=ERROR code=4000 message='bad pid'", error)
        close = relay.describe('discord', 2, json.dumps({'code': 4000, 'message': 'Invalid Client ID'}).encode())
        self.assertIn("code=4000 message='Invalid Client ID'", close)
        self.assertIn('not-json', relay.describe('client', 1, b'\x00\xff'))

    def test_unknown_peer_keeps_the_pid(self):
        payload = json.dumps({'cmd': 'SET_ACTIVITY', 'args': {'pid': 42}}).encode()
        self.assertIs(relay.rewrite_frame(1, payload, 0), payload)


class RelayProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='discord relay ')
        self.addCleanup(tmp.cleanup)
        self.host_dir = Path(tmp.name) / 'host'
        self.relay_dir = Path(tmp.name) / 'relay' / '4242'
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
        self.assertEqual(os.stat(self.relay_dir.parent).st_mode & 0o777, 0o700)

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
        # The PID is the one Discord sees on its socket (the relay), never the
        # connecting guest process: Discord cannot find mldr processes (#286).
        peer = struct.unpack('3i', server.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))[0]
        self.assertEqual(message['args']['pid'], peer)
        self.assertNotEqual(peer, os.getpid())
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
        while self.relay_dir.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.relay_dir.exists())

    def test_a_stale_socket_from_a_dead_session_is_replaced(self):
        self.relay_dir.mkdir(parents=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.relay_dir / 'discord-ipc-0'))
        stale.close()
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        result = self.start_relay(sleeper.pid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '0')
        guest = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(guest.close)
        guest.connect(str(self.relay_dir / 'discord-ipc-0'))
        self.discord.settimeout(10)
        server, _ = self.discord.accept()
        server.close()

    def test_debug_relay_prints_discord_errors_to_stderr(self):
        proc = subprocess.Popen(
            [sys.executable, str(RELAY), '--host-dir', str(self.host_dir),
             '--relay-dir', str(self.relay_dir), '--foreground', '--debug'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), '0')
        guest = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(guest.close)
        guest.connect(str(self.relay_dir / 'discord-ipc-0'))
        self.discord.settimeout(10)
        server, _ = self.discord.accept()
        self.addCleanup(server.close)
        server.settimeout(10)
        guest.sendall(frame(1, {'cmd': 'SET_ACTIVITY', 'nonce': 'a',
                                'args': {'pid': 31, 'activity': {'details': 'private'}}}))
        read_frame(server)
        reply = {'cmd': 'SET_ACTIVITY', 'evt': 'ERROR', 'nonce': 'a',
                 'data': {'code': 4000, 'message': 'nope'}}
        server.sendall(frame(1, reply))
        guest.settimeout(10)
        # Replies still reach the client unchanged in debug mode.
        self.assertEqual(read_frame(guest), (1, reply))
        server.close()
        guest.close()
        proc.kill()
        _out, err = proc.communicate(timeout=10)
        self.assertIn('discord-rpc: relay pid', err)
        self.assertIn('client op=1', err)
        self.assertIn('cmd=SET_ACTIVITY pid=31 activity=set', err)
        self.assertIn("discord op=1", err)
        self.assertIn("evt=ERROR code=4000 message='nope'", err)
        self.assertNotIn('private', err)

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
        self.assertIn('darling-lol-discord-$(id -u)/$$"', text)
        self.assertIn('"$discord_found" = 1', text)
        self.assertIn('[ "${RCLOL_DISCORD_RPC_DEBUG:-0}" = 1 ] && relay_debug=--debug', text)
        self.assertIn('--watch-pid "$$" $relay_debug)', text)

    def test_launcher_creates_the_discord_games_directory(self):
        # The Discord SDK's macOS launch-command registration writes into this
        # directory and fails when it is missing (#286).
        text = LAUNCHER.read_text()
        block = text[text.index('if [ "${RCLOL_DISCORD_RPC:-1}" != 0 ]; then'):]
        block = block[:block.index('\nelse\n')]
        self.assertIn('mkdir -p "$guest_home/Library/Application Support/discord/games"', block)
        # guest_home comes from runtime-paths.sh, sourced near the top.
        self.assertLess(text.index('. "$runner_dir/runtime-paths.sh"'),
                        text.index('Application Support/discord/games'))
        self.assertIn('guest_home=', (LAUNCHER.parent / 'runtime-paths.sh').read_text())


if __name__ == '__main__':
    unittest.main()
