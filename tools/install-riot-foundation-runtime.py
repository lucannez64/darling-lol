#!/usr/bin/env python3
"""Install the optimized Foundation overlay atomically, without restarting clients.

The guest resolves frameworks through DYLD_FRAMEWORK_PATH=/Users/<user>/RuntimeFrameworks,
so Foundation is added there the same way CoreFoundation/AppKit already are:
copy the SDK framework once, then replace Versions/C/Foundation with the built
binary. The kernel library from the same batch is installed by
tools/install-riot-memory-runtime.py (its default source is the rebuilt
build/riot-libsystem_kernel.dylib); this installer does not touch it.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from runtime_paths import runtime_paths
from file_digest import sha256

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = ROOT / "build/riot-Foundation"


def digest(path):
    return sha256(path)


def copy_framework(sdk_framework, framework):
    """Create the overlay framework from the SDK layout (Versions/C + links)."""
    framework.parent.mkdir(parents=True, exist_ok=True)
    staged = framework.parent / (framework.name + ".staged")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(sdk_framework, staged, symlinks=True)
    os.rename(staged, framework)


def install(artifact, sdk_framework, framework, backup_folder):
    with artifact.open("rb") as input:
        if input.read(4) != b"\xcf\xfa\xed\xfe":
            raise ValueError("Expected the tested 64-bit Mach-O Foundation library")
    expected = digest(artifact)
    records = []

    if not framework.exists():
        # No overlay yet: seed it from the SDK, then replace the binary.
        copy_framework(sdk_framework, framework)
        records.append({"path": str(framework), "created_from": str(sdk_framework)})

    binary = framework / "Versions/C/Foundation"
    if binary.is_symlink() or not binary.is_file():
        raise ValueError("Expected a regular installed Foundation binary: " + str(binary))

    metadata = binary.stat()
    old_hash = digest(binary)
    if old_hash == expected:
        records.append({"path": str(binary), "already_current": True})
        return {"installed": True, "sha256": expected, "libraries": records,
                "restart_performed": False,
                "activation": "Exit the game and clients, then restart Darling; an active overlay may still expose the old library."}

    backup_folder.mkdir(parents=True, exist_ok=True)
    backup = backup_folder / ("foundation-" + old_hash)
    if backup.exists():
        if digest(backup) != old_hash:
            raise ValueError("The existing backup has unexpected contents")
    else:
        shutil.copy2(binary, backup)

    fd, temporary = tempfile.mkstemp(prefix=".Foundation-staged-", dir=binary.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, artifact.open("rb") as input:
            shutil.copyfileobj(input, output)
            output.flush()
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fsync(output.fileno())
        if digest(staged) != expected:
            raise ValueError("The staged library does not match the build")
        os.replace(staged, binary)
        directory = os.open(binary.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if staged.exists():
            staged.unlink()

    if digest(binary) != expected:
        raise ValueError("Installed library verification failed")
    records.append({"path": str(binary), "backup": str(backup), "previous_sha256": old_hash})
    return {"installed": True, "sha256": expected, "libraries": records,
            "restart_performed": False,
            "activation": "Exit the game and clients, then restart Darling; an active overlay may still expose the old library."}


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "build/foundation-runtime-backups")
    return parser, parser.parse_args(argv)


def main():
    parser, args = parse_arguments()
    if not args.binary.is_file():
        parser.exit(1, "The library to install does not exist: " + str(args.binary) + "; build it first: "
                       "python3 build/runtime/tools/build-riot-foundation-runtime.py\n")
    config = json.loads((ROOT / "build/config.json").read_text())
    _, guest_home = runtime_paths(config)
    framework = guest_home / "RuntimeFrameworks/Foundation.framework"
    sdk_framework = Path(config["sdk"]) / "System/Library/Frameworks/Foundation.framework"
    if not sdk_framework.exists():
        parser.exit(1, "The SDK framework does not exist: " + str(sdk_framework) + "\n")
    try:
        result = install(args.binary, sdk_framework, framework, args.backup_dir)
    except PermissionError as error:
        parser.exit(1, "Cannot write " + str(error.filename) + "; choose a writable --backup-dir or run the installer with sudo.\n")
    result["binary"] = str(args.binary)
    result["framework"] = str(framework)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
