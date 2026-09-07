import copy
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multinexus.adapters.zcode_context import prepare_context, read_provider
from multinexus.adapters.zcode_windows import _owner_only_sddl
from multinexus.models import AgentConfig
from test_zcode_native_rules import native_database, connect
from multinexus.adapters.zcode_native_rules import native_project_id

SECRET = 'SYNTHETIC-ZCODE-SECRET-MUST-NOT-LEAK'
PROVIDER_ID = 'builtin:bigmodel-coding-plan'
MODEL_ID = 'GLM-5.3'


def source_config():
    return {
        'model': f'{PROVIDER_ID}/{MODEL_ID}',
        'provider': {
            PROVIDER_ID: {
                'name': 'Synthetic provider', 'kind': 'anthropic', 'enabled': True, 'source': 'user',
                'options': {'apiKey': SECRET, 'baseURL': 'http://127.0.0.1:9'},
                'models': {
                    MODEL_ID: {'reasoning': {'enabled': True, 'levels': ['off', 'max'], 'defaultLevel': 'max'},
                               'limit': {'context': 200000, 'input': 180000, 'output': 8192},
                               'modalities': {'input': ['text', 'image'], 'output': ['text']},
                               'zcode': {'ui': 'not-a-runtime-field'}, 'extra': 'DO_NOT_COPY_MODEL_EXTRA'},
                    'GLM-5.2': {'limit': {'context': 128000}, 'extra': 'DO_NOT_COPY_OTHER_MODEL'},
                },
                'extra': 'DO_NOT_COPY_PROVIDER_EXTRA',
            },
            'other': {'options': {'apiKey': 'OTHER_PROVIDER_SECRET'}},
        },
        'permission': {'mode': 'yolo', 'allowedTools': ['Bash', 'Write']},
        'mcp': {'servers': {'danger': {'command': 'DO_NOT_COPY_MCP'}}},
        'hooks': {'enabled': True}, 'plugins': {'enabled': True},
        'storage': {'sessionDbPath': '/DO_NOT_COPY_USER_DB'}, 'extra': 'DO_NOT_COPY_USER_EXTRA',
    }


class ZCodeContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        (self.workspace / '.git').mkdir()
        self.home = self.root / 'source-home'
        self.source = self.home / '.zcode' / 'cli' / 'config.json'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps(source_config()))
        self.config = AgentConfig(id='z', token='', adapter='zcode', zcode_transport='app-server',
                                  zcode_home_dir=str(self.home), zcode_context_root=str(self.root / 'contexts'))

    def prepare(self, resume=None, config=None):
        return prepare_context(config or self.config, str(self.workspace), resume)

    def persist_session(self, context, session='sess_test'):
        database = context.storage / 'session.sqlite'
        native_database(database)
        context.bind_session(session)
        with connect(database) as db:
            db.execute('INSERT INTO session VALUES(?,?,?,?,?)',
                       (session, native_project_id(context.workspace), None, str(context.workspace), str(context.workspace)))
        context.assert_intact(require_persisted_session=True)
        return session

    def test_only_selected_provider_and_narrow_model_metadata_are_derived(self):
        before = self.source.read_bytes()
        context = self.prepare()
        try:
            derived = json.loads(context.config_file.read_text())
            self.assertEqual(set(derived['provider']), {PROVIDER_ID})
            models = derived['provider'][PROVIDER_ID]['models']
            self.assertEqual(set(models), {MODEL_ID})
            self.assertEqual(models[MODEL_ID], {
                'reasoning': {'enabled': True, 'levels': ['off', 'max'], 'defaultLevel': 'max'},
                'limit': {'context': 200000, 'output': 8192}, 'modalities': {'input': ['text', 'image']},
            })
            self.assertEqual(derived['model'], f'{PROVIDER_ID}/{MODEL_ID}')
            self.assertEqual(derived['permission']['allowedTools'], [])
            self.assertEqual(derived['permission']['mode'], 'build')
            self.assertFalse(derived['hooks']['enabled'])
            self.assertFalse(derived['features']['mcp'])
            self.assertFalse(derived['plugins']['enabled'])
            self.assertNotIn('DO_NOT_COPY', json.dumps(derived))
            self.assertNotIn('OTHER_PROVIDER_SECRET', json.dumps(derived))
            self.assertNotIn(SECRET, repr(context))
            self.assertEqual(context.provider.redact('echo ' + SECRET), 'echo [REDACTED]')
            if os.name != 'nt':
                self.assertEqual(context.config_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(context.directory.stat().st_mode & 0o777, 0o700)
        finally:
            context.close()
        self.assertFalse(context.config_file.exists())
        self.assertEqual(self.source.read_bytes(), before)

    def test_unknown_auth_or_complex_reasoning_is_refused_without_fallback(self):
        for mutate in (
            lambda c: c.update(model={'main': 'other/model'}),
            lambda c: c['provider'][PROVIDER_ID]['options'].update(headers={'X-Secret': SECRET}),
            lambda c: c['provider'][PROVIDER_ID]['models'][MODEL_ID]['reasoning'].update(providerOptionsByLevel={'max': {}}),
            lambda c: c['provider'][PROVIDER_ID].update(enabled=False),
        ):
            value = source_config()
            mutate(value)
            self.source.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                read_provider(self.home)

    def test_desktop_reasoning_names_map_to_same_cold_model_choices(self):
        value = source_config()
        value['provider'][PROVIDER_ID]['models'][MODEL_ID]['reasoning'] = {
            'enabled': True, 'variants': ['high', 'low', 'max'], 'defaultVariant': 'max'}
        self.source.write_text(json.dumps(value))
        before = self.source.read_bytes()
        context = self.prepare()
        try:
            expected = {'enabled': True, 'levels': ['high', 'low', 'max'], 'defaultLevel': 'max'}
            derived = json.loads(context.config_file.read_text())
            self.assertEqual(derived['provider'][PROVIDER_ID]['models'][MODEL_ID]['reasoning'], expected)
            actual = context.provider.runtime_model()['provider']['models'][0]['reasoning']
            self.assertEqual(actual, {'enabled': True, 'levels': [
                {'value': name, 'label': name} for name in ['high', 'low', 'max']], 'defaultLevel': 'max'})
            self.assertEqual(self.source.read_bytes(), before)
        finally:
            context.close()

    def test_desktop_reasoning_mixed_or_invalid_choices_fail_closed(self):
        for reasoning in (
            {'enabled': True, 'variants': ['max'], 'levels': ['low']},
            {'enabled': True, 'variants': ['max'], 'defaultLevel': 'max'},
            {'enabled': True, 'variants': ['max'], 'defaultVariant': 'low'},
            {'enabled': True, 'variants': [{'value': 'max'}]},
            {'enabled': True, 'variants': ['max'], 'providerOptionsByLevel': {}},
        ):
            with self.subTest(reasoning=reasoning):
                value = source_config()
                value['provider'][PROVIDER_ID]['models'][MODEL_ID]['reasoning'] = reasoning
                self.source.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    read_provider(self.home)

    def test_redaction_happens_before_json_escaping(self):
        value = source_config()
        secret = 'synthetic-quote-"-and-slash-\\-secret'
        value['provider'][PROVIDER_ID]['options']['apiKey'] = secret
        self.source.write_text(json.dumps(value))
        provider = read_provider(self.home)
        safe = provider.redact_value({'command': 'prefix ' + secret, 'nested': [secret]})
        self.assertEqual(safe, {'command': 'prefix [REDACTED]', 'nested': ['[REDACTED]']})

    def test_cold_runtime_model_maps_only_selected_connection_and_metadata(self):
        before = self.source.read_bytes()
        provider = read_provider(self.home)
        runtime = provider.runtime_model()
        self.assertEqual(set(runtime), {'revision', 'generatedAt', 'model', 'provider'})
        self.assertTrue(runtime['revision'].startswith('multinexus-'))
        self.assertGreater(runtime['generatedAt'], 0)
        self.assertEqual(runtime['model'], provider.model_ref)
        self.assertEqual(runtime['provider'], {
            'providerId': PROVIDER_ID, 'kind': 'anthropic', 'source': 'ephemeral', 'label': 'Synthetic provider',
            'baseURL': 'http://127.0.0.1:9', 'apiKey': {'source': 'inline', 'value': SECRET},
            'models': [{'modelId': MODEL_ID, 'contextWindow': 200000, 'maxOutputTokens': 8192,
                        'supportsImages': True, 'supportsPdf': False, 'supportsVideo': False,
                        'reasoning': {'enabled': True, 'levels': [{'value': 'off', 'label': 'off'},
                                                               {'value': 'max', 'label': 'max'}], 'defaultLevel': 'max'}}],
        })
        self.assertNotIn('DO_NOT_COPY', json.dumps(runtime))
        self.assertNotIn('OTHER_PROVIDER_SECRET', json.dumps(runtime))
        self.assertNotIn(SECRET, json.dumps(provider.redact_value(runtime)))
        self.assertNotIn(SECRET, repr(provider))
        self.assertEqual(self.source.read_bytes(), before)

    def test_cold_runtime_model_without_metadata_and_boolean_reasoning(self):
        for reasoning in (None, True, False):
            value = source_config()
            selected = value['provider'][PROVIDER_ID]
            selected.pop('models')
            if reasoning is not None:
                selected['models'] = {MODEL_ID: {'reasoning': reasoning}}
            self.source.write_text(json.dumps(value))
            model = read_provider(self.home).runtime_model()['provider']['models'][0]
            expected = {'modelId': MODEL_ID}
            if reasoning is not None:
                expected['reasoning'] = {'enabled': reasoning, 'levels': []}
            self.assertEqual(model, expected)

    def test_cold_runtime_model_refuses_ambiguous_reasoning_defaults(self):
        for reasoning in ({}, {'defaultLevel': 'max'}, {'enabled': True, 'defaultLevel': 'max'}):
            value = source_config()
            value['provider'][PROVIDER_ID]['models'][MODEL_ID]['reasoning'] = reasoning
            self.source.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'unambiguous reasoning'):
                read_provider(self.home).runtime_model()

    def test_environment_is_allowlisted_and_homedir_database_explicit(self):
        context = self.prepare()
        self.addCleanup(context.close)
        source_env = {'PATH': '/synthetic-bin', 'HOME': '/real-home', 'USERPROFILE': '/real-profile',
                      'NODE_OPTIONS': '--require=/evil', 'COORDINATE_REMOTE_MCP_TOKEN': SECRET,
                      'ANTHROPIC_API_KEY': SECRET, 'MULTINEXUS_COORDINATE_HTTP_TOKEN_FILE': '/secret',
                      'DISCORD_TOKEN': SECRET, 'ZCODE_MODEL': 'wrong/provider'}
        with patch.dict(os.environ, source_env, clear=True):
            env = context.environment()
        self.assertEqual(env['HOME'], str(context.home))
        self.assertEqual(env['USERPROFILE'], str(context.home))
        self.assertEqual(env['ZCODE_SESSION_DB_PATH'], str(context.storage / 'session.sqlite'))
        self.assertEqual(env['PATH'], '/synthetic-bin')
        for key in source_env.keys() - {'PATH', 'HOME', 'USERPROFILE'}:
            self.assertNotIn(key, env)
        self.assertNotIn(SECRET, repr(env))

    def test_resume_reuses_exact_context_and_rejects_unknown_or_policy_drift(self):
        first = self.prepare()
        session = self.persist_session(first)
        directory = first.directory
        first.close()
        resumed = self.prepare(session)
        self.assertEqual(resumed.directory, directory)
        resumed.close()
        with self.assertRaisesRegex(ValueError, 'created by this client'):
            self.prepare('sess_unknown')
        changed = copy.copy(self.config)
        changed.zcode_permission_commands = ['python -m unittest']
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.prepare(session, changed)
        value = source_config()
        value['model'] = f'{PROVIDER_ID}/GLM-5.2'
        self.source.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.prepare(session)

    def test_project_config_rejected_to_nearest_git_root_only(self):
        (self.root / 'zcode.json').write_text('{}')
        context = self.prepare()  # The parent config is beyond the Git root.
        context.close()
        nested = self.workspace / 'nested'
        nested.mkdir()
        (self.workspace / '.zcode').mkdir()
        (self.workspace / '.zcode' / 'config.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'project configuration'):
            prepare_context(self.config, str(nested))

    def test_context_workspace_overlap_and_linked_locator_are_refused(self):
        bad = copy.copy(self.config)
        bad.zcode_context_root = str(self.workspace / 'contexts')
        with self.assertRaisesRegex(ValueError, 'separate'):
            self.prepare(config=bad)
        context = self.prepare()
        session = self.persist_session(context)
        locator = context._locator()
        context.close()
        preserved = self.root / 'preserved.json'
        preserved.write_bytes(locator.read_bytes())
        locator.unlink()
        locator.symlink_to(preserved)
        with self.assertRaises(ValueError):
            self.prepare(session)
        self.assertTrue(preserved.exists())

    def test_configuration_drift_and_concurrent_resume_are_fail_closed(self):
        context = self.prepare()
        self.addCleanup(context.close)
        session = self.persist_session(context)
        with self.assertRaisesRegex(ValueError, 'already active'):
            self.prepare(session)
        current = json.loads(context.config_file.read_text())
        current['permission']['allowedTools'] = ['Bash']
        context.config_file.write_text(json.dumps(current))
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            context.assert_intact()

    def test_harmless_config_reformat_and_unicode_escaping_preserve_integrity(self):
        source = source_config()
        source['provider'][PROVIDER_ID]['name'] = '合成 provider'
        self.source.write_text(json.dumps(source))
        context = self.prepare()
        self.addCleanup(context.close)
        original = context.config_file.read_bytes()
        value = json.loads(original)
        context.config_file.write_text(json.dumps(dict(reversed(list(value.items()))), indent=4, ensure_ascii=True) + '\n')
        self.assertNotEqual(context.config_file.read_bytes(), original)
        context.assert_intact()

    def test_config_reformat_does_not_hide_semantic_additions_removals_or_changes(self):
        context = self.prepare()
        self.addCleanup(context.close)
        original = json.loads(context.config_file.read_text())
        mutations = (
            lambda c: c['permission'].update(allowedTools=['Bash']),
            lambda c: c['provider'][PROVIDER_ID]['options'].update(apiKey='changed-synthetic-key'),
            lambda c: c['hooks'].update(enabled=True),
            lambda c: c['storage'].update(sessionDbPath='/other/database'),
            lambda c: c['features'].pop('mcp'),
            lambda c: c.update(unexpected={}),
            lambda c: c['permission'].update(autoApproveHighRisk=0),
        )
        for mutate in mutations:
            changed = copy.deepcopy(original)
            mutate(changed)
            context.config_file.write_text(json.dumps(changed, indent=2) + '\n')
            with self.subTest(mutate=mutate), self.assertRaisesRegex(ValueError, 'configuration changed'):
                context.assert_intact()

    def test_duplicate_keys_and_nonstandard_numbers_are_not_canonical_equivalence(self):
        context = self.prepare()
        self.addCleanup(context.close)
        original = context.config_file.read_text()
        nested_duplicate = original.replace('"mode":"build"', '"mode":"build","mode":"build"', 1)
        cases = [nested_duplicate, '{"hooks":{},' + original[1:]]
        cases.extend(original.replace('"autoApproveHighRisk":false', '"autoApproveHighRisk":' + number, 1)
                     for number in ('NaN', 'Infinity', '-Infinity', '1e999'))
        for raw in cases:
            self.assertNotEqual(raw, original)
            context.config_file.write_text(raw)
            with self.subTest(raw=raw[:80]), self.assertRaisesRegex(ValueError, 'Invalid ZCode configuration JSON'):
                context.assert_intact()

    def test_previous_policy_version_locator_is_rejected_without_rule_migration(self):
        from multinexus.adapters import zcode_context
        with patch.object(zcode_context, 'POLICY_VERSION', 'zcode-native-scoped-v1'):
            first = self.prepare()
            session = self.persist_session(first)
            first.close()
        before = (first.storage / 'session.sqlite').read_bytes()
        with self.assertRaisesRegex(ValueError, 'policy'):
            self.prepare(resume=session)
        self.assertEqual((first.storage / 'session.sqlite').read_bytes(), before)

    def test_windows_acl_validator_rejects_inherited_or_other_principal_access(self):
        sid = 'S-1-5-21-123-456-789-1001'
        self.assertTrue(_owner_only_sddl(f'O:{sid}D:P(A;OICI;FA;;;{sid})', sid))
        self.assertFalse(_owner_only_sddl(f'O:{sid}D:AI(A;OICI;FA;;;{sid})', sid))
        self.assertFalse(_owner_only_sddl(f'O:{sid}D:P(A;OICI;FA;;;WD)', sid))
        self.assertFalse(_owner_only_sddl(f'O:{sid}D:P(A;OICI;FA;;;{sid})(A;;FR;;;BU)', sid))


    def test_windows_native_inherited_file_requires_private_parent_and_same_owner(self):
        from multinexus.adapters.zcode_windows import verify_private
        path = self.home / 'native-config.json'
        path.write_text('{}')
        good_file = 'O:SYD:(A;ID;FA;;;SY)'
        good_parent = 'O:SYD:P(A;OICI;FA;;;SY)'
        module = 'multinexus.adapters.zcode_windows.'
        with patch(module + '_current_sid', return_value='S-1-5-18'):
            with patch(module + '_read_sddl', side_effect=[good_file, good_parent]):
                verify_private(path, allow_inherited_file=True)
            with patch(module + '_read_sddl', side_effect=['O:SYD:(A;;FA;;;SY)', good_parent]):
                verify_private(path, allow_inherited_file=True)  # Actual GetFileSecurityW output.
            with patch(module + '_read_sddl', return_value=good_file):
                with self.assertRaises(ValueError):
                    verify_private(path)  # Directories/default must stay protected.
            for child, parent in (
                (good_file, 'O:SYD:AI(A;OICI;FA;;;SY)'),
                (good_file, 'O:SYD:P(A;OICI;FA;;;SY)(A;;FR;;;BU)'),
                ('O:BAD:(A;ID;FA;;;SY)', good_parent),
                ('O:SYD:(A;ID;FA;;;SY)(A;ID;FR;;;WD)', good_parent),
                ('O:SYD:(A;IDIO;FA;;;SY)', good_parent),
            ):
                with self.subTest(child=child, parent=parent):
                    with patch(module + '_read_sddl', side_effect=[child, parent]):
                        with self.assertRaises(ValueError):
                            verify_private(path, allow_inherited_file=True)
            with patch(module + '_read_sddl', return_value=good_file):
                with self.assertRaises(ValueError):
                    verify_private(self.home, allow_inherited_file=True)


if __name__ == '__main__':
    unittest.main()
