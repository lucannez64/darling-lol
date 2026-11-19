# Agent guide

This is the canonical entrypoint for automated agents (Codex, opencode, Claude
Code, Gemini CLI, GitHub Copilot and similar). Read it before changing code.
It is deliberately an index: the documents it names own the details.

## What this repository is

`darling-lol` is an unofficial compatibility effort that runs the official
macOS League of Legends and Riot Client on x86-64 Linux through Darling. The
repository ships source patches for Darling and original integration tooling;
it never ships Riot or game files, assets, accounts, tokens or prefixes.
Original tooling is GPL-3.0-only; patches keep the license of the upstream
files they modify.

## Non-negotiable rules

- Keep the scope compatibility-only. Do not add DRM, anti-cheat,
  authentication or telemetry circumvention, credential handling, telemetry
  spoofing or gameplay automation.
- Do not add or link Riot/game binaries, assets, logos, screenshots, audio,
  game data, account data, tokens, prefixes, logs or captures.
- Do not attach a debugger, query client APIs, inject hooks, automate input or
  stop a running client/game to test a change.
- Use compatibility language: "compatibility issue", "client check",
  "platform identity". Avoid "stealth", "evasion", "spoofing", "bypass" and
  "unbannable".
- Do not run installers, `scripts/uninstall.sh`, native guest probes or
  anything that launches or stops the client or game unless the user
  explicitly asks.
- Preserve copyright, license and attribution notices. Sign commits off with
  `git commit -s`.

## Fast checks

```sh
python3 scripts/validate.py                 # source audit; runs configure.py
python3 -m unittest discover -s tests       # full host suite (what CI runs)
python3 -m unittest discover -s tests -p 'test_desktop_entry.py'
python3 scripts/preflight.py                # host/build prerequisites
python3 scripts/prepare.py --check-only --source /path/to/darling-src
```

Run the patch check when patches or pins change. Pass a prepared Darling
checkout with the component revisions from `upstream/manifest.json`. On a
fresh clone or after changing pins, use `scripts/prepare.py --source
/path/to/new/darling-src` to prepare a separate checkout at those revisions.

Most tests use temporary HOME/prefix fixtures and do not start Darling. Some
compile C, C++ or Objective-C and need clang, c++, libxml2 and the Objective-C
runtime; CI installs them. `docs/TESTING.md` separates host-only checks from
guest probes; do not run the guest probes on your own.

## Source vs generated files

- Edit `tools/*` and `scripts/*`. `scripts/configure.py` renders every file in
  `tools/` into `build/runtime/tools/`, replacing `@PROJECT_ROOT@`,
  `@HOST_HOME@`, `@GUEST_HOME@`, `@GUEST_USER@`, `@DPREFIX@`, `@DARLING_SDK@`,
  `@REGION@` and `@LOCALE@`.
- Never edit or commit `build/runtime/tools/*`: the next configure run
  overwrites it. Run `python3 scripts/configure.py` after changing a `tools/*`
  source and test the rendered copy.
- `scripts/common.py` owns `REGIONS` and `LOCALES`. `scripts/install-native.sh`
  and `scripts/create-shortcuts.sh` keep copies of those lists in sync.
- `scripts/run.sh` is a thin launcher: it accepts no arguments, reads
  `RCLOL_GAMEMODE`, runs `configure.py`, and delegates to the rendered
  `build/runtime/tools/run-riot-client.sh`. Put launch behavior and flags in
  `tools/run-riot-client.sh`, not in `run.sh`.
- `upstream/manifest.json` pins every upstream revision; `scripts/prepare.py`
  applies the patches and never resets a checkout.

## Read on demand

| Read | When |
|---|---|
| `docs/COMPLIANCE.md`, `docs/INTEROPERABILITY.md`, `docs/PLATFORM_IDENTITY.md` | **Scope, compliance and identity**: before user-facing, security or platform-identity changes. |
| `docs/SETUP.md`, `docs/TROUBLESHOOTING.md`, `scripts/run.sh`, `tools/run-riot-client.sh` | **Setup, launch and flags**: before changing build, install, launch or environment behavior. |
| `scripts/README.md` | **Scripts overview**: before changing or adding a host entry point. |
| `docs/TESTING.md`, `tests`, `.github/workflows/source-check.yml` | **Tests and probes**: before writing or running tests. |
| `docs/STATUS.md`, `docs/COMPATIBILITY.md` | **Current status**: when you need what works or what was measured. |
| `docs/NIX.md`, `docs/PACKAGING.md`, `flake.nix`, `scripts/nix-state.py` | **Nix and rootless runtime**: before touching the flake or the namespace lifecycle. |
| `patches`, `upstream/manifest.json`, `scripts/prepare.py` | **Patches and upstream**: before changing a patch or a pinned revision. |
| `scripts/create-shortcuts.sh`, `scripts/create-desktop-entry.sh`, `tools/desktop-entry.py` | **Desktop integration**: before changing shortcuts, icons or GNOME window entries. |
| `docs/notes`, `docs/reviews`, `research/README.md` | **History and reviews**: only for background; these notes may be stale. |

## Script map

| Script | What it does |
|---|---|
| `scripts/install-native.sh` | Install dependencies, build and install the runtime; native or `--toolbox` |
| `scripts/build.sh` | Preflight, configure, apply pinned patches and build incrementally |
| `scripts/prepare.py` | Fetch pinned Darling sources and apply patches; `--check-only` validates them |
| `scripts/patchkit.py` | Patch-stack status, hunk/manifest verification, single-file resplice and overlay ledger |
| `scripts/configure.py` | Render local paths, region and locale into `build/config.json` and `build/runtime/tools/*` |
| `scripts/install-runtime.sh` | Install the paired per-user runtime overlays; never copies game files |
| `scripts/update.sh` | After `git pull`: re-apply changed patches, rebuild and reinstall the runtime |
| `scripts/run.sh` | Thin launcher: no arguments, optional `gamemoderun`, delegates to `run-riot-client.sh` |
| `scripts/installer.sh` | Run Riot's installer inside the prefix for one region code |
| `scripts/uninstall.sh` | Remove Riot software from the prefix; destructive; keeps the Darling runtime |
| `scripts/preflight.py` | Check host versions and build/runtime prerequisites |
| `scripts/validate.py` | Audit tracked source, rendered shell/Python syntax, pinned revisions and patches |
| `scripts/create-shortcuts.sh` | Interactive shortcut manager; calls the rendered `desktop-entry.py` |
| `scripts/create-desktop-entry.sh` | Non-interactive launcher, icon and opt-in window entries |
| `scripts/desktop-run.sh` | Menu entry: log output to `build/desktop-launch.log`, then `run.sh` |
| `scripts/launch-shortcut.sh` | Menu first run: install the client when missing, else `desktop-run.sh` |
| `scripts/open-guest-url.sh` | Handle `riotclient://` sign-in links for the running guest |
| `scripts/hide-league-splash-window.sh` | Opt-in helper that unmaps the 512x216 League splash window |
| `scripts/darling-rootless.sh` | Enter the rootless Darling namespace as namespace root |
| `scripts/fhs-entry.sh` | Re-enter as the ordinary namespace user with scoped capabilities |
| `scripts/nix-compiler.sh` | Keep the clang driver name while choosing host or Darwin headers |
| `scripts/nix-copy.py` | Refresh tracked package files without deleting workspace data |
| `scripts/nix-idle.py` | Read-only maintenance preflight; never signals a process |
| `scripts/nix-lifecycle.py` | Hold namespace-local shutdown authority until the session exits |
| `scripts/nix-prefix.py` | `--set-prefix`: move or switch the guest prefix |
| `scripts/nix-session.sh` | Run setup and the client in the same rootless namespace |
| `scripts/nix-state.py` | Check per-prefix rootless readiness without starting Darling |
| `scripts/nix-stop.py` | `--stop`: stop the workspace session or its darlingserver safely |
| `tools/run-riot-client.sh` | Real launch logic: profile, GPU/env, helpers, client arguments, supervisor |
| `tools/session-supervisor.py` | Supervise the guest session; idle stop, clean exit, scoped cleanup |
| `tools/exec-guest.py` | Filter host environment, close inherited FDs, start the guest |
| `tools/desktop-entry.py` | Generate launcher, icon and window entries; rendered into `build/runtime/tools` |
| `tools/runtime-package.py` | Export, inspect and install deterministic runtime overlay archives |
| `tools/doctor.py` | Host and prefix diagnostics used by `darling-lol --doctor` |
| `tools/perf-baseline.py` | Collect the baseline data used in `docs/COMPATIBILITY.md` |

## Launch flags

`scripts/run.sh` takes no options. The launcher reads environment variables in
`tools/run-riot-client.sh` (rendered to `build/runtime/tools/run-riot-client.sh`),
`tools/session-supervisor.py`, `tools/exec-guest.py` and, for `RCLOL_GAMEMODE`,
`scripts/run.sh`. The shortcut manager prints the full reference
(`scripts/create-shortcuts.sh`); `docs/SETUP.md` and
`docs/TROUBLESHOOTING.md` document individual flags and their failures.

| Variable | Default | Effect |
|---|---|---|
| `RCLOL_REGION` | configured region | Riot server region passed as `--region` |
| `RCLOL_LOCALE` | configured locale | client language passed as `--locale` |
| `RCLOL_PLATFORM_PROFILE` | `compat` | hardware presentation: `compat` or `host` |
| `RCLOL_GAMEMODE` | `auto` | run under `gamemoderun` when installed; `0` disables |
| `RCLOL_CLEAN_ON_EXIT` | `1` | `0` keeps the runtime running after the clients exit |
| `RCLOL_STOP_AFTER_CLIENT_SECONDS` | `60` | stop after the last client window with no game |
| `RCLOL_STOP_TOOLBOX` | `1` | `0` leaves the Toolbox container running after the session |
| `RCLOL_CDN_IPV4_ONLY` | `1` | `0` keeps the upstream IPv6 resolver behavior |
| `RCLOL_RECOVER_STALE_CLIENT` | `0` | opt-in recovery of a windowless client tree |
| `RCLOL_RECOVER_HIDDEN_CLIENT` | `0` | opt-in recovery of a hidden default-prefix session |
| `RCLOL_CLEAN_STALE_HELPERS` | `0` | remove orphaned in-game helpers before launch |
| `RCLOL_CLIENT_IN_GAME_TRICKS` | `0` | close or hide the client during a match from preferences |
| `RCLOL_CLOSE_CLIENT_IN_GAME` | `0` | low-level close helper |
| `RCLOL_HIDE_CLIENT_IN_GAME` | `0` | low-level hide helper |
| `RCLOL_HIDE_RIOT_CLIENT_IN_GAME` | `0` | also hide the Riot Client with the close helper |
| `RCLOL_HIDE_LEAGUE_SPLASH` | `0` | hide the 512x216 splash window |
| `RCLOL_DISCORD_RPC` | `1` | `0` removes the Discord IPC links |
| `RCLOL_FILTER_HOST_ENV` | `0` | drop host desktop-session variables before the guest |
| `RCLOL_KEEP_HOST_ENV` | empty | names kept by `RCLOL_FILTER_HOST_ENV` |
| `RCLOL_FINISH_LEAGUE_EXIT` | `1` | `0` disables the League exit finish workaround |
| `RCLOL_FINISH_LEAGUE_LOAD` | `1` | `0` disables the League load finish workaround |
| `RCLOL_LAUNCH_PRODUCT` | `league_of_legends` | empty starts the bare client |
| `RCLOL_LAUNCH_PATCHLINE` | `live` | patchline passed to the client |
| `RCLOL_GUEST_LOCALE` | `en_US.UTF-8` | guest `LANG`/`LC_ALL` |
| `RCLOL_REFRESH_HZ` | detected (60 fallback) | display link and frame pacing |
| `RCLOL_CORE_DUMPS` | `0` | `1` keeps core dumps |
| `DSERVER_PCORE_AFFINITY` | unset | `1` pins darlingserver threads to performance cores |

## Project invariants

- Desktop window entries are opt-in (`--window-entries` or
  `DARLING_LOL_WINDOW_ENTRIES=1`), hidden (`NoDisplay=true`), and use
  `StartupWMClass` values `LeagueClientUx`, `LeagueofLegends` and `Riot Client`.
  The launcher itself matches `RiotClientServices`.
- The build is x86-64 only and uses Unix Makefiles. Keep `CMAKE_BUILD_TYPE`
  empty: `Release` breaks the objc4 debug macros. Global flags stay
  `-O3 -march=native -mtune=generic`; `bsdthread_register.c` and `sigexc.c`
  stay at `-O0`.
- Do not commit `build/`, logs, prefixes, `custom-icon.*`, binaries, archives
  or local captures.
- Add or update a test for every behavior change; run the closest test file
  first, then the full suite when practical.

## Definition of done

- [ ] Focused diff with tests.
- [ ] `python3 scripts/validate.py` passes.
- [ ] Relevant `unittest` files pass (full suite when feasible).
- [ ] `python3 scripts/prepare.py --check-only --source /path/to/darling-src` when a patch or pin changed.
- [ ] No generated/local artifacts; commit signed off with `-s`.
