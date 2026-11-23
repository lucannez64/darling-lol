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
        lol = prefix / 'Applications/League of Legends.app/Contents/LoL'
        for folder in (
            prefix / 'private/etc',
            lol / 'Logs/GameLogs',
            lol / 'Config',
            lol / 'Saved/webcache',
            prefix / 'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS',
            prefix / 'Users/Shared/Riot Games/Metadata/league_of_legends.live',
            guest / 'Library/Application Support/Riot Games/Riot Client/Config',
            guest / 'Library/Application Support/Riot Games/Riot Client/Data',
            guest / 'Library/Application Support/Riot Games/Riot Client/Crashes',
            guest / 'Library/Application Support/Riot Games/League of Legends/Config',
            guest / 'Library/Application Support/Riot Games/League of Legends/Data',
            guest / 'Library/Application Support/Riot Games/League of Legends/LeagueClient/Crashes',
            guest / 'Library/Application Support/Riot Client/Local Storage/leveldb',
            guest / 'Library/Application Support/riot-client-ux/sentry',
            guest / 'Library/Application Support/riot-client-ux/Crashpad',
            guest / 'Library/Application Support/CEF',
            guest / 'Library/Application Support/Other',
            guest / 'Library/Logs/Riot Games/Riot Client',
            guest / 'Library/Caches/com.riotgames.RiotClient',
            guest / 'Library/Caches/mesa',
            guest / 'Library/Preferences',
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
        (prefix / 'System/Library/CoreServices').mkdir(parents=True, exist_ok=True)
        (prefix / 'System/Library/CoreServices/SystemVersion.plist.before-platform-change').write_text('old-sv\n')
        (lol / 'game.bin').write_text('game\n')
        (lol / 'Logs/GameLogs/r3dlog.txt').write_text('match\n')
        (lol / 'Config/LeagueClientSettings.yaml').write_text('install-identifier: old-id\n')
        (lol / 'Config/LCUAccountPreferences.yaml').write_text('account: old\n')
        (lol / 'Config/PersistedSettings.json').write_text('{"old": true}\n')
        (lol / 'Config/game.cfg').write_text('[General]\n')
        (lol / 'Config/input.ini').write_text('[GameEvents]\n')
        (lol / 'Config/LCULocalPreferences.yaml').write_text('potatoModeEnabled: true\n')
        (lol / 'Config/PerksPreferences.yaml').write_text('currentPageId: 1234\n')
        (lol / 'lockfile').write_text('LeagueClient:1:2:pass:https\n')
        (prefix / 'Users/Shared/Riot Games/Metadata/league_of_legends.live/league_of_legends.live.update-status.json').write_text('{"productSessionId":"old"}\n')
        (prefix / 'Users/Shared/Riot Games/Metadata/league_of_legends.live/league_of_legends.live.product_settings.yaml').write_text('keep\n')
        (lol / 'Saved/webcache/Cookies').write_text('cookie\n')
        (prefix / 'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices').write_text('bin\n')
        (prefix / 'Users/Shared/Riot Games/machine.cfg').write_text('machine-shared\n')
        (guest / 'Library/Application Support/Riot Games/machine.cfg').write_text('machine-user\n')
        (guest / 'Library/Application Support/Riot Games/Riot Client/Config/RiotClientSettings.yaml').write_text('installation_id: old\n')
        (guest / 'Library/Application Support/Riot Games/Riot Client/Data/RiotGamesPrivateSettings.yaml').write_text('session\n')
        (guest / 'Library/Application Support/Riot Games/Riot Client/Crashes/rc.dmp').write_text('dmp\n')
        (guest / 'Library/Application Support/Riot Games/League of Legends/Config/cfg.yaml').write_text('cfg\n')
        (guest / 'Library/Application Support/Riot Games/League of Legends/Data/data.bin').write_text('data\n')
        (guest / 'Library/Application Support/Riot Games/League of Legends/LeagueClient/Crashes/lc.dmp').write_text('dmp\n')
        (guest / 'Library/Application Support/Riot Client/Cookies').write_text('hmt_id\n')
        (guest / 'Library/Application Support/Riot Client/Local Storage/leveldb/000003.log').write_text('statsig\n')
        (guest / 'Library/Application Support/riot-client-ux/sentry/scope_v3.json').write_text('{"user":{"id":"old"}}\n')
        (guest / 'Library/Application Support/riot-client-ux/Crashpad/settings.dat').write_text('crashpad\n')
        (guest / 'Library/Application Support/CEF/cache.dat').write_text('cef\n')
        (guest / 'Library/Keychains/Darling').mkdir(parents=True, exist_ok=True)
        (guest / 'Library/Keychains/Darling/items-v1.plist').write_text('keychain\n')
        (guest / 'Library/Cookies').mkdir(parents=True, exist_ok=True)
        (guest / 'Library/Cookies/Cookies.binarycookies').write_text('cookies\n')
        (guest / 'Library/HTTPStorages').mkdir(parents=True, exist_ok=True)
        (guest / 'Library/HTTPStorages/com.riotgames.httpstorages').write_text('http\n')
        (guest / 'Library/WebKit').mkdir(parents=True, exist_ok=True)
        (guest / 'Library/WebKit/WebsiteData').write_text('webkit\n')
        (guest / 'Library/Saved Application State').mkdir(parents=True, exist_ok=True)
        (guest / 'Library/Saved Application State/com.riotgames.savedState').write_text('state\n')
        (guest / 'Library/Application Support/Other/keep.txt').write_text('keep\n')
        (guest / 'Library/Logs/Riot Games/Riot Client/rc.log').write_text('riot log\n')
        (guest / 'Library/Caches/com.riotgames.RiotClient/cache.bin').write_text('cache\n')
        (guest / 'Library/Caches/mesa/shader').write_text('shader\n')
        (guest / 'Library/Preferences/com.riotgames.RiotClient.plist').write_text('pref\n')
        (guest / 'Library/Preferences/other.plist').write_text('keep\n')
        (prefix / '.init.pid.stale-1-2').write_text('1\n')
        (outside / 'external-logs/secret.log').write_text('keep\n')
        os.symlink(outside / 'external-logs', guest / 'Library/Logs/Riot Client')

    def run_script(self, *arguments, answer=None, prefix=None, cwd=None, **extra):
        env = dict(
            os.environ,
            HOME=str(self.home),
            DPREFIX=str(prefix or self.prefix),
            RIOT_GUEST_HOME=str((prefix or self.prefix) / 'Users/tester'),
            CLEAN_PREFIX_PROC_ROOT=str(self.proc),
            CLEAN_PREFIX_RUNTIME_ROOT=str(self.runtime),
            # The re-exec itself is covered by test_clean_prefix_toolbox; keep this
            # suite host-independent even when a real toolbox container exists.
            CLEAN_PREFIX_REEXEC='1',
            **extra,
        )
        env.pop('DARLING_WORKSPACE', None)
        command = ['sh', str(self.repo / 'scripts/clean-prefix.sh'), *arguments]
        if answer is None:
            return subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, check=False, env=env, timeout=60)
        master, slave = pty.openpty()
        try:
            os.write(master, answer.encode() + b'\n')
            return subprocess.run(command, cwd=cwd, stdin=slave, capture_output=True,
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
        for archived in (
            'Users/tester/Library/Logs/Riot Games/Riot Client/rc.log',
            'Applications/League of Legends.app/Contents/LoL/Logs/GameLogs/r3dlog.txt',
            'Users/Shared/Riot Games/machine.cfg',
            'Users/tester/Library/Application Support/Riot Games/machine.cfg',
            'Users/tester/Library/Application Support/Riot Games/Riot Client/Config/RiotClientSettings.yaml',
            'Users/tester/Library/Application Support/Riot Games/Riot Client/Data/RiotGamesPrivateSettings.yaml',
            'Users/tester/Library/Application Support/Riot Games/Riot Client/Crashes/rc.dmp',
            'Users/tester/Library/Application Support/Riot Games/League of Legends/Config/cfg.yaml',
            'Users/tester/Library/Application Support/Riot Games/League of Legends/Data/data.bin',
            'Users/tester/Library/Application Support/Riot Games/League of Legends/LeagueClient/Crashes/lc.dmp',
            'Users/tester/Library/Application Support/Riot Client/Cookies',
            'Users/tester/Library/Application Support/Riot Client/Local Storage/leveldb/000003.log',
            'Users/tester/Library/Application Support/riot-client-ux/sentry/scope_v3.json',
            'Users/tester/Library/Application Support/riot-client-ux/Crashpad/settings.dat',
            'Users/tester/Library/Application Support/CEF/cache.dat',
            'Users/tester/Library/Preferences/com.riotgames.RiotClient.plist',
            'Applications/League of Legends.app/Contents/LoL/Config/LeagueClientSettings.yaml',
            'Applications/League of Legends.app/Contents/LoL/Config/LCUAccountPreferences.yaml',
            'Applications/League of Legends.app/Contents/LoL/Config/PersistedSettings.json',
            'Applications/League of Legends.app/Contents/LoL/Config/game.cfg',
            'Applications/League of Legends.app/Contents/LoL/Config/input.ini',
            'Applications/League of Legends.app/Contents/LoL/Config/LCULocalPreferences.yaml',
            'Applications/League of Legends.app/Contents/LoL/Config/PerksPreferences.yaml',
            'private/etc/darling-platform.plist',
            'private/etc/darling-smbios.conf',
            'private/etc/darling-platform.plist.before-platform-change',
            'private/etc/darling-smbios.conf.before-platform-change',
        ):
            self.assertIn(archived, names, archived)
        # Escaping symlink must not be archived and its external target must remain intact.
        self.assertFalse(any('secret.log' in name for name in names))
        self.assertEqual((self.outside / 'external-logs/secret.log').read_text(), 'keep\n')

        for gone in (
            'Users/tester/Library/Logs/Riot Games',
            'Users/tester/Library/Logs/Riot Client',
            'Applications/League of Legends.app/Contents/LoL/Logs',
            'Applications/League of Legends.app/Contents/LoL/Saved/webcache',
            'Applications/League of Legends.app/Contents/LoL/Config/LeagueClientSettings.yaml',
            'Applications/League of Legends.app/Contents/LoL/Config/LCUAccountPreferences.yaml',
            'Applications/League of Legends.app/Contents/LoL/Config/PersistedSettings.json',
            'Applications/League of Legends.app/Contents/LoL/Config/game.cfg',
            'Applications/League of Legends.app/Contents/LoL/Config/input.ini',
            'Applications/League of Legends.app/Contents/LoL/Config/LCULocalPreferences.yaml',
            'Applications/League of Legends.app/Contents/LoL/Config/PerksPreferences.yaml',
            'Applications/League of Legends.app/Contents/LoL/lockfile',
            'Users/Shared/Riot Games/Metadata/league_of_legends.live/league_of_legends.live.update-status.json',
            'Users/Shared/Riot Games/machine.cfg',
            'Users/tester/Library/Application Support/Riot Games',
            'Users/tester/Library/Application Support/Riot Client',
            'Users/tester/Library/Application Support/riot-client-ux',
            'Users/tester/Library/Application Support/CEF',
            'Users/tester/Library/Keychains/Darling/items-v1.plist',
            'Users/tester/Library/Cookies',
            'Users/tester/Library/HTTPStorages',
            'Users/tester/Library/WebKit',
            'Users/tester/Library/Saved Application State',
            'Users/tester/Library/Preferences/com.riotgames.RiotClient.plist',
            'Users/tester/Library/Caches/com.riotgames.RiotClient',
            '.init.pid.stale-1-2',
            # No *.before-platform-change identity copies left readable inside the prefix.
            'private/etc/darling-platform.plist.before-platform-change',
            'private/etc/darling-smbios.conf.before-platform-change',
            'System/Library/CoreServices/SystemVersion.plist.before-platform-change',
        ):
            self.assertFalse(os.path.lexists(self.prefix / gone), gone)

        for kept in (
            'private/etc/darling-system.prop',
            'Applications/League of Legends.app/Contents/LoL/game.bin',
            'Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices',
            'Users/Shared/Riot Games/Metadata/league_of_legends.live/league_of_legends.live.product_settings.yaml',
            'Users/tester/Library/Application Support/Other/keep.txt',
            'Users/tester/Library/Preferences/other.plist',
            'Users/tester/Library/Caches/mesa/shader',
        ):
            self.assertTrue((self.prefix / kept).exists(), kept)

        updated = plistlib.loads((self.prefix / 'private/etc/darling-platform.plist').read_bytes())['properties']
        self.assertNotEqual(updated['IOPlatformUUID'], self.old_uuid)
        self.assertNotEqual(updated['IOPlatformSerialNumber'], self.old_serial)
        self.assertEqual(updated['model'], 'MacBookPro16,1')

    def test_keep_game_settings_preserves_game_cfg_input_ini_and_local_prefs(self):
        result = self.run_script('--keep-identity', '--keep-game-settings', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lol_cfg = self.prefix / 'Applications/League of Legends.app/Contents/LoL/Config'
        self.assertTrue((lol_cfg / 'game.cfg').exists())
        self.assertTrue((lol_cfg / 'input.ini').exists())
        self.assertTrue((lol_cfg / 'LCULocalPreferences.yaml').exists())
        self.assertFalse((lol_cfg / 'PerksPreferences.yaml').exists())
        self.assertFalse((lol_cfg / 'LeagueClientSettings.yaml').exists())
        self.assertFalse((lol_cfg / 'LCUAccountPreferences.yaml').exists())
        self.assertFalse((lol_cfg / 'PersistedSettings.json').exists())

    def test_keep_identity_and_custom_backup_dir_do_not_overwrite_archives(self):
        custom_dir = self.root / 'my backups'
        before_plist = (self.prefix / 'private/etc/darling-platform.plist').read_bytes()
        before_conf = (self.prefix / 'private/etc/darling-smbios.conf').read_bytes()

        first = self.run_script('--keep-identity', '--backup-dir', str(custom_dir), '--yes')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual((self.prefix / 'private/etc/darling-platform.plist').read_bytes(), before_plist)
        self.assertEqual((self.prefix / 'private/etc/darling-smbios.conf').read_bytes(), before_conf)
        self.assertTrue((self.prefix / 'private/etc/darling-platform.plist.before-platform-change').exists())

        # Recreate a file and run again in the same second; the first archive must survive.
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
        # External directory behind the symlinked parent must remain completely untouched
        # and the skip must be reported rather than happening silently.
        self.assertEqual((external_logs / 'Riot Games/keep.log').read_text(), 'precious\n')
        self.assertIn('Skipped (outside prefix):', result.stderr)
        archives = sorted((self.repo / 'build/prefix-cleanup-backups').glob('prefix-cleanup-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0], 'r:gz') as tar:
            names = set(tar.getnames())
        self.assertFalse(any('keep.log' in name for name in names))

    def test_backup_dir_validation_preserves_existing_mode_and_resolves_paths(self):
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
        rel_run = self.run_script('--keep-identity', '--backup-dir', 'rel-backups', '--yes', cwd=str(caller_cwd))
        self.assertEqual(rel_run.returncode, 0, rel_run.stdout + rel_run.stderr)
        self.assertEqual(len(list((caller_cwd / 'rel-backups').glob('prefix-cleanup-*.tar.gz'))), 1)
        self.assertFalse((self.repo / 'rel-backups').exists())

        # --backup-dir=~/... expands against HOME rather than creating a literal '~' directory.
        (self.prefix / 'Users/Shared/Riot Games/machine.cfg').write_text('tilde\n')
        tilde_run = self.run_script('--keep-identity', '--backup-dir=~/tilde-backups', '--yes', cwd=str(caller_cwd))
        self.assertEqual(tilde_run.returncode, 0, tilde_run.stdout + tilde_run.stderr)
        self.assertEqual(len(list((self.home / 'tilde-backups').glob('prefix-cleanup-*.tar.gz'))), 1)
        self.assertFalse((caller_cwd / '~').exists())


if __name__ == '__main__':
    unittest.main()
