"""
ATasks Base Transport module
"""

import asyncio
import logging

from atasks.namespaces import namespaces


logger = logging.getLogger(__name__)


class RequestTimeoutError(TimeoutError):
    """
    Raised by the RPC (``@atask``) call path when no response is received
    within the configured timeout.

    Subclasses the builtin ``TimeoutError`` (the same class as
    ``asyncio.TimeoutError`` since Python 3.11), so callers can catch it
    either specifically or generically - e.g. to stack
    ``backoff.on_exception(backoff.expo, TimeoutError)`` on top of an
    ``@atask``-wrapped call.

    Note that a crashed worker and a slow worker look identical from the
    caller's point of view: AMQP gives no signal that a consumer died mid
    task, so both cases surface here once the timeout elapses.
    """


class UnknownRequestName(Exception):
    """
    Raised by :class:`LoopbackTransport` when :meth:`Transport.send_request` is
    called for a name with no request callback currently registered for it
    (i.e. no local ``@atask`` handles it).

    This is deliberately a fast, explicit failure rather than an attempt to
    imitate a real broker's silent "message unroutable, drop it" behaviour
    (which would mean hanging until ``timeout`` or forever) - see the
    ``LoopbackTransport`` class docstring. Other transports are free to behave
    differently for the same situation; nothing in the base ``Transport``
    contract requires this exception specifically.
    """


class ConnectionLostError(ConnectionError):
    """
    Raised for every RPC (``@atask``) request currently in flight when the
    underlying transport connection to the broker is lost (broker restart,
    network partition, ...).

    Subclasses the builtin ``ConnectionError`` so callers can catch it either
    specifically or generically - e.g.
    ``backoff.on_exception(backoff.expo, ConnectionError)``.

    The transport itself keeps trying to reconnect (with backoff) in the
    background; this exception exists so an in-flight caller is told
    immediately instead of silently hanging until a possibly-much-later
    reconnect and/or the request timeout.
    """


class Transport(object):
    """
    Transport base class
    """
    def __init__(self, namespace='default'):
        """
        Create a transport

        :param namespace: namespace where the transport should be registered to work for
        :type namespace: str
        """
        logger.info("Creating a transport %s in %s", self, namespace)
        self.namespace = namespace

        namespaces.register(namespace, transport=self)

    async def connect(self):
        """
        Connect to the transport backend if necessary.

        May await external events.

        After await this coroutine the transport should be ready
        to send messages and register a callback.
        """
        raise NotImplementedError()

    async def disconnect(self):
        """
        Disconnect from the transport backend if necessary.

        May await external events.

        After await this coroutine the transport should be ready
        to be removed from the memory.
        """
        raise NotImplementedError()

    def is_connected(self):
        """
        Whether this transport currently holds a live connection to its backend.

        :returns: True if connected, False otherwise
        :rtype: bool
        """
        raise NotImplementedError()

    async def send_request(self, name, content, timeout=None):
        """
        Send a request to a service and await its response (RPC pattern).

        :param name: target name to be sent
        :type name: str
        :param content: request to be sent
        :type content: bytes
        :param timeout: maximum number of seconds to wait for a response. ``None``
                        (default) waits forever. Should raise ``RequestTimeoutError``
                        if the timeout elapses before a response arrives.
        :type timeout: float or None
        :returns: response to the request
        :rtype: bytes
        :raises RequestTimeoutError: if no response arrives within ``timeout`` seconds
        :raises ConnectionLostError: if the connection to the broker is lost while waiting
        """
        raise NotImplementedError()

    async def publish_event(self, name, content):
        """
        Publish a fire-and-forget task-queue event (competing consumers pattern).

        Exactly one instance among all which registered a callback for the same
        ``name`` via :meth:`_register_event_callback` will process the event -
        classic AMQP work-queue semantics. No response is expected or returned.

        :param name: name of the queue/task the event belongs to
        :type name: str
        :param content: event payload to be sent
        :type content: bytes
        """
        raise NotImplementedError()

    async def publish_broadcast(self, name, content):
        """
        Publish a fan-out (broadcast/subscribe) event.

        Every instance currently subscribed via
        :meth:`_register_broadcast_callback` under the same ``name`` receives
        its own independent copy of the event - as opposed to
        :meth:`publish_event`, where instances compete for a single delivery.
        No response is expected or returned.

        :param name: name of the broadcast topic
        :type name: str
        :param content: event payload to be sent
        :type content: bytes
        """
        raise NotImplementedError()

    # -- Protected per-name registration methods --------------------------
    #
    # The four pairs below are not meant to be called directly by library
    # users - only ``Router.activate()``/``deactivate()`` calls them, once
    # per name known to it at activation time (see the ``Router`` docstrings
    # and the architecture plan for why registration after ``activate()`` is
    # no longer supported). They share one shape across all three atasks
    # patterns: register/unregister a single name's worth of subscription,
    # given a callback that receives the raw message content (bytes). The
    # request pair additionally expects the callback to *return* the encoded
    # response - the other two are pure fire-and-forget and return nothing.

    async def _register_request_callback(self, name, callback):
        """
        Start receiving RPC (``@atask``) requests for one registered name.

        :param name: name of the atask to receive requests for
        :type name: str
        :param callback: coroutine called with the raw request content (bytes);
                        must return the raw encoded response (bytes)
        :type callback: awaitable(content: bytes) -> bytes
        """
        raise NotImplementedError()

    async def _unregister_request_callback(self, name):
        """
        Stop receiving RPC requests for a name registered via
        :meth:`_register_request_callback`.

        :param name: name of the atask to stop receiving requests for
        :type name: str
        """
        raise NotImplementedError()

    async def _register_event_callback(self, name, callback):
        """
        Register this instance as one of possibly several competing consumers
        for the named task-queue.

        Every message published via :meth:`publish_event` under the same
        ``name`` is delivered to exactly one currently-registered instance,
        never to more than one.

        :param name: name of the queue/task to consume
        :type name: str
        :param callback: coroutine called with the raw event content (bytes)
                        for every delivered message
        :type callback: awaitable(content: bytes)
        """
        raise NotImplementedError()

    async def _unregister_event_callback(self, name):
        """
        Stop consuming the named task-queue registered via
        :meth:`_register_event_callback`.

        :param name: name of the queue/task to stop consuming
        :type name: str
        """
        raise NotImplementedError()

    async def _register_broadcast_callback(self, name, callback):
        """
        Subscribe this instance to the named fan-out broadcast topic.

        Implementations should give this instance its own exclusive,
        auto-cleaned-up subscription (e.g. an exclusive auto-delete queue
        bound to a shared exchange), so every subscribed instance gets a full
        copy of the stream published under ``name``.

        :param name: name of the broadcast topic to subscribe to
        :type name: str
        :param callback: coroutine called with the raw event content (bytes)
                        for every delivered message
        :type callback: awaitable(content: bytes)
        """
        raise NotImplementedError()

    async def _unregister_broadcast_callback(self, name):
        """
        Stop and clean up the subscription registered via
        :meth:`_register_broadcast_callback`.

        :param name: name of the broadcast topic to unsubscribe from
        :type name: str
        """
        raise NotImplementedError()


class LoopbackTransport(Transport):
    """
    Loopback transport which requests own callback with bytes sent to him

    Runs entirely in-process, so "competing consumers" (task-queue mode) and
    "fan-out" (broadcast mode) are modeled by which callbacks are registered on
    *this* instance: :meth:`_register_event_callback` keeps only the most
    recently registered callback per name (one deliverable per event, like a
    real work queue), while :meth:`_register_broadcast_callback` keeps every
    registered callback per name and calls all of them (like a real fan-out).
    Useful for fast unit tests; it does not exercise real network/crash
    behaviour - use ``AMQPTransport`` against a real broker for that.

    This is deliberately the simplest possible implementation of the
    ``Transport`` contract, not a faithful in-process stand-in for
    ``AMQPTransport``'s broker semantics - in particular, :meth:`send_request`
    fails fast with :class:`UnknownRequestName` for a name with no registered
    callback, rather than imitating a real broker's silent
    "message unroutable, drop it" behaviour (which would mean hanging until
    ``timeout`` or forever). Other ``Transport`` implementations are free to
    behave differently in that situation.
    """

    def __init__(self, namespace='default'):
        """Overriden from the base class to add request/event/broadcast bookkeeping."""
        super().__init__(namespace)
        self._connected = False
        self._request_callbacks = {}
        self._event_callbacks = {}
        self._broadcast_callbacks = {}

    async def connect(self):
        """
        Overriden from the base class
        """
        logger.info('Connecting Loopback transport %s', self)
        self._connected = True

    async def disconnect(self):
        """
        Overriden from the base class
        """
        logger.info('Disconnecting Loopback transport %s', self)
        self._connected = False

    def is_connected(self):
        """
        Overriden from the base class
        """
        return self._connected

    async def send_request(self, name, content, timeout=None):
        """
        Overriden from the base class

        :raises UnknownRequestName: if no callback is currently registered for ``name``
        """
        logger.info('Sending a request %s using Loopback transport', name)
        callback = self._request_callbacks.get(name)
        if callback is None:
            raise UnknownRequestName(name)
        try:
            if timeout is not None:
                return await asyncio.wait_for(callback(content), timeout=timeout)
            return await callback(content)
        except asyncio.TimeoutError:
            logger.warning('Timed out waiting for a response to %s', name)
            raise RequestTimeoutError(name) from None

    async def _register_request_callback(self, name, callback):
        """
        Overriden from the base class
        """
        self._request_callbacks[name] = callback

    async def _unregister_request_callback(self, name):
        """
        Overriden from the base class
        """
        self._request_callbacks.pop(name, None)

    async def publish_event(self, name, content):
        """
        Overriden from the base class
        """
        callback = self._event_callbacks.get(name)
        if callback is None:
            logger.warning('No competing consumer registered for event %s - dropping', name)
            return
        await callback(content)

    async def _register_event_callback(self, name, callback):
        """
        Overriden from the base class
        """
        self._event_callbacks[name] = callback

    async def _unregister_event_callback(self, name):
        """
        Overriden from the base class
        """
        self._event_callbacks.pop(name, None)

    async def publish_broadcast(self, name, content):
        """
        Overriden from the base class
        """
        callbacks = list(self._broadcast_callbacks.get(name, ()))
        if not callbacks:
            logger.warning('No broadcast subscriber registered for %s - dropping', name)
            return
        for callback in callbacks:
            await callback(content)

    async def _register_broadcast_callback(self, name, callback):
        """
        Overriden from the base class
        """
        self._broadcast_callbacks.setdefault(name, []).append(callback)

    async def _unregister_broadcast_callback(self, name):
        """
        Overriden from the base class
        """
        callbacks = self._broadcast_callbacks.get(name)
        if callbacks is None:
            return
        self._broadcast_callbacks[name] = []


def get_transport(namespace='default'):
    """
    Get a transport for the namespace.

    :param namespace: name of the namespace the transport for
    :type namespace: str
    :returns: transport for the namespace
    :rtype: Transport
    """
    ns = namespaces.get(namespace)
    return getattr(ns, 'transport', None)
