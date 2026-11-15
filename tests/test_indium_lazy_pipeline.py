"""Lazily built point and line pipelines must not use a render pass saved from an earlier encoder."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def added(name):
    patch = (ROOT / 'patches/metal--deps--indium.patch').read_text()
    marker = f'diff --git a/{name} b/{name}\n'
    section = patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]
    return ''.join(line[1:] + '\n' for line in section.splitlines() if line.startswith('+'))


class LazyPipelineChecks(unittest.TestCase):
    def test_draws_build_missing_pipelines_with_their_own_render_pass(self):
        encoder = added('src/indium/render-command-encoder.cpp')
        for index in range(3):
            self.assertEqual(encoder.count(f'pipelineForTopologyIndex({index}, _renderPass);'), 2)
        self.assertNotIn('pipelineForTopologyIndex(0);', encoder)

    def test_the_pipeline_state_keeps_no_render_pass(self):
        # An encoder-owned render pass dies with its encoder once the device cache is full.
        header = added('private-include/indium/render-pipeline.private.hpp')
        source = added('src/indium/render-pipeline.cpp')
        self.assertNotIn('_compatibleRenderPass', header + source)
        self.assertIn('VkPipeline pipelineForTopologyIndex(size_t index, VkRenderPass compatibleRenderPass);', header)
        self.assertIn('pipelineForTopologyIndex(primaryIndex, compatibleRenderPass);', source)

    def test_lazy_creation_is_serialised_and_leaves_the_vertex_bindings_alone(self):
        source = added('src/indium/render-pipeline.cpp')
        lazy = source.split('PrivateRenderPipelineState::pipelineForTopologyIndex(', 1)[1]
        self.assertIn('std::lock_guard<std::mutex> lock(_lazyPipelineMutex);', lazy)
        self.assertNotIn('_vertexInputBindings =', lazy)
        self.assertEqual(source.count('_vertexInputBindings = plan.metalBindings;'), 1)


if __name__ == '__main__':
    unittest.main()
