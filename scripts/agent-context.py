#!/usr/bin/env python3
"""Print and validate the cross-agent context index.

AGENTS.md is the canonical entrypoint for coding agents.  This helper gives the
same index to a terminal, exports it as JSON for other harnesses, and checks
that every file, script and launch flag it names still exists.  It never runs
project commands or touches the prefix.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = "AGENTS.md"
ADAPTERS = ("CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md")

# (topic, read when, paths)
READS = (
    ("Scope, compliance and identity",
     "before user-facing, security or platform-identity changes",
     ("docs/COMPLIANCE.md", "docs/INTEROPERABILITY.md", "docs/PLATFORM_IDENTITY.md")),
    ("Setup, launch and flags",
     "before changing build, install, launch or environment behavior",
     ("docs/SETUP.md", "docs/TROUBLESHOOTING.md", "scripts/run.sh",
      "tools/run-riot-client.sh")),
    ("Scripts overview",
     "before changing or adding a host entry point",
     ("scripts/README.md",)),
    ("Tests and probes",
     "before writing or running tests",
     ("docs/TESTING.md", "tests", ".github/workflows/source-check.yml")),
    ("Current status",
     "when you need what works or what was measured",
     ("docs/STATUS.md", "docs/COMPATIBILITY.md")),
    ("Nix and rootless runtime",
     "before touching the flake or the namespace lifecycle",
     ("docs/NIX.md", "docs/PACKAGING.md", "flake.nix", "scripts/nix-state.py")),
    ("Patches and upstream",
     "before changing a patch or a pinned revision",
     ("patches", "upstream/manifest.json", "scripts/prepare.py")),
    ("Desktop integration",
     "before changing shortcuts, icons or GNOME window entries",
     ("scripts/create-shortcuts.sh", "scripts/create-desktop-entry.sh",
      "tools/desktop-entry.py")),
    ("History and reviews",
     "only for background; these notes may be stale",
     ("docs/notes", "docs/reviews", "research/README.md")),
)

# (path, summary)
SCRIPTS = (
    ("scripts/install-native.sh", "Install dependencies, build and install the runtime; native or `--toolbox`"),
    ("scripts/build.sh", "Preflight, configure, apply pinned patches and build incrementally"),
    ("scripts/prepare.py", "Fetch pinned Darling sources and apply patches; `--check-only` validates them"),
    ("scripts/patchkit.py", "Patch-stack status, hunk/manifest verification, single-file resplice and overlay ledger"),
    ("scripts/configure.py", "Render local paths, region and locale into `build/config.json` and `build/runtime/tools/*`"),
    ("scripts/install-runtime.sh", "Install the paired per-user runtime overlays; never copies game files"),
    ("scripts/update.sh", "After `git pull`: re-apply changed patches, rebuild and reinstall the runtime"),
    ("scripts/run.sh", "Thin launcher: no arguments, optional `gamemoderun`, delegates to `run-riot-client.sh`"),
    ("scripts/installer.sh", "Run Riot's installer inside the prefix for one region code"),
    ("scripts/uninstall.sh", "Remove Riot software from the prefix; destructive; keeps the Darling runtime"),
    ("scripts/preflight.py", "Check host versions and build/runtime prerequisites"),
    ("scripts/validate.py", "Audit tracked source, rendered shell/Python syntax, pinned revisions and patches"),
    ("scripts/create-shortcuts.sh", "Interactive shortcut manager; calls the rendered `desktop-entry.py`"),
    ("scripts/create-desktop-entry.sh", "Non-interactive launcher, icon and opt-in window entries"),
    ("scripts/desktop-run.sh", "Menu entry: log output to `build/desktop-launch.log`, then `run.sh`"),
    ("scripts/launch-shortcut.sh", "Menu first run: install the client when missing, else `desktop-run.sh`"),
    ("scripts/open-guest-url.sh", "Handle `riotclient://` sign-in links for the running guest"),
    ("scripts/hide-league-splash-window.sh", "Opt-in helper that unmaps the 512x216 League splash window"),
    ("scripts/darling-rootless.sh", "Enter the rootless Darling namespace as namespace root"),
    ("scripts/fhs-entry.sh", "Re-enter as the ordinary namespace user with scoped capabilities"),
    ("scripts/nix-compiler.sh", "Keep the clang driver name while choosing host or Darwin headers"),
    ("scripts/nix-copy.py", "Refresh tracked package files without deleting workspace data"),
    ("scripts/nix-idle.py", "Read-only maintenance preflight; never signals a process"),
    ("scripts/nix-lifecycle.py", "Hold namespace-local shutdown authority until the session exits"),
    ("scripts/nix-prefix.py", "`--set-prefix`: move or switch the guest prefix"),
    ("scripts/nix-session.sh", "Run setup and the client in the same rootless namespace"),
    ("scripts/nix-state.py", "Check per-prefix rootless readiness without starting Darling"),
    ("scripts/nix-stop.py", "`--stop`: stop the workspace session or its darlingserver safely"),
    ("tools/run-riot-client.sh", "Real launch logic: profile, GPU/env, helpers, client arguments, supervisor"),
    ("tools/session-supervisor.py", "Supervise the guest session; idle stop, clean exit, scoped cleanup"),
    ("tools/exec-guest.py", "Filter host environment, close inherited FDs, start the guest"),
    ("tools/desktop-entry.py", "Generate launcher, icon and window entries; rendered into `build/runtime/tools`"),
    ("tools/runtime-package.py", "Export, inspect and install deterministic runtime overlay archives"),
    ("tools/doctor.py", "Host and prefix diagnostics used by `darling-lol --doctor`"),
    ("tools/perf-baseline.py", "Collect the baseline data used in `docs/COMPATIBILITY.md`"),
)

# (name, default, effect)
FLAGS = (
    ("RCLOL_REGION", "configured region", "Riot server region passed as `--region`"),
    ("RCLOL_LOCALE", "configured locale", "client language passed as `--locale`"),
    ("RCLOL_PLATFORM_PROFILE", "`compat`", "hardware presentation: `compat` or `host`"),
    ("RCLOL_GAMEMODE", "`auto`", "run under `gamemoderun` when installed; `0` disables"),
    ("RCLOL_CLEAN_ON_EXIT", "`1`", "`0` keeps the runtime running after the clients exit"),
    ("RCLOL_STOP_AFTER_CLIENT_SECONDS", "`60`", "stop after the last client window with no game"),
    ("RCLOL_STOP_TOOLBOX", "`1`", "`0` leaves the Toolbox container running after the session"),
    ("RCLOL_CDN_IPV4_ONLY", "`1`", "`0` keeps the upstream IPv6 resolver behavior"),
    ("RCLOL_RECOVER_STALE_CLIENT", "`0`", "opt-in recovery of a windowless client tree"),
    ("RCLOL_RECOVER_HIDDEN_CLIENT", "`0`", "opt-in recovery of a hidden default-prefix session"),
    ("RCLOL_CLEAN_STALE_HELPERS", "`0`", "remove orphaned in-game helpers before launch"),
    ("RCLOL_CLIENT_IN_GAME_TRICKS", "`0`", "close or hide the client during a match from preferences"),
    ("RCLOL_CLOSE_CLIENT_IN_GAME", "`0`", "low-level close helper"),
    ("RCLOL_HIDE_CLIENT_IN_GAME", "`0`", "low-level hide helper"),
    ("RCLOL_HIDE_RIOT_CLIENT_IN_GAME", "`0`", "also hide the Riot Client with the close helper"),
    ("RCLOL_HIDE_LEAGUE_SPLASH", "`0`", "hide the 512x216 splash window"),
    ("RCLOL_DISCORD_RPC", "`1`", "`0` removes the Discord IPC links"),
    ("RCLOL_FILTER_HOST_ENV", "`0`", "drop host desktop-session variables before the guest"),
    ("RCLOL_KEEP_HOST_ENV", "empty", "names kept by `RCLOL_FILTER_HOST_ENV`"),
    ("RCLOL_FINISH_LEAGUE_EXIT", "`1`", "`0` disables the League exit finish workaround"),
    ("RCLOL_FINISH_LEAGUE_LOAD", "`1`", "`0` disables the League load finish workaround"),
    ("RCLOL_LAUNCH_PRODUCT", "`league_of_legends`", "empty starts the bare client"),
    ("RCLOL_LAUNCH_PATCHLINE", "`live`", "patchline passed to the client"),
    ("RCLOL_GUEST_LOCALE", "`en_US.UTF-8`", "guest `LANG`/`LC_ALL`"),
    ("RCLOL_REFRESH_HZ", "detected (60 fallback)", "display link and frame pacing"),
    ("RCLOL_CORE_DUMPS", "`0`", "`1` keeps core dumps"),
    ("DSERVER_PCORE_AFFINITY", "unset", "`1` pins darlingserver threads to performance cores"),
)

SOURCES = (
    "scripts/run.sh",
    "tools/run-riot-client.sh",
    "tools/session-supervisor.py",
    "tools/exec-guest.py",
    "patches/darlingserver.patch",
)

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+[^)]*)?\)")
AGENT_IMPORT = re.compile(r"(?<![\w`])@([\w./-]+)")
FLAG_ROW = re.compile(
    r"^\|\s*`([A-Za-z0-9_]+)`\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$",
    re.MULTILINE,
)
COMMENT_LINE = re.compile(r"^\s*(?:#|//|/\*|\*|<!--)")


def _resolve_target(base, target):
    """Resolve a link target relative to the file that contains it."""
    target = target.split("#", 1)[0].strip()
    if not target or "://" in target or target.startswith("mailto:"):
        return None
    path = Path(target)
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def _points_to_entrypoint(adapter, text, root):
    """Return True when the adapter links to or imports the root AGENTS.md."""
    expected = (root / ENTRYPOINT).resolve()
    targets = MARKDOWN_LINK.findall(text) + AGENT_IMPORT.findall(text)
    return any(_resolve_target(adapter.parent, target) == expected
               for target in targets)


def _read_utf8(path, errors, label, display=None):
    """Read a repository text file, reporting decoding failures with its path."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"{label} is not valid UTF-8: {display or path}")
        return None


def _code_without_comments(text, path):
    """Drop comment-only lines so prose mentions are not mistaken for reads."""
    lines = []
    patch = path.suffix == ".patch"
    for line in text.splitlines():
        code = line
        if patch:
            if code.startswith("-"):
                continue
            if code[:1] in "+ ":
                code = code[1:]
        if COMMENT_LINE.match(code):
            continue
        lines.append(code)
    return "\n".join(lines)


def _reads_flag(name, text):
    """Return True when text reads or assigns the exact environment variable."""
    escaped = re.escape(name)
    patterns = (
        rf"\$\{{?{escaped}(?![A-Za-z0-9_])",
        rf"(?m)^\s*(?:export\s+|local\s+|readonly\s+)?{escaped}=",
        rf"[\(\[]\s*['\"]{escaped}['\"]",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _flag_entries(markdown):
    """Return the launch-flag table entries from a Markdown index."""
    marker = "## Launch flags"
    if marker not in markdown:
        return []
    section = markdown.split(marker, 1)[1]
    return [(match.group(1), match.group(2).strip(), match.group(3).strip())
            for match in FLAG_ROW.finditer(section)]


def _check_flag_entries(label, entries, flags, errors):
    """Compare one exported flag table with the canonical flag definitions."""
    expected = list(flags)
    expected_names = {name for name, _default, _effect in expected}
    present_names = {name for name, _default, _effect in entries}
    for name, default, effect in expected:
        if name not in present_names:
            errors.append(f"{label} does not list flag: {name}")
        elif (name, default, effect) not in entries:
            errors.append(f"{label} has an out-of-date entry for flag: {name}")
    for name, _default, _effect in entries:
        if name not in expected_names:
            errors.append(f"{label} lists unexpected flag: {name}")


def render(reads=READS, scripts=SCRIPTS, flags=FLAGS):
    """Return the context index as Markdown."""
    lines = ["## Read on demand", "", "| Read | When |", "|---|---|"]
    for topic, when, paths in reads:
        listed = ", ".join(f"`{path}`" for path in paths)
        lines.append(f"| {listed} | **{topic}**: {when}. |")
    lines += ["", "## Script map", "", "| Script | What it does |", "|---|---|"]
    for path, summary in scripts:
        lines.append(f"| `{path}` | {summary} |")
    lines += ["", "## Launch flags", "",
              "| Variable | Default | Effect |", "|---|---|---|"]
    for name, default, effect in flags:
        lines.append(f"| `{name}` | {default} | {effect} |")
    return "\n".join(lines) + "\n"


def as_dict(reads=READS, scripts=SCRIPTS, flags=FLAGS, adapters=ADAPTERS):
    """Return the context index as a JSON-serializable mapping."""
    return {
        "entrypoint": ENTRYPOINT,
        "adapters": list(adapters),
        "reads": [
            {"topic": topic, "when": when, "paths": list(paths)}
            for topic, when, paths in reads
        ],
        "scripts": [
            {"path": path, "summary": summary} for path, summary in scripts
        ],
        "flags": [
            {"name": name, "default": default, "effect": effect}
            for name, default, effect in flags
        ],
    }


def check(root=ROOT, reads=READS, scripts=SCRIPTS, flags=FLAGS,
          adapters=ADAPTERS, sources=SOURCES):
    """Return a list of harness errors; an empty list means it is consistent."""
    errors = []
    entrypoint = root / ENTRYPOINT
    text = ""
    entrypoint_text = None
    if entrypoint.is_file():
        entrypoint_text = _read_utf8(
            entrypoint, errors, "entrypoint", ENTRYPOINT)
        text = entrypoint_text or ""
    else:
        errors.append(f"missing entrypoint: {ENTRYPOINT}")

    for path in adapters:
        adapter = root / path
        if not adapter.is_file():
            errors.append(f"missing agent adapter: {path}")
            continue
        adapter_text = _read_utf8(adapter, errors, "agent adapter", path)
        if adapter_text is not None and not _points_to_entrypoint(
                adapter, adapter_text, root):
            errors.append(f"agent adapter does not point to {ENTRYPOINT}: {path}")

    if entrypoint_text is not None:
        references = set(re.findall(r"[\w./-]+", text))
        for topic, _when, paths in reads:
            for path in paths:
                if not (root / path).exists():
                    errors.append(f"missing context path for {topic}: {path}")
                elif path not in references:
                    errors.append(
                        f"{ENTRYPOINT} does not list context path: {path}")

        for path, summary in scripts:
            if not (root / path).is_file():
                errors.append(f"missing script: {path}")
            elif path not in references:
                errors.append(f"{ENTRYPOINT} does not list script: {path}")
            if not summary.strip():
                errors.append(f"empty script summary: {path}")

        _check_flag_entries(ENTRYPOINT, _flag_entries(text), flags, errors)

    source_text = []
    for path in sources:
        source = root / path
        if not source.is_file():
            errors.append(f"missing flag source: {path}")
            continue
        source_body = _read_utf8(source, errors, "flag source", path)
        if source_body is not None:
            source_text.append(_code_without_comments(source_body, source))
    source_code = "\n".join(source_text)

    _check_flag_entries("rendered Markdown index",
                        _flag_entries(render(reads, scripts, flags)),
                        flags, errors)
    json_flags = [(item["name"], item["default"], item["effect"])
                  for item in as_dict(reads, scripts, flags, adapters)["flags"]]
    _check_flag_entries("JSON index", json_flags, flags, errors)

    for name, _default, _effect in flags:
        if not _reads_flag(name, source_code):
            errors.append(f"documented flag is not read by the project: {name}")

    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="print the context index as JSON")
    parser.add_argument("--check", action="store_true",
                        help="verify that the harness references still exist")
    arguments = parser.parse_args(argv)

    if arguments.check:
        errors = check()
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"PASS: agent harness lists {len(READS)} reads, "
              f"{len(SCRIPTS)} scripts and {len(FLAGS)} flags")
        return 0
    if arguments.json:
        print(json.dumps(as_dict(), indent=2))
        return 0
    print("# Agent context\n")
    print(f"Canonical entrypoint: `{ENTRYPOINT}`\n")
    print(render(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
