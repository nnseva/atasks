"""
Integration tests reproducing connection-loss-after-idle failures in the AMQP
transport against a real broker.

Background (reported symptom): after an AMQP transport sits idle for a while
(e.g. a long synchronous or asynchronous pause in an interactive session), the
underlying connection is found dead the next time a request is sent, and the
exception that reaches the caller is whatever raw aiormq/aio-pika exception
happened to be in flight at that moment (``AMQPConnectionError``, a bare
``CancelledError``, ``ChannelInvalidStateError``, ...) - *not* the transport's
own documented :class:`~atasks.transport.base.ConnectionLostError`. A caller
written against the documented contract - catch ``ConnectionLostError`` and
retry - does not catch these.

Note this deliberately does *not* test calling ``transport.connect()`` again
after a connection loss as a way to recover: ``aio_pika``'s ``RobustConnection``
already retries in the background on its own for any involuntary drop (broker
restart, network blip, missed heartbeats, ...), and once a connection has been
deliberately/explicitly closed (as ``AMQPTransport.disconnect()`` does, and as
these tests simulate directly to get a reliable, non-flaky repro), aio_pika
considers it permanently dead by design - even the underlying
``RobustConnection.connect()`` refuses with a ``RuntimeError`` in that case.
So the transport should simply rely on the robust connection to heal itself,
and focus on making sure every publish path fails the caller with one
consistent, documented exception in the meantime.

Both tests below are red against the current implementation and are meant to
turn green once the AMQP transport normalizes connection failures to
``ConnectionLostError`` uniformly.

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at ATASKS_TEST_AMQP_URL
(default amqp://guest:guest@localhost/). Tests are skipped (not failed) if no
broker is reachable.
"""
import asyncio
import os
import time
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase

import aio_pika

from atasks.codecs import PickleCodec
from atasks.router import get_router
from atasks.tasks import atask
from atasks.transport.backends.amqp import AMQPTransport
from atasks.transport.base import ConnectionLostError


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')


def _fresh_namespace():
    return 'test-amqp-reconnect-%s' % uuid.uuid4().hex


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


class AMQPReconnectTest(TestCase):
    """Connection-loss-after-idle handling in the AMQP transport"""

    async def asyncSetUp(self):
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)
        self.namespace = _fresh_namespace()
        PickleCodec(namespace=self.namespace)
        self._cleanup_transports = []

    async def asyncTearDown(self):
        ns_router = get_router(self.namespace)
        try:
            await ns_router.deactivate()
        except Exception:
            pass
        for transport in self._cleanup_transports:
            try:
                await transport.disconnect()
            except Exception:
                pass

    async def _new_transport(self, **kw):
        transport = AMQPTransport(namespace=self.namespace, url=AMQP_URL, prefix=self.namespace, **kw)
        await transport.connect()
        self._cleanup_transports.append(transport)
        return transport

    async def test_001_publish_after_connection_loss_raises_connection_lost_error(self):
        """Once the underlying connection is dead, send_request must fail the
        caller with the documented ``ConnectionLostError`` - never with
        whatever raw aiormq/aio-pika exception happened to come out of the
        dead channel (this reproduces the exact second traceback reported: a
        bare ``ChannelInvalidStateError: <RobustChannel> closed``).

        Deliberately closing the connection directly (rather than, say,
        killing it via the broker's management API and racing the robust
        reconnect) makes this deterministic: the connection is guaranteed to
        still be dead by the time we call send_request below, with no
        dependency on reconnect timing.

        NOTE: publish_event/publish_broadcast are NOT covered here even
        though they go through the same dead channel. Both declare a
        queue/exchange (declare_queue/declare_exchange) *before* publishing,
        and unlike basic_publish, aio_pika's declare_* calls don't notice the
        channel is closed and raise - they hang forever waiting for a server
        reply that will never come. That's a separate bug (a hang, not a
        wrong-exception-type) in a codepath the original report never
        exercised, and is deliberately left out of scope here.
        """
        client_transport = await self._new_transport()
        server_transport = await self._new_transport(queue=self.namespace)

        @atask(namespace=self.namespace, timeout=5)
        async def echo(x):
            return x

        router = get_router(self.namespace)
        await router.activate(server_transport)

        # sanity: works before anything goes wrong
        self.assertEqual(await echo(1), 1)

        # Simulate the connection dying under us - a broker restart, a
        # network partition, an idle proxy/NAT dropping the socket, ... From
        # the transport's point of view the observable state is the same in
        # every case: the connection object it is holding is now closed, and
        # (being deliberately closed) aio_pika will not bring it back on its
        # own - which is fine here, since these calls aren't expected to
        # succeed, only to fail predictably.
        await client_transport._connection.close()
        self.assertTrue(client_transport._connection.is_closed)

        with self.assertRaises(ConnectionLostError):
            await client_transport.send_request('echo', b'2', timeout=2)

    async def test_002_idle_heartbeat_timeout_raises_connection_lost_error(self):
        """Simulate the "long inactivity" scenario directly: block the event
        loop synchronously for longer than the negotiated heartbeat interval
        (as a long synchronous computation - or a long pause between
        interactive commands sharing the same loop - would), so the client
        misses its heartbeats and the broker/aiormq tear the connection down
        from under an idle transport. The next request made on it must fail
        with the transport's documented ``ConnectionLostError``, not whatever
        raw exception happened to be in flight when the connection died.
        """
        # A short heartbeat so the test does not need to wait for a realistic
        # idle duration to trigger the same condition.
        url = AMQP_URL + ('&' if '?' in AMQP_URL else '?') + 'heartbeat=1'
        client_transport = AMQPTransport(namespace=self.namespace, url=url, prefix=self.namespace)
        server_transport = AMQPTransport(
            namespace=self.namespace, url=url, prefix=self.namespace, queue=self.namespace,
        )
        await client_transport.connect()
        await server_transport.connect()
        self._cleanup_transports.extend([client_transport, server_transport])

        @atask(namespace=self.namespace, timeout=5)
        async def echo(x):
            return x

        router = get_router(self.namespace)
        await router.activate(server_transport)

        self.assertEqual(await echo(1), 1)

        # Block the event loop synchronously well past the negotiated
        # heartbeat grace period - no heartbeats can be sent or processed,
        # and aiormq's own stuck-connection watchdog tears the connection
        # down concurrently with whatever runs the moment the loop wakes up.
        time.sleep(7)

        with self.assertRaises(ConnectionLostError):
            await echo(2)
