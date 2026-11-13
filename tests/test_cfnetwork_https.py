"""CFNetwork must be able to load an https URL through NSURLSession.

Four defects made such loads fail or crash: the stream offered at most TLS 1.0, a
Set-Cookie header without a Domain attribute dereferenced NULL, transport errors left
NSURLSession outside NSURLErrorDomain where callers cannot classify them, and a body
with a Content-Encoding other than gzip/deflate (for example "utf-8") was failed as
undecodable.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / 'patches/cfnetwork.patch'


def added(path):
    text = PATCH.read_text()
    start = text.index('diff --git a/' + path + ' ')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end > 0 else len(text)]
    return [line[1:].strip() for line in section.splitlines() if line.startswith('+') and not line.startswith('+++')]


class CFNetworkHTTPS(unittest.TestCase):
    def test_negotiated_streams_offer_tls12(self):
        lines = added('src/Stream/CFSocketStream.c')
        self.assertTrue(any('SSLSetProtocolVersionMax(security, kTLSProtocol12)' in line for line in lines))
        self.assertTrue(any('SSLSetProtocolVersionMin(security, kTLSProtocol1)' in line for line in lines))

    def test_cookie_without_domain_does_not_copy_null(self):
        lines = added('src/Cookies/CFHTTPCookie.c')
        self.assertIn('if (domain != NULL)', lines)
        self.assertIn('if (cookie != NULL) {', lines)

    def test_transport_errors_use_nsurlerrordomain(self):
        lines = added('src/URL/NSURLSession.m')
        self.assertTrue(any('_NSURLSessionTransportError(error, _taskRequest)' in line for line in lines))
        for code in ('NSURLErrorSecureConnectionFailed', 'NSURLErrorServerCertificateUntrusted',
                     'NSURLErrorCannotConnectToHost', 'NSURLErrorCannotFindHost', 'NSURLErrorTimedOut'):
            self.assertTrue(any(code in line for line in lines), code)
        self.assertIn('return error;', lines)  # unrecognised errors pass through unchanged

    def test_unknown_content_encoding_is_passed_through(self):
        lines = added('src/URL/CFURLConnection.c')
        self.assertIn('if (connection->contentEncoding == kContentEncodingGZIP ||', lines)
        self.assertIn('connection->contentEncoding == kContentEncodingDeflate) {', lines)

    def test_inflate_uses_the_connection_stream(self):
        lines = added('src/URL/CFURLConnection.c')
        self.assertIn('z_stream *zStream = connection->zStream;', lines)
        self.assertIn('status = inflate(zStream, Z_BLOCK);', lines)
        self.assertIn('CFDataIncreaseLength(outputData, halfLength > 0 ? halfLength : 1);', lines)

    def test_manifest_counts_the_patched_files(self):
        manifest = json.loads((ROOT / 'upstream/manifest.json').read_text())
        component = next(c for c in manifest['components'] if c.get('patch') == 'patches/cfnetwork.patch')
        self.assertEqual(component['modified_files'], PATCH.read_text().count('\ndiff --git ') + 1)


if __name__ == '__main__':
    unittest.main()
