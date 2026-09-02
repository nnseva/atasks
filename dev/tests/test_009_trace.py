"""
Atask call-chain tracing tests (see TRACE-ATASK-STACK.md)
"""
import asyncio
import os
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase

import aio_pika

import atasks
from atasks import trace
from atasks.codecs import PickleCodec
from atasks.router import Router, get_router
from atasks.tasks import atask, atask_broadcast, atask_queue
from atasks.transport.backends.amqp import AMQPTransport
from atasks.transport.base import LoopbackTransport
from dev.tests._amqp_cleanup import teardown_amqp


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')
# The real atasks *package* directory - not a naive '/atasks/' substring check,
# which would also (wrongly) match this very test file: the repo checkout
# itself happens to be named "atasks" too.
ATASKS_PACKAGE_DIR = os.path.dirname(os.path.abspath(atasks.__file__))


def _fresh_namespace():
    """Every test gets its own namespace so registries/routers/transports never collide."""
    return 'test-trace-%s' % uuid.uuid4().hex


async def _check_broker_reachable():
    """True if an AMQP broker answers at AMQP_URL within 2 seconds"""
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


async def _raise_after_await(x):
    """Ordinary (non-atask) async helper - exercises await_frames/raise_frames collection."""
    await asyncio.sleep(0)
    raise ValueError('boom')


class LoopbackTraceTest(TestCase):
    """Trace collection over the in-process LoopbackTransport"""

    async def _wire(self, namespace, **router_options):
        """
        Construct a Router (with the given options) plus codec/transport for a
        fresh namespace.

        Deliberately does *not* call ``router.activate()`` - it only
        subscribes to atasks already registered at the moment it runs, so the
        caller must define every ``@atask``/``@atask_queue``/``@atask_broadcast``
        for this namespace first, then call ``await router.activate(transport)``
        itself (see ``Router.activate``/``LateRegistration``).
        """
        Router(namespace=namespace, **router_options)
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        return router, transport

    async def test_001_rpc_chain_carries_full_trace_to_the_root(self):
        """A three-hop RPC chain (root -> middle -> leaf) that fails must let the
        original caller analyse both the upstream (root -> leaf) atask chain and
        the ordinary await frames on either side of it."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace, hostname='host-under-test')

        @atask(namespace=namespace)
        async def leaf(x):
            await asyncio.sleep(0)
            return await _raise_after_await(x)

        @atask(namespace=namespace)
        async def middle(x):
            return await leaf(x)

        @atask(namespace=namespace)
        async def root(x):
            return await middle(x)

        await router.activate(transport)

        # passed through a variable, not a literal, at the call site: a literal
        # would legitimately show up in the source-line text traceback always
        # captures (exactly as a plain Python traceback would) - a variable is
        # the realistic case, and is what the "no call arguments" guarantee is
        # actually about.
        secret = 'super-secret-argument'
        with self.assertRaises(ValueError) as ctx:
            await root(secret)

        info = trace.get_trace(ctx.exception)
        self.assertIsNotNone(info)

        self.assertEqual(len(info.hops), 3)
        self.assertEqual([hop.seq for hop in info.hops], [0, 1, 2])
        self.assertTrue(all(hop.kind == 'rpc' for hop in info.hops))
        self.assertTrue(all(hop.host == 'host-under-test' for hop in info.hops))
        self.assertEqual(len({hop.call_id for hop in info.hops}), 3)  # all unique
        self.assertTrue(info.hops[0].task.endswith('.root'))
        self.assertTrue(info.hops[1].task.endswith('.middle'))
        self.assertTrue(info.hops[2].task.endswith('.leaf'))
        # hop N's caller_func names the function that made call N - i.e. who called whom
        self.assertEqual(info.hops[1].caller_func, 'root')
        self.assertEqual(info.hops[2].caller_func, 'middle')
        # only the very first hop (called directly, not from within another
        # atask's handler) is the root - there is exactly one, always seq 0
        self.assertEqual([hop.is_root for hop in info.hops], [True, False, False])

        # the code that actually raised is visible past the last atask hop
        self.assertTrue(any(f.func == '_raise_after_await' for f in info.raise_frames))
        self.assertTrue(any(f.func == 'leaf' for f in info.raise_frames))
        self.assertEqual(info.raise_host, 'host-under-test')

        # no call argument, anywhere, leaks into the collected trace
        rendered = trace.format_trace(ctx.exception)
        self.assertNotIn(secret, rendered)
        for hop in info.hops:
            self.assertNotIn(secret, repr(hop))

        self.assertIn('»RPC«', rendered)
        self.assertIn('root', rendered)
        self.assertIn('middle', rendered)
        self.assertIn('leaf', rendered)

    async def test_002_rpc_failure_is_not_logged_by_the_router(self):
        """An RPC failure is routed back to the caller silently - the router itself
        must not log it, since the caller decides whether/how to log it."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace)

        @atask(namespace=namespace)
        async def boom():
            raise ValueError('kaboom')

        await router.activate(transport)

        with self.assertNoLogs('atasks.router', level='WARNING'):
            with self.assertRaises(ValueError):
                await boom()

    async def test_003_queue_failure_terminates_with_the_full_trace_logged(self):
        """A failing atask_queue consumer has no caller to report back to - the
        failure terminates there, logging the full collected trace."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace)

        task_name = 'failing_queue_task'

        @atask_queue(namespace=namespace, name=task_name)
        async def failing_queue_task(x):
            raise RuntimeError('queue boom: %s' % x)

        await router.activate(transport)

        with self.assertLogs('atasks.router', level='ERROR') as logs:
            await failing_queue_task(1)
        self.assertTrue(any('Atask call chain' in message for message in logs.output))
        self.assertTrue(any('»QUEUE«' in message for message in logs.output))

    async def test_004_broadcast_failure_terminates_with_the_full_trace_logged(self):
        """A failing atask_broadcast subscriber has no caller to report back to -
        the failure terminates there, logging the full collected trace."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace)

        task_name = 'failing_broadcast_task'

        @atask_broadcast(namespace=namespace, name=task_name)
        async def failing_broadcast_task(x):
            raise RuntimeError('broadcast boom: %s' % x)

        await router.activate(transport)

        with self.assertLogs('atasks.router', level='ERROR') as logs:
            await failing_broadcast_task(1)
        self.assertTrue(any('»BROADCAST«' in message for message in logs.output))

    async def test_005_max_trace_depth_is_enforced(self):
        """A runaway recursive atask call chain is stopped locally, before ever
        reaching the transport, once it would exceed max_trace_depth."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace, max_trace_depth=5)

        @atask(namespace=namespace)
        async def recurse(depth):
            if depth >= 1000:
                return depth  # pragma: no cover - safety net, the depth guard fires first
            return await recurse(depth + 1)

        await router.activate(transport)

        with self.assertRaises(trace.AtaskStackTooDeep):
            await recurse(0)

    async def test_006_collect_await_frames_false_keeps_only_atask_hops(self):
        """With collect_await_frames disabled, no ordinary await frame is
        collected anywhere - only the atask hops themselves remain."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace, collect_await_frames=False)

        @atask(namespace=namespace)
        async def leaf(x):
            return await _raise_after_await(x)

        @atask(namespace=namespace)
        async def root(x):
            return await leaf(x)

        await router.activate(transport)

        with self.assertRaises(ValueError) as ctx:
            await root(1)

        info = trace.get_trace(ctx.exception)
        self.assertEqual(info.raise_frames, ())
        self.assertTrue(all(hop.await_frames == () for hop in info.hops))

    async def test_007_trace_filter_modules_excludes_library_frames(self):
        """trace_filter_modules strips the named modules' frames out of the
        ordinary-await parts of the trace, while keeping user-code frames."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace, trace_filter_modules=['atasks'])

        @atask(namespace=namespace)
        async def leaf(x):
            return await _raise_after_await(x)

        @atask(namespace=namespace)
        async def root(x):
            return await leaf(x)

        await router.activate(transport)

        with self.assertRaises(ValueError) as ctx:
            await root(1)

        info = trace.get_trace(ctx.exception)
        all_frames = info.raise_frames + tuple(f for hop in info.hops for f in hop.await_frames)
        self.assertTrue(any(f.func == '_raise_after_await' for f in all_frames))
        self.assertFalse(any(os.path.abspath(f.file).startswith(ATASKS_PACKAGE_DIR) for f in all_frames))

    async def test_008_await_frames_are_scoped_to_the_current_hop_only(self):
        """await_frames/raise_frames must never reach into a previous hop's own
        execution, or into the atasks library/transport plumbing between two
        hops - even with no trace_filter_modules configured at all. Only the
        ordinary awaits genuinely local to the current hop should appear."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace)

        @atask(namespace=namespace)
        async def leaf(x):
            return await _raise_after_await(x)

        async def _local_helper(x):
            """Ordinary (non-atask) helper sitting between middle's own body and its call to leaf."""
            await asyncio.sleep(0)
            return await leaf(x)

        @atask(namespace=namespace)
        async def middle(x):
            return await _local_helper(x)

        @atask(namespace=namespace)
        async def root(x):
            return await middle(x)

        await router.activate(transport)

        with self.assertRaises(ValueError) as ctx:
            await root(1)

        info = trace.get_trace(ctx.exception)
        root_hop, middle_hop, leaf_hop = info.hops

        # root's body is a single straight-through call - nothing ordinary
        # happens before reaching middle, and root's own caller (the test
        # method, asyncio/unittest internals above it) must not show up here.
        self.assertEqual(middle_hop.await_frames, ())

        # middle -> leaf: exactly middle's own call site into _local_helper -
        # no more (not _local_helper itself, that's leaf_hop.caller_func;
        # not any atasks-library plumbing; not root's frames either).
        self.assertEqual([f.func for f in leaf_hop.await_frames], ['middle'])
        self.assertEqual(leaf_hop.caller_func, '_local_helper')

        # raise_frames: only leaf's and _raise_after_await's own frames - not
        # _call_coro's "await coro(...)" line the traceback actually starts at.
        self.assertEqual([f.func for f in info.raise_frames], ['leaf', '_raise_after_await'])

        for f in leaf_hop.await_frames + info.raise_frames:
            self.assertFalse(os.path.abspath(f.file).startswith(ATASKS_PACKAGE_DIR))

    async def test_009_rendered_trace_follows_actual_call_chronology(self):
        """format_trace must read top-to-bottom in the order things actually
        happened: a hop's own await_frames (how execution got to its call
        site) before that hop's marker line (the call site itself) - and, for
        the root hop specifically, its leaked ambient stack (there being no
        enclosing atask to bound it) called out and placed the same way."""
        namespace = _fresh_namespace()
        router, transport = await self._wire(namespace)

        @atask(namespace=namespace)
        async def leaf(x):
            return await _raise_after_await(x)

        @atask(namespace=namespace)
        async def root(x):
            return await leaf(x)

        await router.activate(transport)

        async def _caller_wrapper(x):
            """Ordinary (non-atask) helper making the actual root call site."""
            return await root(x=x)

        async def _caller_preamble():
            """Ordinary (non-atask) helper one level above the root call site."""
            await asyncio.sleep(0)
            return await _caller_wrapper(1)

        with self.assertRaises(ValueError) as ctx:
            await _caller_preamble()

        info = trace.get_trace(ctx.exception)
        root_hop, leaf_hop = info.hops
        self.assertTrue(root_hop.is_root)
        self.assertEqual(root_hop.caller_func, '_caller_wrapper')
        # _caller_preamble is one level *above* the actual call site
        # (_caller_wrapper) - exactly the kind of frame that belongs in
        # await_frames, not in caller_func itself.
        self.assertTrue(any(f.func == '_caller_preamble' for f in root_hop.await_frames))

        rendered = trace.format_trace(ctx.exception)
        lines = rendered.splitlines()

        def _find(needle, is_marker):
            # a hop's marker line (its own call site) and a plain await_frame
            # line could in general name the same function, so marker lines
            # (carrying a »...« kind tag) and plain frame lines are told apart
            # explicitly, not just matched by substring.
            return next(i for i, line in enumerate(lines) if needle in line and ('»' in line) == is_marker)

        entry_header = _find('entry point', False)
        preamble_frame = _find('_caller_preamble', False)  # await_frame: one level above the root call site
        root_marker = _find('in _caller_wrapper', True)  # root_hop's marker: the root call site itself
        leaf_marker = _find('in root', True)  # leaf_hop's marker: called from root
        raise_line = _find('_raise_after_await', False)

        self.assertLess(entry_header, preamble_frame)
        self.assertLess(preamble_frame, root_marker)
        self.assertLess(root_marker, leaf_marker)
        self.assertLess(leaf_marker, raise_line)


class AMQPTraceTest(TestCase):
    """Trace collection round-tripping through a real AMQP broker.

    Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at
    ATASKS_TEST_AMQP_URL (default amqp://guest:guest@localhost/). Skipped
    (not failed) if no broker is reachable.
    """

    async def asyncSetUp(self):
        """Skip the whole test case if no broker is reachable"""
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)

    async def test_001_trace_survives_a_real_amqp_round_trip(self):
        """The AtaskHop/AtaskTrace/FrameInfo dataclasses must be picklable and
        must round-trip, unaltered, through a real broker - not just look
        correct against the in-process LoopbackTransport."""
        namespace = _fresh_namespace()
        Router(namespace=namespace, hostname='amqp-test-host')
        PickleCodec(namespace=namespace)
        # See test_006_amqp_rpc.py: construct the server transport first and the
        # client transport last, so the client transport ends up as the
        # namespace's outbound transport while the server transport is bound
        # explicitly (and separately) via router.activate() below.
        server_transport = AMQPTransport(namespace=namespace, url=AMQP_URL, prefix=namespace, queue=namespace)
        client_transport = AMQPTransport(namespace=namespace, url=AMQP_URL, prefix=namespace)
        await server_transport.connect()
        await client_transport.connect()

        @atask(namespace=namespace)
        async def leaf(x):
            return await _raise_after_await(x)

        @atask(namespace=namespace)
        async def root(x):
            return await leaf(x)

        router = get_router(namespace)
        await router.activate(server_transport)
        try:
            with self.assertRaises(ValueError) as ctx:
                await root(1)
        finally:
            await teardown_amqp(router, [client_transport, server_transport])

        info = trace.get_trace(ctx.exception)
        self.assertIsNotNone(info)
        self.assertEqual([hop.seq for hop in info.hops], [0, 1])
        self.assertTrue(info.hops[0].task.endswith('.root'))
        self.assertTrue(info.hops[1].task.endswith('.leaf'))
        self.assertTrue(all(hop.host == 'amqp-test-host' for hop in info.hops))
        self.assertTrue(any(f.func == '_raise_after_await' for f in info.raise_frames))
