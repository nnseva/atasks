"""
AIO Steve Codec Base class
"""

import logging
import pickle

from atasks.namespaces import namespaces


logger = logging.getLogger(__name__)


class Codec(object):
    """
    Codec base class
    """
    def __init__(self, namespace='default'):
        """
        Constructor.

        :param namespace: namespace where the codec shuld be registered to work for.
        :type namespace: str
        """
        namespaces.register(namespace, codec=self)

    async def encode(self, obj):
        """
        Encode the object to bytes. Should be implemented by the ancestor.

        :param obj: any object supposed to be transported
        :type obj: any
        :returns: encoded object
        :rtype: bytes
        """
        raise NotImplementedError()

    async def decode(self, content):
        """
        Decode bytes to the object back. Should be implemented by the ancestor.

        :param content: encoded object
        :type content: bytes
        :returns: object decoded from bytes
        :rtype: any
        """
        raise NotImplementedError()


class PickleCodec(Codec):
    """
    Codec based on the python pickle serialization
    """
    def __init__(self, namespace='default', protocol=None, fix_imports=True, encoding="ASCII", errors="strict"):
        """
        Overriden from the base class.

        Parameter namespace is passed to the base class, and others are passed
        to the correspondent ones of the pickle.dumps/loads parameters. Defaults for
        ones are the same as in the documentation.
        """
        super().__init__(namespace)
        self.protocol = protocol
        self.fix_imports = fix_imports
        self.encoding = encoding
        self.errors = errors

    async def encode(self, obj):
        """
        Implementation
        """
        try:
            return pickle.dumps(obj, protocol=self.protocol, fix_imports=self.fix_imports)
        except Exception as ex:
            logger.error('Error while pickling an object: %s', ex)

    async def decode(self, content):
        """
        Implementation
        """
        try:
            return pickle.loads(content, fix_imports=self.fix_imports, encoding=self.encoding, errors=self.errors)
        except Exception as ex:
            logger.error('Error while unpickling content: %s', ex)


def get_codec(namespace='default'):
    """
    Get a codec for the namespace.

    :param namespace: name of the namespace the codec for
    :type namespace: str
    :returns: codec for the namespace
    :rtype: Codec
    """
    ns = namespaces.get(namespace)
    return getattr(ns, 'codec', None)
