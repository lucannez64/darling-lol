"""Interrupted source initialization cannot repair a different checkout."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class RecoveryTests(unittest.TestCase):
    def test_recovery_is_scoped_and_includes_nested_submodules(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/prepare.py').read_text())
        branch = next(node for node in tree.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "not (source / '.git').exists()")
        code = compile(ast.Module(body=branch.orelse, type_ignores=[]), 'prepare.py', 'exec')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / 'first', root / 'second'
            for source in (first, second):
                (source / 'parent').mkdir(parents=True)
            (second / 'parent/.git').touch()
            user_file = second / 'parent/user-edit'
            user_file.write_text('keep')
            run = Mock(side_effect=subprocess.CalledProcessError(1, 'fetch'))
            repair = Mock()
            namespace = dict(ROOT=root, source=first, a=SimpleNamespace(check_only=False, jobs=2),
                             hashlib=hashlib, os=os, json=json, fetch_pathspecs=lambda _: ['parent'],
                             run=run, repair=repair, submodules=lambda parent: [parent / 'nested'],
                             LFS_URL='lfs', LFS_FILTERS=[])
            with self.assertRaises(subprocess.CalledProcessError):
                exec(code, namespace)
            # The marker left by A must never cause initialized B to be repaired.
            namespace['source'] = second
            exec(code, namespace)
            repair.assert_not_called()
            self.assertEqual(user_file.read_text(), 'keep')
            # Retry A after its interrupted checkout created .git already.
            (first / 'parent/.git').touch()
            run.side_effect = None
            namespace['source'] = first
            exec(code, namespace)
            repair.assert_called_once_with(first, repos=[first / 'parent', first / 'parent/nested'])
            self.assertFalse(list((root / 'build').glob('pending-submodules-*')))
