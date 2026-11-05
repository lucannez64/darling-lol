#!/usr/bin/env python3
from pathlib import Path
import shlex
import subprocess
root = Path(__file__).resolve().parent.parent
flags = {}
for line in (root / 'build/darling-src/build/src/external/metal/CMakeFiles/Metal.dir/flags.make').read_text().splitlines():
    if line.startswith('CXX_') and ' = ' in line:
        key, value = line.split(' = ', 1)
        flags[key] = shlex.split(value)
resource_include=subprocess.check_output(['clang++','-print-resource-dir'],text=True).strip()+'/include'
flags['CXX_INCLUDES']=[resource_include if x.startswith('/usr/lib/clang/') and x.endswith('/include') else x for x in flags['CXX_INCLUDES']]
obj = root / 'build/riot-metal-step-rate-check.o'
subprocess.run(['clang++', *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'], *flags['CXX_FLAGS'], '-O2', '-c', str(root / 'tools/riot-metal-step-rate-check.mm'), '-o', str(obj)], check=True)
subprocess.run(['clang++', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib', '-isysroot', '@DARLING_SDK@', str(obj), '-lSystem', '-lc++', '-lobjc', '-framework', 'Foundation', str(root / 'build/riot-Metal'), '-o', str(root / 'build/riot-metal-step-rate-check')], check=True)
