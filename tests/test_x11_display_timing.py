"""Compile shipped RandR timing and output-association policy without an X server."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class X11DisplayTimingChecks(unittest.TestCase):
    def run_cpp(self, fixture):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'check.cpp').write_text(fixture)
            result = subprocess.run(
                ['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                 str(path / 'check.cpp'), '-o', str(path / 'check')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'check')], check=True, timeout=10)

    def test_fractional_modes_and_actual_window_coverage(self):
        source = runtime.CompatibilityChecks().patched(
            'cocotron', 'AppKit/X11.backend/X11Display.m')
        start = source.index('/* RandR timing policy:')
        end = source.index('/* End RandR timing policy. */', start)
        policy = source[start:end]
        fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <climits>
using BOOL=bool;
using RROutput=unsigned long;
constexpr BOOL YES=true, NO=false;
constexpr unsigned RR_Interlace=1, RR_DoubleScan=2;
struct XRRModeInfo {uint64_t dotClock; uint32_t hTotal,vTotal; unsigned modeFlags;};
''' + policy + r'''
int main() {
    uint64_t n,d;
    XRRModeInfo ntsc{148351648,2200,1125,0};
    X11TimingRateForMode(&ntsc,&n,&d);
    assert(n*2475000==148351648*d); // Preserve the rational rate, never integer Hz.
    ntsc.modeFlags=RR_Interlace;
    X11TimingRateForMode(&ntsc,&n,&d);
    assert(n*2475000==296703296*d);
    ntsc.modeFlags=RR_DoubleScan;
    X11TimingRateForMode(&ntsc,&n,&d);
    assert(n*4950000==148351648*d);
    ntsc.hTotal=0; X11TimingRateForMode(&ntsc,&n,&d); assert(n==0 && d==0);
    X11TimingRateForMode(nullptr,&n,&d); assert(n==0 && d==0);

    X11TimingOutput outputs[] = {
        {70,1,0,0,1920,1080,60000,1001,true},
        {20,2,1920,0,1920,1080,144,1,false},
    };
    auto pick=[&](uint64_t previous,int x,int y,int w,int h) {
        auto* result=X11TimingChooseOutput(outputs,2,previous,x,y,w,h);
        return result ? result->outputID : 0;
    };
    assert(pick(0,1700,0,900,1080)==20); // Child, not parent, coverage wins.
    assert(pick(0,0,0,1920,1080)==70);
    assert(pick(20,960,0,1920,1080)==20); // Previous wins a spanning tie.
    assert(pick(0,960,0,1920,1080)==70); // Otherwise primary wins.
    outputs[0].primary=false;
    assert(pick(0,960,0,1920,1080)==20); // Stable ID is the final tie-break.
    outputs[1].primary=true;
    assert(pick(70,960,0,1920,1080)==70); // Primary reorder keeps previous output.
    RROutput before[]={70,20}, after[]={20,70};
    assert(X11TimingIndexForOutput(before,2,70)==0);
    assert(X11TimingIndexForOutput(after,2,70)==1);
    assert(X11TimingIndexForOutput(after,2,99)==-1); // Hot-unplug is not another display.
    outputs[1].width=0;
    assert(pick(20,1920,0,1920,1080)==70); // Removed/disabled output cannot persist.
    outputs[0].height=0;
    assert(pick(70,0,0,10,10)==0);
    assert(!X11TimingChooseOutput(nullptr,0,0,0,0,10,10));
}
'''
        self.run_cpp(fixture)

    def test_window_publications_follow_move_mode_hotplug_and_reuse(self):
        source = runtime.CompatibilityChecks().patched(
            'cocotron', 'AppKit/X11.backend/X11Display.m')
        start = source.index('/* RandR timing policy:')
        end = source.index('/* End RandR timing policy. */', start)
        policy = source[start:end]
        signature = '- (void) updateDisplayTimingForWindow: (Window) window {'
        method = source.split(signature, 1)[1].split('\n}', 1)[0]
        replacements = {
            '[self screens]': 'screens()',
            '[self forgetDisplayTimingForWindow: window]': 'forget(window)',
            '[self notifyNominalDisplayTimingForWindow: window]': 'notify(window)',
            'if (!_timingWindows) _timingWindows = [NSMutableDictionary new];': '',
            'NSNumber *key = [NSNumber numberWithUnsignedLongLong: window];': 'auto key = window;',
            'NSData *previousData = [_timingWindows objectForKey: key];':
                'auto previousData = storeGet(key);',
            '[previousData length]': 'dataLength(previousData)',
            '[previousData getBytes: &previous length: sizeof(previous)]':
                'dataGet(previousData, &previous)',
            '[_timingOutputData bytes]': 'outputs',
            '[_timingOutputData length]': 'sizeof(outputs)',
            '[_timingWindows setObject: [NSData dataWithBytes: &snapshot length: sizeof(snapshot)] forKey: key]':
                'storeSet(key, snapshot)',
        }
        for old, new in replacements.items():
            method = method.replace(old, new)
        self.run_cpp(r'''
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <map>
using BOOL=bool;
using RROutput=unsigned long;
using Window=uint64_t;
constexpr BOOL YES=true, NO=false;
constexpr unsigned RR_Interlace=1, RR_DoubleScan=2;
constexpr unsigned DG_TIMING_NOMINAL=1, DG_TIMING_X11_SERVER=32;
struct XRRModeInfo {uint64_t dotClock; uint32_t hTotal,vTotal; unsigned modeFlags;};
struct DGTimingSnapshot {
    uint32_t version=0,flags=0,displayID=0,reserved=0;
    uint64_t processID=0,target=0,outputID=0,generation=0;
    uint64_t rateNumerator=0,rateDenominator=0,presentationNS=0,presentationCounter=0;
};
struct XWindowAttributes {int width,height;};
struct Geometry {int x,y,width,height;};
static std::map<Window,Geometry> geometry;
static std::map<Window,DGTimingSnapshot> published;
static std::map<Window,uint64_t> generations;
static unsigned publications;
static unsigned queries;
static uint64_t DGTimingInvalidate(Window window) {published.erase(window); return ++generations[window];}
static int DGTimingPublish(const DGTimingSnapshot* s) {
    assert(s->generation==generations[s->target]);
    published[s->target]=*s; ++publications; return 1;
}
static bool XGetWindowAttributes(int,Window window,XWindowAttributes* a) {
    ++queries;
    auto it=geometry.find(window); if(it==geometry.end()) return false;
    a->width=it->second.width; a->height=it->second.height; return true;
}
static Window DefaultRootWindow(int) {return 99;}
static bool XTranslateCoordinates(int,Window window,Window,int,int,int* x,int* y,Window*) {
    *x=geometry[window].x; *y=geometry[window].y; return true;
}
''' + policy + r'''
struct Display {
    int _display=1;
    bool _timingGeometryDirty=false;
    X11TimingOutput outputs[2] = {{70,1,0,0,1920,1080,60000,1001,true},
                                {20,2,1920,0,1920,1080,144,1,false}};
    std::map<Window,DGTimingSnapshot> stored;
    void screens() {}
    const DGTimingSnapshot* storeGet(Window w) {
        auto it=stored.find(w); return it==stored.end() ? nullptr : &it->second;
    }
    static size_t dataLength(const DGTimingSnapshot* s) {return s ? sizeof(*s) : 0;}
    static void dataGet(const DGTimingSnapshot* a,DGTimingSnapshot* b) {*b=*a;}
    void storeSet(Window w,const DGTimingSnapshot& s) {stored[w]=s;}
    void forget(Window w) {DGTimingInvalidate(w); stored.erase(w);}
    void notify(Window) {}
    void update(Window window) {
''' + method + r'''
    }
};
int main() {
    // The exact rate and output lookup helpers remain part of this fixture.
    XRRModeInfo mode{148500000,2200,1125,0}; uint64_t n,d;
    X11TimingRateForMode(&mode,&n,&d); assert(n==60 && d==1);
    RROutput ids[]={70,20}; assert(X11TimingIndexForOutput(ids,2,20)==1);
    Display display;
    geometry[1]={0,0,1920,1080}; geometry[2]={1920,0,1920,1080};
    display.update(1); display.update(2);
    assert(published[1].rateNumerator==60000 && published[1].rateDenominator==1001);
    assert(published[2].rateNumerator==144 && published[2].outputID==20);
    unsigned count=publications;
    unsigned priorQueries=queries;
    published[1].presentationNS=123456; // EGL adds a newer phase observation.
    display.update(1); assert(publications==count && published[1].presentationNS==123456);
    assert(queries==priorQueries); // A swap does not introduce X11 roundtrips.
    display._timingGeometryDirty=true; // Synthetic ConfigureNotify/mode events.
    geometry[1].x=1400; display.update(1);
    assert(published[1].outputID==20 && published[1].generation==2);
    geometry[1].x=960; display.outputs[0].primary=true; display.outputs[1].primary=false;
    display.update(1); assert(published[1].outputID==20); // Same spanning window retains output.
    display.outputs[1].rateNumerator=120; display.update(1); display.update(2);
    assert(published[1].rateNumerator==120 && published[2].rateNumerator==120);
    display.outputs[1].width=0; display.update(1); display.update(2);
    assert(published[1].outputID==70 && published[2].outputID==70);
    uint64_t oldGeneration=published[1].generation;
    geometry.erase(1); display.update(1); assert(!published.count(1));
    geometry[1]={0,0,1920,1080}; display.update(1);
    assert(published[1].generation>oldGeneration && published[1].presentationNS==0);
}
''')

    def test_public_mode_queries_use_explicit_display_mapping(self):
        source = runtime.CompatibilityChecks().patched(
            'cocotron', 'CoreGraphics/CGDirectDisplay.m')
        for name in ('CGDisplayAvailableModes', 'CGDisplayCopyDisplayMode',
                     'CGDisplayCopyAllDisplayModes', 'CGDisplaySetDisplayMode'):
            with self.subTest(name=name):
                body = runtime.function(source, name)
                self.assertIn('modeIndexForDisplayID(', body)
                self.assertNotIn(' - 1', body)

    def test_embedded_presenter_receives_only_nominal_rational_timing(self):
        source = runtime.CompatibilityChecks().patched(
            'cocotron', 'AppKit/X11.backend/X11Window.m')
        signature = '- (void) setNominalDisplayRateNumerator: (uint64_t) numerator denominator: (uint64_t) denominator {'
        method = source.split(signature, 1)[1].split('\n}', 1)[0]
        self.run_cpp(r'''
#include <cassert>
#include <cstdint>
static unsigned calls;
static uint32_t n,d;
static void setTiming(void* handle,uint32_t numerator,uint32_t denominator) {
    assert(handle==(void*)1); ++calls; n=numerator; d=denominator;
}
struct Window {
    void* _remoteFrameShm=(void*)1;
    void (*_remoteFrameShmSetTiming)(void*,uint32_t,uint32_t)=setTiming;
    void changed(uint64_t numerator,uint64_t denominator) {
''' + method + r'''
    }
};
int main() {
    Window window;
    window.changed(60000,1001); assert(calls==1 && n==60000 && d==1001);
    window.changed(144,1); assert(calls==2 && n==144 && d==1);
    window.changed(uint64_t(UINT32_MAX)+1,1); assert(calls==3 && n==0 && d==0);
    window._remoteFrameShmSetTiming=nullptr; window.changed(120,1); assert(calls==3);
    window._remoteFrameShmSetTiming=setTiming;
    window._remoteFrameShm=nullptr; window.changed(120,1); assert(calls==3);
}
''')
