"""system_profiler hardware/software overviews, with stub guest commands; never starts Darling."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/darling-system_profiler.sh'

IOREG = '''+-o MacBookPro16,1  <class IOPlatformExpertDevice, id 0x100000110, registered, matched, active, busy 0 (0 ms), retain 30>
    {
      "IOPlatformSerialNumber" = "USER-SUPPLIED"
      "model" = <"MacBookPro16,1">
      "manufacturer" = <"Apple Inc.">
      "IOPlatformUUID" = "8E1F2A3B-4C5D-4E6F-8A7B-9C0D1E2F3A4B"
      "provisioning_UDID" = "8E1F2A3B-4C5D-4E6F-8A7B-9C0D1E2F3A4B"
      "ROM" = <0a1b2c3d4e5f>
    }
'''
SYSCTL = {
    'hw.physicalcpu': '8', 'hw.packages': '1', 'hw.memsize': str(32 << 30),
    'machdep.cpu.brand_string': 'AMD Ryzen 7 5800X 8-Core Processor & more',
    'hw.l2cachesize': str(512 << 10), 'hw.l3cachesize': str(32 << 20), 'hw.cpufrequency': '3800000000',
}


class SystemProfilerTests(unittest.TestCase):
    def test_awk_ternaries_parse_under_bsd_awk(self):
        # The guest runs macOS's one-true-awk, which rejects an unparenthesized
        # comparison as a ternary condition inside printf arguments
        # ("i == primary ? ..."); gawk on the host accepts it, so check the source.
        import re
        for number, line in enumerate(SCRIPT.read_text().splitlines(), 1):
            code = line.split('#', 1)[0] if line.lstrip().startswith('#') else line
            for match in re.finditer(r'(\S+)\s+\?\s+"', code):
                self.assertTrue(match.group(1).endswith(')'),
                                f'line {number}: parenthesize the ternary condition: {line.strip()}')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.bin = Path(self.folder.name)
        self.stub('sysctl', 'import sys\nvalues = %r\nname = sys.argv[-1]\n'
                  'if name not in values: sys.exit(1)\nprint(values[name])' % SYSCTL)
        self.stub('sw_vers', 'import sys\nprint({"-productName": "macOS", "-productVersion": "11.7.4", '
                  '"-buildVersion": "20G1120"}[sys.argv[1]])')
        self.stub('uname', 'print("Darwin 20.6.0")')
        self.stub('hostname', 'print("darling")')
        self.stub('ioreg', 'import sys\nsys.stdout.write(%r)' % IOREG)

    def tearDown(self):
        self.folder.cleanup()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!%s\n%s\n' % (sys.executable, body))
        path.chmod(0o755)

    def run_profiler(self, *args, env=None):
        child_env = {'PATH': '%s:%s' % (self.bin, os.environ['PATH']), 'USER': 'player'}
        child_env.update(env or {})
        return subprocess.run(['sh', str(SCRIPT), *args], env=child_env, check=True, capture_output=True, text=True).stdout

    def drm_root(self, vendor='0x1002', device='0x67ff', vram=4 << 30):
        root = Path(self.folder.name) / 'system-root'
        device_dir = root / 'sys/class/drm/card0/device'
        device_dir.mkdir(parents=True)
        (device_dir / 'vendor').write_text(vendor + '\n')
        (device_dir / 'device').write_text(device + '\n')
        (device_dir / 'mem_info_vram_total').write_text(str(vram) + '\n')
        return root

    def display(self, root, **env):
        child_env = {'RCLOL_SYSTEM_ROOT': str(root)}
        child_env.update(env)
        items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=child_env).encode())[0]['_items']
        # Most existing assertions exercise the discrete card; the MacBookPro16,1
        # overview also includes its integrated Intel GPU before it.
        return next((item for item in items if item['sppci_model'] == 'AMD Radeon Pro 5500M'), items[0])

    def xrandr(self, root, output, status=0):
        path = root / 'usr/bin/xrandr'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!%s\nimport sys\nassert sys.argv[1:] == ["--verbose", "--current"]\n'
                        'sys.stdout.write(%r)\nsys.exit(%d)\n' % (sys.executable, output, status))
        path.chmod(0o755)

    def xml_items(self, data_type, env=None):
        report = plistlib.loads(self.run_profiler('-xml', data_type, env=env).encode())
        self.assertEqual(report[0]['_dataType'], data_type)
        return report[0]['_items'][0]

    def test_hardware_reports_iokit_identity_and_sysctl_values(self):
        item = self.xml_items('SPHardwareDataType')
        self.assertEqual(item, {
            '_name': 'hardware_overview', 'machine_name': 'MacBook Pro', 'machine_model': 'MacBookPro16,1',
            'cpu_type': 'AMD Ryzen 7 5800X 8-Core Processor & more', 'current_processor_speed': '3.8 GHz',
            'packages': 1, 'number_processors': 8, 'l2_cache_core': '512 KB', 'l3_cache': '32 MB',
            'physical_memory': '32 GB', 'serial_number': 'USER-SUPPLIED',
            'platform_UUID': '8E1F2A3B-4C5D-4E6F-8A7B-9C0D1E2F3A4B',
            'provisioning_UDID': '8E1F2A3B-4C5D-4E6F-8A7B-9C0D1E2F3A4B',
        })
        text = self.run_profiler('SPHardwareDataType')
        self.assertIn('      Serial Number (system): USER-SUPPLIED\n', text)
        self.assertIn('      Model Name: MacBook Pro\n', text)

    def test_missing_iokit_and_sysctl_values_are_omitted(self):
        self.stub('ioreg', 'import sys\nsys.exit(1)')
        self.stub('sysctl', 'import sys\nsys.exit(1)')
        self.assertEqual(self.xml_items('SPHardwareDataType'), {'_name': 'hardware_overview'})
        self.assertNotIn('Serial Number', self.run_profiler('SPHardwareDataType'))

    def test_software_overview(self):
        item = self.xml_items('SPSoftwareDataType')
        self.assertEqual(item, {
            '_name': 'os_overview', 'os_version': 'macOS 11.7.4 (20G1120)', 'kernel_version': 'Darwin 20.6.0',
            'boot_volume': 'Macintosh HD', 'boot_mode': 'normal_boot', 'local_host_name': 'darling',
            'user_name': 'player',
        })
        self.assertIn('      Boot Mode: Normal\n', self.run_profiler('SPSoftwareDataType'))

    def test_listed_and_default_types(self):
        listed = self.run_profiler('-listDataTypes')
        self.assertIn('SPHardwareDataType', listed)
        self.assertIn('SPSoftwareDataType', listed)
        types = [entry['_dataType'] for entry in plistlib.loads(self.run_profiler('-xml').encode())]
        self.assertEqual(types[:2], ['SPHardwareDataType', 'SPSoftwareDataType'])

    def test_host_display_reports_host_card_by_default(self):
        item = self.display(self.drm_root(), RCLOL_PLATFORM_PROFILE='host',
                            RCLOL_GPU_NAME='host', RCLOL_GPU_VRAM_MB='host')
        self.assertEqual(item['sppci_model'], 'AMD Graphics')
        self.assertEqual(item['spdisplays_device-id'], '0x67ff')
        self.assertEqual(item['spdisplays_vendor'], 'AMD (0x1002)')
        self.assertEqual(item['spdisplays_vram'], '4 GB')
        self.assertEqual(item['spdisplays_bus'], 'spdisplays_builtin')

    def test_compat_display_is_opt_in_and_matches_metal_for_amd(self):
        item = self.display(self.drm_root(), RCLOL_PLATFORM_PROFILE='compat',
                            RCLOL_GPU_NAME='mac', RCLOL_GPU_VRAM_MB='4096')
        self.assertEqual(item['sppci_model'], 'AMD Radeon Pro 5500M')
        self.assertEqual(item['spdisplays_device-id'], '0x7340')
        self.assertEqual(item['spdisplays_vendor'], 'AMD (0x1002)')
        self.assertEqual(item['spdisplays_vram'], '4 GB')
        self.assertEqual(item['spdisplays_bus'], 'spdisplays_pcie')

    def test_compat_display_presents_nvidia_as_the_mac_discrete_gpu(self):
        # No Mac that runs macOS 14 has an NVIDIA GPU; iokitd and Metal present
        # the same Radeon Pro 5500M.
        item = self.display(self.drm_root('0x10de', '0x1b80', 6 << 30),
                            RCLOL_PLATFORM_PROFILE='compat', RCLOL_GPU_NAME='mac',
                            RCLOL_GPU_VRAM_MB='4096')
        self.assertEqual(item['sppci_model'], 'AMD Radeon Pro 5500M')
        self.assertEqual(item['spdisplays_device-id'], '0x7340')
        self.assertEqual(item['spdisplays_vendor'], 'AMD (0x1002)')
        self.assertEqual(item['spdisplays_vram'], '4 GB')
        self.assertEqual(item['spdisplays_bus'], 'spdisplays_pcie')

    def test_compat_macbook_pair_and_active_display_text_and_xml(self):
        root = self.drm_root()
        self.xrandr(root, 'Screen 0: minimum 8 x 8, current 4480 x 1440, maximum 32767 x 32767\n'
                    'DP-1 disconnected (normal left inverted right x axis y axis)\n'
                    'eDP-1 connected 1920x1080+0+0 (normal left inverted right x axis y axis)\n'
                    'DP-2 connected primary 2560x1440+1920+0 (normal left inverted right x axis y axis)\n'
                    '   2560x1440 60.00*+\n')
        env = {'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_PLATFORM_PROFILE': 'compat',
               'RCLOL_GPU_NAME': 'mac', 'RCLOL_GPU_VRAM_MB': '4096'}
        items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=env).encode())[0]['_items']
        self.assertEqual([item['sppci_model'] for item in items],
                         ['Intel UHD Graphics 630', 'AMD Radeon Pro 5500M'])
        intel, amd = items
        self.assertEqual(intel['spdisplays_vram_shared'], '1536 MB')
        self.assertNotIn('spdisplays_vram', intel)
        self.assertEqual(intel['spdisplays_revision-id'], '0x0002')
        self.assertEqual(amd['spdisplays_revision-id'], '0x0040')
        self.assertEqual(amd['spdisplays_pcie_width'], 'x16')
        for item in items:
            self.assertEqual(item['spdisplays_automatic_graphics_switching'], 'spdisplays_supported')
            self.assertEqual(item['spdisplays_gmux-version'], '5.0.0')
            self.assertEqual(item['spdisplays_mtlgpufamilysupport'], 'spdisplays_metal3')
        self.assertNotIn('spdisplays_ndrvs', intel)
        panels = amd['spdisplays_ndrvs']
        self.assertEqual([panel['_name'] for panel in panels], ['Display 3', 'Display 2'])
        self.assertEqual(panels[0]['spdisplays_pixelresolution'], '2560 x 1440')
        self.assertEqual(panels[0]['spdisplays_main'], 'spdisplays_yes')
        self.assertNotIn('spdisplays_main', panels[1])
        self.assertEqual(panels[1]['spdisplays_pixelresolution'], '1920 x 1080')
        self.assertEqual(panels[1]['spdisplays_connection_type'], 'spdisplays_internal')
        self.assertEqual(panels[1]['spdisplays_display_type'], 'spdisplays_LCD')
        text = self.run_profiler('SPDisplaysDataType', env=env)
        self.assertLess(text.index('Intel UHD Graphics 630:'), text.index('AMD Radeon Pro 5500M:'))
        for line in ('      VRAM (Dynamic, Max): 1536 MB', '      VRAM (Total): 4 GB',
                     '      Revision ID: 0x0002', '      Revision ID: 0x0040',
                     '      PCIe Lane Width: x16', '      Vendor: Intel', '      Automatic Graphics Switching: Supported',
                     '      gMux Version: 5.0.0', '      Metal Support: Metal 3',
                     '      Displays:', '        Display 3:', '          Resolution: 2560 x 1440',
                     '          Main Display: Yes', '          Mirror: Off', '          Online: Yes',
                     '          Connection Type: Internal'):
            self.assertIn(line + '\n', text)
        self.assertNotIn('Retina', text)  # NSScreen backingScaleFactor is 1.0 on X11.
        self.assertNotIn('Automatically Adjust Brightness', text)
        self.assertEqual(text, self.run_profiler('-detailLevel', 'mini', 'SPDisplaysDataType', env=env))

    def test_host_display_keeps_one_gpu_and_uses_current_rotated_geometry(self):
        root = self.drm_root('0x10de', '0x1b80', 6 << 30)
        self.xrandr(root, 'DP-1 connected primary 1080x1920-1080+0 left (normal left inverted right x axis y axis)\n'
                    '   1920x1080 60.00*+\n')
        env = {'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_PLATFORM_PROFILE': 'host', 'RCLOL_GPU_NAME': 'host'}
        items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=env).encode())[0]['_items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['spdisplays_device-id'], '0x1b80')
        self.assertEqual(items[0]['spdisplays_mtlgpufamilysupport'], 'spdisplays_metal3')
        self.assertEqual(items[0]['spdisplays_ndrvs'][0]['spdisplays_pixelresolution'], '1080 x 1920')
        text = self.run_profiler('SPDisplaysDataType', env=env)
        self.assertIn('      Metal Support: Metal 3\n      Displays:\n', text)
        self.assertIn('          Resolution: 1080 x 1920\n', text)
        self.assertNotIn('Intel UHD Graphics 630', text)
        self.assertNotIn('Automatic Graphics Switching', text)

    def test_display_query_failure_does_not_invent_a_panel(self):
        root = self.drm_root()
        self.xrandr(root, '', status=1)
        item = self.display(root, RCLOL_PLATFORM_PROFILE='compat', RCLOL_GPU_NAME='mac')
        self.assertNotIn('spdisplays_ndrvs', item)
        self.assertEqual(item['spdisplays_mtlgpufamilysupport'], 'spdisplays_metal3')

    def test_other_model_does_not_add_a_synthetic_gpu(self):
        self.stub('ioreg', 'print(%r)' % IOREG.replace('MacBookPro16,1', 'Macmini8,1'))
        root = self.drm_root()
        env = {'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_PLATFORM_PROFILE': 'compat', 'RCLOL_GPU_NAME': 'mac'}
        items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=env).encode())[0]['_items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sppci_model'], 'AMD Radeon Pro 5500M')

    def test_host_connectors_belong_to_one_gpu_without_duplicate_displays(self):
        root = self.drm_root('0x8086', '0x9bc4', 0)
        amd = root / 'sys/class/drm/card1/device'
        amd.mkdir(parents=True)
        (amd / 'vendor').write_text('0x1002\n')
        (amd / 'device').write_text('0x67ff\n')
        (root / 'sys/class/drm/card1-DP-1').mkdir()
        self.xrandr(root, 'DP-1 connected primary 1920x1080+0+0\n'
                    'XWAYLAND0 connected 1280x720+1920+0\n')
        env = {'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_PLATFORM_PROFILE': 'host', 'RCLOL_GPU_NAME': 'host'}
        items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=env).encode())[0]['_items']
        self.assertEqual([panel['_name'] for panel in items[0]['spdisplays_ndrvs']], ['Display 2'])
        self.assertEqual([panel['_name'] for panel in items[1]['spdisplays_ndrvs']], ['Display 1'])

    def test_mirrored_outputs_and_first_active_main_without_primary(self):
        root = self.drm_root()
        self.xrandr(root, 'DP-1 disconnected\nDP-2 connected 1920x1080+0+0\n'
                    'HDMI-1 connected 1920x1080+0+0\n')
        item = self.display(root, RCLOL_PLATFORM_PROFILE='host', RCLOL_GPU_NAME='host')
        panels = item['spdisplays_ndrvs']
        self.assertEqual([panel['_name'] for panel in panels], ['Display 2', 'Display 3'])
        self.assertEqual(panels[0]['spdisplays_main'], 'spdisplays_yes')
        self.assertTrue(all(panel['spdisplays_mirror'] == 'spdisplays_on' for panel in panels))

    @staticmethod
    def edid_lines(name):
        """An EDID block as xrandr --verbose prints it, with a 0xFC monitor name descriptor."""
        edid = bytearray(128)
        edid[0:8] = bytes.fromhex('00ffffffffffff00')
        descriptor = bytes([0, 0, 0, 0xfc, 0]) + (name.encode() + b'\n').ljust(13, b' ')
        edid[72:90] = descriptor
        hexed = edid.hex()
        return '\tEDID: \n' + ''.join('\t\t%s\n' % hexed[i:i + 32] for i in range(0, 256, 32))

    def test_displays_take_the_edid_monitor_name_like_macos(self):
        root = self.drm_root()
        self.xrandr(root, 'Screen 0: minimum 8 x 8, current 5360 x 1440, maximum 32767 x 32767\n'
                    'DP-2 connected primary 3440x1440+0+0 (0x1c6) normal (normal left inverted right x axis y axis) 800mm x 340mm\n'
                    '\tIdentifier: 0x1c3\n' + self.edid_lines('PL3466WQ') +
                    '\tBorderDimensions: 4 \n'
                    '  3440x1440 (0x1c6) 319.750MHz +HSync -VSync *current +preferred\n'
                    'HDMI-2 connected 1920x1080+3440+0 (0x1d0) normal (normal left inverted right x axis y axis) 521mm x 293mm\n'
                    'DP-3 connected 1280x1024+5360+0 (0x1e0) normal (normal left inverted right x axis y axis) 0mm x 0mm\n'
                    + self.edid_lines('S24F350').replace('fc', 'fd', 1) +
                    '  1920x1080 (0x1d0) 148.500MHz +HSync +VSync *current\n')
        env = {'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_PLATFORM_PROFILE': 'compat',
               'RCLOL_GPU_NAME': 'mac', 'RCLOL_GPU_VRAM_MB': '4096'}
        items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=env).encode())[0]['_items']
        panels = items[-1]['spdisplays_ndrvs']
        # No EDID (HDMI-2) or no name descriptor (DP-3) keeps the NSScreen-style name.
        self.assertEqual([panel['_name'] for panel in panels], ['PL3466WQ', 'Display 2', 'Display 3'])
        self.assertEqual(panels[0]['spdisplays_pixelresolution'], '3440 x 1440')
        text = self.run_profiler('SPDisplaysDataType', env=env)
        self.assertIn('        PL3466WQ:\n          Resolution: 3440 x 1440\n', text)
        self.assertNotIn('Display 1:', text)

    def test_xrandr_retries_the_backend_display_zero_fallback(self):
        root = self.drm_root()
        self.xrandr(root, '')
        path = root / 'usr/bin/xrandr'
        path.write_text('#!%s\nimport os, sys\n'
                        'if os.environ.get("DISPLAY") != ":0": sys.exit(1)\n'
                        'print("DP-1 connected primary 1600x900+0+0")\n' % sys.executable)
        item = self.display(root, RCLOL_PLATFORM_PROFILE='host', RCLOL_GPU_NAME='host', DISPLAY=':99')
        self.assertEqual(item['spdisplays_ndrvs'][0]['spdisplays_pixelresolution'], '1600 x 900')

    def test_pair_preserves_vram_overrides_and_does_not_duplicate_host_gpus(self):
        root = self.drm_root(vram=8 << 30)
        intel = root / 'sys/class/drm/card1/device'
        intel.mkdir(parents=True)
        (intel / 'vendor').write_text('0x8086\n')
        (intel / 'device').write_text('0x1234\n')
        env = {'RCLOL_SYSTEM_ROOT': str(root), 'RCLOL_PLATFORM_PROFILE': 'compat', 'RCLOL_GPU_NAME': 'mac'}
        for requested, expected in [('host', '8 GB'), ('2048', '2 GB'), ('1536', '1536 MB')]:
            with self.subTest(vram=requested):
                env['RCLOL_GPU_VRAM_MB'] = requested
                items = plistlib.loads(self.run_profiler('-xml', 'SPDisplaysDataType', env=env).encode())[0]['_items']
                self.assertEqual(len(items), 2)
                self.assertEqual(items[1]['spdisplays_vram'], expected)
                self.assertEqual(items[0]['spdisplays_vram_shared'], '1536 MB')
                self.assertEqual(items[1]['sppci_bus'], 'spdisplays_pcie')

    def test_host_name_keeps_nvidia_identity(self):
        item = self.display(self.drm_root('0x10de', '0x1b80', 6 << 30),
                            RCLOL_PLATFORM_PROFILE='compat', RCLOL_GPU_NAME='host')
        self.assertEqual(item['spdisplays_device-id'], '0x1b80')
        self.assertEqual(item['spdisplays_vendor'], 'NVIDIA (0x10de)')

    def test_standalone_run_reads_the_installed_profile(self):
        prop = Path(self.folder.name) / 'darling-system.prop'
        prop.write_text('profile=host\nprofile=compat\ncompat.gpu.name=mac\ncompat.gpu.vram=4096\n')
        item = self.display(self.drm_root(), RCLOL_SYSTEM_PROP=str(prop))
        self.assertEqual(item['sppci_model'], 'AMD Radeon Pro 5500M')
        self.assertEqual(item['spdisplays_vram'], '4 GB')

    def test_launcher_handoff_wins_over_the_installed_profile(self):
        prop = Path(self.folder.name) / 'darling-system.prop'
        prop.write_text('profile=compat\ncompat.gpu.name=mac\ncompat.gpu.vram=4096\n')
        item = self.display(self.drm_root(), RCLOL_SYSTEM_PROP=str(prop), RCLOL_PLATFORM_PROFILE='host',
                            RCLOL_GPU_NAME='host', RCLOL_GPU_VRAM_MB='host')
        self.assertNotEqual(item['sppci_model'], 'AMD Radeon Pro 5500M')

    def test_nvidia_proc_model_pcie_bus_and_host_vram_fallback(self):
        root = Path(self.folder.name) / 'nvidia-root'
        device_dir = root / 'sys/class/drm/card1/device'
        device_dir.mkdir(parents=True)
        (device_dir / 'vendor').write_text('0x10de\n')
        (device_dir / 'device').write_text('0x2489\n')
        (device_dir / 'uevent').write_text('DRIVER=nvidia\nPCI_SLOT_NAME=0000:05:00.0\n')
        nv_info = root / 'proc/driver/nvidia/gpus/0000:05:00.0'
        nv_info.mkdir(parents=True)
        (nv_info / 'information').write_text('Model: \t\t NVIDIA GeForce RTX 3060 Ti\nBus Type: \t PCIe\n')
        item = self.display(root, RCLOL_PLATFORM_PROFILE='host', RCLOL_GPU_NAME='host',
                            RCLOL_GPU_VRAM_MB='host', RCLOL_HOST_GPU_VRAM_MB='8192')
        self.assertEqual(item['sppci_model'], 'NVIDIA GeForce RTX 3060 Ti')
        self.assertEqual(item['spdisplays_bus'], 'spdisplays_pcie')
        self.assertEqual(item['spdisplays_vram'], '8 GB')

    def test_host_vram_fallback_is_not_applied_to_non_nvidia_cards(self):
        root = Path(self.folder.name) / 'intel-root'
        device_dir = root / 'sys/class/drm/card0/device'
        device_dir.mkdir(parents=True)
        (device_dir / 'vendor').write_text('0x8086\n')
        (device_dir / 'device').write_text('0x9bc4\n')
        item = self.display(root, RCLOL_PLATFORM_PROFILE='host', RCLOL_GPU_NAME='host',
                            RCLOL_GPU_VRAM_MB='host', RCLOL_HOST_GPU_VRAM_MB='8192')
        self.assertNotIn('8192', item.get('spdisplays_vram', ''))


if __name__ == '__main__':
    unittest.main()
