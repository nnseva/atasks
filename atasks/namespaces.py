"""
AIO Steve namespaces Manager instance
"""

from atasks.registry import Manager


class NamespaceManager(Manager):
    """The only manager of all namespaces"""
    def __init__(self):
        """
        Constructor
        """
        super().__init__('', unite=True)


namespaces = NamespaceManager()
