"""Host-only tests drive the shipped Wayland timing functions without a compositor."""
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#include <wayland-client.h>
#include <wayland-egl.h>
#ifdef DWL_HAVE_PRESENTATION
#include "presentation-time-client-protocol.h"
#endif
static uint64_t synthetic_now = 100000000000ull;
static int64_t synthetic_offset = 5000000000ll;
static int commits;
#ifdef DWL_HAVE_PRESENTATION
static int requests, destroyed;
#endif
static char order[1000];
static size_t order_size;
static void record(char code) { order[order_size++] = code; order[order_size] = 0; }
static int synthetic_clock_gettime(clockid_t clock, struct timespec *ts) {
    uint64_t ns = synthetic_now;
    if (clock != CLOCK_MONOTONIC) ns -= synthetic_offset;
    ts->tv_sec = ns / 1000000000; ts->tv_nsec = ns % 1000000000;
    return 0;
}
static void synthetic_commit(struct wl_surface *surface) { assert(surface); ++commits; record('c'); }
static int synthetic_surface_listener(struct wl_surface *surface, const struct wl_surface_listener *l, void *data) {
    assert(surface && l && data); return 0;
}
static void synthetic_surface_set_data(struct wl_surface *surface, void *data) { (void)surface; (void)data; }
static void synthetic_surface_destroy(struct wl_surface *surface) { (void)surface; }
static void synthetic_egl_destroy(struct wl_egl_window *egl) { (void)egl; }
static uint32_t synthetic_output_version(struct wl_output *output) { (void)output; return 4; }
static void synthetic_output_release(struct wl_output *output) { (void)output; }
#ifdef DWL_HAVE_PRESENTATION
static struct wp_presentation_feedback *synthetic_feedback(struct wp_presentation *p, struct wl_surface *s) {
    assert(p && s); ++requests; record('r'); return (void*)(uintptr_t)(1000 + requests);
}
static int synthetic_feedback_listener(struct wp_presentation_feedback *p, const struct wp_presentation_feedback_listener *l, void *data) {
    assert(p && l && data); return 0;
}
static void synthetic_feedback_destroy(struct wp_presentation_feedback *feedback) { assert(feedback); ++destroyed; record('d'); }
#define wp_presentation_feedback(p,s) synthetic_feedback(p,s)
#define wp_presentation_feedback_add_listener synthetic_feedback_listener
#define wp_presentation_feedback_destroy synthetic_feedback_destroy
#endif
#define clock_gettime synthetic_clock_gettime
#define wl_surface_commit synthetic_commit
#define wl_surface_add_listener synthetic_surface_listener
#define wl_surface_set_user_data synthetic_surface_set_data
#define wl_surface_destroy synthetic_surface_destroy
#define wl_egl_window_destroy synthetic_egl_destroy
#define wl_output_get_version synthetic_output_version
#define wl_output_release synthetic_output_release
#define wl_output_destroy synthetic_output_release
#include "darling-wayland.c"

static struct output *make_output(uint32_t name, int32_t refresh) {
    struct output *out = calloc(1, sizeof *out);
    out->identity = name; out->name = name; out->refresh = refresh; out->timing_refresh = refresh;
    out->output = (void*)(uintptr_t)name;
    return out;
}
static struct window *make_window(int32_t id) {
    struct window *w = calloc(1, sizeof *w);
    w->id = id; w->surface = (void*)(uintptr_t)(id+500); w->egl = (void*)(uintptr_t)(id+600);
    timing_attach_surface(w);
    return w;
}
#ifdef DWL_HAVE_PRESENTATION
static void present(struct window *w, unsigned version, uint32_t refresh, uint64_t sequence, uint32_t flags, struct wl_output *output) {
    g.presentation_version = version;
    uint64_t token = dwl_presentation_begin(w->id, w->surface_generation);
    assert(token && w->feedback);
    struct presentation_pending *pending = w->feedback;
    dwl_presentation_end(w->id, token, 1);
    if (output) timing_sync_output(pending, pending->feedback, output);
    uint64_t time = synthetic_now - synthetic_offset - 1000000;
    timing_presented(pending, pending->feedback, 0, time/1000000000, time%1000000000,
                     refresh, sequence >> 32, sequence, flags);
}
#endif
int main(void) {
    struct output *outputs[3] = {make_output(100, 59940), make_output(200, 144000), make_output(300, 144000)};
    struct window *windows[2] = {make_window(1), make_window(2)};
    g.outputs = outputs; g.output_count = 3; g.windows = windows; g.window_count = 2;
    struct window *w = windows[0];
    struct dwl_timing timing;
    assert(dwl_window_get_timing(w->id, &timing) == 0);
    assert(timing.output_id == 100 && timing.rate_num == 59940 && timing.rate_den == 1000);
    timing_surface_enter(w, w->surface, outputs[1]->output);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.output_id == 200);
    timing_surface_enter(w, w->surface, outputs[2]->output);
    timing_surface_enter(w, w->surface, outputs[0]->output);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.output_id == 200);
    timing_surface_leave(w, w->surface, outputs[1]->output);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.output_id == 300);
    timing_surface_leave(w, w->surface, outputs[2]->output);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.output_id == 100);
    uint64_t old_generation = timing.surface_generation;
    out_mode(outputs[0], outputs[0]->output, WL_OUTPUT_MODE_CURRENT, 1920, 1080, 120000);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.rate_num == 59940);
    out_done(outputs[0], outputs[0]->output);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.rate_num == 120000);
    assert(timing.surface_generation != old_generation);
    assert(dwl_presentation_begin(w->id, old_generation) == 0);
    uint64_t token = dwl_presentation_begin(w->id, timing.surface_generation);
    assert(token && dwl_presentation_begin(w->id, timing.surface_generation) == 0);
    int before = commits;
    window_commit(w);
    assert(commits == before && w->deferred_commit);
    record('s'); synthetic_commit(w->surface); /* EGL's corresponding swap */
    dwl_presentation_end(w->id, token, 1);
    assert(commits == before+1 && !w->deferred_commit);
    token = dwl_presentation_begin(w->id, w->surface_generation);
    window_commit(w);
    dwl_presentation_end(w->id, token, 0);
    assert(commits == before+2); /* failed swap: feedback cancelled before deferred commit */
#ifdef DWL_HAVE_PRESENTATION
    g.presentation = (void*)1; g.presentation_clock_known = true; g.presentation_clock = CLOCK_REALTIME;
    present(w, 3, 8333333, 7, 1|0x10, outputs[1]->output);
    assert(dwl_window_get_timing(w->id, &timing) == 0 && timing.output_id == 200);
    assert(timing.flags & DWL_TIMING_FIXED);
    assert(timing.flags & DWL_TIMING_CLOCK_MONOTONIC);
    assert(timing.presentation_ns == synthetic_now-1000000 && timing.counter == 7);
    uint64_t timestamp = timing.presentation_ns;
    present(w, 3, 8333333, 7, 1|0x10, outputs[1]->output);
    assert(w->observation.presentation_ns == timestamp); /* duplicate rejected */
    synthetic_now += 10000000;
    present(w, 3, 8333333, 8, 1|0x20, outputs[1]->output);
    assert((w->observation.flags & DWL_TIMING_VARIABLE) && !(w->observation.flags & DWL_TIMING_FIXED));
    synthetic_now += 10000000;
    present(w, 2, 8333333, 9, 1, outputs[1]->output);
    assert(!(w->observation.flags & (DWL_TIMING_FIXED|DWL_TIMING_VARIABLE)));
    synthetic_now += 10000000;
    present(w, 1, 8333333, 0, 1, outputs[1]->output);
    assert((w->observation.flags & DWL_TIMING_FIXED) && !(w->observation.flags & DWL_TIMING_COUNTER));
    synthetic_now += 10000000;
    present(w, 1, 0, 10, 1, outputs[1]->output);
    assert(!(w->observation.flags & DWL_TIMING_FIXED));
    token = dwl_presentation_begin(w->id, w->surface_generation);
    struct presentation_pending *pending = w->feedback;
    dwl_presentation_end(w->id, token, 1);
    synthetic_offset += 1000000000;
    uint64_t time = synthetic_now-synthetic_offset-1000000;
    timing_presented(pending,pending->feedback,0,time/1000000000,time%1000000000,8333333,0,11,1|0x10);
    assert(!(w->observation.flags & DWL_TIMING_CLOCK_MONOTONIC));
    /* Discard and failure never fabricate a presentation or wait. */
    token = dwl_presentation_begin(w->id,w->surface_generation);
    assert(w->feedback_count == 1);
    pending = w->feedback;
    dwl_presentation_end(w->id,token,1);
    timing_discarded(pending,pending->feedback);
    assert(w->feedback_count == 0);
    for (int i=0;i<8;++i) {
        token = dwl_presentation_begin(w->id,w->surface_generation);
        assert(token); dwl_presentation_end(w->id,token,1);
    }
    assert(w->feedback_count == 8);
    int request_count = requests;
    token = dwl_presentation_begin(w->id,w->surface_generation);
    assert(token && requests == request_count);
    dwl_presentation_end(w->id,token,1);
    synthetic_now += 1000000001;
    timing_expire(w,synthetic_now);
    assert(w->feedback_count == 0);
    struct output *slow = timing_output(w->timing_output);
    int32_t saved_refresh = slow->timing_refresh;
    slow->timing_refresh = 1000;
    token = dwl_presentation_begin(w->id,w->surface_generation);
    dwl_presentation_end(w->id,token,1);
    synthetic_now += 1000000001;
    timing_expire(w,synthetic_now);
    assert(w->feedback_count == 1); /* eight periods at 1 Hz exceed one second */
    synthetic_now += 7000000000ull;
    timing_expire(w,synthetic_now);
    assert(w->feedback_count == 0);
    slow->timing_refresh = saved_refresh;
    token = dwl_presentation_begin(windows[1]->id,windows[1]->surface_generation);
    dwl_presentation_end(windows[1]->id,token,1);
    assert(windows[1]->feedback_count == 1 && w->feedback_count == 0);
    dwl_window_invalidate_timing(windows[1]->id);
    assert(windows[1]->feedback_count == 0);
    token = dwl_presentation_begin(w->id,w->surface_generation);
    assert(token && w->feedback_count == 1);
    dwl_presentation_end(w->id,token,1);
    dwl_window_invalidate_timing(w->id);
    assert(w->feedback_count == 0 && !(w->observation.flags & DWL_TIMING_PRESENTATION));
    /* Commit transaction requests feedback before swap; unrelated commits defer. */
    order_size = 0; order[0] = 0;
    token = dwl_presentation_begin(w->id,w->surface_generation);
    window_commit(w);
    record('s'); synthetic_commit(w->surface);
    dwl_presentation_end(w->id,token,1);
    assert(!strcmp(order,"rsc"));
    dwl_window_invalidate_timing(w->id);
    order_size = 0; order[0] = 0;
    token = dwl_presentation_begin(w->id,w->surface_generation);
    window_commit(w);
    dwl_presentation_end(w->id,token,0);
    assert(!strcmp(order,"rdc"));
    /* Late delivery from an older generation is ignored. */
    token = dwl_presentation_begin(w->id,w->surface_generation);
    pending = w->feedback;
    dwl_presentation_end(w->id,token,1);
    ++w->surface_generation;
    timing_presented(pending,pending->feedback,0,90,0,8333333,0,12,1|0x10);
    assert(w->feedback_count == 0 && !(w->observation.flags & DWL_TIMING_PRESENTATION));
#endif
    /* Registry removal compacts the array but preserves every surviving ID. */
    registry_global_remove(NULL,NULL,100);
    assert(g.output_count == 2 && outputs[0]->identity == 200 && outputs[1]->identity == 300);
    assert(dwl_output_get_timing(0,&timing) == 0 && timing.output_id == 200);
    assert(dwl_window_get_timing(w->id,&timing) == 0 && timing.output_id == 200);
    old_generation = w->surface_generation;
    timing_attach_surface(w);
    assert(w->surface_generation != old_generation && !w->entered_count);
    dwl_window_destroy(w->id);
    assert(g.window_count == 1);
    dwl_window_destroy(windows[0]->id);
    output_free(outputs[0]); output_free(outputs[1]); free(g.queue);
    printf("Wayland timing: associations, generations, transactions, protocol semantics, queue bounds passed\n");
}

'''


class WaylandTiming(unittest.TestCase):
    def test_timing_contract_is_appended_without_changing_old_layouts(self):
        header = (ROOT / 'tools/darling-wayland-api.h').read_text()
        self.assertIn('#define DWL_API_VERSION 3', header)
        self.assertGreater(header.index('(*presentation_begin)'), header.index('(*window_set_minimized)'))
        self.assertIn('uint64_t output_id, surface_generation;', header)


    @unittest.skipUnless(shutil.which('cc') and shutil.which('wayland-scanner'), 'Wayland build tools required')
    def test_shipped_backend_events_and_transactions(self):
        modules = ['wayland-client', 'wayland-egl', 'xkbcommon', 'wayland-protocols']
        if subprocess.run(['pkg-config', '--exists', *modules], capture_output=True).returncode:
            self.skipTest('Wayland development libraries required')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            pkgdata = Path(subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], text=True).strip())
            protocols = {
                'xdg-shell': 'stable/xdg-shell/xdg-shell.xml',
                'viewporter': 'stable/viewporter/viewporter.xml',
                'xdg-activation-v1': 'staging/xdg-activation/xdg-activation-v1.xml',
                'pointer-constraints-unstable-v1': 'unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
                'relative-pointer-unstable-v1': 'unstable/relative-pointer/relative-pointer-unstable-v1.xml',
                'xdg-output-unstable-v1': 'unstable/xdg-output/xdg-output-unstable-v1.xml',
                'idle-inhibit-unstable-v1': 'unstable/idle-inhibit/idle-inhibit-unstable-v1.xml',
                'presentation-time': 'stable/presentation-time/presentation-time.xml',
            }
            generated = []
            for name, xml in protocols.items():
                if not (pkgdata / xml).exists():
                    self.skipTest('protocol package lacks ' + xml)
                subprocess.run(['wayland-scanner', 'client-header', str(pkgdata / xml), str(folder / (name + '-client-protocol.h'))], check=True, capture_output=True)
                target = folder / (name + '-protocol.c')
                subprocess.run(['wayland-scanner', 'private-code', str(pkgdata / xml), str(target)], check=True, capture_output=True)
                generated.append(str(target))
            (folder / 'fixture.c').write_text(FIXTURE)
            import shlex
            flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client', 'wayland-egl', 'xkbcommon'], text=True))
            for presentation in (None, 1, 2, 3):
                with self.subTest(presentation=presentation):
                    if presentation:
                        xml = ET.parse(pkgdata / protocols['presentation-time'])
                        for interface in xml.getroot().findall('interface'):
                            interface.set('version', str(presentation))
                            for enum in interface.findall('enum'):
                                for entry in list(enum):
                                    if int(entry.get('since', '1')) > presentation:
                                        enum.remove(entry)
                        old = folder / 'presentation.xml'
                        xml.write(old, encoding='utf-8', xml_declaration=True)
                        subprocess.run(['wayland-scanner', 'client-header', str(old), str(folder / 'presentation-time-client-protocol.h')], check=True, capture_output=True)
                        subprocess.run(['wayland-scanner', 'private-code', str(old), str(folder / 'presentation-time-protocol.c')], check=True, capture_output=True)
                    command = ['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-I' + str(folder), '-I' + str(ROOT / 'tools')]
                    if presentation:
                        command.append('-DDWL_HAVE_PRESENTATION')
                    command += [str(folder / 'fixture.c'), *generated, *flags, '-o', str(folder / 'fixture')]
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run([str(folder / 'fixture')], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('queue bounds passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
