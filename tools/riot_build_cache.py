#!/usr/bin/env python3
"""Fingerprint cache so the riot library builders skip redundant work.

Each builder hashes everything that affects its output: its own script, the
configured flags and link lines it consumes, the contents of every source it
recompiles, and the stat (mtime + size) of the configured objects it keeps as
they are. The digest is stored in a stamp file next to the build outputs; a
matching digest with all outputs still present means the last build is current
and the builder exits without compiling or relinking. Anything that could
change the result — a patch, a flags update, a new clang resource directory, a
fresh cmake configure — produces a different digest and forces a rebuild.
"""
import hashlib
import json
import shlex
from pathlib import Path


def digest_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def digest_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def stat_entry(path):
    """Cheap freshness proxy for build-tree objects the builder keeps as-is.

    The main cmake build regenerates these when their sources change, so a new
    mtime or size means the builder would relink against different objects.
    """
    try:
        status = Path(path).stat()
    except OSError:
        return "missing"
    return "%d:%d" % (status.st_mtime_ns, status.st_size)


def fingerprint(chunks):
    return hashlib.sha256("\0".join(chunks).encode()).hexdigest()


def dependency_paths(dep_file):
    """Paths listed in a clang -MD dependency file, or None when unreadable."""
    try:
        text = Path(dep_file).read_text()
    except OSError:
        return None
    body = text.split(":", 1)[1] if ":" in text else text
    try:
        return shlex.split(body.replace("\\\n", " ").replace("$$", "$"), comments=False)
    except ValueError:
        return None


def dependency_chunk(dep_files):
    """Fingerprint chunk over the dependencies the previous build recorded.

    The .d files written next to the objects list every included header; their
    contents are hashed so a header edit invalidates the library even though no
    compiled source changed. A missing or unreadable dependency file — and any
    listed dependency that can no longer be read — acts as a cache miss.
    """
    try:
        digests = {}
        for dep_file in dep_files:
            entries = dependency_paths(dep_file)
            if entries is None:
                return "unreadable-dependency-files"
            for path in entries:
                if path not in digests:
                    digests[path] = digest_file(path)
    except OSError:
        return "unreadable-dependencies"
    return "dependencies:" + ",".join(
        sorted(path + "\0" + value for path, value in digests.items()))


def up_to_date(stamp_path, digest, outputs):
    """True when the recorded digest matches and every output is still there."""
    try:
        stamp = json.loads(Path(stamp_path).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(stamp, dict) or stamp.get("fingerprint") != digest:
        return False
    return all(Path(output).is_file() for output in outputs)


def stable_inputs(before, after):
    """Only cache a build whose full dependency set was known and unchanged.

    The first build discovers headers via -MD. Until a subsequent build verifies
    those inputs across compilation, do not stamp potentially mixed output.
    Unreadable dependency lists must never become reusable fingerprints.
    """
    return (before == after and bool(before)
            and before[-1].startswith("dependencies:"))


def record(stamp_path, digest, note=""):
    Path(stamp_path).write_text(
        json.dumps({"fingerprint": digest, "note": note}, indent=1) + "\n")
