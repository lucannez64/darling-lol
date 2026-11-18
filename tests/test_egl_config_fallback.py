"""#253: CGL must find an X11 window config when EGL has no GLES1 configs.

The host harness compiles the shipped chooseDefaultConfig() against the host
libEGL and an X11 display, with eglChooseConfig wrapped to act like a Mesa
built with -Dgles1=disabled. It never starts Darling.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime

PATH = 'src/frameworks/OpenGL/OpenGL.c'


def source():
    return runtime.CompatibilityChecks().patched('darling', PATH)


class EGLConfigFallbackText(unittest.TestCase):
    def test_every_default_query_goes_through_the_fallback(self):
        text = source()
        chooser = runtime.function(text, 'chooseDefaultConfig')
        outside = text.replace(chooser, '')
        self.assertNotRegex(outside, r'p_eglChooseConfig\([^;]*\battribute_list\b')
        for caller in ('CGLRegisterNativeDisplay', 'ensureDefaultDisplay', 'getCGLDisplay'):
            self.assertIn('chooseDefaultConfig(', runtime.function(text, caller), caller)
        # The working path runs the original query first and returns its result.
        first = chooser.index('p_eglChooseConfig(disp, attribute_list, out, 1, count)')
        self.assertLess(first, chooser.index('gl_window_attribute_list'))
        fallback = text[text.index('gl_window_attribute_list[] = {'):]
        fallback = fallback[:fallback.index('};')]
        self.assertIn('EGL_SURFACE_TYPE, EGL_WINDOW_BIT', fallback)
        self.assertIn('EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT', fallback)
        for symbol in ('eglGetConfigs', 'eglQueryString', 'eglGetError'):
            self.assertIn('dlsym(h, "%s")' % symbol, text)

    def test_failure_line_names_what_steers_egl(self):
        log = runtime.function(source(), 'logNoEGLConfig')
        for needle in ('EGL_VENDOR', 'EGL_VERSION', 'EGL_CLIENT_APIS', 'eglGetConfigs=',
                       'EGL_PLATFORM', '__EGL_VENDOR_LIBRARY_FILENAMES', 'LIBGL_ALWAYS_SOFTWARE',
                       'MESA_LOADER_DRIVER_OVERRIDE', 'DISPLAY'):
            self.assertIn(needle, log)


HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>

static EGLBoolean (*p_eglChooseConfig)(EGLDisplay, const EGLint *, EGLConfig *, EGLint, EGLint *);
static EGLBoolean (*p_eglGetConfigAttrib)(EGLDisplay, EGLConfig, EGLint, EGLint *);
static EGLBoolean (*p_eglGetConfigs)(EGLDisplay, EGLConfig *, EGLint, EGLint *);
static const char *(*p_eglQueryString)(EGLDisplay, EGLint);
static EGLint (*p_eglGetError)(void);

/* mode 0: host EGL as is; 1: no GLES1 configs (default RENDERABLE_TYPE
 * matches nothing); 2: no configs match at all. */
static int mode;
static EGLBoolean chooseShim(EGLDisplay d, const EGLint *attrs, EGLConfig *out, EGLint size, EGLint *count) {
    int renderable = 0;
    for (const EGLint *a = attrs; a && *a != EGL_NONE; a += 2)
        renderable |= a[0] == EGL_RENDERABLE_TYPE;
    if (mode == 2 || (mode == 1 && !renderable)) { *count = 0; return EGL_TRUE; }
    return eglChooseConfig(d, attrs, out, size, count);
}

ATTRIBUTE_LIST
SHIPPED

int main(int argc, char **argv) {
    mode = atoi(argv[1]);
    p_eglChooseConfig = chooseShim;
    p_eglGetConfigAttrib = eglGetConfigAttrib;
    p_eglGetConfigs = eglGetConfigs;
    p_eglQueryString = eglQueryString;
    p_eglGetError = eglGetError;
    Display *x = XOpenDisplay(NULL);
    if (!x) return 77;
    /* Same display call as x11EGLDisplay(). */
    EGLDisplay d = eglGetPlatformDisplay(0x31D5, x, NULL);
    if (d == EGL_NO_DISPLAY || !eglInitialize(d, NULL, NULL)) return 77;

    EGLConfig cfg = NULL; EGLint n = 0;
    EGLBoolean ok = chooseDefaultConfig(d, &cfg, &n, "harness");
    if (mode == 2) { printf("result ok=%d n=%d\n", ok, n); return n == 0 ? 0 : 1; }
    if (!ok || n < 1 || !cfg) { printf("no config\n"); return 1; }
    if (mode == 0) {
        EGLConfig direct = NULL; EGLint dn = 0;
        eglChooseConfig(d, attribute_list, &direct, 1, &dn);
        if (direct != cfg) { printf("success path changed\n"); return 1; }
    }
    /* What cocotron does with it: CGLGetWindow + CGLCreateContext + attach. */
    EGLint visual = 0;
    eglGetConfigAttrib(d, cfg, EGL_NATIVE_VISUAL_ID, &visual);
    XVisualInfo tmpl; memset(&tmpl, 0, sizeof tmpl); tmpl.visualid = visual;
    int nv = 0; XVisualInfo *vi = XGetVisualInfo(x, VisualIDMask, &tmpl, &nv);
    if (!vi) { printf("config has no X visual\n"); return 1; }
    XSetWindowAttributes swa; memset(&swa, 0, sizeof swa);
    swa.colormap = XCreateColormap(x, DefaultRootWindow(x), vi->visual, AllocNone);
    Window w = XCreateWindow(x, DefaultRootWindow(x), 0, 0, 32, 32, 0, vi->depth, InputOutput,
                             vi->visual, CWColormap | CWBorderPixel, &swa);
    EGLSurface s = eglCreateWindowSurface(d, cfg, (EGLNativeWindowType) w, NULL);
    if (s == EGL_NO_SURFACE) { printf("surface failed 0x%x\n", eglGetError()); return 1; }
    if (!eglBindAPI(EGL_OPENGL_API)) return 1;
    EGLContext c = eglCreateContext(d, cfg, EGL_NO_CONTEXT, NULL);
    if (c == EGL_NO_CONTEXT) { printf("context failed 0x%x\n", eglGetError()); return 1; }
    if (!eglMakeCurrent(d, s, s, c) || !eglSwapBuffers(d, s)) { printf("current/swap failed\n"); return 1; }
    eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(d, c); eglDestroySurface(d, s);
    XDestroyWindow(x, w); XFree(vi);
    eglTerminate(d); XCloseDisplay(x);
    printf("window ok\n");
    return 0;
}
'''


@unittest.skipUnless(shutil.which('cc') and shutil.which('pkg-config'), 'C compiler and pkg-config required')
class EGLConfigFallbackHost(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get('DISPLAY'):
            raise unittest.SkipTest('X11 display required')
        if subprocess.run(['pkg-config', '--exists', 'egl', 'x11']).returncode:
            raise unittest.SkipTest('EGL and X11 development files required')
        text = source()
        attribute_list = re.search(r'static EGLint const attribute_list\[\] = \{.*?\};', text, re.S).group(0)
        start = text.index('static EGLint const gl_window_attribute_list[]')
        shipped = text[start:text.index('};', start) + 2] + '\n'
        for name in ('eglEnvValue', 'logNoEGLConfig', 'chooseDefaultConfig'):
            shipped += runtime.function(text, name)
        cls.folder = tempfile.TemporaryDirectory()
        c_file = Path(cls.folder.name) / 'egl_fallback.c'
        c_file.write_text(HARNESS.replace('ATTRIBUTE_LIST', attribute_list).replace('SHIPPED', shipped))
        cls.binary = Path(cls.folder.name) / 'egl_fallback'
        flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'egl', 'x11'], text=True).split()
        subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', str(c_file), '-o', str(cls.binary)] + flags,
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def run_mode(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=60)
        if result.returncode == 77:
            self.skipTest('host EGL X11 display unavailable')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_working_egl_keeps_the_original_config_silently(self):
        result = self.run_mode(0)
        self.assertIn('window ok', result.stdout)
        self.assertNotIn('darling-lol EGL', result.stderr)

    def test_egl_without_gles1_gets_a_window_config(self):
        result = self.run_mode(1)
        self.assertIn('window ok', result.stdout)
        self.assertEqual(result.stderr.count('using an OpenGL window config'), 1, result.stderr)

    def test_no_config_logs_one_diagnostic_line(self):
        result = self.run_mode(2)
        lines = [l for l in result.stderr.splitlines() if 'darling-lol EGL: no usable config' in l]
        self.assertEqual(len(lines), 1, result.stderr)
        for needle in ('EGL_VENDOR="', 'EGL_VERSION="', 'EGL_CLIENT_APIS="', 'eglGetConfigs=',
                       'window+opengl=', 'EGL_PLATFORM='):
            self.assertIn(needle, lines[0])
        self.assertNotRegex(lines[0], r'eglGetConfigs=(-1|0) ')


if __name__ == '__main__':
    unittest.main()
