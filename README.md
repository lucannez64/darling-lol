<div align="center">
  <img width="150" height="150" alt="darling-lol" src="darling-lol.png" />
  <h3>Darling-LoL</h3>

  <p>
    Experimental macOS League of Legends client on x86-64 Linux through Darling
  </p>
</div>

> [!IMPORTANT]
>
> ## Critical Announcement
>
> **1. Maintenance:** Built mainly as a technical challenge, because everyone kept telling me it was impossible. I do not have the time to maintain it fully. Apart from issues that block you completely, I will only review and merge pull requests.
>
> **2. Bans:** **You will probably get permabanned.** You alone are responsible for launching the game in a modified environment (Darling).
>
> - **DO NOT cheat.**
> - **DO NOT play ranked.**
> - **DO NOT use your main account.**
>
> _Note: Currently triggering 3-Day Suspensions ("UNSUPPORTED_THIRD_PARTY_TOOLS"). DON'T USE IT RN._
>
> **3. Continuity:** If discontinued, patches remain available under their upstream licenses, and the original integration tooling under GPL-3.0.
>
> **4. To Riot:** A lot of players want to play your game on Linux. If you want to fix this, make LoL Next compatible natively or via a layer like Proton (Epic did it with Easy Anti-Cheat, kernel anticheat included). Contact: `darling-lolriot@proton.me` (especially for an official or supported version of League on Linux).
>
> _Open to work: part-time remote as an employee, or full-time remote as a freelancer for a mission._

---

## Status

This is a development snapshot, not a finished launcher. The Riot and League clients launch, and manually played ARAM games have run. Graphics, audio, input, startup crashes and memory reclamation have received compatibility fixes.

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

- **OS:** x86-64 Linux **5.14+**, X11 session or Xwayland.
- **GPU:** Working hardware Vulkan/OpenGL driver. AMD RADV and NVIDIA (proprietary and open modules) have been exercised; others are not validated. See [NVIDIA](docs/TROUBLESHOOTING.md#nvidia). Hybrid laptops usually render on the iGPU unless started with PRIME offload: see [hybrid graphics](docs/TROUBLESHOOTING.md#hybrid-graphics-laptops-amdintel-igpu--nvidia).
- **Tools:** Git + Git LFS (2.13+), Bash, curl, unzip, CMake 3.20+, Make, Clang/LLD, LLVM dev files (16+; 22/23 is what the Metal overlays are tested with), C/C++ compiler, Python 3.10+. Node.js only for offline JavaScript checks.
- **Libs:** X11/XCB/SHM development files.
- **Disk:** at least **50 GB** free (Darling sources/build, Git LFS assets and the game).

`scripts/build.sh` and `scripts/install-runtime.sh` run `scripts/preflight.py` first and stop on unsupported versions (`RCLOL_SKIP_PREFLIGHT=1` bypasses it). For other distributions, see the dependency list in the [pinned Darling source](https://github.com/darlinghq/darling/tree/60ba801decee7a00782f74f6be4c8ffb013f79ff); `tools/env-check.sh` is a starting check (package names may need adapting).

---

## Automated Installation

Installs dependencies, configures the build environment and compiles Darling-LoL. Requires at least **50 GiB** free.

Supported distributions: Fedora 44+, Arch Linux, Debian 14+, Ubuntu 26.04+.

```bash
git clone https://github.com/lucannez64/darling-lol
cd darling-lol
bash scripts/install-native.sh
```

**Options:**

- `--toolbox`, `-t`: Run the installation inside a Fedora Toolbox container (Fedora hosts only), keeping build dependencies off the host. The container is named by `DARLING_LOL_TOOLBOX_NAME` (default `darling-lol`). Afterwards, enter it with `toolbox enter darling-lol` (or your custom name), `cd` to the checkout then follow the Riot Client installation and launch steps below and run update commands inside the container.
- `--verbose`, `-v`: Full command output.
- `--yes`, `-y`: Skip confirmations (needed when not run from a terminal). On Arch, the installer runs a full `pacman -Syu` and asks first unless this is given.
- `--region CODE`: Riot region printed for the Riot Client install step (`EUW`, `EUNE`, `NA`, `BR`, `KR`, `JP`, `OCE`, `LAN`, `LAS`, `TR`, `RU`).

Options can be combined: `bash scripts/install-native.sh --toolbox --verbose`

---

## Build Instructions

### Nix / Rootless Package (experimental)

Nix supplies the launcher and dependencies, **not** a prebuilt Darling or the game. First setup is interactive and builds into the writable per-user workspace.

```bash
nix run github:lucannez64/darling-lol#installer
nix run github:lucannez64/darling-lol#default -- --update
nix run github:lucannez64/darling-lol#default
nix run github:lucannez64/darling-lol#shell
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
git clone https://github.com/lucannez64/darling-lol.git
cd darling-lol
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
- **Client handling in-game:** `RCLOL_CLIENT_IN_GAME_TRICKS=1 ./scripts/run.sh`. Reads `lol-user-experience` from `LCULocalPreferences.yaml`; when `potatoModeEnabled` is true, closes or hides the client UI depending on `closeLeagueClientDuringGame`. Missing or invalid values select no action. Low-level switches `RCLOL_CLOSE_CLIENT_IN_GAME` and `RCLOL_HIDE_CLIENT_IN_GAME` remain available. [Details](docs/TROUBLESHOOTING.md#automatic-client-handling-during-a-match).
- **League splash overlay:** `RCLOL_HIDE_LEAGUE_SPLASH=1 ./scripts/run.sh`. Polls every five seconds and unmaps only the borderless `512x216` override-redirect `LeagueClient/LeagueClient` window that can appear at random. Off by default; local window management only, never hides the game or managed client windows, and does not patch client binaries, CEF, anti-cheat or DRM. Standalone: `./scripts/hide-league-splash-window.sh` (`--once` inspects without changes). [Details](docs/TROUBLESHOOTING.md#league-splash-window-appears-over-the-client).

These window-management helpers are opt-in, off by default, and do not patch client binaries.

---

## Updating

Exit the game and both clients first.

**Nix users:** use `nix run --refresh github:lucannez64/darling-lol#default -- --update` when running through Nix. For an installed profile or system package, [upgrade the package first](docs/NIX.md), then run `darling-lol --update`. The commands below are for native and manual builds.

**Native / manual builds** (for a `--toolbox` install, run these inside the container):

```bash
git pull
./scripts/update.sh
sh scripts/run.sh
```

`update.sh` re-applies changed patches (reverting the earlier version of each, so other edits survive), rebuilds incrementally and reinstalls the base runtime and per-user overrides. It refuses to run while a Riot or League process is open. For single components, see [incremental updates](docs/SETUP.md#updating-an-existing-runtime).

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

**Donations:** [Support on Ko-Fi](https://ko-fi.com/lucannez64)
