<p align="center">
  <img src="docs/assets/league-on-linux.png" alt="A black-and-white penguin nestled in a circular gold L emblem" width="160">
</p>

<h1 align="center">League on Linux</h1>

<p align="center">Run the official macOS League of Legends and Riot Client on x86-64 Linux through <a href="https://github.com/darlinghq/darling">Darling</a>, the way Wine runs Windows programs.</p>

This repository ([PiotrWarzachowski/league-on-linux](https://github.com/PiotrWarzachowski/league-on-linux)) is a fork of [Darling-LoL](https://github.com/lucannez64/darling-lol). It ships source patches for Darling and original integration tooling; it never ships Riot or game files.

## Why this fork

Darling-LoL proved the macOS client can run on Linux, but its author built it as a technical challenge and does not have time to maintain it (see the upstream announcement below). This fork exists to keep the project moving:

- **1:1 parity with a real Mac.** The guest presents itself the way macOS 15.5 (24F74) does, and every change is checked against real Macs over SSH: the same system values, files, tools and log lines a macOS program sees. Like Wine, nothing is hidden from the client; the goal is that it finds the system it was written for.
- **Continued maintenance.** I have several Macs available as references and intend to keep working on the project: following new macOS and client releases, comparing them with the Linux guest and turning every difference into a tested patch.
- **Open to contributions.** Pull requests are welcome, from small fixes to new compatibility patches. Read [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md) first.

## Where it stands

- **Played:** about 7 matches (bots, Swiftplay) on the current runtime, with the client and game stable throughout. In those matches we saw no client errors that a Mac does not also log, no crashes and no descriptor or memory leaks. That is a small sample: it says nothing about how Riot will treat this setup, so read the upstream warning below.
- **macOS 15.5 parity patches:** process view (descriptor list and limits, `RLIMIT_NOFILE`/`RLIMIT_NPROC`, `sysctl` values), framework `Info.plist` metadata, global language preferences, the launchd environment, `WatchPaths` jobs, a macOS-style `/dev`, Apple's `lsof`, `mount` and `umount`, an Apple-compatible `defaults`, and the `NSURLCache`/`HTTPStorages` databases. See [platform identity](docs/PLATFORM_IDENTITY.md).
- **Pull requests:** open work is tracked in [pull requests](https://github.com/PiotrWarzachowski/league-on-linux/pulls).

### Measured

| Machine | Setup | Result |
|---|---|---|
| Intel i9-12900KF, NVIDIA RTX 3070, 32 GB, Ubuntu 26.04 in a distrobox, X11 | 3440×1440, graphics on Low, Swiftplay, 2026-11-26 | 119.5 FPS average over 20 minutes (10-second averages 78–159, median 118); game process steady at about 1.45 GB and 200 descriptors |
| Ryzen 5 5500, RX 6600 (upstream) | ARAM | about 118–135 FPS ([status](docs/STATUS.md)) |
| Same Swiftplay match, comparison with a MacBook Air M1 on macOS 15.5 | game, client and system logs | only expected differences: presented hardware, the x86-64 game slice, champions and match outcome |

For comparison, the same 3440×1440 machine reported 200–240 FPS on Windows. The game's main thread is the limit (GPU about 70% busy); on hybrid Intel CPUs `RCLOL_GAME_PCORE_AFFINITY=1` keeps the game on the performance cores ([troubleshooting](docs/TROUBLESHOOTING.md#lower-fight-fps-than-windows)).

### Requirements at a glance

- x86-64 Linux 5.14+, X11 or Xwayland; a working Vulkan driver (NVIDIA proprietary and AMD RADV have been used).
- Ubuntu 26.04, Fedora 44+, Arch or Debian 14+ natively, or any distribution through a Toolbox/distrobox container.
- About 50 GiB of disk for Darling sources, build and the game. Full list below under [Requirements](#requirements).


- **Performance:** On a Ryzen 5 5500 / RX 6600, ARAM averages about **118–135 FPS** (slowest 10-second stretches around 106–119; higher figures with Hyprland's direct scanout off). Fights were 70–80 FPS before the 2026-10-13/14 work, versus 170–190 on Windows.
- **Not yet measured:** the effect of the memory changes on client RAM.
- **Not validated:** a fresh checkout on another machine, end to end.
- Details: [docs/STATUS.md](docs/STATUS.md) (manual ARAM run and current status) and [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) (real machines and measured frame times). `darling-lol --doctor` and `darling-lol --perf` (available through the Nix launcher) produce the data for a new row. On native and manual installs, run the equivalent commands `python3 tools/doctor.py` and `python3 tools/perf-baseline.py`.

### What's included

- Pinned upstream revisions and patches for 30 Darling components (Metal/Indium, AppKit, QuartzCore, CoreAudio, kernel shim, darlingserver).
- Event-poll presentation and GPU sync fixes, floating-point-safe GL resolvers, multi-window presentation, nullable LaunchServices error handling.
- Reusable server receive buffers and Darwin allocator hints translated to Linux lazy reclamation.
- Build/install/launch wrappers, atomic incremental installers and standalone regression probes.

**Not included:** game files, Riot installers, account databases, tokens, prefixes, captures or compiled binaries. Install the game manually through Riot. The normal launcher does not patch the client, enable an Electron inspector, or start UI automation helpers. It clears `DYLD_INSERT_LIBRARIES` and keeps Darling runtime overrides; this is not a claim of approval by Riot.

---

## Requirements

- **OS:** x86-64 Linux **5.14+**, X11 session or Xwayland (optional opt-in experimental native Wayland via `RCLOL_WAYLAND=game` or `1`, see [setup](docs/SETUP.md#experimental-native-wayland-windows-rclol_wayland)).
- **GPU:** Working hardware Vulkan/OpenGL driver. AMD RADV and NVIDIA (proprietary and open modules) have been exercised; others are not validated. See [NVIDIA](docs/TROUBLESHOOTING.md#nvidia). Hybrid laptops usually render on the iGPU unless started with PRIME offload: see [hybrid graphics](docs/TROUBLESHOOTING.md#hybrid-graphics-laptops-amdintel-igpu--nvidia).
- **Tools:** Git + Git LFS (2.13+), Bash, curl, unzip, CMake 3.20+, Make, Clang/LLD, LLVM dev files (16+; 22/23 is what the Metal overlays are tested with), C/C++ compiler, Python 3.10+. Node.js only for offline JavaScript checks.
- **Libs:** X11/XCB/SHM development files.
- **Disk:** at least **50 GiB** in total for Darling sources/build, Git LFS assets and the game. Space already used by a previous install (build tree, `~/.darling` prefix and system runtime) counts toward this minimum only when it is on the same filesystem as the checkout. Keep at least **5 GiB** free on that filesystem for build and staging files.

`scripts/build.sh` and `scripts/install-runtime.sh` run `scripts/preflight.py` first and stop on unsupported versions (`RCLOL_SKIP_PREFLIGHT=1` bypasses it). For other distributions, see the dependency list in the [pinned Darling source](https://github.com/darlinghq/darling/tree/60ba801decee7a00782f74f6be4c8ffb013f79ff); `tools/env-check.sh` is a starting check (package names may need adapting).

---

## Automated Installation

Installs dependencies, configures the build environment and compiles Darling-LoL. Requires at least **50 GiB** of disk space in total; a previous build/prefix already on disk counts toward it only when it is on the same filesystem as the checkout.

Supported distributions: Fedora 44+, Arch Linux, Debian 14+, Ubuntu 26.04+. Hosts outside this list (for example Ubuntu 24.04 and its derivatives) can still install through a Fedora Toolbox container with `--toolbox`.

```bash
git clone https://github.com/PiotrWarzachowski/league-on-linux
cd league-on-linux
bash scripts/install-native.sh
```

**Options:**

- `--toolbox`, `-t`: Run the installation inside a Fedora Toolbox container, keeping build dependencies off the host. Works on any host with `toolbox` and `podman` installed. The container is named by `DARLING_LOL_TOOLBOX_NAME` (default `darling-lol`).
- `--verbose`, `-v`: Full command output.
- `--yes`, `-y`: Skip confirmations (needed when not run from a terminal). On Arch, a native install runs a full `pacman -Syu` and asks first unless this is given.
- `--region CODE`: Riot region (`EUW`, `EUNE`, `NA`, `BR`, `KR`, `JP`, `OCE`, `LAN`, `LAS`, `TR`, `RU`, `SG2`). Without it, an interactive install asks; with `--yes` or without a terminal it is the region of an explicit `--locale`, else `EUW`.
- `--locale CODE`: Riot Client language for new installs, for example `pt_BR`. Without it, an interactive install asks and suggests the system locale; with `--yes` or without a terminal it is `en_GB`. A prefix that already ran the client keeps its saved language unless `RCLOL_LOCALE` is exported at launch.

Options can be combined: `bash scripts/install-native.sh --toolbox --verbose`

---

## Build Instructions

### Nix / Rootless Package (experimental)

Nix supplies the launcher and dependencies, **not** a prebuilt Darling or the game. First setup is interactive and builds into the writable per-user workspace.

```bash
nix run github:PiotrWarzachowski/league-on-linux#installer
nix run github:PiotrWarzachowski/league-on-linux#default -- --update
nix run github:PiotrWarzachowski/league-on-linux#default
nix run github:PiotrWarzachowski/league-on-linux#shell
```

From a local checkout:

```bash
nix run .#installer
nix run .#default
```

Limits: run updates only after manually closing the clients and shutting Darling down; a live prefix marker blocks a second launch; one workspace/namespace is supported at a time; do not delete `build/nix-gcroots/` while in use. See [Nix setup and operational limits](docs/SETUP.md#nixos-package).

### Manual Build (Non-Nix)

Ensure no Riot/League session is running before installing the runtime.

```bash
git clone https://github.com/PiotrWarzachowski/league-on-linux.git
cd league-on-linux
python3 -m venv build/venv
build/venv/bin/pip install -r requirements.txt
python3 scripts/configure.py --guest-user "$USER"
./scripts/build.sh
sudo cmake --install build/darling-src/build
./scripts/install-runtime.sh
```

Default layout: `/usr/local/libexec/darling`, `~/.darling` and `/Users/$USER` inside the guest (use the real guest account name if different). Paths are rendered into ignored files under `build/runtime/tools`; don't run the tokenized `tools/` templates directly. The install wrapper shuts Darling down before replacing per-user overrides and backs up relevant files. See [setup details](docs/SETUP.md).

---

## Launch & Usage

For native and manual builds, install the Riot Client (change `euw` to your region), then sign in and install League through Riot's own UI. For Nix, use the [Nix launch instructions](docs/NIX.md):

```bash
./scripts/installer.sh euw
```

Launch:

```bash
./scripts/run.sh
```

First installs and future Riot updates can still hit compatibility gaps (e.g. an accepted Install action failing native LaunchAgent registration and leaving the download idle). The archived inspector-based workaround is excluded from the manual workflow. The client window-visibility correction is part of the installed runtime (Darling's AppKit overlay), not a patch to Riot's binaries. See [troubleshooting](docs/TROUBLESHOOTING.md) for the known limitation and the fixes already in the runtime.

**Advanced launch options:**

- **Region/locale:** `RCLOL_REGION=NA RCLOL_LOCALE=en_US ./scripts/run.sh` (display follows `$DISPLAY`; legacy fallback is `:1`).
- **Recover stale client** (client closed but `darlingserver` still running): `RCLOL_RECOVER_STALE_CLIENT=1 ./scripts/run.sh`. Stops only a windowless Riot/League/CEF tree at least 60 s old; preserves `darlingserver`, launchd/mldr, sockets and `.init.pid`. [Details](docs/TROUBLESHOOTING.md#client-remains-running-without-a-window).
- **Clean orphaned helpers** (several `close-league-client-in-game.py` watchers): `RCLOL_CLEAN_STALE_HELPERS=1 ./scripts/run.sh`. Only removes orphaned helpers of this prefix. [Details](docs/TROUBLESHOOTING.md#multiple-client-in-game-helpers).
- **Client handling in-game:** `RCLOL_CLIENT_IN_GAME_TRICKS=1 ./scripts/run.sh`. Reads `lol-user-experience` from `LCULocalPreferences.yaml`; closes the client UI when `closeLeagueClientDuringGame` is true, and hides it when that value is false and `potatoModeEnabled` is true. Missing or invalid values select no action. Low-level switches `RCLOL_CLOSE_CLIENT_IN_GAME` and `RCLOL_HIDE_CLIENT_IN_GAME` remain available. [Details](docs/TROUBLESHOOTING.md#automatic-client-handling-during-a-match).
- **League splash overlay:** `RCLOL_HIDE_LEAGUE_SPLASH=1 ./scripts/run.sh`. Polls every five seconds and unmaps only the borderless `512x216` override-redirect `LeagueClient/LeagueClient` window that can appear at random. Off by default; local window management only, never hides the game or managed client windows, and does not patch client binaries, CEF, anti-cheat or DRM. Standalone: `./scripts/hide-league-splash-window.sh` (`--once` inspects without changes). [Details](docs/TROUBLESHOOTING.md#league-splash-window-appears-over-the-client).

These window-management helpers are opt-in, off by default, and do not patch client binaries.

---

## Updating

Exit the game and both clients first.

**Nix users:** use `nix run --refresh github:PiotrWarzachowski/league-on-linux#default -- --update` when running through Nix. For an installed profile or system package, [upgrade the package first](docs/NIX.md), then run `darling-lol --update`.

**Native, manual, and Toolbox builds**:

```bash
git pull
./scripts/update.sh
```

For a Toolbox install, run these commands from the host. The updater detects the recorded container and runs the update inside it.

`update.sh` re-applies changed patches (reverting the earlier version of each, so other edits survive), rebuilds incrementally, reinstalls the base runtime and per-user overlays (including canonical prefix overlays), signs the prefix overlays and base SDK, and replaces any legacy shared platform identity. It refuses to run while a Riot or League process is open. Background steps log their output by default. Pass `--verbose` to stream all output to the terminal. If you maintain an additional or non-default prefix via `DPREFIX`, update that prefix as well with `DPREFIX=/path/to/prefix sh scripts/install-runtime.sh`. For single components, see [incremental updates](docs/SETUP.md#updating-an-existing-runtime).

A built runtime can also be shipped without the toolchain via `tools/runtime-package.py`: see [prebuilt runtime overlays](docs/PACKAGING.md).

---

## Testing

Start with the standalone regression checks, then play a match manually: [docs/TESTING.md](docs/TESTING.md). Do **not** attach a debugger, query client APIs, inject diagnostic hooks or automate client/game input. The project includes standalone compatibility probes and the opt-in host-side window-management helpers described above, but no client injection hooks, client API automation or input automation; the normal launcher enables none of them.

FPS counters, presentation requests and display refresh are different measurements. Record average and p99 frame times, scene, graphics settings, power state and focus. Game-process speed switches are in [setup](docs/SETUP.md#game-process-switches); the passive match sampler is mentioned in [status](docs/STATUS.md#game-fps-2026-10-1314).

---

## Repository Layout

```
scripts/                portable entry points
upstream/manifest.json  exact base revisions and patch ownership
patches/                current source changes, one patch per repository
tools/                  runtime templates, installers, standalone probes
tests/                  regression tests
research/               research notes
docs/                   setup, testing, status, compliance, interoperability
.github/workflows/      CI
build/                  local sources, outputs, rendered tools (ignored)
```

---

## Legal & Compliance

This is an unofficial interoperability effort. It distributes source patches for Darling and original tooling, not the Riot client, game code, assets or accounts. The normal launcher does not patch the client, game, CEF or anti-cheat. Users obtain the client from Riot and are responsible for complying with Riot's terms; account restrictions are possible. See [interoperability scope](docs/INTEROPERABILITY.md) and [compliance guidance](docs/COMPLIANCE.md) (neither is legal advice).

Darling/upstream components keep their licenses ([NOTICE.md](NOTICE.md)). Original integration tooling is GPL-3.0; patches retain the licensing of their target files.

_League of Legends and Riot Games are trademarks or registered trademarks of Riot Games, Inc. This project is not affiliated with, endorsed by, or sponsored by Riot Games._

**Donations to the original author:** [Support on Ko-Fi](https://ko-fi.com/lucannez64)
