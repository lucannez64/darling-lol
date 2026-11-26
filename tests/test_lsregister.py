"""lsregister exists where macOS has it and registers bundles into the LaunchServices database.

Riot Client's agent registers its bundle with `lsregister -R -f` from
CoreServices.framework/Frameworks/LaunchServices.framework/Support. On macOS 15.5
lsregister exits 0 for a bundle, 1 with `failed to scan <path>: -43` for a
missing path and -10811 for a path that is not a bundle, 1 with no arguments
and 255 for an unknown option. Darling had no lsregister, so registration
failed. These checks read the patch; CI cannot run Darling.
"""
from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = 'patches/darling.patch'
LSD = 'src/frameworks/CoreServices/src/LaunchServices/launchservicesd/'


def section(path):
    sections = (ROOT / PATCH).read_text().split('diff --git ')
    return next(s for s in sections if s.startswith('a/' + path + ' b/'))


def added(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def removed(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('-') and not line.startswith('---'))


class LsregisterTests(unittest.TestCase):
    def test_installed_at_the_macos_path(self):
        cmake = added(LSD + 'CMakeLists.txt')
        self.assertIn('add_darling_executable(lsregister lsregister.m LSBundle.m)', cmake)
        self.assertIn('CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework', cmake)
        self.assertIn('DESTINATION "${lsregister_framework}/Versions/A/Support"', cmake)
        self.assertIn('InstallSymlink("Versions/Current/Support"', cmake)

    def test_registers_into_the_launchservicesd_database(self):
        tool = added(LSD + 'lsregister.m')
        self.assertIn('[LSBundle registerBundleAtPath:path]', tool)
        self.assertIn('[LSBundle deleteBundleAtPath:path]', tool)
        self.assertIn('[LSBundle databaseIsOpen]', tool)
        # -R also registers the apps and frameworks inside the bundle.
        self.assertIn('enumeratorAtPath:path', tool)

    def test_exit_codes_and_messages_match_macos(self):
        tool = added(LSD + 'lsregister.m')
        self.assertIn('return 255;', tool)  # unknown option
        self.assertIn('"failed to scan %s: %d\\n"', tool)
        self.assertIn('unregister ? -10814 : -43', tool)
        self.assertIn('-10811', tool)
        no_args = tool.split('if ([paths count] == 0)', 1)[1].split('}', 1)[0]
        self.assertIn('return 1;', no_args)

    def test_accepts_the_options_riot_client_passes(self):
        tool = added(LSD + 'lsregister.m')
        self.assertIn('strcmp(arg, "-R") == 0', tool)
        self.assertIn('strcmp(arg, "-f") == 0', tool)

    def test_bundle_delete_runs_the_statement(self):
        # executeQuery: only prepares a statement; deletions never happened.
        bundle = added(LSD + 'LSBundle.m')
        self.assertIn('[g_database executeUpdate: @"delete from bundle where path = ?", path];', bundle)
        self.assertIn('[g_database executeQuery: @"delete from bundle where path = ?", path];',
                      removed(LSD + 'LSBundle.m'))

    def test_manifest_lists_lsregister_as_new(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        main = next(c for c in manifest['components'] if c['path'] == '.')
        self.assertIn(LSD + 'lsregister.m', main['new_files'])


if __name__ == '__main__':
    unittest.main()
