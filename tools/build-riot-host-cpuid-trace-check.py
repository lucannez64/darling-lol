#!/usr/bin/env python3
"""Build and run the host-only cpuid execute-breakpoint tracer check.

Compiles darling-lol's actual cpuid_trace.c (the real arming + SIGTRAP handler)
against tiny host shims and runs it. Nothing but the check's own temporary log
file is written; no prefix, guest or process is touched. The production flag and
log paths are overridden so the real /tmp files are never read or created.

The tracer translation unit is compiled with a shim <sys/signal.h> (the guest
signal headers only need `sigset_t` to exist for prototypes the tracer never
calls) and with the predefined `linux`/`unix` macros removed, because the guest
sources are normally compiled for a Darwin target where those are absent. The
test harness translation unit uses the real host libc, then the two are linked.
"""
from pathlib import Path
import subprocess
import tempfile

project = Path(__file__).resolve().parent.parent
emulation = project / "build/darling-src/src/external/xnu/darling/src/libsystem_kernel/emulation"
headers = emulation / "include"
folder = project / "build/logs/cpuid-trace"
alias = folder / "include/darling/emulation"
alias.parent.mkdir(parents=True, exist_ok=True)
if not alias.exists():
    alias.symlink_to(headers, target_is_directory=True)
if alias.resolve() != headers.resolve():
    raise RuntimeError("Unexpected host-test header alias")

# Shim <sys/signal.h>: satisfy the guest signal headers without pulling in the
# host's, which conflicts with the guest's own sigset handling in one TU.
shim = folder / "shim"
(shim / "sys").mkdir(parents=True, exist_ok=True)
(shim / "sys/signal.h").write_text(
    "#ifndef HOST_SHIM_SYS_SIGNAL_H\n"
    "#define HOST_SHIM_SYS_SIGNAL_H\n"
    "typedef unsigned long sigset_t;\n"
    "#endif\n")

binary = folder / "host-cpuid-trace-check"
with tempfile.TemporaryDirectory(prefix="cpuid-trace-") as tmp:
    log = Path(tmp) / "trace.log"
    flag = Path(tmp) / "flag"  # never created: the tracer's setup() path is unused here
    trace_o = folder / "cpuid_trace.o"
    test_o = folder / "check.o"

    subprocess.run(
        ["cc", "-std=gnu11", "-O2", "-g", "-Wall", "-Wno-unused-function",
         "-Ulinux", "-Uunix",
         "-I", str(shim), "-I", str(folder / "include"),
         '-DRCLOL_CPUID_LOG_PATH="' + str(log) + '"',
         '-DRCLOL_CPUID_FLAG_PATH="' + str(flag) + '"',
         "-c", str(emulation / "src/linux_premigration/signal/cpuid_trace.c"),
         "-o", str(trace_o)],
        check=True)
    # The identity table is shared with the sysctl side (common/cpu_identity.c).
    identity_o = folder / "cpu_identity.o"
    subprocess.run(
        ["cc", "-std=gnu11", "-O2", "-g", "-Wall", "-Wno-unused-function",
         "-Ulinux", "-Uunix",
         "-I", str(shim), "-I", str(folder / "include"),
         "-c", str(emulation / "src/common/cpu_identity.c"),
         "-o", str(identity_o)],
        check=True)
    subprocess.run(
        ["cc", "-std=gnu11", "-O2", "-g", "-Wall", "-Wno-unused-function", "-pthread",
         "-I", str(folder / "include"),
         "-c", str(project / "tools/riot-host-cpuid-trace-check.c"),
         "-o", str(test_o)],
        check=True)
    subprocess.run(["cc", "-pthread", str(test_o), str(trace_o), str(identity_o), "-o", str(binary)], check=True)
    print("Built", binary)

    subprocess.run([str(binary)], check=True)

    # The profile switch: the values are read from /proc/self/environ, so they
    # have to be in the environment when the check process is executed.
    import os
    plain_env = {key: value for key, value in os.environ.items()
                 if key not in ("RCLOL_PLATFORM_PROFILE", "RCLOL_CPU_BRAND")}
    subprocess.run([str(binary), "profile", "host"], check=True, env=plain_env)
    compat_env = dict(plain_env)
    compat_env["RCLOL_PLATFORM_PROFILE"] = "compat"
    compat_env["RCLOL_CPU_BRAND"] = "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz"
    subprocess.run([str(binary), "profile", "compat",
                    "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz"], check=True, env=compat_env)

    if log.exists():
        print("\n--- tracer log ---")
        print(log.read_text(), end="")
