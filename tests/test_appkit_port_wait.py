"""Keep the AppKit Mach port wait wired to CoreFoundation and its safety fallbacks.

The behaviour itself is checked in Darling by tools/check-appkit-idle-wait.py.
"""
import unittest

import test_runtime_compatibility as runtime

DISPLAY = 'AppKit/X11.backend/X11Display.m'


class AppKitPortWaitChecks(unittest.TestCase):
    def test_corefoundation_exposes_mode_port_set(self):
        source = runtime.CompatibilityChecks().patched('corefoundation', 'CFRunLoop.c')
        body = runtime.function(source, '_CFRunLoopGetModePortSet_darling')
        self.assertIn('__CFRunLoopFindMode(rl, modeName, false)', body)
        self.assertIn('__CFRunLoopModeUnlock(rlm)', body)
        self.assertIn('__CFRunLoopUnlock(rl)', body)

    def test_display_waits_on_ports_with_fallbacks(self):
        source = runtime.CompatibilityChecks().patched('cocotron', DISPLAY)
        # Older CoreFoundation without the accessor keeps the polling wait.
        self.assertIn('_CFRunLoopGetModePortSet_darling(CFRunLoopRef, CFStringRef) '
                      '__attribute__((weak_import))', source)
        self.assertIn('RCLOL_APPKIT_PORT_WAIT', source)
        # The game process keeps its input timing.
        self.assertIn('!X11IsLeagueGameWindow(_display, None, nil)', source)
        # Cross-thread postEvent: must wake the blocked main loop.
        post = source[source.index('- (void) postEvent: (NSEvent *) event atStart: (BOOL) atStart {'):]
        post = post[:post.index('\n}\n')]
        self.assertIn('CFRunLoopWakeUp(CFRunLoopGetMain())', post)
        # The idle wait still falls back to the short poll when ports are not watched.
        self.assertIn('![self _waitForX11OrPortsUntil: deadline mode: cfMode]', source)


if __name__ == '__main__':
    unittest.main()
