"""Verify what scripts/clean-prefix.sh archives, cleans, regenerates and refuses."""
import os
import plistlib
import pty
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CleanPrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='clean-prefix-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        (self.repo / 'tools').mkdir()
        for name in ('clean-prefix.sh', 'configure.py', 'common.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        for name in ('runtime-paths.sh', 'runtime_paths.py', 'configure-darling-platform.py'):
            shutil.copy2(ROOT / 'tools' / name, self.repo / 'tools' / name)
        self.stop_log = self.root / 'stop.log'
        (self.repo / 'tools/stop-darling-runtime.py').write_text(
            f'open({str(self.stop_log)!r}, "a").write("STOP\\n")\n')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.runtime = self.root / 'runtime root'
        version = self.runtime / 'System/Library/CoreServices/SystemVersion.plist'
        version.parent.mkdir(parents=True)
        version.write_bytes(plistlib.dumps({
            'ProductName': 'Darling',
            'ProductVersion': '11.7.4',
            'ProductBuildVersion': 'Darling-test',
        }))
        self.prefix = self.root / 'my prefix'
        self.guest = self.prefix / 'Users/tester'
        self.outside = self.root / 'my'
        self.build_prefix()

    def build_prefix(self):
        prefix, guest, outside = self.prefix, self.guest, self.outside
        for folder in (
            prefix / 'private/etc',
            prefix / 'Applications/League of Legends.app/Contents/LoL/Logs/GameLogs',
            prefix / 'Applications/League of Legends.app/Contents/LoL/Config',
            prefix / 'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS',
            prefix / 'Users/Shared/Riot Games/Metadata/league_of_legends.live',
            guest / 'Library/Application Support/Riot Games/Riot Client/Config',
            guest / 'Library/Application Support/Riot Games/Riot Client/Data',
            guest / 'Library/Logs/Riot Games/Riot Client',
            guest / 'Library/Caches/com.riotgames.RiotClient',
            guest / 'Library/Caches/mesa',
            outside / 'external-logs',
        ):
            folder.mkdir(parents=True)
        (prefix / 'private/etc/darling-system.prop').write_text('profile=compat\n')
        self.old_uuid = '11111111-2222-4333-8444-555555555555'
        self.old_serial = 'OLDSERIAL123'
        (prefix / 'private/etc/darling-platform.plist').write_bytes(plistlib.dumps({
            'schema': 1,
            'properties': {
                'IOPlatformSerialNumber': self.old_serial,
                'IOPlatformUUID': self.old_uuid,
                'provisioning_UDID': self.old_uuid,
                'mlb-serial-number': 'OLDMLB12345678901',
                'ROM': bytes.fromhex('021122334455'),
                'model': 'MacBookPro16,1',
                'product-name': 'MacBookPro16,1',
            },
        }))
        (prefix / 'private/etc/darling-smbios.conf').write_text(
            f'SMBIOS_SERIAL_NUMBER={self.old_serial}\nSMBIOS_PLATFORM_UUID={self.old_uuid}\n')
        (prefix / 'private/etc/darling-platform.plist.before-platform-change').write_text('first-plist\n')
        (prefix / 'private/etc/darling-smbios.conf.before-platform-change').write_text('first-conf\n')
        (prefix / 'Applications/League of Legends.app/Contents/LoL/game.bin').write_text('game\n')
        (prefix / 'Applications/League of Legends.app/Contents/LoL/Logs/GameLogs/r3dlog.txt').write_text('match\n')
        (prefix / 'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices').write_text('bin\n')
        (prefix / 'Users/Shared/Riot Games/machine.cfg').write_text('machine-shared\n')
        (guest / 'Library/Application Support/Riot Games/machine.cfg').write_text('machine-user\n')
        (guest / 'Library/Application Support/Riot Games/Riot Client/Config/machine.cfg').write_text('machine-cfg\n')
        (guest / 'Library/Application Support/Riot Games/Riot Client/Config/RiotClientSettings.yaml').write_text('locale: en_US\n')
        (guest / 'Library/Application Support/Riot Games/Riot Client/Data/RiotGamesPrivateSettings.yaml').write_text('session\n')
        (guest / 'Library/Logs/Riot Games/Riot Client/rc.log').write_text('riot log\n')
        (guest / 'Library/Caches/com.riotgames.RiotClient/cache.bin').write_text('cache\n')
        (guest / 'Library/Caches/mesa/shader').write_text('shader\n')
        (prefix / '.init.pid.stale-1-2').write_text('1\n')
        (outside / 'external-logs/secret.log').write_text('keep\n')
        os.symlink(outside / 'external-logs', guest / 'Library/Logs/Riot Client')

    def run_script(self, *arguments, answer=None, prefix=None, **extra):
        env = dict(
            os.environ,
            HOME=str(self.home),
            DPREFIX=str(prefix or self.prefix),
            RIOT_GUEST_HOME=str((prefix or self.prefix) / 'Users/tester'),
            CLEAN_PREFIX_PROC_ROOT=str(self.proc),
            CLEAN_PREFIX_RUNTIME_ROOT=str(self.runtime),
            **extra,
        )
        env.pop('DARLING_WORKSPACE', None)
        command = ['sh', str(self.repo / 'scripts/clean-prefix.sh'), *arguments]
        if answer is None:
            return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, check=False, env=env, timeout=60)
        master, slave = pty.openpty()
        try:
            os.write(master, answer.encode() + b'\n')
            return subprocess.run(command, stdin=slave, capture_output=True,
                                  text=True, check=False, env=env, timeout=60)
        finally:
            os.close(master)
            os.close(slave)

    def stops(self):
        return self.stop_log.read_text().count('STOP') if self.stop_log.exists() else 0

    def test_backs_up_cleans_and_regenerates_identity_while_keeping_game(self):
        result = self.run_script('--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), 1)

        archives = sorted((self.repo / 'build/prefix-cleanup-backups').glob('prefix-cleanup-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        with tarfile.open(archives[0], 'r:gz') as tar:
            names = set(tar.getnames())
        self.assertIn('Users/tester/Library/Logs/Riot Games/Riot Client/rc.log', names)
        self.assertIn('Applications/League of Legends.app/Contents/LoL/Logs/GameLogs/r3dlog.txt', names)
        self.assertIn('Users/Shared/Riot Games/machine.cfg', names)
        self.assertIn('Users/tester/Library/Application Support/Riot Games/machine.cfg', names)
        self.assertIn('Users/tester/Library/Application Support/Riot Games/Riot Client/Config/machine.cfg', names)
        self.assertIn('Users/tester/Library/Application Support/Riot Games/Riot Client/Data/RiotGamesPrivateSettings.yaml', names)
        self.assertIn('private/etc/darling-platform.plist', names)
        self.assertIn('private/etc/darling-smbios.conf', names)
        self.assertIn('private/etc/darling-platform.plist.before-platform-change', names)
        self.assertIn('private/etc/darling-smbios.conf.before-platform-change', names)
        # Escaping symlink must not be archived and its external target must remain intact.
        self.assertFalse(any('secret.log' in name for name in names))
        self.assertEqual((self.outside / 'external-logs/secret.log').read_text(), 'keep\n')

        for gone in (
            'Users/tester/Library/Logs/Riot Games',
            'Users/tester/Library/Logs/Riot Client',
            'Applications/League of Legends.app/Contents/LoL/Logs',
            'Users/Shared/Riot Games/machine.cfg',
            'Users/tester/Library/Application Support/Riot Games/machine.cfg',
            'Users/tester/Library/Application Support/Riot Games/Riot Client/Config/machine.cfg',
            'Users/tester/Library/Application Support/Riot Games/Riot Client/Data',
            'Users/tester/Library/Caches/com.riotgames.RiotClient',
            '.init.pid.stale-1-2',
        ):
            self.assertFalse(os.path.lexists(self.prefix / gone), gone)
        # Stale pre-reset .before-platform-change copies were replaced by the fresh backup of the prior identity.
        self.assertNotEqual(
            (self.prefix / 'private/etc/darling-platform.plist.before-platform-change').read_text(errors='ignore'),
            'first-plist\n',
        )

        for kept in (
            'private/etc/darling-system.prop',
            'Applications/League of Legends.app/Contents/LoL/game.bin',
            'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices',
            'Users/Shared/Riot Games/Metadata/league_of_legends.live',
            'Users/tester/Library/Application Support/Riot Games/Riot Client/Config/RiotClientSettings.yaml',
            'Users/tester/Library/Caches/mesa/shader',
        ):
            self.assertTrue((self.prefix / kept).exists(), kept)

        updated = plistlib.loads((self.prefix / 'private/etc/darling-platform.plist').read_bytes())['properties']
        self.assertNotEqual(updated['IOPlatformUUID'], self.old_uuid)
        self.assertNotEqual(updated['IOPlatformSerialNumber'], self.old_serial)
        self.assertEqual(updated['model'], 'MacBookPro16,1')

    def test_keep_identity_and_custom_backup_dir_do_not_overwrite_archives(self):
        custom_dir = self.root / 'my backups'
        before_plist = (self.prefix / 'private/etc/darling-platform.plist').read_bytes()
        before_conf = (self.prefix / 'private/etc/darling-smbios.conf').read_bytes()

        first = self.run_script('--keep-identity', '--backup-dir', str(custom_dir), '--yes')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual((self.prefix / 'private/etc/darling-platform.plist').read_bytes(), before_plist)
        self.assertEqual((self.prefix / 'private/etc/darling-smbios.conf').read_bytes(), before_conf)

        # Recreate a log and run again in the same second; the first archive must survive.
        (self.prefix / 'Users/Shared/Riot Games/machine.cfg').write_text('second\n')
        second = self.run_script('--keep-identity', f'--backup-dir={custom_dir}', '--yes')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        archives = sorted(custom_dir.glob('prefix-cleanup-*.tar.gz'))
        self.assertEqual(len(archives), 2)

    def test_running_client_is_refused_without_touching_prefix(self):
        (self.proc / '4242').mkdir()
        (self.proc / '4242/cmdline').write_bytes(b'/x/Riot Client.app/Contents/MacOS/RiotClientServices\0')
        result = self.run_script('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('A Riot Client or League process is running', result.stderr)
        self.assertEqual(self.stops(), 0)
        self.assertTrue((self.prefix / 'Users/Shared/Riot Games/machine.cfg').exists())

    def test_requires_confirmation_and_refuses_invalid_prefixes(self):
        no_tty = self.run_script()
        self.assertNotEqual(no_tty.returncode, 0)
        self.assertIn('pass --yes', no_tty.stderr)
        self.assertEqual(self.stops(), 0)

        declined = self.run_script(answer='n')
        self.assertNotEqual(declined.returncode, 0)
        self.assertIn('Aborted.', declined.stderr)
        self.assertEqual(self.stops(), 0)

        plain = self.root / 'not-a-prefix'
        (plain / 'Users/tester').mkdir(parents=True)
        bad = self.run_script('--yes', prefix=plain)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('is not a Darling prefix', bad.stderr)

        (self.home / 'private/etc').mkdir(parents=True)
        (self.home / 'private/etc/darling-system.prop').write_text('profile=compat\n')
        (self.home / 'Users/tester').mkdir(parents=True)
        home_refused = self.run_script('--yes', prefix=self.home)
        self.assertNotEqual(home_refused.returncode, 0)
        self.assertIn('home directory', home_refused.stderr)

    def test_missing_stop_helper_aborts_before_any_changes(self):
        (self.repo / 'tools/stop-darling-runtime.py').unlink()
        missing_stop = self.run_script('--yes')
        self.assertNotEqual(missing_stop.returncode, 0)
        self.assertIn('darling shutdown', missing_stop.stderr)
        self.assertEqual(self.stops(), 0)
        self.assertTrue((self.prefix / 'Users/Shared/Riot Games/machine.cfg').exists())

    def test_symlinked_parent_outside_prefix_is_never_archived_or_deleted(self):
        external_logs = self.outside / 'parent-logs'
        (external_logs / 'Riot Games').mkdir(parents=True)
        (external_logs / 'Riot Games/keep.log').write_text('precious\n')
        shutil.rmtree(self.guest / 'Library/Logs')
        os.symlink(external_logs, self.guest / 'Library/Logs')

        result = self.run_script('--keep-identity', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # External directory behind the symlinked parent must remain completely untouched.
        self.assertEqual((external_logs / 'Riot Games/keep.log').read_text(), 'precious\n')
        archives = sorted((self.repo / 'build/prefix-cleanup-backups').glob('prefix-cleanup-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0], 'r:gz') as tar:
            names = set(tar.getnames())
        self.assertFalse(any('keep.log' in name for name in names))

    def test_backup_dir_validation_preserves_existing_mode_and_resolves_relative_to_cwd(self):
        # Backup directory inside prefix (e.g. cache glob) must be refused before stopping runtime.
        inside_cache = self.guest / 'Library/Caches/com.riotgames.backups'
        refused = self.run_script('--backup-dir', str(inside_cache), '--yes')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('inside the Darling prefix', refused.stderr)
        self.assertEqual(self.stops(), 0)
        self.assertTrue((self.prefix / 'Users/Shared/Riot Games/machine.cfg').exists())

        # Existing backup directory keeps its original permissions (not forced to 0700).
        existing = self.root / 'existing-backups'
        existing.mkdir(mode=0o755)
        os.chmod(existing, 0o755)
        res = self.run_script('--keep-identity', '--backup-dir', str(existing), '--yes')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(existing.stat().st_mode & 0o777, 0o755)

        # Relative --backup-dir resolves against caller cwd, not repo root.
        (self.prefix / 'Users/Shared/Riot Games/machine.cfg').write_text('rel\n')
        caller_cwd = self.root / 'caller cwd'
        caller_cwd.mkdir()
        env = dict(
            os.environ,
            HOME=str(self.home),
            DPREFIX=str(self.prefix),
            RIOT_GUEST_HOME=str(self.guest),
            CLEAN_PREFIX_PROC_ROOT=str(self.proc),
            CLEAN_PREFIX_RUNTIME_ROOT=str(self.runtime),
        )
        env.pop('DARLING_WORKSPACE', None)
        rel_run = subprocess.run(
            ['sh', str(self.repo / 'scripts/clean-prefix.sh'), '--keep-identity',
             '--backup-dir', 'rel-backups', '--yes'],
            cwd=str(caller_cwd), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, check=False, env=env, timeout=60,
        )
        self.assertEqual(rel_run.returncode, 0, rel_run.stdout + rel_run.stderr)
        self.assertEqual(len(list((caller_cwd / 'rel-backups').glob('prefix-cleanup-*.tar.gz'))), 1)
        self.assertFalse((self.repo / 'rel-backups').exists())


if __name__ == '__main__':
    unittest.main()
