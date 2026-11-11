"""install-runtime.sh installs the Electron 43 framework stubs and builds dc-codesign (#174)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallRuntimeFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / 'scripts/install-runtime.sh').read_text()

    def test_frameworks_are_installed_before_validation_and_signing(self):
        install = self.script.index('tools/build-riot-electron43-frameworks.py --install')
        self.assertLess(install, self.script.index('validate-riot-runtime.py'))
        self.assertLess(install, self.script.index('sign-darling-runtime.sh" $sign_args'))

    def test_dc_codesign_is_built_before_signing(self):
        build = self.script.index('make -s -C "$repo/tools/dc-codesign" dc-codesign')
        self.assertLess(build, self.script.index('sign-darling-runtime.sh" $sign_args'))
        self.assertTrue((ROOT / 'tools/dc-codesign/Makefile').is_file())

    def test_signing_hint_names_the_real_makefile(self):
        sign = (ROOT / 'tools/sign-darling-runtime.sh').read_text()
        self.assertIn('make -C $here/dc-codesign', sign)


if __name__ == '__main__':
    unittest.main()
