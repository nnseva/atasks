import asyncio
import logging
import uuid

import aio_pika

from atasks.transport.base import (
    ConnectionLostError,
    RequestTimeoutError,
    Transport,
)


logger = logging.getLogger(__name__)


class AMQPTransport(Transport):
    """
    AMQP transport which uses AMQP for enqueue requests and receive responces

    Implements all three atasks patterns on top of a single ``aio_pika``
    (robust) connection. Every interaction is declared as a durable **topic**
    exchange, and it's the routing key alone - never the exchange identity -
    that keeps the four kinds of traffic apart: ``request_exchange``,
    ``response_exchange``, ``event_exchange`` and ``broadcast_exchange`` all
    default to the same name (``'atask'``) and are perfectly happy to resolve
    to one physical exchange, because each pattern publishes under its own
    reserved routing-key namespace (``<prefix>.r.``, ``<prefix>.e.``,
    ``<prefix>.b.`` respectively; the reply-to queue's own random name plays
    the same role for responses). Point any of them at a different name and
    nothing else has to change - the routing keys stay just as distinct.

    - RPC (request/response): requests are published with routing key
      ``<prefix>.r.<name>``; the worker queue binds ``<prefix>.r.#`` so one
      queue serves every request name. Responses go out on a private
      exclusive reply-to queue per transport instance, bound to its own
      (random) queue name, correlated by ``correlation_id`` - see
      :meth:`send_request`/:meth:`register_callback`.
    - task-queue (fire-and-forget, competing consumers): events are published
      with routing key ``<prefix>.e.<name>``; one shared durable queue per
      task name binds that exact key, so every instance which calls
      :meth:`register_event_callback` with the same ``name`` competes for
      messages off *the same* queue - see
      :meth:`publish_event`/:meth:`register_event_callback`.
    - broadcast/subscribe (fire-and-forget, fan-out): broadcasts are published
      with routing key ``<prefix>.b.<name>``; every subscribing instance binds
      its own exclusive, auto-delete queue to that exact key, so every
      subscribed instance gets its own copy of every message - see
      :meth:`publish_broadcast`/:meth:`register_broadcast_callback`.

    Uses ``aio_pika.connect_robust``, so the underlying connection
    automatically reconnects (with ``reconnect_interval`` backoff) after the
    broker becomes unreachable, transparently re-declaring exchanges, queues
    and consumers. Any RPC request in flight at the moment the connection
    drops is failed immediately with :class:`ConnectionLostError` instead of
    hanging until (or past) reconnection - the caller is expected to retry
    (e.g. by stacking ``backoff.on_exception`` on the call site) rather than
    the library silently retrying on its behalf.
    """

    def __init__(
        self,
        namespace='default',
        url='amqp://localhost/',
        request_exchange='atask',
        response_exchange='atask',
        event_exchange='atask',
        broadcast_exchange='atask',
        prefix='atask',
        queue='atask',
        reconnect_interval=5,
        client_properties=None,
    ):
        super().__init__(namespace=namespace)
        self.url = url
        self.request_exchange_name = request_exchange
        self.response_exchange_name = response_exchange
        self.event_exchange_name = event_exchange
        self.broadcast_exchange_name = broadcast_exchange
        self.prefix = prefix
        self.queue_name = queue
        self.reconnect_interval = reconnect_interval
        self.client_properties = client_properties
        # Reserved per-pattern routing-key namespaces - see the class
        # docstring for why these are what keep request/event/broadcast
        # traffic apart even when their exchanges resolve to the same name.
        self._request_routing_prefix = '%s.r.' % prefix
        self._event_routing_prefix = '%s.e.' % prefix
        self._broadcast_routing_prefix = '%s.b.' % prefix
        self._lock = asyncio.Lock()
        self._awaiting_requests = {}
        self._event_queues = {}
        self._event_consumers = {}
        self._broadcast_queues = {}
        self._broadcast_consumers = {}

    async def unregister_callback(self):
        await self._lock.acquire()
        try:
            if hasattr(self, '_queue') and hasattr(self, '_connection') and not self._connection.is_closed:
                # Cancelling a consumer is itself an RPC round-trip through the
                # channel - if the connection is already gone (e.g. disconnect()
                # was called first, or the broker dropped us) this call would
                # otherwise hang forever waiting for a reply that can never
                # arrive. Skip it in that case: there is nothing left to cancel.
                await self._queue.cancel(self._consumer)
            if hasattr(self, '_queue'):
                del self._queue
            if hasattr(self, '_consumer'):
                del self._consumer
            await super().unregister_callback()
        finally:
            self._lock.release()

    async def disconnect(self):
        await self._lock.acquire()
        try:
            if not hasattr(self, '_connection'):
                return
            await self._connection.close()
            del self._connection
            del self._channel
            del self._request_exchange
            del self._response_exchange
            del self._event_exchange
            del self._broadcast_exchange
            del self._response_queue
            del self._response_consumer
            self._event_queues.clear()
            self._event_consumers.clear()
            self._broadcast_queues.clear()
            self._broadcast_consumers.clear()
        finally:
            self._lock.release()

    async def connect(self):
        loop = asyncio.get_event_loop()
        await self._lock.acquire()
        try:
            if hasattr(self, '_connection'):
                return
            logger.info('Connecting transport %s', self)
            self._connection = await aio_pika.connect_robust(
                self.url,
                loop=loop,
                reconnect_interval=self.reconnect_interval,
                client_properties=self.client_properties,
            )
            self._connection.close_callbacks.add(self._on_connection_closed)
            self._connection.reconnect_callbacks.add(self._on_reconnected)
            self._channel = await self._connection.channel()

            # All four exchanges are plain durable topics, and their names are
            # free to coincide (they default to the same 'atask') - it's the
            # reserved routing-key namespace per pattern, not exchange
            # identity, that keeps request/response/event/broadcast traffic
            # apart (see the class docstring). Declaring the same name twice
            # is harmless (idempotent, same type/durable both times), but the
            # cache avoids the redundant round-trip in the common case where
            # they do coincide.
            declared_exchanges = {}

            async def _get_exchange(name):
                if name not in declared_exchanges:
                    declared_exchanges[name] = await self._channel.declare_exchange(
                        name,
                        type=aio_pika.ExchangeType.TOPIC,
                        durable=True,
                    )
                return declared_exchanges[name]

            self._request_exchange = await _get_exchange(self.request_exchange_name)
            self._response_exchange = await _get_exchange(self.response_exchange_name)
            self._event_exchange = await _get_exchange(self.event_exchange_name)
            self._broadcast_exchange = await _get_exchange(self.broadcast_exchange_name)
            self._response_queue = await self._channel.declare_queue(
                '', exclusive=True,
            )
            await self._response_queue.bind(self._response_exchange, self._response_queue.name)
            await self._channel.set_qos(prefetch_count=1)

            async def _on_response_message(message):
                async with message.process():
                    info = message.info()
                    response = message.body
                correlation_id = info['correlation_id']
                future = self._awaiting_requests.pop(correlation_id, None)
                if future is None:
                    logger.warning(
                        'Got a response for an unknown or already finished request [%s] - discarding',
                        correlation_id,
                    )
                    return
                logger.info('Got response for [%s]', correlation_id)
                if not future.done():
                    future.set_result(response)

            self._response_consumer = await self._response_queue.consume(_on_response_message)
        finally:
            self._lock.release()

    def _fail_awaiting_requests(self, exc=None):
        """
        Fail every RPC request currently in flight with :class:`ConnectionLostError`,
        instead of leaving them to hang until (or past) some future reconnect.

        This is the one place that knows how to give up on in-flight requests, so
        every publish path calls it directly the moment *it* notices the connection
        is unusable - instead of only reacting to it secondhand, once (and if)
        :meth:`_on_connection_closed` gets around to it. A publish can fail with a
        raw aiormq/aio-pika exception (``ChannelInvalidStateError``,
        ``AMQPConnectionError``, a bare ``CancelledError`` from a write racing the
        teardown, ...) well before that callback runs, or even without it ever
        running at all - calling this directly, right there, guarantees the
        caller always sees the one documented exception type regardless of which
        code path noticed the connection was gone first. Calling it twice for the
        same drop (e.g. once from a failed publish and again from the callback) is
        harmless: whichever runs second finds nothing left to fail.
        """
        if not self._awaiting_requests:
            return
        logger.warning(
            'Connection lost for %s (%r) - failing %d in-flight request(s)',
            self, exc, len(self._awaiting_requests),
        )
        pending, self._awaiting_requests = self._awaiting_requests, {}
        for correlation_id, future in pending.items():
            if not future.done():
                future.set_exception(ConnectionLostError(
                    'AMQP connection lost while awaiting a response [%s]: %r' % (correlation_id, exc)
                ))

    async def _on_connection_closed(self, connection, exc=None):
        """
        Called by aio_pika whenever the underlying connection is lost - including
        transient drops which the robust connection will retry (with backoff) in
        the background. Fails every RPC request currently in flight immediately,
        instead of leaving the caller hanging until (or past) reconnection.
        """
        self._fail_awaiting_requests(exc)

    async def _on_reconnected(self, connection):
        """Called by aio_pika after the underlying connection is successfully reestablished."""
        logger.info('Reconnected transport %s', self)

    async def register_callback(self, callback):
        await self._lock.acquire()
        try:
            await super().register_callback(callback)
            self._queue = await self._channel.declare_queue(
                self.queue_name,
                durable=True,
            )
            binding_key = self._request_routing_prefix + '#'
            logger.info('Binding queue to %s', binding_key)
            await self._queue.bind(self._request_exchange, binding_key)

            async def _on_message(message):
                async with message.process():
                    info = message.info()
                    request = message.body
                name = info['routing_key'][len(self._request_routing_prefix):]
                correlation_id = info['correlation_id']
                logger.info('Got request for %s[%s]', name, correlation_id)
                try:
                    response = await self.callback(name, request)
                except Exception:
                    # TODO: should the exception here be propagated back to the caller?
                    logger.exception('Unhandled error handling request for %s[%s]', name, correlation_id)
                    return

                logger.info('Publishing result for %s[%s]', name, correlation_id)
                try:
                    await self._response_exchange.publish(
                        aio_pika.Message(
                            correlation_id=correlation_id,
                            body=response
                        ),
                        routing_key=info['reply_to'],
                    )
                except Exception as exc:
                    # TODO: should the failure to publish the response be propagated?
                    logger.exception('Failed to publish response for %s[%s]', name, correlation_id)
                    # If this transport instance is also used to make outbound
                    # requests (client and server sharing one instance) and the
                    # connection just died, don't leave those hanging either.
                    self._fail_awaiting_requests(exc)

            self._consumer = await self._queue.consume(_on_message)
        finally:
            self._lock.release()
        logger.info('Callback registered %s', callback)

    async def send_request(self, name, content, timeout=None):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            correlation_id = uuid.uuid4().hex  # probably not unique but with almost zero probability
            future = asyncio.get_event_loop().create_future()
            self._awaiting_requests[correlation_id] = future
            logger.info('Publishing for %s[%s]', name, correlation_id)
            try:
                await self._request_exchange.publish(
                    aio_pika.Message(
                        correlation_id=correlation_id,
                        body=content,
                        reply_to=self._response_queue.name,
                    ),
                    routing_key=self._request_routing_prefix + name,
                )
            except asyncio.CancelledError as exc:
                # aiormq can surface a dead connection as a bare CancelledError
                # from deep inside its own reader/writer plumbing (it uses
                # cancellation internally to unblock a write that can never
                # complete) - by exception type alone that's indistinguishable
                # from *this* task genuinely being cancelled by someone else,
                # which must never be swallowed. Task.cancelling() tells the two
                # apart: it only counts actual cancel() calls made against this
                # task, so if it's zero, nobody asked to cancel us and this can
                # only be aiormq's internal signal - handle it exactly like any
                # other publish failure below. Otherwise this is a real
                # cancellation and has to propagate untouched.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    # A real cancellation propagates untouched, but nothing past
                    # this point will run the `finally` that normally pops our
                    # own correlation_id (it's further down, after the lock is
                    # released) - clean it up here so it doesn't linger in
                    # _awaiting_requests forever if a response never arrives.
                    self._awaiting_requests.pop(correlation_id, None)
                    raise
                self._fail_awaiting_requests(exc)
            except Exception as exc:
                # The publish itself failed - almost always because the connection
                # is gone or mid-reconnect. Route it through the same cleanup
                # _on_connection_closed uses: this fails *our own* future (among
                # any other in-flight ones) with ConnectionLostError, which the
                # await below then raises immediately - no separate raise needed
                # here, and the caller sees the same exception type regardless of
                # whether the publish failed outright or the connection dropped
                # while we were waiting for a response.
                self._fail_awaiting_requests(exc)
            else:
                logger.debug('Published for %s[%s]', name, correlation_id)
        finally:
            self._lock.release()
        try:
            if timeout is not None:
                ret = await asyncio.wait_for(future, timeout=timeout)
            else:
                ret = await future
        except asyncio.TimeoutError:
            logger.warning('Timed out waiting for a response to %s[%s] after %ss', name, correlation_id, timeout)
            raise RequestTimeoutError(name) from None
        finally:
            self._awaiting_requests.pop(correlation_id, None)
        logger.debug('Got a result for %s[%s]', name, correlation_id)
        return ret

    # -- task-queue (fire-and-forget, competing consumers) ---------------------

    async def publish_event(self, name, content):
        """
        Overriden from the base class

        Publishes to ``event_exchange`` with routing key ``<prefix>.e.<name>``.
        Every instance which calls :meth:`register_event_callback` with the
        same ``name`` binds *the same* durable queue to that exact key, so
        they compete for each message. Note this means a message published
        before any consumer has ever registered for ``name`` - so no queue is
        bound to that key yet - is dropped, same as an unroutable message on
        any exchange.
        """
        await self._lock.acquire()
        try:
            routing_key = self._event_routing_prefix + name
            logger.info('Publishing event for %s', name)
            await self._event_exchange.publish(
                aio_pika.Message(body=content, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=routing_key,
            )
        finally:
            self._lock.release()

    async def register_event_callback(self, name, callback):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            queue_name = '%s.q.%s' % (self.prefix, name)
            queue = await self._channel.declare_queue(queue_name, durable=True)
            await queue.bind(self._event_exchange, self._event_routing_prefix + name)

            async def _on_event_message(message):
                async with message.process():
                    content = message.body
                try:
                    await callback(content)
                except Exception:
                    logger.exception('Unhandled error handling queue event %s', name)

            consumer_tag = await queue.consume(_on_event_message)
            self._event_queues[name] = queue
            self._event_consumers[name] = consumer_tag
        finally:
            self._lock.release()

    async def unregister_event_callback(self, name):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            queue = self._event_queues.pop(name, None)
            consumer_tag = self._event_consumers.pop(name, None)
            if queue is not None and consumer_tag is not None and hasattr(self, '_connection') \
                    and not self._connection.is_closed:
                await queue.cancel(consumer_tag)
        finally:
            self._lock.release()

    # -- broadcast/subscribe (fire-and-forget, fan-out) -------------------------

    async def publish_broadcast(self, name, content):
        """
        Overriden from the base class

        Publishes to ``broadcast_exchange`` with routing key
        ``<prefix>.b.<name>``. Every instance which calls
        :meth:`register_broadcast_callback` with the same ``name`` binds its
        own exclusive, auto-delete queue to that exact key, so every
        subscribed instance receives its own copy of every message.
        """
        await self._lock.acquire()
        try:
            routing_key = self._broadcast_routing_prefix + name
            logger.info('Publishing broadcast for %s', name)
            await self._broadcast_exchange.publish(
                aio_pika.Message(body=content, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=routing_key,
            )
        finally:
            self._lock.release()

    async def register_broadcast_callback(self, name, callback):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            queue = await self._channel.declare_queue('', exclusive=True, auto_delete=True)
            await queue.bind(self._broadcast_exchange, self._broadcast_routing_prefix + name)

            async def _on_broadcast_message(message):
                async with message.process():
                    content = message.body
                try:
                    await callback(content)
                except Exception:
                    logger.exception('Unhandled error handling broadcast event %s', name)

            consumer_tag = await queue.consume(_on_broadcast_message)
            self._broadcast_queues[name] = queue
            self._broadcast_consumers[name] = consumer_tag
        finally:
            self._lock.release()

    async def unregister_broadcast_callback(self, name):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            queue = self._broadcast_queues.pop(name, None)
            consumer_tag = self._broadcast_consumers.pop(name, None)
            if queue is not None and consumer_tag is not None and hasattr(self, '_connection') \
                    and not self._connection.is_closed:
                await queue.cancel(consumer_tag)
        finally:
            self._lock.release()
