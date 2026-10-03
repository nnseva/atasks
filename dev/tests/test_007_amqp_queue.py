"""
Integration tests for the task-queue (fire-and-forget, competing consumers) pattern
against a real AMQP broker.

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at ATASKS_TEST_AMQP_URL
(default amqp://guest:guest@localhost/). An unavailable broker fails the test
unless AMQP tests were explicitly disabled with ATASKS_SKIP_AMQP_TESTS=1.
"""
import asyncio
import os
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.codecs import PickleCodec
from atasks.router import get_router
from atasks.tasks import atask_queue
from atasks.transport.backends.amqp import AMQPTransport
from dev.tests._amqp_cleanup import teardown_amqp
from dev.tests._amqp_environment import require_amqp


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')


def _fresh_namespace():
    return 'test-amqp-queue-%s' % uuid.uuid4().hex


class AMQPQueueTest(TestCase):
    """task-queue pattern: fire-and-forget, exactly one competing consumer per message"""

    async def asyncSetUp(self):
        if not await require_amqp():
            self.skipTest('AMQP integration tests explicitly disabled by ATASKS_SKIP_AMQP_TESTS=1')
        self.namespace = _fresh_namespace()
        self._cleanup_transports = []

    async def asyncTearDown(self):
        await teardown_amqp(None, self._cleanup_transports)

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
        await router.activate(transport)

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

        await worker_a._register_event_callback(name, _handle_a)
        await worker_b._register_event_callback(name, _handle_b)

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
        not dropped, if the durable queue exists"""
        name = 'late.consumer'
        publisher = await self._new_transport()
        worker = await self._new_transport()
        received = []
        got_it = asyncio.Event()

        async def _handle(content):
            received.append(content.decode())
            got_it.set()

        # The first registration will create a durable queue
        await worker._register_event_callback(name, _handle)
        await worker._unregister_event_callback(name)
        # First send the event before any consumer has registered
        await publisher.publish_event(name, b'queued-before-consumer')
        # check that the message published before the consumer was registered is NOT yet received
        await asyncio.sleep(1)  # give a moment to ensure the message is not yet received
        self.assertEqual(received, [], 'no message should be received before the consumer is registered')
        await worker._register_event_callback(name, _handle)
        await asyncio.wait_for(got_it.wait(), timeout=5)
        self.assertEqual(received, ['queued-before-consumer'])

    async def test_004_publish_before_queue_exists_is_dropped(self):
        """An event for a never-declared queue is fire-and-forget and has no replay."""
        name = 'never.declared'
        publisher = await self._new_transport()
        await publisher.publish_event(name, b'before-queue-exists')

        worker = await self._new_transport()
        received = []

        async def _handle(content):
            received.append(content.decode())

        await worker._register_event_callback(name, _handle)
        await asyncio.sleep(0.3)

        await publisher.publish_event(name, b'after-queue-exists')
        await asyncio.sleep(0.5)

        self.assertEqual(received, ['after-queue-exists'])
