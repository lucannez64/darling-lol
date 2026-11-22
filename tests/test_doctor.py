"""Host checks used by `darling-lol --doctor`; never starts the client or runtime."""
import contextlib
import importlib.util
import io
import json
import socket
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/doctor.py'
SPEC = importlib.util.spec_from_file_location('doctor', SCRIPT)
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)

GIB = 1024 ** 3


def facts(**overrides):
    base = {
        'host': {'distro': 'Test', 'kernel': '6.8.0', 'arch': 'x86_64', 'python': '3.12.0'},
        'session': {'type': 'wayland', 'desktop': 'Hyprland', 'display': ':0', 'wayland_display': 'wayland-1'},
        'memory': {'total': 16 * GIB, 'swap': 8 * GIB, 'total_gib': 16.0, 'swap_gib': 8.0},
        'disk': {'free': 100 * GIB, 'free_gib': 100.0},
        'gpus': [{'card': 'card0', 'driver': 'i915', 'vendor_id': '0x8086',
                  'device_id': '0x9b41', 'name': 'UHD 620'}],
        'vulkan_icds': ['/usr/share/vulkan/icd.d/intel_icd.json'],
        'prefix': '/tmp/prefix',
        'prefix_exists': True,
        'prefix_launchd': True,
        'prefix_socket': True,
        'prefix_state': {'pid': 0, 'state': 'absent', 'stale_markers': 0},
        'psr': '0',
        'perf_event_paranoid': '2',
        'perf_event_open': 'ok',
        'platform_identity': 'saved',
        'trust_store': '/home/user/.config/darling/trustcache',
        'core_limit': '0',
        'environment': {},
        'trace_env': [],
        'switches': {'diagnostic': [], 'tuning': []},
        'tools': {},
    }
    base.update(overrides)
    return base


class CheckTests(unittest.TestCase):
    def test_kernel_rejects_old_release(self):
        self.assertEqual(doctor.check_kernel(facts(host={'kernel': '5.4.0'}))[0], 'warn')
        self.assertEqual(doctor.check_kernel(facts(host={'kernel': '5.13.0'}))[0], 'warn')
        self.assertEqual(doctor.check_kernel(facts(host={'kernel': '5.14.0'}))[0], 'ok')
        self.assertEqual(doctor.check_kernel(facts(host={'kernel': '6.8.0-arch1'}))[0], 'ok')
        self.assertEqual(doctor.check_kernel(facts(host={'kernel': 'unparsed'}))[0], 'warn')

    def test_memory_requires_minimum(self):
        low = facts(memory={'total': 2 * GIB, 'swap': 0, 'total_gib': 2.0, 'swap_gib': 0.0})
        tight = facts(memory={'total': 6 * GIB, 'swap': 0, 'total_gib': 6.0, 'swap_gib': 0.0})
        self.assertEqual(doctor.check_memory(low)[0], 'fail')
        self.assertEqual(doctor.check_memory(tight)[0], 'warn')
        self.assertEqual(doctor.check_memory(facts())[0], 'ok')
        unknown = facts(memory={'total': 0, 'swap': 0, 'total_gib': 0.0, 'swap_gib': 0.0})
        self.assertEqual(doctor.check_memory(unknown)[0], 'ok')

    def test_disk_space(self):
        self.assertEqual(doctor.check_disk(facts(disk={'free': 1 * GIB, 'free_gib': 1.0}))[0], 'fail')
        self.assertEqual(doctor.check_disk(facts(disk={'free': 10 * GIB, 'free_gib': 10.0}))[0], 'warn')
        self.assertEqual(doctor.check_disk(facts())[0], 'ok')

    def test_gpu_driver_reports_card(self):
        status, message = doctor.check_gpu(facts())
        self.assertEqual(status, 'ok')
        self.assertIn('UHD 620 (i915)', message)
        self.assertEqual(doctor.check_gpu(facts(gpus=[]))[0], 'fail')
        nouveau = facts(gpus=[{'card': 'card0', 'driver': 'nouveau', 'vendor_id': '0x10de',
                               'device_id': '0x1f91', 'name': 'GTX 1650'}])
        self.assertEqual(doctor.check_gpu(nouveau)[0], 'warn')

    def test_vulkan_icds(self):
        self.assertEqual(doctor.check_vulkan(facts())[0], 'ok')
        status, message = doctor.check_vulkan(facts(vulkan_icds=[]))
        self.assertEqual(status, 'fail')
        self.assertIn('Mesa', message)

    def test_session_needs_display_on_wayland(self):
        self.assertEqual(doctor.check_session(facts())[0], 'ok')
        missing = facts(session={'type': 'wayland', 'desktop': '', 'display': '', 'wayland_display': 'wayland-1'})
        self.assertEqual(doctor.check_session(missing)[0], 'fail')
        x11 = facts(session={'type': 'x11', 'desktop': 'i3', 'display': ':1', 'wayland_display': ''})
        self.assertEqual(doctor.check_session(x11)[0], 'ok')
        bare = facts(session={'type': '', 'desktop': '', 'display': '', 'wayland_display': ''})
        self.assertEqual(doctor.check_session(bare)[0], 'warn')

    def test_mangohud_is_a_failure(self):
        self.assertEqual(doctor.check_hud(facts(environment={'MANGOHUD': '1'}))[0], 'fail')
        self.assertEqual(doctor.check_hud(facts(environment={'DISABLE_MANGOHUD': '1'}))[0], 'ok')
        self.assertEqual(doctor.check_hud(facts())[0], 'ok')

    def test_core_dumps_and_psr(self):
        self.assertEqual(doctor.check_core_dumps(facts(core_limit='unlimited'))[0], 'warn')
        self.assertEqual(doctor.check_core_dumps(facts(core_limit='0'))[0], 'ok')
        self.assertEqual(doctor.check_psr(facts(psr='1'))[0], 'warn')
        self.assertEqual(doctor.check_psr(facts(psr='0'))[0], 'ok')

    def test_perf_event_paranoid(self):
        for allowed in ('-1', '0', '1', '2', '', 'non-numeric'):
            self.assertEqual(doctor.check_perf_event_paranoid(facts(perf_event_paranoid=allowed))[0], 'ok')
        for blocked in ('3', '4'):
            status, message = doctor.check_perf_event_paranoid(facts(perf_event_paranoid=blocked))
            self.assertEqual(status, 'warn')
            self.assertIn(f'kernel.perf_event_paranoid={blocked}', message)
            self.assertIn('sudo sysctl kernel.perf_event_paranoid=2', message)
            self.assertIn('docs/TROUBLESHOOTING.md', message)

    def test_perf_event_open_refusal_beyond_paranoid_names_the_sandbox(self):
        for fine in ('ok', 'unavailable'):
            self.assertEqual(doctor.check_perf_event_open(facts(perf_event_open=fine))[0], 'ok')
        status, message = doctor.check_perf_event_open(facts(perf_event_open='EPERM'))
        self.assertEqual(status, 'warn')
        self.assertIn('seccomp', message)
        self.assertIn('docs/TROUBLESHOOTING.md', message)
        # Under a restrictive paranoid value the paranoid check already explains it.
        status, message = doctor.check_perf_event_open(facts(perf_event_open='EACCES', perf_event_paranoid='3'))
        self.assertEqual(status, 'warn')
        self.assertNotIn('seccomp', message)

    def test_gather_runs_the_perf_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            data = doctor.gather(prefix=Path(folder), env={}, host_tmp=Path(folder),
                                 which=lambda name: None, perf_probe=lambda: 'EPERM')
            self.assertEqual(data['perf_event_open'], 'EPERM')
            by_id = {result['id']: result for result in doctor.run_checks(data)}
            self.assertEqual(by_id['perf_event_open']['status'], 'warn')

    def test_real_perf_probe_answers(self):
        self.assertTrue(doctor.probe_perf_event_open())

    def test_platform_identity_states(self):
        self.assertEqual(doctor.check_platform_identity(facts())[0], 'ok')
        for state in ('missing', 'shared', 'unreadable'):
            status, message = doctor.check_platform_identity(facts(platform_identity=state))
            self.assertEqual(status, 'warn')
            self.assertIn('configure-darling-platform.py --provision --write', message)
        self.assertIn('DARLING- serial', doctor.check_platform_identity(facts(platform_identity='missing'))[1])
        self.assertEqual(doctor.check_platform_identity(
            facts(prefix_exists=False, platform_identity='missing'))[0], 'ok')

    def test_platform_identity_state_reads_the_prefix(self):
        spec = importlib.util.spec_from_file_location(
            'platform_tool', Path(doctor.__file__).with_name('configure-darling-platform.py'))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            self.assertEqual(doctor.platform_identity_state(prefix), 'missing')
            tool.save(prefix, {'IOPlatformSerialNumber': tool.KNOWN_SERIAL, 'model': 'MacBookPro16,1'})
            self.assertEqual(doctor.platform_identity_state(prefix), 'shared')
            tool.save(prefix, tool.provision({'model': 'MacBookPro16,1'}))
            self.assertEqual(doctor.platform_identity_state(prefix), 'saved')
            tool.destination(prefix).write_text('not a plist')
            self.assertEqual(doctor.platform_identity_state(prefix), 'unreadable')

    def test_runtime_signing_needs_a_trust_store(self):
        self.assertEqual(doctor.check_runtime_signing(facts())[0], 'ok')
        status, message = doctor.check_runtime_signing(facts(trust_store=''))
        self.assertEqual(status, 'warn')
        self.assertIn('sign-darling-runtime.sh --sdk', message)
        self.assertIn('scripts/update.sh', message)
        self.assertEqual(doctor.check_runtime_signing(facts(trust_store='', prefix_launchd=False))[0], 'ok')
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'cfg/darling/trustcache'
            store.mkdir(parents=True)
            self.assertEqual(doctor.trust_store({'XDG_CONFIG_HOME': str(Path(folder) / 'cfg')}), str(store))
            override = Path(folder) / 'trust/trustcache'
            override.mkdir(parents=True)
            self.assertEqual(doctor.trust_store({'DARLING_TRUST_DIR': str(override.parent),
                                                 'XDG_CONFIG_HOME': str(Path(folder) / 'cfg')}),
                             str(override))

    def test_prefix_follows_dprefix_then_the_configured_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'build').mkdir()
            (root / 'build/config.json').write_text(json.dumps({'prefix': '/mnt/disk/darling'}))
            self.assertEqual(doctor.configured_prefix({}, roots=[root]), Path('/mnt/disk/darling'))
            self.assertEqual(doctor.configured_prefix({'DPREFIX': '/other'}, roots=[root]), Path('/other'))
            self.assertEqual(doctor.configured_prefix({}, roots=[root / 'missing']),
                             Path.home() / '.darling')
            env = {'DARLING_WORKSPACE': str(root)}
            self.assertEqual(doctor.configured_prefix(env), Path('/mnt/disk/darling'))

    def test_gather_reads_perf_event_paranoid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc_root = root / 'proc'
            sysctl_dir = proc_root / 'sys/kernel'
            sysctl_dir.mkdir(parents=True)
            (sysctl_dir / 'perf_event_paranoid').write_text('3\n')
            data = doctor.gather(prefix=root / 'prefix', env={}, proc_root=proc_root,
                                 sys_root=root / 'sys', host_tmp=root / 'tmp',
                                 which=lambda name: None)
            self.assertEqual(data['perf_event_paranoid'], '3')
            by_id = {result['id']: result for result in doctor.run_checks(data)}
            self.assertEqual(by_id['perf_event_paranoid']['status'], 'warn')

    def test_prefix_must_be_installed(self):
        self.assertEqual(doctor.check_prefix(facts(prefix_exists=False))[0], 'fail')
        self.assertEqual(doctor.check_prefix(facts(prefix_launchd=False))[0], 'fail')
        self.assertEqual(doctor.check_prefix(facts())[0], 'ok')

    def test_runtime_states(self):
        self.assertEqual(doctor.check_runtime(facts(prefix_state={'pid': 42, 'state': 'running',
                                                                 'stale_markers': 0}))[0], 'ok')
        self.assertEqual(doctor.check_runtime(facts(prefix_state={'pid': 42, 'state': 'stale',
                                                                 'stale_markers': 0}))[0], 'warn')
        self.assertEqual(doctor.check_runtime(facts(prefix_state={'pid': 0, 'state': 'absent',
                                                                 'stale_markers': 0}))[0], 'ok')

    def test_stale_marker_count(self):
        self.assertEqual(doctor.check_stale_markers(
            facts(prefix_state={'pid': 0, 'state': 'absent', 'stale_markers': 2}))[0], 'ok')
        self.assertEqual(doctor.check_stale_markers(
            facts(prefix_state={'pid': 0, 'state': 'absent', 'stale_markers': 10}))[0], 'warn')


INTEL = {'card': 'card0', 'driver': 'i915', 'vendor_id': '0x8086', 'device_id': '0xa7a0', 'name': 'Iris Xe'}
NVIDIA = {'card': 'card1', 'driver': 'nvidia', 'vendor_id': '0x10de', 'device_id': '0x2860', 'name': 'RTX 4070'}
PRIME = {'__NV_PRIME_RENDER_OFFLOAD': '1', '__VK_LAYER_NV_optimus': 'NVIDIA_only',
         '__GLX_VENDOR_LIBRARY_NAME': 'nvidia'}


class HybridAndSwitchTests(unittest.TestCase):
    def test_single_gpu_is_ok(self):
        self.assertEqual(doctor.check_hybrid(facts())[0], 'ok')
        self.assertEqual(doctor.check_hybrid(facts(gpus=[NVIDIA]))[0], 'ok')

    def test_hybrid_without_offload_warns(self):
        status, message = doctor.check_hybrid(facts(gpus=[INTEL, NVIDIA]))
        self.assertEqual(status, 'warn')
        self.assertIn('prime-run', message)

    def test_hybrid_with_full_prime_offload_is_ok(self):
        self.assertEqual(doctor.check_hybrid(facts(gpus=[INTEL, NVIDIA], environment=PRIME))[0], 'ok')

    def test_partial_prime_offload_warns(self):
        partial = {'__NV_PRIME_RENDER_OFFLOAD': '1'}
        status, message = doctor.check_hybrid(facts(gpus=[INTEL, NVIDIA], environment=partial))
        self.assertEqual(status, 'warn')
        self.assertIn('__VK_LAYER_NV_optimus', message)

    def test_metal_gpu_alone_splits_the_hybrid_path(self):
        data = facts(gpus=[INTEL, NVIDIA], environment=PRIME,
                     switches={'diagnostic': ['metal-gpu'], 'tuning': []})
        status, message = doctor.check_hybrid(data)
        self.assertEqual(status, 'warn')
        self.assertIn('/tmp/metal-gpu', message)

    def test_switches_report_diagnostics_before_tuning(self):
        self.assertEqual(doctor.check_switches(facts())[0], 'ok')
        traced = facts(switches={'diagnostic': ['rclol-input-trace'], 'tuning': ['rclol-core-isolation']})
        status, message = doctor.check_switches(traced)
        self.assertEqual(status, 'warn')
        self.assertIn('/tmp/rclol-input-trace', message)
        self.assertNotIn('core-isolation', message)
        tuned = facts(switches={'diagnostic': [], 'tuning': ['rclol-core-isolation']})
        self.assertIn('/tmp/rclol-core-isolation', doctor.check_switches(tuned)[1])
        env = facts(trace_env=['RCLOL_GPU_DIRECT_TRACE'])
        self.assertIn('RCLOL_GPU_DIRECT_TRACE', doctor.check_switches(env)[1])

    def test_active_switches_reads_guest_and_host_tmp(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guest = root / 'prefix/private/tmp'
            guest.mkdir(parents=True)
            host = root / 'host-tmp'
            host.mkdir()
            (guest / 'rclol-input-trace').write_text('')
            (guest / 'metal-submit-batch-us').write_text('2000')
            (guest / 'unrelated').write_text('')
            (host / 'rclol-core-isolation').write_text('')
            self.assertEqual(doctor.active_switches(root / 'prefix', host),
                             {'diagnostic': ['rclol-input-trace'],
                              'tuning': ['metal-submit-batch-us', 'rclol-core-isolation']})

    def test_gather_names_trace_variables_without_values(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {'RCLOL_INPUT_TRACE': '1', 'RCLOL_REGION': 'EUW', 'RCLOL_AUDIO_TRACE': '',
                   'DARLING_METAL_DEVICE_LOG': '1'}
            data = doctor.gather(prefix=Path(folder), env=env, host_tmp=Path(folder),
                                 which=lambda name: None)
            self.assertEqual(data['trace_env'], ['DARLING_METAL_DEVICE_LOG', 'RCLOL_INPUT_TRACE'])
            self.assertEqual(doctor.report(data, [])['trace_env'], data['trace_env'])


class PrefixStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.prefix = self.root / 'prefix'
        self.prefix.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()

    def tearDown(self):
        self.folder.cleanup()

    def test_absent(self):
        self.assertEqual(doctor.prefix_state(self.prefix, self.proc)['state'], 'absent')

    def test_running(self):
        (self.prefix / '.init.pid').write_text('123\n')
        status = self.proc / '123'
        status.mkdir()
        (status / 'status').write_text('Name:\tdarlingserver\nState:\tS (sleeping)\n')
        self.assertEqual(doctor.prefix_state(self.prefix, self.proc)['state'], 'running')

    def test_zombie_and_missing_pid_are_stale(self):
        (self.prefix / '.init.pid').write_text('123\n')
        status = self.proc / '123'
        status.mkdir()
        (status / 'status').write_text('Name:\tdarlingserver\nState:\tZ (zombie)\n')
        self.assertEqual(doctor.prefix_state(self.prefix, self.proc)['state'], 'stale')
        (status / 'status').unlink()
        status.rmdir()
        self.assertEqual(doctor.prefix_state(self.prefix, self.proc)['state'], 'stale')

    def test_stale_markers_are_counted(self):
        (self.prefix / '.init.pid.stale-1-2').write_text('1')
        (self.prefix / '.init.pid.stale-3-4').write_text('3')
        self.assertEqual(doctor.prefix_state(self.prefix, self.proc)['stale_markers'], 2)


class RuntimeDirTests(unittest.TestCase):
    def test_workspace_runtime_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / 'usr-local/libexec/darling'
            (runtime / 'sbin').mkdir(parents=True)
            (runtime / 'sbin/launchd').write_text('')
            self.assertEqual(doctor.runtime_dir({'DARLING_WORKSPACE': folder}), runtime)

    def test_config_sdk_is_a_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / 'workspace'
            runtime = Path(folder) / 'sdk'
            (runtime / 'sbin').mkdir(parents=True)
            (runtime / 'sbin/launchd').write_text('')
            (workspace / 'build').mkdir(parents=True)
            (workspace / 'build/config.json').write_text(json.dumps({'sdk': str(runtime)}))
            self.assertEqual(doctor.runtime_dir({'DARLING_WORKSPACE': str(workspace)}), runtime)

    def test_native_path_is_last(self):
        native = Path('/usr/local/libexec/darling')
        with mock.patch.object(Path, 'is_file', autospec=True,
                               side_effect=lambda path: path == native / 'sbin/launchd'):
            self.assertEqual(doctor.runtime_dir({'DARLING_WORKSPACE': '/nonexistent'}),
                             native)

    def test_no_runtime_returns_expected_workspace_path(self):
        with mock.patch.object(Path, 'is_file', return_value=False), \
             mock.patch.object(Path, 'is_dir', return_value=False):
            self.assertEqual(doctor.runtime_dir({'DARLING_WORKSPACE': '/nonexistent'}),
                             Path('/nonexistent/usr-local/libexec/darling'))
            self.assertEqual(doctor.runtime_dir({}), Path('/usr/local/libexec/darling'))


class GpuAndVulkanTests(unittest.TestCase):
    def test_read_gpus_follows_driver_link(self):
        with tempfile.TemporaryDirectory() as folder:
            sys_root = Path(folder)
            card = sys_root / 'class/drm/card0/device'
            card.mkdir(parents=True)
            (card / 'vendor').write_text('0x8086\n')
            (card / 'device').write_text('0x9b41\n')
            (card / 'product_name').write_text('Intel UHD 620\n')
            drivers = sys_root / 'drivers/i915'
            drivers.mkdir(parents=True)
            (card / 'driver').symlink_to(drivers)
            gpus = doctor.read_gpus(sys_root)
            self.assertEqual(gpus, [{'card': 'card0', 'driver': 'i915', 'vendor_id': '0x8086',
                                     'device_id': '0x9b41', 'name': 'Intel UHD 620'}])

    def test_read_gpus_uses_nvidia_proc_model_when_product_name_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sys_root = root / 'sys'
            proc_root = root / 'proc'
            card = sys_root / 'class/drm/card1/device'
            card.mkdir(parents=True)
            (card / 'vendor').write_text('0x10de\n')
            (card / 'device').write_text('0x2489\n')
            (card / 'uevent').write_text('DRIVER=nvidia\nPCI_SLOT_NAME=0000:05:00.0\n')
            drivers = sys_root / 'drivers/nvidia'
            drivers.mkdir(parents=True)
            (card / 'driver').symlink_to(drivers)
            nv_info = proc_root / 'driver/nvidia/gpus/0000:05:00.0'
            nv_info.mkdir(parents=True)
            (nv_info / 'information').write_text('Model:\t\t NVIDIA GeForce RTX 3060 Ti\nBus Type:\t PCIe\n')
            gpus = doctor.read_gpus(sys_root, proc_root)
            self.assertEqual(gpus, [{'card': 'card1', 'driver': 'nvidia', 'vendor_id': '0x10de',
                                     'device_id': '0x2489', 'name': 'NVIDIA GeForce RTX 3060 Ti'}])

    def test_vulkan_icds_deduplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            icd = Path(folder) / 'vulkan/icd.d'
            icd.mkdir(parents=True)
            (icd / 'intel.json').write_text('{}')
            second = Path(folder) / 'other/vulkan/icd.d'
            second.mkdir(parents=True)
            (second / 'radeon.json').write_text('{}')
            found = doctor.vulkan_icds({'XDG_DATA_DIRS': '%s:%s' % (folder, second.parents[1])})
            self.assertIn(str(icd / 'intel.json'), found)
            self.assertIn(str(second / 'radeon.json'), found)
            self.assertEqual(len(found), len(set(found)))


class WaylandCheckTests(unittest.TestCase):
    def wayland(self, **fields):
        base = {'requested': '', 'session_type': 'x11', 'socket': '/run/user/1000/wayland-1',
                'socket_found': False, 'library': '/checkout/build/libdarling-wayland.so',
                'library_found': False}
        base.update(fields)
        return facts(wayland=base)

    def test_unset_flag_on_x11_is_informational_ok(self):
        self.assertEqual(doctor.check_wayland(self.wayland())[0], 'ok')
        self.assertEqual(doctor.check_wayland(facts())[0], 'ok')

    def test_wayland_session_reports_the_facts_without_failing(self):
        status, message = doctor.check_wayland(self.wayland(session_type='wayland'))
        self.assertEqual(status, 'ok')
        self.assertIn('socket /run/user/1000/wayland-1 (missing)', message)
        self.assertIn('library /checkout/build/libdarling-wayland.so (missing)', message)

    def test_flag_on_with_socket_and_library_is_ok(self):
        status, message = doctor.check_wayland(self.wayland(
            requested='1', session_type='wayland', socket_found=True, library_found=True))
        self.assertEqual(status, 'ok')
        self.assertIn('RCLOL_WAYLAND=1', message)

    def test_flag_on_without_socket_or_library_warns_and_never_fails(self):
        for fields in ({'socket_found': False, 'library_found': True},
                       {'socket_found': True, 'library_found': False},
                       {'socket_found': False, 'library_found': False}):
            status, message = doctor.check_wayland(self.wayland(requested='1', **fields))
            self.assertEqual(status, 'warn', fields)
            self.assertIn('stays on X11', message)

    def test_game_scope_is_a_valid_on_value(self):
        status, message = doctor.check_wayland(self.wayland(
            requested='game', session_type='wayland', socket_found=True, library_found=True))
        self.assertEqual(status, 'ok')
        self.assertIn('RCLOL_WAYLAND=game', message)
        status, message = doctor.check_wayland(self.wayland(requested='game', socket_found=False,
                                                            library_found=True))
        self.assertEqual(status, 'warn')
        self.assertIn('stays on X11', message)

    def test_invalid_value_warns_as_off(self):
        status, message = doctor.check_wayland(self.wayland(requested='yes'))
        self.assertEqual(status, 'warn')
        self.assertIn('is not 0, 1 or game', message)
        self.assertIn('treats it as 0', message)

    def test_socket_rule_matches_the_launcher(self):
        self.assertEqual(doctor.wayland_socket_path({'WAYLAND_DISPLAY': '/tmp/w/sock',
                                                     'XDG_RUNTIME_DIR': '/run/user/1000'}), '/tmp/w/sock')
        self.assertEqual(doctor.wayland_socket_path({'WAYLAND_DISPLAY': 'wayland-2',
                                                     'XDG_RUNTIME_DIR': '/run/user/1000'}),
                         '/run/user/1000/wayland-2')
        self.assertEqual(doctor.wayland_socket_path({'XDG_RUNTIME_DIR': '/run/user/1000'}),
                         '/run/user/1000/wayland-0')

    def test_gather_finds_a_real_socket_and_the_workspace_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / 'runtime'
            runtime.mkdir()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(runtime / 'wayland-1'))
                workspace = root / 'workspace'
                (workspace / 'build').mkdir(parents=True)
                (workspace / 'build/libdarling-wayland.so').write_bytes(b'')
                env = {'RCLOL_WAYLAND': '1', 'WAYLAND_DISPLAY': 'wayland-1',
                       'XDG_RUNTIME_DIR': str(runtime), 'XDG_SESSION_TYPE': 'wayland',
                       'DARLING_WORKSPACE': str(workspace)}
                data = doctor.gather(prefix=root / 'prefix', env=env, host_tmp=root,
                                     which=lambda name: None)
            finally:
                listener.close()
            wayland = data['wayland']
            self.assertTrue(wayland['socket_found'])
            self.assertTrue(wayland['library_found'])
            self.assertEqual(wayland['library'], str(workspace / 'build/libdarling-wayland.so'))
            self.assertEqual(doctor.check_wayland(data)[0], 'ok')
            self.assertIn('RCLOL_WAYLAND', data['environment'])
            self.assertIn('wayland', [check[0] for check in doctor.CHECKS])

    def test_run_checks_includes_the_wayland_check(self):
        results = doctor.run_checks(facts())
        self.assertIn('wayland', {result['id'] for result in results})


class ReportTests(unittest.TestCase):
    def test_run_checks_isolates_a_broken_probe(self):
        results = doctor.run_checks({'host': {}})
        by_id = {result['id']: result for result in results}
        self.assertEqual(len(results), len(doctor.CHECKS))
        self.assertEqual(by_id['kernel']['status'], 'warn')
        self.assertIn('could not run', by_id['kernel']['message'])

    def test_summary_counts(self):
        results = [{'status': 'ok'}, {'status': 'ok'}, {'status': 'warn'}, {'status': 'fail'}]
        self.assertEqual(doctor.summarize(results), {'ok': 2, 'warn': 1, 'fail': 1})

    def test_report_has_no_environment_values(self):
        data = facts(environment={'DISPLAY': ':0', 'RCLOL_REGION': 'EUW'})
        document = doctor.report(data, doctor.run_checks(data))
        self.assertEqual(document['schema'], doctor.SCHEMA)
        self.assertEqual(document['environment'], {'DISPLAY': ':0', 'RCLOL_REGION': 'EUW'})
        self.assertNotIn('MANGOHUD', document['environment'])

    def test_report_redacts_paths_by_default(self):
        home = str(Path.home())
        data = facts(prefix=home + '/.darling', workspace=home + '/work',
                     environment={'DPREFIX': home + '/.darling',
                                  'DARLING_WORKSPACE': home + '/work'},
                     prefix_state={'pid': 0, 'state': 'absent', 'stale_markers': 0})
        results = [{'id': 'prefix', 'title': 'Darling prefix', 'status': 'ok',
                    'message': home + '/.darling'}]
        document = doctor.report(data, results)
        self.assertEqual(document['prefix'], '<prefix>')
        self.assertEqual(document['environment']['DPREFIX'], '<prefix>')
        self.assertEqual(document['environment']['DARLING_WORKSPACE'], '<workspace>')
        self.assertEqual(document['checks'][0]['message'], '<prefix>')
        self.assertNotIn(home, json.dumps(document))

    def test_report_can_keep_real_paths(self):
        data = facts(prefix='/tmp/darling-prefix')
        document = doctor.report(data, [], redact=False)
        self.assertEqual(document['prefix'], '/tmp/darling-prefix')
        self.assertFalse(document['redacted'])

    def test_cli_writes_json(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'prefix'
            prefix.mkdir()
            out = Path(folder) / 'doctor.json'
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = doctor.main(['--prefix', str(prefix), '--report', str(out)])
            self.assertIn(code, (0, 1))
            document = json.loads(out.read_text())
            self.assertEqual(document['tool'], 'darling-lol doctor')
            self.assertIn('checks', document)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
