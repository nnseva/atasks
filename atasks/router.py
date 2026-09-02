"""
ATasks Router
"""

import logging
import socket
import sys

from atasks import trace
from atasks.codecs import get_codec
from atasks.namespaces import namespaces
from atasks.registry import Manager
from atasks.transport.base import get_transport


logger = logging.getLogger(__name__)


class NoClientTransportRegistered(Exception):
    """No client transport found in a namespace"""
    pass


class NoCodecRegistered(Exception):
    """No codec found in a namespace"""
    pass


class JobNotFound(Exception):
    """No requested atask in a namespace"""
    pass


class TransportError(Exception):
    """Transport error while sending a request"""
    pass


class Router(object):
    """
    Router is a core atasks class which registers asynchronous tasks,
    creates remote reference functions, routes reference calls to the remote coroutines,
    etc.

    It is registered in the namespace and uses codec and transport from it.
    """
    def __init__(
        self,
        namespace='default',
        hostname=None,
        max_trace_depth=1000,
        trace_filter_modules=None,
        collect_await_frames=True,
    ):
        """
        Constructor

        To have ``hostname`` (or the other trace options below) taken into
        account, construct the ``Router`` yourself before the first direct or
        indirect call to :func:`get_router` for this namespace - it will then
        return this instance. :func:`get_router` itself is unchanged and
        keeps auto-creating a default-configured ``Router`` on first use.

        :param namespace: name of the namespace which the router will use to send requests
        :type namespace: str
        :param hostname: host identification recorded in every atask call
                         trace made through this router. Defaults to
                         ``socket.gethostname()`` (a local, non-blocking call -
                         no resolver is involved). Uniqueness across the
                         deployment is the deployer's responsibility.
        :type hostname: str or None
        :param max_trace_depth: maximum number of atask hops (not counting
                                 ordinary ``await`` frames) allowed in a call
                                 chain before :class:`atasks.trace.AtaskStackTooDeep`
                                 is raised - a guard against runaway recursive
                                 or cyclic atask calls
        :type max_trace_depth: int
        :param trace_filter_modules: dotted module name prefixes (e.g.
                                      ``('atasks', 'backoff')``) whose frames
                                      are excluded from the ordinary-``await``
                                      part of the trace. Defaults to no
                                      filtering - every frame, library
                                      internals included, is kept.
        :type trace_filter_modules: list or tuple or None
        :param collect_await_frames: if ``False``, ordinary ``await`` frames
                                      are not collected at all - the trace
                                      then holds only atask hops
        :type collect_await_frames: bool
        """
        logger.info("Creating a router for %s", namespace)
        namespaces.register(namespace, router=self, registry=Manager(namespace, unite=False))
        self.namespace = namespace
        self.server = None
        self.hostname = hostname if hostname is not None else socket.gethostname()
        self.max_trace_depth = max_trace_depth
        self.trace_filter_modules = tuple(trace_filter_modules or ())
        self.collect_await_frames = collect_await_frames

    async def activate(self, server):
        """
        Activate a server transport.

        The server transport will be used to receive requests.

        The server's `register_callback` will be called to register a router callback.
        """
        logger.info('Activating %s for the router of %s', server, self.namespace)
        if self.server == server:
            return
        if self.server:
            await self.server.unregister_callback()
        self.server = server
        if self.server:
            await self.server.register_callback(self._on_request)

    async def deactivate(self):
        """
        Deactivate a server transport.

        The server's `unregister_callback` will be called to unregister
        a router callback.
        """
        logger.info('Deactivating %s', self.server)
        if self.server:
            await self.server.unregister_callback()
        self.server = None

    async def send_request(self, name, *argv, timeout=None, trace_chain=(), **kwargs):
        """
        Send a request.

        Uses codec got from the namespace to encode the request content.

        Uses transport got from the namespace to send an encoded content and receive a result.

        Uses codec got from the namespace to decode the request response.

        :param name: name of the coroutine to be called
        :type name: str
        :param argv: arbitrary positional parameters
        :param timeout: maximum number of seconds to wait for a response. ``None``
                        (default) waits forever, preserving the historical behaviour.
        :type timeout: float or None
        :param trace_chain: atask call chain to send along with the request, as built by
                            :func:`atasks.trace.push_hop`
        :type trace_chain: tuple
        :param kwargs: arbitrary named parameters
        :returns: success flag and job awaiting result, or exception in case of the exception handled
        :raises atasks.transport.base.RequestTimeoutError: if ``timeout`` elapses with no response
        :raises atasks.transport.base.ConnectionLostError: if the broker connection is lost while waiting
        """
        logger.debug('Sending request %s %s %s', name, argv, kwargs)
        client = get_transport(self.namespace)
        if not client:
            raise NoClientTransportRegistered()

        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        content = await codec.encode((argv, kwargs, trace_chain))
        logger.debug('Sending request %s using %s', name, client)
        response = await client.send_request(name, content, timeout=timeout)
        logger.debug('Response for %s returned', name)
        if not response:
            raise TransportError()
        success, result = await codec.decode(response)
        logger.debug('Sending request %s response success = %s content: %s', name, success, result)
        if not success:
            raise result
        return result

    async def send_event(self, name, *argv, trace_chain=(), **kwargs):
        """
        Publish a fire-and-forget task-queue event (see :meth:`register_queue_task`).

        Exactly one competing consumer instance among all currently subscribed via
        :meth:`activate_queue` will process it. No result is returned to the caller -
        the call resolves as soon as the event is handed off to the transport.

        :param name: name of the queue/task
        :type name: str
        :param argv: arbitrary positional parameters
        :param trace_chain: atask call chain to send along with the event, as built by
                            :func:`atasks.trace.push_hop`
        :type trace_chain: tuple
        :param kwargs: arbitrary named parameters
        """
        logger.debug('Sending event %s %s %s', name, argv, kwargs)
        client = get_transport(self.namespace)
        if not client:
            raise NoClientTransportRegistered()

        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        content = await codec.encode((argv, kwargs, trace_chain))
        await client.publish_event(name, content)

    async def send_broadcast(self, name, *argv, trace_chain=(), **kwargs):
        """
        Publish a fan-out event (see :meth:`register_broadcast_task`).

        Every instance currently subscribed via :meth:`activate_broadcast` under the
        same name receives and processes its own copy. No result is returned to the
        caller - the call resolves as soon as the event is handed off to the transport.

        :param name: name of the broadcast topic
        :type name: str
        :param argv: arbitrary positional parameters
        :param trace_chain: atask call chain to send along with the event, as built by
                            :func:`atasks.trace.push_hop`
        :type trace_chain: tuple
        :param kwargs: arbitrary named parameters
        """
        logger.debug('Sending broadcast %s %s %s', name, argv, kwargs)
        client = get_transport(self.namespace)
        if not client:
            raise NoClientTransportRegistered()

        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        content = await codec.encode((argv, kwargs, trace_chain))
        await client.publish_broadcast(name, content)

    async def _on_request(self, name, content):
        """
        Callback receiving a request.

        Uses codec got from the namespace to decode the request content.

        Awaits the job found in the registry.

        Uses codec got from the namespace to encode the request response.

        :param name: name of the request
        :type name: str
        :param content: content of the request
        :type content: bytes
        :returns: encoded response
        :rtype: bytes
        """
        logger.info('Request received %s', name)
        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        argv, kwargs, trace_chain = await codec.decode(content)
        item = namespaces.get(self.namespace).registry.get(name)
        if not item:
            raise JobNotFound(name)

        coro = item.coro
        options = item.options

        logger.debug('Request received %s with %s %s', name, argv, kwargs)
        token = trace.enter(trace_chain)
        try:
            success, result = await self._call_coro(coro, argv, kwargs, options)
        finally:
            trace.leave(token)
        # RPC failures are routed back to the caller as-is (together with the
        # atask trace attached by _call_coro) and are never logged here - the
        # caller decides whether/how to log what it does with the exception.
        logger.debug('Request %s response returning success = %s: %s', name, success, result)
        response = await codec.encode((success, result))
        logger.info('Request %s response returning', name)
        return response

    async def _on_event(self, name, content):
        """
        Callback receiving a competing-consumer task-queue event.

        Unlike :meth:`_on_request`, no response is encoded or returned -
        the underlying transport already knows not to expect one.

        :param name: name of the request
        :type name: str
        :param content: content of the request
        :type content: bytes
        """
        logger.info('Queue event received %s', name)
        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        argv, kwargs, trace_chain = await codec.decode(content)
        item = namespaces.get(self.namespace).registry.get(name)
        if not item:
            raise JobNotFound(name)

        token = trace.enter(trace_chain)
        try:
            success, result = await self._call_coro(item.coro, argv, kwargs, item.options)
        finally:
            trace.leave(token)
        if not success:
            # A queue task has no caller waiting for a response - the failure
            # terminates here, so the full collected trace is logged in full,
            # instead of just the exception's repr.
            logger.error('Queue task %s raised an exception:\n%s', name, trace.format_trace(result))

    async def _on_broadcast(self, name, content):
        """
        Callback receiving a fan-out broadcast event.

        Unlike :meth:`_on_request`, no response is encoded or returned -
        the underlying transport already knows not to expect one.

        :param name: name of the request
        :type name: str
        :param content: content of the request
        :type content: bytes
        """
        logger.info('Broadcast event received %s', name)
        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        argv, kwargs, trace_chain = await codec.decode(content)
        item = namespaces.get(self.namespace).registry.get(name)
        if not item:
            raise JobNotFound(name)

        token = trace.enter(trace_chain)
        try:
            success, result = await self._call_coro(item.coro, argv, kwargs, item.options)
        finally:
            trace.leave(token)
        if not success:
            # A broadcast task has no caller waiting for a response - the failure
            # terminates here, so the full collected trace is logged in full,
            # instead of just the exception's repr.
            logger.error('Broadcast task %s raised an exception:\n%s', name, trace.format_trace(result))

    async def _call_coro(self, coro, argv, kwargs, options):
        """
        Calls coroutine and returns success flag and result or exception

        On failure, attaches the atask call chain collected so far to the
        exception (see :func:`atasks.trace.attach`) before returning it.

        Marks this frame's ``await coro(...)`` line as the current hop's entry
        point for the duration of the call - the boundary
        :func:`atasks.trace.push_hop`/:func:`atasks.trace.attach` use to keep
        ordinary-``await`` frames scoped to this hop's own execution, instead
        of reaching back through the atasks library/transport/event-loop
        plumbing into a previous hop.
        """
        token = trace.ENTRY_FRAME.set(sys._getframe())
        try:
            result = await coro(*argv, **kwargs)
        except Exception as ex:
            trace.attach(ex, self)
            return False, ex
        finally:
            trace.ENTRY_FRAME.reset(token)

        return True, result

    def register_atask(self, name, coro=None, options={}):
        """
        Register atask in the registry.

        Returns a network reference stub used to await atask remotely

        :param name: name of the atask
        :type name: str
        :param coro: coroutine to be registered as atask
        :type coro: awaitable
        :param options: registering additional options passed from atask decorator
        :type options: dict
        :returns: network reference stub to await atask remotely
        :rtype: awaitable
        """
        namespace = self.namespace

        namespaces.get(namespace).registry.register(name, coro=coro, options=options)
        default_timeout = options.get('timeout')

        async def aioref(*argv, **kwargs):
            chain = trace.push_hop(self, name, namespace, 'rpc')
            result = await get_router(namespace).send_request(
                name, *argv, timeout=default_timeout, trace_chain=chain, **kwargs
            )
            return result

        aioref.__qualname__ = 'ref[%s/%s]' % (name, namespace)
        logger.info('Registered %s', aioref)
        return aioref

    def register_atask_queue(self, name, coro=None, options={}):
        """
        Register a fire-and-forget, competing-consumers task-queue task in the registry.

        Returns a network reference stub used to publish the event remotely. Calling
        the stub publishes the event and returns ``None`` immediately - it does not wait
        for, or receive, a result.

        :param name: name of the task-queue task
        :type name: str
        :param coro: coroutine to be registered as the consumer-side handler
        :type coro: awaitable
        :param options: registering additional options passed from the atask_queue decorator
        :type options: dict
        :returns: network reference stub used to publish the event remotely
        :rtype: awaitable
        """
        namespace = self.namespace

        namespaces.get(namespace).registry.register(name, coro=coro, options=options)

        async def aioref(*argv, **kwargs):
            chain = trace.push_hop(self, name, namespace, 'queue')
            await get_router(namespace).send_event(name, *argv, trace_chain=chain, **kwargs)

        aioref.__qualname__ = 'queue[%s/%s]' % (name, namespace)
        logger.info('Registered queue task %s', aioref)
        return aioref

    def register_atask_broadcast(self, name, coro=None, options={}):
        """
        Register a fire-and-forget, fan-out broadcast task in the registry.

        Returns a network reference stub used to publish the event remotely. Calling
        the stub publishes the event and returns ``None`` immediately - it does not wait
        for, or receive, a result.

        :param name: name of the broadcast topic
        :type name: str
        :param coro: coroutine to be registered as the subscriber-side handler
        :type coro: awaitable
        :param options: registering additional options passed from the atask_broadcast decorator
        :type options: dict
        :returns: network reference stub used to publish the event remotely
        :rtype: awaitable
        """
        namespace = self.namespace

        namespaces.get(namespace).registry.register(name, coro=coro, options=options)

        async def aioref(*argv, **kwargs):
            chain = trace.push_hop(self, name, namespace, 'broadcast')
            await get_router(namespace).send_broadcast(name, *argv, trace_chain=chain, **kwargs)

        aioref.__qualname__ = 'broadcast[%s/%s]' % (name, namespace)
        logger.info('Registered broadcast task %s', aioref)
        return aioref

    async def activate_queue(self, name, transport=None):
        """
        Start consuming the named task-queue as one of possibly several competing
        consumer instances.

        :param name: name of the task-queue task, as passed to ``atask_queue``
        :type name: str
        :param transport: transport to consume from, defaults to this namespace's
                          registered transport
        :type transport: atasks.transport.base.Transport or None
        """
        transport = transport or get_transport(self.namespace)
        if not transport:
            raise NoClientTransportRegistered()

        async def _callback(content):
            await self._on_event(name, content)

        await transport.register_event_callback(name, _callback)

    async def deactivate_queue(self, name, transport=None):
        """
        Stop consuming the named task-queue previously activated with :meth:`activate_queue`.

        :param name: name of the task-queue task
        :type name: str
        :param transport: transport to stop consuming from, defaults to this namespace's
                          registered transport
        :type transport: atasks.transport.base.Transport or None
        """
        transport = transport or get_transport(self.namespace)
        if transport:
            await transport.unregister_event_callback(name)

    async def activate_broadcast(self, name, transport=None):
        """
        Subscribe to the named fan-out broadcast topic.

        This instance receives its own independent copy of every event published
        under ``name``, regardless of how many other instances are also subscribed.

        :param name: name of the broadcast topic, as passed to ``atask_broadcast``
        :type name: str
        :param transport: transport to subscribe on, defaults to this namespace's
                          registered transport
        :type transport: atasks.transport.base.Transport or None
        """
        transport = transport or get_transport(self.namespace)
        if not transport:
            raise NoClientTransportRegistered()

        async def _callback(content):
            await self._on_broadcast(name, content)

        await transport.register_broadcast_callback(name, _callback)

    async def deactivate_broadcast(self, name, transport=None):
        """
        Unsubscribe from the named broadcast topic previously activated with
        :meth:`activate_broadcast`.

        :param name: name of the broadcast topic
        :type name: str
        :param transport: transport to unsubscribe from, defaults to this namespace's
                          registered transport
        :type transport: atasks.transport.base.Transport or None
        """
        transport = transport or get_transport(self.namespace)
        if transport:
            await transport.unregister_broadcast_callback(name)


def get_router(namespace='default'):
    """
    Get or create a router for the namespace.

    :param namespace: name of the namespace which the router will use to send requests
    :type namespace: str
    """
    ns = namespaces.get(namespace)
    router = getattr(ns, 'router', None)
    if not router:
        router = Router(namespace)
    return router
