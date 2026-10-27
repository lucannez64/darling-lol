#!/usr/bin/env python3
"""Check launch behavior using stub commands; never start or contact a client."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def check():
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="riot-launcher-offline-", dir=ROOT / "build") as directory:
        folder = Path(directory)
        launcher = folder / "launch.sh"
        launcher.write_text((ROOT / "tools/run-riot-client.sh").read_text().replace("@GUEST_HOME@", "/Users/offline-check"))
        (folder / "exec-guest.py").write_text((ROOT / "tools/exec-guest.py").read_text())
        binary_folder = folder / "bin"
        binary_folder.mkdir()
        log = folder / "calls.jsonl"
        stub = f'''#!{sys.executable}
import json, os, sys
record = {{"command": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}}
# The launcher hands the guest to the session supervisor before exec-guest.py.
# Run the supervised command without recording the supervisor itself, so the
# existing assertions still inspect the guest launch that follows.
if record["command"] == "python3" and sys.argv[1:] and os.path.basename(sys.argv[1]) == "session-supervisor.py":
    remainder = sys.argv[2:]
    if "--probe-runtime" in remainder:
        with open({str(log)!r}, "a") as output:
            output.write(json.dumps({{"command": "runtime-probe"}}) + "\\n")
        print("stopped")
        sys.exit(0)
    if "--" in remainder:
        assert remainder[remainder.index("--runtime-at-start") + 1] == "stopped"
        command = remainder[remainder.index("--") + 1:]
        if command and command[0] == "python3":
            os.execv(sys.executable, [sys.executable] + command[1:])
# The launcher starts the guest through exec-guest.py; run it for real.
if record["command"] == "python3" and sys.argv[1:] and os.path.basename(sys.argv[1]) == "exec-guest.py":
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
if record["command"] == "darling":
    # Descriptors open now were inherited; the one listdir used is closed again before this check.
    def is_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False
    record["fds"] = sorted(fd for fd in map(int, os.listdir("/proc/self/fd")) if is_open(fd))
    record["flags"] = {{key: os.environ.get(key) for key in
        ("RCLOL_APP_ARG3", "RCLOL_APP_ARG3_SLOT", "RCLOL_UX_ARG2", "RCLOL_UX_ARG2_SLOT", "RCLOL_UX_ARG1", "RCLOL_APP_ARGS", "RCLOL_APP_ARG2", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DARLING_XSHM_LIBRARY",
         "WAYLAND_DISPLAY", "XAUTHORITY", "DRI_PRIME", "MESA_NO_ERROR", "__GL_THREADED_OPTIMIZATIONS", "SHELL")}}
record["hud"] = {{key: os.environ.get(key) for key in ("MANGOHUD", "DISABLE_MANGOHUD")}}
with open({str(log)!r}, "a") as output:
    output.write(json.dumps(record) + "\\n")
'''
        for name in ("python3", "darling"):
            path = binary_folder / name
            path.write_text(stub)
            path.chmod(0o755)
        env = dict(os.environ)
        env.pop("RIOT_LAUNCHER_HOST", None)
        env.pop("RCLOL_LAUNCH_PRODUCT", None)
        env["PATH"] = str(binary_folder) + os.pathsep + env.get("PATH", "")
        prefix = folder / "prefix"
        (prefix / "Applications/Riot Client.app").mkdir(parents=True)
        env["DPREFIX"] = str(prefix)
        env.pop("RIOT_GUEST_HOME", None)
        (folder / "runtime-paths.sh").write_text(
            (ROOT / "tools/runtime-paths.sh").read_text()
            .replace("@DPREFIX@", str(prefix)).replace("@GUEST_HOME@", "/Users/offline-check"))
        env["DYLD_INSERT_LIBRARIES"] = "/Users/offline-check/zzlogdir.dylib:/tmp/inherited.dylib"
        env["RCLOL_APP_ARGS"] = "--inherited-shim-argument"
        env["RCLOL_APP_ARG2"] = "--inherited-shim-argument-2"
        env["MANGOHUD"] = "1"
        env.pop("DISABLE_MANGOHUD", None)
        env["RCLOL_NATIVE_VPX"] = "1"
        env["RCLOL_LEAGUE_UX_TUNING"] = "1"
        env["XDG_RUNTIME_DIR"] = "/run/user/offline-check"
        env.pop("RCLOL_DISCORD_RPC", None)
        # Inherited legacy settings must not re-enable inspection or unlimited frames.
        env.update({
            "RCLOL_APP_ARG3": "--inspect=127.0.0.1:9229",
            "RCLOL_APP_ARG3_SLOT": "--session-id=",
            "RCLOL_UX_ARG2": "--disable-frame-rate-limit",
            "RCLOL_UX_ARG2_SLOT": "--crash-environment=",
            "RCLOL_UNTHROTTLED_BEGIN_FRAMES": "1",
            "RCLOL_FOREGROUND_SCHEDULING": "1",
            "RCLOL_LEAGUE_UPDATE_HANDOFF": "1",
            "RCLOL_OPEN_URL_BRIDGE": "1",
            "RCLOL_DISABLE_GPU_VSYNC": "1",
        })
        # Leaked host descriptors, including multi-digit ones, must not reach the guest.
        leaked = list(os.pipe())
        for target in (12, 200):
            leaked.append(os.dup2(leaked[0], target, inheritable=False))
        try:
            subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True, pass_fds=leaked)
        finally:
            for fd in leaked:
                os.close(fd)
        records = [json.loads(line) for line in log.read_text().splitlines()]
        probe = next(i for i, record in enumerate(records) if record["command"] == "runtime-probe")
        roots = next(i for i, record in enumerate(records)
                     if record["command"] == "python3" and
                     Path(record["args"][0]).name == "ensure-darling-root-certificates.py")
        assert probe < roots, "runtime ownership was captured after certificate startup"
        maintenance = [r for r in records if r["command"] == "python3"]
        assert [Path(r["args"][0]).name for r in maintenance] == [
            "runtime_paths.py", "ensure-darling-runtime.py", "ensure-darling-service-accounts.py", "ensure-league-runtime-layout.py",
            "ensure-darling-root-certificates.py",
        ], "unexpected automatic helper"
        launches = [r for r in records if r["command"] == "darling"]
        assert len(launches) == 1, "unexpected launch count"
        for record in records:
            if record["command"] == "runtime-probe":
                continue
            assert record["hud"] == {"MANGOHUD": None, "DISABLE_MANGOHUD": "1"}, "MangoHud survived before a runtime helper"
        launch = launches[0]
        assert launch["fds"] == [0, 1, 2], f"inherited descriptors reached the guest: {launch['fds']}"
        assert all(launch["flags"][key] is None for key in (
            "RCLOL_APP_ARG3", "RCLOL_APP_ARG3_SLOT", "RCLOL_UX_ARG2", "RCLOL_UX_ARG2_SLOT"
        )), "inherited inspector/unlimited-frame argument survived"
        assert all(launch["flags"][key] is None for key in (
            "DYLD_INSERT_LIBRARIES", "RCLOL_APP_ARGS", "RCLOL_APP_ARG2", "RCLOL_UX_ARG1"
        )), "injection or former shim settings survived"
        assert all(launch["flags"][key] for key in (
            "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DARLING_XSHM_LIBRARY"
        )), "Darling runtime overrides lost"
        assert "--launch-product=league_of_legends" in launch["args"], "product launch lost"
        assert "--app-root=/Applications/Riot Client.app" in launch["args"], "application root lost"
        assert "--log-dir=/Users/offline-check/Library/Logs/Riot Games/Riot Client" in launch["args"], "absolute log directory lost"
        # Discord Rich Presence: the guest's XDG_RUNTIME_DIR must lead out to the host socket.
        assert os.readlink(prefix / "run/user/offline-check/discord-ipc-0") == \
            "/Volumes/SystemRoot/run/user/offline-check/discord-ipc-0", "Discord IPC link missing"
        # Opting out removes the links an earlier launch left behind.
        subprocess.run(["sh", str(launcher)], env={**env, "RCLOL_DISCORD_RPC": "0"}, check=True, capture_output=True, text=True)
        assert not os.path.lexists(prefix / "run/user/offline-check/discord-ipc-0"), "stale Discord IPC link kept"
        # An inherited injection must also be absent for first-time installation.
        env["RIOT_LAUNCHER_HOST"] = str(folder / "Install Riot.app/Contents/MacOS/RiotClientServices")
        env["DISABLE_MANGOHUD"] = "0"  # Inherited settings cannot override launcher policy.
        subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True)
        install = json.loads(log.read_text().splitlines()[-1])
        assert install["args"][0] == env["RIOT_LAUNCHER_HOST"]
        assert "--skip-to-install" in install["args"]
        assert not any(arg.startswith("--app-root=") for arg in install["args"])
        assert install["flags"]["DYLD_INSERT_LIBRARIES"] is None
        assert install["hud"] == {"MANGOHUD": None, "DISABLE_MANGOHUD": "1"}
        env.pop("RIOT_LAUNCHER_HOST")
        env.pop("DYLD_INSERT_LIBRARIES")
        subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True)
        clean = json.loads(log.read_text().splitlines()[-1])
        assert clean["flags"]["DYLD_INSERT_LIBRARIES"] is None
        assert clean["hud"] == {"MANGOHUD": None, "DISABLE_MANGOHUD": "1"}
        # The opt-in host environment filter drops session metadata but never the launcher's GPU tuning.
        env.update({"RCLOL_FILTER_HOST_ENV": "1", "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "/tmp/xauth",
                    "DRI_PRIME": "1", "SHELL": "/bin/fish"})
        env.pop("MESA_NO_ERROR", None)
        env.pop("__GL_THREADED_OPTIMIZATIONS", None)
        subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True)
        filtered = json.loads(log.read_text().splitlines()[-1])["flags"]
        assert filtered["WAYLAND_DISPLAY"] is None and filtered["SHELL"] == "/bin/zsh", "host session metadata survived"
        assert (filtered["XAUTHORITY"], filtered["DRI_PRIME"]) == ("/tmp/xauth", "1"), "X11/GPU selection lost"
        assert (filtered["MESA_NO_ERROR"], filtered["__GL_THREADED_OPTIMIZATIONS"]) == ("1", "0"), "launcher GPU tuning lost"
        print("LAUNCHER_OFFLINE pass=1 installed=PASS installer=PASS inherited_injection=absent runtime=preserved inherited_fds=closed host_env_filter=PASS mangohud=disabled discord_ipc=linked")


if __name__ == "__main__":
    check()
