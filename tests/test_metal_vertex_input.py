"""Offline checks for the vertex layouts and shader reflection that game patch 26.22 needs."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_text(name):
    return (ROOT / 'patches' / name).read_text()


def section(patch, path):
    marker = f'diff --git a/{path} b/{path}\n'
    assert marker in patch, path
    return patch.split(marker, 1)[1].split('\ndiff --git ', 1)[0]


def added(text):
    return '\n'.join(line[1:] for line in text.splitlines() if line.startswith('+') and not line.startswith('+++'))


class VertexStepRateTests(unittest.TestCase):
    PATCH = 'metal--deps--indium.patch'

    def test_pipeline_creation_accepts_step_rates(self):
        pipeline = added(section(patch_text(self.PATCH), 'src/indium/render-pipeline.cpp'))
        self.assertNotIn('TODO: support step rates other than 1', pipeline)
        self.assertIn('VkPipelineVertexInputDivisorStateCreateInfoEXT', pipeline)

    def test_device_enables_the_divisor_only_when_supported(self):
        device = added(section(patch_text(self.PATCH), 'src/indium/device.cpp'))
        self.assertIn('if (vertexDivisor.vertexAttributeInstanceRateDivisor) {', device)
        self.assertIn('VK_EXT_VERTEX_ATTRIBUTE_DIVISOR_EXTENSION_NAME', device)

    def test_draws_never_bind_a_null_pipeline(self):
        encoder = added(section(patch_text(self.PATCH), 'src/indium/render-command-encoder.cpp'))
        self.assertEqual(encoder.count('if (drawHasNoPipeline(pipeline)) return;'), 2)


class VertexAttributeReflectionTests(unittest.TestCase):
    def test_iridium_keeps_vertex_inputs(self):
        patch = patch_text('metal--deps--indium.patch')
        self.assertIn('struct VertexAttributeInfo', added(section(patch, 'include/iridium/iridium.hpp')))
        self.assertIn('funcInfo.vertexAttributes.push_back', added(section(patch, 'src/iridium/air.cpp')))
        self.assertIn('funcInfo.vertexAttributes = info.vertexAttributes;', added(section(patch, 'src/indium/device.cpp')))

    def test_metal_functions_report_vertex_attributes(self):
        patch = patch_text('metal.patch')
        self.assertIn('@interface MTLVertexAttribute : NSObject', added(section(patch, 'include/Metal/MTLLibrary.h')))
        library = added(section(patch, 'src/Metal/MTLLibrary.mm'))
        self.assertIn('@implementation MTLVertexAttribute', library)
        self.assertIn('_function->vertexAttributeInfo()', library)


if __name__ == '__main__':
    unittest.main()
