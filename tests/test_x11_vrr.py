"""Compile the patched VRR eligibility and drawable ownership methods with inert fixtures."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import test_runtime_compatibility as sources


def body(source, signature):
    return source.split(signature + ' {', 1)[1].split('\n}', 1)[0]


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class VrrChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def run_cpp(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'check.cpp').write_text('#include <cstddef>\n' + source)
            result = subprocess.run(['c++', '-std=c++17', str(path/'check.cpp'), '-o', str(path/'check')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path/'check')], check=True, timeout=10)

    def test_hints_only_allow_vrr_for_fullscreen_parent_presentation(self):
        source = self.patched('cocotron', 'AppKit/X11.backend/X11Window.m')
        method = body(source, '- (void) updateLayerPresentationHints: (BOOL) fullscreen')
        getter = body(source, '- (void *) layerDrawableFor: (id) owner')
        acquire = body(source, '- (void) acquireLayerDrawable: (id) owner').replace('[self updateLayerPresentationHints: _fullscreenRequested]', 'hints(_fullscreenRequested)')
        release = body(source, '- (void) releaseLayerDrawable: (id) owner').replace('[self updateLayerPresentationHints: _fullscreenRequested]', 'hints(_fullscreenRequested)')
        self.run_cpp(r'''
#include <cassert>
#include <cstring>
using BOOL=bool;
constexpr auto nil=nullptr;
constexpr int False=0, XA_CARDINAL=6, PropModeReplace=0;
static bool game=true;
static int adaptive=-1,bypass=-1,calls=0;
static bool X11IsLeagueGameWindow(int,int,int) { return game; }
static const char* XInternAtom(int,const char* name,int) { return name; }
static int XChangeProperty(int,int,const char* name,int type,int bits,int mode,unsigned char* data,int count) {
    assert(type==XA_CARDINAL && bits==32 && mode==PropModeReplace && count==1);
    int value=*(unsigned long*)data; ++calls;
    if(!strcmp(name,"_VARIABLE_REFRESH")) adaptive=value;
    else { assert(!strcmp(name,"_NET_WM_BYPASS_COMPOSITOR")); bypass=value; }
    return 0;
}
struct Window {
 int _display=1,_window=1,_delegate=1;
 void* _layerDrawableOwner=nullptr;
 void* _cglWindow=(void*)3; bool _hasAlpha=false,_fullscreenRequested=true;
 void hints(BOOL fullscreen) {
''' + method + '\n }\n void* drawable(void* owner) {' + getter + '\n}\n void acquire(void* owner) {' + acquire + '\n}\n void release(void* owner) {' + release + r'''
 }
};
int main() {
 Window w;
 w.hints(true); assert(adaptive==0 && bypass==2);
 w._layerDrawableOwner=&w;
 w.hints(true); assert(adaptive==1 && bypass==0);
 w.hints(false); assert(adaptive==0 && bypass==0);
 w._layerDrawableOwner=nullptr;
 w.hints(true); assert(adaptive==0 && bypass==2);
 int before=calls; game=false; w.hints(true); assert(calls==before);
 assert(!w.drawable(&w)); game=true;
 int a,b;
 assert(w.drawable(&a)==(void*)3);
 w.acquire(&a); assert(w.drawable(&a)==(void*)3 && !w.drawable(&b));
 w.acquire(&b); assert(w._layerDrawableOwner==&a);
 w.release(&b); assert(w._layerDrawableOwner==&a);
 w.release(&a); assert(!w._layerDrawableOwner && w.drawable(&b)==(void*)3);
 w._hasAlpha=true; assert(!w.drawable(&a));
}
''')

    def test_subwindows_borrow_only_when_visible_borderless_and_covering_parent(self):
        source = self.patched('cocotron', 'AppKit/X11.backend/X11SubWindow.m')
        method = body(source, '- (void *) sharedCGLWindow')
        replacements = {'[_parent frame]':'_parent->frame', '[_parent styleMask]':'_parent->style',
                        '[_parent layerDrawableFor: self]':'_parent->drawable(this)',
                        '[_parent acquireLayerDrawable: self]':'_parent->acquire(this)',
                        '[_parent releaseLayerDrawable: self]':'_parent->release(this)'}
        for old,new in replacements.items():
            method=method.replace(old,new)
        self.run_cpp(r'''
#include <cassert>
using BOOL=bool;
struct CGRect { struct {double x,y;} origin; struct {double width,height;} size; };
static CGRect CGRectMake(double x,double y,double w,double h) {return {{x,y},{w,h}};}
static bool CGRectEqualToRect(CGRect a,CGRect b) {return a.origin.x==b.origin.x && a.origin.y==b.origin.y && a.size.width==b.size.width && a.size.height==b.size.height;}
constexpr int NSTitledWindowMask=1;
static bool mapped;
static void XMapWindow(int,int) {mapped=true;}
static void XUnmapWindow(int,int) {mapped=false;}
struct Parent {
 CGRect frame=CGRectMake(0,0,800,600); int style=0; bool eligible=true;
 void* owner=nullptr; void* surface=this;
 void* drawable(void* requester) {return eligible && (!owner || owner==requester) ? surface : nullptr;}
 void acquire(void* requester) {assert(!owner); owner=requester;}
 void release(void* requester) {assert(owner==requester); owner=nullptr;}
};
struct Child {
 Parent* _parent; CGRect _drawableFrame=CGRectMake(0,0,800,600);
 BOOL _visible=true,_usingParentDrawable=false,_allowsParentDrawable=true; int _display=1,_window=2;
 void* shared() {
''' + method + r'''
 }
};
int main() {
 Parent p; Child a{&p}; Child b{&p};
 assert(a.shared()==p.surface && !mapped && p.owner==&a);
 assert(a.shared()==p.surface); // Repeated lookup does not reacquire.
 assert(b.shared()==nullptr && mapped); // A second view cannot share ownership.
 a._visible=false; assert(!a.shared() && !mapped && !p.owner);
 a._visible=true; assert(a.shared()==p.surface);
 a._drawableFrame.size.width=400;
 assert(!a.shared() && mapped && !p.owner);
 a._drawableFrame=CGRectMake(0,0,800,600); p.style=NSTitledWindowMask;
 assert(!a.shared() && mapped);
 p.style=0; p.eligible=false; assert(!a.shared() && mapped);
 p.eligible=true; a._allowsParentDrawable=false; assert(!a.shared() && mapped);
 a._allowsParentDrawable=true; a._drawableFrame.origin.x=1; assert(!a.shared() && mapped);
 a._drawableFrame.origin.x=0; assert(a.shared()==p.surface && !mapped);
}
''')

    def test_switching_surfaces_destroys_only_owned_child_drawables(self):
        source = self.patched('cocotron', 'QuartzCore/CALayerContext.m')
        method = body(source, '- (void) updateDrawable')
        method=method.replace('[_subwindow setAllowsParentDrawable: hasMetalLayer(_layer)]', '_subwindow->allow(hasMetalLayer(_layer))')
        method=method.replace('[_subwindow sharedCGLWindow]', '_subwindow->shared').replace('[_subwindow nativeWindow]', '_subwindow->native')
        self.run_cpp(r'''
#include <cassert>
#include <cstdint>
using BOOL=bool;
using CGLContextObj=int;
static int current=0,destroyed=0,created=0;
static void* destroyedSurface;
static int CGLGetCurrentContext() {return current;}
static void CGLSetCurrentContext(int c) {current=c;}
static void CGLDestroyWindow(void* surface) {assert(surface==(void*)10); destroyedSurface=surface; ++destroyed;}
static void* CGLGetWindow(void*) {++created; return (void*)10;}
static void CGLContextMakeCurrentAndAttachToWindow(int c,void*) {current=c;}
static bool hasMetalLayer(int layer) {return layer!=0;}
struct Subwindow {void* shared=nullptr; void* native=(void*)2; void allow(bool enabled) {assert(enabled);} };
struct Context {
 Subwindow* _subwindow; int _layer=1; int _glContext=1; void* _cglWindow=nullptr; bool _ownsCglWindow=false;
 uint64_t _renderFrame=0;
 void update() {
''' + method.replace('CGLSetCurrentContext(NULL)', 'CGLSetCurrentContext(0)') + r'''
 }
};
int main() {
 Subwindow sub; Context c{&sub};
 c.update(); assert(created==1 && c._ownsCglWindow && c._renderFrame==1); // Ordinary clients still get a child.
 c.update(); assert(created==1 && destroyed==0 && c._renderFrame==1);
 current=1; sub.shared=(void*)20;
 c.update(); assert(destroyed==1 && destroyedSurface==(void*)10 && !c._ownsCglWindow && current==1 && c._renderFrame==2);
 c.update(); assert(destroyed==1);
 sub.shared=(void*)30; c.update(); assert(destroyed==1 && c._cglWindow==(void*)30);
 sub.shared=nullptr; c.update(); assert(created==2 && destroyed==1 && c._ownsCglWindow);
 current=2; sub.shared=(void*)20; c.update(); assert(current==2 && destroyed==2);
 c._subwindow=nullptr; c.update(); assert(destroyed==2);
}
''')
