# Agent diagnostics: guest windows, display modes and presentation

Read-only knowledge for debugging what the guest draws and how its windows are
placed. It complements `docs/TESTING.md` (probes) and
`docs/TROUBLESHOOTING.md` (user-facing symptoms). The rules in `AGENTS.md` still
apply: never start or stop the client or game, never inject input, never attach
a debugger, never query client APIs. Everything here observes a session the user
is already running and asks the user for the one or two actions that separate
causes.

## Where the code is

| Path | Contents |
|---|---|
| `build/darling-src/src/external/cocotron/AppKit/X11.backend/X11Display.m` | `setMode:forScreen:`, `_screensLocked`, `frameCoversPhysicalScreen:`, mode/screen geometry helpers |
| `build/darling-src/src/external/cocotron/AppKit/X11.backend/X11Window.m` | `setFrame:`, `setStyleMask:`, `wantsFullscreenWithStyleMask:rootFrame:`, `requestFullscreen:force:`, `updateLayerPresentationHints:` |
| `build/darling-src/src/external/cocotron/AppKit/X11.backend/X11SubWindow.m` | `sharedCGLWindow` parent-drawable rule |
| `build/darling-src/src/external/cocotron/AppKit/X11.backend/X11Display.h` | `_emulatedModes`, `_physicalScreenFrames`, `_screensLocked`, `frameCoversPhysicalScreen:` |
| `patches/cocotron.patch` | canonical patch; edit the prepared tree, resplice, never hand-edit the patch |
| `tests/test_x11_fullscreen_mode.py` | host regression tests: policy, geometry, mode emulation, monitor coverage, resize guard |
| `docs/TROUBLESHOOTING.md` | the user-facing version of the same facts |

`build/runtime/tools/` is rendered from `tools/`. After changing `tools/*`, run
`python3 scripts/configure.py` and use the rendered copy.

## Build and install the X11 backend

The host has no `clang`; build inside the toolbox. Installing the overlay calls
`tools/stop-darling-runtime.py`, so do it only with the client closed (ask the
user to close it; do not stop it yourself).

```sh
# compile only the backend
toolbox run -c darling-lol sh -c \
  'cd build/darling-src/build && cmake --build . --target X11_backend -j"$(nproc)"'

# or the lighter isolated build plus the probes used below
toolbox run -c darling-lol python3 tools/build-riot-x11-runtime.py

# install the per-user overlays (client closed)
python3 scripts/configure.py
toolbox run -c darling-lol sh build/runtime/tools/install-riot-presentation-runtime.sh
python3 tools/sync-canonical-runtime-overlays.py --prefix "$HOME/.darling" \
  --guest-home "$HOME/.darling/Users/<guest-user>"
```

The base SDK under `/usr/local/libexec/darling` is root-owned and `sudo` needs a
password, so the unprivileged overlay install above is the normal path; never
rely on `cmake --install`.

Verify that both overlays are the same binary and carry the new code, then have
the user restart the session (the running guest keeps the old image):

```sh
X="$HOME/.darling/Users/<guest-user>/RuntimeFrameworks/AppKit.framework/Versions/C/Resources/Backends/X11.backend/Contents/MacOS/X11"
md5sum "$X" "$HOME/.darling/System/Library/Frameworks/AppKit.framework/Versions/C/Resources/Backends/X11.backend/Contents/MacOS/X11"
toolbox run -c darling-lol llvm-nm --arch x86_64 "$X" | grep -c frameCoversPhysicalScreen
```

Hashes must match and the selector count must be non-zero.

## Read a live session without touching it

`xwininfo`, `xprop` and `xrandr` are in the toolbox; `import` and `magick` are on
the host. Always pass `DISPLAY=:0` to `toolbox run`.

```sh
# window tree: class names, sizes, positions
DISPLAY=:0 toolbox run -c darling-lol xwininfo -root -tree |
  grep -E 'LeagueofLegends|LeagueClient|Riot Client'

# one window: geometry, map state, borderless, child size
DISPLAY=:0 toolbox run -c darling-lol xwininfo -id <id>

# WM state and compositor hints
DISPLAY=:0 toolbox run -c darling-lol xprop -id <id> |
  grep -E '_NET_WM_STATE|_NET_WM_WINDOW_TYPE|WM_NORMAL_HINTS|_NET_WM_BYPASS|_VARIABLE_REFRESH'

# monitor geometry and the mode list the host really has
DISPLAY=:0 toolbox run -c darling-lol xrandr --current

# screenshot and a one-line content summary (host)
DISPLAY=:0 import -window <id> /tmp/opencode/game.png
magick /tmp/opencode/game.png \
  -format 'mean=%[fx:mean] sd=%[fx:standard_deviation] colors=%[colors] size=%wx%h\n' info:
```

Read them together:

| Field | Healthy | Meaning when wrong |
|---|---|---|
| window size | the requested mode (or the monitor at native) | a stale size hint, EWMH fullscreen pinning the window, or `setFrame:` never arriving |
| `Map State` | `IsViewable` | the game has not mapped its window yet or crashed |
| `_NET_WM_STATE` | `_NET_WM_STATE_FULLSCREEN` only when the window covers a monitor | a smaller emulated mode intentionally has none |
| child window | exactly the size of the parent | a drawable that does not exactly cover the parent gets its own child window instead of the parent drawable |
| `mean`/`sd`/`colors` | non-zero, many colors | `mean=0 sd=0 colors=1` is a uniformly black window |
| `_NET_WM_BYPASS_COMPOSITOR` | `0` while the Metal drawable is owned | `2` means a fullscreen game without a layer drawable owner |

`xwd` is not available on this host or in the toolbox; use `import`/`magick`/`ffmpeg`
on the host.
Guest stdout/stderr are not the launch log: `/proc/<pid>/fd/1` and `/fd/2`
point at `anon_inode:[perf_event]`, so backend `NSLog` from the game is lost.
`build/desktop-launch.log` only holds the launcher and client output. The game's
own log (`~/.darling/Applications/League of Legends.app/Contents/LoL/Logs/GameLogs/<stamp>/<stamp>_r3dlog.txt`)
records resolution and game state but no window geometry.

## How the fullscreen path works

- League calls `CGDisplaySetDisplayMode(displayID, mode, 0)` early in startup,
  then reads `[NSScreen frame]` to place its own window. That order is why the
  mode is emulated through the screen frame instead of the window alone.
- `setMode:forScreen:` records the requested size in `_emulatedModes` keyed by
  display id, drops the cached `NSScreen`s, and `_screensLocked` rebuilds them
  with the mode frame anchored at the monitor's top-left. A request for the
  monitor's own size removes the emulation.
- The window resize in `setMode:` is only a fallback: visible, untitled, exactly
  the physical monitor frame, main thread. A window the game has not mapped and
  sized yet is left alone, and the native mode is a no-op. Resizing early
  interrupts the engine's own fullscreen setup and produced a black game window.
- EWMH fullscreen is requested only when the frame covers a physical monitor.
  Compare `_physicalScreenFrames` (`frameCoversPhysicalScreen:`), never the
  emulated `NSScreen` frame, or the compositor pins the window back to the
  monitor.
- `sharedCGLWindow` uses the parent drawable only when the drawable exactly
  covers an untitled parent; anything else gets its own child drawable and its
  own coordinates.
- A smaller mode therefore becomes a smaller window with the desktop and panel
  visible around it. That is the emulated mode, documented in
  `docs/TROUBLESHOOTING.md`, not a presentation fault. The game still renders one
  framebuffer pixel per window pixel, and pointer events are window-relative
  (`XQueryPointer` on the game window), so moving or resizing the window does not
  offset the pointer.

## Black game window triage

Work in this order; stop at the first line that explains the observation.

| Observation | Likely cause | Next step |
|---|---|---|
| No `LeagueofLegends` window | game not mapped yet, or it exited | wait a few seconds, check the process and the last `r3dlog` state |
| Window missing its Metal child | the game has not created its drawable yet | sample again; a child that appears later is normal |
| Window size is the monitor, `_NET_WM_STATE_FULLSCREEN`, content black | the window was touched during startup, or presentation never composited | compare the child size; ask the user to leave and re-enter fullscreen once (that separates engine from compositor) |
| Window size is neither the requested mode nor the monitor | stale size hints or an EWMH pin | `xprop` `WM_NORMAL_HINTS` and `_NET_WM_STATE` |
| Content visible with the desktop around it | emulated smaller mode | expected; see above |
| Pointer offset from the drawn target | window origin differs from the guest frame, or relative mode is off | ask for `RCLOL_INPUT_TRACE=1`, read `/tmp/rclol-input-trace.log` in the guest |

Do not diagnose from a single sample of a window that is still being configured;
take two samples a few seconds apart. Do not conclude that the compositor is
broken while the window is unmapped, 1x1, or has no child.

## Worked example: issue #126

`FullscreenExclusive` at a lower resolution left the window at desktop size with
the framebuffer and pointer at the requested size. Disassembly of
`LeagueofLegends` showed the game calls `CGDisplaySetDisplayMode`, then
`[NSScreen screens]`, matches `NSScreenNumber`, and places its window from
`[NSScreen frame]`. Resizing the window alone was undone by that placement, so
the fix emulates the mode in the screen frame. The first attempt resized the
window unconditionally from `setMode:` and produced a black fullscreen: League
calls it around 0.6 s, before its own `setStyleMask:`/`makeKeyAndOrderFront:`/
`setFrame:`, and the early resize interrupted the engine's setup. The shipped
version keeps the emulation, treats the resize as a guarded fallback, and
compares EWMH coverage against the recorded physical frames so the native path
is unchanged.

## Finding the call path in the game binary

The guest binary is at
`~/.darling/Applications/League of Legends.app/Contents/LoL/Game/LeagueofLegends.app/Contents/MacOS/LeagueofLegends`.
Disassemble in the toolbox; keep output in `/tmp/opencode`.

```sh
toolbox run -c darling-lol llvm-objdump --arch=x86_64 -d \
  "$HOME/.darling/Applications/League of Legends.app/Contents/LoL/Game/LeagueofLegends.app/Contents/MacOS/LeagueofLegends" \
  > /tmp/opencode/league-full.txt
grep -n 'CGDisplaySetDisplayMode\|NSScreen\|setFrame:display:' /tmp/opencode/league-full.txt | head
```

Use `--start-address`/`--stop-address` to read one function; indirect calls show
the stub address, so resolve them with `llvm-nm` or by following the arguments
(`NSScreen screens`, `setStyleMask:`, `makeKeyAndOrderFront:` identify the AppKit
call path). Offsets change between game builds; the sequence is what matters.

## Tests and patch workflow

```sh
python3 scripts/patchkit.py resplice cocotron \
  AppKit/X11.backend/X11Display.m AppKit/X11.backend/X11Window.m \
  AppKit/X11.backend/X11Display.h --write --sync-manifest
python3 scripts/patchkit.py verify
python3 -m unittest discover -s tests -p 'test_x11_fullscreen_mode.py' -v
python3 scripts/validate.py
python3 scripts/prepare.py --check-only --source build/darling-src
```

The full suite buffers stdout when piped; save it to a file and read the
`Ran`/`OK`/`FAILED` lines from the file instead of piping through `tail`.
`test_x11_fullscreen_mode.py` compiles the patched sources with `c++`, so it
runs on the host without Darling. Two `test_security_host` errors about a missing
`libxml-2.0` pkg-config file are a host dependency, not a regression.

## Environment notes

- Builds, `llvm-nm`, `llvm-objdump`, `xwininfo`, `xprop` and `xrandr` live in the
  toolbox (`toolbox run -c darling-lol ...`); `import`, `magick` and `ffmpeg` are
  on the host. `sudo` needs a password, so no root installs.
- Use `/tmp/opencode` for scratch files, logs and screenshots; do not leave
  captures or logs in the repository.
- A new overlay only takes effect after the user restarts the session.
