"""
Router tests
"""

from django.core.management import call_command
from django.test import TestCase


class ModuleTest(TestCase):
    """Module tests"""
    def test_run_atask(self):
        """Test scenarios"""
        call_command('run_atask', 'dev.tests.scenarios', verbosity=3, mode='loopback')
