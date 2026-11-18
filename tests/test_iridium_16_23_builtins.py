"""Translate AIR shaders using 16.23 vertex and compute parameter builtins into SPIR-V."""
from pathlib import Path
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import llvm_tools  # noqa: E402

COMPONENT = 'src/external/metal/deps/indium'
CHECKOUT = ROOT / 'build/darling-src' / COMPONENT

VERTEX_AIR = r"""
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

define { <4 x float> } @vs_base(i32 %bv, i32 %bi) {
entry:
  %sum = add i32 %bv, %bi
  %f = sitofp i32 %sum to float
  %v0 = insertelement <4 x float> zeroinitializer, float %f, i32 0
  %out = insertvalue { <4 x float> } undef, <4 x float> %v0, 0
  ret { <4 x float> } %out
}

!air.vertex = !{!0}
!0 = !{ptr @vs_base, !1, !2}
!1 = !{!3}
!2 = !{!4, !5}
!3 = !{!"air.position"}
!4 = !{i32 0, !"air.base_vertex"}
!5 = !{i32 1, !"air.base_instance"}
"""

COMPUTE_AIR = r"""
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@sink = internal global i32 0

define void @cs_pos(<3 x i32> %tid, i32 %gid) {
entry:
  %tx = extractelement <3 x i32> %tid, i32 0
  %mix = add i32 %tx, %gid
  store i32 %mix, ptr @sink, align 4
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @cs_pos, !1, !2}
!1 = !{}
!2 = !{!3, !4}
!3 = !{i32 0, !"air.thread_position_in_threadgroup"}
!4 = !{i32 1, !"air.threadgroup_position_in_grid"}
"""


def pinned_revision():
    manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
    return [entry for entry in manifest['components'] if entry['path'] == COMPONENT][0]['revision']


def metallib(bitcode, name):
    """Minimal one-entry metallib container accepted by Iridium's AIR::Library."""
    def tag(label, payload):
        return label + struct.pack('<H', len(payload)) + payload
    group = (tag(b'NAME', name.encode() + b'\0') + tag(b'TYPE', bytes([2])) +
             tag(b'MDSZ', struct.pack('<Q', len(bitcode))) +
             tag(b'OFFT', struct.pack('<QQQ', 0, 0, 0)) + b'ENDT')
    functions = struct.pack('<II', 1, len(group) + 4) + group
    bitcode_offset = 88 + len(functions)
    header = (b'MTLB' + struct.pack('<HHHBBHH', 0x8001, 1, 2, 0, 0x81, 14, 0) +
              struct.pack('<QQQQQQQQQ', bitcode_offset + len(bitcode), 88, len(functions),
                          0, 0, 0, 0, bitcode_offset, len(bitcode)))
    return header + functions + bitcode


def spirv_builtins(module):
    """Return BuiltIn IDs decorated by OpDecorate (opcode 71, decoration 11)."""
    words = struct.unpack(f'<{len(module) // 4}I', module)
    assert words[0] == 0x07230203, 'not a SPIR-V module'
    found = set()
    index = 5
    while index < len(words):
        opcode, count = words[index] & 0xffff, words[index] >> 16
        operands = words[index + 1:index + count]
        if opcode == 71 and len(operands) >= 3 and operands[1] == 11:
            found.add(operands[2])
        index += count
    return found


def prerequisites():
    if not (shutil.which('clang++') and shutil.which('git') and llvm_tools.find('llvm-config')):
        return 'clang++, git and llvm-config are required'
    if not (CHECKOUT / '.git').exists():
        return 'the Indium component checkout is not prepared'
    found = subprocess.run(['git', '-C', str(CHECKOUT), 'cat-file', '-e',
                            pinned_revision() + '^{commit}'], capture_output=True)
    return None if found.returncode == 0 else 'the pinned Indium revision is not in the checkout'


SKIP = prerequisites()


class Iridium1623BuiltinsTests(unittest.TestCase):
    def test_patch_handles_16_23_parameter_kinds(self):
        patch = (ROOT / 'patches/metal--deps--indium.patch').read_text()
        section = patch.split('diff --git a/src/iridium/air.cpp b/src/iridium/air.cpp\n', 1)[1]
        section = section.split('\ndiff --git ', 1)[0]
        added = ''.join(line[1:] + '\n' for line in section.splitlines() if line.startswith('+'))
        for token in (
            '"air.base_vertex"',
            '"air.base_instance"',
            '"air.thread_position_in_threadgroup"',
            '"air.threadgroup_position_in_grid"',
        ):
            self.assertIn(token, added)

    @unittest.skipIf(SKIP, SKIP)
    def test_translates_base_vertex_base_instance_and_compute_thread_positions(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            tree = work / 'indium'
            tree.mkdir()
            archive = subprocess.run(['git', '-C', str(CHECKOUT), 'archive', pinned_revision()],
                                     capture_output=True, check=True).stdout
            subprocess.run(['tar', '-x', '-C', str(tree)], input=archive, check=True)
            subprocess.run(['git', 'apply', str(ROOT / 'patches/metal--deps--indium.patch')],
                           cwd=tree, check=True)

            library = Path(llvm_tools.config('--libdir')) / llvm_tools.config('--link-shared', '--libnames')
            translator = work / 'translate'
            subprocess.run(['clang++', '-O0', '-std=c++17', '-w',
                            f'-DHOST_LLVM_LIBNAME="{library}"',
                            '-DIRIDIUM_SOURCE_HASH="host-test-translator-identity"',
                            '-I' + llvm_tools.config('--includedir'),
                            '-I' + str(tree / 'include'), '-I' + str(tree / 'private-include'),
                            str(ROOT / 'tools/riot-air-translate-check.cpp'),
                            *[str(tree / 'src/iridium' / f'{name}.cpp')
                              for name in ('air', 'dynamic-llvm', 'iridium', 'spirv')],
                            '-ldl', '-o', str(translator)], check=True)

            llvm_as = str(Path(llvm_tools.config('--bindir')) / 'llvm-as')
            vs_ll, vs_bc = work / 'vs.ll', work / 'vs.bc'
            cs_ll, cs_bc = work / 'cs.ll', work / 'cs.bc'
            vs_ll.write_text(VERTEX_AIR)
            cs_ll.write_text(COMPUTE_AIR)
            subprocess.run([llvm_as, str(vs_ll), '-o', str(vs_bc)], check=True)
            subprocess.run([llvm_as, str(cs_ll), '-o', str(cs_bc)], check=True)
            (work / 'in').mkdir()
            (work / 'out').mkdir()
            (work / 'in/vs_base.bin').write_bytes(metallib(vs_bc.read_bytes(), 'vs_base'))
            (work / 'in/cs_pos.bin').write_bytes(metallib(cs_bc.read_bytes(), 'cs_pos'))
            run = subprocess.run([str(translator), str(work / 'in'), str(work / 'out')],
                                 capture_output=True, text=True, timeout=60)
            self.assertIn('2 passed, 0 failed', run.stdout, run.stdout + run.stderr)
            builtins = spirv_builtins((work / 'out/cs_pos.bin.spv').read_bytes())
            self.assertTrue({26, 27}.issubset(builtins), f'expected BuiltIn 26/27, got {builtins}')


if __name__ == '__main__':
    unittest.main()
