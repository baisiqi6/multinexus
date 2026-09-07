import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multinexus.config import _load_toml_agent, load_config
from multinexus.adapters.zcode_permissions import ZCodePermissionPolicy, check_windows_path, check_no_links, path_key


class ZCodePermissionConfigTests(unittest.TestCase):
    def load(self, text='', *, env=None):
        with TemporaryDirectory() as td:
            path = Path(td) / 'agents.toml'
            path.write_text('[[agents]]\nid="z"\nadapter="zcode"\ntoken="synthetic"\n' + text)
            with patch.dict(os.environ, env or {}, clear=True):
                return _load_toml_agent(path, 'z')

    def test_defaults_and_exact_commands_preserve_commas_and_spaces(self):
        default = self.load()
        self.assertEqual(default.zcode_transport, 'headless')
        self.assertIsNone(default.zcode_context_root)
        self.assertEqual(default.zcode_permission_commands, [])
        command = ' python -c "print((1, 2))" '
        config = self.load('zcode_permission_commands = [\' python -c "print((1, 2))" \']\n')
        self.assertEqual(config.zcode_permission_commands, [command])
        overridden = self.load(env={'MULTINEXUS_ZCODE_PERMISSION_COMMANDS': json.dumps([command])})
        self.assertEqual(overridden.zcode_permission_commands, [command])

    def test_invalid_types_and_unknown_transport_fail_loud(self):
        for text in ('zcode_transport="rpc"', 'zcode_transport=1',
                     'zcode_permission_commands="python,a"', 'zcode_permission_commands=[1]',
                     'zcode_context_root="relative"'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)
        for value in ('not-json', '"python"', '{}', '[1]', '[null]', '[""]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(env={'MULTINEXUS_ZCODE_PERMISSION_COMMANDS': value})

    def test_service_env_wins_and_shared_dotenv_cannot_enable_permissions(self):
        with TemporaryDirectory() as td:
            path = Path(td) / 'agents.toml'
            path.write_text('[[agents]]\nid="z"\nadapter="zcode"\ntoken="synthetic"\n')

            def fake_dotenv():
                os.environ['MULTINEXUS_ZCODE_TRANSPORT'] = 'app-server'
                os.environ['MULTINEXUS_ZCODE_CONTEXT_ROOT'] = td
                os.environ['MULTINEXUS_ZCODE_PERMISSION_COMMANDS'] = '["unsafe injection"]'

            with patch.dict(os.environ, {}, clear=True), patch('multinexus.config.load_dotenv', side_effect=fake_dotenv):
                config = load_config(['--config', str(path), '--agent', 'z'])
            self.assertEqual(config.zcode_transport, 'headless')
            self.assertEqual(config.zcode_permission_commands, [])
            with patch.dict(os.environ, {'MULTINEXUS_ZCODE_TRANSPORT': 'headless'}, clear=True), patch(
                'multinexus.config.load_dotenv', side_effect=fake_dotenv
            ):
                config = load_config(['--config', str(path), '--agent', 'z'])
            self.assertEqual(config.zcode_transport, 'headless')


class ZCodePermissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.context_root = self.root / 'contexts'
        self.context_root.mkdir()
        self.policy = ZCodePermissionPolicy(self.workspace, self.context_root, ('python -m unittest tests.test_one',))

    def decide(self, tool, value):
        return self.policy.decide(tool, value)

    def test_write_edit_inside_only_and_unknown_schema_denied(self):
        self.assertTrue(self.decide('Write', {'file_path': 'new.py', 'content': 'print(1)'}).allowed)
        self.assertTrue(self.decide('Edit', {'file_path': str(self.workspace / 'new.py'),
                                           'old_string': 'one', 'new_string': 'two', 'replace_all': False}).allowed)
        for tool, value in (
            ('Write', {'file_path': '../outside', 'content': ''}),
            ('Write', {'file_path': str(self.context_root / 'config.json'), 'content': ''}),
            ('Write', {'file_path': 'safe', 'content': '', 'cwd': '..'}),
            ('Edit', {'file_path': 'safe', 'old_string': 'one', 'new_string': 'two', 'replace_all': 'true'}),
            ('ApplyPatch', {'patch': 'anything'}),
        ):
            with self.subTest(tool=tool, value=value):
                self.assertFalse(self.decide(tool, value).allowed)

    def test_protected_paths_and_symlink_escape_are_denied(self):
        for path in ('.git/config', '.Git/index', 'zcode.json', 'nested/zcode.json',
                     '.zcode/config.json', '.ZCODE/CONFIG.JSON'):
            with self.subTest(path=path):
                self.assertFalse(self.decide('Write', {'file_path': path, 'content': ''}).allowed)
        outside = self.root / 'outside'
        outside.mkdir()
        (self.workspace / 'link').symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.decide('Write', {'file_path': 'link/escape', 'content': ''}).allowed)
        (self.workspace / 'file-link').symlink_to(outside / 'absent')
        self.assertFalse(self.decide('Write', {'file_path': 'file-link', 'content': ''}).allowed)
        (outside / 'hardlinked').write_text('unchanged')
        os.link(outside / 'hardlinked', self.workspace / 'hardlink')
        self.assertFalse(self.decide('Edit', {'file_path': 'hardlink', 'old_string': 'unchanged', 'new_string': 'changed'}).allowed)

    def test_commands_are_exact_and_cwd_foreground_schema_checked(self):
        command = self.policy.commands[0]
        self.assertTrue(self.decide('Bash', {'command': command, 'description': 'Tests'}).allowed)
        self.assertTrue(self.decide('Bash', {'command': command, 'cwd': str(self.workspace)}).allowed)
        for value in (
            {'command': command + ' extra'}, {'command': 'echo hi; ' + command},
            {'command': command + ' > output'}, {'command': command + ' '},
            {'command': command, 'run_in_background': True}, {'command': command, 'cwd': '..'},
            {'command': command, 'env': {}}, {'command': command, 'dangerouslyDisableSandbox': True},
        ):
            with self.subTest(value=value):
                self.assertFalse(self.decide('Bash', value).allowed)
        self.assertFalse(ZCodePermissionPolicy(self.workspace, self.context_root, ()).decide('Bash', {'command': command}).allowed)

    def test_windows_path_comparison_handles_case_and_alternative_separators(self):
        self.assertEqual(path_key(r'C:\Work\Repo\file.py', windows=True),
                         path_key('c:/work/repo/file.py', windows=True))
        self.assertNotEqual(path_key(r'C:\Work\Repo', windows=True),
                            path_key(r'C:\Work\Repo-other', windows=True))
        for path in (r'C:\Work\safe.py', 'c:/work/safe.py', 'relative.py'):
            check_windows_path(path)
        for path in (r'\\?\C:\Work\file', r'\\host\share\file', r'C:relative', 'file:stream',
                     r'C:\Work\NUL.txt', r'C:\Work\file.', r'C:\Work\file ', 'CON'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_windows_path(path)

    def test_reparse_attribute_is_rejected_even_without_posix_symlink_bit(self):
        from types import SimpleNamespace
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)):
            with self.assertRaises(ValueError):
                check_no_links(self.workspace / 'junction')


if __name__ == '__main__':
    unittest.main()
