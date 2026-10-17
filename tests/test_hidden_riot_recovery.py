"""Exercise desktop recovery with fake /proc entries and mocked commands only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('recovery', ROOT / 'tools/recover-hidden-riot-client.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.home = self.root / 'home'
        self.home_patch = patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.env_patch = patch.dict(os.environ, {'DPREFIX': str(self.home / '.darling')})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def process(self, pid, executable, prefix=None):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / 'cmdline').write_bytes(executable.encode() + b'\0')
        (folder / 'environ').write_bytes(b'' if prefix is None else b'DPREFIX=' + os.fsencode(prefix) + b'\0')
        return folder

    def riot(self):
        return self.process(123, '/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices')

    def run_recovery(self, windows=None, error=None, raw=None):
        def command(args, **kwargs):
            if args[0] == 'hyprctl':
                if error:
                    raise error
                return subprocess.CompletedProcess(args, 0, stdout=raw if raw is not None else json.dumps(windows or []))
            return subprocess.CompletedProcess(args, 0)
        with patch.object(recovery.subprocess, 'run', side_effect=command) as run:
            result = recovery.recover(self.proc)
        return result, [call.args[0] for call in run.call_args_list]

    def test_windowless_client_is_recovered(self):
        self.riot()
        result, calls = self.run_recovery()
        self.assertTrue(result)
        self.assertEqual(calls, [['hyprctl', 'clients', '-j'], ['darling', 'shutdown']])

    def test_visible_client_is_preserved(self):
        self.riot()
        for identity in ('Riot Client', 'League of Legends'):
            result, calls = self.run_recovery([{'class': identity, 'mapped': True, 'hidden': False}])
            self.assertFalse(result)
            self.assertEqual(len(calls), 1)

    def test_game_is_preserved_even_without_windows(self):
        self.riot()
        self.process(124, '/Applications/LeagueofLegends.app/Contents/MacOS/LeagueofLegends')
        self.assertEqual(self.run_recovery(), (False, []))

    def test_no_riot_is_preserved(self):
        self.process(124, '/usr/bin/editor')
        self.assertEqual(self.run_recovery(), (False, []))

    def test_foreign_owned_process_is_preserved(self):
        self.riot()
        with patch.object(recovery.os, 'getuid', return_value=os.getuid() + 1):
            self.assertEqual(self.run_recovery(), (False, []))

    def test_custom_prefix_is_preserved(self):
        self.riot()
        with patch.dict(os.environ, {'DPREFIX': str(self.root / 'other-prefix')}):
            self.assertEqual(self.run_recovery(), (False, []))

    def test_riot_in_another_prefix_is_preserved(self):
        self.process(123, '/Applications/Riot Client.app/Contents/MacOS/RiotClientServices', self.root / 'other-prefix')
        self.assertEqual(self.run_recovery(), (False, []))

    def test_missing_or_failed_compositor_is_preserved(self):
        self.riot()
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('hyprctl', 5),
                      subprocess.CalledProcessError(1, 'hyprctl')):
            result, calls = self.run_recovery(error=error)
            self.assertFalse(result)
            self.assertEqual(len(calls), 1)

    def test_malformed_compositor_response_is_preserved(self):
        self.riot()
        for raw in ('invalid', '{}', '[null]'):
            result, calls = self.run_recovery(raw=raw)
            self.assertFalse(result)
            self.assertEqual(len(calls), 1)

    def test_unreadable_environment_is_preserved(self):
        self.riot()
        original = Path.read_bytes
        def read(path):
            if path.name == 'environ':
                raise PermissionError()
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            self.assertEqual(self.run_recovery(), (False, []))


if __name__ == '__main__':
    unittest.main()
