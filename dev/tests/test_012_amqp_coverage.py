"""
Branch-coverage tests for atasks/transport/backends/amqp.py, targeting the
paths a `coverage run --branch` pass over the rest of dev/tests found
untested - most notably the CancelledError-vs-real-cancellation branches in
send_request/publish_event/publish_broadcast, which is exactly the kind of
code a typo (_task_was_register_callactually_cancelled, caught by review, not
by any test) can hide in silently: it lives inside an `except` clause that no
existing test ever entered.

Uses the raw AMQPTransport API directly (_register_request_callback/
send_request, _register_event_callback/publish_event,
_register_broadcast_callback/publish_broadcast) rather than
@atask/@atask_queue/@atask_broadcast, for precise control over each
scenario - the same style dev/tests/test_007_amqp_queue.py and
test_008_amqp_broadcast.py already use.

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at ATASKS_TEST_AMQP_URL
(default amqp://guest:guest@localhost/), with its management plugin enabled at
ATASKS_TEST_AMQP_MANAGEMENT_URL (default http://localhost:15672) for the tests
that need to kill a connection out from under the transport. Tests are skipped
(not failed) if either is not reachable.
"""
import asyncio
import base64
import json
import os
import time
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase
from urllib import request as urlrequest
from urllib.error import URLError

import aio_pika

from atasks.transport.backends.amqp import AMQPTransport
from atasks.transport.base import ConnectionLostError, RequestTimeoutError
from dev.tests._amqp_cleanup import teardown_amqp


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')
MANAGEMENT_URL = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_URL', 'http://localhost:15672')
MANAGEMENT_USER = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_USER', 'guest')
MANAGEMENT_PASSWORD = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_PASSWORD', 'guest')
MANAGEMENT_AUTH = base64.b64encode(('%s:%s' % (MANAGEMENT_USER, MANAGEMENT_PASSWORD)).encode()).decode()

# See the identical constant in test_010_amqp_reconnect.py: on Python 3.10
# (still supported - setup.py/tox.ini), AMQPTransport._task_was_actually_cancelled()
# has no Task.cancelling() to work with and conservatively lets every
# CancelledError propagate untouched, so the heartbeat-timeout tests below
# see a raw CancelledError there instead of the normalized ConnectionLostError.
_SUPPORTS_TASK_CANCELLING = hasattr(asyncio.Task, 'cancelling')


def _fresh_namespace():
    return 'test-amqp-coverage-%s' % uuid.uuid4().hex


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


class AMQPCoverageTest(TestCase):
    """Exercises error/edge branches in AMQPTransport that the rest of dev/tests never reach."""

    async def asyncSetUp(self):
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)
        self.namespace = _fresh_namespace()
        self._cleanup_transports = []

    async def asyncTearDown(self):
        await teardown_amqp(None, self._cleanup_transports)

    async def _new_transport(self, **kw):
        transport = AMQPTransport(namespace=self.namespace, url=AMQP_URL, prefix=self.namespace, **kw)
        await transport.connect()
        self._cleanup_transports.append(transport)
        return transport

    # -- RabbitMQ management API helpers (see test_006_amqp_rpc.py for the same pattern) --

    async def _list_connection_names(self):
        def _list():
            req = urlrequest.Request(
                MANAGEMENT_URL + '/api/connections',
                headers={'Authorization': 'Basic ' + MANAGEMENT_AUTH},
            )
            with urlrequest.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())

        loop = asyncio.get_event_loop()
        try:
            connections = await loop.run_in_executor(None, _list)
        except URLError:
            return None
        return {c['name'] for c in connections}

    async def _find_new_connection_name(self, before_names, attempts=30, delay=0.3):
        for _ in range(attempts):
            await asyncio.sleep(delay)
            after_names = await self._list_connection_names()
            if after_names is None:
                return None
            new_names = after_names - before_names
            if new_names:
                return next(iter(new_names))
        return None

    async def _close_connection(self, name):
        def _close():
            req = urlrequest.Request(
                MANAGEMENT_URL + '/api/connections/' + urlrequest.quote(name, safe=''),
                headers={'Authorization': 'Basic ' + MANAGEMENT_AUTH},
                method='DELETE',
            )
            with urlrequest.urlopen(req, timeout=5):
                pass

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _close)

    # -- real cancellation must propagate untouched, for all three publish paths --

    async def _assert_real_cancellation_propagates(self, transport, exchange_attr, coro_factory):
        """Start ``coro_factory()`` as a task, then cancel it while it is
        (deterministically - the target exchange's publish() is patched with
        an artificial delay below, since a real publish to a local broker can
        complete faster than any fixed pre-cancel delay could reliably beat)
        still suspended inside the underlying publish() call, and assert the
        resulting CancelledError comes out untouched - not swallowed and
        converted into ConnectionLostError, which is what should happen only
        for a CancelledError that *isn't* a real cancellation of this task
        (see the heartbeat-based tests below)."""
        exchange = getattr(transport, exchange_attr)
        original_publish = exchange.publish

        async def _slow_publish(*args, **kwargs):
            await asyncio.sleep(1)
            return await original_publish(*args, **kwargs)

        exchange.publish = _slow_publish
        try:
            task = asyncio.ensure_future(coro_factory())
            await asyncio.sleep(0.05)  # let the task reach the (now slow) publish() await
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            exchange.publish = original_publish

    async def test_001_send_request_real_cancellation_propagates(self):
        """Cancelling the caller's own task while send_request is awaiting the
        publish must propagate CancelledError untouched, and must not leave
        the correlation_id dangling in _awaiting_requests forever."""
        transport = await self._new_transport()
        before = dict(transport._awaiting_requests)

        async def _call():
            return await transport.send_request('whatever', b'x', timeout=5)

        await self._assert_real_cancellation_propagates(transport, '_request_exchange', _call)
        # No new correlation_id should have been left behind.
        self.assertEqual(transport._awaiting_requests, before)

    async def test_002_publish_event_real_cancellation_propagates(self):
        """Same as above, for publish_event."""
        transport = await self._new_transport()

        async def _call():
            return await transport.publish_event('some-queue', b'x')

        await self._assert_real_cancellation_propagates(transport, '_event_exchange', _call)

    async def test_003_publish_broadcast_real_cancellation_propagates(self):
        """Same as above, for publish_broadcast."""
        transport = await self._new_transport()

        async def _call():
            return await transport.publish_broadcast('some-topic', b'x')

        await self._assert_real_cancellation_propagates(transport, '_broadcast_exchange', _call)

    # -- a *bogus* CancelledError (aiormq's own internal signal, not a real
    # cancellation of our task) must be normalized to ConnectionLostError,
    # for publish_event/publish_broadcast too (send_request's version of this
    # is already covered by test_010_amqp_reconnect.py) --

    async def test_004_publish_event_idle_heartbeat_timeout_becomes_connection_lost(self):
        """Block the event loop past the negotiated heartbeat grace period (as
        a long synchronous computation would), so the connection dies from a
        bare CancelledError deep in aiormq - publish_event must still surface
        the documented ConnectionLostError, not that raw exception."""
        url = AMQP_URL + ('&' if '?' in AMQP_URL else '?') + 'heartbeat=1'
        transport = AMQPTransport(namespace=self.namespace, url=url, prefix=self.namespace)
        await transport.connect()
        self._cleanup_transports.append(transport)

        time.sleep(7)  # no heartbeats can be sent/processed while this runs

        # Which raw exception aiormq raises for the same heartbeat-timeout
        # race is itself non-deterministic - sometimes a plain socket-level
        # AMQPConnectionError (already normalized fine on any Python
        # version, via the plain `except Exception` branch), sometimes a
        # bare CancelledError specifically. Only in the latter case does the
        # Python version matter: 3.11+ normalizes it too (via
        # Task.cancelling()); 3.10 conservatively re-raises it untouched -
        # see test_010_amqp_reconnect.py for the same reasoning in full.
        expected_exc = (ConnectionLostError, asyncio.CancelledError) if not _SUPPORTS_TASK_CANCELLING \
            else ConnectionLostError
        with self.assertRaises(expected_exc):
            await transport.publish_event('some-queue', b'x')

    async def test_005_publish_broadcast_idle_heartbeat_timeout_becomes_connection_lost(self):
        """Same as above, for publish_broadcast."""
        url = AMQP_URL + ('&' if '?' in AMQP_URL else '?') + 'heartbeat=1'
        transport = AMQPTransport(namespace=self.namespace, url=url, prefix=self.namespace)
        await transport.connect()
        self._cleanup_transports.append(transport)

        time.sleep(7)

        # Which raw exception aiormq raises for the same heartbeat-timeout
        # race is itself non-deterministic - sometimes a plain socket-level
        # AMQPConnectionError (already normalized fine on any Python
        # version, via the plain `except Exception` branch), sometimes a
        # bare CancelledError specifically. Only in the latter case does the
        # Python version matter: 3.11+ normalizes it too (via
        # Task.cancelling()); 3.10 conservatively re-raises it untouched -
        # see test_010_amqp_reconnect.py for the same reasoning in full.
        expected_exc = (ConnectionLostError, asyncio.CancelledError) if not _SUPPORTS_TASK_CANCELLING \
            else ConnectionLostError
        with self.assertRaises(expected_exc):
            await transport.publish_broadcast('some-topic', b'x')

    # -- a handler raising must not crash the consumer or leave the caller hanging --

    async def test_006_rpc_handler_exception_times_out_caller_without_crashing_consumer(self):
        """A registered RPC handler that raises must not propagate anywhere -
        the caller just times out (no reply was ever sent) - and the consumer
        must keep working for the next request."""
        server = await self._new_transport(queue=self.namespace)
        client = await self._new_transport()

        calls = {'n': 0}

        async def handler(request):
            calls['n'] += 1
            if calls['n'] == 1:
                raise ValueError('boom')
            return b'ok'

        await server._register_request_callback('whatever', handler)

        with self.assertLogs('atasks.transport.backends.amqp', level='ERROR') as logs:
            with self.assertRaises(RequestTimeoutError):
                await client.send_request('whatever', b'1', timeout=2)
        self.assertTrue(any('Unhandled error handling request' in m for m in logs.output))

        # the consumer must still be alive for the next request
        result = await client.send_request('whatever', b'2', timeout=5)
        self.assertEqual(result, b'ok')

    async def test_007_event_handler_exception_does_not_kill_the_consumer(self):
        """Same guarantee for task-queue consumers."""
        name = 'coverage.event.boom'
        publisher = await self._new_transport()
        worker = await self._new_transport()

        received = []
        got_second = asyncio.Event()

        async def handler(content):
            if content == b'first':
                raise ValueError('boom')
            received.append(content)
            got_second.set()

        await worker._register_event_callback(name, handler)

        with self.assertLogs('atasks.transport.backends.amqp', level='ERROR') as logs:
            await publisher.publish_event(name, b'first')
            await publisher.publish_event(name, b'second')
            await asyncio.wait_for(got_second.wait(), timeout=5)
        self.assertTrue(any('Unhandled error handling queue event' in m for m in logs.output))
        self.assertEqual(received, [b'second'])

    async def test_008_broadcast_handler_exception_does_not_kill_the_consumer(self):
        """Same guarantee for broadcast subscribers."""
        name = 'coverage.broadcast.boom'
        publisher = await self._new_transport()
        subscriber = await self._new_transport()

        received = []
        got_second = asyncio.Event()

        async def handler(content):
            if content == b'first':
                raise ValueError('boom')
            received.append(content)
            got_second.set()

        await subscriber._register_broadcast_callback(name, handler)
        # give the exclusive queue's binding a moment to actually land before
        # publishing - unlike task-queue's durable queue, there is no
        # "published before any subscriber" redelivery safety net here.
        await asyncio.sleep(0.3)

        with self.assertLogs('atasks.transport.backends.amqp', level='ERROR') as logs:
            await publisher.publish_broadcast(name, b'first')
            await publisher.publish_broadcast(name, b'second')
            await asyncio.wait_for(got_second.wait(), timeout=5)
        self.assertTrue(any('Unhandled error handling broadcast event' in m for m in logs.output))
        self.assertEqual(received, [b'second'])

    # -- reply-publish failure: the request was handled, but telling the caller failed --

    async def test_009_reply_publish_failure_is_logged_and_caller_times_out(self):
        """If publishing the *reply* fails (as opposed to the request itself),
        the failure must be logged and swallowed - not crash the consumer -
        and the caller (having never received a reply) simply times out.

        The failure is injected deterministically (patching
        server._response_exchange.publish to raise) rather than via a real
        connection kill: killing the connection for real turned out to
        sometimes surface as a bare CancelledError here instead of a plain
        Exception - which this specific except clause does not catch (unlike
        send_request/publish_event/publish_broadcast, it has no
        _task_was_actually_cancelled() guard) - a distinct, separately worth
        addressing asymmetry, not what this test is targeting.
        """
        server = await self._new_transport(queue=self.namespace)
        client = await self._new_transport()

        async def handler(request):
            return b'ok'

        await server._register_request_callback('whatever', handler)

        original_publish = server._response_exchange.publish

        async def _boom(*args, **kwargs):
            raise RuntimeError('simulated reply-publish failure')

        server._response_exchange.publish = _boom
        try:
            with self.assertLogs('atasks.transport.backends.amqp', level='ERROR') as logs:
                with self.assertRaises(RequestTimeoutError):
                    await client.send_request('whatever', b'1', timeout=2)
            self.assertTrue(any('Failed to publish response' in m for m in logs.output))
        finally:
            server._response_exchange.publish = original_publish

        # the consumer must still be alive for the next request
        result = await client.send_request('whatever', b'2', timeout=5)
        self.assertEqual(result, b'ok')

    # -- a response arriving for an already-finished/unknown request is discarded, not fatal --

    async def test_010_late_response_for_timed_out_request_is_discarded(self):
        """A response that finally arrives after the caller has already given
        up (RequestTimeoutError already raised and the correlation_id already
        popped) must be logged and discarded, not raise anywhere."""
        server = await self._new_transport(queue=self.namespace)
        client = await self._new_transport()

        release = asyncio.Event()

        async def slow_handler(request):
            await release.wait()
            return b'too-late'

        await server._register_request_callback('whatever', slow_handler)

        with self.assertRaises(RequestTimeoutError):
            await client.send_request('whatever', b'1', timeout=1)

        with self.assertLogs('atasks.transport.backends.amqp', level='WARNING') as logs:
            release.set()
            # give the late response a moment to actually arrive
            for _ in range(20):
                if any('already finished request' in m for m in logs.output):
                    break
                await asyncio.sleep(0.2)
        self.assertTrue(any('already finished request' in m for m in logs.output))

    # -- unregister paths dev/tests never otherwise exercises --

    async def test_011_unregister_broadcast_callback_full_lifecycle(self):
        """_register_broadcast_callback + _unregister_broadcast_callback: after
        unregistering, further broadcasts must not reach the old handler."""
        name = 'coverage.broadcast.unregister'
        publisher = await self._new_transport()
        subscriber = await self._new_transport()

        received = []

        async def handler(content):
            received.append(content)

        await subscriber._register_broadcast_callback(name, handler)
        await asyncio.sleep(0.3)
        await publisher.publish_broadcast(name, b'before-unregister')
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(received, [b'before-unregister'])

        await subscriber._unregister_broadcast_callback(name)
        # Give the now-consumerless exclusive/auto-delete queue a moment to
        # actually disappear broker-side before publishing again.
        await asyncio.sleep(0.3)
        try:
            await publisher.publish_broadcast(name, b'after-unregister')
        except ConnectionLostError:
            # A message published to a routing key with no bound queue can,
            # right in the window where the last consumer just unsubscribed
            # and its auto-delete queue is disappearing, come back as a
            # delivery nack instead of being silently dropped as unroutable -
            # observed in CI (RabbitMQ 4.3.5) though not reproduced locally.
            # Either way, what this test actually checks is that the old
            # handler never sees the message - not that the publish call
            # itself is guaranteed to succeed in this specific timing window.
            pass
        await asyncio.sleep(0.5)
        self.assertEqual(received, [b'before-unregister'])  # nothing more arrived

    async def test_012_unregister_event_callback_never_registered_is_a_noop(self):
        """Calling _unregister_event_callback for a name that was never
        registered on this instance must not raise."""
        transport = await self._new_transport()
        await transport._unregister_event_callback('never-registered')  # must not raise

    async def test_012b_unregister_broadcast_callback_never_registered_is_a_noop(self):
        """Same as above, for _unregister_broadcast_callback - the negative
        branch of its `queue is not None and consumer_tag is not None and
        ...` guard, never exercised by test_011's happy-path lifecycle."""
        transport = await self._new_transport()
        await transport._unregister_broadcast_callback('never-registered')  # must not raise

    # -- connect()/disconnect() idempotency --

    async def test_013_connect_called_twice_is_a_noop(self):
        transport = AMQPTransport(namespace=self.namespace, url=AMQP_URL, prefix=self.namespace)
        await transport.connect()
        self._cleanup_transports.append(transport)
        original_connection = transport._connection
        await transport.connect()  # must not raise, must not reconnect
        self.assertIs(transport._connection, original_connection)

    async def test_014_disconnect_without_connect_is_a_noop(self):
        transport = AMQPTransport(namespace=self.namespace, url=AMQP_URL, prefix=self.namespace)
        await transport.disconnect()  # must not raise

    async def test_015_unregister_request_callback_without_ever_registering_is_safe(self):
        transport = await self._new_transport()
        # must not raise even though _register_request_callback was never called for this name
        await transport._unregister_request_callback('never-registered')

    # -- the reconnect callback actually fires after a real reconnect --

    async def test_016_reconnected_callback_logs_after_connection_recovers(self):
        """After the broker forcibly drops the connection, aio_pika's own
        automatic reconnect must eventually fire _on_reconnected."""
        before_names = await self._list_connection_names()
        if before_names is None:
            self.skipTest('RabbitMQ management API not reachable at %s' % MANAGEMENT_URL)
        await asyncio.sleep(6)
        before_names = await self._list_connection_names()

        await self._new_transport()
        connection_name = await self._find_new_connection_name(before_names)
        if connection_name is None:
            self.skipTest("Could not identify the transport's AMQP connection via the management API")

        with self.assertLogs('atasks.transport.backends.amqp', level='INFO') as logs:
            await self._close_connection(connection_name)
            for _ in range(30):
                if any('Reconnected transport' in m for m in logs.output):
                    break
                await asyncio.sleep(0.3)
        self.assertTrue(any('Reconnected transport' in m for m in logs.output))
