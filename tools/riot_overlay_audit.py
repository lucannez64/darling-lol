#!/usr/bin/env python3
"""Audit which runtime library file the guest actually loads for every dylib a
binary uses, and estimate how it was compiled.

The guest resolves a dependent dylib by leaf name through DYLD_LIBRARY_PATH
before the install path (dyld3/PathOverrides.cpp:forEachPathVariant and the
legacy dyld2.cpp:loadPhase1 both do this), so a file in RuntimeLibraries
shadows both the per-user prefix copy and the installed SDK copy in
/usr/local/libexec/darling.  Frameworks resolve the same way through
DYLD_FRAMEWORK_PATH.

The optimization estimate is the share of instructions that read or write a
stack slot, counted only in functions of 20 instructions or more.  -O0 spills
every local; register-allocated -O2 code mostly does not.  Calibrate with
--calibrate against the libmalloc -O0/-O2 objects in
build/perf-research/memory/o-compare.
"""
import argparse
import collections
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llvm_tools

ROOT = Path(__file__).resolve().parent.parent
GAME = (Path.home() / ".darling/Applications/League of Legends.app/Contents/LoL/Game"
        / "LeagueofLegends.app/Contents/MacOS/LeagueofLegends")
# Overlay artifacts the tools/ builders produce and install; used only to name
# the builder behind an overlay file.
BUILDERS = {
    "libsystem_kernel.dylib": "build-riot-kqueue-runtime.py",
    "libsystem_malloc.dylib": "build-riot-malloc-runtime.py",
    "libsystem_pthread.dylib": "build-riot-pthread-runtime.py",
    "libsystem_platform.dylib": "build-riot-platform-strings.py",
    "libobjc.A.dylib": "build-riot-objc-runtime.py",
    "libc++abi.dylib": "build-riot-cxxabi-runtime.py",
    "libc++.1.dylib": "build-riot-cxx-optimized.py",
    "libindium.dylib": "build-riot-indium-runtime.py",
    "libiridium.dylib": "build-riot-indium-runtime.py",
    "libz.1.dylib": "-",
}
MIN_FUNCTION_INSTRUCTIONS = 20


def tool(*arguments):
    """Run an LLVM tool, resolving versioned names from PATH or llvm-config."""
    return subprocess.run((llvm_tools.require(arguments[0]), *arguments[1:]),
                          capture_output=True, text=True, errors="replace").stdout


def load_commands(path, requested):
    """Direct dependencies of one Mach-O file, in load-command order."""
    text = tool("llvm-objdump", "--macho", "--dylibs-used", str(path))
    dependencies = []
    for line in text.splitlines():
        match = re.match(r"^\t(\S+) \(compatibility version", line)
        if match:
            dependencies.append(match.group(1))
    return dependencies


def rpaths_of(path):
    text = tool("llvm-objdump", "--macho", "--private-headers", str(path))
    return re.findall(r"^\s+path (\S+) \(offset \d+\)$", text, re.M)


def framework_partial_path(path):
    index = path.find(".framework/")
    if index < 0:
        return None
    return path[path.rfind("/", 0, index) + 1:]


def resolve(install_path, origin, rpaths, config):
    """Return the file the guest would load for an install path, or None."""
    overlay = config["prefix"].rstrip("/") + config["guest_home"] + "/RuntimeLibraries"
    frameworks = config["prefix"].rstrip("/") + config["guest_home"] + "/RuntimeFrameworks"
    sdk = config["sdk"].rstrip("/")
    candidates = []
    partial = framework_partial_path(install_path)
    if partial is not None:
        candidates.append(frameworks + "/" + partial)
    else:
        candidates.append(overlay + "/" + install_path.rsplit("/", 1)[-1])
    if install_path.startswith("@rpath/"):
        for rpath in list(rpaths) + [None]:
            base = str(Path(origin).parent) if rpath in ("@loader_path", None) else rpath
            if rpath == "@executable_path":
                base = str(Path(config["game"]).parent)
            if base is not None:
                candidates.append(base.rstrip("/") + "/" + install_path[len("@rpath/"):])
    elif install_path.startswith("@loader_path/"):
        candidates.append(str(Path(origin).parent) + "/" + install_path[len("@loader_path/"):])
    elif install_path.startswith("@executable_path/"):
        candidates.append(str(Path(config["game"]).parent) + "/" + install_path[len("@executable_path/"):])
    else:
        candidates.append(install_path)
    for candidate in candidates:
        for root in ("", config["prefix"].rstrip("/"), sdk):
            path = Path(root + candidate) if root or candidate.startswith("/") else None
            if path is not None and path.is_file():
                return path.resolve()
    return None


def instructions(path):
    """Function-level instruction lists from a Mach-O file."""
    text = tool("llvm-objdump", "--macho", "--disassemble", "--no-show-raw-insn", str(path))
    functions = collections.OrderedDict()
    current = None
    for line in text.splitlines():
        name = re.match(r"^([_a-zA-Z][\w.$]*):$", line.strip())
        if name and not line.startswith(" "):
            current = name.group(1)
            functions[current] = []
        elif current is not None:
            instruction = re.match(r"^\s+[0-9a-f]+:\t(.*)$", line)
            if instruction:
                functions[current].append(instruction.group(1))
    return functions


def optimization_estimate(path):
    """(instructions, stack references, function count) over functions >= 20
    instructions."""
    total = stack = functions = 0
    try:
        for body in instructions(path).values():
            if len(body) < MIN_FUNCTION_INSTRUCTIONS:
                continue
            functions += 1
            total += len(body)
            stack += sum(1 for i in body if "(%rbp)" in i or "(%rsp)" in i)
    except Exception as error:  # a fat or malformed file is reported, not fatal
        return 0, 0, 0, str(error)
    return total, stack, functions, None


def imports(path):
    """Symbols the file imports, per providing dylib."""
    counts = collections.Counter()
    for option in ("--bind", "--lazy-bind", "--weak-bind"):
        for line in tool("llvm-objdump", "--macho", option, str(path)).splitlines():
            match = re.match(r"^\S+\s+\S+\s+0x[0-9a-f]+\s+\S+\s+\d+\s+(\S+)\s+(\S+)$", line)
            if match:
                counts[match.group(1)] += 1
    return counts


def walk(config):
    """Resolve the transitive closure starting at the game executable."""
    queue = [Path(config["game"]).resolve()]
    seen = {}
    order = []
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        origin = str(path)
        seen[path] = rpaths_of(path)
        order.append(path)
        for dependency in load_commands(path, None):
            if dependency.startswith("/usr/lib/libSystem.B.dylib"):
                continue
            resolved = resolve(dependency, origin, seen[path], config)
            if resolved is None:
                print("warning: cannot resolve " + dependency + " (from " + origin + ")",
                      file=sys.stderr)
                continue
            if resolved not in seen:
                queue.append(resolved)
    return order


def report(config):
    order = walk(config)
    print(f"{'library':42s} {'insns':>8s} {'stack%':>7s} {'funcs':>6s} {'-O':>3s}  effective path")
    names = {}
    for path in order:
        total, stack, functions, error = optimization_estimate(path)
        if error:
            print(f"{path.name:42s} error: {error}")
            continue
        share = 100.0 * stack / max(total, 1)
        estimate = "-O0" if share > 20 else ("-O1+" if share > 8 else "-O2")
        print(f"{path.name:42s} {total:8d} {share:6.1f}% {functions:6d} {estimate:>3s}  {path}")
        names[path] = estimate
    print()
    for path in order:
        if path.name not in BUILDERS or BUILDERS[path.name] == "-":
            continue
        print(f"builder for {path.name}: tools/{BUILDERS[path.name]}")
    return order, names


def calibrate():
    folder = ROOT / "build/perf-research/memory/o-compare"
    if not folder.is_dir():
        sys.exit("no calibration objects in " + str(folder))
    for base in sorted(set(p.name.rsplit(".", 2)[0] for p in folder.glob("*.O?.o"))):
        row = [base]
        for level in ("O0", "O2"):
            path = folder / f"{base}.{level}.o"
            total, stack, functions, _ = optimization_estimate(path)
            row.append(f"{level} {total:6d} insns {100.0*stack/max(total,1):5.1f}% stack")
        print("calibration " + row[0] + ": " + "; ".join(row[1:]))


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=None,
                        help="binary to audit (default: the installed League game)")
    parser.add_argument("--config", type=Path, default=ROOT / "build/config.json")
    parser.add_argument("--calibrate", action="store_true",
                        help="print the -O0/-O2 metric on known same-source objects")
    arguments = parser.parse_args(argv)
    return parser, arguments


def main():
    parser, arguments = parse_arguments()
    if arguments.calibrate:
        calibrate()
        return
    config = json.loads(arguments.config.read_text())
    config["game"] = str(arguments.binary or GAME)
    if not Path(config["game"]).is_file():
        parser.exit(1, "The binary to audit does not exist: " + config["game"] + "\n")
    report(config)


if __name__ == "__main__":
    main()
