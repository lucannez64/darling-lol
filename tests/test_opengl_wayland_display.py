"""OpenGL.framework can present on a host Wayland display (RCLOL_WAYLAND=1).

The AppKit backend hands CGL a wl_display and, per window, a wl_egl_window.
These checks read patches/darling.patch (CI cannot run Darling): the Wayland
registration, the EGL_PLATFORM_WAYLAND display, the X11 paths that must keep
the Wayland display, and the present path that must not touch it.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = 'patches/darling.patch'
OPENGL_C = 'src/frameworks/OpenGL/OpenGL.c'
CGL_INTERNAL_H = 'src/frameworks/OpenGL/include/OpenGL/CGLInternal.h'


def _section(path):
    sections = (ROOT / PATCH).read_text().split('diff --git ')
    return next(s for s in sections if s.startswith('a/' + path + ' b/'))


def new_side(path):
    """`path` as the patch leaves it: context and added lines of its hunks."""
    return '\n'.join(line[1:] for line in _section(path).splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


def added(path):
    return '\n'.join(line[1:] for line in _section(path).splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class OpenGLWaylandDisplayTests(unittest.TestCase):
    def test_header_declares_the_wayland_entry_points(self):
        header = added(CGL_INTERNAL_H)
        self.assertIn('CGL_EXPORT CGLError CGLRegisterNativeWaylandDisplay(void *wl_display);', header)
        self.assertIn('CGL_EXPORT int CGLUsesWaylandDisplay(void);', header)

    def test_wayland_registration_uses_wayland_platform_and_gl_window_config(self):
        code = new_side(OPENGL_C)
        self.assertRegex(code, r'CGLError CGLRegisterNativeWaylandDisplay\(void \*wl_display\) \{')
        self.assertIn('p_eglGetPlatformDisplay(0x31D8 /* EGL_PLATFORM_WAYLAND_KHR */, wl_display, NULL)', code)
        self.assertIn('configIsWindowGL(disp, cfg)', code)
        self.assertIn('gl_window_attribute_list, &fallback', code)
        self.assertIn('p_eglBindAPI(EGL_OPENGL_API)', code)
        self.assertIn('darling-wayland: EGL on the Wayland display (vendor %s)', code)
        self.assertIn('darling-wayland: EGL Wayland display failed (0x%x)', code)

    def test_wayland_mode_is_set_only_after_success(self):
        code = new_side(OPENGL_C)
        bind = code.index('p_eglBindAPI(EGL_OPENGL_API))')
        assign = code.index('display = disp;')
        self.assertLess(bind, assign)
        self.assertLess(assign, code.index('wayland_mode = 1;'))

    def test_x11_registration_returns_early_in_wayland_mode(self):
        code = new_side(OPENGL_C)
        self.assertRegex(
            code,
            r'CGLError CGLRegisterNativeDisplay\(void \*native_display\) \{\s*'
            r'if \(wayland_mode\)\s*return kCGLNoError;\s*ensure_native_egl\(\);',
        )

    def test_default_and_per_connection_lookups_keep_the_wayland_display(self):
        code = new_side(OPENGL_C)
        self.assertRegex(code, r'static void ensureDefaultDisplay\(void\) \{\s*ensure_native_egl\(\);\s*if \(wayland_mode\)\s*return;')
        self.assertRegex(code, r'ensure_native_egl\(\);\s*/\* Wayland mode[^\n]*\n\s*if \(wayland_mode\)\s*return &wayland_cgl_display;')

    def test_alpha_window_ignores_visual_in_wayland_mode(self):
        code = new_side(OPENGL_C)
        self.assertRegex(
            code,
            r'CGLWindowRef CGLGetWindowWithAlpha\(void \*native_window, unsigned long visual_id\) \{\s*'
            r'if \(wayland_mode\)\s*return waylandWindowWithAlpha\(native_window\);',
        )
        helper = code[code.index('static CGLWindowRef waylandWindowWithAlpha'):]
        helper = helper[:helper.index('\n}\n') + 3]
        self.assertIn('EGL_ALPHA_SIZE, 8', helper)
        self.assertIn('p_eglCreateWindowSurface(display, config,', helper)  # default fallback

    def test_present_iosurface_path_is_off_in_wayland_mode(self):
        code = new_side(OPENGL_C)
        self.assertRegex(code, r'CGL_EXPORT int CGLPresentIOSurface\(IOSurfaceRef surface\) \{[\s\S]*?if\(wayland_mode \|\| !surface')

    def test_usable_wayland_mode_flag_is_exported(self):
        code = new_side(OPENGL_C)
        self.assertRegex(code, r'int CGLUsesWaylandDisplay\(void\) \{\s*return wayland_mode;')


if __name__ == '__main__':
    unittest.main()
