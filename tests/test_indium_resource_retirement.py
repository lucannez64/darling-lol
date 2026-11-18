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
namespace Indium { namespace DynamicVK {
inline std::map<uint64_t, uint64_t> counters;    // semaphore -> current value
inline std::set<uint64_t> destroyed;             // destroyed semaphores
inline std::set<uint64_t> failing;               // semaphores whose counter query fails (lost device)
inline unsigned counterQueries = 0;
inline std::set<uint64_t> liveMemory;            // allocated, not freed VkDeviceMemory
inline uint64_t nextMemory = 0x1000;
inline std::function<void(uint64_t)> afterDestroyHandle; // runs when vkDestroyBuffer/vkDestroyImage returns
inline uint64_t key(const void* handle) { return reinterpret_cast<uint64_t>(handle); }
inline VkResult vkGetSemaphoreCounterValue(VkDevice, VkSemaphore semaphore, uint64_t* value) {
    ++counterQueries;
    if (failing.count(key(semaphore))) return VK_ERROR_DEVICE_LOST;
    *value = counters[key(semaphore)];
    return VK_SUCCESS;
}
inline void vkDestroySemaphore(VkDevice, VkSemaphore semaphore, const VkAllocationCallbacks*) {
    if (!destroyed.insert(key(semaphore)).second) throw "semaphore destroyed twice";
}
inline VkResult vkAllocateMemory(VkDevice, const VkMemoryAllocateInfo*, const VkAllocationCallbacks*, VkDeviceMemory* memory) {
    *memory = reinterpret_cast<VkDeviceMemory>(nextMemory += 0x10);
    liveMemory.insert(key(*memory));
    return VK_SUCCESS;
}
inline void vkFreeMemory(VkDevice, VkDeviceMemory memory, const VkAllocationCallbacks*) {
    if (!liveMemory.erase(key(memory))) throw "memory freed twice";
}
inline VkResult vkMapMemory(VkDevice, VkDeviceMemory, VkDeviceSize, VkDeviceSize size, VkMemoryMapFlags, void** data) {
    static char storage[64 << 20];
    *data = storage;
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

    // bounded: beyond 4096 waiting handles further pending ones are leaked, never destroyed early
    for (uint64_t i = 0; i < 5000; ++i) device.putTimelineSemaphore({sem(20000 + i), 1});
    for (uint64_t i = 0; i < 5000; ++i) assert(!destroyed.count(20000 + i));

    // teardown destroys what is still waiting for this device
    destroyRetiringTimelines(device._device);
    assert(destroyed.count(1003) && destroyed.count(20000));
    return 0;
}
'''
        self.run_cpp(program)

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
        afterDestroyHandle = nullptr;
        MemoryPool::allocate(vkDevice, recycled, 0, size, 256, !image, image);
    };
    Resource(handle).destroy();
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

    def run_cpp(self, text):
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
            result = subprocess.run([str(path / 'check')], capture_output=True, text=True, timeout=30,
                                    env={'PATH': '/usr/bin:/bin'})
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
