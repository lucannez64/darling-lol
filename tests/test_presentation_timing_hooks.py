"""Compile shipped presentation hooks with inert EGL and surface transactions."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_metal_presentation_tickets as tickets
import test_runtime_compatibility as runtime


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class PresentationTimingHookTests(unittest.TestCase):
    def compile_run(self, code):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'probe.cpp').write_text(code)
            result = subprocess.run(['c++', '-std=c++17', '-pthread', str(root / 'probe.cpp'),
                                     '-o', str(root / 'probe')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(root / 'probe')], check=True, timeout=20)

    def test_old_bridges_use_nominal_outputs_without_reading_appended_fields(self):
        patched = runtime.CompatibilityChecks().patched
        bridge = patched('cocotron', 'AppKit/X11.backend/DarlingWaylandAPI.h')
        timing = patched('cocotron', 'CoreGraphics/include/CoreGraphics/DarlingDisplayTiming.h')
        source = patched('cocotron', 'AppKit/X11.backend/X11DisplayWayland.m')
        methods = '\n'.join(runtime.function(source, name) for name in
                            ('WaylandOutputIdentity', 'WaylandOutputDisplayID', 'WaylandPublishDefaultTiming'))
        self.compile_run(bridge + timing + r'''
#include <cassert>
#include <initializer_list>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>
using BOOL=bool; constexpr bool YES=true,NO=false;
static int publications=0;
static bool gotDefault=false;
uint64_t DGTimingInvalidate(uint64_t key){assert(key==0);return 3;}
int DGTimingPublish(const DGTimingSnapshot *s){
 assert(s->rateNumerator==59940 && s->rateDenominator==1000 && s->generation==3);
 assert(s->flags==(DG_TIMING_NOMINAL|DG_TIMING_WAYLAND));
 if(!s->displayID)gotDefault=true;
 ++publications;return 1;
}
static int count(){return 1;}
static int output(int index,dwl_output *out){assert(index==0);*out={};out->refresh_mhz=59940;strcpy(out->name,"DP-1");return 0;}
''' + methods + r'''
int main(){
 const size_t page=sysconf(_SC_PAGESIZE);
 auto memory=(char*)mmap(nullptr,page*2,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 assert(memory!=MAP_FAILED && !mprotect(memory+page,page,PROT_NONE));
 for(unsigned version: {1u,2u}){
  const size_t bytes=version==1?offsetof(dwl_api,popup_create):offsetof(dwl_api,window_get_timing);
  auto api=(dwl_api*)(memory+page-bytes);memset(api,0,bytes);
  api->version=version;api->output_count=count;api->output_get=output;
  publications=0;gotDefault=false;WaylandPublishDefaultTiming(api);
  assert(publications==2 && gotDefault);
 }
 munmap(memory,page*2);
}
''')

    def test_commit_transaction_immediately_encloses_swap_and_cancels_failures(self):
        source = runtime.CompatibilityChecks().patched('cocotron', 'QuartzCore/CALayerContext.m')
        body = tickets.method(source, '- (CGLError) flushDrawableWithTiming')
        replacements = {
            '[_subwindow respondsToSelector: @selector(updateDisplayTiming)]': 'hasTiming',
            '[_subwindow updateDisplayTiming]': 'updateTiming()',
            '[_subwindow displayTimingTarget]': 'target()',
            '[_subwindow respondsToSelector: @selector(beginPresentation)]': 'hasHooks',
            '[_subwindow beginPresentation]': 'begin()',
            '[_subwindow endPresentation: token success: result == kCGLNoError]': 'end(token,result==kCGLNoError)',
        }
        for before, after in replacements.items():
            body = body.replace(before, after)
        self.compile_run(r'''
#include <cassert>
#include <cstdint>
#include <vector>
using CGLError=int; using BOOL=bool;
static constexpr int kCGLNoError=0;
static int _glContext=1, swapResult=0;
static bool hasTiming=true,hasHooks=true,ownership=true;
static std::vector<int> order;
static void updateTiming(){order.push_back(1);}
static uint64_t target(){return 41;}
static void DGTimingBind(uint64_t c,uint64_t s){assert(c==1 && s==41);order.push_back(2);}
static uint64_t begin(){order.push_back(3);return ownership?7:0;}
static void end(uint64_t token,bool success){assert(token==7 && success==!swapResult);order.push_back(5);}
static int CGLFlushDrawable(int c){assert(c==1);order.push_back(4);return swapResult;}
static int flush(){
''' + body + r'''
}
int main(){
    assert(flush()==0 && (order==std::vector<int>{1,2,3,4,5}));
    order.clear();swapResult=1;
    assert(flush()==1 && (order==std::vector<int>{1,2,3,4,5}));
    order.clear();ownership=false;swapResult=0;
    assert(flush()==0 && (order==std::vector<int>{1,2,3,4}));
    order.clear();hasHooks=false;
    assert(flush()==0 && (order==std::vector<int>{1,2,4}));
    order.clear();hasTiming=false;
    assert(flush()==0 && (order==std::vector<int>{4}));
}
''')

    def test_unknown_x11_clock_and_missing_counters_cannot_publish_phase(self):
        source = runtime.CompatibilityChecks().patched('darling', 'src/frameworks/OpenGL/OpenGL.c')
        header = runtime.CompatibilityChecks().patched(
            'cocotron', 'CoreGraphics/include/CoreGraphics/DarlingDisplayTiming.h')
        method = runtime.function(source, 'recordEGLPresentation')
        self.compile_run(header + r'''
#include <cassert>
#include <cstring>
using EGLDisplay=int; using EGLSurface=int; using EGLBoolean=int;
constexpr int EGL_EXTENSIONS=1;
struct Context{int egl_display=1,egl_surface=1;}; using CGLContextObj=Context*;
static bool wayland_mode=false,knownClock=true;
static uint64_t counter=40;
static int publications=0;
static DGTimingSnapshot published;
static const char* query(int,int){return "EGL_CHROMIUM_sync_control";}
static EGLBoolean values(int,int,uint64_t *ust,uint64_t *msc,uint64_t *sbc){*ust=1000;*msc=counter;*sbc=1;return 1;}
static auto p_eglQueryString=query;
static auto p_eglGetSyncValuesCHROMIUM=values;
DGTimingSnapshot DGTimingRead(uint64_t,uint32_t,DGTimingCache*){
    DGTimingSnapshot s={};s.target=44;s.generation=9;s.outputID=3;
    s.flags=DG_TIMING_NOMINAL;s.rateNumerator=144;s.rateDenominator=1;return s;
}
int DGTimingMonotonicToMach(uint64_t value,DGTimingClock*,uint64_t *out){*out=value+700;return knownClock;}
int DGTimingTryPublish(const DGTimingSnapshot *s){published=*s;++publications;return 1;}
''' + method + r'''
int main(){
    Context context;
    recordEGLPresentation(&context);assert(publications==1);
    assert((published.flags&(DG_TIMING_FEEDBACK|DG_TIMING_FIXED|DG_TIMING_COUNTER))==14);
    assert(published.presentationNS==1000700 && published.presentationCounter==40);
    knownClock=false;recordEGLPresentation(&context);
    assert(publications==2 && !(published.flags&DG_TIMING_FEEDBACK));
    counter=0;recordEGLPresentation(&context);assert(publications==2);
    counter=40;wayland_mode=true;recordEGLPresentation(&context);assert(publications==2);
    wayland_mode=false;p_eglGetSyncValuesCHROMIUM=nullptr;
    recordEGLPresentation(&context);assert(publications==2);
}
''')

    def test_only_remaining_wayland_owner_can_request_feedback(self):
        patched = runtime.CompatibilityChecks().patched
        bridge = patched('cocotron', 'AppKit/X11.backend/DarlingWaylandAPI.h')
        source = patched('cocotron', 'AppKit/X11.backend/WaylandWindow.m')
        methods = []
        for declaration, selector in [('void acquire(void *owner)', '- (void) acquireLayerDrawable:'),
                                      ('void release(void *owner)', '- (void) releaseLayerDrawable:'),
                                      ('uint64_t begin(void *owner)', '- (uint64_t) beginPresentationFor:')]:
            body = tickets.method(source, selector)
            for original, host in {
                'NSValue *key': 'void *key',
                '[NSValue valueWithPointer: owner]': 'owner',
                '[_layerDrawableOwners containsObject: key]': '_layerDrawableOwners.count(key)',
                '[_layerDrawableOwners containsObject: owner]': '_layerDrawableOwners.count(owner)',
                'if (!_layerDrawableOwners) _layerDrawableOwners = [NSMutableSet new];': '',
                '[_layerDrawableOwners addObject: key]': '_layerDrawableOwners.insert(key)',
                '[_layerDrawableOwners removeObject: key]': '_layerDrawableOwners.erase(key)',
                '[_layerDrawableOwners count]': '_layerDrawableOwners.size()',
                '[self invalidateDisplayTiming]': '++invalidations',
                '[self updateDisplayTiming]': '(void)0',
            }.items():
                body = body.replace(original, host)
            methods.append(declaration + '{' + body + '}')
        self.compile_run(bridge + r"""
#include <cassert>
#include <set>
static dwl_api api{};
static dwl_api *WaylandAPI(){return &api;}
struct Window {
    int _dwlID=1;
    uint64_t _hostTimingGeneration=1;
    unsigned invalidations=0;
    std::set<void*> _layerDrawableOwners;
""" + '\n'.join(methods) + r"""
};
int main(){
    Window w; int a,b;
    api.version=3;api.presentation_begin=[](int32_t,uint64_t){return uint64_t(17);};
    w.acquire(&a);assert(w.begin(&a)==17);w.acquire(&b);
    assert(w.begin(&a)==0 && w.begin(&b)==0);
    w.release(&a);assert(w.begin(&b)==17 && w.begin(&a)==0);
    w.release(&a);w.acquire(&b);assert(w.invalidations==3);
    w.release(&b);assert(w.begin(&b)==0);
}
""")

    def test_window_retries_publication_and_invalidates_lost_output(self):
        patched = runtime.CompatibilityChecks().patched
        bridge = patched('cocotron', 'AppKit/X11.backend/DarlingWaylandAPI.h')
        timing = patched('cocotron', 'CoreGraphics/include/CoreGraphics/DarlingDisplayTiming.h')
        window = patched('cocotron', 'AppKit/X11.backend/WaylandWindow.m')
        method = tickets.method(window, '- (void) updateDisplayTiming {')
        method = method.replace('[self displayTimingTarget]', 'target')
        self.compile_run(bridge + timing + r"""
#include <cassert>
#include <cstring>
static dwl_timing host{};
static dwl_api api{};
static unsigned invalidations, writes;
static bool failWrite;
static uint64_t target=1234;
static int readTiming(int32_t, dwl_timing *out){*out=host;return 0;}
static dwl_api *WaylandAPI(){return &api;}
static uint64_t WaylandOutputIdentity(const dwl_output*){return 77;}
static uint32_t WaylandOutputDisplayID(uint64_t id){return id;}
uint64_t DGTimingInvalidate(uint64_t value){assert(value==target);return ++invalidations;}
int DGTimingPublish(const DGTimingSnapshot *snapshot){
    assert(snapshot->generation==invalidations); ++writes;return !failWrite;
}
int DGTimingMonotonicToMach(uint64_t stamp,DGTimingClock*,uint64_t *result){
    *result=stamp;return 1;
}
struct Window {
    enum {WaylandWindowKindGame=0};
    int _kind=WaylandWindowKindGame, _dwlID=1;
    uint64_t _timingGeneration=0,_hostTimingGeneration=0,_timingOutput=0;
    uint64_t _timingRateNum=0,_timingRateDen=0;
    DGTimingClock _timingClock{};
    dwl_timing _publishedTiming{};
    void update(){
""" + method + r"""
    }
};
int main(){
    Window w;api.version=3;api.window_get_timing=readTiming;
    host.flags=DWL_TIMING_NOMINAL;host.rate_num=144;host.rate_den=1;
    host.surface_generation=1;host.output_id=42;
    failWrite=true;w.update();assert(writes==1 && invalidations==1);
    failWrite=false;w.update();w.update();assert(writes==2 && invalidations==1);
    host.flags|=DWL_TIMING_PRESENTATION|DWL_TIMING_CLOCK_MONOTONIC;
    host.presentation_ns=44;w.update();w.update();assert(writes==3);
    ++host.surface_generation;host.flags=0;
    w.update();w.update();assert(writes==3 && invalidations==2 && !w._timingRateNum);
    host.flags=DWL_TIMING_NOMINAL;w.update();assert(writes==4 && invalidations==3);
}
""")

    def test_embedded_presenter_nominal_metadata_is_coherent_and_versioned(self):
        header = runtime.CompatibilityChecks().patched(
            'darling', 'src/frameworks/OpenGL/include/OpenGL/DarlingNativePresent.h')
        source = (runtime.ROOT / 'tools/m58f299-xshm.c').read_text()
        setter = runtime.function(source.replace('__attribute__((visibility("default"))) void\n',
                                                  'void '), 'DarlingXShmSetTiming')
        presenter = runtime.CompatibilityChecks().patched(
            'darling', 'src/frameworks/OpenGL/CGLAsyncPresent.inc')
        interval = runtime.function(presenter, 'nativeRefreshIntervalNS')
        self.compile_run(header + r'''
#include <cassert>
#include <cstdlib>
#include <thread>
struct Frame{DarlingNativePresentTarget *gpuTarget;};
''' + setter.replace('Frame *f=handle;', 'Frame *f=(Frame*)handle;') + interval + r'''
int main(){
    static_assert(sizeof(DarlingNativePresentTarget)==64);
    DarlingNativePresentTarget t={};Frame f{&t};
    unsetenv("RCLOL_REFRESH_HZ");
    assert(DarlingNativeNominalInterval(&t)==16666667);
    DarlingXShmSetTiming(&f,60000,1001);
    assert(DarlingNativeNominalInterval(&t)==16683333);
    DarlingXShmSetTiming(&f,144,1);
    assert(DarlingNativeNominalInterval(&t)==6944444);
    const char *values[]={"1","60","144","1000","  +144","59.94","144Hz","0","1001","invalid","-1",""};
    const uint64_t intervals[]={1000000000,16666666,6944444,1000000,6944444,16949152,6944444,
                                16666667,16666667,16666667,16666667,16666667};
    for(unsigned i=0;i<sizeof(intervals)/sizeof(*intervals);++i) {
        setenv("RCLOL_REFRESH_HZ",values[i],1);
        assert(nativeRefreshIntervalNS(&t)==intervals[i]);
        assert(nativeRefreshIntervalNS(nullptr)==intervals[i]);
    }
    unsetenv("RCLOL_REFRESH_HZ");
    assert(nativeRefreshIntervalNS(&t)==6944444);
    __atomic_fetch_or(&t.reserved[0],1,__ATOMIC_RELEASE);
    assert(DarlingNativeNominalInterval(&t)==16666667);
    DarlingXShmSetTiming(&f,0,0);
    assert(DarlingNativeNominalInterval(&t)==16666667);
    std::thread writer([&]{for(int i=0;i<100000;++i){DarlingXShmSetTiming(&f,60000,1001);DarlingXShmSetTiming(&f,144,1);}});
    for(int i=0;i<100000;++i){auto n=DarlingNativeNominalInterval(&t);assert(n==16683333 || n==6944444 || n==16666667);}
    writer.join();
}
''')
