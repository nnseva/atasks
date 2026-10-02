"""
Lightweight atask references

``ataskref``/``atask_qref``/``atask_bref`` call a remote ``@atask``/
``@atask_queue``/``@atask_broadcast`` by name alone - no import of the module
that declares it, and no local registration of any kind. They are pure
callers: unlike the matching decorator, using one of these never makes this
instance serve anything, so it has no bearing on what ``Router.activate()``
subscribes to.

See ``LIGHT-AIOREF.md`` for the rationale.
"""

import logging


logger = logging.getLogger(__name__)


class _Ref(object):
    """
    Base class for lightweight, registration-free atask references.

    An instance is a reusable, preconfigured factory bound to a namespace
    (and, for :class:`_AtaskRef`, a caller-side timeout) - it registers
    nothing anywhere and doesn't check that ``name`` exists until the
    reference built from it is actually awaited.

    Calling an instance - ``ref(namespace=..., **options)`` - returns a new
    instance bound to that namespace/options; indexing one - ``ref[name]`` -
    builds and returns the coroutine which actually calls the remote atask
    named ``name``, exactly as the coroutine returned by the matching
    ``@atask``/``@atask_queue``/``@atask_broadcast`` decorator would.
    """

    def __init__(self, namespace='default', **options):
        """
        :param namespace: namespace of the remote atask to call
        :type namespace: str
        :param options: caller-side options - see the subclass for which, if any, apply
        :type options: dict
        """
        self.namespace = namespace
        self.options = options

    def __call__(self, namespace='default', **options):
        """Return a new instance of the same kind, bound to ``namespace``/``options``."""
        return type(self)(namespace=namespace, **options)

    def __getitem__(self, name):
        """Build and return the reference coroutine for the atask named ``name``."""
        raise NotImplementedError


class _AtaskRef(_Ref):
    """Lightweight reference to a remote RPC ``@atask`` - see :data:`ataskref`."""

    def __getitem__(self, name):
        namespace = self.namespace
        timeout = self.options.get('timeout')

        from atasks import trace
        from atasks.router import get_router

        async def ref(*argv, **kwargs):
            router = get_router(namespace)
            chain = trace.push_hop(router, name, namespace, 'rpc')
            return await router.send_request(name, *argv, timeout=timeout, trace_chain=chain, **kwargs)

        ref.__qualname__ = 'ataskref[%s/%s]' % (name, namespace)
        return ref


class _AtaskQRef(_Ref):
    """Lightweight reference to a remote task-queue ``@atask_queue`` - see :data:`atask_qref`."""

    def __getitem__(self, name):
        namespace = self.namespace

        from atasks import trace
        from atasks.router import get_router

        async def ref(*argv, **kwargs):
            router = get_router(namespace)
            chain = trace.push_hop(router, name, namespace, 'queue')
            await router.send_event(name, *argv, trace_chain=chain, **kwargs)

        ref.__qualname__ = 'atask_qref[%s/%s]' % (name, namespace)
        return ref


class _AtaskBRef(_Ref):
    """Lightweight reference to a remote broadcast ``@atask_broadcast`` - see :data:`atask_bref`."""

    def __getitem__(self, name):
        namespace = self.namespace

        from atasks import trace
        from atasks.router import get_router

        async def ref(*argv, **kwargs):
            router = get_router(namespace)
            chain = trace.push_hop(router, name, namespace, 'broadcast')
            await router.send_broadcast(name, *argv, trace_chain=chain, **kwargs)

        ref.__qualname__ = 'atask_bref[%s/%s]' % (name, namespace)
        return ref


#: Lightweight, registration-free reference to a remote RPC atask (``@atask``).
#:
#: ``await ataskref[name](42)`` calls the atask named ``name`` in the default
#: namespace and waits for the result, exactly like the coroutine ``@atask``
#: returns - without importing the module that declares it, and without
#: registering ``name`` locally (so this instance never ends up serving it).
#:
#: Use ``ataskref(namespace=..., timeout=...)[name]`` to bind a different
#: namespace and/or a caller-side response timeout. That ``timeout`` is
#: independent of any default the callee's own ``@atask(timeout=...)`` may
#: set - it is how long *this* caller is willing to wait, not how long the
#: worker thinks is reasonable.
ataskref = _AtaskRef()

#: Lightweight, registration-free reference to a remote task-queue atask
#: (``@atask_queue``). ``await atask_qref[name](42)`` publishes the event and
#: returns ``None`` immediately, like the coroutine ``@atask_queue`` returns.
#: ``atask_qref(namespace=...)[name]`` binds a different namespace.
atask_qref = _AtaskQRef()

#: Lightweight, registration-free reference to a remote broadcast atask
#: (``@atask_broadcast``). ``await atask_bref[name](42)`` publishes the event
#: and returns ``None`` immediately, like the coroutine ``@atask_broadcast``
#: returns. ``atask_bref(namespace=...)[name]`` binds a different namespace.
atask_bref = _AtaskBRef()
