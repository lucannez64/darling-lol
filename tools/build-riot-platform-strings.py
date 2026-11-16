#!/usr/bin/env python3
"""Optimize configured string helpers, preserving every other platform object.

Every src/string/generic source is recompiled at -O3 -fno-builtin (and
-fno-strict-aliasing for this old BSD C) with the object library's own defines
and includes, then relinked into build/riot-libsystem_platform.dylib. The AVX2
and REP paths for memcpy/memmove/memset/bzero/memcmp/memchr live in those same
sources and are selected from the commpage at run time, so this builder only
has to change the optimization level. A stored fingerprint of the script,
flags, link line and source contents skips the whole rebuild when nothing
changed; the compile loop runs in parallel like the other builders.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex,subprocess,shutil,sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import riot_build_cache,riot_build_gate
# build/runtime/build is a symlink to this checkout's build/; resolve it so
# paths refer to the real repository root no matter which copy runs.
root=(Path(__file__).resolve().parent.parent/'build').resolve().parent
folder=root/'build/darling-src/build/src/external/libplatform'
source_root=root/'build/darling-src/src/external/libplatform'
flags_text=(folder/'CMakeFiles/platform_obj.dir/flags.make').read_text()
flags={}
for line in flags_text.splitlines():
 if line.startswith('C_') and ' = ' in line:
  k,v=line.split(' = ',1);flags[k]=shlex.split(v)
inc=subprocess.check_output(['clang','-print-resource-dir'],text=True).strip()+'/include'
flags['C_INCLUDES']=[inc if x.startswith('/usr/lib/clang/') and x.endswith('/include') else x for x in flags['C_INCLUDES']]
link_text=(folder/'CMakeFiles/platform.dir/link.txt').read_text()
link=shlex.split(link_text)
jobs=[]
for original in list(link):
 if not original.startswith('CMakeFiles/platform_obj.dir/src/string/generic/') or not original.endswith('.c.o'):continue
 relative=original.split('platform_obj.dir/',1)[1].removesuffix('.o')
 obj=root/('build/riot-platform-'+Path(relative).name+'.o')
 jobs.append((relative,obj))
chunks=[riot_build_cache.digest_text(Path(__file__).read_text()),
        riot_build_cache.digest_text(flags_text),
        riot_build_cache.digest_text(link_text)]
for relative,obj in jobs:
 chunks.append(relative+'\0'+riot_build_cache.digest_file(source_root/relative))
stamp=root/'build/riot-platform-strings-build.json'
digest=riot_build_cache.fingerprint(chunks)
outputs=[root/'build/riot-libsystem_platform.dylib',
         root/'build/m810-platform-runtime/libsystem_platform.dylib']
if riot_build_cache.up_to_date(stamp,digest,outputs):
 print('libsystem_platform up to date; skipping rebuild',flush=True)
 sys.exit(0)

def compile_one(job):
 relative,obj=job
 c_flags=[f for f in flags['C_FLAGS'] if f not in ('-g','-O0','-O1','-O2')]
 subprocess.run(['clang',*flags['C_DEFINES'],*flags['C_INCLUDES'],*c_flags,'-O3','-march=native','-mtune=generic','-fno-builtin','-fno-strict-aliasing','-c',str(source_root/relative),'-o',str(obj)],check=True)

riot_build_gate.wait_until_free()
with ThreadPoolExecutor(max_workers=6) as pool:
 list(pool.map(compile_one,jobs))
for relative,obj in jobs:
 link[link.index('CMakeFiles/platform_obj.dir/'+relative+'.o')]=str(obj)
recompiled=len(jobs)
link[link.index('-o')+1]=str(root/'build/riot-libsystem_platform.dylib')
subprocess.run(link,cwd=folder,check=True)
print('built build/riot-libsystem_platform.dylib with',recompiled,'string objects at -O3')
p=root/'build/m810-platform-runtime';p.mkdir(exist_ok=True);shutil.copy2(root/'build/riot-libsystem_platform.dylib',p/'libsystem_platform.dylib')
riot_build_cache.record(stamp,digest,'libsystem_platform -O3 -march=native')
