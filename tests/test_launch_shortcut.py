"""Verify the menu launcher's auto-installer behavior in a disposable tree."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/launch-shortcut.sh'

# Binaries the launcher needs from PATH; everything else is a builtin or a
# stub under test control.
REAL_BINARIES = ('dirname', 'mkdir', 'touch', 'chmod', 'id', 'python3', 'tr')


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
        # The launcher resolves the prefix through the real configure step.
        for name in ('configure.py', 'common.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        (self.repo / 'tools').mkdir()
        for name in ('runtime-paths.sh', 'runtime_paths.py'):
            shutil.copy2(ROOT / 'tools' / name, self.repo / 'tools' / name)
        self.home = self.root / 'home'
        self.home.mkdir()
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

    def write_config(self, region, prefix=None):
        command = [sys.executable, str(self.repo / 'scripts/configure.py'), '--region', region]
        if prefix is not None:
            command += ['--prefix', str(prefix)]
        subprocess.run(command, check=True, capture_output=True,
                       env=dict(os.environ, HOME=str(self.home)))

    def mark_installed(self, *, client=False, shared=False):
        # Only a bundle with Contents/MacOS is an installed client.
        if client:
            (self.prefix / 'Applications/Riot Client.app/Contents/MacOS').mkdir(parents=True)
        if shared:
            (self.prefix / 'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS').mkdir(parents=True)

    def mark_partial(self):
        """What a crashed installer leaves: bundles without Contents/MacOS."""
        (self.prefix / 'Applications/Riot Client.app').mkdir(parents=True)
        (self.prefix / 'Users/Shared/Riot Games/Riot Client.app/Contents/Frameworks').mkdir(parents=True)

    def run_launcher(self, *, notify=False, dprefix=True, region=None):
        stub_script(self.repo / 'scripts/desktop-run.sh', 'echo "RUN $*"')
        stub_script(self.repo / 'scripts/installer.sh', 'echo "INSTALL $*"')
        env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin_sandbox(notify)))
        env.pop('DPREFIX', None)
        env.pop('RIOT_GUEST_HOME', None)
        env.pop('RCLOL_REGION', None)
        if region is not None:
            env['RCLOL_REGION'] = region
        if dprefix:
            env['DPREFIX'] = str(self.prefix)
        result = subprocess.run(
            [str(self.repo / 'scripts/launch-shortcut.sh')],
            capture_output=True, text=True, check=False, env=env,
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

    def test_a_partial_bundle_goes_to_the_installer(self):
        self.mark_partial()
        result, log = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL euw', log)
        self.assertNotIn('RUN', log)

    def test_a_client_in_the_configured_prefix_never_starts_the_installer(self):
        # `configure.py --prefix` recorded a custom prefix; DPREFIX is not
        # exported by a menu launch and ~/.darling does not exist.
        self.write_config('BR', prefix=self.prefix)
        for layout in ({'client': True}, {'shared': True}):
            with self.subTest(layout=layout):
                shutil.rmtree(self.prefix)
                self.prefix.mkdir()
                self.mark_installed(**layout)
                (self.repo / 'build/desktop-launch.log').unlink(missing_ok=True)
                shutil.rmtree(self.root / 'bin', ignore_errors=True)
                result, log = self.run_launcher(dprefix=False)
                self.assertEqual(result.returncode, 0, log)
                self.assertIn('RUN', log)
                self.assertNotIn('INSTALL', log)

    def test_a_missing_client_in_the_configured_prefix_names_that_prefix(self):
        self.write_config('BR', prefix=self.prefix)
        result, log = self.run_launcher(dprefix=False)
        self.assertEqual(result.returncode, 0, log)
        self.assertIn(f'Riot Client is not installed in {self.prefix};', log)
        self.assertIn('INSTALL br', log)

    def test_an_unreadable_configuration_never_starts_the_installer(self):
        (self.repo / 'build').mkdir()
        (self.repo / 'build/config.json').write_text('{not json')
        self.mark_installed(client=True)
        result, log = self.run_launcher(dprefix=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not resolve the Darling prefix', log)
        self.assertNotIn('INSTALL', log)
        self.assertNotIn('RUN', log)

    def test_a_missing_client_starts_the_installer_with_the_region(self):
        self.write_config('BR')
        result, log = self.run_launcher(notify=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL br', log)
        self.assertNotIn('RUN', log)
        self.assertIn('Riot Client is not installed', log)
        self.assertIn('NOTIFY', (self.root / 'notify.log').read_text())

    def test_a_shortcut_region_overrides_the_configured_region(self):
        # create-shortcuts.sh bakes the system region into Exec; the first
        # launch must install that region, not the one in build/config.json.
        self.write_config('BR')
        result, log = self.run_launcher(region='KR')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL kr', log)
        self.assertNotIn('INSTALL br', log)

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
