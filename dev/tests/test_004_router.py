"""
Router tests
"""
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.codecs import PickleCodec
from atasks.router import LateRegistration, get_router
from atasks.tasks import atask, atask_broadcast, atask_queue
from atasks.transport.base import LoopbackTransport


class ModuleTest(TestCase):
    """Module tests"""
    async def test_scenarios(self):
        """Test scenarios"""
        PickleCodec()
        transport = LoopbackTransport()
        await transport.connect()
        # Importing the scenario module - which registers its @atask's at
        # module level - must happen before router.activate(transport): it
        # only subscribes to names already registered at that moment (see
        # Router.activate) and registering after it raises LateRegistration.
        from dev.tests.scenarios import request_parallel, request_sequence

        router = get_router()
        await router.activate(transport)

        await request_sequence()
        returns = await request_parallel()
        self.assertEqual(returns, [0, 1, 2, 3, 4, 0, 1, 2, 3, 4])

    async def test_late_registration_rpc(self):
        """Registering an @atask after activate() raises LateRegistration, not silently no-op"""
        PickleCodec(namespace='test_late_registration_rpc')
        transport = LoopbackTransport(namespace='test_late_registration_rpc')
        await transport.connect()
        router = get_router('test_late_registration_rpc')
        await router.activate(transport)

        with self.assertRaises(LateRegistration):
            @atask(namespace='test_late_registration_rpc')
            async def too_late(a):
                return a

    async def test_late_registration_queue(self):
        """Registering an @atask_queue after activate() raises LateRegistration"""
        PickleCodec(namespace='test_late_registration_queue')
        transport = LoopbackTransport(namespace='test_late_registration_queue')
        await transport.connect()
        router = get_router('test_late_registration_queue')
        await router.activate(transport)

        with self.assertRaises(LateRegistration):
            @atask_queue(namespace='test_late_registration_queue')
            async def too_late(a):
                return a

    async def test_late_registration_broadcast(self):
        """Registering an @atask_broadcast after activate() raises LateRegistration"""
        PickleCodec(namespace='test_late_registration_broadcast')
        transport = LoopbackTransport(namespace='test_late_registration_broadcast')
        await transport.connect()
        router = get_router('test_late_registration_broadcast')
        await router.activate(transport)

        with self.assertRaises(LateRegistration):
            @atask_broadcast(namespace='test_late_registration_broadcast')
            async def too_late(a):
                return a

    async def test_activate_deactivate_allows_reregistration(self):
        """
        deactivate() clears Router.server, so registering a new @atask is
        allowed again afterwards - and a subsequent activate() picks it up.
        """
        namespace = 'test_activate_deactivate_allows_reregistration'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        await router.activate(transport)
        await router.deactivate()

        @atask(namespace=namespace)
        async def now_ok(a):
            return a * 2

        await router.activate(transport)
        self.assertEqual(await now_ok(21), 42)
        await router.deactivate()

    async def test_deactivate_is_symmetric_with_activate(self):
        """
        deactivate() unregisters exactly the request callback registered by
        the matching activate() - a request for that name after deactivate()
        must not be servable any more.
        """
        namespace = 'test_deactivate_is_symmetric_with_activate'
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        @atask(namespace=namespace, name='some_task')
        async def some_task(a):
            return a

        router = get_router(namespace)
        await router.activate(transport)
        self.assertIn('some_task', transport._request_callbacks)
        await router.deactivate()
        self.assertNotIn('some_task', transport._request_callbacks)
