"""Exercise the patched vertex fallback bounds checks with real Indium types."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INDIUM = ROOT / 'build/darling-src/src/external/metal/deps/indium'


@unittest.skipUnless(shutil.which('c++') and (INDIUM / 'include/indium/vertex-descriptor.hpp').exists(),
                     'C++ compiler and prepared Indium headers required')
class VertexBufferFallbackTest(unittest.TestCase):
    def test_zero_stride_bounds_and_unsupported_fetches(self):
        patch = (ROOT / 'patches/metal--deps--indium.patch').read_text()
        body = patch.split('+void Indium::PrivateRenderPipelineState::validateVertexBufferFallback', 1)[1]
        body = body.split('\n+};', 1)[0]
        lines = body.splitlines()
        body = lines[0] + '\n' + '\n'.join(line[1:] for line in lines[1:] if line.startswith('+'))
        source = r'''
#include <indium/vertex-descriptor.hpp>
#include <optional>
#include <stdexcept>
#include <string>
#include <limits>
namespace Indium {
class PrivateRenderPipelineState {
public:
    std::optional<VertexDescriptor> _vertexDescriptor;
    void validateVertexBufferFallback(size_t metalIndex, size_t length) const;
};
}
void Indium::PrivateRenderPipelineState::validateVertexBufferFallback''' + body + r'''
};
bool rejected(Indium::PrivateRenderPipelineState& pipeline, size_t length) {
    try { pipeline.validateVertexBufferFallback(29, length); return false; }
    catch (const std::runtime_error&) { return true; }
}
int main() {
    using namespace Indium;
    PrivateRenderPipelineState pipeline;
    pipeline._vertexDescriptor.emplace();
    auto& descriptor = pipeline._vertexDescriptor.value();
    descriptor.layouts[29] = {32, VertexStepFunction::Constant, 0};
    descriptor.attributes[0] = {VertexFormat::Float4, 48, 29};
    descriptor.attributes[1] = {VertexFormat::Float4, 9999, 30};
    if (rejected(pipeline, 64)) return 1; // Constant step forces effective zero stride; exact end fits.
    if (!rejected(pipeline, 63)) return 2; // Fetch crosses the buffer end.
    descriptor.attributes[0].offset = std::numeric_limits<size_t>::max();
    if (!rejected(pipeline, 64)) return 3; // Offset arithmetic must not wrap.
    descriptor.attributes[0].offset = 0;
    descriptor.layouts[29] = {0, VertexStepFunction::PerVertex, 1};
    if (rejected(pipeline, 64)) return 4; // Ordinary zero-stride layout is also bounded.
    descriptor.layouts[29].stride = 1;
    if (!rejected(pipeline, 64)) return 5; // Unbounded vertex indices cannot use a finite fallback.
    descriptor.layouts[29].stride = 0;
    descriptor.attributes[0].format = VertexFormat::Invalid;
    if (!rejected(pipeline, 64)) return 6;
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / 'vertex-fallback.cpp'
            executable = Path(directory) / 'vertex-fallback'
            source_path.write_text(source)
            subprocess.run(['c++', '-std=c++17', '-I' + str(INDIUM / 'include'),
                            str(source_path), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    unittest.main()
