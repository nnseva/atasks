"""
AIO Steve Task Jobs
"""

import logging


logger = logging.getLogger(__name__)


def atask(coro, name=None, namespace='default', **options):
    """
    Decorator for the task coroutine

    :param coro: coroutine to be decorated
    :type coro: coroutine
    :param namespace: namespace of the registry
    :type namespace: str
    :param options: additional options
    :type options: dict
    :returns: reference coroutine
    :rtype: coroutine
    """
    name = '%s.%s' % (coro.__module__, coro.__name__) if name is None else name

    from atasks.router import get_router

    logger.debug('atask: %s[%s/%s] %s', coro, name, namespace, options)
    router = get_router(namespace)
    return router.register_atask(name, coro=coro, options=options)
