import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
build_optional = runpy.run_path(str(TOOLS / 'build-display-timing-runtime.py'))['build_optional_wayland']


class WaylandCandidateTests(unittest.TestCase):
    def test_optional_build_and_install(self):
        for status in ('success', 'skip', 'failure'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                build = root / 'build'
                rendered = build / 'runtime/tools'
                rendered.mkdir(parents=True)
                guest = root / 'guest'
                guest.mkdir()
                host_lib = build / 'libdarling-wayland.so'
                guest_lib = guest / 'libDarlingWayland.dylib'
                for active in (host_lib, guest_lib):
                    active.write_text('active')
                host_next = build / 'libdarling-wayland.next.so'
                guest_next = build / 'libDarlingWayland.next.dylib'
                for candidate in (host_next, guest_next):
                    candidate.write_text('stale')
                script = rendered / 'build-darling-wayland.sh'
                script.write_text({
                    'skip': 'exit 0',
                    'failure': f'echo partial > "{host_next}"\nexit 1',
                    'success': f'echo host > "{host_next}"\necho guest > "{guest_next}"',
                }[status])
                build_optional(root)
                subprocess.run(['sh', '-ec', 'project_dir=$1; guest_home=$2; . "$3"',
                                'sh', directory, str(guest), str(TOOLS / 'install-wayland-candidates.sh')],
                               check=True)
                self.assertEqual(host_lib.read_text().strip(), 'host' if status == 'success' else 'active')
                self.assertEqual(guest_lib.read_text().strip(), 'guest' if status == 'success' else 'active')
                self.assertFalse(host_next.exists())
                self.assertFalse(guest_next.exists())

    def test_both_installers_activate_after_stop(self):
        for name in ('gpu', 'presentation'):
            script = (TOOLS / f'install-riot-{name}-runtime.sh').read_text()
            self.assertLess(script.index('build-display-timing-runtime.py'), script.index('stop-darling-runtime.py'))
            self.assertLess(script.index('stop-darling-runtime.py'), script.index('install-wayland-candidates.sh'))
