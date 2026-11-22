"""Push-to-talk needs a working session event tap (issue #285).

The Riot API's voice chat registers push-to-talk with AXIsProcessTrusted(),
then CGEventTapCreate(kCGSessionEventTap, listen-only, key down/up, flags
changed, right/other mouse buttons) on the main run loop. Darling answered
"not trusted", and its taps never received an event. Now:

- AXIsProcessTrusted/WithOptions answer true (Accessibility granted);
- CGEventTap keeps a registry of live taps; one X11 thread per process reads
  XInput2 raw key/button events on the root window (any window focused) and
  posts them to every enabled tap's port, where the existing callout runs the
  client callback;
- CGEventTapMap.h turns X keycodes (through the AppKit x11ToCarbon table) and
  buttons into macOS tap events; it is compiled and exercised here.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COCOTRON = ROOT / 'patches/cocotron.patch'
DARLING = ROOT / 'patches/darling.patch'


def section(patch, path):
    text = patch.read_text()
    start = text.index(f'diff --git a/{path} b/{path}\n')
    end = text.find('\ndiff --git ', start + 1)
    return text[start:end if end > 0 else len(text)]


def added(patch, path):
    return [line[1:] for line in section(patch, path).splitlines()
            if line.startswith('+') and not line.startswith('+++')]


def added_text(patch, path):
    return '\n'.join(added(patch, path))


def postimage(patch, path):
    """Context and added lines: the patched text of every hunk."""
    return '\n'.join(line[1:] for line in section(patch, path).splitlines()
                     if line.startswith(' ') or (line.startswith('+') and not line.startswith('+++')))


class PushToTalkPatch(unittest.TestCase):
    def test_accessibility_reports_trusted(self):
        path = 'src/frameworks/ApplicationServices/HIServices/src/HIServices.c'
        text = postimage(DARLING, path)
        self.assertIn('Boolean AXIsProcessTrusted(void)\n{\n    return true;\n}', text)
        self.assertIn('Boolean AXIsProcessTrustedWithOptions(CFDictionaryRef options)\n{\n'
                      '    (void) options; // no consent prompt to show\n    return true;\n}', text)
        self.assertNotIn('return NULL', added_text(DARLING, path))

    def test_libxi_is_wrapped_for_the_guest(self):
        self.assertIn('\twrap_elf(Xi libXi.so)', added(DARLING, 'src/native/CMakeLists.txt'))

    def test_taps_are_registered_and_fed(self):
        tap = postimage(COCOTRON, 'CoreGraphics/CGEventTap.m')
        create = tap[tap.index('CFMachPortRef mp = [newTap createCFMachPort];'):]
        self.assertLess(create.index('registerTap(newTap);'), create.index('_CGEventTapX11Start();'))
        # Enable and teardown use the registry instead of a nil object.
        self.assertNotIn('CGEventTap* tapObj = nil;', tap)
        self.assertIn('g_taps[i].tap.enabled = enable;', tap)
        self.assertIn('g_taps[i] = g_taps[--g_tapCount];', tap)
        # Only enabled taps whose mask wants the event; never block on a stalled client.
        self.assertIn('if (!tap.enabled || !(tap.mask & bit))', tap)
        self.assertIn('MACH_SEND_MSG | MACH_SEND_TIMEOUT', tap)
        self.assertIn('CFRelease(event);', tap)  # the reference the failed send would have handed over
        # Keys go through the AppKit table the client's key bindings come from.
        self.assertIn('#include "../AppKit/X11.backend/CarbonKeys.h"', tap)
        self.assertIn('x11ToCarbon[detail]', tap)

    def test_listener_selects_raw_events_on_the_root_window(self):
        listener = added_text(COCOTRON, 'CoreGraphics/CGEventTapX11.c')
        for event in ('XI_RawKeyPress', 'XI_RawKeyRelease', 'XI_RawButtonPress', 'XI_RawButtonRelease'):
            self.assertIn(f'XISetMask(bits, {event});', listener)
        self.assertIn('.deviceid = XIAllMasterDevices', listener)
        self.assertIn('selectEvents(display, defaultRoot(display), &mask, 1);', listener)
        # Optional at run time: a missing library or display only disables global input.
        self.assertIn('dlopen(LIB_XI, RTLD_NOW | RTLD_LOCAL)', listener)
        self.assertIn('report("no X11 display");', listener)
        self.assertIn('pthread_once(&once, start_once);', listener)
        self.assertIn('\tCGEventTapX11.c', added(COCOTRON, 'CoreGraphics/CMakeLists.txt'))

    def test_created_events_carry_their_integer_fields(self):
        event = added_text(COCOTRON, 'CoreGraphics/CGEvent.m')
        self.assertIn('CGEventSetIntegerValueField((CGEventRef) event, kCGKeyboardEventKeycode, virtualKey);', event)
        self.assertIn('CGEventSetIntegerValueField((CGEventRef) event, kCGMouseEventButtonNumber, mouseButton);', event)

    def test_native_installers_provide_libxi(self):
        script = (ROOT / 'scripts/install-native.sh').read_text()
        for package in ('libXi-devel', ' libxi ', 'libxi-dev'):
            self.assertIn(package, script)
        self.assertIn('libxi', (ROOT / 'flake.nix').read_text())


class PushToTalkMapping(unittest.TestCase):
    def test_mapping_runtime(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('needs a C compiler')
        header = added_text(COCOTRON, 'CoreGraphics/CGEventTapMap.h') + '\n'
        program = r'''
#include <assert.h>
#include "CGEventTapMap.h"

int main(void) {
    CGTapMapState s = {0};
    CGTapMapEvent e;

    /* Ordinary key: down/up with its Carbon code. */
    assert(CGTapMapKey(&s, 0x09 /* V */, true, &e));
    assert(e.type == CGTapMapKeyDown && e.keycode == 0x09 && e.flags == 0);
    assert(CGTapMapKey(&s, 0x09, false, &e) && e.type == CGTapMapKeyUp);

    /* Modifiers are flags-changed events carrying the key and the new flags. */
    assert(CGTapMapKey(&s, 0x3B /* Control */, true, &e));
    assert(e.type == CGTapMapFlagsChanged && e.keycode == 0x3B && e.flags == CGTapMapFlagControl);
    assert(!CGTapMapKey(&s, 0x3B, true, &e)); /* no change while held */
    assert(CGTapMapKey(&s, 0x09, true, &e) && e.flags == CGTapMapFlagControl);
    assert(CGTapMapKey(&s, 0x3E /* RightControl */, true, &e) && e.flags == CGTapMapFlagControl);
    assert(CGTapMapKey(&s, 0x3B, false, &e) && e.flags == CGTapMapFlagControl); /* right one still held */
    assert(CGTapMapKey(&s, 0x3E, false, &e) && e.type == CGTapMapFlagsChanged && e.flags == 0);
    assert(!CGTapMapKey(&s, 0x3E, false, &e)); /* stray release */

    assert(CGTapMapKey(&s, 0x3A /* Option */, true, &e) && e.flags == CGTapMapFlagAlternate);
    assert(CGTapMapKey(&s, 0x36 /* RightCommand */, true, &e) &&
           e.flags == (CGTapMapFlagAlternate | CGTapMapFlagCommand));
    assert(CGTapMapKey(&s, 0x3A, false, &e) && CGTapMapKey(&s, 0x36, false, &e) && e.flags == 0);

    /* Caps Lock toggles on press only. */
    assert(CGTapMapKey(&s, 0x39, true, &e) && e.type == CGTapMapFlagsChanged && e.flags == CGTapMapFlagAlphaShift);
    assert(!CGTapMapKey(&s, 0x39, false, &e));
    assert(CGTapMapKey(&s, 0x39, true, &e) && e.flags == 0);

    /* Buttons: 1 left, 3 right, 2 center, 4-7 scroll steps, 8/9 side buttons 3/4. */
    assert(CGTapMapButton(&s, 1, true, &e) && e.type == CGTapMapLeftMouseDown && e.button == 0);
    assert(CGTapMapButton(&s, 3, false, &e) && e.type == CGTapMapRightMouseUp && e.button == 1);
    assert(CGTapMapButton(&s, 2, true, &e) && e.type == CGTapMapOtherMouseDown && e.button == 2);
    for (unsigned b = 4; b <= 7; b++)
        assert(!CGTapMapButton(&s, b, true, &e));
    assert(!CGTapMapButton(&s, 0, true, &e));
    assert(CGTapMapButton(&s, 8, true, &e) && e.type == CGTapMapOtherMouseDown && e.button == 3);
    assert(CGTapMapButton(&s, 9, false, &e) && e.type == CGTapMapOtherMouseUp && e.button == 4);

    /* Riot's tap mask 0x6001c18 covers every event push-to-talk can be bound to. */
    const unsigned long long riotMask = 0x6001c18ull;
    const unsigned wanted[] = { CGTapMapKeyDown, CGTapMapKeyUp, CGTapMapFlagsChanged, CGTapMapRightMouseDown,
                                CGTapMapRightMouseUp, CGTapMapOtherMouseDown, CGTapMapOtherMouseUp };
    for (unsigned i = 0; i < sizeof(wanted) / sizeof(wanted[0]); i++)
        assert(riotMask & (1ull << wanted[i]));

    /* x11ToCarbon gives 0 for unknown keys, and for A (X keycode 38). */
    assert(CGTapMapKnownKeycode(38, 0));
    assert(!CGTapMapKnownKeycode(9, 0));
    assert(CGTapMapKnownKeycode(50, 0x38));
    assert(!CGTapMapKnownKeycode(300, 0x38));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'CGEventTapMap.h').write_text(header)
            src = Path(folder) / 'map_test.c'
            exe = Path(folder) / 'map_test'
            src.write_text(program)
            subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', folder,
                            '-o', str(exe), str(src)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
