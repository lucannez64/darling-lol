"""Exercise the patched presentation policy and CGL call order without a GPU."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import test_runtime_compatibility as sources


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class MetalVSyncChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def test_explicit_vsync_is_scoped_to_each_cgl_context(self):
        source = self.patched('darling', 'src/frameworks/OpenGL/OpenGL.c')
        methods = '\n'.join(sources.function(source, name) for name in ('CGLFlushDrawable', 'CGLSetParameter'))
        fixture = r'''
#include <cassert>
#include <cstdio>
using GLint=int;
using CGLError=int;
using CGLContextParameter=int;
using EGLBoolean=int;
constexpr int kCGLNoError=0, kCGLBadContext=1, kCGLBadState=2, kCGLBadAddress=3, kCGLBadValue=4;
constexpr int kCGLCPSwapInterval=1;
struct Context { int egl_display=1, egl_surface=1, swap_interval=1, swap_interval_requested=0; };
using CGLContextObj=Context*;
static Context* current;
static int display=1, applied=-1, swapIntervalCalls=0;
static bool fail=false;
static int swapInterval(int, int value) { ++swapIntervalCalls; if(fail) return 0; applied=value; return 1; }
static auto p_eglSwapInterval=swapInterval;
static int p_eglSwapBuffers(int,int) { return 1; }
static Context* CGLGetCurrentContext() { return current; }
''' + methods + r'''
int main() {
    Context a, b;
    current=&a;
    int interval=1;
    assert(CGLSetParameter(&a,kCGLCPSwapInterval,&interval)==0 && applied==1);
    assert(swapIntervalCalls==1);
    assert(CGLSetParameter(&a,kCGLCPSwapInterval,&interval)==0 && swapIntervalCalls==1);
    CGLFlushDrawable(&a); assert(applied==1);
    current=&b;
    CGLFlushDrawable(&b); assert(applied==0); // Another window retains its default.
    current=&a;
    interval=0;
    assert(CGLSetParameter(&a,kCGLCPSwapInterval,&interval)==0 && applied==0);
    assert(swapIntervalCalls==3);
    interval=1;
    assert(CGLSetParameter(&a,kCGLCPSwapInterval,&interval)==0 && applied==1);
    assert(swapIntervalCalls==4);
    assert(CGLSetParameter(&a,kCGLCPSwapInterval,&interval)==0 && swapIntervalCalls==4);
    current=&b; // EGL applies the interval to the current surface: never skip it for a non-current context.
    assert(CGLSetParameter(&a,kCGLCPSwapInterval,&interval)==0 && swapIntervalCalls==5);
    current=&b; fail=true;
    assert(CGLSetParameter(&b,kCGLCPSwapInterval,&interval)==kCGLBadValue);
    assert(!b.swap_interval_requested); // Failed requests must not change policy.
    fail=false; CGLFlushDrawable(&b); assert(applied==0);
    assert(CGLFlushDrawable(nullptr)==kCGLBadContext);
}
'''
        self.run_cpp(fixture)

    def test_nested_layers_toggles_windows_and_context_failures(self):
        source = self.patched('cocotron', 'QuartzCore/CALayerContext.m')
        policy = sources.function(source, 'metalSwapInterval')
        # Translate only Objective-C message/iteration syntax; compile the actual
        # policy and flush body against inert layer and CGL fixtures.
        policy = policy.replace('[[layer class] isSubclassOfClass: [CAMetalLayerInternal class]]', 'layer->metal')
        policy = policy.replace('[(CAMetalLayer *)layer displaySyncEnabled]', 'layer->sync')
        policy = policy.replace('for (CALayer *child in layer.sublayers)', 'for (CALayer *child : layer->children)')
        flush = source.split('- (void) flush {', 1)[1].split('\n}\n', 1)[0]
        try_flush = source.split('- (BOOL) tryFlush {', 1)[1].split('\n}\n', 1)[0]
        did_swap = source.split('- (BOOL) didSwapRenderFrame: (uint64_t) frame {', 1)[1].split('\n}\n', 1)[0]
        fixture = r'''
#include <cassert>
#include <cstdint>
#include <vector>
using BOOL = bool;
constexpr bool YES = true, NO = false;
using GLint = int;
using CGLContextObj = int;
constexpr int kCGLNoError = 0, kCGLCPSwapInterval = 1;
constexpr int NULL_CONTEXT = 0;
struct CALayer { bool metal=false, sync=true; std::vector<CALayer*> children; };
struct Context { int interval=-2, swaps=0; } contexts[3];
static int current=2, attached=0;
static bool fail=false, failSwap=false, skip=false;
static int CGLGetCurrentContext() { return current; }
static int CGLSetCurrentContext(int context) { current=context; return 0; }
static int CGLContextMakeCurrentAndAttachToWindow(int context, int window) {
    if(fail) return 1;
    current=context; attached=window; return 0;
}
static int CGLSetParameter(int context, int parameter, const int* interval) {
    assert(current==context && attached==context && parameter==kCGLCPSwapInterval);
    contexts[context].interval=*interval; return 0;
}
static int CGLFlushDrawable(int context) {
    assert(current==context && attached==context);
    ++contexts[context].swaps; return failSwap ? 1 : 0;
}
static bool skipLocalRender(CALayer*, int) { return skip; }
'''
        fixture += policy
        fixture += '\nstruct Window { int _glContext=1, _cglWindow=1, _frame=0; CALayer* _layer;'
        fixture += '\nuint64_t _renderFrame=1, _readyFrame=1, _swappedFrame=0;'
        fixture += '\nbool didSwapRenderFrame(uint64_t frame) {' + did_swap + '\n}'
        fixture += '\nbool tryFlush() {'
        fixture += try_flush.replace('NULL', 'NULL_CONTEXT').replace('_frame.size', '_frame') + '\n}'
        fixture += '\nvoid flush() {' + flush.replace('[self tryFlush]', 'tryFlush()') + '\n} };\n'
        fixture += r'''
int main() {
    CALayer root, container, metal, other;
    metal.metal=other.metal=true;
    root.children={&container}; container.children={&metal};
    assert(metalSwapInterval(&root)==1);
    Window a{1,1,0,&root};
    a.flush(); assert(contexts[1].interval==1 && current==2);
    assert(a.didSwapRenderFrame(1) && !a.didSwapRenderFrame(0));
    metal.sync=false;
    a.flush(); assert(contexts[1].interval==0 && current==2);
    container.children.push_back(&other);
    a.flush(); assert(contexts[1].interval==1);
    other.sync=false;
    a.flush(); assert(contexts[1].interval==0);
    CALayer plain;
    assert(metalSwapInterval(&plain)==-1);
    Window b{2,2,0,&plain};
    b.flush(); assert(contexts[2].interval==-2);
    other.sync=true;
    a.flush(); assert(contexts[1].interval==1 && contexts[2].interval==-2);
    a._cglWindow=0;
    int swaps=contexts[1].swaps;
    a.flush(); assert(contexts[1].swaps==swaps);
    a._cglWindow=1; fail=true;
    a.flush(); assert(contexts[1].swaps==swaps && current==2);
    fail=false; skip=true;
    a.flush(); assert(contexts[1].swaps==swaps);
    skip=false; current=0;
    a.flush(); assert(current==0);
    current=1; a.flush(); assert(current==1);
    root.children.clear();
    assert(metalSwapInterval(&root)==-1);
    a._readyFrame=a._renderFrame=2;
    failSwap=true; current=2;
    assert(!a.tryFlush() && !a.didSwapRenderFrame(2) && current==2);
    failSwap=false;
    assert(a.tryFlush() && a.didSwapRenderFrame(2) && current==2);
    a._renderFrame=3; // A stale render must not retire a newer presentation.
    assert(!a.tryFlush() && !a.didSwapRenderFrame(3) && current==2);
    a._readyFrame=0;
    assert(!a.tryFlush() && current==2);
}
'''
        self.run_cpp(fixture)

    def run_cpp(self, fixture):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'check.cpp').write_text(fixture)
            result = subprocess.run(['c++', '-std=c++17', str(path/'check.cpp'), '-o', str(path/'check')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path/'check')], check=True, timeout=10)
