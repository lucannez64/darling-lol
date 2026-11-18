#!/usr/bin/env python3
"""Build the queue-lifetime repair and its descriptor-leak regression probe."""
from pathlib import Path
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riot_build_cache

# build/runtime/build is a symlink to this checkout's build/; resolve it so
# paths refer to the real repository root no matter which copy runs.
project=(Path(__file__).resolve().parent.parent/'build').resolve().parent
build = project / 'build/darling-src/build'
kernel=build/'src/external/xnu/darling/src/libsystem_kernel'
rsp_text=(kernel/'CMakeFiles/system_kernel.dir/objects1.rsp').read_text()
objects=shlex.split(rsp_text)
link_text=(kernel/'CMakeFiles/system_kernel.dir/link.txt').read_text()
corevideo_text=(build/'src/frameworks/CoreVideo/CMakeFiles/CoreVideo.dir/flags.make').read_text()
# The saved CMake flags name the clang resource directory of the compiler that configured the tree (22); the system
# compiler is 23 now and <stdbool.h> disappears. Follow the installed clang.
resource_dir=subprocess.check_output(['clang','-print-resource-dir'],text=True).strip()
flag_texts=[]
def configured_flags(directory):
    text=(directory/'flags.make').read_text()
    flag_texts.append(text)
    result={}
    for line in text.splitlines():
        if line.startswith('C_') and ' = ' in line:
            key,value=line.split(' = ',1)
            result[key]=[re.sub(r'/usr/lib/clang/\d+/include',resource_dir+'/include',v) for v in shlex.split(value)]
            if key == 'C_FLAGS': result[key].append('-mtune=generic')
    return result
kqueue_flags=configured_flags(build/'src/external/libkqueue/CMakeFiles/kqueue.dir')
emulation_flags=configured_flags(kernel/'emulation/CMakeFiles/emulation.dir')
syscall_flags=configured_flags(kernel/'libsyscall/CMakeFiles/libsyscall.dir')
# Build every configured kqueue object with optimization; the configured
# upstream target explicitly uses -O0 even when the rest of the runtime is
# optimized. Preserve its definitions, debug checks, and source selection.
kqueue_flags={**kqueue_flags,'C_FLAGS':[*kqueue_flags['C_FLAGS'],'-O2','-fno-strict-aliasing']}
kqueue_sources=[value.split('/kqueue.dir/',1)[1][:-2] for value in objects if '/kqueue.dir/' in value and value.endswith('.c.o')]
assert kqueue_sources, 'Configured kqueue objects missing'
# The configured emulation flags carry scripts/build.sh's -O3; the -O2 appended below
# comes last and wins, and the stack-layout-dependent files below pin -O0.
# These files are on the per-call path of the guest (BSD trap dispatch, read/
# write, network, kqueue, psynch/ulock, clocks, madvise), so rebuild them
# optimized. -fno-strict-aliasing matches the other optimized rebuilds in this
# repository; the sources are old BSD/xnu C.
repair_flags={**emulation_flags,'C_FLAGS':[*emulation_flags['C_FLAGS'],'-O2','-fno-strict-aliasing']}
syscall_repair_flags={**syscall_flags,'C_FLAGS':[*syscall_flags['C_FLAGS'],'-O2','-fno-strict-aliasing']}
repairs=[(source,kqueue_flags,project/'build/darling-src/src/external/libkqueue') for source in kqueue_sources]
repairs += [('src/xnu_syscall/bsd/bsd_syscall_table.c',repair_flags,project/'build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation')]
repairs += [('src/common/base.c',repair_flags,project/'build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation')]
# Avoid a redundant absolute-time read when the commpage timestamp is unpublished.
repairs += [('wrappers/__commpage_gettimeofday.c',syscall_repair_flags,project/'build/darling-src/src/external/xnu/libsyscall')]
repairs += [('src/xnu_syscall/bsd/impl/'+name,repair_flags,
    project/'build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation')
    for name in ('bsdthread/pthread_canceled.c','kqueue/kevent_qos.c','bsdthread/workq_kernreturn.c','psynch/psynch_cvwait.c','psynch/psynch_mutexwait.c','psynch/psynch_mutexdrop.c','psynch/ulock_wait.c',
                 # the remaining psynch/ulock traps stay RPCs but are still per-wait
                 'psynch/psynch_cvbroad.c','psynch/psynch_cvclrprepost.c','psynch/psynch_cvsignal.c',
                 'psynch/psynch_rw_rdlock.c','psynch/psynch_rw_unlock.c','psynch/psynch_rw_wrlock.c','psynch/ulock_wake.c',
                 # select busy-loop trace (opt-in) and the network flag conversions
                 'select/select.c','network/recvfrom.c','network/recvmsg.c','network/sendto.c','network/sendmsg.c','unistd/read.c','unistd/close.c','process/fork.c',
                 # the allocator's reclamation hint
                 'mman/madvise.c')]
# RPC guards use the full native signal mask. Rebuild both users of the
# configured forced-include header, without changing unrelated kernel objects.
emulation_root=project/'build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation'
rpc_flags={**emulation_flags,'C_FLAGS':[*emulation_flags['C_FLAGS'],'-O2','-fno-omit-frame-pointer']}
# bsdthread_register.c stays at -O0: its pthread/wqueue entry
# wrappers jump into _thread_start/_start_wqthread through inline assembly that
# sets %rsp from the thread argument, i.e. it depends on the stack layout.
# Signal entry with stack-layout-dependent frame walking stays unoptimized on
# purpose: it runs on the fault/suspend signal stacks.
# The configured flags are optimized now (scripts/build.sh), so pin -O0 explicitly, as the
# emulation CMakeLists does for these two files.
unoptimized_flags={**emulation_flags,'C_FLAGS':[*emulation_flags['C_FLAGS'],'-O0']}
repairs += [('src/xnu_syscall/bsd/impl/bsdthread/bsdthread_register.c',unoptimized_flags,emulation_root)]
repairs += [('src/linux_premigration/signal/sigexc.c',unoptimized_flags,emulation_root)]
repairs += [('src/xnu_syscall/mach/impl/mach_traps.c',rpc_flags,emulation_root)]
# User-space clock (per-CPU TSC calibration), its gettimeofday entry and the cached kern.boottime.
repairs += [('src/linux_premigration/ext/clock_gettime.c',rpc_flags,emulation_root)]
repairs += [('src/xnu_syscall/bsd/impl/time/gettimeofday.c',rpc_flags,emulation_root)]
repairs += [('src/other/mach/lkm.c',rpc_flags,emulation_root)]
# sysctl_kern.c takes the emulated OS identity from per-source definitions (emulation/CMakeLists.txt)
identity=['-DEMULATED_SYSNAME="Darwin"','-DEMULATED_RELEASE="23.6.0"',
    '-DEMULATED_VERSION="Darwin Kernel Version 23.6.0: Mon Jul 29 21:14:30 PDT 2024; root:xnu-10063.141.2~1/RELEASE_X86_64"',
    '-DEMULATED_OSVERSION="23H124"','-DEMULATED_OSPRODUCTVERSION="14.7"']
repairs += [('src/xnu_syscall/bsd/helper/misc/sysctl_kern.c',{**emulation_flags,'C_FLAGS':[*emulation_flags['C_FLAGS'],*identity]},emulation_root)]
# The hw.*/machdep.cpu.* sysctls are not on the per-call path: recompile them
# with the configured flags only because these sources changed (the same way
# bsdthread_register.c keeps -O0). sysctl_hw.c now reports the host's real CPU
# topology; sysctl_machdep.c shares its probe for core_count/thread_count.
repairs += [('src/xnu_syscall/bsd/helper/misc/sysctl_hw.c',emulation_flags,emulation_root)]
repairs += [('src/xnu_syscall/bsd/helper/misc/sysctl_machdep.c',emulation_flags,emulation_root)]
repairs += [('src/linux_premigration/resources/dserver-rpc-defs.c',rpc_flags,emulation_root)]
forced_rpc_flags={**rpc_flags,'C_FLAGS':[*rpc_flags['C_FLAGS'],'-include',str(emulation_root/'include/linux_premigration/resources/dserver-rpc-defs.h')]}
repairs += [('src/rpc.c',forced_rpc_flags,build/'src/external/darlingserver')]
# Process-local Mach semaphores (mach_traps.c) wrap semaphore_create/semaphore_destroy: the MIG stubs get private names.
# mach_vm_region_recurse on the own task is answered in the process (mach_traps.c); the MIG stub gets a private name.
mig_flags=configured_flags(kernel/'libsyscall/CMakeFiles/libsyscall_64.dir')
mig_jobs=[('mach/task-x86_64-User.c',project/'build/riot-kernel-task-x86_64-User.o',
           ['-Dsemaphore_create=__darling_rpc_semaphore_create','-Dsemaphore_destroy=__darling_rpc_semaphore_destroy']),
          ('mach/mach_vm-x86_64-User.c',project/'build/riot-kernel-mach_vm-x86_64-User.o',
           ['-Dmach_vm_region_recurse=__darling_rpc_mach_vm_region_recurse'])]
# Descriptor-leak regression probes, relinked against the repaired kernel.
PROBES=('riot-task-info-check','riot-kqueue-lifetime-check','riot-kqueue-stack-check','riot-workqueue-reuse-check','riot-dispatch-fd-check','riot-kqueue-dispatch2-check','riot-syscall-fastpath-check','riot-select-backoff-check','riot-vnode-close-check','riot-rpc-signal-mask-check','riot-fileport-lifetime-check','riot-kqueue-timer-check','riot-cancel-rpc-check','riot-runloop-wake-check')
# Locate every object slot this build replaces (and assert each is unique)
# before compiling; everything else is linked from the configured tree.
replaced=set()
for source,obj,defines in mig_jobs:
    matches=[i for i,value in enumerate(objects) if value.endswith('/'+source+'.o')]
    assert len(matches)==1,(source,matches)
    replaced.add(matches[0])
for source,settings,base in repairs:
    matches=[i for i,value in enumerate(objects) if value.endswith('/'+source+'.o')]
    assert len(matches)==1,(source,matches)
    replaced.add(matches[0])
# Everything that can change the result: this script, the clang resource dir,
# the configured object list and link line, every flags.make consumed, the
# content of each (re)compiled source, the probe sources, the stat of the
# configured objects linked as-is (the cmake build refreshes those itself), and
# the headers recorded by the previous dependency files.
dep_files=[Path(str(obj)+'.d') for _,obj,_ in mig_jobs]
dep_files+=[Path(str(project/('build/riot-kernel-'+source.replace('/','-')+'.o'))+'.d') for source,settings,base in repairs]
dep_files+=[Path(str(project/('build/%s.o'%probe))+'.d') for probe in PROBES]
# The compiled objects list is mutated below; the fingerprint reads the original.
original_objects=list(objects)
def compute_chunks():
    chunks=[riot_build_cache.digest_text(Path(__file__).read_text()),resource_dir,
            riot_build_cache.digest_text(rsp_text),riot_build_cache.digest_text(link_text),
            riot_build_cache.digest_text(corevideo_text)]
    for text in flag_texts:
        chunks.append(riot_build_cache.digest_text(text))
    for source,obj,defines in mig_jobs:
        chunks.append(source+'\0'+riot_build_cache.digest_file(kernel/'libsyscall'/source))
    for source,settings,base in repairs:
        chunks.append(source+'\0'+riot_build_cache.digest_file(base/source))
    for probe in PROBES:
        chunks.append(probe+'\0'+riot_build_cache.digest_file(project/('tools/%s.c'%probe)))
    for index,value in enumerate(original_objects):
        if index not in replaced:
            chunks.append(value+'\0'+riot_build_cache.stat_entry(kernel/value))
    chunks.append(riot_build_cache.dependency_chunk(dep_files))
    return chunks
stamp=project/'build/riot-kqueue-build.json'
chunks=compute_chunks()
digest=riot_build_cache.fingerprint(chunks)
outputs=[project/'build/riot-libsystem_kernel.dylib']+[project/('build/%s'%probe) for probe in PROBES]
if riot_build_cache.stable_inputs(chunks,chunks) and riot_build_cache.up_to_date(stamp,digest,outputs):
    print('libsystem_kernel up to date; skipping rebuild',flush=True)
    sys.exit(0)
# A failed build must not leave the old stamp validating its outputs.
stamp.unlink(missing_ok=True)

for source,obj,defines in mig_jobs:
    subprocess.run(['clang',*mig_flags['C_DEFINES'],*mig_flags['C_INCLUDES'],*mig_flags['C_FLAGS'],
        *defines,'-MD','-MF',str(obj)+'.d','-c',str(kernel/'libsyscall'/source),'-o',str(obj)],check=True,cwd=kernel/'libsyscall')
for source,settings,base in repairs:
    obj=project/('build/riot-kernel-'+source.replace('/','-')+'.o')
    subprocess.run(['clang',*settings['C_DEFINES'],*settings['C_INCLUDES'],*settings['C_FLAGS'],
        '-MD','-MF',str(obj)+'.d','-c',str(base/source),'-o',str(obj)],check=True)
rsp=project/'build/riot-kernel-objects.rsp'
for source,obj,defines in mig_jobs:
    match=next(i for i,value in enumerate(objects) if value.endswith('/'+source+'.o'))
    objects[match]=str(obj)
for source,settings,base in repairs:
    obj=project/('build/riot-kernel-'+source.replace('/','-')+'.o')
    match=next(i for i,value in enumerate(objects) if value.endswith('/'+source+'.o'))
    objects[match]=str(obj)
rsp.write_text(shlex.join(objects))
link=shlex.split(link_text)
link[link.index('@CMakeFiles/system_kernel.dir/objects1.rsp')]='@'+str(rsp)
link[link.index('-o')+1]=str(project/'build/riot-libsystem_kernel.dylib')
subprocess.run(link,cwd=kernel,check=True)
flags = {}
for line in corevideo_text.splitlines():
    if line.startswith('C_') and ' = ' in line:
        name, value = line.split(' = ', 1)
        flags[name] = shlex.split(value)
for probe in PROBES:
    obj = project / f'build/{probe}.o'
    subprocess.run(['clang', *flags['C_DEFINES'], *flags['C_INCLUDES'], *flags['C_FLAGS'],
                    '-O2', '-MD', '-MF', str(obj)+'.d', '-c', str(project / f'tools/{probe}.c'),
                    '-o', str(obj)], check=True)
    subprocess.run(['clang', '-target', 'x86_64-apple-macosx11.0', '-fuse-ld=lld', '-nostdlib',
                    '-isysroot', '@DARLING_SDK@', str(obj), '-lSystem', '-o',
                    str(project / f'build/{probe}')], check=True)
post=compute_chunks()
if not riot_build_cache.stable_inputs(chunks,post):
    # Inputs changed while compiling; record nothing so the next run rebuilds
    # instead of stamping objects compiled from mixed inputs.
    print('inputs changed while building; not recording the stamp',flush=True)
    sys.exit(0)
riot_build_cache.record(stamp,riot_build_cache.fingerprint(post),'libsystem_kernel queue-lifetime repair + probes')
