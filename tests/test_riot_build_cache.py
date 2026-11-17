"""Exercise cache decisions with real header files and Make dependency lists."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'riot_build_cache', Path(__file__).resolve().parents[1] / 'tools/riot_build_cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class BuildCacheTests(unittest.TestCase):
    def test_escaped_headers_are_hashed_and_edits_invalidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / 'sdk headers#$file.h'
            header.write_text('old')
            dep = root / 'object.d'
            escaped = str(header).replace('$', '$$').replace('#', '\\#').replace(' ', '\\ ')
            dep.write_text('object: ' + escaped + '\n')
            before = ['source', cache.dependency_chunk([dep])]
            self.assertTrue(cache.stable_inputs(before, before))
            header.write_text('new')
            after = ['source', cache.dependency_chunk([dep])]
            self.assertFalse(cache.stable_inputs(before, after))
            output = root / 'lib.dylib'
            output.write_text('compiled')
            stamp = root / 'stamp.json'
            cache.record(stamp, cache.fingerprint(before))
            self.assertFalse(cache.up_to_date(stamp, cache.fingerprint(after), [output]))
            cache.record(stamp, cache.fingerprint(after))
            self.assertTrue(cache.up_to_date(stamp, cache.fingerprint(after), [output]))
            output.unlink()
            self.assertFalse(cache.up_to_date(stamp, cache.fingerprint(after), [output]))

    def test_unknown_or_changing_dependencies_cannot_be_stamped(self):
        for dependency in ('unreadable-dependencies', 'unreadable-dependency-files'):
            chunks = ['source', dependency]
            self.assertFalse(cache.stable_inputs(chunks, chunks))
            self.assertFalse(cache.stable_inputs(chunks, ['source', 'dependencies:header']))
        self.assertFalse(cache.stable_inputs(['old', 'dependencies:a'], ['new', 'dependencies:a']))
        self.assertFalse(cache.stable_inputs(['source', 'dependencies:a'], ['source', 'dependencies:b']))


if __name__ == '__main__':
    unittest.main()
