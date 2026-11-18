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

COMPUTE_16BIT_AIR = r"""
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@sink16 = internal global i16 0

define void @cs_pos16(<2 x i16> %tid, i16 %gid) {
entry:
  %ty = extractelement <2 x i16> %tid, i32 1
  %mix = add i16 %ty, %gid
  store i16 %mix, ptr @sink16, align 2
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @cs_pos16, !1, !2}
!1 = !{}
!2 = !{!3, !4}
!3 = !{i32 0, !"air.thread_position_in_threadgroup"}
!4 = !{i32 1, !"air.threadgroup_position_in_grid"}
"""

# The two League 16.23 kernels that failed on main take these builtins as uint3; ushort3 too.
COMPUTE_VEC3_AIR = r"""
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

@sink = internal global i32 0
@sink16 = internal global i16 0

define void @cs_pos3(<3 x i32> %tid, <3 x i16> %gid) {
entry:
  %tz = extractelement <3 x i32> %tid, i32 2
  store i32 %tz, ptr @sink, align 4
  %gz = extractelement <3 x i16> %gid, i32 2
  store i16 %gz, ptr @sink16, align 2
  ret void
}

!air.kernel = !{!0}
!0 = !{ptr @cs_pos3, !1, !2}
!1 = !{}
!2 = !{!3, !4}
!3 = !{i32 0, !"air.thread_position_in_threadgroup"}
!4 = !{i32 1, !"air.threadgroup_position_in_grid"}
"""

# The 16.23 blur kernels' pattern: a threadgroup tile written per thread, a threadgroup barrier,
# then a neighbour read. threadgroup_barrier(mem_flags::mem_threadgroup) is air.wg.barrier(2, 1).
COMPUTE_THREADGROUP_AIR = r"""
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"

%struct.tile = type { [64 x <2 x float>] }
@tile = internal unnamed_addr addrspace(3) global %struct.tile zeroinitializer, align 8
@sink = internal global <2 x float> zeroinitializer

define void @cs_tile(<3 x i32> %tid) {
entry:
  %x = extractelement <3 x i32> %tid, i32 0
  %i = zext i32 %x to i64
  %slot = getelementptr inbounds %struct.tile, ptr addrspace(3) @tile, i64 0, i32 0, i64 %i
  store <2 x float> <float 1.0, float 2.0>, ptr addrspace(3) %slot, align 8
  tail call void @air.wg.barrier(i32 2, i32 1)
  %nx = xor i32 %x, 1
  %n = zext i32 %nx to i64
  %neighbour = getelementptr inbounds %struct.tile, ptr addrspace(3) @tile, i64 0, i32 0, i64 %n
  %v = load <2 x float>, ptr addrspace(3) %neighbour, align 8
  store <2 x float> %v, ptr @sink, align 8
  ret void
}

declare void @air.wg.barrier(i32, i32)

!air.kernel = !{!0}
!0 = !{ptr @cs_tile, !1, !2}
!1 = !{}
!2 = !{!3}
!3 = !{i32 0, !"air.thread_position_in_threadgroup"}
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


def spirv_instructions(module):
    """Yield (opcode, operands) for every instruction of a SPIR-V module."""
    words = struct.unpack(f'<{len(module) // 4}I', module)
    assert words[0] == 0x07230203, 'not a SPIR-V module'
    index = 5
    while index < len(words):
        opcode, count = words[index] & 0xffff, words[index] >> 16
        assert count > 0 and index + count <= len(words), f'invalid SPIR-V word count {count} at word {index}'
        yield opcode, words[index + 1:index + count]
        index += count


def spirv_builtins(module):
    """Return BuiltIn IDs decorated by OpDecorate (opcode 71, decoration 11)."""
    return {operands[2] for opcode, operands in spirv_instructions(module)
            if opcode == 71 and len(operands) >= 3 and operands[1] == 11}


def spirv_constants(module):
    """Map the result IDs of 32-bit OpConstant (opcode 43) to their values."""
    return {operands[1]: operands[2] for opcode, operands in spirv_instructions(module)
            if opcode == 43 and len(operands) == 3}


def prerequisites():
    if not (shutil.which('clang++') and shutil.which('git') and llvm_tools.find('llvm-config')):
        return 'clang++, git and llvm-config are required'
    assembler = Path(llvm_tools.config('--bindir')) / 'llvm-as'
    if not assembler.is_file():
        return f'llvm-as executable missing at {assembler}'
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
            'SPIRV::BuiltinID::LocalInvocationId',
            'SPIRV::BuiltinID::WorkgroupId',
            'SPIRVOp::UConvert',
            'StorageClass::Workgroup',
            '"air.wg.barrier"',
            'SPIRVOp::ControlBarrier',
        ):
            self.assertIn(token, added)

    @unittest.skipIf(SKIP, SKIP)
    def test_translates_base_vertex_base_instance_and_compute_thread_positions(self):
        # 4424/4425 BaseVertex/BaseInstance, 26 WorkgroupId, 27 LocalInvocationId
        expected = {'vs_base': {4424, 4425}, 'cs_pos': {26, 27}, 'cs_pos16': {26, 27}, 'cs_pos3': {26, 27},
                    'cs_tile': {27}}
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
            (work / 'in').mkdir()
            (work / 'out').mkdir()
            for name, ir in (('vs_base', VERTEX_AIR), ('cs_pos', COMPUTE_AIR), ('cs_pos16', COMPUTE_16BIT_AIR),
                             ('cs_pos3', COMPUTE_VEC3_AIR), ('cs_tile', COMPUTE_THREADGROUP_AIR)):
                ll, bc = work / f'{name}.ll', work / f'{name}.bc'
                ll.write_text(ir)
                subprocess.run([llvm_as, str(ll), '-o', str(bc)], check=True)
                (work / 'in' / f'{name}.bin').write_bytes(metallib(bc.read_bytes(), name))
            run = subprocess.run([str(translator), str(work / 'in'), str(work / 'out')],
                                 capture_output=True, text=True, timeout=60)
            self.assertIn(f'{len(expected)} passed, 0 failed', run.stdout, run.stdout + run.stderr)
            for name, wanted in expected.items():
                builtins = spirv_builtins((work / 'out' / f'{name}.bin.spv').read_bytes())
                self.assertTrue(wanted.issubset(builtins), f'{name}: expected BuiltIn {wanted}, got {builtins}')
                spirv_val = shutil.which('spirv-val')
                if spirv_val:
                    # Iridium sizes every kernel with LocalSizeId (maintenance4); that predates these builtins
                    check = subprocess.run([spirv_val, '--target-env', 'vulkan1.2', '--allow-localsizeid',
                                            str(work / 'out' / f'{name}.bin.spv')],
                                           capture_output=True, text=True)
                    self.assertEqual(check.returncode, 0, f'{name}: {check.stdout}{check.stderr}')

            # threadgroup tile: an OpVariable (59) in Workgroup storage (4), and OpControlBarrier (224)
            # with Workgroup (2) execution/memory scopes and AcquireRelease|WorkgroupMemory (0x108)
            tile = (work / 'out' / 'cs_tile.bin.spv').read_bytes()
            instructions = list(spirv_instructions(tile))
            self.assertTrue(any(opcode == 59 and operands[2] == 4 for opcode, operands in instructions))
            constants = spirv_constants(tile)
            barriers = [[constants.get(operand) for operand in operands] for opcode, operands in instructions if opcode == 224]
            self.assertEqual(barriers, [[2, 2, 0x108]])


if __name__ == '__main__':
    unittest.main()
