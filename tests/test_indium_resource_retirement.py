"""Lifetime rules for Indium semaphores and pooled memory (#252), compiled from the patched sources with stub Vulkan calls.

A timeline semaphore that leaves the pool must not be destroyed while a submitted batch can still signal it, and a
pooled allocation must be released while its VkBuffer/VkImage handle value still belongs to the resource.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import test_runtime_compatibility as sources

ROOT = Path(__file__).resolve().parents[1]

DYNAMIC_VK = r'''
#pragma once
#include <vulkan/vulkan.h>
#include <cstdint>
#include <functional>
#include <map>
#include <set>
#include <vector>
#include <sys/mman.h>
namespace Indium { namespace DynamicVK {
inline std::map<uint64_t, uint64_t> counters;    // semaphore -> current value
inline std::set<uint64_t> destroyed;             // destroyed semaphores
inline std::set<uint64_t> failing;               // semaphores whose counter query fails (lost device)
inline unsigned counterQueries = 0;
inline std::map<uint64_t, bool> fenceSignalled;
inline std::set<uint64_t> liveFences;
inline uint64_t nextFence = 0x5000;
inline unsigned fenceCreates = 0;
inline unsigned fenceDestroys = 0;
inline unsigned fenceResets = 0;
inline unsigned emptySubmits = 0;
inline bool autoSignalOnSubmit = false;
inline bool failFenceSubmit = false;
inline bool timeOutFenceWait = false;
inline std::vector<std::vector<uint64_t>> submitWaits;
inline std::set<uint64_t> liveMemory;            // allocated, not freed VkDeviceMemory
inline std::map<uint64_t, VkDeviceSize> memorySize;
inline std::map<uint64_t, void*> mappings;       // like a driver: vkFreeMemory unmaps
inline uint64_t nextMemory = 0x1000;
inline std::function<void(uint64_t)> afterDestroyHandle; // runs when vkDestroyBuffer/vkDestroyImage returns
inline uint64_t key(const void* handle) { return reinterpret_cast<uint64_t>(handle); }
inline VkResult vkCreateFence(VkDevice, const VkFenceCreateInfo*, const VkAllocationCallbacks*, VkFence* fence) {
    *fence = reinterpret_cast<VkFence>(nextFence += 0x10);
    liveFences.insert(key(*fence));
    fenceSignalled[key(*fence)] = false;
    ++fenceCreates;
    return VK_SUCCESS;
}
inline void vkDestroyFence(VkDevice, VkFence fence, const VkAllocationCallbacks*) {
    if (!liveFences.erase(key(fence))) throw "fence destroyed twice";
    ++fenceDestroys;
}
inline VkResult vkWaitForFences(VkDevice, uint32_t fenceCount, const VkFence* pFences, VkBool32, uint64_t) {
    if (timeOutFenceWait) return VK_TIMEOUT;
    for (uint32_t i = 0; i < fenceCount; ++i) {
        if (!fenceSignalled[key(pFences[i])]) return VK_TIMEOUT;
    }
    return VK_SUCCESS;
}
inline VkResult vkResetFences(VkDevice, uint32_t fenceCount, const VkFence* pFences) {
    ++fenceResets;
    for (uint32_t i = 0; i < fenceCount; ++i) {
        fenceSignalled[key(pFences[i])] = false;
    }
    return VK_SUCCESS;
}
inline VkResult vkQueueSubmit2(VkQueue, uint32_t submitCount, const VkSubmitInfo2* pSubmits, VkFence fence) {
    if (failFenceSubmit) return VK_ERROR_DEVICE_LOST;
    for (uint32_t i = 0; i < submitCount; ++i) {
        if (pSubmits[i].commandBufferInfoCount == 0) ++emptySubmits;
        std::vector<uint64_t> waits;
        for (uint32_t j = 0; j < pSubmits[i].waitSemaphoreInfoCount; ++j) {
            waits.push_back(key(pSubmits[i].pWaitSemaphoreInfos[j].semaphore));
        }
        submitWaits.push_back(std::move(waits));
    }
    if (autoSignalOnSubmit && fence != VK_NULL_HANDLE) fenceSignalled[key(fence)] = true;
    return VK_SUCCESS;
}
inline VkResult vkGetSemaphoreCounterValue(VkDevice, VkSemaphore semaphore, uint64_t* value) {
    ++counterQueries;
    if (failing.count(key(semaphore))) return VK_ERROR_DEVICE_LOST;
    *value = counters[key(semaphore)];
    return VK_SUCCESS;
}
inline VkResult vkWaitSemaphores(VkDevice, const VkSemaphoreWaitInfo* info, uint64_t) {
    for (uint32_t i = 0; i < info->semaphoreCount; ++i) {
        if (failing.count(key(info->pSemaphores[i]))) return VK_ERROR_DEVICE_LOST;
        if (counters[key(info->pSemaphores[i])] < info->pValues[i]) return VK_TIMEOUT;
    }
    return VK_SUCCESS;
}
inline void vkDestroySemaphore(VkDevice, VkSemaphore semaphore, const VkAllocationCallbacks*) {
    if (!destroyed.insert(key(semaphore)).second) throw "semaphore destroyed twice";
}
inline VkResult vkAllocateMemory(VkDevice, const VkMemoryAllocateInfo* info, const VkAllocationCallbacks*, VkDeviceMemory* memory) {
    *memory = reinterpret_cast<VkDeviceMemory>(nextMemory += 0x10);
    liveMemory.insert(key(*memory));
    memorySize[key(*memory)] = info->allocationSize;
    return VK_SUCCESS;
}
inline void vkFreeMemory(VkDevice, VkDeviceMemory memory, const VkAllocationCallbacks*) {
    if (!liveMemory.erase(key(memory))) throw "memory freed twice";
    auto mapping = mappings.find(key(memory));
    if (mapping != mappings.end()) {
        munmap(mapping->second, memorySize[key(memory)]);
        mappings.erase(mapping);
    }
}
inline size_t mapOffset = 0;                     // a driver may return a pointer inside a page (minMemoryMapAlignment)
inline VkResult vkMapMemory(VkDevice, VkDeviceMemory memory, VkDeviceSize, VkDeviceSize, VkMemoryMapFlags, void** data) {
    memorySize[key(memory)] += mapOffset;        // vkFreeMemory unmaps the whole range, from the page base
    void* base = mmap(nullptr, memorySize[key(memory)], PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (base == MAP_FAILED) return VK_ERROR_MEMORY_MAP_FAILED;
    mappings[key(memory)] = base;
    *data = static_cast<char*>(base) + mapOffset;
    return VK_SUCCESS;
}
inline void vkDestroyBuffer(VkDevice, VkBuffer buffer, const VkAllocationCallbacks*) {
    if (afterDestroyHandle) afterDestroyHandle(key(buffer));
}
inline void vkDestroyImage(VkDevice, VkImage image, const VkAllocationCallbacks*) {
    if (afterDestroyHandle) afterDestroyHandle(key(image));
}
}}
'''


def body(source, name):
    """Text of a destructor/method definition starting at `name` (sources.function needs a return type)."""
    start = source.index(name)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[opening:end]


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/vulkan/vulkan.h').exists(), 'C++ compiler and Vulkan headers required')
class ResourceRetirementChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def test_full_pool_never_destroys_a_timeline_semaphore_with_a_pending_signal(self):
        device = self.patched('metal--deps--indium', 'src/indium/device.cpp')
        helpers = device[device.index('namespace {\nstruct RetiringTimelineSemaphore'):
                         device.index('Indium::TimelineSemaphore Indium::PrivateDevice::getTimelineSemaphore()')]
        put = sources.function(device, 'Indium::PrivateDevice::putTimelineSemaphore')
        program = r'''
#include <indium/dynamic-vk.hpp>
#include <indium/memory-pool.private.hpp>
#include <algorithm>
#include <atomic>
#include <cassert>
#include <mutex>
#include <utility>
#include <vector>
using namespace Indium::DynamicVK;
namespace Indium {
struct TimelineSemaphore { VkSemaphore semaphore; uint64_t count; };
class PrivateDevice {
public:
    VkDevice _device = reinterpret_cast<VkDevice>(0x77);
    std::mutex _timelineSemaphorePoolMutex;
    std::vector<std::pair<VkSemaphore, uint64_t>> _availableTimelineSemaphores;
    void putTimelineSemaphore(const TimelineSemaphore& semaphore);
};
}
''' + helpers + put + r'''
static VkSemaphore sem(uint64_t n) { return reinterpret_cast<VkSemaphore>(n); }
int main() {
    Indium::PrivateDevice device;
    // a pool with room keeps the handle, pending or not, and costs no counter query
    counters[1] = 0;
    device.putTimelineSemaphore({sem(1), 3});
    assert(device._availableTimelineSemaphores.size() == 1 && destroyed.empty() && counterQueries == 0);
    for (uint64_t i = 2; i <= 256; ++i) device.putTimelineSemaphore({sem(i), 0});
    assert(device._availableTimelineSemaphores.size() == 256 && counterQueries == 0);

    // pool full: the event loop has seen the command buffer's own semaphore, but this texture's signal is pending
    counters[1000] = 4;
    device.putTimelineSemaphore({sem(1000), 5});
    assert(!destroyed.count(1000));
    // already signalled: destroyed at once
    counters[1001] = 9;
    device.putTimelineSemaphore({sem(1001), 9});
    assert(destroyed.count(1001) && !destroyed.count(1000));
    // the pending signal lands; the next put of any semaphore retires it
    counters[1000] = 5;
    device._availableTimelineSemaphores.pop_back();
    device.putTimelineSemaphore({sem(1002), 1});
    assert(destroyed.count(1000) && !destroyed.count(1002));

    // a lost device never reaches the value: the handle is leaked, not destroyed
    failing.insert(1003);
    device.putTimelineSemaphore({sem(1003), 1});
    counters[1004] = 7;
    device.putTimelineSemaphore({sem(1004), 7});
    assert(!destroyed.count(1003) && destroyed.count(1004));

    // beyond 4096 waiting handles further pending ones are still never destroyed early, and stay tracked
    for (uint64_t i = 0; i < 5000; ++i) device.putTimelineSemaphore({sem(20000 + i), 1});
    for (uint64_t i = 0; i < 5000; ++i) assert(!destroyed.count(20000 + i));

    // teardown destroys the waiting handles whose last signal landed, including those past the cap, and leaks the
    // ones still pending or on a lost device
    for (uint64_t i = 0; i < 4999; ++i) counters[20000 + i] = 1;
    destroyRetiringTimelines(device._device);
    assert(destroyed.count(20000) && destroyed.count(24998));
    assert(!destroyed.count(24999) && !destroyed.count(1003));
    return 0;
}
'''
        self.assertEqual(self.run_cpp(program), '')
        # debug mode: every deferral (a destroy the old code did too early) and its later destroy are logged
        log = self.run_cpp(program, poison=True)
        self.assertIn('poison enabled', log)
        self.assertIn('timeline semaphore 0x3e8 destroy deferred (#1): value 4, last signal 5 still pending', log)
        self.assertIn('timeline semaphore 0x3e8 destroyed: its last signal (5) landed', log)
        self.assertRegex(log, r'timeline semaphore 0x[0-9a-f]+ kept past the cap: 4096 destroys already wait')
        self.assertIn('timeline semaphore 0x61a7 leaked at device teardown: its last signal (1) never landed', log)

    def test_poison_mode_traps_cpu_writes_to_freed_mappings_and_logs_their_origin(self):
        program = r'''
#include <indium/dynamic-vk.hpp>
#include <indium/memory-pool.private.hpp>
#include <cassert>
#include <csignal>
#include <cstdlib>
#include <sys/wait.h>
#include <unistd.h>
using namespace Indium;
static VkDevice vkDevice = reinterpret_cast<VkDevice>(0x55);
int main(int argc, char** argv) {
    const bool poison = MemoryPool::Debug::enabled();
    MemoryPool::allocate(vkDevice, 0xa000, 0, 100000, 256, false, true);
    MemoryPool::allocate(vkDevice, 0xb000, 0, 100000, 256, false, true);
    MemoryPool::Allocation first, second;
    assert(MemoryPool::lookup(0xa000, first) && MemoryPool::lookup(0xb000, second));
    // off: small buffers share a pool block; on: each has its own memory, so freeing it unmaps it
    assert((first.memory == second.memory) == !poison);
    if (!poison) return 0;
    auto* bytes = static_cast<volatile char*>(MemoryPool::map(vkDevice, 0xa000));
    bytes[0] = 1;
    MemoryPool::allocate(vkDevice, 0xb000, 0, 4096, 256, false, true); // a recycled handle registered again
    MemoryPool::free(vkDevice, 0xa000);
    pid_t child = fork();
    if (child == 0) {
        bytes[4096] = 2; // a late CPU write into the freed mapping
        _exit(0);
    }
    int status = 0;
    waitpid(child, &status, 0);
    assert(WIFSIGNALED(status) && (WTERMSIG(status) == SIGSEGV || WTERMSIG(status) == SIGBUS));

    // a mapping that starts inside a page is reserved from its page base
    DynamicVK::mapOffset = 64;
    MemoryPool::allocate(vkDevice, 0xc000, 0, 100000, 256, false, true);
    auto* offsetBytes = static_cast<volatile char*>(MemoryPool::map(vkDevice, 0xc000));
    assert(reinterpret_cast<uintptr_t>(offsetBytes) % getpagesize() == 64);
    offsetBytes[0] = 1;
    MemoryPool::free(vkDevice, 0xc000);
    child = fork();
    if (child == 0) {
        offsetBytes[0] = 2; // a late CPU write into the first, partial page
        _exit(0);
    }
    waitpid(child, &status, 0);
    assert(WIFSIGNALED(status) && (WTERMSIG(status) == SIGSEGV || WTERMSIG(status) == SIGBUS));
    return 0;
}
'''
        self.assertEqual(self.run_cpp(program), '')
        log = self.run_cpp(program, poison=True)
        self.assertIn('pool owner 0xb000 registered twice', log)
        self.assertRegex(log, r'freed buffer owner=0xa000 bytes=100000 cpu=\[0x[0-9a-f]+,0x[0-9a-f]+\) reserved-no-access age=[0-9.]+s origin: \S')
        self.assertRegex(log, r'freed buffer owner=0xc000 bytes=100000 cpu=\[0x[0-9a-f]+000,0x[0-9a-f]+000\) reserved-no-access')

    def test_pool_memory_is_released_before_the_handle_value_can_be_reused(self):
        buffer = self.patched('metal--deps--indium', 'src/indium/buffer.cpp')
        texture = self.patched('metal--deps--indium', 'src/indium/texture.cpp')
        buffer_body = body(buffer, 'Indium::PrivateBuffer::~PrivateBuffer()')
        texture_body = body(texture, 'Indium::ConcreteTexture::~ConcreteTexture()')
        # the oneshot retirement and image views are not under test here
        texture_body = texture_body[texture_body.index('auto owner'):]
        texture_body = '{\n' + texture_body
        program = r'''
#include <indium/dynamic-vk.hpp>
#include <indium/memory-pool.private.hpp>
#include <cassert>
using namespace Indium;
using namespace Indium::DynamicVK;
static VkDevice vkDevice = reinterpret_cast<VkDevice>(0x55);
struct Device { VkDevice device() { return vkDevice; } };
struct Buffer {
    Device deviceObject; Device* _privateDevice = &deviceObject;
    VkBuffer _buffer;
    explicit Buffer(uint64_t handle): _buffer(reinterpret_cast<VkBuffer>(handle)) {}
    void destroy() BUFFER_BODY
};
struct Texture {
    Device deviceObject; Device* _device = &deviceObject;
    VkImage _image;
    explicit Texture(uint64_t handle): _image(reinterpret_cast<VkImage>(handle)) {}
    void destroy() TEXTURE_BODY
};
template<class Resource> static void check(bool image, VkDeviceSize size) {
    const uint64_t handle = image ? 0xbeef00 : 0xbeef80;
    MemoryPool::allocate(vkDevice, handle, 0, size, 256, image, !image);
    // another thread creates a resource as soon as the driver has recycled the handle value
    afterDestroyHandle = [&](uint64_t recycled) {
        MemoryPool::allocate(vkDevice, recycled, 0, size, 256, !image, image);
    };
    Resource(handle).destroy();
    afterDestroyHandle = nullptr; // not inside the callback: that would destroy the closure while it runs
    MemoryPool::Allocation allocation;
    assert(MemoryPool::lookup(handle, allocation));     // the new resource still owns its entry...
    assert(liveMemory.count(key(allocation.memory)));   // ...and its memory
}
int main() {
    check<Buffer>(false, 4096);
    check<Buffer>(false, 8 << 20);   // dedicated allocation: freed immediately, so the race would free live memory
    check<Texture>(true, 4096);
    check<Texture>(true, 8 << 20);
    return 0;
}
'''.replace('BUFFER_BODY', buffer_body).replace('TEXTURE_BODY', texture_body)
        self.run_cpp(program)

    def test_binary_semaphore_retirement_reuses_one_fence_and_preserves_two_generations(self):
        device = self.patched('metal--deps--indium', 'src/indium/device.cpp')
        header = self.patched('metal--deps--indium', 'private-include/indium/device.private.hpp')
        decl_start = header.index('struct BinaryRetirementSubmit')
        decl_end = header.index('\n};', decl_start)
        decls = header[decl_start:decl_end]
        impl_start = device.index('namespace {\n// darling-lol (issue #252)')
        impl_end = device.index('std::shared_ptr<Indium::BinarySemaphore> Indium::PrivateDevice::getWrappedBinarySemaphore')
        impl = device[impl_start:impl_end]
        program = r'''
#include <indium/dynamic-vk.hpp>
#include <indium/memory-pool.private.hpp>
#include <cassert>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
using namespace Indium::DynamicVK;
namespace Indium {
''' + decls + r'''
class PrivateDevice;
struct BinarySemaphore { PrivateDevice* device; VkSemaphore semaphore; };
class PrivateDevice {
public:
    VkDevice _device;
    explicit PrivateDevice(uint64_t handle) : _device(reinterpret_cast<VkDevice>(handle)) {}
    void putBinarySemaphore(const BinarySemaphore& semaphore);
};
}
''' + impl + r'''
static VkSemaphore sem(uint64_t n) { return reinterpret_cast<VkSemaphore>(n); }
int main() {
    VkQueue queue = reinterpret_cast<VkQueue>(0xaa);
    Indium::PrivateDevice dev1(0x77);

    // 1. Waited presentation semaphores: putBinarySemaphore does no Vulkan work, and 12 retirement
    //    generations reuse one VkFence with zero vkDestroyFence calls and zero empty vkQueueSubmit2 calls.
    VkFence firstFence = VK_NULL_HANDLE;
    for (uint64_t cycle = 0; cycle < 12; ++cycle) {
        if (firstFence != VK_NULL_HANDLE) {
            fenceSignalled[key(firstFence)] = true;
        }
        VkSemaphore s = sem(100 + cycle);
        Indium::noteBinarySemaphoreSignalPending(s);
        Indium::noteBinarySemaphoreWaited(s);
        dev1.putBinarySemaphore({&dev1, s});
        assert(fenceCreates == (cycle == 0 ? 0u : 1u));

        // Simulate a submit while the previous fence was still in flight before it completed: no second fence.
        if (cycle == 0) {
            auto sub0 = Indium::beginBinaryRetirementSubmit(dev1._device);
            assert(!sub0.needsWaitSubmit && sub0.fence != VK_NULL_HANDLE && sub0.destroy.empty());
            assert(fenceCreates == 1 && fenceDestroys == 0 && emptySubmits == 0);
            firstFence = sub0.fence;
            Indium::finishBinaryRetirementSubmit(dev1._device, queue, sub0, true);
            assert(emptySubmits == 0);

            auto inFlight = Indium::beginBinaryRetirementSubmit(dev1._device);
            assert(inFlight.fence == VK_NULL_HANDLE && !inFlight.needsWaitSubmit && inFlight.destroy.empty());
            assert(!destroyed.count(100));
            continue;
        }

        auto sub = Indium::beginBinaryRetirementSubmit(dev1._device);
        assert(!sub.needsWaitSubmit && sub.fence == firstFence);
        if (cycle == 1) {
            // First fence signalled: sem(100) only aged, not destroyed yet (two-generation margin).
            assert(sub.destroy.empty() && !destroyed.count(100));
        } else {
            assert(sub.destroy.size() == 1 && sub.destroy[0] == sem(100 + cycle - 2));
            Indium::destroyBinaryRetirementSemaphores(dev1._device, sub.destroy);
            assert(sub.destroy.empty());
            assert(destroyed.count(100 + cycle - 2));
            assert(!destroyed.count(100 + cycle - 1));
        }
        assert(!destroyed.count(100 + cycle));
        Indium::finishBinaryRetirementSubmit(dev1._device, queue, sub, true);
    }
    assert(fenceCreates == 1 && fenceDestroys == 0 && fenceResets == 11 && emptySubmits == 0);

    // 2. A failed real submit puts pending semaphores back cleanly for retry on the next submit.
    fenceSignalled[key(firstFence)] = true;
    dev1.putBinarySemaphore({&dev1, sem(200)});
    auto failSub = Indium::beginBinaryRetirementSubmit(dev1._device);
    assert(failSub.fence == firstFence);
    Indium::destroyBinaryRetirementSemaphores(dev1._device, failSub.destroy);
    Indium::finishBinaryRetirementSubmit(dev1._device, queue, failSub, false);

    auto retrySub = Indium::beginBinaryRetirementSubmit(dev1._device);
    assert(retrySub.fence == firstFence && !retrySub.needsWaitSubmit);
    Indium::finishBinaryRetirementSubmit(dev1._device, queue, retrySub, true);

    // 3. Unwaited acquire semaphore (dropped drawable) falls back to one empty wait batch referencing it.
    Indium::PrivateDevice dev2(0x88);
    Indium::noteBinarySemaphoreSignalPending(sem(400));
    dev2.putBinarySemaphore({&dev2, sem(400)});
    auto waitSub = Indium::beginBinaryRetirementSubmit(dev2._device);
    assert(waitSub.needsWaitSubmit && waitSub.fence == VK_NULL_HANDLE);
    Indium::finishBinaryRetirementSubmit(dev2._device, queue, waitSub, true);
    assert(emptySubmits == 1 && fenceCreates == 2);
    assert(!submitWaits.empty() && submitWaits.back().size() == 1 && submitWaits.back()[0] == key(sem(400)));
    assert(!destroyed.count(400));

    // Advance dev2 through two fence generations; verify persistent fence still lives until teardown.
    uint64_t dev2FenceKey = *liveFences.rbegin();
    fenceSignalled[dev2FenceKey] = true;
    auto dev2Gen1 = Indium::beginBinaryRetirementSubmit(dev2._device);
    assert(dev2Gen1.destroy.empty() && !destroyed.count(400));
    Indium::finishBinaryRetirementSubmit(dev2._device, queue, dev2Gen1, true);
    fenceSignalled[dev2FenceKey] = true;
    auto dev2Gen2 = Indium::beginBinaryRetirementSubmit(dev2._device);
    assert(dev2Gen2.destroy.size() == 1 && dev2Gen2.destroy[0] == sem(400));
    Indium::destroyBinaryRetirementSemaphores(dev2._device, dev2Gen2.destroy);
    Indium::finishBinaryRetirementSubmit(dev2._device, queue, dev2Gen2, true);
    assert(destroyed.count(400) && fenceDestroys == 0);

    // 4. Device teardown waits on in-flight + pending fences and destroys all semaphores and fences when idle.
    dev2.putBinarySemaphore({&dev2, sem(450)});
    auto dev2Gen3 = Indium::beginBinaryRetirementSubmit(dev2._device);
    Indium::finishBinaryRetirementSubmit(dev2._device, queue, dev2Gen3, true); // dev2FenceKey in flight
    dev2.putBinarySemaphore({&dev2, sem(451)});                                // still pending
    fenceSignalled[dev2FenceKey] = true;
    autoSignalOnSubmit = true;
    unsigned liveBeforeTeardown = liveFences.size();
    destroyRetiringBinarySemaphores(dev2._device, queue);
    assert(destroyed.count(450) && destroyed.count(451));
    assert(liveFences.size() == liveBeforeTeardown - 1); // dev1 still keeps its 1 persistent fence; dev2's fences freed

    // 5. Device teardown leaks semaphores and the in-flight fence if the wait times out (#252).
    Indium::PrivateDevice dev3(0x99);
    dev3.putBinarySemaphore({&dev3, sem(500)});
    auto dev3Sub = Indium::beginBinaryRetirementSubmit(dev3._device);
    Indium::finishBinaryRetirementSubmit(dev3._device, queue, dev3Sub, true);
    timeOutFenceWait = true;
    destroyRetiringBinarySemaphores(dev3._device, queue);
    assert(!destroyed.count(500));
    assert(liveFences.count(key(dev3Sub.fence)));
    return 0;
}
'''
        self.assertEqual(self.run_cpp(program), '')

    def test_presenting_commit_merges_submit_batch_and_gates_persistent_semaphore(self):
        tex = self.patched('metal--deps--indium', 'src/indium/texture.cpp')
        cmdbuf = self.patched('metal--deps--indium', 'src/indium/command-buffer.cpp')
        acquire = body(tex, 'Indium::PrivateTexture::acquirePresentationSemaphore()')
        self.assertIn('_device->getWrappedBinarySemaphore(needsExportablePresentationSemaphore())', acquire)
        commit = body(cmdbuf, 'void Indium::PrivateCommandBuffer::commit()')
        self.assertIn('!_drawablesToPresent.empty()\n\t\t\t\t? privateTexture->acquirePresentationSemaphore()\n\t\t\t\t: _privateDevice->getWrappedBinarySemaphore(privateTexture->needsExportablePresentationSemaphore())', commit)
        batch_branch = commit.split('if (Indium::submitBatchEnabled()) {', 1)[1].split('\n\t} else {', 1)[0]
        self.assertIn('appendSubmitBatchItemLocked(_privateDevice, _commandBuffer, frameSample, waitInfos, signalInfos)', batch_branch)
        self.assertIn('flushSubmitBatchLocked(presenting)', batch_branch)
        self.assertNotIn('DynamicVK::vkQueueSubmit2', batch_branch)

    def run_cpp(self, text, poison=False):
        """Compile and run; returns the debug log (empty without `poison`)."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            include = path / 'include/indium'
            include.mkdir(parents=True)
            (include / 'dynamic-vk.hpp').write_text(DYNAMIC_VK)
            header = self.patched('metal--deps--indium', 'private-include/indium/memory-pool.private.hpp')
            (include / 'memory-pool.private.hpp').write_text(header)
            (path / 'check.cpp').write_text(text)
            compile = subprocess.run(['c++', '-std=c++17', '-pthread', '-I', str(path / 'include'), str(path / 'check.cpp'),
                                      '-o', str(path / 'check')], capture_output=True, text=True)
            self.assertEqual(compile.returncode, 0, compile.stderr)
            env = {'PATH': '/usr/bin:/bin'}
            if poison:
                env.update(INDIUM_POISON_FREED_MAPPINGS='1', INDIUM_POISON_LOG=str(path / 'poison.log'))
            result = subprocess.run([str(path / 'check')], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            log = path / 'poison.log'
            return log.read_text() if log.exists() else ''


if __name__ == '__main__':
    unittest.main()
