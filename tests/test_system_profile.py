"""The single hardware-presentation profile and its launcher/guest handoff."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'tools/system.prop'


def profile_values():
    values = {}
    for raw in PROFILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator:
            raise AssertionError(f'invalid system.prop line: {raw!r}')
        if key in values:
            raise AssertionError(f'duplicate system.prop key: {key}')
        values[key] = value
    return values


class SystemProfileTests(unittest.TestCase):
    def test_default_profile_is_host_and_compat_is_opt_in(self):
        values = profile_values()
        self.assertEqual(values['profile'], 'host')
        self.assertEqual(values['compat.cpu.brand'],
                         'Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz')
        self.assertEqual(values['compat.gpu.name'], 'mac')
        self.assertEqual(values['compat.gpu.vram'], '4096')
        self.assertEqual(values['compat.gpu.unified'], '1')
        self.assertEqual(values['compat.memory.bytes'], '17179869184')

    def test_profile_does_not_contain_real_apple_or_account_identifiers(self):
        values = profile_values()
        self.assertNotIn('serial', values)
        self.assertNotIn('uuid', values)
        self.assertNotIn('rom', values)
        self.assertNotIn('token', values)

    def test_launcher_reads_the_profile_and_does_not_pin_mac_defaults(self):
        launcher = (ROOT / 'tools/run-riot-client.sh').read_text()
        self.assertIn('darling-system.prop', launcher)
        self.assertIn('RCLOL_PLATFORM_PROFILE', launcher)
        self.assertNotIn('DARLING_METAL_VRAM_MB:-4096', launcher)
        self.assertNotIn('DARLING_METAL_MAC_GPU_NAME=1', launcher.split('if [ "$profile" = compat ]', 1)[0])

    def test_guest_sysctls_use_the_launcher_values(self):
        xnu = (ROOT / 'patches/xnu.patch').read_text()
        self.assertIn('getenv("RCLOL_CPU_BRAND")', xnu)
        self.assertIn('getenv("RCLOL_MEMORY_BYTES")', xnu)
        self.assertIn('strtoull(memory, NULL, 10)', xnu)

    def test_install_runtime_preserves_an_existing_profile(self):
        installer = (ROOT / 'scripts/install-runtime.sh').read_text()
        self.assertIn('darling-system.prop', installer)
        self.assertIn('[ ! -e "$prefix/private/etc/darling-system.prop" ]', installer)

    def launcher_handoff(self, profile):
        launcher = (ROOT / 'tools/run-riot-client.sh').read_text()
        start = launcher.index('system_prop=')
        end = launcher.index('# Opt-in partial recovery:')
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'prefix'
            (prefix / 'private/etc').mkdir(parents=True)
            (prefix / 'private/etc/darling-system.prop').write_text(PROFILE.read_text())
            block = Path(folder) / 'profile-block.sh'
            block.write_text(launcher[start:end])
            env = {
                'PATH': os.environ['PATH'], 'HOME': folder, 'prefix_dir': str(prefix),
                'runner_dir': str(ROOT / 'tools'), 'RCLOL_PLATFORM_PROFILE': profile,
            }
            script = ('. "$1"; '
                      'printf "profile=%s\\ncpu=%s\\ngpu=%s\\nvram=%s\\nmetal_mac=%s\\nmetal_vram=%s\\n" '
                      '"$RCLOL_PLATFORM_PROFILE" "$RCLOL_CPU_BRAND" "$RCLOL_GPU_NAME" '
                      '"$RCLOL_GPU_VRAM_MB" "${DARLING_METAL_MAC_GPU_NAME-}" "${DARLING_METAL_VRAM_MB-}"')
            result = subprocess.run(['sh', '-c', script, 'sh', str(block)], env=env,
                                    check=True, capture_output=True, text=True)
        return dict(line.split('=', 1) for line in result.stdout.splitlines())

    def test_launcher_defaults_to_host_and_compat_is_explicit(self):
        host = self.launcher_handoff('host')
        self.assertEqual(host['profile'], 'host')
        self.assertTrue(host['cpu'])
        self.assertEqual(host['gpu'], 'host')
        self.assertEqual(host['metal_mac'], '')
        self.assertEqual(host['metal_vram'], '')

        compat = self.launcher_handoff('compat')
        self.assertEqual(compat['profile'], 'compat')
        self.assertEqual(compat['cpu'], 'Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz')
        self.assertEqual(compat['gpu'], 'mac')
        self.assertEqual(compat['vram'], '4096')
        self.assertEqual(compat['metal_mac'], '1')
        self.assertEqual(compat['metal_vram'], '4096')


if __name__ == '__main__':
    unittest.main()
