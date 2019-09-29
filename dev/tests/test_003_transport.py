"""
Transport tests
"""
import asyncio

from atasks.transport.base import LoopbackTransport, Transport, get_transport

from django.test import TestCase


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

    def test_002_loopback_transport(self):
        """Test, whether the loopback transport works fine"""
        async def _test_():
            """Async test body"""
            t = LoopbackTransport()

            async def _callback(name, content):
                self.assertIsInstance(content, bytes)
                self.assertEqual(name, 'test')
                return content

            await t.register_callback(_callback)
            result = await t.send_request('test', b'123')
            self.assertEqual(result, b'123')

        asyncio.get_event_loop().run_until_complete(_test_())
