"""
Integration tests for the task-queue (fire-and-forget, competing consumers) pattern
against a real AMQP broker.

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
from atasks.tasks import atask_queue
from atasks.transport.backends.amqp import AMQPTransport


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')


def _fresh_namespace():
    return 'test-amqp-queue-%s' % uuid.uuid4().hex


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


class AMQPQueueTest(TestCase):
    """task-queue pattern: fire-and-forget, exactly one competing consumer per message"""

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

    async def test_001_atask_queue_decorator_basic(self):
        """@atask_queue: calling the decorated function publishes and returns None
        immediately, and the registered consumer actually gets to run it."""
        namespace = self.namespace
        PickleCodec(namespace=namespace)
        # Constructing the consumer transport *after* the publisher transport makes
        # it the namespace's current outbound transport too, which is irrelevant here
        # since we only ever publish through the queue-task's send_event() path (any
        # connected instance can publish to the shared durable queue by name).
        transport = await self._new_transport()

        processed = []
        done = asyncio.Event()

        # No need to embed the (already long, uuid-suffixed) namespace in the task
        # name too: AMQPTransport already scopes exchange/queue names by its own
        # `prefix` (set to the namespace for every transport in this test module),
        # and AMQP exchange/queue names are capped at 127 bytes - doubling up on
        # the namespace here would blow past that limit.
        task_name = 'record_event'

        @atask_queue(namespace=namespace, name=task_name)
        async def record_event(value):
            processed.append(value)
            done.set()

        router = get_router(namespace)
        await router.activate_queue(task_name, transport)

        result = await record_event('hello')
        self.assertIsNone(result)  # fire-and-forget: no result is returned to the caller

        await asyncio.wait_for(done.wait(), timeout=5)
        self.assertEqual(processed, ['hello'])

    async def test_002_competing_consumers_share_the_load_exactly_once(self):
        """Two independently-connected consumer instances registered for the same
        task-queue compete: every published message is delivered to exactly one of
        them - no message is lost, and none is delivered to both (competing
        consumers / classic AMQP work-queue semantics, as opposed to broadcast)."""
        name = 'shared.work'
        publisher = await self._new_transport()
        worker_a = await self._new_transport()
        worker_b = await self._new_transport()

        received_a = []
        received_b = []

        async def _handle_a(content):
            received_a.append(content.decode())

        async def _handle_b(content):
            received_b.append(content.decode())

        await worker_a.register_event_callback(name, _handle_a)
        await worker_b.register_event_callback(name, _handle_b)

        total = 40
        for i in range(total):
            await publisher.publish_event(name, str(i).encode())

        deadline = asyncio.get_event_loop().time() + 10
        while len(received_a) + len(received_b) < total and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        self.assertEqual(len(received_a) + len(received_b), total, 'every message must be delivered exactly once')
        self.assertEqual(
            set(received_a) & set(received_b), set(),
            'no message should ever be delivered to more than one competing consumer',
        )
        self.assertEqual(
            set(received_a) | set(received_b), {str(i) for i in range(total)},
            'every published message must have been delivered to someone',
        )
        # With prefetch_count=1 and 40 messages, both workers should get a share -
        # not a strict guarantee of the AMQP spec, but a reasonable sanity check
        # that this is genuine competing-consumer distribution and not one
        # instance silently starving the other.
        self.assertGreater(len(received_a), 0)
        self.assertGreater(len(received_b), 0)

    async def test_003_publish_before_any_consumer_is_not_lost(self):
        """Messages published to a task-queue before any consumer has registered are
        not dropped - publish_event declares the durable queue itself, so the event
        waits there until a competing consumer eventually attaches."""
        name = 'late.consumer'
        publisher = await self._new_transport()

        await publisher.publish_event(name, b'queued-before-consumer')

        worker = await self._new_transport()
        received = []
        got_it = asyncio.Event()

        async def _handle(content):
            received.append(content.decode())
            got_it.set()

        await worker.register_event_callback(name, _handle)
        await asyncio.wait_for(got_it.wait(), timeout=5)
        self.assertEqual(received, ['queued-before-consumer'])
