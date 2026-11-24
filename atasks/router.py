"""
ATasks Router
"""

import asyncio
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


class LateRegistration(Exception):
    """
    An ``@atask``/``@atask_queue``/``@atask_broadcast`` was registered after
    :meth:`Router.activate` had already been called for this namespace.

    ``activate()`` subscribes, once, to exactly the names known to it at that
    moment - a name registered afterwards would never get its queue bound,
    and (with the previous mask-bound single RPC queue) could silently steal
    messages meant for another instance instead. Registration order is
    therefore enforced instead of silently tolerated: load every module that
    registers atasks this instance should serve, then call ``activate()`` -
    never the other way round.
    """
    pass


class ServiceShuttingDown(Exception):
    """
    A remote call was cancelled because the service shut down before it finished.

    Sent back to the caller in place of a result.
    """
    def __init__(self, name):
        """Identify the cancelled remote call by name."""
        super().__init__(name)
        self.name = name


class ResultNotSerializable(Exception):
    """
    The result of a remote call could not be encoded, so it was not sent.

    Sent back to the caller in place of the unserializable result.
    """
    def __init__(self, name, result_type):
        """Identify the remote call and the type that could not be serialized."""
        super().__init__(name, result_type)
        self.name = name
        self.result_type = result_type

    def __str__(self):
        return f'{self.name}: result of type {self.result_type} cannot be serialized'


CANCEL_GRACE_SECONDS = 2


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
        namespaces.register(
            namespace,
            router=self,
            # Three independent registries, one per atask kind, rather than a
            # single shared one keyed only by name: `activate()` needs to
            # enumerate "every currently registered RPC name" (etc.)
            # separately per kind, to subscribe each kind the right way -
            # see `activate()`/`_activate_request()`/`_activate_queue()`/
            # `_activate_broadcast()` below.
            rpc_registry=Manager(namespace, unite=False),
            queue_registry=Manager(namespace, unite=False),
            broadcast_registry=Manager(namespace, unite=False),
        )
        self.namespace = namespace
        self.server = None
        self._activated = {'rpc': [], 'queue': [], 'broadcast': []}
        # Accepting is switched off synchronously, as the first step of
        # deactivate(), so no handler task can be created after it.
        self._accepting = False
        # Strong references to every handler task until it finishes.
        self._active_tasks = set()
        self.hostname = hostname if hostname is not None else socket.gethostname()
        self.max_trace_depth = max_trace_depth
        self.trace_filter_modules = tuple(trace_filter_modules or ())
        self.collect_await_frames = collect_await_frames

    async def activate(self, server):
        """
        Activate a server transport.

        Subscribes, once, to every atask/atask_queue/atask_broadcast name
        currently registered in this namespace - the transport ends up with
        one subscription per registered name (see
        :meth:`atasks.transport.base.Transport._register_request_callback`
        and its event/broadcast counterparts), never a single catch-all
        subscription for every possible name.

        Registering a new atask/atask_queue/atask_broadcast after this call
        is no longer supported - see :class:`LateRegistration`. Load every
        module that registers atasks this instance should serve *before*
        calling ``activate()``.

        :param server: transport to receive requests/events/broadcasts from
        :type server: atasks.transport.base.Transport or None
        """
        logger.info('Activating %s for the router of %s', server, self.namespace)
        if self.server == server:
            return
        if self.server:
            await self.deactivate()
        self.server = server
        if not self.server:
            return
        self._accepting = True

        ns = namespaces.get(self.namespace)
        names = {
            'rpc': ns.rpc_registry.names(),
            'queue': ns.queue_registry.names(),
            'broadcast': ns.broadcast_registry.names(),
        }
        for name in names['rpc']:
            await self._activate_request(name)
        for name in names['queue']:
            await self._activate_queue(name)
        for name in names['broadcast']:
            await self._activate_broadcast(name)
        # Recorded so that `deactivate()` unregisters exactly what was
        # activated here, regardless of whatever the registries hold by then.
        self._activated = names

    async def deactivate(self):
        """
        Deactivate the current server transport.

        Unregisters exactly the names activated by the last :meth:`activate`
        call (rather than re-reading the registries, which should - but,
        strictly, are not guaranteed to - still hold the same names).
        """
        logger.info('Deactivating %s', self.server)
        if not self.server:
            self._accepting = False
            return
        for name in self._activated['rpc']:
            await self._deactivate_request(name)
        for name in self._activated['queue']:
            await self._deactivate_queue(name)
        for name in self._activated['broadcast']:
            await self._deactivate_broadcast(name)
        self._activated = {'rpc': [], 'queue': [], 'broadcast': []}
        self._accepting = False
        self.server = None

    async def _activate_request(self, name):
        """Protected: subscribe the current server to RPC requests for one name."""
        def _callback(content, reply):
            return self._on_request(name, content, reply)
        await self.server._register_request_callback(name, _callback)

    async def _deactivate_request(self, name):
        """Protected: undo :meth:`_activate_request` for one name; failures are logged, not raised."""
        try:
            await self.server._unregister_request_callback(name)
        except Exception:
            logger.exception('Could not unregister request %s', name)

    async def _activate_queue(self, name):
        """Protected: subscribe the current server to a task-queue's events for one name."""
        def _callback(content):
            return self._on_event(name, content)
        await self.server._register_event_callback(name, _callback)

    async def _deactivate_queue(self, name):
        """Protected: undo :meth:`_activate_queue` for one name; failures are logged, not raised."""
        try:
            await self.server._unregister_event_callback(name)
        except Exception:
            logger.exception('Could not unregister event %s', name)

    async def _activate_broadcast(self, name):
        """Protected: subscribe the current server to a broadcast topic for one name."""
        def _callback(content):
            return self._on_broadcast(name, content)
        await self.server._register_broadcast_callback(name, _callback)

    async def _deactivate_broadcast(self, name):
        """Protected: undo :meth:`_activate_broadcast` for one name; failures are logged, not raised."""
        try:
            await self.server._unregister_broadcast_callback(name)
        except Exception:
            logger.exception('Could not unregister broadcast %s', name)

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
        Publish a fire-and-forget task-queue event (see :meth:`register_atask_queue`).

        Exactly one competing consumer instance among all currently subscribed via
        :meth:`activate` will process it. No result is returned to the caller -
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

        Every instance currently subscribed via :meth:`activate` under the
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

    def _spawn(self, label, coro):
        """
        Protected: schedule a handler coroutine as a tracked task.

        The router holds a strong reference to every task until it finishes,
        so :meth:`wait_idle` and :meth:`cancel_active` can see all of them.
        """
        task = asyncio.get_running_loop().create_task(coro, name=label)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    def _on_request(self, name, content, reply):
        """
        Callback receiving a request.

        Synchronous on purpose: the acceptance check and the creation of the
        handler task happen in one step, so a request either gets a task that
        :meth:`wait_idle` will see, or is refused - there is no window in
        between where a shutdown could miss it.

        Name lookup errors (no codec, unknown name) are raised here, before a
        task exists, and propagate to the transport as before.

        :param name: name of the request
        :type name: str
        :param content: content of the request
        :type content: bytes
        :param reply: coroutine function delivering the encoded response back
                      to the caller
        :type reply: callable
        :returns: task handling the request, or ``None`` if the router no
                  longer accepts requests (the transport must then return the
                  message to the queue)
        :rtype: asyncio.Task or None
        """
        if not self._accepting:
            logger.info('Request %s refused: service is shutting down', name)
            return None
        logger.info('Request received %s', name)
        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()
        item = namespaces.get(self.namespace).rpc_registry.get(name)
        if not item:
            raise JobNotFound(name)
        return self._spawn(f'request {self.namespace}.{name}', self._run_request(name, content, reply, codec, item))

    async def _run_request(self, name, content, reply, codec, item):
        """
        Protected: the body of a request task - decode, call, encode, reply.

        RPC failures are routed back to the caller as-is (together with the
        atask trace attached by _call_coro) and are never logged here - the
        caller decides whether/how to log what it does with the exception.

        If the task is cancelled (shutdown timeout), the caller still gets a
        :class:`ServiceShuttingDown` answer instead of waiting for its timeout.
        """
        try:
            argv, kwargs, trace_chain = await codec.decode(content)
            logger.debug('Request received %s with %s %s', name, argv, kwargs)
            token = trace.enter(trace_chain)
            try:
                success, result = await self._call_coro(item.coro, argv, kwargs, item.options)
            finally:
                trace.leave(token)
            logger.debug('Request %s response returning success = %s: %s', name, success, result)
            response = await codec.encode((success, result))
            if response is None:
                response = await codec.encode((False, ResultNotSerializable(name, type(result).__name__)))
            await reply(response)
            logger.info('Request %s response returning', name)
        except asyncio.CancelledError:
            try:
                await reply(await codec.encode((False, ServiceShuttingDown(name))))
            except Exception:
                logger.exception('Could not report the cancellation of request %s', name)
            raise
        except Exception:
            logger.exception('Request %s could not be processed', name)

    def _on_event(self, name, content):
        """
        Callback receiving a competing-consumer task-queue event.

        Like :meth:`_on_request`, but no response is sent back.

        :param name: name of the event
        :type name: str
        :param content: content of the event
        :type content: bytes
        :returns: task handling the event, or ``None`` if refused
        :rtype: asyncio.Task or None
        """
        return self._dispatch_without_reply('Queue', namespaces.get(self.namespace).queue_registry, name, content)

    def _on_broadcast(self, name, content):
        """
        Callback receiving a fan-out broadcast event.

        Like :meth:`_on_request`, but no response is sent back.

        :param name: name of the broadcast
        :type name: str
        :param content: content of the broadcast
        :type content: bytes
        :returns: task handling the broadcast, or ``None`` if refused
        :rtype: asyncio.Task or None
        """
        return self._dispatch_without_reply('Broadcast', namespaces.get(self.namespace).broadcast_registry, name, content)

    def _dispatch_without_reply(self, kind, registry, name, content):
        """Protected: shared acceptance and task creation for events and broadcasts."""
        if not self._accepting:
            logger.info('%s event %s refused: service is shutting down', kind, name)
            return None
        logger.info('%s event received %s', kind, name)
        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()
        item = registry.get(name)
        if not item:
            raise JobNotFound(name)
        return self._spawn(f'{kind.lower()} {self.namespace}.{name}', self._run_without_reply(kind, name, item, content, codec))

    async def _run_without_reply(self, kind, name, item, content, codec):
        """
        Protected: the body of an event/broadcast task - decode and call.

        A failure terminates here, so the full collected trace is logged in
        full, instead of just the exception's repr.
        """
        try:
            argv, kwargs, trace_chain = await codec.decode(content)
            token = trace.enter(trace_chain)
            try:
                success, result = await self._call_coro(item.coro, argv, kwargs, item.options)
            finally:
                trace.leave(token)
            if not success:
                logger.error('%s task %s raised an exception:\n%s', kind, name, trace.format_trace(result))
        except Exception:
            logger.exception('%s task %s could not be processed', kind, name)

    async def wait_idle(self, timeout):
        """
        Wait until all accepted handler tasks finish, or the timeout expires.

        Call only after :meth:`deactivate`, so that no new tasks can appear.

        :param timeout: maximum number of seconds to wait
        :type timeout: float
        :returns: ``True`` if every active task finished, ``False`` on timeout
        :rtype: bool
        """
        pending = set(self._active_tasks)
        if not pending:
            return True
        logger.info('Waiting up to %s s for %d active task(s) of %s to finish', timeout, len(pending), self.namespace)
        _, not_done = await asyncio.wait(pending, timeout=timeout)
        return not not_done

    async def cancel_active(self, grace=CANCEL_GRACE_SECONDS):
        """
        Cancel every handler task still active, and let the cancellations settle.

        Each cancelled task reports :class:`ServiceShuttingDown` to its caller
        before finishing. Call before the transport is disconnected, so that
        those replies can still be published.

        :param grace: maximum number of seconds to wait for the tasks to finish
                      after cancelling them
        :type grace: float
        :returns: number of tasks that did not finish within ``grace``
        :rtype: int
        """
        pending = set(self._active_tasks)
        for task in pending:
            logger.warning('Cancelling %s: service is shutting down', task.get_name())
            task.cancel()
        if not pending:
            return 0
        _, not_done = await asyncio.wait(pending, timeout=grace)
        return len(not_done)

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
        :raises LateRegistration: if :meth:`activate` was already called for this namespace
        """
        if self.server is not None:
            raise LateRegistration(name)

        namespace = self.namespace

        namespaces.get(namespace).rpc_registry.register(name, coro=coro, options=options)
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
        :raises LateRegistration: if :meth:`activate` was already called for this namespace
        """
        if self.server is not None:
            raise LateRegistration(name)

        namespace = self.namespace

        namespaces.get(namespace).queue_registry.register(name, coro=coro, options=options)

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
        :raises LateRegistration: if :meth:`activate` was already called for this namespace
        """
        if self.server is not None:
            raise LateRegistration(name)

        namespace = self.namespace

        namespaces.get(namespace).broadcast_registry.register(name, coro=coro, options=options)

        async def aioref(*argv, **kwargs):
            chain = trace.push_hop(self, name, namespace, 'broadcast')
            await get_router(namespace).send_broadcast(name, *argv, trace_chain=chain, **kwargs)

        aioref.__qualname__ = 'broadcast[%s/%s]' % (name, namespace)
        logger.info('Registered broadcast task %s', aioref)
        return aioref


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
