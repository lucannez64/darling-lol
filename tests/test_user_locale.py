"""Language preferences live in the global domain, as on macOS 15.5; never starts Darling."""
import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('user_locale', ROOT / 'tools/provision-user-locale.py')
locale_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(locale_tool)


class UserLocaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prefix = Path(self.tmp.name).resolve()
        self.prefs = self.prefix / 'Users/pedro/Library/Preferences'
        self.prefs.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, data):
        (self.prefs / name).write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))

    def get(self, name):
        return plistlib.loads((self.prefs / name).read_bytes())

    def run_tool(self, *extra):
        return locale_tool.main(['--prefix', str(self.prefix), '--guest-user', 'pedro',
                                 '--locale', 'en_GB', *extra])

    def test_global_domain_gets_macos_language_keys_and_app_copies_go(self):
        self.put('.GlobalPreferences.plist', {'AppleMiniaturizeOnDoubleClick': False})
        self.put('com.riotgames.LeagueofLegends.GameClient.plist', {'AppleLanguages': []})
        self.put('com.riotgames.RiotGames.RiotClient.plist',
                 {'AppleLanguages': [], 'AppleTextDirection': True})
        self.put('com.example.kept.plist', {'AppleLanguages': ['fr-FR'], 'Other': 1})
        self.assertEqual(self.run_tool('--check'), 1)
        self.assertEqual(self.run_tool('--write'), 0)
        self.assertEqual(self.get('.GlobalPreferences.plist'), {
            'AppleMiniaturizeOnDoubleClick': False, 'AppleLanguages': ['en-GB'], 'AppleLocale': 'en_GB',
            'AppleLanguagesSchemaVersion': 5400, 'AppleLanguagesDidMigrate': '15.5'})
        self.assertFalse((self.prefs / 'com.riotgames.LeagueofLegends.GameClient.plist').exists())
        self.assertEqual(self.get('com.riotgames.RiotGames.RiotClient.plist'), {'AppleTextDirection': True})
        # A real per-application language choice is the user's, not a Darling artifact.
        self.assertEqual(self.get('com.example.kept.plist'), {'AppleLanguages': ['fr-FR'], 'Other': 1})
        self.assertEqual((self.prefs / '.GlobalPreferences.plist').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_tool('--check'), 0)

    def test_existing_user_languages_are_kept(self):
        self.put('.GlobalPreferences.plist', {'AppleLanguages': ['pl-PL'], 'AppleLocale': 'pl_PL'})
        self.assertEqual(self.run_tool('--write'), 0)
        self.assertEqual(self.get('.GlobalPreferences.plist'), {'AppleLanguages': ['pl-PL'], 'AppleLocale': 'pl_PL'})

    def test_preview_changes_nothing_and_running_prefix_is_refused(self):
        self.put('com.riotgames.LeagueofLegends.GameClient.plist', {'AppleLanguages': []})
        self.assertEqual(self.run_tool(), 0)
        self.assertTrue((self.prefs / 'com.riotgames.LeagueofLegends.GameClient.plist').exists())
        self.assertFalse((self.prefs / '.GlobalPreferences.plist').exists())
        (self.prefix / '.init.pid').write_text('%d\n' % __import__('os').getpid())
        with self.assertRaises(ValueError):
            self.run_tool('--write')

    def test_invalid_locale_is_rejected(self):
        with self.assertRaises(ValueError):
            locale_tool.global_values('en_GB.UTF-8')

    def test_foundation_no_longer_copies_languages_into_app_domains(self):
        patch = (ROOT / 'patches/foundation.patch').read_text()
        self.assertIn('-        [standardDefaults setObject:[NSLocale preferredLanguages] forKey:@"AppleLanguages"];', patch)
        self.assertIn('-        [standardDefaults setObject:[[NSLocale systemLocale] languageCode] forKey:@"AppleLocale"];', patch)

    def test_install_provisions_language_preferences(self):
        script = (ROOT / 'scripts/install-runtime.sh').read_text()
        self.assertIn('tools/provision-user-locale.py" --prefix "$prefix_dir" --write', script)
        self.assertLess(script.index('provision-framework-info.py'), script.index('provision-user-locale.py'))


if __name__ == '__main__':
    unittest.main()
