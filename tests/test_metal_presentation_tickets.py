"""Exercise the game's QuartzCore Metal presentation lifecycle without a GPU.

Compile shipped method bodies together, adapting Objective-C syntax to C++.
CGL, the renderer, drawable allocation and the main queue are inert host fixtures.
Execution is deterministic and single-threaded; Objective-C runtime ownership,
concurrent scheduling and physical display behavior are outside this check.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_metal_drawable_pool as pool
import test_runtime_compatibility as sources


def method(source, signature):
    """Extract one Objective-C method, including nested blocks."""
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[opening + 1:end - 1]


def translate(body):
    replacements = (
        ('[self flushDrawableWithTiming]', 'CGLFlushDrawable(_glContext)'),
        ('dispatch_async(dispatch_get_main_queue(), ^{', 'dispatch_async([=]{'),
        ('[self->_drawableCondition lock]', '_drawableCondition->lock()'),
        ('[self->_drawableCondition unlock]', '_drawableCondition->unlock()'),
        ('[self retireSwappedPresentation]', 'retireSwappedPresentation()'),
        ('[self->_context render]', '_context->render()'),
        ('[self->_context flushForQueuedPresentation]', '_context->flushForQueuedPresentation()'),
        ('[_drawableCondition lock]', '_drawableCondition->lock()'),
        ('[_drawableCondition unlock]', '_drawableCondition->unlock()'),
        ('[_drawableCondition broadcast]', '_drawableCondition->broadcast()'),
        ('[_drawableCondition signal]', '_drawableCondition->signal()'),
        ('[_context didSwapRenderFrame: _preparedFrame]', '_context->didSwapRenderFrame(_preparedFrame)'),
        ('[self tryFlush]', 'tryFlush()'),
        ('[self startTimerIfNeeded]', 'startTimerIfNeeded()'),
        ('[self assignTextureIdsToLayerTree: layer]', 'assignTextureIdsToLayerTree(layer)'),
        ('[self prepareMetalLayers: layer frame: frame]', 'prepareMetalLayers(layer, frame)'),
        ('[_renderer render]', '_renderer->render()'),
        ('[[_deleteTextureIds count]]', '[1 + _deleteTextureIds.size()]'),
        ('for (NSNumber *number in _deleteTextureIds)', 'for (auto number : _deleteTextureIds)'),
        ('[number unsignedIntValue]', 'number'),
        ('[_deleteTextureIds removeAllObjects]', '_deleteTextureIds.clear()'),
        ('((MTLDeviceInternal*)self.device).device', '_device'),
        ('_context.glContext', '_context->_glContext'),
        # The fixture checks reentrancy and ownership order, not monitor locking.
        ('@synchronized(self)', 'if (true)'),
        ('(void*) self', '(void*) this'),
        ('(self,', '(this,'),
        ('self->', 'this->'),
        ('_frame.size', '_frame'),
        (' != NULL', ' != 0'),
    )
    for objc, cpp in replacements:
        body = body.replace(objc, cpp)
    return body


@unittest.skipUnless(shutil.which('c++') and shutil.which('patch'), 'C++ compiler and patch required')
class MetalPresentationTicketChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        patched = sources.CompatibilityChecks().patched
        layer = patched('cocotron', 'QuartzCore/CAMetalLayer.mm')
        context = patched('cocotron', 'QuartzCore/CALayerContext.m')
        drawable = patched('cocotron', 'QuartzCore/CAMetalDrawable.mm')
        context_methods = {
            'void renderLayer(Layer* layer)': '- (void) renderLayer:',
            'bool didSwapRenderFrame(uint64_t frame)': '- (BOOL) didSwapRenderFrame:',
            'bool tryFlush()': '- (BOOL) tryFlush',
            'void flush()': '- (void) flush {',
            'void flushForQueuedPresentation()': '- (void) flushForQueuedPresentation',
        }
        layer_methods = {
            'void retireSwappedPresentation()': '- (void)retireSwappedPresentation',
            'bool queuePresent(NSUInteger drawableID, CAMetalDrawableActual* drawable)':
                '- (BOOL)queuePresent: (NSUInteger)drawableID fromDrawable:',
            'void releaseDrawable(NSUInteger drawableID, CAMetalDrawableActual* drawable)':
                '- (void)releaseDrawable: (NSUInteger)drawableID fromDrawable:',
            'void recreateDrawables()': '- (void)recreateDrawables\n{',
            'void prepareRenderForFrame(uint64_t frame, Context* context)':
                '- (void)prepareRenderForFrame:',
        }
        definitions = ''
        for owner, source, methods in (('Context', context, context_methods), ('Layer', layer, layer_methods)):
            for declaration, signature in methods.items():
                result, name = declaration.split(' ', 1)
                definitions += f'\n{result} {owner}::{name} {{\n{translate(method(source, signature))}\n}}\n'
        for name in ('didPresent', 'didDrop'):
            definitions += sources.function(drawable, 'CAMetalDrawableActual::' + name)
        _, loop = pool.MetalDrawablePoolChecks().wait_loop()
        definitions += '\nbool Layer::waitForDrawable() {\n' + translate(loop) + '\nreturn _usableDrawablesBitmap != 0;\n}\n'
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        path = Path(folder.name)
        cls.executable = path / 'tickets'
        (path / 'tickets.cpp').write_text(FIXTURE + definitions + SCENARIOS)
        result = subprocess.run(['c++', '-std=c++17', str(path / 'tickets.cpp'), '-o', str(cls.executable)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr)

    def check_scenario(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inline_swap_suppresses_only_the_served_callback(self):
        self.check_scenario('inline')

    def test_queued_drawables_present_in_fifo_order(self):
        self.check_scenario('fifo')

    def test_failed_swap_keeps_the_callback_live(self):
        self.check_scenario('swap-failure')

    def test_failed_attach_keeps_the_drawable_queued(self):
        self.check_scenario('attach-failure')

    def test_queued_swap_failure_requests_a_timer_retry(self):
        self.check_scenario('timer-retry')

    def test_stale_render_generation_cannot_retire_a_ticket(self):
        self.check_scenario('stale-frame')

    def test_pool_recreation_retires_old_callbacks_and_rejects_old_slots(self):
        self.check_scenario('recreate')

    def test_reentrant_presented_handler_keeps_the_new_texture_alive(self):
        self.check_scenario('reentrant')

    def test_two_metal_layers_retire_independently_in_one_context(self):
        self.check_scenario('layers')

    def test_two_inline_swaps_retire_both_callbacks(self):
        self.check_scenario('two-inline')

    def test_intervening_render_preserves_retirement_of_a_swapped_ticket(self):
        self.check_scenario('intervening-render')

    def test_queue_capacity_rejection_does_not_issue_a_ticket(self):
        self.check_scenario('queue-full')

    def test_unmatched_context_cannot_confirm_presentation(self):
        self.check_scenario('wrong-context')

    def test_drop_handler_can_install_a_new_callback(self):
        self.check_scenario('drop-handler')


FIXTURE = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <functional>
#include <memory>
#include <string>
#include <vector>
#include <unistd.h>
using BOOL=bool;
using NSUInteger=size_t;
using CFTimeInterval=double;
using GLint=int;
using GLsizei=int;
using GLuint=unsigned;
using CGLContextObj=int;
constexpr bool YES=true, NO=false;
constexpr int kCGLNoError=0, kCGLCPSwapInterval=1, GL_BACK=1, GL_DEPTH_TEST=2,
 GL_SMOOTH=3, GL_PROJECTION=4, GL_RGBA=5, GL_UNSIGNED_BYTE=6, GL_TEXTURE_2D=7,
 GL_RGBA8=8, GL_TEXTURE_MAG_FILTER=9, GL_TEXTURE_MIN_FILTER=10, GL_NEAREST=11,
 GL_TEXTURE_WRAP_S=12, GL_TEXTURE_WRAP_T=13, GL_REPEAT=14, GL_TEXTURE_SWIZZLE_A=15, GL_ONE=16;
struct Size { double width=1920, height=1080; };
enum class MTLPixelFormat { BGRA8Unorm, BGRA8Unorm_sRGB, RGBA16Float, RGB10A2Unorm,
 BGR10A2Unorm, BGRA10_XR, BGRA10_XR_sRGB, BGR10_XR, BGR10_XR_sRGB };
constexpr auto MTLPixelFormatBGRA8Unorm=MTLPixelFormat::BGRA8Unorm;
constexpr auto MTLPixelFormatBGRA8Unorm_sRGB=MTLPixelFormat::BGRA8Unorm_sRGB;
constexpr auto MTLPixelFormatRGBA16Float=MTLPixelFormat::RGBA16Float;
constexpr auto MTLPixelFormatRGB10A2Unorm=MTLPixelFormat::RGB10A2Unorm;
constexpr auto MTLPixelFormatBGR10A2Unorm=MTLPixelFormat::BGR10A2Unorm;
constexpr auto MTLPixelFormatBGRA10_XR=MTLPixelFormat::BGRA10_XR;
constexpr auto MTLPixelFormatBGRA10_XR_sRGB=MTLPixelFormat::BGRA10_XR_sRGB;
constexpr auto MTLPixelFormatBGR10_XR=MTLPixelFormat::BGR10_XR;
constexpr auto MTLPixelFormatBGR10_XR_sRGB=MTLPixelFormat::BGR10_XR_sRGB;
namespace Indium {
enum class PixelFormat { BGRA8Unorm, BGRA8Unorm_sRGB, RGBA16Float, RGB10A2Unorm,
 BGR10A2Unorm, BGRA10_XR, BGRA10_XR_sRGB, BGR10_XR, BGR10_XR_sRGB, Invalid };
namespace Waits { struct Block { Block(const char*){} }; }
}
struct Condition {
 bool locked=false;
 void lock(){ assert(!locked); locked=true; }
 void unlock(){ assert(locked); locked=false; }
 void signal(){ assert(locked); }
 void broadcast(){ assert(locked); }
};
struct Layer;
struct Context;
static std::atomic<void*> mainThreadDrawableWaiter{nullptr};
static std::deque<std::function<void()>> callbacks;
static Condition* submittingCondition;
static bool failAttach=false, failSwap=false;
static int current=2, swaps=0;
static double CACurrentMediaTime(){ return 1; }
static double presentIntervalNow(){ return 1; }
struct Sample { std::atomic<double> renderStart{0}, renderDone{0}, flushDone{0}; };
static Sample* beginPresentIntervalSample(){ return nullptr; }
static void darlingMetalTrace(const char*,long,long){}
static void darlingMetalTiming(const char*,double){}
static void darlingQCTiming(const char*,double){}
static bool darlingMetalDebug(){ return false; }
static void reportGLErrors(){}
static void dispatch_async(std::function<void()> callback){
 assert(submittingCondition && submittingCondition->locked);
 callbacks.push_back(std::move(callback));
}
static void runCallback(){
 assert(!callbacks.empty()); auto callback=std::move(callbacks.front());
 callbacks.pop_front(); callback();
}
static int CGLGetCurrentContext(){ return current; }
static int CGLSetCurrentContext(int value){ current=value; return 0; }
static int CGLContextMakeCurrentAndAttachToWindow(int context,int){
 if(failAttach)return 1; current=context; return 0;
}
static int CGLSetParameter(int,int,const int*){ return 0; }
static int CGLFlushDrawable(int){ ++swaps; return failSwap?1:0; }
static bool skipLocalRender(Layer*,Size){ return false; }
static int metalSwapInterval(Layer*){ return 0; }
template<class... T> void glDrawBuffer(T...){}
template<class... T> void glReadBuffer(T...){}
template<class... T> void glEnable(T...){}
template<class... T> void glShadeModel(T...){}
template<class... T> void glViewport(T...){}
template<class... T> void glMatrixMode(T...){}
template<class... T> void glLoadIdentity(T...){}
template<class... T> void glOrtho(T...){}
template<class... T> void glDeleteTextures(T...){}
template<class... T> void glCreateTextures(T...){}
template<class... T> void glTextureStorage2D(T...){}
template<class... T> void glTextureParameteri(T...){}
template<class... T> void glGetTextureImage(T...){}
struct Texture {
 int releases=0;
 void releasePersistentPresentationSemaphore(){ ++releases; }
 int glTexture(){ return 1; }
};
struct CAMetalDrawableActual {
 Layer* owner; size_t id; bool disowned=false;
 int syncs=0;
 bool _waitedPersistentSemaphore=false;
 double _presentedTime=0;
 std::shared_ptr<Texture> _texture=std::make_shared<Texture>();
 std::function<void()> _didPresentCallback;
 CAMetalDrawableActual(Layer* o,Size,size_t i,Indium::PixelFormat,void*,int):owner(o),id(i){}
 void disown(){ disowned=true; }
 void synchronizeRender(){ ++syncs; }
 std::shared_ptr<Texture> texture(){ return _texture; }
 void didPresent();
 void didDrop();
};
struct Renderer {
 int renders=0;
 std::vector<size_t> textures;
 Context* context;
 void render();
};
struct Context {
 int _glContext=1, _cglWindow=1;
 uint64_t _renderFrame=0, _readyFrame=0, _swappedFrame=0;
 Size _frame;
 std::vector<GLuint> _deleteTextureIds;
 Layer* _layer=nullptr;
 Renderer renderer{0,{},this};
 Renderer* _renderer=&renderer;
 std::vector<Layer*> layers;
 bool timerPending=false;
 void renderLayer(Layer* layer);
 bool didSwapRenderFrame(uint64_t frame);
 bool tryFlush();
 void flush();
 void flushForQueuedPresentation();
 void assignTextureIdsToLayerTree(Layer*){}
 void prepareMetalLayers(Layer*,uint64_t frame);
 void startTimerIfNeeded(){ timerPending=true; }
 // Surface selection is covered separately by test_x11_vrr.py. Drive the
 // shipped renderLayer body here, with the fixture surface already attached.
 void render(){ if(_layer && _cglWindow)renderLayer(_layer); }
 // Model delivery of the one-shot timer requested by the shipped flush body.
 void tick(){ assert(timerPending); timerPending=false; render(); flush(); }
};
struct Layer {
 Context* _context;
 Condition condition;
 Condition* _drawableCondition=&condition;
 void* _device=(void*)1;
 Size _drawableSize;
 MTLPixelFormat _pixelFormat=MTLPixelFormatBGRA8Unorm;
 GLuint _tex=0;
 NSUInteger _maximumDrawableCount=3, _queuedDrawableCount=0;
 std::array<std::shared_ptr<CAMetalDrawableActual>,3> _drawables{};
 std::array<NSUInteger,3> _queuedDrawables{};
 std::shared_ptr<CAMetalDrawableActual> _lastPresentedDrawable;
 uint8_t _usableDrawablesBitmap=0;
 uint64_t _issuedPresentation=0, _retiredPresentation=0, _preparedPresentation=0, _preparedFrame=0;
 bool _allowsNextDrawableTimeout=true;
 Layer(Context& context):_context(&context){
  context.layers.push_back(this); if(!context._layer)context._layer=this;
  recreateDrawables();
 }
 bool queuePresent(NSUInteger,CAMetalDrawableActual*);
 void releaseDrawable(NSUInteger,CAMetalDrawableActual*);
 void retireSwappedPresentation();
 void recreateDrawables();
 void prepareRenderForFrame(uint64_t,Context*);
 bool waitForDrawable();
 void conditionLock(){ condition.lock(); }
 void conditionUnlock(){ condition.unlock(); }
 bool isMainThread(){ return true; }
 bool conditionWaitUntilDate(){ return false; }
 void conditionWait(){ assert(false); }
 void contextRender(){ _context->render(); }
 void contextFlush(){ _context->flush(); }
 bool submit(size_t id){
  submittingCondition=&condition;
  return queuePresent(id,_drawables.at(id).get());
 }
 void holdAll(){ _usableDrawablesBitmap=0; }
};
void Context::prepareMetalLayers(Layer*,uint64_t frame){
 for(auto* layer:layers)layer->prepareRenderForFrame(frame,this);
}
void Renderer::render(){
 ++renders;
 for(auto* layer:context->layers)
  if(layer->_lastPresentedDrawable)textures.push_back(layer->_lastPresentedDrawable->id);
}
'''


SCENARIOS = r'''
int main(int argc,char** argv){
 assert(argc==2); std::string scenario=argv[1];
 Context c; Layer layer(c);
 if(scenario=="inline"){
  layer.holdAll(); layer._lastPresentedDrawable=layer._drawables[0];
  auto old=layer._drawables[0]; old->_didPresentCallback=[&]{layer.releaseDrawable(0,old.get());};
  assert(layer.submit(1) && layer.submit(2));
  layer.condition.lock(); assert(layer.waitForDrawable()); layer.condition.unlock();
  assert(c.renderer.renders==1 && swaps==1 && layer._lastPresentedDrawable->id==1);
  runCallback(); // Ticket 1 was already swapped inline.
  assert(c.renderer.renders==1 && swaps==1 && layer._retiredPresentation==1);
  assert(layer._queuedDrawableCount==1);
  runCallback(); // Ticket 2 still needs its own render and swap.
  assert(c.renderer.renders==2 && swaps==2 && layer._lastPresentedDrawable->id==2);
 }else if(scenario=="fifo"){
  assert(layer.submit(2) && layer.submit(0) && layer.submit(1));
  runCallback(); runCallback(); runCallback();
  assert((c.renderer.textures==std::vector<size_t>{2,0,1}));
  assert(c.renderer.renders==3 && swaps==3);
 }else if(scenario=="swap-failure"){
  assert(layer.submit(1)); c.render(); failSwap=true; c.flush();
  assert(!c.didSwapRenderFrame(layer._preparedFrame));
  failSwap=false; runCallback();
  assert(c.renderer.renders==2 && swaps==2 && layer._retiredPresentation==0);
  layer.condition.lock(); layer.retireSwappedPresentation(); layer.condition.unlock();
  assert(layer._retiredPresentation==1);
 }else if(scenario=="attach-failure"){
  assert(layer.submit(1)); failAttach=true; c.render(); c.flush();
  assert(c.renderer.renders==0 && swaps==0 && layer._queuedDrawableCount==1);
  failAttach=false; runCallback();
  assert(c.renderer.renders==1 && swaps==1 && layer._lastPresentedDrawable->id==1);
 }else if(scenario=="timer-retry"){
  assert(layer.submit(1)); failSwap=true; runCallback();
  assert(c.timerPending && layer._retiredPresentation==0 && c.renderer.renders==1);
  failSwap=false; c.tick();
  layer.condition.lock(); layer.retireSwappedPresentation(); layer.condition.unlock();
  assert(!c.timerPending && layer._retiredPresentation==1 && swaps==2);
 }else if(scenario=="stale-frame"){
  assert(layer.submit(1)); c.render(); ++c._renderFrame;
  c.flush(); assert(!c.didSwapRenderFrame(layer._preparedFrame));
  runCallback(); assert(c.renderer.renders==2 && swaps==2);
 }else if(scenario=="recreate"){
  auto old=layer._drawables[1];
  int drops=0; old->_didPresentCallback=[&]{++drops;};
  assert(layer.submit(1)); layer.recreateDrawables();
  assert(drops==1 && old->disowned && layer._retiredPresentation==1);
  assert(!layer.queuePresent(1,old.get()));
  layer._usableDrawablesBitmap &= ~(1u<<1);
  layer.releaseDrawable(1,old.get()); assert(!(layer._usableDrawablesBitmap & (1u<<1)));
  layer.releaseDrawable(1,layer._drawables[1].get());
  assert(layer._usableDrawablesBitmap & (1u<<1));
  assert(!layer.queuePresent(99,nullptr));
  assert(layer.submit(2)); runCallback(); // Old callback cannot consume the new pool's drawable.
  assert(c.renderer.renders==0 && swaps==0 && layer._queuedDrawableCount==1);
  runCallback(); assert(c.renderer.renders==1 && layer._issuedPresentation==2);
 }else if(scenario=="reentrant"){
  layer._lastPresentedDrawable=layer._drawables[0];
  auto old=layer._drawables[0];
  old->_waitedPersistentSemaphore=true;
  int presented=0;
  old->_didPresentCallback=[&]{
   ++presented; assert(layer._lastPresentedDrawable->id==1);
   c.render(); c.flush(); // Handler is allowed to reenter the compositor.
   old->_didPresentCallback=[&]{++presented;};
  };
  assert(layer.submit(1)); c.render(); c.flush();
  assert(presented==1 && layer._lastPresentedDrawable->id==1);
  assert(old->_texture->releases==1);
  old->didPresent(); assert(presented==2); // New callback was not cleared by the outer call.
  runCallback(); assert(callbacks.empty());
 }else if(scenario=="layers"){
  Layer other(c);
  assert(layer.submit(1) && other.submit(2));
  c.render(); c.flush();
  runCallback(); runCallback();
  assert(c.renderer.renders==1 && swaps==1);
  assert(layer._retiredPresentation==1 && other._retiredPresentation==1);
 }else if(scenario=="two-inline"){
  layer.holdAll(); auto first=layer._drawables[0];
  first->_didPresentCallback=[&]{layer.releaseDrawable(0,first.get());};
  assert(layer.submit(0) && layer.submit(1) && layer.submit(2));
  layer.condition.lock(); assert(layer.waitForDrawable()); layer.condition.unlock();
  assert(c.renderer.renders==2 && swaps==2 && layer._queuedDrawableCount==1);
  runCallback(); runCallback();
  assert(c.renderer.renders==2 && swaps==2 && layer._retiredPresentation==2);
  runCallback(); assert(c.renderer.renders==3 && swaps==3);
 }else if(scenario=="intervening-render"){
  assert(layer.submit(1)); c.render(); c.flush();
  c.render(); // An unrelated redraw must retire the old generation before replacing it.
  assert(layer._retiredPresentation==1);
  runCallback(); assert(c.renderer.renders==2 && swaps==1);
 }else if(scenario=="queue-full"){
  assert(layer.submit(0) && layer.submit(1) && layer.submit(2));
  assert(!layer.submit(0) && layer._issuedPresentation==3 && callbacks.size()==3);
  runCallback(); runCallback(); runCallback();
  assert(c.renderer.renders==3);
 }else if(scenario=="wrong-context"){
  assert(layer.submit(1)); Context other;
  layer.prepareRenderForFrame(1,&other);
  c._renderFrame=c._readyFrame=c._swappedFrame=1;
  layer.condition.lock(); layer.retireSwappedPresentation(); layer.condition.unlock();
  assert(layer._preparedFrame==0 && layer._retiredPresentation==0);
  runCallback(); assert(c.renderer.renders==1 && swaps==1);
 }else if(scenario=="drop-handler"){
  auto old=layer._drawables[1]; int drops=0;
  old->_waitedPersistentSemaphore=true;
  old->_didPresentCallback=[&]{++drops; old->_didPresentCallback=[&]{++drops;};};
  old->didDrop(); assert(drops==1 && old->_texture->releases==1 && old->_presentedTime==0);
  old->didDrop(); old->didDrop();
  assert(drops==2 && old->_texture->releases==1);
 }else{ assert(false); }
 assert(callbacks.empty());
 assert(!layer.condition.locked);
 puts("PASS");
}
'''
