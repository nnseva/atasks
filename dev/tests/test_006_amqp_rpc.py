"""
Integration tests for the RPC (request/response) pattern against a real AMQP broker.

Covers correlation_id/reply-to round-tripping, request timeout handling, worker
crash / connection loss handling, and composing @atask with the backoff package
on both the caller and the worker side.

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at ATASKS_TEST_AMQP_URL
(default amqp://guest:guest@localhost/). Tests are skipped (not failed) if no
broker is reachable.
"""
import asyncio
import base64
import os
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase
from urllib import request as urlrequest
from urllib.error import URLError

import aio_pika
import backoff

from atasks.codecs import PickleCodec
from atasks.namespaces import namespaces
from atasks.router import get_router
from atasks.tasks import atask
from atasks.transport.backends.amqp import AMQPTransport
from atasks.transport.base import ConnectionLostError, RequestTimeoutError
from dev.tests._amqp_cleanup import teardown_amqp


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')
# NOTE: urllib does not honour user:pass@host in a URL (it's not an HTTP auth
# mechanism, just a URI syntax convenience most HTTP libraries choose not to
# implement) - management API credentials are supplied separately, as a Basic
# auth header, below.
MANAGEMENT_URL = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_URL', 'http://localhost:15672')
MANAGEMENT_USER = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_USER', 'guest')
MANAGEMENT_PASSWORD = os.environ.get('ATASKS_TEST_AMQP_MANAGEMENT_PASSWORD', 'guest')
MANAGEMENT_AUTH = base64.b64encode(('%s:%s' % (MANAGEMENT_USER, MANAGEMENT_PASSWORD)).encode()).decode()


def _fresh_namespace():
    """Every test gets its own namespace so registries/routers/transports never collide."""
    return 'test-amqp-rpc-%s' % uuid.uuid4().hex


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


class AMQPRPCTest(TestCase):
    """RPC pattern (correlation_id/reply-to, timeout, crash handling, backoff stacking)"""

    async def asyncSetUp(self):
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)
        self.namespace = _fresh_namespace()
        PickleCodec(namespace=self.namespace)
        # Transport.__init__ registers itself as *the* current transport for the
        # namespace (used by Router.send_request/send_event/send_broadcast to look
        # up "the" outbound transport) - construct the server transport first and
        # the client transport last, so it is the client transport that ends up as
        # the namespace's outbound transport while the server transport is bound
        # explicitly (and separately) via router.activate() below.
        # Also give the request queue itself a namespace-unique name: the default
        # ('atask' for every instance) would otherwise be shared broker-side across
        # every test method/run, accumulating stale bindings over time.
        self.server_transport = AMQPTransport(
            namespace=self.namespace, url=AMQP_URL, prefix=self.namespace, queue=self.namespace,
        )
        self.client_transport = AMQPTransport(namespace=self.namespace, url=AMQP_URL, prefix=self.namespace)
        await self.server_transport.connect()
        await self.client_transport.connect()

    async def asyncTearDown(self):
        ns = namespaces.get(self.namespace)
        router = getattr(ns, 'router', None)
        await teardown_amqp(
            router,
            [getattr(self, 'client_transport', None), getattr(self, 'server_transport', None)],
        )

    async def test_001_round_trip(self):
        """Basic correlation_id/reply-to round trip: the client transport calls out,
        the server transport (a different connection entirely) replies, and the
        client gets back exactly the right result for the right call."""
        namespace = self.namespace

        @atask(namespace=namespace)
        async def add(a, b):
            return a + b

        router = get_router(namespace)
        await router.activate(self.server_transport)

        results = await asyncio.gather(add(1, 2), add(10, 20), add(100, 200))
        self.assertEqual(results, [3, 30, 300])

    async def test_002_remote_exception_propagates(self):
        """An exception raised inside the remote coroutine is re-raised to the caller."""
        namespace = self.namespace

        @atask(namespace=namespace)
        async def boom():
            raise ValueError('kaboom')

        router = get_router(namespace)
        await router.activate(self.server_transport)

        with self.assertRaises(ValueError):
            await boom()

    async def test_003_timeout_when_no_worker(self):
        """A call whose worker never responds (never registered, or crashed) must
        raise RequestTimeoutError after the configured timeout, not hang forever."""
        namespace = self.namespace

        @atask(namespace=namespace, timeout=1)
        async def never_answered():
            return 'unreachable'  # never actually registered/served on server_transport

        loop = asyncio.get_event_loop()
        start = loop.time()
        with self.assertRaises(RequestTimeoutError):
            await never_answered()
        elapsed = loop.time() - start
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 5)

    async def test_004_slow_worker_times_out(self):
        """A worker that is registered but too slow also surfaces RequestTimeoutError -
        from the caller's perspective, indistinguishable from a crashed worker."""
        namespace = self.namespace

        @atask(namespace=namespace, timeout=1)
        async def slow():
            await asyncio.sleep(10)
            return 'too late'

        router = get_router(namespace)
        await router.activate(self.server_transport)

        with self.assertRaises(RequestTimeoutError):
            await slow()

    async def test_005_connection_lost_fails_in_flight_request(self):
        """Simulate a broker-side connection kill (as would happen on a worker crash
        or network partition) while a request is in flight: the caller must get a
        clear ConnectionLostError promptly, not hang until some later reconnect."""
        namespace = self.namespace

        # The management API's stats aggregation lags real connection state by
        # several seconds (observed empirically against this broker's default
        # stats interval - a read taken right after connecting reliably comes
        # back empty even though the connection is real). Identify our new
        # connection by diffing the before/after connection-name sets rather
        # than relying on any property visible immediately, but first give the
        # "before" snapshot a conservative settle time: a naive
        # read-until-two-consecutive-reads-match doesn't work here, because two
        # consecutive *empty* reads spaced closer than the lag itself are
        # indistinguishable from "settled" and would match too early, before
        # setUp's real connections are reflected - then those show up as
        # spurious "new" connections later and confuse the diff.
        await asyncio.sleep(6)
        before_names = await self._list_connection_names()
        if before_names is None:
            self.skipTest('RabbitMQ management API not reachable at %s' % MANAGEMENT_URL)

        client_transport = AMQPTransport(namespace=namespace, url=AMQP_URL, prefix=namespace)
        await client_transport.connect()

        @atask(namespace=namespace, timeout=30)
        async def never_answered_either():
            return 'unreachable'

        # Fire the request in the background using a dedicated router/transport pair,
        # then kill the underlying connection via the management API mid-flight.
        call_task = asyncio.ensure_future(never_answered_either())

        target_name = await self._find_new_connection_name(before_names, attempts=30, delay=0.5)
        if target_name is None:
            call_task.cancel()
            await client_transport.disconnect()
            self.skipTest('Could not identify the new AMQP connection via the management API')

        await self._close_connection(target_name)

        try:
            with self.assertRaises(ConnectionLostError):
                await asyncio.wait_for(call_task, timeout=10)
        finally:
            await client_transport.disconnect()

    async def _list_connection_names(self):
        def _list():
            req = urlrequest.Request(
                MANAGEMENT_URL + '/api/connections',
                headers={'Authorization': 'Basic ' + MANAGEMENT_AUTH},
            )
            with urlrequest.urlopen(req, timeout=5) as resp:
                import json
                return json.loads(resp.read())

        loop = asyncio.get_event_loop()
        try:
            connections = await loop.run_in_executor(None, _list)
        except URLError:
            return None
        return {c['name'] for c in connections}

    async def _find_new_connection_name(self, before_names, attempts=20, delay=0.3):
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

    async def test_006_backoff_stacks_on_both_sides(self):
        """Verify the documented decorator-stacking pattern actually composes:

            @backoff.on_exception(...)   # caller-side retry of the whole remote call
            @atask
            @backoff.on_exception(...)   # worker-side retry of the local execution

        Both @atask and backoff.on_exception just wrap an async function, so this
        should "fall out" of correct design - this test proves it concretely.
        """
        namespace = self.namespace
        attempts = {'worker': 0}

        @atask(namespace=namespace, timeout=5)
        @backoff.on_exception(backoff.constant, ValueError, max_tries=3, interval=0.05)
        async def flaky_worker_side(x):
            attempts['worker'] += 1
            if attempts['worker'] < 3:
                raise ValueError('transient failure #%d' % attempts['worker'])
            return x * 2

        router = get_router(namespace)
        await router.activate(self.server_transport)

        # The worker-side backoff.on_exception should absorb the first two failures
        # internally and only the final, successful attempt's result crosses the wire.
        result = await flaky_worker_side(21)
        self.assertEqual(result, 42)
        self.assertEqual(attempts['worker'], 3)

        # Now verify caller-side stacking with a *real* race, not a simulated one:
        # the worker only starts serving requests after a short delay (as if it
        # were still starting up / recovering from a crash), so the first couple
        # of real @atask calls genuinely time out over the wire before
        # backoff.on_exception on the *caller* side retries the whole call and it
        # eventually reaches the now-available worker and succeeds.
        #
        # Registering a new @atask requires the router to not be active (see
        # Router.activate/LateRegistration) - deactivate the first part's
        # activation of self.server_transport before registering
        # becomes_available_late; nothing further needs flaky_worker_side served.
        # teardown_amqp also deletes flaky_worker_side's durable queue and
        # disconnects self.server_transport early (harmless - it's not used
        # again in this test, and asyncTearDown's own cleanup is idempotent).
        await teardown_amqp(router, [self.server_transport])

        late_server_transport = AMQPTransport(namespace=namespace, url=AMQP_URL, prefix=namespace, queue=namespace)
        await late_server_transport.connect()

        @atask(namespace=namespace, timeout=0.5)
        async def becomes_available_late(x):
            return x + 1

        async def _activate_after_delay():
            await asyncio.sleep(0.7)
            await get_router(namespace).activate(late_server_transport)

        activation = asyncio.ensure_future(_activate_after_delay())

        @backoff.on_exception(backoff.constant, RequestTimeoutError, max_tries=5, interval=0.1)
        async def call_with_retry(x):
            return await becomes_available_late(x)

        result = await call_with_retry(41)
        self.assertEqual(result, 42)

        await activation
        # teardown_amqp deactivates (cancelling the AMQP consumer through the
        # still-live channel) and deletes becomes_available_late's durable
        # queue before disconnecting late_server_transport - doing it in the
        # other order (disconnect first) would leave the deactivate/delete
        # step trying to reach an already-torn-down channel/connection.
        await teardown_amqp(get_router(namespace), [late_server_transport])

    async def test_007_instance_without_a_handler_never_receives_or_loses_its_requests(self):
        """Regression test for the original bug this architecture was changed to fix
        (see ATASK-NEW-ARCHITECTURE-PLAN.md): two independent instances - modelling
        two hosts in a heterogeneous fleet, each with its own registered @atask
        subset - connected to the *same* broker with the *same* routing-key/queue
        prefix. Before the per-name RPC queue fix, every instance that had called
        ``router.activate()`` shared one mask-bound queue regardless of which
        @atasks it had actually registered, so a request for a name only the
        *other* instance could serve would land, roughly half the time, on an
        instance with no handler for it - raising JobNotFound there and losing
        the message (it was already acked off the queue before that was
        discovered). With one queue per registered name, an instance that never
        registered a given name is never bound to (and can never dequeue from)
        that name's queue in the first place - so this must hold for every one
        of many concurrent, interleaved calls, not just "most of the time".
        """
        shared_prefix = 'test-isolation-%s' % uuid.uuid4().hex
        namespace_a = _fresh_namespace()
        namespace_b = _fresh_namespace()
        PickleCodec(namespace=namespace_a)
        PickleCodec(namespace=namespace_b)

        # Same prefix (routing-key namespace) *and* same queue-naming prefix for
        # both instances - exactly the "one shared deployment, two differently
        # capable hosts" scenario from the architecture plan - only the set of
        # locally registered @atask names differs between them.
        transport_a = AMQPTransport(namespace=namespace_a, url=AMQP_URL, prefix=shared_prefix, queue=shared_prefix)
        transport_b = AMQPTransport(namespace=namespace_b, url=AMQP_URL, prefix=shared_prefix, queue=shared_prefix)
        await transport_a.connect()
        await transport_b.connect()

        @atask(namespace=namespace_a, name='only_a', timeout=5)
        async def only_a(x):
            return ('a', x)

        @atask(namespace=namespace_b, name='only_b', timeout=5)
        async def only_b(x):
            return ('b', x)

        router_a = get_router(namespace_a)
        router_b = get_router(namespace_b)
        await router_a.activate(transport_a)
        await router_b.activate(transport_b)
        try:
            # router_a.send_request()/router_b.send_request() publish through
            # transport_a/transport_b respectively - both connected to the same
            # broker/exchange/prefix, so it is genuinely irrelevant *which*
            # instance's connection does the publishing; what matters is which
            # instance's queue the routing key can reach.
            #
            # Many concurrent, interleaved calls for both names at once: with
            # the old shared-queue bug, roughly half of the calls for a name
            # would have been round-robined to the instance without a handler
            # for it and lost (surfacing here as RequestTimeoutError).
            count = 20
            results_b_via_a = await asyncio.gather(
                *[router_a.send_request('only_b', i, timeout=5) for i in range(count)],
                *[router_b.send_request('only_a', i, timeout=5) for i in range(count)],
            )
            results_only_b = results_b_via_a[:count]
            results_only_a = results_b_via_a[count:]
            self.assertEqual(results_only_b, [('b', i) for i in range(count)])
            self.assertEqual(results_only_a, [('a', i) for i in range(count)])
        finally:
            await teardown_amqp(router_a, [transport_a])
            await teardown_amqp(router_b, [transport_b])
