#!/usr/bin/env python3
from pathlib import Path
import shlex, subprocess
root=Path(__file__).resolve().parent.parent
import re
res=subprocess.check_output(['clang','-print-resource-dir'],text=True).strip()
flags={}
for line in (root/'build/darling-src/build/src/frameworks/CoreVideo/CMakeFiles/CoreVideo.dir/flags.make').read_text().splitlines():
    if line.startswith('C_') and ' = ' in line:
        key,value=line.split(' = ',1); flags[key]=[re.sub(r'/usr/lib/clang/\d+/include',res+'/include',v) for v in shlex.split(value)]
obj=root/'build/riot-thread-exit-port-check.o'
subprocess.run(['clang',*flags['C_DEFINES'],*flags['C_INCLUDES'],*flags['C_FLAGS'],'-I'+str(root/'build/darling-src/src/startup/mldr/elfcalls'),'-O2','-c',str(root/'tools/riot-thread-exit-port-check.c'),'-o',str(obj)],check=True)
subprocess.run(['clang','-target','x86_64-apple-macosx11.0','-fuse-ld=lld','-nostdlib','-isysroot','@DARLING_SDK@',str(obj),'-lSystem','-o',str(root/'build/riot-thread-exit-port-check')],check=True)
