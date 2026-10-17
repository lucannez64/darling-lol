# Testing on another PC

Run standalone probes and offline mocks; launch and play matches manually. Do not attach a debugger, query client APIs, inject diagnostic client/game hooks or automate input. Do not stop a running client/game to test a runtime change. Presentation probes should use their own windows with clients closed, since the native presentation fixtures use shared per-user target names.

## Patch and offline checks

After preparing the pinned source tree:

```sh
python3 scripts/prepare.py --check-only
python3 tools/riot-launcher-offline-check.py
python3 tools/riot-memory-installer-offline-check.py
python3 tools/riot-foundation-installer-offline-check.py
python3 tools/riot-llvm-version-offline-check.py
```

The LLVM version check compiles Iridium's actual dynamic loader against small stub libraries. It verifies that a matching major initializes, older/newer majors produce a version mismatch, and a missing library fails initialization. It does not start Darling. The native AIR translator (`python3 tools/build-riot-air-check.py --native`) selects both headers and the shared library from the same `llvm-config`; its build prints those paths so shader results can be attributed to the tested LLVM installation.

The patch check uses temporary base indexes and does not reset or modify the source checkout. The launcher check uses stub commands and never starts or contacts Riot or Darling. It checks installed-client and first-time-installer launches, strips inherited `DYLD_INSERT_LIBRARIES` even with the old VPX opt-in set, and preserves Darling runtime library/framework paths. The memory-installer check imports the installer and exercises its policy (default source is the prepared artifact; the artifact is never a destination of a different library) in temporary directories. The Foundation-installer check does the same for the overlay installer: it seeds a temporary framework from a stub SDK, replaces `Versions/C/Foundation`, keeps the standard symlinks, stages a backup, treats a re-run as current and refuses a non-Mach-O source. The installer handoff mock covers 28 scenarios, including HTTP 409, interrupted verification and preserving active downloads.

## NSURLSession lifecycle regression

After configuring and building the patched Darling source:

```sh
python3 tools/build-riot-urlsession-check.py
python3 tools/riot-urlsession-check.py
```

The runner starts a loopback-only HTTP fixture, reserves a second non-listening
port for the transport-error case, and creates a fresh private DPREFIX. It runs
only the independent probe, then shuts down that private prefix. It never
starts Riot/League, queries their APIs, sends game input or shuts down the
normal prefix. The fixture delays its response so session invalidation is
tested with a request still outstanding. Run native guest probes with clients
and the game closed; source-only CI does not run them.

The builder accepts `--build-dir` and `--sdk` for an existing configured tree.
The runner accepts `--frameworks` and `--libraries` host paths for installed
overlays, and keeps diagnostics in mode-0600 `build/urlsession-check.log`.

Expected checks: five successful HTTP GETs with exact response bytes/status;
connection refusal delivered as an error; repeated resume/cancel without
duplicate completion; finish waiting for task completion; and session
invalidation delivered once for both graceful finish and cancellation.
Expected output includes `URLSESSION_PASS` and `LIFECYCLE_PASS`. The original
runtime aborts on missing data-task initialization or session invalidation.
These checks do not establish full NSURLSession conformance or transport-level
interruption of an already-running request.

Host-only checks, which do not start Darling:

```sh
python3 tools/riot-cdn-dns-offline-check.py
python3 -m unittest discover -s tests -p 'test_hidden_riot_recovery.py'
python3 -m unittest discover -s tests -p 'test_desktop_run.py'
python3 -m unittest discover -s tests -p 'test_urlsession_runner.py'
```

The DNS check compiles the actual `dr_query` addition from `Libinfo.patch`
against stub resolver functions. It covers default/opt-out behavior, case
insensitivity, A queries, non-CDN names and misleading domain suffixes without
sending a DNS query. Recovery tests use fake `/proc` entries and mock every
compositor/shutdown command. The desktop test checks exit/argument forwarding,
append-only output and tightening an existing public log to mode 0600. The
runner test uses a fake `darling` executable to verify loopback fixtures and
private-prefix-only shutdown/cleanup.

## CoreText font-list and AppKit resize regressions

After preparing the source and configuring the runtime tools:

```sh
python3 build/runtime/tools/build-riot-coretext-runtime.py
python3 build/runtime/tools/build-riot-appkit-runtime.py
```

After installing and activating the runtime with both clients and the game closed, run the standalone probes (replace `me` and the checkout path):

```sh
darling shell env DYLD_FRAMEWORK_PATH=/Users/me/RuntimeFrameworks \
  /Volumes/SystemRoot/path/to/darling-lol/build/riot-coretext-font-list-check
darling shell env DYLD_FRAMEWORK_PATH=/Users/me/RuntimeFrameworks \
  /Volumes/SystemRoot/path/to/darling-lol/build/riot-window-layout-check
darling shell env DYLD_FRAMEWORK_PATH=/Users/me/RuntimeFrameworks \
  /Volumes/SystemRoot/path/to/darling-lol/build/riot-display-modes-check
```

`riot-display-modes-check` (built by `build-riot-display-modes-check.py`) expects `DISPLAY_MODES_CHECK pass=1`. It checks two things for the main display, display 0 and an unknown display:

- `CGDisplayCopyAllDisplayModes` never returns NULL. League passes the id from `[[window screen] deviceDescription]` (0 while the window is off every screen) straight to `CFArrayGetCount`, and NULL crashed it.
- `CGDisplayScreenSize` returns a nonzero size (`SCREEN_SIZE ... pass=1`). It returned 0×0 for any display with EDID. League divides by this size for its camera aspect ratio, so the view-projection matrix's x row became NaN and every 3D vertex was discarded. The scene then showed only its blue clear colour, and world clicks did nothing. This was found with a RenderDoc capture and vertex shader debugging.

Expected: `CORETEXT_FONT_LIST pass=1` with nonzero family/face counts (306/1124 on the development host), `VIEW_LAYER_RESIZE initial=1600x900 pass=1`, `NATIVE_RESIZE_ORDER size=1600x900 pass=1` `CONTENT_SIBLING_RESIZE ... pass=1` and `WINDOW_LAYOUT_CHECK ... pass=1`.

The font probe reproduces Chromium's installed-family enumeration, mandatory family matching and localized-name lookup with a null language output. It checks ownership of the single-match result and that a nonexistent mandatory family produces no substitute. The resize probe checks backing-layer bounds immediately after frame/bounds changes, including a scaled view and a child layer, without relying on input or coordinate conversion to refresh them. It also verifies that native parent size is current during child layout, and that adding an overlay sibling after the content view does not redirect subsequent content resizing into that overlay. The old sibling selection left content at 1600×900 while stretching the 30×20 overlay to 1280×720; the corrected layout resizes the content and preserves the overlay. Before the fixes, these checks report a stale 1280×720 layer and native parent after a 1600×900 resize. The `FRAME_SIZE_HOOK` check uses a subclass whose `setFrameSize:` publishes its viewport size. It checks growth and shrinkage through parent autoresizing, direct `setFrame:`, and direct `setFrameSize:`. Before component-setter dispatch was corrected only the final direct size setter reached the subclass (one callback); afterward all four updates do, with current geometry visible inside the callback. No probe launches or contacts Riot/League. The user confirmed that the language dropdown and League window resizing work after the final X11 correction (2026-10-15). The practice-tool crash remains unresolved.

### Presentation publisher handoff

With clients closed, run `build/riot-client-resize-check --handoff` through Darling with the normal runtime overrides. This creates an owned window and two fixture frame slots. The live software owner first supplies a 1280×720 main frame, then switches to a 262×131 popup; another slot supplies the resized main frame without the software flag. The test samples its own window beyond the original frame extent and checks generation colors across growth and shrinkage. The old selector reports black at the 1600×900 sample; the correction passes all four sizes. Repeat with `RCLOL_XSHM=0` and with `DARLING_XSHM_LIBRARY` pointing to the normal transport to cover both paths. The fixtures remove their own frame slots on exit; they do not read client pixels or send input.

### Pixels after a window resize

`python3 build/runtime/tools/build-riot-client-resize-check.py` builds an independent AppKit window and IOSurface publisher. With clients closed and a display at least 1600×900, run it with the same framework/library overrides and `DARLING_XSHM_LIBRARY=/Users/me/libDarlingXShm.dylib`, `DARLING_CA_PRESENT_AT_COMMIT=1`, `DARLING_CA_SKIP_LOCAL_IOSURFACE_RENDER=1`. The default mode publishes software pixels. For the GPU path add `--gpu` and set `RCLOL_GPU_DIRECT_PRESENT=1 RCLOL_GPU_DIRECT_ASYNC=1` in the guest environment.

The probe cycles through 1280×720, 1600×900, 1024×576 and 1280×720. Each stage changes the frame color and samples its own X11 window near the bottom-right corner, beyond the original frame's extent when growing. It waits up to 2.5 seconds for presentation and prints `RESIZE_PIXEL ... pass=1` for every stage. This verifies presented pixels rather than only view properties. Success does not reproduce League's own resize callbacks; the reported League UI failure remains open, with the opt-in [geometry trace](TROUBLESHOOTING.md#language-dropdown-closes-riot-or-client-resize-leaves-a-clipped-ui) needed to locate the remaining mismatch.

## Legacy operating-system version query

```sh
python3 build/runtime/tools/build-riot-carboncore-runtime.py
```

After installing and activating CarbonCore, run the independent `build/riot-gestalt-version-check` through Darling with the normal framework/library overrides. Expected on the current kernel: `GESTALT_VERSION product=14.7 version=14.7.0 packed=0x1470 architecture=10 pass=1`. It compares the separate version selectors against `kern.osproductversion`, checks packed BCD, confirms architecture remains a separate result, and covers null outputs and unknown selectors. The previous implementation returned 10 for every version component and the packed selector. The probe does not launch or contact Riot. Passing it establishes the API correction, not Vanguard compatibility.

## Standalone guest probes

Build and install the base/runtime first. Native probes are built by the incremental runtime scripts. With the default guest username, run a probe like this (replace `me` and the host checkout path):

```sh
darling shell env \
  DYLD_FRAMEWORK_PATH=/Users/me/RuntimeFrameworks \
  DYLD_LIBRARY_PATH=/Users/me/RuntimeLibraries \
  /Volumes/SystemRoot/path/to/darling-lol/build/riot-appkit-pump-check
```

Build additional probes when needed, for example:

```sh
python3 build/runtime/tools/build-riot-metal-concurrency-check.py
```

Useful probes (build tokenized Python helpers from `build/runtime/tools/`):

- `riot-launchservices-null-error-check`: nullable LaunchServices error outputs must not crash, and a supplied empty output must receive an error. Build with `build-riot-launchservices-check.py`; expected `nullable_outputs=1 error_populated=1 missing_handlers=1`.
- `riot-appkit-pump-check`: every queued callback must arrive in its own event poll; expected `events=200 callbacks=200 late_polls=0`.
- `riot-event-coalescing-check`: preserve key events and latest mouse motion.
- `m327-cgl-resolver-check`: first calls through dynamically looked-up GL functions retain floating-point arguments; expected `first_sampler_linear=1 first_color_arguments=1 errors=0,0`. Build with `build-m58f327-opengl.py` and run with `run-riot-native-check.py m327-cgl-resolver-check`.
- `m327-cgl-direct-check`: with `RCLOL_GPU_DIRECT_OTHER_WINDOW_TEST=1` and an owned 337x339 native target, interleave 338x339 fallback surfaces; expected `active_presenter_preserved=1 fallback_frames=357`. This needs the native presentation fixture; it is not a `run-riot-native-check.py` choice.
- `riot-metal-presentation-check`: Vulkan clear reaches the GL texture, including late window/context attachment. Set `RIOT_PRESENTATION_NESTED=1` to place the Metal layer beneath two ordinary layers and verify that its drawables are still presented and recycled past the three-drawable pool limit.
- `riot-metal-concurrency-check`: submission ordering, resource lifetime and concurrent recording.

`RIOT_PRESENTATION_STRESS=1` makes the presentation probe run 120 frames. With the game and clients closed, force async encoding for this standalone probe using the guest flag `/tmp/metal-async-encode`, then remove it. The tested old path leaked 120 synchronization FDs over 120 frames; the fixed path leaked zero. Inspect the probe's host `/proc/PID/fd` for `anon_inode:syncobj_file` if reproducing that test.

## Host receive queue regression

The receive-buffer regression runs entirely on independent host sockets:

```sh
c++ -std=c++17 -O2 -pthread \
  -I build/darling-src/src/external/darlingserver/internal-include \
  tools/riot-host-receive-check.cpp \
  build/darling-src/src/external/darlingserver/src/message.cpp \
  -Wl,--wrap=malloc -o build/riot-host-receive-check
build/riot-host-receive-check
python3 tools/riot-launcher-offline-check.py
```

Expected: `warm_empty_allocations=0 single_packet_allocations=2`, correct credentials, descriptor ownership/CLOEXEC, sender addresses, multiple batches, truncation flags, callback reentrancy and exception cleanup. The old receive implementation makes 32 allocations for a warmed empty receive and 64 for a single-packet receive in the same fixture. These are allocation counts, not an FPS benchmark. The launcher check uses stub commands and never starts or contacts Riot or Darling. It checks installed-client and first-time-installer launches, strips inherited `DYLD_INSERT_LIBRARIES` even with the old VPX opt-in set, and preserves Darling runtime library/framework paths.

The same source passed AddressSanitizer/UndefinedBehaviorSanitizer and leak detection. Allocation counts depend on this fixture/compiler/library; preserve the no-wasted-slot property rather than interpreting them as game performance. See [setup](SETUP.md#updating-an-existing-runtime) for server build/install/activation.

## System sleep/wake notifications

The power monitor uses `IORegisterForSystemPower`, an `IOPMrootDomain` user
client and standard IOKit interest messages. `iokitd` listens to logind's
`PrepareForSleep` signals on the host system bus. A delay inhibitor stays open
until every notified application acknowledges sleep or the deadline expires.
Wake notifications arrive in `SystemWillPowerOn`, `SystemHasPoweredOn` order.
Registration also works without logind, with no host events in that case.

Build the runtime and standalone probes:

```sh
cmake --build build/darling-src/build --target darlingserver iokitd --parallel 3
python3 tools/build-riot-power-notify-check.py
cc -Wall -Wextra -Werror -pthread \
  -I build/darling-src/src/external/iokitd/src \
  tools/riot-logind-sleep-check.c \
  build/darling-src/src/external/iokitd/src/LogindSleep.c \
  build/darling-src/src/external/iokitd/src/HostPowerInhibit.c \
  $(pkg-config --cflags --libs libsystemd) -o build/riot-logind-sleep-check
build/riot-logind-sleep-check
```

The host check uses a private D-Bus daemon and fake logind. It covers absent
services, delay-lock ownership, acknowledgement deadlines, stale
acknowledgements, repeated sleep/wake, a logind restart, a full system-bus
restart and a sleep signal during startup timeout discovery. Expected:
`LOGIND_SLEEP PASS checks=73 failures=0`. It also runs with
`-fsanitize=address,undefined`.

`IOServiceOpen` sends an empty out-of-line descriptor. Older Darling servers
reject that descriptor with `0x400` before the request reaches `iokitd`.
Install the rebuilt server with Darling stopped, using the server installer in
[setup](SETUP.md#updating-an-existing-runtime), before running:

```sh
python3 tools/riot-power-integration-check.py
```

The integration check creates its own prefix, copies the built `iokitd` into
it and connects it exclusively to fake logind. It never suspends the host,
starts Riot or uses the normal Darling prefix. Expected: `POWER_INTEGRATION
PASS`, covering a full notification queue, two clients, dead destinations,
failed-registration cleanup, the acknowledgement timeout, deregistration during
sleep and sleep with no clients. Fixture logs stay under
`build/power-integration-*`; Darling's overlay work directory may require root
to remove.

The integration check passed against the installed rebuilt server without an
interposer. Its bootstrap run can also acquire an inhibitor, so readiness
requires a new lock from the persistent launcher plus confirmation that its
shell has started. Lock numbering is relative to that launcher, rather than
assuming that the first recorded lock belongs to the test clients.

## Power-save assertions

Chromium uses the legacy names `NoIdleSleepAssertion` and
`NoDisplaySleepAssertion` with `IOPMAssertionCreateWithName`. These share
backends with the modern `PreventUserIdleSystemSleep` and
`PreventUserIdleDisplaySleep` names. System-idle prevention uses a logind
`idle/block` inhibitor; display prevention additionally holds a desktop
`org.freedesktop.ScreenSaver.Inhibit` cookie. The existing X11 DPMS backend is
a fallback when desktop inhibition is unavailable, and restores the prior
DPMS enabled state. These assertions do not take a logind `sleep/block` lock.

Create, retain, release, level On/Off and process-exit cleanup are supported.
The last enabled assertion releases the backend; retaining an assertion ID
does not add another backend hold. Unsupported types and nonzero assertion
timeouts return `kIOReturnUnsupported`, rather than silently creating an
indefinite inhibitor. Host-backend failures are returned to the caller.

After the builds above:

```sh
python3 tools/build-riot-power-assertion-check.py
python3 tools/riot-power-assertion-integration-check.py
```

Expected: `POWER_ASSERTION_INTEGRATION PASS`. The fixture uses private fake
logind and ScreenSaver services for both D-Bus addresses and a separate Darling
prefix. It checks legacy/modern aliases, multiple clients, retain/final release,
level changes, failed requests and cleanup of assertions left held by an exiting
process. It never contacts the real desktop's inhibitor services or launches
Riot. The native check above additionally verifies backend-denial and missing-bus
cleanup, and passes under AddressSanitizer/UndefinedBehaviorSanitizer.

The assertion integration fails against the previously installed `iokitd` with
the reported `0xe00002c9` create error, and passes with the rebuilt service.
Only `iokitd` needs reinstalling for this follow-up, with Darling stopped:

```sh
sudo install -m755 build/darling-src/build/src/external/iokitd/iokitd \
  /usr/local/libexec/darling/usr/sbin/iokitd
```

## Host URL hand-off regression

LaunchServices' D-Bus hand-off code (`LSHostOpenBus.c`) builds natively against the host's libsystemd. The check starts a private `dbus-daemon` with no service directories and a fake portal/systemd service, so nothing real is activated and no browser opens:

```sh
cc -std=gnu11 -O2 -Wall -Wextra \
  -I build/darling-src/src/frameworks/CoreServices/src/LaunchServices \
  tools/riot-host-open-url-check.c \
  build/darling-src/src/frameworks/CoreServices/src/LaunchServices/LSHostOpenBus.c \
  $(pkg-config --cflags --libs libsystemd) -o build/riot-host-open-url-check
build/riot-host-open-url-check
```

Expected: `HOST_OPEN_URL PASS checks=79 failures=0`. It covers: the portal receives the exact URL; with no portal, or a portal error, `StartTransientUnit` runs `[xdg-open, url]`; a portal timeout is not retried; rejected schemes, control characters and overlong URLs never connect to the bus; and a missing bus or libsystemd returns an error. It also covers the `/run/user/<uid>/bus` fallback when `DBUS_SESSION_BUS_ADDRESS` is unset, against a temporary directory. It needs `dbus-daemon` and the libsystemd headers.

## Host reply path regression

The direct-reply checks run entirely on independent host sockets and exercise the real `src/message.cpp` (`MessageQueue::pushAndSend`, `sendMany`, `empty`):

```sh
c++ -std=c++17 -O2 -pthread \
  -I build/darling-src/src/external/darlingserver/internal-include \
  tools/riot-host-reply-check.cpp \
  build/darling-src/src/external/darlingserver/src/message.cpp \
  -o build/riot-host-reply-check
build/riot-host-reply-check
```

Expected: `HOST_REPLY pass=1 direct_send=ok eagain_retry=ok fifo=ok concurrency=ok notify=ok descriptors=ok dead_peer=ok`. It covers: a direct send that drains the queue does not invoke the wakeup callback while a queued message does; EAGAIN leaves the queue intact and the retry path delivers every message in order; four concurrent pushers plus a draining "main loop" preserve per-pusher order without loss or duplication; descriptors are delivered exactly once, closed on the sender side right after the send and released on the receiver side; and a dead peer (ENOENT after unlink, ECONNREFUSED after close) drops only its own message without wedging the queue or leaking descriptors. Compile the same way with `-fsanitize=address,undefined` and `-fsanitize=thread`; the ThreadSanitizer run is the important one.

A same-process microbenchmark compares the direct path with the old `push` + eventfd + main-loop `sendMany` hand-off:

```sh
c++ -std=c++17 -O2 -pthread \
  -I build/darling-src/src/external/darlingserver/internal-include \
  tools/riot-host-reply-bench.cpp \
  build/darling-src/src/external/darlingserver/src/message.cpp \
  -o build/riot-host-reply-bench
build/riot-host-reply-bench 5000
```

Expected: a `HOST_REPLY_BENCH same_process=1 ...` line; the direct path is a few microseconds faster on average. Both endpoints are threads of one process, so this isolates the hand-off, not real guest scheduling.

## Memory reclamation regression

The memory-reclamation check links the actual Darling `sys_madvise` source to host syscall hooks and a `read_string` stub that selects the host's transparent-huge-page policy, and uses only its own 32 MiB anonymous allocation:

```sh
python3 tools/build-riot-host-madvise-check.py
```

The build script runs the check once per policy. Expected:

```
HOST_MADVISE pass=1 mode=always madvise_calls=6 sub_page_syscalls=0 reclaimable_KiB=32768 reuse_reclaimable_KiB=0 anon_huge_KiB=32768/32768 contents=preserved
HOST_MADVISE pass=1 mode=never ... reclaimable_KiB=32768 reuse_reclaimable_KiB=0 ...
HOST_MADVISE pass=1 mode=madvise ... reclaimable_KiB=32768 reuse_reclaimable_KiB=0 ...
```

`mode=always` covers the deployment host: sub-huge-page hints reach no `madvise` syscall and do not reduce the mapping's `AnonHugePages` (read from `/proc/self/smaps`), the 2 MiB-aligned region is marked reclaimable, an unaligned large range is rounded inward to the contained whole huge pages, and reuse cancels reclamation with contents preserved. `mode=never`/`mode=madvise` prove the full range is still forwarded when the host does not give this layer's mappings huge pages. The old implementation returned success without marking any pages.

Darwin `MADV_FREE` and `MADV_FREE_REUSABLE` map to Linux `MADV_FREE` for whole 2 MiB huge pages only; Darwin `MADV_FREE_REUSE` uses `MADV_POPULATE_WRITE` over the full requested range to cancel lazy reclamation while preserving surviving contents. The reuse operation requires Linux 5.14 or newer; an unsupported or invalid operation returns the translated kernel error rather than reporting false success. Reclamation is lazy and does not promise an immediate RSS decrease when RAM is available.

For the actual guest ABI, `tools/build-riot-madvise-check.py` builds an independent executable. After a full restart has activated the installed library, run it as follows; replace `me` and the checkout path:

```sh
python3 tools/build-riot-madvise-check.py
darling shell env \
  DYLD_LIBRARY_PATH=/Users/me/RuntimeLibraries \
  /Volumes/SystemRoot/path/to/darling-lol/build/logs/madvise-fix/riot-madvise-check
```

Expected: `DARLING_MADVISE pass=1 reclaimable_KiB=4096 reuse_reclaimable_KiB=0 contents=preserved`. The probe marks a 2 MiB-aligned 4 MiB region inside a larger mapping, because only whole huge pages are forwarded; it uses only its own allocation and Linux memory counters, and never contacts a client/game.

## Host vDSO clock regression

The clock check compiles the actual `emulation/src/linux_premigration/ext/clock_gettime.c` against a stub ELF bridge backed by the host's `dlopen`/`dlsym`:

```sh
python3 tools/build-riot-host-vdso-clock-check.py
build/logs/vdso-clock/host-vdso-clock-check default
build/logs/vdso-clock/host-vdso-clock-check no-symbol
build/logs/vdso-clock/host-vdso-clock-check race
```

Expected (costs vary with machine load):

```
HOST_VDSO_CLOCK pass=1 mode=default syscall_ns=528 vdso_ns=24 dlopen_calls=1 dlsym_calls=1 million_reads=1 threads=8 fork=preserved
HOST_VDSO_CLOCK pass=1 mode=no-symbol ... (syscall fallback, correct values)
HOST_VDSO_CLOCK pass=1 mode=race ... dlopen_calls=1 ...
```

`default` reads the clock before the bridge exists (syscall fallback, not cached), resolves the vDSO entry once the bridge appears, checks a million reads per clock id against the raw syscall and across eight threads, and checks that a forked child inherits the resolved pointer. `no-symbol` proves the fallback when the bridge has no entry. `race` starts eight threads before resolution and requires that only one resolution ran.

Install the tested library with `python3 tools/install-riot-memory-runtime.py`. Its default source is the optimized relink `build/riot-libsystem_kernel.dylib`; if it is missing, build it first with `python3 build/runtime/tools/build-riot-kqueue-runtime.py`. The installer uses user-owned `build/memory-runtime-backups` instead of the shared `build/backups` directory, which an earlier sudo server install may have made root-owned. It atomically replaces the two per-user kernel-library copies, without stopping any process or changing the system SDK. The prepared artifact is never a destination of a different library: a run with an explicit `--binary` replaces the two prefix copies and leaves `build/riot-libsystem_kernel.dylib` untouched (the check `tools/riot-memory-installer-offline-check.py` covers this). The active Darling overlay can retain the old file even for new guest processes after an update to its backing directory; this was verified by differing host versus namespace checksums. After exiting the game and both clients manually, run `darling shutdown`, then `sh scripts/run.sh` to start a new namespace. Do not restart Darling during a match. Verify reclamation and client memory use after that restart.

## CPU topology regression

The hw.* / machdep.cpu.* handlers are checked on the host against the host's real topology. The check links the actual `sysctl_hw.c` and `sysctl_unspec.c` to host shims (real `/sys` reads, forwarded syscalls, sentinel Mach replies) and drives each handler directly plus `sysctlbyname("hw.*")` through the real name-to-oid lookup:

```sh
python3 tools/build-riot-host-sysctl-cpu-check.py
```

Expected on this 6-core/12-thread host:

```
HOST_SYSCTL_CPU pass=1 mode=normal ncpu=12 active=12 logicalcpu=12 logicalcpu_max=12 physicalcpu=6 physicalcpu_max=6 model=MacBookPro16,1
HOST_SYSCTL_CPU pass=1 mode=affinity ncpu=12 active=4 logicalcpu=4 logicalcpu_max=12 physicalcpu=4 physicalcpu_max=6 model=MacBookPro16,1
HOST_SYSCTL_CPU pass=1 mode=no-detect ncpu=8 active=8 logicalcpu=8 logicalcpu_max=8 physicalcpu=8 physicalcpu_max=8 model=MacBookPro16,1
HOST_SYSCTL_CPU pass=1 mode=optout ncpu=8 active=8 logicalcpu=8 logicalcpu_max=8 physicalcpu=8 physicalcpu_max=8 model=MacBookPro16,1
```

`normal` is the deployment case: hw.ncpu/hw.logicalcpu_max/hw.physicalcpu_max are the host's online counts, hw.activecpu/hw.availcpu/hw.logicalcpu/hw.physicalcpu follow this process's affinity mask. `affinity` restricts the check to four CPUs first, so avail/active fall to 4 while the max values stay at 12 (and 6). `no-detect` fails every `/sys` CPU read and `sched_getaffinity`, `optout` sets the flag file (a check-local path compiled into that binary); both must fall back to the old fixed 8s. The emulated `hw.model` must stay `MacBookPro16,1` in every mode. The production opt-out flag is `/tmp/rclol-fixed-cpu-count`; the host check overrides only the path, never touches the real file.

The commpage fields the same change publishes (`_COMM_PAGE_NCPUS`, `_COMM_PAGE_ACTIVE_CPUS`, `_COMM_PAGE_PHYSICAL_CPUS`, `_COMM_PAGE_LOGICAL_CPUS` and the `kNumCPUs` capability bits) come from mldr. `build/riot-mldr` is the rebuilt loader; activating it means replacing the root-owned SDK copy (`/usr/local/libexec/darling/usr/libexec/darling/mldr`) after exiting clients and shutting down Darling, as in the setup notes.

## Optimized library overlays and string primitives

The string primitives the guest resolves to are checked on the host against two copies of the same libplatform sources, a byte-loop reference, and an unmapped page on each side of every buffer:

```sh
python3 tools/build-riot-platform-memory-check.py
```

Expected (five runs: the generic path, the vector path, the vector plus REP path, then the same check built with AddressSanitizer and UndefinedBehaviorSanitizer):

```
PLATFORM_MEMORY_CHECK pass=1 capabilities=0 cases=194680 mismatches=0 page_faults=0 variants=2
PLATFORM_MEMORY_CHECK pass=1 capabilities=0x20000000 cases=194680 mismatches=0 page_faults=0 variants=2
PLATFORM_MEMORY_CHECK pass=1 capabilities=0x28000000 cases=194680 mismatches=0 page_faults=0 variants=2
PLATFORM_MEMORY_CHECK pass=1 capabilities=0 cases=194680 mismatches=0 page_faults=0 variants=2
PLATFORM_MEMORY_CHECK pass=1 capabilities=0x28000000 cases=194680 mismatches=0 page_faults=0 variants=2
```

`current` is the code and flags the installed `libsystem_platform.dylib` was built from and `new` is what `tools/build-riot-platform-strings.py` produces; every case runs against both and against the reference, for every size up to 512 with a subset of the 64x64 alignments, for random sizes and alignments, for both overlap directions, and for buffers that only just fit the guarded region. The same script builds `build/riot-platform-memory-bench`, which measures the same functions against the host's glibc for every size from 1 to 256 and at 512 B to 1 MiB (`BENCH size=... op=... current_ns=... new_ns=... glibc_ns=...`). That is a throughput measurement of the primitives, not an FPS measurement.

Which file the guest actually loads for a library, and how that file was compiled, is listed by:

```sh
python3 tools/riot_overlay_audit.py
```

An overlay is compared with the library it would replace by:

```sh
python3 tools/riot_overlay_verify.py build/riot-libsystem_c.dylib /usr/local/libexec/darling/usr/lib/system/libsystem_c.dylib
```

Expected `OVERLAY_VERIFY pass=1` plus the export counts and the stack-reference share before and after. The check requires the same install name and versions, the same dependent libraries, re-exports and segment protections, the same exported symbols (a weak definition the optimized build drops is reported as a note, because the ABI makes a weak definition optional), no new undefined symbol, and an improved optimization estimate. It never starts a guest process.

The batch installer's policy and rollback are exercised in temporary directories:

```sh
python3 tools/riot-libs-installer-offline-check.py
```

Expected: `LIBS_INSTALLER_OFFLINE pass=1 overlays=5 backup_before_replace=1 rollback_restores=1 rollback_removes=1 idempotent=1 rejects_non_macho=1`. It imports the installer, never executes it, and never touches the real prefix.

## Local semaphore model

```sh
sh tools/riot-lsem-model/run.sh
sh tools/riot-lsem-model/sensitivity-check.sh
```

`extract-lsem.py` copies the shipped local-semaphore table and trap functions verbatim out of the patched `mach_traps.c`; it refuses to run if the generation-checked token compare-exchange or the negative destroy marker is missing. The model compiles that code with shims for the raw syscalls and server RPCs. It runs create/destroy churn with name reuse on 8 threads, waiter/signaler pairs, timed waits, `signal_all`, destroy while waiting, and the XNU ordering of `wait_signal`. Expected: `LSEM_MODEL checks=686 failed=0 spurious_success=0 lost_wakeup=0 steal=0 hang=0`. The sensitivity check re-breaks the protocol in a copy and must report failures: positive tombstone (`failed=5 spurious_success=1`), missing generation bump (`failed=2 spurious_success=400`). Outputs go to `build/riot-lsem-model`.

## Metal bind model

```sh
sh tools/riot-metal-bind-model/run.sh
```

The bind-tracking helpers are extracted verbatim from `MTLRenderCommandEncoder.mm`. The model drives two encoder states, one receiving every call and one only the forwarded calls, and requires them to be equal after every call. That covers directed scenarios, among them a freed wrapper whose address is reused by a different resource and depth-stencil retention, plus 119,401 random calls. Expected: `randomized calls=119401 forwarded=110294 dropped=9107 (7.6%)`, `BIND_DEDUP_MODEL failures=0` and `BLIT_CAST_MODEL failures=0`.

## Indium hand-off tests

```sh
sh tools/run-deferred-handoff-tests.sh
```

Both tests compile the real Indium private headers with the host compiler. `test-deferred-handoff` checks four things. The submit thread runs tasks in post order. The present-dispatch token returns while a later task is still queued, and `drain()` waits for that task. The wait counters write nothing without `/tmp/metal-debug-waits` (checked in a forked child). With the flag, they write one `waits enabled` line and then count only waits that actually blocked. `test-deferred-encoder-model` checks the deferred blit and render hand-off: order, resource lifetime and the drop after commit. The test creates the host `/tmp/metal-debug-waits` for its run and removes it afterwards. In the runtime these mechanisms are opt-in (see [setup](SETUP.md#metal-and-indium-overlays)). Expected: `all host deferred-hand-off tests passed`.

## Manual game observation

Play matches manually through Riot. Check sound, keyboard/mouse buttons, fullscreen confinement, camera motion and returning to the client. Inspector, input-automation, client-API and client-binary-patching helpers are not part of this project. Normal launches enable none of these helpers and do not force unlimited UI frames. Automatic installer handoff and host external-link forwarding are consequently unavailable.

Observe host CPU/thread scheduling, GPU counters, temperatures and memory pressure without controlling the client/game. Record manual FPS readings with timestamps and comparable graphics settings. Do not interpret a socket wait as avoidable RPC latency without distinguishing intentional synchronization. Server-only CPU sampling identifies costs but does not measure per-frame causality.

Compare similar scene, ability and camera activity with the same power settings (and correct charger on a laptop). Do not report a paused, dead, unfocused or empty scene as a fight benchmark. If an allowed host measurement supplies frame times, report p99 in milliseconds; its reciprocal is percentile-equivalent FPS, not the mean of the slowest 1% of frames. Manual FPS readings alone do not establish p99 frame times.

The CPU symbolizer offers `--leaf-only` because the host DWARF unwinder can drop guest Mach-O stacks. Successfully unwound samples alone are not a representative profile. Local API lockfiles, account data and raw logs must stay outside version control.

## CFHost cancellation regression

After configuring and building CFNetwork, build and run the isolated probe:

```sh
python3 build/runtime/tools/build-riot-cfhost-cancel-check.py
python3 build/runtime/tools/run-riot-native-check.py riot-cfhost-cancel-check
```

Run against the installed CFNetwork override. Expect
`CFHOST_CANCEL_CHECK cancel callback lifetime repeated pass=1`. The unpatched
library traps in `CFRetain(NULL)`; the runner requires the exact success line because
Darling can return status zero even when the guest traps. The probe uses localhost
and exercises 100 cancellations with both retained and early-released caller
references, without launching or inspecting Riot. A failure prints
`CFHOST_CANCEL_CHECK fail stage=...`; `unexpected-callback` means a completed
lookup arrived instead of the cancellation.

## Primary-monitor geometry regression

After preparing the sources and configuring/building Darling, run:

```sh
python3 scripts/prepare.py --check-only
python3 tools/check-x11-display-geometry.py
cc -std=c11 -Wall -Wextra -Werror tools/test-x11-relative-mouse.c -o build/test-x11-relative-mouse
build/test-x11-relative-mouse
```

The native probe requires the existing per-user AppKit and CoreGraphics overlays
and a running X11/Xwayland display. It rebuilds the changed X11 and CoreGraphics
objects and tests them in a temporary framework overlay, without installing them.
Seven `DISPLAY_GEOMETRY ...=pass` lines are required; full output is saved in
`build/pr12-display-check.log`.

It checks the live primary screen's origin, synthetic single-monitor and offset
layouts (including vertically arranged screens and negative Cocoa coordinates),
unmapped window creation/resizing, display IDs after primary-first ordering,
CoreGraphics bounds and spatial queries, absolute and relative warp translation,
relative deltas, both RandR cache-invalidation events, and the root-screen fallback.
Warps are intercepted: the probe does not move the desktop pointer or send game
input. Synthetic layouts exercise coordinate conversion and display API behavior;
they do not simulate physical monitor hotplug or validate League rendering.
The probe also checks that managed borderless windows covering a whole monitor
request EWMH fullscreen, including on secondary monitors, and clear the property
when resized before mapping. Titled windows, unmanaged popups and windows sized
to the panel-reduced work area are excluded. Mapped-window transitions and panel
stacking require manual verification under KDE Plasma/Xwayland. Also verify game
launch, window placement, click alignment and edge panning before rollout.

After installing the tested X11 and CoreGraphics binaries locally, the user
reported that everything was working (2026-10-16). Fullscreen policy was unchanged.
