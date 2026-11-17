"""Compile the shipped X11 occlusion rule and check when a window counts as hidden."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_runtime_compatibility as runtime

DISPLAY = 'AppKit/X11.backend/X11Display.m'

HARNESS = r'''
#include <assert.h>
typedef signed char BOOL;
typedef unsigned long Window;
#define YES 1
#define NO 0
// FUNCTIONS
int main(void) {
    const unsigned long stack[] = {10, 20, 30};
    long client = rclolStackIndex(stack, 3, 20), game = rclolStackIndex(stack, 3, 30);
    long below = rclolStackIndex(stack, 3, 10), missing = rclolStackIndex(stack, 3, 99);
    assert(client == 1 && game == 2 && below == 0 && missing == -1);

    /* A focused fullscreen game above the client and containing it hides it. */
    assert(rclolForeignWindowOccludes(320, 180, 1280, 720, 0, 0, 1920, 1080, YES, YES, client, game));
    /* Same rectangle still covers. */
    assert(rclolForeignWindowOccludes(0, 0, 1920, 1080, 0, 0, 1920, 1080, YES, YES, client, game));
    /* Below the client in the stacking order: a floating client stays visible. */
    assert(!rclolForeignWindowOccludes(320, 180, 1280, 720, 0, 0, 1920, 1080, YES, YES, client, below));
    /* Not fullscreen: a tiled focused window may be drawn under the client. */
    assert(!rclolForeignWindowOccludes(320, 180, 1280, 720, 0, 0, 1920, 1080, NO, YES, client, game));
    /* Translucent or ARGB cover: the client shows through. */
    assert(!rclolForeignWindowOccludes(320, 180, 1280, 720, 0, 0, 1920, 1080, YES, NO, client, game));
    /* Client on the second monitor, or only partly covered. */
    assert(!rclolForeignWindowOccludes(2240, 180, 1280, 720, 0, 0, 1920, 1080, YES, YES, client, game));
    assert(!rclolForeignWindowOccludes(1000, 500, 1280, 720, 0, 0, 1920, 1080, YES, YES, client, game));
    /* Without a stacking position for either window nothing is occluded. */
    assert(!rclolForeignWindowOccludes(320, 180, 1280, 720, 0, 0, 1920, 1080, YES, YES, missing, game));
    assert(!rclolForeignWindowOccludes(320, 180, 1280, 720, 0, 0, 1920, 1080, YES, YES, client, missing));
    /* Empty rectangles never count. */
    assert(!rclolForeignWindowOccludes(0, 0, 0, 0, 0, 0, 1920, 1080, YES, YES, client, game));
    return 0;
}
'''


class X11OcclusionChecks(unittest.TestCase):
    def test_occlusion_rule(self):
        if not shutil.which('cc'):
            self.skipTest('C compiler required')
        source = runtime.CompatibilityChecks().patched('cocotron', DISPLAY)
        functions = (runtime.function(source, 'rclolForeignWindowOccludes') +
                     runtime.function(source, 'rclolStackIndex'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'check.c').write_text(HARNESS.replace('// FUNCTIONS', functions))
            subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', '-o', str(path / 'check'),
                            str(path / 'check.c')], check=True)
            subprocess.run([str(path / 'check')], check=True)

    def test_occlusion_state_follows_platform(self):
        window = runtime.CompatibilityChecks().patched('cocotron', 'AppKit/NSWindow.m')
        body = window[window.index('- (NSUInteger) occlusionState {'):]
        body = body[:body.index('\n}\n')]
        self.assertIn('isOccluded', body)
        self.assertIn('if (!_isVisible)', body)
        display = runtime.CompatibilityChecks().patched('cocotron', DISPLAY)
        self.assertIn('RCLOL_X11_OCCLUSION', display)
        self.assertIn('NSWindowDidChangeOcclusionStateNotification', display)


if __name__ == '__main__':
    unittest.main()
