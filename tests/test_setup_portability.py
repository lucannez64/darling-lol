"""Filesystem/subprocess fixtures: never invoke a host runtime or compiler."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


GUEST_VALIDATION_INPUTS = (
    'libDarlingXShm.dylib',
    *(f'RuntimeLibraries/{name}.dylib' for name in (
        'libsystem_kernel', 'libsystem_c', 'libsystem_platform', 'libsystem_malloc',
        'libsystem_pthread', 'libobjc.A', 'libc++abi', 'libc++.1',
        'libGL', 'libindium', 'libiridium')),
    *(f'RuntimeFrameworks/{name}.framework/Versions/{version}/{name}' for name, version in (
        ('AppKit', 'C'), ('QuartzCore', 'A'), ('CoreFoundation', 'A'),
        ('SystemConfiguration', 'A'), ('CFNetwork', 'A'), ('IOSurface', 'A'),
        ('OpenGL', 'A'), ('CoreVideo', 'A'), ('CoreAudio', 'A'), ('AudioToolbox', 'A'),
        ('CoreGraphics', 'A'), ('CoreText', 'A'), ('Metal', 'A'), ('CarbonCore', 'A'),
        ('LaunchServices', 'A'))),
    'RuntimeFrameworks/AppKit.framework/Versions/C/Resources/Backends/X11.backend/Contents/MacOS/X11',
    'RuntimeFrameworks/OpenGL.framework/Versions/A/Libraries/libGL.dylib',
    'Library/Audio/Plug-Ins/Components/DarlingRiotAudio.component/Contents/MacOS/CoreAudio',
)
PREFIX_VALIDATION_INPUTS = (
    'usr/lib/system/libsystem_kernel.dylib',
    'System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/libvDSP.dylib',
    'System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/LaunchServices',
    'System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/CarbonCore.framework/Versions/A/CarbonCore',
)


class SetupFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='setup fixture ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project space'
        self.root.mkdir()
        for name in ('scripts', 'tools'):
            shutil.copytree(ROOT / name, self.root / name)
        self.home = Path(self.tmp.name) / 'host home'
        self.home.mkdir()
        self.prefix = Path(self.tmp.name) / 'custom prefix'
        self.guest = self.prefix / 'Users/Guest Account'
        self.bin = Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), TMPDIR=self.tmp.name,
                        DPREFIX=str(self.prefix), PYTHONDONTWRITEBYTECODE='1',
                        PATH=str(self.bin) + ':' + os.environ['PATH'])
        self.config = dict(project_root=str(self.root), host_home=str(self.home),
                           prefix=str(self.prefix), guest_user='Guest Account',
                           guest_home='/Users/Guest Account', sdk=str(self.root / 'sdk'), jobs=1)
        (self.root / 'build').mkdir()
        (self.root / 'build/config.json').write_text(json.dumps(self.config))
        self.log = Path(self.tmp.name) / 'events'
        self.env['FIXTURE_LOG'] = str(self.log)


    def run_script(self, path, *args):
        return subprocess.run([sys.executable if path.endswith('.py') else 'sh',
                               str(self.root / path), *args], env=self.env,
                              text=True, capture_output=True, timeout=20)


    def configure(self):
        result = self.run_script('scripts/configure.py')
        self.assertEqual(result.returncode, 0, result.stderr)


    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o755)


    def runtime_stubs(self):
        # Nested Python is a strict allowlist: no accidental live helper execution.
        self.stub('python3', '''printf 'python %s\\n' "$*" >> "$FIXTURE_LOG"
case "$1" in
 -|*runtime_paths.py) exec REAL_PYTHON "$@";;
 *stop-darling-runtime.py) [ "${STOP_FAIL:-0}" = 0 ] || exit 9; printf stopped\\n >> "$FIXTURE_LOG";;
 *build-*.py|*ensure-darling-runtime.py|*install-darling-system-profiler.py|*install-riot-*.py) :;;
 *) exit 90;;
esac'''.replace('REAL_PYTHON', sys.executable))
        self.stub('darling', '''printf 'darling %s\\n' "$*" >> "$FIXTURE_LOG"
if [ "$1" = shutdown ]; then sleep "${SHUTDOWN_DELAY:-0}"; exit "${SHUTDOWN_FAIL:-0}"; fi''')
        self.stub('clang', '''last=; for arg do if [ "$last" = -o ]; then printf binary > "$arg"; fi; last=$arg; done''')
        self.stub('sudo', 'exit 1')
        self.stub('make', ':')
        self.stub('cc', 'exec clang "$@"')
        self.stub('pkg-config', ':')
        self.stub('install', '''case "$(cat "$FIXTURE_LOG")" in *stopped*) :;; *) exit 88;; esac
exec REAL_INSTALL "$@"'''.replace('REAL_INSTALL', shutil.which('install')))


    def test_configure_replaces_readonly_and_repeats(self):
        self.configure()
        generated = self.root / 'build/runtime/tools/run-riot-client.sh'
        generated.chmod(0o444)
        self.configure()
        self.configure()
        self.assertTrue(generated.stat().st_mode & 0o200)


    def test_configure_unlinks_dangling_symlink_without_writing_victim(self):
        self.configure()
        target = self.root / 'build/runtime/tools/run-riot-client.sh'
        target.unlink()
        victim = self.home / 'must not be created'
        target.symlink_to(victim)
        self.configure()
        self.assertFalse(victim.exists(), 'configure followed the dangling generated-file symlink')
        self.assertFalse(target.is_symlink())


    def write_config(self, **changes):
        (self.root / 'build/config.json').write_text(json.dumps(dict(self.config, **changes)))


    def config_value(self, key):
        return json.loads((self.root / 'build/config.json').read_text())[key]


    def test_configure_rehomes_default_prefix_after_home_change(self):
        previous = Path(self.tmp.name) / 'previous home'
        self.write_config(host_home=str(previous), prefix=str(previous / '.darling'))
        result = self.run_script('scripts/configure.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config_value('prefix'), str(self.home / '.darling'))
        self.assertIn('previous home', result.stderr)


    def test_configure_keeps_explicit_prefix_after_home_change(self):
        previous = Path(self.tmp.name) / 'previous home'
        self.write_config(host_home=str(previous), prefix=str(self.prefix))
        self.configure()
        self.assertEqual(self.config_value('prefix'), str(self.prefix))
        self.assertEqual(self.config_value('host_home'), str(self.home))


    def test_configure_warns_about_prefix_inside_previous_home(self):
        previous = Path(self.tmp.name) / 'previous home'
        self.write_config(host_home=str(previous), prefix=str(previous / 'custom prefix'))
        result = self.run_script('scripts/configure.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config_value('prefix'), str(previous / 'custom prefix'))
        self.assertIn('--prefix', result.stderr)


    def test_configure_creates_relative_build_symlink(self):
        self.configure()
        link = self.root / 'build/runtime/build'
        self.assertEqual(os.readlink(link), '..')
        self.assertEqual(link.resolve(), (self.root / 'build').resolve())


    def test_configure_repairs_absolute_and_dangling_build_symlink(self):
        link = self.root / 'build/runtime/build'
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root / 'build')
        self.configure()
        self.assertEqual(os.readlink(link), '..')
        link.unlink()
        link.symlink_to(self.root / 'moved away/build')
        self.configure()
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.root / 'build').resolve())


    def test_configure_leaves_foreign_build_directory(self):
        link = self.root / 'build/runtime/build'
        link.mkdir(parents=True)
        (link / 'keep').write_text('local build')
        result = self.run_script('scripts/configure.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.is_symlink())
        self.assertEqual((link / 'keep').read_text(), 'local build')
        self.assertIn('not a symlink', result.stderr)


    def test_configure_warns_about_stale_cmake_cache(self):
        cache = self.root / 'build/darling-src/build/CMakeCache.txt'
        cache.parent.mkdir(parents=True)
        cache.write_text('// generated\nCMAKE_HOME_DIRECTORY:INTERNAL=/root/old checkout/build/darling-src\n')
        result = self.run_script('scripts/configure.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(cache.is_file(), 'configure removed the CMake cache')
        self.assertIn('/root/old checkout/build/darling-src', result.stderr)
        self.assertIn('Remove', result.stderr)


    def test_configure_accepts_current_cmake_cache(self):
        cache = self.root / 'build/darling-src/build/CMakeCache.txt'
        cache.parent.mkdir(parents=True)
        cache.write_text(f'CMAKE_HOME_DIRECTORY:INTERNAL={self.root}/build/darling-src\n')
        result = self.run_script('scripts/configure.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')


    def test_presentation_creates_prerequisites_and_repeat_install(self):
        self.configure()
        self.runtime_stubs()
        self.presentation_inputs()
        for _ in range(2):
            result = self.run_script('build/runtime/tools/install-riot-presentation-runtime.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.guest / 'libDarlingXShm.dylib').is_file())
        self.assertTrue((self.guest / 'RuntimeLibraries/libsystem_kernel.dylib').is_file())
        self.assertIn('stopped', self.log.read_text())


    def presentation_inputs(self):
        for name in ('external/cocotron/AppKit/AppKit', 'external/cocotron/AppKit/X11',
                     'external/cocotron/QuartzCore/QuartzCore',
                     'external/corefoundation/CoreFoundation',
                     'external/configd/SystemConfiguration.fproj/SystemConfiguration',
                     'external/cfnetwork/src/CFNetwork',
                     'external/libplatform/libsystem_platform.dylib',
                     'external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib'):
            path = self.root / 'build/darling-src/build/src' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('library')
        for framework, version in (('AppKit', 'C'), ('QuartzCore', 'A'),
                                   ('CoreFoundation', 'A'), ('SystemConfiguration', 'A'),
                                   ('CFNetwork', 'A')):
            path = self.root / 'sdk/System/Library/Frameworks' / (framework + '.framework')
            binary = path / 'Versions' / version / framework
            binary.parent.mkdir(parents=True)
            binary.write_text('sdk binary')
            binary.chmod(0o555)
            (path / 'Versions/Current').symlink_to(version)
            (path / framework).symlink_to('Versions/Current/' + framework)
            if framework == 'AppKit':
                backend = binary.parent / 'Resources/Backends/X11.backend/Contents/MacOS/X11'
                backend.parent.mkdir(parents=True)
                backend.write_text('sdk backend')
        p = self.root / 'build/libcxx-install/lib'
        p.mkdir(parents=True)
        (p / 'libc++.1.0.dylib').write_text('library')


    def prepare_validation(self):
        self.configure()
        for root, inputs in ((self.guest, GUEST_VALIDATION_INPUTS),
                             (self.prefix, PREFIX_VALIDATION_INPUTS)):
            for relative in inputs:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 5000)
        self.stub('llvm-objdump', '''printf '%s\\n' "$*" >> "$FIXTURE_LOG"
exit "${CHECK_FAIL:-0}"''')


    def test_launcher_selects_paths_before_prefix_helpers(self):
        source = (ROOT / 'tools/run-riot-client.sh').read_text()
        self.assertLess(source.index('runtime-paths.sh'), source.index('ensure-darling-runtime.py'))

    def test_metal_pair_uses_shared_runtime_paths(self):
        source = (ROOT / 'tools/install-riot-metal-pair.sh').read_text()
        self.assertIn('runtime-paths.sh', source)
        self.assertNotIn('$HOME/.darling', source)

    def test_paths_reject_symlink_escape(self):
        self.configure()
        outside = self.home / 'outside'
        outside.mkdir()
        self.prefix.mkdir(parents=True, exist_ok=True)
        (self.prefix / 'escape').symlink_to(outside, target_is_directory=True)
        self.env['RIOT_GUEST_HOME'] = str(self.prefix / 'escape/new-user')
        result = self.run_script('build/runtime/tools/validate-riot-runtime.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inside DPREFIX', result.stderr)
        result = self.run_script('build/runtime/tools/runtime-paths.sh')
        self.assertNotEqual(result.returncode, 0)

    def test_validation_missing_empty_and_checker_failure(self):
        self.prepare_validation()
        self.env['CHECK_FAIL'] = '1'
        self.assertNotEqual(self.run_script('build/runtime/tools/validate-riot-runtime.py').returncode, 0)
        self.env.pop('CHECK_FAIL')
        vdsp = self.prefix / 'System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/libvDSP.dylib'
        vdsp.unlink(missing_ok=True)
        self.assertNotEqual(self.run_script('build/runtime/tools/validate-riot-runtime.py').returncode, 0)
        vdsp.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 5000)
        shutil.rmtree(self.guest)
        self.assertNotEqual(self.run_script('build/runtime/tools/validate-riot-runtime.py').returncode, 0)

    def test_validation_uses_host_guest_override(self):
        self.prepare_validation()
        override = self.prefix / 'Users/Override Guest'
        self.guest.rename(override)
        self.env['RIOT_GUEST_HOME'] = str(override)
        result = self.run_script('build/runtime/tools/validate-riot-runtime.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(override), self.log.read_text())

    def test_libs_cli_prefix_revalidates_environment_guest_home(self):
        self.configure()
        self.env['RIOT_GUEST_HOME'] = str(self.guest)
        other_prefix = Path(self.tmp.name) / 'other prefix'
        result = self.run_script('build/runtime/tools/install-riot-libs-runtime.py',
                                 '--prefix', str(other_prefix), '--only', 'libsystem_c.dylib')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inside DPREFIX', result.stderr)

    def test_libs_cli_guest_home_overrides_invalid_environment(self):
        self.configure()
        self.env['RIOT_GUEST_HOME'] = str(self.home / 'outside prefix')
        other_prefix = Path(self.tmp.name) / 'other prefix'
        artifact = self.root / 'build/riot-libsystem_c.dylib'
        artifact.write_bytes(b'\xcf\xfa\xed\xfe' + b'library')
        result = self.run_script('build/runtime/tools/install-riot-libs-runtime.py',
                                 '--prefix', str(other_prefix), '--guest-home', '/Users/New Guest',
                                 '--only', 'libsystem_c.dylib')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((other_prefix / 'Users/New Guest/RuntimeLibraries/libsystem_c.dylib').is_file())


    def test_failed_stop_with_all_compile_outputs_available(self):
        self.configure()
        self.runtime_stubs()
        self.presentation_inputs()
        self.guest.mkdir(parents=True)
        self.env['RIOT_GUEST_HOME'] = str(self.guest)
        self.env.update(STOP_FAIL='1', SHUTDOWN_FAIL='1')
        result = self.run_script('build/runtime/tools/install-riot-presentation-runtime.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stop-darling-runtime.py', self.log.read_text())
        self.assertFalse((self.guest / 'libDarlingXShm.dylib').exists())


if __name__ == '__main__':
    unittest.main()
