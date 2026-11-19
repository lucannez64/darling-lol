#!/usr/bin/env python3
"""Build and run the host-only check of mldr's commpage CPU and memory fields.

Compiles Darling's real src/startup/mldr/commpage.c (from build/darling-src, or
--source) against a stand-in for <i386/cpu_capabilities.h> holding the real
commpage offsets, calls commpage_setup(true) in a child process per profile and
reads the page back. Nothing outside build/logs/commpage-check is written; no
prefix, guest or Darling process is touched.

Checks:
- profile=compat: NCPUS/ACTIVE/LOGICAL = 12, PHYSICAL = 6 (the i7-9750H), and
  MEMORY_SIZE = RCLOL_MEMORY_BYTES, whatever the host has;
- no profile: the host's own counts and memory are left as they were.
"""
import argparse
import os
import re
from pathlib import Path
import subprocess
import sys

project = Path(__file__).resolve().parent.parent

SHIM = r"""
#ifndef HOST_SHIM_I386_CPU_CAPABILITIES_H
#define HOST_SHIM_I386_CPU_CAPABILITIES_H
/* Offsets from xnu osfmk/i386/cpu_capabilities.h (64-bit commpage). */
#define kUP                           0x00008000
#define _COMM_PAGE32_AREA_LENGTH      (1 * 4096)
#define _COMM_PAGE32_BASE_ADDRESS     (0xffff0000)
#define _COMM_PAGE64_AREA_LENGTH      (1 * 4096)
#define _COMM_PAGE64_BASE_ADDRESS     (0x00007fffffe00000ULL)
#define _COMM_PAGE_START_ADDRESS      _COMM_PAGE64_BASE_ADDRESS
#define _COMM_PAGE_SIGNATURE          (_COMM_PAGE_START_ADDRESS+0x000)
#define _COMM_PAGE_CPU_CAPABILITIES64 (_COMM_PAGE_START_ADDRESS+0x010)
#define _COMM_PAGE_VERSION            (_COMM_PAGE_START_ADDRESS+0x01E)
#define _COMM_PAGE_THIS_VERSION       14
#define _COMM_PAGE_CPU_CAPABILITIES   (_COMM_PAGE_START_ADDRESS+0x020)
#define _COMM_PAGE_NCPUS              (_COMM_PAGE_START_ADDRESS+0x022)
#define _COMM_PAGE_ACTIVE_CPUS        (_COMM_PAGE_START_ADDRESS+0x034)
#define _COMM_PAGE_PHYSICAL_CPUS      (_COMM_PAGE_START_ADDRESS+0x035)
#define _COMM_PAGE_LOGICAL_CPUS       (_COMM_PAGE_START_ADDRESS+0x036)
#define _COMM_PAGE_MEMORY_SIZE        (_COMM_PAGE_START_ADDRESS+0x038)
#define _COMM_PAGE_KERNEL_PAGE_SHIFT  (_COMM_PAGE_START_ADDRESS+0x04D)
#define _COMM_PAGE_USER_PAGE_SHIFT_64 (_COMM_PAGE_START_ADDRESS+0x04E)
#define _COMM_PAGE_USER_PAGE_SHIFT_32 _COMM_PAGE_USER_PAGE_SHIFT_64
#endif
"""

MAIN = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include "commpage.h"

/* Some mldr trees declare it; the check only needs commpage_setup(). */
void commpage_setup(bool _64bit);

int main(void) {
    /* commpage_setup maps at the hint address; read back through /proc/self/maps
     * would be overkill: the fixed address is free in this tiny process. */
    commpage_setup(true);
    const uint8_t* page = (const uint8_t*) 0x00007fffffe00000ULL;
    uint64_t memory;
    memcpy(&memory, page + 0x38, sizeof(memory));
    printf("%u %u %u %u %llu\n", page[0x22], page[0x34], page[0x35], page[0x36],
           (unsigned long long) memory);
    return 0;
}
"""


def host_memory():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) * 1024
    raise SystemExit('no MemTotal in /proc/meminfo')


def run(binary, env):
    out = subprocess.run([str(binary)], env=env, check=True, capture_output=True, text=True).stdout
    ncpus, active, physical, logical, memory = (int(v) for v in out.split())
    return ncpus, active, physical, logical, memory


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source', type=Path,
                        default=project / 'build/darling-src/src/startup/mldr',
                        help='the mldr source folder holding commpage.c')
    parser.add_argument('--xnu', type=Path,
                        default=project / 'build/darling-src/src/external/xnu',
                        help='the xnu tree whose cpu_capabilities.h holds the k* capability bits')
    args = parser.parse_args()

    # The capability bits (k64Bit, kHasSSE, ...) come from the real header.
    real = (args.xnu / 'osfmk/i386/cpu_capabilities.h').read_text().splitlines()
    bits = [line for line in real if re.match(r'#define\s+k\w+\s+\S', line)]
    if not bits:
        raise SystemExit('no k* capability bits found in cpu_capabilities.h')

    folder = project / 'build/logs/commpage-check'
    (folder / 'include/i386').mkdir(parents=True, exist_ok=True)
    (folder / 'include/i386/cpu_capabilities.h').write_text(
        SHIM.replace('#endif\n', '\n'.join(b for b in bits if not b.split()[1] == 'kUP') + '\n#endif\n'))
    (folder / 'main.c').write_text(MAIN)
    binary = folder / 'commpage-check'
    flag = folder / 'fixed-cpu-count.flag'
    flag.unlink(missing_ok=True)
    subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wno-unused-function',
                    f'-DRCLOL_FIXED_CPU_FLAG="{flag}"',
                    '-I', str(folder / 'include'), '-I', str(args.source),
                    str(args.source / 'commpage.c'), str(folder / 'main.c'),
                    '-o', str(binary)], check=True)

    base = {k: v for k, v in os.environ.items()
            if k not in ('RCLOL_PLATFORM_PROFILE', 'RCLOL_MEMORY_BYTES')}
    failures = []

    profile_memory = 16 << 30
    got = run(binary, dict(base, RCLOL_PLATFORM_PROFILE='compat',
                           RCLOL_MEMORY_BYTES=str(profile_memory)))
    want = (12, 12, 6, 12, profile_memory)
    print(f'COMMPAGE mode=compat ncpus={got[0]} active={got[1]} physical={got[2]} '
          f'logical={got[3]} memory={got[4]}')
    if got != want:
        failures.append(f'compat: got {got}, want {want}')

    # Not a plain decimal number: the host's size stays, as in xnu.
    got = run(binary, dict(base, RCLOL_PLATFORM_PROFILE='compat',
                           RCLOL_MEMORY_BYTES=f'{profile_memory}junk'))
    print(f'COMMPAGE mode=compat-bad-memory memory={got[4]}')
    if got[4] != host_memory():
        failures.append(f'compat-bad-memory: got memory {got[4]}, want host {host_memory()}')

    # The A/B opt-out flag wins over compat: the old fixed 8s, as in xnu.
    flag.write_text('')
    try:
        got = run(binary, dict(base, RCLOL_PLATFORM_PROFILE='compat'))
    finally:
        flag.unlink(missing_ok=True)
    print(f'COMMPAGE mode=compat-optout ncpus={got[0]} active={got[1]} physical={got[2]} logical={got[3]}')
    if got[:4] != (8, 8, 8, 8):
        failures.append(f'compat-optout: got {got[:4]}, want (8, 8, 8, 8)')

    got = run(binary, base)
    online = os.cpu_count()
    print(f'COMMPAGE mode=host ncpus={got[0]} active={got[1]} physical={got[2]} '
          f'logical={got[3]} memory={got[4]}')
    if got[0] != online or got[4] != host_memory():
        failures.append(f'host: got {got}, want ncpus={online} memory={host_memory()}')

    if failures:
        print('COMMPAGE pass=0 ' + '; '.join(failures))
        return 1
    print('COMMPAGE pass=1')
    return 0


if __name__ == '__main__':
    sys.exit(main())
