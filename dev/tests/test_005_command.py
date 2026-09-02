"""
Router tests
"""
from unittest import TestCase
from unittest.mock import patch

from atasks.run import main
from atasks.transport.base import LoopbackTransport


class ModuleTest(TestCase):
    """Module tests"""
    def test_run_atask(self):
        """Test scenarios"""
        main(['run.py', 'dev.tests.scenarios', '--verbosity=3', '--mode=loopback'])

    def test_run_connect_disconnect_symmetry(self):
        """Regression test: ``aiomain()`` must disconnect whatever transport it
        connects, even with zero scenarios to run - otherwise a real transport
        (e.g. AMQPTransport) leaves background tasks dangling for the interpreter
        to tear down out of order on exit, instead of a clean shutdown. See the
        reported ``python -m atasks.run -M client -T amqp`` shutdown noise
        ("Task was destroyed but it is pending!" / "Event loop is closed").

        Stands in for a real AMQP-backed run (which needs a broker, see
        test_009_run_shutdown.py) by monkeypatching a call-recording transport in
        place of AMQPTransport - this only needs to prove ``connect``/``disconnect``
        are called in strictly matching pairs regardless of transport identity, and
        (for loopback mode with atasks actually registered) that so are
        ``_register_request_callback``/``_unregister_request_callback``.
        """
        calls = []

        class RecordingTransport(LoopbackTransport):
            def __init__(self, url=None, **kwargs):
                # Accept (and ignore) the 'url' kwarg run.py passes for -T amqp.
                super().__init__(**kwargs)

            async def connect(self):
                calls.append('connect')
                await super().connect()

            async def disconnect(self):
                calls.append('disconnect')
                await super().disconnect()

            async def _register_request_callback(self, name, callback):
                calls.append('register:' + name)
                await super()._register_request_callback(name, callback)

            async def _unregister_request_callback(self, name):
                calls.append('unregister:' + name)
                await super()._unregister_request_callback(name)

        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            main(['run.py', '--verbosity=3', '--mode=client', '--transport=amqp'])
        self.assertEqual(calls, ['connect', 'disconnect'])

        calls.clear()
        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            # No scenario module is passed on this particular command line -
            # but the 'default' namespace's registries are process-global and
            # persist for the life of the test run, so whatever an *earlier*
            # test (e.g. test_run_atask, or test_004_router.py) already
            # registered into 'default' is still there and still gets
            # (un)registered here. What must hold regardless of that history:
            # connect is first, disconnect is last, and every name registered
            # during activate() is unregistered exactly once during deactivate().
            main(['run.py', '--verbosity=3', '--mode=loopback', '--transport=amqp'])
        self.assertEqual(calls[0], 'connect')
        self.assertEqual(calls[-1], 'disconnect')
        registered = [c[len('register:'):] for c in calls if c.startswith('register:')]
        unregistered = [c[len('unregister:'):] for c in calls if c.startswith('unregister:')]
        self.assertEqual(sorted(registered), sorted(unregistered))

    def test_run_activate_registers_and_deactivate_unregisters_every_atask(self):
        """With a real scenario module loaded, ``activate()``/``deactivate()``
        must (un)register a request callback for every ``@atask`` name it
        declares - one call each, never more, never fewer - and every
        registration must happen (during ``activate()``) strictly before every
        unregistration (during ``deactivate()``, at shutdown)."""
        calls = []

        class RecordingTransport(LoopbackTransport):
            def __init__(self, url=None, **kwargs):
                super().__init__(**kwargs)

            async def _register_request_callback(self, name, callback):
                calls.append(('register', name))
                await super()._register_request_callback(name, callback)

            async def _unregister_request_callback(self, name):
                calls.append(('unregister', name))
                await super()._unregister_request_callback(name)

        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            main(['run.py', 'dev.tests.scenarios', '--verbosity=3', '--mode=loopback', '--transport=amqp'])

        registered = [name for (op, name) in calls if op == 'register']
        unregistered = [name for (op, name) in calls if op == 'unregister']
        self.assertTrue(registered, 'expected at least one @atask to be registered')
        self.assertEqual(sorted(registered), sorted(unregistered))
        self.assertEqual(len(registered), len(set(registered)), 'no name should be registered twice')
        last_register_index = max(i for i, (op, _) in enumerate(calls) if op == 'register')
        first_unregister_index = min(i for i, (op, _) in enumerate(calls) if op == 'unregister')
        self.assertLess(last_register_index, first_unregister_index)
