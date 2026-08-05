"""
Integration tests for the broadcast/subscribe (fan-out) pattern against a real
AMQP broker - the mode used by a fleet of ws_gateway instances, each of which
must receive every published event to filter and relay to its own
independently-held WebSocket connections.

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at ATASKS_TEST_AMQP_URL
(default amqp://guest:guest@localhost/). Tests are skipped (not failed) if no
broker is reachable.
"""
import asyncio
import os
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase

import aio_pika

from atasks.codecs import PickleCodec
from atasks.router import get_router
from atasks.tasks import atask_broadcast
from atasks.transport.backends.amqp import AMQPTransport


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')


def _fresh_namespace():
    return 'test-amqp-broadcast-%s' % uuid.uuid4().hex


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


class AMQPBroadcastTest(TestCase):
    """broadcast/subscribe pattern: fire-and-forget, every subscriber gets every message"""

    async def asyncSetUp(self):
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)
        self.namespace = _fresh_namespace()
        self._cleanup_transports = []

    async def asyncTearDown(self):
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

    async def test_001_atask_broadcast_decorator_basic(self):
        """@atask_broadcast: calling the decorated function publishes and returns
        None immediately, and a subscribed instance actually gets to run it."""
        namespace = self.namespace
        PickleCodec(namespace=namespace)
        transport = await self._new_transport()

        processed = []
        done = asyncio.Event()

        # No need to embed the (already long, uuid-suffixed) namespace in the task
        # name too: AMQPTransport already scopes exchange/queue names by its own
        # `prefix` (set to the namespace for every transport in this test module),
        # and AMQP exchange/queue names are capped at 127 bytes - doubling up on
        # the namespace here would blow past that limit.
        task_name = 'record_broadcast'

        @atask_broadcast(namespace=namespace, name=task_name)
        async def record_broadcast(value):
            processed.append(value)
            done.set()

        router = get_router(namespace)
        await router.activate_broadcast(task_name, transport)
        # give the exclusive queue's binding a moment to take effect before publishing
        await asyncio.sleep(0.2)

        result = await record_broadcast('hello')
        self.assertIsNone(result)  # fire-and-forget: no result is returned to the caller

        await asyncio.wait_for(done.wait(), timeout=5)
        self.assertEqual(processed, ['hello'])

    async def test_002_every_subscriber_gets_every_message(self):
        """Three independently-connected subscriber instances each receive their own
        copy of every published event - fan-out, not competition: this is what
        distinguishes broadcast mode from the task-queue mode in test_007, and is
        exactly the topology a fleet of ws_gateway instances needs (each instance
        holds a different set of WebSocket connections and must see every event to
        decide what's relevant to its own sockets)."""
        name = 'realtime.events'
        publisher = await self._new_transport()

        subscribers = []
        received = []
        for i in range(3):
            transport = await self._new_transport()
            bucket = []
            received.append(bucket)

            def _make_handler(bucket):
                async def _handle(content):
                    bucket.append(content.decode())
                return _handle

            await transport.register_broadcast_callback(name, _make_handler(bucket))
            subscribers.append(transport)

        # exclusive queues need a beat to be declared/bound before publishing
        await asyncio.sleep(0.3)

        total = 10
        for i in range(total):
            await publisher.publish_broadcast(name, str(i).encode())

        deadline = asyncio.get_event_loop().time() + 10
        while any(len(bucket) < total for bucket in received) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected = [str(i) for i in range(total)]
        for i, bucket in enumerate(received):
            self.assertEqual(
                sorted(bucket, key=int), expected,
                'subscriber #%d should have received every single broadcast event, not a subset' % i,
            )

    async def test_003_late_subscriber_does_not_retroactively_get_earlier_events(self):
        """Broadcast subscriptions use an exclusive, auto-delete queue per instance
        (like channels_rabbitmq) - a subscriber only receives events published while
        it is actively subscribed, not a replay of history from before it joined.
        This is a documented, accepted MVP limitation (see the package README), not
        a bug - it must be explicit and tested so nobody relies on retroactive
        delivery by accident."""
        name = 'no.replay'
        publisher = await self._new_transport()
        await publisher.publish_broadcast(name, b'before-anyone-subscribed')

        transport = await self._new_transport()
        received = []

        async def _handle(content):
            received.append(content.decode())

        await transport.register_broadcast_callback(name, _handle)
        await asyncio.sleep(0.3)

        await publisher.publish_broadcast(name, b'after-subscription')
        await asyncio.sleep(0.5)

        self.assertEqual(received, ['after-subscription'])
