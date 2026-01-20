"""
Router tests
"""
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.codecs import PickleCodec
from atasks.router import get_router
from atasks.transport.base import LoopbackTransport


class ModuleTest(TestCase):
    """Module tests"""
    async def test_scenarios(self):
        """Test scenarios"""
        PickleCodec()
        transport = LoopbackTransport()
        await transport.connect()
        router = get_router()
        await router.activate(transport)
        from dev.tests.scenarios import request_parallel, request_sequence

        await request_sequence()
        returns = await request_parallel()
        self.assertEqual(returns, [0, 1, 2, 3, 4, 0, 1, 2, 3, 4])
