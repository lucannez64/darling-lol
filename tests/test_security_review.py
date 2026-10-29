"""Regression checks for the security continuation's review findings."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_security_host import ROOT, added_file, function


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecurityReviewTests(unittest.TestCase):
    def test_fork_registration_and_exec_reset(self):
        source = added_file('patches/darlingserver.patch', 'src/process.cpp')
        prepare = function(source, '_prepareCsStateForCheckin').replace('DarlingServer::Process::', 'Process::')
        debug = function(source[source.index('bool DarlingServer::Process::allowsDebugFrom'):], 'allowsDebugFrom').replace('DarlingServer::Process::', 'Process::')
        header = added_file('patches/darlingserver.patch', 'include/darlingserver/cs_state.h')
        code = header + r'''
#include <atomic>
#include <mutex>
#include <shared_mutex>
#include <vector>
#include <cassert>
using uid_t = unsigned;
static bool rootless = false;
static bool rootlessUserNamespace() { return rootless; }
struct Process {
 std::shared_mutex _csMutex;
 dc_cs_state _csState = {};
 bool _isForkChild = false, _initialCsCheckinPending = true;
 std::atomic<bool> _allowCsStateRegistration{false};
 std::vector<unsigned char> _csBlob, _csEntitlements, _csDerEntitlements;
 uid_t guest = 1000, host = 1000;
 uid_t uid() const { return guest; }
 uid_t hostUid() const { return host; }
 bool signingEnforce() const { return _csState.signing_enforce; }
 void _prepareCsStateForCheckin(bool didExec);
 bool allowsDebugFrom(const Process&, bool) const;
};
''' + prepare + '\n' + debug + r'''
int main() {
 Process initial; initial._prepareCsStateForCheckin(false);
 assert(initial._allowCsStateRegistration.exchange(false));
 initial._prepareCsStateForCheckin(false); assert(!initial._allowCsStateRegistration);
 Process forked; forked._isForkChild = true; // Parent may be gone; no weak pointer is consulted.
 forked._prepareCsStateForCheckin(false); assert(!forked._allowCsStateRegistration);
 forked._csState.verified = 1; forked._csState.flags = DC_CS_VALID;
 forked._csBlob = {1}; forked._csEntitlements = {2}; forked._csDerEntitlements = {3};
 forked._prepareCsStateForCheckin(true); assert(forked._allowCsStateRegistration);
 assert(!forked._csState.verified && !forked._csState.flags);
 assert(forked._csState.version == DC_CS_STATE_VERSION);
 assert(forked._csBlob.empty() && forked._csEntitlements.empty() && forked._csDerEntitlements.empty());
 Process caller, target; target._csState.signing_enforce = 1;
 caller.guest = 0; assert(!target.allowsDebugFrom(caller, false));
 caller.host = 0; assert(target.allowsDebugFrom(caller, false));
 rootless = true; assert(!target.allowsDebugFrom(caller, false)); rootless = false;
 caller.host = 1000; target._csState.flags = DC_CS_GET_TASK_ALLOW;
 assert(!target.allowsDebugFrom(caller, false)); target._csState.verified = 1;
 assert(target.allowsDebugFrom(caller, false));
}
'''
        # The real methods are const; their lock is mutable in the real class.
        code = code.replace(' std::shared_mutex _csMutex;', ' mutable std::shared_mutex _csMutex;')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'check.cpp'
            path.write_text(code)
            binary = Path(tmp) / 'check'
            result = subprocess.run(['c++', '-std=c++17', '-pthread', str(path), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True)

    def test_private_key_creation_permissions_and_umask_restoration(self):
        trust = module_at('review_trustcache', ROOT / 'tools/dc-trustcache.py')
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            pub = Path(tmp) / 'pub'
            old = os.umask(0o022)
            def generate(args):
                dest = Path(args[-1])
                dest.write_bytes(b'test key')
                if 'genpkey' in args:
                    self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            try:
                with patch.object(trust, 'run', side_effect=generate):
                    trust.ensure_key(Path(tmp) / 'store', key, pub)
                self.assertEqual(os.umask(0o022), 0o022)
                key.unlink()
                with patch.object(trust, 'run', side_effect=RuntimeError('generation failure')):
                    with self.assertRaises(RuntimeError):
                        trust.ensure_key(Path(tmp) / 'store', key, pub)
                self.assertEqual(os.umask(0o022), 0o022)
            finally:
                os.umask(old)

    def test_prefix_trust_requires_an_explicit_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            sdk = folder / 'sdk'
            overlay = folder / 'prefix/Users/test/RuntimeLibraries'
            sdk.mkdir(); overlay.mkdir(parents=True)
            for dest in (sdk / 'sdk.dylib', overlay / 'overlay.dylib'):
                dest.write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 28)
            # Rebind only the absolute SDK fixture path; run the production shell policy.
            signer = folder / 'signer.sh'
            signer.write_text((ROOT / 'tools/sign-darling-runtime.sh').read_text().replace('/usr/local/libexec/darling', str(sdk)))
            dcc = folder / 'dcc'
            dcc.write_text('#!/bin/sh\nif [ "$1" = cdhash ]; then case "$2" in *sdk.dylib) echo sdk-hash;; *) echo overlay-hash;; esac; fi\n')
            dcc.chmod(0o755)
            dct = folder / 'dct.py'
            log = folder / 'entries'
            dct.write_text('import os, pathlib, sys\nif sys.argv[1] == "default-store": print("test-store")\nelse: pathlib.Path(os.environ["DC_POLICY_LOG"]).write_text(pathlib.Path(sys.argv[-1]).read_text())\n')
            env = dict(os.environ, DCC=str(dcc), DCT=str(dct), RCODESIGN='/bin/true', DPREFIX=str(folder / 'prefix'), DC_POLICY_LOG=str(log))
            cases = [(['--prefix'], set()), (['--sdk', '--prefix'], {'sdk-hash'}),
                     ([], {'sdk-hash'}), (['--prefix', '--trust'], {'overlay-hash'}),
                     (['--sdk', '--prefix', '--no-trust'], set())]
            for args, expected in cases:
                with self.subTest(args=args):
                    log.unlink(missing_ok=True)
                    result = subprocess.run(['sh', str(signer), '--allow-running', *args], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    entries = {line.split()[0] for line in log.read_text().splitlines()} if log.exists() else set()
                    self.assertEqual(entries, expected)

    def test_ground_truth_does_not_infer_success_from_unavailable_data(self):
        ground = module_at('review_ground_truth', ROOT / 'tests/security/ground_truth_to_golden.py')
        golden = json.loads((ROOT / 'tests/security/fixtures/macos-golden.json').read_text())
        env = golden['environments'][0]
        env['sip'] = 'enabled'
        env['probe']['status_rc'] = -1
        env['probe']['status_errno'] = 1
        env['probe']['status_flags'] = None
        env['probe']['flag_names'] = []
        env['probe']['entitlements'] = {'rc': -1, 'errno': 1}
        golden['environments'] = [env]
        rendered = ground.render_markdown(golden)
        self.assertIn('unavailable (rc=-1 errno=1)', rendered)
        self.assertNotIn('VMs boot with SIP disabled', rendered)
        self.assertNotIn('empty entitlements blob returns success', rendered)
        self.assertNotIn('therefore reports', rendered)
