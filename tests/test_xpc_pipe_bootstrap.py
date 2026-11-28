"""Patch regressions for direct launchd pipes and Mach-supplied caller identity."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def added_pipe():
    patch = (ROOT / 'patches/libxpc.patch').read_text()
    section = patch.split('diff --git a/src/pipe.m b/src/pipe.m\n', 1)[1]
    section = section.split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


class XPCPipeBootstrapTests(unittest.TestCase):
    def test_bootstrap_destination_uses_the_retained_port_without_checkin(self):
        code = added_pipe()
        branch = re.search(r'if \(MACH_PORT_VALID\(bootstrap_port\) && port == bootstrap_port\) \{(.*?)\n\s*\}',
                           code, re.S)
        self.assertIsNotNone(branch, 'pipes to launchd must not require a connection CHECKIN')
        direct = branch.group(1)
        self.assertIn('this->send_port = port;', direct)
        self.assertIn('this->state = xpc_pipe_state_active;', direct)
        self.assertIn('return self;', direct)
        self.assertNotIn('this->checkin_port = port;', direct)
        self.assertNotIn('xpc_mach_port_create_', direct)

    def test_apple_sync_ipc_override_flag_keeps_its_meaning(self):
        # XPC_PIPE_USE_SYNC_IPC_OVERRIDE is Apple's QoS flag; Libinfo and the
        # syslog shim pass it for ordinary named services that still need the
        # connection handshake, so it must not select the direct mode.
        code = added_pipe()
        self.assertNotRegex(code, r'if\s*\(\s*flags\s*&\s*XPC_PIPE_USE_SYNC_IPC_OVERRIDE')

    def test_only_server_receive_paths_attach_the_kernel_audit_token(self):
        # The two server receive paths (launchd's routines authenticate every
        # request) attach the token; the routine reply path does not, so
        # Libinfo's OpenDirectory reply check sees the same token as before.
        code = added_pipe()
        self.assertEqual(code.count('audit_token_t* token = pipe_audit_token(header);'), 2)
        self.assertEqual(code.count('[dict setAssociatedAuditToken: token];'), 2)
        self.assertEqual(len(re.findall(r'if \(token\) \{\s*\[dict setAssociatedAuditToken: token\];',
                                        code)), 2)
        self.assertNotRegex(code, r'audit_token_t\* token = pipe_audit_token\(header\);\n'
                                  r'(?:.*\n){0,5}\s*\*reply = \[dict retain\];')
        self.assertRegex(code, r'\*incomingMessage = \[dict retain\];')

    def test_audit_trailer_validation_and_alignment(self):
        code = added_pipe()
        helper = re.search(r'static audit_token_t\* pipe_audit_token\([^)]*\) \{.*?\n\}',
                           code, re.S)
        self.assertIsNotNone(helper, 'pipe messages must retain their Mach audit trailer')
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('C compiler required')
        harness = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
typedef struct { uint32_t val[8]; } audit_token_t;
typedef struct { uint32_t msgh_size; uint32_t padding; } mach_msg_header_t;
typedef struct { uint32_t msgh_trailer_type, msgh_trailer_size; } mach_msg_trailer_t;
typedef struct { mach_msg_trailer_t head; audit_token_t msgh_audit; } mach_msg_audit_trailer_t;
#define MACH_MSG_TRAILER_FORMAT_0 0
#define round_msg(size) (((size) + 3) & ~3)
/* HELPER */
int main(void) {
    union { uint64_t alignment; unsigned char bytes[128]; } storage;
    memset(&storage, 0, sizeof(storage));
    mach_msg_header_t *header = (void *)storage.bytes;
    header->msgh_size = sizeof(*header) + 1;
    mach_msg_audit_trailer_t *trailer = (void *)(storage.bytes + round_msg(header->msgh_size));
    trailer->head.msgh_trailer_type = MACH_MSG_TRAILER_FORMAT_0;
    trailer->head.msgh_trailer_size = sizeof(*trailer);
    trailer->msgh_audit.val[5] = 5;
    assert(pipe_audit_token(header) == &trailer->msgh_audit);
    assert(pipe_audit_token(header)->val[5] == 5);
    trailer->head.msgh_trailer_size = sizeof(*trailer) - 1;
    assert(pipe_audit_token(header) == NULL);
    trailer->head.msgh_trailer_size = sizeof(*trailer);
    trailer->head.msgh_trailer_type = 99;
    assert(pipe_audit_token(header) == NULL);
    return 0;
}
'''.replace('/* HELPER */', helper.group(0))
        with tempfile.TemporaryDirectory(prefix='xpc-pipe-audit-') as folder:
            source, binary = Path(folder) / 'check.c', Path(folder) / 'check'
            source.write_text(harness)
            result = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                     str(source), '-o', str(binary)], capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
