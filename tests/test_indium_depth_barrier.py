"""Metal render-target barriers must flush depth writes, so effects that sample scene depth see it (issue #213)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def added(name):
    patch = (ROOT / 'patches/metal--deps--indium.patch').read_text()
    marker = f'diff --git a/{name} b/{name}\n'
    section = patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
    return ''.join(line[1:] + '\n' for line in section.splitlines() if line.startswith('+'))


def flags(text, prefix):
    return set(re.findall(rf'\b{prefix}[A-Z_]+_BIT\b', text))


def memory_barrier():
    encoder = added('src/indium/render-command-encoder.cpp')
    body = encoder.split('void Indium::PrivateRenderCommandEncoder::memoryBarrier(', 1)[1]
    return body.split('\n}\n', 1)[0]


def self_dependency():
    encoder = added('src/indium/render-command-encoder.cpp')
    body = encoder.split('VkSubpassDependency dependency {};', 1)[1]
    return body.split('dependencies.push_back(dependency);', 1)[0]


class DepthBarrierChecks(unittest.TestCase):
    def render_target_lines(self, text):
        return '\n'.join(re.findall(r'if \(scope & 4\)[^;]*;', text, re.S))

    def test_render_target_scope_flushes_depth_into_texture_reads(self):
        targets = self.render_target_lines(memory_barrier())
        self.assertIn('VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT', targets)
        self.assertIn('VK_ACCESS_SHADER_READ_BIT', targets)
        self.assertIn('VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT', targets)
        self.assertIn('VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT', targets)

    def test_barrier_fits_the_render_pass_self_dependency(self):
        # Vulkan: a barrier recorded inside a render pass may only use stages and access the self-dependency declares.
        barrier, dependency = memory_barrier(), self_dependency()
        self.assertLessEqual(flags(barrier, 'VK_PIPELINE_STAGE_'), flags(dependency, 'VK_PIPELINE_STAGE_'))
        self.assertLessEqual(flags(barrier, 'VK_ACCESS_'), flags(dependency, 'VK_ACCESS_'))
        self.assertIn('VK_DEPENDENCY_BY_REGION_BIT', barrier)
        self.assertIn('VK_DEPENDENCY_BY_REGION_BIT', dependency)


if __name__ == '__main__':
    unittest.main()
