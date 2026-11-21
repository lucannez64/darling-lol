"""install-runtime.sh installs the Electron 43 framework stubs and builds dc-codesign (#174)."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
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

    def test_prefix_signing_includes_bootstrap_libsystem_kernel(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / 'prefix'
            thin_macho = b'\xcf\xfa\xed\xfe' + b'\x00' * 12
            targets = [
                prefix / 'usr/lib/system/libsystem_kernel.dylib',
                prefix / 'System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/LaunchServices',
                prefix / 'System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/CarbonCore.framework/Versions/A/CarbonCore',
                prefix / 'System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/libvDSP.dylib',
                prefix / 'Users/tester/Library/Audio/Plug-Ins/Components/DarlingRiotAudio.component/Contents/MacOS/CoreAudio',
                prefix / 'Users/tester/libDarlingXShm.dylib',
                prefix / 'Users/tester/libDarlingWayland.dylib',
            ]
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(thin_macho)

            stub_dcc = root / 'dc-codesign'
            stub_dcc.write_text('#!/bin/sh\nexit 1\n')
            stub_dcc.chmod(stub_dcc.stat().st_mode | stat.S_IXUSR)

            stub_rcodesign = root / 'rcodesign'
            stub_rcodesign.write_text('#!/bin/sh\nexit 0\n')
            stub_rcodesign.chmod(stub_rcodesign.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env.update({
                'DPREFIX': str(prefix),
                'DCC': str(stub_dcc),
                'RCODESIGN': str(stub_rcodesign),
            })
            result = subprocess.run(
                ['sh', str(ROOT / 'tools/sign-darling-runtime.sh'),
                 '--prefix', '--dry-run', '--allow-running', '--no-trust'],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            for target in targets:
                self.assertIn(f'would sign: {target}', result.stdout)

    def test_existing_prefixes_normalize_system_version_after_stopping_runtime(self):
        stop = self.script.index('stop-darling-runtime.py')
        normalize = self.script.index('configure-darling-platform.py --prefix "$prefix_dir" --normalize-system-version --write')
        self.assertLess(stop, normalize)


if __name__ == '__main__':
    unittest.main()
