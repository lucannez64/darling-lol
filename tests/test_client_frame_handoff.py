"""Host regression tests for post-game League client frame presentation handoff.

Covers three interacting bugs that left LeagueClientUx stuck on the loading
spinner after returning from a match:
1. DarlingXShmStart in tools/m58f299-xshm.c publishing the child XID before
   XSync / worker creation or leaving stale magic while reinitializing.
2. CGLPresentIOSurface in patches/darling.patch latching native_present_window
   when EGL surface/context creation failed, preventing retries.
3. RemoteFrameSelect in patches/cocotron.patch staying pinned forever to a
   software-tagged publisher slot (e.g. a renderer that wrote a window-sized
   IOSurface at startup and moved on to smaller tiles) when a fresher GPU
   readback slot exists for the window.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _patch_section(patch_rel, file_path):
    text = (ROOT / patch_rel).read_text()
    marker = 'diff --git a/' + file_path + ' b/' + file_path + '\n'
    return text.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def _added_lines(patch_rel, file_path):
    section = _patch_section(patch_rel, file_path)
    return '\n'.join(
        line[1:] for line in section.splitlines()
        if line.startswith('+') and not line.startswith('+++')
    ) + '\n'


def _new_side_lines(patch_rel, file_path):
    section = _patch_section(patch_rel, file_path)
    return '\n'.join(
        line[1:] for line in section.splitlines()
        if line[:1] in (' ', '+') and not line.startswith('+++')
    ) + '\n'


def _is_clang(compiler):
    version = subprocess.run([compiler, '--version'], capture_output=True, text=True)
    return 'clang' in version.stdout.lower()


REMOTE_FRAME_HARNESS = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "RemoteFrameSlot.h"

static void write_slot(const char *shm_dir, int pid, uint32_t width, uint32_t height,
                       uint32_t seq, int software, int visible_pixels, time_t mtime_sec) {
    char path[256];
    snprintf(path, sizeof(path), "%s/darling-ca-frame-%u-%d", shm_dir, (unsigned)getuid(), pid);
    int fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0600);
    assert(fd >= 0);

    uint32_t stride = width * 4;
    uint64_t data_size = (uint64_t)stride * height;
    size_t total = sizeof(DarlingCARemoteFrame) + 64u * 1024u * 1024u;
    assert(ftruncate(fd, (off_t)total) == 0);

    DarlingCARemoteFrame head;
    memset(&head, 0, sizeof(head));
    head.magic = DARLING_CA_FRAME_MAGIC;
    head.sequence = seq & ~1u;
    head.width = width;
    head.height = height;
    head.bytesPerRow = stride;
    head.pixelFormat = 0x42475241u | (software ? 0x80000000u : 0u);
    head.dataSize = data_size;
    assert(pwrite(fd, &head, sizeof(head), 0) == (ssize_t)sizeof(head));

    if (visible_pixels) {
        uint8_t row[4096];
        size_t chunk = stride < sizeof(row) ? stride : sizeof(row);
        memset(row, 0x7f, chunk);
        assert(pwrite(fd, row, chunk, (off_t)sizeof(head)) == (ssize_t)chunk);
    }

    struct timespec times[2] = {
        { .tv_sec = mtime_sec, .tv_nsec = 0 },
        { .tv_sec = mtime_sec, .tv_nsec = 0 },
    };
    assert(futimens(fd, times) == 0);
    close(fd);
}

static void slot_path(const char *shm_dir, int pid, char *out, size_t out_size) {
    snprintf(out, out_size, "%s/darling-ca-frame-%u-%d", shm_dir, (unsigned)getuid(), pid);
}

static void remove_slots(const char *shm_dir, int sw_pid, int gpu_pid) {
    char path[256];
    slot_path(shm_dir, sw_pid, path, sizeof(path));
    unlink(path);
    slot_path(shm_dir, gpu_pid, path, sizeof(path));
    unlink(path);
}

int main(void) {
    const char *shm_dir = TEST_SHM_DIR;
    int sw_pid = (int)getpid();
    int gpu_pid = sw_pid + 100000;
    char sw_path[256], gpu_path[256], chosen[256];
    slot_path(shm_dir, sw_pid, sw_path, sizeof(sw_path));
    slot_path(shm_dir, gpu_pid, gpu_path, sizeof(gpu_path));

    RemoteFrameTarget target1280 = { 1280, 720 };
    RemoteFrameTarget target1600 = { 1600, 900 };
    DarlingCARemoteFrame header;
    RemoteFrameSlot slot;

    /* 1. Startup renderer hijack -> GPU handoff without resize. */
    memset(&slot, 0, sizeof(slot));
    remove_slots(shm_dir, sw_pid, gpu_pid);
    write_slot(shm_dir, sw_pid, 1280, 720, 2, 1, 1, 100);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.0, 0, 0, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));
    assert(slot.softwarePublisher == YES);

    /* Software slot moves on to 262x131 tiles while GPU renders static Home UI at 1280x720. */
    write_slot(shm_dir, sw_pid, 262, 131, 4, 1, 1, 102);
    write_slot(shm_dir, gpu_pid, 1280, 720, 2, 0, 1, 101);

    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.25, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.50, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    /* Static GPU frame (mtime unchanged at 101) takes over once 0.5s elapses. */
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.75, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, gpu_path) == 0);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));
    assert(slot.softwarePublisher == NO);
    RemoteFrameRelease(&slot);
    assert(slot.mapping == NULL && slot.mappingPath[0] == '\0');
    assert(!slot.softwarePublisher && !slot.softwareMismatchActive);
    assert(slot.softwareMatchedMtime == 0 && slot.softwareMismatchSince == 0.0);

    /* 2. Software slot won on black top rows (spinner), then GPU renders visible Home screen. */
    memset(&slot, 0, sizeof(slot));
    remove_slots(shm_dir, sw_pid, gpu_pid);
    write_slot(shm_dir, sw_pid, 1280, 720, 2, 1, 0, 100);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.0, 0, 0, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));
    assert(slot.softwarePublisher == YES);

    write_slot(shm_dir, gpu_pid, 1280, 720, 2, 0, 1, 101);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.25, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.50, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.75, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, gpu_path) == 0);
    RemoteFrameRelease(&slot);

    /* 3. Genuine software compositor with brief popup (< 0.5s) resets mismatch timer. */
    memset(&slot, 0, sizeof(slot));
    remove_slots(shm_dir, sw_pid, gpu_pid);
    write_slot(shm_dir, sw_pid, 1280, 720, 2, 1, 1, 100);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 20.0, 0, 0, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));

    write_slot(shm_dir, sw_pid, 262, 131, 4, 1, 1, 101);
    write_slot(shm_dir, gpu_pid, 1280, 720, 2, 0, 1, 101);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 20.25, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(slot.softwareMismatchActive == YES);

    write_slot(shm_dir, sw_pid, 1280, 720, 6, 1, 1, 102);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 20.50, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(slot.softwareMismatchActive == NO);

    /* A second brief popup gets a full fresh 0.5s window. */
    write_slot(shm_dir, sw_pid, 262, 131, 8, 1, 1, 103);
    write_slot(shm_dir, gpu_pid, 1280, 720, 4, 0, 1, 103);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 20.75, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 21.00, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    RemoteFrameRelease(&slot);

    /* 4. Long popup + stale GPU slot (mtime <= softwareMatchedMtime) stays on software owner. */
    memset(&slot, 0, sizeof(slot));
    remove_slots(shm_dir, sw_pid, gpu_pid);
    write_slot(shm_dir, gpu_pid, 1280, 720, 2, 0, 1, 50);
    write_slot(shm_dir, sw_pid, 1280, 720, 2, 1, 1, 100);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.0, 0, 0, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));

    write_slot(shm_dir, sw_pid, 262, 131, 4, 1, 1, 101);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.25, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.75, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 20.00, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    RemoteFrameRelease(&slot);

    /* 5. Software owner + newer all-black GPU slot stays on software owner indefinitely. */
    memset(&slot, 0, sizeof(slot));
    remove_slots(shm_dir, sw_pid, gpu_pid);
    write_slot(shm_dir, sw_pid, 1280, 720, 2, 1, 1, 100);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.0, 0, 0, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));

    write_slot(shm_dir, sw_pid, 262, 131, 4, 1, 1, 101);
    write_slot(shm_dir, gpu_pid, 1280, 720, 2, 0, 0, 101);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.25, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 20.00, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, sw_path) == 0);
    RemoteFrameRelease(&slot);

    /* 6. Window resize (1280x720 -> 1600x900) hands off immediately with zero grace delay. */
    memset(&slot, 0, sizeof(slot));
    remove_slots(shm_dir, sw_pid, gpu_pid);
    write_slot(shm_dir, sw_pid, 1280, 720, 2, 1, 1, 100);
    assert(RemoteFrameSelect(&slot, &target1280, 1, 10.0, 0, 0, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(RemoteFrameMap(&slot, chosen));
    assert(RemoteFrameReadHeader(&slot, &header));

    write_slot(shm_dir, sw_pid, 262, 131, 4, 1, 1, 101);
    write_slot(shm_dir, gpu_pid, 1600, 900, 2, 0, 1, 50);
    assert(RemoteFrameSelect(&slot, &target1600, 1, 10.25, 1280, 720, chosen, sizeof(chosen)) == RemoteFrameSelectChosen);
    assert(strcmp(chosen, gpu_path) == 0);
    RemoteFrameRelease(&slot);
    remove_slots(shm_dir, sw_pid, gpu_pid);
    return 0;
}
'''


CGL_PRESENT_HARNESS_PREFIX = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "DarlingNativePresent.h"

#define CGL_EXPORT
typedef void *IOSurfaceRef;
typedef uint32_t IOSurfaceID;
typedef struct _CGLContextObj *CGLContextObj;
typedef int GLint;
typedef unsigned int GLuint;
typedef unsigned int GLenum;
typedef int GLsizei;
typedef struct __GLsync *GLsync;
typedef void *EGLDisplay;
typedef void *EGLConfig;
typedef void *EGLSurface;
typedef void *EGLContext;
typedef uintptr_t EGLNativeWindowType;
typedef int EGLBoolean;
typedef int32_t EGLint;

#define EGL_TRUE 1
#define EGL_FALSE 0
#define EGL_NO_SURFACE ((EGLSurface)0)
#define EGL_NO_CONTEXT ((EGLContext)0)
#define EGL_SURFACE_TYPE 0x3033
#define EGL_WINDOW_BIT 0x0004
#define EGL_RENDERABLE_TYPE 0x3040
#define EGL_OPENGL_BIT 0x0008
#define EGL_NATIVE_VISUAL_ID 0x302E
#define EGL_WIDTH 0x3057
#define EGL_HEIGHT 0x3056
#define EGL_NONE 0x3038

#define GL_SYNC_GPU_COMMANDS_COMPLETE 0x9117
#define GL_TIMEOUT_IGNORED 0xFFFFFFFFFFFFFFFFull
#define GL_READ_FRAMEBUFFER 0x8CA8
#define GL_DRAW_FRAMEBUFFER 0x8CA9
#define GL_COLOR_ATTACHMENT0 0x8CE0
#define GL_FRAMEBUFFER_COMPLETE 0x8CD5
#define GL_BACK 0x0405
#define GL_FRONT 0x0404
#define GL_SCISSOR_TEST 0x0C11
#define GL_FRAMEBUFFER_SRGB 0x8DB9
#define GL_COLOR_BUFFER_BIT 0x00004000
#define GL_NEAREST 0x2600
#define GL_RGBA 0x1908
#define GL_UNSIGNED_BYTE 0x1401
#define GL_NO_ERROR 0

struct _CGLContextObj {
    GLuint retain_count;
    pthread_mutex_t lock;
    EGLContext egl_context;
    EGLDisplay egl_display;
    EGLConfig egl_config;
    EGLSurface egl_surface;
    int swap_interval;
    int swap_interval_requested;
    EGLSurface native_present_surface;
    EGLContext native_present_context;
    DarlingNativePresentTarget *native_present_target;
    uint32_t native_present_window;
    uint32_t native_present_failed_window;
    uint64_t native_present_retry_ns;
    uint64_t native_next_present_ns;
    struct DarlingAsyncPresent *native_async;
};

typedef struct {
    IOSurfaceID surfaceID;
    CGLContextObj context;
    GLenum target;
    GLenum format;
    GLenum type;
    GLuint texture;
    GLuint plane;
    IOSurfaceRef surface;
} CGLIOSurfaceBinding;

static int wayland_mode = 0;
static CGLIOSurfaceBinding ioSurfaceBindings[4];
static unsigned ioSurfaceBindingCount = 0;
static pthread_mutex_t ioSurfaceBindingMutex = PTHREAD_MUTEX_INITIALIZER;
static CGLContextObj mock_current_ctx = NULL;
static uint64_t mock_now_ns = 1000000000ull;

static size_t IOSurfaceGetPlaneCount(IOSurfaceRef s) { (void)s; return 1; }
static size_t IOSurfaceGetBytesPerElement(IOSurfaceRef s) { (void)s; return 4; }
static size_t IOSurfaceGetWidth(IOSurfaceRef s) { (void)s; return 1280; }
static size_t IOSurfaceGetHeight(IOSurfaceRef s) { (void)s; return 720; }
static IOSurfaceID IOSurfaceGetID(IOSurfaceRef s) { (void)s; return 1; }
static CGLContextObj CGLGetCurrentContext(void) { return mock_current_ctx; }
static CGLContextObj CGLRetainContext(CGLContextObj c) { if (c) ++c->retain_count; return c; }
static void CGLReleaseContext(CGLContextObj c) { if (c) --c->retain_count; }
static uint64_t nativeMonotonicNS(void) { return mock_now_ns; }
static uint64_t nativeRefreshIntervalNS(const DarlingNativePresentTarget *target) { return DarlingNativeNominalInterval(target); }
static long __lolsys_syscall(long a, long b, long c, long d, long e, long f, int nr) {
    (void)a; (void)c; (void)d; (void)e; (void)f;
    if (nr == 228 && b) memset((void *)(uintptr_t)b, 0, sizeof(struct timespec));
    return 0;
}

static int mock_fail_surface = 0;
static int mock_fail_context = 0;
static int mock_create_surface_calls = 0;
static int mock_destroy_surface_calls = 0;
static int mock_create_context_calls = 0;
static int mock_destroy_context_calls = 0;
static int mock_enqueue_calls = 0;
static int mock_stop_calls = 0;
static EGLNativeWindowType mock_last_window = 0;
static uintptr_t mock_next_handle = 0x100;

static void stopAsyncPresent(CGLContextObj ctx) { (void)ctx; ++mock_stop_calls; }
static int enqueueAsyncPresent(CGLContextObj ctx, GLuint tex, GLenum target, unsigned w, unsigned h) {
    (void)ctx; (void)tex; (void)target; (void)w; (void)h;
    ++mock_enqueue_calls;
    return 1;
}

static EGLBoolean p_eglChooseConfig(EGLDisplay d, const EGLint *a, EGLConfig *cfgs, EGLint size, EGLint *cnt) {
    (void)d; (void)a; (void)size;
    if (!cfgs) { *cnt = 1; return EGL_TRUE; }
    cfgs[0] = (EGLConfig)(uintptr_t)0xCAFE;
    *cnt = 1;
    return EGL_TRUE;
}
static EGLBoolean mock_eglGetConfigAttrib(EGLDisplay d, EGLConfig cfg, EGLint attr, EGLint *val) {
    (void)d; (void)cfg;
    if (attr == EGL_NATIVE_VISUAL_ID) *val = 0x70;
    return EGL_TRUE;
}
static EGLBoolean (*p_eglGetConfigAttrib)(EGLDisplay, EGLConfig, EGLint, EGLint *) = mock_eglGetConfigAttrib;
static EGLSurface p_eglCreateWindowSurface(EGLDisplay d, EGLConfig cfg, EGLNativeWindowType win, const EGLint *a) {
    (void)d; (void)cfg; (void)a;
    ++mock_create_surface_calls;
    mock_last_window = win;
    if (mock_fail_surface) return EGL_NO_SURFACE;
    return (EGLSurface)(mock_next_handle += 0x10);
}
static EGLBoolean p_eglDestroySurface(EGLDisplay d, EGLSurface s) {
    (void)d; (void)s;
    ++mock_destroy_surface_calls;
    return EGL_TRUE;
}
static EGLContext p_eglCreateContext(EGLDisplay d, EGLConfig cfg, EGLContext share, const EGLint *a) {
    (void)d; (void)cfg; (void)share; (void)a;
    ++mock_create_context_calls;
    if (mock_fail_context) return EGL_NO_CONTEXT;
    return (EGLContext)(mock_next_handle += 0x10);
}
static EGLBoolean p_eglDestroyContext(EGLDisplay d, EGLContext c) {
    (void)d; (void)c;
    ++mock_destroy_context_calls;
    return EGL_TRUE;
}
static EGLBoolean p_eglQuerySurface(EGLDisplay d, EGLSurface s, EGLint attr, EGLint *val) {
    (void)d; (void)s;
    *val = (attr == EGL_WIDTH) ? 1280 : 720;
    return EGL_TRUE;
}
static EGLBoolean p_eglMakeCurrent(EGLDisplay d, EGLSurface dr, EGLSurface rd, EGLContext c) {
    (void)d; (void)dr; (void)rd; (void)c; return EGL_TRUE;
}
static EGLBoolean p_eglSwapInterval(EGLDisplay d, EGLint v) { (void)d; (void)v; return EGL_TRUE; }
static EGLBoolean p_eglSwapBuffers(EGLDisplay d, EGLSurface s) { (void)d; (void)s; return EGL_TRUE; }
static GLsync glFenceSync(GLenum cond, GLuint flags) { (void)cond; (void)flags; return NULL; }
static void glWaitSync(GLsync s, GLuint flags, uint64_t timeout) { (void)s; (void)flags; (void)timeout; }
static void glDeleteSync(GLsync s) { (void)s; }
static void glFlush(void) {}
static void glGenFramebuffers(GLsizei n, GLuint *ids) { (void)n; *ids = 1; }
static void glDeleteFramebuffers(GLsizei n, const GLuint *ids) { (void)n; (void)ids; }
static void glBindFramebuffer(GLenum t, GLuint id) { (void)t; (void)id; }
static void glFramebufferTexture2D(GLenum t, GLenum a, GLenum tt, GLuint tex, GLint lvl) {
    (void)t; (void)a; (void)tt; (void)tex; (void)lvl;
}
static GLenum glCheckFramebufferStatus(GLenum t) { (void)t; return GL_FRAMEBUFFER_COMPLETE; }
static void glDrawBuffer(GLenum m) { (void)m; }
static void glReadBuffer(GLenum m) { (void)m; }
static void glDisable(GLenum c) { (void)c; }
static void glBlitFramebuffer(GLint sx0, GLint sy0, GLint sx1, GLint sy1,
                              GLint dx0, GLint dy0, GLint dx1, GLint dy1, GLbitfield m, GLenum f) {
    (void)sx0; (void)sy0; (void)sx1; (void)sy1; (void)dx0; (void)dy0; (void)dx1; (void)dy1; (void)m; (void)f;
}
static void glReadPixels(GLint x, GLint y, GLsizei w, GLsizei h, GLenum f, GLenum t, void *p) {
    (void)x; (void)y; (void)w; (void)h; (void)f; (void)t; (void)p;
}
static GLenum glGetError(void) { return GL_NO_ERROR; }
'''

CGL_PRESENT_HARNESS_MAIN = r'''
int main(void) {
    assert(setenv("RCLOL_GPU_DIRECT_PRESENT", "1", 1) == 0);
    assert(setenv("RCLOL_GPU_DIRECT_ASYNC", "1", 1) == 0);

    DarlingNativePresentTarget target;
    memset(&target, 0, sizeof(target));
    target.magic = DARLING_NATIVE_PRESENT_MAGIC;
    target.target = 0x5600004u;
    target.width = 1280;
    target.height = 720;
    target.ready = 2;
    target.visual = 0x70;

    struct _CGLContextObj ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.retain_count = 1;
    ctx.egl_display = (EGLDisplay)(uintptr_t)0x1;
    ctx.egl_context = (EGLContext)(uintptr_t)0x2;
    ctx.native_present_target = &target;

    mock_current_ctx = &ctx;
    ioSurfaceBindings[0] = (CGLIOSurfaceBinding){ 1, &ctx, 0x0DE1, 0x1908, 0x1401, 42, 0, (IOSurfaceRef)(uintptr_t)0x3 };
    ioSurfaceBindingCount = 1;

    /* 1. Transient eglCreateWindowSurface failure does not latch window and throttles retries for 250ms. */
    mock_fail_surface = 1;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 0);
    assert(mock_create_surface_calls == 1);
    assert(ctx.native_present_window == 0);
    assert(ctx.native_present_failed_window == 0x5600004u);

    /* 2. 10ms later on the same failed window: throttled (no EGL call). */
    mock_now_ns += 10000000ull;
    mock_fail_surface = 0;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 0);
    assert(mock_create_surface_calls == 1);

    /* 3. 250ms+ after failure: retries, succeeds, latches window, clears failure state. */
    mock_now_ns += 250000000ull;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 1);
    assert(mock_create_surface_calls == 2);
    assert(ctx.native_present_window == 0x5600004u);
    assert(ctx.native_present_failed_window == 0);
    assert(ctx.native_present_retry_ns == 0);
    assert(mock_enqueue_calls == 1);

    /* 4. Failure on stale windowOld followed 1ms later by windowNew retries windowNew immediately. */
    target.target = 0x5600005u;
    mock_fail_surface = 1;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 0);
    assert(ctx.native_present_window == 0);
    assert(ctx.native_present_failed_window == 0x5600005u);
    assert(mock_create_surface_calls == 3);

    mock_now_ns += 1000000ull;
    target.target = 0x5600006u;
    mock_fail_surface = 0;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 1);
    assert(mock_create_surface_calls == 4);
    assert(mock_last_window == 0x5600006u);
    assert(ctx.native_present_window == 0x5600006u);
    assert(mock_enqueue_calls == 2);

    /* 5. eglCreateContext failure destroys the created surface (plus prior window's surface), does not latch, and retries after backoff. */
    int destroys_before = mock_destroy_surface_calls;
    target.target = 0x5600007u;
    mock_fail_context = 1;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 0);
    int ctx_calls_after_fail = mock_create_context_calls;
    assert(mock_destroy_surface_calls == destroys_before + 2);
    assert(ctx.native_present_surface == EGL_NO_SURFACE);
    assert(ctx.native_present_context == EGL_NO_CONTEXT);
    assert(ctx.native_present_window == 0);
    assert(ctx.native_present_failed_window == 0x5600007u);

    mock_now_ns += 10000000ull;
    mock_fail_context = 0;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 0);
    assert(mock_create_context_calls == ctx_calls_after_fail);

    mock_now_ns += 250000000ull;
    assert(CGLPresentIOSurface((IOSurfaceRef)(uintptr_t)0x3) == 1);
    assert(ctx.native_present_window == 0x5600007u);
    assert(mock_enqueue_calls == 3);
    assert(ctx.retain_count == 1);
    return 0;
}
'''


class ClientFrameHandoffTests(unittest.TestCase):
    def test_remote_frame_slot_releases_stale_software_lockin_without_breaking_popups(self):
        compiler = shutil.which('cc') or shutil.which('clang')
        if not compiler:
            self.skipTest('C compiler required')
        header = _added_lines('patches/cocotron.patch', 'AppKit/X11.backend/RemoteFrameSlot.h')
        source = _added_lines('patches/cocotron.patch', 'AppKit/X11.backend/RemoteFrameSlot.m')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm_dir = root / 'shm'
            shm_dir.mkdir()
            (root / 'Foundation').mkdir()
            (root / 'Foundation/Foundation.h').write_text(
                '#ifndef SHIM_FOUNDATION_H\n'
                '#define SHIM_FOUNDATION_H\n'
                'typedef int BOOL;\n'
                '#define YES 1\n'
                '#define NO 0\n'
                '#define st_mtimespec st_mtim\n'
                '#endif\n'
            )
            (root / 'RemoteFrameSlot.h').write_text(header)
            patched_source = source.replace('"/dev/shm"', 'TEST_SHM_DIR').replace('"/dev/shm/', 'TEST_SHM_DIR "/')
            (root / 'RemoteFrameSlot.c').write_text(patched_source)
            (root / 'harness.c').write_text(REMOTE_FRAME_HARNESS)
            binary = root / 'remote-frame-check'
            warn_flags = ['-Wno-deprecated']
            if not _is_clang(compiler):
                warn_flags.append('-Wno-format-truncation')
            compile_res = subprocess.run(
                [
                    compiler, '-x', 'c', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                    *warn_flags, '-DTEST_SHM_DIR="' + str(shm_dir) + '"',
                    '-I', str(root),
                    str(root / 'RemoteFrameSlot.c'), str(root / 'harness.c'),
                    '-o', str(binary),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(compile_res.returncode, 0, compile_res.stderr)
            run_res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run_res.returncode, 0, run_res.stderr)

    def test_cgl_present_does_not_latch_failed_target_window(self):
        compiler = shutil.which('cc') or shutil.which('clang')
        if not compiler:
            self.skipTest('C compiler required')
        open_gl = _added_lines('patches/darling.patch', 'src/frameworks/OpenGL/OpenGL.c')
        start = open_gl.index('CGL_EXPORT int CGLPresentIOSurface(IOSurfaceRef surface) {')
        end = open_gl.index('\n}\n', start) + 3
        cgl_present = open_gl[start:end]

        darling_context = _new_side_lines('patches/darling.patch', 'src/frameworks/OpenGL/OpenGL.c')
        self.assertIn('uint32_t native_present_failed_window;', darling_context)
        self.assertIn('uint64_t native_present_retry_ns;', darling_context)
        self.assertIn('context->native_present_failed_window=0;', darling_context)
        self.assertIn('context->native_present_retry_ns=0;', darling_context)

        present_header = _added_lines(
            'patches/darling.patch',
            'src/frameworks/OpenGL/include/OpenGL/DarlingNativePresent.h',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'DarlingNativePresent.h').write_text(present_header)
            test_c = root / 'cgl_present_check.c'
            # GLbitfield is needed by the mock glBlitFramebuffer signature before CGLPresentIOSurface.
            prefix = CGL_PRESENT_HARNESS_PREFIX.replace('typedef int GLsizei;', 'typedef int GLsizei;\ntypedef unsigned int GLbitfield;')
            test_c.write_text(prefix + '\n' + cgl_present + '\n' + CGL_PRESENT_HARNESS_MAIN)
            binary = root / 'cgl-present-check'
            warn_flags = ['-Wno-unused-function', '-Wno-sign-compare']
            if not _is_clang(compiler):
                warn_flags.append('-Wno-misleading-indentation')
            compile_res = subprocess.run(
                [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                 *warn_flags, '-I', str(root), str(test_c), '-o', str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(compile_res.returncode, 0, compile_res.stderr)
            run_res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run_res.returncode, 0, run_res.stderr)

    def test_xshm_syncs_and_starts_worker_before_publishing_gpu_target_magic(self):
        text = (ROOT / 'tools/m58f299-xshm.c').read_text()
        start = text.index('DarlingXShmStart(')
        end = text.index('DarlingXShmDestroy(', start)
        body = text[start:end]

        xcreate = body.index('XCreateWindow(')
        xshape = body.index('XShapeCombineRectangles(')
        xsync = body.index('XSync(f->display, False);')
        open_target = body.index('open(f->gpuTargetPath,')
        clear_magic = body.index('__atomic_store_n(&f->gpuTarget->magic, 0, __ATOMIC_RELEASE);')
        write_target = body.index('f->gpuTarget->target =')
        create_worker = body.index('pthread_create(&f->worker, NULL, pumpFrames, f)')
        publish_magic = body.index('__atomic_store_n(&f->gpuTarget->magic, DARLING_NATIVE_PRESENT_MAGIC, __ATOMIC_RELEASE);')

        self.assertLess(xcreate, xsync)
        self.assertLess(xshape, xsync)
        self.assertLess(xsync, open_target)
        self.assertLess(clear_magic, write_target)
        self.assertLess(write_target, create_worker)
        self.assertLess(create_worker, publish_magic)

    def test_xshm_compiles_cleanly_when_x11_dev_headers_are_available(self):
        compiler = shutil.which('cc') or shutil.which('clang')
        pkg_config = shutil.which('pkg-config')
        if not compiler or not pkg_config:
            self.skipTest('cc and pkg-config required')
        modules = ['x11', 'x11-xcb', 'xcb-shm', 'xext']
        if subprocess.run([pkg_config, '--exists', *modules]).returncode != 0:
            self.skipTest('X11/XShm development headers not installed')
        cflags = subprocess.check_output([pkg_config, '--cflags', *modules], text=True).split()
        libs = subprocess.check_output([pkg_config, '--libs', *modules], text=True).split()
        present_header = _added_lines(
            'patches/darling.patch',
            'src/frameworks/OpenGL/include/OpenGL/DarlingNativePresent.h',
        )
        source = (ROOT / 'tools/m58f299-xshm.c').read_text().replace(
            '#include "../build/darling-src/src/frameworks/OpenGL/include/OpenGL/DarlingNativePresent.h"',
            '#include "DarlingNativePresent.h"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'DarlingNativePresent.h').write_text(present_header)
            xshm_c = root / 'm58f299-xshm.c'
            xshm_c.write_text(source)
            output = root / 'libdarling-xshm.so'
            res = subprocess.run(
                [compiler, '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                 '-I', str(root), str(xshm_c), '-o', str(output), '-pthread', *cflags, *libs],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == '__main__':
    unittest.main()
