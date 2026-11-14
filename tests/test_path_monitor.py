"""libnetwork has a working nw_path_monitor instead of NULL-returning stubs.

The League client's ConnectivityProvider creates an NWPathMonitor at startup;
with every nw_path_monitor_* entry point stubbed it logged
'nw_path_monitor_create_failed'. The monitor now delivers the current path once
on its queue and honours cancel.

These checks read the patch (CI cannot run Darling).
"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = 'patches/libnetwork.patch'
IMPLEMENTED = ('nw_path_monitor_create', 'nw_path_monitor_create_with_type',
               'nw_path_monitor_prohibit_interface_type', 'nw_path_monitor_set_queue',
               'nw_path_monitor_set_update_handler', 'nw_path_monitor_set_cancel_handler',
               'nw_path_monitor_start', 'nw_path_monitor_cancel',
               'nw_path_has_dns', 'nw_path_uses_interface_type',
               'nw_path_enumerate_interfaces', 'nw_path_is_equal')


def section(path):
    sections = (ROOT / PATCH).read_text().split('diff --git ')
    return next(s for s in sections if s.startswith('a/' + path + ' b/'))


def added(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def removed(path):
    return '\n'.join(line[1:] for line in section(path).splitlines()
                     if line.startswith('-') and not line.startswith('---'))


def body(source, signature):
    return source.split(signature, 1)[1].split('\n};', 1)[0]


class PathMonitorTests(unittest.TestCase):
    def setUp(self):
        self.header = added('include/nw/path.h')
        self.source = added('src/nw/path.m')

    def test_stubs_are_replaced_under_the_same_symbol_names(self):
        for name in IMPLEMENTED:
            with self.subTest(name=name):
                self.assertNotIn(f'DARLING_NW_STUB({name});', self.source)
                self.assertNotIn(f'void* {name}(void);', self.header)
                self.assertRegex(self.header, rf'\b{name}\(')
                self.assertRegex(self.source, rf'(?m)^[\w ]+[ *]{name}\([^;]*\) \{{$')
        # A stub is only removed when it is implemented, or was never upstream.
        gone = set(re.findall(r'DARLING_NW_STUB\((\w+)\);', removed('src/nw/path.m')))
        self.assertLessEqual(gone, set(IMPLEMENTED))

    def test_signatures_and_constants_match_network_framework(self):
        for prototype in (
                'nw_path_monitor_t nw_path_monitor_create(void);',
                'nw_path_monitor_t nw_path_monitor_create_with_type(nw_interface_type_t required_interface_type);',
                'void nw_path_monitor_set_queue(nw_path_monitor_t monitor, dispatch_queue_t queue);',
                'void nw_path_monitor_set_update_handler(nw_path_monitor_t monitor, '
                'nw_path_monitor_update_handler_t update_handler);',
                'void nw_path_monitor_start(nw_path_monitor_t monitor);',
                'void nw_path_monitor_cancel(nw_path_monitor_t monitor);',
                'bool nw_path_uses_interface_type(nw_path_t path, nw_interface_type_t interface_type);',
                'typedef void (^nw_path_monitor_update_handler_t)(nw_path_t path);'):
            with self.subTest(prototype=prototype):
                self.assertIn(prototype, self.header)
        for constant in ('nw_path_status_invalid = 0', 'nw_path_status_satisfied = 1',
                         'nw_path_status_unsatisfied = 2', 'nw_path_status_satisfiable = 3'):
            self.assertIn(constant, self.header)
        interface = added('include/nw/interface.h')
        for constant in ('nw_interface_type_other = 0', 'nw_interface_type_wifi = 1',
                         'nw_interface_type_cellular = 2', 'nw_interface_type_wired = 3',
                         'nw_interface_type_loopback = 4'):
            self.assertIn(constant, interface)

    def test_create_returns_an_object_that_can_be_retained(self):
        self.assertIn('@interface _NW_CONCRETE(nw_path_monitor)', self.source)
        self.assertIn('return [_NW_CONCRETE_IMPL(nw_path_monitor) new];',
                      body(self.source, 'nw_path_monitor_t nw_path_monitor_create(void) {'))
        # C and C++ callers own nw objects through nw_retain/nw_release.
        base = added('src/nw/nw.m')
        self.assertIn('return [(id)nw_object retain];', base)
        self.assertIn('[(id)nw_object release];', base)

    def test_status_comes_from_the_guest_interfaces(self):
        create = body(self.source + '\n};', 'static nw_path_t nw_path_create_for_monitor(').split('\n}\n', 1)[0]
        self.assertIn('getifaddrs(&interfaces) == 0', create)
        self.assertIn('!(ifa->ifa_flags & IFF_UP) || (ifa->ifa_flags & IFF_LOOPBACK)', create)
        self.assertIn('freeifaddrs(interfaces);', create)
        self.assertIn('(path->_has_ipv4 || path->_has_ipv6) ? nw_path_status_satisfied : nw_path_status_unsatisfied',
                      create)
        self.assertIn('return path ? NW_PATH(path)->_status : nw_path_status_invalid;', self.source)

    def test_start_delivers_one_update_on_the_queue_unless_cancelled(self):
        start = body(self.source, 'void nw_path_monitor_start(nw_path_monitor_t monitor) {')
        self.assertIn('if (!m->_queue || m->_started || m->_cancelled)\n\t\t\treturn;', start)
        self.assertEqual(start.count('dispatch_async(queue, ^{'), 1)
        asynchronous = start.split('dispatch_async(queue, ^{', 1)[1]
        self.assertIn('if (!m->_cancelled && m->_update_handler)', asynchronous)
        self.assertIn('update_handler(path);', asynchronous)
        self.assertNotIn('update_handler(path);', start.split('dispatch_async(queue, ^{', 1)[0])

    def test_cancel_runs_the_cancel_handler_once_on_the_queue(self):
        cancel = body(self.source, 'void nw_path_monitor_cancel(nw_path_monitor_t monitor) {')
        self.assertRegex(cancel, r'if \(m->_cancelled\)\s*return;\s*m->_cancelled = true;')
        self.assertIn('m->_update_handler = NULL;', cancel)
        self.assertIn('m->_cancel_handler = NULL;', cancel)
        self.assertEqual(cancel.count('cancel_handler();'), 1)
        self.assertIn('cancel_handler();', cancel.split('dispatch_async(queue, ^{', 1)[1].split('} else {', 1)[0])

    def test_manifest_counts_the_patched_files(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == PATCH)
        files = set(re.findall(r'^diff --git \w/(\S+) ', (ROOT / PATCH).read_text(), re.M))
        self.assertEqual(component['modified_files'], len(files - set(component['new_files'])))


if __name__ == '__main__':
    unittest.main()
