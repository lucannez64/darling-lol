#!/usr/bin/env python3
"""Build and run the host-only CPU-topology sysctl check.

Compiles Darling's actual sysctl_hw.c and sysctl_unspec.c against host shims
(host /sys reads, host syscalls, sentinel Mach replies). Nothing but the
check's own temporary flag file is written; no prefix, guest or process is
touched.
"""
from pathlib import Path
import subprocess

project = Path(__file__).resolve().parent.parent
folder = project / "build/logs/sysctl-cpu"
emulation = project / "build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation"
headers = emulation / "include"
alias = folder / "include/darling/emulation"
alias.parent.mkdir(parents=True, exist_ok=True)
if not alias.exists():
    alias.symlink_to(headers, target_is_directory=True)
if alias.resolve() != headers.resolve():
    raise RuntimeError("Unexpected host-test header alias")

# Minimal host shims for the guest-only headers the sources include.
(folder / "include/sys").mkdir(parents=True, exist_ok=True)
(folder / "include/mach").mkdir(parents=True, exist_ok=True)
(folder / "include/sys/sysctl.h").write_text("""\
/* Host shim: the parts of the guest <sys/sysctl.h> the checked sources use. */
#ifndef HOST_SHIM_SYS_SYSCTL_H
#define HOST_SHIM_SYS_SYSCTL_H
#include <stddef.h>
#define CTLTYPE_NODE   1
#define CTLTYPE_INT    2
#define CTLTYPE_STRING 3
#define CTLTYPE_QUAD   4
#define CTLTYPE_OPAQUE 5
#define CTLTYPE_STRUCT 6
#define CTL_HW         6
#define HW_MACHINE     1
#define HW_MODEL       2
#define HW_NCPU        3
#define HW_PAGESIZE    7
#define HW_MEMSIZE     24
#define HW_AVAILCPU    25
#endif
""")
(folder / "include/mach/machine.h").write_text("""\
/* Host shim: CPU id types for the checked sources. */
#ifndef HOST_SHIM_MACH_MACHINE_H
#define HOST_SHIM_MACH_MACHINE_H
typedef int cpu_type_t;
typedef int cpu_subtype_t;
typedef int cpu_threadtype_t;
#endif
""")
(folder / "include/mach/mach_init.h").write_text("""\
/* Host shim: Mach init types and calls for the checked sources. */
#ifndef HOST_SHIM_MACH_MACH_INIT_H
#define HOST_SHIM_MACH_MACH_INIT_H
#include <stdint.h>
typedef int kern_return_t;
typedef unsigned int mach_port_t;
typedef unsigned int mach_port_name_t;
typedef mach_port_t ipc_space_t;
mach_port_t mach_host_self(void);
mach_port_t mach_task_self(void);
#endif
""")
(folder / "include/mach/host_info.h").write_text("""\
/* Host shim: HOST_BASIC_INFO as the checked source reads it. */
#ifndef HOST_SHIM_MACH_HOST_INFO_H
#define HOST_SHIM_MACH_HOST_INFO_H
#include <stdint.h>
#include <mach/machine.h>
typedef int integer_t;
typedef unsigned int mach_msg_type_number_t;
typedef integer_t* host_info_t;
typedef int host_flavor_t;
#define HOST_BASIC_INFO 1
#define HOST_BASIC_INFO_COUNT 8
struct host_basic_info {
    integer_t avail_cpus;
    integer_t max_cpus;
    cpu_type_t cpu_type;
    cpu_subtype_t cpu_subtype;
    cpu_threadtype_t cpu_threadtype;
    integer_t physical_cpu;
    integer_t physical_cpu_max;
    integer_t logical_cpu;
    integer_t logical_cpu_max;
    uint64_t max_mem;
};
#endif
""")

flag = "/tmp/rclol-fixed-cpu-count.host-check"
binary = folder / "host-sysctl-cpu-check"
sources = [
    project / "tools/riot-host-sysctl-cpu-check.c",
    emulation / "src/xnu_syscall/bsd/helper/misc/sysctl_hw.c",
    emulation / "src/xnu_syscall/bsd/helper/misc/sysctl_unspec.c",
    emulation / "src/xnu_syscall/bsd/helper/misc/sysctl_machdep.c",
    emulation / "src/common/cpu_identity.c",
]
subprocess.run(["cc", "-std=gnu11", "-O2", "-Wall", "-Wno-unused-function",
                "-I", str(folder / "include"),
                '-DDARLING_CPU_FIXED_FLAG="' + flag + '"',
                *[str(source) for source in sources], "-o", str(binary)], check=True)
print("Built", binary)

# Each mode is its own process: the topology probe caches once per process.
subprocess.run([str(binary)], check=True)
subprocess.run([str(binary), "affinity"], check=True)
subprocess.run([str(binary), "no-detect"], check=True)
Path(flag).unlink(missing_ok=True)
Path(flag).write_text("")
try:
    subprocess.run([str(binary), "optout"], check=True)
finally:
    Path(flag).unlink(missing_ok=True)

# machdep.cpu.*: the host's own view, then the compat profile. The profile is
# read from /proc/self/environ, so it has to be in the environment at exec.
import os
compat_env = {key: value for key, value in os.environ.items()
              if key not in ("RCLOL_PLATFORM_PROFILE", "RCLOL_CPU_BRAND")}
subprocess.run([str(binary), "machdep"], check=True, env=compat_env)
compat_env["RCLOL_PLATFORM_PROFILE"] = "compat"
compat_env["RCLOL_CPU_BRAND"] = "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz"
subprocess.run([str(binary), "machdep-compat"], check=True, env=compat_env)
