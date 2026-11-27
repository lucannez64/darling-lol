"""Check the system root store generator against OpenSSL without starting Darling.

A stub `darling` runs the guest script on the host with a stub `security` tool, so
the generated trust record, the guest/host hand-off and the idempotency stamp are
exercised; the real keychain database is not.
"""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import plistlib
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/ensure-darling-root-certificates.py'
tool = runpy.run_path(str(SCRIPT))

DARLING_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, subprocess, sys
    with open(os.environ['STUB_LOG'], 'a') as log:
        log.write(' '.join(sys.argv[1:]) + '\\n')
    if os.environ.get('STUB_FAIL'):
        sys.exit(3)
    assert sys.argv[1] == 'shell'
    strip = lambda value: value[len('/Volumes/SystemRoot'):] if value.startswith('/Volumes/SystemRoot/') else value
    env = dict(os.environ, PATH=os.environ['STUB_BIN'] + ':' + os.environ['PATH'])
    sys.exit(subprocess.run([strip(arg) for arg in sys.argv[2:]], env=env).returncode)
''')

SECURITY_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import hashlib, sys
    command, args = sys.argv[1], sys.argv[2:]
    if command == 'create-keychain':
        open(args[-1], 'wb').close()
    elif command == 'add-certificates':
        keychain = args[args.index('-k') + 1]
        with open(keychain, 'a') as stream:
            for path in args[args.index('-k') + 2:]:
                data = open(path, 'rb').read()
                stream.write(hashlib.sha1(data).hexdigest().upper() + '\\n')
    elif command == 'find-certificate':
        for line in open(args[-1]):
            print('keychain: "%s"' % args[-1])
            print('SHA-1 hash: ' + line.strip())
''')


def openssl(der, *options):
    return subprocess.run(['openssl', 'x509', '-inform', 'DER', '-noout', *options], input=der,
                          capture_output=True, check=True).stdout.decode()


def asn1_issuer(der):
    """Issuer bytes located by OpenSSL's own ASN.1 parser: the fourth TBSCertificate field
    (third without an explicit version)."""
    lines = subprocess.run(['openssl', 'asn1parse', '-inform', 'DER'], input=der,
                           capture_output=True, check=True).stdout.decode().splitlines()
    fields = []
    for line in lines:
        match = re.match(r'\s*(\d+):d=(\d+)\s+hl=(\d+)\s+l=\s*(\d+)\s+(\w+):\s*(.*)', line)
        if match and match.group(2) == '2':
            fields.append(match)
    if fields[0].group(6).startswith('cont [ 0 ]'):
        fields = fields[1:]
    offset, header, length = (int(fields[2].group(index)) for index in (1, 3, 4))
    return der[offset:offset + header + length]


@unittest.skipUnless(shutil.which('openssl'), 'openssl is required')
class RootCertificateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = tool['default_bundle']()
        self.data = self.bundle.read_bytes()
        self.prefix = self.root / 'prefix'
        self.prefix.mkdir()
        stubs = self.root / 'bin'
        stubs.mkdir()
        for name, text in (('darling', DARLING_STUB), ('security', SECURITY_STUB)):
            # /usr/bin/env is not everywhere (e.g. the Nix build sandbox).
            (stubs / name).write_text(text.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
            (stubs / name).chmod(0o755)
        self.darling = [str(stubs / 'darling')]
        self.log = self.root / 'darling.log'
        self.log.touch()
        self.environment = {'STUB_LOG': str(self.log), 'STUB_BIN': str(stubs)}

    def run_tool(self, bundle=None, **extra):
        output = io.StringIO()
        saved = dict(os.environ)
        os.environ.update(self.environment, **extra)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                status = tool['main'](['--prefix', str(self.prefix), '--bundle', str(bundle or self.bundle),
                                       '--darling', ' '.join(self.darling)])
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return status, output.getvalue()

    def test_fields_match_openssl(self):
        roots = tool['load_roots'](self.data, report=lambda line: None)
        self.assertGreater(len(roots), 50)
        samples = [roots[0], roots[len(roots) // 2], roots[-1]]
        # Include a serial whose INTEGER needs a leading zero octet, if the bundle has one.
        samples += [cert for cert in roots if cert['serial'][0] == 0][:1]
        for cert in samples:
            fingerprint = openssl(cert['der'], '-fingerprint', '-sha1').split('=', 1)[1].strip()
            self.assertEqual(cert['sha1'], fingerprint.replace(':', ''))
            serial = openssl(cert['der'], '-serial').split('=', 1)[1].strip()
            self.assertEqual(int.from_bytes(cert['serial'], 'big'), int(serial, 16))
            self.assertEqual(cert['issuer'], asn1_issuer(cert['der']))

    def test_trust_record_and_idempotency(self):
        status, output = self.run_tool()
        self.assertEqual(status, 0, output)
        folder = self.prefix / 'System/Library/Keychains'
        record = plistlib.loads((folder / 'SystemTrustSettings.plist').read_bytes())
        self.assertEqual(record['trustVersion'], 1)
        roots = tool['load_roots'](self.data, report=lambda line: None)
        self.assertEqual(set(record['trustList']), {cert['sha1'] for cert in roots})
        for cert in roots[:5]:
            entry = record['trustList'][cert['sha1']]
            self.assertEqual(entry['issuerName'], cert['issuer'])
            self.assertEqual(entry['serialNumber'], cert['serial'])
            self.assertEqual(entry['trustSettings'], [])
            self.assertEqual(hashlib.sha1(cert['der']).hexdigest().upper(), cert['sha1'])
        self.assertEqual(len((folder / 'SystemRootCertificates.keychain').read_text().split()), len(roots))
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        status, output = self.run_tool()
        self.assertEqual((status, output), (0, ''))
        self.assertEqual(len(self.log.read_text().splitlines()), 1, 'unchanged bundle restarted Darling')
        # A changed bundle is rebuilt.
        smaller = self.root / 'smaller.pem'
        smaller.write_bytes(b''.join(re.findall(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n?',
                                                self.data, re.S)[:3]))
        status, output = self.run_tool(smaller)
        self.assertEqual(status, 0, output)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)
        self.assertEqual(len(plistlib.loads((folder / 'SystemTrustSettings.plist').read_bytes())['trustList']), 3)

    def test_failure_is_reported_and_not_stamped(self):
        status, output = self.run_tool(STUB_FAIL='1')
        self.assertEqual(status, 1)
        self.assertIn('Darling system root certificates were not installed', output)
        self.assertFalse((self.prefix / 'System/Library/Keychains' / tool['STAMP']).exists())
        kept = re.search(r'see (\S+)/guest\.log', output)
        self.assertTrue(kept, output)
        shutil.rmtree(kept.group(1))


if __name__ == '__main__':
    unittest.main()
