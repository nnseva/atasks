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
    (robust) connection:

    - RPC (request/response): a topic exchange for requests plus a private
      exclusive reply-to queue per transport instance, correlated by
      ``correlation_id`` - see :meth:`send_request`/:meth:`register_callback`.
    - task-queue (fire-and-forget, competing consumers): one shared durable
      queue per task name - see :meth:`publish_event`/:meth:`register_event_callback`.
    - broadcast/subscribe (fire-and-forget, fan-out): one shared fanout
      exchange per topic name, with one exclusive auto-delete queue per
      subscribing instance - see :meth:`publish_broadcast`/:meth:`register_broadcast_callback`.

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
        prefix='atask',
        queue='atask',
        reconnect_interval=5,
        client_properties=None,
    ):
        super().__init__(namespace=namespace)
        self.url = url
        self.request_exchange_name = request_exchange
        self.response_exchange_name = response_exchange
        self.prefix = prefix
        self.queue_name = queue
        self.reconnect_interval = reconnect_interval
        self.client_properties = client_properties
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
            self._request_exchange = await self._channel.declare_exchange(
                self.request_exchange_name,
                type=aio_pika.ExchangeType.TOPIC,
                durable=True,
            )
            self._response_exchange = self._request_exchange
            if not self.response_exchange_name == self.request_exchange_name:
                self._response_exchange = await self._channel.declare_exchange(
                    self.response_exchange_name,
                    type=aio_pika.ExchangeType.TOPIC,
                    durable=True,
                )
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

    async def _on_connection_closed(self, connection, exc=None):
        """
        Called by aio_pika whenever the underlying connection is lost - including
        transient drops which the robust connection will retry (with backoff) in
        the background. Fails every RPC request currently in flight immediately,
        instead of leaving the caller hanging until (or past) reconnection.
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

    async def _on_reconnected(self, connection):
        """Called by aio_pika after the underlying connection is successfully reestablished."""
        logger.info('Reconnected transport %s', self)

    async def register_callback(self, callback):
        await self._lock.acquire()
        try:
            await super().register_callback(callback)
            self._queue = await self._channel.declare_queue(
                self.queue_name,
            )
            logger.info('Binding queue to %s', self.prefix + '.#')
            await self._queue.bind(self._request_exchange, self.prefix + '.#')

            async def _on_message(message):
                async with message.process():
                    info = message.info()
                    request = message.body
                name = info['routing_key'][len(self.prefix) + 1:]
                correlation_id = info['correlation_id']
                logger.info('Got request for %s[%s]', name, correlation_id)
                try:
                    response = await self.callback(name, request)
                except Exception:
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
                except Exception:
                    logger.exception('Failed to publish response for %s[%s]', name, correlation_id)

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
            await self._request_exchange.publish(
                aio_pika.Message(
                    correlation_id=correlation_id,
                    body=content,
                    reply_to=self._response_queue.name,
                ),
                routing_key='%s.%s' % (self.prefix, name),
            )
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

        Declares (idempotently) a durable queue named after ``name`` so an event
        published before any consumer has started isn't silently dropped, then
        publishes directly to it via the default exchange. Every instance which
        calls :meth:`register_event_callback` with the same ``name`` consumes
        from *the same* queue, so they compete for each message.
        """
        await self._lock.acquire()
        try:
            queue_name = '%s.q.%s' % (self.prefix, name)
            queue = await self._channel.declare_queue(queue_name, durable=True)
            logger.info('Publishing event for %s', name)
            await self._channel.default_exchange.publish(
                aio_pika.Message(body=content, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=queue.name,
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

        Declares (idempotently) a durable fanout exchange named after ``name``
        and publishes to it with no routing key. Every instance which calls
        :meth:`register_broadcast_callback` with the same ``name`` gets its own
        exclusive, auto-delete queue bound to this exchange, so every subscribed
        instance receives its own copy of every message.
        """
        await self._lock.acquire()
        try:
            exchange_name = '%s.fanout.%s' % (self.prefix, name)
            exchange = await self._channel.declare_exchange(
                exchange_name,
                type=aio_pika.ExchangeType.FANOUT,
                durable=True,
            )
            logger.info('Publishing broadcast for %s', name)
            await exchange.publish(
                aio_pika.Message(body=content, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key='',
            )
        finally:
            self._lock.release()

    async def register_broadcast_callback(self, name, callback):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            exchange_name = '%s.fanout.%s' % (self.prefix, name)
            exchange = await self._channel.declare_exchange(
                exchange_name,
                type=aio_pika.ExchangeType.FANOUT,
                durable=True,
            )
            queue = await self._channel.declare_queue('', exclusive=True, auto_delete=True)
            await queue.bind(exchange, routing_key='')

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
