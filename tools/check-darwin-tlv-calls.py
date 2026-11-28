#!/usr/bin/env python3
"""Fail if a Mach-O binary calls a thread-local variable's thunk without its descriptor in %rdi.

dyld's tlv_get_addr reads the descriptor from %rdi. clang 23 tuning for AMD Zen (-mtune=znver*,
also implied by -march=native on Zen) can load the descriptor into another register and call
through it, leaving %rdi unrelated: the thunk then returns NULL and the first write crashes.
Usage: check-darwin-tlv-calls.py BINARY... ; exits 1 and lists every bad access it finds.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llvm_tools


def thread_vars_ranges(path, objdump=None):
    """Address ranges of the __thread_vars sections, which hold the TLV descriptors."""
    tool = objdump or llvm_tools.require('llvm-objdump')
    output = subprocess.run([tool, '--macho', '--section-headers', path],
                            capture_output=True, text=True, check=True).stdout
    ranges = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == '__thread_vars':
            start, size = int(fields[3], 16), int(fields[2], 16)
            ranges.append((start, start + size))
    return ranges


def bad_accesses(path, objdump=None):
    with open(path, 'rb') as binary:
        if binary.read(4) not in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
            return []
    tool = objdump or llvm_tools.require('llvm-objdump')
    ranges = thread_vars_ranges(path, tool)
    if not ranges:
        return []
    disassembly = subprocess.run([tool, '-d', '--no-show-raw-insn', path],
                                 capture_output=True, text=True, check=True).stdout
    load = re.compile(r'(?:leaq|movq)\s+-?0x[0-9a-f]+\(%rip\), %(\w+)\s+#+ 0x([0-9a-f]+)')
    found, function, pending = [], '?', {}
    for line in disassembly.splitlines():
        if line.endswith('>:'):
            function, pending = line.split('<', 1)[-1][:-2], {}
            continue
        match = load.search(line)
        if match and any(start <= int(match[2], 16) < end for start, end in ranges):
            pending[match[1]] = line.strip()
            continue
        call = re.search(r'callq\s+\*\(%(\w+)\)', line)
        if call and call[1] in pending:
            if call[1] != 'rdi':
                found.append('%s: %s | %s' % (function, pending[call[1]], line.strip()))
            pending.pop(call[1])
    return found


def _worker_count(total):
    raw = os.environ.get('RCLOL_TLV_JOBS')
    if raw:
        try:
            jobs = int(raw)
            if jobs > 0:
                return min(total, jobs)
        except ValueError:
            pass
    return max(1, min(total, os.cpu_count() or 4))


def main(argv=None):
    binaries = list(sys.argv[1:] if argv is None else argv)
    if not binaries:
        sys.exit('No runtime binaries supplied for validation')

    objdump = llvm_tools.require('llvm-objdump')
    workers = _worker_count(len(binaries))
    if workers > 1 and len(binaries) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda binary: bad_accesses(binary, objdump), binaries))
    else:
        results = [bad_accesses(binary, objdump) for binary in binaries]

    status = 0
    for binary, problems in zip(binaries, results):
        if problems:
            status = 1
            print('%s: %d thread-local access(es) without the descriptor in %%rdi' % (binary, len(problems)))
            for problem in problems[:20]:
                print('  ' + problem)
    return status


if __name__ == '__main__':
    sys.exit(main())
