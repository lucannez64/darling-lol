"""Smoke checks for Riot Client 142 (Electron 43 / Chromium 150) under Darling.

The 142 client is the first build that binds symbols Darling does not provide at
all. Both classes of failure stop it in dyld, before the first window, so the
checks below can run without starting the client: they look at the bundle the
prefix actually holds and at the overlays the guest resolves.

    dyld: Symbol not found: _OBJC_CLASS_$_QLPreviewPanel         (QuickLookUI)
    dyld: Symbol not found: _OBJC_CLASS_$_NSConstantIntegerNumber (Foundation)
    dyld: Symbol not found: ___NSArray0__struct                  (CoreFoundation)

A live launch is opt-in with RCLOL_142_LAUNCH=1 because it opens a window. It
runs only when the installed Riot Client.app already is a 142 bundle, so the
test never swaps the launcher's link; it asserts the client gets past dyld into
Electron, and that its local UI load does not fail with ERR_NETWORK_CHANGED.

Set RCLOL_142_APP to inspect one bundle (for example a backup bundle kept next
to the installed one). Otherwise every 142 bundle in the prefix is used.
"""
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'build/config.json'
RUNNER = ROOT / 'build/runtime/tools/run-riot-client.sh'
# Frameworks Electron 43 imports non-weakly: dyld refuses the image without them.
STUB_FRAMEWORKS = ('QuickLookUI', 'MediaPlayer', 'CoreML', 'CoreHaptics', 'Accessibility')
# Constant-literal classes and pointer variables clang emits for @42, @[], @{}.
CONSTANTS = {
    'CoreFoundation': ('_OBJC_CLASS_$_NSConstantArray', '_OBJC_CLASS_$_NSConstantDictionary',
                       '___NSArray0__struct', '___NSDictionary0__struct'),
    'Foundation': ('_OBJC_CLASS_$_NSConstantIntegerNumber', '_OBJC_CLASS_$_NSConstantDoubleNumber',
                   '_OBJC_CLASS_$_NSConstantFloatNumber'),
}


def configuration():
    if not CONFIG.exists():
        raise unittest.SkipTest('run scripts/configure.py first')
    return json.loads(CONFIG.read_text())


def candidates():
    """142 bundles in the prefix, or the one named by RCLOL_142_APP."""
    chosen = os.environ.get('RCLOL_142_APP')
    if chosen:
        return [Path(chosen)]
    config = configuration()
    prefix = Path(os.environ.get('DPREFIX') or config['prefix'])
    shared = prefix / 'Users/Shared/Riot Games'
    found = sorted(p for p in shared.glob('Riot Client.app*') if (p / 'Contents/Info.plist').is_file())
    return found


def version(bundle):
    """Riot ships the client version as FileVersion; older bundles also carry a short version."""
    try:
        with (bundle / 'Contents/Info.plist').open('rb') as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return ''
    return info.get('FileVersion') or info.get('CFBundleShortVersionString') or ''


def bundles_142():
    found = [bundle for bundle in candidates() if version(bundle).startswith('142.')]
    if not found:
        raise unittest.SkipTest('no Riot Client 142 bundle in the prefix (set RCLOL_142_APP)')
    return found


def symbols(binary):
    """Exported names in a Mach-O symbol table."""
    out = subprocess.run(['llvm-nm', '-gU', '--arch=x86_64', str(binary)],
                         capture_output=True, text=True, check=True).stdout
    return {line.split()[-1] for line in out.splitlines() if line.strip()}


def imports_from(binary, framework):
    """Non-weak imports attributed to one framework by `nm -m`'s "(from ...)" tag.

    Weak imports are skipped: Chromium disables those features when the image is
    absent, so they do not stop the process in dyld.
    """
    out = subprocess.run(['llvm-nm', '-m', '-u', '--arch=x86_64', str(binary)],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        if '(weak' in line or f'(from {framework})' not in line:
            continue
        found.append(line.split()[-3])
    return found


def overlay_framework(home, name):
    for binary in (home / 'RuntimeFrameworks' / f'{name}.framework/Versions/A' / name,
                   home / 'RuntimeFrameworks' / f'{name}.framework/Versions/C' / name,
                   home / 'RuntimeFrameworks' / f'{name}.framework' / name):
        if binary.is_file():
            return binary
    return None


@unittest.skipUnless(shutil.which('llvm-nm'), 'llvm-nm required to read Mach-O symbol tables')
class RiotClient142Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = configuration()
        prefix = Path(os.environ.get('DPREFIX') or config['prefix'])
        cls.home = prefix / config['guest_home'].lstrip('/')
        cls.bundles = bundles_142()

    def electron(self, bundle):
        """The Electron image, which sits in the outer app or the nested client app."""
        for framework in bundle.glob('Contents/Frameworks/**/Electron Framework.framework/Electron Framework'):
            if framework.is_file():
                return framework
        self.fail(f'{bundle.name}: no Electron Framework image')

    def test_bundles_ship_electron_43(self):
        for bundle in self.bundles:
            electron = self.electron(bundle)
            strings = subprocess.run(['strings', '-a', str(electron)], capture_output=True, text=True,
                                     check=True).stdout
            self.assertIn('Electron/43', strings, f'{bundle.name}: not the Electron 43 runtime')
            self.assertRegex(strings, r'Chrome/1[4-9][0-9]\.', f'{bundle.name}: unexpected Chromium version')

    def test_framework_stubs_cover_nonweak_imports(self):
        missing = {}
        for bundle in self.bundles:
            electron = self.electron(bundle)
            for name in STUB_FRAMEWORKS:
                binary = overlay_framework(self.home, name)
                if binary is None:
                    missing[f'{bundle.name}:{name}'] = [
                        f'stub not installed in {self.home}/RuntimeFrameworks']
                    continue
                exported = symbols(binary)
                absent = sorted(symbol for symbol in imports_from(electron, name) if symbol not in exported)
                if absent:
                    missing[f'{bundle.name}:{name}'] = absent
        self.assertEqual({}, missing, 'framework stubs miss imports the client binds')

    def test_constant_literal_symbols_are_exported(self):
        missing = {}
        for framework, required in CONSTANTS.items():
            binary = overlay_framework(self.home, framework)
            if binary is None:
                missing[framework] = [f'overlay missing from {self.home}/RuntimeFrameworks']
                continue
            have = symbols(binary)
            absent = [symbol for symbol in required if symbol not in have]
            if absent:
                missing[framework] = absent
        self.assertEqual({}, missing, 'overlays miss constant-literal classes Electron 43 binds')

    @unittest.skipUnless(os.environ.get('RCLOL_142_LAUNCH') == '1',
                         'set RCLOL_142_LAUNCH=1 to start the client (opens a window)')
    def test_client_gets_past_dyld(self):
        if not RUNNER.is_file():
            self.skipTest('build the runtime first (scripts/build.sh)')
        installed = Path(os.environ.get('DPREFIX') or configuration()['prefix']) / \
            'Applications/Riot Client.app'
        if not (installed.exists() and version(installed.resolve()).startswith('142.')):
            self.skipTest('the installed Riot Client.app is not 142; the runner would start another build')
        with tempfile.NamedTemporaryFile('w+', suffix='.log') as log:
            process = subprocess.Popen(['sh', str(RUNNER)], stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'RCLOL_LAUNCH_PRODUCT': ''})
            try:
                process.wait(timeout=int(os.environ.get('RCLOL_142_TIMEOUT', '90')))
            except subprocess.TimeoutExpired:
                pass
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            log.seek(0)
            output = log.read()
        self.assertNotIn('Symbol not found', output, 'dyld rejected the client')
        self.assertNotIn('Library not loaded', output, 'dyld rejected a dependency')
        self.assertNotIn('ERR_NETWORK_CHANGED', output,
                         'the BSD syscall trampoline leaks %rdx (see patches/xnu.patch)')
        # Electron logs its own local UI load; reaching it proves the process is past dyld.
        self.assertRegex(output, r'Riot Client\[|Failed to load URL|ERR_',
                         'no Electron activity in the client log')


if __name__ == '__main__':
    unittest.main()
