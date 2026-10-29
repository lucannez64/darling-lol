"""Preflight and LLVM tool discovery, with fake host tools and no Darling runtime."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / 'scripts/preflight.py'
KERNEL = '6.1.0-test'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreflightFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='preflight fixture ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.llvm_bindir = Path(self.tmp.name) / 'llvm bin'
        self.llvm_bindir.mkdir()
        self.good()

    def good(self):
        self.stub('llvm-config', '''case "$1" in
 --version) echo 23.1.5;;
 --bindir) printf '%s\\n' "LLVM_BINDIR";;
esac'''.replace('LLVM_BINDIR', str(self.llvm_bindir)))
        self.stub('llvm-objdump', 'exit 0')
        self.stub('llvm-nm', 'exit 0')
        self.stub('cmake', 'echo "cmake version 3.31.6"')
        self.stub('git', 'case "$*" in "lfs version") echo "git-lfs/3.7.0 (GitHub; linux amd64)";; esac')
        self.vulkan([('1.3.289', 'Test GPU', True)])
        self.stub('xdpyinfo', 'exit 0')

    def vulkan(self, devices):
        """Stub vulkaninfo with [(apiVersion, name, timeline)] per device."""
        summary = ['Devices:', '========']
        features = ['Device Properties and Extensions:', '=================================']
        for index, (api, name, timeline) in enumerate(devices):
            summary += [f'GPU{index}:', f'\tapiVersion = {api}', f'\tdeviceName = {name}']
            features += [f'GPU{index}:', 'VkPhysicalDeviceProperties:',
                         f'\tapiVersion = {api}', f'\tdeviceName = {name}']
            if timeline is not None:
                features += ['VkPhysicalDeviceVulkan12Features:',
                             f'\ttimelineSemaphore = {"true" if timeline else "false"}']
        self.stub('vulkaninfo', 'case "$*" in\n'
                  " --summary) printf '%s\\n' " + ' '.join(f"'{line}'" for line in summary) + ';;\n'
                  " *) printf '%s\\n' " + ' '.join(f"'{line}'" for line in features) + ';;\nesac')

    def stub(self, name, body, folder=None):
        path = (folder or self.bin) / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o755)
        return path

    def preflight(self, *arguments, kernel=KERNEL, display=':0'):
        env = dict(os.environ, PATH=str(self.bin),
                   RCLOL_PREFLIGHT_KERNEL_RELEASE=kernel,
                   RCLOL_PREFLIGHT_SOURCE=str(Path(self.tmp.name) / 'not fetched'))
        if display is None:
            env.pop('DISPLAY', None)
        else:
            env['DISPLAY'] = display
        return subprocess.run([sys.executable, str(PREFLIGHT), *arguments], env=env,
                              text=True, capture_output=True, timeout=120)

    def test_all_required_checks_pass(self):
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('preflight: ', result.stdout)
        self.assertNotIn('FAIL', result.stdout)
        self.assertIn('ok   llvm-tools', result.stdout)


    def test_llvm_15_stops_with_the_reason(self):
        self.stub('llvm-config', 'echo 15.0.7')
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL llvm', result.stdout)
        self.assertIn('LLVM 16 or newer is required', result.stdout)
        self.assertIn('LLVMGetVersion', result.stdout)
        self.assertIn('RCLOL_SKIP_PREFLIGHT=1', result.stderr)


    def test_untested_llvm_major_warns_but_builds(self):
        self.stub('llvm-config', 'case "$1" in --version) echo 17.0.6;; esac')
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('warn llvm-tested', result.stdout)
        self.assertIn('1 warning', result.stdout)


    def test_versioned_llvm_tools_are_found(self):
        (self.bin / 'llvm-objdump').unlink()
        (self.bin / 'llvm-nm').unlink()
        self.stub('llvm-objdump-15', 'exit 0')
        self.stub('llvm-nm-15', 'exit 0')
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('llvm-objdump-15', result.stdout)
        self.assertIn('llvm-nm-15', result.stdout)


    def test_llvm_tools_come_from_llvm_config_bindir(self):
        (self.bin / 'llvm-objdump').unlink()
        (self.bin / 'llvm-nm').unlink()
        self.stub('llvm-objdump-23', 'exit 0', folder=self.llvm_bindir)
        self.stub('llvm-nm-23', 'exit 0', folder=self.llvm_bindir)
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('llvm bin', result.stdout)


    def test_missing_llvm_tool_names_what_to_install(self):
        (self.bin / 'llvm-nm').unlink()
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL llvm-tools', result.stdout)
        self.assertIn('llvm-nm not found', result.stdout)
        self.assertIn('llvm23-tools', result.stdout)


    def test_missing_llvm_config_names_the_package(self):
        (self.bin / 'llvm-config').unlink()
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL llvm', result.stdout)
        self.assertIn('llvm-config not found', result.stdout)
        self.assertIn('llvm-dev', result.stdout)


    def test_old_cmake_stops_the_setup(self):
        self.stub('cmake', 'echo "cmake version 3.16.3"')
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL cmake', result.stdout)
        self.assertIn('CMake 3.20', result.stdout)


    def test_old_git_lfs_stops_the_setup(self):
        self.stub('git', 'case "$*" in "lfs version") echo "git-lfs/2.9.2 (GitHub; linux amd64)";; esac')
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL git-lfs', result.stdout)
        self.assertIn('Git LFS 2.13', result.stdout)


    def test_old_kernel_is_required(self):
        result = self.preflight(kernel='5.10.0-28-amd64')
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL kernel', result.stdout)
        self.assertIn('Linux 5.14', result.stdout)


    def test_old_vulkan_stops_the_setup(self):
        self.vulkan([('1.2.170', 'Old GPU', True)])
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL vulkan', result.stdout)
        self.assertIn('Vulkan 1.3 or newer', result.stdout)
        self.assertIn('Old GPU 1.2', result.stdout)


    def test_missing_vulkaninfo_only_warns(self):
        (self.bin / 'vulkaninfo').unlink()
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('warn vulkan', result.stdout)
        self.assertIn('vulkaninfo not available', result.stdout)
        self.assertIn('Install vulkan-tools', result.stdout)


    def test_broken_vulkaninfo_is_required(self):
        self.stub('vulkaninfo', 'exit 1')
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL vulkan', result.stdout)
        self.assertIn('vulkaninfo did not finish', result.stdout)
        self.assertIn('timed out', result.stdout)


    def test_missing_timeline_semaphores_is_reported(self):
        self.vulkan([('1.3.289', 'Test GPU', False)])
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL vulkan', result.stdout)
        self.assertIn('timelineSemaphore false', result.stdout)
        self.assertIn('on the same device', result.stdout)


    def test_a_second_device_can_satisfy_both_requirements(self):
        self.vulkan([('1.4.309', 'New GPU without semaphores', False),
                     ('1.4.309', 'Second GPU', True)])
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ok   vulkan', result.stdout)


    def test_requirements_are_not_combined_across_devices(self):
        self.vulkan([('1.2.170', 'Old GPU with semaphores', True),
                     ('1.4.309', 'New GPU without semaphores', False)])
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL vulkan', result.stdout)
        self.assertIn('Old GPU with semaphores 1.2', result.stdout)
        self.assertIn('New GPU without semaphores 1.4', result.stdout)


    def test_no_reported_device_is_required(self):
        self.vulkan([])
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAIL vulkan', result.stdout)
        self.assertIn('no devices reported', result.stdout)


    def test_unreachable_display_only_warns(self):
        self.stub('xdpyinfo', 'exit 1')
        result = self.preflight(display=':99')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('warn x11', result.stdout)
        self.assertIn('XAUTHORITY', result.stdout)


    def test_headless_host_only_warns(self):
        result = self.preflight(display=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DISPLAY unset', result.stdout)


    def test_json_report_names_every_check(self):
        result = self.preflight('--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['ok'])
        names = {check['check'] for check in report['checks']}
        self.assertTrue({'python', 'cmake', 'git-lfs', 'lfs-pointer', 'kernel', 'llvm',
                         'llvm-tools', 'vulkan', 'x11'} <= names)
        self.assertTrue(all('required' in check and 'detail' in check for check in report['checks']))


class LfsPointerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lfs pointer ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'darling-src'
        self.source.mkdir()
        self.preflight = load('preflight_lfs', PREFLIGHT)
        self.preflight.SOURCE = self.source

    def check(self):
        reports = []
        self.preflight.check_lfs_pointers(lambda *arguments, **keywords: reports.append(
            (arguments, keywords)))
        self.assertEqual(len(reports), 1)
        arguments, keywords = reports[0]
        name, ok, detail = arguments[:3]
        hint = arguments[3] if len(arguments) > 3 else keywords.get('hint')
        return name, ok, detail, hint

    def write(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_pointer_text_in_a_library_is_reported(self):
        self.write('lib/libswiftCore.dylib',
                   b'version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 123\n')
        self.write('lib/libreal.dylib', b'\xcf\xfa\xed\xfe' + b'\0' * 64)
        name, ok, detail, hint = self.check()
        self.assertEqual(name, 'lfs-pointer')
        self.assertFalse(ok)
        self.assertIn('lib/libswiftCore.dylib', detail)
        self.assertNotIn('libreal', detail)
        self.assertIn('git lfs pull', hint)

    def test_pointer_text_outside_a_library_is_ignored(self):
        self.write('notes.dylib.txt', b'version https://git-lfs.github.com/spec/v1\n')
        self.write('lib/libreal.dylib', b'not a pointer')
        _, ok, detail, _ = self.check()
        self.assertTrue(ok, detail)

    def test_missing_checkout_is_not_a_failure(self):
        self.preflight.SOURCE = self.source / 'not fetched'
        _, ok, detail, _ = self.check()
        self.assertTrue(ok)
        self.assertIn('no prepared checkout', detail)


class LlvmToolsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='llvm tools ', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.saved = os.environ.get('PATH')
        os.environ['PATH'] = str(self.bin)
        self.addCleanup(self.restore)
        self.tools = load('llvm_tools', ROOT / 'tools/llvm_tools.py')

    def restore(self):
        os.environ['PATH'] = self.saved or ''

    def stub(self, name, body='exit 0', folder=None):
        path = (folder or self.bin) / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
        return path

    def test_prefers_the_unversioned_name(self):
        unversioned = self.stub('llvm-objdump')
        self.stub('llvm-objdump-23')
        self.assertEqual(self.tools.find('llvm-objdump'), str(unversioned))


    def test_prefers_the_highest_versioned_name(self):
        self.stub('llvm-objdump-15')
        newest = self.stub('llvm-objdump-23')
        self.stub('llvm-objdump-9')
        self.assertEqual(self.tools.find('llvm-objdump'), str(newest))


    def test_ignores_a_name_that_is_not_a_version(self):
        self.stub('llvm-objdump-tools')
        self.assertIsNone(self.tools.find('llvm-objdump'))


    def test_falls_back_to_llvm_config_bindir(self):
        libexec = Path(self.tmp.name) / 'libexec'
        libexec.mkdir()
        expected = self.stub('llvm-nm-22', folder=libexec)
        self.stub('llvm-config', 'echo "%s"' % libexec)
        self.assertEqual(self.tools.find('llvm-nm'), str(expected))


    def test_llvm_config_timeout_is_reported(self):
        self.stub('llvm-config', 'exec /bin/sleep 5')
        self.tools.TIMEOUT = 1
        with self.assertRaises(SystemExit) as raised:
            self.tools.config('--version')
        self.assertIn('did not finish within 1 seconds', str(raised.exception))
        self.assertIn('reinstall the LLVM development package', str(raised.exception))


    def test_llvm_config_timeout_does_not_hang_the_bindir_probe(self):
        self.stub('llvm-config', 'exec /bin/sleep 5')
        self.tools.TIMEOUT = 1
        self.assertIsNone(self.tools.find('llvm-objdump'))


    def test_require_explains_what_to_install(self):
        with self.assertRaises(SystemExit) as raised:
            self.tools.require('llvm-objdump')
        self.assertIn('llvm-objdump not found', str(raised.exception))
        self.assertIn('llvm-config --bindir', str(raised.exception))


class FileDigestFixture(unittest.TestCase):
    def test_sha256_matches_hashlib(self):
        digest = load('file_digest', ROOT / 'tools/file_digest.py')
        with tempfile.TemporaryDirectory(prefix='digest ', dir=os.environ.get('TMPDIR')) as folder:
            path = Path(folder) / 'payload'
            payload = b'darling' * 400_000  # More than one chunk.
            path.write_bytes(payload)
            self.assertEqual(digest.sha256(path), hashlib.sha256(payload).hexdigest())
