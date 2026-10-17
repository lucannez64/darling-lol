# Troubleshooting

Use the [setup instructions](SETUP.md) from the repository root as your ordinary user. Exit the game and both clients manually before a full installation or Darling restart. Incremental server/memory installers can replace files without interrupting processes, but their changes need a later full restart to activate.

## Memory installer: permission denied

An earlier sudo server installation can create root-owned `build/backups`. The old memory installer tried to create `build/backups/memory-runtime` there and failed at `backup_folder.mkdir(...)`. The corrected default is a separate user-owned `build/memory-runtime-backups` directory:

```sh
python3 tools/install-riot-memory-runtime.py
```

For an explicitly chosen backup location:

```sh
python3 tools/install-riot-memory-runtime.py --backup-dir "$HOME/darling-memory-backups"
```

If the destination library itself is root-owned, inspect the named path and its ownership before retrying with sudo. Do not run all setup/build steps as root or recursively change ownership of the system SDK. Backups and binaries are ignored by Git and must stay local.

## Updated library or server still behaves like the old version

Atomic replacement preserves active process mappings. Darling also uses an overlay filesystem; updating its backing prefix from the host can leave an old file visible inside the active namespace. A fresh standalone guest check continued to report the old memory behavior despite the host path having the new checksum. Reopening only Riot does not replace this namespace.

After exiting the game and both clients manually:

```sh
darling shutdown
sh scripts/run.sh
```

Do not run these commands during a match. Use the independent reclamation probe in [testing](TESTING.md) to verify the new guest behavior after activation. An installer result with `installed: true` and `restart_performed: false` confirms the disk update, not activation.

## Around 5 GB of RAM with just the clients open

The observed proportional resident footprint was roughly 4.3 GiB across Riot, League and Darling, including 50 crash-handler processes. Roughly 3 GiB came from private anonymous mappings/native heaps; graphics mappings accounted for about 238 MiB. Large virtual address reservations are not equivalent to resident RAM, and summed RSS double-counts shared pages. Use proportional set size (PSS), swap and host memory pressure to assess the actual footprint.

The old kernel library returned success for Darwin allocator free/reuse hints without making any pages reclaimable. The replacement maps free/reusable hints to Linux `MADV_FREE`, and reuse to `MADV_POPULATE_WRITE`. The standalone 4 MiB buffer becomes reclaimable and returns to normal on reuse while preserving surviving data. This fixes a demonstrated runtime behavior, but does not explain all client allocations or prove a specific RAM reduction. Linux may retain lazily reclaimable pages in RSS while RAM is available. A five-minute idle observation did not show rapid client memory growth.

Build/install the kernel library using [the incremental update steps](SETUP.md#updating-an-existing-runtime), then perform the manual restart when finished playing. Linux 5.14+ is required. Do not kill client helpers as an attempted memory optimization during play.

## Installer exits, cannot write temporary files, or Install appears idle

Use `sh scripts/installer.sh euw` (or your supported region). The first-time launcher must omit the installed client's `--app-root` and pass `--skip-to-install`; the normal wrapper now does this. It also selects `league_of_legends` so the product installation UI appears after login.

The installer window closing on its own, followed by `subprocess exit`, is usually a successful install: Riot's installer puts the client in `/Users/Shared/Riot Games/Riot Client.app` and exits, and its LaunchServices hand-off to that client does not start it under Darling. `scripts/installer.sh` now launches the installed client itself, and the runner links it to `/Applications/Riot Client.app` when that link is missing. If the client was not installed, the wrapper prints the installer's log directory; check the end of the newest `Launcher` log there.

If logs report guest `/private/tmp` permission errors, the default prefix needs its own temporary directory. The corrected full runtime installer creates it. To repair just this default-prefix directory as your ordinary user:

```sh
mkdir -p "$HOME/.darling/private/tmp"
chmod 1777 "$HOME/.darling/private/tmp"
```

The installer fetcher no longer treats a missing archive `Info.plist` or the informational `unzip | head` listing as a fatal download failure. Download and extraction errors still fail. The wrapper invokes this fetcher with Bash, since its `pipefail` setting is unsupported by some `/bin/sh` implementations.

There is also a separate known compatibility gap: native LaunchAgent watch registration can return HTTP 409 after the League install path is accepted, leaving the download idle. A historical inspector hook handles this and interrupted `pending_verify` states in 28 mock cases, but that hook is disabled in normal launches and excluded from the manual workflow. There is no verified runtime-only replacement for that handoff in this snapshot. Do not assume the native permission and launcher fixes resolve every idle Install case, or enable debugger/API automation to work around it.

## Riot exits and League reports that it lost its connection

Check the launcher stderr or `build/desktop-launch.log` when launched through
the generated application-menu entry. The following Objective-C exceptions
were observed immediately before RiotClientServices aborted:

```
-[NSURLSessionDataTask initWithRequest:completionHandler:]: unrecognized selector
-[NSURLSession finishTasksAndInvalidate]: unrecognized selector
```

League then reports localhost connection refusal and loses its external-client
authorization. Restarting temporarily clears the broken session, but does not
implement the missing methods. The CFNetwork patch now supplies a minimal
completion-handler data-task implementation, uses the existing NSURLConnection
transport on a worker, and tracks outstanding tasks while a session finishes
or cancels. Session factories create the concrete CFNetwork session so its
delegate-queue storage is not confused with the public class's synthesized
properties. The URL convenience method also preserves GET rather than forcing POST.

Rebuild and install the base and paired runtime with both clients and the game
closed. Run the standalone NSURLSession regression in [testing](TESTING.md#nsurlsession-lifecycle-regression).
This is partial NSURLSession compatibility: in-flight cancellation reports the
cancellation once but does not interrupt the synchronous transport already in
progress. Completion handlers use a global worker queue rather than the
session's delegate queue. Delegate-only data tasks, suspension, background
sessions, downloads and uploads are not established by these tests.

### Client remains running without a window

New launches can exit with `Client already running, exiting` when a stale Riot
session has no visible window. After exiting the game and clients, `darling
shutdown` followed by `scripts/run.sh` recovers this case without deleting game
files. For a dedicated Riot installation in the default prefix, Hyprland users
can opt in to automatic recovery:

```sh
RCLOL_RECOVER_HIDDEN_CLIENT=1 ./scripts/run.sh
```

The helper requires an owned Riot process in the default prefix and a successful
Hyprland window query. It preserves a running game, any visible Riot/League
window, foreign-owned processes and custom prefixes, and takes no action when
ownership or window information cannot be read. A hidden background client or
download is restarted when recovery is enabled. Other desktop environments are
not supported by this helper; recovery is off by default.

### Downloads repeatedly fail with an IPv6 CONNECT_TO error

The bundled downloader was observed retrying CDN chunks with:

```
Use of IPv6 in *_CONNECT_TO without IPv6 support built-in
```

The launcher now defaults `RCLOL_CDN_IPV4_ONLY=1`. The Libinfo resolver suppresses
AAAA queries only for subdomains of `riotcdn.net`, retaining IPv4 DNS rotation,
TLS and the normal resolver for all other names. It does not pin CDN addresses
or disable IPv6 on the Linux host. Opt out with:

```sh
RCLOL_CDN_IPV4_ONLY=0 ./scripts/run.sh
```

The resolver change requires a rebuilt/installed base runtime. The environment
flag alone cannot alter an older Libinfo binary.

## Riot closes after a network lookup is cancelled

Two local crashes were traced to `CFNetwork::_HostCancel` calling `CFRetain(NULL)`.
`CFHostCancelInfoResolution` scheduled its cancellation callback with an empty
context, so processing that source triggered `SIGTRAP` in CoreFoundation and
terminated RiotClientServices. The UI helpers then exited as a consequence.

The CFNetwork patch supplies the host and retains it until the cancellation source
is invalidated. The standalone `riot-cfhost-cancel-check` reproduces the old trap
and passes 100 cancellations with the fix, including releasing the caller's host
reference before callback delivery. This establishes the cancellation fix; a long
Riot session still needs confirmation. With both clients and the game closed,
run `./scripts/update.sh` to install it, then `sh scripts/run.sh`.

## UI crashes after a button opens an external link

LaunchServices dereferenced a nullable error-output pointer in six paths. The patch checks the pointer itself and populates supplied outputs; the standalone regression covers missing handlers and null outputs. Electron opens LaunchServices through an absolute CoreServices subframework path, so the paired installer also replaces that binary in the stopped guest prefix. Updating only `RuntimeFrameworks/LaunchServices.framework` can leave the crash unfixed.

LaunchServices now opens `http`, `https` and `mailto` links on the Linux desktop itself. It loads the host's `libsystemd.so.0` and asks `xdg-desktop-portal` (`org.freedesktop.portal.OpenURI`) on the user session bus to open the link, so your default browser starts outside Darling and survives `darling shutdown`. Without a portal, it starts `xdg-open` in a transient systemd user unit instead. It also reports a handler for these schemes, since Electron checks for one before opening a link. Other schemes, control characters and URLs over 4096 bytes are rejected. If a link still does nothing, check the client's stderr for a `LaunchServices:` line and that `busctl --user status org.freedesktop.portal.Desktop` works on the host. Both LaunchServices and AppKit must be rebuilt. The hand-off passed the host-side D-Bus regression and opened the host browser from a guest through both `open(1)` and `NSWorkspace`; a link clicked in the Riot client is not yet confirmed. The null-pointer fix does not explain every unrelated button crash.

## Language dropdown closes Riot, or client resize leaves a clipped UI

The [report in issue #1](https://github.com/lucannez64/darling-lol/issues/1#issuecomment-6194498757) contains an Electron renderer abort with `Symbol not found: _CTFontDescriptorCreateMatchingFontDescriptors`. CoreText now exports font-descriptor matching and localized-attribute lookup, and enumerates installed font families through fontconfig instead of returning a null stub. Localized lookup currently falls back to canonical font names. The standalone font-list check exercises Chromium's sequence, including mandatory family matching and retained results.

AppKit also left backing-layer geometry at the old size until a coordinate conversion occurred. It now updates that geometry during frame/bounds changes, and invalidates parent transforms before child resize callbacks. Native parent geometry is updated before resizing child windows, whose Y positions depend on the parent's current height. Standalone regressions reproduce both stale-size defects and pass with the corrections. A follow-up test confirmed that the language dropdown is fixed, but League still displays a 1280×720 image inside a 1600×900 window. The geometry regressions alone do not establish a fix for that remaining resize failure.

Rebuild and install the runtime using the [update instructions](SETUP.md#updating-an-existing-runtime), with the game and both clients closed. CoreText is now included in the GPU/runtime installer alongside AppKit; existing installations need this new override too. Until then, a locale can be selected at launch, for example `RCLOL_REGION=BR RCLOL_LOCALE=pt_BR ./scripts/run.sh`.

Live inspection subsequently confirmed a 1600×900 outer League window with both its CoreAnimation child and presentation target still at 1280×720. `NSThemeFrame` selected the last non-menu/toolbar child as the content view. A standalone reproduction showed that an overlay added after the real content received the resize instead. Frame layout now identifies the window's actual content owner (including a containing wrapper), and applies ordinary autoresizing masks to other decorations. A hidden-window regression reproduces the old behavior with an overlay sibling and passes after the fix. The user retested with this build loaded; League still failed to resize. This correction therefore does not resolve the remaining client failure.

The subsequent file trace showed the browser view reaching the new frame size without a renderer resize, and a later shrink left `RenderWidgetHostViewCocoa` at 1600×900 inside 1280×720 parents. Darling's `NSView setFrame:` directly updated geometry without dispatching `setFrameSize:`. [Chromium's Cocoa implementation](https://chromium.googlesource.com/chromium/src/+/114a1ef9e848ccebec516681bcf1786f85b5cbd6/content/browser/renderer_host/render_widget_host_view_mac.mm#2273) explicitly relies on this dispatch to notify the renderer. AppKit now calls the component setters for changed origins/sizes, with a private geometry updater to avoid recursion. A standalone subclass regression failed before this correction (one callback from four size changes) and passes after it (four callbacks, including parent autoresizing in both directions). The user retested with this AppKit build loaded and still saw clipping. Passive inspection then showed the native CoreAnimation child and a published frame at 1600×900, with the presentation target remaining at 1280×720.

The X11 presentation selector also retained a software publisher across popup updates even after the window outgrew its old presentation target. If that publisher switched to a small popup and a different publisher supplied the resized main frame, the selector kept following the popup slot and never resized the presentation child. It now retains that owner only while the old presentation dimensions still match a native target. A two-publisher standalone pixel regression reproduces the black enlarged area on the old backend; the new backend passes 1280×720 → 1600×900 → 1024×576 → 1280×720 with and without the shared-memory transport. After installing this backend locally, the user confirmed that League window resizing works (2026-10-15).

For the AppKit and X11 corrections, exit the game and both clients manually, then use the small update instead of rebuilding the entire runtime:

```sh
python3 scripts/configure.py
python3 build/runtime/tools/build-riot-appkit-runtime.py
python3 build/runtime/tools/build-riot-x11-runtime.py
darling shutdown
sh build/runtime/tools/install-riot-appkit-runtime.sh
sh scripts/run.sh
```

The installer requires the existing per-user AppKit overlay, saves a checksum-named backup under `build/appkit-runtime-backups`, and atomically replaces each binary, including the paired X11 backend. It does not stop processes or change the system SDK. After launch, change League between 1280×720 and 1600×900 and verify both the rendered extent and click alignment.

The installer previously preferred an existing `build/riot-QuartzCore` over its newly built `build/m336-QuartzCore`. It now installs the artifact it just rebuilt, and that builder recompiles all patched compositor and Metal presentation objects. This avoids silently installing an older optional probe build.

If resize clipping recurs, launch with size-only runtime tracing and resize the client manually:

```sh
mkdir -p build
env DARLING_UI_GEOMETRY_TRACE=1 sh scripts/run.sh 2>&1 \
  | rg --line-buffered '^UI_GEOMETRY ' | tee build/ui-geometry.log
```

Change from 1280×720 to 1600×900 and leave the client open at the broken size. AppKit also writes a bounded window/view hierarchy to the guest `/tmp/darling-ui-geometry.log` (host `~/.darling/private/tmp/darling-ui-geometry.log`). Read that file as well as `build/ui-geometry.log`: League redirects stderr, so the terminal capture alone can omit all of its resize events. The traces record window/view classes, identities, dimensions and autoresizing masks, layer scale and IOSurface dimensions at size changes. Tracing is disabled on ordinary launches. It uses the runtime's own geometry setters and surface publisher, with no debugger, client API access or input automation.

The practice-tool exit in the same report remains unresolved: the supplied League client log records the game starting and exiting about two seconds later, but contains no game-process crash reason. These font and resize fixes do not establish a fix for that exit. Further diagnosis needs the game's own log/crash report or stderr from the affected machine.

## Client reports macOS 10.10 and VAN -101

The legacy CarbonCore `Gestalt` implementation read a Darwin kernel release as though it were a macOS product version, then fell through into `gestaltSysArchitecture`, overwriting every result with `gestaltIntel` (10). The standalone probe reproduced `gestalt=10.10.10` even though `kern.osproductversion` was already `14.7`. Version selectors now read that existing product version, return before the architecture case, initialize missing patch versions to zero and implement the documented packed-BCD limits. Null outputs and unknown selectors return errors.

After installing this correction and restarting locally, the client reported macOS 14.7 and the user confirmed that Play became available. Later launches failed again, including a launch without `DARLING_UI_GEOMETRY_TRACE`. The running client was verified to have the corrected CarbonCore binary loaded. Both successful and failed sessions reported macOS 14.7; successful sessions logged `MvgState: 0`, while failed sessions logged `MvgState: 5`, despite `MvgInit` and `MvgRegisterLogin` returning zero in both. These state values are observations, not a diagnosis of their undocumented meaning. The version correction is therefore insufficient to resolve VAN -101, and tracing is not required to reproduce the failure. Entering a match remains unverified. [Riot describes VAN -101](https://support.riotgames.com/en-us/riot/performance/error-van--101) as an unsupported system configuration and recommends a supported operating system or a support ticket with logs. No verified Darling compatibility fix for this failure is available in this snapshot. A temporary test rollback of commit `ac4a832` restores `proc_listpids` returning `ENOTSUP`; it was followed by both a successful launch and another VAN failure, so it has not established that commit as the cause. Tracing was enabled on the latter launch; its contribution remains unproven.

Build the small update with `python3 build/runtime/tools/build-riot-carboncore-runtime.py`. After manually exiting the game and both clients:

```sh
darling shutdown
sh build/runtime/tools/install-riot-carboncore-runtime.sh
sh scripts/run.sh
```

The installer adds a CarbonCore framework override and the matching absolute CoreServices subframework binary in the guest prefix, with checksum-named backups in `build/carboncore-runtime-backups`. It does not modify the SDK or restart processes. Both forms are needed because framework lookup and explicit subframework loading can take different paths. Ordinary full runtime installation includes this update too.

## `LOLSIGFAIL … addr=0x143` every 10 seconds

`iokitd` crashed in host glibc's `pthread_mutex_lock` and launchd restarted it every 10 seconds. It called `eglGetDisplay()` with an Xlib display, so Mesa picked the platform from `EGL_PLATFORM`. Wayland sessions often set that to `wayland`, and the first `darling` command passes its environment to every guest service, including `iokitd`. `scripts/run.sh` sets `EGL_PLATFORM=x11` and was unaffected, but `darling shell` from a Wayland terminal (including the one in `scripts/install-runtime.sh`) started the loop. `iokitd` now requests the X11 platform explicitly. The fix is part of the base install (`sudo cmake --install`); until then, start Darling with `EGL_PLATFORM=x11`.

## Blocky video or flickering startup logo

The first dynamically resolved GL call could lose floating-point arguments during ELF symbol lookup. Chromium's linear sampler request then failed and left nearest sampling. The generated resolver now preserves those registers. Install both the rebuilt native `libGL.dylib` in `RuntimeLibraries` and the OpenGL framework's library copy using the paired runtime installer; the framework alone is insufficient.

A separate multi-window path stopped the active async presenter when another differently sized surface was drawn in the same context. Such surfaces now use readback without stopping it. Native regressions cover both fixes; user confirmation of the startup splash remains pending. See [testing](TESTING.md) for the first-call sampler and interleaved-window probes.

## Build fails on a new compiler or runs out of memory

- **objc4 debug macro mismatch:** use the empty build type from `scripts/build.sh`, rather than `Release`/`NDEBUG`.
- **OpenDirectory 32-bit ivar errors:** keep `TARGET_i386=OFF`; the supported client is x86-64.
- **Undefined empty Objective-C collection symbols:** retain the patched macOS 10.15 Objective-C runtime code generation.
- **LLVM branch opcode errors:** retain the Indium LLVM 22/23 compatibility patch; do not replace its branch handling with an unconditional version-specific opcode.
- **Scalar float errors or opcode 71 after updating:** `Unsupported LLVM value: float 0.000000e+00` (also `1.000000e+00`) and `Unhandled instruction opcode: 71` can result from compiling Iridium with LLVM 22 headers and loading LLVM 23. The LLVM C API changed enum values between those majors. The build now selects headers and the shared library together; initialization rejects a mismatched major with `Iridium LLVM version mismatch`. Close the game and both clients, then run `git pull` and `./scripts/update.sh` to rebuild and reinstall both the base runtime and per-user overrides. If the same errors persist, include the update's `Iridium LLVM headers:` line and a newly generated `metal-errors.log` with the report.
- **Every game shader fails to compile on LLVM 23** (r3dlog full of `Failed to compile shader`, then `Material Missing Pipeline` and a crash before the loading screen; `/tmp/metal-errors.log` shows `Unsupported LLVM value: <3 x float> zeroinitializer`): LLVM 23 represents vector splats as vector-typed `ConstantFP`/`ConstantInt`, which Iridium did not translate. Indium now splats them itself. Rebuild and reinstall the Metal/Indium pair ([setup](SETUP.md#metal-and-indium-overlays)). The offline check `python3 tools/build-riot-air-check.py --native` translates shader libraries without the game.
- **Build process killed:** lower parallelism, for example `JOBS=2 ./scripts/build.sh` or `--parallel 1` for an incremental component build.
- **Incremental helper cannot find `flags.make` or `link.txt`:** use Unix Makefiles, not Ninja.
- **Prebuilt Swift file contains Git LFS pointer text:** install Git LFS and fetch the original upstream assets; pointer text is not a usable library.
- **Submodule fetch stops with no output right after `src/external/sqlite`:** the next submodule is `src/external/swift`, whose pinned `.lfsconfig` points at the retired `git-lfs.darlinghq.org`. The checkout is blocked on that host, not on sqlite. `prepare.py` redirects that submodule's LFS requests to GitHub. To finish a fetch that was interrupted before any patches were applied, run `git -C build/darling-src -c lfs.url=https://github.com/darlinghq/darling-swift.git/info/lfs submodule update --init --recursive --progress`, then re-run `./scripts/build.sh`. Do not run that submodule update once `prepare.py` has applied patches: it resets every submodule working tree to the pinned revision and discards the patches.

## launchd crashes at boot after changing compiler flags

`darling shell` fails with `Error connecting to shellspawn` and `/tmp/lolsig.log` shows `LOLSIGFAIL pid=1 sig=11 code=1 addr=0x0` in `memmove`, called from `snprintf` in libkqueue's `epoll_event_dump`. The binaries were tuned for AMD Zen: clang 23 with `-mtune=znver*` (also selected by `-march=native` or `-mtune=native` on Zen) loads a thread-local variable's descriptor into a register other than `%rdi`, but dyld's `tlv_get_addr` reads `%rdi`, so every `__thread` and `thread_local` access returns NULL. Rebuild with `-mtune=generic` (the default in `scripts/build.sh` and the overlay builders), and check any binary with:

```sh
python3 tools/check-darwin-tlv-calls.py <binary>...
```

It prints each thread-local access that calls its thunk without the descriptor in `%rdi` and exits 1. A plain `darling shell` boot does not load the `RuntimeLibraries` overlays; to boot the way the launcher does, run `env DYLD_LIBRARY_PATH=/Users/<user>/RuntimeLibraries DYLD_FRAMEWORK_PATH=/Users/<user>/RuntimeFrameworks darling shell /usr/bin/true`.

## Base install refuses to continue while Darling is not running

`sudo cmake --install` prints `Darling currently running for <user>, shutting it down...` several times and then `Darling launchd PID … remains alive after shutdown`. A guest launchd outlived its darlingserver, so `darling shutdown` sees no container to stop. `python3 build/runtime/tools/ensure-darling-runtime.py` stops such a launchd (your own, only when no darlingserver runs); `scripts/run.sh` and `scripts/install-runtime.sh` run it too.

## Lower fight FPS than Windows

The game code itself runs natively under Darling: the x86-64 slice of the universal binary executes unchanged, picks its instruction-set paths with `cpuid` on the real CPU, and sizes its worker pool from the real logical CPU count. What differs from Windows is the path around it. Every Metal call goes through Objective-C, the Metal wrapper, Indium and Vulkan instead of a native Direct3D driver, and kernel services go through the emulation and darlingserver. In an ARAM fight the game's own code now takes most of a frame; see [status](STATUS.md#game-fps-2026-10-1314) for measurements.

If FPS drops back towards the 10-13 morning numbers (75–85 on Hexgate):

1. Make sure the current kernel library, Metal and Indium overlays are installed and active: exit the game and both clients, then `darling shutdown` and `sh scripts/run.sh` ([setup](SETUP.md#game-process-switches)).
2. Check the game's line in `/tmp/rclol-game-decisions.log`: it should read `local_semaphores=1` and `core_isolation=0`. No `/tmp/rclol-no-local-*` file should exist unless you created it on purpose.
3. A server wait above about 0.5 ms per frame means one of the local paths is off. A runqueue wait above a few percent on the main thread means the CPU is oversubscribed, for example by `/tmp/rclol-core-isolation` or background load.

Compare matches on the same map: Bilgewater runs a few percent below Hexgate.

On Hyprland, also check that direct scanout is off (`hyprctl getoption render:direct_scanout` prints `int: 0`). The first match with it off ran about 16 FPS faster than the previous one on the same build; see the next section.

## Hybrid graphics laptops (AMD/Intel iGPU + NVIDIA)

Metal renders with Vulkan, and each finished frame is handed to an OpenGL (EGL) context that presents it. The hand-off uses opaque file descriptors, which only work when Vulkan and OpenGL run on the same GPU. Indium takes the first Vulkan device the loader lists, and on a hybrid laptop both that device and the EGL context are normally the integrated GPU, so the discrete GPU stays idle. A report in [issue #3](https://github.com/lucannez64/darling-lol/issues/3) (Ryzen 9 5900HS, RTX 3060 Laptop) measured 60-70 FPS in ARAM on the integrated GPU; on the NVIDIA GPU the game showed a black screen. **Running on an NVIDIA GPU is not validated.**

### Run on the discrete GPU

Start the launcher with PRIME render offload so that both Vulkan and OpenGL move to the NVIDIA GPU:

```sh
prime-run sh scripts/run.sh
```

`prime-run` sets `__NV_PRIME_RENDER_OFFLOAD=1`, `__VK_LAYER_NV_optimus=NVIDIA_only` and `__GLX_VENDOR_LIBRARY_NAME=nvidia`. `darling` copies the caller's environment into the guest, and guest processes inherit it; whether the Riot client passes all of it to the game has not been checked; the log below shows the result. Darling's OpenGL uses EGL, so if the log below still shows the integrated GPU as the presenting renderer, also set `__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json`. On two Mesa GPUs, `DRI_PRIME=1` does the same.

The guest file `/tmp/metal-gpu` (a device index or part of its name, for example `nvidia`) moves only the Vulkan side. Use it for diagnosis: on its own it splits the two sides across GPUs, and frames cannot be presented.

### Check which GPU rendered

Each process that uses Metal appends to guest `/tmp/metal-device.log` (host `~/.darling/private/tmp/metal-device.log`): every Vulkan device the loader reported, the selected one and why, and the renderer of the presenting OpenGL context with both device UUIDs:

```
[MetalDevice] pid=… physical name="NVIDIA GeForce RTX 3060 Laptop GPU" type=discrete vendorID=0x10de … supported=yes
[MetalDevice] pid=… selected name="NVIDIA GeForce RTX 3060 Laptop GPU" type=discrete … reason=loader-first
[MetalDevice] pid=… created VkDevice for 1 of 1 supported device(s) (selected only)
[MetalDevice] pid=… presenting GL renderer="…" vendor="NVIDIA Corporation" … match=yes
```

`MISMATCH` on the presenting line means Vulkan and OpenGL are on different GPUs. `match=unknown` means the OpenGL driver reports no device UUID. Include this file with performance reports.

The log also lists the selected device's memory heaps and types, and one line the first time each kind of resource (`buffer`, `texture`, `drawable`, `drawable-internal`) gets a storage mode and memory type:

```
[MetalDevice] pid=… memory heap=0 size=6144MiB deviceLocal=yes
[MetalDevice] pid=… memory type=0 flags=0x0(none) heap=1
[MetalDevice] pid=… memory alloc kind=texture storage=private typeBits=0x… type=1 flags=0x1(DEVICE_LOCAL) heap=0 heapDeviceLocal=yes firstFit=0 flags=0x0(none) heap=1 heapDeviceLocal=no changed=yes
```

Resources only the GPU uses (`private`, `memoryless`, and drawables, including League's `framebufferOnly`-off ones unless `/tmp/metal-no-drawable-vram` exists) take the first allowed `DEVICE_LOCAL` type. Before 2026-10-16 they took the first allowed type (`firstFit`), which on NVIDIA can be system RAM; `changed=yes` marks where the two differ. `shared` and `managed` resources keep the first host-visible type. On the RX 6600 (RADV) type 0 is `DEVICE_LOCAL`, so nothing changes there.

The first import of a drawable into the presenting OpenGL context, and the first semaphore import, are logged too; later ones only when they fail (at most five):

```
[MetalDevice] pid=… drawable GL import (first) dedicated=yes size=… 1920x1080 memoryType=1 vkTiling=linear glTiling=linear dedicated_error=0x0 import_error=0x0 setup_error=0x0 storage_error=0x0 query_error=0x0 ok
[MetalDevice] pid=… drawable GL semaphore (first) import_error=0x0 wait_error=0x0 ok (checked for the first 600 imports)
```

A non-zero error or `FAILED` means the frames cannot reach the window. Before 2026-10-16 the import did not mark the memory as dedicated, which the extension requires; Mesa ignores it, and it is the suspected cause of the NVIDIA black screen (not yet confirmed). An NVIDIA log of 2026-10-16 showed `glTiling=optimal` with `setup_error=0x501 storage_error=0x501 FAILED`: NVIDIA's GL rejects `GL_LINEAR_TILING_EXT`, so the linear Vulkan image could not be imported. Since then the internal image and the GL texture use optimal tiling when `GL_VENDOR` contains NVIDIA (`vkTiling=optimal glTiling=optimal ok` is the expected line); everything else keeps linear. If the game shows a black screen on NVIDIA, report it with this file.

An additional NVIDIA report on `68f816d` showed successful optimal-tiling import but repeated `Metal Device timed out giving us a Drawable to present` messages. Its [presentation trace](https://github.com/lucannez64/darling-lol/issues/3#issuecomment-6214732591) shows all three drawables queued and all three main-thread presentation callbacks running, followed by timeouts with three frames still queued. The compositor only prepared a Metal layer when it was the root layer, skipping nested Metal layers. It now prepares Metal layers throughout the tree. A standalone nested-layer reproduction exhausts the pool before this correction and passes ten presentations with pixel readback after it; the root-layer probe also passes. Confirmation on the reporter's NVIDIA system is still needed.

### Host-side checks

These are host settings, not game settings. Run on AC power, and use a performance power profile (`powerprofilesctl set performance`, or `asusctl profile -P Performance` on ASUS laptops). On the tested desktop the game is limited mostly by its own main thread on the CPU, not by the GPU (see [status](STATUS.md#game-fps-2026-10-1314)), so laptop CPU power limits matter as much as the GPU choice.

## Mouse latency despite good FPS

[Issue #10](https://github.com/lucannez64/darling-lol/issues/10) reports mouse latency with FPS comparable to Windows. FPS alone does not measure input-to-display latency, and the report does not yet identify which stage adds the delay.

The X11 backend previously called `XSync` after every cursor-shape update, waiting for an X server reply on the input thread. It now uses `XFlush` to send the change immediately without waiting. This removes an avoidable round trip during cursor updates. A local manual retest on 2026-10-16 found that the mouse problem no longer occurred with VSync enabled. Input-to-display latency has not been measured, and confirmation from the original reporter is still pending. Rebuild and install with `./scripts/update.sh` after exiting the game and both clients.

A later input trace showed League setting the cursor about 190 times and unhiding it about 100 times per second, each time sending the same cursor to the X server. The backend now skips the update when the window already has that cursor. The effect on latency has not been measured.

The backend's relative mouse mode (`CGAssociateMouseAndMouseCursorPosition(false)`) previously warped the pointer to the window center, treated the game's own cursor warps as motion and reported `deltaY` upside down. It now follows macOS: the cursor stays where it is, events carry the physical motion as deltas (positive `deltaY` downwards, also for ordinary motion), the game's warps move the cursor without a delta, and `+[NSEvent mouseLocation]` returns that cursor. On Xwayland a warp takes effect only while the cursor is hidden, so with a visible cursor the pointer is not held in place. `python3 tools/riot-input-offline-check.py` checks the shipped patch on the host without fetching Darling. Add `--xvfb /path/to/Xvfb` to exercise real Xorg protocol on an isolated virtual display; it never uses your desktop display. For Xorg/XLibre (issue #17), relative capture now starts from a fresh `XQueryPointer` snapshot and ignores older queued motion. Warp echoes match both their X request serial and position, so a real motion crossing the anchor cannot be mistaken for an echo. Application warp targets are clipped to the grabbed window in root coordinates, including on a secondary monitor; events, `mouseLocation`, and rewarps share that target. Queued motion before a pending warp does not generate another rewarp. A failed pointer query leaves relative capture suspended, and focus loss releases the grab until the window receives keyboard focus again. Host regression tests cover these paths, but League on the tested Hyprland setup never enters this mode; in-game confirmation on XLibre is still required.

For a manual comparison, keep the scene, FPS cap, display refresh and compositor settings the same. Record whether the delay affects the cursor itself, camera movement, or actions after clicking, and compare the in-game VSync setting on and off. Include the desktop/compositor, X11 or Xwayland session, GPU/driver, monitor refresh and fullscreen/windowed mode in the report. Do not automate game input. The fullscreen cursor-shape problem below is a separate known symptom.

## Click offset grows towards the bottom of the screen

A growing vertical offset has been reported on KDE Plasma/Wayland with Xwayland.
The cause is unconfirmed, including whether it is related to commit `426fb75`.
The borderless fullscreen request has been restored separately; retest alignment
once the game covers the panel.

If it persists, enable the passive input trace before the next manual game launch:

```sh
darling shell touch /tmp/rclol-input-trace
```

The flag is checked once per process, so enable it before launching the game.
During a short manual test, compare clicks near the top, middle and bottom, and
note the direction and approximate size of the error. Include the in-game
resolution and window mode, Plasma's scale setting for each monitor, and the
host output of `xrandr --listmonitors`. Collect the guest log with:

```sh
darling shell cat /tmp/rclol-input-trace.log > rclol-input-trace.log
darling shell rm -f /tmp/rclol-input-trace
```

Removing the flag disables tracing for subsequently started processes. The log
records motion, warps, pointer-position queries and relative-mode transitions;
it can establish whether the relative path is active, but cannot by itself
establish where the game rendered a clicked target. If practical, compare the
same resolution in a smaller window and in borderless fullscreen.

## Cursor stops changing shape in fullscreen

On Hyprland the game's cursor can keep its first shape (for example, it never switches to the attack cursor) until you leave and re-enter fullscreen. Darling sends every change to the X server; what freezes the shape on screen is Hyprland's direct scanout of the fullscreen Xwayland window. Turn it off in `hyprland.conf`:

```
render {
    direct_scanout = 0
}
```

`hyprctl keyword render:direct_scanout 0` applies it until Hyprland restarts.

### Black layer over a running Metal game

The X11 Chromium presenter searches shared Core Animation frame slots by surface size. Native League game windows now bypass that presenter, so a same-size client frame cannot create a presentation child over the game's Metal drawable. This also disables the remote-frame polling timer for the game while preserving Chromium client presentation.

`tools/riot-remote-frame-ownership-check.m` exercises the cached-frame path without creating a display or window. `tools/build-riot-x11-runtime.py` builds this probe. For an unidentified overlay, `python3 tools/trace-riot-windows.py --output build/riot-window-trace.jsonl` passively records Riot/League windows, including unmanaged windows and child stacking, for one hour. It requires the host `python-xlib` package and does not send input.

If a black fullscreen game has only its normal Metal child and becomes visible after removing and restoring fullscreen, inspect the initial fullscreen transition. Automatic fullscreen now requires the window to cover an active monitor, so temporary 1x1 startup windows and ordinary windowed sizes are not promoted. Explicit Cocoa fullscreen requests are preserved. `riot-fullscreen-geometry-check` (built by the X11 builder) checks the policy against active XRandR monitor geometry using only read requests; the next game launch is still required to validate the startup sequence.

### Visible cursor while relative on Xwayland (PR #18)

When Xwayland does not honour warps, the virtual cursor follows real pointer motion and application warps leave it in place. Expected warp echoes are still discarded using their request serial and coordinates. This keeps clicks and camera edge panning aligned with the visible cursor on GNOME/gamescope. The contributor confirmed this path in a gamescope practice-tool session; plain GNOME and NVIDIA still require manual verification.
