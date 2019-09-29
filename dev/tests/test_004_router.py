"""
Router tests
"""
import asyncio

from atasks.codecs import PickleCodec
from atasks.router import get_router
from atasks.transport.base import LoopbackTransport

from django.test import TestCase


class ModuleTest(TestCase):
    """Module tests"""
    def test_scenarios(self):
        """Test scenarios"""
        async def _test_():
            """Async test body"""
            PickleCodec()
            transport = LoopbackTransport()
            await transport.connect()
            router = get_router()
            await router.activate(transport)
            from dev.tests.scenarios import (
                request_sequence, request_parallel
            )

            await request_sequence()
            returns = await request_parallel()
            self.assertEqual(returns, [0, 1, 2, 3, 4, 0, 1, 2, 3, 4])

        asyncio.get_event_loop().run_until_complete(_test_())
