"""
Registry Manager tests
"""

from atasks import registry

from django.test import TestCase


class ModuleTest(TestCase):
    """Module tests"""
    def test_registry_item(self):
        """Unit Test for registry item"""
        item = registry.RegistryItem(a=1, b=2)
        self.assertEqual(item.a, 1)
        self.assertEqual(item.b, 2)
        self.assertEqual(item, registry.RegistryItem(a=1, b=2))
        item.update(registry.RegistryItem(b=3, c=4))
        self.assertEqual(item, registry.RegistryItem(a=1, b=3, c=4))
        self.assertEqual(item, dict(a=1, b=3, c=4))
        self.assertNotEqual(item, dict(a=1, b=3))
        self.assertNotEqual(item, None)

    def test_002_manager_no_unite(self):
        """Unit test for manager with no unite functions"""
        manager = registry.Manager('test', unite=False)
        manager.register('the test', a=1, b=2)
        manager.register('the test2', a=3, b=4)
        manager.register('the test3', a=5, b=6)
        self.assertEqual(manager.get('the test').a, 1)
        self.assertEqual(manager.get('the test2').a, 3)
        self.assertRaises(registry.RegisteringTwice, manager.register, 'the test', x=1, y=2)
        manager.unregister('the test2')
        self.assertEqual(manager.get('the test2'), None)

    def test_003_manager_unite(self):
        """Unit test for manager with unite functions"""
        manager = registry.Manager('test')
        manager.register('the test', a=1, b=2)
        manager.register('the test2', a=3, b=4)
        manager.register('the test3', a=5, b=6)
        self.assertEqual(manager.get('the test').a, 1)
        self.assertEqual(manager.get('the test2').a, 3)
        self.assertEqual(manager.get('the test'), registry.RegistryItem(a=1, b=2))
        manager.register('the test', x=1, y=2)
        self.assertEqual(manager.get('the test'), registry.RegistryItem(a=1, b=2, x=1, y=2))
        manager.unregister('the test2')
        self.assertEqual(manager.get('the test2'), registry.RegistryItem())
