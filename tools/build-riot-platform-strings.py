#!/usr/bin/env python3
"""Optimize configured string helpers, preserving every other platform object.

Every src/string/generic source is recompiled at -O3 -fno-builtin (and
-fno-strict-aliasing for this old BSD C) with the object library's own defines
and includes, then relinked into build/riot-libsystem_platform.dylib. The AVX2
and REP paths for memcpy/memmove/memset/bzero/memcmp/memchr live in those same
sources and are selected from the commpage at run time, so this builder only
has to change the optimization level. A stored fingerprint of the script,
effective flags, link line, source contents, retained objects and dependency
headers skips the whole rebuild when nothing changed; the compile loop runs in
parallel like the other builders.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex,shutil,subprocess,sys
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
# The configured objects the link keeps as-is: relinking against refreshed
# objects must invalidate the cache even when no recompiled source changed.
retained=[entry for entry in link
          if entry.startswith('CMakeFiles/platform_obj.dir/') and entry.endswith('.o')
          and entry not in {'CMakeFiles/platform_obj.dir/'+relative+'.o' for relative,_ in jobs}]
# Wait for a quiet machine before hashing (and later compiling).
riot_build_gate.wait_until_free()

def compute_chunks():
 chunks=[riot_build_cache.digest_text(Path(__file__).read_text()),
         # The effective flags, after the clang resource-directory rewrite: a
         # compiler update changes these and must invalidate the cache.
         riot_build_cache.digest_text('\0'.join(k+'='+' '.join(flags[k]) for k in sorted(flags))),
         riot_build_cache.digest_text(link_text)]
 for relative,obj in jobs:
  chunks.append(relative+'\0'+riot_build_cache.digest_file(source_root/relative))
 for entry in retained:
  chunks.append(entry+'\0'+riot_build_cache.stat_entry(folder/entry))
 chunks.append(riot_build_cache.dependency_chunk([Path(str(obj)+'.d') for _,obj in jobs]))
 return chunks

stamp=root/'build/riot-platform-strings-build.json'
chunks=compute_chunks()
digest=riot_build_cache.fingerprint(chunks)
outputs=[root/'build/riot-libsystem_platform.dylib',
         root/'build/m810-platform-runtime/libsystem_platform.dylib']
if riot_build_cache.stable_inputs(chunks,chunks) and riot_build_cache.up_to_date(stamp,digest,outputs):
 print('libsystem_platform up to date; skipping rebuild',flush=True)
 sys.exit(0)
# A failed build must not leave the old stamp validating its outputs.
stamp.unlink(missing_ok=True)

def compile_one(job):
 relative,obj=job
 c_flags=[f for f in flags['C_FLAGS'] if f not in ('-g','-O0','-O1','-O2')]
 subprocess.run(['clang',*flags['C_DEFINES'],*flags['C_INCLUDES'],*c_flags,'-O3','-march=native','-mtune=generic','-fno-builtin','-fno-strict-aliasing','-MD','-MF',str(obj)+'.d','-c',str(source_root/relative),'-o',str(obj)],check=True)

with ThreadPoolExecutor(max_workers=6) as pool:
 list(pool.map(compile_one,jobs))
for relative,obj in jobs:
 link[link.index('CMakeFiles/platform_obj.dir/'+relative+'.o')]=str(obj)
recompiled=len(jobs)
link[link.index('-o')+1]=str(root/'build/riot-libsystem_platform.dylib')
subprocess.run(link,cwd=folder,check=True)
print('built build/riot-libsystem_platform.dylib with',recompiled,'string objects at -O3')
p=root/'build/m810-platform-runtime';p.mkdir(exist_ok=True);shutil.copy2(root/'build/riot-libsystem_platform.dylib',p/'libsystem_platform.dylib')
post=compute_chunks()
if not riot_build_cache.stable_inputs(chunks,post):
 # Inputs changed while compiling; record nothing so the next run rebuilds
 # instead of stamping objects compiled from mixed inputs.
 print('inputs changed while building; not recording the stamp',flush=True)
 sys.exit(0)
riot_build_cache.record(stamp,riot_build_cache.fingerprint(post),'libsystem_platform -O3 -march=native')
