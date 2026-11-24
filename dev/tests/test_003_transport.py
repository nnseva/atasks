"""
Transport tests
"""
import asyncio
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.transport.base import (
    LoopbackTransport,
    NoRouteError,
    Transport,
    UnknownRequestName,
    get_transport,
)


class ModuleTest(TestCase):
    """Module tests"""
    def test_001_namespace_registry(self):
        """Test proper storing transport objects in a namespace"""
        t1 = Transport()
        self.assertEqual(get_transport(), t1)
        t2 = Transport()
        self.assertNotEqual(get_transport(), t1)
        self.assertEqual(get_transport(), t2)
        t3 = Transport('the test')
        self.assertEqual(get_transport('the test'), t3)

    async def test_002_loopback_transport(self):
        """Test, whether the loopback transport works fine"""
        t = LoopbackTransport()

        def _callback(content, reply):
            self.assertIsInstance(content, bytes)
            return asyncio.create_task(reply(content))

        await t._register_request_callback('test', _callback)
        result = await t.send_request('test', b'123')
        self.assertEqual(result, b'123')

    async def test_003_loopback_transport_unknown_name(self):
        """
        A request for a name with no registered callback fails fast, instead
        of imitating a real broker's silent unroutable-message drop (which
        would mean hanging until timeout or forever) - see the
        LoopbackTransport class docstring.
        """
        t = LoopbackTransport()
        with self.assertRaises(UnknownRequestName):
            await t.send_request('no such atask', b'123')

    async def test_004_loopback_transport_unregister(self):
        """Unregistering a request callback stops the transport from serving it"""
        t = LoopbackTransport()

        def _callback(content, reply):
            return asyncio.create_task(reply(content))

        await t._register_request_callback('test', _callback)
        await t._unregister_request_callback('test')
        with self.assertRaises(UnknownRequestName):
            await t.send_request('test', b'123')

    async def test_005_loopback_transport_logs_handler_errors(self):
        """Exceptions raised by event, broadcast and request handler tasks are logged, as AmqpTransport does"""
        t = LoopbackTransport()

        def _failing(content, *args):
            async def _impl():
                raise ValueError('boom')
            return asyncio.create_task(_impl())

        await t._register_event_callback('ev', _failing)
        await t._register_broadcast_callback('bc', _failing)
        await t._register_request_callback('rq', _failing)

        with self.assertLogs('atasks.transport.base', level='ERROR') as logs:
            with self.assertRaises(ValueError):
                await t.publish_event('ev', b'1')
            with self.assertRaises(ValueError):
                await t.publish_broadcast('bc', b'1')
            with self.assertRaises(ValueError):
                await t.send_request('rq', b'1')
        self.assertEqual(len(logs.records), 3)
        self.assertIn('queue event ev', logs.output[0])
        self.assertIn('broadcast event bc', logs.output[1])
        self.assertIn('request for rq', logs.output[2])

    async def test_006_loopback_request_refused_by_callback_raises_no_route(self):
        """LoopbackTransport only: a request callback that refuses (returns None) makes send_request raise NoRouteError"""
        t = LoopbackTransport()
        await t._register_request_callback('rq', lambda content, reply: None)

        with self.assertRaises(NoRouteError):
            await t.send_request('rq', b'1')

    async def test_007_loopback_event_without_consumer_is_dropped_with_warning(self):
        """Publishing an event with no registered consumer, or to a consumer that refuses, logs a warning and returns"""
        t = LoopbackTransport()
        with self.assertLogs('atasks.transport.base', level='WARNING') as logs:
            await t.publish_event('ev', b'1')
        self.assertIn('No competing consumer registered for event ev', logs.output[0])

        await t._register_event_callback('ev', lambda content: None)
        with self.assertLogs('atasks.transport.base', level='WARNING') as logs:
            await t.publish_event('ev', b'1')
        self.assertIn('Competing consumer refused event ev', logs.output[0])

    async def test_008_loopback_broadcast_without_subscriber_or_with_refusal_is_handled(self):
        """Broadcast with no subscribers logs a warning; a refusing subscriber is skipped while the others still run"""
        t = LoopbackTransport()
        with self.assertLogs('atasks.transport.base', level='WARNING') as logs:
            await t.publish_broadcast('bc', b'1')
        self.assertIn('No broadcast subscriber registered for bc', logs.output[0])

        received = []

        def _refusing(content):
            return None

        def _accepting(content):
            async def _impl():
                received.append(content)
            return asyncio.create_task(_impl())

        await t._register_broadcast_callback('bc', _refusing)
        await t._register_broadcast_callback('bc', _accepting)
        with self.assertLogs('atasks.transport.base', level='WARNING') as logs:
            await t.publish_broadcast('bc', b'1')
        self.assertIn('Broadcast subscriber refused bc', logs.output[0])
        self.assertEqual(received, [b'1'])
