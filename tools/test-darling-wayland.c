/* Host smoke and diagnostic program for the experimental Wayland shim (tools/darling-wayland.c).
 * It talks to the library only through the dwl_api table, like the guest does.
 *   --globals            list optional globals and outputs
 *   --window SECONDS     640x360 window with a solid CPU frame, print every event
 *   --egl SECONDS        same through EGL (the path frameworks/OpenGL uses)
 *   --popup SECONDS      640x360 toplevel plus a 200x120 popup at 40,40, moved to 80,60 after 1 s
 *   --move               manual: click and hold on the window; the compositor then drags it (window_move)
 *   --activation         xdg-activation focus bridge experiment (needs a click, see below)
 * The library is loaded with dlopen from $DARLING_WAYLAND_LIB, else build/libdarling-wayland.so, else
 * build/libdarling-wayland.next.so next to this program. */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <dlfcn.h>
#include <libgen.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "darling-wayland-api.h"

static const struct dwl_api *api;

static int64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static const struct dwl_api *load_api(const char *self) {
    char path[PATH_MAX];
    const char *candidates[3] = {getenv("DARLING_WAYLAND_LIB"), NULL, NULL};
    char dir[PATH_MAX], a[PATH_MAX], b[PATH_MAX];
    snprintf(dir, sizeof dir, "%s", self);
    snprintf(a, sizeof a, "%s/libdarling-wayland.so", dirname(dir));
    snprintf(dir, sizeof dir, "%s", self);
    snprintf(b, sizeof b, "%s/libdarling-wayland.next.so", dirname(dir));
    candidates[1] = a;
    candidates[2] = b;
    for (int i = 0; i < 3; i++) {
        if (!candidates[i]) continue;
        snprintf(path, sizeof path, "%s", candidates[i]);
        void *h = dlopen(path, RTLD_NOW);
        if (!h) continue;
        const struct dwl_api *(*get)(void) = (const struct dwl_api *(*)(void))dlsym(h, "DarlingWaylandAPI");
        if (get) {
            fprintf(stderr, "using %s\n", path);
            return get();
        }
    }
    fprintf(stderr, "cannot load libdarling-wayland (set DARLING_WAYLAND_LIB): %s\n", dlerror());
    return NULL;
}

static int connect_wayland(void) {
    const char *runtime = getenv("XDG_RUNTIME_DIR");
    const char *display = getenv("WAYLAND_DISPLAY");
    char path[PATH_MAX];
    if (!runtime) runtime = "/run/user/1000";
    if (!display) display = "wayland-0";
    if (display[0] == '/')
        snprintf(path, sizeof path, "%s", display);
    else
        snprintf(path, sizeof path, "%s/%s", runtime, display);
    return api->connect(path);
}

static void print_event(const struct dwl_event *e) {
    switch (e->type) {
    case DWL_EVENT_CONFIGURE:
        printf("CONFIGURE w=%d size=%dx%d flags=0x%x\n", e->window, e->width, e->height, e->flags);
        break;
    case DWL_EVENT_CLOSE: printf("CLOSE w=%d\n", e->window); break;
    case DWL_EVENT_POINTER_ENTER: printf("POINTER_ENTER w=%d %.2f,%.2f\n", e->window, e->x, e->y); break;
    case DWL_EVENT_POINTER_LEAVE: printf("POINTER_LEAVE w=%d\n", e->window); break;
    case DWL_EVENT_POINTER_MOTION: printf("POINTER_MOTION w=%d %.2f,%.2f t=%u\n", e->window, e->x, e->y, e->time_ms); break;
    case DWL_EVENT_POINTER_BUTTON:
        printf("POINTER_BUTTON w=%d button=0x%x pressed=%u %.2f,%.2f\n", e->window, e->button, e->pressed, e->x, e->y);
        break;
    case DWL_EVENT_POINTER_AXIS:
        printf("POINTER_AXIS w=%d axis=%d value=%.3f value120=%d\n", e->window, e->axis, e->value, e->value120);
        break;
    case DWL_EVENT_RELATIVE_MOTION:
        printf("RELATIVE_MOTION w=%d d=%.2f,%.2f unaccel=%.2f,%.2f\n", e->window, e->dx, e->dy, e->dx_unaccel, e->dy_unaccel);
        break;
    case DWL_EVENT_KEYBOARD_ENTER: printf("KEYBOARD_ENTER w=%d mods=0x%x\n", e->window, e->mods); break;
    case DWL_EVENT_KEYBOARD_LEAVE: printf("KEYBOARD_LEAVE w=%d\n", e->window); break;
    case DWL_EVENT_KEY:
        printf("KEY w=%d keycode=%u pressed=%u sym=0x%x base=0x%x utf32=0x%x mods=0x%x repeat=%u\n", e->window,
               e->keycode, e->pressed, e->keysym, e->keysym_base, e->utf32, e->mods, e->repeat);
        break;
    case DWL_EVENT_MODIFIERS: printf("MODIFIERS w=%d mods=0x%x\n", e->window, e->mods); break;
    case DWL_EVENT_OUTPUTS_CHANGED: printf("OUTPUTS_CHANGED\n"); break;
    case DWL_EVENT_ACTIVATION_DONE: printf("ACTIVATION_DONE w=%d focused=%u\n", e->window, e->pressed); break;
    case DWL_EVENT_POPUP_DONE: printf("POPUP_DONE w=%d\n", e->window); break;
    case DWL_EVENT_POPUP_CONFIGURE:
        printf("POPUP_CONFIGURE w=%d pos=%.0f,%.0f size=%dx%d\n", e->window, e->x, e->y, e->width, e->height);
        break;
    default: printf("event type %d\n", e->type); break;
    }
    fflush(stdout);
}

/* Dispatch for up to ms milliseconds (waiting on the connection fd), calling handler per event. */
static int pump(int ms, void (*handler)(const struct dwl_event *)) {
    int64_t end = now_ms() + ms;
    for (;;) {
        if (api->dispatch() < 0) return -1;
        struct dwl_event e;
        while (api->next_event(&e)) {
            print_event(&e);
            if (handler) handler(&e);
        }
        int64_t left = end - now_ms();
        if (left <= 0) return 0;
        int wait = api->repeat_timeout_ms();
        if (wait < 0 || wait > left) wait = (int)left;
        api->flush();
        struct pollfd pfd = {.fd = api->fd(), .events = POLLIN};
        poll(&pfd, 1, wait > 50 ? 50 : wait);
    }
}

static void print_globals(void) {
    static const char *names[] = {"xdg_activation_v1", "zwp_pointer_constraints_v1", "zwp_relative_pointer_manager_v1",
                                  "wp_pointer_warp_v1", "wp_viewporter", "wp_cursor_shape_manager_v1",
                                  "zxdg_output_manager_v1", "zwp_idle_inhibit_manager_v1"};
    for (size_t i = 0; i < sizeof names / sizeof *names; i++) printf("%-34s %s\n", names[i], api->has_global(names[i]) ? "yes" : "no");
    int n = api->output_count();
    printf("outputs: %d\n", n);
    for (int i = 0; i < n; i++) {
        struct dwl_output o;
        if (api->output_get(i, &o) == 0)
            printf("  [%d] %s logical %d,%d %dx%d mode %dx%d @ %.3f Hz scale %d\n", i, o.name, o.x, o.y, o.width, o.height,
                   o.pixel_width, o.pixel_height, o.refresh_mhz / 1000.0, o.scale);
    }
}

static int configured;
static int closed;
static void note_event(const struct dwl_event *e) {
    if (e->type == DWL_EVENT_CONFIGURE) configured = 1;
    if (e->type == DWL_EVENT_CLOSE) closed = 1;
}

static int popup_configures, popup_dones;
static void popup_note(const struct dwl_event *e) {
    note_event(e);
    if (e->type == DWL_EVENT_POPUP_CONFIGURE) popup_configures++;
    if (e->type == DWL_EVENT_POPUP_DONE) popup_dones++;
}

static void solid_frame(int32_t window, int w, int h, uint32_t argb) {
    uint32_t *px = malloc((size_t)w * h * 4);
    if (!px) return;
    for (int i = 0; i < w * h; i++) px[i] = argb;
    api->window_present_pixels(window, px, w, h, w * 4);
    free(px);
}

static int mode_window(int seconds) {
    int32_t win = api->window_create(640, 360, "darling-wayland test (closes itself)", "darling-wayland-test");
    if (!win) return 1;
    pump(200, note_event);
    solid_frame(win, 640, 360, 0xff3060c0);
    int64_t end = now_ms() + seconds * 1000;
    int frames = 1;
    while (now_ms() < end && !closed) {
        if (pump(250, note_event) < 0) return 1;
    }
    printf("window mode done: configured=%d presented=%d\n", configured, frames);
    api->window_destroy(win);
    return 0;
}

static int mode_popup(int seconds) {
    int32_t win = api->window_create(640, 360, "darling-wayland popup test (closes itself)", "darling-wayland-test");
    if (!win) return 1;
    pump(200, popup_note);
    solid_frame(win, 640, 360, 0xff3060c0);
    pump(200, popup_note);
    int32_t popup = api->popup_create(win, 40, 40, 200, 120, 0);
    printf("popup_create -> %d\n", popup);
    if (!popup) {
        api->window_destroy(win);
        return 1;
    }
    pump(200, popup_note);
    solid_frame(popup, 200, 120, 0xffd0a030);
    int64_t start = now_ms(), end = start + seconds * 1000;
    int repositioned = 0;
    while (now_ms() < end && !closed && !popup_dones) {
        if (!repositioned && now_ms() - start >= 1000) {
            api->popup_reposition(popup, 80, 60, 200, 120);
            printf("popup_reposition(80,60,200,120)\n");
            repositioned = 1;
        }
        if (pump(100, popup_note) < 0) return 1;
        if (repositioned == 1) {
            solid_frame(popup, 200, 120, 0xff30d060);
            repositioned = 2;
        }
    }
    printf("popup mode done: popup_configure_events=%d popup_done_events=%d repositioned=%d\n", popup_configures,
           popup_dones, repositioned != 0);
    api->window_destroy(win); /* destroys the popup first */
    pump(100, NULL);
    return 0;
}

static int move_started;
static void move_event(const struct dwl_event *e) {
    note_event(e);
    if (e->type == DWL_EVENT_POINTER_BUTTON && e->pressed && !move_started) {
        move_started = 1;
        printf("window_move -> %d\n", api->window_move(e->window));
    }
}

static int mode_move(void) {
    int32_t win = api->window_create(640, 360, "darling-wayland move test", "darling-wayland-test");
    if (!win) return 1;
    pump(200, note_event);
    solid_frame(win, 640, 360, 0xff3060c0);
    printf("Press and hold a mouse button on the window and drag: the compositor should move it.\n");
    printf("The test exits after 30 s or when the window is closed.\n");
    fflush(stdout);
    int64_t end = now_ms() + 30000;
    while (now_ms() < end && !closed)
        if (pump(250, move_event) < 0) return 1;
    api->window_destroy(win);
    return 0;
}

static int mode_egl(int seconds) {
    int32_t win = api->window_create(640, 360, "darling-wayland test (closes itself)", "darling-wayland-test");
    if (!win) return 1;
    pump(200, note_event);
    PFNEGLGETPLATFORMDISPLAYPROC get_platform_display = (PFNEGLGETPLATFORMDISPLAYPROC)eglGetProcAddress("eglGetPlatformDisplay");
    if (!get_platform_display) {
        fprintf(stderr, "eglGetPlatformDisplay missing\n");
        return 1;
    }
    EGLDisplay dpy = get_platform_display(EGL_PLATFORM_WAYLAND_KHR, api->native_display(), NULL);
    EGLint major = 0, minor = 0;
    if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, &major, &minor)) {
        fprintf(stderr, "EGL display init failed (0x%x)\n", eglGetError());
        return 1;
    }
    printf("EGL %d.%d vendor=%s\n", major, minor, eglQueryString(dpy, EGL_VENDOR));
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint cfg_attr[] = {EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_RED_SIZE, 8,
                               EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
    EGLConfig cfg;
    EGLint ncfg = 0;
    if (!eglChooseConfig(dpy, cfg_attr, &cfg, 1, &ncfg) || ncfg < 1) {
        fprintf(stderr, "no EGL config (0x%x)\n", eglGetError());
        return 1;
    }
    const EGLint ctx_attr[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr);
    void *egl_window = api->window_egl_window(win);
    if (ctx == EGL_NO_CONTEXT || !egl_window) {
        fprintf(stderr, "context/window failed (0x%x)\n", eglGetError());
        return 1;
    }
    PFNEGLCREATEPLATFORMWINDOWSURFACEPROC create_surface =
        (PFNEGLCREATEPLATFORMWINDOWSURFACEPROC)eglGetProcAddress("eglCreatePlatformWindowSurface");
    EGLSurface surf = create_surface ? create_surface(dpy, cfg, egl_window, NULL)
                                     : eglCreateWindowSurface(dpy, cfg, (EGLNativeWindowType)egl_window, NULL);
    if (surf == EGL_NO_SURFACE || !eglMakeCurrent(dpy, surf, surf, ctx)) {
        fprintf(stderr, "surface/makecurrent failed (0x%x)\n", eglGetError());
        return 1;
    }
    eglSwapInterval(dpy, 0);
    void (*clear_color)(float, float, float, float) = (void (*)(float, float, float, float))eglGetProcAddress("glClearColor");
    void (*clear)(unsigned) = (void (*)(unsigned))eglGetProcAddress("glClear");
    if (!clear_color || !clear) {
        fprintf(stderr, "glClear not resolvable through eglGetProcAddress\n");
        return 1;
    }
    int frames = 0;
    int64_t start = now_ms(), end = start + seconds * 1000;
    while (now_ms() < end && !closed) {
        float t = (float)((now_ms() - start) % 1000) / 1000.0f;
        clear_color(t, 0.3f, 1.0f - t, 1.0f);
        clear(0x4000);
        if (!eglSwapBuffers(dpy, surf)) {
            fprintf(stderr, "eglSwapBuffers failed (0x%x)\n", eglGetError());
            break;
        }
        frames++;
        if (pump(16, note_event) < 0) return 1;
    }
    printf("egl mode done: configured=%d frames=%d in %lld ms\n", configured, frames, (long long)(now_ms() - start));
    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroySurface(dpy, surf);
    eglDestroyContext(dpy, ctx);
    eglTerminate(dpy);
    api->window_destroy(win);
    return 0;
}

/* ---- activation experiment ------------------------------------------------------------------- */

static int act_kbd_enter, act_done, act_focused;
static void act_event(const struct dwl_event *e) {
    if (e->type == DWL_EVENT_KEYBOARD_ENTER) act_kbd_enter = 1;
    if (e->type == DWL_EVENT_ACTIVATION_DONE) {
        act_done = 1;
        act_focused = e->pressed;
    }
}

static void send_startup_info(Display *dpy, Window root, Window sender, const char *message) {
    Atom begin = XInternAtom(dpy, "_NET_STARTUP_INFO_BEGIN", False);
    Atom more = XInternAtom(dpy, "_NET_STARTUP_INFO", False);
    size_t len = strlen(message) + 1; /* includes the terminating NUL */
    for (size_t off = 0; off < len; off += 20) {
        XEvent ev;
        memset(&ev, 0, sizeof ev);
        ev.xclient.type = ClientMessage;
        ev.xclient.window = sender;
        ev.xclient.message_type = off == 0 ? begin : more;
        ev.xclient.format = 8;
        size_t n = len - off < 20 ? len - off : 20;
        memcpy(ev.xclient.data.b, message + off, n);
        XSendEvent(dpy, root, False, PropertyChangeMask, &ev);
    }
    XSync(dpy, False);
}

static void variant(const char *label, int mode, const char *token) {
    printf("\n== %s ==\n", label);
    act_kbd_enter = act_done = act_focused = 0;
    configured = 0;
    int32_t win = api->window_create(400, 240, label, "darling-wayland-test");
    if (!win) {
        printf("window_create failed\n");
        return;
    }
    pump(300, note_event);
    solid_frame(win, 400, 240, 0xffc06030);
    pump(300, act_event);
    printf("before activation: keyboard focus on the new window = %s\n", act_kbd_enter ? "yes" : "no");
    if (mode != 0) {
        int rc = api->window_activate(win, token);
        printf("window_activate(\"%s\") returned %d\n", token, rc);
    }
    int64_t end = now_ms() + (mode ? 1500 : 1000);
    while (now_ms() < end && !act_done) pump(50, act_event);
    printf("RESULT %s: keyboard enter=%s activation_done=%s focused=%d\n", label, act_kbd_enter ? "yes" : "no",
           act_done ? "yes" : "no", act_focused);
    api->window_destroy(win);
    pump(200, NULL);
}

static int mode_activation(void) {
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) {
        fprintf(stderr, "no X11 display (XWayland) available\n");
        return 1;
    }
    int screen = DefaultScreen(dpy);
    Window root = RootWindow(dpy, screen);
    Window win = XCreateSimpleWindow(dpy, root, 100, 100, 400, 200, 0, 0, 0xc0c0c0);
    XStoreName(dpy, win, "darling-wayland activation test: click me");
    XSelectInput(dpy, win, ButtonPressMask | ExposureMask);
    XMapWindow(dpy, win);
    XSync(dpy, False);
    printf("Click the X11 window titled \"darling-wayland activation test: click me\" now.\n");
    fflush(stdout);
    unsigned long click_time = 0;
    for (;;) {
        XEvent ev;
        XNextEvent(dpy, &ev);
        if (ev.type == ButtonPress) {
            click_time = ev.xbutton.time;
            break;
        }
    }
    printf("ButtonPress at X server time %lu; creating Wayland windows in 3 s\n", click_time);
    fflush(stdout);
    Window sender = XCreateSimpleWindow(dpy, root, -10, -10, 1, 1, 0, 0, 0);
    sleep(3);

    variant("a: no token", 0, "");

    char id[160], message[320], host[64] = "host";
    gethostname(host, sizeof host - 1);
    snprintf(id, sizeof id, "darling-wayland-test/test/%d-0-%s_TIME%lu", (int)getpid(), host, click_time);
    snprintf(message, sizeof message, "new: ID=%s TIMESTAMP=%lu NAME=test SCREEN=0", id, click_time);
    send_startup_info(dpy, root, sender, message);
    variant("b: X11 startup id", 1, id);
    snprintf(message, sizeof message, "remove: ID=%s", id);
    send_startup_info(dpy, root, sender, message);

    char random_token[40];
    snprintf(random_token, sizeof random_token, "darling-test-%08x%08x", (unsigned)rand() ^ (unsigned)getpid(), (unsigned)rand());
    variant("c: random string", 1, random_token);
    XCloseDisplay(dpy);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s --globals | --window SECONDS | --egl SECONDS | --popup SECONDS | --move | --activation\n", argv[0]);
        return 2;
    }
    char self[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", self, sizeof self - 1);
    self[n > 0 ? n : 0] = '\0';
    api = load_api(self);
    if (!api || api->version != DWL_API_VERSION) return 1;
    if (connect_wayland() != 0) return 1;
    int rc = 2;
    if (!strcmp(argv[1], "--globals")) {
        print_globals();
        rc = 0;
    } else if (!strcmp(argv[1], "--window") && argc > 2) {
        rc = mode_window(atoi(argv[2]));
    } else if (!strcmp(argv[1], "--popup") && argc > 2) {
        rc = mode_popup(atoi(argv[2]));
    } else if (!strcmp(argv[1], "--move")) {
        rc = mode_move();
    } else if (!strcmp(argv[1], "--egl") && argc > 2) {
        rc = mode_egl(atoi(argv[2]));
    } else if (!strcmp(argv[1], "--activation")) {
        rc = mode_activation();
    } else {
        fprintf(stderr, "unknown mode %s\n", argv[1]);
    }
    api->disconnect();
    return rc;
}
