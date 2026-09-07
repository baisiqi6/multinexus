"""No-send regressions for managed context checkpoint safety.

Run through pytest from the repository root. All stores are temporary;
provider/Coordinate are fakes. No network, provider CLI or delivery is invoked.
"""
import asyncio

import pytest
from multinexus.adapters.base import AdapterResult
from multinexus.agentd.worker import AgentdWorker
import test_worker_context_cursor as harness_module
from test_worker_context_cursor import RecordingAdapter
from test_agentd_execution_lease import _make_lease, _make_binding


@pytest.fixture
def h():
    fixture = harness_module.WorkerContextCursorTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def managed_claim(h, full, origin, name):
    claim = h._claim(job_id=name, prompt=full, origin=origin)
    claim['execution_lease'] = _make_lease(
        job_id=name, agent_id='test-agent', runner_profile_id='test-agent',
        host_id='test-host', worktree_path=str(h.root / 'workspace'))
    claim['job']['payload']['executor_binding'] = _make_binding(
        executor_instance_id='test-agent', runner_profile_id='test-agent')
    return claim


def checkpoint(worker, envelope):
    return worker.session_store.get_context_cursor(
        scope_id=envelope['session_scope_id'], agent_id='test-agent')


def test_unapplied_terminal_replay_must_not_advance(h):
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    h._seed_session(worker, envelope)
    h._advance_to_first_history(worker, envelope)
    before = checkpoint(worker, envelope)
    async def immutable_replay(**kwargs):
        # Coordinate runtime._replay_terminal_result returns this successfully
        # even when submitted status/result differ from the terminal authority.
        return {'result': {
            'job': {'id': kwargs['job_id'], 'status': 'failed', 'attempt_count': 2},
            'event': {'event_type': 'job.result_replayed', 'payload': {
                'applied': False, 'reason': 'terminal_result_immutable'}},
            'event_created': True, 'delivery': None, 'delivery_created': False}}
    worker.coordinate.report_job = immutable_replay
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-replay')))
    assert checkpoint(worker, envelope) == before


def test_late_success_must_not_resurrect_reset_session(h):
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    h._seed_session(worker, envelope)
    h._advance_to_first_history(worker, envelope)
    class ResetWhileRunning(RecordingAdapter):
        async def resume(self, session_id, prompt, **kwargs):
            worker.session_store.mark_stale(
                scope_id=envelope['session_scope_id'], agent_id='test-agent')
            return AdapterResult(text='late success', session_id=session_id)
    worker.adapter = ResetWhileRunning()
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-reset')))
    assert checkpoint(worker, envelope) is None


def test_late_old_generation_must_not_overwrite_new_checkpoint(h):
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    h._seed_session(worker, envelope)
    h._advance_to_first_history(worker, envelope)
    newer = {}
    class NewGenerationWhileRunning(RecordingAdapter):
        async def resume(self, session_id, prompt, **kwargs):
            worker.session_store.upsert(
                scope_id=envelope['session_scope_id'], agent_id='test-agent',
                adapter='claude', session_id=session_id,
                work_dir=str(h.root / 'workspace'), context_generation='new-generation')
            assert worker.session_store.advance_context_cursor(
                scope_id=envelope['session_scope_id'], agent_id='test-agent',
                session_id=session_id, context_generation='new-generation',
                expected_cursor_order_token=None, expected_cursor_message_id=None,
                cursor_order_token='99', cursor_message_id='newer-m99')
            newer.update(checkpoint(worker, envelope))
            return AdapterResult(text='late success', session_id=session_id)
    worker.adapter = NewGenerationWhileRunning()
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-generation')))
    assert checkpoint(worker, envelope) == newer


def test_first_request_after_worker_restart_must_use_full_per_plan(h):
    full, envelope, origin, _ = h._context()
    first, _ = h._worker_with_report(RecordingAdapter())
    h._seed_session(first, envelope)
    h._advance_to_first_history(first, envelope)
    second, _ = h._worker_with_report(RecordingAdapter())
    asyncio.run(second._process_job(managed_claim(h, full, origin, 'job-restart')))
    assert second.adapter.resumes[0][1] == full


@pytest.mark.parametrize('mismatch', ['missing', 'status', 'attempt', 'bool_attempt', 'session', 'lease', 'response', 'context'])
def test_mismatched_or_missing_receipt_leaves_session_unchanged(h, mismatch):
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    h._seed_session(worker, envelope)
    before = checkpoint(worker, envelope)
    async def report(**kwargs):
        job = {'id': kwargs['job_id'], 'assigned_agent': kwargs['agent_id'],
               'status': kwargs['status'], 'attempt_count': kwargs['attempt_token'],
               'result': dict(kwargs['result_json'])}
        if mismatch == 'missing':
            return {'result': {}}
        if mismatch == 'status': job['status'] = 'failed'
        if mismatch == 'attempt': job['attempt_count'] = 2
        if mismatch == 'bool_attempt': job['attempt_count'] = True
        if mismatch == 'session': job['result']['session_id'] = 'other-session'
        if mismatch == 'lease': job['result']['lease_id'] = 'other-lease'
        if mismatch == 'response': job['result']['response_text'] = 'different-response'
        if mismatch == 'context': job['result']['execution_context_id'] = 'different-context'
        return {'result': {'job': job}}
    worker.coordinate.report_job = report
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-mismatch')))
    assert checkpoint(worker, envelope) == before


def test_exact_success_replay_confirms_checkpoint(h):
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    async def replay(**kwargs):
        return {'result': {
            'job': {'id': kwargs['job_id'], 'assigned_agent': kwargs['agent_id'],
                    'status': 'done', 'attempt_count': kwargs['attempt_token'],
                    'result': dict(kwargs['result_json'])},
            'event': {'event_type': 'job.result_replayed', 'payload': {'applied': False}}}}
    worker.coordinate.report_job = replay
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-exact-replay')))
    assert checkpoint(worker, envelope)['context_cursor_message_id'] == 'm3'


def test_progress_then_rejected_report_does_not_create_session(h):
    from multinexus.agentd.coordinate_client import CoordinateRuntimeError
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    seen = []
    class ProgressAdapter(RecordingAdapter):
        async def call(self, prompt, *, on_progress=None, **kwargs):
            on_progress({'session_id': 'session-1', 'stage': 'running'})
            assert checkpoint(worker, envelope) is None
            return AdapterResult(text='success', session_id='session-1')
    worker.adapter = ProgressAdapter()
    async def progress(**kwargs):
        seen.append(kwargs)
        return {'result': {}}
    async def reject(**kwargs):
        raise CoordinateRuntimeError('report rejected')
    worker.coordinate.record_progress = progress
    worker.coordinate.report_job = reject
    with pytest.raises(CoordinateRuntimeError):
        asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-rejected')))
    assert seen[0]['session_id'] == 'session-1'
    assert checkpoint(worker, envelope) is None


def test_failed_old_resume_cannot_retire_new_session_or_start_fresh(h):
    from multinexus.adapters.base import OUTCOME_FAILED
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    h._seed_session(worker, envelope)
    class FailedLateAdapter(RecordingAdapter):
        async def resume(self, session_id, prompt, **kwargs):
            worker.session_store.upsert(
                scope_id=envelope['session_scope_id'], agent_id='test-agent',
                adapter='claude', session_id='new-session', work_dir=str(h.root / 'workspace'))
            return AdapterResult(text='failed', outcome=OUTCOME_FAILED, session_id=session_id)
    worker.adapter = FailedLateAdapter()
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-late-failed')))
    assert checkpoint(worker, envelope)['session_id'] == 'new-session'
    assert worker.adapter.calls == []


def test_reset_between_session_write_and_cursor_cas_rejects_old_snapshot(h):
    full, envelope, origin, _ = h._context()
    worker, _ = h._worker_with_report(RecordingAdapter())
    advance = worker.session_store.advance_context_cursor
    def reset_then_advance(**kwargs):
        worker.session_store.mark_stale(scope_id=envelope['session_scope_id'], agent_id='test-agent')
        h._seed_session(worker, envelope)
        return advance(**kwargs)
    worker.session_store.advance_context_cursor = reset_then_advance
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-cas-reset')))
    assert checkpoint(worker, envelope)['context_cursor_message_id'] is None


@pytest.mark.parametrize("restart", [False, True])
def test_continuous_session_delta_includes_other_agent_and_new_current(h, restart):
    import time
    from multinexus.context.prompt import build_agent_prompt_with_context
    full, envelope, origin, context_store = h._context()
    adapter = RecordingAdapter()
    if restart:
        prior, _ = h._worker_with_report(RecordingAdapter())
        h._seed_session(prior, envelope)
        h._advance_to_first_history(prior, envelope)
    worker, _ = h._worker_with_report(adapter)
    asyncio.run(worker._process_job(managed_claim(h, full, origin, 'job-first')))
    first_prompt = adapter.resumes[0][1] if restart else adapter.calls[0][0]
    assert first_prompt == full
    now = int(time.time() * 1000) + 100
    for i, (mid, content, bot) in enumerate([
        ('m4', 'other-agent-new-message', True), ('m5', 'next-current-message', False)]):
        context_store.record_message(
            message_id=mid, channel_id=envelope['scope_id'], author_id='other' if bot else 'human',
            author_name='Other' if bot else 'Human', author_is_bot=bot, content=content,
            created_at_ms=now+i, source='test', ttl_seconds=3600)
    next_full, next_envelope = build_agent_prompt_with_context(
        context_store=context_store, config=worker.config, bot_id=42,
        channel_id=envelope['scope_id'], message_id='m5', current_text='next-current-message',
        scope_id=envelope['scope_id'], session_scope_id=envelope['session_scope_id'], recipient={'id': 'test-agent'})
    next_origin = {**origin, 'message_id': 'm5', 'context': next_envelope}
    asyncio.run(worker._process_job(managed_claim(h, next_full, next_origin, 'job-next')))
    prompt = adapter.resumes[-1][1]
    assert 'old-message-m1' not in prompt
    assert 'current-message-m3' not in prompt
    assert 'other-agent-new-message' in prompt
    assert 'sender_role=other_agent' in prompt
    assert 'next-current-message' in prompt
    assert checkpoint(worker, next_envelope)['context_cursor_message_id'] == 'm5'
