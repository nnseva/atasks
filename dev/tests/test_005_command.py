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
        (and, for loopback mode, ``register_callback``/``unregister_callback``)
        are called in strictly matching pairs regardless of transport identity.
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

            async def register_callback(self, callback):
                calls.append('register_callback')
                await super().register_callback(callback)

            async def unregister_callback(self):
                calls.append('unregister_callback')
                await super().unregister_callback()

        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            main(['run.py', '--verbosity=3', '--mode=client', '--transport=amqp'])
        self.assertEqual(calls, ['connect', 'disconnect'])

        calls.clear()
        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            main(['run.py', '--verbosity=3', '--mode=loopback', '--transport=amqp'])
        self.assertEqual(calls, ['connect', 'register_callback', 'unregister_callback', 'disconnect'])
