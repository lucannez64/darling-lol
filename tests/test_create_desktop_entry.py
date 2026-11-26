"""Exercise the noninteractive shortcut wrapper without starting a container."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CreateDesktopEntryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='desktop wrapper ')
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.home = self.repo / 'home'
        self.home.mkdir()
        (self.repo / 'scripts').mkdir()
        shutil.copy2(ROOT / 'scripts/create-desktop-entry.sh', self.repo / 'scripts')
        (self.repo / 'scripts/configure.py').touch()
        tools = self.repo / 'build/runtime/tools'
        tools.mkdir(parents=True)
        (tools / 'desktop-entry.py').write_text('import json, sys\nprint(json.dumps(sys.argv[1:]))\n')

        self.bin = self.repo / 'bin'
        self.bin.mkdir()
        for name, body in {
            'toolbox': 'printf "toolbox should not start for shortcut creation\\n" >&2; exit 99',
            'podman': '[ "$1" = container ] && [ "$2" = exists ] && [ "$3" = -- ] && [ "$4" = my-box ]',
        }.items():
            script = self.bin / name
            script.write_text(f'#!/bin/sh\n{body}\n')
            script.chmod(0o755)

    def run_entry(self, *args, inside=False, name=''):
        if not inside and Path('/run/.toolboxenv').is_file():
            self.skipTest('host runs inside a Toolbox container')
        env = {**os.environ, 'HOME': str(self.home),
               'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
               'DARLING_LOL_TOOLBOX': '1' if inside else '0',
               'DARLING_LOL_TOOLBOX_NAME': name,
               'DARLING_LOL_WINDOW_ENTRIES': '0'}
        result = subprocess.run(['sh', str(self.repo / 'scripts/create-desktop-entry.sh'), *args],
                                env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), result.stderr

    def record_toolbox(self, name='my-box'):
        (self.repo / 'build/toolbox-container').write_text(name + '\n')

    def record_previous_entry(self):
        entry = self.home / '.local/share/applications/darling-launcher.desktop'
        entry.parent.mkdir(parents=True)
        entry.write_text('[Desktop Entry]\nExec=toolbox run -c old-box /path/to/launch-shortcut.sh\n')

    def test_host_uses_recorded_toolbox(self):
        self.record_toolbox()
        args, _ = self.run_entry('--window-entries')
        self.assertEqual(args, ['--toolbox', 'my-box', '--window-entries'])

    def test_inside_uses_recorded_toolbox_before_previous_entry(self):
        self.record_toolbox()
        self.record_previous_entry()
        args, _ = self.run_entry(inside=True)
        self.assertEqual(args, ['--toolbox', 'my-box'])

    def test_inside_preserves_explicit_toolbox_name(self):
        self.record_toolbox()
        args, _ = self.run_entry(inside=True, name='manual-box')
        self.assertEqual(args, ['--toolbox', 'manual-box'])

    def test_inside_preserves_previous_entry_without_install_marker(self):
        self.record_previous_entry()
        args, _ = self.run_entry(inside=True)
        self.assertEqual(args, ['--toolbox', 'old-box'])

    def test_host_ignores_previous_entry_without_install_marker(self):
        self.record_previous_entry()
        args, _ = self.run_entry()
        self.assertEqual(args, [])

    def test_host_prefers_native_install_to_toolbox_marker(self):
        self.record_toolbox()
        (self.repo / 'build/native-install').touch()
        args, _ = self.run_entry()
        self.assertEqual(args, [])

    def test_host_ignores_a_missing_container(self):
        self.record_toolbox('deleted-box')
        args, stderr = self.run_entry()
        self.assertEqual(args, [])
        self.assertIn('deleted-box', stderr)
