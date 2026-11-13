"""Run the patched CAMetalLayer drawable hand-out against a deterministic pool, without a GPU.

A presented drawable returns to the pool only when the compositor block that
queuePresent: sends to the main queue runs. A main thread that asks for a
drawable while the pool is empty cannot run that block, so nextDrawable has to
present the queued drawables itself instead of timing out after one second.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import test_runtime_compatibility as sources


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class MetalDrawablePoolChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def wait_loop(self):
        source = self.patched('cocotron', 'QuartzCore/CAMetalLayer.mm')
        method = source.split('- (id<CAMetalDrawable>)nextDrawable\n{', 2)[2]
        loop = method[method.index('\tint onMainThread = -1;'):method.index('\tif (_usableDrawablesBitmap == 0) {\n\t\t// timeout reached')]
        # Translate only the Objective-C message syntax; the loop itself is the shipped code.
        for objc, cpp in (('[NSThread isMainThread]', 'isMainThread()'),
                          ('[_drawableCondition unlock]', 'conditionUnlock()'),
                          ('[_drawableCondition lock]', 'conditionLock()'),
                          ('[_drawableCondition waitUntilDate: endTime]', 'conditionWaitUntilDate()'),
                          ('[_drawableCondition wait]', 'conditionWait()'),
                          ('[_context render]', 'contextRender()'),
                          ('[_context flush]', 'contextFlush()'),
                          ('@autoreleasepool {', '{')):
            self.assertIn(objc, loop)
            loop = loop.replace(objc, cpp)
        self.assertNotIn('[', loop.replace('[]', ''))
        return source, loop

    def test_queue_present_wakes_a_waiting_main_thread_before_unlocking(self):
        source, _ = self.wait_loop()
        body = source.split('- (void)queuePresent: (NSUInteger)drawableID\n{', 1)[1].split('\n}\n', 1)[0]
        wake = body.index('if (mainThreadDrawableWaiter.load(std::memory_order_relaxed) == (void*) self) {')
        self.assertLess(body.index('++_queuedDrawableCount;'), wake)
        self.assertLess(wake, body.index('[_drawableCondition broadcast];'))
        self.assertLess(body.index('[_drawableCondition broadcast];'), body.index('[_drawableCondition unlock];'))
        self.assertLess(body.index('[_drawableCondition unlock];'), body.index('dispatch_async(dispatch_get_main_queue()'))

    def test_main_thread_presents_queued_drawables_instead_of_timing_out(self):
        _, loop = self.wait_loop()
        fixture = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
using BOOL = bool;
using CFTimeInterval = double;
constexpr bool YES = true;
namespace Indium { namespace Waits { struct Block { Block(const char*) {} }; } }
static std::atomic<void*> mainThreadDrawableWaiter{nullptr};
static double CACurrentMediaTime() { return 0; }
static int inlineTimings;
static void darlingMetalTiming(const char*, double) { ++inlineTimings; }

// Pool model: three drawables, as in CAMetalLayerInternal.
struct Layer {
    uint8_t _usableDrawablesBitmap = 0;
    unsigned _queuedDrawableCount = 0;
    int queued[3] = {};
    int lastPresented = -1;
    bool _allowsNextDrawableTimeout = true;

    bool mainThread = true, canRender = true, locked = true;
    int pendingWorkerPresent = -1;   // drawable the submit worker queues while the caller waits
    int mainThreadChecks = 0, renders = 0, flushes = 0, timedWaits = 0, wakeups = 0;
    void* self = this;

    bool isMainThread() { ++mainThreadChecks; return mainThread; }
    void conditionUnlock() { assert(locked); locked = false; }
    void conditionLock() { assert(!locked); locked = true; }
    void queuePresent(int id) {
        queued[_queuedDrawableCount++] = id;
        if (mainThreadDrawableWaiter.load() == self) ++wakeups;
    }
    // prepareRender: take the oldest queued drawable, recycle the one it replaces on screen.
    void contextRender() {
        assert(!locked);   // releaseDrawable: takes the condition lock
        ++renders;
        if (!canRender) return;
        int next = -1;
        if (_queuedDrawableCount > 0) {
            next = queued[0];
            queued[0] = queued[1]; queued[1] = queued[2];
            --_queuedDrawableCount;
        }
        if (lastPresented >= 0) _usableDrawablesBitmap |= 1u << lastPresented;
        lastPresented = next;
    }
    void contextFlush() { assert(!locked); ++flushes; }
    bool conditionWaitUntilDate() {
        assert(locked);
        ++timedWaits;
        if (pendingWorkerPresent >= 0) {
            // Only a main-thread waiter that registered itself is woken by queuePresent:.
            const int before = wakeups;
            queuePresent(pendingWorkerPresent);
            pendingWorkerPresent = -1;
            return wakeups > before;
        }
        return false;   // one second passed
    }
    void conditionWait() { assert(false); }

    // Returns false on timeout.
    bool waitForDrawable() {
''' + loop + r'''
        assert(locked);
        assert(mainThreadDrawableWaiter.load() == nullptr);
        return _usableDrawablesBitmap != 0;
    }
};

int main() {
    {   // Steady state: a free drawable costs no thread check, render or wait.
        Layer layer; layer._usableDrawablesBitmap = 0b110; layer.lastPresented = 0;
        assert(layer.waitForDrawable());
        assert(layer.mainThreadChecks == 0 && layer.renders == 0 && layer.timedWaits == 0);
    }
    {   // End of loading: one drawable on screen, two queued, main thread asks for a third.
        Layer layer; layer.lastPresented = 0; layer.queuePresent(1); layer.queuePresent(2);
        assert(layer.waitForDrawable());
        assert(layer._usableDrawablesBitmap == 0b001 && layer.lastPresented == 1 && layer._queuedDrawableCount == 1);
        assert(layer.renders == 1 && layer.flushes == 1 && layer.timedWaits == 0 && layer.mainThreadChecks == 1);
    }
    {   // Nothing on screen yet and all three queued: the second present frees the first.
        Layer layer; layer.queuePresent(0); layer.queuePresent(1); layer.queuePresent(2);
        assert(layer.waitForDrawable());
        assert(layer._usableDrawablesBitmap == 0b001 && layer.renders == 2 && layer.timedWaits == 0);
    }
    {   // The submit worker has not queued the presentation yet: wait, get woken by it, then present.
        Layer layer; layer.lastPresented = 0; layer.pendingWorkerPresent = 1;
        assert(layer.waitForDrawable());
        assert(layer.timedWaits == 1 && layer.wakeups == 1 && layer.renders == 1 && layer._usableDrawablesBitmap == 0b001);
    }
    {   // Another thread keeps the previous behaviour: it waits for the main thread and never renders.
        Layer layer; layer.mainThread = false; layer.lastPresented = 0; layer.queuePresent(1); layer.queuePresent(2);
        assert(!layer.waitForDrawable());
        assert(layer.renders == 0 && layer.flushes == 0 && layer.timedWaits == 1 && layer.wakeups == 0);
    }
    {   // A context that cannot render yet: a bounded number of attempts, then the timed wait.
        Layer layer; layer.canRender = false; layer.lastPresented = 0; layer.queuePresent(1); layer.queuePresent(2);
        assert(!layer.waitForDrawable());
        assert(layer.renders == 4 && layer.timedWaits == 1);
    }
    {   // Nothing queued and nothing arriving: still the one-second timeout.
        Layer layer; layer.lastPresented = 0;
        assert(!layer.waitForDrawable());
        assert(layer.renders == 0 && layer.timedWaits == 1);
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'check.cpp').write_text(fixture)
            result = subprocess.run(['c++', '-std=c++17', str(path/'check.cpp'), '-o', str(path/'check')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path/'check')], check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
