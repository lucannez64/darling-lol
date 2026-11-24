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
int DGTimingPublish(const DGTimingSnapshot *s){published=*s;++publications;return 1;}
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
