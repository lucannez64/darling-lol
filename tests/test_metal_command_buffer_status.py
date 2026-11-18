"""Checks for MTLCommandBuffer status/timing properties and newLibraryWithURL exception safety."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def diff_section(patch_rel, file_rel):
    patch = (ROOT / patch_rel).read_text()
    marker = f'diff --git a/{file_rel} b/{file_rel}\n'
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added_lines(patch_rel, file_rel):
    section = diff_section(patch_rel, file_rel)
    return ''.join(line[1:] + '\n' for line in section.splitlines()
                   if line.startswith('+') and not line.startswith('+++'))


class MetalCommandBufferStatusTests(unittest.TestCase):
    def test_status_enums_and_protocol_surface_match(self):
        hdr = added_lines('patches/metal.patch', 'include/Metal/MTLCommandBuffer.h')
        indium_hdr = added_lines('patches/metal--deps--indium.patch', 'include/indium/command-buffer.hpp')
        for expected in (
            'MTLCommandBufferStatusNotEnqueued = 0',
            'MTLCommandBufferStatusEnqueued = 1',
            'MTLCommandBufferStatusCommitted = 2',
            'MTLCommandBufferStatusScheduled = 3',
            'MTLCommandBufferStatusCompleted = 4',
            'MTLCommandBufferStatusError = 5',
            '@property(readonly) MTLCommandBufferStatus status;',
            '@property(readonly, nullable) NSError* error;',
            '@property(readonly) NSTimeInterval kernelStartTime;',
            '@property(readonly) NSTimeInterval kernelEndTime;',
            '@property(readonly) NSTimeInterval GPUStartTime;',
            '@property(readonly) NSTimeInterval GPUEndTime;',
            '- (void)enqueue;',
            '- (void)waitUntilScheduled;',
        ):
            self.assertIn(expected, hdr)
        for expected in (
            'NotEnqueued = 0',
            'Enqueued = 1',
            'Committed = 2',
            'Scheduled = 3',
            'Completed = 4',
            'Error = 5',
            'virtual CommandBufferStatus status() const = 0;',
            'virtual double gpuStartTime() const = 0;',
            'virtual double gpuEndTime() const = 0;',
            'virtual void enqueue() = 0;',
            'virtual void waitUntilScheduled() = 0;',
        ):
            self.assertIn(expected, indium_hdr)

    def test_objective_c_wrapper_and_new_library_url_catch(self):
        impl = added_lines('patches/metal.patch', 'src/Metal/MTLCommandBuffer.mm')
        for selector in (
            '- (MTLCommandBufferStatus)status',
            '- (NSError*)error',
            '- (NSTimeInterval)kernelStartTime',
            '- (NSTimeInterval)kernelEndTime',
            '- (NSTimeInterval)GPUStartTime',
            '- (NSTimeInterval)GPUEndTime',
            '- (void)enqueue',
            '- (void)waitUntilScheduled',
        ):
            self.assertIn(selector, impl)
        dev = diff_section('patches/metal.patch', 'src/Metal/MTLDevice.mm')
        self.assertIn('MTLErrorFromCurrentException("newLibraryWithURL")', dev)
        self.assertIn('try { lib = _device->newLibrary(data.bytes, data.length); }', dev)

    def test_private_command_buffer_appends_new_members_after_committed_atomic(self):
        priv = diff_section('patches/metal--deps--indium.patch',
                            'private-include/indium/command-buffer.private.hpp')
        committed_pos = priv.find('_committedAtomic')
        status_pos = priv.find('_status')
        self.assertGreater(committed_pos, 0)
        self.assertGreater(status_pos, committed_pos)

    def test_batch_check_covers_command_buffer_status_and_timing(self):
        batch = (ROOT / 'tools/riot-metal-batch-check.mm').read_text()
        self.assertIn('MTLCommandBufferStatusNotEnqueued', batch)
        self.assertIn('MTLCommandBufferStatusEnqueued', batch)
        self.assertIn('waitUntilScheduled', batch)
        self.assertIn('GPUStartTime', batch)
        self.assertIn('GPUEndTime', batch)

    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
    def test_monotonic_status_transitions(self):
        cpp_text = added_lines('patches/metal--deps--indium.patch', 'src/indium/command-buffer.cpp')
        match = re.search(r'void Indium::PrivateCommandBuffer::advanceStatus\(.*?\n\}\n', cpp_text, re.S)
        self.assertIsNotNone(match)
        advance_fn = match.group(0).replace('Indium::PrivateCommandBuffer::', 'MockCommandBuffer::')
        harness = f"""
#include <atomic>
#include <cassert>
#include <cstddef>
enum class CommandBufferStatus: size_t {{
    NotEnqueued = 0, Enqueued = 1, Committed = 2, Scheduled = 3, Completed = 4, Error = 5
}};
struct MockCommandBuffer {{
    std::atomic<CommandBufferStatus> _status {{CommandBufferStatus::NotEnqueued}};
    void advanceStatus(CommandBufferStatus next);
    void enqueue() {{
        auto expected = CommandBufferStatus::NotEnqueued;
        _status.compare_exchange_strong(expected, CommandBufferStatus::Enqueued, std::memory_order_acq_rel);
    }}
}};
{advance_fn}
int main() {{
    MockCommandBuffer cb;
    assert(cb._status == CommandBufferStatus::NotEnqueued);
    cb.enqueue();
    assert(cb._status == CommandBufferStatus::Enqueued);
    cb.advanceStatus(CommandBufferStatus::Committed);
    cb.enqueue(); // Late enqueue must not regress Committed status.
    assert(cb._status == CommandBufferStatus::Committed);
    cb.advanceStatus(CommandBufferStatus::Completed);
    cb.advanceStatus(CommandBufferStatus::Scheduled); // Late Scheduled must not regress Completed.
    assert(cb._status == CommandBufferStatus::Completed);
    return 0;
}}
"""
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / 'status_test.cpp'
            exe = Path(folder) / 'status_test'
            src.write_text(harness)
            subprocess.run(['c++', '-std=c++17', str(src), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
