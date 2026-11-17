#!/usr/bin/env python3
"""Rebuild libc++ and libc++abi with -O3 -march=native -mtune=generic.

The CMake tree has no build type, so both libraries are clang's -O0 output: the
game imports 131 functions from libc++.1.dylib (out-of-line std::string,
iostream, locale, shared_ptr/atomic refcounts, mutex, chrono, new/delete) and
13 from libc++abi.dylib, and neither has an overlay today.

This is tools/build-riot-cxx-optimized.py without -DNDEBUG: the configured
defines and includes are kept exactly, only the optimization level changes.
The earlier builder also compiles libc++ at -O2, but with assertions disabled,
which changes what the library checks at run time; the batch installer uses
this one so the shipped library differs from the configured build in code
generation only. Outputs build/riot-libc++.1.dylib and build/riot-libc++abi.dylib.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riot_build_cache
import riot_build_gate

# build/runtime/build is a symlink to this checkout's build/; resolve it so
# path strings match the real repository root no matter which copy runs.
root = (Path(__file__).resolve().parent.parent / "build").resolve().parent
resource_dir = subprocess.check_output(["clang", "-print-resource-dir"], text=True).strip()


def current_clang(values):
    return [re.sub(r"/usr/lib/clang/\d+/include", resource_dir + "/include", value) for value in values]


def build(library, object_dir, link_dir, output):
    folder = root / "build/darling-src/build/src/external" / library
    source = root / "build/darling-src/src/external" / library
    flags_text = (folder / ("CMakeFiles/%s/flags.make" % object_dir)).read_text()
    flags = {}
    for line in flags_text.splitlines():
        if line.startswith("CXX_") and " = " in line:
            key, value = line.split(" = ", 1)
            flags[key] = current_clang(shlex.split(value))
    link_text = (folder / ("CMakeFiles/%s/link.txt" % link_dir)).read_text()
    link = shlex.split(link_text)
    jobs = []
    for index, argument in enumerate(link):
        if argument.startswith("CMakeFiles/%s/" % object_dir) and argument.endswith(".o"):
            relative = argument[len("CMakeFiles/%s/" % object_dir):-2]
            obj = root / ("build/riot-opt-%s-%s.o" % (library, relative.replace("/", "_")))
            jobs.append((relative, obj))
            link[index] = str(obj)
    # Wait for a quiet machine before hashing (and later compiling).
    riot_build_gate.wait_until_free()

    def compute_chunks():
        # Everything that can change the result: this script, the effective
        # flags (after the clang resource-directory rewrite, so a compiler
        # update invalidates the cache), the link line, every recompiled
        # source's content, and the headers the previous dependency files list.
        chunks = [riot_build_cache.digest_text(Path(__file__).read_text()),
                  riot_build_cache.digest_text("\0".join(
                      key + "=" + " ".join(flags[key]) for key in sorted(flags))),
                  riot_build_cache.digest_text(link_text)]
        for relative, obj in jobs:
            chunks.append(relative + "\0" + riot_build_cache.digest_file(source / relative))
        chunks.append(riot_build_cache.dependency_chunk(
            [Path(str(obj) + ".d") for _, obj in jobs]))
        return chunks

    stamp = root / ("build/riot-cxx-%s-build.json" % library)
    chunks = compute_chunks()
    digest = riot_build_cache.fingerprint(chunks)
    if riot_build_cache.stable_inputs(chunks, chunks) and riot_build_cache.up_to_date(stamp, digest, [root / "build" / output]):
        print(library, "up to date; skipping rebuild", flush=True)
        return
    # A failed build must not leave the old stamp validating its outputs.
    stamp.unlink(missing_ok=True)
    print(library, "recompiling", len(jobs), "objects at -O3", flush=True)

    def compile_one(job):
        relative, obj = job
        cxx_flags = [f for f in flags["CXX_FLAGS"] if f not in ("-g", "-O0", "-O1", "-O2")]
        subprocess.run(["clang++", *flags["CXX_DEFINES"], *flags["CXX_INCLUDES"], *cxx_flags,
                        "-O3", "-march=native", "-mtune=generic", "-fno-strict-aliasing",
                        "-MD", "-MF", str(obj) + ".d",
                        "-c", str(source / relative), "-o", str(obj)],
                       check=True, capture_output=True)

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 8) as pool:
        list(pool.map(compile_one, jobs))
    link[link.index("-o") + 1] = str(root / "build" / output)
    subprocess.run(link, cwd=folder, check=True)
    print("built build/" + output, len(jobs), "objects at -O3", flush=True)
    post = compute_chunks()
    if not riot_build_cache.stable_inputs(chunks, post):
        # Inputs changed while compiling; record nothing so the next run
        # rebuilds instead of stamping objects compiled from mixed inputs.
        print(library, "inputs changed while building; not recording the stamp", flush=True)
        return
    riot_build_cache.record(stamp, riot_build_cache.fingerprint(post), output + " -O3 -march=native")


if __name__ == "__main__":
    build("libcxxabi", "cxxabi_obj.dir", "cxxabi.dir", "riot-libc++abi.dylib")
    build("libcxx", "cxx_obj.dir", "cxx.dir", "riot-libc++.1.dylib")
