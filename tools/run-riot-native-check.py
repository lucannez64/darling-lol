#!/usr/bin/env python3
"""Run an owned native probe with bounded capture and scoped timeout cleanup."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import tempfile

root = Path(__file__).resolve().parent.parent
checks = {
    "riot-display-link-isolation-check": "DISPLAY_LINK_ISOLATION PASS",
    "riot-registry-compatibility-check": "REGISTRY_COMPAT",
    "riot-cfhost-cancel-check": "CFHOST_CANCEL_CHECK",
    "riot-launchservices-null-error-check": "LAUNCHSERVICES_NULL_ERROR",
    "m327-cgl-resolver-check": "GL_RESOLVER",
    "riot-mach-exception-state-check": "MACH_EXCEPTION_STATE",
    "riot-running-application-check": "RUNNING_APP",
    "riot-task-info-check": "TASK_INFO",
    "riot-appkit-pump-check": "APPKIT_PUMP",
    "riot-runloop-idle-check": "RUNLOOP_IDLE",
    "riot-event-coalescing-check": "EVENT_COALESCING",
    "riot-scroll-event-check": "SCROLL_",
    "riot-pthread-integrity-check": "PTHREAD_INTEGRITY",
    "riot-malloc-integrity-check": "MALLOC_INTEGRITY",
    "riot-rpc-signal-mask-check": "RPC_SIGNAL_MASK",
    "riot-workqueue-reuse-check": "WORKQUEUE_REUSE",
    "riot-kqueue-lifetime-check": "KQUEUE_",
    "riot-vnode-close-check": "VNODE_CLOSE",
    "riot-kqueue-timer-check": "KQUEUE_TIMER",
    "riot-kqueue-stack-check": "KQUEUE_SMALL_STACK",
    "riot-kqueue-dispatch2-check": "DISPATCH2",
    "riot-dispatch-fd-check": "DISPATCH_FD",
    "riot-runloop-wake-check": "RUNLOOP_WAKE",
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("check", choices=checks)
parser.add_argument("--library-path", default="@GUEST_HOME@/RuntimeLibraries")
parser.add_argument("--framework-path", default="@GUEST_HOME@/RuntimeFrameworks")
parser.add_argument("--timeout", type=int, default=30)
args = parser.parse_args()
if not 1 <= args.timeout <= 120:
    parser.error("timeout must be between 1 and 120 seconds")
binary = (root / "build" / args.check).resolve()
if not binary.is_file():
    parser.error("probe has not been built")

def owned_guests():
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.joinpath("exe").readlink().name != "mldr":
                continue
            if str(binary) not in entry.joinpath("maps").read_text():
                continue
            fields = entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            found[int(entry.name)] = fields[19]
        except OSError:
            pass
    return found

previous = owned_guests()
environment = dict(os.environ, DYLD_LIBRARY_PATH=args.library_path,
                   DYLD_FRAMEWORK_PATH=args.framework_path)
# A killed shell can leave a guest holding inherited stdout open. File capture
# avoids communicate() waiting forever on that guest's pipe after a timeout.
with tempfile.TemporaryFile() as output:
    process = subprocess.Popen(["darling", "shell", "/Volumes/SystemRoot" + str(binary)],
                               env=environment, stdout=output, stderr=output)
    timed_out = False
    try:
        result = process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=3)
        for pid, identity in owned_guests().items():
            if pid not in previous and owned_guests().get(pid) == identity:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        result = 124
    output.seek(0)
    observed = False
    for line in output.read().decode(errors="replace").splitlines():
        if line.startswith(checks[args.check]):
            observed = True
            print(line)
            if (args.check == "riot-running-application-check" and
                    line != "RUNNING_APP found=1 activated=1 missing=1"):
                result = 1
            if (args.check == "riot-cfhost-cancel-check" and
                    line != "CFHOST_CANCEL_CHECK cancel callback lifetime repeated pass=1"):
                result = 1
            if (args.check == "riot-mach-exception-state-check" and
                    line != "MACH_EXCEPTION_STATE state=1 identity=1 handled=2 server=1"):
                result = 1
    if result == 0 and not observed:
        print("NATIVE_CHECK missing_probe_output=1")
        result = 1
    print(f"NATIVE_CHECK name={args.check} timed_out={int(timed_out)} exit={result}")
raise SystemExit(result)
