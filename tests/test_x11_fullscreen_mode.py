"""Compile the patched fullscreen policy and display-mode geometry helpers."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import test_runtime_compatibility as sources


def body(source, signature):
    return source.split(signature + ' {', 1)[1].split('\n}', 1)[0]


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class FullscreenModeChecks(unittest.TestCase):
    def source(self, path):
        if shutil.which('patch'):
            return sources.CompatibilityChecks.patched(self, 'cocotron', path)
        # Hosts without the patch utility (CI has it) still exercise the
        # prepared tree instead of skipping the whole regression.
        prepared = sources.ROOT / 'build/darling-src/src/external/cocotron' / path
        if not prepared.exists():
            self.skipTest('patch utility or prepared Darling sources required')
        return prepared.read_text()

    def run_cpp(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'check.cpp').write_text(source)
            result = subprocess.run(['c++', '-std=c++17', str(path / 'check.cpp'),
                                     '-o', str(path / 'check')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'check')], check=True, timeout=10)

    def test_explicit_game_fullscreen_needs_the_monitor_size(self):
        source = self.source('AppKit/X11.backend/X11Window.m')
        policy = sources.function(source, 'X11ShouldRequestFullscreen')
        self.run_cpp(r'''
#include <cassert>
using BOOL=bool;
constexpr BOOL NO=false, YES=true;
''' + policy + r'''
int main() {
    /* A game that keeps the exclusive-fullscreen style but resizes below the
     * monitor must not stay pinned to the monitor by EWMH fullscreen. */
    assert(!X11ShouldRequestFullscreen(true, true, false, false));
    assert(X11ShouldRequestFullscreen(true, true, false, true));
    assert(X11ShouldRequestFullscreen(true, false, false, true));
    assert(!X11ShouldRequestFullscreen(true, false, false, false));
    /* Ordinary Cocoa fullscreen keeps its explicit request. */
    assert(X11ShouldRequestFullscreen(false, true, true, false));
    assert(X11ShouldRequestFullscreen(false, true, false, false));
    /* Borderless windows still need to cover a monitor; titled ones do not. */
    assert(!X11ShouldRequestFullscreen(false, false, true, true));
    assert(X11ShouldRequestFullscreen(false, false, false, true));
    assert(!X11ShouldRequestFullscreen(false, false, false, false));
}
''')

    def test_display_modes_fit_the_monitor_and_keep_its_top_left(self):
        source = self.source('AppKit/X11.backend/X11Display.m')
        fits = sources.function(source, 'X11ModeFitsScreen')
        frame = sources.function(source, 'X11FrameForModeOnScreen')
        screen = sources.function(source, 'X11ScreenFrameForMode')
        self.run_cpp(r'''
#include <cassert>
using BOOL=bool;
struct NSSize { double width,height; };
struct NSPoint { double x,y; };
struct NSRect { NSPoint origin; NSSize size; };
static NSSize NSMakeSize(double w,double h) {return {w,h};}
static NSRect NSMakeRect(double x,double y,double w,double h) {return {{x,y},{w,h}};}
static double NSWidth(NSRect r) {return r.size.width;}
static double NSHeight(NSRect r) {return r.size.height;}
static double NSMinX(NSRect r) {return r.origin.x;}
static double NSMaxY(NSRect r) {return r.origin.y+r.size.height;}
''' + fits + frame + screen + r'''
static bool same(NSRect a,NSRect b) {
    return a.origin.x==b.origin.x && a.origin.y==b.origin.y &&
           a.size.width==b.size.width && a.size.height==b.size.height;
}
int main() {
    NSRect primary=NSMakeRect(0,0,1920,1080);
    assert(X11ModeFitsScreen(NSMakeSize(1368,768),primary));
    assert(X11ModeFitsScreen(NSMakeSize(1024,768),primary));
    assert(X11ModeFitsScreen(NSMakeSize(1920,1080),primary));
    assert(!X11ModeFitsScreen(NSMakeSize(2560,1440),primary));
    assert(!X11ModeFitsScreen(NSMakeSize(0,768),primary));
    /* Cocoa frames grow upward: the monitor's top-left stays fixed. */
    assert(same(X11FrameForModeOnScreen(primary,NSMakeSize(1368,768)),
                NSMakeRect(0,312,1368,768)));
    assert(same(X11FrameForModeOnScreen(primary,NSMakeSize(1024,768)),
                NSMakeRect(0,312,1024,768)));
    assert(same(X11FrameForModeOnScreen(primary,NSMakeSize(1920,1080)),
                NSMakeRect(0,0,1920,1080)));
    /* An emulated mode changes the frame NSScreen reports; an impossible mode
     * leaves the physical frame untouched. */
    assert(same(X11ScreenFrameForMode(primary,NSMakeSize(1368,768)),
                NSMakeRect(0,312,1368,768)));
    assert(same(X11ScreenFrameForMode(primary,NSMakeSize(2560,1440)),primary));
    /* A monitor above the primary keeps its own top-left. */
    NSRect upper=NSMakeRect(1920,-68,1366,768);
    assert(!X11ModeFitsScreen(NSMakeSize(1368,768),upper));
    assert(same(X11FrameForModeOnScreen(upper,NSMakeSize(1024,768)),
                NSMakeRect(1920,-68,1024,768)));
    assert(same(X11ScreenFrameForMode(upper,NSMakeSize(1024,768)),
                NSMakeRect(1920,-68,1024,768)));
}
''')

    def test_fullscreen_method_uses_the_policy(self):
        source = self.source('AppKit/X11.backend/X11Window.m')
        method = body(source, '- (BOOL) wantsFullscreenWithStyleMask: (NSUInteger) mask rootFrame: (O2Rect) frame')
        self.assertIn('X11ShouldRequestFullscreen(', method)
        # Coverage must be the physical monitor: an emulated mode makes the
        # NSScreen frame smaller, and EWMH would pin the window back.
        self.assertIn('[display frameCoversPhysicalScreen: frame]', method)
        self.assertNotIn('[display screens]', method)
        self.assertNotIn('if (mask & NSWindowStyleMaskFullScreen) return YES;', method)

    def test_display_mode_request_emulates_the_screen_frame(self):
        source = self.source('AppKit/X11.backend/X11Display.m')
        method = body(source, '- (BOOL) setMode: (NSDictionary *) mode forScreen: (int) screenIndex')
        self.assertIn('X11ModeFitsScreen(requested, physicalFrame)', method)
        self.assertIn('X11ScreenFrameForMode(physicalFrame, requested)', method)
        self.assertIn('[_emulatedModes setObject: [NSValue valueWithSize: requested] forKey: displayKey]', method)
        self.assertIn('[_emulatedModes removeObjectForKey: displayKey]', method)
        self.assertIn('_lastScreens = nil', method)
        self.assertIn('[game setFrame: frame display: YES]', method)
        self.assertNotIn('TODO: Use XRRSetCrtcConfig', method)
        # The screen frame is what the game reads back to place its window, so
        # the emulated mode is recorded on any thread; only the window resize is
        # held back to the main thread.
        self.assertIn('@synchronized (self)', method)
        self.assertLess(method.index('[_emulatedModes setObject:'),
                        method.index('pthread_main_np()'))
        self.assertLess(method.index('pthread_main_np()'),
                        method.index('[game setFrame: frame display: YES]'))
        # The resize is a fallback: never touch a window the game has not mapped
        # and sized to the monitor yet, and never "resize" to the native mode.
        self.assertIn('NSEqualSizes(requested, physicalFrame.size)', method)
        self.assertIn('if (![window isVisible]', method)
        self.assertIn('NSEqualRects([window frame], physicalFrame)', method)

    def test_physical_coverage_ignores_the_emulated_mode(self):
        source = self.source('AppKit/X11.backend/X11Display.m')
        method = body(source, '- (BOOL) frameCoversPhysicalScreen: (NSRect) frame')
        self.assertIn('_physicalScreenFrames', method)
        self.assertIn('[self rootRectForFrame: physical]', method)
        self.assertNotIn('[screen frame]', method)

    def test_screens_apply_the_emulated_mode(self):
        source = self.source('AppKit/X11.backend/X11Display.m')
        method = body(source, '- (NSArray *) _screensLocked')
        self.assertIn('_physicalScreenFrames', method)
        self.assertIn('X11ScreenFrameForMode(physicalFrame, [emulated sizeValue])', method)
        self.assertIn('_physicalScreenFrames = physicalFrames', method)
        # The cache is read and rebuilt under the same lock the mode change uses.
        wrapper = body(source, '- (NSArray *) screens')
        self.assertIn('@synchronized (self)', wrapper)
        self.assertIn('[self _screensLocked]', wrapper)

    def test_display_mode_helper_is_declared_before_its_callers(self):
        source = self.source('AppKit/X11.backend/X11Display.m')
        declaration = ('static NSRect X11ScreenFrameForMode('
                       'NSRect physicalFrame, NSSize mode);')
        self.assertIn(declaration, source)
        # The screen cache calls it before the definition; without this
        # declaration the backend relies on an implicit function declaration.
        self.assertLess(source.index(declaration),
                        source.index('X11ScreenFrameForMode(physicalFrame'))

    def test_emulated_primary_keeps_coregraphics_bounds_at_the_origin(self):
        source = self.source('CoreGraphics/CGDirectDisplay.m')
        convert = sources.function(source, 'globalRectForFrameOnPrimary')
        self.run_cpp(r'''
#include <cassert>
struct NSPoint { double x,y; };
struct NSSize { double width,height; };
struct NSRect { NSPoint origin; NSSize size; };
typedef NSRect CGRect;
static NSRect NSMakeRect(double x,double y,double w,double h) {return {{x,y},{w,h}};}
static CGRect CGRectMake(double x,double y,double w,double h) {return {{x,y},{w,h}};}
static double NSMaxY(NSRect r) {return r.origin.y+r.size.height;}
static bool NSIsEmptyRect(NSRect r) {return r.size.width<=0||r.size.height<=0;}
''' + convert + r'''
int main() {
    /* A 1368x768 mode on a 1920x1080 primary reports (0,312,1368,768) in
     * Cocoa coordinates; its CoreGraphics bounds still start at the origin. */
    NSRect emulated=NSMakeRect(0,312,1368,768);
    CGRect bounds=globalRectForFrameOnPrimary(emulated,emulated);
    assert(bounds.origin.x==0 && bounds.origin.y==0);
    assert(bounds.size.width==1368 && bounds.size.height==768);
    /* A monitor beside the primary keeps its x and shares the top edge. */
    NSRect secondary=NSMakeRect(1920,0,1920,1080);
    CGRect beside=globalRectForFrameOnPrimary(emulated,secondary);
    assert(beside.origin.x==1920 && beside.origin.y==0);
    /* Without emulation the conversion is unchanged. */
    NSRect native=NSMakeRect(0,0,1920,1080);
    CGRect unchanged=globalRectForFrameOnPrimary(native,native);
    assert(unchanged.origin.x==0 && unchanged.origin.y==0);
    assert(unchanged.size.width==1920 && unchanged.size.height==1080);
    /* An empty frame is passed through. */
    CGRect none=globalRectForFrameOnPrimary(emulated,NSMakeRect(0,0,0,0));
    assert(none.size.width==0 && none.size.height==0);
}
''')
