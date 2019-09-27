"""
Codecs tests
"""
import asyncio

from atasks.codecs import Codec, PickleCodec, get_codec
from atasks.namespaces import namespaces

from django.test import TestCase


class ModuleTest(TestCase):
    """Module tests"""
    def test_001_namespace_registry(self):
        """Test proper storing codec objects in a namespace"""
        c1 = Codec()
        self.assertEqual(get_codec(), c1)
        c2 = Codec()
        self.assertNotEqual(get_codec(), c1)
        self.assertEqual(get_codec(), c2)
        c3 = Codec('the test')
        self.assertEqual(get_codec('the test'), c3)

    def test_002_pickle_codec(self):
        """Test, whether the pickle codec works fine"""
        async def _test_():
            """Async test body"""
            c = PickleCodec()
            check = ([1, 2], {'a':1, 'b':2})
            encoded = await c.encode(check)
            self.assertIsInstance(encoded, bytes)
            decoded = await c.decode(encoded)
            self.assertEqual(check, decoded)

        asyncio.get_event_loop().run_until_complete(_test_())
