"""Compile the patched native-thread signal bridge, completion budget policy and texture timeline policy."""
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
import test_runtime_compatibility as sources


class NativeThreadChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched
    run_c = sources.CompatibilityChecks.run_c

    def test_resize_forwarding_is_limited_to_foreign_threads_and_successful_delivery(self):
        source = self.patched('xnu', 'darling/src/libsystem_kernel/emulation/src/linux_premigration/signal/sigexc.c')
        bridge = sources.function(source, 'forward_native_resize_signal')
        self.run_c('''
#include <assert.h>
#define LINUX_SIGWINCH 28
#define __NR_getpid 1
#define __NR_gettid 2
#define __NR_tgkill 3
static long tid=101, result=0;
static int sends;
static long fake_syscall(int number, ...) {
    if (number==__NR_getpid) return 100;
    if (number==__NR_gettid) return tid;
    ++sends; return result;
}
#define LINUX_SYSCALL(...) fake_syscall(__VA_ARGS__)
''' + bridge + '''
int main(void) {
    assert(forward_native_resize_signal(28,-32)==1 && sends==1);
    assert(forward_native_resize_signal(28,0)==0 && sends==1);
    assert(forward_native_resize_signal(28,-9)==0 && sends==1);
    for(int sig=1;sig<=64;++sig)
        if(sig!=28) assert(forward_native_resize_signal(sig,-32)==0);
    assert(sends==1);
    tid=100; assert(forward_native_resize_signal(28,-32)==0 && sends==1);
    tid=101; result=-3;
    assert(forward_native_resize_signal(28,-32)==0 && sends==2);
}
''')

    def test_native_thread_without_rpc_socket_returns_an_error_instead_of_aborting(self):
        source = self.patched('darling', 'src/startup/mldr/elfcalls/threads.c')
        self.run_c('''
#include <assert.h>
#include <stdlib.h>
#define SYS_gettid 1
static int t_server_socket=-1, __dserver_main_thread_socket_fd=71;
static int tid=101;
static int getpid(void) { return 100; }
static int syscall(int number) { return tid; }
''' + sources.function(source, '__darling_thread_rpc_socket') + '''
int main(void) {
    assert(__darling_thread_rpc_socket()==-1);
    t_server_socket=73; assert(__darling_thread_rpc_socket()==73);
    t_server_socket=-1; tid=100; assert(__darling_thread_rpc_socket()==71);
}
''')

    def test_cpu_readback_skips_cached_surfaces_but_preserves_mixed_metal_trees(self):
        source = self.patched('cocotron', 'QuartzCore/CALayerContext.m')
        self.run_c('''#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <stdlib.h>
typedef int BOOL;
typedef struct { int metal, full; } CALayer;
typedef struct { int width, height; } CGSize;
static BOOL hasMetalLayer(CALayer* layer) { return layer->metal; }
static BOOL hasFullWindowSurface(CALayer* layer, CGSize size) { return layer->full; }
''' + sources.function(source, 'skipLocalRender') + '''
int main(void) {
    CGSize size={16,16};
    assert(!skipLocalRender(NULL,size));
    for(int shortcut=0;shortcut<=1;++shortcut)
    for(int direct=0;direct<=1;++direct)
    for(int metal=0;metal<=1;++metal)
    for(int full=0;full<=1;++full) {
        CALayer layer={metal,full};
        if(shortcut) setenv("DARLING_CA_SKIP_LOCAL_IOSURFACE_RENDER","1",1);
        else unsetenv("DARLING_CA_SKIP_LOCAL_IOSURFACE_RENDER");
        if(direct) setenv("RCLOL_GPU_DIRECT_PRESENT","1",1);
        else unsetenv("RCLOL_GPU_DIRECT_PRESENT");
        assert(!!skipLocalRender(&layer,size)==(shortcut && !metal && (!direct || full)));
    }
}
''')


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class CompletionBudgetChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def test_vendor_defaults_disable_clamping_and_malformed_overrides(self):
        header = self.patched('metal--deps--indium', 'private-include/indium/completion-spin.private.hpp')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'budget.hpp').write_text(header)
            (path / 'check.cpp').write_text('''
#include "budget.hpp"
#include <cassert>
#include <string>
int main() {
    using Indium::completionSpinBudget;
    assert(completionSpinBudget(0x1002,nullptr)==0);
    assert(completionSpinBudget(0x10de,nullptr)==2000000);
    assert(completionSpinBudget(0x1002,nullptr)==0);
    assert(completionSpinBudget(0x10de,"0")==0);
    assert(completionSpinBudget(0x1002,"15")==15000);
    assert(completionSpinBudget(0x10de,"10001")==10000000);
    const std::string huge(10000,'9');
    assert(completionSpinBudget(0x10de,huge.c_str())==10000000);
    for(const char* invalid : {"", "-1", "1ms", "18446744073709551616x"}) {
        assert(completionSpinBudget(0x10de,invalid)==2000000);
        assert(completionSpinBudget(0x1002,invalid)==0);
    }
}
''')
            subprocess.run(['c++','-std=c++17',str(path/'check.cpp'),'-o',str(path/'check')], check=True, capture_output=True)
            subprocess.run([str(path/'check')], check=True, capture_output=True, timeout=10)

    def test_polling_preserves_idle_blocking_callbacks_and_timeout_budget(self):
        source = self.patched('metal--deps--indium', 'src/indium/device.cpp')
        poll = sources.function(source, 'Indium::PrivateDevice::pollEvents')
        harness = '''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <mutex>
#include <vector>
using VkSemaphore = int;
struct VkSemaphoreWaitInfo {
    int sType, flags;
    size_t semaphoreCount;
    const VkSemaphore* pSemaphores;
    const uint64_t* pValues;
};
constexpr int VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO=1, VK_SEMAPHORE_WAIT_ANY_BIT=1;
constexpr int VK_SUCCESS=0, VK_TIMEOUT=1;
static int counterQueries, waitResult=VK_TIMEOUT;
static uint64_t lastTimeout;
static bool ready;
namespace DynamicVK {
int vkGetSemaphoreCounterValue(int, int semaphore, uint64_t* count) {
    ++counterQueries; *count=ready && semaphore==2 ? 1 : 0; return VK_SUCCESS;
}
int vkWaitSemaphores(int, const VkSemaphoreWaitInfo*, uint64_t timeout) {
    lastTimeout=timeout; return waitResult;
}
}
namespace Indium {
class PrivateDevice {
public:
    std::mutex _pollingMutex, _eventLoopMutex;
    int _device=0;
    uint64_t _completionSpinNanoseconds=1000000;
    std::atomic<uint64_t> _eventLoopRegistrations {0};
    std::atomic<unsigned> _eventLoopBlockedPollers {0};
    std::vector<VkSemaphore> _eventLoopSemaphores={1};
    std::vector<uint64_t> _eventLoopWaitValues={1};
    std::vector<std::function<void()>> _eventLoopCallbacks={nullptr};
    void pollEvents(uint64_t timeoutNanoseconds);
};
}
''' + poll + '''
int main() {
    Indium::PrivateDevice device;
    device.pollEvents(2000000);
    assert(counterQueries==0 && lastTimeout==2000000);
    int callbacks=0;
    device._eventLoopSemaphores.push_back(2);
    device._eventLoopWaitValues.push_back(1);
    device._eventLoopCallbacks.push_back([&]{++callbacks;});
    device.pollEvents(2000000);
    assert(counterQueries>0 && lastTimeout<2000000 && callbacks==0);
    ready=true; waitResult=VK_SUCCESS;
    device.pollEvents(2000000);
    assert(lastTimeout==0 && callbacks==1);
    assert(device._eventLoopSemaphores.size()==1);
    counterQueries=0; waitResult=VK_TIMEOUT;
    device._eventLoopSemaphores.push_back(2);
    device._eventLoopWaitValues.push_back(1);
    device._eventLoopCallbacks.push_back(nullptr);
    device.pollEvents(0);
    assert(counterQueries==0 && lastTimeout==0);
    device._completionSpinNanoseconds=0;
    device.pollEvents(2000000);
    assert(counterQueries==0 && lastTimeout==2000000);
}
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'poll.cpp').write_text(harness)
            subprocess.run(['c++', '-std=c++17', '-pthread', str(path/'poll.cpp'), '-o', str(path/'poll')],
                           check=True, capture_output=True)
            subprocess.run([str(path/'poll')], check=True, capture_output=True, timeout=10)


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class TextureTimelineChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def compiled(self, folder, text, name='check'):
        path = Path(folder)
        (path / (name + '.cpp')).write_text(text)
        build = subprocess.run(['c++', '-std=c++17', '-pthread', '-I', str(path), str(path / (name + '.cpp')),
                                '-o', str(path / name)], capture_output=True, text=True)
        self.assertEqual(build.returncode, 0, build.stderr)
        return str(path / name)

    def test_nvidia_defaults_to_no_texture_timelines_and_the_files_are_read_once(self):
        header = self.patched('metal--deps--indium', 'private-include/indium/texture-sync.private.hpp')
        device = self.patched('metal--deps--indium', 'src/indium/device.cpp')
        start = device.index('bool Indium::PrivateDevice::textureTimelines() const {')
        method = device[start:device.index('\n}\n', start) + 3]
        program = r'''
#include "texture-sync.private.hpp"
#include <cassert>
#include <cstdlib>
#include <cstring>
#define F_OK 0
static bool forceOn, forceOff;
static int lookups;
static int test_access(const char* path, int) {
    ++lookups;
    if (!strcmp(path, "/tmp/metal-texture-timelines")) return forceOn ? 0 : -1;
    if (!strcmp(path, "/tmp/metal-no-texture-timelines")) return forceOff ? 0 : -1;
    return -1;
}
#define access test_access
namespace Indium {
class PrivateDevice {
public:
    struct { uint32_t vendorID; } _properties;
    bool textureTimelines() const;
};
}
''' + method + r'''
int main(int argc, char** argv) {
    using Indium::textureTimelinesWanted;
    assert(!textureTimelinesWanted(0x10de, false, false));
    assert(textureTimelinesWanted(0x1002, false, false) && textureTimelinesWanted(0x8086, false, false));
    assert(textureTimelinesWanted(0x10de, true, false) && textureTimelinesWanted(0x10de, true, true));
    assert(!textureTimelinesWanted(0x1002, false, true) && textureTimelinesWanted(0x1002, true, true));

    assert(argc == 4);
    forceOn = argv[2][0] == '1';
    forceOff = argv[3][0] == '1';
    const bool on = forceOn, off = forceOff;
    Indium::PrivateDevice device, radeon;
    device._properties.vendorID = strtoul(argv[1], nullptr, 16);
    radeon._properties.vendorID = 0x1002;
    assert(device.textureTimelines() == textureTimelinesWanted(device._properties.vendorID, on, off));
    // a texture keeps the choice made when it was created, so the files must not be read again
    forceOn = !forceOn;
    forceOff = !forceOff;
    assert(device.textureTimelines() == textureTimelinesWanted(device._properties.vendorID, on, off));
    assert(radeon.textureTimelines() == textureTimelinesWanted(0x1002, on, off));
    assert(lookups == 2);
}
'''
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'texture-sync.private.hpp').write_text(header)
            check = self.compiled(folder, program)
            for vendor in ('10de', '1002'):
                for on in '01':
                    for off in '01':
                        result = subprocess.run([check, vendor, on, off], capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, (vendor, on, off, result.stderr))

    def test_texture_without_a_timeline_adds_no_semaphore_operations(self):
        texture = self.patched('metal--deps--indium', 'src/indium/texture.cpp')
        self.assertIn('if (device->textureTimelines()) _syncSemaphore = device->getWrappedTimelineSemaphore();', texture)
        acquire = texture[texture.index('const Indium::TimelineSemaphore& Indium::PrivateTexture::acquire('):
                          texture.index('void Indium::PrivateTexture::beginUpdatingPresentationSemaphore(')]
        program = r'''
#include <cassert>
#include <cstdint>
#include <memory>
#include <mutex>
struct VkSemaphore_T;
using VkSemaphore = VkSemaphore_T*;
#define VK_NULL_HANDLE nullptr
namespace Indium {
class PrivateDevice;
struct TimelineSemaphore { std::shared_ptr<PrivateDevice> device; VkSemaphore semaphore; uint64_t count; };
struct BinarySemaphore { std::shared_ptr<PrivateDevice> device; VkSemaphore semaphore; };
class PrivateTexture {
public:
    std::mutex _syncMutex;
    std::shared_ptr<TimelineSemaphore> _syncSemaphore;
    std::shared_ptr<BinarySemaphore> _extraWaitSemaphore;
    const TimelineSemaphore& acquire(uint64_t& waitValue, std::shared_ptr<BinarySemaphore>& extraWaitSemaphore, uint64_t& signalValue);
};
}
''' + acquire + r'''
int main() {
    using namespace Indium;
    const auto handle = reinterpret_cast<VkSemaphore>(uintptr_t(7));
    uint64_t wait = 99, signal = 99;
    std::shared_ptr<BinarySemaphore> extra;

    // no timeline: nothing to wait on or signal, but a drawable's acquire semaphore is still handed over once
    PrivateTexture bare;
    bare._extraWaitSemaphore = std::make_shared<BinarySemaphore>();
    const TimelineSemaphore& none = bare.acquire(wait, extra, signal);
    assert(none.semaphore == VK_NULL_HANDLE && wait == 0 && signal == 0);
    assert(extra && !bare._extraWaitSemaphore);
    extra.reset();
    bare.acquire(wait, extra, signal);
    assert(!extra);

    // with a timeline the values advance exactly as before
    PrivateTexture owned;
    owned._syncSemaphore = std::make_shared<TimelineSemaphore>(TimelineSemaphore { nullptr, handle, 4 });
    const TimelineSemaphore& first = owned.acquire(wait, extra, signal);
    assert(first.semaphore == handle && wait == 4 && signal == 5);
    owned.acquire(wait, extra, signal);
    assert(wait == 5 && signal == 6);
}
'''
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([self.compiled(folder, program)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

        commands = self.patched('metal--deps--indium', 'src/indium/command-buffer.cpp')
        commit = commands[commands.index('void Indium::PrivateCommandBuffer::commit()'):]
        extra_wait = commit.index('waitInfos.push_back(extraWaitInfo);')
        skipped = commit.index('if (sema.semaphore == VK_NULL_HANDLE) return;')
        self.assertLess(extra_wait, skipped)
        self.assertLess(skipped, commit.index('waitInfo.semaphore = sema.semaphore;'))
        self.assertLess(skipped, commit.index('signalInfo.semaphore = sema.semaphore;'))
        unbatched = commit.split('if (Indium::submitBatchEnabled()) {', 1)[1].split('\n\t} else {', 1)[1]
        barrier = unbatched.index('orderedInfos[orderedCount++].commandBuffer = submitBatchBarrier(_privateDevice);')
        self.assertLess(unbatched.index('if (!_privateDevice->textureTimelines()) {'), barrier)
        self.assertLess(barrier, unbatched.index('orderedInfos[orderedCount++] = commandBufferInfo;'))
        self.assertIn('info.commandBufferInfoCount = orderedCount;\n\t\tinfo.pCommandBufferInfos = orderedInfos;', unbatched)

    def test_a_batch_leads_with_the_barrier_only_without_texture_timelines(self):
        commands = self.patched('metal--deps--indium', 'src/indium/command-buffer.cpp')
        batch = commands[commands.index('std::vector<VkSemaphore> flushSubmitBatchLocked('):
                         commands.index('// darling-lol: tunable batch window and size.')]
        program = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <map>
#include <memory>
#include <vector>
using VkSemaphore = uint64_t;
using VkCommandBuffer = uint64_t;
using VkFence = uint64_t;
using VkQueue = int;
using VkDevice = int;
constexpr uint64_t VK_NULL_HANDLE = 0;
constexpr int VK_SUCCESS = 0, VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO = 1, VK_STRUCTURE_TYPE_SUBMIT_INFO_2 = 2;
struct VkSemaphoreSubmitInfo { int sType; VkSemaphore semaphore; uint64_t value; };
struct VkCommandBufferSubmitInfo { int sType; VkCommandBuffer commandBuffer; };
struct VkSubmitInfo2 {
    int sType;
    size_t commandBufferInfoCount; const VkCommandBufferSubmitInfo* pCommandBufferInfos;
    size_t signalSemaphoreInfoCount; const VkSemaphoreSubmitInfo* pSignalSemaphoreInfos;
    size_t waitSemaphoreInfoCount; const VkSemaphoreSubmitInfo* pWaitSemaphoreInfos;
};
static std::vector<VkCommandBuffer> submitted;
static std::vector<VkSemaphoreSubmitInfo> waited, signalled;
struct FrameIntervalSample { std::atomic<uint64_t> submitted {0}; };
static uint64_t frameIntervalNow() { return 1; }
static std::atomic<uint64_t> statSubmits {0};
namespace Indium {
struct PrivateDevice {
    bool timelines = true;
    bool textureTimelines() const { return timelines; }
    VkDevice device() { return 0; }
    VkQueue graphicsQueue() { return 0; }
};
static int globalInstance;
struct BinaryRetirementSubmit { VkFence fence = 0; std::vector<VkSemaphore> destroy; };
static BinaryRetirementSubmit beginBinaryRetirementSubmit(VkDevice) { return {}; }
static void finishBinaryRetirementSubmit(VkDevice, VkQueue, BinaryRetirementSubmit&, bool) {}
static void count(const char*, uint64_t) {}
namespace DynamicVK {
static void beginDiagnosticCapture(int) {}
static void endDiagnosticCapture(bool) {}
static int vkQueueSubmit2(VkQueue, uint32_t count, const VkSubmitInfo2* info, VkFence) {
    assert(count == 1);
    submitted.clear();
    for (size_t i = 0; i < info->commandBufferInfoCount; ++i) submitted.push_back(info->pCommandBufferInfos[i].commandBuffer);
    waited.assign(info->pWaitSemaphoreInfos, info->pWaitSemaphoreInfos + info->waitSemaphoreInfoCount);
    signalled.assign(info->pSignalSemaphoreInfos, info->pSignalSemaphoreInfos + info->signalSemaphoreInfoCount);
    return VK_SUCCESS;
}
}
}
struct PendingSubmit {
    VkCommandBuffer commandBuffer = 0;
    FrameIntervalSample* frameSample = nullptr;
    std::vector<VkSemaphoreSubmitInfo> waits, signals;
};
struct SubmitBatchState {
    std::vector<PendingSubmit> items;
    std::map<VkSemaphore, uint64_t> signaled;
    std::shared_ptr<Indium::PrivateDevice> device;
    std::chrono::steady_clock::time_point since;
};
static SubmitBatchState& submitBatchState() { static SubmitBatchState state; return state; }
static const VkCommandBuffer barrier = 900;
static VkCommandBuffer submitBatchBarrier(const std::shared_ptr<Indium::PrivateDevice>&) { return barrier; }
''' + batch + r'''
using Order = std::vector<VkCommandBuffer>;
static Order flushed(const std::shared_ptr<Indium::PrivateDevice>& device, size_t commandBuffers) {
    for (size_t i = 0; i < commandBuffers; ++i)
        appendSubmitBatchItemLocked(device, 10 + i, nullptr, {}, {{0, 50 + i, 1}});
    flushSubmitBatchLocked();
    assert(signalled.size() == commandBuffers && waited.empty());
    return submitted;
}
int main() {
    auto device = std::make_shared<Indium::PrivateDevice>();
    // texture timelines carry the order between batches: a barrier only separates the items of one batch
    assert(flushed(device, 1) == (Order {10}));
    assert(flushed(device, 3) == (Order {10, barrier, 11, barrier, 12}));
    // without them every batch starts with the barrier
    device->timelines = false;
    assert(flushed(device, 1) == (Order {barrier, 10}));
    assert(flushed(device, 3) == (Order {barrier, 10, barrier, 11, barrier, 12}));
    assert(flushSubmitBatchLocked().empty() && submitted.size() == 6);
}
'''
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([self.compiled(folder, program)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
