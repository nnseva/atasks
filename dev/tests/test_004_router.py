"""
Router tests
"""
import asyncio
from unittest import IsolatedAsyncioTestCase as TestCase
from unittest.mock import AsyncMock, patch

from atasks.codecs import PickleCodec, get_codec
from atasks.namespaces import namespaces
from atasks.router import (
    JobNotFound,
    LateRegistration,
    NoClientTransportRegistered,
    NoCodecRegistered,
    ResultNotSerializable,
    ServiceShuttingDown,
    TransportError,
    get_router,
)
from atasks.tasks import atask, atask_broadcast, atask_queue
from atasks.transport.base import LoopbackTransport


class ModuleTest(TestCase):
    """Module tests"""
    async def test_scenarios(self):
        """Test scenarios"""
        PickleCodec()
        transport = LoopbackTransport()
        await transport.connect()
        # Importing the scenario module - which registers its @atask's at
        # module level - must happen before router.activate(transport): it
        # only subscribes to names already registered at that moment (see
        # Router.activate) and registering after it raises LateRegistration.
        from dev.tests.scenarios import request_parallel, request_sequence

        router = get_router()
        await router.activate(transport)

        await request_sequence()
        returns = await request_parallel()
        self.assertEqual(returns, [0, 1, 2, 3, 4, 0, 1, 2, 3, 4])

    async def test_late_registration_rpc(self):
        """Registering an @atask after activate() raises LateRegistration, not silently no-op"""
        PickleCodec(namespace='test_late_registration_rpc')
        transport = LoopbackTransport(namespace='test_late_registration_rpc')
        await transport.connect()
        router = get_router('test_late_registration_rpc')
        await router.activate(transport)

        with self.assertRaises(LateRegistration):
            @atask(namespace='test_late_registration_rpc')
            async def too_late(a):
                return a

    async def test_late_registration_queue(self):
        """Registering an @atask_queue after activate() raises LateRegistration"""
        PickleCodec(namespace='test_late_registration_queue')
        transport = LoopbackTransport(namespace='test_late_registration_queue')
        await transport.connect()
        router = get_router('test_late_registration_queue')
        await router.activate(transport)

        with self.assertRaises(LateRegistration):
            @atask_queue(namespace='test_late_registration_queue')
            async def too_late(a):
                return a

    async def test_late_registration_broadcast(self):
        """Registering an @atask_broadcast after activate() raises LateRegistration"""
        PickleCodec(namespace='test_late_registration_broadcast')
        transport = LoopbackTransport(namespace='test_late_registration_broadcast')
        await transport.connect()
        router = get_router('test_late_registration_broadcast')
        await router.activate(transport)

        with self.assertRaises(LateRegistration):
            @atask_broadcast(namespace='test_late_registration_broadcast')
            async def too_late(a):
                return a

    async def test_activate_deactivate_allows_reregistration(self):
        """
        deactivate() clears Router.server, so registering a new @atask is
        allowed again afterwards - and a subsequent activate() picks it up.
        """
        namespace = 'test_activate_deactivate_allows_reregistration'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        await router.activate(transport)
        await router.deactivate()

        @atask(namespace=namespace)
        async def now_ok(a):
            return a * 2

        await router.activate(transport)
        self.assertEqual(await now_ok(21), 42)
        await router.deactivate()

    async def test_deactivate_is_symmetric_with_activate(self):
        """
        deactivate() unregisters exactly the request callback registered by
        the matching activate() - a request for that name after deactivate()
        must not be servable any more.
        """
        namespace = 'test_deactivate_is_symmetric_with_activate'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        @atask(namespace=namespace, name='some_task')
        async def some_task(a):
            return a

        router = get_router(namespace)
        await router.activate(transport)
        self.assertIn('some_task', transport._request_callbacks)
        await router.deactivate()
        self.assertNotIn('some_task', transport._request_callbacks)


class DeactivateDrainTest(TestCase):
    """Message delivery while the router is being deactivated and drained"""

    def setUp(self):
        self.reply_payloads = []

    async def _reply(self, payload):
        self.reply_payloads.append(payload)

    async def _setup(self, namespace):
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        return transport, get_router(namespace)

    async def test_request_during_deactivation_is_accepted_and_drained(self):
        """A request delivered while still accepting is tracked, so wait_idle() waits for it"""
        namespace = 'test_request_during_deactivation_is_accepted_and_drained'
        transport, router = await self._setup(namespace)
        release = asyncio.Event()
        started = asyncio.Event()

        @atask(namespace=namespace, name='slow')
        async def slow(a):
            started.set()
            await release.wait()
            return a + 1

        await router.activate(transport)
        original = transport._unregister_request_callback
        handles = []

        async def unregister_and_deliver(name):
            if name == 'slow':
                content = await get_codec(namespace).encode(([1], {}, []))
                handles.append(router._on_request('slow', content, self._reply))
            await original(name)

        transport._unregister_request_callback = unregister_and_deliver
        await router.deactivate()
        transport._unregister_request_callback = original

        self.assertEqual(len(handles), 1)
        self.assertIsNotNone(handles[0])
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(await router.wait_idle(0.05))
        release.set()
        self.assertTrue(await router.wait_idle(1))
        self.assertEqual(len(self.reply_payloads), 1)
        self.assertEqual(await get_codec(namespace).decode(self.reply_payloads[0]), (True, 2))

    async def test_request_after_deactivation_is_refused(self):
        """After deactivate(), request/event/broadcast deliveries are refused and the handler never runs"""
        namespace = 'test_request_after_deactivation_is_refused'
        transport, router = await self._setup(namespace)
        calls = []

        @atask(namespace=namespace, name='noop')
        async def noop(a):
            calls.append(a)
            return a

        @atask_queue(namespace=namespace, name='noop_queue')
        async def noop_queue(a):
            calls.append(a)

        @atask_broadcast(namespace=namespace, name='noop_broadcast')
        async def noop_broadcast(a):
            calls.append(a)

        await router.activate(transport)
        await router.deactivate()

        self.assertIsNone(router._on_request('noop', b'', self._reply))
        self.assertIsNone(router._on_event('noop_queue', b''))
        self.assertIsNone(router._on_broadcast('noop_broadcast', b''))
        await asyncio.sleep(0)
        self.assertEqual(calls, [])
        self.assertTrue(await router.wait_idle(0.05))

    async def test_cancel_active_reports_shutdown_to_caller(self):
        """cancel_active() cancels a blocked handler and the caller gets ServiceShuttingDown"""
        namespace = 'test_cancel_active_reports_shutdown_to_caller'
        transport, router = await self._setup(namespace)
        started = asyncio.Event()

        @atask(namespace=namespace, name='blocked')
        async def blocked(a):
            started.set()
            await asyncio.Event().wait()

        await router.activate(transport)
        content = await get_codec(namespace).encode(([1], {}, []))
        self.assertIsNotNone(router._on_request('blocked', content, self._reply))
        await asyncio.wait_for(started.wait(), 1)
        await router.deactivate()

        with self.assertLogs('atasks.router', level='WARNING'):
            unfinished = await router.cancel_active(grace=1)
        self.assertEqual(unfinished, 0)
        self.assertEqual(len(self.reply_payloads), 1)
        success, error = await get_codec(namespace).decode(self.reply_payloads[0])
        self.assertFalse(success)
        self.assertIsInstance(error, ServiceShuttingDown)

    async def test_failed_cancellation_reply_is_logged_and_task_still_cancelled(self):
        """If the ServiceShuttingDown reply cannot be sent, the failure is logged and the task is still cancelled"""
        namespace = 'test_failed_cancellation_reply_is_logged_and_task_still_cancelled'
        transport, router = await self._setup(namespace)
        started = asyncio.Event()

        @atask(namespace=namespace, name='blocked_reply_fails')
        async def blocked_reply_fails(a):
            started.set()
            await asyncio.Event().wait()

        async def broken_reply(payload):
            raise RuntimeError('reply transport is down')

        await router.activate(transport)
        content = await get_codec(namespace).encode(([1], {}, []))
        task = router._on_request('blocked_reply_fails', content, broken_reply)
        self.assertIsNotNone(task)
        await asyncio.wait_for(started.wait(), 1)
        await router.deactivate()

        with self.assertLogs('atasks.router', level='ERROR') as logs:
            unfinished = await router.cancel_active(grace=1)
        self.assertEqual(unfinished, 0)
        self.assertTrue(task.cancelled())
        self.assertTrue(any('Could not report the cancellation of request blocked_reply_fails' in line for line in logs.output))

    async def test_failed_unregister_is_logged_and_others_still_unregistered(self):
        """An exception from one _unregister_* call is logged and does not stop the others"""
        namespace = 'test_failed_unregister_is_logged_and_others_still_unregistered'
        transport, router = await self._setup(namespace)

        @atask(namespace=namespace, name='bad')
        async def bad(a):
            return a

        @atask(namespace=namespace, name='good')
        async def good(a):
            return a

        await router.activate(transport)
        original = transport._unregister_request_callback

        async def failing(name):
            if name == 'bad':
                raise RuntimeError('boom')
            await original(name)

        transport._unregister_request_callback = failing
        with self.assertLogs('atasks.router', level='ERROR'):
            await router.deactivate()
        transport._unregister_request_callback = original

        self.assertNotIn('good', transport._request_callbacks)
        self.assertIsNone(router.server)

    async def test_failed_queue_unregister_is_logged_and_others_still_unregistered(self):
        """An exception from _unregister_event_callback is logged and does not stop the others"""
        namespace = 'test_failed_queue_unregister_is_logged_and_others_still_unregistered'
        transport, router = await self._setup(namespace)

        @atask_queue(namespace=namespace, name='bad')
        async def bad(a):
            return a

        @atask_queue(namespace=namespace, name='good')
        async def good(a):
            return a

        await router.activate(transport)
        original = transport._unregister_event_callback

        async def failing(name):
            if name == 'bad':
                raise RuntimeError('boom')
            await original(name)

        transport._unregister_event_callback = failing
        with self.assertLogs('atasks.router', level='ERROR') as logs:
            await router.deactivate()
        transport._unregister_event_callback = original

        self.assertTrue(any('Could not unregister event bad' in line for line in logs.output))
        self.assertNotIn('good', transport._event_callbacks)
        self.assertIsNone(router.server)

    async def test_failed_broadcast_unregister_is_logged_and_others_still_unregistered(self):
        """An exception from _unregister_broadcast_callback is logged and does not stop the others"""
        namespace = 'test_failed_broadcast_unregister_is_logged_and_others_still_unregistered'
        transport, router = await self._setup(namespace)

        @atask_broadcast(namespace=namespace, name='bad')
        async def bad(a):
            return a

        @atask_broadcast(namespace=namespace, name='good')
        async def good(a):
            return a

        await router.activate(transport)
        original = transport._unregister_broadcast_callback

        async def failing(name):
            if name == 'bad':
                raise RuntimeError('boom')
            await original(name)

        transport._unregister_broadcast_callback = failing
        with self.assertLogs('atasks.router', level='ERROR') as logs:
            await router.deactivate()
        transport._unregister_broadcast_callback = original

        self.assertTrue(any('Could not unregister broadcast bad' in line for line in logs.output))
        self.assertEqual(transport._broadcast_callbacks.get('good', []), [])
        self.assertIsNone(router.server)


class IdleAndCancelEmptyTest(TestCase):
    """wait_idle() and cancel_active() with no active handler tasks"""

    async def test_wait_idle_without_active_tasks_returns_true(self):
        """With nothing in flight, wait_idle() returns True without waiting"""
        namespace = 'test_wait_idle_without_active_tasks_returns_true'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        await router.activate(transport)
        await router.deactivate()

        self.assertTrue(await router.wait_idle(timeout=0.1))

    async def test_cancel_active_without_active_tasks_returns_zero(self):
        """With nothing in flight, cancel_active() cancels nothing and reports zero unfinished"""
        namespace = 'test_cancel_active_without_active_tasks_returns_zero'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        await router.activate(transport)
        await router.deactivate()

        self.assertEqual(await router.cancel_active(grace=0.1), 0)


class UnserializableResultTest(TestCase):
    """A handler result that cannot be pickled is reported to the caller as an exception"""

    async def test_unserializable_result_is_reported_to_caller(self):
        """The caller receives ResultNotSerializable instead of a missing or empty reply"""
        namespace = 'test_unserializable_result_is_reported_to_caller'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask(namespace=namespace, name='unserializable_result_task')
        async def unserializable_result_task():
            return lambda: None

        await router.activate(transport)
        try:
            with self.assertRaises(ResultNotSerializable) as cm:
                await unserializable_result_task()
            self.assertEqual(cm.exception.name, 'unserializable_result_task')
            self.assertEqual(cm.exception.result_type, 'function')
        finally:
            await router.deactivate()


class UndecodableRequestTest(TestCase):
    """A request whose payload cannot be decoded is logged on the server and never replied to"""

    async def test_undecodable_request_is_logged_and_not_replied(self):
        """Corrupt request bytes make the request task log the failure and finish without a reply"""
        namespace = 'test_undecodable_request_is_logged_and_not_replied'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        called = []

        @atask(namespace=namespace, name='undecodable_request_task')
        async def undecodable_request_task():
            called.append(True)
            return None

        replies = []

        async def reply(payload):
            replies.append(payload)

        await router.activate(transport)
        try:
            task = router._on_request('undecodable_request_task', b'not a pickle', reply)
            self.assertIsNotNone(task)
            with self.assertLogs('atasks.router', level='ERROR') as logs:
                await task
            self.assertEqual(called, [])
            self.assertEqual(replies, [])
            self.assertTrue(any('could not be processed' in line for line in logs.output))
        finally:
            await router.deactivate()


class UndecodableEventTest(TestCase):
    """An event whose payload cannot be decoded is logged on the server and its handler never runs"""

    async def test_undecodable_event_is_logged_and_handler_not_called(self):
        """Corrupt event bytes make the event task log the failure and finish without calling the handler"""
        namespace = 'test_undecodable_event_is_logged_and_handler_not_called'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        called = []

        @atask_queue(namespace=namespace, name='undecodable_event_task')
        async def undecodable_event_task():
            called.append(True)
            return None

        await router.activate(transport)
        try:
            task = router._on_event('undecodable_event_task', b'not a pickle')
            self.assertIsNotNone(task)
            with self.assertLogs('atasks.router', level='ERROR') as logs:
                await task
            self.assertEqual(called, [])
            self.assertTrue(any('could not be processed' in line for line in logs.output))
            self.assertEqual(router._active_tasks, set())
        finally:
            await router.deactivate()


class CancelledEventTest(TestCase):
    """A blocked event task cancelled during shutdown is reported with a warning"""

    async def test_cancelled_event_task_logs_warning(self):
        """cancel_active() logs a warning naming the cancelled event task"""
        namespace = 'test_cancelled_event_task_logs_warning'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        started = asyncio.Event()

        @atask_queue(namespace=namespace, name='blocked_event_task')
        async def blocked_event_task():
            started.set()
            await asyncio.Event().wait()

        await router.activate(transport)
        content = await get_codec(namespace).encode(([], {}, []))
        task = router._on_event('blocked_event_task', content)
        self.assertIsNotNone(task)
        await asyncio.wait_for(started.wait(), 1)
        await router.deactivate()

        with self.assertLogs('atasks.router', level='WARNING') as logs:
            unfinished = await router.cancel_active(grace=1)
        self.assertEqual(unfinished, 0)
        self.assertTrue(task.cancelled())
        self.assertTrue(any('Cancelling' in line and 'blocked_event_task' in line for line in logs.output))


class ActivateGuardsTest(TestCase):
    """Re-activation with the same server and activation with no server are no-ops"""

    async def test_same_server_reactivation_is_noop(self):
        """Activating the already active transport does not deactivate or resubscribe"""
        namespace = 'test_same_server_reactivation_is_noop'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask(namespace=namespace, name='same_server_task')
        async def same_server_task():
            return 'ok'

        await router.activate(transport)
        try:
            with patch.object(router, 'deactivate', new=AsyncMock()) as deactivate, \
                    patch.object(router, '_activate_request', new=AsyncMock()) as activate_request:
                await router.activate(transport)
                deactivate.assert_not_awaited()
                activate_request.assert_not_awaited()
            self.assertIs(router.server, transport)
            self.assertTrue(router._accepting)
            self.assertEqual(await same_server_task(), 'ok')
        finally:
            await router.deactivate()

    async def test_activate_none_without_server_is_noop(self):
        """Activating None on a router that never had a server changes nothing"""
        namespace = 'test_activate_none_without_server_is_noop'
        PickleCodec(namespace=namespace)
        router = get_router(namespace)

        self.assertIsNone(router.server)
        await router.activate(None)
        self.assertIsNone(router.server)
        self.assertFalse(router._accepting)

    async def test_activate_none_deactivates_current_server(self):
        """Activating None after a server was active deactivates it and stops accepting"""
        namespace = 'test_activate_none_deactivates_current_server'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask(namespace=namespace, name='activate_none_task')
        async def activate_none_task():
            return 'ok'

        await router.activate(transport)
        await router.activate(None)
        self.assertIsNone(router.server)
        self.assertFalse(router._accepting)
        self.assertEqual(router._activated, {'rpc': [], 'queue': [], 'broadcast': []})

    async def test_deactivate_without_server_is_noop(self):
        """Deactivating a router that was never activated returns cleanly and unregisters nothing"""
        namespace = 'test_deactivate_without_server_is_noop'
        PickleCodec(namespace=namespace)
        router = get_router(namespace)

        self.assertIsNone(router.server)
        router._accepting = True
        with patch.object(router, '_deactivate_request', new=AsyncMock()) as deactivate_request, \
                patch.object(router, '_deactivate_queue', new=AsyncMock()) as deactivate_queue, \
                patch.object(router, '_deactivate_broadcast', new=AsyncMock()) as deactivate_broadcast:
            await router.deactivate()
            deactivate_request.assert_not_awaited()
            deactivate_queue.assert_not_awaited()
            deactivate_broadcast.assert_not_awaited()
        self.assertIsNone(router.server)
        self.assertFalse(router._accepting)


class EmptyResponseTest(TestCase):
    async def test_empty_transport_response_raises_transport_error(self):
        """A transport returning no bytes for a request surfaces as TransportError to the caller"""
        namespace = 'test_empty_transport_response_raises_transport_error'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask(namespace=namespace, name='empty_response_task')
        async def empty_response_task():
            return 'ok'

        await router.activate(transport)
        try:
            with patch.object(transport, 'send_request', new=AsyncMock(return_value=None)):
                with self.assertRaises(TransportError):
                    await empty_response_task()
        finally:
            await router.deactivate()


class NoCodecRegisteredTest(TestCase):
    """A namespace without a codec cannot send or serve requests"""

    async def test_caller_without_codec_gets_no_codec_registered(self):
        """Sending a request in a namespace with no codec raises NoCodecRegistered"""
        namespace = 'test_caller_without_codec_gets_no_codec_registered'
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask(namespace=namespace, name='no_codec_task')
        async def no_codec_task():
            return None

        await router.activate(transport)
        try:
            with self.assertRaises(NoCodecRegistered):
                await no_codec_task()
        finally:
            await router.deactivate()

    async def test_server_without_codec_refuses_to_spawn_handler(self):
        """A request reaching a server without a codec raises before any handler task is created"""
        namespace = 'test_server_without_codec_refuses_to_spawn_handler'
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        called = []

        @atask(namespace=namespace, name='no_codec_server_task')
        async def no_codec_server_task():
            called.append(True)
            return None

        await router.activate(transport)
        try:
            with self.assertRaises(NoCodecRegistered):
                router._on_request('no_codec_server_task', b'', AsyncMock())
            self.assertEqual(called, [])
            self.assertEqual(router._active_tasks, set())
        finally:
            await router.deactivate()

    async def test_server_without_codec_refuses_events_and_broadcasts(self):
        """Events and broadcasts reaching a server without a codec raise before any handler task is created"""
        namespace = 'test_server_without_codec_refuses_events_and_broadcasts'
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        called = []

        @atask_queue(namespace=namespace, name='no_codec_queue_task')
        async def no_codec_queue_task():
            called.append('queue')

        @atask_broadcast(namespace=namespace, name='no_codec_broadcast_task')
        async def no_codec_broadcast_task():
            called.append('broadcast')

        await router.activate(transport)
        try:
            with self.assertRaises(NoCodecRegistered):
                router._on_event('no_codec_queue_task', b'')
            with self.assertRaises(NoCodecRegistered):
                router._on_broadcast('no_codec_broadcast_task', b'')
            self.assertEqual(called, [])
            self.assertEqual(router._active_tasks, set())
        finally:
            await router.deactivate()
    """Sending from a namespace missing its transport or codec fails before any encoding"""

    async def test_send_without_transport_raises(self):
        """Every send method raises NoClientTransportRegistered when the namespace has no transport"""
        namespace = 'test_send_without_transport_raises'
        PickleCodec(namespace=namespace)
        router = get_router(namespace)

        with self.assertRaises(NoClientTransportRegistered):
            await router.send_request('no_transport_task')
        with self.assertRaises(NoClientTransportRegistered):
            await router.send_event('no_transport_task')
        with self.assertRaises(NoClientTransportRegistered):
            await router.send_broadcast('no_transport_task')

    async def test_send_without_codec_raises(self):
        """Every send method raises NoCodecRegistered when the namespace has a transport but no codec"""
        namespace = 'test_send_without_codec_raises'
        LoopbackTransport(namespace=namespace)
        router = get_router(namespace)

        with self.assertRaises(NoCodecRegistered):
            await router.send_request('no_codec_task')
        with self.assertRaises(NoCodecRegistered):
            await router.send_event('no_codec_task')
        with self.assertRaises(NoCodecRegistered):
            await router.send_broadcast('no_codec_task')


class JobNotFoundTest(TestCase):
    """A name that disappears from a registry after activation is refused, not silently dropped"""

    async def test_request_for_name_missing_from_rpc_registry_raises_job_not_found(self):
        """A request whose RPC registry entry was removed after activation gets JobNotFound"""
        namespace = 'test_request_for_name_missing_from_rpc_registry_raises_job_not_found'
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask(namespace=namespace, name='vanishing_task')
        async def vanishing_task():
            return None

        await router.activate(transport)
        try:
            namespaces.get(namespace).rpc_registry.unregister('vanishing_task')
            with self.assertRaises(JobNotFound):
                router._on_request('vanishing_task', await PickleCodec(namespace=namespace).encode(([], {}, [])), AsyncMock())
            self.assertEqual(router._active_tasks, set())
        finally:
            await router.deactivate()

    async def test_event_for_name_missing_from_queue_registry_raises_job_not_found(self):
        """An event whose queue registry entry was removed after activation gets JobNotFound"""
        namespace = 'test_event_for_name_missing_from_queue_registry_raises_job_not_found'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)

        @atask_queue(namespace=namespace, name='vanishing_queue')
        async def vanishing_queue():
            return None

        await router.activate(transport)
        try:
            namespaces.get(namespace).queue_registry.unregister('vanishing_queue')
            with self.assertRaises(JobNotFound):
                router._on_event('vanishing_queue', b'')
            self.assertEqual(router._active_tasks, set())
        finally:
            await router.deactivate()
