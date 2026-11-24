# Game display timing and Metal drawable presentation

## Scope

The target is the game engine's Metal frame path: CoreVideo scheduling,
QuartzCore presentation and drawable retirement. Public Apple API signatures,
EGL swap intervals and swap-based ticket retirement remain unchanged. Timing
feedback never gates rendering, drawable recycling or VSync-off swaps.

Chromium/CEF's separate IOSurface presenter only receives target nominal timing
through its existing versioned metadata. It does not receive the game's feedback
scheduling optimization. These are shared framework correctness changes; other
users of the same APIs can observe them.

## Runtime display timing

A private CoreGraphics C provider publishes versioned, process-scoped,
host-backed snapshots shared by AppKit, QuartzCore, OpenGL and CoreVideo.
Snapshots identify the process, native surface generation, stable output,
public display ID, rational nominal rate and optional presentation observation.
Publishers invalidate before changing associations. Callback workers read with
a nonblocking lock and coherent cache; startup, changed generations, incomplete
publication and contention without a matching cache use fallback timing.
Backend calls and callbacks never run under the timing lock.

On X11/Xwayland, RandR mode dot clocks retain fractional precision, including
interlace and doublescan. The actual parent or child drawable follows the output
with greatest pixel coverage; ties retain the previous output, then prefer the
primary output and stable ID. Movement, resize, primary reorder, mode changes,
hotplug and surface replacement update the association. Mode queries resolve
public display IDs explicitly instead of subtracting one.

Optional `EGL_CHROMIUM_sync_control` observations supply monotonic UST and MSC.
The clock is mapped with bracketed samples; failed or discontinuous mappings
cannot supply phase. This is server presentation timing on Xwayland, not proof
of physical scanout. X11 Present event collection is not implemented.

The native Wayland bridge appends timing and transaction functions in API v3;
the existing v1/v2 struct layouts remain intact. Surface enter/leave, output
done/removal and valid presentation `sync_output` events determine association.
Otherwise the previous entered output wins, then the fastest entered output,
with stable-ID ties. Before enter, the first valid Wayland output supplies nominal
timing. Older bridges retain rendering and nominal fallback without v3 reads.

Optional `wp_presentation` feedback is requested immediately before the matching
EGL swap. A surface transaction defers unrelated shim commits. Ambiguous borrowed
ownership skips feedback while still swapping. Failed swaps cancel their request;
rebinding, hiding and teardown cancel outstanding observations. Each window has
at most eight feedback objects, expiring after the greater of one second or eight
nominal periods. Protocol v1 nonzero refresh can describe fixed timing; v2 cannot
reliably distinguish minimum intervals from fixed refresh; v3 fixed/variable flags
are respected. Unknown or variable refresh remains observational.

CoreVideo stores binding, cache and deadlines per link in its existing host-backed
lifecycle records. The latest explicit setter wins: display binding clears context
tracking; context binding follows that context's future drawable. The pump sleeps
until the earliest link deadline and checks changes at least every 16 ms. It skips
missed cycles without callback bursts and accumulates fractional remainders.
`videoTimeScale` is always one billion; timestamps consistently convert Mach time.
Eligible fixed-rate feedback with a valid counter supplies the next future phase;
emitted timestamps never move backwards. When `RCLOL_REFRESH_HZ` is absent,
fallback order is eligible feedback,
associated-output nominal timing, backend default output, then labeled software
60 Hz timing. No rate is inferred from submitted FPS or variable-rate intervals.

`RCLOL_REFRESH_HZ` retains its upstream behavior and takes precedence over runtime
output timing. The launcher preserves an explicit value; otherwise it detects and
rounds the primary monitor's current `xrandr` mode (the first active monitor when
none is primary), and exports a nonempty result. The guest environment filter
preserves it. CoreVideo and the separate IOSurface presenter's software limiter
use the configured integer rate, from 1 through 1000 Hz, with the existing 60 Hz
fallback for invalid values. Parsing retains upstream integer-prefix behavior
(for example, an explicit `59.94` is read as 59 Hz; automatic detection rounds it
to 60). Configured timing does not claim backend phase or presentation feedback.
When no value is supplied or detected, runtime timing follows the output as
described above. EGL swap interval behavior remains unchanged.

Specifications: [EGL_CHROMIUM_sync_control](https://github.com/google/angle/blob/main/extensions/EGL_CHROMIUM_sync_control.txt),
[Wayland presentation-time](https://raw.githubusercontent.com/wayland-mirror/wayland-protocols/main/stable/presentation-time/presentation-time.xml).

## Presentation lifecycle

QuartzCore assigns presentation tickets and records completed render generations.
When pool exhaustion forces an inline render and swap, later callbacks for served
tickets skip duplicate work. Unserved tickets retain FIFO order. Attach failures
leave drawables queued; failed swaps and stale render generations cannot confirm
presentation. Queued failures request a one-shot compositor retry. Pool recreation
retires discarded tickets and rejects stale drawable objects referring to reused
slots. Texture rotation precedes reentrant presented handlers.

Indium coalesces preparation by root texture for exportable presentation semaphores
within one command buffer. Distinct drawables and command buffers retain their
preparation, conversion and synchronization work.

## Verification and building

```sh
python3 -m unittest discover -s tests -p 'test_display*py'
python3 -m unittest discover -s tests -p 'test_wayland_timing.py'
python3 -m unittest discover -s tests -p 'test_x11_display_timing.py'
python3 -m unittest discover -s tests -p 'test_presentation_timing_hooks.py'
python3 -m unittest discover -s tests -p 'test_metal_presentation_tickets.py'
python3 scripts/validate.py
python3 scripts/prepare.py --check-only --source build/darling-src
python3 scripts/configure.py
python3 tools/build-display-timing-runtime.py
```

Run the full host suite with its complete output saved to a log, then read its
`Ran`/`OK`/`FAILED` summary. See [testing guidance](../TESTING.md). The paired builder
links CoreGraphics, OpenGL, CoreVideo, QuartzCore and X11 together under
`build/display-timing-runtime/` and rebuilds the Wayland bridge and wrapper. It
installs nothing and launches no native probe. Runtime installers consume this
paired set after their existing stop step; never replace a subset of a running
runtime. See [packaging](../PACKAGING.md#display-timing-runtime).

Host fixtures compile shipped method bodies with synthetic clocks, EGL calls and
backend events. They cover concurrent 59.94/144 Hz links, rational deadlines,
setters, fresh/late/duplicate/variable feedback, clock offsets/discontinuities,
missing counters, queue bounds/expiry, output reorder/hotplug, spanning windows,
surface generations, old protocols/bridges, interrupted publication, cross-instance
visibility and callback-reentrant stop/release. The 14 connected presentation
lifecycle regressions remain, including pool recycling without feedback.
The lifecycle regression loads two independent framework views and checks both
callback-side stop/release and an external thread waiting for the active callback.
The async presenter regression executes the shipped enqueue body and verifies
that successful, failed and saturated submissions preserve nominal metadata.

The Objective-C fixtures adapt syntax to C++ and use inert graphics and scheduling
models. They do not establish native commit attribution, Objective-C runtime
ownership, actual GPU completion, game integration, hardware refresh or physical
VRR. No client, game or native guest probe was launched for this change.

## Performance limits

Savings depend on duplicate work occurring and on the system's bottleneck. CPU
limits, game frame caps, VSync and compositor pacing can hide them. A historical
synthetic 1920x1080 blit measurement reported about 0.12 ms saved by omitting two
duplicate blits on one GPU; it excluded OpenGL import and display presentation,
and is not reproduced by these tests. Uncontrolled matches establish no gameplay
speedup or regression. Hardware compatibility and FPS gains remain unverified.
