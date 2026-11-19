# Scripts

Host-side entry points for darling-lol. The runtime copies under
`build/runtime/tools/` are rendered from `tools/` by `configure.py`; edit the
sources in `tools/` and `scripts/`, never the generated copies.

## Install, build and update

| Script | What it does |
|---|---|
| `install-native.sh` | Full native or `--toolbox` install: dependencies, build, runtime and optional desktop entries. Main installer and option reference. |
| `build.sh` | Preflight, configure, fetch/apply the pinned patches and build incrementally. |
| `install-runtime.sh` | Install the paired per-user runtime overlays; never copies game files. |
| `update.sh` | After `git pull`: re-apply changed patches, rebuild and reinstall the runtime. |
| `installer.sh` | Run Riot's installer inside the prefix for one region code. |
| `uninstall.sh` | Remove Riot software from the prefix so the installer can start clean; destructive, keeps the Darling runtime. |
| `preflight.py` | Check host versions and build/runtime prerequisites; `RCLOL_SKIP_PREFLIGHT=1` bypasses it. |

## Launch and desktop integration

| Script | What it does |
|---|---|
| `run.sh` | Thin launch wrapper: takes no arguments, optionally uses `gamemoderun`, delegates to the rendered `run-riot-client.sh`. |
| `desktop-run.sh` | Menu entry: logs to `build/desktop-launch.log`, then runs `run.sh`. |
| `launch-shortcut.sh` | Menu first run: installs the client when missing, otherwise hands off to `desktop-run.sh`. |
| `create-desktop-entry.sh` | Non-interactive launcher, icon and opt-in GNOME window entries. |
| `create-shortcuts.sh` | Interactive shortcut manager: name, window entries, launch flags, icon and Toolbox target. |
| `open-guest-url.sh` | `riotclient://` handler for sign-in links opened on the host. |
| `hide-league-splash-window.sh` | Opt-in helper that unmaps the 512x216 League splash window. |

## Validation

| Script | What it does |
|---|---|
| `validate.py` | Audit tracked source, rendered Python/shell syntax, pinned revisions and patches. |
| `prepare.py` | Fetch pinned Darling sources and apply patches; `--check-only` validates applicability. |
| `patchkit.py` | Patch-stack status, hunk/manifest verification, single-file resplice and overlay install/rollback ledger. |
| `configure.py` | Render local paths, region and locale into `build/config.json` and `build/runtime/tools/*`. |
| `agent-context.py` | Print the `AGENTS.md` index and verify its paths, scripts and launch flags still exist. |

## Nix and rootless runtime

| Script | What it does |
|---|---|
| `darling-rootless.sh` | Enter the rootless Darling namespace as namespace root. |
| `fhs-entry.sh` | Re-enter as the ordinary namespace user with scoped capabilities. |
| `nix-session.sh` | Run setup and the client in the same rootless namespace. |
| `nix-state.py` | Check per-prefix rootless readiness without starting Darling. |
| `nix-stop.py` | `--stop`: stop the workspace session or its `darlingserver` safely. |
| `nix-lifecycle.py` | Hold namespace-local shutdown authority until the session exits. |
| `nix-idle.py` | Read-only maintenance preflight; never signals a process. |
| `nix-prefix.py` | `--set-prefix`: move or switch the guest prefix. |
| `nix-copy.py` | Refresh tracked package files without deleting workspace data. |
| `nix-compiler.sh` | Keep the clang driver name while choosing host or Darwin headers. |

## Shared modules

| File | What it does |
|---|---|
| `common.py` | `ROOT`, `REGIONS`, `LOCALES` and the config/render helpers used by `configure.py`, `prepare.py` and `validate.py`. |

Runtime launch logic, the session supervisor, `exec-guest.py` and the desktop
entry generator live in `tools/` and are documented in `AGENTS.md` and
`docs/`.
