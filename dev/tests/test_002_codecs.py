"""
Codecs tests
"""
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.codecs import Codec, PickleCodec, get_codec


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

    async def test_002_pickle_codec(self):
        """Test, whether the pickle codec works fine"""
        c = PickleCodec()
        check = ([1, 2], {'a': 1, 'b': 2})
        encoded = await c.encode(check)
        self.assertIsInstance(encoded, bytes)
        decoded = await c.decode(encoded)
        self.assertEqual(check, decoded)
