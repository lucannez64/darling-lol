"""Agent context harness: entrypoint consistency and drift detection."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'agent_context', ROOT / 'scripts/agent-context.py')
agent_context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_context)


class RepositoryTests(unittest.TestCase):
    def test_harness_is_consistent(self):
        self.assertEqual(agent_context.check(), [])

    def test_render_lists_every_read_script_and_flag(self):
        text = agent_context.render()
        for _topic, _when, paths in agent_context.READS:
            for path in paths:
                self.assertIn(f'`{path}`', text)
        for path, _summary in agent_context.SCRIPTS:
            self.assertIn(f'`{path}`', text)
        for name, _default, _effect in agent_context.FLAGS:
            self.assertIn(f'`{name}`', text)

    def test_json_has_every_entry(self):
        data = agent_context.as_dict()
        self.assertEqual(data['entrypoint'], 'AGENTS.md')
        self.assertEqual(len(data['reads']), len(agent_context.READS))
        self.assertEqual(len(data['scripts']), len(agent_context.SCRIPTS))
        self.assertEqual(len(data['flags']), len(agent_context.FLAGS))
        self.assertTrue(all(set(item) == {'topic', 'when', 'paths'}
                            for item in data['reads']))
        self.assertTrue(all(set(item) == {'path', 'summary'}
                            for item in data['scripts']))
        self.assertTrue(all(set(item) == {'name', 'default', 'effect'}
                            for item in data['flags']))

    def test_check_command_passes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agent_context.main(['--check']), 0)
        self.assertIn('PASS: agent harness', output.getvalue())

    def test_json_command_is_valid_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agent_context.main(['--json']), 0)
        self.assertEqual(json.loads(output.getvalue())['entrypoint'], 'AGENTS.md')


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs').mkdir()
        (self.root / 'scripts').mkdir()
        (self.root / 'docs/demo.md').write_text('demo\n')
        (self.root / 'scripts/demo.sh').write_text('#!/bin/sh\n')
        (self.root / 'source.sh').write_text('RCLOL_DEMO=1\n')
        (self.root / 'CLAUDE.md').write_text('Read AGENTS.md\n')
        (self.root / 'AGENTS.md').write_text(
            'docs/demo.md scripts/demo.sh RCLOL_DEMO\n')

    def check(self):
        return agent_context.check(
            root=self.root,
            reads=(('Demo', 'always', ('docs/demo.md',)),),
            scripts=(('scripts/demo.sh', 'demo'),),
            flags=(('RCLOL_DEMO', '1', 'demo'),),
            adapters=('CLAUDE.md',),
            sources=('source.sh',),
        )

    def errors(self):
        return '\n'.join(self.check())

    def test_fixture_passes(self):
        self.assertEqual(self.check(), [])

    def test_missing_context_path_is_reported(self):
        (self.root / 'docs/demo.md').unlink()
        self.assertIn('missing context path', self.errors())
        self.assertIn('docs/demo.md', self.errors())

    def test_entrypoint_must_list_every_context_path(self):
        (self.root / 'AGENTS.md').write_text('')
        self.assertIn('does not list context path', self.errors())

    def test_context_path_with_an_extra_suffix_is_reported(self):
        (self.root / 'AGENTS.md').write_text(
            'docs/demo.md.bak scripts/demo.sh RCLOL_DEMO\n')
        self.assertIn('does not list context path: docs/demo.md', self.errors())

    def test_script_path_with_an_extra_suffix_is_reported(self):
        (self.root / 'AGENTS.md').write_text(
            'docs/demo.md scripts/demo.sh.bak RCLOL_DEMO\n')
        self.assertIn('does not list script: scripts/demo.sh', self.errors())

    def test_missing_script_is_reported(self):
        (self.root / 'scripts/demo.sh').unlink()
        self.assertIn('missing script', self.errors())

    def test_empty_script_summary_is_reported(self):
        errors = agent_context.check(
            root=self.root,
            reads=(),
            scripts=(('scripts/demo.sh', ' '),),
            flags=(),
            adapters=(),
            sources=(),
        )
        self.assertIn('empty script summary', '\n'.join(errors))

    def test_flag_missing_from_sources_is_reported(self):
        (self.root / 'source.sh').write_text('')
        self.assertIn('documented flag is not read', self.errors())

    def test_prefixed_flag_identifier_is_not_a_match(self):
        (self.root / 'source.sh').write_text('RCLOL_DEMO_EXTRA=1\n')
        self.assertIn('documented flag is not read by the project: RCLOL_DEMO',
                      self.errors())

    def test_flag_that_is_only_listed_as_a_prefix_is_reported(self):
        (self.root / 'AGENTS.md').write_text(
            'docs/demo.md scripts/demo.sh RCLOL_DEMO_EXTRA\n')
        self.assertIn('does not list flag: RCLOL_DEMO', self.errors())

    def test_adapter_must_point_to_the_entrypoint(self):
        (self.root / 'CLAUDE.md').write_text('no entrypoint here\n')
        self.assertIn('does not point to AGENTS.md', self.errors())

    def test_missing_entrypoint_is_reported(self):
        (self.root / 'AGENTS.md').unlink()
        self.assertIn('missing entrypoint: AGENTS.md', self.errors())


if __name__ == '__main__':
    unittest.main()
