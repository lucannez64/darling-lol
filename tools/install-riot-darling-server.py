#!/usr/bin/env python3
"""Atomically install a built Darling server; never stop or start any process."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from file_digest import sha256

ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    return sha256(path)


def live_binaries(destination):
    result = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        executable = process / "exe"
        try:
            if str(executable.readlink()).removesuffix(" (deleted)") == str(destination):
                result[process.name] = digest(executable)
        except (OSError, ProcessLookupError):
            continue
    return result


def install(source, destination, backups):
    source = source.resolve()
    destination = destination.absolute()
    if destination.is_symlink():
        raise ValueError("The server destination must be a regular file, not a symlink")
    installed = destination.stat()
    if not stat.S_ISREG(installed.st_mode):
        raise ValueError("The server destination is not a regular file")
    with source.open("rb") as input:
        if input.read(4) != b"\x7fELF":
            raise ValueError("The source is not an ELF binary")
    new_hash, old_hash = digest(source), digest(destination)
    before = live_binaries(destination)
    if new_hash == old_hash:
        return {"installed": True, "already_current": True, "sha256": new_hash, "restart_performed": False}
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / ("darlingserver-" + old_hash)
    if backup.exists():
        if digest(backup) != old_hash:
            raise ValueError("The existing backup has unexpected contents")
    else:
        shutil.copy2(destination, backup)
    fd, temporary = tempfile.mkstemp(prefix=".darlingserver-staged-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input:
            shutil.copyfileobj(input, output)
            output.flush()
            os.fchmod(output.fileno(), installed.st_mode & 0o777)
            os.fchown(output.fileno(), installed.st_uid, installed.st_gid)
            os.fsync(output.fileno())
        if digest(Path(temporary)) != new_hash:
            raise ValueError("The staged binary does not match the build")
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if digest(destination) != new_hash:
        raise ValueError("The installed binary does not match the build")
    after = live_binaries(destination)
    if any(after[pid] != old for pid, old in before.items() if pid in after):
        raise ValueError("An active process no longer maps its original binary")
    return {
        "installed": True, "sha256": new_hash, "backup": str(backup),
        "active_servers_preserved": len(before.keys() & after.keys()), "restart_performed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/darling-src/build/src/external/darlingserver/darlingserver")
    parser.add_argument("--destination", type=Path, default=Path("/usr/local/bin/darlingserver"))
    parser.add_argument("--backups", type=Path, default=ROOT / "build/backups/darlingserver")
    args = parser.parse_args()
    try:
        result = install(args.binary, args.destination, args.backups)
    except PermissionError:
        parser.exit(1, "Installation requires permission to write the server directory; run this installer with sudo in your terminal.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
