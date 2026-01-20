"""
Transport tests
"""
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.transport.base import LoopbackTransport, Transport, get_transport


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

        async def _callback(name, content):
            self.assertIsInstance(content, bytes)
            self.assertEqual(name, 'test')
            return content

        await t.register_callback(_callback)
        result = await t.send_request('test', b'123')
        self.assertEqual(result, b'123')
