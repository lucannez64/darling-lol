#!/usr/bin/env python3
"""Install the tested reclamation library atomically, without restarting clients."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from runtime_paths import runtime_paths
from file_digest import sha256

ROOT = Path(__file__).resolve().parents[1]
# The optimized relink from tools/build-riot-kqueue-runtime.py is the library the
# client runtime is supposed to run; the plain CMake build (no -O flag) is not.
DEFAULT_ARTIFACT = ROOT / "build/riot-libsystem_kernel.dylib"


def digest(path):
    return sha256(path)


def install(binary, destinations, backup_folder):
    with binary.open("rb") as input:
        if input.read(4) != b"\xcf\xfa\xed\xfe":
            raise ValueError("Expected the tested 64-bit Mach-O kernel library")
    version = re.match(r"(\d+)\.(\d+)", os.uname().release)
    if not version or tuple(map(int, version.groups())) < (5, 14):
        raise ValueError("The reuse translation requires Linux 5.14 or newer")
    expected = digest(binary)
    records, staged = [], []
    # Prepare every backup and replacement before changing any destination.
    try:
        for index, destination in enumerate(destinations):
            if destination.is_symlink() or not destination.is_file():
                raise ValueError("Expected a regular installed library: " + str(destination))
            metadata = destination.stat()
            old_hash = digest(destination)
            if old_hash == expected:
                records.append({"path": str(destination), "already_current": True})
                continue
            backup = backup_folder / (str(index) + "-" + old_hash + ".dylib")
            backup_folder.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                if digest(backup) != old_hash:
                    raise ValueError("The existing backup has unexpected contents")
            else:
                shutil.copy2(destination, backup)
            fd, temporary = tempfile.mkstemp(prefix=".libsystem_kernel-staged-", dir=destination.parent)
            staged.append((Path(temporary), destination))
            with os.fdopen(fd, "wb") as output, binary.open("rb") as input:
                shutil.copyfileobj(input, output)
                output.flush()
                os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
                os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
                os.fsync(output.fileno())
            if digest(Path(temporary)) != expected:
                raise ValueError("The staged library does not match the build")
            records.append({"path": str(destination), "backup": str(backup)})
        for temporary, destination in staged:
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        if any(digest(path) != expected for path in destinations):
            raise ValueError("Installed library verification failed")
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()
    return {"installed": True, "sha256": expected, "libraries": records,
            "restart_performed": False,
            "activation": "Exit the game and clients, then restart Darling; an active overlay may still expose the old library."}


def prefix_destinations(config, binary, artifact):
    """Destination list for an installation, and a note about the artifact.

    The prefix copies may be replaced from any tested source, but the prepared
    artifact is never a destination of a different library: a run that does not
    install the artifact itself leaves it untouched. That is what keeps the
    optimized relink from being silently replaced by the plain CMake build.
    """
    prefix, guest_home = runtime_paths(config)
    destinations = [
        guest_home / "RuntimeLibraries/libsystem_kernel.dylib",
        # Keep the per-prefix bootstrap copy consistent; leave the system SDK alone.
        prefix / "usr/lib/system/libsystem_kernel.dylib",
    ]
    if not artifact.exists():
        return destinations, "the prepared artifact " + str(artifact) + " does not exist yet"
    if Path(binary).resolve() != artifact.resolve():
        return destinations, ("refusing to overwrite " + str(artifact)
                              + " with a different library; rebuild it with "
                                "build/runtime/tools/build-riot-kqueue-runtime.py")
    destinations.append(artifact)
    return destinations, None


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    # A previous sudo server install can make build/backups root-owned.
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "build/memory-runtime-backups")
    return parser, parser.parse_args(argv)


def main():
    parser, args = parse_arguments()
    if not args.binary.is_file():
        parser.exit(1, "The library to install does not exist: " + str(args.binary) + "; build the "
                       "optimized relink first: python3 build/runtime/tools/build-riot-kqueue-runtime.py\n")
    config = json.loads((ROOT / "build/config.json").read_text())
    destinations, note = prefix_destinations(config, args.binary, args.artifact)
    if note:
        print("note:", note)
    try:
        result = install(args.binary, destinations, args.backup_dir)
    except PermissionError as error:
        parser.exit(1, "Cannot write " + str(error.filename) + "; choose a writable --backup-dir or run the installer with sudo.\n")
    result["artifact"] = str(args.artifact)
    result["artifact_replaced"] = any(Path(record["path"]).resolve() == args.artifact.resolve()
                                      for record in result["libraries"] if not record.get("already_current"))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
