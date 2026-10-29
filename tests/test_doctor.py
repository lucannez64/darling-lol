"""Host checks used by `darling-lol --doctor`; never starts the client or runtime."""
import contextlib
import importlib.util
import io
import json
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
        'core_limit': '0',
        'environment': {},
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
