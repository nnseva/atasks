"""
Tests for ``atasks.run``'s -N/--namespace SPEC parsing (``_parse_namespace_spec``)
and for how ``main()``/``aiomain()`` translate a parsed SPEC into the matching
:class:`atasks.router.Router` constructor arguments for its namespace -
``hostname``, ``max-trace-depth``, ``trace-filter-modules`` and
``collect-await-frames`` map one-to-one onto the Router constructor (besides
``name``/``mode``/``transport``/``url``, exercised end-to-end in
test_009_run_shutdown.py).

Each ``main()``-based test uses its own fresh namespace so the Router it
inspects can only be the one that particular call just built, never a
leftover from another test (the ``default`` namespace's registries are
process-global and persist for the life of the test run - see the analogous
comment in test_005_command.py). Every namespace here is 'client' mode with
the (default) loopback transport, so ``main()`` returns on its own - no
signal/subprocess machinery needed.
"""
import uuid
from unittest import TestCase

from atasks.router import get_router
from atasks.run import _parse_namespace_spec, main


def _fresh_namespace():
    """Every test gets its own namespace so the Router it inspects can never
    be one left behind by another test."""
    return 'test-run-router-options-%s' % uuid.uuid4().hex


class ParseNamespaceSpecTest(TestCase):
    """Unit tests for ``_parse_namespace_spec()`` itself - the SPEC grammar
    behind -N/--namespace, independent of ``main()``/``aiomain()``."""

    def test_name_only_gets_every_default(self):
        self.assertEqual(_parse_namespace_spec('name=orders'), {
            'name': 'orders',
            'mode': 'loopback',
            'transport': 'loopback',
            'url': None,
            'hostname': None,
            'max_trace_depth': None,
            'trace_filter_modules': None,
            'collect_await_frames': None,
        })

    def test_every_key_is_parsed(self):
        spec = _parse_namespace_spec(
            'name=orders,mode=server,transport=amqp,url=amqp://host/,hostname=worker-1,'
            'max-trace-depth=500,trace-filter-modules=atasks:backoff,collect-await-frames=false'
        )
        self.assertEqual(spec, {
            'name': 'orders',
            'mode': 'server',
            'transport': 'amqp',
            'url': 'amqp://host/',
            'hostname': 'worker-1',
            'max_trace_depth': 500,
            'trace_filter_modules': ['atasks', 'backoff'],
            'collect_await_frames': False,
        })

    def test_collect_await_frames_accepts_true_yes_1(self):
        for value in ('true', 'yes', '1', 'TRUE'):
            with self.subTest(value=value):
                self.assertIs(_parse_namespace_spec('name=n,collect-await-frames=%s' % value)['collect_await_frames'], True)

    def test_collect_await_frames_accepts_false_no_0(self):
        for value in ('false', 'no', '0', 'FALSE'):
            with self.subTest(value=value):
                self.assertIs(_parse_namespace_spec('name=n,collect-await-frames=%s' % value)['collect_await_frames'], False)

    def test_missing_name_is_default(self):
        spec = _parse_namespace_spec('mode=server')
        self.assertEqual(spec['name'], 'default')

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(Exception):
            _parse_namespace_spec('name=n,bogus=1')

    def test_key_given_twice_is_rejected(self):
        with self.assertRaises(Exception):
            _parse_namespace_spec('name=n,name=m')

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(Exception):
            _parse_namespace_spec('name=n,mode=wrong')

    def test_invalid_max_trace_depth_is_rejected(self):
        with self.assertRaises(Exception):
            _parse_namespace_spec('name=n,max-trace-depth=not-a-number')

    def test_invalid_collect_await_frames_is_rejected(self):
        with self.assertRaises(Exception):
            _parse_namespace_spec('name=n,collect-await-frames=maybe')


class RunRouterOptionsTest(TestCase):
    """``main()`` must translate one -N SPEC's Router-related keys into the
    matching Router constructor arguments for that namespace."""

    def test_no_router_keys_leaves_router_defaults(self):
        """With none of hostname/max-trace-depth/trace-filter-modules/
        collect-await-frames given, run.py must not construct a Router itself
        at all - get_router() is left to lazily create one with the
        constructor's own defaults."""
        namespace = _fresh_namespace()
        main(['run.py', '-N', 'name=%s' % namespace])

        router = get_router(namespace)
        self.assertEqual(router.namespace, namespace)
        self.assertEqual(router.max_trace_depth, 1000)
        self.assertEqual(router.trace_filter_modules, ())
        self.assertTrue(router.collect_await_frames)

    def test_hostname_reaches_the_router(self):
        namespace = _fresh_namespace()
        main(['run.py', '-N', 'name=%s,hostname=custom-host' % namespace])

        self.assertEqual(get_router(namespace).hostname, 'custom-host')

    def test_max_trace_depth_reaches_the_router(self):
        namespace = _fresh_namespace()
        main(['run.py', '-N', 'name=%s,max-trace-depth=5' % namespace])

        self.assertEqual(get_router(namespace).max_trace_depth, 5)

    def test_trace_filter_modules_reaches_the_router(self):
        namespace = _fresh_namespace()
        main(['run.py', '-N', 'name=%s,trace-filter-modules=atasks:backoff' % namespace])

        self.assertEqual(get_router(namespace).trace_filter_modules, ('atasks', 'backoff'))

    def test_collect_await_frames_can_be_disabled(self):
        namespace = _fresh_namespace()
        main(['run.py', '-N', 'name=%s,collect-await-frames=false' % namespace])

        self.assertFalse(get_router(namespace).collect_await_frames)

    def test_all_router_keys_reach_the_router_together(self):
        namespace = _fresh_namespace()
        main(['run.py', '-N', (
            'name=%s,hostname=custom-host,max-trace-depth=7,'
            'trace-filter-modules=atasks,collect-await-frames=false'
        ) % namespace])

        router = get_router(namespace)
        self.assertEqual(router.namespace, namespace)
        self.assertEqual(router.hostname, 'custom-host')
        self.assertEqual(router.max_trace_depth, 7)
        self.assertEqual(router.trace_filter_modules, ('atasks',))
        self.assertFalse(router.collect_await_frames)

    def test_router_options_do_not_leak_into_another_namespace(self):
        """A Router built for one namespace must never affect a sibling
        namespace's own (independently defaulted or configured) Router."""
        customized = _fresh_namespace()
        untouched = _fresh_namespace()
        main(['run.py', '-N', 'name=%s,max-trace-depth=3,collect-await-frames=false' % customized])
        main(['run.py', '-N', 'name=%s' % untouched])

        self.assertEqual(get_router(customized).max_trace_depth, 3)
        self.assertFalse(get_router(customized).collect_await_frames)
        self.assertEqual(get_router(untouched).max_trace_depth, 1000)
        self.assertTrue(get_router(untouched).collect_await_frames)

    def test_no_namespace_flag_at_all_defaults_to_default_namespace(self):
        """Omitting -N/--namespace entirely still runs exactly one namespace,
        named 'default', client mode, loopback transport."""
        main(['run.py'])

        router = get_router('default')
        self.assertEqual(router.namespace, 'default')

    def test_multiple_namespaces_get_independent_routers(self):
        first = _fresh_namespace()
        second = _fresh_namespace()
        main(['run.py', '-N', 'name=%s,max-trace-depth=11' % first, '-N', 'name=%s,max-trace-depth=22' % second])

        self.assertEqual(get_router(first).max_trace_depth, 11)
        self.assertEqual(get_router(second).max_trace_depth, 22)

    def test_duplicate_namespace_name_is_rejected(self):
        namespace = _fresh_namespace()
        with self.assertRaises(SystemExit):
            main(['run.py', '-N', 'name=%s' % namespace, '-N', 'name=%s,mode=client' % namespace])
