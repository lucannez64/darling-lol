"""Exercise the patched AIR reflection scan with synthetic LLVM metadata."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class AirReflectionMetadataTest(unittest.TestCase):
    def test_values_are_not_reinterpreted_as_keys(self):
        patch = (ROOT / 'patches/metal--deps--indium.patch').read_text()
        section = patch.split('+    // Preserve AIR reflection metadata', 1)[1]
        section = section.split('\n+\n \n \t//\n \t// analyze parameters', 1)[0]
        scan = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+'))
        source = r'''
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>
struct Node { std::string text; uint64_t number = 0; bool isString = false; std::vector<Node*> operands; };
using LLVMValueRef = Node*;
namespace DynamicLLVM {
size_t LLVMGetMDNodeNumOperands(Node* node) { return node->operands.size(); }
void LLVMGetMDNodeOperands(Node* node, Node** output) { for (auto item : node->operands) *output++ = item; }
bool LLVMIsAMDString(Node* node) { return node->isString; }
uint64_t LLVMConstIntGetZExtValue(Node* node) { return node->number; }
}
std::string_view llvmMDStringToStringView(Node* node) { return node->text; }
enum class BindingType { Buffer, Texture, Sampler, VertexInput };
struct Member { size_t offset; std::string type; std::string name; };
struct ArgumentInfo { BindingType type; uint64_t index = 0, size = 0, alignment = 0; std::string name, typeName; std::vector<Member> members; };
struct FunctionInfo { std::vector<ArgumentInfo> arguments; };
int main() {
    Node parameterNumber, kind{"air.vertex_input", 0, true}, locationKey{"air.location_index", 0, true};
    Node location{"", 7}, nameKey{"air.arg_name", 0, true}, name{"air.arg_type_name", 0, true};
    Node flag{"air.no_perspective", 0, true}, typeKey{"air.arg_type_name", 0, true}, type{"float4", 0, true};
    Node parameter{"", 0, false, {&parameterNumber, &kind, &locationKey, &location, &nameKey, &name, &flag, &typeKey, &type}};
    std::vector<LLVMValueRef> parameterOperands {&parameter};
    FunctionInfo funcInfo;
''' + scan + r'''
    if (funcInfo.arguments.size() != 1) return 1;
    const auto& result = funcInfo.arguments[0];
    if (result.index != 7 || result.name != "air.arg_type_name" || result.typeName != "float4") return 2;
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / 'air-test.cpp'
            executable = Path(directory) / 'air-test'
            source_path.write_text(source)
            subprocess.run(['c++', '-std=c++17', str(source_path), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    unittest.main()
