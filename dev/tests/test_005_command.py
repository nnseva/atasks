"""
``atasks.run`` command-line runner tests
"""
import asyncio
from unittest import IsolatedAsyncioTestCase as TestCase
from unittest.mock import patch

import atasks.run as run_module
from atasks.run import aiomain, main
from atasks.transport.base import LoopbackTransport


def _ns(name, **overrides):
    """
    Build one namespace options dict exactly as ``aiomain()`` expects it -
    i.e. what ``_parse_namespace_spec()`` would return for ``name=<name>`` -
    for tests that call ``aiomain()`` directly instead of going through
    ``main()``'s command-line parsing (covered separately by
    test_011_run_router_options.py).
    """
    spec = {
        'name': name,
        'mode': 'client',
        'transport': 'loopback',
        'url': None,
        'hostname': None,
        'max_trace_depth': None,
        'trace_filter_modules': None,
        'collect_await_frames': None,
    }
    spec.update(overrides)
    return spec


async def _stop_soon(delay=0.05):
    """
    Flip ``run_module.exit_run`` shortly after being scheduled - stands in for
    an external SIGINT/SIGQUIT/SIGTERM (see ``sig_handler()``) so a 'server'
    namespace's "Listening for requests" wait loop in ``aiomain()`` returns
    instead of blocking the test forever. Real signal delivery against a
    background ``python -m atasks.run`` process is covered separately by
    test_009_run_shutdown.py.
    """
    await asyncio.sleep(delay)
    run_module.exit_run = True


class ModuleTest(TestCase):
    """Module tests"""

    async def asyncSetUp(self):
        """A previous test's (or signal's) exit_run=True must never leak into the next one."""
        run_module.exit_run = False

    def test_run_client_mode_via_cli_calls_nothing(self):
        """A 'client'-only run (the default when no -N is given at all) must
        complete and return - it never activates a Router, so it can never
        end up waiting for a signal."""
        main(['run.py', '--verbosity=3'])

    async def test_run_scenario_end_to_end(self):
        """Regression/integration test: a 'server' namespace paired with the
        loopback transport is the single-process combination that replaces
        the old ``-M loopback`` mode (see dev/tests/scenarios.py's aiomain) -
        its Router gets activated, the scenario module's own calls are
        exercised against it (raising AssertionError on the way out through
        asyncio.gather() if anything about the request/response round trip is
        wrong), and this only returns because ``_stop_soon()`` above stands in
        for a signal.
        """
        stopper = asyncio.ensure_future(_stop_soon())
        try:
            await aiomain(
                namespaces=[_ns('default', mode='server')],
                scenario=['dev.tests.scenarios'],
                opt=None,
            )
        finally:
            await stopper

    async def test_run_connect_disconnect_symmetry(self):
        """Regression test: ``aiomain()`` must disconnect whatever transport it
        connects, even with zero scenarios to run - otherwise a real transport
        (e.g. AMQPTransport) leaves background tasks dangling for the interpreter
        to tear down out of order on exit, instead of a clean shutdown. See the
        reported ``python -m atasks.run -N mode=client,transport=amqp`` shutdown
        noise ("Task was destroyed but it is pending!" / "Event loop is closed").

        Stands in for a real AMQP-backed run (which needs a broker, see
        test_009_run_shutdown.py) by monkeypatching a call-recording transport in
        place of AMQPTransport - this only needs to prove ``connect``/``disconnect``
        are called in strictly matching pairs regardless of transport identity, and
        (for the 'server' namespace, once actually activated) that so are
        ``_register_request_callback``/``_unregister_request_callback``.
        """
        calls = []

        class RecordingTransport(LoopbackTransport):
            def __init__(self, url=None, **kwargs):
                # Accept (and ignore) the 'url' kwarg run.py passes for transport=amqp.
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
            await aiomain(namespaces=[_ns('default', transport='amqp')], scenario=[], opt=None)
        self.assertEqual(calls, ['connect', 'disconnect'])

        calls.clear()
        stopper = asyncio.ensure_future(_stop_soon())
        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            try:
                # No scenario module is passed on this particular run - but the
                # 'default' namespace's registries are process-global and persist
                # for the life of the test run, so whatever an *earlier* test
                # (e.g. test_run_scenario_end_to_end, or test_004_router.py)
                # already registered into 'default' is still there and still gets
                # (un)registered here. What must hold regardless of that history:
                # connect is first, disconnect is last, and every name registered
                # during activate() is unregistered exactly once during deactivate().
                await aiomain(namespaces=[_ns('default', mode='server', transport='amqp')], scenario=[], opt=None)
            finally:
                await stopper
        self.assertEqual(calls[0], 'connect')
        self.assertEqual(calls[-1], 'disconnect')
        registered = [c[len('register:'):] for c in calls if c.startswith('register:')]
        unregistered = [c[len('unregister:'):] for c in calls if c.startswith('unregister:')]
        self.assertEqual(sorted(registered), sorted(unregistered))

    async def test_run_activate_registers_and_deactivate_unregisters_every_atask(self):
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

        stopper = asyncio.ensure_future(_stop_soon())
        with patch('atasks.transport.backends.amqp.AMQPTransport', RecordingTransport):
            try:
                await aiomain(
                    namespaces=[_ns('default', mode='server', transport='amqp')],
                    scenario=['dev.tests.scenarios'],
                    opt=None,
                )
            finally:
                await stopper

        registered = [name for (op, name) in calls if op == 'register']
        unregistered = [name for (op, name) in calls if op == 'unregister']
        self.assertTrue(registered, 'expected at least one @atask to be registered')
        self.assertEqual(sorted(registered), sorted(unregistered))
        self.assertEqual(len(registered), len(set(registered)), 'no name should be registered twice')
        last_register_index = max(i for i, (op, _) in enumerate(calls) if op == 'register')
        first_unregister_index = min(i for i, (op, _) in enumerate(calls) if op == 'unregister')
        self.assertLess(last_register_index, first_unregister_index)
