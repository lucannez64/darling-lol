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
        binary_folder = folder / "bin"
        binary_folder.mkdir()
        log = folder / "calls.jsonl"
        stub = f'''#!{sys.executable}
import json, os, sys
record = {{"command": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}}
if record["command"] in ("darling", "python3"):
    record["flags"] = {{key: os.environ.get(key) for key in
        ("RCLOL_APP_ARG3", "RCLOL_APP_ARG3_SLOT", "RCLOL_UX_ARG2", "RCLOL_UX_ARG2_SLOT", "RCLOL_UX_ARG1", "RCLOL_APP_ARGS", "RCLOL_APP_ARG2", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DARLING_XSHM_LIBRARY", "MANGOHUD", "DISABLE_MANGOHUD")}}
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
        subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True)
        records = [json.loads(line) for line in log.read_text().splitlines()]
        maintenance = [r for r in records if r["command"] == "python3"]
        assert [Path(r["args"][0]).name for r in maintenance] == [
            "runtime_paths.py", "ensure-darling-runtime.py", "ensure-darling-service-accounts.py", "ensure-league-runtime-layout.py",
            "ensure-darling-root-certificates.py",
        ], "unexpected automatic helper"
        launches = [r for r in records if r["command"] == "darling"]
        assert len(launches) == 1, "unexpected launch count"
        for record in records:
            assert record["flags"]["MANGOHUD"] is None, "inherited MangoHud activation survived"
            assert record["flags"]["DISABLE_MANGOHUD"] == "1", "MangoHud not disabled before a runtime helper"
        launch = launches[0]
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
        # An inherited injection must also be absent for first-time installation.
        env["RIOT_LAUNCHER_HOST"] = str(folder / "Install Riot.app/Contents/MacOS/RiotClientServices")
        env["DISABLE_MANGOHUD"] = "0"  # An inherited value cannot override launcher policy.
        subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True)
        install = json.loads(log.read_text().splitlines()[-1])
        assert install["args"][0] == env["RIOT_LAUNCHER_HOST"]
        assert "--skip-to-install" in install["args"]
        assert not any(arg.startswith("--app-root=") for arg in install["args"])
        assert install["flags"]["DYLD_INSERT_LIBRARIES"] is None
        assert install["flags"]["MANGOHUD"] is None
        assert install["flags"]["DISABLE_MANGOHUD"] == "1"
        env.pop("RIOT_LAUNCHER_HOST")
        env.pop("DYLD_INSERT_LIBRARIES")
        subprocess.run(["sh", str(launcher)], env=env, check=True, capture_output=True, text=True)
        clean = json.loads(log.read_text().splitlines()[-1])
        assert clean["flags"]["DYLD_INSERT_LIBRARIES"] is None
        assert clean["flags"]["MANGOHUD"] is None
        assert clean["flags"]["DISABLE_MANGOHUD"] == "1"
        print("LAUNCHER_OFFLINE pass=1 installed=PASS installer=PASS inherited_injection=absent mangohud=disabled runtime=preserved")


if __name__ == "__main__":
    check()
