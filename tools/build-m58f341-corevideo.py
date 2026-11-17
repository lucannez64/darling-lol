#!/usr/bin/env python3
"""Build the corrected display clock as an isolated per-user candidate."""
import shlex, subprocess
from pathlib import Path
project=Path(__file__).resolve().parent.parent
framework=project/'build/darling-src/build/src/frameworks/CoreVideo'
flags={}
for line in (framework/'CMakeFiles/CoreVideo.dir/flags.make').read_text().splitlines():
 if line.startswith(('C_','CXX_')) and ' = ' in line:
  k,v=line.split(' = ',1);flags[k]=shlex.split(v)
obj=project/'build/m341-CVDisplayLink.o'
subprocess.run(['clang++',*flags['CXX_DEFINES'],*flags['CXX_INCLUDES'],*flags['CXX_FLAGS'],'-O2','-Wno-macro-redefined','-c',str(project/'build/darling-src/src/frameworks/CoreVideo/src/CVDisplayLink.mm'),'-o',str(obj)],check=True)
link=shlex.split((framework/'CMakeFiles/CoreVideo.dir/link.txt').read_text())
link[link.index('CMakeFiles/CoreVideo.dir/src/CVDisplayLink.mm.o')]=str(obj)
link[link.index('-o')+1]=str(project/'build/m341-CoreVideo')
subprocess.run(link,cwd=framework,check=True)
probe=project/'build/m341-clock-check.o'
subprocess.run(['clang',*flags['C_DEFINES'],*flags['C_INCLUDES'],*flags['C_FLAGS'],'-Wno-macro-redefined','-c',str(project/'tools/m58f341-display-clock-check.c'),'-o',str(probe)],check=True)
subprocess.run(['clang','-target','x86_64-apple-macosx11.0','-fuse-ld=lld','-nostdlib','-isysroot','@DARLING_SDK@',str(probe),'-lSystem','-framework','CoreVideo','-o',str(project/'build/m341-clock-check')],check=True)
print('Built isolated CoreVideo display clock and cadence probe')
probe=project/'build/riot-display-link-isolation-check.o'
subprocess.run(['clang',*flags['C_DEFINES'],*flags['C_INCLUDES'],*flags['C_FLAGS'],'-Wno-macro-redefined','-c',str(project/'tools/riot-display-link-isolation-check.c'),'-o',str(probe)],check=True)
subprocess.run(['clang','-target','x86_64-apple-macosx11.0','-fuse-ld=lld','-nostdlib','-isysroot','@DARLING_SDK@',str(probe),'-lSystem','-framework','CoreVideo','-o',str(project/'build/riot-display-link-isolation-check')],check=True)
