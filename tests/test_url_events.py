"""URL events reach running guest apps, as LaunchServices delivers them on macOS.

The Riot Client finishes Google sign-in through riotclient://auth/...; its
RiotClientServices registers an NSAppleEventManager handler for kAEGetURL and
also implements -application:openURLs:. Darling had neither path: the manager
only printed STUB, NSAppleEventDescriptor forwarded every selector to NSLog, and
LaunchServices rejected every scheme but http, https, mailto and file.

These checks read the patches (CI cannot run Darling); the behaviour is covered
in the guest by tools/riot-url-event-check.m.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def new_side(patch, path):
    """`path` as the patch leaves it: context and added lines of its hunks."""
    sections = (ROOT / patch).read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


def added(patch, path):
    sections = (ROOT / patch).read_text().split('diff --git ')
    section = next(s for s in sections if s.startswith('a/' + path + ' b/'))
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class AppleEventFoundationTests(unittest.TestCase):
    def test_manager_records_handlers_and_dispatches(self):
        manager = added('patches/foundation.patch', 'src/NSAppleEventManager.m')
        self.assertIn('valueWithNonretainedObject:handler', manager)  # not retained, as on macOS
        self.assertIn('- (BOOL)_darlingDispatchAppleEvent:(NSAppleEventDescriptor *)event', manager)
        self.assertIn('performSelector:selector withObject:event withObject:reply', manager)
        # currentAppleEvent is the event while its handler runs, nil otherwise.
        self.assertRegex(manager, r'_currentEvent = event;[\s\S]*@finally \{\s*_currentEvent = savedEvent;')
        self.assertNotIn('printf("STUB', new_side('patches/foundation.patch', 'src/NSAppleEventManager.m'))

    def test_descriptor_carries_a_url_parameter(self):
        descriptor = added('patches/foundation.patch', 'src/NSAppleEventDescriptor.m')
        self.assertNotIn('// DUMMY', new_side('patches/foundation.patch', 'src/NSAppleEventDescriptor.m'))
        for method in ('appleEventWithEventClass:', 'descriptorWithString:', 'setParamDescriptor:',
                       'paramDescriptorForKeyword:', '- (NSString *)stringValue', '- (AEEventClass)eventClass'):
            with self.subTest(method=method):
                self.assertIn(method, descriptor)
        self.assertIn("kDarlingTypeUTF8Text    = 'utf8'", descriptor)

    def test_manifest_counts_the_descriptor(self):
        import json
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c['patch'] == 'patches/foundation.patch')
        sections = (ROOT / 'patches/foundation.patch').read_text().split('\ndiff --git ')
        new = sum('\nnew file mode' in s.split('\n@@', 1)[0] for s in sections)
        self.assertEqual(component['modified_files'], len(sections) - new)


class AppKitInboxTests(unittest.TestCase):
    def setUp(self):
        self.appkit = added('patches/cocotron.patch', 'AppKit/NSApplication.m')

    def test_serves_one_port_per_declared_scheme_after_will_finish_launching(self):
        self.assertIn('#define DARLING_URL_EVENT_PORT_PREFIX @"org.darlinghq.url-event."', self.appkit)
        self.assertIn('objectForInfoDictionaryKey: @"CFBundleURLTypes"', self.appkit)
        self.assertIn('CFMessagePortCreateLocal(NULL, name, _darlingURLEventReceived', self.appkit)
        # Right after the WillFinishLaunching notification block, when handlers exist.
        self.assertRegex(new_side('patches/cocotron.patch', 'AppKit/NSApplication.m'),
                         r'NS_ENDHANDLER\n\s*// Handlers for URL events are registered by now '
                         r'\(applicationWillFinishLaunching:\)\.\n\s*\[self _darlingStartURLEventInboxes\];\n'
                         r'\s*// Load the application icon')

    def test_serves_a_scheme_declared_twice_once(self):
        self.assertIn('[served containsObject: scheme]', self.appkit)
        self.assertIn('[served addObject: scheme];', self.appkit)

    def test_leaves_web_and_file_schemes_to_the_host(self):
        for scheme in ('http', 'https', 'file'):
            self.assertIn(f'[scheme isEqualToString: @"{scheme}"]', self.appkit)

    def test_checks_every_url_it_receives(self):
        # Any guest process can write to the port.
        check = self.appkit.split('static BOOL _darlingURLIsDeliverable', 1)[1].split('\n}', 1)[0]
        self.assertIn('DARLING_URL_EVENT_MAX_LENGTH', check)
        self.assertIn('c < 0x20 || c == 0x7f', check)
        self.assertIn('isEqualToString: scheme', check)
        self.assertIn('#define DARLING_URL_EVENT_MAX_LENGTH 4096', self.appkit)

    def test_prefers_the_apple_event_handler_then_the_delegate(self):
        received = self.appkit.split('static CFDataRef _darlingURLEventReceived', 1)[1].split('\n}\n', 1)[0]
        self.assertLess(received.index('_darlingDispatchAppleEvent: event'),
                        received.index('application: NSApp'))
        self.assertIn("kDarlingInternetEventClass = 'GURL', kDarlingAEGetURL = 'GURL', kDarlingKeyDirectObject = '----'",
                      self.appkit)


class LaunchServicesDeliveryTests(unittest.TestCase):
    def test_both_open_paths_try_the_running_app(self):
        lsopen = new_side('patches/darling.patch', 'src/frameworks/CoreServices/src/LaunchServices/LSOpen.m')
        self.assertIn('LS_URL_EVENT_PORT_PREFIX "org.darlinghq.url-event."', lsopen)
        self.assertRegex(lsopen, r'status = _LSOpenURLInRunningApp\(url\);\s*if \(status != kLSApplicationNotFoundErr\)')
        spec = new_side('patches/darling.patch', 'src/frameworks/CoreServices/src/LaunchServices/LaunchServices.c')
        self.assertRegex(spec, r'temp = _LSOpenURLInRunningApp\(url\);\s*if \(temp == kLSApplicationNotFoundErr\)')

    def test_port_prefix_matches_appkit(self):
        lsopen = added('patches/darling.patch', 'src/frameworks/CoreServices/src/LaunchServices/LSOpen.m')
        appkit = added('patches/cocotron.patch', 'AppKit/NSApplication.m')
        prefix = re.search(r'LS_URL_EVENT_PORT_PREFIX "([^"]+)"', lsopen).group(1)
        self.assertIn(f'DARLING_URL_EVENT_PORT_PREFIX @"{prefix}"', appkit)

    def test_keeps_file_urls_and_the_size_limit(self):
        lsopen = added('patches/darling.patch', 'src/frameworks/CoreServices/src/LaunchServices/LSOpen.m')
        self.assertIn('strcmp(schemeBuf, "file") == 0', lsopen)
        self.assertIn('#define LS_URL_EVENT_MAX_LENGTH 4096', lsopen)


if __name__ == '__main__':
    unittest.main()
