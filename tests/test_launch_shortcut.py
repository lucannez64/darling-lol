"""Verify the menu launcher's auto-installer behavior in a disposable tree."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/launch-shortcut.sh'

# Binaries the launcher needs from PATH; everything else is a builtin or a
# stub under test control.
REAL_BINARIES = ('dirname', 'mkdir', 'touch', 'chmod', 'id', 'python3')


def stub_script(path, body):
    path.write_text(f'#!/bin/sh\n{body}\n')
    path.chmod(0o755)


class LaunchShortcutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='launch-shortcut-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        shutil.copy2(SCRIPT, self.repo / 'scripts/launch-shortcut.sh')
        self.repo.joinpath('scripts/launch-shortcut.sh').chmod(0o755)
        self.prefix = self.root / 'prefix'
        self.prefix.mkdir()

    def bin_sandbox(self, notify=False):
        """Build a PATH holding only the binaries the launcher may use."""
        sandbox = self.root / 'bin'
        sandbox.mkdir()
        for name in REAL_BINARIES:
            source = shutil.which(name)
            self.assertTrue(source, name)
            os.symlink(source, sandbox / name)
        if notify:
            log = self.root / 'notify.log'
            stub_script(sandbox / 'notify-send',
                        f'echo "NOTIFY $*" >> {log}')
        return sandbox

    def write_config(self, region):
        config = self.repo / 'build'
        config.mkdir(exist_ok=True)
        (config / 'config.json').write_text(f'{{"region": "{region}"}}\n')

    def mark_installed(self, *, client=False, shared=False):
        if client:
            (self.prefix / 'Applications/Riot Client.app').mkdir(parents=True)
        if shared:
            (self.prefix / 'Users/Shared/Riot Games/Riot Client.app').mkdir(parents=True)

    def run_launcher(self, *, notify=False):
        stub_script(self.repo / 'scripts/desktop-run.sh', 'echo "RUN $*"')
        stub_script(self.repo / 'scripts/installer.sh', 'echo "INSTALL $*"')
        result = subprocess.run(
            [str(self.repo / 'scripts/launch-shortcut.sh')],
            capture_output=True, text=True, check=False,
            env=dict(os.environ, DPREFIX=str(self.prefix),
                     PATH=str(self.bin_sandbox(notify))),
        )
        log = self.repo / 'build/desktop-launch.log'
        return result, log.read_text() if log.exists() else ''

    def test_an_installed_client_launches_the_runtime(self):
        self.mark_installed(client=True)
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RUN', log)
        self.assertNotIn('INSTALL', log)

    def test_a_client_in_the_shared_folder_counts_as_installed(self):
        self.mark_installed(shared=True)
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RUN', log)
        self.assertNotIn('INSTALL', log)

    def test_a_missing_client_starts_the_installer_with_the_region(self):
        self.write_config('BR')
        result, log = self.run_launcher(notify=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL br', log)
        self.assertNotIn('RUN', log)
        self.assertIn('Riot Client is not installed', log)
        self.assertIn('NOTIFY', (self.root / 'notify.log').read_text())

    def test_a_missing_config_falls_back_to_the_default_region(self):
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL euw', log)

    def test_installation_proceeds_without_the_notification_service(self):
        self.write_config('BR')
        result, log = self.run_launcher(notify=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL br', log)
        self.assertNotIn('NOTIFY', log)


if __name__ == '__main__':
    unittest.main()
