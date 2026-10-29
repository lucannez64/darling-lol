#!/usr/bin/env python3
"""Install the optimized libc, libc++ and libsystem_platform overlays.

The guest finds a library by leaf name in DYLD_LIBRARY_PATH before its install
path (dyld3/PathOverrides.cpp:forEachPathVariant), so a file in the per-user
RuntimeLibraries directory replaces the copy in the prefix and in the installed
SDK without touching either. The overlays installed here are exactly the
artifacts built by tools/build-riot-*.py; each is staged next to its
destination, verified against the built file, and renamed over the old one
after the old one is backed up. --rollback restores every file this installer
replaced and removes the ones it created.

This installer was checked offline (tools/riot-libs-installer-offline-check.py)
and is never run against the real prefix by the build. Nothing is activated
until the guest processes are restarted.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from runtime_paths import validate_runtime_paths
from file_digest import sha256

ROOT = Path(__file__).resolve().parents[1]

# (built artifact, file name in the guest's RuntimeLibraries directory). The
# artifacts are the ones tools/build-riot-libs-runtime.sh builds.
# libsystem_m is deliberately absent: its C sources are built at -O0 upstream and an
# -O2 rebuild made the Riot client UI render a black window (2026-10-13).
LIBRARIES = [
    ("build/riot-libsystem_c.dylib", "libsystem_c.dylib"),
    ("build/riot-libc++.1.dylib", "libc++.1.dylib"),
    ("build/riot-libc++abi.dylib", "libc++abi.dylib"),
    ("build/riot-libsystem_platform.dylib", "libsystem_platform.dylib"),
]


def digest(path):
    return sha256(path)


def destinations(prefix, guest_home):
    folder = Path(prefix) / guest_home.lstrip("/") / "RuntimeLibraries"
    return [(ROOT / artifact, folder / name) for artifact, name in LIBRARIES]


def existing_records(backup_folder):
    """What the previous installation recorded, so a second run keeps the
    state from before the first one instead of overwriting it."""
    manifest = backup_folder / "manifest.json"
    if not manifest.is_file():
        return {}
    return {record["path"]: record for record in json.loads(manifest.read_text())["libraries"]}


def install(libraries, backup_folder):
    previous_records = existing_records(backup_folder)
    expected = {}
    for artifact, destination in libraries:
        if not artifact.is_file():
            raise ValueError("The artifact does not exist: " + str(artifact))
        with artifact.open("rb") as input:
            if input.read(4) not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
                raise ValueError("Expected a 64-bit Mach-O library: " + str(artifact))
        expected[destination] = digest(artifact)
    records, staged = [], []
    try:
        for index, (artifact, destination) in enumerate(libraries):
            earlier = previous_records.get(str(destination))
            current = digest(destination) if destination.is_file() else None
            if current == expected[destination]:
                # keep the state from before the first installation, if any
                records.append({"path": str(destination), "already_current": True,
                                "existed": earlier["existed"] if earlier else destination.is_file(),
                                "previous_sha256": earlier["previous_sha256"] if earlier else current,
                                "backup": earlier.get("backup") if earlier else None,
                                "sha256": expected[destination]})
                continue
            existed = earlier["existed"] if earlier else destination.is_file()
            previous = earlier["previous_sha256"] if earlier else current
            backup = Path(earlier["backup"]) if earlier and earlier.get("backup") else None
            if backup is None and current is not None:
                backup_folder.mkdir(parents=True, exist_ok=True)
                backup = backup_folder / ("%d-%s.dylib" % (index, current))
                if backup.exists():
                    if digest(backup) != current:
                        raise ValueError("The existing backup has unexpected contents: " + str(backup))
                else:
                    shutil.copy2(destination, backup)
            metadata = destination.stat() if destination.is_file() else None
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name + "-staged-",
                                                     dir=destination.parent)
            staged.append((Path(temporary), destination))
            with os.fdopen(descriptor, "wb") as output, artifact.open("rb") as input:
                shutil.copyfileobj(input, output)
                output.flush()
                if metadata is not None:
                    os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
                    os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
                else:
                    os.fchmod(output.fileno(), 0o755)
                os.fsync(output.fileno())
            if digest(Path(temporary)) != expected[destination]:
                raise ValueError("The staged library does not match the build: " + str(artifact))
            records.append({"path": str(destination), "backup": str(backup) if backup else None,
                            "existed": existed, "sha256": expected[destination],
                            "previous_sha256": previous})
        for temporary, destination in staged:
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        for record in records:
            path = Path(record["path"])
            if not path.is_file() or digest(path) != record["sha256"]:
                raise ValueError("Installed library verification failed: " + str(path))
        backup_folder.mkdir(parents=True, exist_ok=True)
        manifest = backup_folder / "manifest.json"
        manifest.write_text(json.dumps({"libraries": records}, indent=1) + "\n")
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()
    return {"installed": True, "libraries": records, "restart_performed": False,
            "manifest": str(backup_folder / "manifest.json"),
            "activation": "Exit the game and the clients, then start them again; a running guest keeps the library it already loaded."}


def rollback(backup_folder):
    manifest = backup_folder / "manifest.json"
    if not manifest.is_file():
        raise ValueError("No installation manifest in " + str(backup_folder))
    records = json.loads(manifest.read_text())["libraries"]
    restored, removed = [], []
    for record in reversed(records):
        destination = Path(record["path"])
        if record.get("previous_sha256") is None:
            # the installer created this file; removing it restores the SDK copy
            if destination.is_file() and digest(destination) == record["sha256"]:
                destination.unlink()
                removed.append(str(destination))
        else:
            backup = Path(record["backup"])
            if not backup.is_file():
                raise ValueError("The backup is missing: " + str(backup))
            if digest(backup) != record["previous_sha256"]:
                raise ValueError("The backup has unexpected contents: " + str(backup))
            descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name + "-restore-",
                                                     dir=destination.parent)
            with os.fdopen(descriptor, "wb") as output, backup.open("rb") as input:
                shutil.copyfileobj(input, output)
                output.flush()
                os.fchmod(output.fileno(), 0o755)
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            restored.append(str(destination))
    return {"rolled_back": True, "restored": restored, "removed": removed,
            "activation": "Exit the game and the clients, then start them again."}


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=None,
                        help="guest prefix (default: build/config.json)")
    parser.add_argument("--guest-home", default=None)
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "build/libs-runtime-backups")
    parser.add_argument("--only", default=None,
                        help="comma-separated library names to install instead of all of them "
                             "(to isolate a faulty overlay; run --rollback before changing the set)")
    parser.add_argument("--rollback", action="store_true",
                        help="restore every file this installer replaced and remove the ones it created")
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, arguments = parse_arguments(argv)
    if arguments.rollback:
        try:
            print(json.dumps(rollback(arguments.backup_dir)))
        except (ValueError, OSError) as error:
            parser.exit(1, str(error) + "\n")
        return
    config = json.loads((ROOT / "build/config.json").read_text())
    prefix = arguments.prefix or Path(os.environ.get("DPREFIX") or config["prefix"])
    if arguments.guest_home:
        host_guest_home = prefix / arguments.guest_home.lstrip("/")
    elif os.environ.get("RIOT_GUEST_HOME"):
        host_guest_home = Path(os.environ["RIOT_GUEST_HOME"])
    else:
        host_guest_home = prefix / config["guest_home"].lstrip("/")
    try:
        prefix, host_guest_home = validate_runtime_paths(prefix, host_guest_home)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
    libraries = destinations(host_guest_home, "")
    if arguments.only:
        wanted = set(arguments.only.split(","))
        unknown = wanted - {name for _, name in LIBRARIES}
        if unknown:
            parser.exit(1, "Unknown library: " + ", ".join(sorted(unknown)) + "\n")
        libraries = [(artifact, destination) for artifact, destination in libraries
                     if destination.name in wanted]
    for artifact, _ in libraries:
        if not artifact.is_file():
            parser.exit(1, "The library to install does not exist: " + str(artifact)
                        + "; build it first: sh build/runtime/tools/build-riot-libs-runtime.sh\n")
    try:
        result = install(libraries, arguments.backup_dir)
    except PermissionError as error:
        parser.exit(1, "Cannot write " + str(error.filename) + "; choose a writable --backup-dir or run "
                       "the installer with sudo.\n")
    result["prefix"] = str(prefix)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
