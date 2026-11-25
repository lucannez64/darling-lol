"""Compile the patched Indium commit() precommit pass against a host fixture.

A QuartzCore drawable prepares its full image in precommit(). One command
buffer that samples, renders to and blits into the same drawable (directly or
through a view) must prepare it once, while ordinary textures keep one call per
use. Vulkan, encoders and the drawable blit itself are inert fixtures.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_runtime_compatibility as sources


FIXTURE = r'''
#include <algorithm>
#include <cassert>
#include <map>
#include <memory>
#include <vector>
namespace Indium {
struct PrivateCommandBuffer;
static std::map<const void*, int> precommits;
struct Texture {
    virtual ~Texture() = default;
    virtual std::shared_ptr<Texture> parentTexture() { return nullptr; }
};
struct PrivateTexture : Texture {
    bool exportable;
    explicit PrivateTexture(bool drawable = false) : exportable(drawable) {}
    virtual void precommit(std::shared_ptr<PrivateCommandBuffer>) { ++precommits[this]; }
    virtual bool needsExportablePresentationSemaphore() const { return exportable; }
};
struct TextureView : PrivateTexture {
    std::shared_ptr<PrivateTexture> _original;
    explicit TextureView(std::shared_ptr<PrivateTexture> original) : _original(original) {}
    std::shared_ptr<Texture> parentTexture() override { return _original; }
    void precommit(std::shared_ptr<PrivateCommandBuffer> cmdbuf) override { _original->precommit(cmdbuf); }
    bool needsExportablePresentationSemaphore() const override { return _original->needsExportablePresentationSemaphore(); }
};
struct CommandEncoder { virtual ~CommandEncoder() = default; };
struct PrivateRenderCommandEncoder : CommandEncoder {
    std::vector<std::shared_ptr<Texture>> readOnly, readWrite;
    std::vector<std::shared_ptr<Texture>>& readOnlyTextures() { return readOnly; }
    std::vector<std::shared_ptr<Texture>>& readWriteTextures() { return readWrite; }
};
struct PrivateBlitCommandEncoder : CommandEncoder {
    std::vector<std::shared_ptr<Texture>> readWrite;
    std::vector<std::shared_ptr<Texture>>& readWriteTextures() { return readWrite; }
};
struct PrivateCommandBuffer : std::enable_shared_from_this<PrivateCommandBuffer> {
    std::vector<std::shared_ptr<CommandEncoder>> _commandEncoders;
    void precommitAll();
};
}
'''

SCENARIO = r'''
int main() {
    using namespace Indium;
    auto drawable = std::make_shared<PrivateTexture>(true);
    auto other = std::make_shared<PrivateTexture>(true);
    auto offscreen = std::make_shared<PrivateTexture>();
    auto view = std::make_shared<TextureView>(drawable);
    auto nested = std::make_shared<TextureView>(view);
    auto first = std::make_shared<PrivateRenderCommandEncoder>();
    first->readOnly = {drawable, offscreen};
    first->readWrite = {view, drawable};
    auto second = std::make_shared<PrivateRenderCommandEncoder>();
    second->readOnly = {nested, other, offscreen};
    auto blit = std::make_shared<PrivateBlitCommandEncoder>();
    blit->readWrite = {drawable, other};
    auto buffer = std::make_shared<PrivateCommandBuffer>();
    buffer->_commandEncoders = {first, second, blit};
    buffer->precommitAll();
    assert(precommits[drawable.get()] == 1);  // direct, view and nested view uses coalesce
    assert(precommits[other.get()] == 1);
    assert(precommits[offscreen.get()] == 2); // ordinary textures keep per-use calls
    // The next command buffer prepares the drawable again.
    auto next = std::make_shared<PrivateCommandBuffer>();
    next->_commandEncoders = {blit};
    next->precommitAll();
    assert(precommits[drawable.get()] == 2 && precommits[other.get()] == 2);
}
'''


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class DrawablePrecommitChecks(unittest.TestCase):
    patched = sources.CompatibilityChecks.patched

    def test_one_command_buffer_prepares_each_drawable_once(self):
        commands = self.patched('metal--deps--indium', 'src/indium/command-buffer.cpp')
        helper_start = commands.index('static std::shared_ptr<Indium::Texture> rootTexture(')
        helper = commands[helper_start:commands.index('\n}\n', helper_start) + 3]
        commit = commands[commands.index('void Indium::PrivateCommandBuffer::commit() {'):]
        block = commit[commit.index('std::vector<const Texture*> preparedDrawables;'):
                       commit.index('// TODO: same for other encoders')]
        # The submit task reuses the file-scope helper instead of its own lambda.
        self.assertNotIn('auto rootTexture = [', commit)
        program = (FIXTURE + helper + '\nvoid Indium::PrivateCommandBuffer::precommitAll() {\n'
                   + block + '\n\t}\n}\n' + SCENARIO)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'precommit.cpp'
            binary = Path(folder) / 'precommit'
            source.write_text(program)
            result = subprocess.run(['c++', '-std=c++17', str(source), '-o', str(binary)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
