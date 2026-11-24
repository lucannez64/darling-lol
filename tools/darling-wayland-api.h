/* darling-lol: experimental native Wayland transport for the League game window (RCLOL_WAYLAND=1).
 *
 * Contract between the host-native shim (tools/darling-wayland.c, an ELF library linked against
 * libwayland-client, libwayland-egl and libxkbcommon) and the guest AppKit X11 backend, which reaches
 * it through the Mach-O wrapper tools/darling-wayland-wrapper.c (libDarlingWayland.dylib).
 *
 * The guest calls DarlingWaylandGetAPI() and gets a table of plain C function pointers. No Wayland
 * type crosses this boundary: windows are small integer ids, the display and EGL window are opaque
 * pointers handed straight to EGL by frameworks/OpenGL/OpenGL.c.
 *
 * Threading: every function is called from the guest's main thread only. The shim never calls back
 * into the guest; it queues events that the guest drains with next_event().
 *
 * A copy of this file lives in cocotron AppKit/X11.backend/DarlingWaylandAPI.h; the two must stay
 * byte-identical (tests/test_wayland_api_header.py checks it).
 *
 * Coordinates are surface-local logical pixels, origin top-left, as Wayland delivers them; the guest
 * converts to AppKit (bottom-left) itself.
 */
#ifndef DARLING_WAYLAND_API_H
#define DARLING_WAYLAND_API_H

#include <stddef.h>
#include <stdint.h>

#define DWL_API_VERSION 3 /* 3 appends display timing and presentation transactions. */

enum dwl_event_type {
    DWL_EVENT_NONE = 0,
    DWL_EVENT_CONFIGURE,       /* window, width, height (0 = client decides), flags (DWL_CONFIGURE_*) */
    DWL_EVENT_CLOSE,           /* window: xdg_toplevel.close */
    DWL_EVENT_POINTER_ENTER,   /* window, x, y */
    DWL_EVENT_POINTER_LEAVE,   /* window */
    DWL_EVENT_POINTER_MOTION,  /* window, x, y, time_ms */
    DWL_EVENT_POINTER_BUTTON,  /* window, button (linux BTN_*), pressed, time_ms, x, y (last position) */
    DWL_EVENT_POINTER_AXIS,    /* window, axis (0 vertical, 1 horizontal), value (wl_fixed as double,
                                  positive = down/right), value120 (0 when not a wheel), time_ms */
    DWL_EVENT_RELATIVE_MOTION, /* window, dx, dy (accelerated), dx_unaccel, dy_unaccel, time_ms */
    DWL_EVENT_KEYBOARD_ENTER,  /* window, mods */
    DWL_EVENT_KEYBOARD_LEAVE,  /* window */
    DWL_EVENT_KEY,             /* window, keycode (X11 keycode = evdev + 8), pressed, keysym (xkb, with
                                  current modifiers), keysym_base (level 1, no modifiers), utf32 (0 when
                                  none), mods, repeat (1 for shim-generated key repeat), time_ms */
    DWL_EVENT_MODIFIERS,       /* window, mods */
    DWL_EVENT_OUTPUTS_CHANGED, /* an output was added, removed or changed; re-query outputs */
    DWL_EVENT_ACTIVATION_DONE, /* window, pressed = 1 if keyboard focus arrived within 500 ms of
                                  activate(), 0 if not; the shim logs the result too */
    /* version 2 */
    DWL_EVENT_POPUP_DONE,      /* window (a popup): the compositor dismissed it (xdg_popup.popup_done);
                                  the guest closes the corresponding window */
    DWL_EVENT_POPUP_CONFIGURE, /* window (a popup), x, y (relative to the parent), width, height */
};

/* Modifier mask, X11 layout so the guest can reuse its X state handling. */
#define DWL_MOD_SHIFT   (1u << 0) /* ShiftMask */
#define DWL_MOD_CAPS    (1u << 1) /* LockMask */
#define DWL_MOD_CTRL    (1u << 2) /* ControlMask */
#define DWL_MOD_ALT     (1u << 3) /* Mod1Mask */
#define DWL_MOD_NUM     (1u << 4) /* Mod2Mask */
#define DWL_MOD_SUPER   (1u << 6) /* Mod4Mask */

#define DWL_CONFIGURE_FULLSCREEN (1u << 0)
#define DWL_CONFIGURE_ACTIVATED  (1u << 1)
#define DWL_CONFIGURE_MAXIMIZED  (1u << 2)
#define DWL_CONFIGURE_SUSPENDED  (1u << 3)

struct dwl_event {
    int32_t type;
    int32_t window;
    double x, y;
    double dx, dy, dx_unaccel, dy_unaccel;
    double value;
    int32_t value120;
    int32_t axis;
    int32_t width, height;
    uint32_t flags;
    uint32_t button;
    uint32_t pressed;
    uint32_t keycode;
    uint32_t keysym;
    uint32_t keysym_base;
    uint32_t utf32;
    uint32_t mods;
    uint32_t repeat;
    uint32_t time_ms;
};

struct dwl_output {
    int32_t x, y;           /* logical position (xdg_output, else wl_output geometry) */
    int32_t width, height;  /* logical size */
    int32_t pixel_width, pixel_height; /* current mode */
    int32_t refresh_mhz;
    int32_t scale;
    char name[64];
};

/* Version 3 timing is deliberately separate: neither dwl_output nor dwl_event
 * changes size. Times are host CLOCK_MONOTONIC nanoseconds when CLOCK_MONOTONIC
 * is valid, otherwise the raw presentation clock observation. Rates are Hz. */
#define DWL_TIMING_NOMINAL          (1u << 0)
#define DWL_TIMING_PRESENTATION     (1u << 1)
#define DWL_TIMING_CLOCK_MONOTONIC  (1u << 2)
#define DWL_TIMING_COUNTER          (1u << 3)
#define DWL_TIMING_FIXED            (1u << 4)
#define DWL_TIMING_VARIABLE         (1u << 5)
#define DWL_TIMING_VSYNC            (1u << 6)
struct dwl_timing {
    uint32_t version, flags;
    uint64_t output_id, surface_generation;
    uint64_t rate_num, rate_den;
    uint64_t presentation_ns, counter, refresh_ns;
    uint32_t presentation_version, reserved;
};

struct dwl_api {
    uint32_t version; /* DWL_API_VERSION */

    /* Connect to an absolute socket path. Returns 0, or -1 after logging why (socket missing, a
     * required global missing: wl_compositor, wl_shm, wl_seat, xdg_wm_base). Optional globals
     * (activation, pointer constraints, relative pointer, pointer warp, viewporter, cursor shape,
     * xdg_output, idle inhibit) are used when present; has_global() reports them. */
    int (*connect)(const char *socket_path);
    void (*disconnect)(void);
    int (*has_global)(const char *interface_name);
    /* The connection fd, for the run loop's wait. */
    int (*fd)(void);
    /* Non-blocking: flush, read what is readable, dispatch, run key repeat. Returns the number of
     * queued events, or -1 when the connection is lost (the guest then stops using Wayland). */
    int (*dispatch)(void);
    /* Flush pending requests; call before the run loop blocks. Returns 0, or -1 on a fatal error. */
    int (*flush)(void);
    /* Pop one queued event. Returns 1 with *out filled, 0 when the queue is empty. */
    int (*next_event)(struct dwl_event *out);
    /* Milliseconds until the next key-repeat event is due, or -1 when none; bounds the run-loop wait. */
    int (*repeat_timeout_ms)(void);
    /* The wl_display, for eglGetPlatformDisplay(EGL_PLATFORM_WAYLAND_KHR, ...). */
    void *(*native_display)(void);

    /* Windows: an xdg_toplevel. Nothing is visible until the first buffer is committed, through EGL
     * on egl_window() or through present_pixels(). Returns an id > 0, or 0 on failure. */
    int32_t (*window_create)(int32_t width, int32_t height, const char *title, const char *app_id);
    void (*window_destroy)(int32_t window);
    void (*window_set_title)(int32_t window, const char *title);
    void (*window_set_fullscreen)(int32_t window, int fullscreen, int32_t output_index /* -1 = any */);
    void (*window_set_min_size)(int32_t window, int32_t width, int32_t height);
    /* Size of the content; resizes the wl_egl_window too. */
    void (*window_set_size)(int32_t window, int32_t width, int32_t height);
    /* Crop/scale the buffer to this destination size with wp_viewporter (0,0 = no viewport). */
    void (*window_set_viewport)(int32_t window, int32_t dest_width, int32_t dest_height);
    /* The wl_egl_window* for eglCreateWindowSurface; created on first call at the current size. */
    void *(*window_egl_window)(int32_t window);
    /* CPU content: BGRA premultiplied (CGImage kCGImageAlphaPremultipliedFirst | little endian), copied
     * into a wl_shm buffer and committed. Ignored once egl_window() has been used for this window. */
    void (*window_present_pixels)(int32_t window, const void *pixels, int32_t width, int32_t height,
                                  int32_t stride);
    /* xdg_activation_v1: ask for focus with token (a startup id or a token from request_token). The
     * result arrives as DWL_EVENT_ACTIVATION_DONE. Returns -1 when the compositor lacks the protocol. */
    int (*window_activate)(int32_t window, const char *token);
    /* A token from this client's last input serial on window (seat + serial + surface). Returns the
     * token length, or -1 when there is no serial yet or no activation protocol. */
    int (*request_token)(int32_t window, char *out, size_t out_size);
    void (*window_idle_inhibit)(int32_t window, int inhibit);

    /* Pointer. lock: zwp_locked_pointer (persistent) + relative motion events; confine: a confined
     * pointer over the whole surface. Both are released by the compositor on focus loss and re-armed
     * by the shim on the next pointer enter while still requested. */
    void (*pointer_lock)(int32_t window, int lock);
    void (*pointer_confine)(int32_t window, int confine);
    /* Move the pointer to surface-local x,y: wp_pointer_warp_v1 when unlocked, the locked pointer's
     * cursor position hint when locked. Returns 0 when sent, -1 when not possible (no pointer focus,
     * no enter serial, no protocol). */
    int (*pointer_warp)(int32_t window, double x, double y);
    /* Cursor image, BGRA premultiplied, cached by key (the same key reuses the buffer and does
     * nothing when it is already current). width 0 hides the cursor. */
    void (*cursor_set_image)(int32_t window, uint64_t key, const void *pixels, int32_t width,
                             int32_t height, int32_t stride, int32_t hot_x, int32_t hot_y);
    void (*cursor_hide)(int32_t window);

    /* Outputs. */
    int (*output_count)(void);
    int (*output_get)(int index, struct dwl_output *out);

    /* ---- version 2: client windows ---- */

    /* An xdg_popup child of `parent` (a toplevel or another popup), placed at x,y relative to the parent
     * surface's top-left, width x height. grab = 1 takes an explicit grab with the last input serial
     * (menus, dropdowns: the compositor sends POPUP_DONE when the user clicks elsewhere); 0 for tooltips.
     * The popup gets pointer/keyboard events, present_pixels, egl_window and cursors like a toplevel.
     * Destroy it with window_destroy. Returns an id > 0, or 0 on failure. */
    int32_t (*popup_create)(int32_t parent, int32_t x, int32_t y, int32_t width, int32_t height, int grab);
    /* Move/resize an existing popup (xdg_popup.reposition when supported, else recreate internally). */
    void (*popup_reposition)(int32_t popup, int32_t x, int32_t y, int32_t width, int32_t height);
    /* Start a compositor-driven move / resize of a toplevel with the serial of the button press that is
     * currently held (xdg_toplevel.move / resize). edges: xdg_toplevel resize_edge values. Returns 0, or -1
     * when there is no current button serial on this window. */
    int (*window_move)(int32_t window);
    int (*window_resize)(int32_t window, uint32_t edges);
    /* xdg_toplevel.set_parent (0 clears). */
    void (*window_set_parent)(int32_t window, int32_t parent);
    void (*window_set_maximized)(int32_t window, int maximized);
    void (*window_set_minimized)(int32_t window);

    /* ---- version 3: main-thread-only timing and commit ownership ---- */
    int (*window_get_timing)(int32_t window, struct dwl_timing *out);
    int (*output_get_timing)(int index, struct dwl_timing *out);
    /* Call immediately before EGL swaps the returned window's surface, and
     * end immediately afterwards, including on failure. A nonzero token
     * reserves commits on that surface; unrelated shim commits are deferred.
     * Zero means ownership unavailable: still render, without feedback.
     * A reservation need not create feedback (optional protocol/queue full).
     * The EGL surface must have one owner; a caller unable to guarantee this
     * must skip begin rather than attributing another owner's submission. */
    uint64_t (*presentation_begin)(int32_t window, uint64_t surface_generation);
    void (*presentation_end)(int32_t window, uint64_t token, int swap_success);
    /* Hide, rebind or surface replacement cancels all outstanding feedback. */
    void (*window_invalidate_timing)(int32_t window);
};

/* Exported by libDarlingWayland.dylib (guest) and as DarlingWaylandAPI by the host shim. Returns NULL
 * when the host library cannot be loaded. */
const struct dwl_api *DarlingWaylandGetAPI(void);

#endif
