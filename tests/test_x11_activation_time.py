"""Check that the patched X11 backend publishes the user's input time and activates windows with it."""
import unittest
from pathlib import Path
import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]


def body(source, signature):
    return source.split(signature + ' {', 1)[1].split('\n}', 1)[0]


class ActivationTimeChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def test_patch_carries_user_time_handling(self):
        patch = (ROOT / 'patches/cocotron.patch').read_text()
        self.assertIn('diff --git a/AppKit/X11.backend/X11Display.m b/AppKit/X11.backend/X11Display.m', patch)
        for needle in ('"_NET_WM_USER_TIME"', 'XChangeProperty(display, window, rclolUserTimeAtom, XA_CARDINAL, 32',
                       'rclolRateLimited(&stamp, 250000000ULL)', 'Time X11ActivationTime(Display *display) {',
                       '- (Time) lastUserTime {', 'rclolNoteUserInput(_display, ev->xany.window'):
            self.assertIn(needle, patch)

    def test_make_key_does_not_send_current_time(self):
        source = self.patched('cocotron', 'AppKit/X11.backend/X11Window.m')
        method = body(source, '- (void) makeKey')
        self.assertIn('act.xclient.data.l[1] = X11ActivationTime(_display);', method)
        self.assertNotIn('data.l[1] = CurrentTime', method)

    def test_activate_process_sets_focus_only_without_a_user_time(self):
        source = self.patched('cocotron', 'AppKit/X11.backend/X11Display.m')
        method = body(source, '- (BOOL) activateProcessIdentifier: (pid_t) processIdentifier')
        self.assertIn('event.xclient.data.l[1] = activationTime;', method)
        self.assertNotIn('data.l[1] = CurrentTime', method)
        self.assertIn('if (activationTime == CurrentTime)\n            XSetInputFocus(_display, window, RevertToPointerRoot, activationTime);', method)
        self.assertNotIn('RevertToPointerRoot, CurrentTime);', method)

    def test_user_time_activation_needs_the_wayland_opt_in(self):
        source = self.patched('cocotron', 'AppKit/X11.backend/X11Display.m')
        gate = body(source, 'static BOOL rclolUserTimeActivation(void)')
        self.assertIn('getenv("RCLOL_WAYLAND_SOCKET")', gate)
        helper = body(source, 'static void rclolNoteUserInput(Display *display, Window window, Time userTime)')
        self.assertIn('if (userTime == CurrentTime || !rclolUserTimeActivation()) return;', helper)
        activation = body(source, 'Time X11ActivationTime(Display *display)')
        self.assertLess(activation.index('if (!rclolUserTimeActivation()) return CurrentTime;'),
                        activation.index('return rclolLastUserTime;'))

    def test_input_events_record_their_time(self):
        source = self.patched('cocotron', 'AppKit/X11.backend/X11Display.m')
        method = body(source, '- (void) postXEvent: (XEvent *) ev')
        self.assertIn('rclolNoteUserInput(_display, ev->xany.window', method)
        helper = body(source, 'static void rclolNoteUserInput(Display *display, Window window, Time userTime)')


if __name__ == '__main__':
    unittest.main()
