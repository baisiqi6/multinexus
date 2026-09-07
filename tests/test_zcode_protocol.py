import asyncio
import copy
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multinexus.adapters.base import OUTCOME_FAILED, OUTCOME_SUCCESS, OUTCOME_TIMED_OUT
from multinexus.adapters.zcode import ZCodeAdapter
from multinexus.models import AgentConfig
from test_zcode_context import MODEL_ID, PROVIDER_ID, SECRET, source_config
from test_zcode_notifications import observations
from test_zcode_native_rules import native_database, connect
from multinexus.adapters.zcode_native_rules import native_project_id


class _ProbeProcess:
    pid = 7901
    returncode = 0

    def __init__(self, env, *, bad_home=False):
        self.env = env
        self.bad_home = bad_home

    async def communicate(self):
        return json.dumps({'home': '/wrong' if self.bad_home else self.env['HOME'],
                           'db': self.env['ZCODE_SESSION_DB_PATH']}).encode(), b''


class _Stdin:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        self.process.receive(json.loads(data))

    async def drain(self):
        pass

    def close(self):
        self.process.close()


class _NativeProcess:
    def __init__(self, env, cwd, *, scenario='success', number=1):
        self.pid = 7901 + number
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdin = _Stdin(self)
        self.env = env
        self.cwd = Path(cwd)
        self.scenario = scenario
        self.session = f'sess_native_{number}'
        self.turn = f'turn_native_{number}'
        self.seq = 5
        self.started = asyncio.Event()
        self.exited = asyncio.Event()
        self.requests = []
        self.client_replies = []
        self.pending = {}
        self.handled = set()
        self.input_id = None
        self.terminal_emitted = False
        self.finalization_reads = 0
        self.restore_warning = False
        self.config_file = Path(env['HOME']) / '.zcode/cli/config.json'
        self.derived_config = json.loads(self.config_file.read_text())
        database = Path(env['ZCODE_SESSION_DB_PATH'])
        if not database.exists():
            native_database(database)
        if scenario == 'native_rules_legacy':
            with connect(database) as db:
                db.execute('INSERT INTO permission VALUES(?,1,1,?)', (native_project_id(self.cwd), '{}'))
        if scenario == 'format_config':
            self.config_file.write_text(json.dumps(self.derived_config, indent=2) + '\n')
        self.actions = [
            ('Write', {'file_path': str(self.cwd / 'inside.txt'), 'content': 'safe synthetic content'}),
            ('Bash', {'command': 'python -m unittest tests.test_one', 'description': 'Tests'}),
            ('Write', {'file_path': str(self.cwd.parent / 'outside.txt'), 'content': 'denied'}),
            ('Bash', {'command': 'python -m unittest tests.test_one; echo extra'}),
        ]

    def emit(self, value):
        self.stdout.feed_data((json.dumps(value) + '\n').encode())

    def response(self, rid, result):
        self.emit({'id': rid, 'result': result})

    def event(self, typ, payload, **overrides):
        self.seq += 1
        event = {'eventId': f'evt_{self.seq}', 'sessionId': self.session, 'turnId': self.turn,
                 'seq': self.seq, 'timestamp': self.seq * 1000, 'type': typ, 'payload': payload}
        event.update(overrides)
        self.emit({'method': 'session/event', 'params': event})
        return event

    def snapshot(self, *, mode='build'):
        model = {'providerId': PROVIDER_ID, 'modelId': MODEL_ID}
        if self.scenario == 'model_mismatch':
            model = {**model, 'modelId': 'SILENT_FALLBACK'}
        runtime = {'eventSeq': self.seq, 'pendingRequestIds': []}
        status = 'error' if self.terminal_emitted and self.scenario == 'failed_terminal' else 'idle'
        if self.terminal_emitted:
            if (self.scenario == 'drain_busy' and self.finalization_reads < 3) or self.scenario == 'drain_never':
                runtime['activeTurnId'] = self.turn
            if self.scenario == 'drain_wrong_turn':
                runtime['activeTurnId'] = 'unrelated_turn'
            if self.scenario == 'drain_pending':
                runtime['pendingRequestIds'] = ['pending_permission']
            if self.scenario == 'drain_stale_sequence':
                runtime['eventSeq'] = 0
            if self.scenario == 'drain_error_projection':
                status = 'error'
            if self.scenario == 'drain_model_drift':
                model['modelId'] = 'changed_model'
        projection = {'status': status}
        if self.restore_warning:
            projection['lastError'] = {'type': 'ZCODE_RUNTIME_MODEL_UNAVAILABLE', 'message': 'SYNTHETIC ' + SECRET}
        return {'protocol': {'name': 'ZCode Protocol', 'version': 1},
                'session': {'sessionId': 'sess_wrong' if self.scenario == 'session_mismatch' else self.session,
                            'workspace': {'workspacePath': str(self.cwd), 'workspaceKey': str(self.cwd)}},
                'settings': {'mode': {'current': mode}, 'permission': {'mode': mode}, 'model': {'current': model}},
                'runtime': runtime, 'projection': projection,
                'messages': [{'type': 'reasoning', 'text': 'PRIVATE_REASONING_MUST_NOT_BE_CAPTURED'}]}

    def receive(self, message):
        if 'method' not in message:
            self.client_replies.append(message)
            pending = self.pending.pop(message['id'], None)
            if pending is None:
                return
            kind, value = pending
            if kind == 'create':
                self.response(value, self.snapshot())
            elif kind == 'tool' and value not in self.handled:
                self.handled.add(value)
                self.permission(value + 1)
            return
        self.requests.append(message)
        method, params, rid = message['method'], message['params'], message['id']
        if method == 'session/create':
            self.pending['server-preferences'] = ('create', rid)
            self.emit({'id': 'server-preferences', 'method': 'session/requestRuntimePreferences',
                       'params': {'sessionId': self.session, 'scope': 'runtime-materialization'}})
        elif method == 'session/resume':
            self.session = params['sessionId']
            if self.scenario == 'resume_requires_runtime_model':
                runtime_model = params.get('runtimeModel', {})
                self.restore_warning = (runtime_model.get('model') != {'providerId': PROVIDER_ID, 'modelId': MODEL_ID}
                                        or runtime_model.get('provider', {}).get('apiKey') != {'source': 'inline', 'value': SECRET})
            if self.scenario == 'resume_model_unavailable':
                self.restore_warning = True
            if self.scenario == 'resume_missing':
                self.emit({'id': rid, 'error': {'code': -32004, 'message': 'SYNTHETIC ' + SECRET}})
            else:
                self.response(rid, self.snapshot(mode='plan'))
        elif method in ('session/setMode', 'session/read'):
            if method == 'session/read' and self.terminal_emitted:
                self.finalization_reads += 1
                if self.scenario == 'drain_eof':
                    self.close(code=0)
                    return
                if self.scenario == 'drain_late_permission':
                    self.permission(0)
            self.response(rid, self.snapshot())
        elif method == 'session/subscribe':
            if self.scenario == 'native_rules_missing_on_subscribe':
                with connect(Path(self.env['ZCODE_SESSION_DB_PATH'])) as db:
                    db.execute("DELETE FROM local_setting WHERE key='ruleset'")
            self.response(rid, {'sessionId': self.session, 'eventSeq': self.seq, 'events': []})
        elif method == 'session/send':
            if self.restore_warning:
                self.emit({'id': rid, 'error': {'code': -32031, 'message': 'SYNTHETIC ' + SECRET,
                                               'data': {'code': 'ZCODE_RUNTIME_MODEL_UNAVAILABLE'}}})
                return
            self.input_id = params['inputId']
            with connect(Path(self.env['ZCODE_SESSION_DB_PATH'])) as db:
                if not db.execute('SELECT 1 FROM session WHERE id=?', (self.session,)).fetchone():
                    db.execute('INSERT INTO session VALUES(?,?,?,?,?)',
                               (self.session, native_project_id(self.cwd), None, str(self.cwd), str(self.cwd)))
            self.started.set()
            if self.scenario in ('native_admission_order', 'admission_without_terminal'):
                for reason, revision in (('prompt_started', 2), ('prompt_completed', 3)):
                    self.emit({'method': 'state.updated', 'params': {'type': 'state.updated', 'scope': 'session',
                               'sessionId': self.session, 'revision': revision, 'reason': reason}})
            if self.scenario == 'observations':
                for observation_method, observation_params in observations():
                    self.emit({'method': observation_method, 'params': observation_params})
            self.event('turn.started', {'inputId': self.input_id, 'queryId': params['queryId'], 'turnNumber': 1})
            self.event('model.streaming', {'kind': 'reasoning_delta', 'delta': 'PRIVATE_REASONING_MUST_NOT_BE_CAPTURED'})
            self.permission(0)  # Interleaves a callback before the client receives accepted.
            if self.returncode is None:
                self.response(rid, {'sessionId': self.session, 'accepted': True, 'stateRevision': 1})
        elif method == 'session/stop':
            pass
        else:
            raise AssertionError(method)

    def permission(self, index):
        if self.scenario == 'hang':
            return
        if self.scenario == 'eof':
            self.close(code=1)
            return
        if self.scenario == 'unknown_callback':
            self.emit({'id': 'server-unknown', 'method': 'interaction/requestProviderRuntimeHeaders',
                       'params': {'sessionId': self.session, 'secret': SECRET}})
            return
        if self.scenario == 'framing':
            self.stdout.feed_data(b'{"jsonrpc":"2.0","id":"alien","result":{}}\n')
            return
        if index == len(self.actions):
            self.complete()
            return
        tool, value = self.actions[index]
        params = {'sessionId': self.session, 'turnId': self.turn, 'requestId': f'perm_{index}',
                  'toolCallId': f'tool_{index}', 'toolName': tool, 'reason': 'Explicit approval required',
                  'riskLevel': 'high' if tool == 'Bash' else 'medium', 'input': value,
                  'options': [{'optionId': 'allow_once', 'kind': 'allow_once', 'name': 'Allow once', 'response': {'decision': 'allow'}}]}
        if self.scenario == 'stray_permission':
            params['turnId'] = 'turn_other'
        if self.scenario == 'permission_schema':
            params['options'] = [{}]
        if self.scenario == 'config_drift':
            self.config_file.write_text('{}')
        rid = f'server-permission-{index}'
        self.pending[rid] = ('tool', index)
        self.emit({'id': rid, 'method': 'interaction/requestPermission', 'params': params})
        if index == 0 and self.scenario in ('duplicate', 'conflicting_duplicate'):
            duplicate = copy.deepcopy(params)
            if self.scenario == 'conflicting_duplicate':
                duplicate['input']['file_path'] = str(self.cwd.parent / 'changed.txt')
            self.pending['server-reannounced'] = ('tool', index)
            self.emit({'id': 'server-reannounced', 'method': 'interaction/requestPermission', 'params': duplicate})

    def complete(self):
        result_type = 'cancelled' if self.scenario == 'cancelled_terminal' else 'success'
        response = 'Native final ' + SECRET if self.scenario == 'redaction' else 'Native final'
        payload = {'inputId': self.input_id, 'response': response, 'resultType': result_type}
        if self.scenario == 'stray_terminal':
            payload['inputId'] = 'previous_input'
        if self.scenario not in ('missing_terminal', 'admission_without_terminal'):
            self.event('turn.failed' if self.scenario == 'failed_terminal' else 'turn.completed', payload)
            self.terminal_emitted = True
        if self.scenario in ('native_admission_order', 'admission_without_terminal'):
            return
        self.emit({'method': 'state.updated', 'params': {'type': 'state.updated', 'scope': 'session',
                   'sessionId': self.session, 'revision': 0 if self.scenario == 'stale_completion' else 2,
                   'reason': 'prompt_failed' if self.scenario == 'failed_after_success' else 'prompt_completed',
                   'patch': {'mode': {'current': 'build'}}}})

    def close(self, code=0):
        if self.terminal_emitted and self.scenario == 'native_rules_changed_on_close':
            with connect(Path(self.env['ZCODE_SESSION_DB_PATH'])) as db:
                db.execute("UPDATE local_setting SET value='{}' WHERE key='ruleset'")
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


class ZCodeNativeInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / '.git').mkdir()
        self.home = self.root / 'source-home'
        self.source = self.home / '.zcode/cli/config.json'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps(source_config()))
        self.binary = self.root / 'zcode.cjs'
        self.binary.write_text('synthetic pinned native fixture')
        self.binary.chmod(0o700)
        self.config = AgentConfig(id='z', token='', adapter='zcode', zcode_transport='app-server',
                                  zcode_bin=str(self.binary), zcode_home_dir=str(self.home),
                                  zcode_context_root=str(self.root / 'contexts'),
                                  zcode_permission_commands=['python -m unittest tests.test_one'], timeout=2)
        self.processes = []
        self.cleaned = []
        self.scenario = 'success'

    async def spawn(self, *args, **kwargs):
        if '-e' in args:
            return _ProbeProcess(kwargs['env'], bad_home=self.scenario == 'bad_node_home')
        self.assertIn('app-server', args)
        self.assertNotIn('--prompt', args)
        process = _NativeProcess(kwargs['env'], kwargs['cwd'], scenario=self.scenario, number=len(self.processes) + 1)
        self.processes.append(process)
        return process

    async def cleanup(self, process):
        self.cleaned.append(process)
        process.close(code=-9)

    def patches(self):
        return (
            patch('multinexus.adapters.zcode_protocol.APP_SERVER_SHA256', hashlib.sha256(self.binary.read_bytes()).hexdigest()),
            patch('multinexus.adapters.zcode_protocol.shutil.which', side_effect=lambda value: str(self.binary) if value == str(self.binary) else '/synthetic/node'),
            patch('multinexus.adapters.zcode_protocol.asyncio.create_subprocess_exec', side_effect=self.spawn),
            patch('multinexus.adapters.zcode_protocol.terminate_owned_process_group', side_effect=self.cleanup),
        )

    async def invoke(self, *, resume=None, timeout=None, progress=None, prompt='Synthetic prompt'):
        a, b, c, d = self.patches()
        with a, b, c, d:
            adapter = ZCodeAdapter(self.config)
            if resume:
                return await adapter.resume(resume, prompt, work_dir=str(self.work), timeout=timeout, on_progress=progress)
            return await adapter.call(prompt, work_dir=str(self.work), timeout=timeout, on_progress=progress)

    def assert_credentials_cleaned(self):
        if (self.root / 'contexts').exists():
            self.assertEqual(list((self.root / 'contexts').glob('ctx_*/home/.zcode/cli/config.json')), [])
            self.assertEqual(list((self.root / 'contexts').glob('ctx_*/.active')), [])

    async def test_interleaved_native_permissions_and_correlated_result(self):
        before = self.source.read_bytes()
        progress = []
        result = await self.invoke(progress=progress.append)
        self.assertEqual(result.outcome, OUTCOME_SUCCESS)
        self.assertEqual(result.text, 'Native final')
        evidence = result.metadata['provider_evidence']
        self.assertEqual(evidence['observed_model'], {'providerId': PROVIDER_ID, 'modelId': MODEL_ID})
        self.assertEqual(evidence['observed_model_source'], 'native_snapshot.settings.model.current')
        self.assertFalse(evidence['downstream_model_verified'])
        self.assertTrue(evidence['bash_permission_gate']['session_persisted'])
        self.assertEqual(evidence['bash_permission_gate']['session_id'], result.session_id)
        self.assertEqual([d['decision'] for d in evidence['permission_decisions']], ['allow', 'allow', 'deny', 'deny'])
        for decision in evidence['permission_decisions']:
            self.assertEqual(decision['session_id'], result.session_id)
            self.assertEqual(decision['input_id'], evidence['input_id'])
            self.assertEqual(decision['turn_id'], evidence['turn_id'])
        requests = self.processes[0].requests
        self.assertEqual(requests[0]['method'], 'session/create')
        self.assertNotIn('toolAllowlist', requests[0]['params'])
        self.assertNotIn('mcpServers', requests[0]['params'])
        self.assertNotIn('permissionUpdates', json.dumps(self.processes[0].client_replies))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn('PRIVATE_REASONING', repr(result))
        self.assertNotIn('PRIVATE_REASONING', repr(progress))
        self.assert_credentials_cleaned()

    async def test_native_restriction_failures_prevent_model_send(self):
        for scenario in ('native_rules_legacy', 'native_rules_missing_on_subscribe'):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                result = await self.invoke()
                self.assertEqual(result.outcome, OUTCOME_FAILED)
                self.assertEqual(result.error_category, 'unavailable')
                self.assertFalse(any(r.get('method') == 'session/send' for r in self.processes[-1].requests))
                self.assertIn(self.processes[-1], self.cleaned)
                self.assert_credentials_cleaned()

    async def test_native_rule_readback_failure_writes_no_locator_or_model_input(self):
        from multinexus.adapters.zcode_native_rules import ZCodeNativeRuleError
        with patch('multinexus.adapters.zcode_native_rules._verify_bash_restriction',
                   side_effect=ZCodeNativeRuleError('injected committed-rule readback failure')):
            result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertFalse(any(r.get('method') == 'session/send' for r in self.processes[-1].requests))
        self.assertEqual(list((self.root / 'contexts/sessions').glob('*.json')), [])
        self.assert_credentials_cleaned()

    async def test_post_terminal_rule_drift_cannot_be_reported_as_success(self):
        self.scenario = 'native_rules_changed_on_close'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, 'unavailable')
        self.assert_credentials_cleaned()

    async def test_identical_permission_reannouncement_replies_to_current_wire_id(self):
        self.scenario = 'duplicate'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_SUCCESS)
        evidence = result.metadata['provider_evidence']['permission_decisions']
        self.assertEqual(len(evidence), 4)
        self.assertEqual(evidence[0]['delivery_count'], 2)
        replies = {r['id']: r for r in self.processes[0].client_replies}
        self.assertEqual(replies['server-permission-0']['result'], replies['server-reannounced']['result'])
        self.assert_credentials_cleaned()

    async def test_real_notification_names_interleave_without_granting_or_completing(self):
        self.scenario = 'observations'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_SUCCESS)
        self.assertEqual(result.text, 'Native final')
        evidence = result.metadata['provider_evidence']
        self.assertEqual([item['decision'] for item in evidence['permission_decisions']],
                         ['allow', 'allow', 'deny', 'deny'])
        self.assertEqual(evidence['turn_id'], self.processes[0].turn)
        self.assertNotIn('session_detached', repr(result))
        self.assertEqual(len(self.processes[0].client_replies), 5)
        self.assert_credentials_cleaned()

    async def test_protocol_failures_stop_owned_process_without_leaking_secrets(self):
        for scenario in ('conflicting_duplicate', 'stray_permission', 'stray_terminal', 'unknown_callback',
                         'framing', 'eof', 'config_drift', 'model_mismatch', 'session_mismatch',
                         'failed_after_success', 'permission_schema', 'drain_wrong_turn', 'drain_pending',
                         'drain_error_projection', 'drain_model_drift', 'drain_eof', 'drain_late_permission'):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                result = await self.invoke()
                self.assertEqual(result.outcome, OUTCOME_FAILED)
                self.assertEqual(result.error_category, 'unavailable' if scenario == 'config_drift' else 'protocol_error')
                self.assertNotIn(SECRET, repr(result))
                self.assertIn(self.processes[-1], self.cleaned)
                self.assert_credentials_cleaned()
        unknown = next(p for p in self.processes if p.scenario == 'unknown_callback')
        reply = next(r for r in unknown.client_replies if r['id'] == 'server-unknown')
        self.assertEqual(reply['error']['code'], -32601)
        conflicting = next(p for p in self.processes if p.scenario == 'conflicting_duplicate')
        reply = next(r for r in conflicting.client_replies if r['id'] == 'server-reannounced')
        self.assertEqual(reply['result']['decision'], 'deny')

    async def test_cancelled_native_terminal_is_failure_even_with_summary(self):
        self.scenario = 'cancelled_terminal'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, 'provider_error')
        self.assertEqual(result.metadata['provider_evidence']['result_type'], 'cancelled')
        self.assert_credentials_cleaned()

    async def test_native_admission_state_order_requires_causal_terminal_and_post_terminal_read(self):
        for scenario in ('native_admission_order', 'stale_completion', 'drain_busy'):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                result = await self.invoke()
                self.assertEqual(result.outcome, OUTCOME_SUCCESS)
                self.assertEqual(result.text, 'Native final')
                process = self.processes[-1]
                self.assertEqual(process.finalization_reads, 3 if scenario == 'drain_busy' else 1)
                self.assertEqual(process.requests[-1]['method'], 'session/read')
                self.assertNotIn(process, self.cleaned)
                self.assert_credentials_cleaned()
                if scenario == 'native_admission_order':
                    resumed = await self.invoke(resume=result.session_id)
                    self.assertEqual(resumed.outcome, OUTCOME_SUCCESS)
                    self.assertEqual(resumed.session_id, result.session_id)
                    self.assertEqual(self.processes[-1].finalization_reads, 1)
                    self.assert_credentials_cleaned()

    async def test_native_turn_failed_drains_error_projection_and_remains_provider_failure(self):
        self.scenario = 'failed_terminal'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, 'provider_error')
        self.assertEqual(self.processes[-1].finalization_reads, 1)
        self.assert_credentials_cleaned()

    async def test_resume_only_known_context_and_same_model_without_fresh_fallback(self):
        first = await self.invoke()
        second = await self.invoke(resume=first.session_id)
        self.assertEqual(second.outcome, OUTCOME_SUCCESS)
        self.assertTrue(second.resumed)
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(self.processes[0].env['HOME'], self.processes[1].env['HOME'])
        methods = [r['method'] for r in self.processes[1].requests]
        self.assertEqual(methods[:4], ['session/resume', 'session/setMode', 'session/read', 'session/subscribe'])
        self.assertNotIn('session/create', methods)
        unknown = await self.invoke(resume='sess_unowned')
        self.assertEqual(unknown.outcome, OUTCOME_FAILED)
        self.assertEqual(len(self.processes), 2)
        self.scenario = 'resume_missing'
        missing = await self.invoke(resume=first.session_id)
        self.assertEqual(missing.outcome, OUTCOME_FAILED)
        self.assertNotIn(SECRET, repr(missing))
        self.assertEqual([r['method'] for r in self.processes[-1].requests if r['method'] != 'session/stop'], ['session/resume'])
        self.assert_credentials_cleaned()

    async def test_cold_resume_rehydrates_exact_static_connection_without_auth_artifacts(self):
        self.scenario = 'resume_requires_runtime_model'
        first = await self.invoke()
        second = await self.invoke(resume=first.session_id)
        self.assertEqual(second.outcome, OUTCOME_SUCCESS)
        self.assertEqual(second.session_id, first.session_id)
        fresh = self.processes[-2].requests[0]
        self.assertEqual(fresh['method'], 'session/create')
        self.assertNotIn('runtimeModel', fresh['params'])
        resume = self.processes[-1].requests[0]
        self.assertEqual(resume['method'], 'session/resume')
        runtime = resume['params']['runtimeModel']
        self.assertEqual(runtime['model'], {'providerId': PROVIDER_ID, 'modelId': MODEL_ID})
        self.assertEqual(runtime['provider']['apiKey'], {'source': 'inline', 'value': SECRET})
        self.assertEqual(set(runtime['provider']), {'providerId', 'kind', 'source', 'baseURL', 'apiKey', 'models', 'label'})
        self.assertEqual([model['modelId'] for model in runtime['provider']['models']], [MODEL_ID])
        self.assertNotIn('session/setModel', [request['method'] for request in self.processes[-1].requests])
        self.assertNotIn(SECRET, repr(second))
        for path in (self.root / 'contexts').rglob('*'):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path.name)
        self.assert_credentials_cleaned()
        self.scenario = 'resume_model_unavailable'
        unavailable = await self.invoke(resume=first.session_id)
        self.assertEqual(unavailable.outcome, OUTCOME_FAILED)
        self.assertIn('no fallback', unavailable.text)
        self.assertNotIn(SECRET, repr(unavailable))
        self.assertEqual([r['method'] for r in self.processes[-1].requests if r['method'] != 'session/stop'], ['session/resume'])
        self.assert_credentials_cleaned()

    async def test_timeout_missing_terminal_and_cancellation_cleanup(self):
        for scenario in ('hang', 'missing_terminal', 'admission_without_terminal', 'drain_never', 'drain_stale_sequence'):
            self.scenario = scenario
            result = await self.invoke(timeout=0.08)
            self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
            self.assertIn(self.processes[-1], self.cleaned)
            self.assert_credentials_cleaned()
        self.scenario = 'hang'
        a, b, c, d = self.patches()
        with a, b, c, d:
            task = asyncio.create_task(ZCodeAdapter(self.config).call('Synthetic prompt', work_dir=str(self.work)))
            while not self.processes or self.processes[-1].returncode is not None:
                await asyncio.sleep(0)
            await self.processes[-1].started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn(self.processes[-1], self.cleaned)
        self.assert_credentials_cleaned()

        self.scenario = 'drain_never'
        a, b, c, d = self.patches()
        with a, b, c, d:
            task = asyncio.create_task(ZCodeAdapter(self.config).call('Synthetic prompt', work_dir=str(self.work)))
            while self.processes[-1].scenario != 'drain_never' or self.processes[-1].finalization_reads == 0:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn(self.processes[-1], self.cleaned)
        self.assert_credentials_cleaned()

    async def test_final_text_and_persisted_permission_evidence_are_secret_redacted(self):
        self.scenario = 'redaction'
        result = await self.invoke()
        self.assertEqual(result.text, 'Native final [REDACTED]')
        self.assertNotIn(SECRET, repr(result))
        files = list((self.root / 'contexts').glob('ctx_*/permissions-*.json'))
        self.assertEqual(len(files), 1)
        audit = json.loads(files[0].read_text())
        self.assertEqual(len(audit['decisions']), 4)
        self.assertNotIn(SECRET, files[0].read_text())
        self.assert_credentials_cleaned()

    async def test_wrong_binary_hash_and_wrong_node_home_fail_before_native_spawn(self):
        result = await ZCodeAdapter(self.config).call('Synthetic prompt', work_dir=str(self.work))
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, 'unavailable')
        self.assert_credentials_cleaned()
        self.scenario = 'bad_node_home'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(len(self.processes), 0)
        self.assert_credentials_cleaned()

    async def test_slash_commands_after_js_whitespace_fail_before_context_or_spawn(self):
        prompts = ('/', '/permissions allow Bash', ' \t/mode yolo', '\ufeff/custom',
                   '\ufeff \ufeff\t/name', '\u00a0\u2003\u2028\u3000/permissions', '\r\n/compact')
        with patch('multinexus.adapters.zcode_protocol.prepare_context') as prepare:
            for prompt in prompts:
                with self.subTest(prompt=prompt):
                    result = await self.invoke(prompt=prompt)
                    self.assertEqual(result.outcome, OUTCOME_FAILED)
                    self.assertIn('does not support slash/custom commands', result.text)
            prepare.assert_not_called()
        self.assertEqual(self.processes, [])
        self.assertFalse((self.root / 'contexts').exists())

    async def test_plain_prompt_and_slashes_in_body_are_not_control_commands(self):
        for prompt in ('Review path/to/file.py', 'Explain this text:\n/permissions allow Bash', '\u0085/ordinary text'):
            with self.subTest(prompt=prompt):
                result = await self.invoke(prompt=prompt)
                self.assertEqual(result.outcome, OUTCOME_SUCCESS)
        self.config.system_prompt = 'Ordinary system instruction'
        result = await self.invoke(prompt='/permissions in quoted user content')
        self.assertEqual(result.outcome, OUTCOME_SUCCESS)
        send = next(request for request in self.processes[-1].requests if request['method'] == 'session/send')
        self.assertTrue(send['params']['content'].startswith('Ordinary system instruction'))
        self.assert_credentials_cleaned()

    async def test_cold_resume_and_system_prompt_use_the_final_wire_text_guard(self):
        first = await self.invoke()
        with patch('multinexus.adapters.zcode_protocol.prepare_context') as prepare:
            result = await self.invoke(resume=first.session_id, prompt=' \ufeff/permissions allow Bash')
            self.assertEqual(result.outcome, OUTCOME_FAILED)
            self.assertIn('slash/custom commands', result.text)
            prepare.assert_not_called()
        self.assertEqual(len(self.processes), 1)
        self.config.system_prompt = '\ufeff/custom-command'
        result = await self.invoke(prompt='Ordinary task')
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(len(self.processes), 1)
        self.assert_credentials_cleaned()

    async def test_native_config_reformat_is_accepted_through_the_invocation(self):
        self.scenario = 'format_config'
        result = await self.invoke()
        self.assertEqual(result.outcome, OUTCOME_SUCCESS)
        self.assert_credentials_cleaned()

    async def test_headless_slash_prompt_keeps_existing_transport_behavior(self):
        from test_zcode_adapter import _FakeProcess, _success
        self.config.zcode_transport = 'headless'
        process = _FakeProcess(_success())
        with patch('multinexus.adapters.zcode.asyncio.create_subprocess_exec', return_value=process) as spawn:
            result = await ZCodeAdapter(self.config).call('/permissions', work_dir=str(self.work))
        self.assertEqual(result.text, 'hello')
        args = spawn.call_args.args
        self.assertEqual(args[args.index('--prompt') + 1], '/permissions')
        self.assertNotIn('app-server', args)
        self.assertFalse((self.root / 'contexts').exists())


if __name__ == '__main__':
    unittest.main()
