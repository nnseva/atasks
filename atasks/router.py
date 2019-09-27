"""
AIO Steve Jobs Router
"""

import logging

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


class Router(Manager):
    """
    Router is a core atasks class which registers asynchronous tasks,
    creates remote reference functions, routes reference calls to the remote coroutines,
    etc.

    It is registered in the namespace and uses codec and transport from it.
    """
    def __init__(self, namespace='default'):
        """
        Constructor

        :param namespace: name of the namespace which the router will use to send requests
        :type namespace: str
        """
        super().__init__(namespace, unite=False)
        logger.info("Creating a router for %s", namespace)
        namespaces.register(namespace, router=self)
        self.namespace = namespace
        self.server = None

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

    async def send_request(self, name, *argv, **kwargs):
        """
        Send a request.

        Uses codec got from the namespace to encode the request content.

        Uses transport got from the namespace to send an encoded content and receive a result.

        Uses codec got from the namespace to decode the request response.

        :param name: name of the coroutine to be called
        :type name: str
        :param argv: arbitrary positional parameters
        :param kwargs: arbitrary named parameters
        :returns: success flag and job awaiting result, or exception in case of the exception handled
        """
        logger.debug('Sending request %s %s %s', name, argv, kwargs)
        client = get_transport(self.namespace)
        if not client:
            raise NoClientTransportRegistered()

        codec = get_codec(self.namespace)
        if not codec:
            raise NoCodecRegistered()

        content = await codec.encode((argv, kwargs))
        logger.debug('Sending request %s using %s', name, client)
        response = await client.send_request(name, content)
        logger.debug('Sending request %s response returned', name)
        if not response:
            raise TransportError()
        success, result = await codec.decode(response)
        logger.debug('Sending request %s response success = %s content: %s', name, success, result)
        if not success:
            raise result
        return result

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

        argv, kwargs = await codec.decode(content)
        item = self.get(name)
        if not item:
            raise JobNotFound(name)

        coro = item.coro
        options = item.options

        logger.debug('Request received %s with %s %s', name, argv, kwargs)
        success, result = await self._call_coro(coro, argv, kwargs, options)
        logger.debug('Request %s response returning success = %s: %s', name, success, result)
        response = await codec.encode((success, result))
        logger.info('Request %s response returning', name)
        return response

    async def _call_coro(self, coro, argv, kwargs, options):
        """
        Calls coroutine and returns success flag and result or exception
        """
        try:
            result = await coro(*argv, **kwargs)
        except Exception as ex:
            return False, ex

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
        self.register(name, coro=coro, options=options)

        async def aioref(*argv, **kwargs):
            result = await self.send_request(name, *argv, **kwargs)
            return result

        aioref.__qualname__ = 'ref[%s/%s]' % (name, self.namespace)
        logger.info('Registered %s', aioref)
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
