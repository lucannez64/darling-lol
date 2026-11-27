"""Regression checks for the security continuation's review findings."""
import importlib.util
import json
import os
import shutil
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
        bin_true = shutil.which('true')
        if not bin_true:
            raise unittest.SkipTest('"true" is unavailable')
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
            env = dict(os.environ, DCC=str(dcc), DCT=str(dct), RCODESIGN=bin_true, DPREFIX=str(folder / 'prefix'),
                       DC_POLICY_LOG=str(log), DARLING_SIGN_CACHE=str(folder / 'sign-cache'))
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

    def test_signing_reuses_cached_copies_of_reinstalled_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = folder / 'runtime'
            (root / 'lib').mkdir(parents=True)
            unsigned = {
                'lib/a.dylib': b'\xcf\xfa\xed\xfe' + b'\x06\0\0\0' * 3 + b'A' * 64,
                'lib/b.dylib': b'\xcf\xfa\xed\xfe' + b'\x06\0\0\0' * 3 + b'B' * 64,
                'lib/c.o': b'\xcf\xfa\xed\xfe' + b'\x07\0\0\0' * 2 + b'\x01\0\0\0' + b'C' * 64,
                'lib/notes.txt': b'plain text',
            }
            def install():  # what `cmake --install` does to signed binaries
                for name, data in unsigned.items():
                    (root / name).write_bytes(data)
            install()
            # Fakes: a signature is a trailing marker; every rcodesign run is logged.
            calls = folder / 'rcodesign.log'
            rcodesign = folder / 'rcodesign'
            rcodesign.write_text('#!/bin/sh\nfor last; do :; done\necho "$last" >> "%s"\n'
                                 'printf SIGNED >> "$last"\n' % calls)
            dcc = folder / 'dcc'
            dcc.write_text('#!/bin/sh\ncase "$1" in cdhash) echo hash;; *) tail -c 6 "$2" | grep -q SIGNED;; esac\n')
            for tool in (rcodesign, dcc):
                tool.chmod(0o755)
            cache = folder / 'cache'
            env = dict(os.environ, DCC=str(dcc), RCODESIGN=str(rcodesign), DARLING_SIGN_CACHE=str(cache))
            command = ['sh', str(ROOT / 'tools/sign-darling-runtime.sh'), '--allow-running', '--no-trust', '--quiet', str(root)]

            def sign(*extra):
                result = subprocess.run(command[:-1] + list(extra) + command[-1:], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout.strip().splitlines()[-1]

            self.assertIn('total=3 signed=2 restored=0 skipped=0', sign())
            self.assertIn('object=1', sign())
            first = {name: (root / name).read_bytes() for name in ('lib/a.dylib', 'lib/b.dylib')}
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            # Reinstalled unchanged binaries come back from the cache, byte for byte.
            install()
            self.assertIn('signed=0 restored=2 skipped=0', sign())
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            for name, data in first.items():
                self.assertEqual((root / name).read_bytes(), data)
            # A changed binary is signed; the unchanged one is still restored.
            install()
            (root / 'lib/b.dylib').write_bytes(unsigned['lib/b.dylib'] + b'v2')
            self.assertIn('signed=1 restored=1', sign())
            # Entries the root no longer uses are pruned; already-signed binaries keep theirs.
            (root / 'lib/b.dylib').unlink()
            self.assertIn('skipped=1', sign())
            [directory] = [path for path in cache.iterdir() if path.is_dir()]
            self.assertEqual(len([path for path in directory.iterdir() if path.name != 'index']), 1)
            install()
            self.assertIn('signed=1 restored=1', sign())
            # --force and --no-cache always sign; a cache others can write is refused.
            install()
            self.assertIn('signed=2 restored=0', sign('--no-cache'))
            install()
            cache.chmod(0o777)
            self.assertIn('signed=2 restored=0', sign())
            cache.chmod(0o755)
            self.assertIn('signed=2 restored=0 skipped=0', sign('--force'))

    def test_parallel_signing_handles_shared_cache_keys_and_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = folder / 'runtime'
            (root / 'a').mkdir(parents=True)
            (root / 'b').mkdir(parents=True)
            # Identical content and basename in two folders compete for the same cache key.
            payload = b'\xcf\xfa\xed\xfe' + b'\x06\0\0\0' * 3 + b'X' * 64
            targets = [
                root / 'a/shared.dylib',
                root / 'b/shared.dylib',
                root / 'a/unique1.dylib',
                root / 'b/unique2.dylib',
            ]
            for idx, target in enumerate(targets):
                target.write_bytes(payload if 'shared' in target.name else payload + bytes([idx]))
            rcodesign = folder / 'rcodesign'
            rcodesign.write_text('#!/bin/sh\nfor last; do :; done\nprintf SIGNED >> "$last"\n')
            dcc = folder / 'dcc'
            dcc.write_text('#!/bin/sh\ncase "$1" in cdhash) echo hash;; *) tail -c 6 "$2" | grep -q SIGNED;; esac\n')
            for tool in (rcodesign, dcc):
                tool.chmod(0o755)
            cache = folder / 'cache'
            env = dict(os.environ, DCC=str(dcc), RCODESIGN=str(rcodesign),
                       DARLING_SIGN_CACHE=str(cache), RCLOL_SIGN_JOBS='4')
            cmd = ['sh', str(ROOT / 'tools/sign-darling-runtime.sh'),
                   '--allow-running', '--no-trust', '--quiet', str(root)]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn('total=4 signed=4 restored=0 skipped=0', res.stdout.splitlines()[-1])
            # Reinstall unsigned copies and restore concurrently with 4 workers.
            for idx, target in enumerate(targets):
                target.write_bytes(payload if 'shared' in target.name else payload + bytes([idx]))
            res2 = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
            self.assertIn('total=4 signed=0 restored=4 skipped=0', res2.stdout.splitlines()[-1])
            # Also verify single-worker mode keeps working.
            res3 = subprocess.run(cmd, env={**env, 'RCLOL_SIGN_JOBS': '1'},
                                  capture_output=True, text=True)
            self.assertEqual(res3.returncode, 0, res3.stdout + res3.stderr)
            self.assertIn('total=4 signed=0 restored=0 skipped=4', res3.stdout.splitlines()[-1])

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

    def test_regeneration_preserves_archived_builds_and_original_provenance(self):
        ground = module_at('review_ground_truth_merge', ROOT / 'tests/security/ground_truth_to_golden.py')
        archive = json.loads((ROOT / 'tests/security/fixtures/macos-golden.json').read_text())
        old = next(e for e in archive['environments'] if e['runner'] == 'macos-14')
        updated = json.loads(json.dumps(next(e for e in archive['environments'] if e['runner'] == 'macos-15')))
        updated['probe']['child_matches_self'] = False
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'golden.json'
            markdown = Path(tmp) / 'report.md'
            output.write_text(json.dumps(archive))
            argv = ['ground_truth_to_golden.py', tmp, '--json', str(output),
                    '--markdown', str(markdown), '--run-id', 'test-new-run', '--date', '2026-10-29']
            with patch('sys.argv', argv), patch.object(ground, 'parse_env', return_value=[updated]):
                ground.main()
                # Repeating the same build refreshes it without duplicating it.
                ground.main()
            merged = json.loads(output.read_text())
            self.assertEqual(len(merged['environments']), len(archive['environments']))
            retained = next(e for e in merged['environments'] if e['runner'] == 'macos-14')
            self.assertEqual(retained, dict(old, source=old.get('source') or {}))
            fresh = next(e for e in merged['environments'] if e['runner'] == 'macos-15')
            self.assertEqual(fresh['source']['run_id'], 'test-new-run')
            self.assertFalse(fresh['probe']['child_matches_self'])
            rendered = markdown.read_text()
            self.assertIn(archive['source'], merged['archive_sources'])
            legacy_row = next(line for line in rendered.splitlines() if line.startswith('| macos-14 |'))
            self.assertTrue(legacy_row.endswith('| unavailable | unavailable |'))
            self.assertIn('test-new-run', rendered)

    def test_legacy_provenance_is_not_inferred_from_archive_metadata(self):
        ground = module_at('review_ground_truth_legacy', ROOT / 'tests/security/ground_truth_to_golden.py')
        golden = json.loads((ROOT / 'tests/security/fixtures/macos-golden.json').read_text())
        old = golden['environments'][0]
        known = dict(old, runner='known-row', source={'run_id': 'established-row-run', 'date': '2026-10-24'})
        previous = dict(golden, environments=[old, known], source={'run_id': 'unproven-archive-run'})
        merged = ground.merge_observations(previous, [], {'run_id': 'fresh-run'})
        unknown = next(e for e in merged if e['runner'] == old['runner'])
        established = next(e for e in merged if e['runner'] == 'known-row')
        self.assertEqual(unknown['source'], {})
        self.assertEqual(established['source'], known['source'])
        rendered = ground.render_markdown(dict(previous, source={}, environments=merged))
        self.assertIn('established-row-run', rendered)
        self.assertNotIn('unproven-archive-run', rendered)
        # Direct rendering of a legacy archive must not infer row provenance either.
        rendered = ground.render_markdown(golden)
        row = next(line for line in rendered.splitlines() if line.startswith('| macos-14 |'))
        self.assertTrue(row.endswith('| unavailable | unavailable |'))
