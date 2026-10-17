#!/usr/bin/env python3
"""Build the Metal resource/command bridges and their GPU regression check."""
from pathlib import Path
import shlex
import subprocess
root = Path(__file__).resolve().parent.parent
folder = root / 'build/darling-src/build/src/external/metal'
source = root / 'build/darling-src/src/external/metal/src/Metal'
flags = {}
for line in (folder / 'CMakeFiles/Metal.dir/flags.make').read_text().splitlines():
    if line.startswith('CXX_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
resource_include = subprocess.check_output(['clang++', '-print-resource-dir'], text=True).strip() + '/include'
flags['CXX_INCLUDES'] = [resource_include if value.startswith('/usr/lib/clang/') and value.endswith('/include') else value for value in flags['CXX_INCLUDES']]
flags['CXX_INCLUDES'].append('-I' + str(root / 'build/darling-src/src/external/metal/deps/indium/private-include'))
link = shlex.split((folder / 'CMakeFiles/Metal.dir/link.txt').read_text())
# These objects passed live League validation. Keep the broader resource/queue
# optimization experiment out of the default build after fresh-match stalls.
for name in ('MTLBuffer', 'MTLTexture', 'MTLDevice', 'MTLSamplerDescriptor', 'MTLTextureDescriptor', 'MTLCommandBuffer', 'MTLBlitCommandEncoder', 'MTLDepthStencilDescriptor', 'MTLStencilDescriptor', 'MTLVertexDescriptor', 'MTLRenderPipeline', 'MTLComputeCommandEncoder', 'MTLRenderCommandEncoder', 'MTLArgument', 'MTLLibrary'):
    obj = root / f'build/riot-metal-{name}.o'
    subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'],
                    *flags['CXX_FLAGS'], '-O2', '-c', str(source / (name + '.mm')),
                    '-o', str(obj)], check=True)
    original = 'CMakeFiles/Metal.dir/src/Metal/' + name + '.mm.o'
    if original in link:
        link[link.index(original)] = str(obj)
    else:
        link.append(str(obj))
link[link.index('-o') + 1] = str(root / 'build/riot-Metal')
subprocess.run(link, cwd=folder, check=True)
obj = root / 'build/riot-metal-sampler-check.o'
subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'],
                *flags['CXX_FLAGS'], '-c', str(root / 'tools/riot-metal-sampler-check.mm'),
                '-o', str(obj)], check=True)
batch = root / 'build/riot-metal-batch-check.o'
subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'],
                *flags['CXX_FLAGS'], '-c', str(root / 'tools/riot-metal-batch-check.mm'),
                '-o', str(batch)], check=True)
subprocess.run(['clang++', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld',
                '-nostdlib', '-isysroot', '@DARLING_SDK@', str(obj), str(batch),
                '-lSystem', '-lc++', '-lobjc', '-framework', 'Foundation',
                str(root / 'build/riot-Metal'), str(root / 'build/riot-libindium.dylib'), '-o',
                str(root / 'build/riot-metal-sampler-check')], check=True)
