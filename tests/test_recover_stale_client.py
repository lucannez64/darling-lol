"""Test the opt-in partial client-tree recovery with fake /proc entries."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'recover_stale_client', ROOT / 'tools/recover-stale-client.py')
recover_stale_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recover_stale_client)

GAME = '/Applications/League of Legends.app/Contents/LoL/LeagueofLegends'
CLIENT = '/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices'


class RecoverStaleClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        (self.proc / 'uptime').write_text('1000 0\n')
        self.prefix = self.root / '.darling'

    def process(self, pid, comm, argv, env=None, executable=None, age=120, ppid=0):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / 'comm').write_text(comm + '\n')
        (folder / 'cmdline').write_bytes(
            (argv[0] if executable is None else executable).encode() + b'\0' +
            b''.join(arg.encode() + b'\0' for arg in argv[1:]))
        ticks = os.sysconf('SC_CLK_TCK')
        fields = ['S', str(ppid)] + ['0'] * 17 + [str(int((1000 - age) * ticks))]
        (folder / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields) + '\n')
        if env is not None:
            (folder / 'environ').write_bytes(env)
        return folder

    def client_env(self, prefix=None):
        prefix = self.prefix if prefix is None else prefix
        return b'%s=%s\0' % (
            recover_stale_client.SOCK_VARIABLE,
            os.fsencode(prefix / recover_stale_client.SOCK_NAME),
        )

    def run_recover(self, window=False, on_kill=None):
        killed = []

        def kill(pid, signum):
            killed.append((pid, signum))
            if on_kill is not None:
                on_kill(pid, signum)

        out = io.StringIO()
        with patch.object(recover_stale_client, 'visible_client_window',
                          return_value=window):
            result = recover_stale_client.recover(
                self.prefix, proc_root=self.proc, window_check=lambda: window,
                kill=kill, sleep=lambda _seconds: None, out=out)
        return result, killed, out.getvalue()

    def test_no_darlingserver_does_nothing(self):
        self.process(10, 'mldr', ['/sbin/launchd'])
        result, killed, output = self.run_recover()
        self.assertEqual((result, killed), (0, []))
        self.assertIn('no darlingserver', output)

    def test_game_is_never_touched(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(11, 'mldr', [GAME])
        result, killed, output = self.run_recover()
        self.assertEqual((result, killed), (0, []))
        self.assertIn('game running', output)

    def test_visible_window_does_nothing(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(11, 'mldr', [CLIENT], self.client_env())
        result, killed, output = self.run_recover(window=True)
        self.assertEqual((result, killed), (0, []))
        self.assertIn('client window present', output)

    def test_stale_client_is_killed_but_runtime_is_preserved(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(11, 'mldr', ['/sbin/launchd'])
        self.process(12, 'mldr', [CLIENT], self.client_env())
        result, killed, output = self.run_recover()
        self.assertEqual(result, 0)
        self.assertEqual([pid for pid, _sig in killed], [12, 12])
        self.assertIn('stale client tree', output)

    def test_other_prefix_and_game_are_preserved(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(11, 'mldr', [CLIENT], self.client_env(self.root / 'other'))
        self.process(12, 'mldr', [GAME], self.client_env())
        result, killed, output = self.run_recover()
        self.assertEqual((result, killed), (0, []))
        self.assertIn('game running', output)

    def test_runtime_process_names_are_not_killed(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(11, 'mldr', ['/sbin/launchd'])
        self.process(12, 'mldr', [CLIENT], self.client_env())
        result, killed, _output = self.run_recover()
        self.assertEqual(result, 0)
        self.assertNotIn(10, [pid for pid, _sig in killed])
        self.assertNotIn(11, [pid for pid, _sig in killed])

    def test_pid_identity_is_rechecked_before_sigkill(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(12, 'mldr', [CLIENT], self.client_env())

        def reuse_pid(pid, signum):
            if signum == recover_stale_client.signal.SIGTERM:
                (self.proc / str(pid) / 'cmdline').write_bytes(b'/usr/bin/editor\0')

        result, killed, _output = self.run_recover(on_kill=reuse_pid)
        self.assertEqual(result, 0)
        self.assertEqual(killed, [(12, recover_stale_client.signal.SIGTERM)])

    def test_children_are_terminated_before_their_parents(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(12, 'mldr', [CLIENT], self.client_env())
        self.process(13, 'mldr', ['/Applications/League of Legends.app/Contents/MacOS/LeagueClientUx'], self.client_env(), ppid=12)
        self.process(14, 'CrUtilityMain', ['/Applications/Riot Client.app/Contents/Frameworks/CrUtilityMain'], self.client_env(), ppid=13)
        result, killed, output = self.run_recover()
        self.assertEqual(result, 0)
        self.assertEqual(killed, [
            (14, recover_stale_client.signal.SIGTERM),
            (13, recover_stale_client.signal.SIGTERM),
            (12, recover_stale_client.signal.SIGTERM),
            (14, recover_stale_client.signal.SIGKILL),
            (13, recover_stale_client.signal.SIGKILL),
            (12, recover_stale_client.signal.SIGKILL),
        ])
        self.assertIn('terminating 3 process(es)', output)

    def test_leftover_processes_are_reported(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(12, 'mldr', [CLIENT], self.client_env())

        def survivor(pid, signum):
            pass

        result, killed, output = self.run_recover(on_kill=survivor)
        self.assertEqual(result, 0)
        self.assertIn('still present after SIGKILL', output)
        self.assertIn('parent PID(s): 0', output)

    def test_young_client_is_preserved(self):
        self.process(10, 'darlingserver', ['darlingserver'])
        self.process(12, 'mldr', [CLIENT], self.client_env(),
                     age=recover_stale_client.MIN_AGE_SECONDS - 1)
        result, killed, output = self.run_recover()
        self.assertEqual((result, killed), (0, []))
        self.assertIn('no stale client processes', output)

    def test_missing_net_client_list_is_inconclusive(self):
        with patch.object(recover_stale_client.subprocess, 'check_output',
                          return_value='_NET_CLIENT_LIST:  not found.\n'):
            self.assertTrue(recover_stale_client.visible_client_window())

    def test_empty_client_list_is_no_window(self):
        with patch.object(recover_stale_client.subprocess, 'check_output',
                          return_value='_NET_CLIENT_LIST(WINDOW): window id #\n'):
            self.assertFalse(recover_stale_client.visible_client_window())

    def test_missing_wm_class_is_inconclusive(self):
        with patch.object(recover_stale_client.subprocess, 'check_output',
                          side_effect=[
                              '_NET_CLIENT_LIST(WINDOW): window id # 0x10\n',
                              'WM_CLASS:  not found.\n',
                          ]):
            self.assertTrue(recover_stale_client.visible_client_window())

    def test_riot_client_shell_is_a_visible_window(self):
        with patch.object(recover_stale_client.subprocess, 'check_output',
                          side_effect=[
                              '_NET_CLIENT_LIST(WINDOW): window id # 0x10\n',
                              'WM_CLASS(STRING) = "RiotClientServices", "RiotClientServices"\n',
                          ]):
            self.assertTrue(recover_stale_client.visible_client_window())


if __name__ == '__main__':
    unittest.main()
