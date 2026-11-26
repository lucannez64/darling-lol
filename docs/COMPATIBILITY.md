# Compatibility matrix

This page answers one question: **on which machines has darling-lol actually
been used to play, and what did it run like?**

It is a record of real reports, not a list of every configuration that might
work. "Verified" below means the Riot Client opened, League launched, a match
was played manually, and the store was reachable. Experimental or untested
hardware is not listed until someone sends a report.

The project is unofficial and carries account risk. Never test on an account you
care about, and never play ranked while testing (see the warning in the
[README](../README.md)).

## How to add a row

Run these on the machine you played on, after a match:

```sh
darling-lol --doctor                 # host, GPU, Vulkan, session, prefix checks
darling-lol --doctor --report doctor.json   # same checks as a JSON attachment
darling-lol --perf                   # frame pacing from the game's own logs
darling-lol --perf --json            # the same numbers as JSON
```

`darling-lol --doctor` needs no runtime and starts nothing, so it is safe to run
at any time. `darling-lol --perf` reads the `netstats.csv` files that League
already writes; it does not touch the client or the game.

Open an issue titled `Compatibility: <distro> / <GPU>` and attach `doctor.json`
plus the `perf --json` output. Do not paste a full `~/.darling` log or your home
directory; `darling-lol --report` produces a redacted bundle if more
diagnostics are needed. Then add a row here in a pull request, or mention the issue and a
maintainer will add it.

### What the numbers mean

- `avgfps` is the inverse of the mean sampled frame time over the match, not
  the average of per-second FPS values.
- `p50/p95/p99ms` are percentiles of League's ten-second `game.frametime_average`
  samples: the typical sample, the slow 5 %, and the slowest 1 %. They describe
  ten-second averages, not individual frames.
- `slowfps` is the average FPS over the slowest quarter of the match, which is
  closer to what stutter feels like than the headline average.
- `hitch` counts ten-second samples whose frame time is at least twice the
  match median.
- FPS depends on map, graphics settings, charger/power state, other programs and
  shader-cache warmth. Treat comparisons as useful only when those match.

## Verified configurations

| Distro / desktop | CPU / GPU | Driver | Session | Kernel | Runtime | Result | Frame pacing |
|---|---|---|---|---|---|---|---|
| Omarchy (Arch), Hyprland | Intel i5-10210U / Intel UHD 620 | `i915`, Mesa ANV | Wayland + Xwayland | 7.2.5 | patched Darling, `-O2` libc/libc++ | Client, game, ARAM/Swiftplay and store work; playable at low settings | 24–32 avg FPS over five ARAM matches; p99 44–98 ms; slowest quarter 20–27 FPS |

Notes for the UHD 620 row:

- Measured 2026-10-23 with `tools/perf-baseline.py` on five match logs, no
  FPS overlay. The laptop has 11.5 GiB RAM and a 15 W Comet Lake CPU.
- The 24 FPS match is also the longest log and the one with the most samples;
  the spread between matches is larger than the difference between recent
  builds, so single-match comparisons are not meaningful.
- Intel panel self-refresh (`i915.enable_psr`) was on for part of this period.
  See [panel self-refresh](TROUBLESHOOTING.md#blocky-video-or-flickering-startup-logo).
- Expect first-match shader compilation to show up as a high `p99` until the
  cache is warm.

The Ryzen 5 5500 / RX 6600 development machine is faster and is documented
separately in [status](STATUS.md#game-fps-2026-10-1314). It is not a laptop
baseline and should not be read as the expected result on integrated graphics.

## Not verified

These are common laptop configurations without a confirmed play test here.
Reports are welcome, but treat them as unknown rather than supported:

- Recent AMD integrated graphics (Radeon 680M/780M) under RADV.
- NVIDIA hybrid laptops using PRIME render offload.
- Fedora Silverblue and other immutable distributions with Nix.
- Intel Arc discrete GPUs.

Known driver caveats live in the
[troubleshooting guide](TROUBLESHOOTING.md#nvidia) and
[hybrid graphics notes](TROUBLESHOOTING.md#hybrid-graphics-laptops-amdintel-igpu--nvidia).

## Filesystem launch events

Darling's `UserEventAgent` is installed as a system LaunchDaemon with
`EventMonitor = true`. It provides launchd's `com.apple.fsevents.matching`
stream for `WatchPaths`, `QueueDirectories` and `KeepAlive.PathState`, including
paths that do not exist when a job is loaded. `WatchPaths` changes are coalesced
for about one second and delivered as a pulse; queue-directory and path-state
conditions stay asserted while their condition holds. This supplies the
filesystem notifications used by the Riot Client's install-check LaunchAgent.
Ordinary user jobs launched through shellspawn inherit the system bootstrap and
are covered by this monitor; a separate per-user launchd process requires its
own monitor. Host tests inspect the distributed patch and compile its state and
path-handling code against filesystem fixtures; delivery to a loaded job still
requires guest verification after installing the updated runtime.
