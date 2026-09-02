"""
Transport tests
"""
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.transport.base import (
    LoopbackTransport,
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

        async def _callback(content):
            self.assertIsInstance(content, bytes)
            return content

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

        async def _callback(content):
            return content

        await t._register_request_callback('test', _callback)
        await t._unregister_request_callback('test')
        with self.assertRaises(UnknownRequestName):
            await t.send_request('test', b'123')
