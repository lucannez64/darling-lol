#!/usr/bin/env python3
"""Rebuild libsystem_c (libc) with -O3 -march=native -mtune=generic.

The CMake tree is configured with an empty build type, so the installed
libsystem_c.dylib is clang's -O0 output except for the three string files
libplatform marks optimized. The game imports 122 functions from this library
(stdio, string, stdlib, locale, time) and it is on the path of every frame;
this builder takes each object's own compile command out of the generated
build.make, keeps every define, include and per-file option it has, replaces
the optimization level with -O3 and relinks the result with the configured
link line. -fno-strict-aliasing matches what the existing optimized rebuilds
use for this ten-year-old BSD C.

Files whose correctness depends on frame layout or on running single-threaded
in a forked child keep their configured flags and are listed in EXCLUDED below.
No -DNDEBUG, no -ffast-math, no LTO: only the optimization level changes.
Output: build/riot-libsystem_c.dylib.
"""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riot_build_cache
import riot_build_gate

# The rendered copy in build/runtime/tools resolves build/runtime/build (a
# symlink to this checkout's build/), but build.make holds real-root path
# strings; path matching needs the real repository root, not the symlink view.
root = (Path(__file__).resolve().parent.parent / "build").resolve().parent
folder = root / "build/darling-src/build/src/external/libc"
source_root = root / "build/darling-src/src/external/libc"
resource_dir = subprocess.check_output(["clang", "-print-resource-dir"], text=True).strip()

# Kept at their configured flags:
EXCLUDED = {
    # fork/vfork paths run in a forked child before anything else is safe
    "sys/fork.c",
    "sys/_libc_fork_child.c",
    # the stack guard handler must not rely on the frame it reports
    "sys/OpenBSD/stack_protector.c",
    # walks frame pointers to build a backtrace
    "gen/thread_stack_pcs.c",
    # __builtin_return_address in a diagnostic; nothing to gain here
    "os/assumes.c",
}


def current_clang(values):
    return [re.sub(r"/usr/lib/clang/\d+/include", resource_dir + "/include", value) for value in values]


@lru_cache(maxsize=None)
def variables_of(target_folder):
    variables = {}
    for line in (target_folder / "flags.make").read_text().splitlines():
        if line.startswith("C_") and " = " in line:
            key, value = line.split(" = ", 1)
            variables["$(" + key + ")"] = current_clang(shlex.split(value))
    return variables


def split_entry(entry):
    """'db/CMakeFiles/libc-db.dir/btree/bt_close.c.o' -> (head, target, relative)."""
    head, tail = entry.split("CMakeFiles/", 1)
    target, relative = tail.split(".dir/", 1)
    return head, target, relative[:-2]


def source_of(head, relative):
    path = source_root / (head + relative)
    if not path.is_file():
        # CMake writes an out-of-directory source as a "__" component
        path = (source_root / (head + relative).replace("__", "..")).resolve()
    return path


def compile_command(target_folder, source, object_path):
    """The configured compile command for one object, with the object path and
    the dependency flags taken out and -O3 -fno-strict-aliasing added."""
    for line in (target_folder / "build.make").read_text().splitlines():
        if "$(C_DEFINES)" not in line or " -c " not in line or str(source) not in line:
            continue
        command = shlex.split(line.split("&&", 1)[1])
        expanded = []
        for token in command:
            if token in variables_of(target_folder):
                expanded += variables_of(target_folder)[token]
            else:
                expanded.append(token)
        cleaned, skip = [], False
        for token in expanded:
            if skip:
                skip = False
                continue
            if token in ("-MD", "-MT", "-MF"):
                skip = token != "-MD"
                continue
            if token == "-o":
                cleaned.append(token)
                cleaned.append(object_path)
                skip = True
                continue
            cleaned.append(token)
        cleaned = [t for t in cleaned if t not in ("-g", "-O0", "-O1", "-O2")]
        cleaned.extend(["-O3", "-march=native", "-mtune=generic", "-fno-strict-aliasing"])
        return cleaned
    return None


def build():
    link_text = (folder / "CMakeFiles/system_c.dir/link.txt").read_text()
    link = shlex.split(link_text)
    rsp_index = next(index for index, argument in enumerate(link) if argument.startswith("@"))
    # The response file is a whitespace-separated list; CMake leaves the
    # objects of other directories unquoted, so split like the linker does.
    entries = shlex.split((folder / link[rsp_index][1:]).read_text())
    jobs, objects, kept = [], [], []
    for entry in entries:
        head, target, relative = split_entry(entry)
        source = source_of(head, relative)
        target_folder = folder / head / "CMakeFiles" / (target + ".dir")
        try:
            source_key = str(source.relative_to(source_root))
        except ValueError:
            source_key = relative
        if "libkqueue" in head:
            # These objects belong to the libkqueue target that the kernel
            # library also links; the kernel component owns them, so they keep
            # the configured flags here.
            objects.append(entry)
            kept.append(entry)
            continue
        if source_key in EXCLUDED or source.suffix != ".c" or not source.is_file():
            objects.append(entry)
            kept.append(entry)
            continue
        obj = root / ("build/riot-libc-%s.o" % (head + target + "-" + relative).replace("/", "_"))
        command = compile_command(target_folder, source, str(obj))
        if command is None:
            print("no compile command for", entry, "- keeping the configured object")
            objects.append(entry)
            kept.append(entry)
            continue
        # Also record the dependencies, so header edits invalidate the cache.
        command.extend(["-MD", "-MF", str(obj) + ".d"])
        jobs.append((target_folder, command, obj, source))
        objects.append(str(obj))
    print("recompiling", len(jobs), "of", len(entries), "objects at -O2", flush=True)

    # Wait for a quiet machine before hashing (and later compiling): nothing
    # below burns CPU a running match would notice.
    riot_build_gate.wait_until_free()

    def compute_chunks():
        # Everything that can change the result: this script, the link line,
        # each recompiled source's content and compile command, the stat of
        # every configured object kept as-is (the cmake build refreshes those
        # itself), and the headers recorded by the previous dependency files.
        chunks = [riot_build_cache.digest_text(Path(__file__).read_text()),
                  riot_build_cache.digest_text(link_text)]
        for target_folder, command, obj, source in jobs:
            chunks.append(" ".join(command) + "\0" + riot_build_cache.digest_file(source))
        for entry in kept:
            chunks.append(entry + "\0" + riot_build_cache.stat_entry(folder / entry))
        chunks.append(riot_build_cache.dependency_chunk(
            [Path(str(obj) + ".d") for _, _, obj, _ in jobs]))
        return chunks

    stamp = root / "build/riot-libc-build.json"
    chunks = compute_chunks()
    digest = riot_build_cache.fingerprint(chunks)
    if riot_build_cache.stable_inputs(chunks, chunks) and riot_build_cache.up_to_date(stamp, digest, [root / "build/riot-libsystem_c.dylib"]):
        print("libsystem_c up to date; skipping rebuild", flush=True)
        return
    # A failed build must not leave the old stamp validating its outputs.
    stamp.unlink(missing_ok=True)

    def compile_one(job):
        working_directory, command, obj, source = job
        result = subprocess.run(command, cwd=working_directory, capture_output=True, text=True)
        if result.returncode != 0:
            print("failed:", " ".join(command[-6:]))
            print(result.stderr[-2000:])
            raise SystemExit(1)
        if not obj.is_file():
            raise SystemExit("no object produced for " + str(obj))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(compile_one, jobs))

    rsp = root / "build/riot-libc-objects.rsp"
    rsp.write_text(" ".join('"%s"' % object for object in objects) + "\n")
    link[rsp_index] = "@" + str(rsp)
    link[link.index("-o") + 1] = str(root / "build/riot-libsystem_c.dylib")
    subprocess.run(link, cwd=folder, check=True)
    print("built build/riot-libsystem_c.dylib with", len(jobs), "objects at -O2", flush=True)
    post = compute_chunks()
    if not riot_build_cache.stable_inputs(chunks, post):
        # Inputs changed while compiling; record nothing so the next run
        # rebuilds instead of stamping objects compiled from mixed inputs.
        print("inputs changed while building; not recording the stamp", flush=True)
        return
    riot_build_cache.record(stamp, riot_build_cache.fingerprint(post), "libsystem_c -O3 -march=native")


if __name__ == "__main__":
    build()
