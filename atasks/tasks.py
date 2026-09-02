"""
AIO Steve Task Jobs
"""

import functools
import logging


logger = logging.getLogger(__name__)


def atask(coro=None, name=None, namespace='default', **options):
    """
    Decorator for the RPC (request/response) task coroutine.

    The decorated function transparently proxies to the worker holding the
    real coroutine over the network: calling it publishes a request and
    ``await``s the correlated reply (correlation_id + reply-to queue), just
    like awaiting the original coroutine directly.

    Works both bare and parameterized::

        @atask
        async def some_task(a):
            ...

        @atask(timeout=30)
        async def some_other_task(a):
            ...

    :param coro: coroutine to be decorated
    :type coro: coroutine
    :param namespace: namespace of the registry
    :type namespace: str
    :param options: additional options. ``timeout`` (float, seconds) sets a default
                    request timeout - if no response arrives in time, the caller gets
                    ``atasks.transport.base.RequestTimeoutError`` instead of hanging
                    forever. Defaults to ``None`` (wait forever), preserving historical
                    behaviour.
    :type options: dict
    :returns: reference coroutine, or (if called with no positional ``coro``
              argument) a decorator to be applied to one
    :rtype: coroutine
    """
    if coro is None:
        return functools.partial(atask, name=name, namespace=namespace, **options)

    name = '%s.%s' % (coro.__module__, coro.__name__) if name is None else name

    from atasks.router import get_router

    logger.debug('atask: %s[%s/%s] %s', coro, name, namespace, options)
    router = get_router(namespace)
    return router.register_atask(name, coro=coro, options=options)


def atask_queue(coro=None, name=None, namespace='default', **options):
    """
    Decorator for a fire-and-forget task-queue coroutine (competing consumers).

    Calling the decorated function publishes an event and returns ``None``
    immediately - it neither waits for, nor receives, a result. Exactly one
    consumer instance among all instances which called
    ``atasks.router.get_router(namespace).activate_queue(name)`` for the same
    name processes each published event - classic AMQP work-queue semantics,
    the right choice for specialized single-purpose consumer services (e.g.
    rating recalculation, notification generation) where an event must be
    handled exactly-once-per-publish (well, at-least-once - see the
    idempotency note in the README), never once per running instance.

    :param coro: coroutine to be decorated, registered as the consumer-side handler
    :type coro: coroutine
    :param namespace: namespace of the registry
    :type namespace: str
    :param options: additional options, forwarded to the registry
    :type options: dict
    :returns: reference coroutine which publishes the event and returns ``None``,
              or (if called with no positional ``coro`` argument) a decorator to
              be applied to one
    :rtype: coroutine
    """
    if coro is None:
        return functools.partial(atask_queue, name=name, namespace=namespace, **options)

    name = '%s.%s' % (coro.__module__, coro.__name__) if name is None else name

    from atasks.router import get_router

    logger.debug('atask_queue: %s[%s/%s] %s', coro, name, namespace, options)
    router = get_router(namespace)
    return router.register_atask_queue(name, coro=coro, options=options)


def atask_broadcast(coro=None, name=None, namespace='default', **options):
    """
    Decorator for a fire-and-forget broadcast (fan-out/subscribe) coroutine.

    Calling the decorated function publishes an event and returns ``None``
    immediately - it neither waits for, nor receives, a result. Every instance
    which called ``atasks.router.get_router(namespace).activate_broadcast(name)``
    for the same name receives and processes its own independent copy of the
    event (as opposed to ``atask_queue``, where instances compete for a single
    delivery). This is the mode used by a fleet of ``ws_gateway`` instances,
    each of which needs every event to filter and relay to its own
    independently-held WebSocket connections.

    :param coro: coroutine to be decorated, registered as the subscriber-side handler
    :type coro: coroutine
    :param namespace: namespace of the registry
    :type namespace: str
    :param options: additional options, forwarded to the registry
    :type options: dict
    :returns: reference coroutine which publishes the event and returns ``None``,
              or (if called with no positional ``coro`` argument) a decorator to
              be applied to one
    :rtype: coroutine
    """
    if coro is None:
        return functools.partial(atask_broadcast, name=name, namespace=namespace, **options)

    name = '%s.%s' % (coro.__module__, coro.__name__) if name is None else name

    from atasks.router import get_router

    logger.debug('atask_broadcast: %s[%s/%s] %s', coro, name, namespace, options)
    router = get_router(namespace)
    return router.register_atask_broadcast(name, coro=coro, options=options)
