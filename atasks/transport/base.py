"""
ATasks Base Transport module
"""

import logging

from atasks.namespaces import namespaces


logger = logging.getLogger(__name__)


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
        self.callback = None

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

    async def send_request(self, name, content):
        """
        Send a request to a service

        :param name: target name to be sent
        :type name: str
        :param content: request to be sent
        :type content: bytes
        :returns: response to the request
        :rtype: bytes
        """
        raise NotImplementedError()

    async def register_callback(self, callback):
        """
        Register a callback to receive requests.

        The transport may receive requests only when the callback is registered.

        Can be used to override in ancestor, to avoid receiving
        requests when the callback is not registered (yet or already).

        :param callback: callback to be called on the request received,
                        it gets a request content and returnes a response
                        content as bytes serialized by the namespace codec
        :type callback: awaitable(name: str, content: bytes): bytes
        """
        logger.info("Registering a callback for %s in %s: [%s]", self, self.namespace, callback)
        self.callback = callback

    async def unregister_callback(self):
        """
        Unregister previously registered callback if present

        The transport may receive requests only when the callback is registered.

        Can be used to override in ancestor, to avoid receiving
        requests when the callback is not registered (yet or already).
        """
        logger.info("Unregistering a callback for %s in %s", self, self.namespace)
        self.callback = None


class LoopbackTransport(Transport):
    """
    Loopback transport which requests own callback with bytes sent to him
    """
    async def connect(self):
        """
        Overriden from the base class
        """
        logger.info('Connecting Loopback transport %s', self)

    async def disconnect(self):
        """
        Overriden from the base class
        """
        logger.info('Disconnecting Loopback transport %s', self)

    async def send_request(self, name, content):
        """
        Overriden from the base class
        """
        logger.info('Sending a request %s using Loopback transport', name)
        try:
            return await self.callback(name, content)
        except Exception as ex:
            logger.error('Error while calling a callback: %s', ex)


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
