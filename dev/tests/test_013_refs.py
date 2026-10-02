"""
Lightweight atask reference tests (``atasks.refs``)

See ``LIGHT-AIOREF.md`` for the rationale.
"""
import asyncio
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.codecs import PickleCodec
from atasks.refs import atask_bref, atask_qref, ataskref
from atasks.router import get_router
from atasks.tasks import atask, atask_broadcast, atask_queue
from atasks.transport.base import (
    LoopbackTransport,
    RequestTimeoutError,
    UnknownRequestName,
)


def _fresh_namespace():
    return 'test-refs-%s' % uuid.uuid4().hex


class AtaskRefTest(TestCase):
    """``ataskref`` - lightweight reference to a remote RPC ``@atask``"""

    async def test_calls_remote_atask_by_name_alone(self):
        """``ataskref[name]`` calls an already-registered ``@atask`` and returns its result,
        with the caller never referring to the Python function that implements it."""
        namespace = _fresh_namespace()
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        @atask(namespace=namespace, name='remote_add')
        async def remote_add(a, b):
            return a + b

        router = get_router(namespace)
        await router.activate(transport)

        result = await ataskref(namespace=namespace)['remote_add'](2, 3)
        self.assertEqual(result, 5)

    async def test_bound_ref_is_reusable_across_names(self):
        """``ataskref(namespace=...)`` returns a factory that can be indexed more than once."""
        namespace = _fresh_namespace()
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        @atask(namespace=namespace)
        async def double(a):
            return a * 2

        @atask(namespace=namespace)
        async def triple(a):
            return a * 3

        router = get_router(namespace)
        await router.activate(transport)

        ref = ataskref(namespace=namespace)
        # Default @atask names are "<module>.<funcname>" - build them the same way.
        self.assertEqual(await ref['%s.double' % __name__](21), 42)
        self.assertEqual(await ref['%s.triple' % __name__](21), 63)

    async def test_does_not_register_anything_locally(self):
        """Unlike ``@atask``, indexing ``ataskref`` after ``activate()`` never raises
        ``LateRegistration`` - it registers nothing, so there is nothing to be late about.
        Calling it for a name nobody serves fails with the transport's own
        "no such callback" error instead."""
        namespace = _fresh_namespace()
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()
        router = get_router(namespace)
        await router.activate(transport)

        ref = ataskref(namespace=namespace)['nobody.serves.this']
        with self.assertRaises(UnknownRequestName):
            await ref()

    async def test_timeout_is_caller_side_and_independent_of_the_callee(self):
        """``ataskref(timeout=...)`` bounds how long *this* caller waits, regardless of
        whether the callee itself was registered with a ``@atask(timeout=...)``."""
        namespace = _fresh_namespace()
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        @atask(namespace=namespace, name='slow')
        async def slow():
            await asyncio.sleep(0.5)
            return 'too late'

        router = get_router(namespace)
        await router.activate(transport)

        with self.assertRaises(RequestTimeoutError):
            await ataskref(namespace=namespace, timeout=0.05)['slow']()


class AtaskQRefTest(TestCase):
    """``atask_qref`` - lightweight reference to a remote ``@atask_queue``"""

    async def test_publishes_and_returns_none(self):
        namespace = _fresh_namespace()
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        processed = []

        @atask_queue(namespace=namespace, name='record')
        async def record(value):
            processed.append(value)

        router = get_router(namespace)
        await router.activate(transport)

        result = await atask_qref(namespace=namespace)['record']('hello')
        self.assertIsNone(result)
        self.assertEqual(processed, ['hello'])


class AtaskBRefTest(TestCase):
    """``atask_bref`` - lightweight reference to a remote ``@atask_broadcast``"""

    async def test_publishes_and_returns_none(self):
        namespace = _fresh_namespace()
        PickleCodec(namespace=namespace)
        transport = LoopbackTransport(namespace=namespace)
        await transport.connect()

        received = []

        @atask_broadcast(namespace=namespace, name='relay')
        async def relay(payload):
            received.append(payload)

        router = get_router(namespace)
        await router.activate(transport)

        result = await atask_bref(namespace=namespace)['relay']('event')
        self.assertIsNone(result)
        self.assertEqual(received, ['event'])
