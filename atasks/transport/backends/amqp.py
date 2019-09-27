import asyncio
import logging
import uuid

import aio_pika
from atasks.transport.base import Transport


logger = logging.getLogger(__name__)


class AMQPTransport(Transport):
    """
    AMQP transport which uses AMQP for enqueue requests and receive responces
    """

    def __init__(
        self,
        namespace='default',
        url='amqp://localhost/',
        request_exchange='atask',
        response_exchange='atask',
        prefix='atask',
        queue='atask',
    ):
        super().__init__(namespace=namespace)
        self.url = url
        self.request_exchange_name = request_exchange
        self.response_exchange_name = response_exchange
        self.prefix = prefix
        self.queue_name = queue
        self._lock = asyncio.Lock()
        self._awaiting_requests = {}

    async def unregister_callback(self):
        await self._lock.acquire()
        try:
            await self._queue.cancel(self._consumer)
            del self._queue
            del self._consumer
            await super().unregister_callback()
        finally:
            self._lock.release()

    async def disconnect(self):
        await self._lock.acquire()
        try:
            await self._connection.close()
            del self._connection
            del self._channel
            del self._request_exchange
            del self._response_exchange
            del self._response_queue
            del self._response_consumer
        finally:
            self._lock.release()

    async def connect(self):
        loop = asyncio.get_event_loop()
        await self._lock.acquire()
        if hasattr(self, '_connection'):
            return
        try:
            logger.info('Connecting transport %s', self)
            self._connection = await aio_pika.connect_robust(self.url, loop=loop)
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
                logger.info('Got response for [%s]', correlation_id)
                future = self._awaiting_requests[correlation_id]
                future.set_result(response)

            self._response_consumer = await self._response_queue.consume(_on_response_message)
        finally:
            self._lock.release()

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
                response = await self.callback(name, request)

                logger.info('Publishing result for %s[%s]', name, correlation_id)
                await self._response_exchange.publish(
                    aio_pika.Message(
                        correlation_id=correlation_id,
                        body=response
                    ),
                    routing_key=info['reply_to'],
                )

            self._consumer = await self._queue.consume(_on_message)
        finally:
            self._lock.release()
        logger.info('Callback registered %s', callback)

    async def send_request(self, name, content):
        """
        Overriden from the base class
        """
        await self._lock.acquire()
        try:
            correlation_id = uuid.uuid4().hex  # probably not unique but with almost zero probability
            future = asyncio.Future()
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
        ret = await future
        logger.debug('Got a result for %s[%s]', name, correlation_id)
        del self._awaiting_requests[correlation_id]
        return ret
