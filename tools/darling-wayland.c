/* darling-lol: host-native Wayland shim for the experimental native game window (RCLOL_WAYLAND=1).
 *
 * An ELF library linked against libwayland-client, libwayland-egl and libxkbcommon. Mach-O guest code
 * reaches it through tools/darling-wayland-wrapper.c; the contract is tools/darling-wayland-api.h.
 * Single threaded by design: the guest main thread calls in, events are queued, nothing calls back.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include <wayland-egl.h>
#include <xkbcommon/xkbcommon.h>

#include "idle-inhibit-unstable-v1-client-protocol.h"
#include "pointer-constraints-unstable-v1-client-protocol.h"
#include "relative-pointer-unstable-v1-client-protocol.h"
#include "viewporter-client-protocol.h"
#include "xdg-activation-v1-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "xdg-shell-client-protocol.h"
#ifdef DWL_HAVE_POINTER_WARP
#include "pointer-warp-v1-client-protocol.h"
#endif

#ifdef DWL_HAVE_PRESENTATION
#include "presentation-time-client-protocol.h"
#endif

#include "darling-wayland-api.h"

#define LOG(...) do { fprintf(stderr, "darling-wayland: " __VA_ARGS__); fputc('\n', stderr); } while (0)
#define UNUSED __attribute__((unused))

#define SHM_BUFFERS 3
#define CURSOR_CACHE 16
#define ACTIVATION_WINDOW_MS 500
#define MAX_QUEUE (1u << 16)
#define MAX_TOKEN_LOG 12

struct shmbuf {
    struct wl_buffer *buffer;
    void *data;
    size_t size;
    int32_t width, height;
    bool busy;
};

struct presentation_pending;

struct window {
    int32_t id;
    struct wl_surface *surface;
    struct xdg_surface *xdg_surface;
    struct xdg_toplevel *toplevel; /* NULL for popups */
    struct xdg_popup *popup;       /* NULL for toplevels */
    struct window *parent;         /* popups: the window this popup was created on */
    bool is_popup, popup_grab;
    int32_t popup_x, popup_y;      /* requested position relative to the parent */
    int32_t pending_px, pending_py, pending_pw, pending_ph; /* from xdg_popup.configure */
    uint32_t reposition_token;
    bool button_held;              /* a button pressed on this window is still down */
    uint32_t button_serial, button_code;
    struct wp_viewport *viewport;
    struct wl_egl_window *egl;
    struct zwp_locked_pointer_v1 *locked;
    struct zwp_confined_pointer_v1 *confined;
    struct zwp_idle_inhibitor_v1 *idle;
    char *app_id;
    int32_t width, height;
    int32_t vp_width, vp_height;
    bool configured;
    int32_t pending_width, pending_height;
    uint32_t pending_flags;
    bool want_lock, want_confine;
    uint32_t enter_serial; /* last wl_pointer.enter serial for this surface */
    uint32_t input_serial; /* last key/button serial for this surface */
    bool have_input_serial;
    double last_x, last_y;
    struct shmbuf bufs[SHM_BUFFERS];
    bool act_pending;
    int64_t act_deadline;
    char act_token[MAX_TOKEN_LOG + 1];
    uint64_t *entered;
    size_t entered_count;
    uint64_t timing_output, surface_generation, presentation_token;
    bool deferred_commit;
    struct dwl_timing observation;
    struct presentation_pending *feedback;
    unsigned feedback_count;
};

struct output {
    uint32_t name;
    uint64_t identity;
    int32_t timing_refresh;
    struct wl_output *output;
    struct zxdg_output_v1 *xdg;
    int32_t geo_x, geo_y, transform;
    int32_t pixel_width, pixel_height, refresh, scale;
    int32_t xdg_x, xdg_y, xdg_width, xdg_height;
    bool has_xdg_pos, has_xdg_size;
    char name_str[64];
};

struct cursor_entry {
    bool used;
    uint64_t key;
    struct wl_buffer *buffer;
    void *data;
    size_t size;
    int32_t width, height, hot_x, hot_y;
    uint64_t stamp;
};

enum cursor_kind { CURSOR_NONE, CURSOR_IMAGE, CURSOR_HIDDEN };

struct axis_pending {
    bool have;
    double value;
    int32_t value120;
    uint32_t time;
};

static struct {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct xdg_wm_base *wm_base;
    struct xdg_activation_v1 *activation;
    struct zwp_pointer_constraints_v1 *constraints;
    struct zwp_relative_pointer_manager_v1 *relative_manager;
#ifdef DWL_HAVE_POINTER_WARP
    struct wp_pointer_warp_v1 *warp;
#endif
    struct wp_viewporter *viewporter;
    struct zxdg_output_manager_v1 *xdg_output_manager;
    struct zwp_idle_inhibit_manager_v1 *idle_manager;
#ifdef DWL_HAVE_PRESENTATION
    struct wp_presentation *presentation;
    uint32_t presentation_name, presentation_version;
    clockid_t presentation_clock;
    bool presentation_clock_known;
#endif
    char **advertised;
    size_t advertised_count;
    bool dead;

    struct wl_pointer *pointer;
    struct wl_keyboard *keyboard;
    struct zwp_relative_pointer_v1 *relative;
    uint32_t seat_version;
    struct window *ptr_focus, *kbd_focus;
    uint32_t ptr_enter_serial;
    uint32_t grab_serial; /* last button/key serial on the seat, for popup grabs and move/resize */
    bool have_grab_serial;
    struct axis_pending axes[2];

    struct xkb_context *xkb;
    struct xkb_keymap *keymap;
    struct xkb_state *xkb_state;
    uint32_t mods;
    bool rep_active;
    struct window *rep_window;
    uint32_t rep_key;
    int64_t rep_next;
    int32_t rep_rate, rep_delay;

    struct window **windows;
    size_t window_count;
    int32_t next_window_id;

    struct output **outputs;
    size_t output_count;

    struct wl_surface *cursor_surface;
    struct cursor_entry cursors[CURSOR_CACHE];
    uint64_t cursor_stamp;
    enum cursor_kind cursor_kind;
    uint64_t cursor_key;
    struct cursor_entry *cursor_current;
    enum cursor_kind applied_kind;
    uint64_t applied_key;
    uint32_t applied_serial;
    bool applied_valid;

    struct dwl_event *queue;
    size_t queue_cap, queue_head, queue_len;

    struct xdg_activation_token_v1 *handoff_token;
    int64_t handoff_last_ms;
} g;

/* ------------------------------------------------------------------ helpers */

static int64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static uint32_t min_u32(uint32_t a, uint32_t b) { return a < b ? a : b; }

static void queue_event(const struct dwl_event *e) {
    if (g.queue_len >= MAX_QUEUE) return;
    if (g.queue_len == g.queue_cap) {
        size_t cap = g.queue_cap ? g.queue_cap * 2 : 256;
        struct dwl_event *q = malloc(cap * sizeof *q);
        if (!q) return;
        for (size_t i = 0; i < g.queue_len; i++) q[i] = g.queue[(g.queue_head + i) % g.queue_cap];
        free(g.queue);
        g.queue = q;
        g.queue_cap = cap;
        g.queue_head = 0;
    }
    g.queue[(g.queue_head + g.queue_len) % g.queue_cap] = *e;
    g.queue_len++;
}

static struct dwl_event make_event(int type, const struct window *w) {
    struct dwl_event e;
    memset(&e, 0, sizeof e);
    e.type = type;
    e.window = w ? w->id : 0;
    return e;
}

static struct window *find_window(int32_t id) {
    for (size_t i = 0; i < g.window_count; i++)
        if (g.windows[i]->id == id) return g.windows[i];
    return NULL;
}

static struct window *window_of_surface(struct wl_surface *surface) {
    return surface ? wl_surface_get_user_data(surface) : NULL;
}

static void *bind_global(struct wl_registry *reg, uint32_t name, const struct wl_interface *iface,
                         uint32_t advertised, uint32_t cap) {
    uint32_t max = min_u32(advertised, min_u32(cap, (uint32_t)iface->version));
    return wl_registry_bind(reg, name, iface, max);
}

static void flush_best_effort(void) {
    if (!g.display || g.dead) return;
    if (wl_display_flush(g.display) < 0 && errno != EAGAIN) g.dead = true;
}

static void read_pending_nonblocking(void) {
    if (!g.display || g.dead) return;
    flush_best_effort();
    if (g.dead) return;
    while (wl_display_prepare_read(g.display) != 0) {
        if (wl_display_dispatch_pending(g.display) < 0) {
            g.dead = true;
            return;
        }
    }
    struct pollfd pfd = {.fd = wl_display_get_fd(g.display), .events = POLLIN};
    int pr = poll(&pfd, 1, 0);
    if (pr > 0 && (pfd.revents & (POLLIN | POLLERR | POLLHUP))) {
        if (wl_display_read_events(g.display) < 0) {
            g.dead = true;
            return;
        }
    } else {
        wl_display_cancel_read(g.display);
    }
    if (wl_display_dispatch_pending(g.display) < 0) g.dead = true;
}

static void handoff_token_done(void *data UNUSED, struct xdg_activation_token_v1 *t, const char *token) {
    if (g.handoff_token == t) g.handoff_token = NULL;
    xdg_activation_token_v1_destroy(t);
    if (!token || !token[0]) return;
    size_t len = strlen(token);
    if (len >= 480) return;
    for (size_t i = 0; i < len; i++) {
        if ((unsigned char)token[i] <= 0x20) return;
    }
    char tmp[128], dst[128], buf[520];
    unsigned uid = (unsigned)getuid();
    snprintf(tmp, sizeof tmp, "/dev/shm/darling-wayland-handoff-%u.%d.tmp", uid, (int)getpid());
    snprintf(dst, sizeof dst, "/dev/shm/darling-wayland-handoff-%u", uid);
    int n = snprintf(buf, sizeof buf, "%lld %s\n", (long long)time(NULL), token);
    if (n <= 0 || (size_t)n >= sizeof buf) return;
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (fd < 0) return;
    if (fchmod(fd, 0600) != 0) {
        close(fd);
        unlink(tmp);
        return;
    }
    ssize_t off = 0;
    while (off < (ssize_t)n) {
        ssize_t written = write(fd, buf + off, (size_t)((ssize_t)n - off));
        if (written < 0) {
            if (errno == EINTR) continue;
            close(fd);
            unlink(tmp);
            return;
        }
        off += written;
    }
    close(fd);
    if (rename(tmp, dst) != 0) unlink(tmp);
}

static const struct xdg_activation_token_v1_listener handoff_token_listener = {
    .done = handoff_token_done,
};

static void maybe_mint_handoff_token(struct window *w, uint32_t serial) {
    if (!g.activation || !g.seat || !w || !w->surface || w->is_popup) return;
    int64_t now = now_ms();
    if (g.handoff_last_ms && now - g.handoff_last_ms < 250) return;
    if (g.handoff_token) {
        xdg_activation_token_v1_destroy(g.handoff_token);
        g.handoff_token = NULL;
    }
    struct xdg_activation_token_v1 *t = xdg_activation_v1_get_activation_token(g.activation);
    if (!t) return;
    g.handoff_token = t;
    g.handoff_last_ms = now;
    xdg_activation_token_v1_add_listener(t, &handoff_token_listener, NULL);
    xdg_activation_token_v1_set_serial(t, serial, g.seat);
    xdg_activation_token_v1_set_surface(t, w->surface);
    xdg_activation_token_v1_commit(t);
    flush_best_effort();
}

/* ------------------------------------------------------------------ display timing
 * SPDX-License-Identifier: GPL-3.0-only
 * Optional feedback never owns drawable retirement or pacing. All state here
 * belongs to the existing main-thread Wayland connection. */

static uint64_t next_surface_generation, next_presentation_token, connection_identity;
static void queue_outputs_changed(void);

static uint64_t timing_now_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static struct output *timing_output(uint64_t identity) {
    for (size_t i = 0; i < g.output_count; ++i)
        if (g.outputs[i]->identity == identity) return g.outputs[i];
    return NULL;
}

static bool timing_entered(const struct window *w, uint64_t identity) {
    for (size_t i = 0; i < w->entered_count; ++i)
        if (w->entered[i] == identity) return true;
    return false;
}

static struct output *timing_choose_output(struct window *w, uint64_t sync_output) {
    if (!sync_output && (w->observation.flags & DWL_TIMING_PRESENTATION))
        sync_output = w->observation.output_id;
    struct output *out = timing_output(sync_output);
    if (out && out->timing_refresh > 0) return out;
    out = timing_output(w->timing_output);
    if (out && out->timing_refresh > 0 && timing_entered(w, out->identity)) return out;
    struct output *best = NULL;
    for (size_t i = 0; i < g.output_count; ++i) {
        out = g.outputs[i];
        if (out->timing_refresh <= 0 || !timing_entered(w, out->identity)) continue;
        if (!best || out->timing_refresh > best->timing_refresh ||
            (out->timing_refresh == best->timing_refresh && out->identity < best->identity)) best = out;
    }
    if (best) return best;
    /* Before enter, use the first usable Wayland output, never RandR. */
    for (size_t i = 0; i < g.output_count; ++i)
        if (g.outputs[i]->timing_refresh > 0) return g.outputs[i];
    return NULL;
}

struct presentation_pending {
    struct presentation_pending *next;
    struct window *window;
    uint64_t token, generation, output_id, submitted_ns, expires_ns;
    int64_t clock_low, clock_high;
    bool clock_valid, committed;
#ifdef DWL_HAVE_PRESENTATION
    struct wp_presentation_feedback *feedback;
#endif
};

static void timing_drop_feedback(struct presentation_pending *pending) {
    struct presentation_pending **link = &pending->window->feedback;
    while (*link && *link != pending) link = &(*link)->next;
    if (*link) { *link = pending->next; --pending->window->feedback_count; }
#ifdef DWL_HAVE_PRESENTATION
    if (pending->feedback) wp_presentation_feedback_destroy(pending->feedback);
#endif
    free(pending);
}

static void timing_invalidate(struct window *w) {
    /* Invalidation precedes association/surface mutation. The guest publishes
     * this generation into its own process-scoped timing snapshots. */
    w->surface_generation = ++next_surface_generation;
    memset(&w->observation, 0, sizeof w->observation);
    while (w->feedback) timing_drop_feedback(w->feedback);
}

static void timing_reassociate(struct window *w, uint64_t sync_output) {
    struct output *out = timing_choose_output(w, sync_output);
    uint64_t identity = out ? out->identity : 0;
    if (identity != w->timing_output) {
        timing_invalidate(w);
        w->timing_output = identity;
    }
}

static void timing_expire(struct window *w, uint64_t now) {
    struct presentation_pending *pending = w->feedback;
    while (pending) {
        struct presentation_pending *next = pending->next;
        if (now >= pending->expires_ns) timing_drop_feedback(pending);
        pending = next;
    }
}

static void timing_surface_enter(void *data, struct wl_surface *surface UNUSED, struct wl_output *output) {
    struct window *w = data;
    for (size_t i = 0; i < g.output_count; ++i) {
        struct output *out = g.outputs[i];
        if (out->output != output || timing_entered(w, out->identity)) continue;
        uint64_t *entered = realloc(w->entered, (w->entered_count + 1) * sizeof *entered);
        if (!entered) return;
        w->entered = entered;
        w->entered[w->entered_count++] = out->identity;
        timing_reassociate(w, 0);
        queue_outputs_changed();
        return;
    }
}

static void timing_surface_leave(void *data, struct wl_surface *surface UNUSED, struct wl_output *output) {
    struct window *w = data;
    for (size_t i = 0; i < g.output_count; ++i) {
        if (g.outputs[i]->output != output) continue;
        uint64_t identity = g.outputs[i]->identity;
        for (size_t j = 0; j < w->entered_count; ++j) {
            if (w->entered[j] != identity) continue;
            memmove(&w->entered[j], &w->entered[j + 1], (w->entered_count - j - 1) * sizeof *w->entered);
            --w->entered_count;
            if (w->timing_output == identity) timing_invalidate(w);
            timing_reassociate(w, 0);
            queue_outputs_changed();
            return;
        }
    }
}

static const struct wl_surface_listener timing_surface_listener = {
    .enter = timing_surface_enter, .leave = timing_surface_leave,
};

static void timing_attach_surface(struct window *w) {
    timing_invalidate(w);
    w->entered_count = 0;
    w->timing_output = 0;
    wl_surface_add_listener(w->surface, &timing_surface_listener, w);
}

/* Every shim commit to a content surface passes here. EGL alone commits
 * between begin/end. Cursor surfaces are independent and need no reservation. */
static void window_commit(struct window *w) {
    if (w->presentation_token) { w->deferred_commit = true; return; }
    wl_surface_commit(w->surface);
}

static void timing_fill_output(struct output *out, struct dwl_timing *timing) {
    if (!out) return;
    timing->output_id = out->identity;
    if (out->timing_refresh > 0) {
        timing->rate_num = (uint64_t)out->timing_refresh;
        timing->rate_den = 1000;
        timing->flags |= DWL_TIMING_NOMINAL;
    }
}

static int dwl_output_get_timing(int index, struct dwl_timing *out) {
    if (!out || index < 0 || (size_t)index >= g.output_count) return -1;
    memset(out, 0, sizeof *out);
    out->version = 1;
    timing_fill_output(g.outputs[index], out);
    return 0;
}

static int dwl_window_get_timing(int32_t window, struct dwl_timing *out) {
    struct window *w = find_window(window);
    if (!w || !out || g.dead) return -1;
    timing_expire(w, timing_now_ns());
    timing_reassociate(w, 0);
    *out = w->observation;
    out->version = 1;
    out->surface_generation = w->surface_generation;
    timing_fill_output(timing_output(w->timing_output), out);
    return 0;
}

#ifdef DWL_HAVE_PRESENTATION
/* A bracket gives a bounded conversion rather than treating receipt time as
 * presentation time. Repeat it on completion and reject clock discontinuity. */
static bool timing_clock_bracket(int64_t *low, int64_t *high) {
    struct timespec before, source, after;
    if (!g.presentation_clock_known ||
        clock_gettime(CLOCK_MONOTONIC, &before) ||
        clock_gettime(g.presentation_clock, &source) ||
        clock_gettime(CLOCK_MONOTONIC, &after)) return false;
    __int128 a = (__int128)before.tv_sec * 1000000000 + before.tv_nsec;
    __int128 b = (__int128)after.tv_sec * 1000000000 + after.tv_nsec;
    __int128 s = (__int128)source.tv_sec * 1000000000 + source.tv_nsec;
    if (b < a || b - a > 1000000 || a - s < INT64_MIN || b - s > INT64_MAX) return false;
    *low = (int64_t)(a - s); *high = (int64_t)(b - s);
    return true;
}

static void timing_clock_id(void *data UNUSED, struct wp_presentation *presentation UNUSED, uint32_t clock) {
    g.presentation_clock = (clockid_t)clock;
    g.presentation_clock_known = true;
}
static const struct wp_presentation_listener presentation_listener = {.clock_id = timing_clock_id};

static void timing_sync_output(void *data, struct wp_presentation_feedback *feedback UNUSED, struct wl_output *output) {
    struct presentation_pending *pending = data;
    for (size_t i = 0; i < g.output_count; ++i)
        if (g.outputs[i]->output == output) pending->output_id = g.outputs[i]->identity;
}

static void timing_presented(void *data, struct wp_presentation_feedback *feedback UNUSED,
                             uint32_t sec_hi, uint32_t sec_lo, uint32_t nsec,
                             uint32_t refresh, uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    struct presentation_pending *pending = data;
    struct window *w = pending->window;
    uint64_t now = timing_now_ns();
    uint64_t sequence = ((uint64_t)seq_hi << 32) | seq_lo;
    __uint128_t raw = (__uint128_t)(((uint64_t)sec_hi << 32) | sec_lo) * 1000000000 + nsec;
    if (!pending->committed || pending->generation != w->surface_generation ||
        now >= pending->expires_ns || nsec >= 1000000000 || raw > UINT64_MAX) {
        timing_drop_feedback(pending); return;
    }
    uint64_t timestamp = (uint64_t)raw;
    int64_t low, high;
    bool clock_valid = pending->clock_valid && timing_clock_bracket(&low, &high);
    if (clock_valid) {
        if (low < pending->clock_low) low = pending->clock_low;
        if (high > pending->clock_high) high = pending->clock_high;
        __int128 converted = (__int128)timestamp + low + (high - (__int128)low) / 2;
        clock_valid = low <= high && converted >= 0 && converted <= UINT64_MAX;
        if (clock_valid) timestamp = (uint64_t)converted;
    }
    /* Receipt delay does not alter phase. A future timestamp or clock reset
     * is retained as an observation without granting scheduling eligibility. */
    if (clock_valid && (timestamp > now || (__uint128_t)timestamp + 1000000000ull < pending->submitted_ns)) clock_valid = false;
    uint64_t output_id = timing_output(pending->output_id) ? pending->output_id : w->timing_output;
    uint32_t timing_flags = DWL_TIMING_PRESENTATION;
    if (clock_valid) timing_flags |= DWL_TIMING_CLOCK_MONOTONIC;
    if (sequence) timing_flags |= DWL_TIMING_COUNTER;
    if (flags & WP_PRESENTATION_FEEDBACK_KIND_VSYNC) timing_flags |= DWL_TIMING_VSYNC;
    /* v1 requires refresh=0 for variable rate; v2 allows a fastest interval
     * with no way to distinguish it. Only v3's fixed_rate is authoritative. */
    if (refresh && ((g.presentation_version == 1) ||
        (g.presentation_version >= 3 && (flags & 0x10) && !(flags & 0x20))))
        timing_flags |= DWL_TIMING_FIXED;
    if (refresh && g.presentation_version >= 3 && (flags & 0x20) && !(flags & 0x10))
        timing_flags |= DWL_TIMING_VARIABLE;
    if (w->observation.flags & DWL_TIMING_PRESENTATION) {
        if ((clock_valid && (w->observation.flags & DWL_TIMING_CLOCK_MONOTONIC) &&
             timestamp <= w->observation.presentation_ns) ||
            (sequence && w->observation.counter && output_id == w->observation.output_id &&
             sequence <= w->observation.counter)) {
            timing_drop_feedback(pending); return;
        }
    }
    /* Detach before reassociation: changing output cancels all other feedback. */
    timing_drop_feedback(pending);
    timing_reassociate(w, output_id);
    w->observation = (struct dwl_timing){.version = 1, .flags = timing_flags,
        .output_id = w->timing_output, .surface_generation = w->surface_generation,
        .presentation_ns = timestamp, .counter = sequence, .refresh_ns = refresh,
        .presentation_version = g.presentation_version};
}

static void timing_discarded(void *data, struct wp_presentation_feedback *feedback UNUSED) {
    timing_drop_feedback(data);
}
static const struct wp_presentation_feedback_listener timing_feedback_listener = {
    .sync_output = timing_sync_output, .presented = timing_presented, .discarded = timing_discarded,
};
#endif

static uint64_t dwl_presentation_begin(int32_t window, uint64_t generation) {
    struct window *w = find_window(window);
    if (!w || !w->surface || !w->egl || g.dead || w->presentation_token || generation != w->surface_generation) return 0;
    uint64_t now = timing_now_ns();
    timing_expire(w, now);
    w->presentation_token = ++next_presentation_token;
#ifdef DWL_HAVE_PRESENTATION
    if (g.presentation && w->feedback_count < 8) {
        struct presentation_pending *pending = calloc(1, sizeof *pending);
        if (pending) {
            struct output *out = timing_output(w->timing_output);
            uint64_t period = out && out->timing_refresh > 0 ? 1000000000000ull / out->timing_refresh : 16666667;
            uint64_t lifetime = period * 8 > 1000000000ull ? period * 8 : 1000000000ull;
            pending->window = w; pending->token = w->presentation_token;
            pending->generation = w->surface_generation;
            pending->submitted_ns = now; pending->expires_ns = now + lifetime;
            pending->clock_valid = timing_clock_bracket(&pending->clock_low, &pending->clock_high);
            pending->feedback = wp_presentation_feedback(g.presentation, w->surface);
            if (pending->feedback) {
                pending->next = w->feedback; w->feedback = pending; ++w->feedback_count;
                wp_presentation_feedback_add_listener(pending->feedback, &timing_feedback_listener, pending);
            } else free(pending);
        }
    }
#endif
    return w->presentation_token;
}

static void dwl_presentation_end(int32_t window, uint64_t token, int success) {
    struct window *w = find_window(window);
    if (!w || !token || w->presentation_token != token) return;
    struct presentation_pending *pending = w->feedback;
    while (pending && pending->token != token) pending = pending->next;
    if (pending) {
        if (success) pending->committed = true;
        else timing_drop_feedback(pending);
    }
    w->presentation_token = 0;
    if (w->deferred_commit) {
        w->deferred_commit = false;
        /* Successful EGL swap already committed the pending surface state. */
        if (!success) window_commit(w);
    }
}

static void dwl_window_invalidate_timing(int32_t window) {
    struct window *w = find_window(window);
    if (w) timing_invalidate(w);
}

/* ------------------------------------------------------------------ cursor */

static void cursor_entry_free(struct cursor_entry *c) {
    if (c->buffer) wl_buffer_destroy(c->buffer);
    if (c->data) munmap(c->data, c->size);
    memset(c, 0, sizeof *c);
}

static void apply_cursor(void) {
    if (!g.pointer || !g.ptr_focus || !g.compositor || g.cursor_kind == CURSOR_NONE) return;
    uint32_t serial = g.ptr_enter_serial;
    if (g.applied_valid && g.applied_serial == serial && g.applied_kind == g.cursor_kind &&
        (g.cursor_kind == CURSOR_HIDDEN || g.applied_key == g.cursor_key))
        return;
    if (g.cursor_kind == CURSOR_HIDDEN || !g.cursor_current) {
        wl_pointer_set_cursor(g.pointer, serial, NULL, 0, 0);
    } else {
        struct cursor_entry *c = g.cursor_current;
        if (!g.cursor_surface) g.cursor_surface = wl_compositor_create_surface(g.compositor);
        if (!g.cursor_surface) return;
        wl_surface_attach(g.cursor_surface, c->buffer, 0, 0);
        wl_surface_damage_buffer(g.cursor_surface, 0, 0, c->width, c->height);
        wl_surface_commit(g.cursor_surface);
        wl_pointer_set_cursor(g.pointer, serial, g.cursor_surface, c->hot_x, c->hot_y);
    }
    g.applied_valid = true;
    g.applied_serial = serial;
    g.applied_kind = g.cursor_kind;
    g.applied_key = g.cursor_key;
}

static struct cursor_entry *cursor_lookup(uint64_t key) {
    for (int i = 0; i < CURSOR_CACHE; i++)
        if (g.cursors[i].used && g.cursors[i].key == key) return &g.cursors[i];
    return NULL;
}

static struct shmbuf_raw {
    struct wl_buffer *buffer;
    void *data;
    size_t size;
} shm_alloc(int32_t width, int32_t height, int32_t stride) {
    struct shmbuf_raw r = {NULL, NULL, 0};
    size_t size = (size_t)stride * (size_t)height;
    if (!g.shm || width <= 0 || height <= 0 || size == 0) return r;
    int fd = memfd_create("darling-wayland", MFD_CLOEXEC);
    if (fd < 0) return r;
    if (ftruncate(fd, (off_t)size) < 0) {
        close(fd);
        return r;
    }
    void *data = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (data == MAP_FAILED) {
        close(fd);
        return r;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(g.shm, fd, (int32_t)size);
    r.buffer = wl_shm_pool_create_buffer(pool, 0, width, height, stride, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    close(fd);
    r.data = data;
    r.size = size;
    return r;
}

static void copy_rows(void *dst, int32_t dst_stride, const void *src, int32_t src_stride, int32_t width,
                      int32_t height) {
    for (int32_t y = 0; y < height; y++)
        memcpy((char *)dst + (size_t)y * dst_stride, (const char *)src + (size_t)y * src_stride,
               (size_t)width * 4);
}

static void dwl_cursor_set_image(int32_t window, uint64_t key, const void *pixels, int32_t width,
                                 int32_t height, int32_t stride, int32_t hot_x, int32_t hot_y) {
    (void)window;
    if (!g.display) return;
    if (width <= 0 || height <= 0 || !pixels) {
        g.cursor_kind = CURSOR_HIDDEN;
        g.cursor_current = NULL;
        apply_cursor();
        flush_best_effort();
        return;
    }
    struct cursor_entry *c = cursor_lookup(key);
    if (!c) {
        struct cursor_entry *victim = NULL;
        for (int i = 0; i < CURSOR_CACHE; i++) {
            struct cursor_entry *e = &g.cursors[i];
            if (!e->used) {
                victim = e;
                break;
            }
            if (g.cursor_current == e) continue;
            if (!victim || e->stamp < victim->stamp) victim = e;
        }
        if (!victim) return;
        cursor_entry_free(victim);
        struct shmbuf_raw r = shm_alloc(width, height, width * 4);
        if (!r.buffer) return;
        copy_rows(r.data, width * 4, pixels, stride, width, height);
        victim->used = true;
        victim->key = key;
        victim->buffer = r.buffer;
        victim->data = r.data;
        victim->size = r.size;
        victim->width = width;
        victim->height = height;
        c = victim;
    }
    c->hot_x = hot_x;
    c->hot_y = hot_y;
    c->stamp = ++g.cursor_stamp;
    g.cursor_kind = CURSOR_IMAGE;
    g.cursor_key = key;
    g.cursor_current = c;
    apply_cursor();
    flush_best_effort();
}

static void dwl_cursor_hide(int32_t window) {
    (void)window;
    if (!g.display) return;
    g.cursor_kind = CURSOR_HIDDEN;
    g.cursor_current = NULL;
    apply_cursor();
    flush_best_effort();
}

/* ------------------------------------------------------------------ constraints */

static void locked_locked(void *d UNUSED, struct zwp_locked_pointer_v1 *l UNUSED) {}
static void locked_unlocked(void *data, struct zwp_locked_pointer_v1 *l) {
    struct window *w = data;
    /* Persistent lifetime survives, but re-create on the next pointer enter so the state is known. */
    if (w->locked == l) {
        zwp_locked_pointer_v1_destroy(l);
        w->locked = NULL;
    }
}
static const struct zwp_locked_pointer_v1_listener locked_listener = {
    .locked = locked_locked,
    .unlocked = locked_unlocked,
};

static void confined_confined(void *d UNUSED, struct zwp_confined_pointer_v1 *c UNUSED) {}
static void confined_unconfined(void *data, struct zwp_confined_pointer_v1 *c) {
    struct window *w = data;
    if (w->confined == c) {
        zwp_confined_pointer_v1_destroy(c);
        w->confined = NULL;
    }
}
static const struct zwp_confined_pointer_v1_listener confined_listener = {
    .confined = confined_confined,
    .unconfined = confined_unconfined,
};

static void window_arm_constraints(struct window *w) {
    if (!g.constraints || !g.pointer) return;
    if (w->want_lock) {
        if (w->confined) {
            zwp_confined_pointer_v1_destroy(w->confined);
            w->confined = NULL;
        }
        if (!w->locked) {
            w->locked = zwp_pointer_constraints_v1_lock_pointer(
                g.constraints, w->surface, g.pointer, NULL, ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT);
            if (w->locked) zwp_locked_pointer_v1_add_listener(w->locked, &locked_listener, w);
        }
    } else {
        if (w->locked) {
            zwp_locked_pointer_v1_destroy(w->locked);
            w->locked = NULL;
        }
        if (w->want_confine) {
            if (!w->confined) {
                w->confined = zwp_pointer_constraints_v1_confine_pointer(
                    g.constraints, w->surface, g.pointer, NULL, ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT);
                if (w->confined) zwp_confined_pointer_v1_add_listener(w->confined, &confined_listener, w);
            }
        } else if (w->confined) {
            zwp_confined_pointer_v1_destroy(w->confined);
            w->confined = NULL;
        }
    }
}

static void dwl_pointer_lock(int32_t window, int lock) {
    struct window *w = find_window(window);
    if (!w) return;
    w->want_lock = lock != 0;
    window_arm_constraints(w);
}

static void dwl_pointer_confine(int32_t window, int confine) {
    struct window *w = find_window(window);
    if (!w) return;
    w->want_confine = confine != 0;
    window_arm_constraints(w);
}

static int dwl_pointer_warp(int32_t window, double x, double y) {
    struct window *w = find_window(window);
    if (!w || !g.display) return -1;
    if (w->locked) {
        zwp_locked_pointer_v1_set_cursor_position_hint(w->locked, wl_fixed_from_double(x), wl_fixed_from_double(y));
        window_commit(w);
        return 0;
    }
#ifdef DWL_HAVE_POINTER_WARP
    if (g.warp && g.pointer && g.ptr_focus == w && w->enter_serial) {
        wp_pointer_warp_v1_warp_pointer(g.warp, w->surface, g.pointer, wl_fixed_from_double(x),
                                        wl_fixed_from_double(y), w->enter_serial);
        return 0;
    }
#endif
    return -1;
}

/* ------------------------------------------------------------------ relative pointer */

static void relative_motion(void *d UNUSED, struct zwp_relative_pointer_v1 *rp UNUSED, uint32_t hi, uint32_t lo,
                            wl_fixed_t dx, wl_fixed_t dy, wl_fixed_t dxu, wl_fixed_t dyu) {
    if (!g.ptr_focus) return;
    struct dwl_event e = make_event(DWL_EVENT_RELATIVE_MOTION, g.ptr_focus);
    e.dx = wl_fixed_to_double(dx);
    e.dy = wl_fixed_to_double(dy);
    e.dx_unaccel = wl_fixed_to_double(dxu);
    e.dy_unaccel = wl_fixed_to_double(dyu);
    e.time_ms = (uint32_t)((((uint64_t)hi << 32) | lo) / 1000u);
    queue_event(&e);
}
static const struct zwp_relative_pointer_v1_listener relative_listener = {.relative_motion = relative_motion};

static void ensure_relative(void) {
    if (g.relative || !g.relative_manager || !g.pointer) return;
    g.relative = zwp_relative_pointer_manager_v1_get_relative_pointer(g.relative_manager, g.pointer);
    if (g.relative) zwp_relative_pointer_v1_add_listener(g.relative, &relative_listener, NULL);
}

/* ------------------------------------------------------------------ keyboard */

static void emit_key(struct window *w, uint32_t evdev, bool pressed, bool repeat, uint32_t time_ms) {
    struct dwl_event e = make_event(DWL_EVENT_KEY, w);
    uint32_t keycode = evdev + 8;
    e.keycode = keycode;
    e.pressed = pressed;
    e.repeat = repeat;
    e.time_ms = time_ms;
    e.mods = g.mods;
    if (g.xkb_state && g.keymap) {
        e.keysym = xkb_state_key_get_one_sym(g.xkb_state, keycode);
        xkb_layout_index_t layout = xkb_state_key_get_layout(g.xkb_state, keycode);
        const xkb_keysym_t *syms = NULL;
        int n = xkb_keymap_key_get_syms_by_level(g.keymap, keycode, layout, 0, &syms);
        e.keysym_base = n > 0 ? syms[0] : 0;
        if (pressed) e.utf32 = xkb_state_key_get_utf32(g.xkb_state, keycode);
    }
    queue_event(&e);
}

static uint32_t compute_mods(void) {
    if (!g.xkb_state) return 0;
    static const struct {
        const char *name;
        uint32_t bit;
    } map[] = {
        {XKB_MOD_NAME_SHIFT, DWL_MOD_SHIFT}, {XKB_MOD_NAME_CAPS, DWL_MOD_CAPS}, {XKB_MOD_NAME_CTRL, DWL_MOD_CTRL},
        {XKB_MOD_NAME_ALT, DWL_MOD_ALT},     {XKB_MOD_NAME_NUM, DWL_MOD_NUM},   {XKB_MOD_NAME_LOGO, DWL_MOD_SUPER},
    };
    uint32_t mods = 0;
    for (size_t i = 0; i < sizeof map / sizeof *map; i++)
        if (xkb_state_mod_name_is_active(g.xkb_state, map[i].name, XKB_STATE_MODS_EFFECTIVE) > 0) mods |= map[i].bit;
    return mods;
}

static void kb_keymap(void *d UNUSED, struct wl_keyboard *k UNUSED, uint32_t format, int32_t fd, uint32_t size) {
    if (format == WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 && g.xkb && size > 0) {
        char *map = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (map != MAP_FAILED) {
            struct xkb_keymap *km =
                xkb_keymap_new_from_string(g.xkb, map, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
            munmap(map, size);
            struct xkb_state *st = km ? xkb_state_new(km) : NULL;
            if (st) {
                xkb_state_unref(g.xkb_state);
                xkb_keymap_unref(g.keymap);
                g.keymap = km;
                g.xkb_state = st;
                g.mods = 0;
            } else {
                if (km) xkb_keymap_unref(km);
                LOG("keymap from compositor could not be compiled");
            }
        }
    }
    close(fd);
}

static void stop_repeat(void) { g.rep_active = false; g.rep_window = NULL; }

static void kb_enter(void *d UNUSED, struct wl_keyboard *k UNUSED, uint32_t serial, struct wl_surface *surface,
                     struct wl_array *keys UNUSED) {
    struct window *w = window_of_surface(surface);
    if (!w) return;
    g.kbd_focus = w;
    w->input_serial = serial;
    w->have_input_serial = true;
    struct dwl_event e = make_event(DWL_EVENT_KEYBOARD_ENTER, w);
    e.mods = g.mods;
    queue_event(&e);
    if (w->act_pending) {
        w->act_pending = false;
        struct dwl_event done = make_event(DWL_EVENT_ACTIVATION_DONE, w);
        done.pressed = 1;
        queue_event(&done);
        LOG("activation token=%s\xe2\x80\xa6 result=focused", w->act_token);
    }
    if (g.ptr_focus == w) window_arm_constraints(w);
}

static void kb_leave(void *d UNUSED, struct wl_keyboard *k UNUSED, uint32_t serial UNUSED,
                     struct wl_surface *surface) {
    struct window *w = window_of_surface(surface);
    if (!w) return;
    if (g.kbd_focus == w) g.kbd_focus = NULL;
    stop_repeat();
    queue_event(&(struct dwl_event){.type = DWL_EVENT_KEYBOARD_LEAVE, .window = w->id});
}

static void kb_key(void *d UNUSED, struct wl_keyboard *k UNUSED, uint32_t serial, uint32_t time, uint32_t key,
                   uint32_t state) {
    struct window *w = g.kbd_focus;
    if (!w) return;
    bool pressed = state == WL_KEYBOARD_KEY_STATE_PRESSED;
    w->input_serial = serial;
    w->have_input_serial = true;
    g.grab_serial = serial;
    g.have_grab_serial = true;
    emit_key(w, key, pressed, false, time);
    if (pressed) {
        maybe_mint_handoff_token(w, serial);
        if (g.keymap && g.rep_rate > 0 && xkb_keymap_key_repeats(g.keymap, key + 8)) {
            g.rep_active = true;
            g.rep_window = w;
            g.rep_key = key;
            g.rep_next = now_ms() + g.rep_delay;
        }
    } else if (g.rep_active && g.rep_key == key) {
        stop_repeat();
    }
}

static void kb_modifiers(void *d UNUSED, struct wl_keyboard *k UNUSED, uint32_t serial UNUSED, uint32_t depressed,
                         uint32_t latched, uint32_t locked, uint32_t group) {
    if (!g.xkb_state) return;
    xkb_state_update_mask(g.xkb_state, depressed, latched, locked, 0, 0, group);
    g.mods = compute_mods();
    if (g.kbd_focus) {
        struct dwl_event e = make_event(DWL_EVENT_MODIFIERS, g.kbd_focus);
        e.mods = g.mods;
        queue_event(&e);
    }
}

static void kb_repeat_info(void *d UNUSED, struct wl_keyboard *k UNUSED, int32_t rate, int32_t delay) {
    g.rep_rate = rate;
    g.rep_delay = delay;
    if (rate <= 0) stop_repeat();
}

static const struct wl_keyboard_listener keyboard_listener = {
    .keymap = kb_keymap,
    .enter = kb_enter,
    .leave = kb_leave,
    .key = kb_key,
    .modifiers = kb_modifiers,
    .repeat_info = kb_repeat_info,
};

/* ------------------------------------------------------------------ pointer */

static void flush_axes(void) {
    for (int i = 0; i < 2; i++) {
        struct axis_pending *a = &g.axes[i];
        if (!a->have) continue;
        a->have = false;
        if (g.ptr_focus && (a->value != 0.0 || a->value120 != 0)) {
            struct dwl_event e = make_event(DWL_EVENT_POINTER_AXIS, g.ptr_focus);
            e.axis = i;
            e.value = a->value;
            e.value120 = a->value120;
            e.time_ms = a->time;
            queue_event(&e);
        }
        a->value = 0;
        a->value120 = 0;
    }
}

static void ptr_enter(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t serial, struct wl_surface *surface,
                      wl_fixed_t sx, wl_fixed_t sy) {
    struct window *w = window_of_surface(surface);
    if (!w) return;
    g.ptr_focus = w;
    g.ptr_enter_serial = serial;
    w->enter_serial = serial;
    w->last_x = wl_fixed_to_double(sx);
    w->last_y = wl_fixed_to_double(sy);
    g.applied_valid = false;
    apply_cursor();
    window_arm_constraints(w);
    struct dwl_event e = make_event(DWL_EVENT_POINTER_ENTER, w);
    e.x = w->last_x;
    e.y = w->last_y;
    queue_event(&e);
}

static void ptr_leave(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t serial UNUSED, struct wl_surface *surface) {
    struct window *w = window_of_surface(surface);
    if (!w) return;
    if (g.ptr_focus == w) g.ptr_focus = NULL;
    queue_event(&(struct dwl_event){.type = DWL_EVENT_POINTER_LEAVE, .window = w->id});
}

static void ptr_motion(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t time, wl_fixed_t sx, wl_fixed_t sy) {
    struct window *w = g.ptr_focus;
    if (!w) return;
    w->last_x = wl_fixed_to_double(sx);
    w->last_y = wl_fixed_to_double(sy);
    struct dwl_event e = make_event(DWL_EVENT_POINTER_MOTION, w);
    e.x = w->last_x;
    e.y = w->last_y;
    e.time_ms = time;
    queue_event(&e);
}

static void ptr_button(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t serial, uint32_t time, uint32_t button,
                       uint32_t state) {
    struct window *w = g.ptr_focus;
    if (!w) return;
    w->input_serial = serial;
    w->have_input_serial = true;
    g.grab_serial = serial;
    g.have_grab_serial = true;
    if (state == WL_POINTER_BUTTON_STATE_PRESSED) {
        w->button_held = true;
        w->button_serial = serial;
        w->button_code = button;
        maybe_mint_handoff_token(w, serial);
    } else if (w->button_code == button) {
        w->button_held = false;
    }
    struct dwl_event e = make_event(DWL_EVENT_POINTER_BUTTON, w);
    e.button = button;
    e.pressed = state == WL_POINTER_BUTTON_STATE_PRESSED;
    e.time_ms = time;
    e.x = w->last_x;
    e.y = w->last_y;
    queue_event(&e);
}

static void ptr_axis(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t time, uint32_t axis, wl_fixed_t value) {
    if (axis > 1) return;
    g.axes[axis].have = true;
    g.axes[axis].value = wl_fixed_to_double(value);
    g.axes[axis].time = time;
    if (g.seat_version < WL_POINTER_FRAME_SINCE_VERSION) flush_axes();
}

static void ptr_frame(void *d UNUSED, struct wl_pointer *p UNUSED) { flush_axes(); }
static void ptr_axis_source(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t s UNUSED) {}
static void ptr_axis_stop(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t t UNUSED, uint32_t a UNUSED) {}
static void ptr_axis_discrete(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t axis, int32_t discrete) {
    if (axis > 1 || g.axes[axis].value120) return;
    g.axes[axis].have = true;
    g.axes[axis].value120 = discrete * 120;
}
static void ptr_axis_value120(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t axis, int32_t v120) {
    if (axis > 1) return;
    g.axes[axis].have = true;
    g.axes[axis].value120 = v120;
}
static void ptr_axis_direction(void *d UNUSED, struct wl_pointer *p UNUSED, uint32_t a UNUSED, uint32_t dir UNUSED) {}

static const struct wl_pointer_listener pointer_listener = {
    .enter = ptr_enter,
    .leave = ptr_leave,
    .motion = ptr_motion,
    .button = ptr_button,
    .axis = ptr_axis,
    .frame = ptr_frame,
    .axis_source = ptr_axis_source,
    .axis_stop = ptr_axis_stop,
    .axis_discrete = ptr_axis_discrete,
    .axis_value120 = ptr_axis_value120,
    .axis_relative_direction = ptr_axis_direction,
};

static void seat_capabilities(void *d UNUSED, struct wl_seat *seat, uint32_t caps) {
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !g.pointer) {
        g.pointer = wl_seat_get_pointer(seat);
        wl_pointer_add_listener(g.pointer, &pointer_listener, NULL);
        ensure_relative();
        for (size_t i = 0; i < g.window_count; i++) window_arm_constraints(g.windows[i]);
    } else if (!(caps & WL_SEAT_CAPABILITY_POINTER) && g.pointer) {
        if (g.relative) zwp_relative_pointer_v1_destroy(g.relative);
        g.relative = NULL;
        for (size_t i = 0; i < g.window_count; i++) {
            struct window *w = g.windows[i];
            if (w->locked) zwp_locked_pointer_v1_destroy(w->locked);
            if (w->confined) zwp_confined_pointer_v1_destroy(w->confined);
            w->locked = NULL;
            w->confined = NULL;
        }
        if (wl_pointer_get_version(g.pointer) >= WL_POINTER_RELEASE_SINCE_VERSION)
            wl_pointer_release(g.pointer);
        else
            wl_pointer_destroy(g.pointer);
        g.pointer = NULL;
        g.ptr_focus = NULL;
    }
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !g.keyboard) {
        g.keyboard = wl_seat_get_keyboard(seat);
        wl_keyboard_add_listener(g.keyboard, &keyboard_listener, NULL);
    } else if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD) && g.keyboard) {
        if (wl_keyboard_get_version(g.keyboard) >= WL_KEYBOARD_RELEASE_SINCE_VERSION)
            wl_keyboard_release(g.keyboard);
        else
            wl_keyboard_destroy(g.keyboard);
        g.keyboard = NULL;
        g.kbd_focus = NULL;
        stop_repeat();
    }
}
static void seat_name(void *d UNUSED, struct wl_seat *s UNUSED, const char *n UNUSED) {}
static const struct wl_seat_listener seat_listener = {.capabilities = seat_capabilities, .name = seat_name};

/* ------------------------------------------------------------------ outputs */

static void queue_outputs_changed(void) { queue_event(&(struct dwl_event){.type = DWL_EVENT_OUTPUTS_CHANGED}); }

static void out_geometry(void *data, struct wl_output *o UNUSED, int32_t x, int32_t y, int32_t pw UNUSED,
                         int32_t ph UNUSED, int32_t subpixel UNUSED, const char *make UNUSED, const char *model UNUSED,
                         int32_t transform) {
    struct output *out = data;
    out->geo_x = x;
    out->geo_y = y;
    out->transform = transform;
}
static void out_done(void *data, struct wl_output *o);

static void out_mode(void *data, struct wl_output *o UNUSED, uint32_t flags, int32_t w, int32_t h, int32_t refresh) {
    struct output *out = data;
    if (!(flags & WL_OUTPUT_MODE_CURRENT)) return;
    out->pixel_width = w;
    out->pixel_height = h;
    out->refresh = refresh;
    if (wl_output_get_version(o) < 2) out_done(out, o);
}
static void out_done(void *data, struct wl_output *o UNUSED) {
    struct output *out = data;
    out->timing_refresh = out->refresh;
    for (size_t i = 0; i < g.window_count; ++i) {
        struct window *w = g.windows[i];
        if (w->timing_output == out->identity) timing_invalidate(w);
        timing_reassociate(w, 0);
    }
    queue_outputs_changed();
}
static void out_scale(void *data, struct wl_output *o UNUSED, int32_t factor) {
    ((struct output *)data)->scale = factor;
}
static void out_name(void *data, struct wl_output *o UNUSED, const char *name) {
    struct output *out = data;
    if (!out->name_str[0]) snprintf(out->name_str, sizeof out->name_str, "%s", name);
}
static void out_description(void *d UNUSED, struct wl_output *o UNUSED, const char *desc UNUSED) {}
static const struct wl_output_listener output_listener = {
    .geometry = out_geometry,
    .mode = out_mode,
    .done = out_done,
    .scale = out_scale,
    .name = out_name,
    .description = out_description,
};

static void xo_position(void *data, struct zxdg_output_v1 *x UNUSED, int32_t px, int32_t py) {
    struct output *out = data;
    out->xdg_x = px;
    out->xdg_y = py;
    out->has_xdg_pos = true;
}
static void xo_size(void *data, struct zxdg_output_v1 *x UNUSED, int32_t w, int32_t h) {
    struct output *out = data;
    out->xdg_width = w;
    out->xdg_height = h;
    out->has_xdg_size = true;
}
static void xo_done(void *d UNUSED, struct zxdg_output_v1 *x UNUSED) {
    /* Deprecated in version 3; wl_output.done follows. Earlier versions only have this. */
    if (g.xdg_output_manager && zxdg_output_manager_v1_get_version(g.xdg_output_manager) < 3) queue_outputs_changed();
}
static void xo_name(void *data, struct zxdg_output_v1 *x UNUSED, const char *name) {
    struct output *out = data;
    snprintf(out->name_str, sizeof out->name_str, "%s", name);
}
static void xo_description(void *d UNUSED, struct zxdg_output_v1 *x UNUSED, const char *desc UNUSED) {}
static const struct zxdg_output_v1_listener xdg_output_listener = {
    .logical_position = xo_position,
    .logical_size = xo_size,
    .done = xo_done,
    .name = xo_name,
    .description = xo_description,
};

static void output_attach_xdg(struct output *out) {
    if (!g.xdg_output_manager || out->xdg) return;
    out->xdg = zxdg_output_manager_v1_get_xdg_output(g.xdg_output_manager, out->output);
    if (out->xdg) zxdg_output_v1_add_listener(out->xdg, &xdg_output_listener, out);
}

static void output_free(struct output *out) {
    if (out->xdg) zxdg_output_v1_destroy(out->xdg);
    if (out->output) {
        if (wl_output_get_version(out->output) >= WL_OUTPUT_RELEASE_SINCE_VERSION)
            wl_output_release(out->output);
        else
            wl_output_destroy(out->output);
    }
    free(out);
}

/* ------------------------------------------------------------------ registry */

static void remember_advertised(const char *iface) {
    char **list = realloc(g.advertised, (g.advertised_count + 1) * sizeof *list);
    if (!list) return;
    g.advertised = list;
    char *copy = strdup(iface);
    if (copy) g.advertised[g.advertised_count++] = copy;
}

static void registry_global(void *d UNUSED, struct wl_registry *reg, uint32_t name, const char *iface,
                            uint32_t version) {
    remember_advertised(iface);
    if (!strcmp(iface, wl_compositor_interface.name)) {
        if (version >= 4) g.compositor = bind_global(reg, name, &wl_compositor_interface, version, 6);
    } else if (!strcmp(iface, wl_shm_interface.name)) {
        g.shm = bind_global(reg, name, &wl_shm_interface, version, 1);
    } else if (!strcmp(iface, wl_seat_interface.name)) {
        if (!g.seat) {
            g.seat = bind_global(reg, name, &wl_seat_interface, version, 8);
            g.seat_version = wl_seat_get_version(g.seat);
            wl_seat_add_listener(g.seat, &seat_listener, NULL);
        }
    } else if (!strcmp(iface, xdg_wm_base_interface.name)) {
        g.wm_base = bind_global(reg, name, &xdg_wm_base_interface, version, 6);
    } else if (!strcmp(iface, xdg_activation_v1_interface.name)) {
        g.activation = bind_global(reg, name, &xdg_activation_v1_interface, version, 1);
    } else if (!strcmp(iface, zwp_pointer_constraints_v1_interface.name)) {
        g.constraints = bind_global(reg, name, &zwp_pointer_constraints_v1_interface, version, 1);
    } else if (!strcmp(iface, zwp_relative_pointer_manager_v1_interface.name)) {
        g.relative_manager = bind_global(reg, name, &zwp_relative_pointer_manager_v1_interface, version, 1);
        ensure_relative();
#ifdef DWL_HAVE_POINTER_WARP
    } else if (!strcmp(iface, wp_pointer_warp_v1_interface.name)) {
        g.warp = bind_global(reg, name, &wp_pointer_warp_v1_interface, version, 1);
#endif
    } else if (!strcmp(iface, wp_viewporter_interface.name)) {
        g.viewporter = bind_global(reg, name, &wp_viewporter_interface, version, 1);
    } else if (!strcmp(iface, zxdg_output_manager_v1_interface.name)) {
        g.xdg_output_manager = bind_global(reg, name, &zxdg_output_manager_v1_interface, version, 3);
        for (size_t i = 0; i < g.output_count; i++) output_attach_xdg(g.outputs[i]);
    } else if (!strcmp(iface, zwp_idle_inhibit_manager_v1_interface.name)) {
        g.idle_manager = bind_global(reg, name, &zwp_idle_inhibit_manager_v1_interface, version, 1);
#ifdef DWL_HAVE_PRESENTATION
    } else if (!strcmp(iface, wp_presentation_interface.name)) {
        g.presentation = bind_global(reg, name, &wp_presentation_interface, version, 3);
        g.presentation_name = name;
        g.presentation_version = wp_presentation_get_version(g.presentation);
        wp_presentation_add_listener(g.presentation, &presentation_listener, NULL);
#endif
    } else if (!strcmp(iface, wl_output_interface.name)) {
        struct output *out = calloc(1, sizeof *out);
        struct output **list = realloc(g.outputs, (g.output_count + 1) * sizeof *list);
        if (!out || !list) {
            free(out);
            if (list) g.outputs = list;
            return;
        }
        g.outputs = list;
        out->name = name;
        out->identity = (connection_identity << 32) | name;
        out->scale = 1;
        out->output = bind_global(reg, name, &wl_output_interface, version, 4);
        wl_output_add_listener(out->output, &output_listener, out);
        output_attach_xdg(out);
        g.outputs[g.output_count++] = out;
    }
}

static void registry_global_remove(void *d UNUSED, struct wl_registry *reg UNUSED, uint32_t name) {
#ifdef DWL_HAVE_PRESENTATION
    if (g.presentation && g.presentation_name == name) {
        for (size_t i = 0; i < g.window_count; ++i) timing_invalidate(g.windows[i]);
        wp_presentation_destroy(g.presentation); g.presentation = NULL;
        g.presentation_clock_known = false;
    }
#endif
    for (size_t i = 0; i < g.output_count; i++) {
        if (g.outputs[i]->name != name) continue;
        struct output *removed = g.outputs[i];
        for (size_t j = 0; j < g.window_count; ++j) {
            /* A pending sync_output may name this output even before the
             * selected output changes. Cancel it before freeing the global. */
            timing_invalidate(g.windows[j]);
            timing_surface_leave(g.windows[j], g.windows[j]->surface, removed->output);
        }
        output_free(removed);
        memmove(&g.outputs[i], &g.outputs[i + 1], (g.output_count - i - 1) * sizeof *g.outputs);
        g.output_count--;
        for (size_t j = 0; j < g.window_count; ++j) timing_reassociate(g.windows[j], 0);
        queue_outputs_changed();
        return;
    }
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

/* ------------------------------------------------------------------ xdg-shell */

static void wm_ping(void *d UNUSED, struct xdg_wm_base *wm, uint32_t serial) { xdg_wm_base_pong(wm, serial); }
static const struct xdg_wm_base_listener wm_base_listener = {.ping = wm_ping};

static void xs_configure(void *data, struct xdg_surface *xs, uint32_t serial) {
    struct window *w = data;
    xdg_surface_ack_configure(xs, serial);
    w->configured = true;
    if (w->is_popup) {
        struct dwl_event pe = make_event(DWL_EVENT_POPUP_CONFIGURE, w);
        pe.x = w->pending_px;
        pe.y = w->pending_py;
        pe.width = w->pending_pw;
        pe.height = w->pending_ph;
        queue_event(&pe);
        return;
    }
    struct dwl_event e = make_event(DWL_EVENT_CONFIGURE, w);
    e.width = w->pending_width;
    e.height = w->pending_height;
    e.flags = w->pending_flags;
    queue_event(&e);
}
static const struct xdg_surface_listener xdg_surface_listener = {.configure = xs_configure};

static void tl_configure(void *data, struct xdg_toplevel *tl UNUSED, int32_t width, int32_t height,
                         struct wl_array *states) {
    struct window *w = data;
    uint32_t flags = 0;
    uint32_t *state;
    wl_array_for_each(state, states) {
        switch (*state) {
        case XDG_TOPLEVEL_STATE_FULLSCREEN: flags |= DWL_CONFIGURE_FULLSCREEN; break;
        case XDG_TOPLEVEL_STATE_ACTIVATED: flags |= DWL_CONFIGURE_ACTIVATED; break;
        case XDG_TOPLEVEL_STATE_MAXIMIZED: flags |= DWL_CONFIGURE_MAXIMIZED; break;
#ifdef XDG_TOPLEVEL_STATE_SUSPENDED_SINCE_VERSION
        case XDG_TOPLEVEL_STATE_SUSPENDED: flags |= DWL_CONFIGURE_SUSPENDED; break;
#endif
        default: break;
        }
    }
    w->pending_width = width > 0 ? width : 0;
    w->pending_height = height > 0 ? height : 0;
    w->pending_flags = flags;
}
static void tl_close(void *data, struct xdg_toplevel *tl UNUSED) {
    struct window *w = data;
    queue_event(&(struct dwl_event){.type = DWL_EVENT_CLOSE, .window = w->id});
}
static void tl_bounds(void *d UNUSED, struct xdg_toplevel *tl UNUSED, int32_t w UNUSED, int32_t h UNUSED) {}
static void tl_caps(void *d UNUSED, struct xdg_toplevel *tl UNUSED, struct wl_array *caps UNUSED) {}
static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = tl_configure,
    .close = tl_close,
    .configure_bounds = tl_bounds,
    .wm_capabilities = tl_caps,
};

static void popup_configure(void *data, struct xdg_popup *p UNUSED, int32_t x, int32_t y, int32_t width,
                            int32_t height) {
    struct window *w = data;
    w->pending_px = x;
    w->pending_py = y;
    w->pending_pw = width;
    w->pending_ph = height;
}
static void popup_done(void *data, struct xdg_popup *p UNUSED) {
    struct window *w = data;
    queue_event(&(struct dwl_event){.type = DWL_EVENT_POPUP_DONE, .window = w->id});
}
static void popup_repositioned(void *data UNUSED, struct xdg_popup *p UNUSED, uint32_t token UNUSED) {
    /* The configure that follows carries the new geometry; nothing to do here. */
}
static const struct xdg_popup_listener popup_listener = {
    .configure = popup_configure,
    .popup_done = popup_done,
    .repositioned = popup_repositioned,
};

/* ------------------------------------------------------------------ windows */

static void shmbuf_free(struct shmbuf *b) {
    if (b->buffer) wl_buffer_destroy(b->buffer);
    if (b->data) munmap(b->data, b->size);
    memset(b, 0, sizeof *b);
}

static void buffer_release(void *data, struct wl_buffer *buffer UNUSED) { ((struct shmbuf *)data)->busy = false; }
static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};

static int32_t dwl_window_create(int32_t width, int32_t height, const char *title, const char *app_id) {
    if (!g.display || !g.compositor || !g.wm_base) return 0;
    struct window *w = calloc(1, sizeof *w);
    struct window **list = realloc(g.windows, (g.window_count + 1) * sizeof *list);
    if (!w || !list) {
        free(w);
        if (list) g.windows = list;
        return 0;
    }
    g.windows = list;
    w->id = ++g.next_window_id;
    w->width = width > 0 ? width : 1;
    w->height = height > 0 ? height : 1;
    w->app_id = strdup(app_id ? app_id : "");
    w->surface = wl_compositor_create_surface(g.compositor);
    wl_surface_set_user_data(w->surface, w);
    timing_attach_surface(w);
    w->xdg_surface = xdg_wm_base_get_xdg_surface(g.wm_base, w->surface);
    xdg_surface_add_listener(w->xdg_surface, &xdg_surface_listener, w);
    w->toplevel = xdg_surface_get_toplevel(w->xdg_surface);
    xdg_toplevel_add_listener(w->toplevel, &toplevel_listener, w);
    if (title) xdg_toplevel_set_title(w->toplevel, title);
    if (app_id && *app_id) xdg_toplevel_set_app_id(w->toplevel, app_id);
    if (g.viewporter) w->viewport = wp_viewporter_get_viewport(g.viewporter, w->surface);
    /* The initial commit without a buffer asks the compositor for the first configure. */
    window_commit(w);
    g.windows[g.window_count++] = w;
    flush_best_effort();
    return w->id;
}

static struct window *find_child_popup(const struct window *parent) {
    for (size_t i = 0; i < g.window_count; i++)
        if (g.windows[i]->is_popup && g.windows[i]->parent == parent) return g.windows[i];
    return NULL;
}

static void dwl_window_destroy(int32_t window) {
    struct window *w = find_window(window);
    if (!w) return;
    /* Popups must be destroyed top-down: children first. */
    for (struct window *child; (child = find_child_popup(w)) != NULL;) dwl_window_destroy(child->id);
    for (size_t i = 0; i < g.window_count; i++) {
        if (g.windows[i] == w) {
            memmove(&g.windows[i], &g.windows[i + 1], (g.window_count - i - 1) * sizeof *g.windows);
            g.window_count--;
            break;
        }
    }
    if (g.ptr_focus == w) g.ptr_focus = NULL;
    if (g.kbd_focus == w) g.kbd_focus = NULL;
    if (g.rep_window == w) stop_repeat();
    if (w->locked) zwp_locked_pointer_v1_destroy(w->locked);
    if (w->confined) zwp_confined_pointer_v1_destroy(w->confined);
    if (w->idle) zwp_idle_inhibitor_v1_destroy(w->idle);
    if (w->viewport) wp_viewport_destroy(w->viewport);
    if (w->egl) wl_egl_window_destroy(w->egl);
    for (int i = 0; i < SHM_BUFFERS; i++) shmbuf_free(&w->bufs[i]);
    if (w->popup) xdg_popup_destroy(w->popup);
    if (w->toplevel) xdg_toplevel_destroy(w->toplevel);
    if (w->xdg_surface) xdg_surface_destroy(w->xdg_surface);
    if (w->surface) {
        wl_surface_set_user_data(w->surface, NULL);
        wl_surface_destroy(w->surface);
    }
    timing_invalidate(w);
    free(w->entered);
    free(w->app_id);
    free(w);
    flush_best_effort();
}

static void dwl_window_set_title(int32_t window, const char *title) {
    struct window *w = find_window(window);
    if (w && w->toplevel && title) xdg_toplevel_set_title(w->toplevel, title);
}

static void dwl_window_set_fullscreen(int32_t window, int fullscreen, int32_t output_index) {
    struct window *w = find_window(window);
    if (!w || !w->toplevel) return;
    if (fullscreen) {
        struct wl_output *out = NULL;
        if (output_index >= 0 && (size_t)output_index < g.output_count) out = g.outputs[output_index]->output;
        xdg_toplevel_set_fullscreen(w->toplevel, out);
    } else {
        xdg_toplevel_unset_fullscreen(w->toplevel);
    }
    flush_best_effort();
}

static void dwl_window_set_min_size(int32_t window, int32_t width, int32_t height) {
    struct window *w = find_window(window);
    if (w && w->toplevel) xdg_toplevel_set_min_size(w->toplevel, width, height);
}

static void window_update_opaque_region(struct window *w) {
    if (!g.compositor || !w || !w->surface || !w->egl) return;
    int32_t ow = (w->vp_width > 0 && w->vp_height > 0) ? w->vp_width : w->width;
    int32_t oh = (w->vp_width > 0 && w->vp_height > 0) ? w->vp_height : w->height;
    if (ow <= 0 || oh <= 0) return;
    struct wl_region *r = wl_compositor_create_region(g.compositor);
    if (!r) return;
    wl_region_add(r, 0, 0, ow, oh);
    wl_surface_set_opaque_region(w->surface, r);
    wl_region_destroy(r);
}

static void dwl_window_set_size(int32_t window, int32_t width, int32_t height) {
    struct window *w = find_window(window);
    if (!w || width <= 0 || height <= 0) return;
    w->width = width;
    w->height = height;
    if (w->egl) wl_egl_window_resize(w->egl, width, height, 0, 0);
    window_update_opaque_region(w);
    flush_best_effort();
}

static void dwl_window_set_viewport(int32_t window, int32_t dest_width, int32_t dest_height) {
    struct window *w = find_window(window);
    if (!w || !w->viewport) return;
    if (dest_width > 0 && dest_height > 0) {
        w->vp_width = dest_width;
        w->vp_height = dest_height;
        wp_viewport_set_destination(w->viewport, dest_width, dest_height);
    } else {
        w->vp_width = 0;
        w->vp_height = 0;
        wp_viewport_set_destination(w->viewport, -1, -1);
    }
    window_update_opaque_region(w);
    flush_best_effort();
}

/* A buffer may only be attached after the first configure was acknowledged. */
static void wait_configured(struct window *w) {
    for (int i = 0; i < 4 && !w->configured && !g.dead; i++)
        if (wl_display_roundtrip(g.display) < 0) g.dead = true;
}

static void *dwl_window_egl_window(int32_t window) {
    struct window *w = find_window(window);
    if (!w) return NULL;
    if (!w->egl) {
        wait_configured(w);
        w->egl = wl_egl_window_create(w->surface, w->width, w->height);
        window_update_opaque_region(w);
    }
    return w->egl;
}

static void dwl_window_present_pixels(int32_t window, const void *pixels, int32_t width, int32_t height,
                                      int32_t stride) {
    struct window *w = find_window(window);
    if (!w || w->egl || !pixels || width <= 0 || height <= 0 || stride < width * 4) return;
    wait_configured(w);
    if (!w->configured) return;
    struct shmbuf *b = NULL;
    for (int pass = 0; pass < 2 && !b; pass++) {
        for (int i = 0; i < SHM_BUFFERS; i++) {
            if (w->bufs[i].busy) continue;
            if (!b || (w->bufs[i].width == width && w->bufs[i].height == height)) b = &w->bufs[i];
        }
        if (!b && pass == 0) read_pending_nonblocking();
    }
    if (!b) return; /* all buffers still held by the compositor: drop this frame */
    if (!b->buffer || b->width != width || b->height != height) {
        shmbuf_free(b);
        struct shmbuf_raw r = shm_alloc(width, height, width * 4);
        if (!r.buffer) return;
        b->buffer = r.buffer;
        b->data = r.data;
        b->size = r.size;
        b->width = width;
        b->height = height;
        wl_buffer_add_listener(b->buffer, &buffer_listener, b);
    }
    copy_rows(b->data, width * 4, pixels, stride, width, height);
    b->busy = true;
    wl_surface_attach(w->surface, b->buffer, 0, 0);
    wl_surface_damage_buffer(w->surface, 0, 0, width, height);
    window_commit(w);
    flush_best_effort();
}

static int dwl_window_activate(int32_t window, const char *token) {
    struct window *w = find_window(window);
    if (!w || !g.activation || !token || !*token) return -1;
    snprintf(w->act_token, sizeof w->act_token, "%.*s", MAX_TOKEN_LOG, token);
    xdg_activation_v1_activate(g.activation, token, w->surface);
    if (g.kbd_focus == w) {
        struct dwl_event done = make_event(DWL_EVENT_ACTIVATION_DONE, w);
        done.pressed = 1;
        queue_event(&done);
        LOG("activation token=%s\xe2\x80\xa6 result=focused", w->act_token);
        return 0;
    }
    w->act_pending = true;
    w->act_deadline = now_ms() + ACTIVATION_WINDOW_MS;
    return 0;
}

struct token_result {
    char *token;
    bool done;
};
static void token_done(void *data, struct xdg_activation_token_v1 *t UNUSED, const char *token) {
    struct token_result *r = data;
    free(r->token);
    r->token = strdup(token);
    r->done = true;
}
static const struct xdg_activation_token_v1_listener token_listener = {.done = token_done};

static int dwl_request_token(int32_t window, char *out, size_t out_size) {
    struct window *w = find_window(window);
    if (!w || !g.activation || !g.seat || !w->have_input_serial || !out || out_size == 0) return -1;
    struct xdg_activation_token_v1 *t = xdg_activation_v1_get_activation_token(g.activation);
    if (!t) return -1;
    struct token_result r = {NULL, false};
    xdg_activation_token_v1_add_listener(t, &token_listener, &r);
    xdg_activation_token_v1_set_serial(t, w->input_serial, g.seat);
    xdg_activation_token_v1_set_surface(t, w->surface);
    if (w->app_id && *w->app_id) xdg_activation_token_v1_set_app_id(t, w->app_id);
    xdg_activation_token_v1_commit(t);
    for (int i = 0; i < 3 && !r.done; i++)
        if (wl_display_roundtrip(g.display) < 0) {
            g.dead = true;
            break;
        }
    xdg_activation_token_v1_destroy(t);
    int result = -1;
    if (r.token && strlen(r.token) < out_size) {
        strcpy(out, r.token);
        result = (int)strlen(r.token);
    }
    free(r.token);
    return result;
}

static void dwl_window_idle_inhibit(int32_t window, int inhibit) {
    struct window *w = find_window(window);
    if (!w || !g.idle_manager) return;
    if (inhibit && !w->idle) {
        w->idle = zwp_idle_inhibit_manager_v1_create_inhibitor(g.idle_manager, w->surface);
    } else if (!inhibit && w->idle) {
        zwp_idle_inhibitor_v1_destroy(w->idle);
        w->idle = NULL;
    }
}

/* ------------------------------------------------------------------ popups and client window requests */

static struct xdg_positioner *popup_positioner(int32_t x, int32_t y, int32_t width, int32_t height) {
    struct xdg_positioner *pos = xdg_wm_base_create_positioner(g.wm_base);
    if (!pos) return NULL;
    xdg_positioner_set_size(pos, width, height);
    xdg_positioner_set_anchor_rect(pos, x, y, 1, 1);
    xdg_positioner_set_anchor(pos, XDG_POSITIONER_ANCHOR_TOP_LEFT);
    xdg_positioner_set_gravity(pos, XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT);
    xdg_positioner_set_constraint_adjustment(pos, XDG_POSITIONER_CONSTRAINT_ADJUSTMENT_SLIDE_X |
                                                      XDG_POSITIONER_CONSTRAINT_ADJUSTMENT_SLIDE_Y |
                                                      XDG_POSITIONER_CONSTRAINT_ADJUSTMENT_FLIP_Y);
    if (xdg_wm_base_get_version(g.wm_base) >= XDG_POSITIONER_SET_REACTIVE_SINCE_VERSION)
        xdg_positioner_set_reactive(pos);
    return pos;
}

/* Create the wl_surface/xdg_surface/xdg_popup triple for w (also used to recreate it). */
static bool popup_build(struct window *w) {
    struct xdg_positioner *pos = popup_positioner(w->popup_x, w->popup_y, w->width, w->height);
    if (!pos) return false;
    w->configured = false;
    w->surface = wl_compositor_create_surface(g.compositor);
    wl_surface_set_user_data(w->surface, w);
    timing_attach_surface(w);
    w->xdg_surface = xdg_wm_base_get_xdg_surface(g.wm_base, w->surface);
    xdg_surface_add_listener(w->xdg_surface, &xdg_surface_listener, w);
    w->popup = xdg_surface_get_popup(w->xdg_surface, w->parent->xdg_surface, pos);
    xdg_positioner_destroy(pos);
    xdg_popup_add_listener(w->popup, &popup_listener, w);
    if (w->popup_grab && g.seat && g.have_grab_serial) xdg_popup_grab(w->popup, g.seat, g.grab_serial);
    if (g.viewporter) w->viewport = wp_viewporter_get_viewport(g.viewporter, w->surface);
    /* The initial commit without a buffer asks the compositor for the configure; the grab must precede it. */
    window_commit(w);
    return true;
}

static int32_t dwl_popup_create(int32_t parent, int32_t x, int32_t y, int32_t width, int32_t height, int grab) {
    if (!g.display || !g.compositor || !g.wm_base) return 0;
    struct window *p = find_window(parent);
    if (!p || !p->xdg_surface || width <= 0 || height <= 0) return 0;
    struct window *w = calloc(1, sizeof *w);
    struct window **list = realloc(g.windows, (g.window_count + 1) * sizeof *list);
    if (!w || !list) {
        free(w);
        if (list) g.windows = list;
        return 0;
    }
    g.windows = list;
    w->id = ++g.next_window_id;
    w->width = width;
    w->height = height;
    w->app_id = strdup("");
    w->is_popup = true;
    w->parent = p;
    w->popup_x = x;
    w->popup_y = y;
    w->popup_grab = grab != 0 && g.seat && g.have_grab_serial;
    if (!popup_build(w)) {
        free(w->app_id);
        free(w);
        return 0;
    }
    g.windows[g.window_count++] = w;
    flush_best_effort();
    return w->id;
}

/* Fallback without xdg_popup.reposition: tear the popup surface down and build it again under the same id. */
static void popup_recreate(struct window *w) {
    timing_invalidate(w);
    for (struct window *child; (child = find_child_popup(w)) != NULL;) {
        queue_event(&(struct dwl_event){.type = DWL_EVENT_POPUP_DONE, .window = child->id});
        dwl_window_destroy(child->id);
    }
    if (g.ptr_focus == w) g.ptr_focus = NULL;
    if (g.kbd_focus == w) g.kbd_focus = NULL;
    if (g.rep_window == w) stop_repeat();
    if (w->locked) zwp_locked_pointer_v1_destroy(w->locked);
    if (w->confined) zwp_confined_pointer_v1_destroy(w->confined);
    if (w->idle) zwp_idle_inhibitor_v1_destroy(w->idle);
    if (w->viewport) wp_viewport_destroy(w->viewport);
    for (int i = 0; i < SHM_BUFFERS; i++) shmbuf_free(&w->bufs[i]);
    xdg_popup_destroy(w->popup);
    xdg_surface_destroy(w->xdg_surface);
    wl_surface_set_user_data(w->surface, NULL);
    wl_surface_destroy(w->surface);
    w->locked = NULL;
    w->confined = NULL;
    w->viewport = NULL;
    bool idle = w->idle != NULL;
    w->idle = NULL;
    w->button_held = false;
    if (!popup_build(w)) {
        /* Cannot recover the surface: report the popup as dismissed so the guest drops it. */
        LOG("popup %d could not be recreated", w->id);
        w->popup = NULL;
        w->xdg_surface = NULL;
        w->surface = NULL;
        queue_event(&(struct dwl_event){.type = DWL_EVENT_POPUP_DONE, .window = w->id});
        dwl_window_destroy(w->id);
        return;
    }
    if (idle) dwl_window_idle_inhibit(w->id, 1);
}

static void dwl_popup_reposition(int32_t popup, int32_t x, int32_t y, int32_t width, int32_t height) {
    struct window *w = find_window(popup);
    if (!w || !w->is_popup || !w->popup || width <= 0 || height <= 0) return;
    w->popup_x = x;
    w->popup_y = y;
    w->width = width;
    w->height = height;
    if (xdg_popup_get_version(w->popup) >= XDG_POPUP_REPOSITION_SINCE_VERSION) {
        struct xdg_positioner *pos = popup_positioner(x, y, width, height);
        if (!pos) return;
        xdg_popup_reposition(w->popup, pos, ++w->reposition_token);
        xdg_positioner_destroy(pos);
        if (w->egl) wl_egl_window_resize(w->egl, width, height, 0, 0);
    } else if (w->egl) {
        /* A wl_egl_window cannot move to a new surface under a live EGLSurface; only the size can follow. */
        LOG("popup %d: reposition needs xdg_wm_base v3 for EGL popups; position unchanged", w->id);
        wl_egl_window_resize(w->egl, width, height, 0, 0);
    } else {
        popup_recreate(w);
    }
    flush_best_effort();
}

static int dwl_window_move(int32_t window) {
    struct window *w = find_window(window);
    if (!w || !w->toplevel || !g.seat || !w->button_held) return -1;
    xdg_toplevel_move(w->toplevel, g.seat, w->button_serial);
    flush_best_effort();
    return 0;
}

static int dwl_window_resize(int32_t window, uint32_t edges) {
    struct window *w = find_window(window);
    if (!w || !w->toplevel || !g.seat || !w->button_held) return -1;
    xdg_toplevel_resize(w->toplevel, g.seat, w->button_serial, edges);
    flush_best_effort();
    return 0;
}

static void dwl_window_set_parent(int32_t window, int32_t parent) {
    struct window *w = find_window(window);
    if (!w || !w->toplevel) return;
    struct window *p = parent ? find_window(parent) : NULL;
    if (parent && (!p || !p->toplevel || p == w)) return;
    xdg_toplevel_set_parent(w->toplevel, p ? p->toplevel : NULL);
}

static void dwl_window_set_maximized(int32_t window, int maximized) {
    struct window *w = find_window(window);
    if (!w || !w->toplevel) return;
    if (maximized)
        xdg_toplevel_set_maximized(w->toplevel);
    else
        xdg_toplevel_unset_maximized(w->toplevel);
    flush_best_effort();
}

static void dwl_window_set_minimized(int32_t window) {
    struct window *w = find_window(window);
    if (w && w->toplevel) {
        timing_invalidate(w);
        xdg_toplevel_set_minimized(w->toplevel);
        flush_best_effort();
    }
}

/* ------------------------------------------------------------------ outputs api */

static int dwl_output_count(void) { return (int)g.output_count; }

static int dwl_output_get(int index, struct dwl_output *out) {
    if (index < 0 || (size_t)index >= g.output_count || !out) return -1;
    struct output *o = g.outputs[index];
    memset(out, 0, sizeof *out);
    out->pixel_width = o->pixel_width;
    out->pixel_height = o->pixel_height;
    out->refresh_mhz = o->refresh;
    out->scale = o->scale > 0 ? o->scale : 1;
    out->x = o->has_xdg_pos ? o->xdg_x : o->geo_x;
    out->y = o->has_xdg_pos ? o->xdg_y : o->geo_y;
    if (o->has_xdg_size) {
        out->width = o->xdg_width;
        out->height = o->xdg_height;
    } else {
        bool swap = (o->transform & 1) != 0;
        out->width = (swap ? o->pixel_height : o->pixel_width) / out->scale;
        out->height = (swap ? o->pixel_width : o->pixel_height) / out->scale;
    }
    snprintf(out->name, sizeof out->name, "%s", o->name_str);
    return 0;
}

/* ------------------------------------------------------------------ connection */

static void reset_state(void) {
    for (size_t i = 0; i < g.advertised_count; i++) free(g.advertised[i]);
    free(g.advertised);
    free(g.windows);
    free(g.outputs);
    free(g.queue);
    if (g.xkb_state) xkb_state_unref(g.xkb_state);
    if (g.keymap) xkb_keymap_unref(g.keymap);
    if (g.xkb) xkb_context_unref(g.xkb);
    memset(&g, 0, sizeof g);
}

static void dwl_disconnect(void) {
    if (!g.display) return;
    if (g.handoff_token) {
        xdg_activation_token_v1_destroy(g.handoff_token);
        g.handoff_token = NULL;
    }
    while (g.window_count) dwl_window_destroy(g.windows[0]->id);
    for (size_t i = 0; i < g.output_count; i++) output_free(g.outputs[i]);
    g.output_count = 0;
    for (int i = 0; i < CURSOR_CACHE; i++) cursor_entry_free(&g.cursors[i]);
    if (g.cursor_surface) wl_surface_destroy(g.cursor_surface);
#ifdef DWL_HAVE_PRESENTATION
    if (g.presentation) wp_presentation_destroy(g.presentation);
#endif
    if (!g.dead) wl_display_flush(g.display);
    wl_display_disconnect(g.display);
    g.display = NULL;
    reset_state();
}

static int dwl_connect(const char *socket_path) {
    if (g.display) return 0;
    if (!socket_path || socket_path[0] != '/') {
        LOG("connect needs an absolute socket path");
        return -1;
    }
    ++connection_identity;
    g.display = wl_display_connect(socket_path);
    if (!g.display) {
        LOG("cannot connect to %s: %s", socket_path, strerror(errno));
        return -1;
    }
    g.xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    g.rep_rate = 25;
    g.rep_delay = 600;
    g.registry = wl_display_get_registry(g.display);
    wl_registry_add_listener(g.registry, &registry_listener, NULL);
    for (int i = 0; i < 3; i++) {
        if (wl_display_roundtrip(g.display) < 0) {
            LOG("registry roundtrip failed: %s", strerror(errno));
            dwl_disconnect();
            return -1;
        }
        if (i == 0) {
            const char *missing[4];
            int n = 0;
            if (!g.compositor) missing[n++] = "wl_compositor (version 4+)";
            if (!g.shm) missing[n++] = "wl_shm";
            if (!g.seat) missing[n++] = "wl_seat";
            if (!g.wm_base) missing[n++] = "xdg_wm_base";
            if (n) {
                for (int k = 0; k < n; k++) LOG("required global %s missing from the compositor", missing[k]);
                dwl_disconnect();
                return -1;
            }
            xdg_wm_base_add_listener(g.wm_base, &wm_base_listener, NULL);
        }
    }
    if (!g.xkb) LOG("xkbcommon context unavailable; key events will carry no keysyms");
    return 0;
}

static int dwl_has_global(const char *name) {
    if (!name) return 0;
    if (!strcmp(name, "xdg_activation_v1")) return g.activation != NULL;
    if (!strcmp(name, "zwp_pointer_constraints_v1")) return g.constraints != NULL;
    if (!strcmp(name, "zwp_relative_pointer_manager_v1")) return g.relative_manager != NULL;
    if (!strcmp(name, "wp_pointer_warp_v1"))
#ifdef DWL_HAVE_POINTER_WARP
        return g.warp != NULL;
#else
        return 0; /* advertised or not, this build cannot use it */
#endif
    if (!strcmp(name, "wp_viewporter")) return g.viewporter != NULL;
    if (!strcmp(name, "zxdg_output_manager_v1")) return g.xdg_output_manager != NULL;
    if (!strcmp(name, "zwp_idle_inhibit_manager_v1")) return g.idle_manager != NULL;
    for (size_t i = 0; i < g.advertised_count; i++)
        if (!strcmp(g.advertised[i], name)) return 1;
    return 0;
}

static int dwl_fd(void) { return g.display ? wl_display_get_fd(g.display) : -1; }

static int dwl_flush(void) {
    if (!g.display || g.dead) return -1;
    if (wl_display_flush(g.display) < 0 && errno != EAGAIN) {
        g.dead = true;
        return -1;
    }
    return 0;
}

static void run_timers(void) {
    int64_t now = now_ms();
    if (g.rep_active && g.rep_window && g.rep_rate > 0 && now >= g.rep_next) {
        emit_key(g.rep_window, g.rep_key, true, true, (uint32_t)now);
        int64_t interval = 1000 / g.rep_rate;
        if (interval < 1) interval = 1;
        g.rep_next += interval;
        if (g.rep_next < now) g.rep_next = now + interval; /* stalled loop: do not burst */
    }
    for (size_t i = 0; i < g.window_count; i++) {
        struct window *w = g.windows[i];
        if(w->feedback)timing_expire(w, timing_now_ns());
        if (w->act_pending && now >= w->act_deadline) {
            w->act_pending = false;
            struct dwl_event done = make_event(DWL_EVENT_ACTIVATION_DONE, w);
            done.pressed = 0;
            queue_event(&done);
            LOG("activation token=%s\xe2\x80\xa6 result=not-focused", w->act_token);
        }
    }
}

static int dwl_dispatch(void) {
    if (!g.display || g.dead) return -1;
    if (wl_display_dispatch_pending(g.display) < 0) goto fatal;
    if (wl_display_flush(g.display) < 0 && errno != EAGAIN) goto fatal;
    while (wl_display_prepare_read(g.display) != 0)
        if (wl_display_dispatch_pending(g.display) < 0) goto fatal;
    if (wl_display_flush(g.display) < 0 && errno != EAGAIN) {
        wl_display_cancel_read(g.display);
        goto fatal;
    }
    struct pollfd pfd = {.fd = wl_display_get_fd(g.display), .events = POLLIN};
    int pr = poll(&pfd, 1, 0);
    if (pr > 0 && (pfd.revents & (POLLIN | POLLERR | POLLHUP))) {
        if (wl_display_read_events(g.display) < 0) goto fatal;
    } else {
        wl_display_cancel_read(g.display);
    }
    if (wl_display_dispatch_pending(g.display) < 0) goto fatal;
    run_timers();
    return (int)g.queue_len;
fatal:
    g.dead = true;
    for (size_t i = 0; i < g.window_count; ++i) timing_invalidate(g.windows[i]);
    LOG("connection lost: %s", strerror(errno));
    return -1;
}

static int dwl_next_event(struct dwl_event *out) {
    if (!out || g.queue_len == 0) return 0;
    *out = g.queue[g.queue_head];
    g.queue_head = (g.queue_head + 1) % g.queue_cap;
    g.queue_len--;
    return 1;
}

static int dwl_repeat_timeout_ms(void) {
    bool have = false;
    int64_t best = 0, now = now_ms();
    if (g.rep_active && g.rep_window && g.rep_rate > 0) {
        best = g.rep_next - now;
        have = true;
    }
    for (size_t i = 0; i < g.window_count; i++) {
        uint64_t ns = g.windows[i]->feedback ? timing_now_ns() : 0;
        for (struct presentation_pending *p = g.windows[i]->feedback; p; p = p->next) {
            int64_t d = p->expires_ns > ns ? (int64_t)((p->expires_ns - ns + 999999) / 1000000) : 0;
            if (!have || d < best) best = d;
            have = true;
        }
        if (!g.windows[i]->act_pending) continue;
        int64_t d = g.windows[i]->act_deadline - now;
        if (!have || d < best) best = d;
        have = true;
    }
    if (!have) return -1;
    return best < 0 ? 0 : (int)best;
}

static void *dwl_native_display(void) { return g.display; }

static const struct dwl_api api = {
    .version = DWL_API_VERSION,
    .connect = dwl_connect,
    .disconnect = dwl_disconnect,
    .has_global = dwl_has_global,
    .fd = dwl_fd,
    .dispatch = dwl_dispatch,
    .flush = dwl_flush,
    .next_event = dwl_next_event,
    .repeat_timeout_ms = dwl_repeat_timeout_ms,
    .native_display = dwl_native_display,
    .window_create = dwl_window_create,
    .window_destroy = dwl_window_destroy,
    .window_set_title = dwl_window_set_title,
    .window_set_fullscreen = dwl_window_set_fullscreen,
    .window_set_min_size = dwl_window_set_min_size,
    .window_set_size = dwl_window_set_size,
    .window_set_viewport = dwl_window_set_viewport,
    .window_egl_window = dwl_window_egl_window,
    .window_present_pixels = dwl_window_present_pixels,
    .window_activate = dwl_window_activate,
    .request_token = dwl_request_token,
    .window_idle_inhibit = dwl_window_idle_inhibit,
    .pointer_lock = dwl_pointer_lock,
    .pointer_confine = dwl_pointer_confine,
    .pointer_warp = dwl_pointer_warp,
    .cursor_set_image = dwl_cursor_set_image,
    .cursor_hide = dwl_cursor_hide,
    .output_count = dwl_output_count,
    .output_get = dwl_output_get,
    .popup_create = dwl_popup_create,
    .popup_reposition = dwl_popup_reposition,
    .window_move = dwl_window_move,
    .window_resize = dwl_window_resize,
    .window_set_parent = dwl_window_set_parent,
    .window_set_maximized = dwl_window_set_maximized,
    .window_set_minimized = dwl_window_set_minimized,
    .window_get_timing = dwl_window_get_timing,
    .output_get_timing = dwl_output_get_timing,
    .presentation_begin = dwl_presentation_begin,
    .presentation_end = dwl_presentation_end,
    .window_invalidate_timing = dwl_window_invalidate_timing,
};

__attribute__((visibility("default"))) const struct dwl_api *DarlingWaylandAPI(void) { return &api; }
