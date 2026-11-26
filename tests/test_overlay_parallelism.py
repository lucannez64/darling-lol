"""The overlay builders run concurrently without losing their ordering constraints."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OverlayPoolTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / 'tools/install-riot-gpu-runtime.sh').read_text()

    def test_metal_runs_after_the_independent_pool(self):
        list_start = self.script.index("printf '%s\\n' \\")
        pool = self.script.index('sh tools/run-parallel.sh')
        metal = self.script.index('python3 tools/build-riot-metal-runtime.py')
        self.assertLess(list_start, pool)
        self.assertLess(pool, metal)
        builders = self.script[list_start:pool]
        self.assertIn('build-riot-indium-runtime.py', builders)
        self.assertNotIn('build-riot-metal-runtime.py', builders)

    def test_every_builder_goes_through_the_pool(self):
        builders = self.script[self.script.index("printf '%s\\n' \\"):
                               self.script.index('sh tools/run-parallel.sh')]
        for name in ('build-riot-x11-runtime.py', 'build-riot-server-runtime.py',
                     'build-m58f333-iosurface.py'):
            self.assertIn("'python3 tools/%s'" % name, builders)
        # Metal is the only builder allowed outside the pool: it waits for it.
        direct = [line.strip() for line in self.script.splitlines()
                  if line.strip().startswith('python3 tools/build-')]
        self.assertEqual(direct, ['python3 tools/build-riot-metal-runtime.py'])


if __name__ == '__main__':
    unittest.main()
