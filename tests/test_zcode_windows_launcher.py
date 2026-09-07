import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch, Mock

from multinexus.adapters import zcode_windows


class NativeLauncherTests(unittest.TestCase):
    def test_owner_failure_never_starts_native(self):
        with patch.object(zcode_windows, '_set_current_process_owner', side_effect=OSError('owner denied')):
            with patch.object(zcode_windows.subprocess, 'run') as run:
                with self.assertRaises(OSError):
                    zcode_windows._run_native_child(['native', 'app-server'])
                run.assert_not_called()

    def test_owner_is_verified_before_native_and_exit_code_is_preserved(self):
        observed = []
        command = ['node.exe', 'native.cjs', 'app-server', '--cwd', 'C:/workspace with spaces']
        def spawn(argv):
            self.assertEqual(observed, ['owner_verified'])
            self.assertEqual(argv, command)
            return Mock(returncode=23)
        with patch.object(zcode_windows, '_set_current_process_owner', side_effect=lambda: observed.append('owner_verified')):
            with patch.object(zcode_windows.subprocess, 'run', side_effect=spawn):
                self.assertEqual(zcode_windows._run_native_child(command), 23)

    def test_restriction_owner_failure_never_opens_database(self):
        from multinexus.adapters import zcode_native_rules as rules
        with patch.object(zcode_windows, '_set_current_process_owner', side_effect=OSError('owner denied')):
            with patch.object(rules, '_connection') as connection:
                with self.assertRaises(OSError):
                    zcode_windows._run_restriction_child(['verify', '/private/session.sqlite', '/workspace', 'sess_fixture', '0'])
                connection.assert_not_called()

    def test_restriction_child_failure_or_timeout_never_returns_evidence(self):
        from multinexus.adapters import zcode_native_rules as rules
        for outcome in (Mock(returncode=125, stdout=b''), subprocess.TimeoutExpired('private check', 5)):
            with self.subTest(outcome=type(outcome).__name__):
                with patch.object(rules.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
                    with patch.object(rules.subprocess, 'run') as run:
                        if isinstance(outcome, Exception):
                            run.side_effect = outcome
                        else:
                            run.return_value = outcome
                        with self.assertRaises(rules.ZCodeNativeRuleError):
                            rules._windows_restriction('verify', Path('/private/session.sqlite'), Path('/workspace'), 'sess_fixture')
