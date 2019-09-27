"""
ATask Manager
"""

import logging


logger = logging.getLogger(__name__)


class RegisteringTwice(AttributeError):
    """Error registering the same name twice"""
    pass


class RegistryItem(object):
    """Item of the registry"""
    def __init__(self, **options):
        """Copies all options to it's own instance dict"""
        self.__dict__ = {
            **options
        }

    def update(self, other):
        """Equal to dict method"""
        self.__dict__.update(other.__dict__)

    def __eq__(self, other):
        """Equal to dict method"""
        if other is None:
            return False
        if isinstance(other, dict):
            return self.__dict__ == other
        return self.__dict__ == other.__dict__


class Manager(object):
    """The registry manager"""

    def __init__(self, name, unite=True):
        """
        Constructor

        :param namespace: name of the registry namespace
        :type namespace: str
        :param unite: unite registered items with the same name.
                      If unite=False, raises RegisteringTwice when
                      using the same name.
        """
        self._name = name
        self._unite = unite
        self._registry = {}

    def register(self, name, **options):
        """
        Register an item

        :param name: name of the item
        :type name: str
        :param options: options to register
        :type options: dict
        """
        item = RegistryItem(**options)
        old = self._registry.get(name, None)
        if old:
            if self._unite:
                old.update(item)
                return
            raise RegisteringTwice("Registering twice in %s: %s" % (self._name, name))
        self._registry[name] = item

    def unregister(self, name):
        """
        Unregister the item

        :param name: name of the item
        :type name: str
        """
        logger.debug('unregistering from %s: %s', self._name, name)
        del self._registry[name]

    def get(self, name):
        """
        Get a register item by name

        :param name: name of the item
        :type name: str
        :returns: Item of the registry or default value
        :rtype: RegistryItem
        """
        ret = self._registry.get(name, None)
        if not ret and self._unite:
            ret = RegistryItem()
            self._registry[name] = ret
        return ret
